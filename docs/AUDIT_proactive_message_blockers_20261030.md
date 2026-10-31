# 只读审计：主动消息投递被什么卡住（2026-10-30）

> 审计对象：`/root/Ai-works/情绪app`（本机）+ 生产 `root@64.81.114.30:/root/apps/情绪app`；
> 官方插件存档 `/tmp/emotion-v7-official-2xr71_m6/astrbot_plugin_private_companion`（7.0.0，只读）。
> 本文件**只记录证据与判定，未改任何源码/生产数据**。

---

## 0. 结论先行

1. **「设为核心用户」按钮恒 disabled 的根因（主代理已修）在生产仍未生效** —— 生产那份
   `host/astrbot_host/compat/api/web.py` 仍是旧版（md5 `3f7823e7e736342d5fa998bb7033cfa3`，
   mtime `2026-10-29 16:24:08`），进程 PID 344382 起于 `2026-10-30 12:25:48`。本机（含修复）
   为 `04746c8000abcb102599adf1813062fd`。**黑盒实测**：`GET /acquaintance?read_mode=bogus`
   → `HTTP 200 status=ok`；若 query 真被读到，官方代码会返回 `acquaintance_request_invalid`
   （`astrbot_host/acquaintance_api.py:456`）⇒ 生产 `request.query` 仍为空。
2. **用户的方向是对的，但机制不是「只有核心用户才能主动消息」**：`proactive.contact` **不是**
   `core_only`（`identity_policy/object_access.py:59`）。准确规则是
   「**STANDARD 必须持有该窗口的 `proactive.contact` grant；IMPORTANT/CORE 免 grant**」
   （由 `object_access.py:268-277` 的表达式推出，见 §1）。
3. **即便设成 core，主动消息**仍然**不会投递**。确定性阻塞点是
   **宿主从不派发 `OnAgentDoneEvent` 等 Agent 生命周期钩子** ⇒ 官方插件无法把
   `conversation_native_turns` 标成 `completed` ⇒ 主动消息的**候选集恒空**。
   生产实测：`conversation_native_turns` 11 行，**`completed` = 0**（9 failed / 2 running）；
   `proactive_contacts` / `background_deliveries` / `agenda_dispatches` 全 0。
4. 第三层（运营层）也**尚未配置**：`proactive.contact.enabled` 与 `automatic_enabled` 官方默认
   均为 `False`（`proactive/configuration.py:228,287`），而生产窗口级配置一行都没有。

---

## 1. 身份链：哪一步要求什么身份

官方 7.0.0 `companion_v2/identity_policy/object_access.py`：

| 步骤 | 位置 | 要求 |
|---|---|---|
| 动作契约 | `:30-116` `DEFAULT_ACTIONS` | `proactive.contact`（`:59`）**没有** `core_only`；也**没有** `private_owner_without_grant`（对比 `role.expression.read` `:60`、`calendar.*` `:95-96` 都带） |
| CORE 硬前置 | `:261-263` | 只在 `definition.core_only` 时返回 `core_access_required`。`core_only` 仅见于 `access.policy.set`、`role.growth.manage`、`memory.manage`、`capability.*`、`diagnostics.*` ⇒ **与主动消息无关** |
| 私聊路由豁免 | `:268-273` | `route` 只在 `allow_private_owner and definition.private_owner_without_grant and scope.kind is PRIVATE` 时计算 ⇒ 对 `proactive.contact` **`route` 恒为 None** ⇒ `private_owner_feature` **恒为 False** |
| grant 判定 | `:275-277` | `if explicit_private_decision or (tier is STANDARD and not private_owner_feature): identity.authorize(...)` |
| 逐档结果（按上式代入） | —— | `standard` → **需要该 scope 的 `proactive.contact` grant**；`important` → 不需要；`core` → 不需要 |

私聊投递授权（**不是**主动消息的前置，但容易混淆）：

- `astrbot_host/multi_authority.py:475-480` `delivery_access()`：正式版私聊投递**不再需要授权**，
  只校验目标是配置里认得的私聊对象。生产 `mode=multi`（`/host/status` 实测），
  故 `staging_delivery.py` 那条严格的 `message.delivery` 授权链不参与。
