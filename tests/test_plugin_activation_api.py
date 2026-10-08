# -*- coding: utf-8 -*-
"""面板「插件启停」判据 —— `GET /api/v1/plugins` / `PUT /api/v1/plugins/inactivated`。

## 这个能力守什么

真 AstrBot 有 `inactivated_plugins`（停用插件），我们以前**没有**：
不想让它跑的插件只能一直跑，于是日志里每轮必现四类噪音
（`prompt_tools` 的钩子 ERROR / `meme_manager` 的 import 失败 /
`gemini_content_keeper` 的 initialize 失败 / `media_parser` 的 initialize 超时）。

## 判据设计（照项目铁律）

  * **反向也要验**：未鉴权必须 401（本项目真事故：新增路由漏 `require_auth()`
    → 服务绑 `0.0.0.0` 时公网可读运行日志）；
  * **不验"接口 200"就收工**：要验**落盘内容**与**重启后宿主真的没加载它**；
  * **失败不许显示成"没有"**：宿主没起来时是 503，不是空列表；
  * **"需重启生效"必须可证伪**：`restart_required` 在重启后自己变 false。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
HOST_DIR = APP_ROOT / "host"
TOKEN = "test-token-for-plugin-activation-01"

for p in (str(APP_ROOT), str(HOST_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from emotion_app.infra.config import Config  # noqa: E402
from emotion_app.storage.factory import build_store  # noqa: E402
from emotion_app.web.app import create_app  # noqa: E402
from emotion_app.web.auth import AuthManager  # noqa: E402

#: 最小但**真实**的插件（用插件的真实写法，不碰我们的内部东西）。
PLUGIN_TEMPLATE = """\
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


class Probe(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)

    @filter.command("回声")
    async def echo_cmd(self, event: AstrMessageEvent, word: str = "默认"):
        yield event.plain_result(f"收到:{word}")

    async def initialize(self) -> None:
        return None
"""


def _make_plugin(root: Path, name: str) -> Path:
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "metadata.yaml").write_text(
        f"name: {name}\nversion: 1.0\nauthor: t\ndesc: t\n", encoding="utf-8"
    )
    (pkg / "main.py").write_text(PLUGIN_TEMPLATE, encoding="utf-8")
    return pkg


class Harness:
    """一个装好了插件宿主的 app + 未启动/已启动两种取用方式。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.plugin_root = tmp_path / "plugins"
        self.plugin_root.mkdir(parents=True, exist_ok=True)
        _make_plugin(self.plugin_root, "act_probe_a")
        _make_plugin(self.plugin_root, "act_probe_b")
        #: 🔴 面板配置**必须隔离到 tmp** —— 绝不能写项目的 `data/`
        self.profile_path = tmp_path / "data" / "config_profile.json"

    def cfg(self) -> Config:
        cfg = Config()
        cfg.auth.access_token = TOKEN
        cfg.storage.sqlite_path = str(self.tmp_path / "t.db")
        cfg.plugins.enabled = True
        cfg.plugins.mount_web_apis = True
        cfg.plugins.plugin_dirs = str(self.plugin_root)
        cfg.plugins.data_dir = str(self.tmp_path / "plugin_data")
        cfg.plugins.source_mirror_dir = str(self.tmp_path / "plugin_src")
        cfg.plugins.auto_initialize = False
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


def _run(coro):
    """跑一个协程（**每个判据只调一次** —— 里面不能再嵌套 asyncio.run）。"""
    return asyncio.run(coro)


def _reset_process_globals() -> None:
    """清空宿主**进程级全局**（等价模拟"重启 = 新进程"）。

    🔴 为什么必须清：`star_handlers_registry` / `llm_tools` / `star_map` 都是
       模块级单例，同一进程里第二次 `start()` **不会**清掉上一次注册的 handler。
       真机重启是新进程（表自然为空），所以这里手动清空才是等价模拟 ——
       不清的话，"停用插件没注册 handler"这条判据会被上一次的残留弄成假红/假绿。
       ⚠️ 这是**测试侧**的模拟缺口，不是产品问题：产品里 `start()` 只被调一次
       （`before_serving`），`PluginHost.start()` 还有 `_started` 幂等闸。
    """
    from astrbot_host.compat.core.star.base import star_map, star_registry
    from astrbot_host.registry import llm_tools, star_handlers_registry

    star_handlers_registry.clear()
    star_map.clear()
    star_registry.clear()
    llm_tools.clear()


async def _client(app, *, login: bool = True):
    c = app.test_client()
    if login:
        await c.post("/api/v1/auth/login", json={"token": TOKEN})
    return c


async def _get(c, path):
    r = await c.get(path)
    return r.status_code, await r.get_json()


async def _put(c, path, body):
    r = await c.put(path, json=body)
    return r.status_code, await r.get_json()


@pytest.fixture
def hz(tmp_path):
    return Harness(tmp_path)


# ===========================================================================
# ① 鉴权（对照隔壁那行的 `require_auth()` —— 本项目真事故）
# ===========================================================================


