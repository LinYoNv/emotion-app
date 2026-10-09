# 面板自托管字体

## 为什么自托管

面板是**零构建**的原生 HTML/JS（没有打包器、没有 CDN 依赖），
而且部署环境可能没有外网。字体一律**本地自托管**，不引任何外部 `@font-face` 源。

## 文件

| 文件 | 字体 | 轴 | 用途 |
|---|---|---|---|
| `geist-latin.woff2` / `geist-latin-ext.woff2` | Geist | `wght 400..700`（可变字体） | 面板正文与标题 |
| `geist-mono-latin.woff2` / `geist-mono-latin-ext.woff2` | Geist Mono | `wght 400..500`（可变字体） | 日志、ID、代码（对齐好看） |

来源：Google Fonts（`fonts.googleapis.com` / `fonts.gstatic.com`）的 **latin + latin-ext** 子集，
按 Google Fonts 返回的 `unicode-range` 原样搬运，四个文件共约 84 KB。

## 中文怎么办

**不含中文字形**（中文字体动辄 5 MB，本项目不引）。
`app.css` 的 `--font-sans` 里 Geist 后面跟的是**系统中文族**：

    "PingFang SC"        （macOS / iOS）
    "MiSans"             （若用户设备已装；小米开源、SIL OFL）
    "HarmonyOS Sans SC"  （鸿蒙）
    "Noto Sans CJK SC"   （Android / Linux，本机实测 fc-match 命中）
    "Source Han Sans SC" （思源黑体）
    "Microsoft YaHei"    （Windows）

⚠️ **不要**把插件里的 `MiSans-*.woff2`（每个 5 MB）复制进来 ——
那是插件的资产（`astrbot_plugin_memory_companion`），复制会既胀大仓库、
又制造一份会与上游漂移的副本。中文字形交给系统字体，观感本来就更贴平台。

## 许可

- **Geist / Geist Mono**：SIL Open Font License 1.1，全文见同目录 `LICENSE.txt`。
  OFL 允许随产品分发与自托管，要求保留许可与版权声明（本文件 + `LICENSE.txt` 即为此）。
- 本面板**不再使用** MiSans（此前 `--font-sans` 里对它只是"若已安装则用"的软引用，
  现在改为显式列出系统族，语义不变、只是更清楚）。
