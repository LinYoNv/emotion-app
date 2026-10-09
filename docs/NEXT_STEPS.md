# 下一步规划（2026-09-21 重写 · **2026-09-22 第三轮**：P-N6 工具执行链 + P-N5 全局配置 · **2026-09-25 第四轮**：生图参考图 + 人设固定参考图 + 预设 57 + 主动消息已开）

> 本文承接 **`docs/P0_P1_TASKS.md`**（已完成项的详细记录 + 注入验证）。
> 本文件**只写"接下来做什么"** —— 已完成的一律不在这里重复，去 `P0_P1_TASKS.md` 查。
>
> 🔴 写作原则：**每条都标"凭什么"**（实测数字 / 代码位置 / 未知就写未知）。
> 不写"应该差不多"这类没有依据的话。
>
> 🎯 路线（情绪 2026-09-20 定，未变）：**「只需要模拟 AstrBot 和当前版陪伴的关系」**。
> 新框架相关的排期一律**不做**（陪伴插件自己会做），审计结论留在
> `docs/FRAMEWORK_V1_COMPAT_AUDIT.md` 当背景资料。

---

## 0. 现状（**2026-09-22 第三轮收尾实测**，不是回忆）

| 维度 | 状态 |
|---|---|
| **插件真的在跑** | ✅ `initialize()` 起来后**主动消息循环真的在转**（`[主链] 主动消息循环已启动`） |
| **四条链路全通** | ✅ 命令派发 / LLM 钩子（记忆注入）/ 模型端口 / 投递端口 |
| **插件面板** | ✅ 能开、能存配置（配置项 **747 个**，此前是 0） |
| **按会话配置** | ✅ 每个会话绑一个「插件用户 ID」= umo 第三段 = 插件 `sender_id`（方案 B） |
| **模型供应商** | ✅ 两层结构（源 1:N 模型）+ **4 类**（对话/语音合成/语音识别/嵌入）+ **50** 条厂商预设 |
| **设置页** | ✅ 配置文件式：会话 **12** 字段 + 上下文 10 字段，照后端 schema 动态渲染 |
| **人设链路** | ✅ **宿主补齐 `context.persona_manager`** —— 插件能读到我们的人设了 |
| **插件 function calling** | ✅ 09-21 修通「**能被看见**」（`tool_called=True`）；**09-22 修通「真的执行」**（见 §0.6 P-N6） |
| **平台可扩展** | ✅ `host/astrbot_host/platforms.py` 注册表 + 三条**注册期**硬校验（加 onebot 等新平台不再改多处常量） |
| **🆕 工具执行链（P-N6）** | ✅ **09-22 修通**：`step_until_done` 多轮循环 + 工具执行器 + 工具并入请求 + 结果回填上游 |
| **🆕 全局配置（P-N5）** | ✅ **09-22 修通**：`get_config(umo)` 真读 `cmd_config.json`（此前**恒为空字典**） |
| **🆕 生图参考图（A1）** | ✅ **09-25 修通**：`selfie` 走精准参考（`director_reference_*`）/ `edit` 走**真 img2img** —— 此前参考图**只被记录、不发给上游**（静默丢弃） |
| **🆕 人设参考图** | ✅ **09-25 接通两代**：① 全局兜底 `EMOTION_IMAGE_PERSONA_REFERENCE`；② **per-persona 面板上传**（情绪「用户能填的就让用户改」）—— 三级优先，`edit`/`text2img` 都不吃它 |
| **🆕 预设 50→57** | ✅ 忠实副本 `PRESETS` 不动，另立 `LOCAL_EXTENSION_PRESETS`（7 条 OpenAI 兼容）；**可执行对话预设 19→26** |
| **🆕 主动消息** | ✅ **09-25 已开**（`default`，克制档 3 条/天）；实测整条链真跑起来了（P-N6 的最终验收） |
| **🆕 陪伴 Wiki 已审读** | ✅ 作者发的 `陪伴wiki`（36 文件）读完 → `docs/WIKI_REVIEW_结论.md`（含 7 条待办 W1-W7） |
| **回归基线**（2026-09-27 实测） | 主 **780 passed**（1 deselected）/ host **274 passed** → 合计 **1054 passed, 1 deselected** |
| **浏览器判据** | 登录 **6/6** · 设置 **21/21** · 日志 **19/19** · 供应商 **84/84** · 聊天人设 **16/16** · 插件面板 **9/9** · 会话绑定 **5/5** · 手机 **5/5** · 会话 user_id **6/6** · 🆕 人设参考图 **17/17**（**10 套**） |

> ⚠️ **基线的正确跑法（踩过坑）**：
> `pytest tests -k "not live"` 是**子串匹配**，会**误伤 16 个含 "delivery" 的正常测试**
> （实测 639 passed / 17 deselected，而不是真值）。
> ✅ 正确写法：`.venv/bin/python -m pytest tests host/tests -q --deselect tests/test_plugin_host_e2e.py::test_router_matches_real_implementation_live`
> → **1054 passed, 1 deselected**（2026-09-27 实测；主 780 + host 274）。
>
> ⚠️ **旧记「主 669 / host 298 = 992」是假基线**（**算术就不自洽**：669+298=967≠992）。
> 查证结论：`host/tests` 磁盘内容与 HEAD 逐字节一致，逐文件求和 = 257（第五轮 +10 = 267，
> 2026-09-27 子代④ 修 `AstrMessageEvent` 位置参数缺口 +7 = **274**）。
> 那个 298 来自**另一个会话**的 `pytest -q`（**无路径**，cwd 不同）。
> **教训：「数字对不上就要查」，别拿旧记忆当事实。**

---

## 0.6 ✅ 第三轮已完成：P-N6 工具执行链 + P-N5 全局配置（2026-09-22）

> 详细记录（含逐条注入验证）→ `P0_P1_TASKS.md`；本节只留**结论 + 会再踩的坑**。

### P-N6：工具**执行链**（上一轮只修到"模型说出要调什么"）

上一轮修的是"工具调用**能被看见**"；从"说出来"到"**真的执行、结果回到模型**"
这一段是**完全断的**，而且断在**多处、其中四处完全静默**：

| # | 断点 | 静默方式 |
|---|---|---|
| ① | `_AgentRunner` 只有 `run`/`run_stream` | `private_image.py:6555`/`proactive_message.py:4665` 用 `hasattr(...,"step_until_done")` 守卫 → **整条工具循环被静默跳过** |
| ② | `private_image.py:6599`/`proactive_message.py:5188` **无守卫**地调 `get_final_llm_resp()` | 缺方法 → `AttributeError`（**潜在崩溃**；当前未触发因为唯一的真实用户 `default` 的 `proactive_private_enabled=False`） |
| ③ | 没有工具执行器 | 全仓无代码调用工具的 `handler` |
| ④ | `req.func_tool` 里没有插件注册的工具 | 真实现 `astr_main_agent.py:599-617` 有 merge，我们零调用 → 插件自己那些"按会话裁剪/标注工具"的逻辑（`main.py:18914/:18964/:18254`）**静默 no-op** |
| ⑤ | 工具 handler 是**裸函数**（缺 `self`） | 真实现 `star_manager.py:1273-1299` 才绑插件实例 → 谁调都必 `TypeError` |
| ⑥ | `tool_calls_result` 被放在 `_IGNORED_TEXT_CHAT_KWARGS` 里"刻意不转发" | 工具结果**永远回不到模型** |

**做了什么**：`_AgentRunner` 补 `step`/`step_until_done`/`done`/`get_final_llm_resp`
（照真实现 `tool_loop_agent_runner.py:1060-1087` 的语义，含**达上限拔工具 + 注入提示词**）；
`_execute_tools` 执行工具并把 `1 × role:"assistant"（带 tool_calls）+ N × role:"tool"`
回填上下文；`_merge_plugin_tools` 把插件工具并进 `req.func_tool`；
`_bind_llm_tools_to_instance` 绑插件实例；`ToolCallsResult.to_openai_messages()`；
`provider.text_chat` 把 `tool_calls_result` 拼成消息喂回上游。