def test_list_plugins_requires_auth(hz):
    async def run():
        app, store, _svc = await hz.app()
        try:
            code, body = await _get(await _client(app, login=False), "/api/v1/plugins")
            assert code == 401, f"未鉴权也能读插件清单：{code} {body}"
        finally:
            await store.close()

    _run(run())


def test_save_requires_auth(hz):
    """🔴 写接口更要验：未鉴权能改配置 = 公网能停掉你的插件。"""

    async def run():
        app, store, _svc = await hz.app()
        try:
            code, body = await _put(
                await _client(app, login=False),
                "/api/v1/plugins/inactivated",
                {"inactivated_plugins": ["act_probe_a.main"]},
            )
            assert code == 401, f"未鉴权也能写停用名单：{code} {body}"
            # 而且**真的没写**
            assert not hz.profile_path.exists(), "未鉴权的请求竟然落盘了"
        finally:
            await store.close()

    _run(run())


# ===========================================================================
# ② 读：全部插件 + 状态可区分 + 失败不显示成"没有"
# ===========================================================================


def test_lists_every_plugin_with_state(hz):
    async def run():
        app, store, _svc = await hz.app()
        try:
            code, body = await _get(await _client(app), "/api/v1/plugins")
            assert code == 200, body
            names = sorted(p["id"] for p in body["plugins"])
            assert names == ["act_probe_a", "act_probe_b"], names
            for p in body["plugins"]:
                assert p["state"] == "loaded", p
                assert p["module_path"] == f"{p['id']}.main", p
                assert p["enabled_saved"] is True, p
            assert body["inactivated_saved"] == []
            assert body["inactivated_active"] == []
            assert body["restart_required"] is False
        finally:
            await store.close()

    _run(run())


def test_unavailable_is_503_not_empty_list(hz):
    """宿主没起来 → **503 + 原因**，绝不返回空列表（空列表会被读成"没有插件"）。"""

    async def run():
        app, store, _svc = await hz.app(start=False)
        try:
            code, body = await _get(await _client(app), "/api/v1/plugins")
            assert code == 503, f"{code} {body}"
            assert "error" in body and body["error"]["message"], body
        finally:
            await store.close()

    _run(run())


def test_empty_list_keeps_report_byte_identical(hz):
    """🔴 反向判据：**没配停用名单**时，报告与状态必须与加这个能力之前一致。

    （这是"新能力不能改变默认行为"的守门判据 —— 名单为空只准是**零影响**。）
    """

    async def run():
        app, store, svc = await hz.app()
        try:
            report = svc.report
            assert report.inactivated == []
            assert report.unmatched_inactivated == []
            assert "已停用" not in report.summary(), report.summary()
            assert report.loaded_plugins == ["act_probe_a", "act_probe_b"]
            st = svc.status()
            assert st["inactivated"] == [] and st["unmatched_inactivated"] == []
            code, body = await _get(await _client(app), "/api/v1/plugins")
            assert code == 200 and body["restart_required"] is False, body
            assert all(p["enabled_saved"] for p in body["plugins"]), body
        finally:
            await store.close()

    _run(run())


# ===========================================================================
# ③ 写：真的落盘 + 校验会拒绝
# ===========================================================================


def test_save_writes_to_disk_and_reports_restart(hz):
    async def run():
        app, store, _svc = await hz.app()
        try:
            code, body = await _put(
                await _client(app),
                "/api/v1/plugins/inactivated",
                {"inactivated_plugins": ["act_probe_a.main"]},
            )
            assert code == 200, body
            # ---- 落盘内容（不是只看接口返回）----
            raw = json.loads(hz.profile_path.read_text(encoding="utf-8"))
            assert raw["plugins"]["inactivated_plugins"] == ["act_probe_a.main"], raw
            # ---- 勾选状态 = **保存的意图**（重启前运行态还是 loaded）----
            a = next(p for p in body["plugins"] if p["id"] == "act_probe_a")
            assert a["enabled_saved"] is False, a
            assert a["state"] == "loaded", "重启前它当然还在跑（这里如实反映）"
            # ---- "需重启生效"是算出来的 ----
            assert body["restart_required"] is True, body
            assert body["inactivated_active"] == [], body
        finally:
            await store.close()

    _run(run())


def test_save_is_idempotent_at_the_byte_level(hz):
    """整体替换 = 幂等：同一份 body 发两次，磁盘内容**逐字相同**。"""

    async def run():
        app, store, _svc = await hz.app()
        try:
            c = await _client(app)
            body = {"inactivated_plugins": ["act_probe_b.main", "act_probe_a.main"]}
            assert (await _put(c, "/api/v1/plugins/inactivated", body))[0] == 200
            first = hz.profile_path.read_bytes()
            assert (await _put(c, "/api/v1/plugins/inactivated", body))[0] == 200
            assert hz.profile_path.read_bytes() == first, "两次同样的写入结果不一致"
        finally:
            await store.close()

    _run(run())