- ⇒ 「设为核心用户」对**被动回复**没有影响；对**主动消息**的影响是**绕过 grant 要求**。

CORE 身份还额外解锁三项干预权限（`identity_policy/interventions.py:11-15`）：
`role.state.intervene`、`calendar.intervene`、`role.rest.wake`
——这也是页面把 CORE 描述成「核心管理员」的原因，与主动消息无关。

**生产现状（只读）**：

- `GET /acquaintance` → `persons[0].identity_tier = "standard"`（`onebot-user:3412642082`）。
- DB `subject_access_tiers` 仅 1 行：`dashboard:394151a1…` = `core`（那是 Dashboard 操作员身份，
  由 `astrbot_host/management_identity.py:23-46` 首次访问时创建，**不是** QQ 用户）。
- `identity_grants` 里该用户有 `conversation.reply`/`text`/`memory.read|propose|reflect|subscribe`/
  `configuration.read`/`extension.tool.invoke`，**没有 `proactive.contact`**
  （符合 `astrbot_host/multi_authority.py:321-348`：`PROACTIVE_DEFAULT = False` 时**不签发**）。

---

## 2. 主动消息链路（官方 7.0.0）

```
BackgroundCoordinator.advance / Application._drive            companion_v2/host/application.py:875-964
  └─ contact_schedule.advance_async()                        :947
       ├─ _begin_tick()  取候选                               proactive/scheduling.py:1444-1494
       │    ├─ _legacy_rows(cursor)   conversation_flows     :1443-1449
       │    └─ native rows: AND n.generation_state = 'completed'   :1488-1493  ← 候选来源（严格）
       ├─ _plan_one → _plan(row, at)                          :1498-1507 / :449
       │    ├─ contacts.proactive_denied(...)                 :454    (权限：只看 REVOCABLE_ABSENCE_CODES)
       │    ├─ preferences.values["automatic_enabled"]        :459    (总开关，默认 False)
       │    ├─ health/sleep/rhythm/idle/budget/dedup/history  :461-700+
       │    └─ _origin(context, batch_id, at)                 :291-309 ← 硬要求 native generation_state == "completed"
       ├─ _activate_plan → create_task(self._run(plan))       :1516-1541
       └─ _run(plan) → contacts.contact(...)                  :1295-1380
            └─ ProactiveContacts.contact                     proactive/service.py:543-620
                 ├─ platform_circuit.cooldown_until          :592-595  (QQ 1200 拒发熔断)
                 ├─ tasks.authorize_context(action=proactive.contact)  :599, :606
                 └─ … → execution → delivery
投递：astrbot_host/multi_session.py:1260-1274 send()
       └─ OneBotPrivateSender / AstrBotPlatformSender
            ├─ OneBot: api.call_action("send_private_msg", ...)   adapters/astrbot/onebot.py:274-280
            └─ 其它:  platform.send_by_session(session, chain)    adapters/astrbot/platform_sender.py:230
```

要点：

- 主动消息**不依赖** `event.send`；走 `send_private_msg` / `send_by_session`。
- `OneBotTextSender` 有**精确类型**检查：`type(api) is self.reverse_api_type`
  且 `api._api_clients is client._wsr_api_clients` 且该账号在线
  （`adapters/astrbot/onebot.py:70-74`、`:134-137`）。`reverse_api_type` 来自
  `astrbot_host/native.py:13,269` 的 `aiocqhttp.api_impl.WebSocketReverseApi`（与库同对象）。
- QQ 侧频控/风控：`proactive/platform_circuit.py`（`FAILURE_CODE="qq_eventchecker_1200"`、
  `THRESHOLD=2`、`COOLDOWN=2h`），事件检查器拒发在 `onebot.py:25-34` 识别。
  生产该表 **0 行** ⇒ 从没触发过（因为一次主动消息都没发出去）。

---

## 3. 宿主实现对照表

