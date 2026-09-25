# 兼容性审计：陪伴新框架 v1 终稿 × 情绪app（已做 / 将做）

> 审计日期：2026-09-20。审计人：良芙。
> 被审对象：`COMPLETE_NEW_FRAMEWORK_DESIGN_V1.md`（作者 2026-09-20 终稿，590 行，sha256 `8a3a82da`）。
> 对照物：`情绪app` 现役代码（`emotion_app/` + `host/astrbot_host/`）+ `docs/NEXT_STEPS.md` 规划。
>
> **证据口径**：所有引用逐字摘录并带 `file:line`。凡"缺"的，写"零命中"而不是"应该有"。
> 两份支撑材料（本报告的下挂证据，不重复粘贴）：
> * `docs/FRAMEWORK_V1_REQUIREMENTS_EXTRACT.md` —— 新框架对宿主的**要求面**与**禁令面**全文提取（1,175 行）
> * `docs/FRAMEWORK_V1_OUR_INVENTORY.md` —— 我们**已实现面**的逐项清单（764 行）
>
> ⚠️ 本报告**只判断、不改代码**。所有结论都可以由上面两份材料复核。

---

## 0. 一句话结论

**不冲突，但现在的方向要分叉。**

新框架 v1 与我们**没有一处硬性接口冲突** —— 它要求的东西我们几乎全缺，所以谈不上"冲突"，
只谈得上"缺"。真正需要立刻处理的是**三件事**：

| # | 事项 | 严重度 | 一句话 |
|---|---|---|---|
| **1** | 我们花力气做的「AstrBot 兼容插件宿主」，在新框架里**是过渡件、不是新根** | 🔴 高 | 作者明文：**现有隔离 bridge 不能直接移植为新根的兼容包装器**（仅可作语义参照 + fixture） |
| **2** | `NEXT_STEPS.md` 的 P0（事件派发）**不会白干**，但**不能当新框架的地基** | 🟡 中 | 它服务的是"让老插件在过渡期能跑"，与新根 G0-G2 是**两条并行的路**，不是一条 |
| **3** | 真要接新框架，**代价集中在「身份/scope」与「投递/回执」两处**，且两处**都必须新写** | 🟡 中 | `RuntimeScope` 14 项必填，我们**只能填出 2 项**；投递回执我们**一张都没有** |

**一句话给情绪**：**新框架不是"要不要换"的问题，是"现在还换不了"的问题** ——
作者自己写着整体仍在 `S2→S3`、**生产未切换**、S4-S7 全是 `D / NR`。
我们继续做现在的过渡件是**对的**，但**别把过渡件当成新根的地基**（见 §3）。

---

## 1. 先厘清一件事：我们和新框架是什么关系**

`docs/PLATFORM_POSITIONING.md` 里我们给自己定的三层是：

```
L1 前端（爱语形态） / L2 后端（AstrBot 型插件宿主平台）/ L3 插件（陪伴及其同级）
```

新框架 §3.1 定义的分层是：

```
Host[宿主与应用装配 AstrBot、移动端、独立客户端]
  → Adapter[入口与平台适配]
    → Kernel[Foundation/Kernel]
      ↔ Role ↔ Domains ↔ Storage ↔ Providers
```

**两张图不矛盾，但我们的 L2 在新框架里被劈成了两半**：

| 我们的 L2 提供的 | 新框架里它属于 | 判定 |
|---|---|---|
| 插件加载、handler 注册、`/api/plugins/extensions` 挂载、插件页 | **Host + 旧系统适配**（§13.1 的"窄 `adapters/`"） | 过渡件。作者要求它**存在**，但**不能进新根** |
| 聊天链路、会话、人设、上下文、摘要、生图 | 新框架里是 **Role / Memory / Content / Image 四个领域 owner** | ⚠️ 与我们重复。见 §2.2 |
| 供应商两层存储（`provider_sources`→`providers`） | 新框架里是 **Provider + CapabilityDescriptor** | 形状对，字段不对。见 §2.5 |

### 1.1 我们该声明哪种部署模式？（作者留了个口子，对我们有利）

`PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:141-146` 定义了四种 shell：

| 模式 | 原文"适用场景" |
|---|---|
| `plugin_hosted` | AstrBot 完整发行设计、当前兼容运行与分批迁移 |
| `standalone_service` | 独立 Bot 服务或后台任务 |
| `embedded_app` | 桌面、移动端或**自有 Web 应用** |
| `split_services` | 高并发或独立扩缩容 |

我们是"自有 Web 应用 + 进程内托管插件"，**`standalone_service` 与 `embedded_app` 都说得通**，
文档没给进一步划界。但有一条**决定性**的宽免（`:148`）：

> 模式只改变 transport 和 lifecycle shell，不改变能力 ID、schema、owner、回执和状态机。**首期可以是 in-process facade**；…

