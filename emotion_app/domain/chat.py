# -*- coding: utf-8 -*-
"""聊天编排：把"组装提示词 → 调上游 → 返回"串起来。

这是 L3 领域层（见 `docs/BACKEND_FRAMEWORK.md` §2.1），**不含 HTTP 细节**。
"""
from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from collections.abc import AsyncIterator
from typing import Any

from ..infra.config import Config
from ..infra.logging_setup import get_logger
from . import context as context_mod
from . import prompt as prompt_mod
from . import tool_context
from .message import ROLE_ASSISTANT, ROLE_USER, Message, from_upstream_message
from .persona import Persona
from .session_repo import Session
from .tools import ToolCall, ToolRegistry, ToolResult
from .backends import OpenAIChatBackend
from .ports import ChatBackend
from .upstream import (
    DEFAULT_REFUSAL_KEYWORDS,
    ChatResult,
    StreamChunk,
    UpstreamError,
    UpstreamTarget,
    apply_nested_params,
    looks_like_refusal,
)

log = get_logger(__name__)


def _accepts_session_id(hook: Any) -> bool:
    """钩子能不能收 `session_id=` 这个关键字参数（**按签名判断**）。

    🔴 为什么不用 `except TypeError` 兜底（本项目踩过的坑）：
       宽 `except` 会把**钩子内部**抛的 `TypeError` 也当成"签名不匹配"，
       然后拿老签名重跑一遍 → 真因被换成一个看起来合理、实际无关的结果。
       这里在**绑定那一刻**就把结论定下来，此后每次调用都不再猜。

    ⚠️ 判 **keyword-only**（新形状是 `async (messages, *, session_id="")`）：
       要求对方**能按关键字收**它，而不是"按位置能收" ——
       否则一个 `async (messages, something_else)` 的钩子会被塞进会话 id。
       `inspect.signature` 对 `**kwargs` 同样报 True（那确实收得下）。

    ⚠️ 签名读不出来（内置函数/被装饰器包成 C 对象）→ **按老形状处理**：
       老形状是"什么都不传"，是更保守的那一边（不会给钩子塞个它没要的参数）。
    """
    if hook is None:
        return False
    try:
        sig = inspect.signature(hook)
    except (TypeError, ValueError):
        return False
    for name, param in sig.parameters.items():
        if param.kind is param.VAR_KEYWORD:
            # `**kwargs` 确实收得下 `session_id=`
            return True
        if name != "session_id":
            continue
        # 纯 POSITIONAL_ONLY 收不下关键字参数 —— 我们只按关键字调
        if param.kind is param.POSITIONAL_ONLY:
            return False
        return True
    return False


class ChatError(Exception):
    """聊天编排层错误。"""


@dataclass(slots=True)
class ChatRequest:
    """一次聊天请求（内部表示）。"""

    messages: list[Message] = field(default_factory=list)
    model: str = ""
    stream: bool = False
    persona: Persona | None = None
    custom_params: dict[str, Any] = field(default_factory=dict)
    fallback_models: tuple[str, ...] = ()

    #: 会话 id（空 = 无状态单轮，向后兼容）
    session_id: str = ""
    #: 本次是否持久化（会话模式下由服务端决定）
    persist: bool = True

    #: 🔴 **`persona` 是"用户显式选的"还是"解析来的"**。
    #:
    #: 为什么要这个标记（2026-09-20 实测）：`_prepare_session` 要把
    #: "用户在面板给会话绑的人设"**持久化**，但**绝不能**把
    #: "从全局当前人设解析出来的"也写进会话 —— 那会把会话**永久钉死**
    #: （详见 `_prepare_session` 里的长注释）。
    #:
    #: 而两者在 `req.persona` 上**长得一模一样**，只能靠这个标记区分。
    #: ⚠️ 默认 `False`：旧调用方（没设过这个字段）走"不写回"这条更安全的路。
    persona_explicit: bool = False


