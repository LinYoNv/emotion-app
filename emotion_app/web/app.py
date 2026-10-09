# -*- coding: utf-8 -*-
"""Quart 应用：只做协议转换，不写业务规则（见 docs/BACKEND_FRAMEWORK.md §2.2）。

路由（S0 阶段）：
  GET  /health            —— 存活与配置自检（**不需要鉴权**，便于监控）
  GET  /api/v1/status     —— 运行状态（**需要鉴权**，脱敏）
  POST /api/v1/auth/login —— 令牌换会话
  POST /api/v1/auth/logout
"""
from __future__ import annotations

from typing import Any

import json as _json
import time
import uuid
from collections.abc import AsyncIterator

from pathlib import Path

from quart import Quart, Response, jsonify, request, send_from_directory

from ..domain.chat import ChatError, ChatService, parse_chat_request
from ..domain.upstream import UpstreamError
from ..infra.config import Config
from ..infra.logging_setup import get_logger
from ..storage.base import StoreBackend
from .api_admin import build_blueprint
from .auth import AuthManager, parse_bearer

log = get_logger(__name__)

SESSION_COOKIE = "emotion_session"


def create_app(
    config: Config,
    store: StoreBackend,
    auth: AuthManager,
    chat: ChatService | None = None,
    personas: Any = None,
    settings: Any = None,
    sessions: Any = None,
    chat_backend: Any = None,
) -> Quart:
    """组装应用。依赖**显式注入**（不做全局单例，便于测试）。"""
    # 🔴 `static_folder=None` **必须**（实测踩到）：
    #    `Quart(__name__)` 会**自动注册**一条 `/static/<path:filename>`，
    #    而它**优先级高于**我们后面自己加的那条同路径路由 → 我们的
    #    `Cache-Control: no-cache` 被静默忽略，静态资源继续按 12 小时缓存
    #    （正是"改完前端、手机上看不到"的元凶）。传 `None` 关掉内置的，
    #    由我们自己的 `/static/<path:filename>` 提供（它带 no-cache 头）。
    app = Quart(__name__, static_folder=None)
    app.config["EMOTION_CONFIG"] = config
    app.config["EMOTION_STORE"] = store
    app.config["EMOTION_AUTH"] = auth
    app.config["EMOTION_CHAT"] = chat
    app.config["EMOTION_PERSONAS"] = personas
    app.config["EMOTION_SETTINGS"] = settings
    app.config["EMOTION_SESSIONS"] = sessions
    #: 🔴 领域层的**上游客户端**（`ChatBackend`）。插件供应商要打到**同一个上游**，
    #: 所以把它挂上来，由 `plugin_host` 装配供应商时取用。
    #: ⚠️ 没传时为 None → 供应商照建但调不通（会**如实抛错**，不是静默空回复）。
    app.config["EMOTION_CHAT_BACKEND"] = chat_backend

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _session_token() -> str | None:
        """从 cookie 或 Bearer 头取会话令牌。"""
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            return token
        return parse_bearer(request.headers.get("Authorization"))

    def _require_auth() -> tuple[Any, int] | None:
        """未通过鉴权时返回 (响应, 状态码)；通过返回 None。"""
        if not auth.verify_session(_session_token()):
            return jsonify({"error": {"message": "未授权", "type": "unauthorized"}}), 401
        return None

    def _authenticated() -> bool:
        """统一的鉴权判定：**会话 cookie 或访问令牌本身**。

        🔴 为什么统一（实测踩到）：
          最初 `/v1/*` 认 Bearer、`/api/v1/*` 只认 cookie，
          结果**用令牌直接调管理接口会 401** —— 而 App 端配置恰恰需要这个用法。
          现在两条路径都接受"访问令牌当 Bearer"，行为一致。

        ⚠️ 安全取舍：**不再接受任意 Bearer**（旧实现把任何 Bearer 都放过，
           等于匿名可用）。现在 Bearer 必须**就是我们的访问令牌**。
           自用场景下把令牌填进 OpenAI 客户端即可（README 已这么写）。
        """
        if auth.verify_session(_session_token()):
            return True
        return auth.is_access_token(parse_bearer(request.headers.get("Authorization")))

    def _require_auth_or_api_key() -> tuple[Any, int] | None:
        """`/v1/*` 与 `/api/v1/*` 共用的鉴权闸门。"""
        if _authenticated():
            return None
        return (
            jsonify(
                {
                    "error": {
                        "message": "未授权：请提供会话，或用访问令牌作为 Bearer",
                        "type": "unauthorized",
                        "code": "",
                    }
                }
            ),
            401,
        )

    # ------------------------------------------------------------------
    # 健康检查（免鉴权）
    # ------------------------------------------------------------------

    @app.get("/health")
    async def health():  # type: ignore[no-untyped-def]
        """存活 + 配置自检。

        ⚠️ **脱敏**：只回显"是否配置了"，绝不回显密钥本身。
        """
        store_health = await store.health_check()
        ok = bool(store_health.get("ok"))
        return (
            jsonify(
                {
                    "status": "ok" if ok else "degraded",
                    "service": "emotion-app",
                    "storage": store_health,
                    "config": config.public_view(),
                    "auth": auth.stats(),
                    "chat_ready": _chat_or_none() is not None,
                }
            ),
            200 if ok else 503,
        )

    # ------------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------------

    @app.post("/api/v1/auth/login")
    async def login():  # type: ignore[no-untyped-def]
        body = await request.get_json(silent=True) or {}
        provided = body.get("token") or parse_bearer(request.headers.get("Authorization"))

        # ⚠️ 来源标识仅用于**失败限流计数**（绑 0.0.0.0 后登录口对全网开放）
        client_key = request.remote_addr or "-"
        if auth.is_rate_limited(client_key):
            return (
                jsonify(
                    {
                        "error": {
                            "message": "失败次数过多，请稍后再试",
                            "type": "rate_limited",
                        }
                    }
                ),
                429,
            )

        session = await auth.login(provided or "", client_key)
        if session is None:
            return jsonify({"error": {"message": "令牌无效", "type": "invalid_token"}}), 401

        resp = jsonify(
            {
                "ok": True,
                "expires_at": session.expires_at,
                "ttl_hours": auth.ttl_hours,
            }
        )
        resp.set_cookie(
            SESSION_COOKIE,
            session.token,
            httponly=True,          # JS 读不到
            samesite="Lax",         # 基本 CSRF 防护
            # ⚠️ 本机 http 必须 False，否则浏览器直接不收 cookie。
            #    上了 TLS 反代后应把 EMOTION_SERVER_HTTPS_ONLY 打开（见 config）。
            secure=config.server.https_only,
            max_age=auth.ttl_hours * 3600,
        )
        return resp, 200

    @app.post("/api/v1/auth/logout")
    async def logout():  # type: ignore[no-untyped-def]
        auth.logout(_session_token())
        resp = jsonify({"ok": True})
        resp.delete_cookie(SESSION_COOKIE)
        return resp, 200

    # ------------------------------------------------------------------
    # 状态（需鉴权）
    # ------------------------------------------------------------------

    @app.get("/api/v1/status")
    async def status():  # type: ignore[no-untyped-def]
        denied = _require_auth()
        if denied is not None:
            return denied

        return (
            jsonify(
                {
                    "service": "emotion-app",
                    "config": config.public_view(),
                    "storage": await store.health_check(),
                    "auth": auth.stats(),
                }
            ),
            200,
        )

    # ------------------------------------------------------------------
    # OpenAI 兼容接口（S1）
    # ------------------------------------------------------------------

    def _chat_or_none() -> ChatService | None:
        return app.config.get("EMOTION_CHAT")

    def _effective_model(req_model: str = "") -> str:
        """🔴 **响应里该回显哪个模型名** —— 全局**唯一**的解析处（2026-09-27 修）。

        ## 修的是什么（情绪报障）
        「我切了对话模型怎么聊天那还是用着 `gemini-3.1-flash-lite`」

        实测：**实际请求上游用的确实是他切的新模型**（`chat.py:322` 的
        `resolve_target` 优先级是对的：人设覆盖 > 请求指定 > **运行时设置** > 环境变量），
        **但响应体里回显的是旧名字** —— 因为这里原来写

            "model": req.model or config.upstream.model

        而 **`config` 是服务启动那一刻的快照**（来自 `.env`）：
        * `.env` 里 `EMOTION_UPSTREAM_MODEL=gemini-3.1-flash-lite`（启动值）
        * 用户在面板切成 `gemini-3.8-flash-high` → 写进**运行时设置**（`EMOTION_SETTINGS`）
        ⇒ `config.upstream.model` **永远**是旧值，面板里那个模型名标签就是假的。

        🔴 **为什么这是最坏的那类 bug**：模型标签**唯一**的用途就是告诉用户
        "这条回复是谁生成的"。它说错话时，用户会去改一个**本来就对**的地方
        （他就是这么来报障的），而真因在另一个文件里。

        ⚠️ **同一个概念原来有 3 条写入路径**（非流式 / 流式 envelope / 另一条流式分支）
        —— 本项目踩过 3 次的「同一概念多条写入路径」。
        所以抽成**这一个函数**，三处都调它。

        ⚠️ 优先级必须与 `chat.py:resolve_target` **一致**，否则又会出现
        "实际用 A、回显 B"。这里**直接复用它**（而不是再写一遍）——
        两处各写一份必然漂移，那正是这个 bug 的成因。

        ⚠️ `req_model`（调用方显式指定的 `model` 字段）位置保持**和原来一样**：
           调用方说了就用它的 —— 那是 OpenAI 客户端语义，不该被这次修复改掉。
        """
        if req_model:
            return req_model
        chat = _chat_or_none()
        if chat is not None:
            try:
                # 传空 persona / 空 override ⇒ 得到"全局默认"，与真正发请求时同源
                return chat.resolve_target(None).model
            except Exception:  # noqa: BLE001
                # 解析失败不能把响应搞挂 —— 但**必须留日志**（静默失败最贵）
                log.warning("解析生效模型名失败，回落到启动配置", exc_info=True)
        return config.upstream.model

    def _api_key_from_request() -> str | None:
        """从 Authorization 头取上游 key（OpenAI 客户端的用法）。

        ⚠️ 这里取的是**调用方给我们的** key。当前自用场景下：
           若调用方给了 key，就透传给上游（方便用现成客户端直连）；
           否则用服务端配置的 key。
        """
        return parse_bearer(request.headers.get("Authorization"))

    def _openai_error(message: str, etype: str, status: int) -> tuple[Response, int]:
        return jsonify({"error": {"message": message, "type": etype, "code": ""}}), status

    def _plugin_host_or_none() -> Any:
        """插件宿主服务（未启用/启动失败时为 None）。"""
        return app.config.get("EMOTION_PLUGIN_HOST")

    async def _session_user_id(svc: Any, session_id: str) -> str:
        """取该会话绑定的**插件用户 id**（方案 B）；取不到返回空串。

        🔴 **规则本体只有一份**：`plugin_host.session_user_id()`。
           这里只是把"从哪个对象拿会话仓库"接上（`svc._sessions`）——
           **绝不在本文件复刻一遍判定**，否则它与钩子那条路径会漂移，
           而漂移的表现正是 2026-09-26 那个真 bug：
           派发记到用户 A、注入按用户 B 算（两边各自看起来都正常）。

        ⚠️ 任何取不到的情况都返回空串（不抛）：
           无 session_id / 无会话仓库 / 会话不存在 / 会话没绑 →
           调用方回落全局 `plugins.umo_user_id`（**行为与以前一致**）。
        """
        from emotion_app.web.plugin_host import session_user_id

        return await session_user_id(getattr(svc, "_sessions", None), session_id)

    async def _plugin_first_reply_payload(
        text: str, *, svc: Any, req: Any
    ) -> tuple[dict[str, Any], Any] | None:
        """把用户这轮输入派发给插件；**有产出就返回 OpenAI 格式 payload**。

        返回 `None` = "插件没说话，请照常走 LLM"。

        🔴 **为什么返回 payload 而不是 `jsonify(...)` 的响应**（2026-09-26 实测踩到）：
           流式分支要在**上游没被调用之前**就决定"这轮交给谁"，而它是在
           `async def sse()` 生成器里跑的 —— **Quart 的请求/app 上下文在
           响应迭代开始后就不在了**（`RuntimeError: Not within an app context`）。
           所以 `jsonify()` 必须留在仍有上下文的**路由函数**里调用。
           （原来只有非流式一个调用方、且它在路由函数体里，所以看不出来。）

        Returns:
            `(payload, dispatch_result)`；插件没接管时 `(None, None)`。

        🔴 判定"插件有没有说话"的依据（缺一不可，否则会**吞掉正常聊天**）：
          1. `result.emitted` 非空 —— 插件产出了回复内容；
          2. 或者 `result.stopped` —— 插件**主动终止**了这条消息
             （如 `陪伴` 这种接管型指令：它自己会异步回，我们不该再调 LLM）。

        ⚠️ **为什么必须看 `stopped`**：实测 `陪伴` 指令的产出是**空字符串**
           （它走流式异步回，不走返回值），但 `stopped=True`。
           只看 `emitted` 的话，这类"接管型指令"会被判成"没说话" →
           又去调一次 LLM → **用户会收到两条互相矛盾的回复**。

        ⚠️ 反过来也重要：普通聊天（"你好呀"）派发后
           `emitted=[]` 且 `stopped=False` → 必须**返回 None 走 LLM**，
           否则聊天功能直接被吞掉（这是最严重的一类回归）。
        """
        if not text.strip():
            return None
        host = _plugin_host_or_none()
        if host is None:
            return None

        # 🔴 **本次派发该用哪个"插件用户 id"**（方案 B，情绪 2026-09-20 定）。
        #
        # 插件的 `data["users"]` 是**按 user_id 分**的（记忆/关系/主动消息开关
        # 全在里面），而 `user_id` 来自 `event.get_sender_id()` ——
        # 在真实现里就是 **umo 的第三段**（`build_umo(user_id)`）。
        #
        # 所以：**会话绑了 `user_id` 就用它**，没绑才回落全局配置。
        # 这样"三个会话、只有一个开主动消息"才是可能的 ——
        # 三个会话 = 插件里的三个 user，各自有独立的开关。
        #
        # ⚠️ 取不到（无会话 / 会话没绑）→ 传空串，由 `dispatch_message`
        #    回落到 `plugins.umo_user_id`（**向后兼容**，行为与以前一致）。
        uid = await _session_user_id(svc, getattr(req, "session_id", ""))

        try:
            res = await host.dispatch_message(text, user_id=uid)
        except Exception:  # noqa: BLE001
            # 插件出错**不该影响聊天** —— 但必须留日志（不许静默）
            log.error("插件派发异常（本条按无插件处理）", exc_info=True)
            return None

        if res is None:
            return None

        # 🔴 判定"插件有没有说话"**只在这一处实现**
        #    （`plugin_host.plugin_reply_text`）—— 集成层与单测共用同一份规则，
        #    避免"测试里复刻一份规则"导致的自证式假通过。
        from emotion_app.web.plugin_host import plugin_reply_non_text, plugin_reply_text

        content = plugin_reply_text(res)
        if content is None:
            # 插件没接管 → 交回 LLM（**最常见的正常路径**）
            return None

        rid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        payload: dict[str, Any] = {
            "id": rid,
            "object": "chat.completion",
            "created": int(time.time()),
            # 🔴 走**唯一**的解析处（原来直接读启动快照 `config` ⇒ 回显旧模型名）
            "model": _effective_model(req.model),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            # ⭐ 并列扩展字段：标明这条来自插件（标准客户端会忽略）
            "plugin_dispatch": {
                "executed": len(res.executed or []),
                "errors": [{"handler": n, "error": e} for n, e in (res.errors or [])],
                "stopped": bool(res.stopped),
                # 🔴 **插件产出里被降级掉的部分**（图/语音/文件…）。
                #
                # 为什么必须写进 payload：`plugin_reply_text()` 只返回**文本**，
                #   非文本段在流式分支里**没有对应通道**。若不如实标注，
                #   流式分支就只能**静默丢弃**插件发的图 —— 正是本项目最贵的
                #   那类 bug（参考图裸 base64 事故：无日志、无报错，
                #   用户只能凭感觉报障）。
                # ⚠️ 判据只此一份（`plugin_reply_non_text`），别处不许再判一遍。
                "non_text": plugin_reply_non_text(res),
            },
        }
        return payload, res

    async def _plugin_content_from_payload(
        payload: dict[str, Any]
    ) -> str | None:
        """从插件 payload 里取**那段文本**；取不到返回 `None`。

        🔴 **只取文本，别的字段如实忽略** —— 但**不静默**：
           非文本段（图/语音/文件）在流式分支里**没有对应通道**，
           所以这里打一条 `log.info` 说明"未透传附件"。
           本项目最贵的 bug 就是静默丢弃（参考图裸 base64 事故）。

        Returns:
            文本；拿不到时 `None`（调用方按"插件没接管"处理，回落 LLM）。
        """
        choices = payload.get("choices")
        text = ""
        if isinstance(choices, list) and choices:
            message = (choices[0] or {}).get("message") or {}
            text = str(message.get("content") or "")
        if not text.strip():
            return None

        # 🔴 **非文本内容在流式分支没有通道 —— 如实说出来，不许静默丢**
        non_text = (payload.get("plugin_dispatch") or {}).get("non_text") or []
        if non_text:
            kinds = "、".join(str(x.get("type", "?")) for x in non_text)
            log.info(
                "插件回复含非文本内容（%s）：流式分支未透传附件，本条只发了文本；"
                "需要附件请用非流式（stream=false）",
                kinds,
            )
        return text

    def _last_user_text(req: Any) -> str:
        """取本次请求里**最后一条**用户消息的纯文本。

        为什么取"最后一条用户消息"（而不是拼接全部）：
        插件的指令过滤器是按 `message_str` 做**前缀匹配**的
        （`CommandFilter.filter` → `message_str.startswith(cmd)`），
        拼接历史会让指令永远匹配不上。**只喂这一轮的用户输入**才是对的。
        """
        for m in reversed(list(getattr(req, "messages", []) or [])):
            role = getattr(m, "role", "") or (m.get("role") if isinstance(m, dict) else "")
            if role != "user":
                continue
            content = getattr(m, "content", None)
            if content is None and isinstance(m, dict):
                content = m.get("content")
            if isinstance(content, str):
                return content
            # 多模态分段：只取 text 段（图/文件暂不喂给插件指令）
            if isinstance(content, list):
                parts = [
                    str(p.get("text", ""))
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                if parts:
                    return "".join(parts)
        return ""

    @app.get("/v1/models")
    async def list_models():  # type: ignore[no-untyped-def]
        """模型列表。形状照 OpenAI（爱语对外也是这么做的）。"""
        denied = _require_auth_or_api_key()
        if denied is not None:
            return denied

        # 🔴 走**唯一**的解析处（这里原来自己写了一遍 `svc.resolve_target(...)` ——
        #    与响应回显那几处是**同一逻辑的两份实现**，正是它们漂移出了
        #    "实际用新模型、回显旧模型名" 那个 bug。现在共用一个函数。）
        model = _effective_model()

        return jsonify(
            {
                "object": "list",
                "data": [
                    {
                        "id": model,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "emotion-app",
                    }
                ],
            }
        ), 200

    @app.post("/v1/chat/completions")
    async def chat_completions():  # type: ignore[no-untyped-def]
        """聊天补全（OpenAI 兼容，支持 SSE 流式）。"""
        denied = _require_auth_or_api_key()
        if denied is not None:
            return denied

        svc = _chat_or_none()
        if svc is None:
            return _openai_error("聊天服务未配置", "server_error", 503)

        body = await request.get_json(silent=True)
        if not isinstance(body, dict):
            return _openai_error("请求体必须是 JSON 对象", "invalid_request_error", 400)

        try:
            req = parse_chat_request(body)
        except ChatError as exc:
            return _openai_error(str(exc), "invalid_request_error", 400)

        # 🔴 **人设解析优先级：显式 > 会话绑定 > 当前人设**（2026-09-20 修）。
        #
        # ⚠️ **必须把 `session_id` 传进去** —— 原来这里调 `resolve_persona()`
        #    不传会话 id，于是面板上给会话绑的人设**从来没被读过**
        #    （实测：会话绑「甲」+ 全局为空 → 解析出 `None`，绑了等于没绑）。
        #
        # ⚠️ 不设 `persona_explicit`：这里的 `persona` 是**解析出来的**，
        #    不代表"用户显式选了它"。设了它 `_prepare_session` 就会把
        #    全局人设**永久写进会话**（那正是这次修掉的次生 bug）。
        if req.persona is None:
            req.persona = await svc.resolve_persona(session_id=req.session_id)

        rid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        # ---- 非流式 ----
        if not req.stream:
            # ⭐⭐ **先给插件一次机会**（P0-2/P0-3 的接线处）：
            #    用户说的是指令（"画图 xxx" / "mcomp status" / "陪伴"）时，
            #    插件会产出回复；这时**不再调 LLM**（真实现同款语义：
            #    `_has_send_oper`/有结果 就不再走默认 LLM 链路）。
            #    没有插件产出 → 原样回落 LLM，聊天行为**完全不变**。
            plugin_reply = await _plugin_first_reply_payload(
                _last_user_text(req), svc=svc, req=req
            )
            if plugin_reply is not None:
                return jsonify(plugin_reply[0]), 200

            try:
                result = await svc.complete(req)
            except UpstreamError as exc:
                # 🔴 必须把 body 一并记进日志（2026-10-08）：`exc.status` 只说"403"，
                #    而**为什么 403** 全在 body 里（额度？权限？渠道组不对？）。
                log.warning(
                    "上游失败",
                    extra={"data": {"status": exc.status, "body": exc.body[:500]}},
                )
                return _openai_error(f"上游调用失败：{exc}", "upstream_error", 502)
            except ChatError as exc:
                return _openai_error(str(exc), "invalid_request_error", 400)

            payload: dict[str, Any] = {
                "id": rid,
                "object": "chat.completion",
                "created": created,
                "model": result.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": result.content},
                        "finish_reason": result.finish_reason or "stop",
                    }
                ],
                "usage": result.usage,
            }
            # ⭐ 工具产出的附件（如生成的图片 URL）——
            #    ⚠️ 实测踩到：非流式漏了这个字段 → 图生成了但**用户看不到**。
            #       OpenAI 协议没有它，所以是并列扩展字段（标准客户端会忽略）。
            if result.attachments:
                payload["attachments"] = result.attachments
                # 同时给出扁平 urls，方便简单客户端直接用
                payload["images"] = [
                    a["url"] for a in result.attachments if a.get("type") == "image"
                ]

            return jsonify(payload), 200

        # ---- 流式 ----
        #
        # ⭐⭐ **先给插件一次机会**（与非流式分支**同一处判定**，2026-09-26 补）。
        #
        # 🔴 为什么这条曾经完全缺失（真 bug，差分实验实测）：
        #    派发**只写在非流式分支里**（上面的 `if not req.stream:`），
        #    而面板聊天**永远走流式**（`static/app.js` 发 `stream: true`）
        #    → 用户在面板里说的每一句话，插件**一次都没收到过**：
        #      没建档、没 umo、记忆/关系/被动状态管线拿不到用户的话。
        #    而对照判据 `tools/proactive_multisession_check.py` 用的是
        #    `"stream": False` —— **判据只覆盖了不被使用的那条路**，
        #    所以它全绿。教训：判据要盯**用户真正走的那条路**。
        #
        # 🔴 **必须在返回 SSE 响应【之前】跑**（就在这里，不是放在 `sse()` 里）：
        #    实测踩到 —— 生成器一开跑，Quart 的 app/请求上下文就没了
        #    （`RuntimeError: Not within an app context`），而派发链路要读
        #    `app.config` / 会话仓库并打日志。正解是**在还有上下文的时候算完**，
        #    而不是往生成器里塞 `jsonify` 去"绕过"它。
        #    行为上完全等价：派发本来就必须发生在"调上游之前"。
        #
        # ⚠️ 插件接管时**不许再调上游**（下面会跳过 `svc.stream(req)`），
        #    否则用户会收到两条互相矛盾的回复。
        plugin_reply = await _plugin_first_reply_payload(
            _last_user_text(req), svc=svc, req=req
        )
        plugin_content: str | None = None
        if plugin_reply is not None:
            plugin_content = await _plugin_content_from_payload(plugin_reply[0])

        async def sse() -> AsyncIterator[str]:
            """SSE 生成器。**逐块 yield，不缓冲整段。**

            错误在流中途发生时：发一个 error 事件再发 [DONE]，
            让客户端能明确知道"是失败了"而不是"正常结束"。
            """
            def frame(payload: dict) -> str:
                return f"data: {_json.dumps(payload, ensure_ascii=False)}\n\n"

            def envelope(delta: dict, finish: str | None = None) -> dict:
                return {
                    "id": rid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    # 🔴 走**唯一**的解析处（原来直接读启动快照 `config` ⇒ 回显旧模型名）
            "model": _effective_model(req.model),
                    "choices": [
                        {"index": 0, "delta": delta, "finish_reason": finish}
                    ],
                }

            try:
                # 首个 chunk 带 role（与 OpenAI 惯例一致）
                yield frame(envelope({"role": "assistant", "content": ""}))

                # ⚠️ **派发已经在上面的路由函数体里做完了**（`plugin_reply` /
                #    `plugin_content`）—— **不在生成器里做**：
                #    生成器一开跑就没有 Quart 的 app/请求上下文了（实测
                #    `RuntimeError: Not within an app context`）。
                #    这里只负责按结果**选一条路走**，逻辑上仍在"调上游之前"。
                if plugin_content is None:
                    # 🔴 **必须完整消费生成器，不能 break**。
                    #    实测踩到：最初这里 `if chunk.done: break`，
                    #    结果生成器被提前抛弃 → `ChatService.stream()` 尾部的
                    #    **持久化逻辑永远不执行** → 流式聊天永远不存历史。
                    #    （判据：这里 break 是"省一点循环"换"功能静默失效"，不划算。）
                    async for chunk in svc.stream(req):
                        if chunk.done:
                            continue          # 只忽略该 chunk，不中断循环
                        if chunk.delta or chunk.reasoning_delta:
                            delta: dict = {}
                            if chunk.delta:
                                delta["content"] = chunk.delta
                            if chunk.reasoning_delta:
                                delta["reasoning_content"] = chunk.reasoning_delta
                            yield frame(envelope(delta))
                        if chunk.finish_reason:
                            yield frame(envelope({}, chunk.finish_reason))
                        # ⭐ 工具产出的图片：用**独立事件**推给客户端
                        #    （OpenAI 协议没有"图"这个 delta，所以用并列字段，
                        #      标准客户端会忽略它，我们的面板会渲染）
                        for tc in chunk.tool_call_deltas or []:
                            if tc.get("status") == "success":
                                yield frame({
                                    "id": rid,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    # 🔴 走**唯一**的解析处（原来直接读启动快照 `config` ⇒ 回显旧模型名）
            "model": _effective_model(req.model),
                                    "images": tc.get("data", {}).get("urls", []),
                                    "kind": tc.get("data", {}).get("kind", ""),
                                    "caption": tc.get("data", {}).get("caption", ""),
                                    "choices": [],
                                })
                else:
                    # 插件接管 → 把它的文本用**既有 SSE 帧格式**发出去。
                    # ⚠️ 形状照本生成器一贯的写法：带 role 的空 delta（已在上面发过）
                    #    → content delta → finish_reason="stop"；结束的
                    #    `data: [DONE]` 由下面**共用的那一行**补上（不重复发）。
                    yield frame(envelope({"content": plugin_content}))

                yield frame(envelope({}, "stop"))
            except UpstreamError as exc:
                # 🔴 同非流式路径：body 必须进日志（2026-10-08）
                log.warning(
                    "流式中断",
                    extra={"data": {"status": exc.status, "body": exc.body[:500]}},
                )
                yield frame({"error": {"message": f"上游调用失败：{exc}", "type": "upstream_error"}})
            except Exception:  # noqa: BLE001 — 流里必须兜住，否则连接半开
                log.exception("流式处理异常")
                yield frame({"error": {"message": "内部错误", "type": "server_error"}})

            yield "data: [DONE]\n\n"

        return Response(
            sse(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # 反代下禁用缓冲（爱语也这么做）
                "Connection": "keep-alive",
            },
        )

    # ------------------------------------------------------------------
    # 统一错误处理：**不泄漏内部细节**
    # ------------------------------------------------------------------

    @app.errorhandler(404)
    async def not_found(_exc):  # type: ignore[no-untyped-def]
        return (
            jsonify(
                {
                    "error": {
                        "message": f"Not Found: {request.path}",
                        "type": "invalid_request_error",
                    }
                }
            ),
            404,
        )

    @app.errorhandler(500)
    async def server_error(exc):  # type: ignore[no-untyped-def]
        log.exception("未处理异常", extra={"data": {"path": request.path}})
        return (
            jsonify({"error": {"message": "内部错误", "type": "server_error"}}),
            500,
        )

    # ------------------------------------------------------------------
    # 面板（原生 HTML/JS，零构建工具）
    # ------------------------------------------------------------------

    static_dir = Path(__file__).parent / "static"

    @app.get("/")
    async def panel():  # type: ignore[no-untyped-def]
        """面板首页。**免鉴权**（页面本身只是壳，数据都要 API 鉴权）。

        🔴 **绝不能被缓存**（实测事故）：
            Quart 的 `send_from_directory` 默认 `max_age=12 小时`,
            于是**改完前端、用户手机上还是旧的** —— 情绪报「移动端适配没有/导入没有」,
            实际代码早就生效了,只是浏览器一直用 12 小时前的缓存。
            这类"改动看不见"极难自查(服务端一切正常、curl 也对),
            所以面板一律 `no-cache` + ETag(每次校验,没变就 304,开销可忽略)。
        """
        resp = await send_from_directory(static_dir, "index.html")
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp

    @app.get("/static/<path:filename>")
    async def static_files(filename: str):  # type: ignore[no-untyped-def]
        """静态资源：同样 `no-cache`（靠 ETag 走 304）。

        ⚠️ 不要改成 `max-age=31536000, immutable` 那套"长缓存 + 文件名指纹"策略 ——
           本项目是**零构建**的原生 HTML/JS(没有打包器给文件名加 hash),
           长缓存会让每次改动都要手动 bump `?v=`,而漏 bump 就是又一次
           "改完看不见"。`no-cache` 每次多一个 304 往返,自用场景完全可接受。
        """
        resp = await send_from_directory(static_dir, filename)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp

    # ------------------------------------------------------------------
    # 生成图片的访问（让"生成结果"真正能被看到）
    # ------------------------------------------------------------------

    @app.get("/api/v1/images/<path:filename>")
    async def get_generated_image(filename: str):  # type: ignore[no-untyped-def]
        """取回生成的图片。

        ⚠️ **需要鉴权** —— 生成图可能含隐私（"自拍"），不能公开可读。
        🔴 路径穿越防护：只允许输出目录下的**纯文件名**。
        """
        denied = _require_auth_or_api_key()
        if denied is not None:
            return denied

        # 只接受纯文件名（拒绝 ../ 与绝对路径）
        if "/" in filename or "\\" in filename or filename.startswith("."):
            return jsonify({"error": {"message": "非法文件名", "type": "invalid_request_error"}}), 400

        out_dir = Path(config.image.output_dir)
        target = (out_dir / filename).resolve()
        if not str(target).startswith(str(out_dir.resolve())):
            return jsonify({"error": {"message": "非法路径", "type": "invalid_request_error"}}), 400
        if not target.is_file():
            return jsonify({"error": {"message": "图片不存在", "type": "not_found"}}), 404

        return await send_from_directory(out_dir, filename)

    # ------------------------------------------------------------------
    # 管理 API（人设 / 设置）
    # ------------------------------------------------------------------

    app.register_blueprint(build_blueprint(_require_auth_or_api_key))

    # 模型供应商管理 API（`/api/v1/providers`，契约 docs/PROVIDER_CONTRACT.md §3）
    from .api_providers import build_blueprint as build_providers_blueprint

    app.register_blueprint(build_providers_blueprint(_require_auth_or_api_key))

    # ------------------------------------------------------------------
    # 插件 Web API（把 266 条插件路由挂上路）
    # ------------------------------------------------------------------

    # 🔴 **条件注册**：`plugins.enabled=False` 时连 import 都不发生，
    #    所以不想用插件的部署**完全不依赖** `host/`（也不会有导入开销）。
    if config.plugins.enabled and config.plugins.mount_web_apis:
        _mount_plugin_routes(app, config, _require_auth_or_api_key)

    # ------------------------------------------------------------------
    # 按会话选模型（情绪 2026-10-09）
    # ------------------------------------------------------------------

    # ⭐ 情绪 2026-10-09：**按会话选模型** —— 把"模型 id → base_url/key"的解析器
    #    接进 ChatService。会话上存的是模型供应商页里的模型 id（`<源id>/<模型名>`），
    #    所以这里读的是同一份 `data/providers.json`。
    #
    # ⚠️ **用回调而不是现在就取 store**（与上面 `_config_profile_path` 同款）：
    #    `create_app(...)` 执行时，测试还没设 `EMOTION_PROVIDERS_PATH`；
    #    直接取值会读到默认路径 ⇒ 测试会去碰项目真实的 `data/providers.json`。
    #    回调在**真正要解析时**才求值，那时配置已经就位。
    #
    # ⚠️ 找不到模型时回 `None`，**绝不**假装成功（`resolve_session_model` 会据此报错）。
    #
    # ⚠️🔴 这段**必须住在 `create_app` 主流程**，不能放进 `_mount_plugin_routes()`：
    #     「按会话选模型」跟插件**毫无关系**，而那个函数只在
    #     `config.plugins.enabled and mount_web_apis` 时才被调用
    #     ⇒ 不启插件的部署会**永远接不上**。我第一版就写在那儿，
    #     被 `tests/test_session_model.py` 当场抓住（"未装配模型解析器"）。
    _chat_svc = app.config.get("EMOTION_CHAT")
    if _chat_svc is not None and hasattr(_chat_svc, "bind_model_resolver"):

        def _resolve_model(model_id: str) -> dict[str, Any] | None:
            """把模型 id（`<源id>/<模型名>`）解析成 `{base_url, api_key, model}`。

            🔴 这个函数是「按会话选模型」唯一的解析处 —— 别在别处再写一套
               （本项目「同一概念多条写入路径」已踩 6 次）。
            """
            from ..infra.providers_store import DEFAULT_PATH, ProviderStore

            mid = (model_id or "").strip()
            if not mid:
                return None
            path = app.config.get("EMOTION_PROVIDERS_PATH") or DEFAULT_PATH
            try:
                store = ProviderStore(path)
                model = store.get_model(mid)
                if model is None:
                    log.warning(
                        "会话选的模型在模型供应商里不存在",
                        extra={"data": {"model_id": mid, "path": str(path)}},
                    )
                    return None
                source = store.get_source(model.provider_source_id)
                if source is None:
                    log.warning(
                        "模型所属的源不存在",
                        extra={"data": {"model_id": mid, "source": model.provider_source_id}},
                    )
                    return None
                # ⚠️ key 可能是多把（轮询/故障切换）—— 取第一把，与插件侧同口径
                from .provider_bridge import _first_key

                return {
                    "base_url": str(getattr(source, "api_base", "") or ""),
                    "api_key": _first_key(getattr(source, "key", None)),
                    "model": str(getattr(model, "model", "") or mid.rsplit("/", 1)[-1]),
                }
            except Exception as exc:  # noqa: BLE001
                # 读盘失败不能把聊天带走，但**必须留痕**（静默失败是最贵的 bug）
                log.error(
                    "读取模型供应商配置失败",
                    extra={"data": {"model_id": mid, "error": f"{type(exc).__name__}: {exc}"}},
                )
                return None

        _chat_svc.bind_model_resolver(_resolve_model)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @app.before_serving
    async def _startup() -> None:
        log.info(
            "服务启动",
            extra={
                "data": {
                    "host": config.server.host,
                    "port": config.server.port,
                    "backend": store.backend_name(),
                }
            },
        )
        if config.plugins.enabled:
            report = await app.config["EMOTION_PLUGIN_HOST"].start()
            if report is not None:
                log.info(
                    "插件已挂载",
                    extra={
                        "data": {
                            "loaded": report.loaded,
                            "mounted_apis": report.mounted_count,
                            "initialized": len(report.initialized),
                        }
                    },
                )

    @app.after_serving
    async def _shutdown() -> None:
        svc = app.config.get("EMOTION_PLUGIN_HOST")
        if svc is not None:
            await svc.stop()
        await store.close()
        log.info("服务已停止")

    return app


