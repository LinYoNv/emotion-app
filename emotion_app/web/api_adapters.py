# -*- coding: utf-8 -*-
"""适配器管理 API —— 面板「适配器」页的后端。

## 路由表

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/adapters` | 列表（凭据**打码**）+ 类型目录 + 运行状态 |
| POST | `/api/v1/adapters` | 新增（`{type, name, config, enabled}`） |
| GET | `/api/v1/adapters/types` | 可添加的适配器类型（含字段定义，前端据此渲染表单） |
| PUT | `/api/v1/adapters/<adapter_id>` | 更新（改名 / 改配置 / 启停） |
| DELETE | `/api/v1/adapters/<adapter_id>` | 删除（内置的拒绝） |
| GET | `/api/v1/adapters/<adapter_id>/status` | 单个适配器的实时状态 |

## 🔴 三条必须遵守的纪律

1. **全部要鉴权**。漏了就是"公网可读/可改适配器配置"——
   而适配器配置里含 **OneBot access_token**。本项目真实事故：
   漏 `require_auth()` 导致公网可读日志。
2. **凭据一律打码返回**（`to_public()` 里做）。
   更新时收到打码值 = "用户没改这一项" → **保持原值**，
   否则用户一保存就把 token 洗成 `••••`（照 providers 页的同款做法）。
3. **改完要真的生效**（`reload`），并**如实返回**每个适配器的
   `running` / `connected`。只说"保存成功"而没生效，是静默失败。

## 为什么"状态"和"配置"分成两个字段返回

配置是**持久**的（`data/adapters.json`），状态是**易失**的（内存里那个 adapter）。
把它们混在一个对象里，前端就无法区分"我保存了但没连上"与"我没保存成功"。
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
    """造适配器管理 API 蓝图。"""
    bp = Blueprint("adapters", __name__, url_prefix="/api/v1/adapters")

    def _store() -> Any:
        return current_app.config.get("EMOTION_ADAPTERS_STORE")

    def _runtime() -> Any:
        return current_app.config.get("EMOTION_ADAPTERS_RUNTIME")

    async def _reload() -> None:
        """把配置变更**真的**应用到运行时。"""
        store = _store()
        runtime = _runtime()
        if store is None or runtime is None:
            # ⚠️ 未装配时**如实记 warning**，不假装成功
            logger.warning(
                "适配器运行时未装配，配置已保存但**没有生效**（需要重启服务）"
            )
            return
        await runtime.reload(store.all())

    @bp.get("")
    @bp.get("/")
    async def list_adapters():
        denied = require_auth()
        if denied:
            return denied
        store = _store()
        if store is None:
            return _fail("适配器存储未装配", "adapters_unavailable", 503)
        runtime = _runtime()
        # 运行状态按 id 索引，方便前端合并
        live: dict[str, Any] = {}
        if runtime is not None:
            for item in runtime.status():
                live[str(item.get("id"))] = item

        items = []
        for record in store.all():
            public = record.to_public()
            public["runtime"] = live.get(
                record.id,
                {
                    "id": record.id,
                    "running": False,
                    "connected": False,
                    # 内置 Web 端不是"未运行"，它本来就不需要连接
                    "last_error": None,
                },
            )
            items.append(public)
        return _ok({"adapters": items, "types": _type_catalog()})

    @bp.get("/types")
    async def list_types():
        denied = require_auth()
        if denied:
            return denied
        return _ok({"types": _type_catalog()})

    @bp.post("")
    async def create_adapter():
        denied = require_auth()
        if denied:
            return denied
        store = _store()
        if store is None:
            return _fail("适配器存储未装配", "adapters_unavailable", 503)
        payload = await request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return _fail("请求体必须是一个对象", "invalid_body")
        from emotion_app.infra.adapters_store import (  # noqa: PLC0415
            AdapterValidationError,
        )

        try:
            record = store.add(
                adapter_type=str(payload.get("type") or ""),
                name=str(payload.get("name") or ""),
                config=payload.get("config") or {},
                enabled=bool(payload.get("enabled", True)),
            )
        except AdapterValidationError as exc:
            return _fail(str(exc), "adapter_invalid")
        except (KeyError, TypeError, ValueError) as exc:
            return _fail(f"适配器配置无效：{exc}", "adapter_invalid")
        await _reload()
        return _ok(record.to_public(), "适配器已添加", 201)

    @bp.put("/<adapter_id>")
    @bp.patch("/<adapter_id>")
    async def update_adapter(adapter_id: str):
        denied = require_auth()
        if denied:
            return denied
        store = _store()
        if store is None:
            return _fail("适配器存储未装配", "adapters_unavailable", 503)
        payload = await request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return _fail("请求体必须是一个对象", "invalid_body")
        from emotion_app.infra.adapters_store import (  # noqa: PLC0415
            AdapterNotFound,
            AdapterValidationError,
        )

        try:
            record = store.update(
                adapter_id,
                name=payload.get("name"),
                config=payload.get("config"),
                enabled=payload.get("enabled"),
            )
        except AdapterNotFound:
            return _fail(f"找不到适配器 {adapter_id!r}", "adapter_not_found", 404)
        except AdapterValidationError as exc:
            return _fail(str(exc), "adapter_invalid")
        except (KeyError, TypeError, ValueError) as exc:
            return _fail(f"适配器配置无效：{exc}", "adapter_invalid")
        await _reload()
        return _ok(record.to_public(), "适配器已更新")

    @bp.delete("/<adapter_id>")
    async def delete_adapter(adapter_id: str):
        denied = require_auth()
        if denied:
            return denied
        store = _store()
        if store is None:
            return _fail("适配器存储未装配", "adapters_unavailable", 503)
        from emotion_app.infra.adapters_store import (  # noqa: PLC0415
            AdapterNotFound,
            AdapterValidationError,
        )

        try:
            store.delete(adapter_id)
        except AdapterNotFound:
            return _fail(f"找不到适配器 {adapter_id!r}", "adapter_not_found", 404)
        except AdapterValidationError as exc:
            # 内置适配器不许删 → 403（不是 400：这是权限，不是格式错）
            return _fail(str(exc), "adapter_protected", 403)
        await _reload()
        return _ok({"id": adapter_id}, "适配器已删除")

    @bp.get("/<adapter_id>/status")
    async def adapter_status(adapter_id: str):
        denied = require_auth()
        if denied:
            return denied
        store = _store()
        runtime = _runtime()
        if store is None:
            return _fail("适配器存储未装配", "adapters_unavailable", 503)
        from emotion_app.infra.adapters_store import AdapterNotFound  # noqa: PLC0415

        try:
            record = store.get(adapter_id)
        except AdapterNotFound:
            return _fail(f"找不到适配器 {adapter_id!r}", "adapter_not_found", 404)

        live = None
        if runtime is not None:
            live = next(
                (x for x in runtime.status() if str(x.get("id")) == adapter_id), None
            )
        return _ok(
            {
                "id": adapter_id,
                "enabled": record.enabled,
                "runtime": live
                or {"id": adapter_id, "running": False, "connected": False},
            }
        )

    return bp


def _type_catalog() -> list[dict[str, Any]]:
    """把 `ADAPTER_TYPES` 转成前端友好的列表（**字段定义后端给**）。"""
    from emotion_app.infra.adapters_store import ADAPTER_TYPES  # noqa: PLC0415

    out = []
    for type_id, meta in ADAPTER_TYPES.items():
        out.append(
            {
                "type": type_id,
                "label": meta.get("label", type_id),
                "description": meta.get("description", ""),
                "fields": meta.get("fields", []),
                "builtin": bool(meta.get("builtin")),
            }
        )
    return out


__all__ = ["build_blueprint"]