| 官方调用 | 本仓库宿主实现 | 实际行为 | 影响 |
|---|---|---|---|
| `context.platform_manager.get_insts()` / `.platform_insts` | `host/astrbot_host/platform_manager.py:299-330` `OurPlatformManager`（两者都有，返回同一列表） | **真实实现** | OK |
| `platform.meta().id/name` | `platform_manager.py:174-175` ← `umo.build_platform_metadata` | **真实**（单一定义源） | OK |
| `Platform.status == RUNNING` | `platform_manager.py:172` | 构造时即 `RUNNING` | OK |
| `platform.send_by_session(session, chain)` | `platform_manager.py:204-292` | **真实**：`sender`(OneBot)→`delivery`(面板)→否则**如实 False**，失败记 ERROR/WARNING | OK |
| `platform.get_client()` | `platform_manager.py:177-203` → `adapters/runtime.py:176-186` | 真实返回 `adapter._bot`（`CQHttp`），未连接时 `None`；插件据此判 `host_account_unavailable`（如实） | OK |
| `client._api._wsr_api` / `_wsr_api_clients` | aiocqhttp 库自身（`CQHttp._configure`，`api_impl.WebSocketReverseApi`） | **真实**（生产 venv 可 import；`_api_clients is _wsr_api_clients` 成立） | OK |
| `api.call_action("send_private_msg", user_id=…, message=[…])` | `adapters/onebot.py:712-750` 用 `aiocqhttp` 的反向 WS 直发 | **真实**（`astrbot_host/adapters/onebot.py:733-746`） | OK |
| 定时/后台循环 | `Application._drive`（插件内部）+ 宿主仅负责启动 `host.start()` | **真实**（插件自带调度器，宿主不需要外部 cron） | OK |
| 持久化（proactive_* 表） | 插件自己的 SQLite `data/plugins/.../companion-7/installations/main/runtime/companion-v2.sqlite3` | **真实**（141 张表，schema `companion-v2.storage.v66`） | OK |
| `context.send_message(session, chain)` | `compat/core/star/context.py:433-446` → `SessionDelivery.send` `emotion_app/web/delivery.py:148-236` | 真实写进我们自己的会话存储；空链/无仓库时**如实 False** | 只写面板，不推 QQ（设计如此） |
| **`@filter.on_agent_begin`** | `registry.py:41` + `star_handler.py:444` 有声明；**无任何 `call_event_hook` 派发点** | **永不触发** | 🔴 见下 |
| **`@filter.on_agent_done`** | `registry.py:42` + `star_handler.py:445`；**无派发点** | **永不触发** | 🔴 见下 |
| **`@filter.on_using_llm_tool` / `on_llm_tool_respond`** | `registry.py:45-46` + `star_handler.py:449-452`；**无派发点** | **永不触发** | 工具用量/收尾观察缺失 |

### 3.1 为什么 `completed` 永远不出现（关键因果，逐行）

1. 请求进入时插件先写一次 `_observe_core_phase(prepared, "running")`
   （`main.py:1054`）⇒ `NativeConversationTurns.mark(..., "running")`，turn 变 `running`。
2. 发送成功时 `NativeConversationTurns.sent(...)`（`native_turns.py:143-149`）里的推进语句是：

   ```sql
   generation_state = CASE WHEN generation_state = 'prepared' THEN 'completed'
                           ELSE generation_state END
   ```

   ⇒ 由于第 1 步已把它推成 `running`，**这条 CASE 永远不命中**，turn 停在 `running`。
3. 唯一能把它写成 `completed` 的是 `_observe_core_phase(prepared, "completed")`
   （`main.py:955/994/1106`），其中私有聊天的正常路径是
   `@filter.on_agent_done()`（`main.py:1097-1106`）。
4. **宿主不派发 `OnAgentDoneEvent`** ⇒ 第 3 步永不执行 ⇒ turn 永停 `running`
   （或进程重启时被 `interrupt_open()` 改成 `failed`，见 `native_turns.py:99-105`
   与 `multi_session.py:584-587`）。
5. `proactive/scheduling.py:1490` 只取 `completed` ⇒ **候选集恒空**。

生产 DB 与此**逐点吻合**：11 行 = 9 `failed`（每次重启被 `interrupt_open` 落成 failed）
+ 2 `running`（最后两次对话，其中 rowid 11 的 `last_sent_at` 有值 = 真的送达过，
但状态仍是 `running`）。