@pytest.mark.parametrize(
    "bad",
    [{"inactivated_plugins": "act_probe_a.main"}, {"inactivated_plugins": [1, 2]}, {}],
)
def test_rejects_bad_shapes_with_field_name(hz, bad):
    """形状不对 → 400 且**点名**字段（静默忽略会让用户以为保存成功了）。"""

    async def run():
        app, store, _svc = await hz.app()
        try:
            code, body = await _put(await _client(app), "/api/v1/plugins/inactivated", bad)
            assert code == 400, f"{code} {body}"
            assert body["error"]["field"] == "inactivated_plugins", body
            assert not hz.profile_path.exists(), "被拒绝的请求竟然落盘了"
        finally:
            await store.close()

    _run(run())


def test_unmatched_entry_is_surfaced(hz):
    """拼错的项不许静默丢：保存能成功，但 `unmatched` 里必须回给面板。"""

    async def run():
        app, store, _svc = await hz.app()
        try:
            code, body = await _put(
                await _client(app),
                "/api/v1/plugins/inactivated",
                {"inactivated_plugins": ["nosuch_plugin.main"]},
            )
            assert code == 200, body
            assert body["unmatched"] == ["nosuch_plugin.main"], body
        finally:
            await store.close()

    _run(run())


# ===========================================================================
# ④ 真正生效：重启宿主 → 该插件**没被加载**
# ===========================================================================


def test_saved_plugin_is_not_loaded_after_restart(hz):
    """🔴 端到端：面板保存 → 重启宿主 → 那个插件不在 loaded、不算 failed、在 inactivated。"""

    async def run():
        # ---- 第 1 程：保存 ----
        app, store, _svc = await hz.app()
        try:
            code, _body = await _put(
                await _client(app),
                "/api/v1/plugins/inactivated",
                {"inactivated_plugins": ["act_probe_a.main"]},
            )
            assert code == 200
        finally:
            await store.close()

        # ---- 第 2 程：全新 app（= 重启；同一份配置文件）----
        app2, store2, _svc2 = await hz.app()
        try:
            report = app2.config["EMOTION_PLUGIN_HOST"].report
            assert "act_probe_b" in report.loaded_plugins, report.summary()
            assert "act_probe_a" not in report.loaded_plugins, (
                "保存过的停用插件居然还是被加载了\n" + report.summary()
            )
            # 🔴 关键的两条区分（写错就会变成"我停用了它却报加载失败"）
            assert [n for n, _e in report.failed] == [], report.summary()
            assert report.inactivated == ["act_probe_a.main"], report.summary()

            code, body = await _get(await _client(app2), "/api/v1/plugins")
            assert code == 200, body
            a = next(p for p in body["plugins"] if p["id"] == "act_probe_a")
            assert a["state"] == "inactivated", a
            assert a["enabled_saved"] is False, a
            # ---- "需重启生效"现在必须自己变 false（可证伪）----
            assert body["restart_required"] is False, body
            assert body["inactivated_active"] == ["act_probe_a.main"], body
        finally:
            await store2.close()

    _run(run())


def test_disabled_plugin_registers_no_handlers(hz):
    """停用的插件**一条 handler 都不许注册**（否则钩子那条噪音还在）。"""

    async def run():
        app, store, _svc = await hz.app()
        try:
            code, _body = await _put(
                await _client(app),
                "/api/v1/plugins/inactivated",
                {"inactivated_plugins": ["act_probe_a.main"]},
            )
            assert code == 200
        finally:
            await store.close()

        # ---- 第 2 程：全新 app（= 重启；同一份配置文件）----
        _reset_process_globals()
        app2, store2, svc2 = await hz.app()
        try:
            from astrbot_host.registry import star_handlers_registry

            owners = {h.handler_module_path for h in star_handlers_registry.all_handlers()}
            assert "act_probe_b.main" in owners, "对照组的 handler 也没注册（判据无效）"
            assert "act_probe_a.main" not in owners, "被停用的插件仍然注册了 handler"
            # 它的 Web API 也不该挂上去
            mounted = {a.owner for a in svc2.report.mounted}
            assert "act_probe_a" not in mounted, mounted
        finally:
            await store2.close()

    _run(run())


def test_status_exposes_inactivated_separately(hz):
    """`status()` 里也要能区分（状态报告不许把停用混进失败）。"""

    async def run():
        app, store, _svc = await hz.app()
        try:
            await _put(
                await _client(app),
                "/api/v1/plugins/inactivated",
                {"inactivated_plugins": ["act_probe_a.main"]},
            )
        finally:
            await store.close()
        _reset_process_globals()
        _app2, store2, svc2 = await hz.app()
        try:
            st = svc2.status()
            assert st["inactivated"] == ["act_probe_a.main"], st
            assert st["failed"] == [], st
            assert st["unmatched_inactivated"] == [], st
        finally:
            await store2.close()

    _run(run())
