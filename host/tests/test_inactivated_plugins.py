# -*- coding: utf-8 -*-
"""`inactivated_plugins`（停用插件）判据 —— 照真 AstrBot 的语义。

## 真语义的依据（本机真源码副本，只读引用）

    /root/.local/share/uv/tools/astrbot/lib/python3.12/site-packages/astrbot/

  * `core/star/star_manager.py:1087` —— `inactivated_plugins = await
    sp.global_get("inactivated_plugins", [])`（全局偏好里的一个 list）；
  * `:1115` —— 元素形状：`path = "data.plugins." + root_dir_name + "." + module_str`
    （= **模块路径**，不是目录名）；
  * `:1218` / `:1321` —— `if path not in inactivated_plugins:` **才实例化**；
  * `:1240` —— 命中的分支是 `metadata.star_cls = None` +
    `logger.info("Plugin %s is disabled.")` ⇒ **不是失败路径**；
  * `:1251` `plugin_disabled = metadata.module_path in inactivated_plugins`
    → handler 不绑实例、LLM 工具 `active=False`（`:1290`）；
  * `dashboard/services/plugin_page_service.py:237/360` —— 未启用的插件
    连页面都不给（"插件未启用" / 403 "Plugin is disabled"）。

## 本文件的判据守什么

停用与"加载失败"是**两条不同的路径** —— 这是本能力最容易写错的地方：
把它写成 `error` 或让它掉进 `failed`，用户就会看到"我明明停用了，
它却报加载失败"，然后去查一个不存在的故障。
所以下面每条都要能失败（见交付报告里的注入验证）。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest

HOST_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOST_DIR))

from astrbot_host.loader.plugin_loader import (  # noqa: E402
    PluginLoader,
    entry_module_path,
    normalize_module_path,
)

#: 插件源码模板：`MARK` 是"这个模块被 import 过"的唯一证据。
GOOD_PLUGIN = """\
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

MARK = "imported:%(name)s"


