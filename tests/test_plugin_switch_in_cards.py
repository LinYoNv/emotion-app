# -*- coding: utf-8 -*-
"""判据：启停开关住在**插件卡片上**，且**即时生效**（没有"保存"那一步）。

## 现场（情绪 2026-11-07）

> 「上面那节"启停"好丑，按图二那样加个开关不就好了」
> 「不对不对，启停开关你直接放卡片上啊哇笨笨」

原来是**独立一整个区块**（复选框列表 + 底部「保存启停设置」按钮），
而下面「插件升级」的卡片**用的是同一份数据**（`state.pluginSwitch`）——
同一个插件要在两处看、两处改。

⇒ 合成一张卡片：**启停开关 + 详情 + 配置 + 升级，全在一个卡片里。**

## 本文件守什么

| # | 判据 |
|---|---|
| ① | 独立启停区块的元素**必须已删除**（否则两处并存又漂） |
| ② | 卡片里必须有开关（`togglePluginEnabled` 真的被绑上） |
| ③ | 开关**即时生效**，不许再有「保存启停设置」这种两步操作 |
| ④ | 失败时**必须把开关拨回去**（否则界面显示改了、服务端没改 = 假成功） |
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "emotion_app" / "web" / "static" / "js"
HTML = ROOT / "emotion_app" / "web" / "static" / "index.html"


def test_standalone_switch_block_is_gone():
    """① 独立启停区块必须已删除。

    ⚠️ 留着它 = 同一份数据两个视图。用户在一个地方关掉、另一个地方还显示开着，
       而**两边都不报错** —— 比没有更难查。
    """
    html = HTML.read_text(encoding="utf-8")
    for dead in ('id="plugin-switch-list"', 'id="plugin-switch-save"'):
        assert dead not in html, f"{dead} 还在 —— 启停又变成两个入口了"


def test_toggle_lives_inside_plugin_card():
    """② 开关必须渲染在**卡片**里，且处理函数真的接上。

    ⚠️ 只查"有 toggle 类名"抓不住（首页别处也可能有）；
       必须查它被 append 到 `titleRow`（= 卡片标题行）。
    """
    src = (JS / "views" / "plugins.js").read_text(encoding="utf-8")
    i = src.find("export function renderPluginUpdate")
    assert i > 0
    seg = src[i:i + 9000]
    assert "plugin-card-toggle" in seg, "卡片里没有开关"
    assert "titleRow.appendChild(toggle)" in seg, (
        "开关没挂到卡片标题行 —— 那就不算'放卡片上'"
    )
    assert "togglePluginEnabled(p, cb, toggle)" in seg, (
        "开关没绑处理函数 —— 点了没反应"
    )


def test_switch_is_instant_not_two_step():
    """③ 即时生效：不许再有"保存启停设置"那种两步操作。

    🔴 同一屏里"这个开关要保存、那个按钮即时生效"是灾难 —— 用户必然搞混。
    """
    boot = (JS / "bootstrap.js").read_text(encoding="utf-8")
    # ⚠️ 必须**剔除注释行**再查 —— 我在那里留了一行说明"`#plugin-switch-save` 已删除"，
    #    不剔掉的话它自己就把判据弄红了（这类"注释里提到就红"的判据很常见）。
    code_lines = [
        line for line in boot.splitlines()
        if not line.lstrip().startswith(("//", "*", "/*"))
    ]
    assert not any("plugin-switch-save" in line for line in code_lines), (
        "bootstrap 还在绑已删除的保存按钮"
    )
    src = (JS / "views" / "plugins.js").read_text(encoding="utf-8")
    assert "cb.addEventListener(\"change\"" in src, "开关没有 change 监听（不是即时生效）"


def test_failed_toggle_reverts_the_switch():
    """④ 失败必须**拨回去**。

    🔴 否则界面显示"已停用"而服务端根本没改 —— **假成功**，
       用户以为生效了，下次重启才发现不对。本项目最忌的那类 bug。
    """
    src = (JS / "views" / "plugins.js").read_text(encoding="utf-8")
    i = src.find("export async function togglePluginEnabled")
    assert i > 0
    seg = src[i:i + 2200]
    assert "cb.checked = !wantEnabled" in seg, (
        "失败没有把开关拨回去 —— 会留下一个'看起来已改'的开关"
    )
