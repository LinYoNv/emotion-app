# -*- coding: utf-8 -*-
"""插件**源码镜像** —— 让「源码目录只读」从口号变成事实。

## 为什么必须有（真实事故，2026-09-19 实测抓了两次）

我们原本以为「数据目录分开（`StarTools.get_data_dir` 指向我们的 `data/plugins`）
就安全了」。**错。** 实测反例：

    `astrbot_plugin_comfyui_pro.__init__` → `_auto_update_schema()`
    写的是 **`PLUGIN_DIR / "_conf_schema.json"`**，即**插件源码目录**，
    内容取自 `self.data_dir / "workflow"` 里的文件名。

于是链路变成：

    我们的 data_dir（只有 workflow_api.json）
        → 写进**两边共用**的源码目录 schema
        → 覆盖掉 AstrBot 自己那份（它写着 3 个 workflow）

**这不是"脏了一个文件"，是把别人正在跑的 bot 配置改坏了。**
而且它**完全静默**：插件不报错，我们的日志也没有。

## 本模块的对策

把插件源码**镜像到我们自己的可写目录**，然后从镜像加载。
源码目录于是变成**真正的只读输入**——插件再怎么回写，也只写到我们的副本里。

    /opt/astrbot/data/plugins/   （别人的生产目录：只读，我们绝不写）
              │  镜像（单向）
              ▼
    data/plugin_src/             （我们的可写副本：插件随便写）

⚠️ 镜像**单向**：只从源读到我们这儿，**绝不回写**。
⚠️ 与"写保护哨兵"（`host/astrbot_host/write_guard.py`）是**互补**的两件事：
   哨兵负责**发现**（万一还有别的路径漏网），本模块负责**不发生**。
   —— 只发现不阻止是不够的：我实测发现，探针每跑一次，生产目录就被改一次。
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("emotion_app.plugins")

#: 不参与镜像的目录：`__pycache__` 是生成物（我们这边跑起来会自动重建），
#: `.git` 是版本库（且可能很大）。混进来只是白白占空间、拖慢镜像。
_SKIP_DIR_NAMES = frozenset({"__pycache__", ".git", ".mypy_cache", ".ruff_cache", ".pytest_cache"})
_SKIP_SUFFIXES = (".pyc", ".pyo")


@dataclass
class MirrorReport:
    """一次镜像的结果（如实记录，便于诊断）。"""

    dest_root: str = ""
    plugin_names: list[str] = field(default_factory=list)
    copied: int = 0
    removed: int = 0
    unchanged: int = 0
    skipped_sources: list[str] = field(default_factory=list)

    @property
    def total_touched(self) -> int:
        return self.copied + self.removed

    def summary(self) -> str:
        verb = "已同步" if self.total_touched else "已是最新"
        return (
            f"插件源码镜像{verb}：{len(self.plugin_names)} 个插件 → {self.dest_root}"
            f"（复制 {self.copied} · 删除 {self.removed} · 未变 {self.unchanged}）"
        )


def _iter_source_files(root: Path):
    """遍历源目录里的文件（跳过生成物），产出 ``(相对路径, 绝对路径)``。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        rel_dir = Path(dirpath).relative_to(root)
        for name in filenames:
            if name.endswith(_SKIP_SUFFIXES):
                continue
            yield rel_dir / name, Path(dirpath) / name


def _needs_copy(src: Path, dst: Path) -> bool:
    """比 ``(size, mtime_ns)`` 判断是否需要复制（不读内容，快）。"""
    try:
        s, d = src.stat(), dst.stat()
    except OSError:
        return True
    return (s.st_size, s.st_mtime_ns) != (d.st_size, d.st_mtime_ns)


def mirror_one(src_dir: Path, dst_dir: Path, report: MirrorReport) -> None:
    """把**一个**插件目录镜像到 ``dst_dir``（增量 + 传播删除）。

    ⚠️ 会删除 ``dst_dir`` 里源已不存在的文件 —— 否则插件删掉的文件
       在我们副本里永远留着，加载行为会与源不一致（静默偏差）。
    """
    if not src_dir.is_dir():
        report.skipped_sources.append(f"{src_dir}（不是目录）")
        return

    dst_dir.mkdir(parents=True, exist_ok=True)
    seen: set[Path] = set()

    for rel, src_file in _iter_source_files(src_dir):
        seen.add(rel)
        dst_file = dst_dir / rel
        if dst_file.exists() and not _needs_copy(src_file, dst_file):
            report.unchanged += 1
            continue
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        # copy2 保留 mtime —— 下次比对才能判定"未变"
        shutil.copy2(src_file, dst_file)
        report.copied += 1

    # 传播删除：源里没有的，我们副本里也不留
    for dirpath, dirnames, filenames in os.walk(dst_dir):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        rel_dir = Path(dirpath).relative_to(dst_dir)
        for name in filenames:
            if name.endswith(_SKIP_SUFFIXES):
                continue
            if (rel_dir / name) not in seen:
                try:
                    (Path(dirpath) / name).unlink()
                    report.removed += 1
                except OSError:
                    logger.warning("镜像删除失败：%s", Path(dirpath) / name)


def mirror_plugin_sources(
    sources: list[str | Path],
    dest_root: str | Path,
) -> MirrorReport:
    """把多个插件源目录镜像到 ``dest_root``，返回**我们自己的**插件目录列表。

    Args:
        sources: 插件源目录（**只读**；可能含多个，如"我们自己的 + 别人的"）。
        dest_root: 我们的可写镜像根目录。

    Returns:
        MirrorReport；其 ``plugin_names`` 即"镜像后可加载的插件目录名"。

    🔴 **单向**：只读 ``sources``、只写 ``dest_root``。本函数**绝不**修改源。
    """
    dest = Path(dest_root)
    dest.mkdir(parents=True, exist_ok=True)
    report = MirrorReport(dest_root=str(dest.resolve()))

    seen_names: set[str] = set()
    for raw in sources:
        src = Path(raw)
        if not src.is_dir():
            report.skipped_sources.append(f"{src}（不是目录）")
            continue
        for plugin_dir in sorted(p for p in src.iterdir() if p.is_dir()):
            if plugin_dir.name in _SKIP_DIR_NAMES:
                continue
            # 同名插件以**先出现的源**为准（与插件加载器的先到先得一致）
            if plugin_dir.name in seen_names:
                logger.warning(
                    "插件 %s 在多个源里出现，忽略后一个：%s",
                    plugin_dir.name,
                    plugin_dir,
                )
                continue
            seen_names.add(plugin_dir.name)
            mirror_one(plugin_dir, dest / plugin_dir.name, report)
            report.plugin_names.append(plugin_dir.name)

    report.plugin_names.sort()
    if report.skipped_sources:
        # ⚠️ 不静默：源目录不存在必须留痕（否则"插件少了"无从解释）
        logger.warning("以下插件源被跳过：%s", report.skipped_sources)
    logger.info("%s", report.summary())
    return report
