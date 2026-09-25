# -*- coding: utf-8 -*-
"""装配测试：验证各能力**真的被接进应用**。

🔴 为什么需要这组测试（实测踩到的坑）：
   写完 `summarizer.py` 与它的 17 项单测（全绿），
   但**忘了在 `__main__.py` 里 `bind_summarizer`** →
   自动总结在真实运行时**完全没生效**，而测试全过。

   **判据：模块级单测证明"它能工作"，装配测试证明"它被接上了"。**
   两者缺一不可 —— 只测模块会漏掉"忘记装配"这类最隐蔽的失效。
"""
from __future__ import annotations

import inspect

import pytest

from emotion_app import __main__ as cli


def test_summarizer_is_bound_in_entrypoint():
    """⭐ 回归：`__main__` 必须绑定总结器。"""
    src = inspect.getsource(cli)
    assert "bind_summarizer" in src, "入口必须绑定 Summarizer（否则自动总结不生效）"
    assert "Summarizer(" in src


def test_sessions_bound_in_entrypoint():
    src = inspect.getsource(cli)
    assert "bind_sessions" in src


def test_tools_bound_in_entrypoint():
    """工具（含下发方式）必须装配 —— 同上，光有模块不算。"""
    src = inspect.getsource(cli)
    assert "bind_tools" in src
    assert "build_dispatch" in src, "下发方式必须装配（否则工具协议不生效）"
    assert "register_image_tool" in src


def test_image_backend_assembled():
    src = inspect.getsource(cli)
    assert "OpenAIImageBackend" in src
    assert "ImageToolService" in src


def test_app_receives_all_services():
    """`create_app` 必须收到全部服务（人设/设置/会话）。"""
    src = inspect.getsource(cli)
    for kw in ("personas=", "settings=", "sessions="):
        assert kw in src, f"create_app 少了 {kw}"


def test_background_tasks_are_held():
    """🔴 后台任务必须被强引用持有（asyncio 只持弱引用，否则会被 GC）。"""
    from emotion_app.domain.chat import ChatService
    from emotion_app.infra.config import Config

    svc = ChatService(Config())
    assert hasattr(svc, "_bg_tasks")
    assert isinstance(svc._bg_tasks, set)


def test_chat_service_has_summarize_hook():
    from emotion_app.domain.chat import ChatService
    from emotion_app.infra.config import Config

    svc = ChatService(Config())
    assert callable(getattr(svc, "bind_summarizer", None))
    assert callable(getattr(svc, "schedule_summarize", None))


def test_schedule_summarize_is_sync():
    """`schedule_summarize` 必须是**同步**方法 —— 它是"丢到后台"，不是 await。

    ⚠️ 若它是 async，调用方 `await` 就变成阻塞了（与设计意图相反）。
    """
    from emotion_app.domain.chat import ChatService

    assert not inspect.iscoroutinefunction(ChatService.schedule_summarize)


# ---------------------------------------------------------------------------
# 模型供应商（P2）—— "模块能工作 ≠ 被接上了"
# ---------------------------------------------------------------------------
def test_provider_backend_is_passed_to_create_app():
    """🔴 上游客户端必须传给 `create_app`。

    为什么必须钉这条：供应商要打到**同一个上游**。忘了传 →
       供应商照建但 `llm=None` → 插件调 `text_chat` **抛错**
       → 记忆抽取/日程全废。而单测全绿（它们自己注入了 backend）。
    ⚠️ 这正是本项目踩过的坑：`summarizer` 写完忘了在 `__main__` 绑线。
    """
    src = inspect.getsource(cli)
    assert "chat_backend=" in src, (
        "`__main__` 必须把上游客户端作为 `chat_backend=` 传给 create_app —— "
        "否则插件的模型调用没有出口"
    )