#### 🔴 本轮最值钱的一条（我自己新写的代码里的真 bug）

**`functools.partial` 的 `__module__` 恒为 `'functools'`**，不是被包函数的模块。
→ 按 `handler.__module__` 判归属时，**重载后永远匹配不上** → 绑定被跳过 →
工具继续指着**上一个插件实例**（静默用错对象）。
实测：同进程连 load 两次真陪伴插件，`bind_count` **20 → 0**、`handler_module_path` **有 → None**。
✅ 修法：**先 `handler.func` 拆掉 partial，再判 `__module__`**。
⚠️ 这**不是测试假象**：宿主支持重复 `load_all`（重载插件），单文件跑测试看不出来
（只有"两个测试文件都加载插件"时才现形 —— 我是靠 `pytest host/tests` 全量跑才撞到的）。

### P-N5：`get_config(umo)` 返回**真配置**

宿主原来给插件的是 `HostPorts(config={})` → `get_config()` **恒返回空字典** →
**20 处调用 / 12 个文件静默降级**（`provider_settings` 18 键 / `provider_tts_settings` 11 键 /
`timezone`），而且它正是那句**假根因**「上游 base_url 为空」的来源。

**做了什么**：新增 `astrbot_config.load_global_config()`（读 `data/cmd_config.json`）；
`PluginsConfig.global_config_path`（默认 `data/cmd_config.json`）+ `PLUGINS_GLOBAL_CONFIG_PATH`；
装配处 `config={}` → `config=self._load_global_config()`。

#### 🔴 三条会再踩的坑

1. **`data/cmd_config.json` 带 UTF-8 BOM** → 必须 `encoding="utf-8-sig"`。
   用 `utf-8` 读会 `JSONDecodeError`；**若把它 `except` 成 `{}`，就等于又造了一遍
   "配置恒为空"那个 bug**（同类错误换马甲）。
2. **`get_config()` 绝不能返回 `None`**（原来宿主没注入时就是 `None`）：
   `proactive_engine.py:9929` / `proactive_message.py:10484` 的
   `dict(config.get(...))` **不在 try 内** → 直接炸。空配置 = **空 dict**。
3. **`umo` 必须保持"位置或关键字"参数**：插件 **12 处按位置传**
   （`get_config(target)` / `get_config(str(event.unified_msg_origin))`）——
   改 keyword-only 会**静默打断**它们。真实现签名就是 `(self, umo=None)`。

### 判据与注入验证

| 判据套件 | 结果 |
|---|---|
| `host/tests/test_tool_loop.py`（新，22 条） | **22 passed** |
| `host/tests/test_global_config.py`（新，9 条） | **9 passed** |
| `tests/test_plugin_host_e2e.py`（+3 条 P-N5 **接线**判据） | **3 passed** |
| 全量（main + host） | **926 passed, 1 deselected** |
| 真浏览器 9 套 | 全绿（见 §0 表） |

**注入验证**：P-N6 **16 组**、P-N5 **9 组** —— **全部被抓住**，且逐字节还原。
⚠️ 两次注入**没抓住**，暴露了我自己的判据盲区（都补上了）：
* **⑮ 只测了函数、没测接线** —— `_merge_plugin_tools` 直接调用是对的，
  但把 `build_main_agent` 里那行删掉**照样绿** → 补了一条走真实入口的判据。
* **⑯ 用 async handler 抓不住"签名兜底"** —— async 的体内异常在 `await` 处抛
  （**绕过** try/except），只有**同步** handler 才考得到 → 换成同步 handler +
  另加一条 async 判据（两层都要有）。
**通则（本项目第三次踩）：函数对 ≠ 接线对；判据要能"区分"，不是"能通过"。**

---

## 0.5 ✅ 本轮已完成：兼容层 bug 清算（6 个真 bug）+ 多平台地基（2026-09-21 深夜）

### 起因（情绪原话）

> 「陪伴只能绑定 qq 吗？…**主要还是绑定 umo 啊**，陪伴现在获取到的是默认。」
> 「**不一定要 qq**」「**别写死了，以后可能会加入 onebot 机器人，那边也可以获取 umo。**」
> 「图二的人设栏**这个提示词片段可以不要了**。还有一些已经发现的 bug 记得修。顺便更新文档。」

→ 主线 = 「**平台/umo 不写死**」，顺带把兼容层里已发现的 bug 一次清算。

### 🔴🔴 修掉的 6 个**真** bug（每个都有实测证据 + 注入验证）

| # | bug | 真因（代码位置） | 实测后果 |
|---|---|---|---|
| **1** | **`tool_calls` / `reasoning_content` 被静默丢弃** | 宿主**三条**路各拼一个 `LLMResponse`，都只搬文本（`provider.py:197`、`context.py:721`、`astr_main_agent.py:118`） | 插件读 `resp.tools_call_name`（`main.py:19298/:19363` 等）→ 恒为 `[]` ⇒ **插件的 function calling 完全失效且零日志** |
| **2** | **`MainAgentBuildConfig` 根本构造不出来** | `@dataclass(slots=True)` **外加**手写 `__init__` → 手写的把自动生成的**整个覆盖**；`self.extra` 从未初始化 → `AttributeError` | **任何调用都抛**；插件 `private_image.py:6412`（私有图片回复）与 `proactive_message.py:5093`（主动消息）在此**直接断掉** |
| **3** | **`astrbot.core.agent.tool` 模块不存在** | 我们没实现 `FunctionTool`/`ToolSet`（`shadow.py` 未注册） | 插件 `page_api.py:4199` 抛 `No module named…`，被它 `except` 吞成一行 info → 面板显示「模型判断失败」，**完全指不到根因** |
| **4** | **`ToolSet` 对象原样透传给上游** | `provider.py` 把插件给的 `ToolSet` **对象**直接当 `tools=` 交给上游（没转 OpenAI schema） | `TypeError: Object of type ToolSet is not JSON serializable` |
| **5** | **序列化错误被误当成"签名不匹配"** | `provider_bridge.py` 的 `except TypeError` **无条件**兜底重试 → 用**不带 `base_url`** 的签名重试 | 最终报「上游 base_url 为空」—— **离根因隔了三层** |
| **6** | **人设页无限自刷**（真浏览器判据撞见的） | `applyRoute` 的 persona 分支调 `selectPersona(id)` **漏传 `skipHash`** → `selectPersona` 里 `go()` 又写 hash → `hashchange` → `applyRoute` → **闭环** | 进人设详情后 **4 秒内 `/api/v1/personas` 涨到 4130 次**（每 3 秒 ~2000 次）；浏览器卡死、页面点不动，**且零报错**（每次 fetch 都 200） |

⚠️ **4 与 5 是连体的**：正因为 4 抛了序列化 `TypeError`，5 才把它误接住 →
现场看到的是「base_url 为空」而非「工具集没转格式」。**"错误信息撒谎"是最贵的一类。**

### 🆕 多平台地基（情绪要的「别写死了」）

新增 **`host/astrbot_host/platforms.py`** —— 平台实例的**唯一事实源**：

| 概念 | 说明 |
|---|---|
| `PlatformSpec` | 一个平台实例：`id`（**umo 第一段**）+ `name`（**平台类型名**，决定插件能力表）+ `description` + `label` + `self_id` |
| `PlatformRegistry` | 注册表；默认平台（`11`/`webchat`）**永远在**，配置可**追加** |
| 配置入口 | `.env` 的 `PLUGINS_EXTRA_PLATFORMS`（JSON 数组）+ `PLUGINS_DEFAULT_PLATFORM_ID` |