def _mount_plugin_routes(
    app: Quart,
    config: Config,
    require_auth: Any,
) -> None:
    """挂载插件 Web API 的**两条**通配路由（照真实现，共用同一个派发器）。

        ① `/api/plugins/extensions/<path:plugin_path>` —— 真实现的新形状
           （`plugins.py:379-421`，五种方法）；
        ② `/api/plug/<path:plugin_path>` —— 真实现的 **legacy** 形状
           （`plugins.py:1506`），插件页 JS 里硬编码的那条。

    🔴 **为什么用"一条通配路由"而不是逐条注册**：
       插件路由支持 Flask 风格参数（`<x>` / `<path:x>`），要在 Quart 里逐条
       注册就得把语法转成 Quart 的 `<x>` / `<path:x>` —— 多一层易错转换。
       而真 AstrBot 也是**一条通配路由 + 自己匹配**（`plugins.py:388`
       `@router.get("/plugins/extensions/{plugin_path:path}")`），
       我们照此办理，复用**已验证与真实现逐字相同**的匹配器。

    ⚠️ 五种 HTTP 方法都注册 —— 真实现的新形状同样是 GET/POST/PUT/PATCH/DELETE 五条。

    🔴 **两条路由的 handler 体必须只有一份**（`_dispatch_plugin_api`）：
       它们是"同一个概念的两个 URL 前缀"，复制一份逻辑就等着漂移。
    """
    from .plugin_host import PluginHostService
    from .plugin_routes import not_found_payload, to_quart_response

    service = PluginHostService(config)
    #: 把"取上游客户端"的回调交给宿主服务（供应商装配时用）。
    #: ⚠️ 用回调而非直接传对象：装配顺序上 backend 可能晚于这里。
    service._backend_getter = lambda: app.config.get("EMOTION_CHAT_BACKEND")
    #: 🔴 **会话仓库 + 设置**：投递端口要它们才能把插件的主动消息
    #:    写进用户看得见的会话（`SessionDelivery`）。
    #:    ⚠️ 同样从 `app.config` 取（可能缺席 —— 某些测试只挂插件路由）；
    #:       缺席时投递端口**如实不接**，插件走自己的降级路径。
    service.sessions = app.config.get("EMOTION_SESSIONS")
    service.settings = app.config.get("EMOTION_SETTINGS")
    #: 🔴 **人设仓库**：`context.persona_manager` 靠它才能让插件读到人格
    #:    （情绪 2026-09-21 报障"陪伴获取不到平台的默认人设"）。
    #:    缺了它，插件面板的「快速从已有人格导入」必然报
    #:    「还没有读取到可用的主回复人格文本」—— 那句文案是**误诊**
    #:    （它叫用户去触发一次对话，但缺这个仓库时触发也修不好）。
    service.personas = app.config.get("EMOTION_PERSONAS")
    #: 🔴 **存储后端**：`context.conversation_manager` 靠它才能让插件
    #:    存取自己那份对话历史（主动消息存档）。
    #:    缺了它，插件每轮主动消息都会报「主动消息写入会话历史失败」
    #:    → 存不下自己刚说过的话 → 下一轮读不到上文 → **可能重复话题**
    #:    （情绪 2026-09-27 明确要求修的那条）。
    service.store = app.config.get("EMOTION_STORE")
    #: 🔴 **配置文件路径**（插件启停名单落在 `config_profile` 的 `plugins` 段）。
    #:    ⚠️ 与 `_backend_getter` 同款用回调：本函数在 `create_app` 里执行，
    #:       而测试是在 `create_app(...)` **之后**才设 `EMOTION_CONFIG_PROFILE_PATH`
    #:       —— 直接取值会读到一个还不存在的覆盖（测试就会写进项目的 `data/`）。
    service._config_profile_path = lambda: app.config.get(  # noqa: SLF001
        "EMOTION_CONFIG_PROFILE_PATH"
    )
    app.config["EMOTION_PLUGIN_HOST"] = service

    # ⭐ P0-4：把插件宿主接成 ChatService 的 **LLM 钩子端口**。
    #    这样插件（记忆/人设/关系状态）能在**发给模型之前**注入提示词。
    #    ⚠️ 用 `getattr` 拿 `EMOTION_CHAT`：某些测试只挂插件路由、没有 chat 服务，
    #       那种情况下静默跳过是对的（不是错误路径）。
    _chat_svc = app.config.get("EMOTION_CHAT")
    if _chat_svc is not None and hasattr(_chat_svc, "bind_llm_hook"):
        _chat_svc.bind_llm_hook(service.apply_llm_hooks)
        log.info("LLM 钩子已接入（插件可在调模型前注入提示词）")


    methods = ["GET", "POST", "PUT", "PATCH", "DELETE"]

    async def _dispatch_plugin_api(plugin_path: str) -> Any:
        """把一次插件 Web API 调用**派发给宿主**（本条路由唯一的写入路径）。

        🔴 **为什么抽成内部函数**（本项目铁律：同一概念不许有两条会漂移的写入路径）：
           交付时 `/api/plugins/extensions/...`（真实现形状）与
           `/api/plug/...`（真实现的 **legacy** 形状，见下）必须做**同一件事**。
           复制第二份"鉴权 → 判空 → 派发 → 报错"的逻辑，
           就是"同一概念多条写入路径"的第 4 次复发 —— 改一处忘一处。
           ⇒ 两条路由**都只调这个函数**。

        ⚠️ **鉴权刻意留在这里的第一行**：它不是"顺手的礼貌"，
           而是"把 URL 直接打进来"这条链路的**唯一闸门**
           （本项目真事故：新增 API 路由漏 `require_auth()`
            → 未登录返 200，而服务绑 `0.0.0.0` = 公网可读运行日志）。
        """
        # 复用主服务的鉴权（会话 cookie 或 Bearer 令牌）
        denied = require_auth()
        if denied is not None:
            return denied

        host = service.host
        if host is None:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "插件宿主未启动（查看服务日志）",
                        "data": {},
                    }
                ),
                503,
            )

        try:
            result = await host.call_api(
                plugin_path,
                request_obj=request,
                method=request.method,
                username="owner",  # 单用户：我们只有"主人"这一个身份
            )
        except LookupError as exc:
            # ⚠️ 路由没登记 —— 给出相近候选，别只丢一句 404
            near = [
                a.route
                for a in (host.report.mounted if host.report else [])
                if a.route.startswith(f"/{plugin_path.lstrip('/').split('/')[0]}")
            ][:8]
            log.warning(
                "插件路由未命中",
                extra={"data": {"path": plugin_path, "method": request.method,
                                "similar": near}},
            )
            return jsonify(not_found_payload(plugin_path, near)), 404
        except Exception as exc:  # noqa: BLE001
            # 🔴 **插件抛错必须留全栈**（本项目铁律：静默失败最贵）
            log.exception(
                "插件 API 执行失败",
                extra={"data": {"path": plugin_path, "method": request.method}},
            )
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": f"插件执行失败：{type(exc).__name__}: {exc}",
                        "data": {},
                    }
                ),
                500,
            )

        return await to_quart_response(result)

    @app.route("/api/plugins/extensions/<path:plugin_path>", methods=methods)
    async def plugin_extension(plugin_path: str):  # type: ignore[no-untyped-def]
        """真实现的**新**形状（`plugins.py:379-421`，五种方法都有）。"""
        return await _dispatch_plugin_api(plugin_path)

    # ------------------------------------------------------------------
    # `/api/plug/<path:plugin_path>` —— 真实现的 **legacy 形状**（缺它 = 404）
    #
    # 🔴 为什么必须有（2026-09-27 实测，见 `docs/插件拓展页_API_404_诊断.md`）：
    #    插件页 JS **硬编码** `const API = "/api/plug/<插件名>/page"`，
    #    这不是作者的错 —— 真 AstrBot 4.27.5 确实有这条
    #    （`dashboard/api/plugins.py:1506`
    #     `@legacy_router.api_route("/api/plug/{plugin_path:path}", methods=["GET","POST"])`），
    #    内存插件页的注释也写着 *"the /api/plug route is available across
    #    dashboard versions"*。我们只实现了 `/api/plugins/extensions` 那条 ⇒
    #    `qzone-companion`（`astrbot_plugin_content_companion`）整页取不到数。
    #
    # ⚠️ **方法集与真实现不同，这是有意的**：
    #    真 legacy 那条只有 GET/POST，但隔壁 `/api/plugins/extensions/...` 是五种。
    #    两条路由**指向同一个派发器**，若方法集不同，同一个插件路由
    #    （如 `page/action` 用 PUT）换个前缀就会从"能调"变成"404" ——
    #    那是**又一条会漂移的差异**，且方向上只会让兼容面变小。
    #    ⇒ 取**五种**（与 `/api/plugins/extensions/...` 一致），
    #      兼容面严格是超集；请求最终仍由宿主按插件**自己登记的方法**匹配
    #      （`web_router.match_registered_web_api`），所以放宽外层不会放行
    #      插件没登记的方法（`test_wrong_method_404` 仍守着这条）。
    # ------------------------------------------------------------------

    @app.route("/api/plug/<path:plugin_path>", methods=methods)
    async def plugin_extension_legacy(plugin_path: str):  # type: ignore[no-untyped-def]
        """legacy 前缀 —— 与上面那条**共用同一个派发器**（不许有第二份实现）。"""
        return await _dispatch_plugin_api(plugin_path)

    # ------------------------------------------------------------------
    # 插件**拓展页**（情绪报的「插件没有配置入口」就是缺了这一段）
    #
    # 🔴 上一轮我只挂了插件**数据 API**，没挂**页面**，所以 `metadata.yaml`
    #    里声明了 `pages:` 的插件（陪伴面板/QQ空间/一起房间）在面板里没有入口。
    #
    # 三条路由（见 `host/astrbot_host/plugin_pages.py` 的模块说明）：
    #   ① 列表：/api/v1/plugins/pages        —— 面板用它渲染入口
    #   ② 页面：/api/v1/plugins/<id>/pages/<name>[/assets/<path>]
    #   ③ 根路径兼容：/<plugin_dir>/<path>   —— 插件页 JS 里硬编码的路径
    # ------------------------------------------------------------------

    from .plugin_pages_web import mount_plugin_page_routes

    mount_plugin_page_routes(app, service, require_auth)

    # ------------------------------------------------------------------
    # 插件**启停**（照真 AstrBot 的 `inactivated_plugins`）
    #
    # 为什么要有：真 AstrBot 能把某个插件停掉（`star_manager.turn_off_plugin`），
    # 我们以前**没有这个能力** ⇒ 不想让它跑的插件只能一直跑：
    #   * `prompt_tools` 每轮 LLM 请求一条 ERROR（它用的是旧版 dict API）；
    #   * `meme_manager` 每次启动一条 import 失败；
    #   * `gemini_content_keeper` / `media_parser` 初始化失败/超时。
    #
    # 🔴 两条路由都**必须**先 `require_auth()`（本项目真事故：
    #    我加的 `/api/v1/logs` 漏了它 → 未登录返 200，而服务绑 0.0.0.0
    #    = 公网可读运行日志）。这里的读接口会吐出全部插件名与错误信息，
    #    写接口能改配置 —— 更不能漏。
    #
    # 🔴 生效时机**如实**：保存后需要**重启服务**才会应用（真 AstrBot 的
    #    `turn_on_plugin` 同样是 `await self.reload(plugin_name)`）。
    #    所以响应里带 `restart_required`，面板上会明写这一句 —— 不糊过去。
    # ------------------------------------------------------------------

    @app.get("/api/v1/plugins")
    async def list_all_plugins():  # type: ignore[no-untyped-def]
        """列出**全部**插件及其状态（含失败的、被停用的）。"""
        denied = require_auth()
        if denied is not None:
            return denied
        if service.host is None:
            # ⚠️ **不返回空列表**：空列表 = "一个插件都没有"，那是另一回事
            #    （本项目最贵的 bug 类型：把"读失败/没起来"显示成"没有"）。
            return (
                jsonify(
                    {
                        "error": {
                            "message": "插件宿主未启动（请看服务日志里的原因）",
                            "type": "unavailable",
                        }
                    }
                ),
                503,
            )
        return jsonify(service.plugin_activation_view()), 200

    @app.put("/api/v1/plugins/inactivated")
    async def set_inactivated_plugins():  # type: ignore[no-untyped-def]
        """保存停用名单（**整体替换**）。

        body：`{"inactivated_plugins": ["<目录名>.main", ...]}`

        ⚠️ 用"整体替换"而不是逐个开关：面板本来就展示全部插件，
           整份提交没有"两个请求各改一半"的竞态；而且它天然幂等 ——
           真机判据（发两次同样的 body，磁盘内容逐字相同）能直接验。
        """
        denied = require_auth()
        if denied is not None:
            return denied

        from ..domain.config_profile import ConfigProfileError

        body = await request.get_json(silent=True)
        if not isinstance(body, dict) or "inactivated_plugins" not in body:
            return (
                jsonify(
                    {
                        "error": {
                            "message": "请求体必须是 JSON 对象且带 inactivated_plugins 字段",
                            "type": "bad_request",
                            "field": "inactivated_plugins",
                        }
                    }
                ),
                400,
            )
        try:
            view = service.save_inactivated(body["inactivated_plugins"])
        except ConfigProfileError as exc:
            return (
                jsonify(
                    {
                        "error": {
                            "message": str(exc),
                            "type": "invalid_config_profile",
                            "field": exc.field_name,
                        }
                    }
                ),
                400,
            )
        return jsonify(view), 200

    # ------------------------------------------------------------------
    # 插件**详情**（照真 AstrBot 的 `GET /api/v1/plugins/by-id`）
    #
    # 🔴 为什么需要它（不是"抄个人家有的界面"）：
    #    我们的插件跑在**兼容层**上，"这个插件到底注册上了什么"是最常要回答的
    #    问题 —— 此前面板只能显示 `handler_count` 一个**数字**，
    #    看不出"注册的是哪几条指令、有没有 LLM 工具"。
    #    本项目最贵的 bug 类型是**静默失败**（插件把兼容层缺口 catch 成一行
    #    info 日志），所以"逐条列出来"本身就是判据。
    #
    # ⚠️ 路由形状与真实现一致：`GET /api/v1/plugins/<plugin_id>`。
    #    真实现还有 `/api/v1/plugins/by-id?plugin_id=` 的别名，我们**不**加 ——
    #    没有调用方需要它，多一条就多一个要维护的入口。
    #
    # 🔴 参数名用 `<plugin_id>` 而不是真实现的 `<plugin_id:path>`：
    #    插件 id 是**目录名**，不含 `/`；用 `path` 会把 `/api/v1/plugins/a/b`
    #    也吞进来，和拓展页路由撞车。
    # ------------------------------------------------------------------

    @app.get("/api/v1/plugins/<plugin_id>")
    async def plugin_detail(plugin_id: str):  # type: ignore[no-untyped-def]
        """一个插件的详情（信息 + 注册数量 + 六类"插件行为" + README）。"""
        # 🔴 第一行必须鉴权（本项目真事故：漏了它 → 未登录也能读，
        #    而服务绑 0.0.0.0）。这条会吐出插件的**完整堆栈**，更不能漏。
        denied = require_auth()
        if denied is not None:
            return denied

        host = service.host
        if host is None:
            # ⚠️ **不返回空详情**：那会把"宿主没起来"显示成"这个插件没内容"
            return (
                jsonify(
                    {
                        "error": {
                            "message": "插件宿主未启动（请看服务日志里的原因）",
                            "type": "unavailable",
                        }
                    }
                ),
                503,
            )

        detail = host.plugin_detail(plugin_id)
        if detail is None:
            # 列出可用的 id —— 只说"没有"会让用户不知道自己拼错了哪个字
            available = sorted(service.plugin_activation_ids())
            return (
                jsonify(
                    {
                        "error": {
                            "message": f"没有这个插件：{plugin_id}",
                            "type": "not_found",
                            "available": available,
                        }
                    }
                ),
                404,
            )

        readme = host.plugin_readme(plugin_id)
        if readme is None:
            # 如实为空（照真实现：没有 README 就是没有）
            detail["readme"] = None
            detail["readme_truncated"] = False
        else:
            detail["readme"], detail["readme_truncated"] = readme

        return jsonify(detail), 200
