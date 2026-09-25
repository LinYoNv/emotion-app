# -*- coding: utf-8 -*-
"""真判据：**加载磁盘上全部插件**（不只是陪伴）。

⭐⭐ 为什么需要这个测试（实测教训）：
   `test_loader_real_plugin.py` 只加载陪伴主插件，它是"重 API 面"但**恰好没用**
   某些写法。而实测发现另外 4 个插件因为三种**不同的** API 缺口加载失败：

   | 缺口 | 受害插件 | 根因 |
   |---|---|---|
   | `register_star` 签名错 | prompt_tools / aitag_uploader / content / image | 我写成 2 参数，真实现是 5 参数 |
   | 缺 `MessageEventResult` 导出 | comfyui_pro / memory_companion | `api/event` 没 re-export |
   | 缺 `astrbot.api.all` | meme_manager | 整个模块没实现 |
   | 缺 `astrbot.api.web` | together_companion | 整个模块没实现 |
   | `command_group` 返回值错 | memory_companion / prompt_tools | 应返回 `RegisteringCommandable` |

   → **教训：只测一个插件会漏掉大量 API 缺口。**
     兼容层的验收必须是"**多插件同时加载**"。

判据：
  * **≥11 个插件加载成功**（磁盘共 12 个；唯一失败的是第三方图床 meme_manager，
    缺 `requests` 等它自己的依赖，**不是我们的 API 缺口**）
  * 5 个陪伴系插件**全部成功**
  * 累计 handler ≥180（实测 203）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HOST_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOST_DIR))

PLUGIN_ROOT = Path("/opt/astrbot/data/plugins")

pytestmark = pytest.mark.skipif(
    not PLUGIN_ROOT.is_dir(), reason=f"插件目录不存在：{PLUGIN_ROOT}"
)

#: 陪伴系插件（必须全部加载成功）
COMPANION_PLUGINS = (
    "astrbot_plugin_private_companion",
    "astrbot_plugin_memory_companion",
    "astrbot_plugin_image_companion",
    "astrbot_plugin_content_companion",
    "astrbot_plugin_together_companion",
)

#: 已知的"插件自身依赖缺失"（不是我们的兼容缺口）——
#: 列出来是为了**区分**"我们没实现"与"它缺库"。
KNOWN_ENV_FAILURES = {
    "meme_manager": "第三方图床插件，缺 requests/boto3 等自身依赖",
}


@pytest.fixture(scope="module")
def report():
    import asyncio
    import tempfile
    from pathlib import Path as _Path

    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.loader import PluginLoader
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()
    # 🔴 **必须镜像**（`mirror_root=`）—— 否则这个测试会**改写生产插件目录**。
    #    实测事故：`comfyui_pro.__init__` 会把 workflow 列表回写
    #    自己源码目录里的 `_conf_schema.json`。而 `PLUGIN_ROOT` 是**别人正在跑的
    #    AstrBot 生产目录** → 我们每跑一次测试就覆盖一次对方的配置，
    #    偏偏**35 项测试全绿、毫无提示**（污染完全静默）。
    #    镜像后加载的是副本，源目录只读。
    mirror = _Path(tempfile.mkdtemp(prefix="host-test-mirror-"))
    loader = PluginLoader(plugin_dirs=[PLUGIN_ROOT], mirror_root=mirror)
    return asyncio.run(loader.load_all(ports=HostPorts(config={})))


def test_most_plugins_load(report):
    """≥11 个插件加载成功（磁盘共 12 个）。"""
    assert len(report.loaded) >= 11, (
        f"只成功 {len(report.loaded)} 个：{report.summary()}"
    )


def test_all_companion_plugins_load(report):
    """⭐ **5 个陪伴系插件必须全部成功**（这是本项目的核心判据）。"""
    ok = {r.dir_name for r in report.loaded}
    missing = [p for p in COMPANION_PLUGINS if p not in ok]
    assert not missing, f"陪伴系插件未加载：{missing}\n{report.summary()}"


def test_only_known_env_failures(report):
    """失败的必须只是"插件自身缺依赖"，**不能是我们的 API 缺口**。

    ⚠️ 这条判据很重要：它防止我们把"兼容层没实现"当成"插件有问题"糊过去。
    """
    unexpected = [
        r for r in report.failed
        if r.dir_name not in KNOWN_ENV_FAILURES
        and "No module named" not in (r.error or "")
    ]
    assert not unexpected, (
        "出现非依赖类失败（很可能是我们的 API 缺口）：\n"
        + "\n".join(f"  {r.name}: {r.error}" for r in unexpected)
    )


def test_total_handler_count(report):
    """累计 handler ≥180（实测 203）—— 证明装饰器机制被大面积用上。"""
    total = sum(r.handler_count for r in report.loaded)
    assert total >= 180, f"累计 handler 只有 {total}"


def test_web_apis_attributed_per_plugin(report):
    """⭐ Web API 必须**按插件归属**统计，不能取全局累计值。

    🔴 这条判据来自一个**实测踩到的真 bug**：原实现用
       `len(context.registered_web_apis)` —— 那是**累计值**，
       于是报告里 prompt_tools（21 个 handler）却显示 **266 个 API**，
       而 266 其实是"前面所有插件加起来的数"。报告虚高且**无法定位**。

    判据：
      * 主插件（private_companion）API ≥150 —— 它是真大户
      * prompt_tools 的 API **必须远小于**主插件（它实际登记 0 个）
      * **各插件之和 == 全局总数**（自洽性 —— 能抓住"归属丢失"）
    """
    by_dir = {r.dir_name: r for r in report.loaded}
    main = by_dir["astrbot_plugin_private_companion"]
    prompts = by_dir["prompt_tools"]

    assert main.web_api_count >= 150, f"主插件 API 只有 {main.web_api_count}"
    assert prompts.web_api_count < main.web_api_count, (
        "prompt_tools 的 API 数不该 ≥ 主插件 —— 这几乎肯定是又取了全局累计值"
    )
    total = sum(r.web_api_count for r in report.loaded)
    assert total >= 260, f"各插件 API 之和只有 {total}（实测 266）"


def test_web_apis_are_real_route_tuples():
    """登记的 API 必须是真实现那种 `(route, handler, methods, desc)` 结构。

    ⚠️ 真消费者 `dashboard/api/plugins.py:196` **按属性读、按元组解包**；
       我们若存成 dict 列表，将来挂载时会在解包处炸。
    """
    from astrbot_host.compat.core.star.context import Context

    ctx = Context()
    ctx.set_current_plugin("demo_plugin")

    async def _handler(**_kw):
        return {}

    ctx.register_web_api("/demo/hello", _handler, ["GET"], "示例")
    apis = ctx.registered_web_apis
    assert len(apis) == 1
    route, handler, methods, desc = apis[0]  # 必须能按真实现的顺序解包
    assert (route, handler, methods, desc) == ("/demo/hello", _handler, ["GET"], "示例")

    # 归属可查（我们的扩展）——这是"报告不虚高"的基础
    assert len(ctx.registered_web_apis_of("demo_plugin")) == 1
    assert len(ctx.registered_web_apis_of("someone_else")) == 0


def test_web_api_duplicate_route_replaces():
    """相同 `(route, methods)` 重复登记必须**替换**，不是追加。

    照真实现 `context.py:723-727`（注释明写"会替换现有的 API"）——
    漏了去重会让面板路由出现重复条目。
    """
    from astrbot_host.compat.core.star.context import Context

    ctx = Context()
    ctx.set_current_plugin("demo_plugin")

    async def _first(**_kw):
        return {"n": 1}

    async def _second(**_kw):
        return {"n": 2}

    ctx.register_web_api("/demo/x", _first, ["GET"], "第一次")
    ctx.register_web_api("/demo/x", _second, ["GET"], "第二次")
    assert len(ctx.registered_web_apis) == 1, "重复路由没有被替换"
    assert ctx.registered_web_apis[0][1] is _second, "替换后应保留**后**登记的实现"

    # 不同方法算不同 API（不能过度去重）
    ctx.register_web_api("/demo/x", _second, ["POST"], "另一个方法")
    assert len(ctx.registered_web_apis) == 2


def test_registered_web_apis_is_a_live_list():
    """`registered_web_apis` 必须是**可写的活列表**，不是只读副本。

    🔴 真实现是普通属性（`context.py:126`），消费者/插件可直接 append。
       我们若做成 property 返回副本，外部 append 会被**静默丢弃**
       —— 正是本项目最贵的 bug 类型（静默失败）。
    """
    from astrbot_host.compat.core.star.context import Context

    ctx = Context()

    async def _h(**_kw):
        return {}

    ctx.registered_web_apis.append(("/demo/direct", _h, ["GET"], "直接追加"))
    # 直接追加的必须能被读到（没被丢弃）
    assert len(ctx.registered_web_apis) == 1
    # 且后续 register_web_api 不能把它冲掉（归属表对不齐也不能抛异常）
    ctx.set_current_plugin("p1")
    ctx.register_web_api("/demo/normal", _h, ["GET"], "正常登记")
    assert len(ctx.registered_web_apis) == 2
    assert len(ctx.registered_web_apis_of("p1")) == 1
    assert ctx.web_api_owners().get("p1") == 1


def test_main_companion_plugin_specifics(report):
    """主插件的关键数字（防止回归）。"""
    rec = next(
        r for r in report.loaded
        if r.dir_name == "astrbot_plugin_private_companion"
    )
    assert rec.version == "6.6.2"
    assert rec.handler_count >= 100
    assert rec.star_cls is not None