→ **选哪个不影响契约面**，而且 in-process 被明文允许。**这不是需要现在拍板的事**，记在待问清单。

### 1.2 🔴 一个反直觉但决定架构的约束：`host_kind` **不能**填部署模式

`contracts/v1/schemas/runtime-scope.schema.json:209-225` 有一条 `not` 条款，
**禁止 `host_kind` 取那四个部署模式值中的任何一个**。我实测验证过（`jsonschema` Draft 2020-12）：

```
host_kind='embedded_app'        -> REJECT
host_kind='plugin_hosted'       -> REJECT
host_kind='standalone_service'  -> REJECT
host_kind='split_services'      -> REJECT
host_kind='astrbot'             -> PASS
host_kind='emotionapp'          -> PASS     ← 自造宿主实现名，合法
host_kind='astrbot_host'        -> PASS
```

作者在 `LEVEL_A_CLOSURE_DECISIONS_20260910.md:44,50` 里裁决过这件事：
**"采纳方案 A；Schema 已改为 `deployment_modes` 并限制四种部署模式值，`host_kind` 保持宿主实现语义。"**

🔴 **对我们的直接含义**：我们到处写的 `PLATFORM_NAME = "webchat"`（`host/astrbot_host/umo.py:124`）
在**新框架里落到 `host_kind` 那个位置**，而 `webchat` 是**平台类型名**、不是宿主实现名。
将来填 scope 时**不能**把 `webchat` 填进 `host_kind`；我们是 `host_kind="emotionapp"`（自造）或 `"astrbot"`（继承语义）。

---

## 2. 逐项判定表

图例：**✅ 兼容** / **🟡 形状对、字段不对** / **🔴 冲突或缺失** / **⏸ 暂不相关**

### 2.1 宿主接入方式 —— 🔴 最大的一处判定

| 维度 | 新框架要求 | 我们现在 | 判定 |
|---|---|---|---|
| 插件在新框架里的地位 | 扩展通过 `CapabilityDescriptor` 声明，框架注册/发现/绑定/启用/取消/lookup/resume（v1 `:428`） | 插件通过 AstrBot 兼容层加载（`host/astrbot_host/loader/plugin_loader.py`） | 🔴 **两套模型** |
| 插件能否拿到 sender | **"外部插件不能直接取得全局数据库、人格 Prompt、主动调度或平台 sender"**（v1 `:428`） | 我们**提供了** `platform_manager` 让插件能投递（`plugin_host.py:284`），`send_by_session` 接到 `DeliveryPort` | 🔴 **正是被禁的形状** |
| 唯一 sender | **"每个 target 在同一时刻只有一个可提交 sender"**（v1 `:309`） | 插件自己的发送链 + 我们的投递端口并存 | 🔴 违反 |
| `event.send` 旧发送链 | **"尤其不能直接带入：直接包装 `event.send`、`send_streaming` 或多平台 sender 的旧发送链"**（v1 `:489`） | 我们的 compat 层**实现了** `AstrMessageEvent.send()`（`host/astrbot_host/compat/core/platform/astr_message_event.py`） | 🔴 这就是"直接包装旧发送链" |
| `provider_id`/`generation`/`owner`/`status` | **必须由宿主/Kernel 关联，不能由 JSON 自报**（`NATIVE_PLUGIN_INTEGRATION_V0.md:149`） | 我们**照实读插件元数据**并原样呈现 | 🔴 方向相反 |

**但是** —— 这里必须把话说完整，否则会误判成"我们的宿主白做了"：

> **`docs/S7_RELEASE_MIGRATION_AND_ROLLBACK_V0.md:81`**：
> "旧插件与新应用同机时，逐一登记旧定时器、消息 hook、后台任务、fallback、writer 和 sender。
> **兼容适配器复用原服务；新实现获得业务准入之前，不能再起一套角色、日记、主动决策、设备监听或发送者。**"

→ **新框架明确允许"兼容适配器"存在**，只是要求它**复用原服务**、不许**另起一套**。
我们的宿主就是这种兼容适配器。

**⚠️ 但 v1 `:542` 又说**：
> "现有隔离 bridge 只能作为语义参照与 fixture 来源，**不能直接移植为新根的兼容包装器**。"

**这两句有张力**（`S7` 说"兼容适配器复用原服务"，v1 说"bridge 不能当新根的兼容包装器"）。
**唯一自洽的读法**：
* **过渡期**（现在）：老插件跑在我们的宿主上 ✅ **S7 允许**；
* **新根**（G0 以后）：不许把我们的宿主代码**搬进 `framework/`** 当基础 ❌ **v1 禁止**。
* 两者可以共存，但**文档没把这两件事分开说** → **必须问作者**（见 §5 问题 ⑤）。

