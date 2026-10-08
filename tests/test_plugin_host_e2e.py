# -*- coding: utf-8 -*-
"""宿主装配 + 插件 Web API 挂载的**端到端**判据。

⭐ 为什么这些判据必须有（它们抓的是"装了但跑不起来"这一整类问题）：

   加载器的 35 项测试全绿时，**插件其实一行代码都没被执行过** ——
   266 条 Web API 只是"记在册上"。就像**电话簿抄好了但没接电话线**。
   本文件的判据全部走**真 HTTP 请求 → 真插件 handler → 真产出**，
   任何一环断掉都会失败。

🔴 判据设计原则（本项目铁律）：
   * **没有"跳过"**：插件目录不在就**失败**，不是 skip；
   * **不验"接口返回 200"就收工**：要验**产出里有真数据**（插件真跑过）；
   * **反向也要验**：未鉴权必须 401、假路由必须 404 且带相近提示。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
HOST_DIR = APP_ROOT / "host"
PLUGIN_ROOT = Path("/opt/astrbot/data/plugins")
TOKEN = "test-token-for-plugin-e2e-0001"

for p in (str(APP_ROOT), str(HOST_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from emotion_app.infra.config import Config  # noqa: E402
from emotion_app.storage.factory import build_store  # noqa: E402
from emotion_app.web.app import create_app  # noqa: E402
from emotion_app.web.auth import AuthManager  # noqa: E402


def _cfg(tmp_path: Path, *, enabled: bool = True, mount: bool = True) -> Config:
    cfg = Config()
    cfg.auth.access_token = TOKEN
    cfg.storage.sqlite_path = str(tmp_path / "t.db")
    cfg.plugins.enabled = enabled
    cfg.plugins.mount_web_apis = mount
    cfg.plugins.plugin_dirs = str(PLUGIN_ROOT)
    # 🔴 数据目录必须是**我们自己的临时目录** ——
    #    绝不能指向 /opt/astrbot/data（那是别人 AstrBot 的生产数据）
    cfg.plugins.data_dir = str(tmp_path / "plugin_data")
    # 🔴 源码镜像也落在临时目录：插件回写自己源码目录时只写到副本里
    cfg.plugins.source_mirror_dir = str(tmp_path / "plugin_src")
    cfg.plugins.auto_initialize = False
    cfg.validate()
    return cfg


async def _app_with_plugins(tmp_path: Path, **kw):
    cfg = _cfg(tmp_path, **kw)
    store = build_store(cfg.storage)
    await store.open()
    app = create_app(cfg, store, AuthManager(access_token=TOKEN))
    # 🔴 **配置文件也必须落在临时目录**（2026-09-27 实测踩到）。
    #    `plugins.inactivated_plugins` 是**用户能在面板改的持久设置**，
    #    读生产那份 ⇒ 本文件的 `loaded >= 11` 判据会随用户的启停设置漂移：
    #    情绪在面板停用 4 个插件后，这两条 e2e 判据立刻**假红**
    #    （产品是对的，是判据把"用户设置"当成了"加载能力"）。
    #    指到临时路径 ⇒ 名单为空 ⇒ 判据确定；写盘也只写临时文件。
    app.config["EMOTION_CONFIG_PROFILE_PATH"] = str(tmp_path / "config_profile.json")
    svc = app.config["EMOTION_PLUGIN_HOST"]
    report = await svc.start()
    return app, store, svc, report


# ---------------------------------------------------------------------------
# ① 插件能"装"进来（沿用加载器判据，但在**主服务的装配路径**上再验一次）
# ---------------------------------------------------------------------------


def test_plugin_root_exists():
    """插件目录必须存在 —— **不存在就失败，不 skip**（跳过 = 假通过）。"""
    assert PLUGIN_ROOT.is_dir(), f"插件目录不存在：{PLUGIN_ROOT}"


def test_host_loads_plugins_through_service(tmp_path):
    """宿主经**主服务的装配路径**加载插件：≥11 个成功、陪伴系全成功。"""
    async def run():
        app, store, svc, report = await _app_with_plugins(tmp_path)
        try:
            assert report is not None, "宿主启动失败（看日志）"
            # ⚠️ `>= 11` 的前提是**这份 fixture 没有任何插件被停用**（名单走临时配置文件）。
            #    先断这一条，`11` 这个数字才有确定含义；否则用户改一下面板设置，
            #    这条判据就会以"加载能力退化"的样子假红（2026-09-27 实测踩到）。
            assert report.inactivated == [], (
                f"这份 fixture 的停用名单必须为空，实得 {report.inactivated}"
            )
            assert report.loaded >= 11, f"只加载了 {report.loaded} 个\n{report.summary()}"
            # ⚠️ 判"插件有没有加载"必须用 `loaded_plugins`，**不能用 `mounted`** ——
            #    image_companion / content_companion 注册 **0 条 Web API**，
            #    加载成功却不出现在 mounted 里（实测踩到，差点误判成"没加载"）。
            for name in (
                "astrbot_plugin_private_companion",
                "astrbot_plugin_memory_companion",
                "astrbot_plugin_image_companion",
                "astrbot_plugin_content_companion",
                "astrbot_plugin_together_companion",
            ):
                assert name in report.loaded_plugins, (
                    f"陪伴系插件未加载：{name}\n{report.summary()}"
                )
            # 失败只允许是"插件自己缺依赖"
            for name, err in report.failed:
                assert "No module named" in err, (
                    f"{name} 失败原因不是依赖缺失，很可能是我们的兼容缺口：{err}"
                )
        finally:
            await svc.stop()
            await store.close()

    asyncio.run(run())


def test_266_apis_mounted(tmp_path):
    """⭐ 266 条插件 API **真的被挂上路**（不只是记在册上）。

    这是本轮的核心交付判据：修复前 mounted 概念根本不存在。
    """
    async def run():
        app, store, svc, report = await _app_with_plugins(tmp_path)
        try:
            assert report.mounted_count >= 260, (
                f"只挂载了 {report.mounted_count} 条（实测 266）"
            )
            # 归属要正确（防"全局累计值"回归）
            by_owner: dict[str, int] = {}
            for a in report.mounted:
                by_owner[a.owner] = by_owner.get(a.owner, 0) + 1
            assert by_owner.get("astrbot_plugin_private_companion", 0) >= 150, by_owner
            # 自洽性：分项之和 == 总数
            assert sum(by_owner.values()) == report.mounted_count
        finally:
            await svc.stop()
            await store.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# ② 插件能"跑"起来（真 HTTP → 真 handler → 真产出）
# ---------------------------------------------------------------------------


def test_real_plugin_route_returns_real_data(tmp_path):
    """⭐⭐ 核心判据：真 HTTP 请求打到真插件路由，**产出里有真数据**。

    ⚠️ 只断言 200 是不够的 —— 那可能是个空壳。
       这里要求产出里出现插件自己的字段（证明 handler 真被执行了）。
    """
    async def run():
        app, store, svc, report = await _app_with_plugins(tmp_path)
        try:
            target = next(
                (a for a in report.mounted
                 if a.owner == "astrbot_plugin_private_companion"
                 and a.route.endswith("/overview")
                 and "GET" in a.methods),
                None,
            )
            assert target is not None, "找不到 /overview 路由（插件登记面变了？）"

            client = app.test_client()
            resp = await client.get(
                f"/api/plugins/extensions/{target.route.lstrip('/')}",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert resp.status_code == 200, f"HTTP {resp.status_code}"
            body = await resp.get_json()
            assert isinstance(body, dict), f"产出不是 JSON 对象：{type(body)}"
            # 插件自己的信封（`page_api.py:31587`：{"success":True,"data":...}）
            assert body.get("success") is True, f"插件信封不对：{str(body)[:200]}"
            assert isinstance(body.get("data"), dict), "data 不是对象"
            assert body["data"], "data 是空的 —— handler 可能没真跑"
        finally:
            await svc.stop()
            await store.close()

    asyncio.run(run())


def test_unauthenticated_is_rejected(tmp_path):
    """反向判据：**未鉴权必须 401**（否则插件 API 成了后门）。

    ⚠️ 插件 API 能读写插件数据（人设/记忆/日历），
       绝不能因为"是插件路由"就绕过主服务鉴权。
    """
    async def run():
        app, store, svc, report = await _app_with_plugins(tmp_path)
        try:
            target = next(
                a for a in report.mounted
                if a.owner == "astrbot_plugin_private_companion"
                and a.route.endswith("/overview")
            )
            client = app.test_client()
            resp = await client.get(
                f"/api/plugins/extensions/{target.route.lstrip('/')}"
            )
            assert resp.status_code == 401, f"未鉴权竟然返回 {resp.status_code}！"
        finally:
            await svc.stop()
            await store.close()

    asyncio.run(run())


def test_unknown_route_404_with_hint(tmp_path):
    """假路由 → 404，且**给出相近候选**（否则用户只看到一句"未找到"无从下手）。"""
    async def run():
        app, store, svc, report = await _app_with_plugins(tmp_path)
        try:
            client = app.test_client()
            resp = await client.get(
                "/api/plugins/extensions/astrbot_plugin_private_companion/page/nope",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert resp.status_code == 404, f"期望 404，得到 {resp.status_code}"
            body = await resp.get_json()
            assert body.get("status") == "error"
            similar = (body.get("data") or {}).get("similar")
            assert similar, "404 没有给相近路由提示"
        finally:
            await svc.stop()
            await store.close()

    asyncio.run(run())


def test_wrong_method_404(tmp_path):
    """方法不匹配 → 404（真实现就是"找不到匹配"）。

    ⚠️ 这条防的是"匹配器把方法检查漏掉" —— 漏了就变成
       GET 路由能被 POST 调用，属安全隐患。
    """
    async def run():
        app, store, svc, report = await _app_with_plugins(tmp_path)
        try:
            target = next(
                a for a in report.mounted
                if a.owner == "astrbot_plugin_private_companion"
                and a.route.endswith("/overview")
            )
            assert "PUT" not in [m.upper() for m in target.methods]
            client = app.test_client()
            resp = await client.put(
                f"/api/plugins/extensions/{target.route.lstrip('/')}",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert resp.status_code == 404, f"PUT 竟然进了 GET 路由：{resp.status_code}"
        finally:
            await svc.stop()
            await store.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# ③ 边界：关掉插件时**完全不依赖 host/**
# ---------------------------------------------------------------------------


def test_plugins_disabled_by_default():
    """⭐ 默认**关闭** —— 插件会执行任意代码，不该"默认就开"。

    而且 `enabled=False` 时**连 host/ 都不 import**：
    不想用插件的部署与 401 项主项目测试不该被迫依赖它。
    """
    from emotion_app.infra.config import Config as C

    assert C().plugins.enabled is False, "插件宿主默认必须是关闭的"


def test_disabled_means_no_plugin_routes(tmp_path):
    """关闭时不挂载插件路由（404 而不是 500）。"""
    async def run():
        cfg = _cfg(tmp_path, enabled=False)
        store = build_store(cfg.storage)
        await store.open()
        app = create_app(cfg, store, AuthManager(access_token=TOKEN))
        try:
            assert "EMOTION_PLUGIN_HOST" not in app.config
            client = app.test_client()
            resp = await client.get(
                "/api/plugins/extensions/whatever/page/overview",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert resp.status_code == 404
        finally:
            await store.close()

    asyncio.run(run())


def test_data_dir_is_ours_not_astrbot_production(tmp_path):
    """🔴 插件数据必须写到**我们的** data_dir，绝不碰 `/opt/astrbot/data`。

    这条是**安全判据**：插件源码目录是别人的生产目录（只读），
    数据写进去会污染正在运行的 AstrBot。
    """
    async def run():
        app, store, svc, report = await _app_with_plugins(tmp_path)
        try:
            from astrbot_host.compat.core.star.star_tools import get_data_root

            root = Path(get_data_root()).resolve()
            assert str(root).startswith(str(tmp_path.resolve())), (
                f"数据根目录不在我们的临时目录内：{root}"
            )
            assert not str(root).startswith("/opt/astrbot"), (
                f"数据根目录指向了 AstrBot 生产目录：{root}"
            )
        finally:
            await svc.stop()
            await store.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# ③b 启动健壮性：`initialize()` 必须**超时隔离** + **源码目录写入可见**
#
# 🔴 这两条判据来自 2026-09-19 的**实测事故**（不是假想）：
#    真实服务的默认 `auto_initialize=True` 此前**从未被测过**（别的用例都设 False），
#    一测就发现两个真问题：
#      ① media_parser.initialize() 联网下载 16MB+17MB 字体（超时 180s）
#         → 服务启动被卡 **181.6 秒**；一个插件的网络抖动 = 整个服务起不来；
#      ② comfyui_pro 在 __init__ 里把 workflow 列表**回写插件源码目录**的
#         `_conf_schema.json` —— 那个目录是**两边共用**的（很可能是别人
#         AstrBot 的生产目录），于是我们的 data_dir 内容覆盖了它的配置。
# ---------------------------------------------------------------------------


def test_initialize_has_timeout_isolation(tmp_path):
    """🔴 一个插件的 `initialize()` 永不返回时，宿主必须**按时放弃**并如实记录。

    ⚠️ **判据不许"跳过"**：用一个人为写死的"永不返回"的假插件来验，
       确定性 100%（不受真实插件网络状况影响）。

    反向要求（同样重要）：
      * 超时**不能静默** —— 必须出现在 `init_failed` 里且写明 TimeoutError；
      * 其它插件**不受影响** —— 必须仍然被初始化（隔离性）。
    """
    import time as _time

    async def run():
        from astrbot_host.host_app import PluginHost

        class _Hang:
            async def initialize(self) -> None:
                await asyncio.Event().wait()  # 永不 set → 永不返回

        class _Ok:
            def __init__(self):
                self.done = False

            async def initialize(self) -> None:
                self.done = True

        class _Rec:
            def __init__(self, name, inst):
                self.dir_name = name
                self.instance = inst

        hang, ok = _Hang(), _Ok()
        # 直接测 `_initialize_all`（绕开真插件目录，判据才确定）
        host = PluginHost(plugin_dirs=[str(tmp_path)], auto_initialize=True,
                          initialize_timeout=0.3)
        records = [_Rec("plug_hang", hang), _Rec("plug_ok", ok)]

        t0 = _time.monotonic()
        # ⚠️ 测试自带**总闸**：如果超时保护被改坏，宿主会永远等下去，
        #    而"测试挂死"和"测试失败"是两回事 —— 挂死会拖垮整个套件、
        #    在 CI 里表现为 timeout 而不是一条点名的失败。
        #    所以这里用外层 wait_for 把它变成**确定的失败**。
        try:
            good, bad = await asyncio.wait_for(
                host._initialize_all(records),  # noqa: SLF001
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            pytest.fail(
                "宿主 initialize() 没有超时保护：一个永不返回的插件"
                "把 _initialize_all 卡死了（>5s）—— 这正是服务启动被拖死的成因"
            )
        elapsed = _time.monotonic() - t0

        assert elapsed < 3.0, f"超时没生效，等了 {elapsed:.1f}s"
        assert "plug_ok" in good, f"正常插件未初始化：{good}"
        assert ok.done is True, "正常插件其实没被调用"

        names = [n for n, _ in bad]
        assert "plug_hang" in names, f"超时插件没进 init_failed：{bad}"
        msg = dict(bad)["plug_hang"]
        assert "TimeoutError" in msg, f"失败原因没说清是超时：{msg}"

    asyncio.run(run())


def test_slow_but_not_timeout_is_not_silent(tmp_path, caplog):
    """慢（但没超时）的插件也要留痕 —— 否则"下次就超时了"没人知道。"""
    async def run():
        from astrbot_host.host_app import PluginHost

        class _Slow:
            async def initialize(self) -> None:
                await asyncio.sleep(0.35)

        class _Rec:
            dir_name = "plug_slow"
            instance = _Slow()

        host = PluginHost(plugin_dirs=[str(tmp_path)], initialize_timeout=0.5)
        with caplog.at_level("WARNING"):
            good, bad = await host._initialize_all([_Rec()])  # noqa: SLF001
        assert good == ["plug_slow"] and bad == []
        assert any("偏慢" in r.message for r in caplog.records), (
            "慢插件没有任何告警 —— 静默会掩盖下一个超时"
        )

    asyncio.run(run())


def test_source_dir_writes_are_detected_and_reported(tmp_path, caplog):
    """🔴 插件**回写自己源码目录**必须被检出并如实报告（事故②的判据）。

    为什么必须守这条：源码目录是**只读共用**的（很可能就是别人的 AstrBot
    生产目录）。插件按自己的 `data_dir` 回写 schema 的 options，
    会**覆盖掉对方真实配置**。以前这件事**完全静默** ——
    我是靠人工 diff 生产目录才偶然发现的。

    ⚠️ 同时要验**不误报**：`__pycache__` 是 Python 正常副作用，
       报它会让告警变噪音（最后没人看）。
    """
    async def run():
        from astrbot_host.write_guard import SourceSnapshot, warn_on_source_writes

        # 造一个"插件源码目录"
        pdir = tmp_path / "fake_plugin_src"
        pdir.mkdir()
        schema = pdir / "_conf_schema.json"
        schema.write_text('{"options":["a"]}', encoding="utf-8")
        (pdir / "__pycache__").mkdir()
        (pdir / "__pycache__" / "x.pyc").write_bytes(b"\x00")

        before = SourceSnapshot.take([tmp_path])

        # ① 正常副作用：只动 __pycache__ → 必须**不报**
        #    ⚠️ 这里放一个**非 .pyc 后缀**的文件，专门用来验"目录剪枝"这一层：
        #       之前只用 .pyc 测，结果**后缀过滤**顺手挡住了它 →
        #       我把 `__pycache__` 从剪枝名单里删掉，测试**照样通过**（假通过）。
        #       两道防线（剪枝 / 后缀）必须各自被真正exercise到。
        (pdir / "__pycache__" / "leftover.txt").write_text("junk", encoding="utf-8")
        (pdir / "__pycache__" / "y.pyc").write_bytes(b"\x01")
        assert before.diff(SourceSnapshot.take([tmp_path])) == [], (
            "__pycache__ 是正常副作用，不该算污染（否则告警被噪音淹没）"
        )

        # ② 真污染：插件回写源码目录里的配置
        schema.write_text('{"options":["OURS"]}', encoding="utf-8")
        writes = before.diff(SourceSnapshot.take([tmp_path]))

        assert len(writes) == 1, f"应检出正好 1 处源码目录改动，实得 {writes}"
        assert writes[0].kind == "modified"
        assert writes[0].path.endswith("_conf_schema.json")

        with caplog.at_level("WARNING"):
            warn_on_source_writes(writes)
        assert any("源码目录" in r.message for r in caplog.records), (
            "检出了却没告警 = 还是静默"
        )

    asyncio.run(run())


def test_host_report_exposes_source_writes(tmp_path):
    """装配层要把 `source_writes` 传到状态里（否则面板/日志看不到）。"""
    async def run():
        app, store, svc, report = await _app_with_plugins(tmp_path)
        try:
            st = svc.status()
            assert "source_writes" in st, "status() 没暴露源码目录写入情况"
            assert isinstance(st["source_writes"], list)
        finally:
            await svc.stop()
            await store.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# ③c 源码镜像：**从根上不发生**污染（比"事后发现"强得多）
#
# 🔴 为什么发现还不够：我实测发现，探针**每跑一次**，生产目录就被改一次。
#    "写保护哨兵"能告诉我"有人写了"，但拦不住 —— 别人的配置已经坏了。
#    真正的解法是**别共用那个目录**：镜像到我们自己的可写副本再加载。
# ---------------------------------------------------------------------------


def test_mirror_isolates_plugin_source_writes(tmp_path):
    """🔴 **核心安全判据**：插件回写"源码目录"时，只写到我们的副本，
    源目录**一个字节都不能变**。

    复刻真实事故：comfyui_pro 把 workflow 列表回写 `_conf_schema.json`。
    这里用一个**假插件**确定性地重现该行为（不受真实插件网络/环境影响）。

    判据分两半（缺一不可）：
      ① 源目录**零改动** —— 这是安全边界；
      ② 插件**确实写成功了**（写到了副本里）—— 否则"没改源"可能只是因为
         "根本没写"，那样这条判据就是假通过。
    """
    from astrbot_host.source_mirror import mirror_plugin_sources

    src_root = tmp_path / "prod_plugins"          # 模拟"别人的生产目录"
    plug = src_root / "fake_plugin"
    plug.mkdir(parents=True)
    schema = plug / "_conf_schema.json"
    original = '{"workflow_settings":{"items":{"json_file":{"options":["A"]}}}}'
    schema.write_text(original, encoding="utf-8")
    (plug / "main.py").write_text("# plugin\n", encoding="utf-8")
    (plug / "__pycache__").mkdir()
    (plug / "__pycache__" / "x.pyc").write_bytes(b"\x00")

    mirror_root = tmp_path / "mirror"
    report = mirror_plugin_sources([src_root], mirror_root)

    assert report.plugin_names == ["fake_plugin"], report.plugin_names
    mirrored_schema = mirror_root / "fake_plugin" / "_conf_schema.json"
    assert mirrored_schema.is_file(), "schema 没被镜像过去"
    # `__pycache__` 不该被镜像（生成物，白白占空间）
    assert not (mirror_root / "fake_plugin" / "__pycache__").exists(), (
        "__pycache__ 不该参与镜像"
    )

    # 模拟插件回写"自己的源码目录"——它拿到的是**镜像**目录
    mirrored_schema.write_text('{"options":["OURS"]}', encoding="utf-8")

    # ① 源目录必须**一字未改**
    assert schema.read_text(encoding="utf-8") == original, (
        "🔴 源目录被改写了 —— 镜像没有起到隔离作用！"
    )
    # ② 插件确实写成功了（证明上一行不是"因为压根没写"而侥幸通过）
    assert "OURS" in mirrored_schema.read_text(encoding="utf-8"), (
        "插件根本没写成功 → 上面那条判据是假通过"
    )


def test_mirror_is_incremental_and_propagates_deletes(tmp_path):
    """镜像要**增量**（第二次几乎不复制）且**传播删除**。

    为什么不传播删除很危险：插件删掉/改名一个文件后，我们副本里还留着旧的，
    **加载的是过期代码**，且完全静默 —— 典型"行为与源不一致"。
    """
    from astrbot_host.source_mirror import mirror_plugin_sources

    src_root = tmp_path / "prod"
    plug = src_root / "p1"
    plug.mkdir(parents=True)
    (plug / "a.py").write_text("x=1", encoding="utf-8")
    (plug / "b.py").write_text("y=2", encoding="utf-8")
    mirror_root = tmp_path / "mirror"

    r1 = mirror_plugin_sources([src_root], mirror_root)
    assert r1.copied == 2, f"首次应复制 2 个文件：{r1}"

    # 第二次：无改动 → 不应再复制
    r2 = mirror_plugin_sources([src_root], mirror_root)
    assert r2.copied == 0, f"增量失效，重复复制了 {r2.copied} 个"
    assert r2.unchanged == 2

    # 源删掉 b.py → 副本里也必须消失
    (plug / "b.py").unlink()
    r3 = mirror_plugin_sources([src_root], mirror_root)
    assert not (mirror_root / "p1" / "b.py").exists(), (
        "副本里残留了源已删除的文件 → 会加载过期代码"
    )
    assert r3.removed == 1, f"删除未传播：{r3}"

    # 源改了内容 → 必须重新复制
    (plug / "a.py").write_text("x=999", encoding="utf-8")
    r4 = mirror_plugin_sources([src_root], mirror_root)
    assert r4.copied == 1, f"内容变更未同步：{r4}"
    assert "999" in (mirror_root / "p1" / "a.py").read_text(encoding="utf-8")


def test_service_loads_from_mirror_not_source(tmp_path):
    """装配层必须**真的从镜像加载**（模块能工作 ≠ 被接上了）。

    ⚠️ 这条守的是本项目踩过的经典坑：镜像函数写好了、单测全绿，
       但装配处忘了调用 → 功能完全没生效。判据落在"宿主实际用的目录"上。
    """
    async def run():
        app, store, svc, report = await _app_with_plugins(tmp_path)
        try:
            assert svc.host.mirror_report is not None, "服务没有做源码镜像（接线漏了？）"
            mirror_root = Path(svc.host.mirror_root).resolve()
            # 宿主加载的目录必须是镜像，**不是** /opt/astrbot/data/plugins
            used = [Path(p).resolve() for p in svc.host.loader.plugin_dirs]
            assert used and all(str(p) == str(mirror_root) for p in used), (
                f"宿主不是从镜像加载的：{used}（镜像在 {mirror_root}）"
            )
            assert not any(str(p).startswith("/opt/astrbot") for p in used), (
                f"宿主仍在从 AstrBot 生产目录加载：{used}"
            )
        finally:
            await svc.stop()
            await store.close()

    asyncio.run(run())


def test_real_plugins_produce_no_production_writes(tmp_path):
    """🔴 **端到端安全判据**：用真插件跑完整启动，生产目录**必须零改动**。

    这是把上面所有单点判据合起来问一句最要紧的话：
    "跑完之后，别人的生产目录有没有被我们动过？"

    ⚠️ 对照事实（修复前）：同样的流程会改掉
       `/opt/astrbot/data/plugins/astrbot_plugin_comfyui_pro/_conf_schema.json`
       —— 把 AstrBot 的 3 个 workflow 选项覆盖成我们的 1 个。
    """
    async def run():
        import os

        src_root = PLUGIN_ROOT
        assert src_root.is_dir(), f"插件目录不存在：{src_root}"

        def snapshot() -> dict[str, tuple[int, int]]:
            snap: dict[str, tuple[int, int]] = {}
            for dirpath, dirnames, filenames in os.walk(src_root):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for name in filenames:
                    if name.endswith((".pyc", ".pyo")):
                        continue
                    p = Path(dirpath) / name
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    snap[str(p)] = (st.st_mtime_ns, st.st_size)
            return snap

        before = snapshot()

        # 🔴 **自愈保险**：这条判据故意用**真的**插件目录当输入（那是最强的证据），
        #    但万一代码回归、真的写了源目录，测试自己就会成为污染者。
        #    所以先把可能被改的文件的**原始字节**存下来，无论成败都还原。
        #    （判据仍然照常失败 —— 自愈是为了不留伤，不是为了掩盖问题。）
        backups: dict[str, bytes] = {}
        for path in before:
            try:
                backups[path] = Path(path).read_bytes()
            except OSError:
                pass

        app, store, svc, report = await _app_with_plugins(tmp_path)
        try:
            assert report is not None and report.loaded >= 11
            after = snapshot()

            changed = sorted(set(after) ^ set(before)) + sorted(
                k for k in before if k in after and before[k] != after[k]
            )
            assert changed == [], (
                "🔴 插件源码目录（可能是别人的生产目录）被改动了：\n  "
                + "\n  ".join(changed)
            )
            # 宿主自己报的 source_writes 也应指向**镜像内**（不是源目录）
            for w in report.source_writes:
                assert not str(w.path).startswith(str(src_root)), (
                    f"宿主报告写到了源目录：{w.path}"
                )
        finally:
            await svc.stop()
            await store.close()

            # 还原被改动的文件（若代码正确，这里什么都不会做）
            restored: list[str] = []
            for path, blob in backups.items():
                try:
                    if Path(path).read_bytes() != blob:
                        Path(path).write_bytes(blob)
                        restored.append(path)
                except OSError:
                    pass
            for path in set(snapshot()) - set(before):
                # 新建的文件：删掉（同样只会在回归时发生）
                try:
                    Path(path).unlink()
                    restored.append(path)
                except OSError:
                    pass
            if restored:
                import warnings

                warnings.warn(
                    "测试已自动还原被污染的插件源文件（说明代码确实回归了）："
                    + ", ".join(restored),
                    stacklevel=1,
                )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# ③d 插件**拓展页**：情绪报的「插件的配置入口没有喵」
#
# 🔴 根因（不是插件没写页面）：宿主**只挂了插件的数据 API、没挂页面**。
#    `metadata.yaml` 里声明 `pages:` 的插件（陪伴面板/QQ空间/一起房间）
#    在面板里完全没有入口。
#
# 🔴 另外两个真 bug（都在这组判据里守着）：
#    ① 页面 URL **必须以 `/` 结尾** —— 插件页用相对路径（`./app.js`），
#       少斜杠会让所有静态资源 404、页面白屏。
#    ② 插件页要 `window.AstrBotPluginPage` 桥才肯取数，不注入垫片 → 白屏。
# ---------------------------------------------------------------------------


def test_discovers_plugin_pages_from_metadata(tmp_path):
    """`metadata.yaml` 的 `pages:` 必须被读出来，且**只收入口存在的**。"""
    from astrbot_host.plugin_pages import discover_pages

    class _Rec:
        def __init__(self, dir_name, path, pages):
            self.dir_name = dir_name
            self.path = path
            self.pages = pages

    root = tmp_path / "src"
    good = root / "plug_ok"
    (good / "pages" / "panel").mkdir(parents=True)
    (good / "pages" / "panel" / "index.html").write_text("<html></html>", encoding="utf-8")

    # 声明了页面但**入口不存在** → 必须被跳过（并且打 WARNING，不静默）
    bad = root / "plug_missing"
    (bad / "pages" / "ghost").mkdir(parents=True)

    recs = [
        _Rec("plug_ok", good, [{"name": "panel", "title": "好面板"}]),
        _Rec("plug_missing", bad, [{"name": "ghost", "title": "幽灵页"}]),
        _Rec("plug_none", root / "nope", []),
    ]
    pages = discover_pages(recs, plugin_root_of=lambda r: r.path)

    got = {(p.plugin_id, p.page_name) for p in pages}
    assert got == {("plug_ok", "panel")}, f"应只收入口存在的页，实得 {got}"
    assert pages[0].title == "好面板"


def test_normalize_pages_accepts_real_world_shapes():
    """`pages:` 的三种真实写法都要能吃（字符串 / dict / 混合），无 name 的要丢。"""
    from astrbot_host.loader.plugin_loader import _normalize_pages

    assert _normalize_pages(["companion-panel"]) == [
        {"name": "companion-panel", "title": "companion-panel"}
    ]
    assert _normalize_pages([{"name": "p", "title": "标题"}]) == [
        {"name": "p", "title": "标题"}
    ]
    # 没有 title → 用 name 兜底；没有 name → 丢掉（定位不到 pages/<name>/）
    assert _normalize_pages([{"name": "p"}, {"title": "无名字"}]) == [
        {"name": "p", "title": "p"}
    ]
    assert _normalize_pages(None) == []


# ---------------------------------------------------------------------------
# 🔴 页面发现必须【扫目录】（2026-10-08 修的真缺口）
# ---------------------------------------------------------------------------


def _mk_page(plugin_root, name: str, *, with_entry: bool = True):
    """造一个插件页目录 `<plugin_root>/pages/<name>/index.html`。"""
    d = plugin_root / "pages" / name
    d.mkdir(parents=True, exist_ok=True)
    if with_entry:
        (d / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    return d


def test_discovers_page_without_metadata_declaration(tmp_path):
    """🔴 **没在 `metadata.yaml` 里声明、但目录存在的页面，必须被发现。**

    这正是情绪报的那个缺口：「记忆插件**存在吗**？我看插件面板里面没有呢，
    **应该是有扩展页的**」—— 实测 `astrbot_plugin_memory_companion`
    已加载且运行中、`pages/记忆面板/index.html` 真实存在，
    但它的 metadata **根本没有 `pages:` 段** ⇒ 只读 metadata 的实现在这里漏掉它。

    真 AstrBot 的 `discover_plugin_pages()` 是**扫目录**的（不看 metadata）。
    ⚠️ 这条判据会真的失败：把实现改回只读 `_normalize_pages(declared)` 即红。
    """
    from astrbot_host.loader.plugin_loader import discover_plugin_pages

    _mk_page(tmp_path, "记忆面板")
    assert discover_plugin_pages(tmp_path, None) == [
        {"name": "记忆面板", "title": "记忆面板"}
    ]


def test_page_dirs_win_over_metadata_declaration(tmp_path):
    """目录扫描是**权威**：声明与目录**都**存在时，两边都要保留（去重）。

    实测形态（`private_companion`）：metadata 只声明了 `companion-panel`，
    但 `pages/` 下有 **两个**目录（`companion-panel` 和 `陪伴面板`）
    ⇒ 漏的正是没声明的那个。连"声明过"的插件也漏，说明**发现方式本身错了**。
    """
    from astrbot_host.loader.plugin_loader import discover_plugin_pages

    _mk_page(tmp_path, "companion-panel")
    _mk_page(tmp_path, "陪伴面板")
    discovered = discover_plugin_pages(
        tmp_path, [{"name": "companion-panel", "title": "陪伴面板"}]
    )
    names = [p["name"] for p in discovered]
    # 两个目录页都在，且**不重复**
    assert sorted(names) == ["companion-panel", "陪伴面板"], names
    assert len(names) == len(set(names)), f"去重失败：{names}"


def test_directory_without_entry_file_is_not_a_page(tmp_path):
    """**没有 `index.html` 的目录不算页面**（照真实现 `discover_plugin_pages`）。"""
    from astrbot_host.loader.plugin_loader import discover_plugin_pages

    _mk_page(tmp_path, "real-page")
    _mk_page(tmp_path, "assets", with_entry=False)  # 是资源目录，不是页面
    assert [p["name"] for p in discover_plugin_pages(tmp_path, None)] == ["real-page"]


def test_declared_but_missing_dir_is_kept_for_diagnosis(tmp_path):
    """声明了但目录不存在 → **保留**（让用户看见"声明了却没页面"这个事实）。

    本项目铁律：**不许静默丢弃**。真 AstrBot 里这种条目会在取页面时 404，
    但那比"面板上不显示、用户永远不知道"要好。
    """
    from astrbot_host.loader.plugin_loader import discover_plugin_pages

    _mk_page(tmp_path, "exists")
    got = discover_plugin_pages(tmp_path, [{"name": "ghost", "title": "幽灵页"}])
    assert {p["name"] for p in got} == {"exists", "ghost"}


def test_illegal_page_dir_name_is_skipped(tmp_path):
    """非法的页面目录名要跳过（照真实现 `normalize_plugin_page_name` 的安全校验）。

    ⚠️ 中文名是**合法**的（真实现只用它拒 `..`/斜杠/点开头）—— 别过度校验。
    """
    from astrbot_host.loader.plugin_loader import discover_plugin_pages

    _mk_page(tmp_path, "ok-page")
    _mk_page(tmp_path, ".hidden")
    got = [p["name"] for p in discover_plugin_pages(tmp_path, None)]
    assert got == ["ok-page"], got


def test_page_asset_blocks_directory_traversal(tmp_path):
    """🔴 **安全判据**：插件页资源**绝不能**读到页面目录之外。

    这是新增路由里唯一的安全关键点：`Path(root)/"../../etc/passwd"` 会跳出目录。
    """
    from astrbot_host.plugin_pages import PluginPage, resolve_page_asset

    page_root = tmp_path / "plug" / "pages" / "p"
    page_root.mkdir(parents=True)
    (page_root / "index.html").write_text("ok", encoding="utf-8")
    (page_root / "app.js").write_text("// js", encoding="utf-8")
    secret = tmp_path / "outside.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")

    page = PluginPage(plugin_id="plug", page_name="p", title="P", root=page_root)

    # 正常资源能取到
    assert resolve_page_asset(page, "app.js") is not None
    assert resolve_page_asset(page, "") is not None          # 空 → 入口
    # 穿越必须全部被挡
    for bad in [
        "../outside.txt",
        "../../outside.txt",
        "../../../outside.txt",
        "sub/../../outside.txt",
    ]:
        assert resolve_page_asset(page, bad) is None, f"目录穿越没被挡住：{bad}"


def test_page_asset_blocks_symlink_escape(tmp_path):
    """软链接指向外部文件同样要被挡（`resolve()` 会展开链接）。"""
    from astrbot_host.plugin_pages import PluginPage, resolve_page_asset

    page_root = tmp_path / "plug" / "pages" / "p"
    page_root.mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    link = page_root / "leak.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        import pytest as _pytest

        _pytest.skip("本环境不支持创建软链接")

    page = PluginPage(plugin_id="plug", page_name="p", title="P", root=page_root)
    assert resolve_page_asset(page, "leak.txt") is None, "软链逃逸没被挡住"


def test_injects_bridge_shim_into_page_html(tmp_path):
    """🔴 页面 HTML 里**必须**被注入桥接垫片，否则插件页白屏。

    实测：插件页首选 `window.AstrBotPluginPage` 桥取数；没有桥就退到
    HTTP 模式，而那个模式要求 `?debug_http=1`/standalone 标记，两者都没有
    → 抛「未检测到 AstrBot 官方插件 Page 桥接」→ **页面空白**。
    """
    from astrbot_host.plugin_pages import inject_page_context

    html = "<!doctype html><html><head><title>x</title></head><body></body></html>"
    out = inject_page_context(html, is_dark=True, bridge_src="/static/plugin_page_bridge.js")

    assert "/static/plugin_page_bridge.js" in out, "桥接垫片没注入"
    assert '<meta name="color-scheme" content="dark">' in out
    # 注入必须在 </head> 之前（否则 <script> 会被浏览器挪走/失效）
    assert out.index("plugin_page_bridge.js") < out.index("</head>")
    # 不能破坏插件自己的内容
    assert "<title>x</title>" in out


def test_injects_plugin_id_meta_into_page_html(tmp_path):
    """🔴 页面 HTML 里必须注入**插件身份** meta（2026-09-27 根因修复）。

    守两个后果，都是实测出来的：

    ① **「一起房间」永远取不到数**：它 `launcher.js:47` 只生成
       `page/x` **一种候选、且没有重试**，垫片从前不知道插件名 ⇒
       拼成 `/api/plugins/extensions/page/x` ⇒ **必然 404** ⇒ 页面报
       「未找到该路由」。（其余插件有 6 级候选重试，只是刷日志。）
    ② 日志里成组的「插件路由未命中」（实测 19 条，每组 3 条）。

    ⚠️ meta 必须在垫片 `<script>` **之前** —— 垫片是同步脚本，
       执行时立刻读它；顺序反了就永远读不到（等于没注入，且**静默**）。
    """
    from astrbot_host.plugin_pages import inject_page_context

    html = "<!doctype html><html><head><title>x</title></head><body></body></html>"
    out = inject_page_context(
        html,
        is_dark=True,
        bridge_src="/static/plugin_page_bridge.js",
        plugin_id="astrbot_plugin_together_companion",
        page_name="一起房间",
    )

    assert '<meta name="emotion-plugin-id" content="astrbot_plugin_together_companion">' in out, (
        "插件身份 meta 没注入 —— 垫片只能盲试候选（一起房间会永远 404）"
    )
    assert '<meta name="emotion-plugin-page"' in out, "页面名 meta 没注入"
    # 🔴 顺序：meta 必须早于垫片脚本
    assert out.index("emotion-plugin-id") < out.index("plugin_page_bridge.js"), (
        "meta 排在垫片后面 —— 垫片执行时读不到它（静默失效）"
    )
    # 仍不能破坏插件自己的内容，也仍要注入桥与主题
    assert "<title>x</title>" in out
    assert '<meta name="color-scheme" content="dark">' in out
    assert out.index("plugin_page_bridge.js") < out.index("</head>")


def test_plugin_id_meta_is_escaped_and_optional(tmp_path):
    """插件名要转义；不给 `plugin_id` 时**不注入**（既有调用点行为不变）。"""
    from astrbot_host.plugin_pages import inject_page_context

    html = "<!doctype html><html><head></head><body></body></html>"
    # 不给 plugin_id → 不注入身份 meta（向后兼容，别把既有调用点弄坏）
    plain = inject_page_context(html, bridge_src="/static/plugin_page_bridge.js")
    assert "emotion-plugin-id" not in plain, "没给 plugin_id 却注入了身份 meta"

    # 含引号的名字必须被转义（否则撑破 meta → 垫片读不到 → 又回到盲试）
    nasty = inject_page_context(
        html, plugin_id='ev"il<x>&y', page_name="p", bridge_src="/x.js"
    )
    assert 'content="ev&quot;il&lt;x&gt;&amp;y"' in nasty, (
        f"属性值没被正确转义：{nasty[:200]}"
    )


def test_page_route_passes_plugin_id_into_injection(tmp_path):
    """🔴 装配判据：页面路由必须把 `plugin_id` **传进** `inject_page_context`。

    第一层（注入函数会写 meta）修好了，第二层不接线照样白搭 ——
    这正是本项目「模块能工作 ≠ 被接上了」那类缺口。
    源码级断言（要真起服务取页面代价大，而这个缺口的形态就是"少传一个参数"）。
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent
        / "emotion_app" / "web" / "plugin_pages_web.py"
    ).read_text(encoding="utf-8")
    assert "plugin_id=plugin_id," in src, (
        "页面路由没把 plugin_id 传给 inject_page_context —— 垫片仍拿不到插件名"
    )
    assert "page_name=page_name," in src, "没把 page_name 传进去"


