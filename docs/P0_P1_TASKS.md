# P0/P1 任务清单（实测版·不是估算）

> 2026-09-20 建立。**所有数字都是本轮实测出来的**，不是估的。
> 复现命令见每节末尾。权威背景见 `NEXT_STEPS.md`。

---

## 0. 实测基线（动手前的账）

| 项 | 数字 | 怎么来的 |
|---|---:|---|
| 磁盘插件 | 12 | `/opt/astrbot/data/plugins` |
| **加载成功** | **11** | `meme_manager` 缺 `aiohttp`/`requests`（**它自己的依赖**，非我们缺口） |
| 累计 handler | **203** | `star_handlers_registry.count_by_event()` |
| 主插件 `private_companion` | 109 handler / 190 API | 同上 |
| **`AdapterMessageEvent`** | **82** | 派发目标总量 |
| `OnCallingFuncToolEvent` | 33 | 需要 function-calling 闭环才能跑 |
| `OnDecoratingResultEvent` | 32 | **紧挨着 P0-3**（发送前装饰） |
| `OnLLMRequestEvent` | 24 | P0-4 |
| 单测基线 | **主 440 + host 35** | `pytest --collect-only -q` |

### 复现（⚠️ 一律走镜像 + 临时目录，绝不碰 `/opt/astrbot`）

```bash
cd /root/Ai-works/情绪app
.venv/bin/python - "$(mktemp -d)" <<'EOF'
import asyncio, sys
sys.path.insert(0, "/root/Ai-works/情绪app/host")
from astrbot_host.compat.core.star.context import HostPorts
from astrbot_host.loader import PluginLoader
from astrbot_host.shadow import install_shadow_package
from astrbot_host.registry import star_handlers_registry, EventType
install_shadow_package()
loader = PluginLoader(plugin_dirs=["/opt/astrbot/data/plugins"], mirror_root=sys.argv[1])
rep = asyncio.run(loader.load_all(ports=HostPorts(config={})))
print("成功:", sum(1 for r in rep.records if not r.error), "失败:", [r.name for r in rep.records if r.error])
print("by_event:", star_handlers_registry.count_by_event())
for h in star_handlers_registry.get_handlers_by_event_type(EventType.AdapterMessageEvent):
    print(sorted(type(f).__name__ for f in (h.event_filters or [])), h.handler_name)
EOF
```

---

## 1. ✅ 已修 6 个**兼容层真 bug**（2026-09-20，全部带能失败的判据）

前四个是"扫 82 个派发目标"时扫出来的；**BUG-5/6 是做完派发器后、在真服务上验证时才暴露的**（都属 P0 路径，不修派发就跑不起来）。

| # | Bug | 状态 | 判据 |
|---|---|---|---|
| BUG-1 | 子指令没挂 `CommandFilter` | ✅ 已修 | 单测 2 条 + 差分新段（见下） |
| BUG-2 | `is_stopped` 是字段，真实现是方法 | ✅ 已修 | 单测 2 条 |
| BUG-3 | `plain_result` 不该写 `_result` | ✅ 已修 | 单测 2 条 |
| **BUG-4** | **`main.py` 被执行两次 → 过滤器重复** | ✅ 已修 | 单测 2 条（1 条用真插件数据） |
| **BUG-5** | 注册表缺 `__iter__`/`__len__`/`get_handlers_by_module_name` | ✅ 已修 | 单测 2 条 |
| **BUG-6** | **handler 没绑定插件实例 → 派发时 `self` 被当成 event** | ✅ 已修 | 单测 1 条 |

> 🔴 **六个 bug 都做了「注入 bug 验证」**（把修复改回去 → 判据必须挂）。
> 实测：BUG-1 挂 2 条单测 + 差分 49/50；BUG-2/3 各挂 1 条；BUG-4 挂 2 条；BUG-5 挂 1~2 条；BUG-6 挂 1 条。**全部确认会失败。**
>
> ⚠️ **BUG-5/BUG-6 是「在真服务上验证」才发现的**，不是读源码读出来的：
> BUG-5 来自启动日志里一行 `'StarHandlerRegistry' object is not iterable`；
> BUG-6 来自「派发时该传 event 还是传实例」的追问。
> **通则：兼容层的缺口靠读源码会漏 —— 必须真的把插件跑起来看日志/看行为。**

### BUG-4 是修 BUG-1 时才暴露出来的（附带收获）

`loader/plugin_loader.py:_import_plugin` 里，插件的 `__init__.py` 普遍写
`from .main import XxxPlugin` —— 所以 `import_module(包名)` 时 **`main.py` 已经执行过一次**；
而代码紧接着看到 `"<包>.main" in sys.modules` 就 `importlib.reload(...)` → **第二次执行**。

* **后果**：类体跑两遍 → 所有装饰器挂两遍 → handler 的 `event_filters` 出现**完全相同的重复项**。
  实测 **`memory_companion` 有 23 个 handler 中招**（`CommandFilter` + `PermissionTypeFilter` 各两份）。
* **为什么难发现**：`handler_full_name` 只有一个（注册表按名字幂等），
  所以"重复"在报告里**看不出来**，只会在派发时表现为"同一个 handler 被激活多次"。
* ✅ **修法**：首次 import **不 reload**（`main.py` 刚被 `__init__.py` 跑过，直接用）；
  只有包**本来就在** `sys.modules` 里（= 同进程重复 `load_all`）才 reload。

### 修完后的实测对比（真插件，11 个）

| 指标 | 修前 | 修后 |
|---|---:|---:|
| 零过滤器的 handler | **16**（永不执行） | **0** |
| 挂重复过滤器的 handler | **23** | **0** |
| 带 `CommandFilter` 的 handler | 26 | **65** |
| `CommandFilter` 对象总数 | 88（含重复） | **65** |
| handler 总数 | 203 | 203（不变 ✓） |
| 加载成功 | 11/12 | 11/12（不变 ✓） |

> ⚠️ `CommandFilter` 总数 **88 → 65** 是**变少**，但这是对的：
> 原来那 88 里有 23 个是**重复挂的**。现在 65 = 每个带指令的 handler 恰好一个。
> 这也是"自洽性判据"（分项和 == 总数）能抓问题的又一例。

### BUG-1 子指令没挂 `CommandFilter`（我们早退了一行）

* **真实现**（`core/star/register/star_handler.py:116-124`）：子指令分支**末尾同样**
  `new_command.init_handler_md(handler_md)` + `handler_md.event_filters.append(new_command)`。
* **我们**（`compat/.../register/star_handler.py:245-260`）：子指令分支 `return awaitable` **提前返回**，
  只把过滤器挂进了父指令组，**没挂到 handler 上**。
* **后果**：`prompt_tools` 的 **16 个子指令**（`prompt presets` / `prompt view` …）
  变成 **`event_filters == []`**。而真实现的派发语义是
  （`waking_check/stage.py:182-183`）：
  ```python
  if len(handler.event_filters) == 0:
      continue          # ← 无过滤器的 handler 被【跳过】，不是"每条都触发"
  ```
  → **这 16 个指令会永远不执行**。实测过滤器实况（`prompt_tools`）：

  ```
  prompt_command_group   sub=None  filters=['CommandGroupFilter'] ['prompt']
  list_presets           sub=True  filters=[]     ← 本该是 ['prompt presets']
  view_prompt            sub=True  filters=[]     ← 本该是 ['prompt view']
  … 共 16 个
  ```
  ⚠️ 顺带说明：**指令组的父 handler 不进 `activated_handlers`**（真实现 `:226-230`
  有 `is_group_cmd_handler` 判断，带 `CommandGroupFilter` 的只用于帮助树）；
  **真正执行的是子指令 handler**，所以它们的 `CommandFilter` 是必需的。
* **判据**：`prompt presets` 对应 handler 的 `event_filters` 必须含 `CommandFilter`，
  且 `get_complete_command_names() == ['prompt presets']`。
* **修后预期**：`CommandFilter` 计数 **26 → 42**（+16）。

### BUG-2 `is_stopped` 是字段，真实现是**方法**

* **真实现**（`astr_message_event.py:359`）：`def is_stopped(self) -> bool`
  —— 且语义是 **`_force_stopped` or `_result.is_stopped()`**（`_force_stopped` 独立于 `_result`，
  **不会被 `clear_result()` 重置**，注释原话）。
* **我们**：`self._stopped: bool` 字段 + `is_stopped()` 方法混用。
* **后果**：插件写 `if event.is_stopped:`（漏括号）永远为真 → **静默吞掉所有消息**；
  且 `clear_result()` 语义与真实现不符。
* **判据**：`clear_result()` 后 `is_stopped()` 必须仍为 True（若曾 `stop_event()`）。

### BUG-3 `plain_result` 不该写 `_result`

