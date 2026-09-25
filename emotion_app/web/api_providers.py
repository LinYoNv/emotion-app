# -*- coding: utf-8 -*-
"""模型供应商管理 API —— **两层结构**，路径与响应形状对齐真实 AstrBot
（契约 `docs/ASTRBOT_PROVIDER_CONTRACT.md` §2/§3）。

## 为什么重写成两层

我原来做成了**单层**（`{id,name,model,base_url,api_key,timeout}` 一条就是全部），
而真实契约是两层：

    provider_sources   ← 端点层（api_base + key + timeout + proxy + custom_headers）
          ↑ 1 : N
    providers          ← 模型层（id 形如 "源id/模型名"）

单层表达不了"一个端点挂 5 个模型" —— 用户要在面板上把地址和 key 抄 5 遍。
存储层（`infra/providers_store.py`）已改成两层，本文件把路由与响应形状对齐契约。

## 路由表（★ = 契约 §2/§3 新增；旧路由**保留可用**但返回**新结构**）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/v1/provider-sources` | 源列表 / 新增源 |
| GET/PUT/DELETE | `/api/v1/provider-sources/by-id?source_id=<id>` | 单条源 |
| GET | `/api/v1/provider-sources/models` | 全部源的模型（按源分组） |
| GET/POST | `/api/v1/provider-sources/<源id>/models` | ★ **获取模型列表**（打上游 `/models`） |
| GET/POST | `/api/v1/provider-sources/<源id>/providers` | 该源下已配置的模型 / 给它加模型 |
| GET/POST | `/api/v1/providers` | 模型列表（**脱敏**，含 `provider_sources`）/ 新增模型 |
| GET/PUT/DELETE | `/api/v1/providers/by-id?provider_id=<id>` | 单条模型 |
| GET/PUT/PATCH | `/api/v1/providers/<path:id>` | 单条模型（id 需 URL 编码，含 `/`） |
| POST | `/api/v1/providers/<path:id>/test` | 连通性测试（PATCH 兼容旧前端） |
| DELETE | `/api/v1/providers/<path:id>` | 删除 |
| POST | `/api/v1/providers/<path:id>/default` | 设为默认 |

### ⚠️ 四条实测出来的路由细节（别凭直觉改）

1. 🔴 **单条模型相关的五件事共用一条 `<path:provider_id>` 规则**，
   在**函数内部按后缀分派**（`/default`、`/test`）。写成多条规则时会被前一条吃掉：
   实测 `POST /api/v1/providers/syuan%2Fgpt-5.5/default` 回 **405**。
2. 🔴 **必须用 `path` 转换器**。默认的 `<provider_id>` 匹配不到 URL 编码的斜杠：
   实测 `POST /api/v1/providers/a%2Fb/test` 在 Quart 0.22 + Hypercorn 下是 **404**，
   而 `path` 转换器在真 Hypercorn 上实测能正确解出 `a/b`。
   模型 id **一定含 `/`**，所以这是硬要求。
   → 于是四种形态都通：`by-id?provider_id=<编码>`、`/providers/<编码>`、
   `/providers/<未编码斜杠>`、以及带 `/test`·`/default` 后缀的形态。
3. **静态段优先于 `<path:...>`**：`/providers/by-id` 与 `/providers/<path:id>` 能共存，
   实测 `by-id` 命中静态路由。`/provider-sources/models` 同理不会被
   `<source_id>/models` 抢走（反过来才有问题：真实站上 `/provider-sources/models`
   回 422，是它把 `models` 当成源 id 了）。
4. **`by-id` 的 query 参数名照真实实现**：源是 `source_id`、模型是 `provider_id`。
   实测用 `id=` 会得到 422/400（`Missing key: source_id`）—— 名字写错就是不可用。

## 响应信封（照 AstrBot）

成功：`{"status":"ok","message":null,"data":{...}}`
失败：`{"status":"error","message":"...","reason":"<机器可读>","error":{"message","type"}}`

🔴 失败的响应里**额外带上 `error.{message,type}`**：这是**我们旧前端**的读取方式
（`static/app.js` 的 `api()` 读 `data.error.message`）。AstrBot 只有 `message`，
若只回 `message`，旧前端会把所有错误显示成干巴巴的 "HTTP 400"。
两个都给 = 新老都读得到，且**没有任何多余信息**。

## 四条硬要求（都来自实测痛点，不是洁癖）

1. 🔴 **`key` 永不回显**：响应里 `key` 是掩码列表（`["••••••••"]`）+ `key_configured` 布尔。
   🔴 **不要**把掩码换成固定长度的假 key：那会让"key 有多长"也漏出去，且掩码看起来像真 key。
2. 🔴 **PUT/PATCH 时 `key` 为空 = 不修改**（字段缺失 / `""` / `[]` / 掩码占位符都算空）。
   面板编辑时 key 框一律留空或显示掩码，若当成清空，用户改个超时就会把 key 抹掉。
3. 🔴 **增删改后必须同步宿主 `ProviderManager`**（一个模型 = 一个 `Provider`，
   `provider_id` = `源id/模型名`，连接信息从所属源取）。
   宿主**可能未启用/未启动**：那种情况下管理 API **仍要能工作**，只跳过同步并**记日志**。
   ⚠️ 改**源**（api_base/key/timeout）时，该源下**所有模型**都要重注册 ——
   宿主的 `Provider` 是不可变快照，只改文件的话插件还在用旧地址。
4. 🔴 **悬空引用一律 400**：模型的 `provider_source_id` 指向不存在的源 →
   它永远调不通却"看起来配好了"，是最难查的 bug 类型。

## 关于 `provider_type`

情绪明确说：情绪app **不需要 Agent 能力**。所以只支持 `chat_completion`，
其它分类（embedding / text_to_speech / rerank …）**如实 400 拒绝**并说明
"本平台暂不支持该类型"，**不假装支持**（作者验收判据③：能力不对等要如实 degraded）。
"""
from __future__ import annotations

import time
from typing import Any

from quart import Blueprint, current_app, jsonify, request

from ..infra.logging_setup import get_logger, scrub_text
from ..infra.providers_store import (
    DEFAULT_PATH,
    PROVIDER_TYPE_CHAT,
    ProviderError,
    ProviderModel,
    ProviderNotFound,
    ProviderSource,
    ProviderStore,
    ProviderValidationError,
    UnsupportedProviderType,
)

log = get_logger(__name__)

#: 连通性测试用的提示词。
#: ⚠️ 刻意用一个**极短**的招呼语：这是连通性测试，不是能力评测，
#:    输出越短越快越省额度。
TEST_PROMPT = "hi"