### 2.2 领域重复 —— 🟡 不是冲突，是"将来谁接管"的问题

我们**自己实现**了 6 个领域，新框架把这 6 个都划成了**独立 owner**：

| 我们的模块 | 新框架 owner（v1 §3.2） | 判定 |
|---|---|---|
| `domain/persona.py` + `persona_repo.py`（6 字段 + 提示词片段） | **Role/Relationship/Affect** | 🟡 我们的是"角色卡存储"，新框架的是"角色决策" —— 职责不同，**将来 Role 会取代我们的组装逻辑** |
| `domain/session_repo.py` + `context.py`（三模式上下文） | **Memory + Conversation** | 🟡 同上 |
| `domain/summarizer.py`（会话内摘要） | **Memory**（"只保存已接受、带来源、可修订的长期认识" v1 `:223`） | 🔴 **我们的摘要无来源、无 revision、无撤回** → 不符 |
| `domain/image_gen.py` + `image_tool.py`（function calling 生图） | **Image**（`ImageTask`/`MediaAssetRef`/ACL） | 🟡 能力在，**契约不在** |
| `domain/ports.py`（三个 Protocol） | 无直接对应物 | ✅ **这是我们对的地方** —— 见 §2.6 |

⚠️ **关键判断**：这些**不算冲突**，因为新框架**还没有实现**（S3-S6 全是 `D/NR`）。
但**将来**：`v1:359` 说"丢弃 Content 能力不影响基础日记"、"丢弃创作模块不影响日记"；
`v1:102` 说"**能力不得私建第二个角色或 sender**"。

→ **危险的是**：我们现在就是"第二个角色 + 第二个 sender"（我们自己组装人设、自己发回复）。
**在过渡期这没问题**（我们就是主应用），**但新框架一旦接管 Role，我们的组装逻辑必须让位**。

### 2.3 身份与作用域 —— 🔴 缺得最彻底（这是最贵的一块）

`RuntimeScope` 是**新框架每一个跨域调用的必填封套**（`common.schema.json:208-266` 把 `scope` 列为 required）。
它有 **14 项 required + `additionalProperties:false`**。桌面推演：

| RuntimeScope 必填字段 | 我们能填吗 | 来源 |
|---|---|---|
| `session_id` | ✅ | `emotion_app/domain/session_repo.py:41` |
| `persona_id` | ✅ | `emotion_app/domain/persona.py:88` |
| `ecosystem_id` | ❌ | 零命中 |
| `installation_id` | ❌ | 零命中 |
| `runtime_instance_id` | ❌ | 零命中 |
| `host_kind` | ❌ | 零命中（且**不能**填 `webchat`） |
| `bot_id` | ❌ | 零命中 |
| `platform` | ⚠️ 我们有 `webchat`，但语义不对（那是**类型名**） | `umo.py:124` |
| `account_id` | ❌ | 零命中 |
| `conversation_ref` | ❌ | 零命中 |
| `platform_conversation_id` | ⚠️ 我们有 umo，但它是**拼接串**不是引用 | `umo.py:174` |
| `user_id` | ⚠️ 我们有 `PLUGINS_UMO_USER_ID`（默认 `"default"`） | `infra/config.py:256` |
| `group_id` | ❌（单用户，不需要） | — |
| `persona_binding_revision` | ❌ | 零命中 |

**结果：14 项里能填 2 项。**

**且五项稳定逻辑身份全部零命中**（`grep` 逐 token 验证，见 `FRAMEWORK_V1_OUR_INVENTORY.md` §9）：

```
ecosystem_id  ABSENT(0)   installation_id  ABSENT(0)   runtime_instance_id  ABSENT(0)
host_kind     ABSENT(0)   bot_id           ABSENT(0)   persona_binding_revision ABSENT(0)
```

🔴 **这是最要紧的一条**：新框架的**所有权模型**是六元组
`(ecosystem_id, installation_id, bot_id, persona_id, subject_ref, visibility_namespace)`
（`contracts/v1/README.md:54`）—— **我们一个都没有**。
我们现在的"主键"是 `umo = 11:FriendMessage:<uid>`（`umo.py:174`）与内部 uuid。
**两者不是映射关系，是两套东西**（`umo` 是平台路由，不是 owner）。

⚠️ **但有一处好消息**：`umo.py` 的开头就写着"**格式一改 = 换库**"，
而新框架 `ROLE_RUNTIME_KERNEL_DESIGN.md:448` 也说
"`platform_account_id` 与 `platform_user_id` **不能拼接成跨平台主键**"。
→ 说明**新框架同样把"平台 ID"排除在 owner 之外**，我们的 `umo` 与它**不矛盾，只是层级不同**。

