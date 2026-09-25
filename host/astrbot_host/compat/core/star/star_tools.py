# -*- coding: utf-8 -*-
"""`astrbot.core.star.star_tools` —— `StarTools` 工具类。

⭐ 依据：真 AstrBot `core/star/star_tools.py`。

⚠️ 实测陪伴插件**只用到一个方法**：`StarTools.get_data_dir(PLUGIN_NAME)`
   （`plugin_bootstrap.py:291`，全库唯一一处 `StarTools.*`）。
   所以这里**优先把 `get_data_dir` 做对**，其余能力按需补。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("astrbot")

#: 插件数据根目录（宿主装配时设置；默认放进程 cwd 下的 `data/plugins`）
_data_root: Path = Path("data/plugins")


def set_data_root(path: str | Path) -> None:
    """设置插件数据根目录（宿主启动时调用）。

    ⚠️ 做成显式设置而不是读环境变量 —— 让"数据放哪"在装配处可见，
       而不是藏在一个隐式全局里。
    """
    global _data_root
    _data_root = Path(path)
    logger.info("插件数据根目录：%s", _data_root.resolve())


def get_data_root() -> Path:
    return _data_root


class StarTools:
    """插件工具集（照真实现的静态方法形态）。"""

    @staticmethod
    def get_data_dir(plugin_name: str | None = None) -> Path:
        """取插件的数据目录（**不存在则创建**）。

        真实现语义：
          * 传插件名 → `data/plugins/<plugin_name>/`
          * 不传     → 调用方插件的目录（靠调用栈推断）
          * **目录一定存在**（真实现会 mkdir）

        ⚠️ 不传名字时我们用调用栈猜 —— 真实现也是这么做的；
           猜不到就退回数据根目录，并**留一条 warning**（不静默）。
        """
        if plugin_name:
            d = _data_root / plugin_name
        else:
            d = _guess_caller_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def get_plugin_logger(plugin_name: str) -> logging.Logger:
        return logging.getLogger(f"astrbot.plugin.{plugin_name}")


def _guess_caller_dir() -> Path:
    """从调用栈推断插件目录（照真实现的思路）。"""
    import inspect

    frame = inspect.currentframe()
    try:
        # 往上找第一个不在本包里的帧
        while frame is not None:
            mod = frame.f_globals.get("__name__", "")
            if mod and not mod.startswith("astrbot_host") and not mod.startswith(
                "astrbot.core"
            ):
                pkg = frame.f_globals.get("__package__") or ""
                if pkg:
                    return _data_root / pkg.split(".")[0]
                break
            frame = frame.f_back
    finally:
        del frame
    logger.warning("无法推断调用方插件目录，退回数据根目录 %s", _data_root)
    return _data_root


__all__ = ["StarTools", "get_data_root", "set_data_root"]