宿主**确实会**派发的钩子（全仓 `call_event_hook` 调用点穷举）：

- `host/astrbot_host/llm_hooks.py:126` `OnLLMRequestEvent`
- `host/astrbot_host/llm_hooks.py:139` `OnWaitingLLMRequestEvent`（**函数存在但无调用者**）
- `host/astrbot_host/llm_hooks.py:154` `OnLLMResponseEvent`
- `host/astrbot_host/dispatch.py:463` `OnPluginErrorEvent`
- `emotion_app/web/default_llm_reply.py:337` `OnDecoratingResultEvent`
- `emotion_app/web/default_llm_reply.py:347` `OnAfterMessageSentEvent`

⇒ 缺 `OnAgentBeginEvent` / `OnAgentDoneEvent` / `OnUsingLLMToolEvent` / `OnLLMToolRespondEvent`。

---

## 4. 线上真实证据（只读）

### 4.1 日志（`/root/apps/情绪app/logs/serve.out`，4091 行）

- `:1497` `[CompanionV2] AstrBot 核心回复阶段结束 … send_attempted=True send_state=returned`
- `:3948` 同上（`13:02:21`）
- `:1498` `核心 LLM 已回复并投递（umo=onebot-napcat1:FriendMessage:3412642082，28 字符）`
- `:3912` `ERROR 核心 LLM 回复或投递失败：UpstreamError: 上游返回 400 … unsupported content part: image_url`
- `:4066` `插件路由未命中 {path: "…/acquaintance/identity", method: "GET"}`（页面按 POST 路由名发了 GET）
- 全文**没有**任何 `proactive`/`automatic_contact`/`send_by_session` 的主动消息日志；
  `grep -c "proactive"` = 18，**全部**是启动时的路由登记行（`插件登记 Web API：…/setup/proactive-test`）。

⇒ **被动回复确曾真实送达**（这证明 `send_by_session`+OneBot 通道是通的）；
⇒ **主动消息一次都没发生**（不是因为发送失败，而是根本没产生候选）。

### 4.2 数据库（`companion-v2.sqlite3`，`mode=ro` 只读打开）

| 表 | 行数 | 关键值 |
|---|---|---|
| `conversation_native_turns` | 11 | **`generation_state='completed'` = 0**；9 `failed` / 2 `running`；rowid 9、11 的 `last_sent_at` 有值 |
| `proactive_contacts` | 0 | —— |
| `background_deliveries` | 0 | —— |
| `background_delivery_receipts` | 0 | —— |
| `agenda_dispatches` | 0 | —— |
| `proactive_platform_circuits` | 0 | 从未触发 QQ 风控熔断 |
| `proactive_schedule_diagnostics` | 1 | `stage=scheduler outcome=running reason=contact_schedule_running`，`observed_at=2026-10-30T05:21:58Z` |
| `execution_tasks` | 1 | `memory.reflect` `failed`（10-29）——**没有任何 contact-task** |
| `conversation_native_sends` | 3 | 发送观察有记录 |
| `identity_grants` | 16 | **无 `proactive.contact`** |
| `subject_access_tiers` | 1 | `dashboard:394151a1…` = `core`（操作员，不是 QQ 用户） |
| `configuration_targets` | 1 | `layer=role` `role-3979182137` |
| `configuration_values` | 1 | 仅 `conversation.presentation.enable_humanized_expression` |
| `configuration_consumers` | 3 | 含 `proactive.contact generation=18`（消费者已注册） |

`proactive_schedule_diagnostics` 只有 `coordinator` 那一行 ⇒ 调度器**在跑**，
但**没有任何窗口级诊断行**（`_note_window` 一次都没写过）⇒ 说明 `_begin_tick()` 返回的
候选集为空，`_plan` 从未被调用（否则至少会写一行 `skipped`）。

### 4.3 生产只读 HTTP 探针（GET only，未打印 token）