**`IdentityBinding`：设计包里没有 Schema。**
`grep '"$id"' contracts/ | grep -i ident` → 零命中。
它有两个**字段逐项不同**的散文定义（`PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:63-79` 的 12 字段 JSON
vs `ROLE_RUNTIME_KERNEL_DESIGN.md:432-446` 的 11 字段 text block）。
→ **我们必须自己选一套并明确记录**，或者问作者（见 §5 问题 ④）。

### 2.4 投递、回执、幂等 —— 🔴 能力全缺，且**我们现在的实现方式正好落在反面**

我们已经有一个**能用的投递**：`OurPlatform.send_by_session()`（`host/astrbot_host/platform_manager.py:104-143`），
而且它**做对了最要紧的一件事** —— 端口没接时**如实返回 `False`，绝不伪造送达**（`:126-132`）。

但对照新框架的 `DeliveryReceipt`，我们**一张回执都没有**：

| 新框架要求 | 我们 | 判定 |
|---|---|---|
| `DeliveryReceipt` 20 项 required（`delivery-receipt.schema.json:136-157`） | 零命中 | 🔴 |
| `delivered` 需 `delivered_at`+`submitted_at`+`verification.level=delivery_confirmed` | 无 | 🔴 |
| `accepted` 需 `submitted_at`+`level=platform_accepted`；**非 delivered 时 `delivered_at` 必须为 null** | 无 | 🔴 |
| `cancelled` 是**唯一**强制 `verification.level=not_submitted` 的状态 | 无 | 🔴 |
| `effect_state="applied"` ⇒ `operation_id` + `receipt_ref` 必填（`operation-result.schema.json:354-376`） | 无 | 🔴 |
| `operation_id` / `idempotency_key` / `outbox` / `inbox` | **全部零命中** | 🔴 |
| 重试必须**沿用原 operation**，不能换 ID 重做（v1 `:99`） | 我们有 `_chat_with_fallback`（`chat.py:424-457`）**换模型重试** | ⚠️ 对 LLM 调用 OK（只读），**对有副作用的操作绝不行** |

⚠️ **一个真实的语义差**：我们现在"发消息"= **HTTP 响应返回给调用方**（`app.py:414-436` 的 `chat.completion`）。
那在结构上等价于 `submitted`，**但我们连 `accepted` 都证明不了**（没有平台回执）。
→ 好消息：我们现在**没有伪造 `delivered`**；坏消息：**我们也没有 `delivered` 这个概念**。

📌 **这正是 `NEXT_STEPS.md` P1-3 那条"接不上就如实 False，不许假装成功"的价值** ——
它和新框架 §7.4 的语义**完全一致**。这条设计判断是**对的**，将来直接可用。

### 2.5 供应商 / 能力模型 —— 🟡 形状对，字段不对

| 维度 | 新框架 | 我们 | 判定 |
|---|---|---|---|
| 两层结构 | Provider（可替换 provider：模型、索引、平台、设备、第三方插件）v1 `:117` | `provider_sources` 1:N `providers`（`infra/providers_store.py`） | ✅ **形状一致** |
| 能力目录 | `CapabilityDescriptor` 15 项 required，含 `owner_ref`/`status`/`admission`/`side_effect`/`limits`/`expires_at` | 无 | 🔴 零命中 |
| 声明者 | **宿主/Kernel 关联，插件不能自报** | 我们照实读插件 metadata | 🔴 方向相反 |
| 权限 | `required_permissions` **只是声明**，grant 由 Kernel 签发 | 无授权层 | 🔴 |
| 生命周期 | `discovered → validated → bound → ready/degraded → stopped/failed` + `quiescing/draining/closing` | 我们有"加载/初始化/失败"三分（`host_app.py:54-73`） | 🟡 **语义接近，枚举不齐** |
| 能力五态 | `installed`/`enabled`/`authorized`/`ready`/`furniture projected`（v1 `:414-422`） | 无 | 🔴 |

📌 **一个我们做对了的**：`HostPorts.capabilities()`（`compat/core/star/context.py:118-137`）
把 `platform_manager` 与 `platform_delivery` **分开暴露**、把 `provider_manager` 与 `provider_usable` 分开暴露，
注释写着"**有管理器 ≠ 能送达**"、"避免'诊断说可用、实际插件解析不到'"。
**这和 v1 `:424`"能力 ready 也不等于拥有读取或发送权限"是同一种思维**。
→ 这个习惯要保留，它是我们对接新框架时最省力的地方。

### 2.6 领域层可移植性 —— ✅ **这一条我们过关了**

新框架的硬判据（`PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:41`）：

> 可移植层允许使用 Python 标准库和明确声明的跨平台依赖；**AstrBot、Quart/Hypercorn、OneBot、平台 SDK 和桌面 UI 只出现在宿主/平台/管理入口包。Feature Service 的核心用例不能 import 这些模块，也不能接收其对象。**

