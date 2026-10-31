# -*- coding: utf-8 -*-
"""卸载（`uninstall_plugin`）判据 —— 「可恢复地删」这件事的证明。

## 这一组守什么

卸载是本项目**唯一会动用户磁盘**的能力，所以判据的重点不是"删掉了"，
而是"**删对了地方、且还能拿回来**"：

* 源码目录**真的被删掉**（不是移走）—— 插件代码可再生，不做回收站；
* `delete_data=False` 时数据目录**原封不动**（那是用户的记忆/关系库）；
* `delete_data=True` 时数据目录**真的被删掉**，默认**不删**（卸载后重装配置还在）；
* 停用名单里该插件的 `module_path` **被剔除**（否则下次启动报 unmatched）；
* 解析不到源码目录时抛 `unsupported` 且**磁盘上什么都没动**。

## 两个坑，都用判据钉住

1. 🔴 **必须按 `loader.requested_dirs` 解析源码，不能用 `loader.plugin_dirs`**。
   开了源码镜像（默认开）时后者指向副本 —— 照它删，删的只是副本，
   源码原地不动：用户看到"删除成功"，**重启后插件又回来了**。
2. 🔴 **按裸字符串前缀删会误伤邻居**（`lc_probe` vs `lc_probe_extra`）。
   本文件用「三个插件」夹具守住这一条。

⚠️ 全部用临时目录 + 合成插件：**不依赖仓库 `plugins/` 下那 6 个真插件**。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _plugin_lifecycle_fixtures import (  # noqa: E402
    PROBE_PLUGIN,
    build_host,
    data_root,  # noqa: F401
    owned_handlers_of,
    source_root,  # noqa: F401
    teardown_host,
    three_plugins,  # noqa: F401
    two_plugins,  # noqa: F401
    write_plugin,
)


def _tree(root: Path) -> dict[str, bytes]:
    """一个目录的**逐文件内容**快照（相对路径 → 字节）。"""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }


# ---------------------------------------------------------------------------
# ① 源码目录**真的被删掉**（不做回收站）
# ---------------------------------------------------------------------------


async def test_source_dir_is_really_deleted(two_plugins, data_root):
    """源码目录**真的被删掉**。

    🔴 情绪 2026-10-31 定：**不做回收站、不自动备份**。
       插件代码是可再生的（`metadata.yaml` 的 `repo:` + 「重新安装」一条命令拉回），
       真正不可再生的是配置与持久化数据，那两样由两个勾选项显式决定。

    ⚠️ 判据用 `not exists()`：断言"某个暂存目录存在"在"移走"和"删掉"下都成立，
       抓不住实现被改回移走。
    """
    from astrbot_host.plugin_lifecycle import uninstall_plugin

    host, probe_dir, _bystander = two_plugins
    before = _tree(probe_dir)
    assert "main.py" in before, before

    result = await uninstall_plugin(host, "lc_probe", data_root=data_root)

    assert not probe_dir.exists(), "源码目录还留在原地"
    assert result.source_path == str(probe_dir.resolve()), result.source_path
    # 不该留任何暂存/回收目录（那会变成用户不知道怎么清理的净负债）
    for leftover in ("plugin_trash", "lc_probe-2026"):
        assert not (data_root.parent / leftover).exists(), f"留下了 {leftover}"


async def test_symlinked_plugin_dir_unlinks_only_the_link(two_plugins, data_root, tmp_path):
    """插件目录是**符号链接**时，只删链接本身，**不跟进去删真身**。

    🔴 这条不是洁癖：开发时常见 `plugins/x -> ~/dev/x`。
       `shutil.rmtree` 对指向目录的软链会拒绝或跟进去删，
       跟进去就等于删掉了用户的开发目录 —— 而用户以为只是"卸载一个插件"。
    """
    import os

    from astrbot_host.plugin_lifecycle import uninstall_plugin

    host, probe_dir, _bystander = two_plugins
    real = tmp_path / "real_plugin_src"
    real.mkdir()
    (real / "keep.txt").write_text("真身不许被删", encoding="utf-8")
    # 软链也必须有 main.py —— `resolve_source_dir` 的判据是 `<dir>/main.py` 是文件，
    # 否则整条卸载会以 `unsupported` 拒绝（那是正确行为，但这条判据会变成假红）
    (real / "main.py").write_text("# real plugin\n", encoding="utf-8")

    # 把真实插件目录换成 → real 的软链
    import shutil as _sh
    _sh.rmtree(probe_dir)
    os.symlink(real, probe_dir)

    await uninstall_plugin(host, "lc_probe", data_root=data_root)

    assert not probe_dir.exists(), "软链本身应被移除"
    assert (real / "keep.txt").is_file(), (
        "🔴 跟进去删掉了真身 —— 用户会丢掉自己的开发目录"
    )


# ---------------------------------------------------------------------------
# ② 数据与配置：默认不动，显式要求才**删掉**# ---------------------------------------------------------------------------
# ② 数据与配置：默认不动，显式要求才移走
# ---------------------------------------------------------------------------


async def test_data_and_config_untouched_by_default(two_plugins, data_root):
    """默认**不动** `data/plugins/<id>/` 与配置文件 —— 那是用户的记忆/关系库。"""
    from astrbot_host.plugin_lifecycle import uninstall_plugin

    host, _probe, _bystander = two_plugins
    plugin_data = data_root / "lc_probe"
    plugin_data.mkdir(parents=True, exist_ok=True)
    (plugin_data / "memory.json").write_text('{"keep": true}', encoding="utf-8")
    config_dir = data_root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "lc_probe_config.json"
    config_file.write_text('{"setting": 1}', encoding="utf-8")

    result = await uninstall_plugin(host, "lc_probe", data_root=data_root)

    assert (plugin_data / "memory.json").is_file(), "默认不该动插件数据目录"
    assert config_file.is_file(), "默认不该动插件配置文件"
    assert result.data_dir is None and result.config_file is None


async def test_delete_data_and_config_really_deletes(two_plugins, data_root):
    """显式 `delete_data` / `delete_config` → 一起**真的删掉**（不是移走）。"""
    from astrbot_host.plugin_lifecycle import uninstall_plugin

    host, _probe, _bystander = two_plugins
    plugin_data = data_root / "lc_probe"
    plugin_data.mkdir(parents=True, exist_ok=True)
    (plugin_data / "memory.json").write_text('{"keep": true}', encoding="utf-8")
    config_dir = data_root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "lc_probe_config.json"
    config_file.write_text('{"setting": 1}', encoding="utf-8")

    result = await uninstall_plugin(
        host, "lc_probe", delete_config=True, delete_data=True, data_root=data_root
    )

    assert not plugin_data.exists(), "勾了删数据，但数据目录还在"
    assert not config_file.exists(), "勾了删配置，但配置文件还在"
    # 返回值是**被删掉**的路径（文档必须写"已删除"，不能写"已移走"）
    assert result.data_dir == str(plugin_data), result.data_dir
    assert result.config_file == str(config_file), result.config_file


async def test_delete_data_covers_official_dir_names(two_plugins, data_root):
    """官方那两个数据目录名（`plugin_data` / `plugins_data`）也要删到。

    🔴 守官方那个已知毛病（契约 §0）：`delete_data` 删的是
       `plugin_data/<root_dir_name>`，而 `StarTools.get_data_dir()` 建的是
       `<plugin_name>` ⇒ **目录名 ≠ 插件名时删不到**，用户勾了"删数据"
       磁盘上却还在。我们**候选全删**，不押注某一种命名。
    """
    from astrbot_host.plugin_lifecycle import uninstall_plugin

    host, _probe, _bystander = two_plugins
    for name in ("plugin_data", "plugins_data"):
        d = data_root.parent / name / "lc_probe"
        d.mkdir(parents=True, exist_ok=True)
        (d / "store.json").write_text("{}", encoding="utf-8")

    await uninstall_plugin(host, "lc_probe", delete_data=True, data_root=data_root)

    for name in ("plugin_data", "plugins_data"):
        assert not (data_root.parent / name / "lc_probe").exists(), (
            f"{name}/lc_probe 没被删掉 —— 这就是官方那个毛病"
        )


# ---------------------------------------------------------------------------
# ③ 不误伤：邻居与前缀邻居
# ---------------------------------------------------------------------------


async def test_uninstall_never_touches_another_plugin(three_plugins, data_root):
    """删 A **不许**动 B 与前缀邻居 `lc_probe_extra` 的目录/数据/handler。"""
    from astrbot_host.plugin_lifecycle import uninstall_plugin

    host, _probe, neighbor_dir, bystander_dir = three_plugins
    neighbor_data = data_root / "lc_probe_extra"
    neighbor_data.mkdir(parents=True, exist_ok=True)
    (neighbor_data / "keep.json").write_text("{}", encoding="utf-8")
    neighbor_handlers = owned_handlers_of("lc_probe_extra")
    assert neighbor_handlers

    await uninstall_plugin(host, "lc_probe", data_root=data_root)

    assert (neighbor_dir / "main.py").is_file(), "前缀邻居的源码被动了"
    assert (bystander_dir / "main.py").is_file(), "旁观者的源码被动了"
    assert (neighbor_data / "keep.json").is_file(), "前缀邻居的数据被动了"
    assert owned_handlers_of("lc_probe_extra") == neighbor_handlers, (
        f"前缀邻居的 handler 被误删：{neighbor_handlers - owned_handlers_of('lc_probe_extra')}"
    )


# ---------------------------------------------------------------------------
# ④ 记录 / 报告 / 停用名单
# ---------------------------------------------------------------------------


async def test_records_and_report_drop_the_plugin(two_plugins, data_root):
    """卸载后：`records` 里没有它、`report` 里也不再统计它，邻居仍健在。"""
    from astrbot_host.plugin_lifecycle import uninstall_plugin

    host, _probe, _bystander = two_plugins
    await uninstall_plugin(host, "lc_probe", data_root=data_root)

    assert "lc_probe" not in host.loader.records
    assert "lc_probe" not in host.report.loaded_plugins
    assert "lc_probe" not in [n for n, _ in host.report.failed]
    assert host.report.loaded == 1, host.report.loaded
    assert host.loader.records["lc_bystander"].state == "loaded"


async def test_inactivated_entry_is_removed_through_the_injected_writer(
    two_plugins, data_root
):
    """停用名单里该插件的 `module_path` **被剔除**（否则下次启动报 unmatched）。

    ⚠️ 走**注入的写端口**（不是 mock 掉实现）：这条判据要证明"写配置那条链路
       真的通了"，mock 掉只有"调用过"这个弱证据。
    """
    from astrbot_host.plugin_lifecycle import uninstall_plugin

    host, _probe, _bystander = two_plugins
    written: list[list[str]] = []

    def writer(pid: str) -> str:
        written.append([pid])
        return f"{pid}.main"

    host.inactivated_writer = writer

    result = await uninstall_plugin(host, "lc_probe", data_root=data_root)

    assert written == [["lc_probe"]], written
    assert result.removed_from_inactivated == "lc_probe.main"


async def test_without_writer_it_reports_not_cleaned(two_plugins, data_root):
    """没注入 writer → **如实空串**（不假装清理成功）。"""
    from astrbot_host.plugin_lifecycle import uninstall_plugin

    host, _probe, _bystander = two_plugins
    host.inactivated_writer = None

    result = await uninstall_plugin(host, "lc_probe", data_root=data_root)
    assert result.removed_from_inactivated == ""


# ---------------------------------------------------------------------------
# ⑤ 拒绝执行的情况：磁盘上什么都没动
# ---------------------------------------------------------------------------


async def test_unknown_plugin_changes_nothing_on_disk(two_plugins, data_root):
    """没有这个插件 → `not_loaded`，磁盘上什么都没动。"""
    from astrbot_host.plugin_lifecycle import PluginLifecycleError, uninstall_plugin

    host, probe_dir, bystander_dir = two_plugins
    before = _tree(probe_dir)

    with pytest.raises(PluginLifecycleError) as exc:
        await uninstall_plugin(host, "no_such_plugin", data_root=data_root)

    assert exc.value.code == "not_loaded"
    assert _tree(probe_dir) == before
    assert bystander_dir.is_dir()
    assert not (data_root.parent / "plugin_trash").exists(), "拒绝执行却建了回收站目录"


async def test_unresolvable_source_raises_unsupported_and_touches_nothing(
    two_plugins, data_root
):
    """解析不到源码目录 → `unsupported`，**磁盘上一个字节都没动、宿主也没拆**。

    🔴 这是"删错目录"那一类不可接受错误的最后一道闸。
       构造：把 `requested_dirs` 与 `plugin_dirs` 都清空（= 开了镜像、
       源码根里找不到它的真实情形）。
    """
    from astrbot_host.plugin_lifecycle import PluginLifecycleError, uninstall_plugin

    host, probe_dir, _bystander = two_plugins
    before = _tree(probe_dir)
    handlers_before = owned_handlers_of("lc_probe")

    host.loader.requested_dirs = []
    host.loader.plugin_dirs = []

    with pytest.raises(PluginLifecycleError) as exc:
        await uninstall_plugin(host, "lc_probe", data_root=data_root)

    assert exc.value.code == "unsupported"
    assert _tree(probe_dir) == before, "拒绝执行时动了源码目录"
    assert owned_handlers_of("lc_probe") == handlers_before, (
        "拒绝执行却把宿主拆了（半拆状态）"
    )
    assert "lc_probe" in host.loader.records, "拒绝执行却摘了记录"
    assert not (data_root.parent / "plugin_trash").exists()


async def test_resolve_source_dir_never_falls_back_to_mirror(root_probe_case):
    """`resolve_source_dir` **不许**回落到 `loader.plugin_dirs`（镜像副本）。

    现场：第一版有那个兜底，理由是"万一插件是直接放进 data/plugin_src 的"。
    但开镜像时 `plugin_dirs` 指向副本 ⇒ 照它移走 ⇒ 真源码原地不动 ⇒
    用户看到"删除成功"，**重启后插件又回来了**（下次镜像还会把它再复制一遍）。
    """
    from astrbot_host.plugin_lifecycle import resolve_source_dir

    loader, plugin_id = root_probe_case
    loader.requested_dirs = []
    assert resolve_source_dir(loader, plugin_id) is None, (
        "解析不到源码时回落到了镜像副本"
    )


@pytest.fixture
async def root_probe_case(source_root, data_root):
    """「插件只存在于加载器目录里、源码根里没有」的宿主。"""
    write_plugin(source_root, "lc_probe", PROBE_PLUGIN)
    host = await build_host(source_root, data_root)
    try:
        yield host.loader, "lc_probe"
    finally:
        await teardown_host(host)


# ---------------------------------------------------------------------------
# ⑥ 失败不留半拆状态
# ---------------------------------------------------------------------------


async def test_delete_failure_is_reported_and_plugin_already_detached(
    two_plugins, data_root, monkeypatch
):
    """删目录失败 → `uninstall_failed`，且**插件已从宿主摘干净**（可再删一次）。

    🔴 语义变了（2026-10-31，不再有"移走"这一步）：现在的顺序是
       **先拆内存、再删磁盘**。拆内存不会失败，删磁盘才会。
       所以失败时的状态是「宿主已摘干净 + 目录还在」——
       用户看到的是"插件还在磁盘上但已不运行"，**可以再删一次**；
       而不是旧写法的"注册表空了、目录还在、重启后又冒出来"那种鬼影。

    ⚠️ 这条判据同时守住"顺序没被改回去"：
       若有人把删目录挪到拆解之前，`detached` 就会是 0、且插件仍在 records 里 ⇒ 红。
    """
    from astrbot_host import plugin_lifecycle

    host, probe_dir, _bystander = two_plugins
    handlers_before = owned_handlers_of("lc_probe")
    assert handlers_before, "前置：插件本来没注册 handler，判据无效"

    def boom(path):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(plugin_lifecycle, "_remove_tree", boom)

    with pytest.raises(plugin_lifecycle.PluginLifecycleError) as exc:
        await plugin_lifecycle.uninstall_plugin(
            host, "lc_probe", data_root=data_root
        )
    assert exc.value.code == "uninstall_failed"
    assert "删除源码目录失败" in exc.value.message, exc.value.message

    # 目录被护住了（我们注入的 boom 让删除失败）
    assert probe_dir.is_dir(), "注入的失败没生效，判据无效"
    # 但插件**已从宿主摘干净**
    assert "lc_probe" not in host.loader.records, "失败后记录还在 —— 顺序可能被改回去了"
    assert not owned_handlers_of("lc_probe"), "失败后 handler 还挂着"



# ---------------------------------------------------------------------------
# ⑦ 装配：`PluginHost` 的薄方法必须真委托
# ---------------------------------------------------------------------------


async def test_host_thin_method_delegates_to_lifecycle(two_plugins, data_root):
    """`host.uninstall_plugin()` 是**真委托**（参数也要透到底）。"""
    from astrbot_host import plugin_lifecycle

    host, _probe, _bystander = two_plugins
    calls: list[dict] = []
    original = plugin_lifecycle.uninstall_plugin

    async def spy(host_arg, plugin_id, **kwargs):
        calls.append({"plugin_id": plugin_id, **kwargs})
        return await original(host_arg, plugin_id, **kwargs)

    plugin_lifecycle.uninstall_plugin = spy
    try:
        # ⚠️ 薄方法不接 `data_root`（那是判据专用的隔离参数，生产走
        #    `star_tools.get_data_root()`）—— 这里把它钉到临时目录。
        from astrbot_host.compat.core.star.star_tools import set_data_root

        set_data_root(data_root)
        result = await host.uninstall_plugin("lc_probe", delete_data=True)
    finally:
        plugin_lifecycle.uninstall_plugin = original

    assert calls == [
        {"plugin_id": "lc_probe", "delete_config": False, "delete_data": True}
    ], calls
    # 薄方法走的是真实现 ⇒ 源码目录应当已被删掉
    assert result.source_path, result.source_path


# ---------------------------------------------------------------------------
# ⑧ `source_path` 必须是**绝对路径**（契约 §1）
# ---------------------------------------------------------------------------


async def test_source_path_is_absolute_when_config_uses_relative_dirs(tmp_path, monkeypatch):
    """配置里的 `plugin_dirs` 是**相对路径**时，`source_path` 仍须是绝对路径。

    🔴 现场（2026-10-31 实测，契约偏差）：`EMOTION_PLUGINS_PLUGIN_DIRS=plugins`
       是**默认值**，也就是相对路径。原样拼给面板，`source_path` 会是
       `plugins/lc_probe` —— 相对路径的基准是**进程 cwd**。
       而删除二次确认弹窗就是拿这个字段告诉用户"我删的是哪一份"：
       服务从别的目录起时（`setsid ... --chdir` 不确定），面板显示的和
       真正会被移走的东西**不是同一个**。

       契约 §1 的样例是绝对路径，所以判据必须钉住"绝对 + 指向真实目录"。
    """
    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.compat.core.star.star_tools import set_data_root
    from astrbot_host.host_app import PluginHost

    # 造一个"cwd 之下才有 plugins/"的现场，然后**切到那个 cwd**
    workdir = tmp_path / "cwd"
    (workdir / "plugins").mkdir(parents=True)
    (workdir / "data" / "plugins").mkdir(parents=True)
    write_plugin(workdir / "plugins", "lc_probe", PROBE_PLUGIN)

    monkeypatch.chdir(workdir)
    set_data_root(workdir / "data" / "plugins")
    host = PluginHost(plugin_dirs=[Path("plugins")], auto_initialize=False)
    await host.start(ports=HostPorts(config={}))
    try:
        entry = {p["id"]: p for p in host.plugins_view()}["lc_probe"]
        source = Path(entry["source_path"])
        assert source.is_absolute(), (
            f"source_path 不是绝对路径：{entry['source_path']!r}"
            "（相对路径的基准是进程 cwd，面板据此显示的位置会漂）"
        )
        assert source.is_dir(), f"source_path 指向的目录不存在：{source}"
        assert source.name == "lc_probe", source
        # 两个"勾了才会删"的路径同理：面板确认框要显示绝对路径，
        # 相对路径的基准是进程 cwd ⇒ 用户看到的位置会随启动目录漂。
        assert Path(entry["config_path"]).is_absolute(), entry["config_path"]
        assert Path(entry["data_path"]).is_absolute(), entry["data_path"]
    finally:
        await teardown_host(host)
