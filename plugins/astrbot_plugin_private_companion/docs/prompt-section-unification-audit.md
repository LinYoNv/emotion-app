# PromptSection 全仓库统一化审计

审计日期：2026-09-03
审计分支：`audit/prompt-section-unification`
基线提交：`30d7ed07`
审计范围：插件生产代码、`_conf_schema.json`、提示词相关测试；不扫描用户数据内容，不改运行时代码。

## 1. 结论

改造可行，而且应该做，但不能把现有字符串机械替换成 `prompt_section(title, content)`。

当前 `prompt_section()` 只能表达 `title + content`，不足以覆盖仓库已有的下列结构：

- 用户对话主链的 XML section、结构化群聊历史和 CDATA 分段协议；
- 后台 LLM 的 system/user 双通道、JSON 输出契约、枚举短答和正文改写；
- TTS、成员安全、工具 Schema 等必须逐字节保持的协议；
- 生图的 positive/negative、NAI 权重、冲突清洗和分区预算；
- 外部插件透传、用户自定义模板及其占位符；
- 请求级排序、去重、放置、缓存、审计和 Token 统计。

推荐保留现有三层职责，但只保留一套提示词 authoring model：

1. `conversation_prompt_section.py`：定义 `PromptSection`、内容节点和所有 renderer；这是唯一允许编写提示词结构的入口。
2. `prompt_surface.py`：只负责收集、排序、去重和分区 `PromptSection`，不再接受散装的 `key/content/title/source`。
3. `conversation_injection_plan.py`：只负责请求级 placement、marker、幂等、物化和审计，不再通过 `structured/opaque` 布尔位猜内容类型。

业务模块必须直接创建 `PromptSection`。标题、正文模板、变量绑定、子板块和协议类型在同一个构造调用中声明；`【标题】` 只允许由兼容 renderer 输出，不再由业务字符串手写。

## 2. 扫描基线

本轮按实际 sink、调用函数和 AST 字符串节点交叉检查，不能把以下数字直接理解为待替换数量：

| 指标 | 当前结果 | 说明 |
| --- | ---: | --- |
| 生产代码中的 `【...】` 匹配 | 443 | 分布于 38 个 Python 文件，包含真实标题、协议、正则、占位符和用户内容处理 |
| `prompt_section` 文本命中 | 112 | 含定义、导入和调用；AST 确认实际构造调用 69 处，分布于 17 个文件 |
| `include_heading/as_section(s)` 文本命中 | 232 | AST 确认约 79 个相关函数或调用点 |
| `structured=True/opaque=True/PLACEMENT_TOOL_CONTRACT` 命中 | 37 | 其中约 15 个 `structured=True`、5 个 `opaque=True` |
| 手写会话 XML 业务生产点 | 2 | `main.py:11376`、`proactive_message.py:3521` 的自主分段 CDATA |
| 直接低层 LLM sink | 77 | 23 个文件；另有通过 `caller/getattr` 和外部 bridge 的间接 sink，实际审计入口不少于 79 处 |
| Schema 中 prompt 相关节点 | 81 | 包含嵌套配置和 Legacy flat config 的重复声明 |

### 扫描方法

- 从 `_llm_call()`、`context.llm_generate()`、`provider.text_chat()`、`ProviderRequest`、`req.system_prompt`、`req.prompt` 反向追踪生产者。
- 扫描 `PromptSurface.add()`、`ConversationInjectionPlan.add()`、`_append_turn_prompt_fragment_by_position()` 和直接 request 写入。
- 对 `【】`、手写 XML、`include_heading/as_section(s)`、`structured/opaque` 做 AST 级函数归属。
- 单独审计 `PhotoPromptSection`、NAI/Image Companion bridge、TTS 和 FunctionTool Schema。
- 将日志、正则、UI、消息占位符、用户原文和解析器规则标为非自动替换项。

### 2.1 反向数据流复核

只扫 LLM sink 和标题字面量仍不够。第二轮复核继续从已经进入 section 的动态值向上追踪，发现一类容易漏掉的生产者：领域模块先返回 `instruction`、`voice`、`reason` 或 `prompt` 字符串，调用方再把它包进 `PromptSection`。这仍然不满足“提示词在所属功能的 section builder 内一次定义”的目标。

需要补充迁移的上游生产者：

| 位置 | 当前数据流 | 修改方式 |
| --- | --- | --- |
| `companion_interaction_expression.py:expression_decision_prompt` | 拼接互动表达文案，`main.py` 再包装 | 领域层保留结构化决策；新增所属功能 section builder，在同一次构造中完成标题、固定规则和变量绑定 |
| `domains/affect/reply_temperature.py:_instruction_for/compose_reply_temperature` | projection 内携带可注入的 `instruction` | projection 只返回 tier、score、signals/codes；主链 section builder 根据这些字段生成正文 |
| `domains/social/group_mood.py:summarize_group_mood` | 返回自然语言摘要，`group_observation.py` 再包装 | 返回 mood projection；由 `group.social_mood` builder 统一生成文案 |
| `domains/social/group_moments.py:format_group_moments_prompt` | 返回多行历史文案，调用方再包装 | 返回筛选后的结构化 moment 列表；section 使用 `PromptList` 或 `XmlElement` 渲染 |
| `domains/social/roleplay_strength.py:project_roleplay_strength` | projection 内携带可注入的 `voice` | projection 只保留等级和数值；`group.roleplay_strength` builder 生成 voice 规则 |
| `domains/social/joke_boundary.py:joke_guard_suggestion` | `reason` 直接成为群聊提示词正文 | 返回 reason code、blocked、sensitivity；section builder 将 code 映射成提示文案 |
| `group_cycle_boundary.py:build_group_cycle_boundary` | 返回包含完整 prompt 的字典，`main.py` 再包装 | 只返回 active/phase/topic/private boundary；由 `group.cycle_privacy_boundary` builder 构造英文规则 |
| `scene_context.py:_format_companion_scene_snapshot` | 返回复用于对话、主动和生图的扁平 prompt | 新增 canonical scene section；各消费者按 `BODY_ONLY`、`PHOTO_PROMPT` 或 conversation XML 选择 renderer |
| `extension_api_diagnostics.py:get_realtime_context` | 在 scene prompt 上继续用 f-string 追加活动与连续性 | 返回 section/manifest 与兼容字符串视图；追加信息作为同一 section 的字段或子项 |
| `proactive_chat_runtime_bridge.py:_run_prepare` | 将外部 `prompt_fragment` 直接拼进 `system_prompt` | bridge 契约携带 typed section；无法升级的外部版本明确包为 `ExactText` 并记录来源 |
| `relationship_policy.py:relationship_stage_prompt` | 当前无生产调用，但保留了一套可注入文案生产器 | 若确认无消费者则删除；否则改成只由所属 section builder 暴露 |

这类迁移不应让纯领域模块依赖 XML renderer。领域模块负责可测试的事实、等级、枚举和 reason code；靠近 LLM 边界的功能 adapter 负责创建 `PromptSection`。这样既保持依赖方向，也保证最终提示词文案不在两个模块中各写一半。

## 3. 当前架构问题

### 3.1 同一 section 有三套表示

- `conversation_prompt_section.PromptSection` 只有 `title/content`。
- `prompt_section()` 实际返回普通字典，不返回 `PromptSection`。
- `PromptFragment` 和 `ConversationInjectionBlock` 又各自保存 `key/title/source/content/metadata`。

结果是标题常在业务函数、调用点和排障描述表中重复出现，容易产生当前看到的“带 `【】` 与不带 `【】` 的同字符标题并存”。

### 3.2 内容类型靠布尔位旁路

- `structured=True` 表示调用者已经手工渲染 XML。
- `opaque=True` 表示不能解析或重排。
- `PLACEMENT_TOOL_CONTRACT` 又隐式包含 opaque 语义。

