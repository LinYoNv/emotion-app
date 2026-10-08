#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志聚合：按**消息模板**归类统计 WARNING/ERROR，找出"哪条在刷屏"。

## 为什么必须聚合而不是翻页
日志上万行，**肉眼翻只能看到最后几条**，
而"每 3 秒刷一条"和"偶发一条"在尾部看起来一模一样。
判据 = 按模板归一化后计数，再按次数排序 —— 一眼看出噪音源。

## 🔴 为什么要带日期（2026-10-08 修）
`logs/serve.out` 是 `nohup >>` 追加的，**横跨多天、含多次重启**
（实测：首行 2026-09-25、末行 2026-10-08，含 26 次「服务启动」）。
如果只打 `HH:MM:SS`，**"这条是历史残留还是当前仍在发生"就判不了**。
所以本工具：
  1. 识别两种格式的行：我们自己的（带日期，新格式）与旧的（只有 `HH:MM:SS`）；
  2. 对**旧格式行**用文件里 Hypercorn 打的 `[YYYY-MM-DD HH:MM:SS +0800]`
     当**日期锚点**逐行反推（那些行本来就带日期，正好当标尺）；
  3. 输出 `--since-last-restart` 切片，只看**最后这次运行周期**的信号。

日志格式：
    (新) 2026-10-08 15:12:33 WARNING emotion_app.web.app: 插件路由未命中  {"path": ...}
    (旧) 00:00:00 WARNING emotion_app.web.app: 插件路由未命中  {"path": ...}
    (Hypercorn) [2026-10-08 15:12:33 +0800] [12345] [INFO] Running on http://0.0.0.0:6191

用法：
    python3 tools/log_triage.py [日志路径] [--top N] [--since-last-restart]
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 我们自己的行 —— **新格式带日期**
LINE_DATED = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(\S+?):\s?(.*)$"
)
#: 我们自己的行 —— **旧格式只有时分秒**（历史日志里全是这种）
LINE_LEGACY = re.compile(
    r"^(\d{2}:\d{2}:\d{2})\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(\S+?):\s?(.*)$"
)
#: Hypercorn 自己的行 —— **本来就带日期**，用它给旧格式行反推日期
LINE_HYPERCORN = re.compile(r"^\[(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})\s*([+-]\d{4})?\]")

#: 「服务启动」—— 一次运行周期的开始标记
RESTART_MARK = "服务启动"