* **真实现**（`:399`）：`return MessageEventResult().message(text)` —— **纯构造，不赋值**。
* **我们**：同时 `self._result = ...`。
* **后果**：插件常见写法 `yield event.plain_result("x")` 会把结果**留在 event 上**，
  而真实现是靠 `call_handler`（`context_utils.py:71-73`）在 yield 时才 `set_result`。
  我们提前写 → **`clear_result()` 与"上一个 handler 的结果"语义错位**（真实现 `star_request.py:53`
  每轮 handler 后 `event.clear_result()`）。
* **判据**：`plain_result()` 调用后 `event.get_result()` 必须仍是 `None`。

---

## 1.5 ✅ P0-2 / P0-3 已通：**真插件真的在聊天里回话了**（2026-09-20）

新增 `host/astrbot_host/dispatch.py`（两阶段派发器，照真实现逐条移植）：

| 阶段 | 真实现 | 我们 |
|---|---|---|
| A 选 handler | `pipeline/waking_check/stage.py:100-248` | `apply_wake()` + `select_handlers()` |
| B 执行 | `pipeline/process_stage/method/star_request.py:23-74` | `dispatch()` |
| 调用契约 | `pipeline/context_utils.py:12-75` | `call_handler()` |

### 🎯 端到端实测（真插件、真派发、真输出）

```
--- 'mcomp status' ---
  activated: 8  executed: 8  errors: 0
  emitted: ['我会牢牢记住你：运行中\n版本：2.1.3\n记忆：0 条，稳定 0 条\n…']

--- 'prompt list' ---
  activated: 8  executed: 8  errors: 0
  emitted: ['⚠️ 当前未选择预设，请使用 `/prompt use <索引>` 选择一个预设']

--- 'imgstatus' ---
  activated: 8  executed: 8  errors: 0
  emitted: ['正在检查生图服务...', '本地代理 127.0.0.1:8765: ❌ 离线 …']
```

> ⭐ 这是"**插件能在聊天里回话**"的达成点 —— P0-2 + P0-3 完成。

### 🔴 三个必须照抄、否则必错的语义（都实测踩过）

1. **洋葱模型：结果在 yield 点被消费**
   真实现 `scheduler.py:36-80` 是**递归**的 —— `star_request` 每 yield 一次，
   调度器就在那个点跑后续阶段（`respond` 读 `get_result()` 发出去），
   回来才 `clear_result()`。
   ⚠️ 我第一版在循环**结束后**才取 `get_result()` → **必然是 None**（实测）。
   → 改为**在 yield 点收集**（`DispatchResult.emitted`），投递层消费。

2. **`is_stopped` 不影响 `allow_default_llm` 公式**
   真公式只有三项（`process_stage/stage.py:56-60`）：
   `not _has_send_oper and is_at_or_wake_command and not call_llm`。
   "被终止就不再继续"是**调度器**保证的（`scheduler.py:56-60` 每 yield 查一次）。
   ⚠️ 我一度想往公式里塞 `is_stopped` —— 那是**改语义迎合直觉**，已纠正。

3. **私聊唤醒看的是 `friend_message_needs_wake_prefix`（默认 False），不是"前缀列表空不空"**
   ⚠️ 我第一版写成 `not wake_prefixes` → 错。真配置项在
   `core/config/default.py:96`，默认 `False` ⇒ **私聊无条件唤醒**
   （这正是 AstrBot 网页聊天不用打前缀的原因）。

### 判据（`host/tests/test_dispatch.py`，11 条）

覆盖：指令命中并执⾏ / 参数解析传入 / `stop_event` 阻断 / 不匹配不执行 /
裸字符串**不**自动包装 / `_has_send_oper` 置位 / 异常**响亮**且回给用户 /
未唤醒即终止 / **唤醒前缀被切掉** / `activated_handlers` 写回 event /
**零过滤器 handler 被跳过**。

**注入 bug 验证（都确认会挂）**：
* 「假派发」（只选出不调用）→ **7 条挂**；
* 「前缀不切」→ 1 条挂；
* 「零过滤器不跳过」→ **原本 10 条全绿**，补了第 11 条才抓住 ⚠️
  （说明前 10 条盖不住这个语义 —— **判据缺口是靠注入 bug 找出来的**）。

---

## 1.6 ✅ P1 已通：umo 约定 + platform_manager + 投递（2026-09-20）

### 交付物

| 文件 | 内容 |
|---|---|
| `host/astrbot_host/umo.py`（515 行） | **umo 的唯一权威定义** + 入站事件工厂 |
| `host/astrbot_host/platform_manager.py` | `OurPlatformManager` + `OurPlatform`（含 `send_by_session`） |
| `host/astrbot_host/compat/core/star/context.py` | 补 `platform_manager` 属性；`get_platform_inst()` 不再硬编码 None |
| `emotion_app/web/plugin_host.py` | `plugin_reply_text()`（集成判定规则）+ `dispatch_message()` |
| `emotion_app/web/app.py` | `/v1/chat/completions` 接入「插件优先」 |
| `emotion_app/infra/config.py` | `PLUGINS_UMO_USER_ID`（`session_id` 段可配置） |

### umo 约定（**已固化**）

```
11:FriendMessage:<用户ID>          # 私聊
11:GroupMessage:<群ID>             # 群聊
```
* 第一段 `11` = **平台实例 id**（情绪拍板：照抄现有 AstrBot 连接）；
* 与生产 `companions.json` 里的 `11:FriendMessage:3412642082` **逐字同构**；
* `session_id` 段由 `PLUGINS_UMO_USER_ID` 配置（默认 `default`），
  ⚠️ **它是插件的数据主键 —— 改了等于换库**；
* `build_umo()` **委托真实现的 `str(MessageSession(...))`**，不手写 f-string
  → 格式永远由真实现那段代码决定，不会分叉。

### 三条硬约束的落实

| 约束 | 落实方式 | 判据 |
|---|---|---|
| **绝不冒充 `aiocqhttp`** | `meta().name = "webchat"`（插件把它当一等公民） | 显式断言 `!= "aiocqhttp"` |
| **绝不伪造 delivered** | 端口未注入 → `send_by_session` 返回**字面 `False`** | 注入"返回 True" → 判据挂 ✅ |
| **平台 id 与 umo 必须一致** | 两处**同源**（`platform_manager` 从 `umo.py` 取常量） | 改错 id → 挂 4 条 ✅ |

### 🎯 真 HTTP 端到端实测（服务重启后）

```
$ curl -X POST /v1/chat/completions -d '{"messages":[{"role":"user","content":"mcomp status"}]}'
  content: '我会牢牢记住你：运行中\n版本：2.1.3\n记忆：0 条，稳定 0 条…'
  plugin_dispatch: {'errors': [], 'executed': 8, 'stopped': False}

$ ... "prompt list"
  content: '⚠️ 当前未选择预设，请使用 `/prompt use <索引>` 选择一个预设'

$ ... "陪伴"                      # 接管型指令
  content: '（该指令已由插件接管处理）'
  plugin_dispatch: {'errors': [], 'executed': 7, 'stopped': True}

$ ... "你好呀"                    # 普通聊天 → 正确回落 LLM
  error: 上游返回 503                # ← 上游的事；说明回落路径走对了
```

### 主动消息链路（用**插件自己的方法**逐步验证）

```
插件解析 umo        → 11:FriendMessage:3412642082
插件 _get_platform_for_session → <OurPlatform>
status == RUNNING   → True
插件 _session_for_platform     → 11:FriendMessage:3412642082
send_by_session     → True
端口实际收到        → [('11:FriendMessage:3412642082', '…')]
```
即 `proactive_message.py:17602-17626` 那条精确发送链**每一步都走通**。

### ⚠️ 一个**如实记录的未完成项**

`proactive_chat_runtime_bridge.py` 的 `attach()` 返回 False，
原因是它要找的是**另一个插件**（`astrbot_plugin_proactive_chat`）的实例，
而那个插件**没装**：
```
_last_error: 缺少兼容方法: check_and_chat, _prepare_llm_request, _generate_llm_response,
             _send_proactive_message, _send_chain_with_hooks, _finalize_and_reschedule
```
→ **不是我们的兼容缺口**（它在诚实报告"目标插件不在"）。
   主线 `proactive_message.py` 那条链**已独立验证通过**（见上）。
   面板的"深度联动"功能要等那个插件装上才能验。

### 判据增量

| 文件 | 条数 |
|---|---:|
| `host/tests/test_umo.py` | 22 |
| `host/tests/test_platform_manager.py` | 11 |
| `tests/test_plugin_chat_integration.py` | 7 |

**注入 bug 验证**：平台 id 改错 → 挂 7 条（umo）+ 4 条（platform）；
`get_insts` 返回副本 → 挂 1 条；伪造 delivered → 挂 1 条；
umo 常量重复定义 → **被 `test_umo_format_is_defined_in_exactly_one_place` 当场抓到**
（它抓的是我真实写出来的第二处定义，不是我假想的 bug）。


## 1.7 ✅ P1-4 已通：**面板绑定真实跑通**（2026-09-20）

### 绑定到底是哪一步（上一轮只查到"手动填"，这轮查实了）

面板 `app.js:11382` 的原文给出了入口：