def test_panel_static_assets_are_not_cached(tmp_path):
    """🔴 **面板静态资源必须 `no-cache`**（实测事故，代价很大）。

    症状：改完前端，用户手机上还是旧的 → 情绪报「移动端适配没有/导入没有」，
    而**代码其实早就生效了**。根因是 `send_from_directory` 默认
    `max-age=12 小时`。这类"服务端一切正常、curl 也对，只有用户看不到"
    的问题极难自查，所以必须有判据钉住。

    ⚠️ 同时守一个**更隐蔽**的坑：`Quart(__name__)` 会**自动注册**一条
    `/static/<path:filename>`，优先级**高于**我们自己加的同路径路由
    → 就算我们写了 no-cache 也会被它静默盖掉（实测踩到）。
    所以 `create_app` 必须传 `static_folder=None`。
    """
    async def run():
        cfg = _cfg(tmp_path, enabled=False)
        store = build_store(cfg.storage)
        await store.open()
        app = create_app(cfg, store, AuthManager(access_token=TOKEN))
        try:
            client = app.test_client()
            for path in ("/", "/static/app.css", "/static/app.js"):
                resp = await client.get(path)
                assert resp.status_code == 200, f"{path} → {resp.status_code}"
                cc = resp.headers.get("Cache-Control", "")
                assert "no-cache" in cc, (
                    f"{path} 的 Cache-Control 是 {cc!r} —— 长缓存会让用户"
                    "「改完看不见」（本项目真实事故）"
                )
        finally:
            await store.close()

    asyncio.run(run())