| 请求 | 返回 |
|---|---|
| `GET /health` | `200`，`status=ok`，`storage.ok=true`，`chat_ready=true` |
| `GET /api/v1/status` | `200`，`token_configured=true`，`token_length=43` |
| `GET …/astrbot_plugin_private_companion/host/status` | `200`，`state=running accepting=true mode=multi`，`installation_id=astrbot-private-companion-v2:multi:main`，target `onebot-napcat1/bot-3979182137/participant=3412642082`，`delivery_mode=onebot_private` |
| `GET …/acquaintance` | `200`，`counts.total=1`，`persons[0].identity_tier=standard`，`page_managed_capabilities=[conversation.reply, proactive.contact, role.state.intervene, calendar.intervene, role.rest.wake]` |
| `GET …/acquaintance?read_mode=bogus` | `200 status=ok`（**应为** `acquaintance_request_invalid`） ⇒ query 未生效 |
| `GET …/acquaintance?read_mode=stored` | `200`，`directory_refresh_pending=false`（同上，说明不是 `stored` 分支） |
| `GET …/acquaintance/authorization?subject_id=…&platform=…` | `{"status":"error","error":{"code":"acquaintance_request_invalid"}}` |
| `GET …/acquaintance/settings?subject_id=…` | 同上 `acquaintance_request_invalid` |

⚠️ 未调用 `/integration/manifest`（会经 `dashboard_operator` 首次创建身份，见
`management_identity.py:23-46`）。已确认 `subject_access_tiers` 只有既有那条，未新增。

---

## 5. 判定

**「设为核心用户之后，主动消息就能投递」——不能。**

理由链（每步都有证据）：

1. 设成 core 只能（a）让页面 `/authorization` 读成功、按钮可点（前提是先部署 query 修复）；
   （b）让该用户绕过 `proactive.contact` 的 grant 要求（§1）。
2. 但主动消息需要**候选**，候选来自 `proactive/scheduling.py:1490` 的
   `generation_state='completed'` native turn，生产**一个都没有**。
3. 而 turn 无法变 completed，因为宿主不派发 `OnAgentDoneEvent`
   （插件 `main.py:1097-1106` 的标记点永不执行），`phase()` 只在
   `code_reply_observations.py:307-313` 被调用。
4. 即便候选存在，还需 `proactive.contact.enabled` 与 `automatic_enabled` 为 True，
   而生产窗口级配置**一行都没有**（默认 False）。

### 独立阻塞点清单（按严重程度）

| # | 阻塞点 | 证据 | 严重度 | 归属 |
|---|---|---|---|---|
| 1 | 生产未部署 `compat/api/web.py` query 修复 ⇒ 权限读取恒失败、按钮恒 disabled | 生产 md5/mtime + `?read_mode=bogus` 返回 200 | **P0** | 部署（主代理已修） |
| 2 | 宿主不派发 `OnAgentBegin/Done`、`OnUsingLLMTool/Respond` ⇒ native turn 永不 completed ⇒ 主动消息候选集恒空 | `registry.py:41-46` 有枚举、无派发点；DB completed=0；诊断只有 coordinator 行 | **P0** | 宿主兼容层缺口 |
| 3 | `proactive.contact` 需要按窗口 grant（STANDARD 档） | `object_access.py:59,268-277`；DB 无该 grant | **P1**（设 core 可绕过；否则需页面授权） | 插件策略 + 运营 |
| 4 | `proactive.contact.enabled`/`automatic_enabled` 默认 False，生产无窗口级配置 | `proactive/configuration.py:228,287`；`configuration_values` 仅 1 行 | **P1** | 运营配置（非代码） |
| 5 | QQ 主动消息有 eventchecker 频控（1200），2 次拒发 → 2 小时账户级冷却 | `proactive/platform_circuit.py:10-13`、`onebot.py:25-34` | **P2**（首次真发前不触发） | 插件策略 + NapCat 侧 |
| 6 | 主动消息审批有人格判定开关（默认关）与预算/静默期/去重多重闸门 | `configuration.py:228+`、`rhythm.py`、`budget.py` | P2 | 插件策略（预期行为） |
| 7 | 页面对 `/acquaintance/identity` 用 GET 而路由只登记 POST | `serve.out:4066` + `acquaintance_api.py:308-310` | P3（页面不靠它保存） | 页面/宿主（不影响保存） |

---

## 6. 最小修复清单（**仅列，未改**）

### A. 宿主兼容层缺口