验收判据（`PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:192`）："**核心可导入 | 便携层 import 扫描不依赖 AstrBot、平台 SDK、Quart request 或宿主 event**"

**实测我们的 `emotion_app/domain/`**：

```
外部依赖仅：httpx / threading / binascii / struct / zlib   （全部是标准库或明确声明的跨平台依赖）
零命中：quart / hypercorn / astrbot / host
```

`domain/ports.py:24-29` 甚至写着"用 `typing.Protocol` 而非继承 —— **陪伴插件的类不会也不该继承我们的基类**"，
`domain/chat.py:74` 写着"**只依赖 `ChatBackend` Protocol —— 换后端（陪伴的 provider / 别的协议）不用改这里**"。

✅ **判定：`emotion_app/domain/` 已经满足新框架的便携层判据**，
将来原样搬进 `framework/` 时**不需要重写**。这是本轮审计里**唯一的"直接可用"资产**。

⚠️ **但 `emotion_app/web/` 不满足**，而且**踩了被点名的形状**：
`plugin_pages_web.py:159-190` **直接服务插件的页面目录**，而 `PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:156` 明说
"页面**不直接调用某个插件的 Quart 路由或读取其 data_dir**"，应走 `WorkspaceGateway`。
→ 这是**过渡期可接受、进新根前必须改**的一条。

### 2.7 数据与存储 —— 🟡 我们的是"文档库"，新框架要的是"领域账本"

| 维度 | 新框架（v1 §5） | 我们（`storage/sqlite_store.py`） |
|---|---|---|
| 形状 | 领域正本 / 来源与证据 / 可重建索引 / 资源文件 **四层** | `store(section,payload)` + `meta(k,v)` **两张表**（`:26-37`） |
| 并发 | 每域 CAS/幂等/expected revision | `next_revision()` 是**全局单调计数**（`:150`），**不是实体 revision** |
| 可靠性 | outbox/inbox + 消费游标 + ACK | **零命中** |
| 迁移 | 按 owner 最小一致性交接组 + 水位 + fence | `schema_version` 常量 = 1（`:24`），无迁移器 |
| 导出 | `BackupSet`/`PortableSnapshot` 带水位/墓碑/hash | `export_store()` 返回 `{schema_version, revision, backend, data}`（`:178-184`） |

✅ **做对的两处**：①`export_store`/`import_store` 从第一天就有（`base.py:8-9` 注释写明是为了防数据锁死）；
②`import_store` 的 `replace` **必须显式调用**，不做隐式覆盖（`base.py:78-82`）。
这两条与新框架"快照是恢复候选、不等于自动激活"（v1 `:231`）**方向一致**。

⚠️ **实际数据量**（`data/emotion.db` 实测）：`personas` 28,679B / `settings` 128B / `sessions` 400B / `messages` 2,210B，
`meta.revision = 9`。→ **数据量极小**，**现在换存储模型的代价几乎为零**，这是好消息。

### 2.8 时间窗判断：新框架现在到哪一步了

这一条决定了"我们现在该不该为它改东西"。

| 阶段 | 状态（`FRAMEWORK_ROADMAP_AND_STATUS_V1.md` 逐字） |
|---|---|
| **整体** | `:5` "**整体仍 S2→S3，生产未切换。**" |
| S0 现状盘点 | `:143` "`D`，已完成。" |
| S1 公共语言/身份 | `:153` "`C+R`。…契约包仍为 `review`，**真实授权和 SDK 发行还未完成**。" |
| S2 只读运行壳 | `:163` "`R+H+A(只读切片)`" |
| S3 持久化切片 | `:215` "**完整 S3 仍未验收**" |
| S4 角色运行时 | `:273` "**完整阶段尚未验收（`D / NR`）**" |
| S5 表达/投递/反馈 | `:302` "**完整阶段为 `D / NR`**" |
| S6 原生能力 | `:326` "…**公共领域配套格式、可执行故障夹具和运行证据仍待交付**。" |
| S7 迁移/产品化 | `:342` "**全阶段 `D+BL(S3-S6)`**…24 条 PKG、28 条 REL 分别待验。" |

v1 终稿自己（`:7` 逐字）：
> "**当前结论：设计已经允许在独立源码根开始 G0-G2 的隔离构建；它尚不接管现役 sender、生产 writer、真实 route，也不表示 S0-S7 任一阶段已经升级。**"

🔴 **两个必须知道的事实**：

1. **G0-G6 门槛表在整个设计包里没有对应物**（我逐词检索过：`grep -rn "G0 新根基线\|G0-G6"` → 零命中）。
   它是 v1 终稿 **2026-09-20 全新引入**的。
2. **v1 `:7` 引用的准入条件文件 `evaluations/FRAMEWORK_ZERO_BUILD_READINESS_20260920.md` 在磁盘上不存在**
   （`find / -name "FRAMEWORK_ZERO_BUILD_READINESS*"` → 零输出）。
   → **我们手上没有 G0-G2 的验收判据文本**，要排期必须先向作者索取。