#: 连通性测试超时上限（秒）。
#: ⚠️ **不受 source.timeout 影响**：用户可能把业务超时设成 300s，
#:    但面板上点"测试"按钮等 5 分钟是不可接受的。
#:    也不能太短 —— 实测 NAI 那条链路抖动可达 26s，中转站首包更慢。
TEST_TIMEOUT_SECONDS = 20.0

#: 「获取模型列表」的超时（秒）。列模型比聊天快得多，20s 足够。
MODELS_TIMEOUT_SECONDS = 20.0

#: 样例回复的截断长度（契约：`sample` = "回复前 40 字"）
SAMPLE_CHARS = 40


# ---------------------------------------------------------------------------
# 响应信封
# ---------------------------------------------------------------------------


def _ok(data: Any, message: str | None = None, status: int = 200) -> tuple[Any, int]:
    """成功响应（AstrBot 信封）。"""
    return jsonify({"status": "ok", "message": message, "data": data}), status


def _fail(
    message: str, status: int, reason: str = "invalid_request_error", **extra: Any
) -> tuple[Any, int]:
    """失败响应。

    🔴 同时给 `error.{message,type}`：旧前端 `static/app.js` 的 `api()` 只认这个形状，
       只回 AstrBot 的 `message` 会让它把所有错误显示成 "HTTP 400"。
    """
    body: dict[str, Any] = {
        "status": "error",
        "message": message,
        "reason": reason,
        "error": {"message": message, "type": reason},
    }
    body.update(extra)
    return jsonify(body), status


def _store_error(exc: ProviderError) -> tuple[Any, int]:
    """把存储层异常映射成 HTTP 状态 + 原因码。

    ⚠️ `ProviderValidationError`（含 `UnsupportedProviderType`）→ **400 且说清原因**；
       `ProviderNotFound` → 404。悬空引用是 400（不是 404）——
       请求体里的引用非法是**客户端错误**，与"被访问的对象不存在"是两回事。
    """
    if isinstance(exc, ProviderNotFound):
        return _fail(str(exc), 404, "not_found")
    if isinstance(exc, UnsupportedProviderType):
        return _fail(str(exc), 400, "unsupported_provider_type")
    if isinstance(exc, ProviderValidationError):
        return _fail(str(exc), 400, exc.reason or "invalid_provider")
    return _fail(str(exc), 400, exc.reason or "provider_error")


