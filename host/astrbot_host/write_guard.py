# -*- coding: utf-8 -*-
"""插件源码目录**写保护哨兵** —— 把「静默污染生产目录」变成「响亮告警」。

## 为什么需要它（真实事故，不是假想）

宿主把插件的**数据目录**（`StarTools.get_data_dir`）指向了我们自己的
`data/plugins/`，但**插件源码目录**是**只读共用**的（很可能就是别人正在跑的
AstrBot 生产目录，如 `/opt/astrbot/data/plugins/`）。

我们原以为「数据目录分开了就安全」—— **错**。实测抓到一个反例：

    `astrbot_plugin_comfyui_pro.main.__init__` 里的 `_auto_update_schema()`
    写的是 **`PLUGIN_DIR / "_conf_schema.json"`**（插件**源码**目录），
    而不是 `self.data_dir`。它按自己 `data_dir/workflow/` 里实际有哪些
    workflow 文件，**回写**源码目录里的 schema 的 `options`/`enum`。

于是"宿主启动一次 → 生产插件目录里一个文件被改写"。
更糟的是：**它是静默的**——插件没报错、日志在它自己那儿、我们完全不知情。

## 本模块的定位

🔴 **不是"阻止写入"**（做不到：插件是任意代码，能直接 `open(...,'w')`）。
   而是**启动前后各扫一次 mtime/size，把差异响亮打出来**：

      * 有人（插件/宿主）动了插件源码目录 → WARNING + 逐条清单；
      * 清单里**排除 `__pycache__`**（那是 Python 导入的正常副作用，
        不是"污染"；混进来会让告警变成噪音，最后没人看）。

## 判据（为什么值得存在）

    加它之前：污染**无法被发现**（我靠人工 diff 生产目录才偶然看到）。
    加它之后：`report.source_writes` 非空即"有东西写了源码目录"，
    测试里可以**断言它是空的**——这才叫可守的边界。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("astrbot")

#: 🔴 这些是**正常**副作用，不算污染（否则告警会被噪音淹没）：
#:   `__pycache__/` —— Python 导入插件时写字节码，任何加载器都会产生。
_IGNORED_DIR_NAMES = frozenset({"__pycache__", ".git", ".mypy_cache", ".ruff_cache", ".pytest_cache"})
_IGNORED_SUFFIXES = (".pyc", ".pyo")


@dataclass
class SourceWrite:
    """一条对插件源码目录的改动。"""

    path: str
    kind: str  # "modified" | "created" | "deleted"
    detail: str = ""


@dataclass
class SourceSnapshot:
    """插件源码目录的快照（**只记 mtime_ns + size，不读内容**，所以很快）。"""

    entries: dict[str, tuple[int, int]] = field(default_factory=dict)

    @classmethod
    def take(cls, roots: list[Path]) -> SourceSnapshot:
        snap = cls()
        for root in roots:
            if not root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                # 就地剪枝：不进 __pycache__ 等目录（省时间 + 免噪音）
                dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIR_NAMES]
                for name in filenames:
                    if name.endswith(_IGNORED_SUFFIXES):
                        continue
                    p = Path(dirpath) / name
                    try:
                        st = p.stat()
                    except OSError:
                        continue  # 恰好被删/权限问题：不算改动
                    snap.entries[str(p)] = (st.st_mtime_ns, st.st_size)
        return snap

    def diff(self, other: SourceSnapshot) -> list[SourceWrite]:
        """返回 ``self → other`` 之间的改动（按路径排序，便于比对）。"""
        out: list[SourceWrite] = []
        for path, meta in self.entries.items():
            new = other.entries.get(path)
            if new is None:
                out.append(SourceWrite(path, "deleted"))
            elif new != meta:
                out.append(
                    SourceWrite(
                        path,
                        "modified",
                        f"size {meta[1]}→{new[1]}",
                    )
                )
        for path in other.entries:
            if path not in self.entries:
                out.append(SourceWrite(path, "created"))
        out.sort(key=lambda w: (w.path, w.kind))
        return out


def warn_on_source_writes(writes: list[SourceWrite]) -> None:
    """把改动**响亮**打出来（逐条，不省略）。

    🔴 不静默、不聚合、不截断：这些是"可能改坏了别人生产环境"的证据，
       少一行都可能漏掉真凶。
    """
    if not writes:
        return
    logger.warning(
        "⚠️ 检测到插件**源码目录**被改动 %d 处（该目录本应只读！）\n%s\n"
        "  常见原因：插件在 __init__/initialize 里回写自己的配置文件到"
        "源码目录（如 comfyui_pro 的 _conf_schema.json）。\n"
        "  若该目录是别人 AstrBot 的生产目录，请立即核对上面这些文件。",
        len(writes),
        "\n".join(f"    • [{w.kind}] {w.path} {w.detail}".rstrip() for w in writes),
    )
