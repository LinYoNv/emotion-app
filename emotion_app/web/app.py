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

        🔴 为什么不能只读配置：插件的 `data["users"]` 按 user_id 分，
           "哪个会话开主动消息"就是"哪个 user 的开关为真"。
           所有会话共用一个 `"default"` 的话，**三个会话只能同开同关** ——
           这正是情绪要的场景做不到的原因。

        ⚠️ 任何取不到的情况都返回空串（不抛）：
           无 session_id / 无会话仓库 / 会话不存在 / 会话没绑 →
           调用方回落全局 `plugins.umo_user_id`（**行为与以前一致**）。
        """
        sid = str(session_id or "").strip()
        if not sid:
            return ""
        sessions = getattr(svc, "_sessions", None)
        if sessions is None:
            return ""
        try:
            session = await sessions.get(sid)
        except Exception:  # noqa: BLE001 — 会话不存在不该让聊天崩掉
            return ""
        return str(getattr(session, "user_id", "") or "")

    async def _plugin_first_reply(
        text: str, *, svc: Any, req: Any
    ) -> Any:
        """把用户这轮输入派发给插件；**有产出就返回 OpenAI 格式响应**。

        返回 `None` = "插件没说话，请照常走 LLM"。

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
        from emotion_app.web.plugin_host import plugin_reply_text

        content = plugin_reply_text(res)
        if content is None:
            # 插件没接管 → 交回 LLM（**最常见的正常路径**）
            return None

        rid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        payload: dict[str, Any] = {
            "id": rid,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.model or config.upstream.model,
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
            },
        }
        return jsonify(payload), 200

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

        svc = _chat_or_none()
        model = config.upstream.model
        if svc is not None:
            model = svc.resolve_target(None).model

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
            plugin_reply = await _plugin_first_reply(
                _last_user_text(req), svc=svc, req=req
            )
            if plugin_reply is not None:
                return plugin_reply

            try:
                result = await svc.complete(req)
            except UpstreamError as exc:
                log.warning("上游失败", extra={"data": {"status": exc.status}})
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
                    "model": req.model or config.upstream.model,
                    "choices": [
                        {"index": 0, "delta": delta, "finish_reason": finish}
                    ],
                }

            try:
                # 首个 chunk 带 role（与 OpenAI 惯例一致）
                yield frame(envelope({"role": "assistant", "content": ""}))

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
                                "model": req.model or config.upstream.model,
                                "images": tc.get("data", {}).get("urls", []),
                                "kind": tc.get("data", {}).get("kind", ""),
                                "caption": tc.get("data", {}).get("caption", ""),
                                "choices": [],
                            })

                yield frame(envelope({}, "stop"))
            except UpstreamError as exc:
                log.warning("流式中断", extra={"data": {"status": exc.status}})
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
    """挂载 `/api/plugins/extensions/<path:plugin_path>`（照真实现的路由形状）。

    🔴 **为什么用"一条通配路由"而不是逐条注册**：
       插件路由支持 Flask 风格参数（`<x>` / `<path:x>`），要在 Quart 里逐条
       注册就得把语法转成 Quart 的 `<x>` / `<path:x>` —— 多一层易错转换。
       而真 AstrBot 也是**一条通配路由 + 自己匹配**（`plugins.py:388`
       `@router.get("/plugins/extensions/{plugin_path:path}")`），
       我们照此办理，复用**已验证与真实现逐字相同**的匹配器。

    ⚠️ 五种 HTTP 方法都注册 —— 真实现同样是 GET/POST/PUT/PATCH/DELETE 五条。
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

    @app.route("/api/plugins/extensions/<path:plugin_path>", methods=methods)
    async def plugin_extension(plugin_path: str):  # type: ignore[no-untyped-def]
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
