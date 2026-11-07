# -*- coding: utf-8 -*-
"""判据：插件配置页的**按钮承诺必须兑现** + 保存后**真的热重载**。

## 现场（情绪 2026-11-07）

> 「我在"插件-nai生图"的设置页面配置的东西貌似都无法保存，当我点击"保存并关闭"时
>  会刷新一次但是并没有关闭设置页，我切换到独立webui测试生图时，提示为保存token密钥。」

实测结论（别被表象骗）：
* **后端保存是好的** —— 磁盘文件里值确实写进去了，接口也读得回来；
* 真正的问题有两个：
  1. **按钮叫「保存并关闭」，但它从来没关闭** —— 用户看到页面闪一下还停在那儿，
     自然判断成"没保存成功"；
  2. **保存后不重载插件** —— 插件在 `__init__` 里就把配置读进了属性，
     不重载 = 用户改的不生效。实测生产日志：
        服务启动 22:12:59 → 插件初始化 token=MISSING
        用户保存 23:50:36 → 磁盘和接口都写进去了
        但插件内存里 `self.image_gen_key` 一直是空 ⇒ 独立 webui 报"未配置 token"

## 本文件守什么

| # | 判据 |
|---|---|
| ① | 「保存并关闭」必须真关闭（`go("plugins")`） |
| ② | 关闭**不能把提示弄丢** —— 要落到列表页那个常驻提示位 |
| ③ | 保存后必须调重载（`self.reload_plugin`），且用**已有的那个方法** |
| ④ | 文案必须说"已热重载"（不许再说"尚未实现"这种过期自述） |
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOT = ROOT / "emotion_app" / "web" / "static" / "js" / "bootstrap.js"
HOST = ROOT / "host" / "astrbot_host" / "host_app.py"


def test_save_and_close_actually_closes():
    """① 按钮叫「保存并关闭」，就必须真的关。

    ⚠️ 只查"有 go(...)"抓不住 —— `getSession`/别处也可能调。
       必须查它出现在**保存那段**里（`plugin-config-save` 的 handler 中）。
    """
    src = BOOT.read_text(encoding="utf-8")
    i = src.find('$("plugin-config-save").onclick')
    assert i > 0, "保存按钮的绑定不见了"
    # 取到下一个顶层绑定为止
    j = src.find('$("login-form").onsubmit', i)
    assert j > i
    seg = src[i:j]
    assert 'go("plugins")' in seg, (
        "「保存并关闭」没有离开配置页 —— 用户会以为没保存成功"
    )


def test_closing_keeps_the_message():
    """② 关闭后提示不能丢 —— 要落到**列表页常驻**的那个提示位。

    🔴 `#plugin-config-hint` 在配置页里，`go("plugins")` 之后就不显示了；
       把提示写在那里 = **用户永远看不到结果**（看起来像什么都没发生）。
    """
    src = BOOT.read_text(encoding="utf-8")
    i = src.find('$("plugin-config-save").onclick')
    j = src.find('$("login-form").onsubmit', i)
    seg = src[i:j]
    # ⚠️ **必须写进"只有这里写"的元素**。
    #    我第一版写的是 `#plugin-update-result` —— 但那个元素被 **5 处**写
    #    （升级结果/加载提示/配置结果…），`go("plugins")` 会触发 `loadPluginPages()`
    #    把它立刻覆盖成"共 N 个插件拓展页"。实测用户点完保存只看到一句不相干的话。
    assert 'toast($("plugin-action-result")' in seg, (
        "关闭后的提示没写到专用元素 `#plugin-action-result` —— "
        "会被别的渲染当场覆盖（用户看不到保存结果）"
    )
    html = (ROOT / "emotion_app/web/static/index.html").read_text(encoding="utf-8")
    assert 'id="plugin-action-result"' in html, "专用提示位没在 HTML 里"


def test_config_save_triggers_reload():
    """③ 保存配置后必须重载插件（否则改的不生效）。

    实测依据（生产日志）：
        插件在 `__init__` 里把配置读进属性 ⇒ 只写文件不重载 = 用户改的不生效，
        而他看到的是"已保存"。
    """
    raw = HOST.read_text(encoding="utf-8")
    # ⚠️ **必须先剔注释**：我在那段里写了一行注释「用本类已有的 `self.reload_plugin()`」，
    #    不剔掉的话 `"self.reload_plugin(" in seg` **在删除真调用后仍然为真** ——
    #    我第一版注入验证就是这样"抓不住"，一度以为判据坏了，其实是判据写漏了。
    src = "\n".join(
        line for line in raw.splitlines()
        if not line.lstrip().startswith("#")
    )
    i = src.find("async def save_plugin_config")
    assert i > 0
    j = src.find("async def ", i + 10)
    seg = src[i:j if j > i else i + 6000]
    # 必须是**真的调用**（`await self.reload_plugin(`），不是提了一嘴
    assert "await self.reload_plugin(" in seg, (
        "保存配置后没有重载插件 —— 用户改的不会生效（而界面显示已保存）"
    )
    # ⚠️ 必须用**本类已有的**那个方法，别再 import 一份
    assert "plugin_lifecycle import" in seg or "PluginLifecycleError" in seg
    assert "reload_plugin as" not in seg, (
        "又 import 了一份 reload_plugin —— 同一能力两条调用路径，改语义要改两处"
    )


def test_no_stale_not_implemented_message():
    """④ 不许再出现"尚未实现插件热重载"这种**过期自述**。

    🔴 这类文案最贵：它让用户以为"必须重启"，于是每次改配置都去重启服务 ——
       而实际上保存完就自动重载了。
    """
    raw = HOST.read_text(encoding="utf-8")
    # ⚠️ **剔除注释行**再查 —— 我在代码注释里写明了"原来这里写着…"来说明改动，
    #    不剔掉的话它自己就把判据弄红了（这类"注释里提到就红"的坑踩过多次）。
    src = "\n".join(
        line for line in raw.splitlines()
        if not line.lstrip().startswith(("#",))
    )
    assert "尚未实现插件热重载" not in src, (
        "还留着「尚未实现热重载」的旧文案 —— 会误导用户去重启服务"
    )
    assert "已热重载" in src, "文案没有说清「已热重载」"
