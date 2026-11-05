# -*- coding: utf-8 -*-
"""判据：**移动端自查工具本身不许退化成没用的东西**。

## 背景（情绪 2026-11-05）

> 「pc端的chrome没有问题，移动端有。以后你加个验证方式，你自己在本机浏览器看。」

工具是 `tools/mobile_visual_check.py`。它跑在真 headless Chrome 里，
但**脚本里的检测逻辑**（`_PROBE` 那段 JS）可以被无意改坏 ——
而改坏之后它只会**少报**问题，看起来还是"✅ 全部通过"。
**假绿的验证工具比没有更糟。**

## 本文件守什么

| # | 判据 | 抓的是什么 |
|---|---|---|
| ① | 必须用 `mobile=True` + 3x DPR | 少了它媒体查询按桌面判，扫的根本不是手机布局 |
| ② | 必须有 `clipOf()` 求交 | 这是它与 `scrollWidth` 类判据的**分水岭** |
| ③ | 必须过滤屏幕外/无障碍隐藏元素 | 少了它满屏假警报，真问题被淹（实测过） |
| ④ | 必须存截图 | 判据只是"我以为该查的地方"，截图是唯一兜底 |
| ⑤ | 必须把「前置不满足」当失败 | 跳过判据 = 假通过 |
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "mobile_visual_check.py"


def _src() -> str:
    assert TOOL.is_file(), f"工具不见了：{TOOL}"
    return TOOL.read_text(encoding="utf-8")


def test_uses_real_mobile_viewport():
    """① 必须 `mobile=True` + DPR。

    ⚠️ 只用 `--window-size` 是**不够**的：媒体查询看的是
       `Emulation.setDeviceMetricsOverride` 的 `mobile` 标志，
       不设它浏览器仍按桌面判，扫出来的是桌面布局。
    """
    s = _src()
    assert "setDeviceMetricsOverride" in s, "没设设备度量 —— 视口根本不对"
    # ⚠️ 查**调用参数**，不是查文件里有没有 `mobile=True` 这个字符串
    assert "mobile=True," in s or "mobile=True)" in s, (
        "setDeviceMetricsOverride 没传 mobile=True —— 媒体查询按桌面判"
    )
    assert "mobile=False" not in s.replace("mobile=True", ""), (
        "传了 mobile=False —— 扫的是桌面布局"
    )
    assert "deviceScaleFactor" in s, "没设 DPR —— 截图与真实密度不符"
    assert "390" in s and "844" in s, "视口不是主流手机尺寸（390x844）"


def test_has_clip_intersection():
    """② 必须真的**调用** `clipOf()`，而不是只定义它。

    🔴 这是本工具**唯一**能抓到"元素被 `overflow:hidden` 裁掉"的机制。
       去掉调用、退回只看 `getBoundingClientRect()`，那类 bug **永远查不出来**：
       rect 返回的是未被裁切的完整盒子，DOM 层面一切正常，用户却看不全按钮。

    ⚠️ **判据必须查调用点**（2026-11-05 踩到）：
       我第一版只断言 `"clipOf" in s`，结果把函数定义留着、
       **把调用换成 `rect.width`** 之后，判据**仍然通过** = 假判据。
       ⇒ 必须断言 `clipOf(el)` 真的出现在"算可见宽度"那一步。
    """
    s = _src()
    assert "const c = clipOf(el)" in s, (
        "没有真的调用 clipOf 来算可见区域 —— 被裁切的元素查不出来"
    )
    assert "overflowX" in s and "overflowY" in s, "没读 overflowX/overflowY —— 求交不完整"
    assert "parentElement" in s, "没有向上遍历祖先，求交无从谈起"


def test_filters_invisible_noise():
    """③ 必须过滤屏幕外 / 无障碍隐藏的元素。

    实测教训：第一版没过滤时，每页报 11 个假警报
    （抽屉里的导航 `left:-272`、`skip-link` 的 `left:-9999`），
    真问题被完全淹没 —— 那种工具没人会看第二次。
    """
    s = _src()
    # ⚠️ 必须断言"真的拿它做了 early-return"，只定义变量等于没过滤
    assert "if (!onScreen || hiddenByA11y) return;" in s, (
        "没有用 onScreen/hiddenByA11y 做 early-return —— 噪音会淹掉真问题"
    )
    assert "onScreen =" in s, "没判断元素是否在屏幕内"
    assert "aria-hidden" in s, "没排除 aria-hidden 的装饰节点"
    assert "skip-link" in s, "没排除无障碍跳过链接（它平时必须不可见）"


def test_captures_screenshots():
    """④ 必须存截图 —— 判据之外的人工兜底。"""
    s = _src()
    assert "captureScreenshot" in s, "没有截图 —— 没法人工确认"
    assert "SHOT_DIR" in s, "截图目录没定义"


def test_nonzero_exit_on_problems():
    """⑤ 发现问题必须**非零退出**（否则 CI/脚本会当成通过）。"""
    s = _src()
    assert "return 1" in s, "出错时没返回非零 —— 调用方会误判成通过"
    assert "failures" in s, "没有收集失败项"
