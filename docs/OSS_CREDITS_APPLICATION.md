# OpenAI 开源额度申请 —— 填写文案

> 申请对象：`LinYoNv/emotion-app`
> 核实时间：2026-10-21（数据来自 GitHub API 实测，非估计）

## 一、先确认的硬条件（已核实）

| 表单要求 | 实际状态 |
|---|---|
| GitHub 用户名可见性设为公开 | ✅ `LinYoNv` |
| **代码仓库可见性设为公开** | ✅ `visibility: public`（`private: false`） |
| OpenAI 组织 ID | 你已填 `org-AKihOGCJYgGL7i2tllvU3Cp4` |

⚠️ 记忆里这条仓库曾记作 private，**实测现在是 public**，无需再改。

## 二、可以引用的真实数字（都能被审核方查证）

| 事实 | 数值 | 来源 |
|---|---|---|
| AstrBot 星标 | **42,555** | GitHub API |
| AstrBot 许可证 | AGPL-3.0 | 同上 |
| 本仓库星标 / fork | **1 / 0** | 同上 |
| 本仓库创建时间 | 2026-09-25 | 同上 |
| 本仓库体积 | 50,696 KB | 同上 |
| 可加载的插件数 | 6 | 服务启动日志 |
| 挂载的 API 数 | 148 | 同上 |
| 自动化判据 | 1261 passed | 全量 pytest |
| 打通的链路 | 4（命令派发 / LLM 提示词注入 / 模型调用 / 主动消息投递） | README |

**不能声称的**：月下载量（无发布物）、用户数、任何星标以外的社区指标。

## 三、框 1：为什么这个代码仓库符合要求（230 / 500 字符）

```
本项目把 AstrBot（GitHub 4.2 万星标、AGPL-3.0）的插件生态从"必须装 AstrBot 才能跑"中解耦：实现一层宿主兼容层，让第三方插件不改一行源码就能跑在独立的 Quart/SQLite 后端上。已跑通命令派发、LLM 提示词注入、模型调用、主动消息投递四条链路；6 个真实上游插件可加载并初始化，共挂载 148 个 API，1261 条自动化判据全绿。目前星标很少，但验证的是"插件生态可移植性"这条公共路径，成果可被其他宿主复用。
```

### 英文版（485 / 500 字符，推荐用这版提交，审核方是英文团队）

```
This repo decouples the AstrBot plugin ecosystem (42.5k stars, AGPL-3.0) from its host: a compatibility layer runs third-party plugins unchanged on a standalone Quart/SQLite backend. Four chains work end-to-end — command dispatch, LLM prompt injection, model calls, proactive delivery. Six real upstream plugins load and initialize, exposing 148 APIs, guarded by 1261 automated tests. Stars are few so far; the point is validating plugin portability as a reusable path for other hosts.
```

## 四、框 2：你将如何针对自己的项目使用 API 额度（244 / 500 字符）

```
主要用在真正吃算力的工程环节：①兼容层要复刻 AstrBot 的 API 面，需反复通读上游插件源码来对齐真实签名（如 AstrMessageEvent 的位置参数、StarMetadata 的运行时字段），而不是靠猜；②更关键的是"模型端口"——插件经我这层调上游 LLM，我要用真实端点做对照实验，确认 function calling、流式分块、提示词注入在 OpenAI 兼容协议下行为一致，这类差异本地假上游测不出来；③给 1261 条判据做循环补测与回归。不二次分发、不转卖。
```

### 英文版（493 / 500 字符）

```
Credits go to compute-heavy work: (1) replicating AstrBot's plugin API means reading upstream plugin source to match real signatures (event positional args, runtime metadata fields) instead of guessing; (2) the model port — plugins call upstream LLMs through my layer, so I need real endpoints to verify function calling, streaming and prompt injection behave identically over the OpenAI-compatible protocol, gaps a local fake upstream can't surface; (3) regression over 1261 tests. No resale.
```

## 五、其余字段

| 字段 | 建议 |
|---|---|
| 说明你的角色 | **核心维护者**（你选对了 —— 你是唯一作者） |
| 我感兴趣的是 | 只勾 **项目的 API 额度**（你已选对；Codex Security 是安全研究向，与你的需求无关） |

## 六、提交前建议先补两件事（会影响审核印象）

1. **仓库根目录没有 LICENSE**（实测 `ls LICENSE*` 空）。
   公开仓库无许可证 = 默认「保留所有权利」，严格说**不算开源**，而这是开源额度申请的
   核心条件之一。建议补一个（MIT 或 AGPL-3.0，取决于你想不想让别人闭源复用）。
2. **README 里的数字过期**：写的是 `1142 passed`、`67 条路由`，实际是 **1261 passed / 148 个插件 API**。
   审核方打开仓库第一眼看的就是 README，数字对不上是减分项。

### ⚠️ 另有一件我没替你决定的事

`plugins/` 下打包了 6 个第三方插件，其中 **5 个没有 LICENSE 文件**（只有 `nai_image` 有）。
这些是插件作者的作品，不是你的代码。把它们随仓库公开分发，授权状态是模糊的。
本次申请会把更多注意力引到仓库上，这件事值得你自己权衡一下（补授权说明 / 在 README 注明来源与归属 / 移出 `plugins/` 改为安装脚本拉取）。

## 七、诚实的期望管理

本仓库 **1 星、0 fork、创建不到一个月**。按「星标数 / 下载量」这两项衡量是很弱的。
唯一站得住的论点是第三条「对生态重要」，而且它是**真的**：AstrBot 有 4.2 万星，
你把它的插件生态与宿主解耦了，这个技术点有公共价值。

所以：文案能帮你把真实价值讲清楚，但不能把 1 星变成 4 万星。
申请可以投，**别抱高预期**；即使被拒也不影响项目本身。