**加一个 onebot 平台长这样**（**不用改任何代码**）：
```bash
PLUGINS_EXTRA_PLATFORMS='[{"id":"21","name":"aiocqhttp","description":"QQ 机器人","label":"QQ"}]'
```

#### 🔴 三条**注册期**硬校验（全部来自**实读插件源码**，违反 = **拒绝启动**）

| # | 约束 | 违反后果（实测出处） |
|---|---|---|
| ① | `name` 不能解析成 `generic` | `proactive.py:1056` **硬拒 generic** ⇒ **主动消息整链失效**（约 25 个调用点），任何 capability 都补不回来 |
| ② | `description` 不能含平台别名子串 | `platform_compat.py:165-182` 拿 description 做**子串兜底** → 误判成 `onebot` → 插件去 `int(user_id)`（我们的会话 id 可以是任意串 → **崩**）+ OneBot 原生兜底（**硬失败**） |
| ③ | `description` 不能是 `cronjob` | `tts_tool_sanitizer.py:54` 判成定时任务伪事件 → 跳过空正文守卫（`main.py:18293-18295`）→ **正文可能发两次** |

#### ⚠️ 而我们**自己就埋着一颗**（约束②的活例子）

`OurPlatform` 原来的默认描述是 **`"情绪app 内置 Web 端（非 OneBot）"`** ——
含 `onebot` 子串，插件做子串兜底时会判成 **onebot**。
**否定句被当成断言**（"非 OneBot" 里就有 "OneBot"）。

之前没出事**只因为 `name="webchat"` 先被检查到了** —— 而"先被检查到"不是保证
（改 `name` 就失效）。已改成 **`"情绪app WebChat"`**，并把校验下沉到 `PlatformSpec`。

### 判据与注入验证

| 判据套件 | 结果 |
|---|---|
| `host/tests/test_response_bridge.py`（新，33 条） | **33 passed** |
| `host/tests/test_platforms.py`（新，21 条） | **21 passed** |
| `tools/chat_persona_check.py`（真浏览器，11 → **16 条**） | **16/16** |
| `tools/mobile_check.py`（照新设计改写"空片段表"判据） | **5/5** |

**注入验证 11 组**（每组都确认能失败；恢复后 sha256 逐字节一致）：

| 注入 | 抓它的判据 |
|---|---|
| 桥不再解析 `tool_calls` | **9 条**（三条路各有判据） |
| `description` 校验去掉 | 1 条 |
| `generic` 闸门去掉 | 2 条 |
| `MainAgentBuildConfig` 加回手写 `__init__` | 2 条 |
| `normalize_tools` 改回原样透传 | 1 条 |
| 签名兜底改回无条件吞 `TypeError` | 1 条（**第一次没抓住 → 已补接线判据**） |
| `agent.tool` 从 shadow 注册表删掉 | 1 条（**第一次没抓住 → 已补注册表断言 + 独立进程 import**） |
| `ToolSet.openai_schema` 保留空 description 键 | 1 条 |
| `_tools_of` 改回旧实现（恒返回 None） | 1 条 |
| `registry.get` 回落默认平台（串号风险） | 1 条 |
| persona 路由去掉 `skipHash`（复现无限循环） | **3 条** |

⭐⭐ **注入验证自己抓到我两次判据盲区**（本轮最值钱的过程）：
1. 我只测了 `_looks_like_signature_mismatch` **函数**，没测**接线** → 把接线改回"无条件吞"时
   **31 条判据全绿**。补了走真实调用路径的判据才抓住。
   **通则：函数对 ≠ 接线对**（本项目老坑「模块能工作 ≠ 被接上了」）。
2. `agent.tool` 的判据调 `install_shadow_package()` 后 import —— 而模块**已在 `sys.modules`**，
   把注册表那一项删掉**照样绿**。改成"断言注册表里有它 + **独立进程**真 import"。

### 🔴 本轮最重要的一条通则

**兼容层缺口的真实症状是「插件把它 catch 成一行 info 日志」——
靠读我们自己的源码永远看不出来，必须把插件真跑起来看它返回什么。**

本次 6 个 bug 里有 **4 个**是"真发一次请求 / 真点一次页面，看它返回什么"才现形的
（`No module named 'astrbot.core.agent.tool'` / `ToolSet is not JSON serializable` /
`base_url 为空` 这个假根因 / 人设页自刷）。
**单测全绿、接口判据全绿、页面能打开 —— 全都不算证据。**

---

## 1.5 ✅ 本轮已完成：人设链路打通 + 模型/测试弹窗（2026-09-21 晚）

### 起因（情绪原话）

> 「获取到的模型，与测试都用弹窗形式发送。把配置文件挪到会话旁边，功能冲突了。
> **聊天页面显示不带人格，陪伴设置页面也显示不出人格**」
> 随后澄清：「**2 不干了，主要是因为陪伴那边获取不到平台的默认人设。得改一下。**」

→ 所以「把配置文件挪到会话旁边」**取消不做**（情绪确认），本轮聚焦**人设链路**。

### 🔴 真根因（实测复现，不是推测）

陪伴面板「快速从已有人格导入」点「生成导入草稿」永远报：

```
生成失败：还没有读取到可用的主回复人格文本，请先让 Bot 触发一次对话或检查人格配置
```

**根因不是"没触发过对话"**（那句文案是**误诊**），而是宿主的 compat `Context`
**完全没有 `persona_manager` 属性**（全仓 0 命中）。链路：

```
page_api._roleplay_persona_prompt_for_id        (page_api.py:14799)
  └─ plugin._refresh_default_persona_prompt("") (daily_state.py:11073)
       └─ manager = getattr(context, "persona_manager", None)   → None
            └─ getter = get_default_persona_v3 if manager else None → None
                 └─ DEFAULT_PERSONA_PROMPT_FALLBACK            (daily_state.py:163)
  └─ prompt.startswith("未读取到 AstrBot 默认人格") → 报错      (page_api.py:14849)
```

⚠️ **文案会把排查带偏**：它让用户"让 Bot 触发一次对话"，但
`_refresh_default_persona_prompt` 的两个缓存写入口（`daily_state.py:11113`/`:11153`）
都在 `manager` 分支**之后** → `manager is None` 时**永远不写缓存**，
触发了也修不好，而且**一行日志都没有**（纯静默）。

### 做了什么

| 改动 | 文件 |
|---|---|
| **新增人格桥**（把我们的 `PersonaRepository` 适配成插件的 `PersonaManager`） | `host/astrbot_host/persona_manager.py`（新） |
| `Context` 暴露 `persona_manager` + 能力诊断（`persona_usable` 与"有管理器"分开报） | `host/astrbot_host/compat/core/star/context.py` |
| 装配：`_build_persona_manager()` + `service.personas` 注入 | `emotion_app/web/plugin_host.py`、`emotion_app/web/app.py` |
| 聊天页提示按**真实优先级**（会话绑定 > 全局）说，不再谎报"未设人设" | `emotion_app/web/static/app.js` |
| 「获取模型列表」与「模型测试」结果改成**弹窗** | `index.html` / `app.css` / `app.js` |

### 🔴🔴 本轮最贵的一课：这个修复**新解锁了一条删文件的路径**

插件的"已删除人格对账"（`main.py:3430`，启动 `main.py:8751` + `page_api.py:14426` 触发）
把**我们的**人格 ID 当**权威集合**，把"插件档案里有、我们这里没有"的判为已删除
→ `_retire_deleted_persona_store_sync()` **删 `.db`/`.json`（+WAL/SHM）**。

实测推演（`primary=""`，因为 `plugin_specific_persona_id` 为空）：