这些状态可以互相组合，类型系统不能阻止错误输入。普通字符串一旦误标为 `structured`，就会绕过 XML 转义；精确协议在部分入口仍会经过 `strip()`，也并非真正的逐字节保护。

### 3.3 结构在中途被降级为字符串

- `PromptSurface` 用 `repr(content)` 去重，不同 key 但正文相同的语义块可能被误删。
- `merge_policy=append` 会把结构内容转换成字符串后用空行拼接。
- `ConversationInjectionPlan` 会剥离预渲染 XML root，再重新拼 root。
- 排障页在没有 modules 时调用 `_split_prompt_modules_by_heading()`，用正则从 `【标题】` 反推结构。

这些路径都说明结构不是贯穿管线的一等数据。

### 3.4 renderer 的容错会掩盖错误

- 标题为空时自动回退为“提示词片段”，导致漏标题不会在开发期失败。
- 任意 Mapping 都会被解释为 XML 标签。
- 非法 XML key 会被静默替换，可能出现不同 key 映射到同一 tag。
- 列表元素名依赖 `_list_item_tag()` 的硬编码特例。

目标 API 应在构造时验证，而不是在最终渲染时猜测。

## 4. 目标 authoring model

### 4.1 唯一业务入口

建议把 `prompt_section()` 改为 keyword-only，并返回冻结的 `PromptSection`：

```python
section = prompt_section(
    key="reply.segmentation",
    title="回复分段控制",
    source="segmented_reply",
    template=(
        "你可以自行决定是否把本轮回复分成多条消息。\n"
        "每个消息边界使用：{split_marker}"
    ),
    variables={
        "split_marker": prompt_cdata(LLM_SEGMENT_MARKER),
    },
    metadata={"scope": "main_conversation"},
)
```

最低数据模型：

```python
@dataclass(frozen=True, slots=True)
class PromptSection:
    key: str
    title: str
    source: str
    content: PromptContent
    children: tuple["PromptSection", ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
```

`key/title/source` 应为必填字段；不再静默生成“提示词片段”。标题留在对应业务提示词附近，不建立全局标题常量表。

### 4.2 内容节点

为覆盖现有复杂度，需要少量明确类型，而不是更多布尔开关：

| 类型 | 用途 |
| --- | --- |
| `PromptText` | 固定正文、变量和条件片段的有序拼接；保留换行和 Markdown |
| `PromptField` / Mapping | 身份、状态、JSON 契约等具名字段 |
| `PromptList` | 有序规则、候选项、示例和历史记录 |
| `XmlElement` | 群聊历史等已有的明确 XML 节点 |
| `PromptCData` | 分段标记等必须在 XML 中原样可见的内容，统一安全拆分 `]]>` |
| `ExactText` | TTS、隐藏标签、外部协议等禁止 trim、转义和重排的内容 |

不要求把每句话拆成字段。普通板块仍可是一段 `PromptText`；只有本身存在字段、子板块或协议时才细化。

### 4.3 子板块和变量

同一功能的固定规则、变量和子板块必须在同一个构造树中：

```python
prompt_section(
    key="group.persona_denoise",
    title="群聊人格降噪",
    source="group_observation",
    template="当前发言者：{sender}\n{rules}",
    variables={
        "sender": prompt_value(sender_name, trust="untrusted_identity"),
        "rules": denoise_rules,
    },
    children=(
        prompt_section(
            key="group.joke_boundary",
            title="群聊玩笑边界",
            source="group_observation",
            content=joke_boundary,
        ),
    ),
)
```

构造器必须检查缺失或多余占位符。用户输入、外部资料和内部规则应有 trust/source 元数据，XML renderer 统一转义；业务调用点不自行 escape。

### 4.4 渲染模式

渲染模式由 sink 选择，不由业务正文手写：

| 模式 | 输出用途 |
| --- | --- |
| `CONVERSATION_XML` | 直接参与用户回复的主 LLM 上下文 |
| `LEGACY_BLOCK` | 兼容后台任务的 `【title】\ncontent` |
| `LEGACY_INLINE` | 兼容少量 `【title】content` |
| `BODY_ONLY` | 枚举判断、正文改写、生成内容等不能增加标题的任务 |
| `EXACT` | TTS、隐藏标签、外部工具和其他逐字节协议 |
| `PHOTO_PROMPT` | positive/negative、自然语言、传统标签和 NAI 权重渲染 |

`PromptDocument` 可以负责 section 顺序及 system/user 通道，但不能成为第二套提示词内容格式。它只装载 `PromptSection`：

```python
document = PromptDocument(
    system=(system_section,),
    user=(task_section, input_section),
)
```

### 4.5 下游 API

目标调用形态：

```python
surface.add(section, priority=20)
plan.add(section=section, placement=PLACEMENT_TURN_TAIL, marker=marker)
render_prompt_sections(sections, mode=PromptRenderMode.CONVERSATION_XML)
```

应删除：

- `PromptSurface.add(key, content, title=..., source=...)`；
- `ConversationInjectionPlan.add(content=..., title=..., structured=..., opaque=...)`；
- 所有业务函数的 `include_heading/as_section/as_sections`；
- 业务模块手写 `<private_companion_context>`、`<section>` 和 CDATA；
- 先 render、再传给 plan、再由 plan 拆 root 的路径。

placement、marker、temporary 和 merge policy 属于交付编排，继续由 plan 管理，不塞进正文模板。

## 5. 用户对话主链迁移清单

下表中的函数直接或间接进入回复用户的主 LLM。目标 wire format 为 XML，除明确标注的工具协议外都应返回 `PromptSection` 或 `tuple[PromptSection, ...]`。

### 5.1 核心管线

| 位置 | 当前职责 | 修改方式 |
| --- | --- | --- |
| `conversation_prompt_section.py:12-216` | section 类型和 XML renderer | 合并 dict/dataclass；增加 key/source/content nodes/render mode；严格校验 |
| `prompt_surface.py:16-164` | 收集、排序、正文去重、预渲染 | `add(section)`；只按 key/merge policy 去重；保持结构到最终 flush |
| `conversation_injection_plan.py:67-555` | placement、marker、materialize、structured/opaque 旁路 | block 持有 section；typed exact；结构化 append；禁止预渲染 XML |
| `main.py:13802-13922` | 主链公共 append/materialize helper | 参数改为 section；删除 content/title/source/structured/opaque 并行入口 |
| `main.py:14325-14503` | 并行 collector 和 surface 接入 | collector 统一返回 section；失败信息与业务 section 分离 |
| `event_dispatch.py:1428-1529` | 排障模块说明及 `【】` 反解析 | 直接消费 section manifest；描述只按 key 保存，不再从文本拆标题 |

### 5.2 被动私聊和群聊 surface

主入口为 `passive_state_pipeline.py:inject_humanized_state`。以下每个 key 都要改为由生产函数直接返回完整 section，调用点只做条件选择和 priority：