> 「还没有检测到私聊绑定。请先在目标私聊发送 **`/陪伴 绑定主动消息`**，再点击刷新」

**两条路径必须分清**（`proactive.py` 里两个函数名字很像，作用完全不同）：

| 函数 | 触发 | 写什么 | 面板读不读 |
|---|---|---|---|
| `_note_private_user_umo` | **任意**私聊消息 | `user["umo"]`（**观察**到的） | ❌ 不读 |
| `_bind_private_delivery_umo` | **`陪伴 绑定主动消息`** | `user["bound_delivery_umo"]` | ✅ **面板 `delivery_bound` 读它** |

实测证据（全新数据目录，逐步走）：

```
只发「你好呀」          → umo='11:FriendMessage:3412642082'  bound=None
再发「陪伴 绑定主动消息」 → umo='11:FriendMessage:3412642082'  bound='11:FriendMessage:3412642082'
```

⭐ **这证明绑定链路必须靠派发器才通** —— 没有 P0-1/2/3，`绑定主动消息` 指令永远不执行。

### 🎯 真 HTTP + 真落盘 + 真浏览器（三重验证）

| 层 | 验证方式 | 结果 |
|---|---|---|
| 指令 | `POST /v1/chat/completions` 发 `陪伴 绑定主动消息` | `stopped: True`，done |
| 数据 | 面板接口 `/page/users` | `bound_delivery_umo = "11:FriendMessage:default"` |
| 落盘 | `data/plugins/astrbot_plugin_private_companion/companions.json` | 已写入 |
| **渲染** | **真 Chrome（CDP）打开插件拓展页** | **`state.users` 里有 `delivery_bound` 用户** |

真浏览器输出（`tools/panel_binding_check.py`，新增）：
```
✅ 登录成功（否则后面全是 401 假象）
✅ 插件页已加载（标题非空）
✅ 页面没有卡在「未检测到桥接」
✅ 面板 state.users 里有 delivery_bound 用户
   · 绑定用户 user_id='default' bound_umo='11:FriendMessage:default'
✅ 绑定 umo 符合我们的约定（11:FriendMessage:<id>）
结果：5/5 项通过
```
> ⚠️ 为什么必须用真浏览器：`curl` 只能证明**接口**返回了值，
> 而"面板上看得见绑定"取决于 **JS 有没有渲染出来**
> （`app.js:9882` 的 `setupGuideBoundUsers()` 过滤 `delivery_bound && user_id`）。
> 本项目踩过"接口全绿、界面空白"的坑（人设导入那次）。

### 🔴 上一轮标记的「未实测」项，现在有答案了（好消息）

> **`target_platform` 与 umo 第一段是否必须一致？**

**不必一致 —— 实测它会被自动归一：**

```
target_platform='aiocqhttp' → 实例 id = '11' → umo = '11:FriendMessage:3412642082'
target_platform='webchat'   → 实例 id = '11' → umo = '11:FriendMessage:3412642082'
target_platform='11'        → 实例 id = '11' → umo = '11:FriendMessage:3412642082'
```

原因：`_preferred_platform_instance_id()`（`platform_compat.py:229`）
会**按平台管理器反查真实实例 id** —— 我们的管理器里只有 id=`11` 的实例，
所以填什么类型名都归一到 `11`。

⭐ **但有个前提**：正是因为**我们提供了平台管理器**，这个反查才有结果。
没有它就会退化成"直接用 `target_platform` 的字面值" → **umo 才会拼错**。
→ 也就是说 **P1-2 不只是"让主动消息能发"，它还顺带保证了 umo 拼写正确**。

> ⚠️ 面板 `app.js:9889` 会把非 QQ 的平台预填成 `aiocqhttp`（硬编码）。
> 有了上面的归一化，**这不影响 umo 正确性**，所以**不必去改插件**。
> 如实记录：标签上会显示成"QQ / OneBot"，但底层 umo 与投递用的都是 `11`。

### ⚠️ 一个我差点写成"假通过"的判据

第一次给面板判据做**注入验证**时，我清掉 `bound_delivery_umo` 后判据**仍然全绿** ——
一度以为判据失效。查清后是：**插件在服务启动时会自动重新绑定**
（`message_pipeline.py:376` 的 `_note_private_user_umo`），把注入覆盖掉了。

→ 于是补了一条**更严格的判据**（`test_binding_command_is_the_only_path_that_binds`）：
用**全新数据目录**，先发普通消息（断言 `bound` 为空），再发绑定指令（断言 `bound` 写对）。
它同时守住一个**行为边界**：不许"收到消息就自动同意接收主动消息"
（主动消息会**主动打扰用户**，静默开启是严重的行为变化）。

注入验证（这次改对了位置）：让派发不执行 handler → **挂 2 条** ✅


## 1.8 ✅ P0-4 已通：LLM 钩子（**记忆注入真的生效了**）（2026-09-20）

新增 `host/astrbot_host/llm_hooks.py`（照真实现三处调用点移植）：

| 钩子 | 实测数量 | 真实现调用点 |
|---|---:|---|
| `OnLLMRequestEvent` | **24** | `agent_sub_stages/internal.py:269` |
| `OnLLMResponseEvent` | **12** | `core/astr_agent_hooks.py:31-35` |
| `OnWaitingLLMRequestEvent` | **2** | `internal.py:217` |

### 🎯 最硬的证据：注入进了**真正发出去的 HTTP body**

用 `httpx.MockTransport` 截住上游请求体实测：

```
发给上游的 messages 条数: 3
  [system]  5995 字符 | '<|SYSTEM_INJECT|>【当前时间】\n…【角色扮演动态插图系统｜Comfy…'
  [user]       6 字符 | '我今天有点累'
  [system]  1008 字符 | '<|SYSTEM_INJECT|><MemoryCompanion-Context>\n<memory_companion_context>…'
```

服务日志也能看到：`调用上游 {"messages": 3, ...}`（注入前是 1 条）。

> ⚠️ 为什么要验到 **HTTP body** 这一层：`messages` 里有了注入 ≠ 发出去了 ——
> 中间还有 `to_upstream()` 的字段映射。本项目踩过同类坑
> （`to_upstream()` 漏传 `tool_calls` → 上游 400，**单测全绿、只有真上游才现形**）。

### 接线方式（领域层不依赖插件宿主）

```
ChatService._apply_llm_hook(messages)      ← 领域层只认这个**函数协议**
        ↑ bind_llm_hook() 注入
PluginHostService.apply_llm_hooks          ← 适配层：做两套表示的转换
        ↓
astrbot_host.llm_hooks.run_llm_request_hooks(event, req)
```

* `list[Message]` → `ProviderRequest`（system 合并进 `system_prompt`，其余进 `contexts`）
* 跑钩子（**传同一个对象** —— 传副本注入就丢了）
* 转回 `list[Message]`（`system_prompt` + `contexts` + `extra_user_content_parts`）

⭐ 这样**领域层零耦合**：它只看到一个 `async (messages) -> list[Message] | None` 端口，
   插件宿主换实现（甚至关掉）都不用改领域层。

### 🔴 三条语义（照真实现，都实测过）

1. **`req` 是同一个对象，改它就是生效** —— 真实现把同一个 `ProviderRequest`
   传给所有钩子，之后直接拿它请求模型。**传副本 = 注入静默丢失**。
2. **钩子抛错只记 ERROR 并继续**（真实现 `context_utils.py:102` 是 `except BaseException`）
   —— 陪伴有 24 个 request 钩子，任何一个配置问题都不该让记忆注入整个失效。
3. **正常聊天不受影响**：未注入端口 → 直接返回；钩子抛错 → 原样返回。
   ⚠️ 但**流式与非流式都要接**（只接一条 → "有时候记得、有时候不记得"）。

### 判据（新增 21 条）

| 文件 | 条数 | 重点 |
|---|---:|---|
| `host/tests/test_llm_hooks.py` | 12 | 注入生效 / 链式叠加 / 优先级 / 终止 / 异常不中断 / 生成器钩子 / plugins_name |
| `tests/test_plugin_chat_integration.py`（新增 9 条） | +9 | 转换往返 / 注入回填 / **上游 body 级验证** / 双路径接线 / 失败降级 |

**注入 bug 验证（全部确认会挂）**：
* 钩子传副本 → **挂 4 条**；
* 转回时丢掉 `system_prompt` → **挂 3 条**；
* **流式漏接钩子** → 挂 1 条（专门守"流式没记忆"这个隐蔽缺口）。

### ⚠️ 两个我自己写错、又改回来的地方

1. **手搓桩对象**造 `ChatService` → `resolve_target` 读 `timeout_seconds` 直接
   AttributeError。→ 改用**真的 `UpstreamConfig` dataclass**。
   **通则：桩要长得像真的**，否则测的是"我的桩"而不是产品代码。
2. 忘了 `await backend.open()` → `RuntimeError: 未 open()`。


## 1.9 ✅ P2：**模型供应商（Provider）已接入**（2026-09-20）