---

## 3. 对"即将做的"逐条判定（`NEXT_STEPS.md`）

| 计划项 | 内容 | 判定 | 理由 |
|---|---|---|---|
| **P0-1** | `AstrMessageEvent` 兼容对象 | 🟢 **照做，价值不变** | 过渡期必需；新框架的 `NormalizedInteractionEvent`（`decision/v1/schemas/normalized-interaction-event.schema.json`）是它的**未来替身**，但**不是现在**。⚠️ 别把这个对象**设计成** `NormalizedInteractionEvent`，两者字段差得远（后者 19 项 required、`additionalProperties:false`） |
| **P0-2** | 命令派发 | 🟢 **照做** | 纯过渡期价值，新框架无对应物 |
| **P0-3** | 结果回写 | 🟢 **照做** | 同上 |
| **P0-4** | LLM 钩子（记忆注入） | 🟢 **照做，且长期有效** | 新框架的 `ContextContribution`（`sharing/v1`）就是这个位置的未来形态。⚠️ 但新框架**禁止**"提交任意 system prompt、绕过 ACL"（v1 `:428`、`NATIVE:181`）—— 我们现在的钩子是**整段替换 messages**，将来必须收窄成**有 lane/evidence/TTL 的贡献项** |
| **P1-1** | 定死 umo 约定 | 🟢 **照做，且已做对** | `umo.py` 已定死 `11:FriendMessage:<uid>`，与生产 `companions.json` 逐字同构。⚠️ 但要知道：**这是平台路由键，不是 owner**，将来要与 `RuntimeScope` 并存 |
| **P1-2** | `platform_manager` + 平台实例 | 🟡 **照做，但标注为过渡件** | 新框架要 `ConversationAdapter`/`IdentityAdapter`，不是 `platform_manager`。**过渡期必需，进新根时废弃** |
| **P1-3** | `send_by_session` | 🟢 **照做，设计判断正确** | "接不上就如实 False，不许假装成功"**与新框架 §7.4 完全同源**。将来包一层 `DeliveryReceipt` 即可 |
| **P1-4** | 面板"绑定"能成功 | 🟢 **照做** | 纯过渡期 |
| **P2-2** | 我们自己的主动消息 | 🔴 **建议暂缓** | 新框架把主动消息整个重构成了 `Signal → ProactiveCandidate → IntentPlan → Delivery`（v1 §7.2），且**S5 是 `D/NR`**。我们现在自己写一套，**大概率将来整个丢掉**。⚠️ **这正是 `NEXT_STEPS.md` §4 已列的"不做"之一**（"重写/移植陪伴的机制"）—— 保持一致 |
| **P2-3** | 情绪/关系状态 | 🔴 **建议暂缓** | 新框架里是 `Affect`/`Relationship` 两个独立 owner，且有 `AffectEvent`/`RelationshipView` Schema。**自己做 = 将来必丢** |
| **P2-4** | 长期记忆（跨会话） | 🟡 **可做小步，别做大** | 新框架 **Memory 契约是六大域里最完备的**（`contracts/v1/` 13 个 Schema）。⚠️ 我们的会话内摘要**无来源/无 revision/无撤回**，直接做大将来要重写。**建议只做"会话内摘要 + 可取回"，不做"长期认识"** |
| **P2-5** | 日程 | 🔴 **建议暂缓** | `Calendar` 在新框架里有独立 Schema（`contracts/calendar/v1/` 6 个）与 `StoryProjection`。同上 |

### 3.1 🎯 一句话排期建议

```
现在 ──→ 过渡期（我们做，不与新框架冲突）
         P0-1 → P0-2 → P0-3 → P1-1 → P1-2 → P1-3 → P1-4 → P0-4
         ✅ 全部照做；把「宿主」当产品做，别当新根做

暂缓 ──→ P2-2 / P2-3 / P2-5（新框架已重设计，做了大概率丢）
         P2-4 只做会话内，不做长期认识

将来 ──→ G0-G2（需先向作者索取准入判据）
         独立源码根 + 临时库；把 emotion_app/domain/ 搬进去（已满足便携判据）
```

---

## 4. 结论：冲突清单（按严重度）

### 🔴 真冲突（必须改，但现在不用急着改）

| # | 冲突 | 证据 | 何时必须处理 |
|---|---|---|---|
| C1 | 插件拿得到 sender —— v1 明文禁止 | v1 `:428` vs `plugin_host.py:284` | 进新根前 |
| C2 | 同时存在多个可提交 sender（插件自己的 + 我们的） | v1 `:309` vs 现状 | 进新根前 |
| C3 | 我们包装了 `event.send` 旧发送链 | v1 `:489` vs `astr_message_event.py` 的 `send()` | 进新根前 |
| C4 | 插件元数据被当作权威（`provider_id`/`status` 应宿主签发） | `NATIVE:149` vs 现状 | 进新根前 |
| C5 | 宿主代码不能移植为新根兼容包装器 | v1 `:542` | **从第一天就要守住**：别把 `host/` 设计成新根 |