def test_provider_host_gets_backend_getter():
    """宿主服务必须拿到"取上游客户端"的回调（供应商装配时用）。

    ⚠️ 位置在 **`app.py` 的挂载插件路由处**（不是 `__main__`）——
       我第一版写成了查 `cli`，结果假失败。
       **通则：断言失败先查"我找的地方对不对"**（本项目已踩过两次同类）。
    """
    from emotion_app.web import app as webapp

    src = inspect.getsource(webapp)
    assert "_backend_getter" in src, (
        "要把上游客户端接到插件宿主上，否则供应商拿不到模型出口"
    )
    assert "EMOTION_CHAT_BACKEND" in src, "上游客户端要挂在 app.config 上供宿主取用"


def test_provider_bridge_is_wired_in_host_service():
    """`plugin_host` 装配时必须真的建供应商管理器并注入 HostPorts。

    ⚠️ 只 import 不注入 = 没接上（本项目最贵的 bug 类型）。
    """
    from emotion_app.web import plugin_host as ph

    src = inspect.getsource(ph)
    assert "build_provider_manager" in src, "宿主服务里没调用供应商装配"
    assert "provider_manager=provider_manager" in src, (
        "供应商管理器必须注入 HostPorts，否则 `context.provider_manager` 是 None"
    )


def test_provider_config_path_is_ours_not_astrbot():
    """🔴 供应商配置必须落在**我们的**数据目录，绝不写 AstrBot 生产目录。

    ⚠️ 检查方式要**看代码而不是看文本**：`plugin_host.py` 的文档字符串里
       有一句"绝不往 /opt/astrbot/data/ 里写东西"——纯文本扫描会把它当成违规。
       我第一版就是这么假失败的。→ 改成**去掉注释与字符串后**再查。
    """
    import io
    import tokenize

    from emotion_app.web import plugin_host as ph

    src = inspect.getsource(ph)
    # 用 tokenize 剥掉注释与字符串字面量，只留真正的代码 token
    code_only = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        code_only.append(tok.string)
    code = " ".join(code_only)

    assert "/opt/astrbot" not in code, (
        "**代码里**出现了 /opt/astrbot 路径 —— 那是别人正在跑的 AstrBot 生产目录，"
        "绝不能写（注释里提到它没问题）"
    )
    # 正例：配置路径应该是我们的相对目录
    assert "data/providers.json" in src or "providers_store" in src, (
        "供应商配置应落在我们自己的 data/ 目录"
    )


def test_persona_reference_is_wired_from_config():
    """🔴 人设固定参考图必须**从配置接到服务**上（2026-09-25 加）。

    ⚠️ 为什么单独守这一条：`.env` 里有 `EMOTION_IMAGE_PERSONA_REFERENCE`、
    `ImageConfig` 也认它，**但入口那句 `persona_reference=...` 一旦漏了，
    整个功能就是死的，而且没有任何判据会红** —— 用户只会觉得"怎么画得还是不像"。
    这正是本文件开头说的那类失效（模块能工作 ≠ 被接上了）。
    """
    src = inspect.getsource(cli)
    assert "persona_reference=config.image.persona_reference" in src, (
        "入口没把 config.image.persona_reference 传给 ImageToolService —— "
        "配置白填，selfie 永远不带人设参考图"
    )


def test_persona_reference_env_key_is_wired():
    """🔴 env 键名 `EMOTION_IMAGE_PERSONA_REFERENCE` 必须真的绑到 `image.persona_reference`。

    ⚠️ 只测"入口传了字段"是**不够的**（注入 ⑧ 证明过）：配置的 env 绑定若漏了，
       入口那句照样在、服务照样建，但**用户配 `.env` 永远不生效**。
       → 从**真实 `.env` 形态**出发端到端验一次。
    """
    from emotion_app.infra.config import load_config

    cfg = load_config({
        "EMOTION_AUTH_ACCESS_TOKEN": "t" * 32,
        "EMOTION_IMAGE_PERSONA_REFERENCE": "/tmp/wired-probe.png",
    })
    assert cfg.image.persona_reference == "/tmp/wired-probe.png", (
        "EMOTION_IMAGE_PERSONA_REFERENCE 没有绑到 image.persona_reference —— "
        "用户在 .env 里配了也不生效（且不报错）"
    )
    cfg2 = load_config({"EMOTION_AUTH_ACCESS_TOKEN": "t" * 32})
    assert cfg2.image.persona_reference == ""