def test_plugin_page_routes_serve_real_html(tmp_path):
    """端到端：插件页三条路由都要真的工作（列表 / 入口 / 资源 / 根路径）。"""
    async def run():
        app, store, svc, report = await _app_with_plugins(tmp_path)
        try:
            if report is None or not report.pages:
                pytest.fail("没有发现任何插件拓展页 —— 页面路由无从验证")
            client = app.test_client()
            head = {"Authorization": f"Bearer {TOKEN}"}

            # ① 列表
            r = await client.get("/api/v1/plugins/pages", headers=head)
            assert r.status_code == 200, r.status_code
            pages = (await r.get_json())["pages"]
            assert pages, "列表为空"
            p0 = next(p for p in pages if p["plugin_id"] == "astrbot_plugin_private_companion")

            base = f"/api/v1/plugins/{p0['plugin_id']}/pages/{p0['page_name']}"
            # ② 无斜杠 → 308（相对资源路径要靠尾斜杠才对）
            r = await client.get(base, headers=head)
            assert r.status_code == 308, f"应 308 重定向，实得 {r.status_code}"
            # ③ 入口 HTML（带尾斜杠）
            r = await client.get(base + "/", headers=head)
            html = await r.get_data(as_text=True)
            assert r.status_code == 200
            assert "/static/plugin_page_bridge.js" in html, "入口没注入桥接垫片"
            # ④ 页面自己的静态资源
            r = await client.get(base + "/app.js", headers=head)
            assert r.status_code == 200, f"app.js → {r.status_code}"
            assert len(await r.get_data()) > 1000

            # ⑤ 未鉴权必须被挡（反向判据）
            r = await client.get("/api/v1/plugins/pages")
            assert r.status_code == 401, f"未鉴权应 401，实得 {r.status_code}"
        finally:
            await svc.stop()
            await store.close()

    asyncio.run(run())