### 🟡 缺口（不是冲突，是没有）

| # | 缺口 | 规模 |
|---|---|---|
| G1 | `RuntimeScope` 14 项必填 → 我们只能填 2 项 | 大（但**数据量极小，代价低**） |
| G2 | `DeliveryReceipt` / `OperationResult` / `operation_id` / `idempotency_key` / outbox/inbox —— 全部零命中 | 大 |
| G3 | `CapabilityDescriptor` / `ExtensionManifest` / 能力生命周期 | 中 |
| G4 | `IdentityBinding`（**设计包自己都没 Schema**，两套字段打架） | 中，且**需作者裁决** |
| G5 | 可见性 / 受众 / sharing（`visibility`/`audience`/`sharing_mode` 全零命中） | 中，**单用户场景可先不做** |
| G6 | `emotion_app/web/` 直接读插件 data_dir —— 被点名禁止 | 小，进新根前改 |

### ✅ 已经兼容 / 直接可用

| # | 资产 | 证据 |
|---|---|---|
| A1 | **`emotion_app/domain/` 满足便携层 import 判据** | 实测零 `quart`/`astrbot` 依赖 |
| A2 | **三个 Protocol 的"不写死"设计** | `ports.py:24-29`、`chat.py:74` |
| A3 | **`send_by_session` 如实返回 False，不伪造送达** | `platform_manager.py:126-132` |
| A4 | **能力"分项暴露、不合并成一个是/否"** | `context.py:118-137` |
| A5 | **`export_store`/`import_store` 从第一天就有** | `base.py:8-9` |
| A6 | **`umo` 与生产数据逐字同构** | `umo.py:20-29` |
| A7 | **供应商两层结构（source → model）** | `providers_store.py` vs v1 `:117` 形状一致 |

---

## 5. 需要问作者的 6 个问题（按价值排序）

> ⚠️ 前 2 个**卡住我们的排期**，必须在动手新根前问。

1. **`FRAMEWORK_ZERO_BUILD_READINESS_20260920.md` 能不能发我们？**
   终稿 `:7` 说 G0-G2 的准入条件看这个文件，**磁盘上不存在**（`find /` 零命中）。
   没有它我们排不了 G0。

2. **读合并顺序到底是哪个？**（文档内部 3:3 自相矛盾，**无多数**）
   * `session → user → global`：v1 `:181`、`COMPANION_CONTRACTS_AND_VERTICAL_SLICE.md:89`、`NATIVE_PLUGIN_INTEGRATION_V0.md:77`
   * `global → user → session`：`NATIVE_PLUGIN_INTEGRATION_V0.md:414`、`ROLEPLAY_PROACTIVE_REBUILD.md:232`、`COMPANION_INJECTION_PROTOCOL.md:229`

   ⚠️ 且 `COMPANION_INJECTION_PROTOCOL.md:229` 是**唯一给出语义理由**的地方，理由指向后者：
   "先取当前受众允许的 global 安全投影，再取已解析用户的 user 投影，最后取当前 session 的近期场景"
   —— 这是"越具体越晚应用（越能覆盖）"的 layering。
   若按 `session -> user -> global` 且"后写覆盖"读，**最泛的 global 会覆盖最具体的 session**，语义是错的。

3. **`S7_RELEASE_MIGRATION_AND_ROLLBACK_V0.md:81`"兼容适配器复用原服务"
   与 v1 `:542`"隔离 bridge 不能直接移植为新根的兼容包装器"如何并存？**
   我们的 AstrBot 兼容宿主**是不是**那个"兼容适配器"？过渡期能用多久？

4. **`IdentityBinding` 用哪套字段？** 设计包里**没有它的 Schema**，
   两份散文定义字段逐项不同：
   * `PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:65-79`：`binding_id`/`canonical_subject_ref`/`authority`/`valid_from`…（12 字段，**无 `status`**）
   * `ROLE_RUNTIME_KERNEL_DESIGN.md:432-446`：`binding_ref`/`subject_ref`/`proof_kind`（**有枚举**）/`effective_from`/`status`…（11 字段，**无 `ecosystem_id`**）

5. **`ModelAdapter` / `Store` 的输入输出是什么？**
   十一端口里有 5 个（`ModelAdapter`/`Clock`/`Store`/`TaskSupervisor`/`WorkspaceGateway`）
   **只在名单里出现过一次**，没有 I/O 定义。我们要实现"申请模型"这个能力时无从下手。

