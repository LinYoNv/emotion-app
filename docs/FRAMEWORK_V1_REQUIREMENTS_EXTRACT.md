# 审计：新框架 v1 对 HOST 的要求，与明令禁止继承的旧系统

> 审计日期：2026-09-20。**只读侦察**，除本文件外未修改任何内容。
> 目的：三方的独立 Python 后端「情绪app」（自己实现 chat/persona/image + 内嵌 AstrBot 兼容插件宿主）
> 需要知道：新框架**要求宿主提供什么**、**明令禁止携带什么**。
>
> **所有 `file:line` 均已用脚本逐条回验**（213 项引文子串断言），非估算。

---

## 0. 权威性与证据口径（**先读，否则后面会误判**）

### 0.1 磁盘上有**两份互相矛盾**的设计包

| 路径 | 性质 | 关键内容 |
| --- | --- | --- |
| `/root/Ai-works/爱语/refs/latest_companion/docs/` | 作者仓库 `menglimi/astrbot_plugin_private_companion` 的 **git HEAD = `2313fd1`**（2026-09-18 推送） | 38 个顶层 md；`docs/` 是**子集** |
| `/root/Ai-works/爱语/refs/companion_framework_draft/astrbot_plugin_private_companion/docs/` | **设计包**（同级 `README_DESIGN_PACK.md` 说明：跨 Private/Memory/Content 三仓库汇总的传阅包，**无 `.git`**） | **85 个顶层 md + `evaluations/`(160 份) + 完整 `contracts/`(14 包)** |

**结论：`companion_framework_draft/...` 才是设计包**——`latest_companion/docs/` 里**根本不存在**
`FRAMEWORK_ROADMAP_AND_STATUS_V1.md`、`NATIVE_PLUGIN_INTEGRATION_V0.md`、`evaluations/`。
本审计中凡未注明路径的 `docs/...` 均指 **draft 包**。
（提醒：`latest_companion/docs/` 里**没有** roadmap，所以不能用它判断设计进度。）

### 0.2 v1 文档引用的两份文件**磁盘上根本不存在**

`COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md` 第 5、7、588 行三处引用：

```
:5    > …当前完成度以[路线看板](./FRAMEWORK_ROADMAP_AND_STATUS_V1.md)为准，生产证据以[验证目录](./evaluations/README.md)为准。
:7    > 当前结论：设计已经允许在独立源码根开始 G0-G2 的隔离构建；……准入条件见[从零构建准入与收尾审计](./evaluations/FRAMEWORK_ZERO_BUILD_READINESS_20260920.md)。
:588  | … | [从零构建准入](./evaluations/FRAMEWORK_ZERO_BUILD_READINESS_20260920.md) |
```

实测（**三项全部零命中**）：

```
$ find / -name "FRAMEWORK_ZERO_BUILD_READINESS*" 2>/dev/null
(无输出)
$ grep -rln "从零构建" --include=*.md /root/Ai-works/爱语/refs/
(无输出)
$ grep -rn "G0 新根基线\|G0-G6\|S5-STR-I01" --include=*.md /root/Ai-works/爱语/refs/
(无输出)
```

→ **G0–G6 门槛表、§14.3 的 `S5-STR-I01` 首条隔离闭环、以及 G0–G2 的准入判据，
在整个设计包中没有任何对应物。它们是 v1 文档独有的新内容。**
⚠️ 而 `S5-STR-I01` 与设计包里既有的 `S5-STR-01/02/03`（`docs/FRAMEWORK_ROADMAP_AND_STATUS_V1.md:308-310`）
**只有一字之差但完全是两件事**。

### 0.3 两包共有的 schema **逐字节相同**，只有一份不同

```
SAME  contracts/v1/schemas/runtime-scope.schema.json
SAME  contracts/v1/schemas/common.schema.json
SAME  contracts/execution/v1/schemas/{delivery-receipt,action-request,operation-result,execution-common}.schema.json
SAME  contracts/control/v1/schemas/capability-descriptor.schema.json
DIFF  contracts/control/v1/schemas/extension-manifest.schema.json      ← 唯一差异
```

`extension-manifest.schema.json` 的差异是**决定性的**：

| | `latest_companion`(HEAD 2313fd1) | draft 设计包（**权威，已采纳**） |
| --- | --- | --- |
| 第 12 行 | `"host_kinds"`（复数，`{"type":"string","minLength":1}` 任意串） | `"deployment_modes"`（**枚举四项**） |
| 第 27 行 required | `… "host_kinds" …` | `… "deployment_modes" …` |

**以 draft 为准**，依据 `docs/LEVEL_A_CLOSURE_DECISIONS_20260910.md:44,50`：

> `:44` `| **A（推荐）** | manifest 字段改名 `deployment_modes`，并在 Schema 内补上四项 enum；`runtime-scope` 的 `host_kind` 与 `not` 约束不动 | …`
> `:50` `**决定**：采纳方案 A（2026-09-11）；Schema 已改为 `deployment_modes` 并限制四种部署模式值，`host_kind` 保持宿主实现语义。`

> ⚠️ **给我们的直接含义**：`latest_companion` 那份 checkout **落后于**已采纳决定。
> 我们若照它实现 manifest，会写出**已被设计弃用**的字段名。

---

## A. 宿主侧义务（「宿主必须提供什么」）

### A.1 十一个具名端口及各自声明的输入/输出

**权威表**：`docs/PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:120-127`（§4「平台适配器只转换以下规范接口」）。
端口名单本身在 `docs/PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:45`。

| 端口 | 声明输入 | 声明输出/能力状态 | 证据 |
| --- | --- | --- | --- |
| `InboundAdapter` | 原生更新、连接游标 | `InboundEnvelope`、来源证据、ack/重复更新状态 | `PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:122` |
| `IdentityAdapter` | 外部账号、参与者、绑定请求 | canonical subject 候选、IdentityBinding 或 `identity_unresolved` | `:123` |
| `ConversationAdapter` | 外部 chat/thread/room | `conversation_ref`、参与者、线程能力、可见性和生命周期 | `:124` |
| `DeliveryAdapter` | 规范 ActionRequest、媒体引用、幂等键 | OperationResult + DeliveryReceipt；明确 accepted/delivered/uncertain | `:125` |
| `MediaAdapter` | 授权 ContentRef、用途、过期时间 | 平台可用附件或 `unsupported/degraded`，**不复制原媒体到 Kernel** | `:126` |
| `Auth/SecretProvider` | 逻辑能力请求 | 有效期授权/平台票据；**密钥不进入 DTO、快照或日志** | `:127` |

原文（`docs/PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:45`）：

> 端口和适配器由能力决定，不由平台名决定：`InboundAdapter`、`IdentityAdapter`、`ConversationAdapter`、`DeliveryAdapter`、`MediaAdapter`、`ModelAdapter`、`Clock`、`Store`、`TaskSupervisor`、`WorkspaceGateway` 和 `SecretProvider`。一个平台可以提供多个版本/能力，一个能力可以由不同平台或自建实现提供；Feature 不判断 `aiocqhttp`、Telegram 或 Discord。

**剩下 5 个端口在整份设计包里【只出现在这一行】——没有输入/输出定义。**
逐项核实结果：

| 端口 | 是否有声明 I/O | 设计包里能找到的最接近内容（**均不在 §4 表内，不能当契约**） |
| --- | --- | --- |
| `ModelAdapter` | ❌ **无** | `PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:45` 仅列名。相关但**不同名**：`ModelGateway`「按任务类型、人格授权和资源预算调用 Provider；统一取消和失败语义」`ROLEPLAY_PROACTIVE_REBUILD.md:64`；`ARCHITECTURE_RESET.md:1560`「模型调用先声明任务类型和资源预算，再由 ModelGateway 选择 Provider。失败时按任务执行超时、重试、备用模型和冷却，不由每个功能自行无限重试。」 |
| `Clock` | ❌ 无（列表外有定义） | `ROLEPLAY_PROACTIVE_REBUILD.md:61`「提供带时区的当前时间、时间段和可复现测试时钟」 |
| `Store` | ❌ **无** | 全包只有 `:45` 一处出现 `Store` 作为端口名；相关实体是 `StorageRuntime`（`COMPANION_EXTENSION_LIFECYCLE_V0.md:23`）与「共享数据库连接归 StorageRuntime」（`ARCHITECTURE_RESET.md:1554`） |
| `TaskSupervisor` | ❌ 无（列表外有定义） | `ROLEPLAY_PROACTIVE_REBUILD.md:67`「管理任务、连接、取消、热重载、generation 和资源预算」；`ROLEPLAY_PROACTIVE_REBUILD.md:533`「所有主动来源由 `TaskSupervisor` 统一管理。Feature 插件不创建自己的永久轮询器，也不直接维护发送定时器。」；`ARCHITECTURE_RESET.md:635` |
| `WorkspaceGateway` | ❌ 无（列表外有定义） | `PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:156`「管理 UI 使用 `WorkspaceGateway` 资源处理器和结构化错误/分页/能力状态；页面不直接调用某个插件的 Quart 路由或读取其 data_dir。」；`FRAMEWORK_DESIGN.md:292` |

**另有一条硬性附加声明义务**（`PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:131`）：

> `DeliveryAdapter` 必须声明 `supports_idempotency`、`supports_status_query`、`receipt_levels`、`max_payload`、`rich_media`、`threading` 和资源/限流能力。平台受理不等于送达；不支持查询的超时动作保持 uncertain，不能因为换宿主就自动重发。平台 adapter 不拥有记忆、主动候选或 history owner。

**`InboundEnvelope` 最小字段**（`PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:129`）：

> `InboundEnvelope` 至少包含 envelope_id、platform、account_ref、conversation_ref、external_event_ref、received_at、source_scope、payload 摘要、evidence_refs、dedupe_key 和 adapter_generation。payload 原文由适配器按隐私策略保留；Kernel 只接受结构化文本/媒体引用和最小必要证据。

### A.2 四种 packaging shell，以及**哪一种对应「我们自己的 Python 应用进程内托管插件」**

原文表：`docs/PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:141-146`（§5）。

| 模式 | 组件关系（原文） | 适用场景（原文） |
| --- | --- | --- |
| `plugin_hosted` `:143` | 应用入口由宿主加载，Kernel/Feature 经应用装配；AstrBot 完整发行首装附带全部可选模块，之后可独立丢弃；旧插件可暂以适配器接入 | AstrBot 完整发行设计、当前兼容运行与分批迁移 |
| **`standalone_service`** `:144` ⭐ | **Kernel、Feature、Workspace 在独立进程，平台 adapter 通过连接接入** | **独立 Bot 服务或后台任务** |
| `embedded_app` `:145` | Kernel/Feature 作为一站式应用内模块，**应用提供宿主端口** | 桌面、移动端或**自有 Web 应用** |
| `split_services` `:146` | 控制面、领域服务、平台 gateway 分进程/容器 | 高并发或独立扩缩容 |

**对我们的判定（含一处需要澄清的边界）**：

- 我们**不是** `plugin_hosted`：该模式定义是「**应用入口由宿主加载**」——AstrBot 是宿主。
  我们的关系是**反过来**的：我们的 Python 应用是进程主体，插件是我们的被加载方。
- 最接近的是 **`standalone_service`**：「Kernel、Feature、Workspace 在独立进程，平台 adapter 通过连接接入」——
  我们的 app 就是那个独立进程，平台适配（QQ/其它）通过连接接入。
- 但 `embedded_app` 的场景原文写的是「桌面、移动端或**自有 Web 应用**」，组件关系是
  「应用提供宿主端口」——**如果我们把自己视为「自有 Web 应用」并自任宿主，`embedded_app` 也说得通**。
  **两者差别在文档里没有被进一步定义**，唯一可用的划界语句是 `PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:148`：