| Key / 位置 | 当前生产者 | 修改重点 |
| --- | --- | --- |
| `persona.core_emphasis` / `passive_state_pipeline.py:29` | `_persona_core_emphasis_prompt_section` | 补 key/source；返回 typed section |
| `reply.style` / `main.py:13104-13258` | 人格通道、回复风格、技术准确性 | 合并 `_format_*` 与 `_prompt_section` 双接口；标题只写一次 |
| `dialogue.outfit_continuity` / `daily_state.py:11334-11362` | 当前会话服装连续性 | 删除 `include_heading` |
| `turn.routine_check_boundary` / `main.py:15379-15401` | 轻量例行检查边界 | 删除默认旧标题输出 |
| `identity.fact_attribution` / `user_memory.py:8415-8424` | 事实主语与归属边界 | 返回 section，不在调用点补标题 |
| `state.emotion_inertia` / `user_memory.py:8017-8088` | 情绪惯性 | 删除 `include_heading` |
| `conversation.reunion` / `user_memory.py:8091-8125` | 久别重逢 | 删除 `include_heading` |
| `conversation.departure` / `user_memory.py:8128-8165` | 自然退场 | 删除 `include_heading` |
| `persona.preference_continuity` / `user_memory.py:8257-8293` | Bot 偏好连续性 | 删除 `include_heading` |
| `state.session_update` / `main.py:15535-15903` | 状态更新和私聊策略 | 返回两个 section，不再 `as_sections`/内嵌标题 |
| `state.lightweight/state.full` / `daily_state.py:12823-13122` | Bot 模拟状态 | 把状态字段构造成 Mapping/XmlElement；删除 `include_heading` |
| `life.context` / `daily_state.py:13111` | 模拟生活背景 | 返回 section；正文变量在 section 内绑定 |
| `important.dates` / `daily_state.py:13133` | 近期重要日期 | 返回 section |
| `memo.notes` / `daily_state.py:9373-9422` | 备忘便签 | 合并 body/section/legacy 三种出口 |
| `worldview.adaptation` / `integration_status.py:662-707` | 世界观和知识库参考 | 父 section + 知识库子 section；不手工拼第二个标题 |
| `realtime.activity_continuity` / `main.py:15617-15695` | 实时活动连续性 | 返回一个或多个 section，去掉双标题出口 |
| `conversation.departure` / `user_memory.py:8128` | 退场候选 | 返回 section |
| `identity.anchor` / `user_memory.py:9968-10010` | 私聊身份锚点 | 身份字段使用结构内容，删除 heading flag |
| `atrelay.recent` / `atrelay.py:1061-1103` | 最近转述动作 | 返回 section |
| `worldbook.mentions` / `worldbook.py:1526-1542` | 本轮关系网对象 | 返回 section |
| `environment.lightweight/perception` / `integration_status.py:1445-1513`、`main.py:14802` | 环境感知 | 统一 section；不在 wrapper 再提取 title/content |
| `rest.backlog` / `main.py:17069-17120` | 醒后补看消息 | 返回 section |
| `busy.reply_boundary` / `busy_reply_gate.py` | 忙碌回复节奏 | 生产函数直接返回 section |
| `rest.sleep_reply_boundary` / `user_rest_gate.py` | 休息中回复边界 | 生产函数直接返回 section |
| `group_share.reply_source` / `group_observation.py:3864-3916` | 群聊主动分享追问 | 删除 `as_section`，需要多个板块时返回 tuple |
| `proactive.reply_context` / `user_memory.py:9099-9196` | 主动消息、悬着话头、图片主体 | 固定返回 section tuple，删除 `as_sections` |
| `turn.short_reaction` / `user_memory.py:9914-9974` | 短反应锚点 | 返回 section |
| `turn.repeat_correction_boundary` / `passive_state_pipeline.py:1103` | 重复话题纠正 | 在调用处直接构造完整 section，不散装标题 |
| `reply.chain` / `forward_message.py:1691-1708` | 引用链 | 返回 section；层级用 XmlElement |
| `turn.continuation` / `passive_state_pipeline.py:1219` | 用户连续补话 | 当前文本作为不可信变量，不用 f-string 先拼正文 |
| `recall.query` / `event_dispatch.py:2622-2654` | 撤回消息查询 | 删除 `include_heading`；消息列表结构化 |
| `food.meal_care` / `daily_state.py:8247-8286` | 吃饭关心 | 删除 body/section 双 API |
| `food.menu` / `daily_state.py:8513-8559` | 食物候选 | 候选列表用结构节点 |
| `image.direct/image.*` / `private_image.py:2591-2608`、`passive_state_pipeline.py:1331-1529` | 当前图片、延迟图和引用图 | 每条分支直接构造 section；用户文字/视觉摘要为变量 |
| `creative.hidden`、`bookshelf.*`、`news.recent`、`web_exploration.recent`、`skill.*`、`companion.planner`、`detail.injection`、`timer.scheduling` / `main.py:14535-14734` | 并行私聊上下文 | 所有 collector 固定返回 section(s)，删除适配 lambda 中的 `include_heading/as_section` |

### 5.3 其他主对话 request 注入

| 位置 | 功能 | 修改方式 |
| --- | --- | --- |
| `passive_state_pipeline.py:385-664` | 表达学习、群聊上下文、撤回和自我时间线的提前注入 | section 直接交给 plan；删除 render 后 `structured=True` |
| `group_prompt_context.py:216-610` | 群聊 current/scene/history/slang XML | 保留 XmlElement；顶层直接返回 `PromptSection`，历史预算基于原始内容 |
| `group_observation.py:3344-3429` | 群聊防注入 | 返回 section；fallback 也走 plan，不直接写 request |
| `main.py:13041-13059` | 请求级环境感知 | 接受 section，不再 `.get(title/content)` |
| `main.py:13193-13261` | 回复风格和技术解释准确性 | 返回 section tuple；不预渲染 |
| `main.py:13295-13335` | 群聊高强度护栏 | 返回 section |
| `main.py:14817-14848` | 能力边界和平台边界 | 返回 section tuple；去掉 `structured=True` |
| `main.py:14859-14876` | 媒体投递真实性 | `_media_delivery_truth_prompt_sections()` 返回 typed sections |
| `main.py:14886-15253` | 转述、关系网、空间、生图、备忘、日程、创作等工具说明 | 每个 instruction 生产者固定返回 section(s)，不再 `include_heading=False` 后另传标题 |
| `main.py:15379-15401` | 私聊例行检查 | section 直接入 plan |
| `main.py:15913-16020` | 私聊/群聊经期边界 | 同一生产者返回 section，公开/私有只影响正文变量 |
| `main.py:16023-16158` | 群聊人格降噪与玩笑边界 | 构造成父子或 sibling sections；删除 legacy 双输出 |
| `main.py:16169-16253` | 非目标私聊身份防串 | 直接构造两个 section，不先 render XML |
| `main.py:16272-16650` | 转述目标摘要 | 删除 `include_heading` |
| `main.py:16665-16699` | 世界书提及对象 | 返回 section |
| `main.py:17826-17919` | DeepSeek 工具协议、被动回复工具边界 | 普通规则用 section；确属 exact 的部分声明 ExactText |
| `main.py:18069-18174` | 关系温度与表达决策 | 生产函数返回 section；英文标题不在调用点重写 |
| `main.py:18220-18367` | 屏幕隐私、群周期隐私 | 返回 section；所有 fallback 仍由 plan 渲染 |
| `main.py:18525-18633` | `pc_generate_photo` schema 标注 | 作为 tool schema exact section，最终 Schema 必须字节等价 |
| `daily_review.py:1580-1615` | 每日巡视柔性纠偏 | 返回 section，避免直接 system prompt 拼接 |
| `daily_state.py:5184-5291` | 天气查询上下文 | 返回 section；删除 system/prompt 双分支重复写入 |
| `forward_message.py:35-89,2121-2241` | 合并转发、引用链、富卡片 | formatter 返回 section；注册 materialized 时传同一对象 |
| `private_image.py:77-98,3756-3795,5799-5848` | 图片上下文及延迟图片专用 ProviderRequest | 不预渲染 XML；专用请求使用 PromptDocument |
| `tts_enhancement.py:2640-2945` | 主链 TTS 动态规则 | 普通规则转 section；标签协议使用 ExactText，不直接写 `req.system_prompt` |
| `group_member_safety.py:486-534` | 隐藏成员安全标签协议 | 用 exact section 登记；输出字节不变 |
| `companion_interaction_expression.py:746-803`、`domains/affect/reply_temperature.py:26-56` | 互动表达和回复温度的上游文案 | 领域层返回结构化 projection；`main.py` 的所属 section builder 统一生成正文 |
| `domains/social/group_mood.py:221-245`、`group_moments.py:257-279`、`roleplay_strength.py:63-126`、`joke_boundary.py:239-258` | 群聊社会状态的上游可注入文案 | 领域层返回事实/code；`group_observation.py` 统一构造四个 section |
| `group_cycle_boundary.py:68-100` | 群经期隐私 prompt 字段 | 返回边界事实，主链 builder 生成 section 正文 |
| `scene_context.py:812-1144`、`extension_api_diagnostics.py:37-64` | 跨对话、主动和生图复用的场景 prompt | canonical scene section + sink-specific renderer；公共 API 保留兼容字符串视图 |
| `proactive_chat_runtime_bridge.py:337-377` | Proactive Chat 外部 prompt fragment | typed bridge contract；旧外部 payload 只按显式 `ExactText` 兼容 |

