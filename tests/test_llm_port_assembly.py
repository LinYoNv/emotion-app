# -*- coding: utf-8 -*-
"""装配判据：`HostPorts` 的**模型端口**必须真的接上，并且**真的能用**。

## 为什么单开这个文件

2026-09-20 实测：生产日志里有 **38 条**

```
[Token预算] LLM 调用失败: task=daily_plan|daily_review|diary
      error=宿主未接入模型调用能力（`HostPorts.llm` 为空）
```

根因是 `PluginHostService.start()` 构造 `HostPorts(...)` 时**从来没传 `llm=`**，
而 `Context.llm_generate` 第一步就检查它 → 必然抛错。

⚠️ **这个缺口此前零测试覆盖** —— 因为判据都去测"供应商管理器建好了没"，
   而"llm 端口接上了没"是**另一个**装配点。这正是本项目那条通则：

> **「模块能工作 ≠ 被接上了」；而且"接上了"有**多个**装配点，漏一个就是一条死路。**

🔴 本文件还覆盖一个**我自己引入**的真 bug（值得记住的类型）：
   我给 `_build_default_llm_port` 写了 `flatten_two_layer(store)`，
   但那个名字是在**另一个方法**里 `from ... import` 的**局部导入** ——
   在这个方法里**不可见** → `NameError` → 被 `except Exception` 捕获
   → **打一条 ERROR 后返回 None**。
   后果：服务照常启动、测试全绿、**端口却是 None**，插件的模型任务依旧全挂
   （只是错误信息从"没接"变成"配置读取失败"）。
   ⚠️ 教训：`except Exception` 兜底 + 返回 None 的写法**必须配一条判据**，
      否则它会把"我的代码写错了"伪装成"配置读不出来"。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emotion_app.web.plugin_host import PluginHostService  # noqa: E402


def _service(tmp_path: Path) -> PluginHostService:
    """造一个**未 start** 的宿主服务（只测装配辅助方法）。"""
    from emotion_app.infra.config import Config

    cfg = Config()
    cfg.plugins.data_dir = str(tmp_path / "plugins_data")
    return PluginHostService(cfg)


# ---------------------------------------------------------------------------
# ① 端口真的建出来了（不是 None）
# ---------------------------------------------------------------------------


def test_default_llm_port_is_built_when_provider_configured(mk, tmp_path):
    """🔴 核心判据：配好了供应商 → `HostPorts.llm` **必须不是 None**。

    注入 bug 方式（复现我引入的那个 `NameError`）：
       把 `_build_default_llm_port` 里 `flatten_two_layer` 的导入删掉
       → 本判据必须挂（原来它只会打一条 ERROR 然后返回 None）。
    """
    svc = _service(tmp_path)
    # 供应商配置：造一个两层结构的文件，形状照真实契约
    import json

    providers_path = tmp_path / "providers.json"
    providers_path.write_text(
        json.dumps(
            {
                "provider_sources": [
                    {
                        "id": "src1",
                        "type": "openai_chat_completion",
                        "provider_type": "chat_completion",
                        "key": ["sk-test"],
                        "api_base": "https://example.invalid/v1",
                        "timeout": 30,
                        "enable": True,
                    }
                ],
                "providers": [
                    {
                        "id": "src1/m1",
                        "provider_source_id": "src1",
                        "model": "m1",
                        "enable": True,
                    }
                ],
                "default_provider_id": "src1/m1",
            }
        ),
        encoding="utf-8",
    )

    # 装配层读配置的路径（与 `_build_providers` 同源）
    import emotion_app.web.plugin_host as ph

    orig = ph._load_provider_config
    ph._load_provider_config = lambda: json.loads(providers_path.read_text("utf-8"))  # type: ignore[assignment]
    try:
        svc._backend_getter = lambda: _FakeBackend()
        port = svc._build_default_llm_port()
    finally:
        ph._load_provider_config = orig  # type: ignore[assignment]

    assert port is not None, (
        "`HostPorts.llm` 是 None —— 插件的模型任务会全部报"
        "『宿主未接入模型调用能力』（这正是那 38 条生产 ERROR 的形态）"
    )


def test_default_llm_port_is_none_without_backend(tmp_path):
    """没有聊天后端 → **如实返回 None**（不凭空造一个能用的假端口）。"""
    svc = _service(tmp_path)
    svc._backend_getter = lambda: None
    assert svc._build_default_llm_port() is None


def test_default_llm_port_is_none_when_no_provider_configured(mk, tmp_path):
    """一个供应商都没配 → **如实返回 None**（插件应看到"没配模型"）。"""
    import json

    import emotion_app.web.plugin_host as ph

    svc = _service(tmp_path)
    svc._backend_getter = lambda: _FakeBackend()
    orig = ph._load_provider_config
    ph._load_provider_config = lambda: {  # type: ignore[assignment]
        "provider_sources": [],
        "providers": [],
        "default_provider_id": "",
    }
    try:
        assert svc._build_default_llm_port() is None
    finally:
        ph._load_provider_config = orig  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# ② 端口真的能调通（"接上了 ≠ 能用"）
# ---------------------------------------------------------------------------


async def test_default_llm_port_can_actually_generate(mk, tmp_path):
    """🔴 决定性判据：建出来的端口**真的能调通**，而不只是"不是 None"。

    ⚠️ 这是本项目反复踩的那类坑的下一层：
       「模块能工作 ≠ 被接上了」→「接上了 ≠ 能用」。
    """
    import json

    import emotion_app.web.plugin_host as ph

    svc = _service(tmp_path)
    svc._backend_getter = lambda: _FakeBackend(content="端口答复")
    orig = ph._load_provider_config
    ph._load_provider_config = lambda: {  # type: ignore[assignment]
        "provider_sources": [
            {
                "id": "src1",
                "type": "openai_chat_completion",
                "provider_type": "chat_completion",
                "key": ["sk-test"],
                "api_base": "https://example.invalid/v1",
                "timeout": 30,
            }
        ],
        "providers": [
            {"id": "src1/m1", "provider_source_id": "src1", "model": "m1"}
        ],
        "default_provider_id": "src1/m1",
    }
    try:
        port = svc._build_default_llm_port()
        assert port is not None
        out = await port.generate(prompt="ping")
    finally:
        ph._load_provider_config = orig  # type: ignore[assignment]

    assert getattr(out, "content", None) == "端口答复", out


# ---------------------------------------------------------------------------
# ③ 装配点：`start()` 真的把 llm 传进 HostPorts 了
# ---------------------------------------------------------------------------


def test_start_passes_llm_port_into_host_ports():
    """🔴 装配判据：`start()` 里构造 `HostPorts(...)` 必须带 `llm=`。

    这条守的是最初那 38 条 ERROR 的**直接根因** ——
    端口建得再好，`start()` 不传进去照样是 None。

    用**源码级**断言（不是行为断言）：因为 `start()` 需要真加载 11 个插件，
    在单测里代价太大且有副作用；而这个缺口的形态就是"少写一个关键字参数"，
    源码断言恰好精确覆盖它，且**能失败**（删掉 `llm=llm_port,` 即挂）。
    """
    src = (
        Path(__file__).resolve().parent.parent
        / "emotion_app"
        / "web"
        / "plugin_host.py"
    ).read_text(encoding="utf-8")

    assert "llm=llm_port" in src, (
        "`start()` 构造 HostPorts 时没有传 `llm=` —— "
        "这正是 38 条『宿主未接入模型调用能力』的根因"
    )
    assert "llm_port = self._build_default_llm_port()" in src, (
        "没有调用 `_build_default_llm_port()` 取端口"
    )


class _FakeBackend:
    """形状照 `ChatBackend.complete`（够 `UpstreamLLMPort` 用）。"""

    def __init__(self, content: str = "答复") -> None:
        self.content = content

    async def complete(self, *, messages=None, model=None, tools=None, **kw):  # noqa: ANN001
        return type("R", (), {"content": self.content, "usage": {}})()


# ---------------------------------------------------------------------------
# ④ 投递端口：两条路径都要接上
# ---------------------------------------------------------------------------


def test_start_passes_delivery_into_host_ports_and_platform_manager():
    """🔴 装配判据：`delivery` 必须同时接进 `HostPorts` **和** 平台管理器。

    插件不同调用点走不同路，**只接一个 = 一半调用点仍走不通**：

    | 路径 | 插件的调用 | 依赖 |
    |---|---|---|
    | A | `context.send_message(umo, chain)` | `HostPorts.delivery` |
    | B | `platform.send_by_session(session, chain)` | 平台管理器里的端口 |

    源码级断言（`start()` 要真加载 11 个插件，单测代价太大）；
    缺口的形态就是"少写一个关键字参数"，源码断言恰好精确覆盖且**能失败**。
    """
    src = (
        Path(__file__).resolve().parent.parent
        / "emotion_app" / "web" / "plugin_host.py"
    ).read_text(encoding="utf-8")

    assert "delivery=delivery," in src, (
        "`HostPorts(...)` 没有传 `delivery=` —— 插件路径 A（context.send_message）仍断"
    )
    # ⚠️ 断言形式在 2026-09-21 改过：平台管理器现在**经平台注册表**装配
    #    （情绪要求「别写死了，以后可能会加入 onebot 机器人」），
    #    那一行于是变成跨行的
    #    `build_platform_manager(\n    registry=registry, delivery=delivery\n)`。
    #    → 不再断言"整行字面量"，改成断言**那次调用里两个关键字都在**
    #      （检测力不变：漏传 `delivery=` 照样红，漏传 `registry=` 也红）。
    #    🔴 别为了变绿把断言删掉 —— 那会同时丢掉"投递端口没接上"的检测力。
    assert "build_platform_manager(" in src, "平台管理器根本没装配"
    _call_start = src.index("build_platform_manager(")
    _call_src = src[_call_start : _call_start + 400]
    assert "delivery=delivery" in _call_src, (
        "平台管理器没有拿到投递端口 —— 插件路径 B（send_by_session）仍断"
    )
    assert "registry=" in _call_src, (
        "平台管理器没接平台注册表 —— 多平台（onebot）无法装配"
    )
    assert "delivery = self._build_delivery()" in src, "没有调用 `_build_delivery()`"


def test_build_delivery_returns_none_without_sessions():
    """没有会话仓库 → **如实返回 None**（不造假的"能投递"端口）。"""
    svc = _service(Path("/tmp"))
    svc.sessions = None
    assert svc._build_delivery() is None


def test_build_delivery_returns_port_with_sessions():
    """有会话仓库 → 造出端口（不能是 None）。"""
    svc = _service(Path("/tmp"))
    svc.sessions = object()
    assert svc._build_delivery() is not None


def test_delivery_port_satisfies_the_protocol():
    """造出来的端口必须**有 `send`**（`DeliveryPort` 契约）。

    注入 bug：把 `SessionDelivery` 的 `send` 改名 → 本判据必须挂。
    """
    svc = _service(Path("/tmp"))
    svc.sessions = object()
    port = svc._build_delivery()
    assert callable(getattr(port, "send", None)), (
        "投递端口没有 `send` —— 插件的 `context.send_message` 会 AttributeError"
    )