class ChatService:
    """聊天服务。

    职责边界（刻意划清）：
      * 这里负责**组装**（人设 → 提示词 → 消息列表）与**调上游**；
      * **不碰 HTTP**、**不碰存储**（S3 起会注入会话存储，但仍是接口）。
    """

    def __init__(
        self,
        config: Config,
        client: ChatBackend | None = None,
        settings: Any = None,
        personas: Any = None,
    ) -> None:
        self._config = config
        # ⚠️ 只依赖 `ChatBackend` Protocol —— 换后端（陪伴的 provider / 别的协议）不用改这里
        self._client: ChatBackend = client if client is not None else OpenAIChatBackend()
        self._settings = settings      # SettingsService（可选，S2 起注入）
        self._personas = personas      # PersonaRepository（可选）
        self._sessions = None          # SessionRepository（可选，S3 起注入）
        self._tools: ToolRegistry | None = None   # 工具表（可选，S6 起注入）
        self._dispatch: Any = None                # 工具下发方式（可替换）
        self._refusal_keywords = DEFAULT_REFUSAL_KEYWORDS
        self._summarizer: Any = None       # Summarizer（S6b 注入）
        #: 🔴 **LLM 钩子端口**（P0-4）：让 AstrBot 插件在调模型**之前**介入。
        #:
        #: 形状：`async (messages: list[Message]) -> list[Message] | None`
        #:   * 传**已组装好的消息列表**（人设 + 摘要 + 历史 + 本轮输入）；
        #:   * 返回**修改后的新列表**，或 `None` 表示"不改"。
        #:
        #: ⚠️ 为什么放在领域层而不是 web 层：消息组装（人设/摘要/历史）在这里，
        #:    而插件要注入的正是"组装完之后、发给模型之前"的那份提示词。
        #:    放 web 层就得把组装结果再传出去，反而多一层耦合。
        #: ⚠️ 领域层**不依赖**插件宿主：这里只认这个**函数协议**，
        #:    具体实现（跑 AstrBot 钩子）由装配层注入（照项目"领域层只依赖 Protocol"的约定）。
        self._llm_hook: Any = None
        #: 钩子能不能收 `session_id=`（`bind_llm_hook` 时按签名定一次，见
        #: `_accepts_session_id`）。⚠️ 老的一参数钩子仍然是 False —— 兼容。
        self._llm_hook_takes_session: bool = False
        #: 🔴 **模型解析端口**（情绪 2026-10-09「按会话选模型」）。
        #:
        #: 形状：`(model_id: str) -> dict | None`，返回
        #: `{"base_url": ..., "api_key": ..., "model": ...}`，找不到返回 `None`。
        #:
        #: ⚠️ 为什么用**注入的函数**而不是在这里 import 存储：
        #:    存储住在 `infra/providers_store.py`，而领域层只依赖 Protocol
        #:    （与 `bind_llm_hook` 同款约定）。装配层（`web/app.py`）负责接线。
        #:
        #: ⚠️ 未注入 = 该能力**不存在** ⇒ 走老路径（环境变量/运行时设置），
        #:    保证既有测试与"没配模型供应商"的部署行为不变。
        self._model_resolver: Any = None
        self._bg_tasks: set[asyncio.Task] = set()   # 后台任务（防被 GC 回收）

    def bind_model_resolver(self, resolver: Any) -> None:
        """注入"按模型 id 查 base_url/key"的端口（见 `_model_resolver` 注释）。

        Args:
            resolver: `(model_id) -> {"base_url", "api_key", "model"} | None`；
                传 `None` = 关闭该能力。
        """
        self._model_resolver = resolver
        log.info(
            "会话模型解析端口已接线" if resolver is not None else "会话模型解析端口已关闭",
            extra={"data": {"wired": resolver is not None}},
        )

    def resolve_session_model(self, model_id: str) -> UpstreamTarget | None:
        """把会话上那个**模型 id**（形如 `fast/deepseek-flash`）解析成上游目标。

        Returns:
            解析成功 → `UpstreamTarget`；未注入解析器 / 找不到该 id /
            该模型所属源没有 `base_url` → **`None`**
            （调用方据此**明确报错**，绝不静默回落到别的模型）。
        """
        mid = (model_id or "").strip()
        if not mid or self._model_resolver is None:
            return None
        try:
            found = self._model_resolver(mid)
        except Exception as exc:  # noqa: BLE001
            # 解析器是外部注入的 —— 它炸了不能把聊天带走，但**必须留痕**
            log.error(
                "解析会话模型失败",
                extra={"data": {"model_id": mid, "error": f"{type(exc).__name__}: {exc}"}},
            )
            return None
        if not isinstance(found, dict):
            return None
        base_url = str(found.get("base_url") or "").strip()
        if not base_url:
            # 模型配了、但它挂的源没有 api_base —— 这是**配置不完整**，
            # 必须让调用方看到，不能在下面悄悄退回环境变量
            log.warning(
                "模型 %s 所属的源没有 api_base，无法使用",
                mid,
                extra={"data": {"model_id": mid}},
            )
            return None
        return UpstreamTarget(
            base_url=base_url,
            api_key=str(found.get("api_key") or ""),
            model=str(found.get("model") or mid.rsplit("/", 1)[-1]),
            timeout=self._config.upstream.timeout_seconds,
        )

    def bind_llm_hook(self, hook: Any) -> None:
        """注入 LLM 钩子端口（P0-4）。

        `hook` 形状：`async (messages, *, session_id="") -> list[Message] | None`；
        传 `None` = 关闭（**默认**，即完全不启用插件钩子 —— 聊天行为不变）。

        ⚠️ **老的一参数形状 `async (messages)` 继续可用**（`_apply_llm_hook` 按
           签名判断能不能收 `session_id`，收不了就不传）——
           不这么做的话，`plugin_host.apply_llm_hooks` 加参数那一刻
           所有老钩子（含测试里的）会当场 `TypeError`。
        """
        self._llm_hook = hook
        self._llm_hook_takes_session = _accepts_session_id(hook)

    async def _apply_llm_hook(
        self, messages: list[Message], session_id: str = ""
    ) -> list[Message]:
        """跑 LLM 钩子（P0-4）。**未注入或出错都原样返回**，绝不影响聊天。

        Args:
            session_id: 本次聊天属于哪个会话。**必须传** —— 钩子要用它决定
                "这次是哪个插件用户"（umo 第三段）。不传的后果是插件按全局
                `default` 算记忆/关系/被动状态 → **多会话串台**（2026-09-26 修的真 bug）。

        ⚠️ 四条设计取舍：
          1. **未注入 → 直接返回**（默认关闭，聊天行为与以前完全一致）；
          2. **钩子返回 None → 视为"没改"**（插件常只改 `req` 的字段而不返回）；
          3. **钩子抛错 → 记 ERROR 并原样返回**（插件是可选增强，
             它的故障不能让用户聊不了天）—— 但**必须响亮**，不许静默；
          4. **老的一参数钩子照旧能用** —— 按 `inspect.signature` 判断能否收
             `session_id`，**不用宽 `except TypeError` 兜底**：那会把
             "钩子内部自己抛的 TypeError"也一并咽掉，
             把一个真 bug 换成一条看起来合理的"签名不匹配"
             （本项目踩过：宽 except 让错误信息离根因三层）。
        """
        if self._llm_hook is None:
            return messages
        try:
            # ⚠️ `getattr` 带默认 `None` 而不是直接取属性：有些调用方
            #    （含老测试）用 `ChatService.__new__` 绕过 `__init__`
            #    再直接塞 `_llm_hook` —— 那时缓存位不存在，这里**现算一次**
            #    （与 `bind_llm_hook` 同一份规则，不另写一套）。
            takes_session = getattr(self, "_llm_hook_takes_session", None)
            if takes_session is None:
                takes_session = _accepts_session_id(self._llm_hook)
            if takes_session:
                out = await self._llm_hook(messages, session_id=session_id)
            else:
                out = await self._llm_hook(messages)
        except Exception:  # noqa: BLE001
            log.error("LLM 钩子执行失败（本次按未注入处理）", exc_info=True)
            return messages
        if out is None:
            return messages
        if not isinstance(out, list):
            log.error(
                "LLM 钩子返回了非列表（%s）→ 按未注入处理", type(out).__name__
            )
            return messages
        return out

    def bind_tools(self, registry: ToolRegistry, dispatch: Any = None) -> None:
        """注入工具表与**下发方式**。

        ⚠️ `dispatch` 是可替换的（`ports.ToolDispatch`）——
           默认 function calling，但可换成文本标签等，
           **领域层不假设具体协议**（情绪：「接口别写死了」）。
        """
        self._tools = registry
        if dispatch is not None:
            self._dispatch = dispatch

    @property
    def tools(self) -> ToolRegistry | None:
        return self._tools

    @property
    def dispatch(self) -> Any:
        """当前下发方式；未显式注入时默认 function calling。"""
        if self._dispatch is None:
            from .backends import FunctionCallDispatch

            self._dispatch = FunctionCallDispatch()
        return self._dispatch

    def _tool_payload_and_sections(self) -> tuple[dict[str, Any], list[Any]]:
        """统一的"工具怎么下发"入口。

        ⭐ 返回 `(请求体字段, 提示词片段)`：
           * function calling → `({"tools": [...], "tool_choice": "auto"}, [])`
           * 文本标签        → `({}, [说明片段])`
           **领域层不看具体是哪种**。
        """
        if self._tools is None:
            return {}, []
        specs = self._tools.specs()
        if not specs:
            return {}, []
        d = self.dispatch
        return d.build_payload(specs), d.prompt_sections(specs)

    def bind_sessions(self, sessions: Any) -> None:
        """注入会话仓库（避免构造参数继续膨胀）。"""
        self._sessions = sessions

    def bind_summarizer(self, summarizer: Any) -> None:
        """注入总结器（S6b）。"""
        self._summarizer = summarizer

    def schedule_summarize(self, session_id: str) -> None:
        """**把总结丢到后台**，立刻返回。

        🔴 关键设计（照爱语的"静默总结"）：
           **绝不在回复链里同步做** —— 否则每次到边界用户都要多等一次上游往返。

        ⚠️ 任务要存进 `self._bg_tasks`，否则可能被 GC 提前回收
           （asyncio 只持弱引用）→ 总结静默不执行。
        """
        if self._summarizer is None or not session_id or self._sessions is None:
            return
        task = asyncio.create_task(self._summarize_bg(session_id))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _summarize_bg(self, session_id: str) -> None:
        """后台总结：失败只记日志，**绝不影响用户**。"""
        try:
            session = await self._sessions.get(session_id)
            msgs = await self._sessions.messages(session_id)
            # 🔴 **总结该用哪个模型**（情绪 2026-10-09 的会话级字段）：
            #    ① 会话 policy 里显式指了 `summary_model` → 用它（**那是模型 id**，
            #       要经 `resolve_session_model` 解析成 base_url/key）；
            #    ② 没指 → 用"这条会话自己的对话模型"（不是全局默认！）——
            #       因为会话级选模型之后，"全局默认"已经不是它实际在用的东西了。
            #    ⚠️ 原来这里写的是 `resolve_target(None)` = **完全忽略会话**
            #       ⇒ 总结会偷偷用另一个模型，而用户以为用的是他选的那个
            #       （本项目最忌的"静默用了别的东西"）。
            target = self.resolve_summary_target(session)

            outcome = await self._summarizer.maybe_summarize(
                session_id=session_id,
                messages=msgs,
                policy=session.policy,
                current_summary=session.summary,
                cursor=session.summarized_upto,
                base_url=target.base_url,
                api_key=target.api_key,
                timeout=target.timeout,
                # ⚠️ 传**上游真正的模型名**（`target.model` 已经是解析后的名字）；
                #    不传的话 Summarizer 会用构造时那个默认模型名。
                model=target.model,
            )

            if outcome.ran and outcome.ok:
                # ⭐ 摘要与新游标**一次写入**（避免"游标动了但摘要没存"的中间态）
                await self._sessions.update(session_id, {
                    "summary": outcome.summary,
                    "summarized_upto": outcome.cursor,
                })
                log.info(
                    "会话摘要已更新",
                    extra={"data": {
                        "session": session_id,
                        "chars": outcome.summary_chars,
                        "cursor": outcome.cursor,
                    }},
                )
        except Exception:  # noqa: BLE001 — 后台任务绝不能让异常冒泡
            log.exception("后台总结异常", extra={"data": {"session": session_id}})

    @property
    def sessions(self) -> Any:
        return self._sessions

    # ---- 目标解析（人设可覆盖全局）----

    def resolve_target(
        self,
        persona: Persona | None,
        model_override: str = "",
        session_model: str = "",
    ) -> UpstreamTarget:
        """决定这次调用用哪个 base_url / key / model。

        优先级（从高到低）：

            ① **人设覆盖**（`persona.api_url`/`api_key`/`model`）
            ② **请求指定**（`model_override`）
            ③ **会话选择的模型**（`session_model` —— 情绪 2026-10-09 要的「按会话选模型」）
            ④ 运行时设置（面板「上游连接」那块，正在下线）
            ⑤ 环境变量（`.env`）

        ⚠️ ③ 与 ④⑤ 的差别不只是"优先级高一点"：
           ③ 是**用户在界面上明确选的**，所以它解析失败时**必须报错**，
           不能悄悄退回 ④⑤ —— 那会让用户以为选的那个在用。
           （本项目铁律：静默用了别的东西 = 最贵的那类 bug。）

        ⚠️ 实测爱语支持 per-persona 覆盖（`persona_api_url`/`persona_api_key`/`persona_model`），
           所以 ① 位置最高是有依据的，不是拍脑袋。

        Args:
            session_model: 会话上存的模型 id（形如 `fast/deepseek-flash`）。
                **空串 = 这条会话没选**，此时照旧走 ④⑤（向后兼容）。

        Raises:
            不抛异常。③ 解析不出来时**回落**到 ④⑤，但调用方应先调
            `model_unavailable_reason()` 拿到明确原因并报给用户 ——
            见 `complete()` / `stream()` 里的用法。
        """
        env = self._config.upstream
        # 运行时设置优先于环境变量（情绪 A1 ②：业务配置要能在面板/App 改）
        rt = self._settings.effective_upstream() if self._settings is not None else {}
        base_url = rt.get("base_url") or env.base_url
        api_key = rt.get("api_key") or env.api_key
        model = rt.get("model") or env.model

        # ③ 会话选的模型：它自带 base_url/key（来自模型供应商页的那个源），
        #    所以解析成功后**地址与 key 就定了**。
        #    ⚠️ 这与 `model_override`（请求级临时指定）**不是互斥关系**：
        #       "用哪个端点"由会话决定，"这次叫什么模型名"才可以被 override。
        #       我第一版写成 `if sess_target and not model_override` ——
        #       结果带 override 时**整个端点都被丢掉、退回环境变量**，
        #       请求会打到错的地址上（而模型名看着对，极难查）。
        #       被 `test_request_level_model_override_still_wins` 抓住。
        sess_target = self.resolve_session_model(session_model) if session_model else None
        if sess_target is not None:
            base_url, api_key = sess_target.base_url, sess_target.api_key
            # 没被 override 时，模型名也用它
            if not model_override:
                model = sess_target.model

        return UpstreamTarget(
            base_url=(persona.api_url if persona and persona.api_url else base_url),
            api_key=(persona.api_key if persona and persona.api_key else api_key),
            model=model_override or (persona.model if persona and persona.model else "") or model,
            timeout=env.timeout_seconds,
        )

    def resolve_summary_target(self, session: Any) -> UpstreamTarget:
        """总结该打到哪个上游目标（会话级 `summary_model` 优先）。

        🔴 三级回落，**每一级都是"这条会话"自己的东西**：
            ① `policy.summary_model`（用户为总结单独指定的模型 id）
            ② 会话自己选的对话模型（`session.model`）
            ③ 环境变量/运行时设置（`resolve_target` 的默认行为）

        ⚠️ 为什么不做成"直接用 ①，解析失败就报错"：
           总结是**后台任务**，它失败只影响摘要新旧，不该把用户的聊天打断
           （`schedule_summarize` 的注释就是这个约束）。所以这里逐级回落，
           并在**每次回落时记日志** —— 不静默，但不阻塞。

        ⚠️ ①② 解析出来的是 `UpstreamTarget`，③ 是 `resolve_target` 的产物 ——
           两者形状相同，所以下面统一成一个返回值。
        """
        policy_model = str(getattr(session.policy, "summary_model", "") or "").strip()
        if policy_model:
            t = self.resolve_session_model(policy_model)
            if t is not None:
                return t
            log.warning(
                "会话指定的总结模型不可用，回落到会话对话模型",
                extra={"data": {"summary_model": policy_model}},
            )
        # ② 会话自己的对话模型
        session_model = str(getattr(session, "model", "") or "").strip()
        if session_model:
            t = self.resolve_session_model(session_model)
            if t is not None:
                return t
            log.warning(
                "会话对话模型不可用，总结回落到环境默认",
                extra={"data": {"session_model": session_model}},
            )
        # ③ 兜底
        return self.resolve_target(None)

    def model_unavailable_reason(self, session_model: str) -> str:
        """会话选了模型、但它**用不了**时，返回一句**可行动**的原因；能用则返回 `""`。

        🔴 为什么单独一个函数（而不是在 resolve_target 里顺手打个日志）：
           这里要产出的是**给用户看的话**，必须包含"去哪修"。
           只在日志里说，用户看到的就是"聊天没反应"（本项目最忌的静默）。

        三种"用不了"要分得开（用户要做的事完全不同）：
          ① 没选            → 去设置页给这条会话选一个
          ② 选了但找不到    → 那个模型在模型供应商页已经没了
          ③ 找到了但源没地址 → 源配了一半
        """
        mid = (session_model or "").strip()
        if not mid:
            return (
                "这条会话还没有选择对话模型。"
                "请到「设置 → 配置文件 → 会话」里为它选一个模型。"
            )
        if self._model_resolver is None:
            # 没接线：不是用户的错，是我们的装配问题 —— 说清楚，别让用户去瞎配
            return (
                f"这条会话选了模型 {mid}，但本服务没有接入模型供应商"
                "（服务端未装配模型解析器）—— 这是部署问题，请查看服务日志。"
            )
        if self.resolve_session_model(mid) is None:
            return (
                f"这条会话选的模型 {mid} 现在用不了："
                "它可能已在「模型供应商」里被删除，或它所属的源还没有填 API 地址。"
                "请到「模型供应商」检查，或回到「设置 → 配置文件 → 会话」换一个模型。"
            )
        return ""

    async def resolve_persona(
        self, persona_id: str = "", *, session_id: str = ""
    ) -> Persona | None:
        """解析要用的人设。**优先级：显式 id > 会话人格 > 当前人设**。

        🔴🔴 **为什么必须有"会话人格"这一层**（2026-09-20 实测修）：

        面板早就给了「绑定人设」下拉（`index.html:98` / `app.js:880`/`:950`），
        `PATCH /api/v1/sessions/<id>` 也收 `persona_id`，`Session.persona_id`
        也存得下 —— **但这里从来不读它**。实测（会话绑「甲」、全局设「乙」）：

        ```
        ① 会话A 绑的人设         = 94ce904e…（测试甲）
        ② 全局 active_persona_id = （空）
        ③ resolve_persona() 取到 = None    ← 会话绑的甲被完全无视
        ④ 【应该】取到            = 测试甲
        ```

        → 绑了等于没绑：**只要全局空着，会话绑的人设就被无视**，
          用户看到的是"我明明绑了人设，它却不按那个说话"。

        ⚠️ 会话绑的人设**已不存在**（被删了）时：**响亮记 WARNING 并回落全局** ——
           既不能让聊天崩，也不能静默返回 None（那样用户只知道"人设没了"，
           不知道为什么）。

        ⚠️ 不传 `session_id`（无状态单轮 / 旧调用点）→ **行为与以前完全一致**，
           向后兼容。

        Args:
            persona_id: 显式指定的人设 id（最高优先）。
            session_id: 会话 id；给了就先看该会话绑的人设。

        Returns:
            人设对象；一个都解析不出来时 `None`（调用方按"无人设"处理）。
        """
        if self._personas is None:
            return None

        # ---- 1) 显式指定 ----
        if persona_id:
            return await self._get_persona_or_none(persona_id, source="显式指定")

        # ---- 2) 会话绑的人设（本次修的核心）----
        if session_id and self._sessions is not None:
            sess_pid = await self._session_persona_id(session_id)
            if sess_pid:
                got = await self._get_persona_or_none(sess_pid, source="会话绑定")
                if got is not None:
                    return got
                # 会话指向的人设不存在 → 已记 WARNING，继续往下回落（不静默无提示）

        # ---- 3) 全局"当前人设" ----
        if self._settings is not None and self._settings.active_persona_id:
            return await self._get_persona_or_none(
                self._settings.active_persona_id, source="当前人设"
            )
        return None

    async def _session_persona_id(self, session_id: str) -> str:
        """取会话绑定的人设 id；会话不存在 / 读失败时返回空串（不抛）。"""
        try:
            session = await self._sessions.get(session_id)
        except Exception:  # noqa: BLE001 — 会话不存在不该让聊天崩掉
            return ""
        return str(getattr(session, "persona_id", "") or "")

    async def _get_persona_or_none(self, pid: str, *, source: str) -> Persona | None:
        """按 id 取人设；不存在时**响亮记日志**并返回 None。

        ⚠️ 日志必须带上 `source`：出问题时能一眼看出"是会话绑错了、
           还是全局设置指错了"—— 只打一个 id 没法定位。
        """
        try:
            return await self._personas.get(pid)
        except Exception:  # noqa: BLE001
            log.warning(
                "人设不存在，按无人设处理",
                extra={"data": {"id": pid, "source": source}},
            )
            return None

    # ---- 消息组装 ----

    def build_messages(
        self,
        req: ChatRequest,
        *,
        history: list[Message] | None = None,
        summary: str = "",
    ) -> list[Message]:
        """把请求变成"提示词 + 上下文 + 本轮输入"的完整消息列表。

        ⭐ **前缀稳定性**（P3）：system 只由人设决定，**不含历史**；
           历史一律作为独立 message 追加。这样同一人设下前缀恒定，
           上游 prompt 缓存才能命中（爱语"记忆好"的真实机制）。

        顺序：`[system(人设)] [system(摘要)] [历史...] [本轮输入...]`
        """
        # ⭐ 工具相关提示词：使用规则（必注入）+ 下发方式要求的片段（function calling 为空）
        sections: list[Any] = []
        _, dispatch_sections = self._tool_payload_and_sections()
        if dispatch_sections:
            sections.extend(dispatch_sections)
        if self._tools is not None and self._tools.specs():
            from .image_tool import TOOL_USAGE_RULES
            from .persona import PromptSection

            sections.append(
                PromptSection(
                    title="工具使用规则",
                    content="\n".join(TOOL_USAGE_RULES),
                    order=context_mod.ORDER_TOOLS,
                    source="module",
                )
            )

        assembled = prompt_mod.assemble(
            req.persona, extra_sections=sections or None
        )
        out: list[Message] = []

        if assembled.text.strip():
            out.append(prompt_mod.to_system_message(assembled))

        # 摘要作为**独立的 system 消息**，紧跟人设之后（不与前缀混在一起）
        if summary.strip():
            out.append(
                Message(role="system", content=f"【此前对话摘要】\n{summary.strip()}")
            )

        if history:
            out.extend(history)

        out.extend(req.messages)

        if not any(m.role in ("user", "assistant") for m in out):
            raise ChatError("消息列表中至少要有一条 user/assistant 消息")

        return out

    # ---- 会话编排 ----

    async def prepare_session(self, req: ChatRequest) -> tuple[Session | None, list[Message], str]:
        """会话模式下：确保会话存在、取历史、按策略组装上下文。

        Returns:
            (会话, 历史消息, 摘要)。非会话模式返回 (None, [], "")。
        """
        if not req.session_id or self._sessions is None:
            return None, [], ""

        persona_id = req.persona.id if req.persona else ""
        # 🔴🔴 **不要把全局人设"钉"进会话**（2026-09-20 实测修掉的次生 bug）。
        #
        # 原来这里是：
        #     session = await self._sessions.ensure(req.session_id, persona_id=persona_id)
        #     if persona_id and not session.persona_id:
        #         session = await self._sessions.update(req.session_id, {"persona_id": persona_id})
        #
        # 后果：会话本来**没绑**人设（语义是"跟随当前人设"，面板文案就是这么写的
        # ——`index.html:98`「留空 = 用当前人设」），但**聊过一次之后**，
        # 当时的全局人设被**永久写进**这个会话 →
        # 用户之后在设置里改「当前人设」，**这个会话再也不跟着变**，
        # 而且**没有任何提示**。这是"存了不该存的东西"的典型。
        #
        # ⚠️ 注意区分两个来源（这是本改动唯一的判断点）：
        #   * 会话**自己绑了**人设（用户在面板选的）→ 那是用户意图，**保留**；
        #   * `persona_id` 只是"这一轮解析出来的"（可能来自全局）→ **不写回**。
        # 判断办法：`req` 里是否**显式**带人设。我们用 `req.persona_explicit`
        # 标记它（见 `ChatRequest`），没有该标记的旧调用方按"不写回"处理（更安全）。
        session = await self._sessions.ensure(req.session_id)
        if persona_id and getattr(req, "persona_explicit", False):
            if session.persona_id != persona_id:
                session = await self._sessions.update(
                    req.session_id, {"persona_id": persona_id}
                )

        all_msgs = await self._sessions.messages(req.session_id)
        # ⚠️ 只用**已被总结游标之前**之外的原文？不 —— 摘要与最近原文是叠加的，
        #    所以这里直接用全部历史，由策略决定保留哪些。
        ctx = context_mod.build_context(all_msgs, session.policy, summary=session.summary)
        return session, ctx.history, ctx.summary

    async def record(
        self,
        session_id: str,
        *,
        user_text: str,
        assistant_text: str,
    ) -> None:
        """把一轮对话写回会话。

        ⚠️ 失败**不该影响回复** —— 用户已经拿到答案了，
           存储出错只记日志（这正是爱语"后台维护不打断对话"的思路）。
        """
        if not session_id or self._sessions is None:
            return
        try:
            await self._sessions.append(
                session_id,
                [
                    Message(role=ROLE_USER, content=user_text),
                    Message(role=ROLE_ASSISTANT, content=assistant_text),
                ],
            )
        except Exception:  # noqa: BLE001
            log.exception("写入会话失败", extra={"data": {"session": session_id}})

    # ---- 非流式 ----

    def _reject_if_model_unusable(self, req: ChatRequest, session: Session | None) -> None:
        """会话选了模型但它用不了时 → **抛 `ChatError`**（带可行动的原因）。

        🔴 为什么**必须抛**而不是静默回落（这是本功能的关键取舍）：
           用户既然在界面上为这条会话选了模型，他就有理由相信
           "这条会话用的是那个模型"。如果它坏了我们还照常回话，
           那回复就是**另一个模型**生成的，而界面上写着这个 —— 用户无从察觉。
           本项目最贵的 bug 类型正是"静默用了别的东西"（参考图被丢弃那次）。

        ⚠️ 两种情况**不拦**：
           ① 会话没选模型 → 走老路径（环境变量/运行时设置），这是向后兼容；
           ② 请求里显式带了 `req.model` → 那是一次临时指定，用户意图明确。
        """
        if req.model:
            return
        # ⚠️ `session` 可能是 `None`（没有 session_id 的裸调用）——
        #    先把模型取出来，别在日志行里直接碰 `session.model`
        #    （短路能保住判断，但**日志那一行照样会执行**，会炸在 None 上）。
        session_model = (session.model if session is not None else "").strip()
        # ⚠️ 只拦"**选了**却用不了"那种；"没选"是合法状态（老会话，走环境变量）
        if not session_model:
            return
        reason = self.model_unavailable_reason(session_model)
        if not reason:
            return
        log.warning(
            "会话模型不可用，已拒绝本次请求",
            extra={"data": {"session": req.session_id, "model": session_model, "reason": reason}},
        )
        raise ChatError(reason)

    async def complete(self, req: ChatRequest) -> ChatResult:
        # 🔴 **顺序要紧**：先取会话，再解析目标 —— 因为"用哪个模型"是**会话**上的字段。
        #    早期版本把 `resolve_target` 写在 `prepare_session` 之前，
        #    那样读不到 `session.model`，会话级模型**永远不会生效**
        #    （而且不报错：它会安静地用全局模型，用户以为选的生效了）。
        session, history, summary = await self.prepare_session(req)
        # ⚠️ `session` **可能是 `None`**（没有 session_id 的裸调用 / 会话不存在）。
        #    不去碰它的属性 —— 我第一版直接写 `session.model`，
        #    当场把 32 个既有用例打成 `AttributeError: 'NoneType'`。
        session_model = session.model if session is not None else ""
        self._reject_if_model_unusable(req, session)
        target = self.resolve_target(req.persona, req.model, session_model)

        messages = self.build_messages(req, history=history, summary=summary)
        # ⭐ P0-4：让插件在**发给模型之前**改提示词（记忆/人设注入靠这个）
        # 🔴 **必须传 `session_id`**：钩子要用它决定"这次是哪个插件用户"
        #    （umo 第三段 = 插件 `data["users"]` 的键）。不传的后果是
        #    **所有会话的记忆/关系/被动状态都按全局 `default` 算** → 串台。
        messages = await self._apply_llm_hook(messages, req.session_id)

        body_extra = apply_nested_params({}, req.custom_params) if req.custom_params else None

        log.info(
            "调用上游",
            extra={
                "data": {
                    "url": target.url(),
                    "model": target.model,
                    "messages": len(messages),
                    "history": len(history),
                    "has_summary": bool(summary),
                    "stream": False,
                }
            },
        )
        tool_payload, _ = self._tool_payload_and_sections()

        result = await self._chat_with_fallback(
            target, messages, req.fallback_models, body_extra, tool_payload.get("tools")
        )

        # ⭐ 工具循环：模型要调工具 → 执行 → 把结果回填 → 再问一次
        result = await self._run_tool_loop(
            target, messages, result, body_extra, persona=req.persona
        )

        # 持久化（失败不影响已拿到的回复）
        if req.persist and session is not None:
            last_user = next(
                (m.content for m in reversed(req.messages) if m.role == ROLE_USER), ""
            )
            await self.record(
                session.id, user_text=last_user, assistant_text=result.content
            )
            # ⭐ **把总结丢到后台**（不阻塞本次回复）
            self.schedule_summarize(session.id)

        return result

    async def _chat_with_fallback(
        self,
        target: UpstreamTarget,
        messages: list[Message],
        fallback_models: tuple[str, ...],
        body_extra: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None,
    ) -> ChatResult:
        """按 Protocol 调后端，并在疑似拒答时**换模型重试**。

        借鉴陪伴插件 `model_routing.py`：拒答往往与具体模型的内容策略有关。
        """
        models = (target.model,) + tuple(
            m for m in fallback_models if m and m != target.model
        )

        last: ChatResult | None = None
        for idx, model in enumerate(models):
            result = await self._client.chat(
                messages,
                model=model,
                base_url=target.base_url,
                api_key=target.api_key,
                timeout=target.timeout,
                tools=tools,
                extra=body_extra,
            )
            if not looks_like_refusal(result.content, self._refusal_keywords):
                return result

            last = result
            if idx + 1 < len(models):
                log.warning(
                    "上游疑似拒答，换模型重试",
                    extra={"data": {"from": model, "to": models[idx + 1]}},
                )

        assert last is not None
        return last

    # ---- 工具循环 ----

    async def _run_tool_loop(
        self,
        target: UpstreamTarget,
        messages: list[Message],
        first: ChatResult,
        body_extra: dict[str, Any] | None,
        *,
        persona: Persona | None = None,
    ) -> ChatResult:
        """执行工具调用并把结果回填，再取一次模型回复。

        ⭐ 为什么要这个循环：function calling 的标准流程是
           `模型要求调用 → 我们执行 → 把结果作为 tool 消息回填 → 模型据此生成最终回复`。

        🔴 **防重复**（照陪伴插件规则 2）：
           `ToolRegistry` 的 `max_calls_per_turn=1` 会拒绝同一工具的本轮第二次调用。
           实测：**我们的上游模型确实会重复调用**（回填结果后它又调一次），
           所以这条防护是必需的。

        ⚠️ 只跑**一轮**（不是无限循环）——
           如果模型拿到工具结果后还想要更多，就按它的原文返回，由用户决定下一步。
        """
        if self._tools is None or not first.wants_tools:
            return first

        counts: dict[str, int] = {}
        payloads: list[dict[str, Any]] = []
        tool_msgs: list[Message] = []
        attachments: list[dict[str, Any]] = []      # ⭐ 工具产出的可展示资源
        dispatch = self.dispatch

        # ⚠️ 用**下发方式**解析（而不是写死 tool_calls 结构）
        #
        # 🆕 工具执行前把"当前人设"放进请求级上下文（2026-09-25）：
        #    人设参考图这类**per-persona** 的能力需要知道这一轮是哪个角色。
        #    用 ContextVar 而不是成员变量 —— 两个会话并发生图时，
        #    成员变量会被后一个请求改掉 → **画错脸**，而且单测看不出来（串行）。
        with tool_context.tool_scope(persona=persona):
            for call in dispatch.extract_calls(first):
                result = await self._tools.execute(call, call_counts=counts)
                payloads.append({"tool": call.name, "status": result.status})
                # 收集图片等附件（非流式返回必须带上，否则用户看不到图）
                attachments.extend(_extract_attachments(result))
                # 同样用下发方式渲染回填消息（function calling → role=tool；标签 → user）
                tool_msgs.append(dispatch.render_result(result, call_id=call.id))

        log.info(
            "工具执行完毕，回填结果",
            extra={"data": {"calls": payloads, "count": len(tool_msgs)}},
        )

        # 把助手的 tool_calls 消息 + 工具结果追加进去，再问一次
        followup = list(messages)
        followup.append(
            Message(
                role=ROLE_ASSISTANT,
                content=first.content or "",
                tool_calls=json.dumps(
                    [
                        {
                            "id": str((r.get("function") or {}).get("name") or "") and r.get("id"),
                            "type": "function",
                            "function": r.get("function") or {},
                        }
                        for r in first.tool_calls
                    ],
                    ensure_ascii=False,
                ),
            )
        )
        followup.extend(tool_msgs)

        tool_payload2, _ = self._tool_payload_and_sections()
        try:
            second = await self._client.chat(
                followup,
                model=target.model,
                base_url=target.base_url,
                api_key=target.api_key,
                timeout=target.timeout,
                tools=tool_payload2.get("tools"),
                extra=body_extra,
            )
        except Exception:  # noqa: BLE001 — 工具已执行，第二问失败不该丢掉结果
            log.exception("工具结果回填后的二次请求失败")
            # 退化：至少把工具执行结果作为正文返回
            names = "、".join(p["tool"] for p in payloads)
            return ChatResult(
                content=first.content or f"（已执行 {names}）",
                model=first.model,
                finish_reason="tool_executed",
                tool_calls=first.tool_calls,
                attachments=attachments,
            )

        # 若第二次仍要调工具 → 按防重复规则，它会被拒绝；这里只保留其文本内容
        if second.wants_tools and not second.content:
            log.warning("模型二次请求工具，已按每轮一次规则不再执行")
            second = ChatResult(
                content="",
                model=second.model,
                finish_reason="tool_already_called",
                tool_calls=second.tool_calls,
            )

        second.attachments = attachments
        return second

    # ---- 流式 ----

    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamChunk]:
        """流式对话。

        ⚠️ 持久化**在流结束后**做（累积完整回复再写），
           且失败不影响已经流出去的 token。
        🔴 **顺序要紧**（与 `complete()` 同款，理由见那里）：
           面板**永远走流式**，所以漏了这里 = 会话级模型整个功能没生效
           —— 而且不报错，只是安静地用别的模型。
        """
        session, history, summary = await self.prepare_session(req)
        # ⚠️ `session` **可能是 `None`**（没有 session_id 的裸调用 / 会话不存在）。
        #    不去碰它的属性 —— 我第一版直接写 `session.model`，
        #    当场把 32 个既有用例打成 `AttributeError: 'NoneType'`。
        session_model = session.model if session is not None else ""
        self._reject_if_model_unusable(req, session)
        target = self.resolve_target(req.persona, req.model, session_model)

        messages = self.build_messages(req, history=history, summary=summary)
        # ⭐ P0-4：与 `complete()` 同款（**流式也要注入**，否则"流式聊天没有记忆"）
        # 🔴 同样**必须传 `session_id`**（理由见 `complete()` 那处）
        messages = await self._apply_llm_hook(messages, req.session_id)
        body_extra = apply_nested_params({}, req.custom_params) if req.custom_params else None

        log.info(
            "调用上游(流式)",
            extra={
                "data": {
                    "url": target.url(),
                    "model": target.model,
                    "messages": len(messages),
                    "history": len(history),
                    "has_summary": bool(summary),
                    "stream": True,
                }
            },
        )

        tool_payload, _ = self._tool_payload_and_sections()

        acc: list[str] = []
        tc_fragments: list[dict[str, Any]] = []

        async for chunk in self._client.chat_stream(
            messages,
            model=target.model,
            base_url=target.base_url,
            api_key=target.api_key,
            timeout=target.timeout,
            tools=tool_payload.get("tools"),
            extra=body_extra,
        ):
            if chunk.delta:
                acc.append(chunk.delta)
            if chunk.tool_call_deltas:
                tc_fragments.extend(chunk.tool_call_deltas)
            yield chunk

        # ⭐ 流式下模型可能要求调工具：执行后把结果作为**额外 chunk** 推给调用方
        if tc_fragments and self._tools is not None:
            calls = self.dispatch.extract_stream_calls(tc_fragments)
            if calls:
                # ⚠️ `extract_stream_calls` 现在返回 **ToolCall 对象**（不是 dict）——
                #    重构后这里忘了同步，导致 `c["function"]` 抛 TypeError，
                #    整个流被"内部错误"截断。**已加回归测试。**
                log.info(
                    "流式下收到工具调用",
                    extra={"data": {"count": len(calls), "tools": [c.name for c in calls]}},
                )
                counts: dict[str, int] = {}
                tool_msgs: list[Message] = []      # ⚠️ 必须先初始化（曾漏掉 → NameError）
                for call in calls:
                    # 🆕 与 `_run_tool_loop` 同款：把当前人设放进请求级上下文。
                    #    ⚠️ **只包 `execute` 这一句**，不要把 `yield` 也包进去 ——
                    #    async generator 没有自己的 context 副本（PEP 567 只给 Task 发），
                    #    `yield` 期间把作用域挂着会让"当前人设"暴露在**调用方的上下文**里。
                    with tool_context.tool_scope(persona=req.persona):
                        result = await self._tools.execute(call, call_counts=counts)
                    # 把结果带给上层（上层负责推图/渲染）
                    yield StreamChunk(
                        finish_reason="tool_executed",
                        done=False,
                        tool_call_deltas=[{
                            "tool": call.name,
                            "status": result.status,
                            "message": result.message,
                            "sent": result.sent,
                            "data": result.data,
                            "final_response_instruction": result.final_response_instruction,
                        }],
                    )
                    tool_msgs.append(
                        self.dispatch.render_result(result, call_id=call.id)
                    )

                # ⭐ 工具执行完必须**再问一次模型**，否则用户只看到图、没有正文。
                #    实测踩到：流式下第一轮 `finish_reason=tool_calls` 且 content 为空，
                #    若就此结束，回复正文是空的（体验上像"哑巴"）。
                if tool_msgs:
                    followup = list(messages)
                    followup.append(
                        Message(
                            role=ROLE_ASSISTANT,
                            content="",
                            tool_calls=json.dumps(
                                [
                                    {"id": c.id, "type": "function",
                                     "function": {"name": c.name, "arguments": c.arguments}}
                                    for c in calls
                                ],
                                ensure_ascii=False,
                            ),
                        )
                    )
                    followup.extend(tool_msgs)

                    async for chunk in self._client.chat_stream(
                        followup,
                        model=target.model,
                        base_url=target.base_url,
                        api_key=target.api_key,
                        timeout=target.timeout,
                        tools=tool_payload.get("tools"),
                        extra=body_extra,
                    ):
                        if chunk.delta:
                            acc.append(chunk.delta)
                        yield chunk

        if req.persist and session is not None and acc:
            last_user = next(
                (m.content for m in reversed(req.messages) if m.role == ROLE_USER), ""
            )
            await self.record(
                session.id, user_text=last_user, assistant_text="".join(acc)
            )
            self.schedule_summarize(session.id)