### 5.4 手写 XML

| 位置 | 当前问题 | 修改方式 |
| --- | --- | --- |
| `main.py:11376-11379` | 手写自主分段 XML/CDATA | `prompt_section(..., content=PromptCData(...))` |
| `proactive_message.py:3511-3526` | 主动链重复手写相同 XML/CDATA | 复用同一个 section producer，不再维护第二份 XML |

## 6. 后台与功能性 LLM 清单

这些任务也应使用相同的 `PromptSection` authoring model，但第一阶段必须选择 `LEGACY_BLOCK`、`BODY_ONLY` 或 PromptDocument，保持现有 wire format 和解析契约。不能把它们强制改成用户对话 XML。

| 文件 / 位置 | LLM task 或功能 | 当前契约 | 建议 renderer |
| --- | --- | --- | --- |
| `atrelay.py:567-668` | `atrelay_rewrite`、`atrelay_receipt_rewrite` | 可发送纯文本 | `BODY_ONLY` |
| `command_handlers.py:3406-3553` | `companion_manual_diagnosis` | 长文本答疑 | PromptDocument / `LEGACY_BLOCK` |
| `command_handlers.py:5106-5147` | `natural_photo_ack_rewrite/done_rewrite` | 最终短回复 | `BODY_ONLY` |
| `content_companion_bridge.py:226-268` | 外部 Content Companion prompt | 外部任意格式 | `EXACT` |
| `creative.py:708-1080` | `creative_project/outline/review/extract` | JSON、项目符号或严格字段 | PromptDocument / `LEGACY_BLOCK` |
| `creative.py:1305-1413` | `creative_writing` | 原始创作正文 | `BODY_ONLY` |
| `daily_review.py:936-1391` | `daily_review` | 严格 JSON | PromptDocument |
| `daily_state.py:10710-10749` | `yesterday_summary` | 严格 JSON | `LEGACY_BLOCK` |
| `dreaming.py:570-1109` | `dream/diary_rewrite/diary_derivatives/diary` | 严格 JSON | `LEGACY_BLOCK`，Schema 用结构节点 |
| `event_dispatch.py:4283-4512` | `smart_message_debounce` | JSON | `LEGACY_BLOCK` |
| `event_dispatch.py:4666-4731` | `group_air_reply_guard` | `REPLY/SILENCE` | `BODY_ONLY` |
| `event_dispatch.py:4740-4787` | `group_followup_judge` | `YES/NO` | `BODY_ONLY` |
| `forward_message.py:989-1239` | `forward_message_image_vision` | 固定逐图转述行 | `BODY_ONLY` |
| `forward_message.py:2049-2117` | `forward_message` | 自然转述正文 | `BODY_ONLY` |
| `game_integration.py:730-813` | `game_emotional_afterglow` | 严格 JSON | `LEGACY_BLOCK` |
| `group_member_safety.py:649-732` | `group_member_safety` | 严格 JSON | `LEGACY_BLOCK` |
| `group_observation.py:155-250` | 群片段归档 | system + user，严格 JSON | PromptDocument |
| `group_observation.py:4182-4330` | `group_interject` | JSON | `LEGACY_BLOCK` |
| `group_observation.py:4432-4515` | `group_episode` | system + user，严格 JSON | PromptDocument |
| `group_observation.py:4583-4675` | `group_slang` | JSON | `LEGACY_BLOCK` |
| `main.py:8595-8602` | `reactive_poke_reply` | 直接回复正文 | `BODY_ONLY` |
| `main.py:10692-10728` | `group_question_wakeup_reply_review` | JSON | `LEGACY_BLOCK` |
| `main.py:16923-16959` | `rest_wakeup_judge` | JSON | `LEGACY_BLOCK` |
| `news_exploration.py:2919-2933` | `news_digest` | JSON | `LEGACY_BLOCK` |
| `news_exploration.py:3090-3128` | `external_event_self_link` | JSON | `LEGACY_BLOCK` |
| `news_exploration.py:4581-4604` | `web_exploration_query` | JSON | `LEGACY_BLOCK` |
| `news_exploration.py:4674-4689` | `web_exploration_digest` | JSON | `LEGACY_BLOCK` |
| `planning.py:46-107` | `detail` | system + user，严格 JSON | PromptDocument |
| `planning.py:680-818` | `daily_plan` 及纠偏重试 | 严格 JSON | PromptDocument |
| `proactive_engine.py:2427-2717` | `proactive_persona_judge` | JSON | `LEGACY_BLOCK` |
| `proactive_engine.py:6410-6470` | `full_test_detail` | 细化 JSON | PromptDocument |
| `proactive_message.py:1905-1934` | `screen_narration` | 纯文本摘要 | `BODY_ONLY` |
| `proactive_message.py:4648-4736` | `proactive_reference_rewrite` | 可发送正文 | `BODY_ONLY` |
| `proactive_message.py:5732-6091` | `proactive_send_review` | JSON/判定 | `LEGACY_BLOCK` |
| `proactive_message.py:6931-7120` | `response_review` | JSON/修订正文 | `LEGACY_BLOCK` |
| `proactive_message.py:9072-9201` | `voice/voice_repair` | `<tts>` 等固定协议 | `EXACT` |
| `proactive_message.py:12837-13029` | `photo_prompt` | JSON | `LEGACY_BLOCK` |
| `proactive_message.py:14342-14602` | `photo_reference_intent` | 单编号或 JSON | `BODY_ONLY` / `LEGACY_BLOCK` |
| `private_image.py:1565-1807` | `group_nsfw_image_review` | JSON | `BODY_ONLY`，保持自定义 prompt 契约 |
| `private_image.py:2922-3154` | `private_image_vision` | 固定 3/4 行标签 | `BODY_ONLY` |
| `private_image.py:4624-4704` | 图片回复 fallback/retry | 最终用户正文 | `BODY_ONLY` |
| `qzone_comments.py:307-361` | `qzone_comment_inbox_decision` | JSON | `LEGACY_BLOCK` |
| `qzone_feed.py:515-539` | `qzone_comment` | 可发布纯文本 | `BODY_ONLY` |
| `qzone_publish.py:413-455` | `qzone_publish_sanitize` | 可发布纯文本 | `BODY_ONLY` |
| `qzone_publish.py:472-701` | 发布测试、图片测试草稿 | JSON 或正文 | 按任务选择 `LEGACY_BLOCK/BODY_ONLY` |
| `qzone_publish.py:926-1071` | 配图 prompt | 图片提示词 | `PHOTO_PROMPT` |
| `qzone_schedule.py:718-825` | 长度修订、去重 | 可发布纯文本 | `BODY_ONLY` |
| `qzone_schedule.py:839-1044` | `qzone_publish` | 可发布正文 | `BODY_ONLY` |
| `qzone_schedule.py:1163-1318` | `qzone_emotional_vent` | 可发布正文 | `BODY_ONLY` |
| `reading_archive.py:61-77` | `bookshelf_password` | JSON | `BODY_ONLY`，Schema 节点 |
| `tts_enhancement.py:71-93` | `tts_spoken_conversion` | system + user，朗读正文 | PromptDocument；输出协议不变 |
| `tts_enhancement.py:2222-2245` | `tts_visible_translation` | 可见翻译 | `BODY_ONLY` |
| `tts_enhancement.py:4380-4563` | `tts_conversion/tts_postprocess` | JSON、标签和精确格式 | PromptDocument + `EXACT` |
| `user_memory.py:6184-6234` | `emotion_judgement` | JSON | `LEGACY_BLOCK` |
| `user_memory.py:7739-7855` | `smart_silence` | JSON | `LEGACY_BLOCK` |
| `user_memory.py:8533-8703` | `response_review` | JSON，可能含改写正文 | `LEGACY_BLOCK` |
| `user_memory.py:9253-9401` | `dialogue_episode` | JSON | `LEGACY_BLOCK` |
| `user_memory.py:10042-10122` | `memory_profile` | JSON | `LEGACY_BLOCK` |
| `worldbook.py:1241-1298` | `worldbook_registration` | 单段文本 | `BODY_ONLY` |