```
candidates   = {"default"}     ← data/plugins/…/persona_profiles/default.db
official_ids = {"糯米茨"}       ← 桥报出去的人设名
→ stale = ["default"] → 会删掉 default.db（**150 段真实数据** / 163 KB）
```

⚠️ **加桥之前这条路走不到**（`persona_manager is None` → 对账直接跳过）
→ **是我的修复把它打开的**，所以必须在**同一处**关上。

✅ **关法就是"照真实现"**：真 AstrBot 的 `personas_v3` **永远含一个 `default`**
（`persona_mgr.py:416-418` 没人格时 `append(DEFAULT_PERSONALITY)`）
→ 我们补上它，行为与真实现一致，同时天然堵住误删。
**判据**：`host/tests/test_persona_manager.py::test_publishing_persona_list_keeps_builtin_default`
+ `test_reconcile_simulation_never_marks_default_stale`（照着插件的 stale 算法抄一遍）。
**实测**：重启后 `default.db` sha256 `1c6bb963…` **逐字节未变**。

### 判据与注入验证

| 判据套件 | 结果 |
|---|---|
| `host/tests/test_persona_manager.py`（新，23 条） | **23 passed** |
| `tools/chat_persona_check.py`（新，真浏览器 11 条） | **11/11** |
| `tools/plugin_persona_check.py`（新，真浏览器 9 条） | **9/9** |
| `tools/provider_panel_check.py`（弹窗改造后） | **84/84**（原 70） |

**注入验证 8 组**（每组都确认能失败，恢复后 sha256 逐字节一致）：

| 注入 | 抓它的判据 |
|---|---|
| 去掉内置 `default` 闸门 | 5 条（含两条数据安全） |
| 读失败也标记"已初始化" | 1 条（数据安全） |
| 4 元组 → 3 元组 | 4 条 |
| `Context` 不暴露 `persona_manager` | 1 条 |
| 装配不传 `persona_manager` | 1 条 |
| 忽略 `default_persona_id` | 1 条 |
| `get_default_persona_v3` 可返 None | 3 条 |
| **聊天页提示回到旧行为** | 4 条（复现用户原话 `未设当前人设…`） |

⭐ **最强的一条**：把 `Context.persona_manager` 摘掉后**重启**，
插件层 `/roleplay/personas` 从 3 项掉到 2 项（糯米茨消失）、
`draft_from_persona` **逐字**返回用户报的那句错
→ 浏览器判据 **4 条失败**（④⑥⑦⑧）。**注入即复现原 bug = 判据真的盯着它。**

### 顺带修好的（同一个根因家族）

- `config_profile.session.default_persona_id` 之前是**死配置**（全仓 0 个消费方）
  → 现在它是 `get_default_persona_v3()` 的来源（面板「默认采用的人格」真的生效了）。
- `get_config(umo)` **仍返回 `{}`**（`HostPorts(config={})`）—— **本轮没修**，
  它拖着一批静默降级（`provider_settings` 18 个键 / `provider_tts_settings` 11 个键 /
  `timezone`）。⚠️ 修它时**务必保留 `umo` 为位置参数**：插件有 **12 处**按位置传 umo，
  改成 keyword-only 会**静默打断**它们。→ 已列入 §1 下一轮。

---


## 1. 下一轮要做的（按优先级）

> ✅ **2026-09-25 第二次同步**：`P-N4`（补预设）也**已完成**（50→57，可执行 19→26）。
> 现在**真正剩下的**是：`P-N1`（STT —— 情绪 2026-09-25 决定**先不做**）、
> `P-N2`（两项待定）、`P-N3`（平台自身功能）。
> 🔴 **手机 App 套壳仍是唯一"必做"缺口**（情绪 2026-09-25：「手机app暂时不搞」= 推迟，非取消）。
> 🆕 **生图参考图** + **人设参考图（含 per-persona 面板上传）** 已修好 → `docs/REF_IMAGE.md`。

### 🆕 W1-W7 — 陪伴 Wiki 读出来的 7 条待办（2026-09-25）

> 来源：作者发的 `陪伴wiki`（36 文件 / 1.9 MB），审读结论见 `docs/WIKI_REVIEW_结论.md`。
> 三条分报告：`WIKI_REVIEW_reference.md`(283行) · `WIKI_REVIEW_guides.md`(660行) ·
> `WIKI_REVIEW_extensions.md`(368行)。
> ⚠️ **这四份是本地文件、未进仓库**（情绪 2026-09-25：「**wiki 审读就不用上传了**，
> 给你看是为了更好理解陪伴插件」）→ 已在 `.gitignore` 里挡住。
> ⚠️ 除 **W1 已解决**外，其余**只读不改**（情绪：「先别改奥看一遍」）。

| # | 项 | 为什么值得做 | 状态 |
|---|---|---|---|
| **W1** | ~~插件面板警示~~ → ✅ **已解决（2026-09-25，情绪拍板「换端口」）**：**我们把服务端口从 6190 改成 6191** —— 不再是"加警示"，而是**从根上不撞**。插件那边继续保持它自己的默认 6190。⚠️ 外部 NAT 已同步改成 `31818 → 6191` | ✅ |
| **W2** | 给 `send_by_session` 补**点名判据** | `enable_precise_platform_send` **默认 true** → 主动消息**硬依赖**它；平台没运行会**静默跳过本次** | 待做 |
| **W3** | **实测** `target_platform=aiocqhttp`（陌生用户兜底） | 我们只注册 `webchat` → **首次给从未私聊过的用户主动触达可能失败** | 待做 |
| **W4** | 查 `together` 的 Quick Tunnel 默认开关 | `together.server.enabled` **默认 true**；`public_base_url` 留空会**自动拉起 cloudflared = 公网暴露**。⚠️ 实测我们进程**已在 127.0.0.1:6321 监听**（当前只绑回环，安全） | ✅ **已查（2026-09-29）**：`initialize()` **不开隧道**（全文无 `quick_tunnel.start`），真正开的是 `_ensure_mobile_room_access()`，调用点只有用户**主动发指令**（`main.py:728/1107`）；房间服务只绑 `127.0.0.1` 且有硬防护。该插件**已停用**，这个面消失 |
| **W5** | live-stream 硬编码路径 → **降级可见性** | 它 `importlib.import_module("data.plugins.astrbot_plugin_private_companion.main")`；我们是 `data/plugin_src/` → 必然失败，而 wiki 说它"优雅降级" = **静默失效** | ✅ **已修（2026-10-08）**：根因比记录更重 —— 这类 import **100% 失败**（全项目实测 **40 处**）。修法：加载器注册 `data.plugins.<dir>[.main]` **同对象别名**。真机 `image.available=true`。判据 3 条 + 注入 C1/C2/C3 |
| **W6** | 把 wiki 的诊断方法论抄进 `docs/` | 排障十层顺序 + 「什么时候算修复完成」5 条硬判据（现象消失/边界保留/可解释/**可回退**/**无串线**） | ✅ **已做（2026-10-08）** → `docs/排障方法论.md` |
| **W7** | 查指令派发：`is_command` 何时设 + wake prefix 剥离顺序 | wiki 点名陷阱：「早期版本只检查 `is_command`，而 AstrBot **移除唤醒前缀后不一定设置该标志**」→ 指令被当普通聊天**延时合并** | ✅ **已核实（2026-09-29）**：我们已实现（`dispatch.py:110/137`）；且**真 AstrBot 里根本没有 `.is_command`**，旧记录前提有误 |

> ✅ **两条已确认的好消息**（不用做）：
> ① **`webchat` 是官方支持路径**（v6.0.10 起纳入普通私聊）→ **不需要冒充 `aiocqhttp`**；
> ② **我们 A1 的取舍被官方文档三处独立背书**：「改图原图缺失时按设计**失败关闭**，
>    不应悄悄生成一张新图」→ 与 `reference_unreadable` 硬失败一致。

