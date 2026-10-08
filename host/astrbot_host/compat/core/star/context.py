# -*- coding: utf-8 -*-
"""`astrbot.core.star.context` —— `Context` 类（插件与宿主之间的**唯一通道**）。

⭐⭐ **这是宿主最关键的接口**。实测陪伴主插件只用到 6 个成员
   （AST 统计，见 `爱语/docs/PLATFORM_POSITIONING.md` §3.3.1）：

   | 成员 | 次数 | 宿主要做什么 |
   |---|---:|---|
   | `get_config` | 19 | 读插件配置 |
   | `conversation_manager` | 11 | 会话历史管理 |
   | `send_message` | 9 | 消息投递 |
   | `llm_generate` | 5 | 模型调用 |
   | `get_using_tts_provider` | 4 | TTS（可降级 unavailable） |
   | `get_llm_tool_manager` | 1 | 工具注册表 |

⭐ 依据：真 AstrBot `core/star/context.py`（894 行）。
   **实现策略**：把"要落到宿主真实能力"的部分（模型/会话/投递）
   做成**注入的适配器**（`host_ports`），Context 只做协议转换 ——
   这样换宿主实现不用改 Context，符合作者 §7「传输可换、语义不变」。

🔴 **未实现的能力显式抛 `NotImplementedError`**，绝不静默返回 None/空 ——
   否则插件会以为"没配置/没历史"，产生难排查的静默降级（项目铁律）。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from astrbot_host.registry import FunctionToolManager, llm_tools

logger = logging.getLogger("astrbot")

#: 照真实现 `context.py:53-54`：
#:     `WebApiHandler = Callable[..., Awaitable[Any]]`
#:     `RegisteredWebApi = tuple[str, WebApiHandler, list[str], str]`
WebApiHandler = Callable[..., Awaitable[Any]]
RegisteredWebApi = tuple[str, WebApiHandler, list[str], str]

#: `llm_generate` 里**由我们显式消费**、不能再透给 `Provider.text_chat` 的参数。
#: ⚠️ 只列我们真的会接的；其余（`max_tokens` 等 OpenAI 兼容参数）
#:    一律**透传**给上游 —— 插件靠它们控成本，静默丢掉等于预算失效且无报错。
_LLM_GENERATE_RESERVED = frozenset({"chat_provider_id"})


# ---------------------------------------------------------------------------
# 宿主端口（依赖倒置：Context 不知道宿主是 Quart 还是别的）
# ---------------------------------------------------------------------------


class ConversationManagerPort(Protocol):
    """会话/对话历史端口（🔴 **照真 AstrBot `ConversationManager` 的真实契约**）。

    ## 为什么重写过（2026-09-27 第八轮续）

    本 Protocol **原来只有 `get_history()` / `append()` 两个方法** —— 那是按
    "我们以为插件会怎么用"猜的。实测（读插件全部调用点 + 真源码）：
    插件**从来不用这两个名字**，它用的是真实现的这几个：

        get_curr_conversation_id(umo) -> str | None
        get_conversation(umo, cid, create_if_not_exists=False) -> Conversation | None
        new_conversation(umo, platform_id=None, content=None, title=None, persona_id=None) -> str
        update_conversation(umo, conversation_id=None, history=None, title=None,
                            persona_id=None, token_usage=None) -> None
        add_message_pair(cid, user_message, assistant_message) -> None

    调用点（实测，共 15 处）：
      * `proactive_message.py:4919/4922/18810/18821/18823` —— **主动消息存档**（本次要修的）
      * `main.py:3494/3498`、`private_image.py:6460/6462`、`user_memory.py:9604/9607`、
        `daily_state.py:10583/10586`、`llm_tool_actions.py:8440/8443` —— 读"这个 umo
        当前用哪个对话、那个对话的人设是什么"。

    ⇒ **契约写错 = 永远没人能实现它**（缺口不是"忘了注入"，是**接口本身就是错的**）。
    原 `ConversationManagerPort` 里的 `get_history`/`append` 两个方法
    **一个调用点都没有**（全库 grep 为 0），已删除 —— 留着只会误导下一个人。

    ## 数据形状（照真实现，别自己发明）

    * `Conversation`（`core/db/po.py:558`）字段：
      `platform_id / user_id / cid / history / title / persona_id /
       created_at / updated_at / token_usage`；
    * ⚠️ **`history` 是「JSON 字符串」不是 list**（真实现
      `_convert_conv_from_v2_to_v1` 里 `json.dumps(...)`）——
      插件侧就有 `isinstance(raw_history, str)` 的分支在读它
      （`proactive_message.py:18813`），返回 list 会让那边**静默**走成空历史；
    * `cid` 是 **uuid 字符串**；`new_conversation` 的**第一个位置参数是
      `platform_id`**（不是 title！插件为此写了两层 `TypeError` 兜底，
      见 `proactive_message.py:4935-4944`）。
    """

    async def get_curr_conversation_id(self, unified_msg_origin: str) -> str | None:
        """该 umo 当前选中的对话 id；没有则 `None`（**不自动创建**）。"""
        ...

    async def get_conversation(
        self,
        unified_msg_origin: str,
        conversation_id: str,
        create_if_not_exists: bool = False,
    ) -> Any | None:
        """取一个对话对象（形状见 docstring 上文）；不存在且未要求创建时返回 `None`。"""
        ...

    async def new_conversation(
        self,
        unified_msg_origin: str,
        platform_id: str | None = None,
        content: list[dict[str, Any]] | None = None,
        title: str | None = None,
        persona_id: str | None = None,
    ) -> str:
        """新建对话并把该 umo 的"当前对话"切到它；返回新的 `cid`。"""
        ...

    async def update_conversation(
        self,
        unified_msg_origin: str,
        conversation_id: str | None = None,
        history: list[dict[str, Any]] | None = None,
        title: str | None = None,
        persona_id: str | None = None,
        token_usage: int | None = None,
    ) -> None:
        """更新对话（`conversation_id` 为空时取该 umo 当前的）。"""
        ...

    async def add_message_pair(
        self,
        cid: str,
        user_message: Any,
        assistant_message: Any,
    ) -> None:
        """往 `cid` 的对话里追加「用户+助手」一对消息。

        ⚠️ 真实现里对话不存在会**抛异常**（`Conversation with id ... not found`），
           我们也照此 —— 静默丢弃会让插件的"存档成功"变成假象。
        """
        ...


class LLMPort(Protocol):
    """模型调用端口。"""

    async def generate(
        self,
        *,
        prompt: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Any:
        """生成回复。"""
        ...


class DeliveryPort(Protocol):
    """消息投递端口。"""

    async def send(self, umo: str, chain: Any) -> Any:
        """把消息链投递给用户。"""
        ...


class HostPorts:
    """宿主能力集合（可替换；未提供的能力 = 不可用）。

    ⚠️ 每个能力**单独判断可用性** —— 缺一个不该让整插件加载失败
       （照作者「能力不对等要如实 degraded」的判据）。
    """

    def __init__(
        self,
        *,
        conversation_manager: ConversationManagerPort | None = None,
        llm: LLMPort | None = None,
        delivery: DeliveryPort | None = None,
        config: Any = None,
        data_dir: str | None = None,
        tts_provider: Any = None,
        platform_manager: Any = None,
        provider_manager: Any = None,
        persona_manager: Any = None,
    ) -> None:
        self.conversation_manager = conversation_manager
        self.llm = llm
        self.delivery = delivery
        self.config = config
        self.data_dir = data_dir
        self.tts_provider = tts_provider
        #: 🔴 人格管理器（`HostPersonaManager`）。未提供 = 插件读不到人格
        #: → 陪伴的「从已有人格导入」会报「还没有读取到可用的主回复人格文本」，
        #: 而且那句报错是**误诊**（它会让你"先触发一次对话"，但缺这个属性时
        #: 触发也不会写缓存）—— 详见 `astrbot_host/persona_manager.py` 模块头。
        self.persona_manager = persona_manager
        #: 🔴 平台管理器（`OurPlatformManager`）。
        #: 未提供 = 插件拿不到平台 → **主动消息投递不可用**（如实降级）。
        #: 注意区分两件事：
        #:   * **绑定**（往插件 users 写 umo 字符串）—— 不需要它；
        #:   * **主动投递**（按 umo 找平台发消息）—— 需要它。
        self.platform_manager = platform_manager
        #: 🔴 供应商管理器。未提供 = 插件拿不到模型供应商
        #: → 依赖模型的机制（记忆抽取/日程/关系分析）**如实降级**为模板兜底。
        self.provider_manager = provider_manager

    def capabilities(self) -> dict[str, bool]:
        """当前可用能力（诊断用 —— 让"缺什么"可观测）。"""
        return {
            "conversation_manager": self.conversation_manager is not None,
            "llm": self.llm is not None,
            "delivery": self.delivery is not None,
            "config": self.config is not None,
            "tts": self.tts_provider is not None,
            # 平台管理器：决定"插件的主动消息能不能投递"。
            # ⚠️ 单独一层 `_delivery_wired` —— 有管理器 ≠ 能送达
            #    （端口没接时管理器在、但 `send_by_session` 会如实返回 False）。
            #    分开暴露，避免"看着像能用"。
            "platform_manager": self.platform_manager is not None,
            "platform_delivery": _platform_delivery_wired(self.platform_manager),
            # 供应商：有管理器 ≠ 有可用供应商（可能一个都没配/没接模型端口）。
            # 分开暴露，避免"诊断说可用、实际插件解析不到"。
            "provider_manager": self.provider_manager is not None,
            "provider_usable": _provider_usable(self.provider_manager),
            # 人格：有管理器 ≠ 有人格可用（可能一个人设都没建/没读出来）。
            # 分开暴露 —— 否则"诊断说可用、插件实际拿到兜底文案"。
            "persona_manager": self.persona_manager is not None,
            "persona_usable": bool(
                getattr(self.persona_manager, "personas_v3", None)
            ),
        }


def _provider_usable(pm: Any) -> bool:
    """供应商管理器里**是否至少有一个能真正调模型的供应商**。

    ⚠️ 判据是"有 provider **且它的 llm 端口不为空**" ——
       只数 provider 个数会漏掉"配了但没接模型"的情况（那种照样发不出请求）。
    """
    if pm is None:
        return False
    insts = getattr(pm, "inst_map", None)
    if not isinstance(insts, dict):
        return False
    return any(getattr(p, "_llm", None) is not None for p in insts.values())


def _platform_delivery_wired(pm: Any) -> bool:
    """管理器里的平台实例**是否真的接上了投递端口**。

    ⚠️ 这个判据存在的意义：`platform_manager is not None` 只说明"有个对象"，
       而插件真正依赖的是"消息能不能送出去"。
       两者分开暴露，才不会出现"诊断说可用、实际发不出去"。
    """
    if pm is None:
        return False
    for inst in list(getattr(pm, "platform_insts", []) or []):
        if getattr(inst, "delivery", None) is not None:
            return True
    return False


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


class Context:
    """插件运行时上下文（照真实现的接口面，实现按需补齐）。

    🔴 **`registered_web_apis` 的名字与结构照真实实现**（`context.py:126`）：
       真实现是 `list[RegisteredWebApi]`，元素为
       **元组 `(route, view_handler, methods, desc)`**，
       消费者 `dashboard/api/plugins.py:196` 直接**按属性**读它、按元组解包。

    ⚠️ 我们多做了两件真实现没有的事（都是**只增不减**的扩展，插件侧无感）：
       ① 按 `(route, methods)` **去重替换**（真实现也有，见 `context.py:723-727`）；
       ② 额外记录每条 API 的**归属插件** —— 真实现把 266 个 API 堆在一个全局
          列表里，"某个插件登记了几个"**根本无法回答**，加载报告只能显示
          累计值（实测踩到：prompt_tools 只有 21 个 handler 却报 266 个 API，
          那其实是**前面所有插件的累计**）。多记归属不改变接口面，
          却让计数与排障成为可能。
    """

    def __init__(
        self,
        *,
        ports: HostPorts | None = None,
        event_queue: Any = None,
    ) -> None:
        self._ports = ports or HostPorts()
        self._event_queue = event_queue
        #: 真实现里有 `_config`，`Star._get_context_config` 会退回读它
        self._config = self._ports.config
        self._llm_tools: FunctionToolManager = llm_tools
        #: 🔴 **平台管理器**（真实现是属性：`context.py:155`
        #:    `self.platform_manager = platform_manager`）。
        #:
        #: 为什么必须存在：陪伴插件的**主动消息**要靠它按 umo 第一段找平台，
        #: 找不到就走兜底（`proactive_chat_runtime_bridge.py:545-559`）。
        #: 我们原来是**完全没有这个属性** → 插件 `context.platform_manager`
        #: 直接 AttributeError（或 getattr 拿 None）→ 主动消息必然早退。
        #:
        #: ⚠️ 没注入时是 `None`（不是抛错）—— 真实现在"平台未加载"时也
        #:    近似此行为，且插件对该分支有处理；但**能力诊断里会如实显示 False**。
        self.platform_manager: Any = getattr(self._ports, "platform_manager", None)
        #: 🔴 **供应商管理器**（真实现是构造参数注入的属性，见 `context.py:138`）。
        #: 插件按**供应商 ID** 选模型（陪伴有 20+ 个 `*_PROVIDER_ID` 配置项），
        #: 没有它 → `get_provider_by_id` 永远拿不到 → 模型的活全退化成模板。
        self.provider_manager: Any = getattr(self._ports, "provider_manager", None)
        #: 🔴 **人格管理器**（真实现是构造参数注入的属性，`context.py:141,161`）。
        #:
        #: 为什么必须存在（2026-09-21 情绪报障，已实测复现）：
        #:   插件面板「快速从已有人格导入」永远报
        #:   「还没有读取到可用的主回复人格文本，请先让 Bot 触发一次对话或检查人格配置」。
        #:   根因**不是**"没触发过对话"，而是这个属性**根本不存在** →
        #:   插件每一处 `getattr(context, "persona_manager", None)` 都拿 None，
        #:   而 `getattr` 带默认值 → **全部静默**，只有那句**误诊**文案露出来。
        #:   ⚠️ 那句文案让用户"先让 Bot 触发一次对话"，但缺这个属性时
        #:   触发也修不好（插件的两个缓存写入口都在 manager 分支之后）。
        #:   完整链路见 `astrbot_host/persona_manager.py` 模块头。
        #:
        #: ⚠️ 没注入时是 `None`（不是抛错）—— 与 `platform_manager` 同款：
        #:    插件对 None 有处理路径（会如实报"读不到人格"），
        #:    但**能力诊断里会如实显示 False**。
        self.persona_manager: Any = getattr(self._ports, "persona_manager", None)
        #: 🔴 **照真实现的【属性】形态与【活列表】语义**（`context.py:126`）：
        #:    `registered_web_apis: list[RegisteredWebApi] = []`,
        #:    元素是元组 `(route, view_handler, methods, desc)`。
        #:    ⚠️ 必须是**真列表**而不是 property 返回的副本 ——
        #:       否则插件（或将来照真实现写的消费者）往里 `append` 会被
        #:       **静默丢弃**（本项目最贵的 bug 类型）。真实现就是可写的活列表。
        self.registered_web_apis: list[RegisteredWebApi] = []
        #: 与 `registered_web_apis` **按下标平行**的归属表（我们的扩展）。
        #: ⚠️ 直接用 `register_web_api` 时两者同步增长；若有人绕过它直接
        #:    `append`，读取时按"缺失即空"补齐，绝不因为对不齐而抛异常。
        self._web_api_owners: list[str] = []
        #: 🔴 **名字与结构照真实现 `context.py:129`**：`list[Awaitable]`（裸 awaitable，
        #:    不是元组），由 `core_lifecycle.py:322` 直接遍历 await。
        #:    真实现放在**类**上；我们放实例上以便测试隔离（单进程单 Context，等价）。
        self._register_tasks: list[Awaitable[Any]] = []
        #: 归属追踪用（与 `_register_tasks` 平行，仅供按插件计数）
        self._task_owners: list[str] = []
        #: 当前正在加载的插件目录名（由 `PluginLoader` 设置）—— 用于归属标注。
        #: ⚠️ 用"加载器告知"而非"栈回溯猜"：加载是顺序的，这个值是确定的。
        self._current_plugin: str = ""

    # ---- 归属标注（宿主内部用，非真实现接口）----

    def set_current_plugin(self, dir_name: str) -> None:
        """由 `PluginLoader` 在加载某插件前调用，用于给登记物打归属标记。"""
        self._current_plugin = dir_name

    def _owner_at(self, idx: int) -> str:
        """取第 idx 条登记物的归属（对不齐时返回空串，不抛异常）。"""
        if 0 <= idx < len(self._web_api_owners):
            return self._web_api_owners[idx]
        return ""

    def registered_web_apis_of(self, owner_module: str) -> list[RegisteredWebApi]:
        """某个插件登记的 Web API（按归属过滤）—— 我们的扩展。"""
        return [
            api for idx, api in enumerate(self.registered_web_apis)
            if self._owner_at(idx) == owner_module
        ]

    def web_api_owners(self) -> dict[str, int]:
        """每个插件各登记了多少 Web API（排障/报告用）—— 我们的扩展。"""
        counts: dict[str, int] = {}
        for idx in range(len(self.registered_web_apis)):
            owner = self._owner_at(idx) or "<unknown>"
            counts[owner] = counts.get(owner, 0) + 1
        return counts

    # ================= 实测用到的 6 个成员 =================

    def get_config(self, umo: str | None = None) -> Any:
        """① 读插件配置（**插件用得最多：19 次**）。

        ⚠️ `umo` 参数照真实现保留（多用户配置覆盖），
           但我们目前是单用户 → 忽略它并在有值时**留一条 debug 日志**，
           避免"以为按用户隔离了其实没有"的误解。

        🔴 **`umo` 必须保持"位置或关键字"参数**（2026-09-22 固化）：
           插件有 **12 处按位置传**（`get_config(target)` / `get_config(umo)` /
           `get_config(str(event.unified_msg_origin))`）—— 改成 keyword-only
           会**静默打断**它们（多处被宽 `except` 吞成 `{}`）。
           真实现签名就是 `def get_config(self, umo: str | None = None)`。

        🔴 **永远返回 plain `dict`，绝不返回 `None`**（P-N5，2026-09-22 修）：
           宿主没注入配置时 `self._ports.config` 是 `None`，原来直接返回它
           → 插件那边 `config.get(...)` **抛 AttributeError**，而且
           `proactive_engine.py:9929` / `proactive_message.py:10484` 的
           `dict(config.get(...))` **不在 try 内**（会一路炸出去）。
           真实现返回的是 `AstrBotConfig`（**dict 子类**），所以
           "空配置 = 空 dict" 才是等价语义 —— `None` 是另一回事。
           ⚠️ 注入的若本来就是配置对象（dict 子类）就**原样返回**它，
              只在**为空**时才兜 `{}`（别做无谓的拷贝，插件的
              `save_config()` 之类要作用在同一个对象上）。
        """
        if umo:
            logger.debug("get_config(umo=%s)：宿主当前单用户，忽略 umo", umo)
        cfg = self._ports.config
        return cfg if cfg is not None else {}

    @property
    def conversation_manager(self) -> Any:
        """② 会话历史管理（**11 次**）。

        🔴 未注入时**抛错**而不是返回 None —— 返回 None 会让插件
           在 `conversation_manager.xxx` 处报 AttributeError，
           错误信息离真实原因很远（"没接会话能力"）。
        """
        cm = self._ports.conversation_manager
        if cm is None:
            raise NotImplementedError(
                "宿主未接入会话历史能力（`HostPorts.conversation_manager` 为空）。"
                "请在装配时注入适配器。"
            )
        return cm

    async def send_message(self, session: Any, chain: Any) -> Any:
        """③ 消息投递（**9 次**）。

        ⚠️ 真实现签名是 `send_message(session, message_chain)`；
           `session` 既可能是 `MessageSession` 也可能是 umo 字符串，
           这里都接受（真实现也是）。
        """
        delivery = self._ports.delivery
        if delivery is None:
            raise NotImplementedError(
                "宿主未接入消息投递能力（`HostPorts.delivery` 为空）。"
            )
        umo = session if isinstance(session, str) else _session_to_umo(session)
        return await delivery.send(umo, chain)

    async def llm_generate(
        self,
        *,
        chat_provider_id: str | None = None,
        prompt: str | None = None,
        system_prompt: str | None = None,
        contexts: list[Any] | None = None,
        tools: Any = None,
        **kwargs: Any,
    ) -> Any:
        """④ 模型调用（**5 次**）。

        🔴🔴 **必须走供应商层，不能直接丢给 `HostPorts.llm`**。

        真实现（`astrbot/core/star/context.py:174-213`）是三段：

        ```python
        prov = await self.provider_manager.get_provider_by_id(chat_provider_id)
        if not prov: raise ProviderNotFoundError(...)
        return await prov.text_chat(prompt=..., func_tool=tools, ...)   # → LLMResponse
        ```

        ⚠️ **我原来写错了**（`return await llm.generate(**kwargs)`），后果是两个：

        1. **返回形状不对** —— 直连端口拿到的是 `ChatResult`（只有 `.content`），
           而插件读的是 `getattr(response, "completion_text", "")`
           （`token_budget.py:1556`）。`getattr` 有默认值 →
           **不报错**，只会得到**空字符串** → 插件以为"模型答了空话"
           → 记忆抽取/日程/日记**静默失效**。
        2. **`chat_provider_id` 被忽略** —— 插件为不同任务配了不同模型
           （快速响应 / 复杂推理 / 创作，共 20+ 个 `*_PROVIDER_ID`），
           全部被压到同一个端口上，**配置形同虚设**。

        所以这里照真实现**经供应商层**解析；`Provider.text_chat` 负责
        把结果包成 `LLMResponse`（见 `astrbot_host/provider.py`）。
        """
        # 取出我们的 Domain 专属参数（不能透给 text_chat）
        domain_kwargs = {
            k: v for k, v in kwargs.items() if k not in _LLM_GENERATE_RESERVED
        }

        prov = self._resolve_chat_provider(chat_provider_id)
        if prov is None:
            # ── 兜底：有 `HostPorts.llm` 但**一个供应商都没注册** ──
            #
            # ⚠️ 这种情况是**真实存在**的：宿主接上了模型端口，
            #    但面板里还没配任何供应商（我们 `_build_default_llm_port`
            #    在"一个都没配"时**如实返回 None**，所以更常见的是下面第一种）。
            #
            # 🔴 两条路都必须能走通 —— 这正是一开始 38 条 ERROR 的教训：
            #    "端口接上了" 与 "供应商配好了" 是**两件事**，
            #    只做其中一件，插件就有一半调用点打不通。
            if self._ports.llm is not None and not self._provider_ids():
                out = await self._ports.llm.generate(
                    prompt=prompt,
                    messages=contexts,
                    system_prompt=system_prompt,
                    tools=tools,
                    **domain_kwargs,
                )
                # 🔴 `tools=` 必须传：真实现在 `tools is None` 时**不解析**
                #    `tool_calls`（`openai_source.py:881`），我们照抄那个前提。
                return _as_llm_response(out, tools=tools)

            # 🔴 **如实抛错**，不静默降级：
            #   * 没接模型能力 → NotImplementedError（插件能看见"宿主没接"）；
            #   * 给了 id 但不存在 → 配置错误，必须响亮（否则会用错模型而不自知）。
            if self._ports.llm is None and not self._provider_ids():
                raise NotImplementedError(
                    "宿主未接入模型调用能力（`HostPorts.llm` 为空）。"
                )
            raise LookupError(
                f"未找到可用的模型供应商（chat_provider_id={chat_provider_id!r}）；"
                f"已注册：{sorted(self._provider_ids()) or '（无）'}"
            )

        # 照真实现转调 `text_chat`（它内部再转 `LLMPort.generate` 并包成 LLMResponse）
        return await prov.text_chat(
            prompt=prompt,
            contexts=contexts,
            system_prompt=system_prompt,
            func_tool=tools,
            **domain_kwargs,
        )

    def _provider_ids(self) -> list[str]:
        """已注册的供应商 id（诊断用）。"""
        return [
            str(getattr(p, "provider_id", "") or "") for p in self.get_all_providers()
        ]

    def _resolve_chat_provider(self, chat_provider_id: str | None) -> Any:
        """按 id 取供应商；**没给 id** 时退回默认（真实现同款语义）。

        ⚠️ 两种"没有"必须区分（这是本函数存在的唯一理由）：
          * **没给 id** → 正常用法，用默认供应商（插件的多数调用点就是这样）；
          * **给了 id 但查不到** → **配置错误**，调用方必须响亮失败，
            不能悄悄退回默认（否则插件以为用了 A 模型，实际用了 B，
            记忆/日程会写进错误的模型配置下 —— 属于"静默错"）。
        """
        pid = (chat_provider_id or "").strip()
        if pid:
            prov = self.get_provider_by_id(pid)
            return prov
        return self.get_using_provider()

    def get_using_tts_provider(self, umo: str | None = None) -> Any:
        """⑤ TTS provider（**4 次**）。

        ⚠️ **按作者判据：能力不对等要如实返回 unavailable，不伪造。**
           我们还没有 TTS → 返回 `None` 是**真实语义**（真实现没配 TTS 时也返回 None）。
           插件对 None 有处理路径，所以这是安全降级。
        """
        return self._ports.tts_provider

    def get_llm_tool_manager(self) -> FunctionToolManager:
        """⑥ LLM 工具管理器（**1 次**）。"""
        return self._llm_tools

    # ================= 其余真实现接口：要么实现，要么显式报错 =================

    def get_platform(self, platform_type: Any) -> Any:
        """按**类型**名取平台实例（真实现接口）。

        ⚠️ 与 `get_platform_inst`（按实例 id）的区别：
           真实现里这个按 `platform_type`（如 `"aiocqhttp"`）匹配。
           我们只有一个平台实例，所以直接按它的 name/id 比 ——
           **不是**"永远返回 None"，但也**不会**为了迎合某个类型名而
           凭空造一个平台（那是伪造能力）。
        """
        want = str(platform_type or "")
        pm = self.platform_manager
        if pm is None or not want:
            logger.debug("get_platform(%s)：未注入平台管理器", platform_type)
            return None
        inst = self.get_platform_inst(want)
        if inst is None:
            logger.debug("get_platform(%s)：没有匹配的平台实例", platform_type)
        return inst

    def get_platform_inst(self, platform_id: str) -> Any:
        """按 id 取平台**实例**（真实现接口）。

        🔴 真实现是从 `platform_manager` 里找的；我们照做 ——
           **不再硬编码返回 None**。
        ⚠️ 没有管理器（未注入/未启用插件宿主）时返回 None：
           这是插件**预期内**的分支（真实现"平台未加载"时也返回 None），
           不是静默失败 —— `HostPorts.capabilities()` 里会如实显示不可用。
        """
        pm = self.platform_manager
        if pm is None:
            logger.debug("get_platform_inst(%s)：未注入平台管理器", platform_id)
            return None
        getter = getattr(pm, "get_inst", None)
        if callable(getter):
            return getter(platform_id)
        # 退路：插件也会这样遍历（`platform_compat.py:215-224`）
        for inst in list(getattr(pm, "platform_insts", []) or []):
            try:
                meta = inst.meta()
            except Exception:  # noqa: BLE001
                continue
            if platform_id in {
                str(getattr(meta, "id", "") or ""),
                str(getattr(meta, "name", "") or ""),
            }:
                return inst
        return None

    def get_all_providers(self) -> list[Any]:
        """全部可用供应商（插件面板 `page_api.py:22097` 用它列供应商）。

        🔴 这是插件**优先**走的那条路径（其次是 `provider_manager.inst_map`）。
           我们原来硬编码返回空列表 → 面板必然显示"无可用供应商"
           → 记忆/日程等依赖模型的机制**全部退化成模板兜底**（实测日志可见）。
        """
        pm = self.provider_manager
        if pm is None:
            return []
        getter = getattr(pm, "get_all", None)
        if callable(getter):
            return list(getter() or [])
        inst_map = getattr(pm, "inst_map", None)
        if isinstance(inst_map, dict):
            return list(inst_map.values())
        return []

    def get_using_provider(self, umo: str | None = None) -> Any:
        """当前默认对话供应商（真实现 `context.py:457`）。

        🔴 **返回值必须是"供应商对象"（有 `text_chat`），不是我们的 LLMPort**。
           我原来直接 `return self._ports.llm` —— 那是**接口面不对**：
           插件拿到后会调 `provider.text_chat(...)`（`service.py:7519`），
           而 `LLMPort` 只有 `generate()` → `AttributeError`
           → 插件的 `token_budget` 捕获后**退化成模板兜底**（静默降级）。

        ⚠️ 没有供应商时返回 `None`（真实现"未设置"时也返回 None），
           这是插件**预期内**的分支；`HostPorts.capabilities()` 会如实显示不可用。
        """
        pm = self.provider_manager
        if pm is None:
            logger.debug("get_using_provider：未注入供应商管理器")
            return None
        getter = getattr(pm, "get_using", None)
        if callable(getter):
            return getter(umo)
        return None

    def get_provider_by_id(self, provider_id: str) -> Any:
        """按 id 取供应商（插件 `main.py:19204`、`private_image.py:1204` 用它）。

        ⚠️ 真实现是 **async**（`context.py` 里 `async def get_provider_by_id`），
           但插件普遍用 `maybe_await(...)` 包着调（见 `memory_companion` 的
           `_current_provider`），所以**同步返回也能工作**。
           这里保持同步（我们的 `inst_map` 是内存字典，无需 IO）。
        """
        pm = self.provider_manager
        if pm is None:
            return None
        getter = getattr(pm, "get_provider_by_id", None)
        if callable(getter):
            return getter(provider_id)
        inst_map = getattr(pm, "inst_map", None)
        if isinstance(inst_map, dict):
            return inst_map.get(str(provider_id or "").strip())
        return None

    def get_db(self) -> Any:
        """⚠️ 未实现（插件不该直接摸宿主的库）。"""
        raise NotImplementedError(
            "宿主不向插件暴露数据库句柄（`get_db`）——"
            "请通过领域端口访问数据。"
        )

    def register_web_api(
        self,
        route: str,
        view_handler: Any,
        methods: list[str],
        desc: str = "",
    ) -> None:
        """注册 Web API（陪伴的面板 API 会用到）。

        🔴 **语义照真实现 `context.py:717-727`**：
           **相同 `(route, methods)` 已注册则【替换】**，不重复追加。
           漏掉去重会让面板路由出现重复条目（真实现注释明写
           "如果相同路由和方法已注册，会替换现有的 API"）。

        ⚠️ 目前**只登记不挂载** —— 挂载需要宿主 Web 层配合。
           登记后可通过 `registered_web_apis` 属性看到"插件想挂什么"，
           便于逐步补齐（**不静默丢弃**）。
        """
        methods_norm = list(methods)
        # 🔴 去重语义照真实现：**精确比较 `route` 与 `methods`**（`context.py:723-727`）。
        #    ⚠️ 真实现**不在登记时**做 `upper()` —— 大小写归一化是**消费侧**的事
        #       （`dashboard/api/plugins.py:171`：`[item.upper() for item in methods]`）。
        #       我们若在这里 upper()，插件回头读到的就不是它自己写进去的值了。
        entry = (route, view_handler, methods_norm, desc)
        for idx, api in enumerate(self.registered_web_apis):
            if api[0] == route and list(api[2]) == methods_norm:
                self.registered_web_apis[idx] = entry
                # 归属同步更新（下标平行，见 __init__ 的说明）
                if idx < len(self._web_api_owners):
                    self._web_api_owners[idx] = self._current_plugin
                else:
                    self._web_api_owners.append(self._current_plugin)
                logger.debug("Web API 已替换：%s %s", methods_norm, route)
                return
        self.registered_web_apis.append(entry)
        # 归属：多记这一个，才能回答"某插件登记了几个"（见类 docstring）
        self._web_api_owners.append(self._current_plugin)
        logger.info("插件登记 Web API：%s %s", methods_norm, route)

    def register_task(self, task: Awaitable[Any], desc: str) -> None:
        """[DEPRECATED] 登记后台任务 —— 名字与结构**照真实实现**。

        真 `context.py:884-894`：只往 `_register_tasks` 追加**裸 awaitable**
        （**不带 desc**），由 `core_lifecycle.py:322` 统一 await。
        ⚠️ `desc` 参数真实现**收下就丢**（签名里有，正文不用）—— 我们照此办理。
        """
        self._register_tasks.append(task)
        self._task_owners.append(self._current_plugin)

    def registered_tasks(self) -> list[Awaitable[Any]]:
        """⚠️ **我们的扩展**（真实现无此方法，消费者直接读 `_register_tasks`）。

        返回**裸 awaitable**（与真实现同构）—— 若返回元组，
        "照真实现写的消费者"会解包失败。
        """
        return list(self._register_tasks)

    def registered_tasks_of(self, dir_name: str) -> list[Awaitable[Any]]:
        """某个插件登记的后台任务（按归属过滤）—— 我们的扩展。"""
        return [
            t for t, owner in zip(self._register_tasks, self._task_owners, strict=True)
            if owner == dir_name
        ]

    def get_event_queue(self) -> Any:
        return self._event_queue

    def get_registered_star(self, star_name: str) -> Any:
        from .base import star_map

        for md in star_map.values():
            if md.name == star_name:
                return md
        return None

    def get_all_stars(self) -> list[Any]:
        from .base import star_registry

        return list(star_registry)

    def get_all_tts_providers(self) -> list[Any]:
        return [self._ports.tts_provider] if self._ports.tts_provider else []

    def get_all_stt_providers(self) -> list[Any]:
        return []

    def get_all_embedding_providers(self) -> list[Any]:
        return []

    def get_current_chat_provider_id(self, umo: str) -> str:
        return ""

    def add_llm_tools(self, *tools: Any) -> None:
        logger.debug("add_llm_tools(%d)：宿主暂不支持直接注入工具对象", len(tools))

    def activate_llm_tool(self, name: str) -> bool:
        return self._llm_tools.get_func(name) is not None

    def deactivate_llm_tool(self, name: str) -> bool:
        return self._llm_tools.remove(name)

    def register_llm_tool(self, *args: Any, **kwargs: Any) -> None:
        self._llm_tools.add_func(*args, **kwargs)

    def unregister_llm_tool(self, name: str) -> None:
        self._llm_tools.remove(name)

    def register_provider(self, provider: Any) -> None:
        logger.debug("register_provider：宿主单上游，忽略")

    def register_commands(self, *args: Any, **kwargs: Any) -> None:
        logger.debug("register_commands：宿主暂不支持动态注册指令")


def _session_to_umo(session: Any) -> str:
    """`MessageSession` → umo 字符串（真实现里 umo 就是会话标识）。"""
    for attr in ("unified_msg_origin", "session_id", "id"):
        v = getattr(session, attr, None)
        if v:
            return str(v)
    return str(session)


def _as_llm_response(out: Any, *, tools: Any = None) -> Any:
    """把 `LLMPort.generate()` 的返回值包成 `LLMResponse`。

    🔴 **为什么必须有这一步**（这是 2026-09-20 修掉的真 bug 的核心）：

    插件读响应写的是 `getattr(response, "completion_text", "")`
    （`token_budget.py:1556`）。而我们的 `UpstreamLLMPort` 返回的是
    领域层的 `ChatResult`，它**只有 `.content`、没有 `.completion_text`** ——
    `getattr` 带默认值 → **不报错**，插件只会拿到**空字符串**，
    于是"模型明明答了"却表现为"模型没说话" → 记忆抽取/日程/日记**静默失效**。

    🔴🔴 **2026-09-21 补：本函数原来只搬了文本，把 `tool_calls` 和
    `reasoning_content` 也一起丢了**（与 `Provider.text_chat` 同一个缺口）。
    现在**委托给 `response_bridge.to_llm_response`** ——
    那是全宿主唯一的转换入口，三条路（本函数 / `Provider.text_chat` /
    `_AgentRunner.run`）都走它，**不许在这里再手拼 `LLMResponse`**。

    Args:
        out: `LLMPort.generate()` 的返回值。
        tools: 本次带的工具集（`None` = 没带 → 不解析 tool_calls，照真实现）。
    """
    from astrbot_host.response_bridge import to_llm_response

    return to_llm_response(out, tools=tools)


__all__ = [
    "Context",
    "ConversationManagerPort",
    "DeliveryPort",
    "HostPorts",
    "LLMPort",
]
