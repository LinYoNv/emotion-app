# -*- coding: utf-8 -*-
"""把宿主的模型能力接成 AstrBot 插件的 provider（装配层）。

## 为什么需要这一层

AstrBot 插件调模型有**两条路**：
  1. `context.llm_generate(...)` —— 我们的 `LLMPort` 已经支持；
  2. **`provider.text_chat(...)`** —— 供应商对象上的方法，
     记忆抽取/总结、日程生成、关系分析**全走这条**
     （实测：`memory_companion/core/service.py:7519`、`summarizer.py:77`、`audit.py:334`）。

`host/astrbot_host/provider.py` 里的 `Provider.text_chat` 已经写好了"转调 `LLMPort.generate()`"，
所以本模块要做的只有一件事：**提供一个真的能打到我们上游的 `LLMPort` 实现**，
并把它交给 `Provider`。

## 设计（照项目「领域层只依赖 Protocol」的约定）

```
插件 provider.text_chat(prompt, system_prompt)
        ↓  （host/astrbot_host/provider.py:Provider.text_chat）
LLMPort.generate(prompt=, messages=, system_prompt=, model=)
        ↓  （本模块 UpstreamLLMPort）
我们的上游 HTTP（OpenAI 兼容）
```

⚠️ 两条铁律：
  * **不伪造**：上游失败就**抛错**，让插件走它的降级/报错路径
    （返回编造的"摘要"会被写进记忆库，比失败糟得多）；
  * **不静默**：所有失败都记日志，并保留上游的状态码/原因。
"""
from __future__ import annotations

import logging
from typing import Any

# 🔴 `PROVIDER_TYPE_CHAT` 等常量来自 store(唯一权威定义),**不在这里另抄一份** ——
#    抄一份就会有一天两边不一致(N0 bug 的同类:同一个概念两个地方各写了一遍)。
from ..infra.providers_store import (
    PROVIDER_TYPE_CHAT,
    SUPPORTED_PROVIDER_TYPES,
)

logger = logging.getLogger("emotion_app.providers")


def normalize_tools(tools: Any) -> list[dict[str, Any]] | None:
    """把插件给的**工具集对象**转成上游要的 OpenAI schema 列表。

    🔴 实现委托给宿主侧的 `astrbot_host.response_bridge.normalize_tools`
       （**唯一入口**）—— 与 `Provider.text_chat` 走的是同一段转换逻辑。
       在本层再写一份必然会漂移（本项目的老坑：「同一语义多处实现」）。

    ⚠️ 宿主没装（`plugins.enabled=false` 或 `astrbot_host` 不在 sys.path）时
       **如实降级为 `None`**：宁可"这轮没带工具"，
       也不要抛异常把真实原因埋掉（插件本来就有无工具的降级路径）。
    """
    try:
        from astrbot_host.response_bridge import normalize_tools as _impl
    except Exception:  # noqa: BLE001
        logger.warning(
            "宿主工具集转换器不可用（astrbot_host 未装配）→ 本轮按不带工具处理"
        )
        return None
    return _impl(tools)


def _looks_like_signature_mismatch(exc: TypeError, kwargs: dict[str, Any]) -> bool:
    """这个 `TypeError` 是不是"后端不接受某些关键字参数"？

    🔴 为什么需要它（2026-09-21 实测真 bug）：
       `UpstreamLLMPort.generate` 里有个"后端签名不匹配"的兜底重试。
       原来它**无条件**接住任何 `TypeError` → 把**序列化错误**
       （`Object of type ToolSet is not JSON serializable`）也当成签名问题，
       用不带连接参数的签名重试 → 最终报「上游 base_url 为空」。
       现场看到的错**离根因隔了三层**，排查代价极高。

    判据（宽松但足够）：错误信息里出现 CPython 对"意外关键字参数"的标准措辞。
    只有当**我们传的参数名**出现在消息里时才认 —— 别的 TypeError 一律继续抛。
    """
    text = str(exc)
    if "unexpected keyword" not in text and "positional argument" not in text:
        return False
    return any(name in text for name in kwargs)