def test_plugin_page_root_relative_api_works(tmp_path):
    """🔴 插件页 JS **硬编码**了根路径 `/astrbot_plugin_x/page/...`。

    实测 `companion-panel/app.js:1`：
        `const HTTP_API = "/astrbot_plugin_private_companion/page";`
    —— **不带任何前缀**。真 AstrBot 靠 iframe+postMessage 桥绕开；
    我们同源直连，所以必须在根路径上也能派发，否则插件页取数全 404。
    """
    async def run():
        app, store, svc, report = await _app_with_plugins(tmp_path)
        try:
            client = app.test_client()
            head = {"Authorization": f"Bearer {TOKEN}"}
            r = await client.get(
                "/astrbot_plugin_private_companion/page/overview", headers=head
            )
            assert r.status_code == 200, (
                f"根路径兼容路由没生效：{r.status_code}（插件页会取不到数据）"
            )
            assert len(await r.get_data()) > 1000, "返回体太小，疑似空响应"

            # 未鉴权同样要挡
            r = await client.get("/astrbot_plugin_private_companion/page/overview")
            assert r.status_code == 401
        finally:
            await svc.stop()
            await store.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# ③e `/api/plug/<path>` —— 真实现的 **legacy** 前缀（插件页 JS 硬编码那条）