### 6.1 WebUI/Page 后台模型

| 位置 | 功能 | 建议 |
| --- | --- | --- |
| `page_api.py:2580-2844` | 表情素材视觉识别 | sections + `BODY_ONLY`，保留图片输入 |
| `page_api.py:3836-3877` | 参考图选择试运行 | PromptDocument，外部上下文作为不可信变量 |
| `page_api.py:600-627,4110-4125`、`photo_reference_metadata.py:304-365` | 参考图元数据审批 | PromptDocument，保持 system/user 和 JSON 契约 |
| `page_api.py:9480-9591` | 模型排障 | section + `BODY_ONLY` |
| `page_api.py:15197-15728` | 人格导入草稿和场景校准 | PromptDocument，严格 JSON |
| `page_api.py:15774-16728` | 人格标准化、扩写、修复、风格摘要 | PromptDocument；模型需产出的 legacy 人格块由 renderer token 生成 |
| `page_api.py:16896-16914` | Provider 测试 | section + `BODY_ONLY` |

## 7. 专用协议和不能直接改线格式的路径

### 7.1 FunctionTool Schema

`main.py:12308-13039` 的 20 个 `@filter.llm_tool` 由 AstrBot 根据函数签名和 docstring 生成 schema。它们不能包 XML，也不能因统一构造器改变参数名、required、description 或顺序。

需要为全部工具建立 `openai_schema()` 前后等价测试，尤其：

- `pc_generate_photo`；
- `pc_send_current_media`；
- `pc_find_reaction_image`；
- `pc_manage_memo`、`pc_manage_schedule`；
- QQ 空间、转述、群/用户查询和资料柜工具。

`main.py:18525-18633` 对 `pc_generate_photo.description` 的请求级动态标注属于 tool schema renderer，不能走普通 XML renderer。

### 7.2 TTS 和隐藏标签

- `tts_enhancement.py` 的 `<pc_tts>`、`<tts>`、`[[PCTTS:...]]` 会被后续代码解析。
- `group_member_safety.py:486-534` 的 `<pc_member_safety>...</pc_member_safety>` 是隐藏决策协议。
- DeepSeek 工具调用约束、工具结果和模型输出回执也可能依赖原始标记。

这些内容仍由 `prompt_section()` 登记 key/title/source，但 content 必须是 `ExactText`，renderer 不得 trim、escape 或调整空白。

### 7.3 图片和 NAI

`photo_prompt_context.py:80-104` 的 `PhotoPromptSection` 现有字段为 `name/source/positive/negative/protected/sanitize_conflicts`，并在 `:715-762` 按 traditional、natural、NAI 三种格式输出。

迁移方式：

1. 先让 `PhotoPromptSection` 成为 `PromptSection` 的 typed payload 或兼容 adapter，不改变 `_assemble()` 输出。
2. 再把其 renderer 注册为 `PHOTO_PROMPT`。
3. 保持冲突清洗、protected、分区预算、positive/negative 顺序和 prompt hash 不变。
4. NAI 的 `{}`、`[]`、`-1.5::...::` 是权重语义，不是普通标题，绝不能套 XML。

外部边界必须做字段级 golden：

- `companion/integrations/image_companion_bridge.py` 的 `image.task.v1` 字典；
- `nai_image_bridge.py` 的请求参数；
- 参考图、固定 prompt、负面 prompt 和服装裁决顺序。

### 7.4 外部和用户配置

- `content_companion_bridge.py` 接收外部插件生成的任意 prompt，只能作为 `ExactText` 透传。
- AstrBot 人格 prompt 属于宿主权威输入，插件不得解析其 `【】`。
- 用户自定义日程模板、主动模板、视觉 prompt 和 `{placeholder}` 是公开配置契约；第一阶段先整体视为 exact/template content，再逐项提供显式变量绑定。
- 用户原文、记忆原文、知识库原文和网页资料只作为不可信 content value，不能按标题解析。

## 8. 配置型提示词清单

`_conf_schema.json` 中同名项通常同时存在于分组配置和 Legacy flat config。迁移时只改消费方式，不随意修改配置 key 或 persona schema 语义。

### 8.1 对话和人格

- `reply_style_prompt`
- `persona_conversation_voice_prompt`
- `persona_creative_voice_prompt`
- `persona_planning_voice_prompt`
- `persona_inner_voice_prompt`
- `persona_proactive_voice_prompt`
- `worldview_adaptation_prompt`
- `roleplay_user_profile_prompt`
- `llm_controlled_segmenting_prompt`

### 8.2 日程、主动和创作

- `proactive_prompt_template`
- `creative_direction_prompt`
- `schedule_persona_prompt`
- `schedule_worldview_prompt`
- `daily_plan_prompt`
- `qzone_publish_style_prompt`
- `qzone_publish_image_style_prompt`
- `reactive_poke_prompts`
- `reactive_poke_back_prompts`

### 8.3 图片

- `private_image_vision_custom_prompt`
- `group_nsfw_image_review_custom_prompt`
- `daily_outfit_photo_prompt`
- `natural_language_photo_extra_prompt`
- `photo_generation_style_custom_prompt`
- `photo_generation_negative_prompt`
- `photo_generation_text2img_negative_prompt`
- `photo_generation_selfie_negative_prompt`
- `photo_generation_edit_negative_prompt`
- `photo_generation_fixed_prompt`
- `photo_generation_text2img_fixed_prompt`
- `photo_generation_selfie_fixed_prompt`
- `photo_generation_edit_fixed_prompt`

`photo_generation_prompt_format`、`photo_generation_negative_prompt_mode` 和 `custom_photo_tool_prompt_param` 是控制项/参数名，不是正文，不应被误判为 prompt 内容。

### 8.4 TTS 和状态文字

- `tts_mimo_style_prompt`
- `tts_extra_prompt`
- `main_user_mention_voice_prompt`
- `advanced_cycle_menstrual_prompt`
- `advanced_cycle_follicular_prompt`
- `advanced_cycle_pre_ovulation_prompt`
- `advanced_cycle_ovulation_prompt`
- `advanced_cycle_luteal_prompt`
- `advanced_cycle_pms_prompt`

状态描述虽然 key 含 prompt，但本质是一个 section 的变量值，不应独立渲染标题。

## 9. `【】` 分类与排除规则

### 9.1 必须迁移的业务标题

以下生产者仍自行拼 `【标题】`，应改为返回 `PromptSection`：