class UpstreamLLMPort:
    """把我们的上游聊天后端适配成 AstrBot 的 `LLMPort`。

    它的 `generate()` 直接调**领域层的 `ChatBackend`**（既有的、已验证的实现），
    所以插件用的模型与我们面板聊天用的是**同一套上游配置**。

    ⚠️ 刻意**不**走 `ChatService.complete()`：那条路会带人设/历史/工具循环，
       而插件要的是"给什么提示词就回什么"的**裸模型调用**。
       走 ChatService 会让插件的记忆抽取莫名其妙带上角色扮演上下文
       （而且历史会互相污染）。
    """

    def __init__(
        self,
        *,
        backend: Any,
        model: str = "",
        base_url: str = "",
        api_key: str = "",
        timeout: float = 60.0,
    ) -> None:
        self._backend = backend
        self._model = model
        self._base_url = base_url
        self._api_key = api_key
        self._timeout = timeout

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
        """调用上游，返回带 `.content` / `.usage` 的结果（`ChatResult`）。

        🔴 **异常直接抛**（不吞）：插件的 `token_budget` 靠捕获它记用量/重试；
           吞掉会变成"成功但空回复"的静默失败。

        🔴 **`**kwargs` 必须真的进上游请求体**（`max_tokens` 等）。
           实测踩到：本方法签名收了 `**kwargs` 却**从不使用** ——
           插件传的 `max_tokens`（`token_budget.py:1530`，用于控成本）
           被**静默丢弃**，而且一行日志都没有。上游于是按自己的默认长度生成，
           成本与插件面板显示的预算对不上。→ 现在统一并进 `extra`
           （`build_body` 会做 `body.update(extra)`，故它直接成为请求体顶层字段）。
        """
        if self._backend is None:
            raise RuntimeError(
                "供应商未接入聊天后端（UpstreamLLMPort 的 backend 为空）"
            )

        # 组装 OpenAI 格式消息（system 在前）
        msgs: list[dict[str, Any]] = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        if isinstance(messages, list):
            msgs.extend(messages)
        elif prompt:
            msgs.append({"role": "user", "content": prompt})

        target_model = model or self._model or ""

        # ⚠️ 只透传**非 None** 的值：`{"max_tokens": None}` 会被部分网关判 400。
        extra: dict[str, Any] = {
            k: v for k, v in (kwargs or {}).items() if v is not None
        }
        if extra:
            logger.debug("透传上游参数：%s", sorted(extra))

        # 🔴🔴 两条路：后端有 `complete()` 就用它；没有就**回落到 `chat()`**。
        #
        # 为什么必须回落（这是修掉的真 bug，不是洁癖）：
        #   装配时交给我们的后端是 `OpenAIChatBackend`（`domain/backends.py`），
        #   它**只有 `chat` / `chat_stream`，根本没有 `complete()`**。
        #   而原来这里只写了 `self._backend.complete(...)` + `except TypeError` ——
        #   `AttributeError` **不是** `TypeError`，所以接不住：
        #   插件在**启动路径**上拿到端口后一调用就炸，且错误信息完全指不到根因。
        #   （热同步路径有 `hasattr(backend,"complete")` 闸门，所以只有启动路径中招 ——
        #    这类"两条路只有一条通"的缺口正是最容易被漏掉的。）
        #
        # ⚠️ 回落时**必须把上游参数带全**（model/base_url/api_key/timeout）：
        #    `chat()` 的 `model` 是**必填关键字**，且插件用的是它自己那条源的
        #    base_url/key（与面板主聊天可能不是同一条）—— 漏传就会打到默认上游。
        complete = getattr(self._backend, "complete", None)
        if callable(complete):
            # ⚠️ **连接信息必须传**（实测踩到）：插件用的是它自己那条源，
            #    可能与面板主聊天不是同一条上游。漏传 → 打到默认上游/拼不出地址。
            upstream_kwargs: dict[str, Any] = {
                "messages": msgs,
                "model": target_model or None,
                # 🔴🔴 **工具集必须先转成 OpenAI schema**（2026-09-21 修的真 bug）。
                #    插件给的是 `ToolSet` **对象**（`page_api.py:4217`），
                #    原样透传 → `json` 序列化时
                #    `TypeError: Object of type ToolSet is not JSON serializable`。
                #    ⚠️ 那个 TypeError 还会被下面的兜底**误接**，
                #    最终报成「上游 base_url 为空」—— 离根因隔了三层。
                "tools": normalize_tools(tools),
                "base_url": self._base_url,
                "api_key": self._api_key,
                "timeout": self._timeout,
                # 🔴 插件传的 OpenAI 兼容参数（max_tokens 等）—— 见方法 docstring
                "extra": extra or None,
            }
            try:
                return await complete(**upstream_kwargs)
            except TypeError as exc:
                # 后端签名差异兜底：有的实现只认 messages/model（不认识连接参数）。
                #
                # 🔴🔴 **必须把"签名不匹配"与"别的 TypeError"区分开**
                #    （2026-09-21 实测踩到）：原来无条件重试 → 把
                #    **序列化 TypeError**（`Object of type ToolSet is not
                #    JSON serializable`）也当成签名问题，用**不带 base_url**
                #    的签名重试 → 最后报「上游 base_url 为空」。
                #    那个错误**完全是误导**，把真实根因埋掉了。
                #
                #    判据：错误信息里必须提到**参数名**（`unexpected keyword`
                #    / `got an unexpected` / `positional argument`）才算签名问题。
                if not _looks_like_signature_mismatch(exc, upstream_kwargs):
                    raise
                logger.warning(
                    "后端 %s 不接受连接参数（签名不匹配）→ 按最简签名重试",
                    type(self._backend).__name__,
                )
                return await complete(messages=msgs, model=target_model or None)

        chat = getattr(self._backend, "chat", None)
        if callable(chat):
            # `chat()` 收的是领域层 `Message`（不是 dict）→ 这里做一次转换。
            from ..domain.message import Message as _Msg

            domain_msgs = [
                _Msg(role=str(m.get("role") or "user"), content=str(m.get("content") or ""))
                for m in msgs
                if isinstance(m, dict)
            ]
            return await chat(
                domain_msgs,
                model=target_model,
                base_url=self._base_url,
                api_key=self._api_key,
                timeout=self._timeout,
                tools=tools or None,
                # 🔴 同上：不走 extra 的话 max_tokens 又会被静默丢掉
                extra=extra or None,
            )

        # 两条路都不通 → **如实抛错**（绝不返回空回复让插件以为"模型答了空话"）
        raise RuntimeError(
            f"供应商后端 {type(self._backend).__name__} 既没有 complete() 也没有 chat()，"
            "无法调用模型（UpstreamLLMPort 需要其中之一）"
        )

    def describe(self) -> dict[str, Any]:
        """诊断信息（**不含明文 key**）。"""
        return {
            "model": self._model,
            "base_url": self._base_url,
            "api_key_configured": bool(self._api_key),
            "backend": type(self._backend).__name__ if self._backend else "",
        }