#
# 🔴 现场（`docs/插件拓展页_API_404_诊断.md`）：`qzone-companion/app.js:1`
#    写死 `const API = "/api/plug/astrbot_plugin_content_companion/page"`，
#    而真 AstrBot 4.27.5 **确实有**这条
#    （`dashboard/api/plugins.py:1506`，GET/POST）。我们漏了它 ⇒
#    `GET /api/plug/astrbot_plugin_content_companion/page/status` → 404，
#    且是**被我们自己的保留前缀守卫静默挡掉**的（无日志、连鉴权都没走到）。
#
# 本组判据守四件事（缺一条这类问题就会复发）：
#   ① 新路由**已鉴权**（未登录 401）—— 本项目真事故：漏 `require_auth()`
#      → 未登录返 200，而服务绑 `0.0.0.0` = 公网可读；
#   ② 未登录时**不许**是"路由不存在"的形状（否则插件页又把 404 当终态）；
#   ③ 已登录时**与 `/api/plugins/extensions/...` 走同一个派发器**
#      —— 判据落在"两条路由收到的 `plugin_path` 完全一致"上，
#      而不是"两条都返回 200"（那样复制第二份实现也能过）；
#   ④ 保留前缀被挡时**有日志**（否则下次同类故障还是零可见度）。
# ---------------------------------------------------------------------------


