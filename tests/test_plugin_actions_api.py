# -*- coding: utf-8 -*-
"""面板插件**动作面**的 HTTP 判据：重载 / 删除（移入回收站）/ README。

契约见 `docs/PLUGIN_ACTIONS_CONTRACT.md` §2/§3/§4。

## 为什么单独一份装配级判据

`host/tests/test_plugin_lifecycle.py` 验的是**宿主内部**语义（拆解是否干净、
回收站内容是否一致、锁是否生效）。那套验证**证明不了路由挂没挂上、鉴权有没有漏**——
而这两件事正是本项目最贵的事故形态：

* 真实事故①：新增路由漏了 `require_auth()`，服务绑 `0.0.0.0`
  ⇒ **公网可读运行日志**；
* 真实事故②：模块能工作、单测全绿，但**入口没绑线** ⇒ 功能没生效。

所以这里只走 HTTP，从"客户端能看见什么"这个角度验。
⚠️ 判据**必须验反向**：未鉴权 401、未知插件 404、停用插件重载 409。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
HOST_DIR = APP_ROOT / "host"
TOKEN = "test-token-for-plugin-actions-01"

for p in (str(APP_ROOT), str(HOST_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from emotion_app.infra.config import Config  # noqa: E402
from emotion_app.storage.factory import build_store  # noqa: E402
from emotion_app.web.app import create_app  # noqa: E402
from emotion_app.web.auth import AuthManager  # noqa: E402

#: 一个**会注册东西**的插件：handler + LLM 工具 + Web API。
#: 只有"注册了几样"的插件，重载判据里"计数 > 0"才有意义
#: （注册 0 样时 0 > 0 恒假，判据会变成永远失败）。
PLUGIN_TEMPLATE = """\
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