### 🎨 UI-1 — 面板前端改版（**已完成**，2026-10-09）

> 起因：情绪「用 skill 美化我们的 web ui」。**这是纯前端观感改造，不动任何能力**。

**做法**：先审计再动手（`redesign-existing-projects` 的流程），
保留既有技术栈（零构建原生 HTML/CSS/JS）与既有 IA / id / class，
只重做**设计令牌层 + 观感**。`design-taste-frontend` 的主体是给营销页/作品集的，
按它自己 §13 的声明，dashboard/密集产品 UI **不在范围内** —— 只借用了通用条款
（排版、单一强调色、状态齐全、反 AI-slop）。

#### 审计抓到的 **4 个真问题**（都不是"样式不好看"，是"装配漏了/有 bug"）

| # | 问题 | 后果 | 修法 |
|---|---|---|---|
| ① | `var(--line)` / `var(--text)` / `var(--input-bg)` **从未被定义**（配置文件那段样式在读它们） | `var(--line)` 解析成空 ⇒ `border:1px solid` 变 `currentColor` ⇒ 那圈描边是**亮灰的**，比周围抢眼 | 定义别名指向新令牌，并把"引用即须定义"写成判据 |
| ② | `.empty` 被**定义两次**，后一条（无 `text-align:center`）覆盖前一条 | 空态文字**贴在左上角**，右侧一大片空白（真机截图确认） | 删掉重复定义，只留一处；并把空态做成**有图标+标题+指引**的组合态 |
| ③ | `.modal` 的 `z-index` 写死 **20**，而 `.sidebar` 是 **50** | 手机端抽屉开着时打开的弹层被抽屉压住一半，表现像"按钮没反应" | 改用 `var(--z-modal)`，并加判据锁死栈序 遮罩<侧边栏<弹层 |
| ④ | 面板**没有 favicon**（`/favicon.ico` 实测 404） | 标签页空白图标；每次开面板打一条 404 日志 | 新增 `static/favicon.svg` + `<link rel="icon">` |

#### 主要改动

* **字体自托管**（`static/fonts/`，共 84 KB）：Geist + Geist Mono（**可变字体** 400..700 / 400..500，
  latin + latin-ext 子集，SIL OFL 1.1，随附 `LICENSE.txt`）。
  ⚠️ **不含中文字形** —— `--font-sans` 里显式列了
  `PingFang SC / MiSans / HarmonyOS Sans SC / Noto Sans CJK SC / Source Han Sans SC / Microsoft YaHei`
  （本机 `fc-match "sans-serif:lang=zh-cn"` 实测命中 **Noto Sans CJK SC**）。
  ⚠️ **没有**复制插件里那几个 5 MB 的 `MiSans-*.woff2`（那是 `memory_companion` 的资产）。
* **令牌化**：散落的 **40 处**裸十六进制收敛到令牌（`#262b36` 出现 5 次、
  `#0f1116` 10 次；同族色还被手抄成 3 个近似值 `#20242e`/`#191c23`/`#1a1f28`）。
  改后非 `:root` 区只剩 3 处刻意保留的字面量（遮罩 alpha、iframe 白底）。
* **层级**：表面 4 档（`--bg/--panel/--panel-2/--sunken`）+ 输入框用 `--sunken`
  （改版前输入框与卡片**同底色同描边**，一屏 20 个字段像 20 个框贴 20 个框）。
* **状态齐全**：按钮补 `:disabled`（**改版前没有** —— 点不动和能点长得一样）、
  `:hover` 与默认底色**不再同值**（`.icon-btn:hover` 原来写死 `#262b36`，
  而那正是 `button` 的默认底色 ⇒ 悬停只有 1px 描边变化）；输入框补聚焦柔光。
* **选中 vs 悬停**：`.list li` 的 `:hover` 与 `.active` 原来**都只是"描边变强调色"**
  ⇒ 分不出选中了哪条（而这是"点开进详情"的入口，选错=看错人的数据）。
  现在 `:hover` 中性底、`.active` 柔强调底 + 左侧 2px 标记条。
* **侧边栏**：激活态从**整块实心强调色**改为柔底+标记条（实心块抢走了界面最强的对比度，
  那是留给主操作的）；收起态（只剩 emoji 图标）补**纯 CSS 悬停标签**
  （`data-nav-title`，不引 JS/库）。
* **JS 清理**：13 处 `alert()` → 就近的内联提示（复用既有 `toast()`）；
  `toast()` 不再往内联样式里写主题色（那是"把主题抄进 JS"）。

#### 验证（三层，都要过）

| 层 | 工具 | 结果 |
|---|---|---|
| 源码级 | `tests/test_ui_tokens.py`（**新增 20 条**） | **20 passed** |
| 注入验证 | `tools/inject_ui_tokens_check.py` | **13/13 抓住**（基线 20 passed，逐字节还原） |
| 真浏览器 | `tools/ui_polish_check.py`（桌面 1280×900 + 手机 390×700） | **全过**，零 console 错误 |
| 字体真实性 | `tools/font_probe.py` | **全过**（三判据见下） |
| 全量回归 | `pytest tests host/tests` | **1180 passed, 1 deselected**（1160 → **+20**） |

**`font_probe.py` 的三条判据**（"文件在"根本不算数）：
① 文件 200；② `document.fonts.check()` 认账；
③ **与同类系统字体的实测宽度不同**（sans 对 sans、mono 对 mono），
且**可变字体的 400 与 700 宽度必须不同**（否则等于只有一个字重）。

#### 🔴 本轮的方法论收获（都已固化进判据）

1. **`var()` 引用与定义必须成对** —— 引用未定义的自定义属性**不报错、不警告**，
   只是静默解析成空。判据：`引用集合 ⊆ 定义集合`。
2. **同一选择器重复定义不报错**，后者静默覆盖前者 ⇒
   关键选择器必须有"**声明块数量 == 1**"的判据。
3. **判据要守"那件事"，不是守一个恰好相关的字面量** ——
   我第一版写"`class="primary"` 出现过"就断言主按钮 OK，
   注入验证当场证明**把发送按钮的 primary 去掉后它照样绿**（别的按钮还带着）。
   改成**逐按钮点名**（`#send`、`#new-session`）后才抓住。
4. **判据的正则要覆盖整条语句，不能只匹配最常见的那个形状** ——
   扫"JS 内联样式不许写主题色"时，我只匹配 `style.x = "..."`，
   而改版前那处写的是**三元** `= bad ? "var(--danger)" : "var(--muted)"`
   ⇒ 等号后面不是引号 ⇒ **看不见** ⇒ 注入不红（假绿）。
5. **测试环境的渲染假象会骗人**：本机（Ubuntu 无桌面）**没装任何 emoji 字体**，
   而面板的图标就是 emoji ⇒ 真浏览器截图里全是**豆腐块**。
   我差点把"截图里是方块"当成改版改坏了。装了 `fonts-noto-color-emoji`
   之后截图才可信。**通则：核对截图前先确认"截图里的异常是产品还是环境"。**
6. **判据的输入必须落在被测对象的声明覆盖范围内**：
   我用 `WiM0ма`（含西里尔 `ма`）测 Geist Mono 的"逐字符等宽"，
   而自托管的是 **latin 子集、不含西里尔** ⇒ 那个字符回落成系统字体、
   宽度明显不同 ⇒ 极差 5.75 ⇒ 报"它不是以 Geist Mono 渲染的"（**假红**）。
7. **断言失败先查清"哪个值才是对的"**（本轮又发生一次）：
   登录闸门判据报红，查清是**我的测试**裸 fetch 登录（不经过 `boot()`，
   而关浮层的路径只有"表单提交"和"带 cookie 刷新"两条）——
   三场景对照后确认**产品是对的**。