用户要求：「把 astrbot 的"模型供应商"模块加进来，我去测试陪伴模型配置」+「加在侧边栏」。

### 为什么这个是**必需**的（不是锦上添花）

实现前实测到的真实日志：
```
WARNING [Token预算] 无法解析默认对话 Provider：get_using_provider 与注册表兜底都没有结果；
        未显式配置 provider 的模型任务将退化为模板兜底
WARNING [Token预算] LLM 调用失败: task=daily_plan error=未找到可用的 AstrBot 默认模型 Provider
```
陪伴插件的 `_conf_schema.json` 里有 **20+ 个 `*_PROVIDER_ID`**
（`FAST_RESPONSE_PROVIDER_ID` / `COMPLEX_REASONING_PROVIDER_ID` / `CREATIVE_MODEL_PROVIDER_ID` …），
就是用户截图里那三张卡片。**解析不到 → 记忆抽取/日程生成/关系分析全部退化成固定模板**
（不报错、静默降级）。

### 交付物

| 文件 | 内容 |
|---|---|
| `host/astrbot_host/provider.py`（新） | `Provider`（`text_chat` → 转调 `LLMPort`）+ `ProviderManager` |
| `emotion_app/web/provider_bridge.py`（新） | `UpstreamLLMPort`：把我们的上游接成插件的 LLMPort |
| `emotion_app/infra/providers_store.py`（新） | 配置存储（原子写 + 校验） |
| `emotion_app/web/api_providers.py`（新） | 6 个管理 API |
| `host/astrbot_host/compat/.../context.py` | `provider_manager` + 三个查询方法（**不再硬编码 None/空**） |
| `emotion_app/web/static/{index.html,app.js,app.css}` | 侧边栏「模型供应商」+ 页面 |

### 🔴 我原来写错、这次修掉的关键一处

`get_using_provider()` 我原来 `return self._ports.llm` —— **接口面不对**：
插件拿到后调 `provider.text_chat(...)`（`memory_companion/core/service.py:7519`），
而 `LLMPort` 只有 `generate()` → `AttributeError` → **退化成模板兜底**。
→ 改成返回真正的 `Provider` 对象。**判据专门钉这条**（注入回旧写法 → 判据挂 ✅）。

### 🎯 端到端实测（真服务）

```
$ curl /astrbot_plugin_private_companion/page/providers/available     ← 插件面板真实接口
{"success":true,"data":{"items":[{"id":"fast","name":"快速响应",
  "model":"gemini-3.5-flash","type":"chat_completion","is_default":true}],"total":1}}
```

**热同步也验了**（不用重启，插件的 `ProviderManager` 立刻更新）：

| 操作 | 插件立刻看到 |
|---|---|
| 新增 `deep` | `['fast','deep']` ✅ |
| 设 `deep` 为默认 | `default= deep` ✅ |
| 删除 `deep` | `['fast']`，默认自动转回 ✅ |

### 判据

| 文件 | 条数 |
|---|---:|
| `host/tests/test_provider.py` | 26（Provider 形状 / `text_chat` 契约 / 管理器 / Context 接线 / 装配层） |
| `tests/test_providers_api.py` | 66 |
| `tests/test_assembly.py`（+4） | 装配"有没有接上" |
| `tools/provider_panel_check.py` | 真浏览器 **49/49** |

**注入 bug 验证**：`get_using_provider` 退回 LLMPort → 挂 1 条；
不注入 `provider_manager` 到 HostPorts → 挂 1 条。

### ⚠️ 验收时踩到、值得记住的两点

1. **「测试全绿 ≠ 你测的是最新代码」**：我打真 HTTP 发现「空 model 没被拦住」，
   一度以为是真 bug；查下来是**服务进程启动于 16:24，而 `api_providers.py` 改于 16:42**
   → 我测的是旧代码。重启后 5 条校验全部正确拦截。
   **通则：改完必须重启，并用 `find -newer /proc/<pid>` 确认服务加载的是最新代码。**
2. **上游 503 不是我们的 bug**（已证）：`test` 端点报 `upstream_error`，
   我**直连上游、绕过我们全部代码**打同一个请求 —— 同样 503
   （`No available channel for model ... under group CN`）。
   ⚠️ 顺带发现：`.env` 里的 `gemini-3.1-flash-lite` **不在**上游 17 个可用模型里
   （「获取模型」接口实测）。这是**既有配置问题**，与本次改动无关。

## 2. ⚠️ 四个"插件自身 bug"（共 33 处调用），我们**不修**（照实降级）

实测确认（用**真 AstrBot 解释器** `hasattr` 过的）：真 `AstrMessageEvent`
**没有** `plain` / `wait` / `send_message` / `get_user_id` 这四个成员；
且遍历 `astrbot.core.platform` 下**全部事件子类（实测 0 个）**也都没有。

| 调用点 | 真相 | 处理 |
|---|---|---|
| `image_companion/main.py:593` `yield event.plain(...)` （8 次） | 真实现也没有 → **插件 bug**，在真 AstrBot 上同样 `AttributeError` | **不补**。补了就是伪造能力 |
| `prompt_tools/main.py:406` `await event.wait(timeout=60)` （2 次） | 真实现也没有 → **插件 bug** | **不补** |
| `comfyui_pro/main.py:1259` `await event.send_message(...)` （3 次） | 真实现也没有（真实现是 `context.send_message`）；⚠️ 该处是插件**自测指令** `api_test_all` 里的探测代码 | **不补** |
| `prompt_tools` 多处 `event.get_user_id()`（20 次） | 真实现也没有 → **插件 bug** | **不补** |

> 🔴 **判据**：我们的 compat **不得**提供这四个名字。
> 若将来实测真 AstrBot 某个版本加了，再照版本对齐（**不许凭"看起来该有"就加**）。
> ⚠️ 但也**不要**因此就认为"插件整个不能用" —— 这些都在**具体函数体内**，
> 只有执行到那几行才会炸；其余功能正常。

**P0-1 真正要补的清单（实测「插件用了 + 真实现有 + 我们缺」的交集）**

| # | 成员 | 插件用了几次 | 真实现 |
|---|---|---:|---|
| 1 | `get_group_id()` | 4 | `:195` |
| 2 | `get_session_id()` | 1 | `:191` |
| 3 | `clear_result()` | 0（**但派发循环内部必须调**） | `:378` |
| 4 | `continue_event()` | 0（同上，`stop_event` 的对偶） | `:351` |
| 5 | `make_result()` | 0（`plain_result` 的基础） | `:384` |
| 6 | `image_result()` | 0 | `:403` |
| 7 | `get_platform_id()` | 0 | `:133` |
| 8 | `role` 属性 | — | `:51`，`is_admin()` = `role == "admin"` |
| 9 | `call_llm` 属性 | — | `:94` |
| 10 | `plugins_name` 属性 | — | `:99` |
| 11 | `is_at_or_wake_command` 属性 | — | `:55` |
| 12 | `_force_stopped` / `_has_send_oper` | `_has_send_oper` **6 个文件读** | `:58` / `:92` |

> 说明：3~7 虽然调用次数为 0，但 **4、5、6 是派发/结果链路的基础**（`call_handler` 靠
> `set_result`/`clear_result` 的成对语义）；**5、6 是 `plain_result` 的同族**，一次补齐成本极低。
> 只补"插件直接调到的"，会让 P0-2 的 `clear_result` 无处可调。

---

## 3. P0 任务分解（带实测规模）

### P0-1 `AstrMessageEvent` 补齐 —— **缺口 13 个成员（7 方法 + 6 属性）**

实测：45 个"真的用 `AstrMessageEvent`"的插件文件里，`event.<成员>` 的**全量**使用如下，
标 ❌ 的是我们**当前没有**的：

| 成员 | 次数 | 现状 | 备注 |
|---|---:|---|---|
| `plain_result` | 662 | ✅有 | ⚠️ 但语义错，见 BUG-3 |
| `get` | 280 | ✅有 | |
| `get_sender_id` | 236 | ✅有 | |
| `send` | 153 | ✅有 | 需维护 `_has_send_oper` |
| `stop_event` | 124 | ✅有 | 需 `_force_stopped` 语义 |
| `set_extra` / `get_extra` | 114 / 90 | ✅有 | |
| `get_result` / `set_result` / `chain_result` | 80 / 40 / 58 | ✅有 | `set_result` 需支持 **str** |
| `unified_msg_origin` | 31 | ✅有 | 需改成**属性**（真实现是 property，setter 会重解析 session） |
| **`get_user_id`** | **20** | ❌缺 | ✅**已查清：真实现也没有（连子类都没有）→ 插件 bug，不补**，见 §2 |
| `get_platform_name` | 18 | ✅有 | |
| `is_private_chat` | 16 | ✅有 | |
| `message_str` | 16 | ✅有 | |
| **`is_stopped`** | **12** | ⚠️有但错 | 见 BUG-2 |
| `get_messages` | 12 | ✅有 | |
| `get_self_id` | 10 | ✅有 | ⚠️ 真实现从 `message_obj.self_id` 取 |
| **`plain`** | **8** | ❌缺 | **不补**（插件 bug，见 §2） |
| `message_obj` | 6 | ✅有 | |
| **`get_group_id`** | **4** | ❌**缺** | 真实现**有** → **要补** |
| `session_id` | 4 | ✅有 | |
| `get_message_str` | 4 | ✅有 | |
| **`send_message`** | **3** | ❌缺 | **不补**（插件 bug，见 §2） |
| **`wait`** | **2** | ❌缺 | **不补**（插件 bug，见 §2） |
| `is_admin` / `should_call_llm` / `get_sender_name` / `setdefault` | 各 2 | ✅有 | `should_call_llm` 是 setter，需同时置 `call_llm` |