class Probe(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.initialized = False

    @filter.command("回声")
    async def echo_cmd(self, event: AstrMessageEvent, word: str = "默认"):
        yield event.plain_result(f"收到:{word}")

    async def initialize(self) -> None:
        self.initialized = True
        MARK_FILE.write_text("initialized", encoding="utf-8")


# 放在类之后：initialize() 里要用（模块级名字）
MARK_FILE = __import__("pathlib").Path(__file__).with_name("_initialized.flag")
"""


def _write_plugin(root: Path, name: str, body: str = GOOD_PLUGIN) -> Path:
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "metadata.yaml").write_text(
        f"name: {name}\nversion: 9.9\nauthor: t\ndesc: t\n", encoding="utf-8"
    )
    (pkg / "main.py").write_text(body % {"name": name}, encoding="utf-8")
    return pkg


@pytest.fixture
def plugin_root(tmp_path):
    """三个插件：两个正常 + 一个 **import 就炸**（用来证明"真的没被 import"）。"""
    # 🔴 必须先装影子包：插件的 `from astrbot...` 写在真 AstrBot 的 API 上
    #    （与其它 host 测试同一口径，不在这里另造一套插件写法）。
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()

    root = tmp_path / "plugins"
    root.mkdir()
    _write_plugin(root, "alpha_probe")
    _write_plugin(root, "beta_probe")
    _write_plugin(
        root,
        "gamma_broken",
        body=(
            'raise RuntimeError("这个插件 import 就炸：%(name)s")\n'
        ),
    )
    return root


def _load(root: Path, **kw):
    loader = PluginLoader(plugin_dirs=[str(root)], **kw)
    return loader, asyncio.run(loader.load_all())


# ===========================================================================
# ① 名单命中：不进 loaded、不进 failed、进 inactivated
# ===========================================================================


def test_inactivated_is_neither_loaded_nor_failed(plugin_root):
    """🔴 核心判据：停用的插件**必须**既不在 `loaded`、也不在 `failed`。"""
    loader, rep = _load(plugin_root, inactivated=[entry_module_path("gamma_broken")])

    assert [r.dir_name for r in rep.loaded] == ["alpha_probe", "beta_probe"], (
        f"loaded 不对：{[r.dir_name for r in rep.loaded]}"
    )
    assert [r.dir_name for r in rep.failed] == [], (
        f"停用的插件掉进了 failed（两条路径混了）：{[r.dir_name for r in rep.failed]}"
    )
    assert [r.module_path for r in rep.inactivated] == ["gamma_broken.main"], (
        f"inactivated 不对：{[r.module_path for r in rep.inactivated]}"
    )
    rec = loader.records["gamma_broken"]
    assert rec.state == "inactivated"
    assert rec.error == "", "停用不是错误 —— error 必须为空"
    assert rec.instance is None
    # 状态报告里必须能区分（铁律：不许静默）
    assert "已停用" in rep.summary()
    assert "gamma_broken.main" in rep.summary()


def test_inactivated_plugin_is_never_imported(plugin_root):
    """🔴 停用 = **真的一行代码都没跑**：那个插件 import 就 raise。

    如果实现改成"import 了但不实例化"（真 AstrBot 的做法），这条会失败 ——
    那是**有意的**：我们的加载器把"不加载"做到更彻底（少一类噪音：
    `插件加载失败：meme_manager → No module named 'requests'`）。
    """
    _, rep = _load(plugin_root, inactivated=["gamma_broken"])
    assert [r.dir_name for r in rep.loaded] == ["alpha_probe", "beta_probe"]
    assert rep.failed == []
    # 真被 import 过的话，这里会是一条 "RuntimeError: 这个插件 import 就炸"
    assert "gamma_broken" not in sys.modules, "停用的插件被 import 了"


def test_initialize_is_not_called_for_inactivated(plugin_root):
    """停用 → `initialize()` 也不许被调用（这是"20s 超时"那类噪音的来源）。"""
    loader, rep = _load(plugin_root, inactivated=["alpha_probe"])

    async def run():
        return await loader.initialize_all(rep)

    failed = asyncio.run(run())
    assert failed == []
    assert not (plugin_root / "alpha_probe" / "_initialized.flag").exists(), (
        "被停用的插件竟然跑了 initialize()"
    )
    # 对照组：没停用的那个必须真跑过（否则"没跑"可能只是因为整条链坏了）
    assert (plugin_root / "beta_probe" / "_initialized.flag").is_file()


# ===========================================================================
# ② 反向判据：名单为空 → 行为与加这个能力之前**逐字一致**
# ===========================================================================


def test_empty_list_behaves_exactly_as_before(plugin_root):
    """🔴 反向判据：不传 / 传空名单，加载结果必须与"没有这个能力"时一致。"""
    _, baseline = _load(plugin_root)
    _, empty = _load(plugin_root, inactivated=[])
    _, none_like = _load(plugin_root, inactivated=["", "   "])

    def snap(rep):
        return (
            [(r.dir_name, r.state) for r in rep.records],
            [r.dir_name for r in rep.loaded],
            [r.dir_name for r in rep.failed],
            [r.module_path for r in rep.inactivated],
            rep.unmatched_inactivated,
        )

    assert snap(empty) == snap(baseline)
    assert snap(none_like) == snap(baseline)
    assert [r.dir_name for r in baseline.loaded] == ["alpha_probe", "beta_probe"]
    assert [r.dir_name for r in baseline.failed] == ["gamma_broken"]
    assert baseline.inactivated == []
    # 摘要里不许凭空多出"已停用"那一行（噪音）
    assert "已停用" not in baseline.summary()


# ===========================================================================
# ③ 元素形状：三种写法都要命中（含真 AstrBot 那种）
# ===========================================================================


@pytest.mark.parametrize(
    "written",
    [
        "beta_probe.main",                     # 我们自己的规范写法
        "beta_probe",                          # 只写目录名
        "data.plugins.beta_probe.main",        # **真 AstrBot 的写法**（可直接抄）
    ],
)
def test_three_accepted_spellings(plugin_root, written):
    _, rep = _load(plugin_root, inactivated=[written])
    assert [r.module_path for r in rep.inactivated] == ["beta_probe.main"], (
        f"写法 {written!r} 没命中"
    )
    assert [r.dir_name for r in rep.loaded] == ["alpha_probe"]


def test_normalize_does_not_over_match():
    """归一化的边界：别把别的插件也拉进来（`beta_probe` != `beta_probe2`）。"""
    assert normalize_module_path("beta_probe") == "beta_probe.main"
    assert normalize_module_path("beta_probe.main") == "beta_probe.main"
    assert normalize_module_path("data.plugins.beta_probe.main") == "beta_probe.main"
    assert normalize_module_path("beta_probe2") == "beta_probe2.main"
    assert normalize_module_path("") == ""
    assert normalize_module_path(None) == ""
    assert normalize_module_path("   ") == ""


def test_prefix_stripping_is_not_greedy(plugin_root):
    """只在**开头**剥 `data.plugins.` 前缀 —— 名字里含它不算。"""
    _, rep = _load(plugin_root, inactivated=["beta_probe.data.plugins.main"])
    assert rep.inactivated == [], "把名字里含 data.plugins. 的项错当成了真前缀"


# ===========================================================================
# ④ 名单里对不上任何插件的项：**必须响亮**（不许静默忽略）
# ===========================================================================


def test_unmatched_entries_are_reported_and_logged(plugin_root, caplog):
    """拼错的项要出现在报告里 + 一条 WARNING —— 否则用户以为"已经停用了"。"""
    with caplog.at_level(logging.WARNING, logger="astrbot.host.loader"):
        _, rep = _load(plugin_root, inactivated=["nosuch_plugin"])
    assert rep.unmatched_inactivated == ["nosuch_plugin.main"]
    assert "nosuch_plugin.main" in rep.summary()
    assert any("对不上任何插件" in r.message for r in caplog.records), (
        f"没有打 WARNING：[r.message for r in caplog.records]"
    )


def test_matching_entries_are_not_reported_as_unmatched(plugin_root):
    _, rep = _load(plugin_root, inactivated=["alpha_probe"])
    assert rep.unmatched_inactivated == []


def test_partial_load_does_not_fake_unmatched(plugin_root):
    """`only=` 是调试用的部分加载，**不算** unmatched（否则天天误报）。"""
    loader = PluginLoader(plugin_dirs=[str(plugin_root)], inactivated=["beta_probe"])
    rep = asyncio.run(loader.load_all(only=["alpha_probe"]))
    assert rep.unmatched_inactivated == []
    assert [r.dir_name for r in rep.loaded] == ["alpha_probe"]


# ===========================================================================
# ⑤ 判据只有一份：加载器直接问它，不在别处另写比较
# ===========================================================================


def test_is_inactivated_uses_the_single_normalizer(plugin_root):
    loader = PluginLoader(
        plugin_dirs=[str(plugin_root)],
        inactivated=["data.plugins.alpha_probe.main"],
    )
    assert loader.is_inactivated("alpha_probe.main") is True
    assert loader.is_inactivated("beta_probe.main") is False
    # 名单元素被归一到**和 handler 归属同一个字符串**（不是目录名）
    assert loader.inactivated == {"alpha_probe.main"}
