# -*- coding: utf-8 -*-
"""🔴 **插件目录必须可扩展**：放一个新插件进去、重启就自动加载。

情绪 2026-10-10：「你别忘记了啊，别把插件这块写死后续可能还要装插件的。」

## 这条判据守的是什么（都是"写死"的常见形态）
  ① **不许有插件白名单/硬编码名单** —— `discover()` 必须是**扫目录**；
  ② **`plugin_dirs` 必须是"目录列表"**（分号分隔），能同时挂多个来源；
  ③ **放一个新目录进去就会被发现** —— 用 tmp_path 造假插件实测，不靠读代码断言。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "host"))

from astrbot_host.loader.plugin_loader import PluginLoader  # noqa: E402


def _fake_plugin(root: Path, name: str) -> Path:
    """造一个最小可用插件（只要 `main.py` + 一个 Star 类）。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "main.py").write_text(
        "from astrbot.api.star import Context, Star\n"
        "from astrbot.api.event import filter\n\n"
        "class P(Star):\n"
        "    def __init__(self, context: Context):\n"
        "        super().__init__(context)\n\n"
        "    @filter.command('hello')\n"
        "    async def hello(self, event):\n"
        "        pass\n",
        encoding="utf-8",
    )
    (d / "metadata.yaml").write_text(
        f"name: {name}\ndisplay_name: 测试插件{name}\nversion: 9.9.9\n",
        encoding="utf-8",
    )
    return d


def test_discover_finds_any_plugin_dir_not_a_whitelist(tmp_path):
    """🔴 **新放进去的插件必须被发现** —— 没有任何名单可对。

    ⚠️ 用"造一个从没见过的名字"来测：如果哪天有人把 `discover()` 改成
       只认某个白名单/只认 `astrbot_plugin_*` 前缀，这条立刻红。
    """
    root = tmp_path / "plugins"
    root.mkdir()
    _fake_plugin(root, "totally_new_plugin_xyz")
    _fake_plugin(root, "another_one_2026")

    loader = PluginLoader(plugin_dirs=[root])
    found = {p.name for p in loader.discover()}
    assert found == {"totally_new_plugin_xyz", "another_one_2026"}, (
        f"discover 没扫到全部插件（是不是被改成白名单了？）：{found}"
    )


def test_discover_supports_multiple_dirs(tmp_path):
    """🔴 `plugin_dirs` 支持**多个目录**（换设备/分来源时要用）。

    ⚠️ 这条守的是"能同时挂我们的目录 + 别人的目录"这种真实场景。
    """
    a = tmp_path / "ours"
    b = tmp_path / "theirs"
    a.mkdir(); b.mkdir()
    _fake_plugin(a, "plugin_in_ours")
    _fake_plugin(b, "plugin_in_theirs")

    loader = PluginLoader(plugin_dirs=[a, b])
    found = {p.name for p in loader.discover()}
    assert found == {"plugin_in_ours", "plugin_in_theirs"}, found


def test_discover_skips_dirs_without_main_py(tmp_path):
    """⚠️ 没有 `main.py` 的目录**不算插件**（照真实现）。

    ⚠️ 反面用例同样重要：否则"扫目录"会被误实现成"列所有子目录"，
       而 `pages/`、`data/` 这类目录会被当成插件去加载（然后报一堆错）。
    """
    root = tmp_path / "plugins"
    root.mkdir()
    _fake_plugin(root, "real_one")
    (root / "not_a_plugin").mkdir()                 # 没有 main.py
    (root / ".hidden").mkdir()                      # 点开头，跳过
    (root / "_private").mkdir()                     # 下划线开头，跳过

    loader = PluginLoader(plugin_dirs=[root])
    assert {p.name for p in loader.discover()} == {"real_one"}


def test_plugin_dirs_config_is_a_semicolon_list():
    """🔴 配置里的 `plugin_dirs` 是**分号分隔的列表**，不是写死的单一路径。

    ⚠️ 这条是"不写死"在**配置层**的落点：以后要加插件来源，
       只需在 `.env` 里多写一段（`a;b`），**不用改代码**。
    """
    from emotion_app.infra.config import Config

    cfg = Config()
    cfg.plugins.plugin_dirs = "/one;/two ; /three"
    assert cfg.plugins.dirs() == ["/one", "/two", "/three"]

    # 空项要被丢掉（`a;;b`、结尾分号都不能产出空字符串路径）
    cfg.plugins.plugin_dirs = "/a;;/b;"
    assert cfg.plugins.dirs() == ["/a", "/b"]


def test_plugin_directory_is_a_single_nonempty_source():
    """🔴 **我们的插件源目录只有一个、且不空** —— 情绪 2026-10-10：
    「怎么会有两个插件目录嘛哎哟你真是的留一个」。

    ⚠️ 这条守的是一次真实的失误：`.env` 曾指向 `/opt/astrbot/data/plugins`
        （**别人 AstrBot 的生产目录**），于是 14 个跟我们无关的插件被加载、
        还出现在我们面板上（含一个缺依赖、点进去一片空白的）。

    ⚠️ 判据的路径**从配置取**（不是写死 `plugins/`）—— 否则换个部署形态就假红；
       但它必须落在**本仓库之内**：指向别处就说明又把别人的目录挂进来了。
    """
    from emotion_app.infra.config import Config

    cfg = Config()
    cfg.plugins.plugin_dirs = "plugins"          # 不读用户的 .env，前提自造
    dirs = cfg.plugins.dirs()
    assert len(dirs) == 1, f"插件源目录不止一个（情绪要求只留一个）：{dirs}"

    root = (ROOT / dirs[0]).resolve()
    assert root.is_dir(), f"插件源目录不存在：{root}"
    assert root.is_relative_to(ROOT.resolve()), (
        f"插件源目录跑到仓库外面去了：{root} —— 这会把别人的插件当成我们的加载"
    )

    names = sorted(p.name for p in root.iterdir() if p.is_dir() and (p / "main.py").is_file())
    assert names, f"插件源目录是空的：{root}"