- `astrbot_knowledge.py:_format_roleplay_knowledge_context`
- `atrelay.py:_atrelay_tool_instruction/_format_recent_atrelay_context_for_prompt`
- `balance_awareness.py:_format_balance_awareness_prompt`
- `body_monitor_integration.py:format_health_prompt`
- `companion/integrations/reality_companion_bridge.py:_format_reality_touch_continuity_context`
- `daily_state.py` 中天气、状态、生活背景、重要日期、备忘、图片连续性、技能和预约 formatter
- `forward_message.py` 中合并消息、引用链和富卡片 formatter
- `integration_status.py` 中世界观、长期记忆和环境 formatter
- `llm_tool_actions.py` 中关系网、空间、生图、创作、备忘、日程工具 instruction
- `main.py` 中回复风格、技术准确性、环境、状态、私聊策略、群聊降噪和转述摘要
- `memory_companion_adapter.py:_memory_companion_compose_private_recall`
- `platform_compat.py:_platform_capability_prompt`
- `private_image.py` 中图片身份、视觉和最近群聊 formatter
- `proactive.py`、`proactive_message.py` 的主动作文板块
- `reading_archive.py`、`scene_context.py`、`self_timeline.py`、`user_memory.py`、`worldbook.py` 的对话上下文 formatter

后台 prompt 中的标题也要改为 section authoring，但继续由 legacy renderer 输出，见第 6 节。

### 9.2 不能机械删除的字面协议

- `page_api.py` 要求模型生成的 `【待确认说话方式】`、`【说话方式与对话习惯】`、`【错误格式】`。
- planning 缓存目前通过 `【A｜当前段硬框架】` 切分。
- TTS、成员安全、图片/NAI 的标记和权重语法。

解决方式不是继续在业务 prompt 中手写，而是调用 `legacy_heading_token("...")` 或对应 renderer/token node。解析器和正则仍可保留其字面量。

### 9.3 明确不改的非提示词

- 日志前缀和排障显示，例如 `[主链]`、`【语音消息规则】` 的泄漏检测。
- `event_dispatch.py`、`helpers.py`、`private_image.py`、`group_observation.py` 中识别标题、括号、消息占位符的正则。
- `【图片】`、`【语音】`、`【视频】`、`【文件】` 等消息摘要占位符。
- 用户输入、引用内容、知识库内容和历史数据中真实出现的 `【】`。
- UI 的功能名称、配置说明和文档示例。
- 对旧持久化 prompt trace 的清理匹配；在兼容周期结束后再删除。

因此 CI 不能全局禁止 `【】`，必须按“生产者函数 + 实际 sink”检查并维护带原因的 allowlist。

## 10. 易遗漏的依赖

1. `planning.py:19` 通过标题字面量拆日程细化缓存。必须先改成按 section key 分区，再移除旧标题。
2. `proactive_message.py:3264-3489` 大量 `prompt = f"{prompt}..."` 串接，部分还用正文文本判断是否已加入。必须改成 section key 集合，不可逐行替换。
3. `event_dispatch.py:_split_prompt_modules_by_heading()` 支撑排障页。迁移后应从 PromptSection manifest 读取，不能继续解析 wire text。
4. Token 预算、prompt hash、缓存签名和 Provider fallback 都读取最终字符串。迁移第一阶段需要 byte-for-byte golden，避免缓存整体失效。
5. `PromptSurface` 当前会跨 key 按正文去重。修复为 key-based 后可能增加以前被误删的片段，需要专门检查最终 Token。
6. `structured=True` 块可能包含多个 section；迁移时必须保留 children manifest，不能只包成一个新 section。
7. 主链有多个直接写 `req.system_prompt` 的 fallback。统一后所有 fallback 都应通过 plan，否则仍会出现双路径。
8. 外部插件热加载和可选依赖会改变 section 是否存在，构造器不得在 import 时硬依赖外部插件。
9. Prompt 配置是多人格有效配置的一部分；缺 key 跟随主人格、显式空值等既有语义不能因模板迁移改变。
10. 任何 renderer 都不得调用 `_single_line()` 处理 Markdown、CDATA、JSON 示例或 exact content。
11. `instruction/voice/reason/prompt` 形式的上游字段也可能是真正提示词；CI 和人工审计必须追踪这些字段到 section，不能以“调用点已经包装”为完成标准。
12. 公共扩展 API 可能把 prompt 交给 Image Companion、Proactive Chat 等仓库。迁移时必须同时提供版本化 typed contract 和旧字符串兼容视图，不能在未协商时改变跨插件字段。

## 11. 实施阶段

### 阶段 A：基础设施，不改变 wire

1. 扩展 typed `PromptSection`、`PromptText`、`PromptCData`、`ExactText`、PromptDocument 和 renderer enum。
2. 保留当前 mapping adapter，给旧调用发出测试期告警，不立即删除。
3. 为 XML、legacy block/inline、body-only、exact、photo 建立 byte-for-byte golden。
4. 修复 key 去重和结构化 append，但在切换业务调用前锁定现有输出。

### 阶段 B：主对话链

1. 先迁移 `PromptSurface` 及 passive pipeline。
2. 再迁移 request 级工具说明、身份/隐私边界、群上下文、图片上下文和 TTS 动态规则。
3. 用 `PromptCData` 替换两处自主分段手写 XML。
4. 排障页改读 manifest，删除主链 heading 反解析。
5. 验收主链最终只出现构造器生成的 `<private_companion_context>`。

### 阶段 C：后台和功能 LLM

1. 按第 6 节逐 task 迁移 source representation。
2. 第一轮使用 legacy/body renderer 保持模型输入逐字节一致。
3. JSON schema 和示例改为结构节点，但 renderer 仍输出原有文本。
4. system/user 双通道不合并，缓存前缀和重试 prompt 顺序不变。

### 阶段 D：专用协议

1. FunctionTool schema 建立 snapshot 后接入 tool schema renderer。
2. TTS 和成员安全接入 ExactText。
3. `PhotoPromptSection` 接入通用 section payload 和 PHOTO_PROMPT renderer。
4. Image Companion、NAI、Content Companion 做字段级/字节级回归。

### 阶段 E：删除兼容债务

1. 删除 `include_heading/as_section/as_sections`。
2. 删除 `structured/opaque` 布尔入口。
3. 删除业务函数的 mapping 返回和预渲染 XML。
4. 删除不再需要的 heading splitter；保留旧持久化数据迁移器到约定版本。
5. 开启 AST CI 强约束。

每个阶段独立提交，避免把基础设施、主链 wire 变化和专用协议迁移放进一个 PR。

## 12. 测试矩阵

### 12.1 构造器和 renderer

- 必填 key/title/source；空值、重复 key 和非法 key 报错。
- XML 文本/属性转义、Unicode、控制字符、孤立代理项。
- Markdown 换行、代码块、表格、列表和重复空格保持。
- Mapping/List/XmlElement 有序输出，非法 tag 不静默改名。
- CDATA 中 `]]>` 安全拆分，分段 marker 保持可复制。
- ExactText 前后空白、换行和字节完全一致。
- legacy block/inline 只由 renderer 生成 `【】`。

### 12.2 Surface 和 Plan

- priority + insertion order 稳定。
- 同 key 的 first/replace/append；不同 key 同正文不误删。
- append 不把结构对象转成字符串。
- stable/dynamic/turn/tool 四种 placement。
- 重复 render、冻结 plan、旧 marker 清理和外部 extra parts 保留。
- materialized fallback 与正常 plan 输出一致。
- manifest 不依赖最终文本反解析。

### 12.3 主链场景

- 单人格/多人格，主人格/副人格配置隔离。
- 私聊目标用户、非目标用户和群聊多成员身份。
- 群聊 current/scene/history，空历史和关闭历史注入。
- 普通文字、Markdown、图片、引用图片、转发、富卡片。
- 主动主链、被动主链、戳一戳、群插话、图片 fallback。
- LLM 自主分段、插件分段、TTS、DeepSeek 工具调用。
- 排障页显示的 key/title/source/content 与真实 request 一致。

### 12.4 后台和协议