6. **我们该声明 `standalone_service` 还是 `embedded_app`？**
   `PLATFORM_PORTABILITY_AND_BUNDLING_V0.md:145` 的场景文字（"自有 Web 应用"）两种都说得通。
   （文档 `:148` 说"模式只改变 transport 和 lifecycle shell"，所以**不阻塞**，但 manifest 里总要填一个。）

---

## 6. 我建议的下一步（不动代码）

1. **把这份审计 + 两份支撑材料发给作者**，先问 §5 的 1、2、3（前两个卡排期，第三个卡架构）。
2. **继续按 `NEXT_STEPS.md` 做 P0/P1** —— 它们**不白干**，且**正好是新框架过渡期需要的兼容适配器**。
3. **加一条约束到项目认知**（写进 `NEXT_STEPS.md` 与 `AGENTS.md`）：
   > 🔴 **`host/astrbot_host/` 是过渡件，不是新根。** 它可以完善、可以修 bug，
   > 但**不许**把新框架的 `RuntimeScope`/`DeliveryReceipt`/`CapabilityDescriptor` 概念
   > **半吊子地塞进它** —— 那样两边都不像，还会浪费一轮。
   > 新概念一律进**独立源码根**（G0 时建）。
4. **暂缓 P2-2 / P2-3 / P2-5**（主动消息 / 情绪关系 / 日程）—— 新框架已整体重设计，现在做大概率丢。
5. **P2-4 长期记忆只做会话内**，别做"长期认识"（新框架 Memory 契约是六域里最完备的，硬碰硬不划算）。

---

## 附录 A：本次审计用到的实测命令（可复现）

```bash
# ① host_kind 枚举禁令（决定性，且反直觉）
python3 -c "
import json, jsonschema
base='/root/Ai-works/爱语/refs/latest_companion/docs/contracts/'
scope=json.load(open(base+'v1/schemas/runtime-scope.schema.json'))
common=json.load(open(base+'v1/schemas/common.schema.json'))
store={'urn:companion:schema:common:v1':common,'urn:companion:schema:runtime-scope:v1':scope}
res=jsonschema.RefResolver.from_schema(scope, store=store)
def mk(hk): return {'ecosystem_id':'eco','installation_id':'inst','runtime_instance_id':'rt',
  'host_kind':hk,'host_id':None,'bot_id':'bot','platform':None,'account_id':None,
  'conversation_ref':None,'platform_conversation_id':None,'session_id':None,
  'user_id':None,'group_id':None,'persona_id':'p','persona_binding_revision':0}
for hk in ['embedded_app','plugin_hosted','standalone_service','split_services','astrbot','emotionapp']:
    try: jsonschema.validate(mk(hk), scope, resolver=res); print(hk,'PASS')
    except jsonschema.ValidationError: print(hk,'REJECT')
"

# ② v1 稿引用的 47 个相对链接，逐个在磁盘上找（只有 1 个缺失）
#    缺失的就是 FRAMEWORK_ZERO_BUILD_READINESS_20260920.md

# ③ 新框架身份字段在我们代码里的零命中清单
cd /root/Ai-works/情绪app
for t in ecosystem_id installation_id runtime_instance_id host_kind host_id bot_id \
         persona_binding_revision conversation_ref platform_conversation_id subject_ref \
         canonical_subject sharing_mode sharing_anchor visibility_namespace \
         operation_id idempotency_key outbox inbox capability_descriptor extension_manifest; do
  n=$(grep -rn --include=*.py --include=*.js --include=*.html "$t" emotion_app/ host/ | wc -l)
  printf "%-28s %s\n" "$t" "$([ "$n" = 0 ] && echo 'ABSENT(0)' || echo "PRESENT($n)")"
done

# ④ 领域层便携性判据（新框架 §7 的核心可导入）
grep -rn "^import \|^from " emotion_app/domain/*.py | grep -iE "quart|astrbot|hypercorn|onebot"
# → 零命中 ✅

# ⑤ G0-G6 是否只存在于 v1 稿
grep -rn "G0 新根基线\|G0-G6\|G0-G2" /root/Ai-works/爱语/refs/ --include=*.md
# → 零命中（v1 稿之外不存在）
```

## 附录 B：本审计产出的文件

| 文件 | 内容 |
|---|---|
| `docs/FRAMEWORK_V1_COMPAT_AUDIT.md` | **本报告** —— 判定与建议 |
| `docs/FRAMEWORK_V1_REQUIREMENTS_EXTRACT.md` | 新框架要求面/禁令面全文提取（1,175 行，逐字带行号） |
| `docs/FRAMEWORK_V1_OUR_INVENTORY.md` | 我们已实现面的逐项清单（764 行，逐条 `file:line`） |

---

*审计完毕。本报告只做判断，未改动任何产品代码。*