### ✅ P-N6 — **工具执行链**（**已完成**，见 §0.6）

**为什么排第一**：本轮把「工具调用**能被看见**」修通了（`response_bridge` 统一转换 +
`agent.tool` 补上 + `normalize_tools` 转格式），实测 `tool_called=True`。
**但那只到"模型说出了要调什么"为止** —— 插件真正**执行**工具，还要宿主提供：

| 缺口 | 插件怎么用 | 我们当前 |
|---|---|---|
| `FunctionTool.call()` 的**执行** | 插件的工具 handler 是它自己的 `@filter.llm_tool` 函数 | ✅ compat 有 `call()`，但**谁去调它**没人接 |
| `ToolCallsResult` 回传（把工具结果喂回模型） | `text_chat(tool_calls_result=...)` | ⚠️ 在 `_IGNORED_TEXT_CHAT_KWARGS` 里 —— **刻意不转发**（转发会让上游 400，除非拼成 `role:"tool"` 消息） |
| `context.get_llm_tool_manager()` 的**注册面** | `register_llm_tool` / `activate_llm_tool` | ✅ 有 `FunctionToolManager`，但插件注册进去的工具**没被交给模型**（只有插件显式传 `ToolSet` 的才有） |

**凭什么说是真缺口**：本轮实测那条试跑端点（`page_api.py:4175`）**故意**用
`handler=None`（它只要"模型说出要调什么"），所以它**不能**作为执行链的证据。
真正要执行的是主对话链（`main.py:19298` 起的那段 `tools_call_name` 分支）。

**任务**：先**真跑一次**插件带工具的对话（用一个会触发 `pc_generate_photo` 的输入），
看日志里有没有"工具结果回传"那一步；有缺口再照 `openai_source.py` 的
`tool_calls_result` 序列化实现补。⚠️ **别照猜实现** —— 先跑，看它缺什么。

---

### ✅ P-N5 — 让 `get_config(umo)` 返回**真配置**（**已完成**，见 §0.6）

**为什么排前面**：修人设时顺带查清了 —— 我们给插件的
`HostPorts(config={})`（`emotion_app/web/plugin_host.py`）→
`context.get_config()` **恒返回空字典** → 插件读不到任何全局配置。

⚠️ **本轮的另一条实测印证**：插件那条 tool-call 路径里的
`provider_settings.get("tool_call_timeout", 120)`（`private_image.py:6413`）
拿到的就是空 dict → **静默回落 120s**。本轮实测日志里那句
「上游 base_url 为空」正是从这条链路报出来的（它读不到配置，于是
`int(provider_settings.get(...))` 拿到默认值，再走空 base_url）。

**凭什么说这是真缺口**（实测调用点清单，共 20 处 / 12 个文件）：

| 配置键 | 被读次数 | 缺失后果 |
|---|---|---|
| `provider_settings.tool_call_timeout` | 4 处 | 静默回落 120s |
| `provider_settings.default_personality` | 1 处 | **已由人格桥绕开** |
| `provider_tts_settings.*`（11 个键） | 9 处 | TTS 判为"未配置" |
| 顶层 `timezone` | 1 处 | 静默回落 `Asia/Shanghai` |
| `provider_settings.web_search*`（7 个键） | 2 处 | 联网搜索判为不可用 |

**验收判据**：
- `context.get_config()` 返回的 dict 里 `provider_settings` / `provider_tts_settings`
  子键与 `data/cmd_config.json` **逐键一致**；
- `timezone` 能读到 `'Asia/Shanghai'`；
- ⚠️ **必须返回 plain `dict`**（`proactive_engine.py:9929`、`proactive_message.py:10484`
  非 dict 会抛且不在 try 内）。

🔴 **两条硬约束（踩过就静默坏）**：
1. **`umo` 必须保留为位置参数** —— 插件有 **12 处**按位置传
   （`get_config(target)` 等）。改成 keyword-only 会**静默打断**它们
   （多数被 `except Exception` 吞成 `{}`）。真实现签名就是
   `def get_config(self, umo: str | None = None)`。
2. **子键宁可缺也不要写错类型** —— 多处会 `int()` / `float()` 强转
   （`proactive_message.py:5094`、`private_image.py:6413`），传字符串会 `ValueError`。

---

### 🥇 P-N1 — STT（语音转文字）供应商支持（情绪 2026-09-21 确认要）

**为什么排在前面**：情绪明确说「stt 功能可以加」。而且**地基已经在了**。

**凭什么说地基在了**（实测）：
- `host/astrbot_host/compat/core/star/context.py:630` **已经有 `get_all_stt_providers()`**
  —— 说明兼容层早就留了这个口子。
- `providers_store.py` 目前把 `speech_to_text` 归在**如实拒绝**那一类
  （`KNOWN_UNSUPPORTED_PROVIDER_TYPES`），改成支持是**收窄拒绝面**，不是新造机制。
- AstrBot 那边有 **3 条 STT 预设**：`whisper` / `xinference_stt` / `mimo_stt`
  （已在 `provider_presets.py` 里，只是被 `storable_only` 过滤掉了）。

**⚠️ 必须先想清楚的一件事（实测发现的差异）**：
> **主陪伴插件 `astrbot_plugin_private_companion` 完全不碰 STT**（严格搜 `get_all_stt` /
> `provider_stt` / `STTProvider` / `speech_to_text` 在它的 `main.py` 里 **0 命中**）。
> 用 STT 的是 **`astrbot_plugin_together_companion`**（`main.py:303` `speech.stt_provider_id`、
> `:2142 _get_stt_provider()`、`:1034` 遍历 `get_all_stt_providers`）。

→ **所以 STT 对"陪伴"这条主线不是必需**。做它的正当理由是：
① 情绪要；② `together_companion` 已经在用；③ 面板上语音类供应商目前**一个都不能执行**（见 §2），
加 STT 能让这一类的配置真正有用。

**任务拆解**：

| # | 任务 | 验收判据（必须能失败） |
|---|---|---|
| **N1-1** | `providers_store` 把 `speech_to_text` 加入 `SUPPORTED_PROVIDER_TYPES`，并从 `KNOWN_UNSUPPORTED` 移出 | TTS/embedding 那两条既有判据的**同款**：能存进、能读回、`provider_type` 正确 |
| **N1-2** | `provider_presets` 的 3 条 STT 预设**不再被 `storable_only` 过滤** | 预设总数从 47 → **50**；`list_presets("speech_to_text")` 非空 |
| **N1-3** | 面板「模型供应商」类型筛选加**第 4 类**（语音转文字） | 真浏览器：切到该类能看到 3 条、徽标正确 |
| **N1-4** | 设置页「会话」段加 STT 字段（`stt_enable` / `default_stt_provider_id`） | 照 schema 渲染出来；`provider_select` 只列 STT 类模型 |
| **N1-5** | `adapter_is_executable` 对 STT 适配器的**诚实标注** | `openai_whisper_api` 等我们**执行不了** → `executable_by_us=false`（**不许假装能用**） |

**🔴 关键边界**：和 TTS 一样，**本轮只做配置层**。
STT 适配器（`openai_whisper_api` / `mimo_stt_api` / `xinference_stt`）**我们跑不了** ——
能不能执行由 `adapter_is_executable(type)` 回答，前端如实标注「本平台暂不能执行，仅保存配置」。

**❓ 待情绪定的一个问题**：STT 要不要**真能转写**（即真实现 `openai_whisper_api` 的调用）？
- **只做配置层** = 小活（照 TTS 的现成模式）
- **真能转写** = 大改（音频上传链路 + 面板录音控件），且**主陪伴插件不用它**
  （实测：`private_companion/main.py` 里 STT 相关 **0 命中**；用 STT 的是 `together_companion`）