def _extract_attachments(result: ToolResult) -> list[dict[str, Any]]:
    """从工具结果里提取**可展示资源**（当前是图片 URL）。

    ⚠️ 只认约定字段 `urls`，不猜别的形状 —— 避免把任意 data 当附件。
    🔴 **失败结果一律不提取**（守卫放函数内部，调用方漏不掉）。
    """
    if not result.success:
        return []
    data = result.data or {}
    urls = data.get("urls")
    if not isinstance(urls, list):
        return []
    kind = str(data.get("kind") or "")
    caption = str(data.get("caption") or "")
    return [
        {"type": "image", "url": str(u), "kind": kind, "caption": caption}
        for u in urls
        if isinstance(u, str) and u
    ]


# ---------------------------------------------------------------------------
# 工具：把请求体解析成 ChatRequest
# ---------------------------------------------------------------------------


def parse_chat_request(body: dict[str, Any]) -> ChatRequest:
    """解析 OpenAI 形态的请求体。

    ⚠️ 只做**协议转换与校验**，不加业务规则。
    """
    if not isinstance(body, dict):
        raise ChatError("请求体必须是 JSON 对象")

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise ChatError("缺少 messages 或为空")

    messages = [from_upstream_message(m) for m in raw_messages]

    # 用户自定义参数：去掉我们认识的标准字段，其余透传（支持 `.` 嵌套）
    # ⚠️ `session_id` 是我们的扩展 —— 既不是标准字段，也不该透传给上游
    reserved = {"model", "messages", "stream", "session_id"}
    custom = {k: v for k, v in body.items() if k not in reserved}

    return ChatRequest(
        messages=messages,
        model=str(body.get("model") or ""),
        stream=bool(body.get("stream", False)),
        custom_params=custom,
        session_id=str(body.get("session_id") or ""),
    )