#: 把消息里的**可变部分**归一化，好让同类消息合并计数
NORMALIZERS: list[tuple[str, str]] = [
    (r"0x[0-9a-fA-F]+", "<HEX>"),
    (r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*", "<TS>"),
    (r"\d+\.\d+s?", "<NUM>"),
    (r"\b\d+\b", "<N>"),
    (r"'[^']{40,}'", "<LONGSTR>"),
]


@dataclass
class Entry:
    """一条被记录下来的 WARNING/ERROR。"""

    date: str  # 反推出来的日期；反推不到时为空串（**如实留空，不编造**）
    time: str
    level: str
    logger: str
    key: str  # 归一化后的模板
    raw: str

    @property
    def stamp(self) -> str:
        """`YYYY-MM-DD HH:MM:SS`；没有日期时退化成 `??` 前缀（不假装知道）。"""
        return f"{self.date} {self.time}" if self.date else f"????-??-?? {self.time}"


def normalize(msg: str) -> str:
    out = msg
    # JSON 尾巴（本项目的日志常带 `  {"path": ...}`）→ 只留键名，值归一化
    if out.count("{") and out.rstrip().endswith("}"):
        head, _, tail = out.partition("{")
        try:
            d = json.loads("{" + tail)
            if isinstance(d, dict):
                keys = ",".join(sorted(d))
                return f"{head.strip()} {{keys:{keys}}}"
        except Exception:
            pass
    for pat, rep in NORMALIZERS:
        out = re.sub(pat, rep, out)
    return out.strip()[:180]


def parse_lines(text: str) -> list[Entry]:
    """解析日志文本。

    🔴 **日期反推**：旧格式行（只有 `HH:MM:SS`）的日期取自**最近一条 Hypercorn 行的日期**。
    ⚠️ 反推是**逐行**做的，所以当天日期的变化会被正确跟随；
       而在**第一条 Hypercorn 行之前**的旧格式行**没有标尺** ⇒ 日期留空
       （如实标 `????-??-??`，**绝不猜成今天** —— 那会伪造出错误的"最近发生"）。
    """
    entries: list[Entry] = []
    current_date = ""
    for raw in text.splitlines():
        mh = LINE_HYPERCORN.match(raw)
        if mh:
            current_date = mh.group(1)
            continue
        m = LINE_DATED.match(raw)
        if m:
            stamp, level, logger, msg = m.groups()
            date, _, tm = stamp.partition(" ")
            if level in ("WARNING", "ERROR", "CRITICAL"):
                entries.append(Entry(date, tm, level, logger, f"[{logger}] {normalize(msg)}", raw.strip()[:260]))
            continue
        m = LINE_LEGACY.match(raw)
        if m:
            tm, level, logger, msg = m.groups()
            if level in ("WARNING", "ERROR", "CRITICAL"):
                entries.append(Entry(current_date, tm, level, logger, f"[{logger}] {normalize(msg)}", raw.strip()[:260]))
    return entries


def slice_since_last_restart(text: str) -> str:
    """切出**最后一次运行周期**的文本（从最后一个「服务启动」起）。

    为什么要它：日志是追加的，**历史噪音会把当前问题淹掉**。
    """
    idx = text.rfind(RESTART_MARK)
    if idx < 0:
        return text
    # 回退到该行的行首
    line_start = text.rfind("\n", 0, idx)
    return text[line_start + 1 :] if line_start >= 0 else text


def main() -> int:
    argv = sys.argv[1:]
    # ⚠️ 带值的选项要先摘掉它**和它的值**，否则 `--top 8` 里的 `8` 会被当成日志路径
    #    （实测踩到：`❌ 日志不存在：8`）。
    top = 25
    if "--top" in argv:
        i = argv.index("--top")
        if i + 1 < len(argv):
            top = int(argv[i + 1])
            del argv[i : i + 2]
        else:
            del argv[i]
    positional = [a for a in argv if not a.startswith("--")]
    log_path = Path(positional[0]) if positional else ROOT / "logs" / "serve.out"
    since_restart = "--since-last-restart" in argv

    if not log_path.is_file():
        print(f"❌ 日志不存在：{log_path}")
        return 1

    text = log_path.read_text(encoding="utf-8", errors="replace")
    total_lines = len(text.splitlines())
    if since_restart:
        text = slice_since_last_restart(text)

    entries = parse_lines(text)

    counts: Counter[str] = Counter()
    level_of: dict[str, str] = {}
    first_ts: dict[str, str] = {}
    last_ts: dict[str, str] = {}
    samples: dict[str, str] = {}

    for e in entries:
        counts[e.key] += 1
        level_of[e.key] = e.level
        first_ts.setdefault(e.key, e.stamp)
        last_ts[e.key] = e.stamp
        samples.setdefault(e.key, e.raw)

    scope = "最后一次运行周期（自最后一个「服务启动」起）" if since_restart else "全文"
    print(f"日志 {log_path}  范围：{scope}")
    print(f"  总行数 {total_lines}")
    print(f"  WARNING/ERROR 模板种类 {len(counts)}，总计 {sum(counts.values())} 条")
    undated = sum(1 for e in entries if not e.date)
    if undated:
        print(f"  ⚠️ 其中 {undated} 条**无法反推日期**（在第一条 Hypercorn 行之前，旧格式）")
    print()

    if not counts:
        print("✅ 没有任何 WARNING/ERROR")
        return 0

    print(f"{'次数':>6}  级别     首次 → 最后                     模板")
    print("-" * 110)
    for key, n in counts.most_common(top):
        print(f"{n:>6}  {level_of[key]:<8} {first_ts[key]} → {last_ts[key]}  {key}")

    print("\n===== 每条模板的样例行（便于判断是真错还是噪音）=====")
    for key, n in counts.most_common(top):
        print(f"\n▸ {n} 次 · {level_of[key]} · {key}")
        print(f"   {last_ts[key]}  样例: {samples[key]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