def build_blueprint(require_auth: Any) -> Blueprint:
    """造供应商管理蓝图。

    Args:
        require_auth: 返回 None（通过）或 (响应, 状态码)（拒绝）的可调用对象。
    """
    bp = Blueprint("providers", __name__, url_prefix="/api/v1")

    # ==================================================================
    # 装配：存储 / 宿主
    # ==================================================================

    def _store() -> ProviderStore:
        """取存储实例。

        ⚠️ 路径可被 `app.config["EMOTION_PROVIDERS_PATH"]` 覆盖（测试用）——
           与既有 `EMOTION_MODELS_TRANSPORT` 的可注入风格一致。
           默认就是契约冻结的 `data/providers.json`
           （`emotion_app/web/plugin_host.py` 读的是同一个路径）。
        """
        path = current_app.config.get("EMOTION_PROVIDERS_PATH") or DEFAULT_PATH
        cached = current_app.config.get("EMOTION_PROVIDERS_STORE")
        if cached is not None and str(cached.path) == str(path):
            return cached
        store = ProviderStore(path)
        current_app.config["EMOTION_PROVIDERS_STORE"] = store
        return store

    def _provider_manager() -> tuple[Any, str]:
        """取宿主的 `ProviderManager`。

        Returns:
            `(pm, 不可用的原因)`；`pm is None` 表示"宿主缺席"，**不是错误** ——
            调用方跳过同步并记日志即可（宿主是按配置可选的）。
        """
        service = current_app.config.get("EMOTION_PLUGIN_HOST")
        if service is None:
            return None, "插件宿主未启用（EMOTION_PLUGINS_ENABLED=false）"

        # 兼容三种持有方式（装配层给的是 `PluginHostService`，
        # 它把真实的 `PluginHost` 放在 `.host` 上；`.host` 为 None = 还没 start()）
        ctx = getattr(service, "context", None)
        if ctx is None:
            inner = getattr(service, "host", None)
            if inner is None:
                return None, "插件宿主尚未启动（或启动失败，见服务日志）"
            ctx = getattr(inner, "context", None)
        if ctx is None:
            return None, "插件宿主没有 context（启动未完成）"

        pm = getattr(ctx, "provider_manager", None)
        if pm is None:
            return None, "插件宿主的 context 上没有 provider_manager"
        return pm, ""

    def _llm_backend() -> Any:
        """取"领域层的上游客户端"，接到每个 Provider 上（让 `text_chat` 真能打到上游）。

        ⚠️ 形状要求（`provider_bridge.UpstreamLLMPort`）：后端要有
           `async complete(*, messages, model, tools)` **或** `async chat(messages, *, model, …)`
           —— 任一即可（`generate()` 优先 `complete()`，没有时回落 `chat()`）。
           **两条都没有的一律不接**（返回 None）。

        🔴 **为什么这道闸门不是多余的**（2026-09-20 实测）：
           装配层实际暴露出来的 `EMOTION_CHAT_BACKEND` 是领域层的
           `OpenAIChatBackend`，而它原本**只有 `chat()` / `chat_stream()`，没有 `complete()`**；
           `UpstreamLLMPort.generate()` 调的却是 `backend.complete(...)`，
           且它的兜底只写了 `except TypeError` —— **接不住 `AttributeError`**。
           → 已按**两处都修**处理：①`UpstreamLLMPort` 增加 `chat()` 回落；
             ②`OpenAIChatBackend` 补上 `complete()`（互为保险，见 `domain/backends.py`）。
           → 现在 chat-only 的后端**是能用的**，闸门据此放行
             （判据：`test_chat_only_backend_is_actually_usable_at_startup`）。
           闸门保留的意义：拦住**两条都没有**的对象，把"静默坏掉"变成"启动时就有 WARNING"。
        """
        backend = current_app.config.get("EMOTION_CHAT_BACKEND")
        if backend is None:
            # 兜底：装配层没暴露时从 ChatService 上取（它是可选注入的，故 getattr 防御）
            backend = getattr(current_app.config.get("EMOTION_CHAT"), "_client", None)
        if backend is None:
            return None
        # ⚠️ 判据是 `complete` **或** `chat`（任一即可），不是只要 `complete`：
        #    回落路径已实现（`UpstreamLLMPort.generate` 会回落到 `chat()`），
        #    继续只认 `complete` 会**错杀**一个本来能用的后端 ——
        #    实测：`OpenAIChatBackend` 就是因此被拒 → 插件拿不到模型。
        if not (hasattr(backend, "complete") or hasattr(backend, "chat")):
            log.warning(
                "上游客户端 %s 既没有 complete() 也没有 chat()（只有 %s）—— 供应商将不接模型端口："
                "插件 text_chat 会如实报错，不会静默返回空回复。"
                "要修需在装配层补一个适配（照 provider_bridge.UpstreamLLMPort 的形状）",
                type(backend).__name__,
                [m for m in ("chat_stream", "generate") if hasattr(backend, m)] or "无",
            )
            return None
        return backend

    def _register_model(pm: Any, source: ProviderSource, model: ProviderModel,
                        *, make_default: bool) -> bool:
        """把**一个模型**注册成宿主的 `Provider`（连接信息从所属源取）。

        ⚠️ 这是两层 → 宿主一层接口的**展平点**：AstrBot 的 `Provider` 是
           "一个模型一个实例"，模型自己没有 base_url/key，那些在源上。

        Returns `llm_wired`（是否真的接上了模型端口）。

        🔴🔴 **只有【对话】模型才进宿主的 `ProviderManager`**（2026-09-21 修 N0）：
            `ProviderManager` 在插件眼里就是"**对话**供应商池"
            （`context.get_all_providers()` / `get_using_provider()` 都从它取）。
            把 TTS/STT/embedding 模型注册进去 → 插件会把语音模型当对话模型调。

            ⚠️ **这是 N0 的真正漏点**：我最初只在 `flatten_two_layer()` 加了类型过滤，
               单测全绿，但**真环境仍然泄漏** —— 因为**配置热同步**走的是本函数，
               它**不经过** `flatten_two_layer`（那条路只在服务启动装配时用）。
            **通则:同一个概念有多条写入路径时,每条都要过滤**(启动装配 / 热同步 / 未来新增的)。
        """
        from emotion_app.infra.providers_store import is_chat_provider_type

        if not is_chat_provider_type(source.provider_type):
            # 🔴 **不静默**：这是"配置有效但故意不进宿主"的正常分支，必须留痕，
            #    否则用户会问"我明明配了为什么插件看不到"（而日志里一个字没有）。
            log.info(
                "跳过 ProviderManager 同步（非对话类型不进对话池）：%s provider_type=%s",
                model.id, source.provider_type,
            )
            return None

        from emotion_app.web.plugin_host import ensure_host_on_path

        ensure_host_on_path()
        from astrbot_host.provider import Provider as HostProvider
        from emotion_app.web.provider_bridge import UpstreamLLMPort, _first_key

        backend = _llm_backend()
        # 🔴 **没有可用后端时，就真的不接端口**（`llm=None`），而不是接一个空壳。
        #    宿主 `Context._provider_usable()` 的判据是 `getattr(p, "_llm", None) is not None`，
        #    接空壳会让它回 True → 对插件**谎报"模型可用"**，插件不再走降级，
        #    直到每次调用都抛"供应商未接入聊天后端"。
        port = None
        if backend is not None:
            port = UpstreamLLMPort(
                backend=backend,
                model=model.model,
                base_url=source.api_base,
                api_key=_first_key(source.key),
                timeout=source.timeout,
            )
        hosted = HostProvider(
            provider_id=model.id,
            model=model.model,
            base_url=source.api_base,
            api_key=_first_key(source.key),
            name=model.id,
            timeout=source.timeout,
            llm=port,
        )
        # ⚠️ `register(make_default=False)` 在**没有默认**时也会把这条设成默认
        #    （`host/astrbot_host/provider.py` 的 register），所以新增第一条时
        #    宿主的默认与我们的 `default_provider_id` 语义一致。
        pm.register(hosted, make_default=make_default)
        log.info(
            "已同步模型到 ProviderManager",
            extra={
                "data": {
                    "id": model.id,
                    "provider_source_id": model.provider_source_id,
                    "make_default": make_default,
                    "default": getattr(pm, "default_provider_id", ""),
                    "llm_wired": port is not None,
                }
            },
        )
        if port is None:
            # 不静默：配置对了但模型端口没接上，插件调用时会失败 —— 必须提前说清
            log.warning(
                "模型 %s 未接入模型端口（llm=None）：宿主会如实报"
                "「模型不可用」，插件走自己的降级路径",
                model.id,
            )
        return port is not None

    def _sync_model(model: ProviderModel, *, make_default: bool = False) -> Any:
        """同步单个模型到宿主（宿主缺席时跳过并记日志）。"""
        pm, why = _provider_manager()
        if pm is None:
            # 🔴 宿主缺席是**正常情况**（插件默认关闭）→ 跳过同步，但必须留痕：
            #    否则用户"改了配置却没生效"时，日志里一个字都没有。
            log.info("跳过 ProviderManager 同步（%s）：%s", why, model.id)
            return None
        try:
            source = _store().require_source(model.provider_source_id)
        except ProviderNotFound:
            # 理论上到不了（写入前已校验），但**不许静默**：宿主里会缺这个模型
            log.error(
                "模型 %s 指向的源 %s 不存在，无法同步到宿主（配置已被改为悬空引用）",
                model.id,
                model.provider_source_id,
            )
            return None
        try:
            _register_model(pm, source, model, make_default=make_default)
        except Exception as exc:  # noqa: BLE001
            log.error("同步模型到 ProviderManager 失败（配置已保存）", exc_info=True)
            log.error("导入/注册失败原因：%s", exc)
            return None
        return pm

    def _sync_models_of_source(source_id: str) -> None:
        """# 🔴 改了**源**（api_base/key/timeout）→ 该源下**所有模型**都要重注册。

        宿主的 `Provider` 是**不可变快照**：只改文件的话，插件继续用旧地址/旧 key
        —— 而面板上一切正常（最难查的那种分叉）。
        """
        store = _store()
        models = store.list_models(source_id)
        if not models:
            log.info("源 %s 下没有模型，无需同步宿主", source_id)
            return
        pm, why = _provider_manager()
        if pm is None:
            log.info("跳过 ProviderManager 同步（%s）：源 %s 下的 %d 个模型", why, source_id, len(models))
            return
        default_id = store.default_provider_id
        for m in models:
            _sync_model(m, make_default=(m.id == default_id))

    def _sync_unregister(provider_id: str) -> None:
        pm, why = _provider_manager()
        if pm is None:
            log.info("跳过 ProviderManager 同步（%s）：移除 %s", why, provider_id)
            return
        try:
            gone = pm.unregister(provider_id)
            log.info(
                "已从 ProviderManager 移除",
                extra={
                    "data": {
                        "id": provider_id,
                        "existed": bool(gone),
                        "default": getattr(pm, "default_provider_id", ""),
                    }
                },
            )
        except Exception:  # noqa: BLE001
            log.error("从 ProviderManager 移除失败（配置已删）", exc_info=True)

    def _sync_set_default(provider_id: str) -> None:
        pm, why = _provider_manager()
        if pm is None:
            log.info("跳过 ProviderManager 同步（%s）：设默认 %s", why, provider_id)
            return
        if not provider_id:
            return
        try:
            if not pm.set_default(provider_id):
                # 🔴 不静默：宿主里没有这条 → 插件的 get_using_provider 会解析失败
                log.warning(
                    "ProviderManager 里没有 %r，无法设默认"
                    "（插件可能仍解析不到模型；宿主里现有：%s）",
                    provider_id,
                    list(getattr(pm, "inst_map", {}) or {}),
                )
        except Exception:  # noqa: BLE001
            log.error("设置 ProviderManager 默认供应商失败（文件已改）", exc_info=True)

    async def _body() -> dict[str, Any]:
        """取请求体（非对象一律当空对象；由各路由自己校验必填项）。"""
        body = await request.get_json(silent=True)
        return body if isinstance(body, dict) else {}

    # ==================================================================
    # 厂商预设：/api/v1/provider-presets（契约 §3）
    # ==================================================================

    @bp.get("/provider-presets")
    async def list_provider_presets():  # type: ignore[no-untyped-def]
        """厂商预设目录 → `{"status":"ok","data":{"presets":[...],"total":N}}`。

        query：`provider_type`（可选）= `chat_completion` / `text_to_speech` / `embedding`。
        非法值 → **400 `invalid_provider_type`**，消息里列出合法值（不是静默回空列表）。

        🔴 数据来源 = `infra/provider_presets.py`，抓自**真 AstrBot v4.27.5**
           （50 条），不是我们编的。其中只有 `openai_chat_completion` /
           `openai_responses` 我们**真能执行** —— 每条带 `executable_by_us`，
           前端据此**如实标注**（作者验收判据③：能力不对等要如实 degraded）。
        """
        denied = require_auth()
        if denied is not None:
            return denied

        from ..infra.providers_store import SUPPORTED_PROVIDER_TYPES
        from ..infra.provider_presets import list_presets, preset_public_view

        wanted = (request.args.get("provider_type") or "").strip()
        if wanted and wanted not in SUPPORTED_PROVIDER_TYPES:
            # ⚠️ 必须 400：把 "text_to_speach" 这类拼错**响亮**顶回去。
            #    静默返回空列表会让面板显示"没有预设"，看着像数据问题，其实是拼错了。
            return _fail(
                f"未知的 provider_type：{wanted}；合法值：{list(SUPPORTED_PROVIDER_TYPES)}",
                400,
                "invalid_provider_type",
            )

        items = list_presets(wanted or None)
        return _ok({"presets": [preset_public_view(p) for p in items], "total": len(items)})

    # ==================================================================
    # 端点层：/api/v1/provider-sources
    # ==================================================================

    @bp.get("/provider-sources")
    async def list_provider_sources():  # type: ignore[no-untyped-def]
        """源列表 → `{"status":"ok","data":{"provider_sources":[...]}}`（契约 §2）。

        🔴 **绝不回显 key** —— 脱敏由 `ProviderSource.public_view()` 保证，
           本函数不做任何字段投影（少一处手工拼装 = 少一处泄漏口）。
        """
        denied = require_auth()
        if denied is not None:
            return denied
        return _ok(_store().snapshot_sources())

    @bp.post("/provider-sources")
    async def create_provider_source():  # type: ignore[no-untyped-def]
        """新增源。校验失败 → **400 且说清是哪一项**。

        ⚠️ 成功返回 **201**（照 `api_admin.create_persona` 的先例）。
        AstrBot 真实实现回 200 `{"status":"ok","message":"更新 provider source 成功","data":{}}`
        —— 我们保留 201 + `data` 里回完整对象（前端少一次往返，且**不会**看到 AstrBot
        那种"data 是空对象"的迷惑响应）。这是**有意的偏差**，见报告。
        """
        denied = require_auth()
        if denied is not None:
            return denied
        body = await _body()
        try:
            source = ProviderSource.from_dict(body)
            source.validate()
            created = _store().create_source(source)
        except ProviderError as exc:
            return _store_error(exc)
        return _ok(created.public_view(), "创建 provider source 成功", 201)

    @bp.route("/provider-sources/by-id", methods=["GET", "PUT", "DELETE"])
    async def provider_source_by_id():  # type: ignore[no-untyped-def]
        """单条源（**query 参数 `source_id`**，照真实实现）。

        ⚠️ 参数名必须是 `source_id`：实测真实站用 `id=` 会回 422
           （`Missing key: source_id`）—— 名字写错这个接口就等于不存在。

        PUT 是**部分更新**（照真实实现实测行为：连续两次改 `timeout` 都成功，
        未给的字段保持原值）。`key` 为空 = **不修改**。
        """
        denied = require_auth()
        if denied is not None:
            return denied

        source_id = str(request.args.get("source_id") or "").strip()
        if not source_id:
            return _fail(
                "缺少 query 参数 source_id（例：/api/v1/provider-sources/by-id?source_id=mxgpt）",
                400,
                "missing_source_id",
            )

        store = _store()
        method = request.method.upper()

        if method == "GET":
            try:
                source = store.require_source(source_id)
            except ProviderError as exc:
                return _store_error(exc)
            return _ok({"provider_source": source.public_view()})

        if method == "PUT":
            body = await _body()
            try:
                updated = store.update_source(source_id, body)
            except ProviderError as exc:
                return _store_error(exc)
            # 🔴 改源 = 改连接信息 → 该源下所有模型都要重注册（宿主的 Provider 是不可变快照）
            _sync_models_of_source(source_id)
            return _ok({"provider_source": updated.public_view()}, "更新 provider source 成功")

        # DELETE
        try:
            gone, doomed, new_default = store.delete_source(source_id)
        except ProviderError as exc:
            return _store_error(exc)
        # 🔴 级联删掉的模型也要从宿主里摘掉，否则插件还能选到一个"源都没了"的模型
        for mid in doomed:
            _sync_unregister(mid)
        if new_default:
            _sync_set_default(new_default)
        return _ok(
            {
                "id": gone.id,
                "deleted_models": doomed,
                "default_provider_id": new_default,
            },
            "删除 provider source 成功（同时删除了它下面的模型）",
        )

    @bp.get("/provider-sources/models")
    async def list_all_source_models():  # type: ignore[no-untyped-def]
        """全部源的模型（按源分组）。契约 §2 的 `GET /provider-sources/models`。

        ⚠️ 真实站上这条路由是 **422**（它多半要求 query 参数），
           我们按"全部源的模型"实现并**在报告里说明** —— 它对面板的
           "已配置的模型一览"有用，且不与任何其它路由冲突（实测静态段优先）。
        """
        denied = require_auth()
        if denied is not None:
            return denied
        return _ok(_store().snapshot_models_by_source())

    @bp.get("/provider-sources/<source_id>/providers")
    async def list_models_of_source(source_id: str):  # type: ignore[no-untyped-def]
        """某源下**已配置**的模型（契约 §2）。源不存在 → 404（不是空列表）。"""
        denied = require_auth()
        if denied is not None:
            return denied
        store = _store()
        try:
            store.require_source(source_id)
        except ProviderError as exc:
            return _store_error(exc)
        default_id = store.default_provider_id
        models = store.list_models(source_id)
        return _ok(
            {
                "providers": [
                    {**m.public_view(), "is_default": m.id == default_id} for m in models
                ]
            }
        )

    @bp.post("/provider-sources/<source_id>/providers")
    async def create_model_under_source(source_id: str):  # type: ignore[no-untyped-def]
        """给某源加模型（契约 §2）。

        ⚠️ 路径里的源 id 是**权威**：body 里的 `provider_source_id` 与之不符 → 400。
           实测真实实现会**静默改写成路径里的源**（body 写 `deepseek`、挂在
           `__dsh_probe_put__` 下，结果 `provider_source_id` 变成了路径那个）——
           我们不抄这个静默改写：用户以为自己挂到了 A 源、实际挂到 B 源，
           正是"配置看着对、行为不对"的那类坑。
        """
        denied = require_auth()
        if denied is not None:
            return denied
        store = _store()
        try:
            store.require_source(source_id)
        except ProviderError as exc:
            return _store_error(exc)

        body = await _body()
        given = str(body.get("provider_source_id") or "").strip()
        if given and given != source_id:
            return _fail(
                f"provider_source_id（{given}）与路径里的源（{source_id}）不一致；"
                "模型必须挂在路径指定的源下面",
                400,
                "source_mismatch",
            )
        payload = dict(body)
        payload["provider_source_id"] = source_id
        # 模型 id 缺省时按契约补成 `源id/模型名`（面板"自定义模型"最常用这条）
        if not str(payload.get("id") or "").strip():
            model_name = str(payload.get("model") or "").strip()
            if not model_name:
                return _fail(
                    "模型必须给 model（模型名）；id 可省，会自动补成 `<源id>/<模型名>`",
                    400,
                    "invalid_provider",
                )
            payload["id"] = f"{source_id}/{model_name}"

        try:
            model = ProviderModel.from_dict(payload)
            model.validate_against_sources({s.id for s in store.list_sources()})
            created = store.create_model(model)
        except ProviderError as exc:
            return _store_error(exc)

        is_default = created.id == store.default_provider_id
        _sync_model(created, make_default=is_default)
        return _ok(
            {**created.public_view(), "is_default": is_default},
            "新增服务提供商配置成功",
            201,
        )

    @bp.route("/provider-sources/<source_id>/models", methods=["GET", "POST"])
    async def fetch_upstream_models(source_id: str):  # type: ignore[no-untyped-def]
        """★「**获取模型列表**」：拿该源的 `api_base` + `key` 去打上游 `GET /models`。

        契约 §2 的响应：
        ```json
        {"status":"ok","data":{"models":["gpt-5.5", ...],
          "provider_source_id":"mxgpt","model_metadata":{...}}}
        ```

        ⚠️ 方法上**同时接受 GET 和 POST**（GET 是契约规定的形态）：
           因为"用表单里还没保存的值"这条只能用 body 传（GET 塞 query 也能过，
           但 `api_base` 里全是 `/` 和 `:`，塞 query 又长又要转义，很难看）。
           两者共用同一段实现，语义完全一致 —— 不是两条会漂移的路径。

        🔴 **为什么必须服务端代拉**（照 `api_admin.list_upstream_models` 的同款理由）：
          1. **key 不能下发浏览器**（面板里 key 框是只写的，前端直连就得把 key 塞进页面）；
          2. 能带上**表单里还没保存**的 `api_base` / `key`（用户常是"改地址 → 拉模型 →
             选模型 → 保存"，只读已保存值的话第一次填地址时这个按钮就是废的）。

        🔴 **失败逐条说清**（超时/连不上/401/非 JSON/空列表），
           **空列表不算成功**（那是静默失败：用户会以为"这个站没有模型"）。
        """
        denied = require_auth()
        if denied is not None:
            return denied

        store = _store()
        try:
            source = store.require_source(source_id)
        except ProviderError as exc:
            return _store_error(exc)

        from emotion_app.web.provider_bridge import _first_key

        # 表单里没保存的值优先（用户"改地址 → 拉模型 → 再保存"的常见顺序）。
        # ⚠️ GET 也能从 query 取 —— 让两种方法语义完全一致（前端用哪种都行）。
        body = await _body()
        form_base = str(
            body.get("api_base") or request.args.get("api_base") or ""
        ).strip()
        form_key = str(
            body.get("key") or body.get("api_key") or request.args.get("key") or ""
        ).strip()
        api_base = form_base or source.api_base
        api_key = form_key or _first_key(source.key)
        if not api_base.startswith(("http://", "https://")):
            return _fail(
                f"api_base 必须以 http:// 或 https:// 开头；当前 {api_base!r}",
                400,
                "invalid_api_base",
            )

        url = api_base.rstrip("/") + "/models"
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        # 自定义请求头照契约透传（面板"自定义请求头"那一块）
        headers.update(source.custom_headers or {})

        import httpx

        # 🔴 transport 可注入（测试用 `httpx.MockTransport` 打桩）——
        #    沿用 `api_admin.list_upstream_models` 的做法；不打桩就没法验
        #    "401 怎么说、超时怎么说、空列表怎么说"，而失败路径最容易写成静默。
        transport = current_app.config.get("EMOTION_PROVIDER_MODELS_TRANSPORT")

        try:
            async with httpx.AsyncClient(
                timeout=MODELS_TIMEOUT_SECONDS, transport=transport
            ) as client:
                resp = await client.get(url, headers=headers)
        except httpx.TimeoutException:
            log.warning("获取模型列表超时", extra={"data": {"url": url, "id": source_id}})
            return _fail(
                f"连接上游超时（{MODELS_TIMEOUT_SECONDS:.0f}s）：{url}"
                " —— 检查地址是否可达/是否需要代理",
                502,
                "upstream_timeout",
            )
        except httpx.HTTPError as exc:
            log.warning(
                "获取模型列表失败：%s", exc, extra={"data": {"url": url, "id": source_id}}
            )
            return _fail(
                f"连不上上游：{type(exc).__name__}: {exc}", 502, "upstream_unreachable"
            )

        if resp.status_code in (401, 403):
            return _fail(
                f"上游拒绝鉴权（HTTP {resp.status_code}）——"
                "检查 API Key 是否正确/是否有权列出模型",
                502,
                "upstream_auth",
            )
        if resp.status_code >= 400:
            # 🔴 先过 scrub_text：中转站常把收到的 Authorization 头原样回显在错误体里
            detail = scrub_text((resp.text or "")[:200]).replace("\n", " ")
            return _fail(
                f"上游返回 HTTP {resp.status_code}：{detail}", 502, "upstream_error"
            )

        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            return _fail(
                "上游返回的不是 JSON —— 确认地址填的是 OpenAI 兼容端点（通常以 /v1 结尾）",
                502,
                "upstream_bad_payload",
            )

        raw = payload.get("data") if isinstance(payload, dict) else None
        models: list[str] = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("id"):
                    models.append(str(item["id"]))
                elif isinstance(item, str):
                    models.append(item)
        elif isinstance(payload, dict) and isinstance(payload.get("models"), list):
            # 少数中转站用 `models: [...]`
            for item in payload["models"]:
                if isinstance(item, dict) and item.get("id"):
                    models.append(str(item["id"]))
                elif isinstance(item, str):
                    models.append(item)

        # 去重且保序（中转站常返回重复项）
        seen: set[str] = set()
        ordered = [m for m in models if not (m in seen or seen.add(m))]

        if not ordered:
            # 🔴 **空列表不算成功**（本项目铁律：静默失败比报错贵）
            log.warning(
                "上游返回 200 但没有模型列表", extra={"data": {"url": url, "id": source_id}}
            )
            return _fail(
                "上游返回了 200，但里面没有模型列表（data 为空）—— 不算获取成功。"
                "检查该地址是否为 OpenAI 兼容端点、以及该 key 是否有列出模型的权限",
                502,
                "upstream_empty",
            )

        log.info(
            "已获取上游模型列表", extra={"data": {"url": url, "count": len(ordered)}}
        )
        return _ok(
            {
                "models": ordered,
                "provider_source_id": source_id,
                # ⚠️ 真实契约里有这块（每个模型的能力元信息）。我们**不做缓存、
                #    也不编造**：空对象 = "没有元信息"，不是"这个模型没能力"。
                "model_metadata": {},
            }
        )

    # ==================================================================
    # 模型层：/api/v1/providers
    # ==================================================================

    @bp.route("/providers", methods=["GET", "POST"])
    async def providers_collection():  # type: ignore[no-untyped-def]
        """GET = 模型列表（**脱敏**）；POST = 新增模型。

        GET 响应（契约 §3 冻结，**兼容期**照契约 §7 同时给 `provider_sources`）：
        `{"status":"ok","data":{"providers":[...],"provider_sources":[...],
          "model_metadata":{},"default_provider_id":"源id/模型名"}}`

        ⚠️ 旧前端读的是顶层的 `d.providers` / `d.default_provider_id`；
           新形状把它们放在 `data` 里 → **旧前端需要跟着改**（B 负责）。
           我们不"顺手"在顶层再复制一份：同一个数据两处出口必然漂移。
        🔴 **绝不回显 key**。
        """
        denied = require_auth()
        if denied is not None:
            return denied

        if request.method == "GET":
            return _ok(_store().snapshot())

        body = await _body()
        try:
            model = ProviderModel.from_dict(body)
            store = _store()
            model.validate_against_sources({s.id for s in store.list_sources()})
            created = store.create_model(model)
        except ProviderError as exc:
            return _store_error(exc)

        is_default = created.id == _store().default_provider_id
        _sync_model(created, make_default=is_default)
        return _ok({**created.public_view(), "is_default": is_default}, "新增模型成功", 201)

    @bp.route("/providers/by-id", methods=["GET", "PUT", "DELETE"])
    async def provider_by_id():  # type: ignore[no-untyped-def]
        """单条模型（query 参数 **`provider_id`**，照真实实现）。

        ⚠️ 真实站上 id **必须 URL 编码**（含 `/`）：`?provider_id=deepseek%2Fdeepseek-flash`。
        """
        denied = require_auth()
        if denied is not None:
            return denied

        provider_id = str(request.args.get("provider_id") or "").strip()
        if not provider_id:
            return _fail(
                "缺少 query 参数 provider_id"
                "（例：/api/v1/providers/by-id?provider_id=mxgpt%2Fgpt-5.5）",
                400,
                "missing_provider_id",
            )

        store = _store()
        method = request.method.upper()

        if method == "GET":
            try:
                model = store.require_model(provider_id)
            except ProviderError as exc:
                return _store_error(exc)
            return _ok(
                {
                    "provider": {
                        **model.public_view(),
                        "is_default": model.id == store.default_provider_id,
                    },
                    "model_metadata": {},
                }
            )

        if method == "PUT":
            return await _update_model(store, provider_id)

        try:
            gone, new_default = store.delete_model(provider_id)
        except ProviderError as exc:
            return _store_error(exc)
        _sync_unregister(provider_id)
        if new_default:
            _sync_set_default(new_default)
        return _ok(
            {"id": gone.id, "default_provider_id": new_default}, "删除成功，已经实时生效。"
        )

    async def _update_model(store: ProviderStore, provider_id: str) -> tuple[Any, int]:
        """PUT / PATCH 一条模型的公共实现（两条路由共用一套语义，避免规则漂移）。"""
        body = await _body()
        try:
            updated = store.update_model(provider_id, body)
        except ProviderError as exc:
            return _store_error(exc)

        # 🔴 改了 `model` 名或换了源 → **id 本身会变**（`id = 源id/模型名` 是不变量）。
        #    此时宿主的 `inst_map` 里还留着**旧 id 那条**（僵尸条目）：
        #    `register` 只按新 id 写入，没人删旧的 → 插件仍能选到一个"配置里已经没有了"
        #    的模型，而且它带着旧模型名。实测抓到的就是这条
        #    （`inst_map == ['syuan/m-old', 'syuan/m-new']`）。
        if updated.id != provider_id:
            _sync_unregister(provider_id)
            log.info("模型 id 因改名/换源而变化：%s → %s", provider_id, updated.id)

        is_default = updated.id == store.default_provider_id
        # 🔴 改完**必须重新 register** —— 宿主的 `Provider` 是不可变的快照对象，
        #    `inst_map` 里那个旧实例**不会**跟着变（只改文件 = 插件继续用旧模型）。
        pm = _sync_model(updated, make_default=is_default)
        if pm is not None and is_default:
            # 覆盖注册**不会**改默认（`register` 只在 make_default 或"当前无默认"时才设），
            # 所以显式再确认一次，避免"文件说是默认、宿主说不是"的分叉。
            _sync_set_default(updated.id)
        return _ok(
            {**updated.public_view(), "is_default": is_default}, "更新成功，已经实时生效~"
        )

    # ------------------------------------------------------------------
    # ⚠️ 路由设计的**硬约束**（实测踩出来的，别"顺手优化"掉）
    #
    # 模型 id 一定含 `/`，所以同一个"单条模型"必须支持**四种** URL 形态：
    #   ① ?by-id&provider_id=<URL 编码>      —— 对编码不敏感，最可靠（真实站的形态）
    #   ② /providers/<URL 编码的 id>        —— 契约 §3
    #   ③ /providers/<id 的原始形态>        —— 未编码的斜杠（人手工敲的）
    #   ④ /providers/<id>/default、/<id>/test —— 与 ③ 前缀相同、后缀不同
    #
    # ③④ 与 ② 在**路由层**无法共存：Werkzeug 按"规则书写顺序"匹配，
    # `/providers/<provider_id>` 会先吃掉 `/providers/a/b`，于是
    # `/providers/a/b/default` 落到它身上 → **405**（实测：`POST
    # /api/v1/providers/syuan%2Fgpt-5.5/default` 回 405）。
    #
    # ✅ 解法：用**一条** `<path:provider_id>` 规则接住全部 ②③④，
    #    在**函数内部**按后缀分派。判据 `test_*_via_encoded_and_plain_path`
    #    覆盖了每一种形态（改回多条规则必挂）。
    # ------------------------------------------------------------------

    @bp.route("/providers/<path:provider_id>", methods=["GET", "PUT", "DELETE", "PATCH", "POST"])
    async def provider_by_path(provider_id: str):  # type: ignore[no-untyped-def]
        """单条模型（②③④ 三种形态），按**后缀**分派到"改/删/设默认/测试"。"""
        denied = require_auth()
        if denied is not None:
            return denied

        method = request.method.upper()
        suffix = ""

        # 后缀分派：只有 GET/PUT/DELETE 是"操作模型本身"，
        # POST .../default 与 POST .../test 是子资源（它们要求 POST）。
        if method == "POST":
            if provider_id.endswith("/default"):
                provider_id, suffix = provider_id[: -len("/default")], "default"
            elif provider_id.endswith("/test"):
                provider_id, suffix = provider_id[: -len("/test")], "test"
            else:
                # POST 到"模型本身"没有意义（新增走 /providers）
                return _fail(
                    f"该路径不支持 POST：/api/v1/providers/{provider_id}；"
                    "新增模型请 POST /api/v1/providers",
                    405,
                    "method_not_allowed",
                )

        if not provider_id:
            return _fail("模型 id 为空", 400, "missing_provider_id")

        if suffix == "test":
            return await _run_test(provider_id)
        if suffix == "default":
            return await _set_default(provider_id)

        store = _store()
        hint = _method_hint(store, provider_id, method)
        if hint is not None:
            return hint
        if method == "GET":
            return _get_model(store, provider_id)
        if method in ("PUT", "PATCH"):
            return await _update_model(store, provider_id)
        return _delete_model(store, provider_id)

    def _method_hint(store: ProviderStore, provider_id: str, method: str) -> tuple[Any, int] | None:
        """`GET /providers/<id>/test` 这类**方法用错**的请求 → 405，并说清该用什么方法。

        ⚠️ 为什么需要它（路由的固有歧义）：`<path:provider_id>` 让 `/providers/x/test`
           既能读成"模型 `x/test` 的 GET"，也能读成"模型 `x` 的 test 子资源"。
           前者合法（模型名里允许 `/`），所以不能一律 405。
           **判据**：当"砍掉 `/test`·`/default` 后缀后那个模型**真的存在**"时，
           用户的意图几乎肯定是子资源 → 405 并点明"这是 POST"。
           （前端把测试写成 GET 时，能立刻看到一条能照着改的消息，而不是莫名的 404。）
        """
        if method not in ("GET", "PUT", "DELETE", "PATCH"):
            return None
        for suffix, need in (("/test", "POST"), ("/default", "POST")):
            if provider_id.endswith(suffix):
                head = provider_id[: -len(suffix)]
                if head and store.get_model(head) is not None:
                    return _fail(
                        f"{suffix} 子资源要用 {need}：{need} /api/v1/providers/"
                        f"{head}{suffix}（当前用了 {method}）",
                        405,
                        "method_not_allowed",
                    )
        return None

    def _get_model(store: ProviderStore, provider_id: str) -> tuple[Any, int]:
        """读一条模型（契约 §3 的 `data.provider` + `model_metadata` 形状）。"""
        try:
            model = store.require_model(provider_id)
        except ProviderError as exc:
            return _store_error(exc)
        return _ok(
            {
                "provider": {
                    **model.public_view(),
                    "is_default": model.id == store.default_provider_id,
                },
                "model_metadata": {},
            }
        )

    def _delete_model(store: ProviderStore, provider_id: str) -> tuple[Any, int]:
        """删一条模型（GET/PUT/DELETE 三条路共用，避免各自漏同步宿主）。"""
        try:
            gone, new_default = store.delete_model(provider_id)
        except ProviderError as exc:
            return _store_error(exc)
        _sync_unregister(provider_id)
        if new_default:
            _sync_set_default(new_default)
        return _ok(
            {"id": gone.id, "default_provider_id": new_default}, "删除成功，已经实时生效。"
        )

    async def _set_default(provider_id: str) -> tuple[Any, int]:
        """设为默认。

        契约 §3 的 `PATCH /providers/enabled` 我们**不实现**（它靠路径里没有 id
        来批量改 enable，真实站上语义含混）——
        要启停某个模型就用 `PUT /providers/by-id` 传 `{"enable": false}`，语义明确。
        """
        store = _store()
        try:
            target = store.set_default(provider_id)
        except ProviderError as exc:
            return _store_error(exc)

        # 文件已改。宿主同步前先确保这条**真的注册过** ——
        # 否则 `set_default` 会失败（宿主里没有它），插件的 get_using_provider 就空转。
        _sync_model(target, make_default=True)
        _sync_set_default(provider_id)
        return _ok(
            {"ok": True, "default_provider_id": store.default_provider_id},
            "已设为默认模型",
        )

    # ==================================================================
    # 连通性测试
    # ==================================================================

    async def _run_test(provider_id: str) -> tuple[Any, int]:
        """打一次**真的** `chat/completions`，如实报告结果。

        契约 §3 响应（冻结）：
        ```json
        {"status":"ok","data":{"id":"mxgpt/gpt-5.6-sol","model":"gpt-5.6-sol",
          "type":"chat_completion","name":"mxgpt/gpt-5.6-sol",
          "status":"available","error":null}}
        ```
        ⚠️ **业务失败仍是 HTTP 200 + `status:"ok"`**：`data.status` 才是结论
           （`unavailable`），`data.error` 说清是哪一类失败。这与真实契约一致
           （"测试"是查询，不是修改；把"模型不通"表达成 HTTP 错误会让面板
           把"配置对了但上游不通"和"接口本身坏了"混在一起）。
        404 只在**这个模型不存在**时给（那是真的"你要的东西不在"）。

        🔴 失败原因逐条：`upstream_timeout` / `upstream_unreachable` / `upstream_auth` /
           `upstream_error` / `upstream_bad_payload` / `upstream_empty`。
           **空响应不算成功** —— 上游回 200 但内容是空，插件拿到的就是空回复。
        """
        store = _store()
        try:
            model = store.require_model(provider_id)
        except ProviderError as exc:
            return _store_error(exc)

        try:
            source = store.require_source(model.provider_source_id)
        except ProviderError as exc:
            # 悬空引用：读的时候就该拦住，这是兜底（要点名，不然没法查）
            log.error(
                "模型 %s 指向的源 %s 不存在（悬空引用）", model.id, model.provider_source_id
            )
            return _fail(
                f"模型 {model.id} 指向的 provider source "
                f"{model.provider_source_id} 不存在（悬空引用）——请先修好这个源",
                400,
                "dangling_provider_source",
            )

        body = await _body()
        # 允许表单里**还没保存**的 model（用户常是"改模型 → 想测一下 → 再保存"）
        target_model = str(body.get("model") or "").strip() or model.model
        if not target_model:
            return _fail(
                "没有模型名可测：该供应商的 model 为空，且请求里也没给", 400, "invalid_provider"
            )

        def _result(**kw: Any) -> tuple[Any, int]:
            base = {
                "id": model.id,
                "model": target_model,
                "type": PROVIDER_TYPE_CHAT,
                "name": model.id,
                "status": "unavailable",
                "error": None,
                "latency_ms": None,
                "sample": None,
            }
            base.update(kw)
            return _ok(base)

        url = source.api_base.rstrip("/") + "/chat/completions"
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        from emotion_app.web.provider_bridge import _first_key

        api_key = _first_key(source.key)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        headers.update(source.custom_headers or {})

        # ⚠️ 刻意**不传** `max_tokens` 之类的可选参数：连通性测试的请求越朴素，
        #    被上游以"不认这个参数"拒绝的概率越低（假失败比多花几毛钱糟得多）。
        payload = {
            "model": target_model,
            "messages": [{"role": "user", "content": TEST_PROMPT}],
            "stream": False,
        }

        import httpx

        transport = current_app.config.get("EMOTION_PROVIDERS_TRANSPORT")

        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=TEST_TIMEOUT_SECONDS, transport=transport
            ) as client:
                resp = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException:
            log.warning("供应商测试超时", extra={"data": {"id": model.id, "url": url}})
            return _result(
                error=f"连接上游超时（{TEST_TIMEOUT_SECONDS:.0f}s）：{url}"
                " —— 检查地址是否可达/是否需要代理"
            )
        except httpx.HTTPError as exc:
            log.warning(
                "供应商测试连不上：%s", exc, extra={"data": {"id": model.id, "url": url}}
            )
            return _result(error=f"连不上上游：{type(exc).__name__}: {exc}")

        latency_ms = int((time.monotonic() - started) * 1000)

        if resp.status_code in (401, 403):
            return _result(
                error=f"上游拒绝鉴权（HTTP {resp.status_code}）——"
                "检查 API Key 是否正确/是否有权调用该模型"
            )
        if resp.status_code >= 400:
            # 把上游的原话带一小段回来（最常见的 400 是"模型名不存在"，用户要看得到）。
            #
            # 🔴 **必须先过 scrub_text**：上游（尤其 OpenClaw 系中转站）常把**收到的
            #    Authorization 头原样回显**在错误体里。直接转发 = 我们把用户的 key
            #    通过我们自己的接口又下发给了浏览器一次（恰好是契约最在意的那件事）。
            detail = scrub_text((resp.text or "")[:200]).replace("\n", " ")
            log.warning(
                "供应商测试上游报错",
                extra={"data": {"id": model.id, "status": resp.status_code}},
            )
            return _result(error=f"上游返回 HTTP {resp.status_code}：{detail}")

        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return _result(
                error="上游返回的不是 JSON —— 确认地址填的是 OpenAI 兼容端点"
                "（通常以 /v1 结尾）"
            )

        choices = data.get("choices") if isinstance(data, dict) else None
        first = choices[0] if isinstance(choices, list) and choices else None
        message_obj = first.get("message") if isinstance(first, dict) else None
        content = ""
        if isinstance(message_obj, dict):
            content = str(message_obj.get("content") or "")

        if not content.strip():
            # 🔴 **空响应不算成功**（本项目铁律：静默失败比报错贵）。
            log.warning(
                "供应商测试通过但回复为空", extra={"data": {"id": model.id, "url": url}}
            )
            return _result(
                error="上游返回 200，但回复内容为空 —— 不算连通成功"
                "（插件会拿到空回复）。检查模型名/额度/该模型是否可用"
            )

        log.info(
            "供应商测试通过",
            extra={"data": {"id": model.id, "model": target_model, "latency_ms": latency_ms}},
        )
        return _result(
            status="available",
            error=None,
            latency_ms=latency_ms,
            sample=content.strip()[:SAMPLE_CHARS],
        )

    return bp


__all__ = [
    "MODELS_TIMEOUT_SECONDS",
    "SAMPLE_CHARS",
    "TEST_PROMPT",
    "TEST_TIMEOUT_SECONDS",
    "build_blueprint",
]