**结论：分两类，都要补。**

| 类别 | 数量 | 说明 |
|---|---:|---|
| **(a) 插件直接调用且我们缺的** | **1 个方法**：`get_group_id()` | 只有它同时满足「插件用了 + 真实现有 + 我们缺」 |
| **(b) 派发/结果链路必需的基础设施** | **6 个方法 + 6 个属性** | 调用次数为 0，但 P0-2 的派发循环**必须**依赖它们 |

> ⚠️ **只补 (a) 会让 P0-2 写不出来**：真实现的执行循环本身要调 `clear_result()`（每轮 handler 后）、
> `is_stopped()`（判断中断）、`set_result()`；唤醒判定要写 `is_at_or_wake_command`；
> 结果投递要维护 `_has_send_oper`。这些**不是可选装饰**，是链路的组成部分。

**要补的完整清单（7 方法 + 6 属性）**：

| # | 要补的东西 | 依据 |
|---|---|---|
| 1 | `get_group_id()` | 真实现 `:195`，返回 `message_obj.group_id`（非群聊返回 `""`） |
| 2 | `get_session_id()` | 真实现 `:191` |
| 3 | `clear_result()` | 真实现 `:378` |
| 4 | `continue_event()` | 真实现 `:351` |
| 5 | `make_result()` | 真实现 `:384` |
| 6 | `image_result()` | 真实现 `:403` |
| 7 | `get_platform_id()` | 真实现 `:133` |
| 8 | `role` 属性 | `"member"` / `"admin"`；`is_admin()` = `role == "admin"` |
| 9 | `call_llm` 属性 | `should_call_llm()` 写它 |
| 10 | `plugins_name` 属性 | `None` = 全部启用 |
| 11 | `is_at_or_wake_command` 属性 | **派发的前置条件**（`waking_check` 用它） |
| 12 | `_force_stopped` + `_has_send_oper` | 见 BUG-2；`_has_send_oper` 被 **6 个文件**读 |

> ⚠️ `get_user_id()`（20 次）**已查清 = 插件 bug**：
> 用真解释器 `hasattr` 过，真 `AstrMessageEvent` **没有**这个方法；
> 且遍历 `astrbot.core.platform` 下**全部事件子类（0 个）**也没有 → 见 §2。
> **不补。**

### P0-2 派发入口 —— **新建 ~1 个模块**

照真实现两个阶段（**必须分两步，不能合并**）：

* **阶段 A「按 filter 选 handler」** = 真 `waking_check/stage.py:167-236`
  * 逐 handler 跑 `event_filters`（**AND 逻辑**）
  * `len(event_filters) == 0` → **跳过**（这就是 BUG-1 的杀伤点）
  * `PermissionTypeFilter` 单独处理（`raise_error` 决定"跳过"还是"报错+`stop_event`"）
  * 命中后把 `event.get_extra("parsed_params")` 收进 `handlers_parsed_params[full_name]`，
    然后 **`pop("parsed_params")`**
  * 结果写回 `event.set_extra("activated_handlers", ...)`
* **阶段 B「执行」** = 真 `process_stage/method/star_request.py:36-74`
  * 逐个 handler：`is_stopped()` 就 `break`
  * `call_handler`（真 `context_utils.py:12-75`）：支持 **协程**与**异步生成器**两种；
    `MessageEventResult`/`CommandResult` → `event.set_result(ret)`；否则 `yield ret`
  * **每轮 handler 后 `event.clear_result()`**（真 `:53`）
  * 异常 → 打 ERROR + 走 `OnPluginErrorEvent` + `set_result(":(\n\n在调用插件 …")` + `stop_event()`

**实测的 filter 分布**（决定要支持哪几种）：

| filter 类型 | 数量 |
|---|---:|
| `PermissionTypeFilter` | **48**（全部 `PermissionType.ADMIN`, `raise_error=True`） |
| `CommandFilter` | 26（**修 BUG-1 后会变 26+16=42**） |
| `EventMessageTypeFilter` | 14（`GROUP_MESSAGE` 7 / `ALL` 5 / `PRIVATE_MESSAGE` 2） |
| `CommandGroupFilter` | 5 |

**唤醒判定**（真 `waking_check/stage.py:106-152`）—— 我们有简化空间但要**照实**：

* `wake_prefix` 前缀匹配 → `is_at_or_wake_command = True`，**并从 `message_str` 里切掉前缀**
* 群聊 @ 机器人 / AtAll / 引用机器人 → 同样置位
* **私聊**：`is_private_chat()` 且（不需要前缀 **或平台是 `webchat`**）→ 置位
* 都不满足 → `event.stop_event()`（真 `:247-248`）
* ⚠️ **我们单用户私聊**：走"私聊 + 不需要前缀"这条，**天然全唤醒** ——
  这也解释了为什么 AstrBot 网页聊天不用打前缀。

**指令清单（实测 31 条，去掉重复后）**：

```
HDSI/hdsi · aitag · api_test_all · comfy_add · comfy_lock · comfy_ls ·
comfy_probe_send · comfy_save · comfy_test_send2 · comfy_use · comfy帮助 ·
image · imgstatus · mcomp(×2) · nai_interrogate/反推/nai反推 · prompt ·
prompt group · prompt view · quota · 一起/一起看/陪我 · 创作 · 当前工作流 ·
测试lora · 画图 · 画图no · 画图工作流 · 违禁级别 · 重绘 ·
陪伴/私聊陪伴/主动陪伴 · 陪伴群/群陪伴/群聊陪伴
```

> ⚠️ 注意 `mcomp` 出现两次（两个插件各注册一个）——
> **同名指令冲突**要有确定性处理，否则"哪条生效"会随注册顺序漂移。

### P0-3 结果回写 —— 接到我们既有的聊天链路

* 消费 `event.get_result()` / `event.sent_messages` → 转成我们的回复
* `_has_send_oper` **如实维护**（`send`/`send_streaming` 成功后置 True）
* ⚠️ 与真实现一致：**只有 `_has_send_oper` 为假且 `is_at_or_wake_command` 且未 `call_llm` 时**，
  才走默认 LLM 链路（真 `process_stage/stage.py:56-60`）

### P0-4 LLM 钩子（`OnLLMRequestEvent` 24 个）

* **P0-4 排在 P1 之后**（照 `NEXT_STEPS.md` 的顺序）

---

## 4. P1 任务分解（umo 第一段 = `11`，情绪已拍板）

| # | 任务 | 验收判据 |
|---|---|---|
| **P1-1** | umo 约定定死：`11:FriendMessage:<用户ID>` | 常量 + 单测；**格式与生产 `companions.json` 里 `11:FriendMessage:3412642082` 逐字同构** |
| **P1-2** | `context.platform_manager` + 平台实例 | `get_insts()` **和** `platform_insts` 都要有；实例 `meta().id == "11"`；`status is RUNNING` |
| **P1-3** | 平台实例 `send_by_session(session, chain)` | 真投递才返回成功；**接不上如实 `False`**（不伪造 delivered） |
| **P1-4** | 面板绑定跑通 | 插件 `users` 里出现我们的 umo；`target_platform` 与实例 id 一致 |

> 🔴 **绝不冒充 `aiocqhttp`**：实例 `meta().name` 用 `webchat` 语义
> （插件 `platform_compat.py:102` 把 `webchat` 当一等公民），
> `id="11"` 只为与 umo 第一段字面对齐。

---

## 5. 判据与验证方式

* **每个新判据都要做「注入 bug 验证」**（项目铁律：不会失败的判据 = 没写）。
* **3 个 BUG 各配一个能失败的回归测试**（BUG-1/BUG-2/BUG-3）。
* 跑法（⚠️ 主项目与 host **分开跑**）：
  ```bash
  cd /root/Ai-works/情绪app && .venv/bin/python -m pytest -q          # 主 440
  cd /root/Ai-works/情绪app/host && ../.venv/bin/python -m pytest -q  # host 35
  ```
* **差分对拍**（必须用真 AstrBot 解释器）：
  ```bash
  cd /root/Ai-works/情绪app
  PYTHONPATH=$PWD/host /root/.local/share/uv/tools/astrbot/bin/python host/tests/tools_verify_compat.py
  ```
