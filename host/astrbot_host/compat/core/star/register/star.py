# -*- coding: utf-8 -*-
"""`astrbot.core.star.register.star` —— 插件（Star）注册装饰器。

⭐⭐ **照真 AstrBot `core/star/register/star.py` 逐条移植**。

🔴 **修过一个真 bug**：我第一版签名写成 `register_star(star=None, metadata=None)`,
   而真实现是 **`register_star(name, author, desc, version, repo=None)`** ——
   即 4-5 个**位置参数**的装饰器工厂。
   实测后果：磁盘上**4 个插件直接加载失败**
   （`TypeError: register_star() takes from 0 to 2 positional arguments but 5 were given`）：
   `prompt_tools` / `astrbot_plugin_aitag_uploader` / `content_companion` / `image_companion`。

   → 教训：**"签名看着像什么"永远不能替代"真源码是什么"。**
     这个 bug 只有在**真的去加载第三方插件**时才暴露 —— 自测 hello 插件不会发现。
"""
from __future__ import annotations

import warnings
from typing import Any

from astrbot_host.compat.core.star.base import StarMetadata, star_map

_warned_register_star = False


def register_star(
    name: str,
    author: str = "",
    desc: str = "",
    version: str = "",
    repo: str | None = None,
):
    """注册一个插件（Star）。

    ⚠️ **已废弃**（照真实现）：真 AstrBot v3.5.19 之后不需要它 ——
       加载器会自动识别 `Star` 子类。但**大量现存插件仍在用**，
       所以必须支持（磁盘上就有 4 个）。

    用法（照真实现）::

        @register("插件名", "作者", "描述", "1.0.0")
        class MyPlugin(Star):
            ...
    """
    global _warned_register_star
    if not _warned_register_star:
        _warned_register_star = True
        # 🔴 **必须吞掉异常**（实测踩到）：在 `-W error` 或 pytest 的
        #    `filterwarnings = error` 配置下，`warnings.warn` 会**抛异常**，
        #    而这里正在执行插件类的定义 —— 一抛就整个插件加载失败。
        #
        #    症状很诡异：因为 `_warned_register_star` 只发一次，
        #    表现为"**第一次跑成功、第二次失败**"（不确定性 bug）。
        #    实测 `astrbot_plugin_aitag_uploader` 就这样时而加载成功时而失败。
        #
        #    判据：**给第三方插件的兼容提示，绝不能有能力中断插件加载。**
        try:
            warnings.warn(
                "The 'register_star' decorator is deprecated and will be "
                "removed in a future version.",
                DeprecationWarning,
                stacklevel=2,
            )
        except Exception:  # noqa: BLE001  (Warning 被提升为异常时兜住)
            pass

    def decorator(cls: Any) -> Any:
        md = star_map.get(cls.__module__)
        if md is None:
            star_map[cls.__module__] = StarMetadata(
                name=name, author=author, desc=desc, version=version,
            )
        else:
            md.name = name
            md.author = author
            md.desc = desc
            md.version = version
        # ⚠️ 真实现里 `repo` 存在 StarMetadata 上（我们没有该字段 → 记进 reserved）
        if repo:
            target = star_map[cls.__module__]
            target.reserved["repo"] = repo
        return cls

    return decorator


__all__ = ["register_star"]