class Probe(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        context.register_web_api("probe/page/ping", self.ping, ["GET"], "判据用")

    @filter.command("回声")
    async def echo_cmd(self, event: AstrMessageEvent, word: str = "默认"):
        yield event.plain_result(f"收到:{word}")

    @filter.llm_tool(name="probe_lookup")
    async def lookup(self, event: AstrMessageEvent, query: str):
        \"\"\"判据用工具
        Args:
            query(string): 查询词
        \"\"\"
        return "ok"

    async def ping(self):
        return {"ok": True}

    async def initialize(self) -> None:
        return None

    async def terminate(self) -> None:
        return None
"""


def _make_plugin(root: Path, name: str, *, readme: str | None = None) -> Path:
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "metadata.yaml").write_text(
        f"name: {name}\nversion: 1.0\nauthor: t\ndesc: 判据用\n", encoding="utf-8"
    )
    (pkg / "main.py").write_text(PLUGIN_TEMPLATE, encoding="utf-8")
    if readme is not None:
        (pkg / "README.md").write_text(readme, encoding="utf-8")
    return pkg


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.plugin_root = tmp_path / "plugins"
        self.plugin_root.mkdir(parents=True, exist_ok=True)
        self.with_readme = _make_plugin(
            self.plugin_root, "act_probe_a", readme="# 判据用文档\n\n正文内容。\n"
        )
        self.without_readme = _make_plugin(self.plugin_root, "act_probe_b")
        self.profile_path = tmp_path / "data" / "config_profile.json"
        #: 插件**数据**根：卸载判据要看它是否被保留
        self.data_dir = tmp_path / "plugin_data"

    def cfg(self) -> Config:
        cfg = Config()
        cfg.auth.access_token = TOKEN
        cfg.storage.sqlite_path = str(self.tmp_path / "t.db")
        cfg.plugins.enabled = True
        cfg.plugins.mount_web_apis = True
        cfg.plugins.plugin_dirs = str(self.plugin_root)
        cfg.plugins.data_dir = str(self.data_dir)
        cfg.plugins.source_mirror_dir = str(self.tmp_path / "plugin_src")
        cfg.plugins.auto_initialize = False
        # 🔴 **镜像必须关掉**：卸载判据要验证"源码目录被移走"，
        #    开镜像时 `plugin_dirs` 指向副本，源码根本不动（见
        #    `plugin_lifecycle.resolve_source_dir` 的说明）。
        cfg.plugins.mirror_sources = False
        cfg.validate()
        return cfg

    async def app(self, *, start: bool = True):
        cfg = self.cfg()
        store = build_store(cfg.storage)
        await store.open()
        app = create_app(cfg, store, AuthManager(access_token=TOKEN))
        app.config["EMOTION_CONFIG_PROFILE_PATH"] = str(self.profile_path)
        svc = app.config["EMOTION_PLUGIN_HOST"]
        if start:
            await svc.start()
        return app, store, svc


def _reset_process_globals() -> None:
    """清空宿主的**进程级单例注册表**（等价模拟新进程）。

    🔴 不清的话，同一 pytest 进程里第二次 `start()` 会看到上一次的 handler，
       判据变成假红/假绿。详见 `tests/test_plugin_activation_api.py` 的同名函数。
    """
    from astrbot_host.compat.core.star.base import star_map, star_registry
    from astrbot_host.registry import llm_tools, star_handlers_registry

    star_handlers_registry.clear()
    star_map.clear()
    star_registry.clear()
    llm_tools.clear()


@pytest.fixture
def hz(tmp_path):
    _reset_process_globals()
    yield Harness(tmp_path)
    _reset_process_globals()


async def _client(app, *, login: bool = True):
    c = app.test_client()
    if login:
        await c.post("/api/v1/auth/login", json={"token": TOKEN})
    return c


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# ① 鉴权 —— 三条路由都必须拦（本项目真事故：漏了 `require_auth()`）
# ===========================================================================


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/v1/plugins/act_probe_a/readme"),
        ("post", "/api/v1/plugins/act_probe_a/reload"),
        ("delete", "/api/v1/plugins/act_probe_a"),
    ],
)
def test_all_three_routes_require_auth(hz, method, path):
    """无令牌 → 401。**三条都要测**，不能只测一条（漏的往往是最新加的那条）。"""

    async def run():
        app, store, _svc = await hz.app()
        try:
            c = await _client(app, login=False)
            r = await getattr(c, method)(path, json={})
            assert r.status_code == 401, f"{method.upper()} {path} 没拦住未鉴权请求"
        finally:
            await store.close()

    _run(run())


# ===========================================================================
# ② README（契约 §4）
# ===========================================================================


def test_readme_returns_content(hz):
    async def run():
        app, store, _svc = await hz.app()
        try:
            c = await _client(app)
            r = await c.get("/api/v1/plugins/act_probe_a/readme")
            assert r.status_code == 200
            body = await r.get_json()
            assert "判据用文档" in body["readme"]
            assert body["truncated"] is False
        finally:
            await store.close()

    _run(run())


def test_readme_missing_is_200_with_null_not_404(hz):
    """没有 README → **200 + null**，不是 404。

    ⚠️ 404 会让前端显示"这个插件不存在" —— 而它明明存在，只是没写文档。
    """

    async def run():
        app, store, _svc = await hz.app()
        try:
            c = await _client(app)
            r = await c.get("/api/v1/plugins/act_probe_b/readme")
            assert r.status_code == 200, "没有 README 不该是 404"
            body = await r.get_json()
            assert body["readme"] is None
            assert body["truncated"] is False
        finally:
            await store.close()

    _run(run())


def test_readme_unknown_plugin_is_404_with_available_list(hz):
    """未知插件 → 404，并给出**可用 id 清单**（否则用户不知道拼错了哪个字）。"""

    async def run():
        app, store, _svc = await hz.app()
        try:
            c = await _client(app)
            r = await c.get("/api/v1/plugins/no_such_plugin/readme")
            assert r.status_code == 404
            body = await r.get_json()
            assert "act_probe_a" in body["error"]["available"]
        finally:
            await store.close()

    _run(run())


# ===========================================================================
# ③ 重载（契约 §2）
# ===========================================================================


def test_reload_succeeds_and_says_no_restart(hz):
    """重载成功：`restart_required=false`，且 `handler_count` 与列表**一致**。"""

    async def run():
        app, store, _svc = await hz.app()
        try:
            c = await _client(app)
            r = await c.post("/api/v1/plugins/act_probe_a/reload", json={})
            assert r.status_code == 200, await r.get_json()
            body = await r.get_json()
            assert body["ok"] is True
            assert body["restart_required"] is False
            assert body["result"]["state"] == "loaded"
            assert body["result"]["handler_count"] > 0, (
                "重载后 handler 数为 0 —— 要么插件被 import 但没注册，要么统计口径漂了"
            )

            # 🔴 重载**后**的计数必须与**详情接口**里的 `handler_count` 一致。
            #    ⚠️ 断言对象是 `GET /api/v1/plugins/<id>`（详情），**不是列表** ——
            #       列表接口（`plugins_view()`）**不返回** `handler_count`，
            #       第一版断在列表上，是**断言错、代码对**（本项目已记过这类）。
            #       重载接口与详情接口走的是同一个 `PluginRecord` 的两个出口，
            #       两者不一致就等于"面板与真实状态漂了"。
            detail = await (await c.get("/api/v1/plugins/act_probe_a")).get_json()
            assert detail["handler_count"] == body["result"]["handler_count"], (
                f"重载接口 {body['result']['handler_count']} vs 详情 "
                f"{detail['handler_count']}"
            )
        finally:
            await store.close()

    _run(run())


def test_reload_inactivated_plugin_is_409_with_reason(hz):
    """停用的插件重载 → **409**，且说明"重载不会让它跑起来"。

    ⚠️ 不许回 200 "重载成功" —— 那样用户以为点了就好，实际它从没被 import。
    """

    async def run():
        app, store, svc = await hz.app()
        try:
            c = await _client(app)
            # 通过**正规途径**把插件移到停用名单
            r = await c.put(
                "/api/v1/plugins/inactivated",
                json={"inactivated_plugins": ["act_probe_a.main"]},
            )
            assert r.status_code == 200
            # 直接标记录（真正的停用要重启才生效，这里模拟"重启后"的状态）
            svc.host.loader.records["act_probe_a"].inactivated = True

            resp = await c.post("/api/v1/plugins/act_probe_a/reload", json={})
            assert resp.status_code == 409, await resp.get_json()
            body = await resp.get_json()
            assert body["error"]["type"] == "inactivated"
            assert "重启" in body["error"]["message"], body["error"]["message"]
        finally:
            await store.close()

    _run(run())


def test_reload_with_broken_code_is_400_with_plugin_error(hz):
    """插件代码坏了 → **400**（不是 500），且**把插件自己的错误原样带回来**。

    🔴 400 而不是 500：这是插件代码的问题，不是我们的服务坏了 ——
       500 会让用户去重启服务、翻我们的日志，方向完全错。
    """

    async def run():
        app, store, _svc = await hz.app()
        try:
            c = await _client(app)
            (hz.plugin_root / "act_probe_a" / "main.py").write_text(
                "raise RuntimeError('判据用：故意炸')\n", encoding="utf-8"
            )
            r = await c.post("/api/v1/plugins/act_probe_a/reload", json={})
            assert r.status_code == 400, await r.get_json()
            body = await r.get_json()
            assert body["error"]["type"] == "reload_failed"
            assert "判据用：故意炸" in body["error"]["message"], body["error"]["message"]
            # 完整堆栈要能拿到（用户需要知道是哪一行）
            assert "Traceback" in (body["error"].get("traceback") or "")
        finally:
            await store.close()

    _run(run())


# ===========================================================================
# ④ 删除（契约 §3）—— 移入回收站
# ===========================================================================


def test_uninstall_deletes_source_and_keeps_data(hz):
    """卸载 → 源码目录**被删掉**，**数据目录保留**（默认不删）。"""

    async def run():
        app, store, _svc = await hz.app()
        try:
            c = await _client(app)
            # 造一个数据目录，验它**不被删**
            data = hz.data_dir / "act_probe_a"
            data.mkdir(parents=True, exist_ok=True)
            (data / "memory.json").write_text('{"keep": 1}', encoding="utf-8")

            r = await c.delete("/api/v1/plugins/act_probe_a", json={})
            assert r.status_code == 200, await r.get_json()
            body = await r.get_json()
            assert body["ok"] is True
            assert body["removed"]["handlers"] > 0, "应该摘掉了 handler"
            assert not (hz.plugin_root / "act_probe_a").exists(), "源码目录还在原地"
            assert body["source_path"].endswith("act_probe_a"), body["source_path"]
            assert (data / "memory.json").is_file(), "默认不该删插件数据"
            assert body["removed"]["data_dir"] is None
            # 🔴 默认路径必须是「配置与数据已保留」——这句话决定了用户敢不敢重装
            assert "已保留" in body["message"], body["message"]
            assert "重新安装" in body["note"], body["note"]

            # 列表里也不该再有它
            after = await (await c.get("/api/v1/plugins")).get_json()
            assert "act_probe_a" not in {p["id"] for p in after["plugins"]}
        finally:
            await store.close()

    _run(run())


def test_uninstall_rejects_non_boolean_flags(hz):
    """`{"delete_data": "false"}` 字符串 → **400**。

    🔴 `bool("false") is True` —— 这是经典坑。面板传的是真布尔，
       但 HTTP 面是公开的，字符串必须拒绝而不是"当 True 处理"（那会删数据）。
    """

    async def run():
        app, store, _svc = await hz.app()
        try:
            c = await _client(app)
            r = await c.delete(
                "/api/v1/plugins/act_probe_a", json={"delete_data": "false"}
            )
            assert r.status_code == 400, await r.get_json()
            body = await r.get_json()
            assert "布尔" in body["error"]["message"], body["error"]["message"]
            # ⚠️ 校验失败**不许**已经动了目录
            assert (hz.plugin_root / "act_probe_a").is_dir()
        finally:
            await store.close()

    _run(run())


def test_uninstall_deletes_data_when_asked(hz):
    """显式 `delete_data=true` → 数据目录**真的被删掉**。"""

    async def run():
        app, store, _svc = await hz.app()
        try:
            c = await _client(app)
            data = hz.data_dir / "act_probe_a"
            data.mkdir(parents=True, exist_ok=True)
            (data / "memory.json").write_text('{"keep": 1}', encoding="utf-8")

            r = await c.delete(
                "/api/v1/plugins/act_probe_a",
                json={"delete_data": True, "delete_config": False},
            )
            assert r.status_code == 200, await r.get_json()
            body = await r.get_json()
            assert not data.exists(), "勾了删数据，但数据目录还在"
            assert body["removed"]["data_dir"] == str(data), body["removed"]
            assert "已删除数据目录" in body["message"], body["message"]
        finally:
            await store.close()

    _run(run())


def test_uninstall_unknown_plugin_is_404_and_changes_nothing(hz):
    """未知插件 → 404，且**磁盘上什么都没动**。"""

    async def run():
        app, store, _svc = await hz.app()
        try:
            c = await _client(app)
            r = await c.delete("/api/v1/plugins/no_such_plugin", json={})
            assert r.status_code == 404
            assert (hz.plugin_root / "act_probe_a").is_dir()
            assert (hz.plugin_root / "act_probe_b").is_dir()
        finally:
            await store.close()

    _run(run())


# ===========================================================================
# ⑤ 列表字段（面板动作菜单的可用性判据）
# ===========================================================================


def test_list_exposes_action_availability_with_real_paths(hz):
    """`GET /api/v1/plugins` 的六个新字段，且 `source_path` **指向真实源码目录**。

    ⚠️ 只断言"字段存在"抓不住错误的值 —— 必须比对绝对路径。
    """

    async def run():
        app, store, _svc = await hz.app()
        try:
            c = await _client(app)
            body = await (await c.get("/api/v1/plugins")).get_json()
            entry = {p["id"]: p for p in body["plugins"]}["act_probe_a"]
            assert entry["can_reload"] is True
            assert entry["can_uninstall"] is True
            assert Path(entry["source_path"]) == hz.with_readme.resolve(), (
                entry["source_path"]
            )
            # 两个"勾了才会删"的具体路径（确认框要逐条显示）
            assert entry["config_path"].endswith("act_probe_a_config.json"), entry["config_path"]
            assert entry["data_path"].endswith("act_probe_a"), entry["data_path"]
            assert entry["reloadable_reason"] == ""
            assert entry["uninstallable_reason"] == ""
        finally:
            await store.close()

    _run(run())