def _first_key(raw: Any) -> str:
    """从 `key` 字段取第一把 key。

    🔴 **真实契约里 `key` 是【列表】**（AstrBot `provider_sources[].key = ["sk-..."]`）。
       我们内部只用一个（自用场景不需要多 key 轮询），但**读的时候必须兼容列表**，
       否则 `str(list)` 会把 `"['sk-x']"` 当成 key 发出去 → 上游 401。
    ⚠️ 同时兼容**旧格式的裸字符串**（迁移前的数据）。
    """
    if isinstance(raw, (list, tuple)):
        for item in raw:
            text = str(item or "").strip()
            if text:
                return text
        return ""
    return str(raw or "").strip()


def flatten_two_layer(
    store: dict[str, Any],
    provider_type: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """把**两层**配置展平成扁平的 Provider 列表（一个模型 = 一个 Provider）。

    真实契约（`docs/ASTRBOT_PROVIDER_CONTRACT.md` §2/§3）是两层：
        provider_sources（端点）  1 : N  providers（模型）
    而 AstrBot 的 `Provider` 对象是**一个模型一个实例**（`id = "源id/模型id"`），
    模型自己没有 base_url/key —— 那些在**源**上。

    → 所以这里做**展平**：每个模型继承它所属源的 `base_url`/`api_key`/`timeout`。

    🔴🔴 **`provider_type` 过滤（2026-09-21 修的真 bug）**：
        `provider_type` 写在**源**上。本函数原来**完全不看它** → 后果实测：
        配一个 `provider_type=text_to_speech` 的源+模型后，
        插件的**对话**供应商接口 `items` 里**混进了语音模型**
        （`['fast/gemini-3.5-flash','tts-probe/zh-CN-XiaoxiaoNeural']`，而 `tts_items=[]`）
        → **插件会把语音模型当对话模型去调**，界面全正常、真调用才炸在很远的地方。

    Args:
        store: 两层配置（`provider_sources` + `providers`）。
        provider_type: 只保留这个类别的模型；`None` = 不过滤（返回全部）。
            **默认 `None` 是为了不改变旧调用方的语义**；需要"只要对话模型"的
            调用方（插件的 `ProviderManager`）应**显式传** `PROVIDER_TYPE_CHAT`。

    Returns:
        `(flattened, default_id)`。⚠️ `default_id` 只有落在返回列表内才回传，
        否则回传 `""`（避免调用方拿着一个"不在列表里的默认值"去查）。
    """
    sources = store.get("provider_sources")
    models = store.get("providers")

    # 兼容：store 里若已是旧扁平格式（没有 provider_sources），原样返回。
    # ⚠️ 旧格式**没有** provider_type 信息，无法按类型过滤 —— 如实放行并在日志里说明。
    if not isinstance(sources, list) or not isinstance(models, list):
        flat = [x for x in (models or store.get("providers") or []) if isinstance(x, dict)]
        if provider_type is not None and flat:
            logger.debug(
                "旧扁平格式没有 provider_type 字段 → 无法按 %r 过滤，全部 %d 条放行",
                provider_type, len(flat),
            )
        return flat, str(store.get("default_provider_id") or "")

    by_source_id: dict[str, dict[str, Any]] = {}
    for src in sources:
        if not isinstance(src, dict):
            continue
        sid = str(src.get("id") or "").strip()
        if sid:
            by_source_id[sid] = src

    out: list[dict[str, Any]] = []
    skipped_by_type: dict[str, int] = {}
    for m in models:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or "").strip()
        model_name = str(m.get("model") or "").strip()
        if not mid or not model_name:
            logger.warning("跳过缺少 id/model 的模型配置：%r", m)
            continue
        src = by_source_id.get(str(m.get("provider_source_id") or "").strip())
        if src is None:
            # 🔴 **不静默**：悬空引用会让这个模型永远调不通，必须留痕
            logger.warning(
                "模型 %r 指向不存在的 provider_source_id=%r —— 跳过（配置有问题）",
                mid, m.get("provider_source_id"),
            )
            continue
        if m.get("enable") is False or src.get("enable") is False:
            # 禁用项**不进管理器**（插件不该看到停用的模型）
            continue
        # 🔴 按 provider_type 分流（源上没写 = 对话，与 store 的默认值一致）
        src_type = str(src.get("provider_type") or PROVIDER_TYPE_CHAT).strip()
        if provider_type is not None and src_type != provider_type:
            skipped_by_type[src_type] = skipped_by_type.get(src_type, 0) + 1
            continue
        out.append(
            {
                "id": mid,
                "name": mid,
                "model": model_name,
                # 继承所属源的连接信息
                "base_url": str(src.get("api_base") or ""),
                "api_key": _first_key(src.get("key") or src.get("api_key")),
                "timeout": float(src.get("timeout") or 60.0),
                "provider_type": src_type,
            }
        )

    # 🔴 **不静默**：被类型过滤掉多少要留痕（否则"模型消失了"会变成幽灵问题）
    if skipped_by_type:
        logger.debug(
            "按 provider_type=%r 过滤：保留 %d 条，跳过 %s",
            provider_type, len(out), skipped_by_type,
        )

    default_id = str(store.get("default_provider_id") or "")
    # 默认值必须落在返回列表里，否则调用方会拿一个查不到的 id 去当默认
    if default_id and default_id not in {item["id"] for item in out}:
        kept = next((item["id"] for item in out), "")
        logger.debug(
            "默认供应商 %r 不在返回列表内（可能类型不符）→ 回传 %r",
            default_id, kept,
        )
        default_id = kept
    return out, default_id