* 🔴 **绝不在 `/opt/astrbot` 上做实验**；探针一律 `mirror_root` + 临时目录。

---

## 6. 执行顺序（依赖关系）

```
① 修 3 个兼容层 BUG（BUG-1 是 P0-2 的前置：不修则 16 个子指令【永不执行】）
   ↓
② P0-1  AstrMessageEvent 补齐 13 个成员（7 方法 + 6 属性，含 _force_stopped/_has_send_oper）
   ↓
③ P0-2  派发入口（阶段 A 选 handler + 阶段 B 执行）
   ↓
④ P0-3  结果回写 → 【到这里「插件能在聊天里回一句话」= 通了】
   ↓
⑤ P1-1 umo 定死 → P1-2 platform_manager → P1-3 send_by_session → P1-4 面板绑定
   ↓
⑥ P0-4 LLM 钩子（24 个）
```

**每完成一步**：跑测试 + 注入 bug 验证 + 更新本文件的状态列。

---

# 附录·⑨「模型供应商」两层改造收尾（2026-09-20 晚）

## 结论（一句话）

**真浏览器面板判据 70/70 全绿 · 真 HTTP 端到端通 · 陪伴插件能解析到模型 · 注入 bug 全部被抓。**

| 项 | 结果 |
|---|---|
| 面板判据（`tools/provider_panel_check.py`，真 Chrome CDP） | **70/70 通过** |
| 主测试 | `541 passed, 1 deselected`（deselected = 需真 AstrBot 解释器的 `live` 那条，**故意不 skip**） |
| host 测试 | `139 passed` |
| 供应商 API 测试 | `81 passed` |
| 差分（真 AstrBot 4.27.5） | **92/92 · 50/50 · 56/56** |
| 注入 bug 验证 | **未挂判据 0 条**（约 60 个注入点全部被抓住） |
| 生产完整性 | `/opt/astrbot/.../_conf_schema.json` sha256 `06113bf50e2698e3` **未变**；无 `/opt/astrbot/data/providers.json` |

## 本轮修掉的真缺陷（产品侧 2 个）

1. 🔴 **深链把真根因盖掉**（`static/app.js` `applyRoute` 的 `providers` 分支）
   * 原来：`if (!list.length) { try { await loadProviderSources(); } catch (e) {} }`
   * 而 `loadProviderSources()` 失败时是 **`return false`，不抛异常** → `catch` 永不进
     → 列表仍空 → 下一句 `selectProviderSource(id)` 报「找不到提供商源「x」。」
     → **用户以为"源不存在"，真相是后端 500 / 响应形状不符**。
   * 已改为**看返回值**：`const ok = await loadProviderSources(); if (!ok) return;`
   * 判据：`⑧ 🔴 深链时列表读取失败 → 面板报真根因（500 失败），不许用「找不到提供商源」盖掉`
   * 注入验证：改回 `try/catch` 版本 → **2 条判据挂**（这条 + 形状不符那条）。
   * **通则：`return false` 的函数不能当"会抛异常"的函数用。**

2. 🔴 **迁移数字对不上**：见下"错误注入残留"第 2 条（属于工具事故，已修，附带修好产品文件的损坏）。

## 🔴🔴 本轮最重要的教训：注入验证工具**自己会改坏产品代码**，而且会**把损坏固化**

这是本轮最贵的一次事故，两个独立缺陷叠加：

1. **中途被打断 = 注入留在产品文件里。**
   我用 `timeout 60` 跑注入脚本（它要跑几十轮 pytest，远超 60s）→ **SIGTERM 打断**，
   `restore()` 没执行，`api_providers.py` 里留下
   `except httpx.TimeoutException if False else ():`
   → 超时被 `except httpx.HTTPError` 接走 → **误报成 `upstream_unreachable`**，
   基线 1 条挂（`test_fetch_models_timeout`）。同类残留还有测试文件里的
   `await c.post(...) if False else await c.post(...)`。

2. **更阴的：`backup()` 先 `rmtree` 再复制"磁盘上当前的"文件。**
   第二次运行时它把**上一次残留的坏文件**当成"干净基线"备份下来
   → 从此 `restore()` 恢复的就是坏文件 → **损坏被固化，怎么重跑都修不回来**
   （`/tmp/prov_backup_2layer/api_providers.py` 的 mtime 恰好是事故时刻 `19:01`）。

✅ **对策（都已实现并验证）**：
* `backup()` **拒绝在含注入指纹（`if False` / `# INJECT`）的文件上建基线** ——
  宁可拒绝运行也不固化损坏（注入式验证过它会拦）。
* `atexit` + `SIGINT/SIGTERM` 兜底恢复；`_DIRTY` 在**写入注入的那一刻**置位。
  实测：`kill -TERM` 后文件 sha256 **与基线逐字节相同**、无残留。
* `restore()` 每次比 **sha256**，不一致就大声报错（"我以为恢复了"不算数）。
* 跑完比 4 个目标文件与备份的 sha256：**全部一致**。

**通则（写进全局记忆）：**
* 🔴 **凡是"原地改产品文件"的验证工具，必须有"中断也能恢复"的兜底 + 拒绝在脏文件上建基线。**
* 🔴 **备份不能"复制当前磁盘状态"** —— 那会把上一次的损坏当基线。要**先校验干净**再备份。
* ⚠️ **`timeout N` 会 SIGTERM 掉正在原地改文件的脚本** —— 这类脚本要么给足时间、要么后台跑。

## 判据工具自身修掉的 bug（4 个，全是"桩没长得像真的"）

| # | 症状 | 真根因 |
|---|---|---|
| ① | `put=None` 但 `__calls` 里明明有该 PUT | 过滤写成 `url.indexOf('/provider-sources/by-id')===0` —— 真实 URL 是 `/api/v1/provider-sources/by-id?...`，**永不成立**。→ 改成含 `/api/v1` 的完整前缀 |
| ② | 两条「错误如实显示」判据恒红 | 错误注入开关写成 `url.indexOf("/provider-sources")===0` —— **同样永不成立** → 注入是**死的**。→ 改成 `url === "/api/v1/provider-sources"` |
| ③ | ⑦ 报「先选中（或保存）一个提供商源。」 | 500 注入把 `state.providerSources` 清空后**没恢复现场**；且 `if(li) li.click()` 把"没点到"伪装成"点到了"。→ 显式恢复 + 新增 `select_source()` 返回**是否真点到**并单独判据 |
| ④ | 深链判据测的不是深链 | `location.hash='#/providers'` 在 hash **本来就是**该值时是**空操作** → loader 不重跑；`Page.navigate` 到只差 fragment 的 URL 属**同文档导航** → 不重新加载、注入的桩还在。→ 改 `applyRoute()` 显式重跑 + `Page.reload` 真刷新 |

⭐ **共同点：四个都是"判据在测自己造成的状态"，不是产品缺陷。**
**通则：桩/注入/导航这三件事，都要确认"它真的生效了"，否则判据会测空气。**
（本项目老坑重演：截图脚本只改 hash 导航 → 断言全错。）

---

# 附录·⑩ 宿主「模型端口 + 投递端口」接通（2026-09-20 深夜）

> 承接情绪的路线判断：**「只需要模拟 AstrBot 和当前版陪伴的关系就行了」**。
> 本轮不去追新框架，而是把**陪伴插件真的跑起来**的最后两个断点接上。

## 0. 起因：插件其实**一直在跑**，只是两个端口没接

实测 `logs/server.log`：

```
21:27:36 INFO astrbot: [主链] 主动消息循环已启动        ← 循环真的起来了
38 次  WARNING [Token预算] LLM 调用失败: task=daily_plan|daily_review|diary
       error=宿主未接入模型调用能力（`HostPorts.llm` 为空）
```

链路追到底：`initialize()`（`main.py:8459`）→ `_initialize_before_publication()`
（`:8533`）→ `asyncio.create_task(self._scheduler_loop())`（`:8604`）；
`_scheduler_loop`（`proactive.py:4241`）是个真 `while` 循环。

**结论：陪伴按 AstrBot 的方式活着，我们只要把它要的端口给全。**
而 `HostPorts(llm=..., delivery=...)` **两个都没传**。

## 1. 修掉的 4 个真 bug（全部注入验证）

| # | bug | 后果 | 位置 |
|---|---|---|---|
| ① | `llm_generate` **直连端口**，不走供应商层 | 返回 `ChatResult`（只有 `.content`），插件读 `getattr(r,"completion_text","")` → **拿到空串且不报错** → 记忆/日程/日记**静默失效** | `compat/core/star/context.py` |
| ② | `chat_provider_id` 被**忽略** | 插件为不同任务配了 20+ 个 `*_PROVIDER_ID`，全被压到同一个端口 → **配置形同虚设** | 同上 |
| ③ | `UpstreamLLMPort.generate()` 收了 `**kwargs` **却不用** | 插件的 `max_tokens`（控成本）**静默丢弃**，无任何日志 | `web/provider_bridge.py` |
| ④ | `OpenAIChatBackend.complete()` 收了 `**kwargs` **却不转发 `extra`** | 同③，**第二层**再丢一次 | `domain/backends.py` |