**⚠️ 本轮唯一的真风险点（已实测确认是 bug，不是猜测）**：

`flatten_two_layer`（`emotion_app/web/provider_bridge.py:200`）**完全不看 `provider_type`** ——
它只按 `enable` / 悬空引用筛。→ 所以**非对话类的模型会一起进插件的 `ProviderManager`**。

**实测证据（2026-09-21 本机真环境，不是推理）**：

```
# 加一个 TTS 源 + 一个模型
POST /api/v1/provider-sources          {id:"tts-probe", provider_type:"text_to_speech", type:"edge_tts", ...}  → 201
POST /api/v1/provider-sources/tts-probe/providers  {model:"zh-CN-XiaoxiaoNeural"}                              → 201

# 插件自己的供应商接口现在返回：
对话 items: ['fast/gemini-3.5-flash', 'tts-probe/zh-CN-XiaoxiaoNeural']   ← 🔴 TTS 混进了对话列表
tts_items : []                                                            ← 而 tts_items 是空的
```

🔴 **含义**：插件会把一个**语音模型**当成**对话模型**去调。
这正是本项目最忌讳的"看着能用其实不能用" —— 界面上一切正常，真调用才炸在很远的地方。
（探针已删除，现场已恢复干净；上表是实测输出。）

**→ 这是 N1 之前必须先修的（记为 N0）**：

| # | 任务 | 验收判据（必须能失败） |
|---|---|---|
| **N0-1** | `flatten_two_layer` 按 `provider_type` **分流**：只有 `chat_completion` 进对话 ProviderManager | 加 TTS/STT/embedding 源后，**对话 `items` 不含它们**（就是上表那两条断言反过来） |
| **N0-2** | 非对话类**不静默丢弃** —— 要留日志/或分别进各自的列表 | 有日志；且插件若查 `tts_items`/`embedding_items` 能拿到对应模型 |
| **N0-3** | 回归：现有对话链路不受影响 | 全量测试 + 真 HTTP 端到端 |

> ⚠️ **这是我上一轮引入的缺陷**：我把 `provider_type` 从 1 类扩到 3 类，
> 但**宿主同步这一层没跟着分流**。扩类型时只想到了"存得下"，
> 没想到"存下的东西会被送去哪里"。**通则:扩一类枚举时，要顺着它流过每一层检查。**

---

### 🥈 P-N2 — 上一轮遗留的两个"等你定"（`P0_P1_TASKS.md` 附录·⑪ §7）

这两条**已记录但未决策**，是主动消息能不能真正好用的关键：

| # | 问题 | 现状 | 选项 |
|---|---|---|---|
| **N2-1** | **主动消息落哪个会话？** | 现在是"按 `user_id` 找到**用户自己那个**会话"（**确定性**，重启后不漂移） | ① 保持现状（推荐：确定性最重要）② 改成"注入你**当前打开**的会话"（多会话时要先定策略） |
| **N2-2** | **要不要接外部推送**（离线也能收到） | **刻意没做** —— 只有面板可见 | ① 不做（现状，最安全）② 接（要显式授权 + 安全设计，是最需谨慎的动作） |

---

### 🥉 P-N3 — 平台自身功能（与插件无关，情绪在用）

按 `docs/FEATURE_GAP_ANALYSIS.md` 的优先级，**扣除已被插件覆盖的**：

| # | 任务 | 为什么还做 | 状态 |
|---|---|---|---|
| **N3-1** | 生图参考图 | 差距分析 P5；**插件不一定覆盖**这一条 | ⏳ |
| **N3-2** | 情绪/关系状态 | 差距分析 P2 | ⚠️ **先确认插件是否已覆盖** —— 若覆盖则不做（避免重复劳动） |
| **N3-3** | 长期记忆（跨会话） | 差距分析 P3 | ⚠️ 同上；插件有记忆机制，**先确认** |
| **N3-4** | 日程 | 差距分析 P4 | ⚠️ 同上；插件有日程 |

> 🔴 **N3-2/3/4 动手前必须先查"插件是不是已经做了"** ——
> 路线已定「模拟 AstrBot 和陪伴的关系」，插件自己会做这些。
> **重复实现 = 白干**（这正是 2026-09-20 撤销 P2-2/2-3/2-5 的原因）。

---

### ✅ N0 + N1 + 日志页 已完成（2026-09-21）

**N0（非对话类混进插件对话列表）** —— 我上轮引入的缺陷，已修：
* 加了**唯一判据** `is_chat_provider_type()`（`providers_store.py`），
  两条写入路径都调它：**启动装配** `flatten_two_layer()` + **配置热同步** `_register_model()`。
* 🔴 **修的过程本身是个教训**：我最初只在 `flatten_two_layer()` 加过滤，
  **单测全绿**，但真环境**仍然泄漏** —— 因为热同步**不经过**那个函数。
  真环境实测复现 → 修复 → 复验：`items` 从
  `['fast/…','stt-probe2/whisper-1','tts-probe2/zh-CN-XiaoxiaoNeural']` 变成 `['fast/gemini-3.5-flash']`。
  → **通则：同一概念有多条写入路径时，判据必须唯一，且每条路径各自有判据守着。**

**N1（STT 配置层，情绪选"先只做配置层"）**：
* `speech_to_text` 进入 `SUPPORTED_PROVIDER_TYPES`（从 `KNOWN_UNSUPPORTED` 移出）；
* 3 条 STT 预设**自动变得可下发**（`list_presets` 的过滤照支持集动态算 → 47 → **50 条**，
  **一行过滤代码都没改**）；适配器 `executable_by_us` **全为 false**（能力诚实）；
* 面板「模型供应商」加**第 4 类**筛选 + 标签（"语音合成" / "语音识别" 区分开）；
* 设置页「会话」段加 `stt_enable` / `default_stt_provider_id`（默认**关**、默认不指定）。

**日志页（情绪当场追加的要求）**：
* 后端 `GET /api/v1/logs` + **内存环形缓冲**（`RingBufferHandler`，500 条）。
  🔴 数据源**不是** `logs/server.log` —— 那个文件混着 Hypercorn 输出且轮转不受控；
  内存缓冲还能在出口走**同一条 `scrub()` 脱敏**，**不引入第二条可能泄漏凭据的通道**。
* 面板新增「📜 日志」页：级别过滤 / 条数 / **自动刷新（进开、离停）** / 复制全部。
* 🔴 **实测抓到的真 bug**：我第一版漏了 `require_auth()` → 未登录 `curl /api/v1/logs`
  返回 **200**（隔壁 `/settings` 等都是 401）。服务绑 `0.0.0.0`，等于**公网可读运行日志**。已修。
* 🔴 **真浏览器判据当场抓到的第二个 bug**：我用 `api()` 取数，而 `/api/v1/logs` 是
  **AstrBot 信封**（`{status,data}`）→ 一行都画不出来，提示区却写着条数。
  改用 `apiData()`。**通则：这个项目有两套响应形状（裸对象 vs 信封），取数前先确认是哪套。**

### ❓ 仍未决（等你）

1. **Fish Audio TTS 本机到不了**（凭据**已验证有效**：hk3 上实测 200 / 40,541 字节真 MP3）。
   本机是 **DNS 投毒 + TLS 被 RST**，且代理 `:7897` 节点失效。
   → 要真出声得先解决本机到 `api.fish.audio` 的可达性，或换本机可达的 TTS。
   探针：`python3 tools/fish_tts_probe.py`（逐层报告 DNS/TLS/HTTP）。
2. **STT 要不要真转写**：现在是配置层。要真转写得先定用哪个（本机实测 Groq / 硅基流动 /
   DeepInfra 都可达，但都要 key）。
