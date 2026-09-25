# -*- coding: utf-8 -*-
"""`astrbot.core.utils.astrbot_path` 兼容层 —— 集中式路径解析。

⭐ 依据：真 AstrBot `core/utils/astrbot_path.py`（实测 107 行）。

⚠️ **相对真实现省略了什么**：
   * 真实现 `from astrbot.core.utils.runtime_env import is_packaged_desktop_runtime`
     只为一件事 —— 判断要不要把 root 落到 `~/.astrbot`。为了不引一个新模块，
     这里把那一行**逐字内联**成 `_is_packaged_desktop_runtime()`：
     判据仍是 `os.environ.get("ASTRBOT_DESKTOP_CLIENT") == "1"`，
     行为与真实现完全一致。**没有引入任何新依赖**（纯标准库 `os` / `tempfile`）。
   * 其余 14 个函数**逐字照搬**，包括 `os.path.realpath` 的用法。

🔴 **`ASTRBOT_ROOT` 是本模块唯一的行为开关，也是宿主必须主动设置的环境变量**：
   真实现的 root 默认取 `os.getcwd()`。在陪伴插件里，数据目录是
   **插件自己的持久化目录**（`get_astrbot_data_path()` 被 `image_runtime.py:8336`
   用来校验路径是否越界，见下），所以宿主启动时应当把 `ASTRBOT_ROOT`
   显式设为宿主的数据根，**不要依赖 cwd** —— 否则「进程从哪个目录启动」
   会静默改变插件数据落点（典型事故：重启方式一变，数据"消失"）。

⚠️ **`get_astrbot_path()` 的语义与真实现在宿主里不同，这是有意的**：
   真实现靠 `__file__` 上溯 `../../../` 拿到 **AstrBot 源码树**根
   （实测得到 `.../site-packages`）。我们是影子包，同样上溯得到的是
   `.../host`（兼容层所在工程目录）。**只有在用 `get_astrbot_builtin_plugin_path()`
   时才受影响**（真实现 = `<源码树>/astrbot/builtin_stars`）；
   其余路径函数最终都落在 `get_astrbot_root()/data/...`，**与 `get_astrbot_path()` 无关**，
   所以宿主只需要管好 `ASTRBOT_ROOT`。
   ⚠️ 该函数返回的是路径字符串（`realpath` **不要求路径存在**），
   不会因为目录缺失而报错 —— 别把它当"目录已创建"的判据。
"""
from __future__ import annotations

import os
import tempfile


def _is_packaged_desktop_runtime() -> bool:
    """是否运行在打包的桌面客户端里（照真实现 `runtime_env.py` 内联）。"""
    return os.environ.get("ASTRBOT_DESKTOP_CLIENT") == "1"


def get_astrbot_path() -> str:
    """Return the AstrBot project source path."""
    return os.path.realpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../"),
    )


def get_astrbot_root() -> str:
    """Return the AstrBot root directory."""
    if path := os.environ.get("ASTRBOT_ROOT"):
        return os.path.realpath(path)
    if _is_packaged_desktop_runtime():
        return os.path.realpath(os.path.join(os.path.expanduser("~"), ".astrbot"))
    return os.path.realpath(os.getcwd())


def get_astrbot_data_path() -> str:
    """Return the AstrBot data directory path."""
    return os.path.realpath(os.path.join(get_astrbot_root(), "data"))


def get_astrbot_config_path() -> str:
    """Return the AstrBot config directory path."""
    return os.path.realpath(os.path.join(get_astrbot_data_path(), "config"))


def get_astrbot_plugin_path() -> str:
    """Return the AstrBot plugin directory path."""
    return os.path.realpath(os.path.join(get_astrbot_data_path(), "plugins"))


def get_astrbot_builtin_plugin_path() -> str:
    """Return the AstrBot built-in plugin directory path."""
    return os.path.realpath(
        os.path.join(get_astrbot_path(), "astrbot", "builtin_stars")
    )


def get_astrbot_plugin_data_path() -> str:
    """Return the AstrBot plugin data directory path."""
    return os.path.realpath(os.path.join(get_astrbot_data_path(), "plugin_data"))


def get_astrbot_t2i_templates_path() -> str:
    """Return the AstrBot T2I templates directory path."""
    return os.path.realpath(os.path.join(get_astrbot_data_path(), "t2i_templates"))


def get_astrbot_webchat_path() -> str:
    """Return the AstrBot WebChat data directory path."""
    return os.path.realpath(os.path.join(get_astrbot_data_path(), "webchat"))


def get_astrbot_temp_path() -> str:
    """Return the AstrBot temporary data directory path."""
    return os.path.realpath(os.path.join(get_astrbot_data_path(), "temp"))


def get_astrbot_skills_path() -> str:
    """Return the AstrBot skills directory path."""
    return os.path.realpath(os.path.join(get_astrbot_data_path(), "skills"))


def get_astrbot_workspaces_path() -> str:
    """Return the AstrBot workspaces directory path."""
    return os.path.realpath(os.path.join(get_astrbot_data_path(), "workspaces"))


def get_astrbot_system_tmp_path() -> str:
    """Return the shared system temporary directory used by local tools."""
    return os.path.realpath(os.path.join(tempfile.gettempdir(), ".astrbot"))


def get_astrbot_site_packages_path() -> str:
    """Return the AstrBot third-party site-packages directory path."""
    return os.path.realpath(os.path.join(get_astrbot_data_path(), "site-packages"))


def get_astrbot_knowledge_base_path() -> str:
    """Return the AstrBot knowledge base root path."""
    return os.path.realpath(os.path.join(get_astrbot_data_path(), "knowledge_base"))


def get_astrbot_backups_path() -> str:
    """Return the AstrBot backups directory path."""
    return os.path.realpath(os.path.join(get_astrbot_data_path(), "backups"))


__all__ = [
    "get_astrbot_backups_path",
    "get_astrbot_builtin_plugin_path",
    "get_astrbot_config_path",
    "get_astrbot_data_path",
    "get_astrbot_knowledge_base_path",
    "get_astrbot_path",
    "get_astrbot_plugin_data_path",
    "get_astrbot_plugin_path",
    "get_astrbot_root",
    "get_astrbot_site_packages_path",
    "get_astrbot_skills_path",
    "get_astrbot_system_tmp_path",
    "get_astrbot_t2i_templates_path",
    "get_astrbot_temp_path",
    "get_astrbot_webchat_path",
    "get_astrbot_workspaces_path",
]