class _RecordingHost:
    """假宿主：只记录 `call_api` 收到了什么，不真跑插件。

    ⚠️ 为什么用假的而不是真插件：本组判据问的是**装配层**的问题
      （路由挂没挂、鉴权在不在、两条路由是否同源），
      用真插件会让判据随插件版本漂移，且"两条路由传参一致"这件事
      在假宿主上能被**逐字**断言。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.report = None

    async def call_api(self, plugin_path, *, request_obj=None, method="GET", username=None):
        self.calls.append((plugin_path, method))
        return {"ok": True, "echo_path": plugin_path, "echo_method": method}


def _app_with_recording_host(tmp_path: Path):
    """挂好路由 + 换成假宿主的 app（不启动真插件宿主，判据才确定）。"""
    cfg = _cfg(tmp_path, enabled=True, mount=True)
    store = build_store(cfg.storage)
    app = create_app(cfg, store, AuthManager(access_token=TOKEN))
    app.config["EMOTION_CONFIG_PROFILE_PATH"] = str(tmp_path / "config_profile.json")
    svc = app.config["EMOTION_PLUGIN_HOST"]
    host = _RecordingHost()
    svc.host = host  # 直接替换：`signature` 只看 `service.host is None` 与否
    return app, store, svc, host


def test_legacy_plug_prefix_requires_auth(tmp_path):
    """① `/api/plug/...` 必须**先鉴权**（未登录 401，不是 200/404）。

    🔴 本项目真事故：新增 API 路由漏 `require_auth()` → 未登录返 **200**，
       而服务绑 `0.0.0.0` ⇒ 公网可读。
    """
    async def run():
        app, store, svc, host = _app_with_recording_host(tmp_path)
        try:
            client = app.test_client()
            r = await client.get(
                "/api/plug/astrbot_plugin_private_companion/page/overview"
            )
            assert r.status_code == 401, (
                f"未登录打 /api/plug/... 竟然返回 {r.status_code}！"
                "（插件 API 成了后门）"
            )
            # 未鉴权**绝不能**派发出去
            assert host.calls == [], f"未鉴权也派发了：{host.calls}"
        finally:
            await store.close()

    asyncio.run(run())


def test_legacy_plug_prefix_is_not_404_when_unauthenticated(tmp_path):
    """② 未登录时**不许**返回"路由不存在"那种形状 —— 那样插件页无法区分
       "没登录"与"这条路由不存在"，会把 401 当成终态放弃重试。

    ⚠️ 对照组是**改之前**的样子：`{"error":"not found","type":"not_found"}`，41 字节
       （出处只有 `plugin_pages_web.py` 的保留前缀守卫）。这条判据就是钉死它。
    """
    async def run():
        app, store, svc, host = _app_with_recording_host(tmp_path)
        try:
            client = app.test_client()
            r = await client.get(
                "/api/plug/astrbot_plugin_content_companion/page/status"
            )
            assert r.status_code != 404, (
                f"未登录仍然是 404 —— 说明请求又落回兜底路由的保留前缀守卫"
                f"（plugin_pages_web.py 的 RESERVED_PREFIXES），"
                f"/api/plug 这条精确路由没生效"
            )
            body = await r.get_json()
            assert body.get("type") != "not_found", (
                f"body 还是'路由不存在'的形状：{body}（正是改之前那个 41 字节 body）"
            )
        finally:
            await store.close()

    asyncio.run(run())


def test_legacy_plug_prefix_dispatches_identically(tmp_path):
    """③⭐ 核心判据：两条前缀**走同一个派发器**，收到的 `plugin_path` **完全一致**。

    🔴 为什么不断言"两条都 200"：那样**复制第二份实现**也能过，
       而本项目铁律是"同一概念不许有两条会漂移的写入路径"
       （已复发 3 次）。所以判据落在**传进宿主的参数逐字相同**上。
    """
    async def run():
        app, store, svc, host = _app_with_recording_host(tmp_path)
        try:
            client = app.test_client()
            head = {"Authorization": f"Bearer {TOKEN}"}
            path = "astrbot_plugin_content_companion/page/status"

            r_new = await client.get(f"/api/plugins/extensions/{path}", headers=head)
            r_old = await client.get(f"/api/plug/{path}", headers=head)

            assert r_new.status_code == 200, f"新前缀 {r_new.status_code}"
            assert r_old.status_code == 200, (
                f"legacy 前缀 {r_old.status_code} —— 插件页硬编码的 /api/plug 没通"
            )
            # ①② 两次派发收到的 plugin_path / method **逐字相同**
            assert len(host.calls) == 2, f"派发次数不对：{host.calls}"
            assert host.calls[0] == host.calls[1] == (path, "GET"), (
                f"两条前缀进宿主的参数不一致：{host.calls}"
            )
            # ③ 响应体也一致（同一 handler 的产出，不是两个长得像的实现）
            assert await r_new.get_json() == await r_old.get_json()
        finally:
            await store.close()

    asyncio.run(run())


def test_legacy_plug_prefix_supports_post(tmp_path):
    """legacy 前缀的 **POST** 同样要通（`qzone-companion` 的发布按钮就是 POST）。

    真 legacy 那条只登记 GET/POST；我们取五种（见 `app.py` 的注释：
    两条路由**指向同一个派发器**，方法集不同就是又一条会漂移的差异）。
    """
    async def run():
        app, store, svc, host = _app_with_recording_host(tmp_path)
        try:
            client = app.test_client()
            head = {"Authorization": f"Bearer {TOKEN}"}
            path = "astrbot_plugin_content_companion/page/action"
            r = await client.post(
                f"/api/plug/{path}", json={"action": "publish"}, headers=head
            )
            assert r.status_code == 200, f"POST {r.status_code}"
            assert host.calls == [(path, "POST")], host.calls
        finally:
            await store.close()

    asyncio.run(run())


def test_legacy_plug_prefix_returns_404_with_hint_for_unknown_route(tmp_path):
    """反向判据：**插件没登记**的路由仍要 404 + 相近提示（不是一律 200）。

    ⚠️ 这条防的是"为了修 404 干脆放行一切" —— 那样插件 API 变成任意转发口。
    """
    async def run():
        app, store, svc, host = _app_with_recording_host(tmp_path)
        try:
            call_count = {"n": 0}

            async def _boom(plugin_path, **_kw):
                call_count["n"] += 1
                raise LookupError(f"插件未登记该路由：{plugin_path}")

            host.call_api = _boom  # type: ignore[method-assign]
            client = app.test_client()
            head = {"Authorization": f"Bearer {TOKEN}"}
            r = await client.get(
                "/api/plug/astrbot_plugin_private_companion/page/nope", headers=head
            )
            assert r.status_code == 404, f"期望 404，得到 {r.status_code}"
            assert call_count["n"] == 1, "没有真去问宿主（可能被硬编码 404 挡住）"
            body = await r.get_json()
            assert body.get("status") == "error", body
        finally:
            await store.close()

    asyncio.run(run())


def test_reserved_prefix_block_is_logged(tmp_path, caplog):
    """④🔴 保留前缀挡下的请求**必须留日志**（改之前完全静默，见诊断报告 §2.4）。

    为什么这条最值钱：那次故障在日志里**零痕迹** —— `plugin_pages_web.py`
    的守卫在鉴权**之前** return，连"插件根路径路由未命中"那条 warning
    都打不到。用户只能凭一张截图报障。

    ⚠️ 判据直接复刻**改之前**的现场形状：`/api/<非 plug>/...`
      （`plugin_dir="api"` 命中 `RESERVED_PREFIXES`）。
      改之前 `/api/plug/...` 就是这么被静默 404 的；
      现在 `/api/plug/...` 被新的精确路由接走（⇒ 401/200），
      而**其它**没登记的 `/api/...` 仍然落到守卫上 —— 那时候必须留痕。

    ⚠️ 注意**不能**拿 `/static/...` 造现场：它有自己更具体的路由
      （`/static/<path:filename>`）先命中，根本走不到这个守卫
      （实测：返回 404 但一条日志都没有 —— 会变成假判据）。
    """
    async def run():
        cfg = _cfg(tmp_path, enabled=True, mount=True)
        store = build_store(cfg.storage)
        app = create_app(cfg, store, AuthManager(access_token=TOKEN))
        app.config["EMOTION_CONFIG_PROFILE_PATH"] = str(tmp_path / "config_profile.json")
        try:
            client = app.test_client()

            # ---- 前置（**不满足就是失败，不许跳过**）----
            # 兜底路由必须在路由表里，否则本判据测的根本不是那个守卫。
            rules = {str(r) for r in app.url_map.iter_rules()}
            assert "/<plugin_dir>/<path:rest>" in rules, (
                f"兜底路由没挂上，判据测试的不是守卫：{sorted(rules)[:6]}"
            )
            # 同一个 `api` 前缀下，**精确**的 `/api/plug` 路由必须存在
            # （它才是这次交付的东西；否则本判据会与失败现场混在一起）。
            assert "/api/plug/<path:plugin_path>" in rules, (
                "/api/plug 这条精确路由没挂上 —— 本次交付没生效"
            )

            unregistered = "/api/legacy-not-registered/page/status"
            with caplog.at_level("WARNING", logger="emotion_app.plugins"):
                r = await client.get(unregistered)
            assert r.status_code == 404, (
                f"{unregistered} 期望落到保留前缀守卫（404），实得 {r.status_code} —— "
                "判据的前置不成立"
            )
            hits = [
                rec.getMessage()
                for rec in caplog.records
                if "被保留前缀挡下" in rec.getMessage()
            ]
            assert hits, (
                "保留前缀挡下的请求**没有任何日志** —— 这正是那次 404 "
                f"零可见度的成因。实得日志：{[r.getMessage()[:50] for r in caplog.records]}"
            )
            assert "api" in hits[0], f"日志没说清是哪个保留前缀：{hits[0]}"
            assert "兼容路由" in hits[0], (
                f"日志没提示'可能是缺兼容路由'，下次还是会查半天：{hits[0]}"
            )
        finally:
            await store.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# ④ 路由匹配器与真实现逐字一致（防"自作聪明"回归）
# ---------------------------------------------------------------------------


def test_route_pattern_handles_flask_syntax():
    """`<x>` 单段、`<path:x>` 贪婪 —— 两种 Flask 语法都要对。

    ⚠️ 我们自己写个"按 / 切分"的匹配器就会在这里挂。
    """
    from astrbot_host.web_router import plugin_api_route_pattern

    single = plugin_api_route_pattern("/plug/page/persona/<persona_id>")
    greedy = plugin_api_route_pattern("/plug/page/files/<path:file_path>")
    assert "(?P<persona_id>[^/]+)" in single, single
    assert "(?P<file_path>.*)" in greedy, greedy
    # 贪婪必须能跨 `/`
    import re

    assert re.fullmatch(greedy, "/plug/page/files/a/b/c.png")
    assert not re.fullmatch(single, "/plug/page/persona/a/b")


#: 🔴 **golden 值：直接从真 AstrBot 4.27.5 抓下来的正则输出**。
#:    用途：让"与真实现一致"这条判据**任何时候都能跑**
#:    （真 AstrBot 不在我们的 venv 里，见下方 live 差分测试的说明）。
#:    抓取方式（可复现）：
#:      PYTHONPATH=$PWD/host /root/.local/share/uv/tools/astrbot/bin/python \
#:        -c "from astrbot.dashboard.api.plugins import _plugin_api_route_pattern as f; print(f('/x/<y>'))"
_REAL_ROUTE_PATTERNS = {
    "/astrbot_plugin_x/page/overview": "/astrbot_plugin_x/page/overview",
    "/astrbot_plugin_x/page/persona/<persona_id>":
        "/astrbot_plugin_x/page/persona/(?P<persona_id>[^/]+)",
    "/astrbot_plugin_x/page/files/<path:file_path>":
        "/astrbot_plugin_x/page/files/(?P<file_path>.*)",
    "/astrbot_plugin_x/page/a/<x>/b/<y>":
        "/astrbot_plugin_x/page/a/(?P<x>[^/]+)/b/(?P<y>[^/]+)",
    "/astrbot_plugin_x/page/nested/<path:p>/tail":
        "/astrbot_plugin_x/page/nested/(?P<p>.*)/tail",
    "no-slash-route": "/no\\-slash\\-route",
}


def test_route_pattern_matches_real_astrbot_golden():
    """⭐ 与真 AstrBot 的**正则逐字相同**（对 golden 值，**始终可跑**）。

    ⚠️ 为什么用 golden 值而不是每次都去 import 真实现：
       真 AstrBot 装在 `uv tools` 环境里，**我们的 venv 没有它** ——
       每次都 import 会让这条判据在常规 `pytest` 下**必然失败**，
       而"失败"和"跳过"都不是我们想要的（跳过=假通过，失败=噪音）。
       → 用**从真实现抓下来的常量**做判据：任何时候都能验，
         真实现升级后 golden 值需要重新抓（抓法见上方注释）。
    """
    from astrbot_host.web_router import plugin_api_route_pattern

    for route, expected in _REAL_ROUTE_PATTERNS.items():
        got = plugin_api_route_pattern(route)
        assert got == expected, f"路由 {route!r} 的正则与真实现不一致：{got!r} != {expected!r}"


def test_router_matches_real_implementation_live():
    """差分（**需要真 AstrBot**）：不在时**明确失败并说明怎么跑**。

    ⚠️ 这条判据的**正式入口是那个脚本**（它能用真解释器跑）::

        PYTHONPATH=$PWD/host \\
          /root/.local/share/uv/tools/astrbot/bin/python \\
          host/tests/tools_verify_router.py

    本测试函数做的是**同一件事**，但要求当前解释器能 import 真 AstrBot ——
    我们的 venv 里没有它，所以常规 `pytest` 下这条会**如实失败**。
    跑全量时用 `-k "not live"` 排除（见 README/CI 说明）。

    🔴 **故意不用 skip**：skip 会让"从没验过"看起来像"验过了"。
    """
    try:
        from astrbot.dashboard.api.plugins import (  # type: ignore[import-not-found]
            _match_registered_web_api as real_match,
        )
        from astrbot.dashboard.api.plugins import (
            _plugin_api_route_pattern as real_pattern,
        )
    except ImportError as exc:
        pytest.fail(
            "真 AstrBot 不可导入 → 差分判据未满足（这不是 skip）："
            f"{exc}\n正式跑法：PYTHONPATH=$PWD/host "
            "/root/.local/share/uv/tools/astrbot/bin/python "
            "host/tests/tools_verify_router.py"
        )

    from astrbot_host.web_router import (
        match_registered_web_api as our_match,
    )
    from astrbot_host.web_router import (
        plugin_api_route_pattern as our_pattern,
    )

    routes = list(_REAL_ROUTE_PATTERNS) + ["/trailing/"]
    paths = [
        "astrbot_plugin_x/page/overview",
        "astrbot_plugin_x/page/persona/abc",
        "astrbot_plugin_x/page/persona/a/b",
        "astrbot_plugin_x/page/files/a/b/c.png",
        "astrbot_plugin_x/page/nested/deep/er/tail",
        "no-slash-route",
        "unknown/path",
    ]

    async def _h(**_kw):
        return {}

    for r in routes:
        assert our_pattern(r) == real_pattern(r), f"正则不一致：{r}"

    reg = [(r, _h, ["GET", "POST"], "d") for r in routes]
    for p in paths:
        for m in ("GET", "POST", "DELETE"):
            a = real_match(reg, p, m)
            b = our_match(reg, p, m)
            ka = None if a is None else a[1]
            kb = None if b is None else b[1]
            assert ka == kb, f"匹配不一致 path={p} method={m}: {ka} vs {kb}"


# ---------------------------------------------------------------------------
# ⑨ P-N5：插件要**读得到全局配置**（get_config 不许恒为空）
# ---------------------------------------------------------------------------
#
# 🔴 本组判据守的是"装配接线"而不是"函数本身"：
#    `load_global_config()` 自己早就对了（host 侧 9 条判据），但如果
#    `plugin_host.py` 传的还是 `HostPorts(config={})`，插件拿到的仍是空字典
#    —— 这正是本项目反复踩的**「函数对 ≠ 接线对」**（P-N6 的注入验证
#    已经因为同类盲区漏抓过一次）。
#    ⇒ 所以这里必须走**真实装配路径**（`svc.start()`），再从
#      `svc.host.context` 上把配置读回来断言。


def _write_cmd_config(path: Path, payload: dict) -> Path:
    """写一个**带 UTF-8 BOM** 的 cmd_config.json（模拟真 AstrBot 落盘方式）。

    ⚠️ 必须带 BOM：真 `data/cmd_config.json` 就是带 BOM 的，而"用 utf-8 读
       → JSONDecodeError → 被吞成 {}"是这条链路上最容易复发的静默 bug。
    """
    import json as _json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8-sig")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf"), "前置：文件确实带 BOM"
    return path


def test_assembled_host_reads_real_global_config(tmp_path):
    """⭐⭐ **接线判据**：走真实装配路径时，插件读到的全局配置**不是空字典**。

    注入 `config={}`（或把 `config=self._load_global_config()` 改回去）时本判据必须失败。
    """
    sentinel = tmp_path / "cmd_config.json"
    _write_cmd_config(
        sentinel,
        {
            "timezone": "Asia/Shanghai",
            "provider_settings": {"tool_call_timeout": 77, "default_personality": "default"},
            "provider_tts_settings": {"enable": True, "provider_id": "probe-tts"},
            "_pn5_sentinel": "read-me",
        },
    )

    async def run():
        cfg = _cfg(tmp_path)
        # 🔴 显式指向临时文件：判据不依赖仓库里那份真配置（也不许改它）
        cfg.plugins.global_config_path = str(sentinel)
        cfg.validate()
        store = build_store(cfg.storage)
        await store.open()
        app = create_app(cfg, store, AuthManager(access_token=TOKEN))
        svc = app.config["EMOTION_PLUGIN_HOST"]
        try:
            report = await svc.start()
            assert report is not None, "宿主启动失败（看日志）"
            ctx = svc.host.context
            assert ctx is not None, "拿不到插件 Context"

            got = ctx.get_config()
            # 1) 绝不许是空字典（这就是 P-N5 那个 bug 的症状）
            assert got, (
                "插件读到的全局配置是空的 —— get_config() 又恒返回 {} 了"
                "（检查 plugin_host 是否把 config={} 传给了 HostPorts）"
            )
            # 2) 形状：plain dict（插件多处 config.get(...)、12 处按位置传 umo）
            assert isinstance(got, dict), f"必须是 dict，实际 {type(got).__name__}"
            # 3) 内容真的来自那个文件（不是别的来源凑巧非空）
            assert got.get("_pn5_sentinel") == "read-me", f"没读到哨兵键：{sorted(got)[:8]}"
            assert got.get("timezone") == "Asia/Shanghai"
            # 4) 子键逐键一致（NEXT_STEPS 的验收判据）
            assert got["provider_settings"]["tool_call_timeout"] == 77
            assert got["provider_tts_settings"]["provider_id"] == "probe-tts"
            # 5) 传 umo 也要拿到同一份（插件 12 处按位置传）
            assert ctx.get_config("11:FriendMessage:default") == got
        finally:
            await svc.stop()
            await store.close()

    asyncio.run(run())


def test_missing_global_config_is_loud_not_silent(tmp_path, caplog):
    """全局配置缺失 → **响亮报错**，但服务照常启动（不拖垮主服务）。

    ⚠️ 静默给 `{}` 才是本项目最贵的 bug 类型：插件会把"没配置"当成
       "用户就是这么配的"（TTS 判未配置 / timezone 回落 / 超时回落 120s）。
    """
    async def run():
        cfg = _cfg(tmp_path)
        cfg.plugins.global_config_path = str(tmp_path / "does-not-exist.json")
        cfg.validate()
        store = build_store(cfg.storage)
        await store.open()
        app = create_app(cfg, store, AuthManager(access_token=TOKEN))
        svc = app.config["EMOTION_PLUGIN_HOST"]
        try:
            with caplog.at_level("ERROR"):
                report = await svc.start()
            assert report is not None, "配置缺失不该让宿主起不来（插件是可选增强）"
            errs = [
                r for r in caplog.records
                if r.levelname == "ERROR" and "全局配置" in r.getMessage()
            ]
            assert errs, (
                "全局配置缺失必须记 ERROR（静默降级是最贵的 bug 类型）；"
                f"实际 ERROR：{[r.getMessage()[:60] for r in caplog.records if r.levelname=='ERROR'][:3]}"
            )
            # 且如实降级为空 dict（不是 None —— None 会让插件 .get() 崩）
            got = svc.host.context.get_config()
            assert got == {} and got is not None
        finally:
            await svc.stop()
            await store.close()

    asyncio.run(run())


def test_real_cmd_config_is_reachable_by_default_config(tmp_path):
    """仓库里那份真 `data/cmd_config.json` 必须能被默认设置读到。

    ⚠️ 这条守的是"默认值别写错"：`global_config_path` 默认 `data/cmd_config.json`
       与 `data_dir` 默认 `data/plugins` 是同一层 `data/`。默认值写错时
       **生产上就会静默降级**（而单测因为都显式设了路径，可能照样全绿）。
    """
    from astrbot_host.compat.core.config.astrbot_config import load_global_config

    real = APP_ROOT / "data" / "cmd_config.json"
    assert real.is_file(), f"真全局配置不存在：{real}"

    default_rel = Config().plugins.global_config_path
    assert default_rel == "data/cmd_config.json", (
        f"默认路径变了：{default_rel!r}（要与 data_dir='data/plugins' 同一层 data/）"
    )
    resolved = (APP_ROOT / default_rel).resolve()
    assert resolved == real.resolve(), f"默认路径没指向真配置：{resolved}"

    got = load_global_config(resolved)
    assert got.get("timezone") == "Asia/Shanghai", f"真配置没读出来：{sorted(got)[:8]}"


# ---------------------------------------------------------------------------
# 🔴 装配判据：页面发现必须**接在加载链路上**（2026-10-08）
#
# ⚠️ 为什么单测 `discover_plugin_pages()` 不够（我第一版就栽在这）：
#    那几条判据直接调函数 ⇒ **绕过了 `load_one`** ⇒ 注入"把 load_one 里那行改回
#    只读 metadata"时**一条都不会红**（实测：45 passed 全绿）。
#    这正是本项目的老坑「**模块能工作 ≠ 被接上了**」。
#    ⇒ 必须走**真加载**，断言 `PluginRecord.pages` 真的含那个页面。
# ---------------------------------------------------------------------------


def _load_real_plugin_once(tmp_path_factory, dir_name):
    """真加载一个插件（走 `PluginLoader.load_all`），返回 PluginRecord。"""
    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.loader import PluginLoader
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()
    mirror = tmp_path_factory.mktemp(f"mirror-{dir_name}")
    loader = PluginLoader(plugin_dirs=[PLUGIN_ROOT], mirror_root=mirror)
    report = asyncio.run(loader.load_all(ports=HostPorts(config={}), only=[dir_name]))
    assert report.records, f"加载器没发现插件 {dir_name}"
    return report.records[0]


def test_memory_companion_page_is_discovered_end_to_end(tmp_path_factory):
    """🔴 **真机缺口**：记忆插件的「记忆面板」必须出现在 `rec.pages` 里。

    情绪报：「记忆插件**存在吗**？我看插件面板里面没有呢，**应该是有扩展页的**」
    —— 实查：插件已加载运行，`pages/记忆面板/index.html` 真实存在，
    但 `metadata.yaml` **没有 `pages:` 段** ⇒ 旧实现（只读 metadata）**漏掉它**。

    ⚠️ 这条判据走**真加载链路**（`load_all` → `load_one`），
       所以它能抓住"函数对但没接上"的退化 —— 注入验证已证明它会红。
    """
    if not (PLUGIN_ROOT / "astrbot_plugin_memory_companion").is_dir():
        pytest.fail("依赖的真插件不存在 —— 本判据不许跳过")
    rec = _load_real_plugin_once(tmp_path_factory, "astrbot_plugin_memory_companion")
    names = [p["name"] for p in (rec.pages or [])]
    assert "记忆面板" in names, (
        f"记忆面板没被发现（metadata 未声明、必须靠扫目录）：{names}"
    )


def test_companion_plugin_lists_both_page_dirs(tmp_path_factory):
    """陪伴插件的两个页面目录**都要**在（metadata 只声明了其中 1 个）。

    实测：`pages/` 下有 `companion-panel` 与 `陪伴面板` 两个目录（各有 index.html），
    而 metadata **只声明了 `companion-panel`** ⇒ 旧实现漏掉「陪伴面板」。
    ⚠️ 连"声明过"的插件也漏，说明**发现方式本身错了**，不是个别特例。
    """
    if not (PLUGIN_ROOT / "astrbot_plugin_private_companion").is_dir():
        pytest.fail("依赖的真插件不存在 —— 本判据不许跳过")
    rec = _load_real_plugin_once(tmp_path_factory, "astrbot_plugin_private_companion")
    names = {p["name"] for p in (rec.pages or [])}
    assert {"companion-panel", "陪伴面板"} <= names, (
        f"陪伴插件的两个页面目录没都列出来：{sorted(names)}"
    )


def test_across_all_real_plugins_no_page_dir_is_missed(tmp_path_factory):
    """🔴 **集合判据**：所有真插件 `pages/` 下**有 index.html 的目录**，
    必须**一个不漏**地出现在各自的 `rec.pages` 里。

    ⚠️ 用**集合比较**而不是数量比较 —— 本项目踩过「数量相等 ≠ 组成正确」。
    两边同源（一边扫目录、一边读记录），所以不依赖执行顺序。
    """
    from astrbot_host.loader.plugin_loader import discover_plugin_pages

    if not PLUGIN_ROOT.is_dir():
        pytest.fail("真插件根目录不存在 —— 本判据不许跳过")

    dirs = sorted(d for d in PLUGIN_ROOT.iterdir() if (d / "pages").is_dir())
    assert dirs, "没找到任何带 pages/ 的插件 —— 判据失去意义"

    missing: dict[str, list[str]] = {}
    for plugin_dir in dirs:
        # 期望 = 直接扫目录（权威口径），不看该插件的 metadata 声明
        expected = {p["name"] for p in discover_plugin_pages(plugin_dir, None)}
        if not expected:
            continue
        rec = _load_real_plugin_once(tmp_path_factory, plugin_dir.name)
        got = {p["name"] for p in (rec.pages or [])}
        absent = expected - got
        if absent:
            missing[plugin_dir.name] = sorted(absent)

    assert not missing, f"这些页面目录被漏掉：{missing}"