⭐ **③④ 是"同一个参数被两级各丢一次"** —— 只在装配层验是发现不了的，
必须一路验到**真 HTTP 请求体**（判据 `test_plugin_max_tokens_actually_reaches_the_upstream_body`）。

### 我自己的 bug（也注入验证了）

给 `_build_default_llm_port` 写了 `flatten_two_layer(store)`，但那名字是在
**另一个方法**里 `from ... import` 的**局部导入** → `NameError`
→ 被 `except Exception` 捕获 → **打 ERROR 后返回 None**。
后果：服务照常启动、测试全绿、**端口仍是 None**，只是错误信息从"没接"变成"配置读取失败"。

🔴 **教训：`except Exception` 兜底 + 返回 None 的写法必须配判据**，
否则它会把"我的代码写错了"伪装成"配置读不出来"。

## 2. 投递端口（新增 `emotion_app/web/delivery.py`）

原来 `delivery=` **一直传 None**，插件两条投递路径全断：

| 路径 | 插件的调用 | 原来 | 现在 |
|---|---|---|---|
| A | `context.send_message(umo, chain)` | `NotImplementedError` | ✅ 写进会话 |
| B | `platform.send_by_session(session, chain)` | 返回 `False` | ✅ 写进会话 |

⚠️ **两条都要接** —— 插件不同调用点走不同路，只接一个 = 一半调用点仍断。

### 🔴 三条刻意划的边界

1. **只写我们自己的会话存储**，**不接任何外部推送** ——
   那是最需要显式授权的动作，不该由宿主服务偷偷做；面板就是用户看消息的地方。
2. **不伪造送达**：写不进去返回 `False` / 抛错（作者判据③），插件走它自己的降级路径。
3. **umo → 会话是确定性映射**（sha256 前 16 位 + `plugin:` 前缀）：
   重启后必须还能找回**同一个**会话，否则主动消息会散落在无数个新会话里。

⚠️ **没做"注入当前打开的会话"**：那需要 umo↔多会话映射 + "发给哪一个"的策略
（多会话时是真设计问题），而插件只知道 umo。**先一对一，语义清晰可验证。**

## 3. 判据与注入验证

| 文件 | 判据数 | 覆盖 |
|---|---:|---|
| `host/tests/test_llm_generate.py`（新建） | 10 | 返回形状必须是 `LLMResponse` / 插件读法能取到文本 / `max_tokens` 透传 / system+contexts 不丢 / 三种"没有"的区分 |
| `tests/test_delivery.py`（新建） | 10 | 真写进会话 / 同 umo 同会话 / 两用户不串台 / 写失败返回 False / 无仓库返回 False / 空链不当成功 / 非文本降级留 WARNING |
| `tests/test_llm_port_assembly.py`（新建） | 9 | 端口不是 None / 真能 generate / `start()` 真的传了 `llm=` 与 `delivery=` / 平台管理器也拿到了 |

**注入验证 5 次，全部被抓**：

| 注入 | 挂掉的判据数 |
|---|---:|
| ① `llm_generate` 退回直连端口 | 4 |
| ② `UpstreamLLMPort` 不构造 `extra` | 1 |
| ③ `complete()` 不转发 `extra` | 1 |
| ④ 复现我自己的 `NameError` | 2 |
| ⑤ `start()` 不传 `llm=` | 1 |
| ⑥ 写失败却返回 `True`（伪造送达） | 1 |
| ⑦ 只打日志不写会话（假投递） | 4 |
| ⑧ 平台管理器不接投递端口（路径 B 断） | 1 |

全部 `sha256` 逐字节还原，`INJECT:` 标记零残留。

## 4. 端到端取证（真 HTTP / 真库）

```
① 真上游模型调用（用有效 key）：返回类型 LLMResponse，completion_text='收到'
② 路径A (delivery.send)          → True
③ 路径B (send_by_session)        → True
④ 真 HTTP 读会话：
   GET /api/v1/sessions           → id=plugin:63bde1ed6ef60b85 title='陪伴（主动消息）' msgs=2
   GET .../messages               → [assistant] 【路径A】…  [assistant] 【路径B】…
⑤ 能力诚实：delivery_wired=True（接了）；delivery=None 时 False
⑥ P0 ③ 命令派发（真 HTTP）：`mcomp status` → 真插件真回复（记忆/身份/时间线统计）
```

回归基线：主 **635 passed**（1 failed = 故意不 skip 的 `live`）、host **149 passed**。

## 5. ⚠️ 顺带发现的**预存数据问题**（不是本轮代码的 bug）

面板「模型供应商」里那把 key **与 `.env` 不同步，且已失效**。
直连上游（绕开我们全部代码）实测：

| key 来源 | `gemini-3.5-flash` | `gemini-3.7-flash` |
|---|---|---|
| `data/providers.json`（面板配的） | **503** | **503** |
| `.env` 的 `EMOTION_UPSTREAM_API_KEY` | **200** | **200** |

→ 面板默认供应商 `fast/gemini-3.5-flash` 用的是**失效那把**，
所以插件调模型报 503。**这是配置问题，不是代码问题**（直连复证过）。

⚠️ 另外上游提示：`gemini-3.5-flash` 已下线（返回文案 "no longer available,
please switch to Gemini 3.7 Flash"），而 `.env` 里的 `gemini-3.1-flash-lite`
反而可用 —— **建议把面板那把 key 换成 `.env` 里那把，并换一个上游确认可用的模型**。

## 6. 仍未做（等你定）

1. **主动消息落哪个会话**：现在是"一个 umo 一个固定会话"（确定性）。
   要不要改成"注入你当前打开的会话"？（要 umo↔多会话映射，多会话时得定策略）
2. **要不要接外部推送**（离线也能收到）—— 现在**只有面板可见**，刻意没做推送。

---

# 附录·⑪ 按会话配置（方案 B）：**每个会话 = 插件里的一个用户**（2026-09-21）

> 承接情绪的两句：
> ① 「主动消息是要在陪伴里面配置的**按照 umo 来，每个会话 umo 都不一样**」
> ② 「记得配置文件页面也没有『选择会话』的地方……就像 astrbot 一样」
>
> ⚠️ ② 这句里**我判断错了一半**，先把这个记清楚（下面 §0）。

## 0. 🔴 先纠正我自己的误判（重要）

我当时的结论是"面板没有 per-session 配置"。**实测是错的**：

| 部件 | 状态 |
|---|---|
| 面板「绑定人设」下拉 | **早就有** —— `index.html:98`、`app.js:880` 填充、`:950` 提交 |
| 后端 `PATCH /api/v1/sessions/<id>` | **早就有** —— `api_admin.py:944` 接受 `persona_id` |
| `Session.persona_id` 字段 | **早就有**，存得下 |
| **读**这一侧 | 🔴 **坏的** —— 存了没人读 |

实测证据（真造两个人设 + 绑定的会话）：

```
① 会话A 绑的人设        = 94ce904e…（测试甲）
② 全局 active_persona_id = （空）
③ resolve_persona() 实际取到 = None      ← 会话绑的甲被完全无视
④ 【应该】取到           = 测试甲
```

**教训：「面板没有 X」这个结论必须三条都验过才能下** ——
①UI 元素在不在 ②路由收不收 ③**读的时候用没用**。
只查前两条会得出完全相反的结论（我这次就是）。

## 1. 修掉三个真 bug

| # | bug | 位置 | 后果 |
|---|---|---|---|
| ① | `resolve_persona()` **不看会话** | `domain/chat.py:266` | 全局空着就返回 None，会话绑的人设被无视 |
| ② | 调用它时**不传 `session_id`**（函数也没这参数） | `web/app.py:432` | 同上 |
| ③ | 首次聊天把**全局**人设**写进**会话 | `domain/chat.py` `prepare_session` | → **永久钉死**，之后不再跟随全局 |

⭐ 现在的优先级：**显式指定 > 会话绑定 > 全局当前**（`resolve_persona(persona_id, *, session_id)`）。
⚠️ bug ③ 的修法关键是 **`ChatRequest.persona_explicit` 标记**：
   只有**用户真的显式选了**才写进会话；`resolve_persona()` **解析出来的**不算 ——
   否则第一次聊天就把全局人设钉进会话，与面板上写的「留空 = 用当前人设」**直接矛盾**。

## 2. 方案 B：会话 → 插件用户（**情绪选定的那一条**）

### 为什么必须是"每个会话一个 user_id"

插件的 `data["users"]` 是**按 user_id 分**的 —— 记忆 / 关系 / **主动消息开关**全在里面：

```json
{"users": {"3412642082": {"umo": "11:FriendMessage:3412642082",
                          "proactive_private_enabled": true, ...}}}
```

所有会话共用一个 `"default"` 的话，**三个会话只能同开同关** ——
情绪要的「三个会话只有一个开主动消息」根本做不到。

### 链路（实测自洽）

