# -*- coding: utf-8 -*-
"""子代理接入 API —— 让用户**在面板里连上自己的 agent 平台**。

契约（前端只消费，不许再抄一份字段清单）：

```
GET  /api/v1/subagents            → data:{config, backend_types}
PUT  /api/v1/subagents            → 保存配置（局部更新）
GET  /api/v1/subagents/backends   → data:{types:[{id,label,description,fields}]}
POST /api/v1/subagents/test       → 连通性测试
```

信封照全站约定：成功 `{status:"ok",message,data}`；
失败 `{status:"error",message,reason,error:{message,type}}`。

## 🔴 三条本文件必须守住的纪律

**① 每一条路由都要 `require_auth()`** —— 本项目真事故：漏一个就公网可读。
   测试里必须有**反向**那条（无令牌 → 401），只测 200 不算验过。

**② token 只写不回显** —— 进来的打码值表示"用户没动"，保持原值。
   由 `emotion_app/domain/subagents.py` 统一处理，这里不重复实现。

**③ `/test` 必须**真**连一次** —— 只回 `{"ok": true}` 而不发请求，
   等于让用户以为接通了。本项目最贵的就是这类"看起来成功"。
"""
from __future__ import annotations

import logging
from typing import Any

from quart import Blueprint, current_app, request

logger = logging.getLogger(__name__)


def _ok(data: Any, message: str | None = None, status: int = 200) -> tuple[Any, int]:
    return {"status": "ok", "message": message, "data": data}, status


def _fail(
    message: str, reason: str, status: int = 400, error_type: str = "invalid_request_error"
) -> tuple[Any, int]:
    return (
        {
            "status": "error",
            "message": message,
            "reason": reason,
            "error": {"message": message, "type": error_type},
        },
        status,
    )


def build_blueprint(require_auth: Any) -> Blueprint:
    """造子代理配置 API 蓝图。"""
    bp = Blueprint("subagents", __name__, url_prefix="/api/v1/subagents")

    def _service() -> Any:
        return current_app.config.get("EMOTION_SUBAGENTS_SERVICE")

    @bp.get("")
    @bp.get("/")
    async def read_config():
        denied = require_auth()
        if denied:
            return denied
        service = _service()
        if service is None:
            return _fail("子代理配置服务未装配", "subagents_unavailable", 503)
        return _ok({
            "config": service.public_view(),
            "backend_types": service.backend_types(),
        })

    @bp.get("/backends")
    async def list_backends():
        denied = require_auth()
        if denied:
            return denied
        service = _service()
        if service is None:
            return _fail("子代理配置服务未装配", "subagents_unavailable", 503)
        return _ok({"types": service.backend_types()})

    @bp.put("")
    @bp.patch("")
    async def save_config():
        denied = require_auth()
        if denied:
            return denied
        service = _service()
        if service is None:
            return _fail("子代理配置服务未装配", "subagents_unavailable", 503)
        payload = await request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return _fail("请求体必须是一个对象", "invalid_body")
        try:
            view = await service.update(payload)
        except ValueError as exc:
            return _fail(f"子代理配置无效：{exc}", "subagents_invalid")
        # 保存后把新配置应用到运行时（注册表/后端）。
        # ⚠️ 应用失败要**如实报**，不能让"保存成功但没生效"这种情况静默存在。
        applied, reason = await _apply(service)
        if not applied:
            return _ok(
                {"config": view, "applied": False, "reason": reason},
                "配置已保存，但**尚未生效**：" + reason,
            )
        return _ok({"config": view, "applied": True}, "子代理配置已保存")

    @bp.post("/test")
    async def test_connection():
        """**真连一次**，把成败与原样讲清楚。"""
        denied = require_auth()
        if denied:
            return denied
        service = _service()
        if service is None:
            return _fail("子代理配置服务未装配", "subagents_unavailable", 503)
        payload = await request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return _fail("请求体必须是一个对象", "invalid_body")

        from emotion_app.web.subagent_backends import probe_backend

        # 允许"用还没保存的参数试一下"（用户在表单里填完先测再存）
        connection = dict(service.connection())
        for key in ("backend", "endpoint", "token", "timeout_seconds"):
            if key in payload:
                value = payload[key]
                # 打码值 = 用户没动，沿用已保存的真值
                from emotion_app.domain.subagents import is_masked

                if key == "token" and is_masked(value):
                    continue
                connection[key] = value

        result = await probe_backend(connection)
        if result.get("ok"):
            return _ok(result, "连通性正常")
        # ⚠️ 用 200 返回**业务失败**：这不是"请求格式错了"，
        #    而是"试了，但对面不通"。前端据 `data.ok` 区分。
        return _ok(result, result.get("reason") or "连接失败")

    async def _apply(service: Any) -> tuple[bool, str]:
        """把配置应用到运行时。返回 (是否生效, 原因)。

        🔴 为什么必须有这一步：只保存不应用 = "改了没反应"，
           用户会以为界面坏了（本项目最忌的静默失败）。
        """
        applier = current_app.config.get("EMOTION_SUBAGENTS_APPLY")
        if applier is None:
            logger.warning(
                "子代理运行时应用器未装配：配置已保存但**没有生效**（需要重启服务）"
            )
            return False, "运行时应用器未装配（需要重启服务）"
        try:
            await applier(service)
        except Exception as exc:  # noqa: BLE001 — 要如实报，不能吞
            logger.exception("应用子代理配置失败")
            return False, f"{type(exc).__name__}: {exc}"
        return True, ""

    return bp