def flatten_by_type(store: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """按 `provider_type` 分组展平 —— 给"需要各类供应商"的调用方用。

    插件面板会分别问 `get_all_stt_providers()` / `get_all_tts_providers()` /
    `get_all_embedding_providers()`，本函数一次把各类都算好，避免重复遍历
    （也保证**各类用的是同一套过滤规则**，不会出现"这里滤了那里没滤"）。

    Returns:
        `{provider_type: [扁平模型…]}`，**只含非空类别**。
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for ptype in SUPPORTED_PROVIDER_TYPES:
        items, _ = flatten_two_layer(store, provider_type=ptype)
        if items:
            grouped[ptype] = items
    return grouped


def build_provider_manager(
    *,
    backend: Any,
    providers: list[dict[str, Any]] | None = None,
    default_id: str = "",
) -> Any:
    """按配置造一个装配好的 `ProviderManager`。

    Args:
        backend: 领域层的 `ChatBackend`（上游客户端）。
        providers: **扁平**列表，每项形如
            `{"id","name","model","base_url","api_key","timeout"}`。
            ⚠️ 两层结构要先过 `flatten_two_layer()`。
        default_id: 默认供应商 id（空 = 用第一个）。

    ⚠️ `providers` 为空时返回**空管理器**（`get_using_provider()` → None）——
       这是**如实**的："一个供应商都没配"。插件会对 None 走降级，
       比我们凭空造一个默认供应商好（后者会让插件以为配好了）。
    """
    from astrbot_host.provider import Provider, ProviderManager

    pm = ProviderManager()
    for item in providers or []:
        pid = str(item.get("id") or "").strip()
        if not pid:
            logger.warning("跳过没有 id 的供应商配置：%r", item)
            continue
        port = UpstreamLLMPort(
            backend=backend,
            model=str(item.get("model") or ""),
            base_url=str(item.get("base_url") or ""),
            api_key=str(item.get("api_key") or ""),
            timeout=float(item.get("timeout") or 60.0),
        )
        pm.register(
            Provider(
                provider_id=pid,
                model=str(item.get("model") or ""),
                base_url=str(item.get("base_url") or ""),
                api_key=str(item.get("api_key") or ""),
                name=str(item.get("name") or pid),
                timeout=float(item.get("timeout") or 60.0),
                llm=port,
            )
        )

    if default_id:
        if not pm.set_default(default_id):
            # 🔴 不静默：配置里指的默认 id 不存在，说明配置有问题
            logger.warning(
                "默认供应商 id=%r 不存在（配置里可用的：%s）—— 将用第一个作为默认",
                default_id, list(pm.inst_map.keys()),
            )
    return pm


__all__ = ["UpstreamLLMPort", "build_provider_manager"]