3. **🆕 真要接 onebot 机器人吗？**（本轮只把地基铺好，**没接**，因为不知道你有没有这计划）
   地基已就绪：`.env` 里加一行 `PLUGINS_EXTRA_PLATFORMS='[{"id":"21","name":"aiocqhttp",...}]'`
   就能多出一个平台实例（**不用改代码**，配错会拒绝启动）。
   ⚠️ **但那只是"宿主认识这个平台"**。真要让 onebot 的消息进来，还需要：
   ① 一个真的 OneBot 连接（收/发消息的客户端，本机现在**没有**）；
   ② 会话能绑定到该平台（面板「绑定平台」字段 —— **本轮没做**，仍只有全局默认平台）；
   ③ 投递时按 umo 第一段分发到对应连接（`SessionDelivery` 现在只写本地会话，
      **不往外发** —— 这是刻意的，见 `delivery.py` 模块 docstring）。
   → **你要接就告诉我**，否则保持现状（只有 Web 端，零外部耦合）。

**本轮已确认可用、不需要你决定的**：插件的 function calling 链路已修通
（实测 `tool_called=True`）—— 这条以前是**完全静默失效**的，现在能用了。

## 2. ⚠️ 一个必须让情绪知道的诚实边界：预设"能执行"的比例

上一轮我说了「47 条预设里只有 19 条能执行」，但**没把话说透**。实测拆开是这样的：

| 类别 | 预设总数 | 我们能执行 | 卡在哪 |
|---|---:|---:|---|
| 对话 `chat_completion` | 31 | **19** | 12 条是 AstrBot 专有适配器 |
| 语音 `text_to_speech` | 11 | **0** | 全部是 AstrBot 专有适配器 |
| 嵌入 `embedding` | 5 | **0** | 同上 |

**12 条"不能执行"的对话预设是哪些，以及为什么**（实测 `api_base`）：

| 预设 | 卡在哪 | ⚠️ 值得注意 |
|---|---|---|
| Anthropic / 智谱 GLM / Groq / OpenRouter / AiHubMix / LongCat / 小米 MiMo / 胜算云 | 用的是 AstrBot 的**专有适配器**（如 `anthropic_chat_completion`） | **它们的 `api_base` 看着就是 OpenAI 兼容的**（`/v1`、`/openai/v1`）→ **我们其实只需要一个 `openai_chat_completion` 就能跑**，只是预设里写的是专有 adapter |
| Google Gemini | `googlegenai_chat_completion` | 另有 `google_gemini_openai`（OpenAI 格式）**可执行** |
| Kimi 编程套餐 / MiniMax 套餐 / 小米 MiMo 套餐 | 套餐类专有协议 | — |

> 🎯 **下一轮的低成本高收益项（N4）**：给这些厂商**补一条 OpenAI 兼容格式的预设**
> （照 `google_gemini` → `google_gemini_openai` 的现成先例）。
> 这样"能执行"的预设数能从 19 涨到 **25+**，**而不用实现任何新适配器**。
> ⚠️ 但要**逐条实测**那个 base_url 真的接受 `/chat/completions`（照我们的铁律：不许猜）。

---

### P-N4 — 补 OpenAI 兼容格式的厂商预设（低成本）

| # | 任务 | 验收判据 |
|---|---|---|
| **N4-1** | 对 §2 表里 `api_base` 像 OpenAI 兼容的厂商，**逐个 curl 实测** `/chat/completions` 是否真通 | **能失败的判据**：不通的**不许加**（加了就是假装能用） |
| **N4-2** | 实测通的，补 `openai_chat_completion` 版预设（id 后缀 `-openai`） | 预设数与 `executable_by_us=true` 的数量同步上升；逐字段对源数据比对 |
| **N4-3** | 面板上把"同厂商两种格式"**摆在一起**，让用户能选 | 真浏览器：能看到并选到 |

**⚠️ 前提**：需要一个能用的 key 才能实测。**若没有对应厂商的 key，就不加那条** ——
宁可少一条，也不要一条没验过的预设（那就是"看着能用其实不能用"）。

---

## 3. 明确**不做**的

| 不做 | 为什么 |
|---|---|
| 新框架相关的一切（P2-2 主动消息 / P2-3 情绪关系 / P2-5 日程） | 路线已定：插件自己会做；重复=白干 |
| 重写/移植陪伴的机制 | 几万行；且我们是"宿主"不是"重写者" |
| 冒充 `aiocqhttp` 平台 | 伪造能力（作者判据明确禁止）；且**实测不需要**（插件对 `webchat` 是一等公民支持） |
| 伪造"已送达" | 作者验收判据③：能力不对等要**如实 degraded** |
| 多用户/群聊 | 单用户自用；插件那部分是多用户设计，搬来要做减法 |
| 把插件 45~54 个私有 API 一次补全 | **按需补**（`event_dispatch.py:52-66` 是天然适配层） |
| 实现 AstrBot 的专有适配器（anthropic/googlegenai/zhipu…） | 成本高收益低 —— **改走 N4（补 OpenAI 兼容预设）** |

---

## 4. 建议的动手顺序

```
N0 修「非对话类混进插件对话列表」   ← 🔴 必须先修：已实测确认的 bug（我上轮引入的）
   ↓
N1 STT 配置层（情绪已确认要，地基已在）      ← 最小闭环
   ↓
N2 两个遗留决策（问情绪，不改代码）           ← 决定主动消息最终形态
   ↓
N4 补 OpenAI 兼容预设（逐条实测，不许猜）     ← 能执行的预设 19 → 25+
   ↓
N3-1 生图参考图                              ← 平台自身功能
   ↓
N3-2/3/4 先查"插件是否已覆盖"，覆盖了就不做
```

**为什么 N0 排第一**：它不是"改进"，是**已实测确认的缺陷**（§1 P-N1 里那张实测表）。
不修它就先加 STT，等于**把第 4 类模型也一起送进插件的对话列表** —— 缺陷会扩大而不是收敛。
而且它很小（改 `flatten_two_layer` 一个筛 + 两条断言），**先修再扩**才安全。

**然后做 N1**：情绪明确要的、地基已经有了、且能让"语音类预设一个都不能执行"的尴尬改善一半。

---

## 5. 开工前必须先查的（不许猜）

1. **STT 只做配置层还是真能转写？** —— 见 §1 P-N1 末尾。**要问情绪。**
2. **N4 的厂商 key 有没有？** —— 没有 key 就没法实测，没实测就不许加预设。
3. **N3-2/3/4 插件是否已覆盖？** —— 动手前先 grep 插件源码，覆盖了就不做。
4. ~~`flatten_two_layer` 会不会受 STT 影响？~~ —— ✅ **已实测确认：会，而且现在是 bug**
   （§1 P-N1 的实测表）→ **已升级为 N0，必须先修**，不再挂在"待查"里。

---

## 6. 已完成的（不再列细节，去 `P0_P1_TASKS.md` 查）

命令派发 · 结果回写 · LLM 钩子（记忆注入）· 模型端口 · 投递端口 ·
umo 约定（`11:FriendMessage:<uid>`）· 面板绑定 · 按会话配置（方案 B）·
插件配置持久化（0 → 747 项）· 「获取模型」按钮 · 模型供应商两层结构 + 3 类 + 预设 ·
设置页配置文件式（会话 + 上下文）· **工具执行链（P-N6，2026-09-22）** ·
**全局配置（P-N5，2026-09-22）**。

---

## 7. 🎯 离最终目标还差什么 → **`docs/GAP_TO_GOAL.md`**

> 那份文件专门回答"还差什么"（不是"下一步做什么"），带证据、按优先级排，
> 并列出**等情绪拍板的 9 项决定**。
>
> **一句话**：后端主干已通透，**卡口全在「手机 App 套壳」——零代码、零任务拆解**；
> 其余要么是插件自己会做的（情绪状态/长期记忆/日程），要么是等你定的事。