```
面板会话页「插件用户 ID」  →  Session.user_id
   → 派发时传 `user_id=uid`  →  build_inbound_event(text, uid)
   →  sender = MessageMember(uid)、umo = 11:FriendMessage:<uid>
   →  插件 main.py:16577 `user_id = str(event.get_sender_id())`
   →  data["users"][<uid>]  ← 三个会话 = 插件里的三个用户
```

⭐ **`umo` 第三段 == `sender_id` == 插件 `users` 的键**（三者同一个值）。
⚠️ 实测生产 `companions.json`：`users` 的**键是纯 `3412642082`**（不含冒号），
   `umo` / `bound_delivery_umo` 是它**内部的字段** —— 印证这个取法。

### 投递怎么找到"该落哪个会话"

`emotion_app/web/delivery.py` `_resolve_session_id(umo)`，两条路：

1. **按 `user_id` 找用户自己的会话**（`SessionRepository.by_user_id()`）→ 落进**他看得见的会话**；
2. 找不到（用户还没建过会话）→ 用 `session_id_for_umo()` 派生 id **新建一个**，并绑上该 `user_id`。

⚠️ **原来只有第 2 条** —— 每个 umo 造一个 `plugin:<sha256>` 的**孤儿会话**，
   用户在面板里根本找不到它，而他自己那个会话**永远收不到主动消息**。

## 3. 交付物

| 文件 | 改动 |
|---|---|
| `emotion_app/domain/session_repo.py` | `Session.user_id` 字段（与 `to_dict`/`from_dict`）、`create(user_id=)`、**`by_user_id()`**、`update` 白名单加 `user_id`、模块级 **`_normalize_user_id()`** |
| `emotion_app/domain/chat.py` | `resolve_persona` 改优先级 + `persona_explicit` 标记；`prepare_session` 不再钉全局人设 |
| `emotion_app/web/app.py` | `_session_user_id()`；派发传 `user_id=uid`；`resolve_persona(session_id=...)` |
| `emotion_app/web/api_admin.py` | 会话创建接受 `user_id` |
| `emotion_app/web/delivery.py` | `umo_user_id()` / `_resolve_session_id()` 走方案 B |
| `emotion_app/web/static/index.html` | 会话表单加「插件用户 ID」输入框 + 提示文案 |
| `emotion_app/web/static/app.js` | 选中会话回填、保存时提交 |

### 🔴 `_normalize_user_id()` 为什么必须存在

这个值是插件 `data["users"]` 的**键**，相等比较是**逐字节**的：

* **尾随空格** → 看着一样、**永远匹配不上** → 主动消息"找不到用户"，
  而**日志里两串看起来完全相同**，极难排查。前端 `trim()` 挡不住 API 直调/脚本/表单自动填充。
* **冒号直接拒绝**（不是替换）：`umo` 是 `A:B:C` 三段，user id 里带冒号会让
  `split(":", 2)` **截断**第三段 → 又是"看起来对、实际错位"。
* **空串合法**：语义是"跟随全局设置"（向后兼容），不是错误。

## 4. 判据与注入验证

| 文件 | 判据数 | 覆盖 |
|---|---:|---|
| `tests/test_session_persona.py`（新建） | 20 | 四级优先级 / 不钉全局 / `user_id` 往返 / `by_user_id` / 构建期校验（冒号、空白、超长、空串合法）/ 投递归属 / 回落新建 |
| `tests/test_delivery.py`（新建） | 10 | 真写进会话 / 同 umo 同会话 / 两用户不串台 / 写失败返回 False / 无仓库返回 False / 空链不当成功 / 非文本降级留 WARNING |
| `tests/test_llm_port_assembly.py`（新建） | 9 | 端口不是 None / 真能 generate / `start()` 真传了 `llm=` 与 `delivery=` / 平台管理器也拿到了 |
| `host/tests/test_llm_generate.py`（新建） | 10 | 返回形状必须是 `LLMResponse` / 插件读法能取到文本 / `max_tokens` 透传 |
| **`tools/session_userid_check.py`**（新建，**真浏览器 CDP**） | **6** | 自建夹具 → 输入框在 → 回填对 → 保存真写进接口 → 非法值被拒且未写 → 自建会话已删 |
| **`tools/proactive_multisession_check.py`**（新建，**真 HTTP**） | **5** | 三会话各绑独立 uid → 读回不串台 → **插件按各自 uid 建档**（umo 互不相同）→ **反向：非 e2e 的 user 计数未被改动** → 会话互不污染 |

### 注入验证：**6 次全部被抓**

| 注入 | 挂掉的判据 |
|---|---|
| ① `PATCH` 白名单删掉 `user_id`（保存静默无效） | `session_userid_check` **2 条**（③④） |
| ② 去掉冒号校验（`bad:colon` 能写进去） | **1 条**（④） |
| ③ `selectSession` 不回填 `user_id` | **1 条**（②） |
| ④ 保存时不发 `user_id` 字段 | **2 条**（③④） |
| ⑤ `_session_user_id()` 直接 `return ""`（**绕过方案 B**） | `proactive_multisession_check` **2 条**（③④） |

全部 `sha256` 逐字节还原，`INJECT:` 标记零残留。

### 🔴🔴 本轮三个"判据自己出错"的事故（比产品 bug 更值得记）

1. **注入 Python 后不重启服务 → 判据假绿。**
   第一次跑注入① ②：**4 条判据全绿**，看起来像"判据抓不住 bug"。
   真相是**注入根本没生效** —— 服务在内存里跑的还是旧模块
   （只有 JS 注入生效了，因为浏览器会重新拉 `app.js`）。
   ✅ 对策：注入后**必须重启服务**，并用
   `find emotion_app host/astrbot_host -name '*.py' -newer /proc/<pid>` 证明加载的是新代码。

2. **判据用"上一轮自己造成的状态"当证据 → 假通过。**
   `session_userid_check` 原来写死 `'alice-renamed'`，而库里**已经有**这个值
   （上次跑留下的）→ 注入"白名单删掉 `user_id`"后，判据③照样绿。
   ✅ 对策：每轮用**全新随机值**（`uuid4`），并且**自建夹具 + 跑完删掉**。
   **通则：判据不许依赖现场状态，也不许用陈旧数据当证据。**

3. **字段名照猜 → 判据恒真。**
   反向判据④ 我原来读 `private_inbound_count`，而插件响应里**根本没有这个键**
   （实测为 `None`）→ 两边都是 `None` → **永远成立**。
   真正随入站增长的是 **`inbound_count`**（实测注入后从 6 涨到 9）。
   **通则：断言之前先把真实响应的字段名打出来看一眼。**

## 5. 端到端取证（真服务 / 真插件 / 真库）

```
$ .venv/bin/python tools/proactive_multisession_check.py http://127.0.0.1:6190 "$TOKEN"
✅ ① 三个会话各自绑定独立的插件用户 ID
✅ ② 接口读回的 user_id 与会话一一对应（不串台）
✅ ③ 插件按各自的 user_id 建档（三个 umo 互不相同）
      HTTP 200；插件里的 umo={'e2e_7bd368_alice': '11:FriendMessage:e2e_7bd368_alice', ...}
✅ ④ 派发没有回落全局默认 id（方案 B 未被绕过）
      非 e2e 的 user 计数变化=无；（核对 1 个已有 user：{'default': 6}）
✅ ⑤ 三个会话的消息互不污染

$ .venv/bin/python tools/session_userid_check.py http://127.0.0.1:6190 "$TOKEN"
✅ ⓪ 自建测试会话（判据不依赖预存数据）  → id=89a020557b524f149a5e12b422ed7354
✅ ① 会话页有「插件用户 ID」输入框
✅ ② 选中会话后回填它绑的 user_id      value='chkecc9eedeb1'
✅ ③ 保存后接口里真的改了
✅ ④ 带冒号的非法值被拒且未写入
✅ ⑤ 自建测试会话已删除，没留脏数据
```

回归基线：主 **655 passed**（1 failed = 故意不 skip 的 `live`）、host **149 passed**。

## 6. 边界（刻意划清）

* ⚠️ **模型 / TTS 保持全局** —— 照 AstrBot：它的 `provider`/`model` 是**全局**设置，
  只有 `persona_id` 是 per-session（`conversation_mgr.py`：`umo → conversation_id →
  {persona_id, title, content, token_usage}`）。情绪已确认。
* ⚠️ **投递只写我们自己的会话**，不接任何外部推送（离线收不到）—— 那是最需显式授权的动作。
* ⚠️ **插件里的 `e2e_*` user 记录会留下**（插件没有删 user 的安全 API）；
  它们带随机 tag，不影响真实在用的 user。

## 7. 仍未做（等你定）

1. **主动消息落哪个会话**：现在是"按 `user_id` 找到**用户自己那个**会话"（确定性）。
   要不要改成"注入你**当前打开**的那个会话"？（多会话时得先定策略）
2. **要不要接外部推送**（离线也能收到）—— 现在**只有面板可见**，刻意没做。