- 每个 JSON task 的输出继续被现有 parser 接受。
- planning system/user 缓存段和 prompt hash 不变。
- 20 个 FunctionTool 的 schema 前后等价。
- TTS 标签、成员安全标签、NAI 权重和图片 positive/negative 等价。
- Image Companion task dict、Content Companion prompt 和用户模板透传等价。
- Token 预算分类、fallback provider 和 usage 统计不变。

## 13. 建议的 AST/CI 规则

在迁移完成后增加以下检查：

1. renderer 文件之外禁止手写 `<private_companion_context>`、`<section>`、`<![CDATA[`。
2. 实际 prompt producer 禁止原始 `【标题】`；非提示词和 legacy parser 通过带理由 allowlist 放行。
3. 禁止新增 `include_heading/as_section/as_sections`。
4. 禁止新增 `structured=True/opaque=True`。
5. 禁止 `PromptSurface.add(..., title=...)` 和 `plan.add(content=..., title=...)`。
6. 限制 `req.system_prompt/req.prompt` 直接写入到 InjectionPlan 兼容边界。
7. `_llm_call/provider.text_chat/ProviderRequest` 的固定 prompt 必须来自 renderer 或明确 exact wrapper。
8. `prompt_section()` 必须使用 keyword 参数并提供稳定 key/title/source。
9. 任何 allowlist 项必须注明原因、owner 和可删除条件。

## 14. 需要同步修改的测试

优先修改：

- `tests/test_conversation_injection_plan.py`
- `tests/test_conversation_prompt_structure_supplement.py`
- `tests/test_module_conversation_injection_plan.py`
- `tests/test_prompt_cache_and_parallel_context.py`
- `tests/test_technical_reasoning_prompt.py`
- `tests/test_task_prompt_cache_prefixes.py`
- `tests/test_passive_group_context_decoupling.py`
- `tests/test_cycle_reply_context.py`
- `tests/test_user_requested_photo_generation.py`
- `tests/test_tts_postprocess_tag_guard.py`

其中 `test_conversation_prompt_structure_supplement.py` 当前明确要求业务函数保留 legacy `【标题】` 输出，应改为：同一个 `PromptSection` 分别经过 XML 和 legacy renderer 得到对应 wire，而不是业务函数维护两种返回格式。

还需为第 6、7 节每个 task/协议增加 golden 或 schema snapshot。用户原文中真实出现 `【这个括号要保留】` 的测试必须继续保留，防止 CI 或 renderer 误删内容。

## 15. 完成定义

完成本改造必须同时满足：

- 所有插件自有提示词都由 `prompt_section()` 创建；
- 业务源码不再手写标题括号或会话 XML；
- 同一标题、正文模板和变量绑定只在所属功能附近定义一次；
- 主对话统一输出结构化 XML；
- 后台 LLM、工具、TTS、NAI、图片和外部插件保持各自 wire 契约；
- 排障、去重、预算和缓存直接使用 section 元数据；
- 日志、正则、UI、消息占位符和用户原文未被误改；
- 全量测试不新增相对上游基线的失败。

## 16. 实施结果

实施完成于 2026-09-04，基于 `origin/main@30d7ed07`。

- `conversation_prompt_section.py` 是唯一 authoring model。`PromptSection` 是严格、不可变的类型；`key/title/source` 必填，业务代码只能通过 keyword-only `prompt_section()` 构造。
- 内容类型覆盖文本、模板变量、字段、列表、XML、CDATA、精确文本、标题引用和生图 positive/negative 载荷。原始 `Mapping`、非法 XML 名称和错误 sink 会在构造或渲染时直接失败，不再自动清洗或猜测。
- `PromptSection.children` 只表示真实语义层级；用户对话 XML 保留递归嵌套，只含 children 的父 section 也会正常输出。`PromptDocument` 只负责 system/user 通道和显式 `PromptRenderSpec`，不构成第二套正文模型。
- 用户对话主链统一渲染为 `<private_companion_context>` XML；后台与功能 LLM 的 `【标题】正文`、body/JSON wire 由 `LABELED_*`/`BODY_ONLY` renderer 生成；TTS、成员安全、生图、NAI 和跨插件协议使用 `EXACT` 或专用 renderer，既有 wire 不变。
- `PromptSurface` 与 `ConversationInjectionPlan` 只接收 `PromptSection`。编排层只保留排序、placement、marker、merge policy、幂等与 manifest；已删除 `structured/opaque/temporary`、字符串分区视图和直接 request fallback。
- 投递批次不是提示词语义层级：同批并列 section 直接注册到 Plan，并通过 `delivery_group_marker` 元数据保持原子裁剪；禁止再创建 `.batch`、`passive.static` 或 `passive.dynamic` 之类模型可见的空父 section。
- key 冲突采用确定性策略：指纹相同视为幂等；显式 `replace/append` 按策略执行；未指定策略的不同内容保留首个并记录 warning/manifest conflict；严格测试模式直接抛错。
- `trust` 和 `temporary` 字段已删除。`metadata` 只作排障和來源记录，wire 由 typed content 与 `PromptRenderSpec` 决定；生图六字段载荷也不再从 metadata 反序列化。
- 旧 `PromptValue/coerce_prompt_section/render_prompt_section/legacy_heading_token`、位置参数构造、legacy render enum 和 Surface 字符串 API 已删除。跨插件的旧版字符串输入只在明确的外部协议边界包装为 `ExactText`，不重新开放通用兼容 API。
- 互动表达、回复温度、群聊氛围、名场面、扮演强度、玩笑边界和群周期等上游模块只提供事实、等级或 reason code，最终提示文案由所属功能的 section builder 持有。
- 同一函数的条件分支先计算正文、变量或事实，最后只由一个 `prompt_section()` 调用声明 key/title/source；少数需要提前返回空结果的长函数使用局部 builder，但 section 身份仍只定义一次。CI 会拒绝同一函数重复声明 literal key 或 title。
- 静态检查禁止生产代码新增原始 `【标题】`、手写会话 XML/CDATA、已删 API/控制位、散装 Surface/Plan 参数、直接业务 `PromptSection(...)`、投递批次 section、父子同名标题和 Plan 外的 request prompt 写入。保留的 `【】` 仅限统一 renderer、旧持久化数据识别、消息占位符、正则或用户可见标签。

实施中通过真实 `ProviderRequest` 发现并修复两项基础设施问题：仅含子 section 的组合块不再被误判为空，`ExactText` 在 system/turn 合并时也不再被 `strip()` 改写。不同 key 但正文相同的块按身份分别保留，不再做跨 key 正文去重。

最终验收结果：

- 当前分支全量 pytest：`4611 passed, 20 failed, 1 skipped, 913 subtests passed`。
- 同环境 `origin/main`：`4444 passed, 33 failed, 9 skipped, 786 subtests passed`；当前 20 个失败全部位于上游失败域，本分支新增失败为 0。
- package-aware unittest：运行 `3868` 项，结果为 `5 failures, 11 errors`；失败仍是上游同一组外部插件、时区、固定 schema/registry 和旧 handler 审计基线。必须从仓库父目录使用 package top-level 发现，从 `tests/` 直接发现会伪造相对导入错误。
- OneBot v11 与 QQ Official 的真实事件类各覆盖私聊/群聊：主链 section 只出现一次，QQ Official 专属边界只在对应适配器出现，turn-tail 实际落在 `extra_user_content_parts`。
- 主动私聊、群插话和群归档后台 Prompt 保持既有 wire/golden；20 个 FunctionTool Schema snapshot 保持一致。
- 当前插件 ZIP 已在隔离 `ASTRBOT_ROOT` 中由 AstrBot v4.28.0-beta.1 正式加载、启动并正常关闭。