**A1. 派发 Agent 生命周期钩子（P0，本条最关键）**

- 根因：`EventType` 有枚举、装饰器有注册，但**没有调用点**；插件用
  `@filter.on_agent_done()` 标记 native turn 完成（`main.py:1097-1106`），
  `_observe_core_phase` 才可能把 `generation_state` 推进到 `completed`。
- 改哪一层：`emotion_app/web/llm_turn.py`（已有 `response_hook`）+ `emotion_app/web/default_llm_reply.py`
  （已有 decorating/after_sent 钩子）。建议在**同一事件的**这三点派发：
  - 模型请求前 → `call_event_hook(event, EventType.OnAgentBeginEvent, run_context)`
  - 模型回复后（`response_hook` 末尾）→ `OnAgentDoneEvent`（带 `run_context, response`）
  - 每个工具执行前后 → `OnUsingLLMToolEvent(tool, args)` / `OnLLMToolRespondEvent(tool, args, result)`
- 风险：`run_context` 形状需与真实现对齐（插件 `observe_core_agent_begin/done` 只把它透传给
  `core_reply_telemetry` 与 `restore_core_tool_dispatch`，不会构造模型调用）；钩子抛错必须按
  `_call_event_hook` 的约定记 ERROR 并继续（不能炸整轮）。
- 验证判据：跑一轮真实 QQ 被动回复后，
  `SELECT generation_state, count(*) FROM conversation_native_turns GROUP BY 1` 必须出现 `completed`；
  且 `proactive_schedule_diagnostics` 出现 `window` 级行（哪怕 `skipped`）。
  ⚠️ **不要只断言"钩子被调用"** —— 要断言 DB 里的 `completed` 真的出现。

**A2. 让 `run_waiting_llm_request_hooks` 真的被调用（P2）**

- 现状：`llm_hooks.py:132-139` 定义并导出，**全仓无调用者**（真实现 `internal.py:217` 会调）。
- 影响：`meme_manager` 等用它的插件拿到的是"永不通知"（无报错）。对主动消息无直接影响。
- 改哪一层：`emotion_app/web/plugin_host.py` 取会话锁之前。
- 验证判据：注入一个记录型钩子，确认它在该钩子上收到一次调用。

**A3. 部署 `compat/api/web.py` 的 query 修复（P0，纯部署）**

- 根因：生产仍读 `request_.query_params`（Quart 无此属性）⇒ `request.query` 恒空。
- 改哪一层：无需改代码，**部署**本机 `host/astrbot_host/compat/api/web.py`
  （`_query_pairs` `:113-146`、`PluginMultiDict.__getitem__` `:51-66`、`__bool__` `:71-73`）。
  ⚠️ 三处**必须一起上**：只修 `_query_pairs` 而缺 `__getitem__`，`acquaintance_api._route_query`
  的 `query[key]` 仍会 `TypeError`。
- 风险：属既有 20 文件部署面之外的新文件；需先备份、单独校验、重启一次。
- 验证判据：`GET /acquaintance?read_mode=bogus` **必须**返回 `acquaintance_request_invalid`
  （现在返回 200/ok）；`GET /acquaintance/authorization?subject_id=…` 返回 `status=ok` 且
  含 `identity_tier`。

### B. 插件自身策略/前置（不是宿主缺口）

**B1. 主动消息总开关需按窗口开启（P1，运营动作）**

- 根因：`enabled`/`automatic_enabled` 默认 False；`config` 层要求 `layer=window + subject + 私聊 scope`
  （`proactive/configuration.py:412-417`）。
- 改哪一层：**不改代码**，在「关系与权限/成员配置」里写一次 contact 分区。
- 验证判据：`configuration_values` 出现 `proactive.contact.*` 且 `configuration_targets` 出现
  `layer=window` 那条。

**B2. `proactive.contact` grant（P1）**

- 两条路：① 设核心用户（IMPORTANT/CORE 免 grant，`object_access.py:275-277`）；
  ② 走页面「窗口权限」显式开启（写 `acquaintance-grant:` 前缀的 grant）。
- 验证判据：`identity_grants` 出现 `capability='proactive.contact'` 且该 scope 精确匹配；
  或 `subject_access_tiers` 该 `onebot-user:3412642082` 变成 `core`/`important`。

