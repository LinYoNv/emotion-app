# -*- coding: utf-8 -*-
"""`astrbot` 兼容影子包 —— 让陪伴插件**不改一行代码**跑在我们的宿主上。

⭐ 为什么用"影子包"而不是改插件源码（见 `爱语/docs/PLATFORM_POSITIONING.md` §3.0）：
   作者的设计明确写「**AstrBot 是首个宿主适配器**」「**一站式应用是同一框架的新宿主**」。
   所以正确做法是**实现宿主端口**，而不是 fork/改写插件的 2000+ 处 import。

🔴 用法（只在宿主进程里生效，**不进领域层**）：

    from astrbot_host.shadow import install_shadow_package
    install_shadow_package()          # 把 compat 注册成 `astrbot`
    import astrbot.api.star           # 从此插件能正常 import

⚠️ **硬约束**（来自作者 `PLATFORM_PORTABILITY...` §7 验收判据）：
   ① 领域层（`情绪app/emotion_app/domain/`）**绝不能** import 这个包；
   ② 影子包只做协议转换，**不写业务规则**。
"""
from __future__ import annotations

import importlib
import sys

#: 本包在 sys.modules 里注册成什么名字
SHADOW_NAME = "astrbot"

#: 需要注册的完整子模块清单。
#: ⚠️ **显式列举**（不扫描目录）—— 扫描会把 `__pycache__` 也算进来。
#: 这份清单来自**实测的依赖面**（`爱语/tools/analyze_companion_deps.py`）。
SUBMODULES: tuple[str, ...] = (
    # ---- 公开 API（astrbot.api.*）----
    "api",
    "api.event",
    "api.event.filter",
    "api.star",
    "api.message_components",
    "api.provider",
    "api.web",
    "api.util",
    # ---- 私有 API（astrbot.core.*）—— 陪伴实际用到的那部分 ----
    "core",
    "core.star",
    "core.star.base",
    "core.star.context",
    "core.star.star_tools",
    "core.star.config",
    "core.star.filter",
    "core.star.filter.custom_filter",
    "core.star.filter.regex",
    "core.star.filter.command",
    "core.star.filter.command_group",
    "core.star.filter.event_message_type",
    "core.star.filter.permission",
    "core.star.filter.platform_adapter_type",
    "core.star.register",
    "core.star.star_handler",
    "core.platform",
    "core.platform.message_type",
    "core.platform.message_session",
    "core.platform.astrbot_message",
    "core.platform.platform",
    "core.platform.platform_metadata",
    "core.agent",
    "core.agent.message",
    # 🔴 `core.agent.tool`（`FunctionTool` / `ToolSet`）——
    #    2026-09-21 补：插件用 `ToolSet` 做 function calling
    #    （`page_api.py:4199/:4217`），缺它时插件只记一行 info 日志、
    #    面板显示"模型判断失败"，**完全指不到根因**（实测拍到的
    #    `No module named 'astrbot.core.agent.tool'`）。
    "core.agent.tool",
    # ---- 2026-10-19：陪 7.0.0 需要的新面（实测 17 个缺口，逐个补）----
    "core.log",
    "core.tools",
    "core.tools.message_tools",
    "core.tools.registry",
    "core.utils.shared_preferences",
    "core.utils.quoted_message",
    "core.utils.quoted_message.chain_parser",
    "core.utils.quoted_message.image_resolver",
    "core.utils.quoted_message.onebot_client",
    "core.platform.sources",
    "core.platform.sources.aiocqhttp",
    "core.platform.sources.aiocqhttp.aiocqhttp_message_event",
    "core.provider.sources.gemini_source",
    "core.provider.sources.openai_source",
    "core.provider.sources.fishaudio_tts_api_source",
    "core.config.default",
    "builtin_stars",
    "builtin_stars.builtin_commands",
    "builtin_stars.builtin_commands.commands",
    "builtin_stars.builtin_commands.commands.conversation",
    "builtin_stars.builtin_commands.main",

    "core.utils",
    "core.utils.astrbot_path",
    "core.provider",
    "core.provider.entities",
    "core.provider.sources",
    "core.provider.sources.request_retry",
    "core.db",
    "core.db.po",
    "core.astr_main_agent",
    "core.file_token_service",
    "core.config",
    "core.config.astrbot_config",
    "core.message",
    "core.message.components",
    "core.message.message_event_result",
)

_installed = False


def is_installed() -> bool:
    return _installed


def install_shadow_package(*, force: bool = False) -> bool:
    """把 `astrbot_host.compat` 注册为顶层 `astrbot` 包。

    Returns:
        本次是否真的做了安装（已装过且非 force → False）。

    Raises:
        RuntimeError: `astrbot` 已被**别的实现**占用且未传 force。
    """
    global _installed
    if _installed and not force:
        return False

    existing = sys.modules.get(SHADOW_NAME)
    if existing is not None and not _is_our_shadow(existing) and not force:
        raise RuntimeError(
            f"`{SHADOW_NAME}` 已被占用（{getattr(existing, '__file__', '未知来源')}）——"
            "拒绝覆盖真 AstrBot（静默覆盖会让『到底在谁上面跑』不可知）。"
            "确实要用兼容层时显式传 force=True。"
        )

    import astrbot_host.compat as compat

    # 顶层包
    sys.modules[SHADOW_NAME] = compat
    # 子模块：显式 import 后逐个注册（这样 `import astrbot.api.star` 命中 sys.modules）
    for dotted in SUBMODULES:
        full = f"astrbot_host.compat.{dotted}"
        try:
            mod = importlib.import_module(full)
        except ImportError:
            continue          # 未实现的可选子模块 → 跳过（访问时会被 ImportError 拦住）
        sys.modules[f"{SHADOW_NAME}.{dotted}"] = mod

    _installed = True
    return True


def uninstall_shadow_package() -> None:
    """卸载影子包（测试用；避免污染其它测试）。"""
    global _installed
    for name in list(sys.modules):
        if name == SHADOW_NAME or name.startswith(f"{SHADOW_NAME}."):
            sys.modules.pop(name, None)
    _installed = False


def _is_our_shadow(mod: object) -> bool:
    f = getattr(mod, "__file__", "") or ""
    return "astrbot_host" in f