> 模式只改变 transport 和 lifecycle shell，不改变能力 ID、schema、owner、回执和状态机。首期可以是 in-process facade；同机 IPC、HTTP/gRPC 等远程实现使用同一 envelope，必须具备认证、大小/超时/重放保护、版本协商和 cancellation 语义。Feature 不知道请求走内存、IPC 还是网络。

  → **对我们是好消息**：选哪个 shell **不改变契约面**，只改变 transport 与 lifecycle shell；
  且「首期可以是 in-process facade」被明文允许。
- **无论选哪个，有一条硬约束**（`PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:116`）：

> `host_kind` 与部署模式分开：例如 AstrBot 宿主使用 `host_kind=astrbot`，装配配置选择 `plugin_hosted`；独立服务、嵌入应用与拆分服务也由装配配置声明。纯内部能力调用无需虚构外部账号，对外投递再解析真实平台目标。

  `host_kind` **绝不会**是 `plugin_hosted` —— 见 §C.1 的 `allOf` 禁止条款。

### A.3 `PackageManifest` 必须声明什么

原文（`docs/PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:150`）：

> 一站式应用的 `PackageManifest` 至少声明 package_id/version、SDK/schema 版本、组件、能力/平台需求、迁移脚本版本、健康检查、资源预算、静态资源入口、数据目录策略、备份/恢复版本和可选模块。可选能力包另需关联产品 extension、来源仓库、固定发布资产及完整性、兼容依赖、家具入口、重启条件和迁移回退边界，具体格式与[可选能力主稿](./OPTIONAL_CAPABILITIES_AND_FURNITURE_V0.md#optional-capability-ownership)共同冻结。密钥、外部账号、设备票据和平台 webhook 在 `SecretProvider`/部署配置中注入；包内 manifest 不携带秘密。

**逐项清单**（11 基础 + 2 扩展）：

1. `package_id` / `package_version`
2. SDK / schema 版本
3. 组件（components）
4. 能力 / 平台需求
5. 迁移脚本版本
6. 健康检查
7. 资源预算
8. 静态资源入口
9. 数据目录策略
10. 备份 / 恢复版本
11. 可选模块
12. （AstrBot 完整发行另需）实际随附的每个模块、固定版本、资源完整性、独立安装单元 —— `:152`
13. （可选能力包另需）产品 `extension`、来源仓库、固定发布资产及完整性、兼容依赖、家具入口、重启条件、迁移回退边界 —— `:150`

**禁止**：`包内 manifest 不携带秘密`（`:150`）；`密钥、外部账号、设备票据和平台 webhook 在 SecretProvider/部署配置中注入`。

> ⚠️ **`PackageManifest` 没有 JSON Schema**。`grep -rln "PackageManifest" --include=*.json` 在整个 draft 包里**零命中**。
> 它是纯散文要求；能对上号的机器契约只有 `contracts/control/v1/schemas/extension-manifest.schema.json`
> （**不同对象**，见 §E.2 注）。

### A.4 可移植层「允许 import 什么」的声明规则

原文（`docs/PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:41`）：

> 可移植层允许使用 Python 标准库和明确声明的跨平台依赖；AstrBot、Quart/Hypercorn、OneBot、平台 SDK 和桌面 UI 只出现在宿主/平台/管理入口包。Feature Service 的核心用例不能 import 这些模块，也不能接收其对象。平台适配器可以在边界将原生对象转换为规范 DTO，转换后原对象不得进入领域任务或被后台保存。

分层图内的同名约束（`docs/PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:30-32`）：

```
Companion Kernel + companion_sdk (portable)
  ├─ RuntimeScope, clock, event, capability, task, policy, delivery boundary
  └─ no AstrBot event, platform SDK, Quart request, cookie or file path     ← :32
```

**验收判据**（`PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:192`）：

> | 核心可导入 | 便携层 import 扫描不依赖 AstrBot、平台 SDK、Quart request 或宿主 event |

v1 文档的同义表述（`COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:524`）：

> `adapters/` 可以依赖 foundation 与领域端口；领域层不反向依赖 AstrBot、OneBot、页面对象或旧插件模块。Core 只拥有公共协调，不吸收领域实体。

> 🔴 **对我们的直接含义**：我们**目前把 Quart/Hypercorn 当主框架、把插件宿主放在同进程**。
> 新框架要求 **Feature Service 的核心用例不能 import Quart**（`:41`），**页面不直接读取插件 data_dir**（`:156`）。
> 我们现有的「直接服务插件页面目录」的做法，**属于被点名禁止的形状**
> （`:156`：「页面不直接调用某个插件的 Quart 路由或读取其 data_dir」）。

---

## B. 明令禁止从旧系统携带的内容（**逐条逐字**）

### B.1 v1 §13.1 —— 五条「尤其不能直接带入」

`COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:486`（引导句）：

> 现役插件和旧数据库是能力盘点、行为样本、迁移输入和回归参照；它们不是新根的 Kernel、持久模型或发送权来源。尤其不能直接带入：

`:488-492` 五条：

| # | 行号 | 原文（逐字） |
| --- | --- | --- |
| 1 | `:488` | `- 全插件生命周期、mixin 继承树和隐式启动状态；` |
| 2 | `:489` | `` - 直接包装 `event.send`、`send_streaming` 或多平台 sender 的旧发送链； `` |
| 3 | `:490` | `- S4 ticket、当前会话、最近用户、正文或随机 ID 作为新 producer handoff；` |
| 4 | `:491` | `- 旧 Memory writer、缓存、双写 fallback 和混合 WorkRecords 作为新 owner 正本；` |
| 5 | `:492` | `- 合成 fixture、review Schema 或测试 receipt 作为真实授权和真实平台证据。` |

`:494`（收口句，同时给出**唯一允许的复用形状**）：

> 旧代码只通过窄的 `adapters/` 端口被参照或读取。迁移按 owner 和最小一致性交接组进行：最终快照、增量追平、旧 writer/sender fence、当前治理水位、非空恢复、单一新 owner 和回退边界逐项证明。没有稳定来源或归属的历史内容保留缺口，不用重写、摘要或重新生成伪装为已经迁移。

### B.2 v1 §14.1 —— 不从旧根继承

`COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:507`：

> 新框架应建立在独立源码根和临时数据库上，不从现役插件启动入口、旧数据库、生产 `PYTHONPATH` 或 sender 继承行为。推荐模块边界：

`COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:519`（目录注释，**旧数据只是「窄适配」对象**）：

```
  adapters/         # AstrBot、平台、旧数据和第三方能力的窄适配
```

### B.3 v1 §14.3 —— **最直接、最贵的一条**：隔离 bridge 不得变成兼容包装器

`COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:538-542`（§14.3 首条隔离闭环 `S5-STR-I01`）：

> `:540` 首个推荐闭环是被动纯文本的 `S5-STR-I01`：完整 producer handoff -> Delivery -> 无网络 transport -> History/CAP/Opportunity receipt inbox -> Feedback observation。它验证 producer 不能补造身份或材料、Delivery 必须有完整 task/attempt/scope/target/plan/source/boundary/budget/part、ACK 丢失可去重恢复、未知/迟到 receipt 留在原 operation、关闭先清 transport 再冻结 route。

⭐ **`COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:542`（逐字）**：

> 此 I01 只证明隔离结构。模型、工具、平台、TTS、真实宿主和生产 owner 调用必须为零；**现有隔离 bridge 只能作为语义参照与 fixture 来源，不能直接移植为新根的兼容包装器。**

### B.4 v1 §11.2 / §2.2 —— 插件与能力不得私建 sender / 直取宿主资源

| 行号 | 原文（逐字） |
| --- | --- |
| `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:102` | `8. **能力不得私建第二个角色或 sender。** 扩展保存领域事实，角色、身份、授权、预算、Delivery、长期记忆和公共主动由各自 owner 统一处理。` |
| `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:428` | ``外部插件通过 `CapabilityDescriptor` 声明版本、依赖、来源/查询/动作端口、所需权限、资源预算、安装/升级/卸载、恢复和诊断语义。框架注册、发现、绑定、启用、取消、lookup 和 resume；外部插件不能直接取得全局数据库、人格 Prompt、主动调度或平台 sender。`` |
| `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:430` | `来源类插件只提供版本化材料和读取能力，统一框架控制授权、分享机会、意图、投递和追问。动作类插件通过 ActionRequest/OperationResult 接入，必须提供稳定 operation 和未知结果查账语义。没有完整描述符或权限时，能力显示为不可用或降级，而非隐式走旧路径。` |
| `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:295` | `…外部来源插件只能登记、发现、绑定、读取和提供版本化材料；它不能绕开统一主动、直接发送或自己保存另一份人格偏好。` |
| `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:309` | `每个 target 在同一时刻只有一个可提交 sender。切换 sender 前要冻结旧新派发、盘点非空 operation/part/unknown/通知、提高 route epoch 并保留原账。未知回执不能改走另一个 sender 补发。` |
| `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:99` | `5. **未知先查原操作。** 网络超时、重启、ACK 丢失、回调迟到或 worker 更替后，先查询原 operation、part、receipt 和领域账本；不得换一个 ID 重新做可能已发生的外部动作。` |

### B.5 设计包侧（v1 之外的补充约束，**authoritative pack**）

`docs/S7_RELEASE_MIGRATION_AND_ROLLBACK_V0.md:81`（**同机共存规则**）：

> 旧插件与新应用同机时，逐一登记旧定时器、消息 hook、后台任务、fallback、writer 和 sender。**兼容适配器复用原服务；新实现获得业务准入之前，不能再起一套角色、日记、主动决策、设备监听或发送者。** 代码准备可在线进行，最终交接是否需要停机按真实存储/宿主能力说明，不承诺零停机。

`docs/FRAMEWORK_ROADMAP_AND_STATUS_V1.md:354`（**逐插件定性**，表格行）：

> `| `astrbot_plugin_private_companion` | 宿主入口、人格、关系、情绪、日程、主动、页面、部分发送和诊断 | Role / Affect / Relationship / Proactive；Platform adapter 由装配层承接，Delivery 由唯一投递 owner 承接 | S2、S4、S5 | 旧核心能力最多，**不能整体当作 Kernel**；发送、任务和单键作用域需要逐处拆分 |`

`docs/FRAMEWORK_ROADMAP_AND_STATUS_V1.md:355`：

> `| `astrbot_plugin_remember_you` | 长期记忆、历史整理、召回、画像、权限和迁移 | Memory | S3 | 记忆是首条 Writer/Query 参考切片；**旧 bridge 保留兼容，不能直接当新 Writer** |`

`docs/FRAMEWORK_ROADMAP_AND_STATUS_V1.md:363`：

> `| `astrbot_plugin_bug_companion` | 问题诊断、治理辅助和修复记录 | Governance / Diagnostics | S0-S7 横向 | 它提供证据和治理入口，**不应成为业务状态的第二个 owner** |`

`docs/FRAMEWORK_ROADMAP_AND_STATUS_V1.md:364`（表格末行）：

> `| 旧日历/世界实现 | agenda、活动、日程和部分连续生活投影 | Calendar / World | S3、S4 | 先抽取事实、事件和 checkpoint；**不能把旧 prompt 页直接当长期状态** |`

`docs/NATIVE_PLUGIN_INTEGRATION_V0.md:35`：

> 4. 模型可以产生解释、提议和计划，**不能直接写数据库、提升可见性、发送消息或宣称外部动作成功**。

`docs/PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:131`：

> …平台 adapter **不拥有记忆、主动候选或 history owner**。

`docs/FRAMEWORK_DESIGN.md:9`：

> 工作的主次是：设计更高效、更好用的新框架为主，旧系统的优化、适配和迁移为辅。旧代码提供用户能力清单、行为样本和工程经验，**不能决定新框架的永久模块边界**。

`docs/COMPANION_EXTENSION_LIFECYCLE_V0.md:13`：

> 后续[S7 完整发行与交接设计](./S7_RELEASE_MIGRATION_AND_ROLLBACK_V0.md#release-owner-handoff)明确旧入口盘点、收件缓冲、**独立 sender 交接**、路由提交与候选激活，恢复保持原 operation/attempt 及稳定 owner。

`docs/S5_INTEGRATED_ACCEPTANCE_V0.md:87`：

> …平台有有效的可靠幂等保证时才可按原操作恢复，不支持查询/去重或保证已过期时保留未知并结束旧流，**不能换 generation、计划或旧发送链再发**。

`docs/EXISTING_PLUGIN_AUDIT.md:69`（**对旧代码事实的描述，非规范，但告诉我们旧发送链有多散**）：

> …与此同时，屏幕、直播、游戏、图片和 AT 转发仍有自己的 `event.send`、`context.send_message` 或 `send_by_session` 调用。第一步应逐处判断它们属于命令回复还是业务投递，并记录是否拥有 `dedupe_key`、`DeliveryReceipt` 和 `InteractionLedger` 结算。

### B.6 `docs/S7_RELEASE_MIGRATION_AND_ROLLBACK_V0.md:216-224` —— **「可复用实现」白名单**

这是全包里**唯一**正面列出「当前可复用」的表，**且第三列全部是「仍未交付」**：

| 静态核对入口 | 当前可复用内容 | 本设计要求但仍未交付的部分 |
| --- | --- | --- |
| 构建脚本 / 发行检查 | 当前单插件运行文件集合、确定 ZIP 参数、清单/元数据核对及临时目录安装导入；存在升级/还原代码目录路径 | 完整应用多单元清单、固定上游依赖、非空资料迁移/回滚及真实支持矩阵；缺历史包会跳过旧版路径，不能当通过 |
| 存储迁移接口 | 登记迁移、原子版本/检查点和局部 namespace 写资格 | 旧格式修订追平、**所有旧入口停写**、跨 owner 交接组与生产路由切换 |
| BackupSet / 离线恢复 | 可信清单、资源/治理核验、独立目录与 quarantined 候选 | 当前授权治理全覆盖、备份清理、生产激活、任意 owner 恢复和跨宿主搬迁 |
| 旧快照与差异 | 可信来源快照、逐记录差异、原消息转换和只读对照 | 真实历史归属补证、其它领域转换、最终停写/追平与切换后新增保护 |
| Content 生命周期 | 局部宿主方法、持久选择、任务收束和操作状态 | 通用固定版本安装器、跨模块兼容、完整首装和生产网络升级 |
| 可移植夹具与 SDK | 公共 DTO/映射设计、局部隔离回放和只读 shell | 可用的 PortableSnapshot 格式/工具、真实原生平台录制与多宿主写入交接 |

`docs/S7_RELEASE_MIGRATION_AND_ROLLBACK_V0.md:227`：

> 本轮只读源码和历史报告，没有运行上述工具。代码目录还原可证明的范围仅限代码布局/导入；空库初始化也不能代替本稿非空场景。

### B.7 关键词检索结果（**用词与任务描述不完全一致，如实报告**）

| 检索词 | draft 包命中 | 说明 |
| --- | --- | --- |
| `兼容包装` | **0** | 该词**只出现在 v1 文档** `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:542` |
| `隔离 bridge`（含空格） | **0** | 同上，只在 `:542`（写作「现有隔离 bridge」） |
| `旧发送链` | 2 | `evaluations/S5_JOINT_DESIGN_REVIEW_20260916.md:25`、`S5_INTEGRATED_ACCEPTANCE_V0.md:87` |
| `event.send` | 2 (+源码/测试) | `NATIVE_PLUGIN_INTEGRATION_V0.md` 无；`evaluations/RV3_ROLE_TOOL_20260918.md:35`、`EXISTING_PLUGIN_AUDIT.md:69`；v1 文档 `:489` |
| `send_streaming` | 1 | `evaluations/RV3_ROLE_TOOL_20260918.md:27`；v1 文档 `:489` |
| `不得直接移植` / `不能直接移植` / `不能带入` / `直接带入` / `直接包装` | **0** | 措辞实际是「**尤其不能直接带入**」「**不能直接移植为**」「**直接包装**…的旧发送链」 |

> ⚠️ **重要**：`兼容包装`/`隔离 bridge` 这两个词**在整份设计包里不存在**——它们是 v1 文档
> 2026-09-20 新增的措辞。**不能用旧文档去交叉验证这一条**，只能用 v1 文档本身。

---

## C. 身份 / 作用域契约

### C.1 `RuntimeScope` —— 必填字段与 `allOf` 条件

**Schema**：`docs/contracts/v1/schemas/runtime-scope.schema.json`（227 行）。
**标题（`:4`）**：`"Resolved invocation scope; never an authorization grant"` —— **本身不是授权**。

**14 个 required（`:121-136`）**：
`ecosystem_id`, `installation_id`, `runtime_instance_id`, `host_kind`, `bot_id`, `platform`,
`account_id`, `conversation_ref`, `platform_conversation_id`, `session_id`, `user_id`, `group_id`,
`persona_id`, `persona_binding_revision`（末项在 `:135`）。

**`additionalProperties: false`（`:137`）** —— 多一个字段就校验失败。

**字段约束**：

| 字段 | 约束 | 行 |
| --- | --- | --- |
| `ecosystem_id` | `$defs/token`（必填 string） | `:7-9` |
| `installation_id` | `$defs/token` | `:10-12` |
| `runtime_instance_id` | `$defs/token` | `:13-15` |
| `host_kind` | `$defs/token` **+ 被 `allOf` 的 `not` 条款禁止取四个部署模式值** | `:16-18`, `:209-225` |
| `host_id` | token 或 null（**非 required**） | `:19-28` |
| `bot_id` | `$defs/token` | `:29-31` |
| `platform` | token 或 **null** | `:32-41` |
| `account_id` | token 或 null | `:42-51` |
| `conversation_ref` | token 或 null | `:52-61` |
| `platform_conversation_id` | token 或 null | `:62-71` |
| `conversation_id` | token 或 null，**`"deprecated": true`（`:81`）**，且**不在 required** | `:72-82` |
| `session_id` | token 或 null | `:83-92` |
| `user_id` | token 或 null | `:93-102` |
| `group_id` | token 或 null | `:103-112` |
| `persona_id` | `$defs/token` | `:113-115` |
| `persona_binding_revision` | `integer`, `minimum: 0` | `:116-119` |

`$defs/token` 定义（`docs/contracts/v1/schemas/common.schema.json:6-11`）：
`type: string`、`minLength: 1`、`maxLength: 256`、`pattern: "^[^\\s\\x00-\\x1f\\x7f]+$"` —— **无空白、无 C0 控制字符**。

**`allOf` 四条条件（`:138-226`）：**

| # | 行 | 条件 | 后果 |
| --- | --- | --- | --- |
| 1 | `:139-173` | `if platform == null`（`:142-145`） | **then** `account_id`、`platform_conversation_id`、**`conversation_id`**、`user_id`、`group_id` **必须全为 null**（`:148-164`）；**else** `account_id` 必须是 token（非 null）（`:166-172`） |
| 2 | `:174-189` | `if platform_conversation_id` 是 string（`:177-180`） | **then** `conversation_ref` 必须是 token（`:183-186`） |
| 3 | `:190-208` | `if group_id` 是 string（`:193-196`） | **then** `conversation_ref` **和** `platform_conversation_id` 都必须是 token（`:199-206`） |
| 4 | **`:209-225`** | **`"not": { "properties": { "host_kind": { "enum": ["plugin_hosted","standalone_service","embedded_app","split_services"] } }, "required": ["host_kind"] }`** | **`host_kind` 永远不得等于这四个部署模式值中的任何一个** |

⭐ **第 4 条原文（`:209-225`，逐字，行边界已核对）**：

```json
    {                                     ← :209
      "not": {                            ← :210
        "properties": {
          "host_kind": {
            "enum": [                     ← :213
              "plugin_hosted",            ← :214
              "standalone_service",       ← :215
              "embedded_app",             ← :216
              "split_services"            ← :217
            ]
          }
        },
        "required": [                     ← :221
          "host_kind"                     ← :222
        ]
      }
    }                                     ← :225
```

**语义（`docs/LEVEL_A_CLOSURE_DECISIONS_20260910.md:44,50`）**：

> `:44` `| **A（推荐）** | manifest 字段改名 `deployment_modes`，并在 Schema 内补上四项 enum；`runtime-scope` 的 `host_kind` 与 `not` 约束不动 | …`
> `:50` `**决定**：采纳方案 A（2026-09-11）；Schema 已改为 `deployment_modes` 并限制四种部署模式值，**`host_kind` 保持宿主实现语义**。`

与 `docs/PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:116` 一致：

> `host_kind` 与部署模式分开：例如 AstrBot 宿主使用 `host_kind=astrbot`，装配配置选择 `plugin_hosted`；独立服务、嵌入应用与拆分服务也由装配配置声明。纯内部能力调用无需虚构外部账号，对外投递再解析真实平台目标。

🔴 **对我们的直接含义**：我们的 scope **必须**写 `host_kind = "astrbot"`（若沿用 AstrBot 兼容语义）
或另一个**自造宿主实现名**（例如 `"emotion_app"`），**绝对不能**填 `plugin_hosted`；
而部署模式（`standalone_service` / `embedded_app`）只能出现在 **`extension-manifest.deployment_modes`**。

**冲突点（低风险，但要知道）**：`docs/ROLE_RUNTIME_KERNEL_DESIGN.md:119` 把
`deployment_mode` 放进了 `RuntimeScope` 的字段清单（`:115-121` 的 text block）：

```
  host_kind                  # astrbot 等宿主实现            ← :118
  deployment_mode             # plugin_hosted / standalone_service ...   ← :119
```

这**与 `runtime-scope.schema.json` 的 `additionalProperties:false` + `not` 条款直接冲突**——
按 Schema 校验，带 `deployment_mode` 的 scope **一定不通过**。
`docs/NATIVE_PLUGIN_INTEGRATION_V0.md:125` 已明确站 Schema 一侧：

> canonical subject 通过可信 IdentityBinding 解析；`deployment_mode` 属于装配配置，`scope_fingerprint` 属于绑定投影。`sharing_mode/sharing_anchor/sharing_policy_revision` 按共享包放入对应 DTO，不能加进已有封闭 RuntimeScope；其中 `platform/account_id` 参与主体映射，不能只靠同名数字建立跨平台身份。

→ **取 `NATIVE_PLUGIN_INTEGRATION_V0.md:125` + Schema，弃 `ROLE_RUNTIME_KERNEL_DESIGN.md:119` 的示意块。**

### C.2 `IdentityBinding` 需要什么

**关键事实：设计包里没有 `IdentityBinding` 的 JSON Schema。**
`grep -rn '"\$id"' contracts/ | grep -i ident` → **零命中**（`contracts/` 全部 `$id` 中无 identity 相关）。
它有两个**互相不一致**的定义来源：

**(a) `docs/PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:65-79` §3 示例（12 字段）：**

> `:65` 外部身份使用可审计的 `IdentityBinding`：

```json
{                                                     ← 围栏 :67
  "binding_id": "identity-binding-001",               ← :69
  "ecosystem_id": "ecosystem-001",
  "canonical_subject_ref": "subject-123",
  "platform": "telegram",
  "account_id": "bot-account-9",
  "external_subject_id": "telegram-user-77",
  "kind": "user",
  "authority": "user_confirmed",
  "valid_from": "2026-09-07T00:00:00Z",
  "valid_to": null,
  "revision": 2                                       ← :79
}
```

**(b) `docs/ROLE_RUNTIME_KERNEL_DESIGN.md:432-446` §8.1 text block（11 字段）：**

```text
IdentityBinding {                                     ← :432
  binding_ref
  subject_ref
  platform
  platform_account_id
  platform_user_id
  proof_kind: explicit_consent | account_link | host_verified | imported
  confidence
  authorization_revision
  effective_from / effective_until
  status: pending | active | revoked
}
```

🔴 **两者字段名逐项不同**：`binding_id` vs `binding_ref`；`canonical_subject_ref` vs `subject_ref`；
`account_id` vs `platform_account_id`；`external_subject_id` vs `platform_user_id`；
`authority`（无枚举）vs `proof_kind`（**有枚举**）；`valid_from/valid_to` vs `effective_from/effective_until`；
`revision` vs `authorization_revision`。**例 (a) 完全没有 `status` 字段，(b) 没有 `ecosystem_id`。**

**两侧共同强制的语义（互为补充，可同时采纳）：**

| 约束 | 出处（逐字） |
| --- | --- |
| 昵称/头像/数字 ID/同一字符串**不能自动合并** | `PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:83`：「昵称、头像、平台数字 ID 和同一字符串不能自动合并身份。多个平台身份只有在明确授权、证据和 revision 下投影到同一 canonical subject；不确定时保留为不同主体。群、私聊、频道和网页会话各自有 visibility namespace；迁移不扩大可见范围。」 |
| 两 ID 不能拼成跨平台主键；相似度只是线索 | `ROLE_RUNTIME_KERNEL_DESIGN.md:446`：「`platform_account_id` 是 Bot 在平台上的账号，`platform_user_id` 是该平台用户；二者不能拼接成跨平台主键。昵称、头像、相同数字 ID、IP、语言和行为相似度只能作为待确认线索，不能自动建立 `linked` 绑定。」 |
| 解析失败返回 `identity_unresolved` | `PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:123`（`IdentityAdapter` 输出列） |
| 未确认映射保持隔离 | `ARCHITECTURE_RESET.md:224`：「…无法确认逻辑归属则返回 `scope_unresolved`，**不把 AstrBot 数据目录名、平台数字 ID 或昵称当作稳定 owner**。跨平台的主体、会话和账号使用带 revision 的 IdentityBinding/ConversationBinding，未确认映射保持独立。」 |
| 绑定建立/撤回/过期都推进 `authorization_revision` | `ROLE_RUNTIME_KERNEL_DESIGN.md:459`：「绑定建立、撤回或过期都会增加 `authorization_revision`。撤回后新的事件不能读取跨平台 user 投影；历史记录按 owner 的保留和删除策略处理。跨平台映射不等于跨 Bot 或跨人格共享，后者仍须单独授权。」 |

**v1 文档的定位**（`COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:84`）：

> `| `IdentityBinding` | 可信发送者、操作者、语义主体、Actor、资源 owner、受众和平台路由之间的受管映射。 |`

`COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:169`：

> 任何未绑定、冲突、失效或无法解析的身份都返回明确的缺口，例如 `scope_unresolved`、`authorization_revoked`、`audience_changed` 或 `target_binding_mismatch`；系统不通过先读一次、先发一次或采用最近用户来试探。

> ⚠️ **对我们的直接含义**：**`IdentityBinding` 没有可编程契约**。若我们要实现它，必须
> **自己选一套字段**，并明确记录「我们选了 (a) 还是 (b)」。这是设计包的一个真实缺口，不是我们读漏了。

### C.3 `sharing_mode` 与读合并顺序 —— ⭐⭐⭐ **文档内部直接矛盾**

**`sharing_mode` 取值**（三个，多处一致）：

- Schema：`docs/contracts/sharing/v1/schemas/sharing-binding.schema.json:24-31`（`"mode"` `:24` → `"enum"`；首值 `"session"` `:26`）→ `["session","user","global"]`
- `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:173`：「共享语义使用 `sharing_mode=session|user|global`、由 Kernel 签发的 `sharing_anchor` 和 `sharing_policy_revision`」

**必填三字段（`sharing-binding.schema.json:11-22`）**：`sharing_mode`、`sharing_anchor`、`sharing_policy_revision`；
`additionalProperties: false`（`:23`）；标题（`:4`）：`"Resolved sharing partition; fields are not an authorization grant"`。

#### C.3.1 矛盾：同一份设计包里，**6 处声明，3 : 3 两种相反顺序**

| # | 文件:行 | 原文（逐字） | 顺序 |
| --- | --- | --- | --- |
| 1 | `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:181`（v1 §4.2） | 「读取可以有限组合 `session -> user -> global` 投影，但每层分别检查 ACL、用途、受众、来源版本、TTL、脱敏与预算。写入只针对一个确定归属；从 session 提升到 user 或 global 必须由带证据的领域提议和当前策略确认。」 | **session → user → global** |
| 2 | `docs/COMPANION_CONTRACTS_AND_VERTICAL_SLICE.md:89` | 「读取上下文最多按 `session -> user -> global` 合并，每层都要重新经过授权、用途、版本、TTL、脱敏和预算检查。写入不得由模型把 session 状态自动扩大到 user/global；扩大共享范围必须产生带证据的 StateTransitionProposal 或 MemoryProposal，并由 owner 依据策略和授权确认。群主题、成员关系和第三方发言不因存在 user_id 就进入用户跨会话档案。」 | **session → user → global** |
| 3 | `docs/NATIVE_PLUGIN_INTEGRATION_V0.md:77`（§2.3） | 「私聊窗口使用 `Audience.mode=private`，可以在授权后组合 `session -> user -> global` 三层投影。群聊窗口使用 `Audience.mode=group`，人格和公共世界仍来自同一个 `Actor`，但成员发言、群主题和占用态默认停留在群 Session；只有明确授权的用户投影可以加入当前受众。群聊不会为每个发言人复制人格，也不会把私聊专属事实注入群上下文。」 | **session → user → global** |
| 4 | `docs/NATIVE_PLUGIN_INTEGRATION_V0.md:414`（§5.2） | 「`sharing_mode` 不是授权结果。每个投影还必须携带 `sharing_anchor`、`sharing_policy_revision`、`visibility`、`purpose`、`revision`、TTL 和 `EvidenceRef`。读取时按 `global -> user -> session` 合并，每层独立做 ACL、脱敏、预算和时效检查；写入不能由模型把 session 自动升级到 user/global。」 | **global → user → session** |
| 5 | `docs/ROLEPLAY_PROACTIVE_REBUILD.md:232` | 「PromptCompiler 固定使用 `global -> user -> session` 的有界顺序，并在每层标记 visibility、purpose、revision、TTL 和 evidence_refs。私聊正文、专属关系摘要、群成员观察和第三方信息不能跨层自动升级；只有 Memory/World owner 接受带证据的提议后，才生成可被其他场景读取的 user 或 global 投影。宿主原生历史作为一个可去重的贡献源参与编排，不由群聊路径清空并重建第二套历史。」 | **global → user → session** |
| 6 | `docs/COMPANION_INJECTION_PROTOCOL.md:229`（§5.1.1） | 「上下文采用**有界三层组合：先取当前受众允许的 global 安全投影，再取已解析用户的 user 投影，最后取当前 session 的近期场景。** 每一层保留 `source/anchor/visibility/purpose/revision/TTL`，合并由 Kernel 去重和裁剪；群成员观察、群主题和第三方消息默认停留在 session 或群场景，只有带证据的提议被 owner 接受后才可转成 user/global 状态。global 事件可以影响角色公共情绪、活动和世界进度，但必须去除私聊主体、原文和用户专属证据。」 | **global → user → session** |

**计票：`session → user → global` 三处（v1 §4.2 + CONTRACTS + NATIVE §2.3）；
`global → user → session` 三处（NATIVE §5.2 + ROLEPLAY + INJECTION）。3 : 3，无多数。**

#### C.3.2 为什么这不只是措辞问题

`docs/COMPANION_INJECTION_PROTOCOL.md:229` 是全包里**唯一**对顺序给出**语义理由**的地方，
且理由**指向「global 优先」**：

> 「**先取**当前受众允许的 global 安全投影，**再取**已解析用户的 user 投影，**最后取**当前 session 的近期场景。」

这句描述的是一个**从最泛到最具体、越具体越晚应用（因而越具体越能覆盖）**的 layering——
即 **global → user → session**。若反过来写 `session -> user -> global`，按「后写覆盖」读，
**最泛的 global 会覆盖最具体的 session**，这在语义上是错的（用户级别的偏好不该被全局世界状态压掉）。

#### C.3.3 同一文件里还有**两种**别的排序，别混淆

`docs/COMPANION_INJECTION_PROTOCOL.md:259` —— 这是 **binding 匹配优先级**，与共享层无关：

> 绑定先按安装/Bot 筛选，在平台/账号命名空间内匹配。私聊顺序为 `session` > `conversation + user` > `conversation` > `user` > `bot` > `installation`；群聊为 `session` > `conversation/group` > `bot` > `installation`，普通群消息不应用发言人的私聊绑定。

`docs/COMPANION_INJECTION_PROTOCOL.md:285` —— 这是**窗口修订隔离**，也与共享层无关：

> 窗口切换只改变 `active_window_ref`。A 窗口的事件若改变公共活动或可共享情绪，B 窗口读取新的 `global_revision`；…

→ **我们的判断**：`session -> user -> global` 那三处，很可能把
**「scope 由窄到宽的自然枚举顺序」** 或 **「token 预算裁剪优先级（越窄越先保留 → 先列举）」**
误写成了「合并顺序」。但**文档没有说明**，因此**这是一个未被作者裁决的冲突**。

> 🔴 **给我们的建议（本审计唯一给出的行动建议）**：在我们自己的实现里，
> **显式采用 `global → user → session`（后层覆盖前层），并在代码注释里标明依据是
> `NATIVE_PLUGIN_INTEGRATION_V0.md:414` + `COMPANION_INJECTION_PROTOCOL.md:229`**；
> 同时**去问作者**，因为 `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:181` 是他 2026-09-20 亲手写的最终稿，
> 而他写在最终稿里的顺序与 INJECTION 的语义理由相反。

#### C.3.4 两处一致的部分（可放心实现）

| 规则 | 出处（逐字） |
| --- | --- |
| 每层**独立**校验 ACL / 用途 / 受众 / 版本 / TTL / 脱敏 / 预算 | `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:181`、`NATIVE_PLUGIN_INTEGRATION_V0.md:414` |
| **写入只针对一个确定归属**；模型不得自动升级 session→user/global | 同上两处 + `COMPANION_CONTRACTS_AND_VERTICAL_SLICE.md:89` |
| 升级必须经 owner 接受 `StateTransitionProposal` / `MemoryProposal` | `NATIVE_PLUGIN_INTEGRATION_V0.md:416`、`COMPANION_CONTRACTS_AND_VERTICAL_SLICE.md:89` |
| `sharing_mode` **不是**授权结果；不替代 scope/visibility/purpose/retention | `NATIVE_PLUGIN_INTEGRATION_V0.md:414`、`COMPANION_CONTRACTS_AND_VERTICAL_SLICE.md:85`、`contracts/sharing/v1/README.md:15` |
| `sharing_anchor` 是不透明引用，JSON 字符串本身不是凭证 | `contracts/sharing/v1/README.md:15` |

**三层的 anchor 定义**（`docs/NATIVE_PLUGIN_INTEGRATION_V0.md:409-411`）：

```text
session -> installation + bot + account + persona + conversation_ref + session_id   ← :409
user    -> installation + bot + account + canonical_subject + persona
global  -> explicit installation + bot + persona public partition
```

**global 允许/禁止**（`docs/NATIVE_PLUGIN_INTEGRATION_V0.md:416` 逐字）：

> global 允许的内容是公共人格设定、脱敏的世界实体、公共活动进度和对所有目标都安全的角色状态。global 禁止包含用户姓名、私聊原文、单个用户的关系细节、群成员观察、精确位置、健康原始值、屏幕帧、凭据、游戏私密手牌和未授权媒体。跨层升级必须由 owner 接受 `StateTransitionProposal` 或 `MemoryProposal`。

v1 文档的三范围表（`COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:175-179`，表头 `:175`，`session` 行 `:177`）：

| 范围 | 用途 | 禁止事项 |
| --- | --- | --- |
| `session` | 当前真实会话的临时语境、群内话题和短期互动 | 新会话或其他成员不能借恢复读取。 |
| `user` | 同平台、同账号、已获授权的用户级偏好与关系投影 | 群成员、第三方内容和群主题不能因存在 user ID 自动进入。 |
| `global` | 明确的安装/Bot/角色公共世界资源与角色公共状态 | 不承载用户私密事实，不能当作“全库可见”。 |

---

## D. 投递 / 操作契约

### D.1 `DeliveryReceipt` —— 状态枚举与条件规则

**Schema**：`docs/contracts/execution/v1/schemas/delivery-receipt.schema.json`（423 行）。
**标题（`:4`）**：`"companion.delivery-receipt.v1 effect evidence independent of task completion"`。

**`status` 枚举（8 值，`:58-67` 逐字）**：

```
"pending",      ← :59
"submitted",    ← :60
"accepted",     ← :61
"delivered",    ← :62
"partial",      ← :63
"uncertain",    ← :64
"failed",       ← :65
"cancelled"     ← :66
```

与 v1 §7.4 表（`COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:303-307`）完全一致：

| 状态 | 含义（v1 原文） |
| --- | --- |
| `pending` / `submitted` `:303` | 已持久登记或已向目标提交，尚未得到最终平台证据。 |
| `accepted` `:304` | 平台接受请求，不等于用户已看到或听到。 |
| `delivered` / `partial` `:305` | 有对应的实际送达或部分完成证据。 |
| `uncertain` `:306` | 提交后结果未知；只能查原 operation/part。 |
| `failed` / `cancelled` `:307` | 有明确失败或在外部提交前被停止的证据。 |

**20 个 required（`:136-157`）**：
`schema_version`, `delivery_id`, `receipt_revision`, `operation_id`, `delivery_owner_ref`,
`history_owner_ref`, `scope`, `receipt_kind`, `part_id`, `parts_ref`, `target_ref`, `status`,
`attempt_id`, `attempts`, `platform_receipt_ref`, `submitted_at`, `delivered_at`, `verification`,
`reason_code`, `trace_id`。`additionalProperties: false`（`:158`）。

**`allOf` 九条条件规则（行边界已逐条核对）：**

| # | 行 | 条件 | 后果 |
| --- | --- | --- | --- |
| 1 | `:160-193` | `receipt_kind == "part"`（`:164`） | **then** `part_id` 必为 token、`parts_ref` 必须 **null**、`status` **不得为 `partial`**；**else** `part_id` 必须 null、`parts_ref` 必须是 reference |
| 2 | `:194-216` | `attempts == 0`（`:197-198`） | **then** `attempt_id` 必须 **null**；else `attempt_id` 必为 token |
| 3 | `:217-242` | `status ∈ {submitted, accepted, delivered, partial, uncertain}`（`:220-228`） | **then** `attempts` 必须 ≥1（`:233-236`）、`attempt_id` 必为 token（`:237-239`） |
| 4 | **`:243-282`** | **`status == "delivered"`（`:247`）** | **then** `delivered_at` 必为 time（`:253`）、**`submitted_at` 必为 time（`:256`）**、`verification.level` **必须 `"delivery_confirmed"`（`:267`）** |
| 5 | **`:283-312`** | **`status == "accepted"`（`:287`）** | **then** `submitted_at` 必为 time（`:293`）、`verification.level` **必须 `"platform_accepted"`（`:304`）** |
| 6 | `:313-334` | `receipt_kind=="part"` **且** `status ∈ {accepted, delivered}` | **then** `platform_receipt_ref` 必须是 reference |
| 7 | `:335-359` | `status == "pending"`（`:339`） | **then** `attempts` 必须 **=0**、`submitted_at` null、`verification` null、`platform_receipt_ref` null |
| 8 | `:360-389` | `status == "cancelled"`（`:364`） | **then** `reason_code` 必须是 token、`verification.level` **必须 `"not_submitted"`（`:381`）** |
| 9 | `:390-421` | `status == "uncertain"`（`:394`） / `status == "partial"`（`:410`） | **then** `reason_code` 必须是 token |

⭐ **`delivered` vs `accepted` 的精确差别（这是我们最需要的一条）：**

- **`delivered` 要求**（`:243-274`）：`delivered_at`（时间）+ `submitted_at`（时间，**必填**）+ `verification.level == "delivery_confirmed"`。
- **`accepted` 要求**（`:283-311`）：`submitted_at`（时间）+ `verification.level == "platform_accepted"`；**不要求** `delivered_at`
  （事实上第 4 条 **else 分支 `:275-281`** 规定：**只要 `status != "delivered"`，`delivered_at` 必须为 null**，`:277`）。
- 两者都**不允许** `attempts == 0`（第 3 条），都**不允许** `receipt_kind == "part"` 时缺少 `platform_receipt_ref`（第 6 条）。
- **`cancelled` 是唯一被强制要求 `verification.level == "not_submitted"` 的状态** → 即：
  只有能证明「未提交」才允许记 cancelled。

`verification.level` 枚举（`docs/contracts/execution/v1/schemas/execution-common.schema.json:255-263`，首值 `:257`）：
`platform_accepted`、`delivery_confirmed`、`not_submitted`、`submission_unknown`、`delivery_failed`。

`verification` 对象 required（`execution-common.schema.json:271-275`）：`level`、`evidence_ref`、`observed_at`；`additionalProperties:false`。

**文字侧同义重述**（`docs/NATIVE_PLUGIN_INTEGRATION_V0.md:278`，§3.7，节标题 `:264`）：

> 回执状态为 `pending`、`submitted`、`accepted`、`delivered`、`partial`、`uncertain`、`failed`、`cancelled`。**`accepted` 只表示平台受理，`delivered` 必须有送达时间和平台证据**；不能用 `OperationResult.succeeded` 替代投递回执。多 part 使用 `parts_ref` 聚合，已确认的 part 不重发。

`docs/NATIVE_PLUGIN_INTEGRATION_V0.md:308`：

> `DeliveryReceipt.status=accepted` 只代表平台受理，不能改写为 `delivered`。

v1 文档同义（`COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:98`）：

> 4. **生成不等于发生，提交不等于送达。** 模型生成、领域保存、平台接受、实际送达、播放、用户理解和用户喜欢分别留据。

`COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:309`：

> 每个 target 在同一时刻只有一个可提交 sender。切换 sender 前要冻结旧新派发、盘点非空 operation/part/unknown/通知、提高 route epoch 并保留原账。未知回执不能改走另一个 sender 补发。

### D.2 `effect_state` 枚举，以及**把它绑到 `operation_id` 的那条规则**

**枚举（5 值）** —— `docs/contracts/execution/v1/schemas/execution-common.schema.json:94-102`（逐字）：

```json
    "effect": {                       ← :94
      "enum": [
        "not_applicable",
        "not_applied",
        "applied",
        "partial",
        "unknown"
      ]
    },
```

`OperationResult.effect_state` 直接 `$ref` 它（`operation-result.schema.json:69-71`），且**是 required**（`:123`）。

**把 `effect_state` 绑到 `operation_id` 的那条规则** —— `docs/contracts/execution/v1/schemas/operation-result.schema.json:354-376`（逐字）：

```json
    {                                          ← :354
      "if": {                                  ← :355
        "properties": {
          "effect_state": {
            "const": "applied"                 ← :358
          }
        }
      },
      "then": {
        "properties": {
          "execution": {
            "properties": {
              "operation_id": {                ← :366
                "$ref": "urn:companion:schema:common:v1#/$defs/token"
              }
            }
          },
          "receipt_ref": {                     ← :371
            "$ref": "urn:companion:schema:execution-common:v1#/$defs/reference"
          }
        }
      }
    }
```

⭐ **即：只要 `effect_state == "applied"`，`execution.operation_id` 必须是非 null token，
且 `receipt_ref` 必须是 reference。** 没有任何 `effect_state` 取值可以绕过查账。

**`effect_state` 全部绑定规则（`operation-result.schema.json` 的 `allOf`，`:130-377`）：**

| 行 | 条件 | 允许/要求的 `effect_state` |
| --- | --- | --- |
| `:154-182` | `status == "succeeded"`（`:158`） | ∈ {`not_applicable`,`not_applied`,`applied`}（`:165-169`）；`retryable` 必须 **false**（`:171-173`）；`output_schema` 必填 token；`output` 必须是 object |
| `:183-214` | `status == "pending"`（`:187`） | ∈ {`not_applied`,`unknown`}（`:194-197`）；`execution.operation_id` 必须为 token（`:199-205`）；`receipt_ref` 必须 reference（`:206-208`）；`retryable` 必须 **false**（`:209-211`） |
| `:215-249` | `status == "partial"`（`:219`） | **`effect_state` 必须 = `partial`（`:225-227`）**；`execution.operation_id` 必须 token（`:228-234`）；`parts_ref`（`:235-237`）与 `receipt_ref`（`:238-240`）必须 reference；`degraded` 必须 **true**（`:241-243`）；`retryable` 必须 **false**（`:244-246`） |
| `:250-277` | `status == "uncertain"`（`:254`） | **`effect_state` 必须 = `unknown`（`:260-262`）**；`reason_code` 必须 token（`:263-265`）；`retryable` 必须 **false**（`:266-268`）；`output_schema`/`output` 必须 **null**（`:269-274`） |
| `:278-306` | `status ∈ {rejected, permission_denied, unavailable, timeout, failed, cancelled}`（`:282-289`） | `reason_code` 必须 token（`:295-297`）；`output_schema`/`output` 必须 **null**（`:298-303`） |
| `:307-329` | `status ∈ {timeout, cancelled, failed}`（`:311-315`） | `effect_state` ∈ {`not_applicable`,`not_applied`}（`:321-325`） |
| `:330-353` | `status ∈ {rejected, permission_denied, unavailable}`（`:334-338`） | `effect_state` ∈ {`not_applicable`,`not_applied`,`unknown`}（`:344-349`） |
| **`:354-376`** | **`effect_state == "applied"`（`:358`）** | **`execution.operation_id` 必须 token（`:366`）；`receipt_ref` 必须 reference（`:371`）** |

`OperationResult.status` 枚举（10 值，`operation-result.schema.json:33-46`）：
`succeeded`、`pending`、`partial`、`rejected`、`permission_denied`、`unavailable`、`timeout`、`uncertain`、`failed`、`cancelled`。

`execution` / `correlation` 必填（`execution-common.schema.json:84-92`）：
`task_id`、`attempt_id`、`part_id`、`operation_id`、`budget_ref`、`caused_by`
（**全部 required，但除 `attempt_id`/`budget_ref`/`caused_by` 外可为 null**）。

文字侧同义（`docs/NATIVE_PLUGIN_INTEGRATION_V0.md:308`）：

> 例如，缺少 Reality provider 的调用返回 `status=unavailable, reason_code=dependency_missing, retryable=false, effect_state=not_applied`；设备提交后无回执返回 `status=uncertain, reason_code=external_unknown, retryable=false, effect_state=unknown`，并保留原 `operation_id` 供查账。

### D.3 `ActionRequest` 需要什么（`required_features` 必须含的常量）

**Schema**：`docs/contracts/execution/v1/schemas/action-request.schema.json`（63 行）。
**标题（`:4`）**：`"companion.action.v1 negotiated public execution request"`。

**结构**：`allOf: [$ref common:v1#/$defs/request]`（`:5-9`），因此 **继承 `common.request` 的全部 required**。

`common.request` 的 required（`docs/contracts/v1/schemas/common.schema.json:208-266`）：
`schema_version`、`capability_id`、`capability_version`（`const "1.0"`）、`provider_id`、`provider_generation`、
`request_id`、`trace_id`、`deadline_at`、**`scope`**、`required_features`（`:238`）、`budget`
（`payload` 在 `properties` 中但不在 required 列表）。

`ActionRequest` **自己**的 required（`:54-61`）：
`binding_ref`、`required_features`、`execution`、`idempotency_key`、`payload_schema`、`preconditions`。
`unevaluatedProperties: false`（`:62`）。

⭐ **`required_features` 必须包含的常量（`:22-27` 逐字）**：

```json
    "required_features": {          ← :22
      "type": "array",
      "contains": {
        "const": "companion.execution@1"    ← :25
      }
    },
```

→ **常量 = `companion.execution@1`**（JSON Schema `contains`，即**数组中必须至少有一项等于该值**）。

其余字段（`:10-52`）：

| 字段 | 约束 |
| --- | --- |
| `schema_version` | `const "companion.action.v1"`（`:11-13`） |
| `capability_id` | `pattern "^[a-z][a-z0-9_-]*(?:\\.[a-z][a-z0-9_-]*)+$"`，`maxLength 256`（**要求至少一个 `.` 分段**）（`:14-18`） |
| `binding_ref` | token（`:19-21`） |
| `execution` | `execution-common:v1#/$defs/correlation`（`:28-30`） |
| `idempotency_key` | token **或 null**（**允许 null**，但 required）（`:31-40`） |
| `payload_schema` | token（`:41-43`） |
| `payload` | `type: object`（`:44-46`） |
| `preconditions` | array of `execution-common:v1#/$defs/precondition`（`:47-52`；元素对象 `{target, expected_revision}` 均 required，`execution-common.schema.json:229-251`） |

`required_features` 的元素格式由 `common.request` 约束（`common.schema.json:238-246`）：
`pattern "^[a-z][a-z0-9_.-]*@[1-9][0-9]*$"`、`uniqueItems: true`、`default: []`。
→ **`companion.execution@1` 符合该 pattern**（`companion.execution` + `@1`）。

**其他已知 profile 常量**：`companion.sharing@1`
（`docs/contracts/sharing/v1/README.md:25`：「请求封套 `required_features` 必须含 `companion.sharing@1`」）。

### D.4 稳定 `reason_code` 词表

来源：`docs/NATIVE_PLUGIN_INTEGRATION_V0.md:310`（§3.9 失败回执，节标题 `:304`）。
该节先声明（`:306`）：

> 本文不新增 `companion.failure.v1`。失败复用执行包的 `OperationResult`，由 `status`、`reason_code`、`retryable`、`degraded`、`effect_state` 和 `execution` 中的 task/attempt/operation/part 引用表达；消息失败另附 `DeliveryReceipt`。领域错误详情只能通过获授权的 `output`/`details_ref` 引用返回，不把堆栈、凭据或私有正文复制到 Kernel。

**18 个稳定原因码（`:310` 逐字，顺序照抄）**：

1. `scope_required`
2. `scope_unresolved`
3. `permission_denied`
4. `confirmation_required`
5. `capability_unavailable`
6. `dependency_missing`
7. `unsupported_feature`
8. `lifecycle_not_ready`
9. `invalid_input`
10. `revision_conflict`
11. `deadline_exceeded`
12. `cancelled`
13. `rate_limited`
14. `provider_timeout`
15. `external_rejected`
16. `external_unknown`
17. `storage_error`
18. `privacy_violation`

**同一行的重试规则（逐字）**：

> `retryable=true` 只适用于 owner 明确证明未提交、或远端提供相同幂等键的安全重试；未知外部结果固定为 `uncertain` 且 `retryable=false`，等待查账。

**取消的三层结果**（`docs/NATIVE_PLUGIN_INTEGRATION_V0.md:367-371`，§4.4 表，表头 `:367`，首行 `:369`）：

| 情况 | 回执 | 后续 |
| --- | --- | --- |
| 尚未提交且已停止 | `cancelled / not_applied` | 不再调度，可释放未使用预算 |
| 本地已提交或部分完成 | `partial` 或 `succeeded / applied` | 保留已发生效果，不能回退成 cancelled |
| 已派发但外部状态未知 | `uncertain / unknown` | 调用原 owner 或平台查询，不换 provider 重发 |

`docs/NATIVE_PLUGIN_INTEGRATION_V0.md:373`：

> 超时只代表 deadline 到达。只有能够证明未执行时才返回 `timeout + not_applied`；提交后无可靠回执必须返回 `uncertain`。安全重试仅限只读请求、提交前失败、明确 `retryable=true`，或远端承诺相同目标/参数摘要/幂等键的去重。重试沿用逻辑 operation 和 part_id，不能用新的 idempotency key 把未知动作变成第二次动作。

v1 文档的同义规则（`COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:465`）：

> 取消分为准入前拒绝、外部提交前停止、提交后未知三种。只有前两种在 owner 已证明未提交时可记为 `cancelled`；提交后未得到可靠结果必须保留 `uncertain` 并沿原操作查账。重启、热重载、换 worker 或 route fence 只阻止新的无资格派发，不能证明先前外部请求没有发生。

### D.5 六类 ID 的语义分离（v1 §12.1，`COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:456-463`）

```text
operation_id    领域 owner 签发的逻辑动作，跨重试保留        ← :457
idempotency_key 稳定去重键，不包含 request ID、generation 或随机值
task_id         可恢复的调度单元
attempt_id      单次运行尝试，重试或恢复时改变
part_id         独立可提交的输出/动作部分
trace_id        仅用于诊断关联，不是权限或去重凭据          ← :462
```

---

## E. 能力 / 生命周期契约

### E.1 生命周期状态机与停机阶段

**`docs/NATIVE_PLUGIN_INTEGRATION_V0.md:338-339`（逐字）**：

```text
state: discovered -> validated -> bound -> ready / degraded -> stopped / failed    ← :338
shutdown phase: quiescing -> draining -> closing                                    ← :339
```

**`NATIVE_PLUGIN_INTEGRATION_V0.md:342`**：

> `quiescing/draining/closing` 是停机阶段，**不增加生命周期主状态**。停机开始先置 `admission=false`；资源和 Writer 真正收束后才进入 stopped，失败则保持 failed 与残留资源诊断。

**逐阶段表（表头 `NATIVE_PLUGIN_INTEGRATION_V0.md:344`，数据行 `:346-353`，逐字）：**

| 阶段 | Kernel 动作 | 插件约束 |
| --- | --- | --- |
| `discovered` `:346` | 读取 manifest 摘要并按授权分页 | 不执行 probe、模型调用或扫描 |
| `validated` `:347` | 校验 Schema、handler、依赖和有限 limits | 只准备本代资源 |
| `bound` `:348` | 选择精确 provider/version，签发 BindingHandle | 不因 bind 自动创建领域任务 |
| `ready` `:349` | 开放 invoke/subscribe | 每次调用仍检查 generation 和 ACL |
| `degraded` `:350` | 关闭受影响 feature，保留可用 feature | 返回明确 reason_code，不伪造成功 |
| `phase=quiescing` `:351` | 关闭新准入，撤销旧 generation 写资格 | 停止创建子任务 |
| `phase=draining/closing` `:352` | 等待取消、提交 fence、关闭本代资源并保留未决对账记录 | 不关闭其他 owner 使用的共享连接 |
| `stopped/failed` `:353` | 保留最小诊断和账本引用 | 新调用返回 `lifecycle_not_ready` |

**`NATIVE_PLUGIN_INTEGRATION_V0.md:355`（卸载顺序 + 硬约束，逐字）**：

> `setup` 不得启动无主永久任务；`start` 才能在 TaskSupervisor 监督下建立连接和循环。卸载顺序必须是撤权、关闭准入、取消/等待任务、执行 owner fence、释放资源、删除注册。若无法证明旧 Writer 已停止或已被 fence，相关能力保持 `failed + admission=false`，其他能力继续运行。

**补充（`docs/COMPANION_EXTENSION_LIFECYCLE_V0.md:54`）**：

> 取消安装或初始化时，discovered/validated/bound 也可进入 stopped。卸载过程用 `phase=quiescing|draining|closing` 描述，不新增一套生命周期主枚举；各能力准入在进入 quiescing 时已关闭。若不能证明旧任务已结束或写入已受隔离，则保留 failed、admission=false 和残留资源诊断，不提前标记 stopped。

**`ready` 的完整准入条件（`docs/COMPANION_EXTENSION_LIFECYCLE_V0.md:56`，逐字）**：

> ready 必须同时满足：manifest/schema 通过校验、必需依赖已协商、handler 已挂接、宿主身份有效、setup/start 完成、领域明确报告可服务。**提供方的就绪报告经控制面验证，不能自行改变 ACL。** 扩展 degraded 时，未受影响的能力可以保持 ready；失效能力返回 unavailable。控制面展示逐能力原因、版本、generation、有效预算和最近一次检查时间。

**`docs/COMPANION_EXTENSION_LIFECYCLE_V0.md:60`**：

> 宿主依据实际加载模块及其受信安装记录创建注册上下文；兼容旧插件时映射原宿主身份。**manifest.provider 必须与该身份关联，不能仅相信 JSON 中的名字。**

**停机时的取消语义（`docs/COMPANION_EXTENSION_LIFECYCLE_V0.md:236`，§卸载第 3 步）**：

> 3. 在 shutdown budget 内等待任务和线程操作收束。逐项区分 not_committed、committed、uncertain，**禁止将所有任务统一记作 cancelled**。

**五项独立能力状态（v1 §11.1，表头 `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:416`，数据行 `:418-422`）**：

| 状态 | 含义（v1 原文） |
| --- | --- |
| `bundled`/`installed` `:418` | 本次发行或本地环境中具备能力代码和依赖。 |
| `enabled` `:419` | 用户/角色范围允许该能力参与装配。 |
| `authorized` `:420` | 对所需来源、设备、账号和动作已有有效授权。 |
| `ready` `:421` | 当前版本、依赖、宿主连接和健康检查满足运行条件。 |
| `furniture projected` `:422` | 小屋和管理页能显示该能力及其当前状态。 |

`COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:424`：

> …随包安装不等于角色启用，**能力 ready 也不等于拥有读取或发送权限**。丢弃后仍按 owner 的治理规则保留资料、共享依赖和墓碑；重装使用稳定 owner/dataset 识别已有数据，不能复制第二份领域正本。

### E.2 `CapabilityDescriptor` 需要什么

**Schema**：`docs/contracts/control/v1/schemas/capability-descriptor.schema.json`（35 行）。
**标题（`:4`）**：`"Versioned capability descriptor"`。

**15 个 required（`:33`）**：
`capability_id`, `capability_version`, `provider_id`, `provider_generation`, `descriptor_revision`,
`owner_ref`, `status`, `admission`, `side_effect`, `features`, `required_permissions`,
`input_schema`, `output_schema`, `limits`, `expires_at`。`additionalProperties: false`（`:34`）。

| 字段 | 约束（行） |
| --- | --- |
| `capability_id` | `pattern "^[a-z][a-z0-9_.-]+$"`（`:7`） |
| `capability_version` | **`const "1.0"`**（`:8`） |
| `provider_id` | string, minLength 1（`:9`） |
| `provider_generation` | string, minLength 1（`:10`） |
| `descriptor_revision` | integer, min 0（`:11`） |
| `owner_ref` | string, minLength 1（`:12`） |
| `status` | **enum `["discovered","validated","bound","ready","degraded","stopped","failed"]`**（`:13`） |
| `admission` | boolean（`:14`） |
| `side_effect` | **enum `["none","local","remote"]`**（`:15`） |
| `features` | array\<string\>, `uniqueItems`（`:16`） |
| `required_permissions` | array\<string\>, `uniqueItems`（`:17`） |
| `input_schema` | `["string","null"]`, `pattern "^urn:[^\\s]+$"`（`:18`） |
| `output_schema` | `["string","null"]`, `pattern "^urn:[^\\s]+$"`（`:19`） |
| `limits` | object（`:20-30`）；properties `max_input_bytes`/`max_output_bytes`/`max_concurrency`/`max_duration_ms`（均 integer ≥1）；**required 前三项**（`:28`）；`additionalProperties:false`（`:29`） |
| `expires_at` | `["string","null"]`, `format date-time`（`:31`） |

**`NATIVE_PLUGIN_INTEGRATION_V0.md:127-146` §3.2 的运行时投影示例**（节标题 `:127`，JSON 围栏 `:129-147`，`expires_at` 在 `:145`）：

```json
{
  "capability_id": "memory.query",                      ← :131
  "capability_version": "1.0",
  "provider_id": "astrbot_plugin_memory_companion",
  "provider_generation": "provider-generation-4",
  "descriptor_revision": 3,
  "owner_ref": "memory-owner-1",
  "status": "ready",
  "admission": true,
  "side_effect": "none",
  "features": ["evidence", "operation.lookup"],
  "required_permissions": ["memory.read"],
  "input_schema": "urn:companion:memory-query:v1",
  "output_schema": "urn:companion:answer-evidence:v1",
  "limits": {"max_input_bytes": 65536, "max_output_bytes": 131072, "max_concurrency": 4},
  "expires_at": null                                    ← :145
}
```

> 📌 **注**：`docs/CAPABILITY_PACKAGE_AND_FURNITURE_LIFECYCLE_V0.md:35`（draft）明确
> 「以下七类记录是待发布配套格式的责任划分，**不要求成为七个文件或数据库，也不是新 Schema 已发布**。
> 现有封闭 `ExtensionManifest` 和 `CapabilityDescriptor` **保持原形状**」——
> 即：**`PackageManifest` 与 `ExtensionManifest` 是不同对象**，前者无 Schema，后者有
> （`contracts/control/v1/schemas/extension-manifest.schema.json`）。

### E.3 ⭐ 描述符字段**是否可以由插件自报** —— 明确规则

**`docs/NATIVE_PLUGIN_INTEGRATION_V0.md:149`（逐字，权威）**：

> 上例采用控制包的运行时投影字段；静态声明中的 kind、作用域、依赖、确认、完成方式和幂等策略仍按注入协议的 `CapabilityDescriptor` 维护，并由注册 adapter 映射到控制投影。**`provider_id`、generation、owner 和 status 必须由宿主/Kernel 关联，不能由 JSON 自报。** 描述符只声明可接受的作用域和权限，不包含具体用户、凭据或数据库路径；`features`、Schema 和 limits 参与 bind 协商。多个 provider 通过显式绑定选择，不能按最后注册者覆盖。

**同义重复（`docs/NATIVE_PLUGIN_INTEGRATION_V0.md:96` 表）**：

> `| `CapabilityDescriptor` | [静态声明](./COMPANION_INJECTION_PROTOCOL.md#41-capabilitydescriptor-字段设计)与[控制包](./contracts/control/v1/README.md)分别维护 | **静态声明不自报可信 generation**；控制包现有描述符是运行时投影，两者通过版本化 adapter 映射 |`

**权限只是声明，grant 由 Kernel 签发（`NATIVE_PLUGIN_INTEGRATION_V0.md:359`）**：

> 授权由 Kernel 根据可信 caller、owner、完整 `RuntimeScope`、能力、purpose、参数摘要、受众、有效期和 policy revision 计算。`CapabilityDescriptor.required_permissions`（与 control 包 Schema 字段名一致）**只是声明；真正的 grant 由 Kernel 签发。** 涉及设备动作、第三方服务、付费调用、现实承诺、隐私数据或群体可见性的操作还要检查 confirmation policy。

**同义（`docs/COMPANION_EXTENSION_LIFECYCLE_V0.md:56`）**：「提供方的就绪报告经控制面验证，不能自行改变 ACL。」
**同义（`docs/COMPANION_EXTENSION_LIFECYCLE_V0.md:60`）**：「manifest.provider 必须与该身份关联，不能仅相信 JSON 中的名字。」
**同义（`docs/COMPANION_EXTENSION_LIFECYCLE_V0.md:117`）**：「…新代必须由宿主重新鉴别并签发，**提供方不能要求复用某个 generation**。」

🔴 **对我们的直接含义**：插件的 `metadata.yaml` / manifest **不能**决定
`provider_id` / `provider_generation` / `owner_ref` / `status`——**必须由我们的宿主签发并关联**。
这与我们当前「照实读插件元数据」的做法**方向相反**。

**四类允许的外部注入（`NATIVE_PLUGIN_INTEGRATION_V0.md:318-321`，表头 `:316`，逐字表）**：

| 注入类型 | 允许生产的对象 | 典型生产方 | 不能做的事 |
| --- | --- | --- | --- |
| `EventSource` `:318` | `Event`、`Observation`、`Signal` | Reality、Screen、Live、World、平台 Adapter | 直接发送消息、写 Memory、伪造用户确认 |
| `ContextProvider` `:319` | `ContextContribution`、`EvidenceRef` | Memory、Screen、Together、Content | 提交任意 system prompt、绕过 ACL |
| `CandidateProvider` `:320` | `ProactiveCandidate`、`IntentPlan` | Calendar、World、Memory、Content、Reality | 自行抢占、扣接触额度、调用 Delivery |
| `ActionProvider` `:321` | `ActionRequest`、`OperationResult` | Reality、Game、Live、Image | 自行决定授权、返回猜测成功、读取别的 owner 数据 |

**推荐的宿主侧接口形状（`NATIVE_PLUGIN_INTEGRATION_V0.md:323-333`，引言 `:323`，代码块 `:326-330`，收口 `:333`，逐字）**：

> 推荐的宿主侧接口是受监督的 typed gateway，**而不是暴露注册表或插件实例**：

```python
registration = kernel.register(manifest)                            ← :326
binding = await kernel.bind(requirement, scope_selector, purpose)
result = await kernel.invoke(binding, request_envelope)
await kernel.cancel(task_ref, reason="user_requested")
lookup = await kernel.operation_lookup(owner_ref, locator)
```

> 本地 handler 可以持有 `TaskContext`，但不能把它序列化、交给后台无限持有或放入 DTO。远程适配器只接收可信身份映射后的调用上下文。注入请求之外的贡献必须带完整 `RuntimeScope`；缺少作用域或绑定版本过期时返回失败，不从缓存猜测身份。

**`ExtensionManifest`（注册时提交，`contracts/control/v1/schemas/extension-manifest.schema.json`）**：

required（`:27`）：`extension_id`, `extension_version`（`pattern "^[1-9][0-9]*\\.[0-9]+\\.[0-9]+$"`）, `provider_id`,
`provider_generation`, `manifest_revision`, **`deployment_modes`**（`:12`，enum 四项）, `capabilities`（array，minItems 1）,
`dependencies`, `resource_limits`（required 四项：`max_metadata_bytes`/`max_bindings`/`max_control_inflight`/`max_persistent_receipts`）。
`additionalProperties: false`。

---

## F. 框架当前真实进度

### F.1 S0–S7 的含义与**当前状态**（逐个引用原文）

| 阶段 | 定义（roadmap 节标题） | 当前状态（逐字） | 行 |
| --- | --- | --- | --- |
| **S0** 现状盘点与设计原则 | `### S0：现状盘点与设计原则` | **`D`，已完成。旧代码仍是能力和行为样本，不是新框架边界的最终依据。** | `docs/FRAMEWORK_ROADMAP_AND_STATUS_V1.md:137` / `:143` |
| **S1** 公共语言、身份与所有权 | `### S1：公共语言、身份与所有权` | **`C+R`。公共字段已收口，格式和隔离检查通过；契约包仍为 `review`，真实授权和 SDK 发行还未完成。** | `:147` / `:153` |
| **S2** 只读运行壳与真实宿主接入 | `### S2：只读运行壳与真实宿主接入` | **`R+H+A(只读切片)`。隔离 shell 10 项和 live-runtime 单元测试 4 项通过；`main.py` 已在真实 `on_llm_request` 中调用 `runtime_scope_for_event()`、`AstrBotReadonlyAdapter`、owner resolver 和现有 Memory summary bridge，并把成功摘要作为柔性参考上下文加入当前请求。** | `:157` / `:163` |
| **S3** 首条持久化跨域切片 | `### S3：首条持久化跨域切片` | **反思 Writer 有局部实际提交；纠正、活动 checkpoint、引用重验和工具注册有分批 R/H 证据，纠正/查账/活动恢复另有真实模型隔离验证。这里只汇总已覆盖范围；复杂召回单列于下方 S3-MQ，完整 S3 仍未验收。** | `:205` / `:215` |
| **S3-MQ** 复杂记忆召回 | `### S3-MQ：复杂记忆召回的收敛路线` | **设计已采纳，MQ-01/02 已有写入回执、引用读取、独立原文查询、来源/事件本地成对 Schema、授权分页与临时事件计算的局部证据；公共能力协商、通用依赖计划、完整范围覆盖与 MRQ 语义运行仍未完成。** | `:235` / `:239` |
| **S4** 角色运行时、连续生活与时间边界 | `### S4：角色运行时、连续生活与时间边界` | **完整阶段尚未验收（`D / NR`）；工作便笺首段已有局部 `C/R/H` 并按用户授权开启。`NR` 只描述完整阶段，不能解释成首段没有运行。** | `:267` / `:273` |
| **S5** 回复、主动行为、投递与反馈 | `### S5：回复、主动行为、投递与反馈` | **完整阶段为 `D / NR`；本地普通/主动、原生/QQ 嵌套流式回执及分段主动已有局部证据，完整平台旁支、统一流式接口与反馈收益未验收。** | `:285` / `:302` |
| **S6** 原生能力分波次接入 | `### S6：原生能力分波次接入` | **前两波当前设计范围的领域责任、联合交接和验收映射已齐备（`D`）；…公共领域配套格式、可执行故障夹具和运行证据仍待交付。** | `:316` / `:326` |
| **S7** 跨平台、迁移与产品化 | `### S7：跨平台、迁移与产品化` | **全阶段 `D+BL(S3-S6)`。…精确配套格式、完整发行、通用安装器、旧系统交接和非空恢复/激活仍待交付；24 条 PKG、28 条 REL 分别待验。** | `:336` / `:342` |

**整体位置（两处，逐字）**：

- `docs/FRAMEWORK_ROADMAP_AND_STATUS_V1.md:5`：
  > `> 更新：2026-09-19。[独立进程恢复](./evaluations/RV3_INDEPENDENT_PROCESS_RECOVERY_20260919.md)已为 S6-RV3-INTENT-R10 / R（隔离），17 场景、34 次子进程执行通过。下一执行机会总期限与领取续期。**整体仍 S2→S3，生产未切换。** 方向见[总纲](./FRAMEWORK_DESIGN.md)，逐步选择见[角色主稿](./ROLE_RUNTIME_KERNEL_DESIGN.md#s4-step-confirmation)。`
- `docs/FRAMEWORK_ROADMAP_AND_STATUS_V1.md:15`：
  > `**整体仍在 S2→S3，S4 本地生活与主动链已在原授权范围启用。** S3 未收口不抹掉已经发生的 S4 试用；首段通过也不解除 S3、完整 S4 或后续阶段的验收条件。`

**证据等级表（`docs/FRAMEWORK_ROADMAP_AND_STATUS_V1.md:100-115`）**，前置声明 `:100`：

> 阶段状态使用证据等级，而不是把“有多少 Schema”或“通过多少条脚本”直接当成完成度。**等级可以叠加显示，但不能相加为百分比。**

| 标记 | 含义 | 行 |
| --- | --- | --- |
| `D` | 设计完成 | `:104` |
| `C` | 契约完成 | `:105` |
| `R` | 隔离回放完成 | `:106` |
| `H` | 宿主接入完成 | `:107` |
| `S` | 影子运行完成 | `:108` |
| `A` | 实际运行完成 | `:109` |
| `F` | 正式验收完成 | `:110` |
| `NR` | 尚未运行 | `:111` |
| `BL` | 依赖阻塞 | `:112` |
| `DG` | 局部降级 | `:113` |

`:115`：

> 每个阶段和领域都要同时记录：当前证据、已经证明的内容、尚未证明的内容、阻塞项、下一条最小可验证交付和退出条件。

### F.2 v1 §14.2 的 G0–G6 门槛表（**逐字复现**，`COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:530-536`，表头 `:528`）

| 门槛 | 可以进入的工作 | 必须证明的事 |
| --- | --- | --- |
| G0 新根基线 `:530` | 独立源码根、临时库、fixture 与测试 runner | 允许的旧代码参照路径和禁入路径固定；无现役运行数据或生产导入。 |
| G1 基础契约 `:531` | foundation、identity_policy、storage、版本化 contracts | 引用、scope、授权、版本、失败语义、跨 scope 和撤权/换绑反例可复现。 |
| G2 隔离纵向切片 `:532` | 一个无副作用的完整 owner 闭环 | 幂等、outbox/inbox、ACK 丢失、取消、独立进程恢复和调用计数可验证。 |
| G3 单目标影子盘点 `:533` | 明确一个被动 target 的 shadow comparison | 全部旧 sender branch、operation/part/unknown、收件方和 route owner 已盘点，无外部副作用。 |
| G4 单目标真实接线 `:534` | 一个经过授权的真实 target | 可信身份/受众、真实 receipt、收件方事务、route CAS、关闭/重启和平台证据。 |
| G5 按 owner 迁移 `:535` | 一个数据 owner 或发送 target 的旧新切换 | 最终快照、增量、旧 writer/sender fence、单一新 owner、治理和可回退边界。 |
| G6 发行与产品化 `:536` | 能力包、家具、跨宿主与 release | PKG/REL、资源/备份、授权、迁移、真实环境和发行范围的完整证据。 |

> ⚠️ **再次强调**：**G0–G6 只存在于 v1 文档**。设计包里既没有这张表，也没有
> `evaluations/FRAMEWORK_ZERO_BUILD_READINESS_20260920.md`（v1 `:7` 引用的准入条件文件）。
> 若我们要按 G0–G2 排期，**我们手上没有该门槛的验收判据文本**——需要向作者索取。

### F.3 v1 文档自己怎么说「准备度」与「尚未接管」

**开场块（`COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:3-5`，逐字）**：

> `:3` `> 整理日期：2026-09-20。`
> `:4` `>`
> `:5` `> 本稿把新框架已经确定的产品目标、运行边界、领域责任、管理体验、实施顺序和验收语义收束为一条可连续阅读的主线。它不替代领域契约、Schema、fixture、日期化运行证据或路线看板：精确 wire 以对应 `contracts/` 包为准，当前完成度以[路线看板](./FRAMEWORK_ROADMAP_AND_STATUS_V1.md)为准，生产证据以[验证目录](./evaluations/README.md)为准。`

⭐ **`COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:7`（最关键的一句，逐字）**：

> **当前结论：设计已经允许在独立源码根开始 G0-G2 的隔离构建；它尚不接管现役 sender、生产 writer、真实 route，也不表示 S0-S7 任一阶段已经升级。准入条件见[从零构建准入与收尾审计](./evaluations/FRAMEWORK_ZERO_BUILD_READINESS_20260920.md)。**

`:590`（维护原则，逐字）：

> 本稿的维护原则是：新增跨领域决定先写入本稿和对应 owner 主稿；精确字段写入版本化契约；运行结果写入日期化证据与状态看板。**任何实际生产接管、路由切换、writer 迁移或阶段升级都必须由独立验收证明，而不是由设计稿本身宣布。**

### F.4 现有隔离 bridge / adapter **是否可以复用** —— 三条明确表态

| # | 位置 | 原文（逐字） | 判定 |
| --- | --- | --- | --- |
| 1 | `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:542` | 「此 I01 只证明隔离结构。模型、工具、平台、TTS、真实宿主和生产 owner 调用必须为零；**现有隔离 bridge 只能作为语义参照与 fixture 来源，不能直接移植为新根的兼容包装器。**」 | **不可复用为兼容包装器**；只能作语义参照 + fixture 来源 |
| 2 | `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:494` | 「**旧代码只通过窄的 `adapters/` 端口被参照或读取。** 迁移按 owner 和最小一致性交接组进行：…」 | 仅可**被参照或读取** |
| 3 | `docs/S7_RELEASE_MIGRATION_AND_ROLLBACK_V0.md:81` | 「旧插件与新应用同机时，逐一登记旧定时器、消息 hook、后台任务、fallback、writer 和 sender。**兼容适配器复用原服务；新实现获得业务准入之前，不能再起一套角色、日记、主动决策、设备监听或发送者。**」 | **兼容适配器可以复用原服务**——但与 #1 张力明显（见下） |

#### ⚠️ 第 1 条与第 3 条的张力（**需要向作者澄清**）

- `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:542` 说：隔离 bridge **不能**直接移植为「为**新根**服务的**兼容包装器**」。
- `docs/S7_RELEASE_MIGRATION_AND_ROLLBACK_V0.md:81` 说：「**兼容适配器复用原服务**」。

**两者可以共存**（读法：S7 说的是**过渡期**让**旧插件继续跑、由兼容适配器转发到旧服务**，
即「新应用不要把旧服务重新实现一遍」；v1 §14.3 说的是**不要把这些 bridge 搬进新根当基础**），
**但两份文档都没有把这两件事分开说**。这是本审计中第二个需要作者裁决的点。

### F.5 阶段与门槛的**编号空间冲突**（易踩）

| 编号 | 含义 | 出处 |
| --- | --- | --- |
| `S0`–`S7` | 全局路线八阶段 | `docs/FRAMEWORK_ROADMAP_AND_STATUS_V1.md:120-345` |
| `G0`–`G6` | 从零构建七道门槛 | `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:530-536` |
| `S5-STR-01/02/03` | S5 流式交付三个工作项 | `docs/FRAMEWORK_ROADMAP_AND_STATUS_V1.md:308-310` |
| **`S5-STR-I01`** | v1 §14.3 的**首条隔离闭环** | `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:538-542` |

🔴 **`S5-STR-01` ≠ `S5-STR-I01`**，仅差一个字母 `I`，**是完全不同的两个东西**
（`docs/FRAMEWORK_ROADMAP_AND_STATUS_V1.md:308-310` 的表里也没有 `I` 系列）。
我们在任何文档/代码注释里提到它时**必须写全并加限定词**，否则一定会串台。

---

## G. 汇总：对本项目（情绪app）最要紧的 12 条

| # | 结论 | 依据 |
| --- | --- | --- |
| 1 | **设计已完成、实现极浅。** 整体 `S2→S3`，S4/S5 为 `D / NR`，S6 为 `D`（运行证据待交付），S7 为 `D+BL(S3-S6)`，**生产未切换**。 | `FRAMEWORK_ROADMAP_AND_STATUS_V1.md:5,15,273,302,326,342` |
| 2 | **G0–G2 被作者明确放行**（独立源码根 + 临时库 + 隔离构建），但**准入判据文件不存在于磁盘**。 | `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:7` vs `find` 零命中 |
| 3 | **不得整体复用 `astrbot_plugin_private_companion` 当 Kernel**；发送/任务/单键作用域要逐处拆。 | `FRAMEWORK_ROADMAP_AND_STATUS_V1.md:354` |
| 4 | **不得直接包装 `event.send` / `send_streaming` / 多平台 sender。** | `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:489` |
| 5 | **现有隔离 bridge 不能当新根的兼容包装器**，只能当语义参照 + fixture 来源。 | `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:542` |
| 6 | **`host_kind` 永远不能填 `plugin_hosted`/`standalone_service`/`embedded_app`/`split_services`。** | `contracts/v1/schemas/runtime-scope.schema.json:209-225` |
| 7 | **`RuntimeScope` 是 `additionalProperties:false` 的封闭对象，14 项必填，且 `deployment_mode` / `sharing_*` 都不得塞进去。** | `runtime-scope.schema.json:121-137`；`NATIVE_PLUGIN_INTEGRATION_V0.md:125` |
| 8 | ⭐ **读合并顺序文档自相矛盾（3:3）**：`session→user→global`（含 v1 §4.2）vs `global→user→session`（含 INJECTION 的语义理由）。**需作者裁决。** | `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:181` / `COMPANION_CONTRACTS_AND_VERTICAL_SLICE.md:89` / `NATIVE_PLUGIN_INTEGRATION_V0.md:77` ⟷ `NATIVE_PLUGIN_INTEGRATION_V0.md:414` / `COMPANION_INJECTION_PROTOCOL.md:229` / `ROLEPLAY_PROACTIVE_REBUILD.md:232` |
| 9 | **`accepted` ≠ `delivered`，且 Schema 层面强制**：`delivered` 需 `delivered_at` + `submitted_at` + `verification.level=delivery_confirmed`；`accepted` 需 `submitted_at` + `level=platform_accepted`；只要不是 `delivered`，`delivered_at` **必须为 null**。 | `contracts/execution/v1/schemas/delivery-receipt.schema.json:243-312` |
| 10 | **`effect_state="applied"` ⇒ `execution.operation_id` 必填 + `receipt_ref` 必填**（未知结果只能查原操作，不能换 ID 重做）。 | `contracts/execution/v1/schemas/operation-result.schema.json:354-376` |
| 11 | **插件不得自报 `provider_id`/`generation`/`owner`/`status`**；`required_permissions` 只是声明，grant 由 Kernel 签发。 | `NATIVE_PLUGIN_INTEGRATION_V0.md:149,359`；`COMPANION_EXTENSION_LIFECYCLE_V0.md:56,60,117` |
| 12 | **`ActionRequest.required_features` 必须含常量 `companion.execution@1`。** | `contracts/execution/v1/schemas/action-request.schema.json:22-27` |

---

## H. 需要向作者提问的 6 个问题（按价值排序）

1. **读合并顺序到底是哪个？** `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:181` 写 `session -> user -> global`，
   但 `COMPANION_INJECTION_PROTOCOL.md:229` 的语义理由（「先取 global 安全投影…最后取 session 近期场景」）
   指向 `global -> user -> session`，且 `NATIVE_PLUGIN_INTEGRATION_V0.md:414` 明文写 `global -> user -> session`。
2. **`FRAMEWORK_ZERO_BUILD_READINESS_20260920.md` 在哪？**（v1 `:7` 引用的 G0–G2 准入条件，磁盘上不存在。）
3. **`ModelAdapter` / `Store` 的输入输出是什么？** 十一个端口里有 5 个只在名单行（`PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:45`）出现过。
4. **`IdentityBinding` 用哪套字段？** `PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:65-79` 的 JSON
   与 `ROLE_RUNTIME_KERNEL_DESIGN.md:432-446` 的 text block **字段名逐项不同**，没有 Schema。
5. **`S7_RELEASE_MIGRATION_AND_ROLLBACK_V0.md:81`「兼容适配器复用原服务」与
   `COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md:542`「不能直接移植为兼容包装器」如何并存？**
6. **我们（独立 Python 应用、进程内加载 AstrBot 插件）应声明 `standalone_service` 还是 `embedded_app`？**
   `PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:145` 的场景文字（「自有 Web 应用」）两种都说得通。

---

*证据口径：所有引用均为逐字摘录，`file:line` 为脚本实测行号（213 项引文子串断言全部回验通过）。
`COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md` 位于
`/root/.dsh/attachments/v1/files/8a/8a3a82dadfdb7e5a51bdac518ef0fcd818be54f46ee409afa3cbf6b2866baa0a/`；
未加前缀的 `docs/...` 指
`/root/Ai-works/爱语/refs/companion_framework_draft/astrbot_plugin_private_companion/docs/`。*