**B3. 不要为了"跑通"关掉人格判定/预算/静默期（P2 反建议）**

- 这些是插件设计上的多闸门（`rhythm.py`、`budget.py`、`persona_judge`）。放宽它们等于
  绕过作者策略，且会让"为什么发了"不可解释。

---

## 7. 未覆盖风险

1. **没有发过真实主动消息**，因此 `send_private_msg` 在"非回复态"下的 QQ 行为未知：
   NapCat 可能在无上下文时拒发（eventchecker 1200），届时才会首次写入
   `proactive_platform_circuits`。**这是真正的未知数，不能从被动回复成功推断。**
2. **`_busy_hold`/`_sleep_hold` 依赖窗口级 calendar/rest 配置**，生产这两块也没有配置
   （`configuration_values` 仅 1 行）⇒ 修好前三点后，可能仍被这些 gate 判 `skipped`。
   届时诊断表会给出明确 `reason_code`（`conversation_busy` / `role_sleeping`），**可据此继续**。
3. **`agenda.*`（日程/备忘录提醒）链路**本次未验证：`agenda_dispatches`=0、`agenda_memos` 未查内容。
   它的候选来源与主动消息不同，需单独审计。
4. ~~A1 修复后 `core_reply_observations` 可能不存在~~ —— **此条已核实为误判，撤回**。
   完整链：`main.self.staging`（`main.py:150` = `AstrBotMultiController`）→ `.session`
   （`multi_controller.py:610` = `MultiHostSession`）→ `.core_reply_observations`
   （`multi_session.py:496-500` 构造 `CoreReplyObservations(self)`）。
   该对象在**插件自带的 `astrbot_host/` 包内**（不在我们的 `host/astrbot_host/`，
   所以我 `grep host/ emotion_app/` 无命中，属于**查错位置**）。属性存在。
   ⚠️ 仍需注意：`multi_session.py:498-500` 的构造包在 `try/except` 里，
   初始化失败只记一行 `[CompanionV2] 原生聊天观察初始化降级 error_type=…` WARNING
   ⇒ 那种情况下 `observer=None`，钩子派发了也不会推进 turn。**这属于"需在 A1 判据里一并看日志"的项，
   不是独立阻塞点。**
5. 本报告的"宿主侧 send 链路完整"结论基于**本机** `host/astrbot_host/**` 与生产 md5 全等的
   7 个关键文件（`platform_manager.py`/`llm_hooks.py`/`dispatch.py`/`adapters/runtime.py`/
   `adapters/onebot.py`/`default_llm_reply.py`/`llm_turn.py`/`plugin_host.py` 逐一核对相同），
   但 `compat/api/web.py` 生产**不同**（见 §0-1）。

---

## 附：复现命令（只读，可重跑）

```bash
# 1) 生产 query 修复是否生效（期望：acquaintance_request_invalid）
ssh -i /root/.ssh/id_ed25519_jpvps root@64.81.114.30 'cd /root/apps/情绪app && bash -c "
  set -a; source .env; set +a; T=\$EMOTION_AUTH_ACCESS_TOKEN
  curl -s -H \"Authorization: Bearer \$T\" \
    http://127.0.0.1:6191/api/plugins/extensions/astrbot_plugin_private_companion/acquaintance?read_mode=bogus"' 

# 2) 候选集为空（期望：completed 0 行）
ssh -i /root/.ssh/id_ed25519_jpvps root@64.81.114.30 'cd /root/apps/情绪app && python3 -c "
import sqlite3
p=\"data/plugins/astrbot_plugin_private_companion/companion-7/installations/main/runtime/companion-v2.sqlite3\"
c=sqlite3.connect(\"file:%s?mode=ro\"%p,uri=True)
print(c.execute(\"select generation_state,count(*) from conversation_native_turns group by 1\").fetchall())
print(c.execute(\"select count(*) from proactive_contacts\").fetchall())"'

# 3) 宿主是否派发 Agent 钩子（期望：只列出 request/response/decorating/after_sent/plugin_error）
grep -rn "call_event_hook(" --include=*.py host/ emotion_app/ | grep -v tests