已知失败均来自上游基线，主要包括时区断言、持久化 registry/schema 固定数量、外部插件测试夹具、Provider fallback 测试夹具、旧人格 handler 审计和历史维护写入断言。本次不借提示词重构扩大处理范围。

## 附录 A：按文件审计覆盖表

这张表用于后续执行时逐文件销项。`迁移` 表示存在插件自有 Prompt authoring；`保协议` 表示应接入 typed/exact 或专用 renderer，但首轮不能改变 wire；`排除` 表示该文件中的相关字面量不是 Prompt 标题，禁止机械替换。一个文件可以同时属于多类。

| 文件 | 分类 | 后续动作 |
| --- | --- | --- |
| `conversation_prompt_section.py` | 迁移 | 扩展唯一 typed authoring model 和 renderer |
| `prompt_surface.py` | 迁移 | 只接收 section，删除散装字段和正文去重 |
| `conversation_injection_plan.py` | 迁移 | block 持有 section，删除 structured/opaque 布尔旁路 |
| `main.py` | 迁移 + 保协议 + 排除 | 迁移全部 request 注入；保留 FunctionTool Schema；正则、日志和消息占位符不动 |
| `passive_state_pipeline.py` | 迁移 | passive surface 和群聊提前注入全程传 section |
| `group_prompt_context.py` | 迁移 | 保留现有 XmlElement，停止提前 render |
| `astrbot_knowledge.py` | 迁移 | 知识库正文作为不可信变量，标题字段化 |
| `atrelay.py` | 迁移 | 转述工具说明、最近动作和后台改写均接入 section |
| `balance_awareness.py` | 迁移 | 余额感知返回 section |
| `body_monitor_integration.py` | 迁移 | 身体状态提示返回 section |
| `busy_reply_gate.py` | 迁移 | 忙碌回复边界返回 section |
| `command_handlers.py` | 迁移 + 排除 | 答疑/自然生图短回复使用 PromptDocument；清洗正则不动 |
| `companion/integrations/reality_companion_bridge.py` | 迁移 | 跨设备连续性返回 section |
| `content_companion_bridge.py` | 保协议 | 外部 prompt 用 ExactText 透传 |
| `creative.py` | 迁移 | 项目、提纲、审校、抽取、正文任务按契约渲染 |
| `daily_review.py` | 迁移 | JSON 任务和主链纠偏分别使用 Document/XML |
| `daily_state.py` | 迁移 + 排除 | 大量 heading 双出口改 section；状态数据字段和日志不误改 |
| `dreaming.py` | 迁移 | 梦境、日记和衍生 JSON 任务改统一 authoring |
| `event_dispatch.py` | 迁移 + 排除 | 后台判断任务迁移；删除 Prompt heading 反解析；旧 trace/消息正则保留 |
| `forward_message.py` | 迁移 | 视觉转述、正文转述及主链上下文统一 section |
| `game_integration.py` | 迁移 | 游戏余韵 JSON prompt 改 section |
| `group_member_safety.py` | 迁移 + 保协议 | 后台审核使用 section；隐藏标签使用 ExactText |
| `group_observation.py` | 迁移 + 排除 | 群归档/插话/黑话和主链上下文迁移；消息识别正则不动 |
| `companion_interaction_expression.py` | 迁移 | 只返回互动决策数据；表达文案迁到主链 section builder |
| `domains/affect/reply_temperature.py` | 迁移 | projection 保留事实字段，移出可注入 instruction 文案 |
| `domains/social/group_mood.py` | 迁移 | mood projection 与提示词渲染分离 |
| `domains/social/group_moments.py` | 迁移 | 返回结构化 moment 列表，由 section builder 渲染 |
| `domains/social/roleplay_strength.py` | 迁移 | projection 不携带最终 voice 文案 |
| `domains/social/joke_boundary.py` | 迁移 | 使用 reason code，提示词文案由 group section builder 持有 |
| `integration_status.py` | 迁移 | 世界观、LivingMemory、环境 section-only |
| `llm_tool_actions.py` | 迁移 + 保协议 | 主链工具说明字段化；工具结果契约保持 |
| `memory_companion_adapter.py` | 迁移 + 保协议 | 本地 wrapper 返回 section；外部返回内容视作不可信数据 |
| `memory_context_policy.py` | 迁移 | 核心记忆权限规则作为 section/legacy renderer |
| `news_exploration.py` | 迁移 | 四个 JSON 后台任务统一 authoring |
| `page_api.py` | 迁移 + 保协议 + 排除 | Page 后台模型迁移；人格输出 legacy 协议保留；UI/解析正则不动 |
| `photo_prompt_context.py` | 保协议 | PhotoPromptSection 接入 PHOTO_PROMPT renderer，保持语义和顺序 |
| `photo_reference_metadata.py` | 迁移 | system/user 审批 PromptDocument |
| `photo_wardrobe_decision.py` | 保协议 + 排除 | 图片 prompt 语义分析和正则不机械替换 |
| `planning.py` | 迁移 + 保协议 | 日程/细化 Document；缓存由标题切分改为 key 分区 |
| `platform_compat.py` | 迁移 | 平台边界只返回 section |
| `private_image.py` | 迁移 + 保协议 + 排除 | 主链图片上下文和后台视觉 prompt 迁移；图片占位符/正则保留 |
| `proactive.py` | 迁移 | 主动路线和关系边界返回 section |
| `proactive_engine.py` | 迁移 | 主动判定和完整测试后台 prompt 迁移 |
| `proactive_message.py` | 迁移 + 保协议 | 主动主链从字符串累加改 section；图片/TTS/外部任务保持各自 renderer |
| `qzone_comments.py` | 迁移 | 评论决策 JSON prompt |
| `qzone_feed.py` | 迁移 | 评论正文 prompt |
| `qzone_publish.py` | 迁移 + 保协议 | 发布、净化和配图分别选 body/JSON/photo renderer |
| `qzone_schedule.py` | 迁移 | 长度、去重、生活和情绪说说 prompt |
| `reading_archive.py` | 迁移 | 密码后台 JSON 与主链夹层 section 分开渲染 |
| `scene_context.py` | 迁移 | 手机位置和主动场景位置返回 section |
| `group_cycle_boundary.py` | 迁移 | 只返回边界事实，由主链构造群隐私 section |
| `extension_api_diagnostics.py` | 迁移 + 保协议 | 场景 prompt 返回 typed manifest，并保留跨插件兼容字符串视图 |
| `proactive_chat_runtime_bridge.py` | 迁移 + 保协议 | typed fragment 优先；旧外部 fragment 作为有来源的 ExactText |
| `relationship_policy.py` | 迁移或删除 | 无消费者的 prompt producer 删除；恢复使用时必须从 section builder 暴露 |
| `self_timeline.py` | 迁移 | 自我时间线返回 section |
| `token_budget.py` | 保协议 | 继续消费最终 wire；增加 section/document 统计适配，不改预算语义 |
| `tts_enhancement.py` | 迁移 + 保协议 + 排除 | 主链普通规则 section 化；TTS 标签 exact；标签解析正则不动 |
| `user_memory.py` | 迁移 | 所有主链上下文和后台记忆/审校 Prompt 统一 authoring |
| `user_rest_gate.py` | 迁移 | 休息回复边界返回 section |
| `worldbook.py` | 迁移 | 主链关系网对象和后台自登记 prompt |
| `companion/integrations/image_companion_bridge.py` | 保协议 | `image.task.v1` 字段级兼容 |
| `nai_image_bridge.py` | 保协议 | NAI 权重和请求字典逐字节/字段级兼容 |
| `_conf_schema.json` | 配置契约 | prompt 配置 key、占位符和多人格继承语义保持不变 |
| `helpers.py` | 排除 | `【】` 命中来自识别/清洗正则，不是 Prompt authoring |
| `scripts/ci_static_checks.py` | 迁移 | 增加带 allowlist 理由的 Prompt AST 检查 |
