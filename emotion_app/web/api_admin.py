# -*- coding: utf-8 -*-
"""面板/App 用的管理 API（`/api/v1/*`）。

分工（见 `docs/BACKEND_FRAMEWORK.md` §2.2）：
  * `/v1/*`        —— OpenAI 兼容（给聊天客户端）
  * `/api/v1/*`    —— 管理（给人设/设置面板）
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from quart import Blueprint, jsonify, request, send_from_directory

from ..domain.context import VALID_MODES, ContextPolicy
from ..domain.persona import Persona, PromptSection
from ..domain.persona_import import (
    CONFLICT_MODES,
    CONFLICT_OVERWRITE,
    CONFLICT_RENAME,
    CardImportError,
    export_filename,
    merge_import_sections,
    parse_card,
    parse_pasted_text,
    resolve_name_conflict,
    to_aiyu_backup,
    to_export_dict,
)
from ..domain.persona_repo import PersonaNotFound
from ..domain import persona_ref as persona_ref_mod
from ..domain.session_repo import SessionNotFound
from ..domain.settings import WRITABLE_FIELDS
from ..infra.logging_setup import get_logger, scrub_text
from ..storage.base import StorageError

log = get_logger(__name__)

#: 头像落盘目录（相对运行目录）。与人设数据同源，备份 `data/` 就一起带走了。
AVATAR_DIR = "data/avatars"

#: 头像大小上限 —— PNG 卡本身已限 16 MB，这里再兜一层
MAX_AVATAR_BYTES = 16 * 1024 * 1024


def _avatar_dir() -> Path:
    d = Path(AVATAR_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _save_avatar(persona_id: str, data: bytes) -> str:
    """把头像 PNG 落盘，返回**纯文件名**（存进 `persona.avatar_path`）。

    ⚠️ 只存文件名不存路径：避免把宿主机目录结构写进数据，
       也让"整个 data/ 目录搬走"依然有效。
    """
    if len(data) > MAX_AVATAR_BYTES:
        raise ValueError(f"头像过大（{len(data)} > {MAX_AVATAR_BYTES}）")
    name = f"{persona_id}.png"
    # 走 asyncio 的线程池，别在事件循环里做阻塞 IO
    await asyncio.to_thread((_avatar_dir() / name).write_bytes, data)
    return name


def _err(message: str, etype: str, status: int, *, field: str = "") -> tuple[Any, int]:
    """错误响应。

    ⚠️ `field`（可选）= 出错的**配置项键名**。配置文件页要靠它把错误标在
       **对应的那个控件**旁边 —— 只给一句"值不合法"，用户在几十个字段里找不到是哪一个。
       （消息里其实也带了键名，但让前端去正则抠是不该有的耦合。）
    """
    payload: dict[str, Any] = {"message": message, "type": etype}
    if field:
        payload["field"] = field
    return jsonify({"error": payload}), status


def build_blueprint(require_auth: Any) -> Blueprint:
    """造管理 API 蓝图。

    Args:
        require_auth: 返回 None（通过）或 (响应, 状态码)（拒绝）的可调用对象。
    """
    bp = Blueprint("admin", __name__, url_prefix="/api/v1")

    def _svc() -> tuple[Any, Any, Any]:
        from quart import current_app

        return (
            current_app.config.get("EMOTION_PERSONAS"),
            current_app.config.get("EMOTION_SETTINGS"),
            current_app.config.get("EMOTION_CHAT"),
        )

    def _sessions() -> Any:
        from quart import current_app

        return current_app.config.get("EMOTION_SESSIONS")

    def _session_payload(s: Any) -> dict[str, Any]:
        """会话的对外形状 —— **唯一**的一处（六条路由都走它）。

        🔴 为什么要收成一个函数：会话响应在多处被 jsonify（列表/新建/详情/
           改/清空/已读/换 umo）。各写一遍的话，新增字段必然漏掉其中几处 ——
           本项目已经因为"同一概念有多条写入路径"栽过三次
           （供应商类型过滤 / `LLMResponse` 三条转换路径 / 平台元数据三处来源）。

        🆕 在 `to_dict()` 之上多给两个**派生**字段：
          * `umo`      —— 完整 umo（`11:FriendMessage:<user_id>`）。
                          ⭐ 情绪 2026-09-26 要的：他拿这个值去陪伴面板建档、
                          开主动消息（「陪你插件不就是靠 umo 去识别用户的嘛」）。
                          没绑 user_id 的旧会话 → **空串**（不是编一个假的）。
          * `platform` —— umo 第一段（平台实例 id），便于面板显示"发到哪个平台"。
        """
        d = s.to_dict()
        uid = str(d.get("user_id") or "")
        umo = ""
        if uid:
            try:
                # 🔴 **必须复用宿主的唯一权威定义**（`host/astrbot_host/umo.py`）：
                #    `host/tests/test_umo.py` 有一条判据禁止第二处手写 umo 片段，
                #    前端更是绝对不能自己拼（它连平台 id 都不知道）。
                from .plugin_host import ensure_host_on_path

                ensure_host_on_path()
                from astrbot_host.umo import build_umo

                umo = build_umo(uid)
            except Exception:  # noqa: BLE001
                # 插件宿主不可用（如 `plugins.enabled=false`）时**如实留空并留痕**，
                # 绝不静默返回一个"看起来对"的 umo（那会让用户去陪伴面板
                # 建一个永远收不到消息的用户）。
                log.warning(
                    "计算会话 umo 失败（插件宿主不可用？）—— 该会话的 umo 将为空",
                    extra={"data": {"session": d.get("id")}},
                    exc_info=True,
                )
        d["umo"] = umo
        d["platform"] = umo.split(":", 1)[0] if umo else ""
        return d

    # ==================================================================
    # 人设 CRUD
    # ==================================================================

    @bp.get("/personas")
    async def list_personas():  # type: ignore[no-untyped-def]
        denied = require_auth()
        if denied is not None:
            return denied
        repo, settings, _ = _svc()
        if repo is None:
            return _err("人设服务未就绪", "server_error", 503)

        items = await repo.list()
        active = settings.active_persona_id if settings else ""
        return jsonify(
            {
                "object": "list",
                "active_persona_id": active,
                "data": [p.to_dict() for p in items],
            }
        ), 200

    @bp.get("/personas/<persona_id>")
    async def get_persona(persona_id: str):  # type: ignore[no-untyped-def]
        denied = require_auth()
        if denied is not None:
            return denied
        repo, _, _ = _svc()
        try:
            p = await repo.get(persona_id)
        except PersonaNotFound:
            return _err(f"人设不存在：{persona_id}", "not_found", 404)
        return jsonify(p.to_dict()), 200

    @bp.post("/personas")
    async def create_persona():  # type: ignore[no-untyped-def]
        denied = require_auth()
        if denied is not None:
            return denied
        repo, _, _ = _svc()

        body = await request.get_json(silent=True)
        if not isinstance(body, dict):
            return _err("请求体必须是 JSON 对象", "invalid_request_error", 400)

        try:
            persona = Persona.from_dict(body)
            created = await repo.create(persona)
        except ValueError as exc:
            return _err(str(exc), "invalid_request_error", 400)
        except Exception as exc:  # noqa: BLE001
            log.warning("创建人设失败", extra={"data": {"error": str(exc)}})
            return _err(f"创建失败：{exc}", "invalid_request_error", 400)

        return jsonify(created.to_dict()), 201

    @bp.patch("/personas/<persona_id>")
    async def update_persona(persona_id: str):  # type: ignore[no-untyped-def]
        denied = require_auth()
        if denied is not None:
            return denied
        repo, _, _ = _svc()

        body = await request.get_json(silent=True)
        if not isinstance(body, dict):
            return _err("请求体必须是 JSON 对象", "invalid_request_error", 400)

        try:
            updated = await repo.update(persona_id, body)
        except PersonaNotFound:
            return _err(f"人设不存在：{persona_id}", "not_found", 404)
        except ValueError as exc:
            return _err(str(exc), "invalid_request_error", 400)

        return jsonify(updated.to_dict()), 200

    @bp.delete("/personas/<persona_id>")
    async def delete_persona(persona_id: str):  # type: ignore[no-untyped-def]
        denied = require_auth()
        if denied is not None:
            return denied
        repo, settings, _ = _svc()
        try:
            await repo.delete(persona_id)
        except PersonaNotFound:
            return _err(f"人设不存在：{persona_id}", "not_found", 404)

        # 删掉的正好是当前人设 → 清空当前选择（不留悬空引用）
        if settings is not None and settings.active_persona_id == persona_id:
            await settings.set_active_persona("")

        return jsonify({"ok": True, "deleted": persona_id}), 200

    @bp.post("/personas/<persona_id>/duplicate")
    async def duplicate_persona(persona_id: str):  # type: ignore[no-untyped-def]
        denied = require_auth()
        if denied is not None:
            return denied
        repo, _, _ = _svc()

        body = await request.get_json(silent=True) or {}
        try:
            clone = await repo.duplicate(persona_id, new_name=str(body.get("name") or ""))
        except PersonaNotFound:
            return _err(f"人设不存在：{persona_id}", "not_found", 404)

        return jsonify(clone.to_dict()), 201

    @bp.post("/personas/<persona_id>/activate")
    async def activate_persona(persona_id: str):  # type: ignore[no-untyped-def]
        """设为"当前人设"（聊天时自动带上）。"""
        denied = require_auth()
        if denied is not None:
            return denied
        repo, settings, _ = _svc()
        if settings is None:
            return _err("设置服务未就绪", "server_error", 503)

        if not await repo.exists(persona_id):
            return _err(f"人设不存在：{persona_id}", "not_found", 404)

        await settings.set_active_persona(persona_id)
        return jsonify({"ok": True, "active_persona_id": persona_id}), 200

    @bp.post("/personas/deactivate")
    async def deactivate_persona():  # type: ignore[no-untyped-def]
        denied = require_auth()
        if denied is not None:
            return denied
        _, settings, _ = _svc()
        if settings is not None:
            await settings.set_active_persona("")
        return jsonify({"ok": True, "active_persona_id": ""}), 200

    # ==================================================================
    # 提示词预览（⭐ 情绪 A3：人设要能看"组成什么提示词"）
    # ==================================================================

    @bp.get("/personas/<persona_id>/preview")
    async def preview_prompt(persona_id: str):  # type: ignore[no-untyped-def]
        """预览这个人的 system prompt —— 让人设"看得见"。"""
        denied = require_auth()
        if denied is not None:
            return denied
        repo, _, _ = _svc()
        try:
            p = await repo.get(persona_id)
        except PersonaNotFound:
            return _err(f"人设不存在：{persona_id}", "not_found", 404)

        from ..domain import prompt as prompt_mod

        assembled = prompt_mod.assemble(p)
        return jsonify(
            {
                "persona_id": persona_id,
                "text": assembled.text,
                "sections": [s.to_dict() for s in assembled.sections],
                "describe": prompt_mod.describe(assembled),
            }
        ), 200

    # ==================================================================
    # 日志（面板「日志」板块）—— 情绪 2026-09-21 要求
    # ==================================================================

    @bp.get("/logs")
    async def get_logs():  # type: ignore[no-untyped-def]
        """最近的服务日志 → `{"status":"ok","data":{"items":[...],"total":N,...}}`。

        🔴 **数据源是内存环形缓冲**（`infra/logging_setup.RingBufferHandler`），
           不是读 `logs/server.log`。理由两条：
             ① 那个文件混着 Hypercorn 自己的输出，且轮转/权限不受我们控制；
             ② **脱敏**：文件里是已写出的字节，而内存缓冲能在出口走**同一条** `scrub()`
                → **不引入第二条可能泄漏凭据的通道**（本项目铁律）。

        Query:
            limit: 取多少条（默认 200，上限见 `LOG_BUFFER_SIZE`）
            level: 只看该级别及以上（`INFO`/`WARNING`/`ERROR`/`DEBUG`）

        ⚠️ 缓冲未初始化时**如实**返回 `available=false` + 空列表，
           **不假装"没有日志就是没出错"**（那是最误导人的一种失败）。

        🔴 **必须鉴权**（实测踩过）：我第一版漏了下面这道 `require_auth()`
           —— 未登录直接 `curl /api/v1/logs` 返回 **200**，而隔壁
           `/settings`、`/config-profile`、`/provider-sources` 都是 401。
           日志里有 logger 名、上游报错、插件路径等运行细节，
           **不该对外裸奔**（服务是绑 `0.0.0.0` 的，等于公网可读）。
        """
        denied = require_auth()
        if denied is not None:
            return denied

        from ..infra.logging_setup import get_log_buffer

        raw_limit = (request.args.get("limit") or "200").strip()
        try:
            limit = int(raw_limit)
        except ValueError:
            return jsonify({
                "status": "error",
                "error": {
                    "type": "invalid_limit",
                    "message": f"limit 必须是整数，收到 {raw_limit!r}",
                    "field": "limit",
                },
            }), 400
        if limit <= 0:
            return jsonify({
                "status": "error",
                "error": {
                    "type": "invalid_limit",
                    "message": f"limit 必须为正整数，收到 {limit}",
                    "field": "limit",
                },
            }), 400

        level = (request.args.get("level") or "").strip().upper()
        if level and level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            return jsonify({
                "status": "error",
                "error": {
                    "type": "invalid_level",
                    "message": f"level 取值非法：{level!r}",
                    "field": "level",
                },
            }), 400

        buf = get_log_buffer()
        if buf is None:
            # 🔴 如实说"日志不可用"，而不是回一个空列表让用户以为"服务很干净"
            return jsonify({
                "status": "ok",
                "data": {
                    "items": [],
                    "total": 0,
                    "available": False,
                    "reason": "日志缓冲未初始化（服务可能还没 setup_logging）",
                    "capacity": 0,
                },
            })

        items = buf.snapshot(limit=limit, level=level)
        return jsonify({
            "status": "ok",
            "data": {
                "items": items,
                "total": len(items),
                "available": True,
                "reason": "",
                "capacity": getattr(buf._buf, "maxlen", 0),  # noqa: SLF001
                "level": level,
            },
        })

    # ==================================================================
    # 运行时设置（情绪 A1 ②：业务配置）
    # ==================================================================

    @bp.get("/settings")
    async def get_settings():  # type: ignore[no-untyped-def]
        denied = require_auth()
        if denied is not None:
            return denied
        _, settings, _ = _svc()
        if settings is None:
            return _err("设置服务未就绪", "server_error", 503)
        return jsonify(
            {"settings": settings.public_view(), "writable_fields": list(WRITABLE_FIELDS)}
        ), 200

    @bp.patch("/settings")
    async def update_settings():  # type: ignore[no-untyped-def]
        """改业务配置（上游地址/模型/key/当前人设）。

        ⚠️ 不接受 `server.*` / `auth.*` / `storage.*` —— 那些是部署时的（情绪 A1 ③）。
        """
        denied = require_auth()
        if denied is not None:
            return denied
        _, settings, _ = _svc()
        if settings is None:
            return _err("设置服务未就绪", "server_error", 503)

        body = await request.get_json(silent=True)
        if not isinstance(body, dict):
            return _err("请求体必须是 JSON 对象", "invalid_request_error", 400)

        rejected = [k for k in body if k not in WRITABLE_FIELDS]
        view = await settings.update(body)
        resp: dict[str, Any] = {"settings": view}
        if rejected:
            # 明确告知哪些被忽略 —— 不静默吞掉
            resp["ignored_fields"] = rejected
            resp["note"] = "以上字段不可运行时修改（属部署配置）"
        return jsonify(resp), 200

    @bp.delete("/settings/overrides")
    async def clear_overrides():  # type: ignore[no-untyped-def]
        """把运行时覆盖清掉，恢复成环境变量默认。"""
        denied = require_auth()
        if denied is not None:
            return denied
        _, settings, _ = _svc()
        if settings is None:
            return _err("设置服务未就绪", "server_error", 503)

        fields = (await request.get_json(silent=True) or {}).get("fields")
        targets = fields if isinstance(fields, list) else list(WRITABLE_FIELDS)
        await settings.clear(*targets)
        return jsonify({"ok": True, "settings": settings.public_view()}), 200

    # ==================================================================
    # 配置文件（会话 + 上下文）—— 契约 §4.3
    # ==================================================================

    def _cfg_profile():  # type: ignore[no-untyped-def]
        """取配置文件 store；**路径可被 app.config 覆盖**（测试必须隔离到 tmp）。

        🔴 为什么路径要可覆盖：单测绝不能写进项目的 `data/`，
           更不能碰 `/opt/astrbot`（别人 AstrBot 的生产目录）。
           `test_providers_api` 对 providers 走的是同一套做法。
        """
        from quart import current_app

        from ..domain.config_profile import DEFAULT_PATH, ConfigProfileStore

        path = current_app.config.get("EMOTION_CONFIG_PROFILE_PATH") or DEFAULT_PATH
        return ConfigProfileStore(path)

    @bp.get("/config-profile")
    async def get_config_profile():  # type: ignore[no-untyped-def]
        """读配置 + **字段元数据**（前端照 `schema` 渲染，不在 JS 里再抄一份）。

        ⚠️ 形状照本蓝图既有约定（裸对象），**不是**供应商蓝图的 AstrBot 信封。
        """
        denied = require_auth()
        if denied is not None:
            return denied
        from ..domain.config_profile import schema

        store = _cfg_profile()
        return jsonify({"profile": store.load(), "schema": schema()}), 200

    @bp.patch("/config-profile")
    async def patch_config_profile():  # type: ignore[no-untyped-def]
        """部分更新（**只覆盖传进来的键**）。未知键/类型不符/越界 → **400 且点名**。

        body 例：`{"session": {"streaming_response": false}}`、
                `{"context": {"mode": "semantic"}}`、或两者一起。
        """
        denied = require_auth()
        if denied is not None:
            return denied

        from ..domain.config_profile import ConfigProfileError

        body = await request.get_json(silent=True)
        if not isinstance(body, dict):
            return _err("请求体必须是 JSON 对象", "bad_request", 400)
        if not body:
            return _err(
                "请求体是空的：至少给一段（session / context）",
                "bad_request", 400,
            )
        store = _cfg_profile()
        try:
            profile = store.patch(body)
        except ConfigProfileError as exc:
            # 🔴 必须点名是哪个字段（前端要把它显示在对应控件旁）
            return _err(str(exc), "invalid_config_profile", 400, field=exc.field_name)
        return jsonify({"profile": profile}), 200

    @bp.delete("/config-profile")
    async def reset_config_profile():  # type: ignore[no-untyped-def]
        """恢复默认（删掉落盘的覆盖）。"""
        denied = require_auth()
        if denied is not None:
            return denied
        store = _cfg_profile()
        return jsonify({"profile": store.reset()}), 200

    @bp.post("/settings/ping")
    async def ping_upstream():  # type: ignore[no-untyped-def]
        """★「**测试连接**」—— **真的打一次上游**，不是自检。

        ## 🔴 修的是什么（情绪 2026-10-09 报障）

        情绪：「点击『测试连接』竟然是**秒返回正常**，检查一下。」

        查实：前端那个按钮打的是 `fetch("/v1/models")` —— 而 `/v1/models`
        是**我们自己的** OpenAI 兼容端点（`app.py:465`，给爱语那种外部客户端用的），
        它**只回显当前配置的模型名、一个上游请求都不发**：

            model = _effective_model()          # 只读自己内存里的配置
            return jsonify({... "id": model ...})

        实测 `HTTP 200，耗时 0.0016 秒`（1.6 毫秒）。

        ⇒ 那个按钮测的是「**我们的进程还活着**」，与上游毫无关系：
          **key 全废、上游全断、域名解析不了，它照样秒回"连接正常"。**
          这类"判据测错了对象"比没有判据更糟 —— 它给出的是**假的安全感**。

        ## 为什么由服务端代打

        与 `/settings/models` 同款理由（**不重复实现，直接复用同一套解析与分类**）：
          1. **key 不能下发浏览器**（面板里 key 框是只写的）；
          2. 能带上**表单里还没保存**的 `base_url` / `api_key`
             （用户常是"改地址 → 测一下 → 再保存"，只读已保存值的话第一次填地址时按钮就是废的）。

        ## 与 `/settings/models` 的区别（刻意分开，别合并）

        * `models` 是**功能**：要拿到完整模型列表（前端拿去渲染勾选）。
        * `ping` 是**诊断**：只关心"通不通、哪一类不通"，成功时**少干活**
          （只回报条数与是否含当前配置的那个模型），失败时给出上游原话。

        ⚠️ 内置模型列表为空**不算失败**（与 `models` 的判据**故意不同**）：
           有些中转站确实不给 `/models`，但聊天端点照样能用。
           `models` 那里空列表要报错（用户明确要列表，给空就是没拿到东西）；
           而 `ping` 这里空列表只标注一句，**不据此判定不通**。

        Returns:
            200 `{ok: true, base_url, model, model_count, model_present, detail}`
            502 `{ok: false, error_type, detail}` —— 逐类说清是哪一种不通
        """
        denied = require_auth()
        if denied is not None:
            return denied
        _, settings, _ = _svc()
        if settings is None:
            return _err("设置服务未就绪", "server_error", 503)

        body = await request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            body = {}

        eff = settings.effective_upstream()
        base_url = str(body.get("base_url") or eff.get("base_url") or "").strip()
        api_key = str(body.get("api_key") or "").strip() or str(eff.get("api_key") or "")
        model = str(eff.get("model") or "").strip()
        # ⚠️ 地址为空的检查**必须排在 `_fail` 定义之后** —— 早期版本我把它写在
        #    上面，于是 `_fail` 还没绑定就调用 → `UnboundLocalError`。
        #    这是被 `tests/test_settings_ping.py::test_ping_empty_base_url_is_rejected`
        #    当场抓住的（判据先于产品发现问题，而不是反过来）。

        url = base_url.rstrip("/") + "/models"
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        import httpx

        from quart import current_app

        # 🔴 用**独立**的 config key 注入 transport，不与 `/settings/models` 共用：
        #    两者是不同能力，共用一个 key 会让"只想打桩其中一个"变成不可能。
        transport = current_app.config.get("EMOTION_PING_TRANSPORT")
        timeout = 15.0

        def _fail(message: str, error_type: str) -> tuple[Any, int]:
            """失败统一出口 —— **每条失败都必须能被用户看到**（不许静默）。"""
            log.warning(
                "测试上游连接失败：%s",
                message,
                extra={"data": {"url": url, "error_type": error_type}},
            )
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": {"message": message, "type": error_type},
                        "error_type": error_type,
                        "detail": message,
                        "base_url": base_url,
                    }
                ),
                502,
            )

        if not base_url:
            # ⚠️ 走 `_fail` 而不是 `_err`：本端点的**成功与失败共用同一个信封**
            #    （都带 `ok` 字段），前端一条判断分支就能处理完。
            #    混两种信封会让"地址为空"这条少一个 `ok`，前端得为它写特例。
            return _fail("上游 API 地址为空，无法测试连接", "invalid_api_base")

        try:
            async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
                resp = await client.get(url, headers=headers)
        except httpx.TimeoutException:
            return _fail(
                f"连接上游超时（{timeout:.0f}s）：{url} —— 检查地址是否可达/是否需要代理",
                "upstream_timeout",
            )
        except httpx.HTTPError as exc:
            return _fail(
                f"连不上上游：{type(exc).__name__}: {exc}", "upstream_unreachable"
            )

        if resp.status_code in (401, 403):
            return _fail(
                f"上游拒绝鉴权（HTTP {resp.status_code}）—— 检查 API Key 是否正确",
                "upstream_auth",
            )
        if resp.status_code >= 400:
            # 🔴 先过 scrub_text：中转站常把收到的 Authorization 头原样回显在错误体里
            detail = scrub_text((resp.text or "")[:200]).replace("\n", " ")
            return _fail(f"上游返回 HTTP {resp.status_code}：{detail}", "upstream_error")

        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            return _fail(
                "上游返回的不是 JSON —— 确认地址填的是 OpenAI 兼容端点（通常以 /v1 结尾）",
                "upstream_bad_payload",
            )

        names: list[str] = []
        raw = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("id"):
                    names.append(str(item["id"]))
                elif isinstance(item, str):
                    names.append(item)
        elif isinstance(payload, dict) and isinstance(payload.get("models"), list):
            for item in payload["models"]:
                if isinstance(item, dict) and item.get("id"):
                    names.append(str(item["id"]))
                elif isinstance(item, str):
                    names.append(item)

        present = (model in names) if model else None
        if names:
            if present:
                detail = f"上游可达，返回 {len(names)} 个模型，含当前配置的 {model}"
            elif model:
                detail = (
                    f"上游可达，返回 {len(names)} 个模型，但**不含当前配置的 {model}**"
                    " —— 地址/key 是通的，模型名可能不对"
                )
            else:
                detail = f"上游可达，返回 {len(names)} 个模型"
        else:
            detail = (
                "上游可达（HTTP 200），但它没有返回模型列表"
                " —— 地址/key 通，只是该站不支持 /models；这不影响聊天"
            )

        log.info(
            "测试上游连接成功",
            extra={"data": {"url": url, "count": len(names), "model_present": present}},
        )
        return (
            jsonify(
                {
                    "ok": True,
                    "base_url": base_url,
                    "model": model,
                    "model_count": len(names),
                    "model_present": present,
                    "detail": detail,
                }
            ),
            200,
        )

    @bp.post("/settings/models")
    async def list_upstream_models():  # type: ignore[no-untyped-def]
        """拉取**上游**的模型列表，供面板"获取模型"按钮使用。

        🔴 **为什么必须由服务端代拉，而不是前端直接打上游：**

          1. **API Key 不能下发到浏览器**。我们的 key 存在服务端（env 或运行时覆盖），
             面板里那个输入框是**只写**的（`public_view()` 只报"是否已设置"）。
             让前端直连上游就必须把 key 塞进页面，等于把凭据泄露给任何能打开面板的人。
          2. **能带上"还没保存"的表单值**。用户常常是「先改 API 地址 → 想拉模型看看
             → 再选模型 → 最后保存」。如果只能读**已保存**的配置，那第一次填地址时
             就拉不到模型，按钮等于没用。
             → 所以 body 里可传 `base_url` / `api_key`（未传则用当前生效值）。

        ⚠️ **失败必须说清原因**（本项目铁律）：上游 401/403/超时/返回非 JSON
           都要把**可行动的信息**回给用户，不能只丢一句"失败"。

        Returns:
            200 `{"models": [...], "source": {...}}`
            502 上游不可用（带 `detail` 说明是哪一类问题）
        """
        denied = require_auth()
        if denied is not None:
            return denied
        _, settings, _ = _svc()
        if settings is None:
            return _err("设置服务未就绪", "server_error", 503)

        body = await request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            body = {}

        eff = settings.effective_upstream()
        base_url = str(body.get("base_url") or eff.get("base_url") or "").strip()
        # 表单里的 key 优先；没填就用生效值（用户可能只想换地址、沿用同一把 key）
        api_key = str(body.get("api_key") or "").strip() or str(eff.get("api_key") or "")
        if not base_url:
            return _err("上游 API 地址为空，无法获取模型", "invalid_request_error", 400)

        url = base_url.rstrip("/") + "/models"
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        import httpx

        # 🔴 **transport 可注入**（测试用 `httpx.MockTransport` 打桩）。
        #    本函数直连上游，不打桩就没法在单测里验证"401 怎么说、超时怎么说" ——
        #    而**失败路径恰恰是最容易出静默 bug 的地方**（本项目铁律）。
        #    从 app.config 取，缺省为 None = 用 httpx 默认实现。
        from quart import current_app

        transport = current_app.config.get("EMOTION_MODELS_TRANSPORT")

        try:
            async with httpx.AsyncClient(timeout=20.0, transport=transport) as client:
                resp = await client.get(url, headers=headers)
        except httpx.TimeoutException:
            log.warning("获取模型列表超时", extra={"data": {"url": url}})
            return (
                jsonify(
                    {
                        "error": {
                            "message": f"连接上游超时（20s）：{url}",
                            "type": "upstream_timeout",
                        }
                    }
                ),
                502,
            )
        except httpx.HTTPError as exc:
            log.warning(
                "获取模型列表失败：%s", exc, extra={"data": {"url": url}}
            )
            return (
                jsonify(
                    {
                        "error": {
                            "message": f"连不上上游：{type(exc).__name__}: {exc}",
                            "type": "upstream_unreachable",
                        }
                    }
                ),
                502,
            )

        if resp.status_code in (401, 403):
            return (
                jsonify(
                    {
                        "error": {
                            "message": (
                                f"上游拒绝鉴权（HTTP {resp.status_code}）——"
                                "检查 API Key 是否正确/是否有权列出模型"
                            ),
                            "type": "upstream_auth",
                        }
                    }
                ),
                502,
            )
        if resp.status_code >= 400:
            return (
                jsonify(
                    {
                        "error": {
                            "message": f"上游返回 HTTP {resp.status_code}",
                            "type": "upstream_error",
                        }
                    }
                ),
                502,
            )

        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            return (
                jsonify(
                    {
                        "error": {
                            "message": "上游返回的不是 JSON —— 确认地址填的是 OpenAI 兼容端点（通常以 /v1 结尾）",
                            "type": "upstream_bad_payload",
                        }
                    }
                ),
                502,
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
            return (
                jsonify(
                    {
                        "error": {
                            "message": "上游返回了 200，但里面没有模型列表（data 为空）",
                            "type": "upstream_empty",
                        }
                    }
                ),
                502,
            )

        log.info(
            "已获取上游模型列表",
            extra={"data": {"url": url, "count": len(ordered)}},
        )
        return (
            jsonify(
                {
                    "models": ordered,
                    # 告诉前端"这次用的是哪套参数"，用户才知道拉的是哪个地址的结果
                    "source": {
                        "base_url": base_url,
                        "used_form_api_key": bool(str(body.get("api_key") or "").strip()),
                        "used_saved_api_key": not bool(str(body.get("api_key") or "").strip())
                        and bool(eff.get("api_key")),
                        "current_model": eff.get("model") or "",
                    },
                }
            ),
            200,
        )

    # ==================================================================
    # 人设导入 / 导出（SillyTavern 角色卡）
    # ==================================================================

    @bp.post("/personas/import")
    async def import_persona():  # type: ignore[no-untyped-def]
        """导入人设（**批量**）。

        格式依据是**爱语真 dex 实测**（见 `爱语/docs/PERSONA_IMPORT_FORMAT.md`）：
        爱语备份 `{"data":{"prompts":{...}}}` **一个文件可含多个人设**
        （`ty.h()` 遍历全部加载，Toast 是「共加载 N 个人设」）。

        两种入口（同一个解析器）：
          * `{"text": "...", "conflict": "rename"}` —— 粘贴 JSON / 数据 URL
          * 文件上传 —— 表单字段 `file`（PNG 或 JSON）

        `conflict` 照爱语 `ty.c()` 的策略：`overwrite` / `rename`（默认）/ `skip`。

        ⚠️ 解析失败要**说清原因与下一步**（400），不静默导入 0 个却说成功。
        """
        denied = require_auth()
        if denied is not None:
            return denied
        repo, _, _ = _svc()

        override: dict[str, Any] = {}
        conflict = CONFLICT_RENAME
        filename = ""

        files = await request.files
        upload = files.get("file") if files else None

        if upload is not None:
            filename = getattr(upload, "filename", "") or ""
            try:
                data = upload.read()
            except Exception as exc:  # noqa: BLE001
                return _err(f"读取上传文件失败：{exc}", "invalid_request_error", 400)
            if not isinstance(data, bytes):
                data = str(data).encode("utf-8")
            form = await request.form
            raw_override = form.get("override")
            if raw_override:
                try:
                    override = json.loads(raw_override)
                except json.JSONDecodeError:
                    override = {}
            conflict = (form.get("conflict") or conflict).strip() or conflict
            try:
                parsed = parse_card(data, filename=filename)
            except CardImportError as exc:
                return _err(str(exc), "invalid_request_error", 400)
        else:
            body = await request.get_json(silent=True) or {}
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                return _err(
                    "请提供人设数据：表单字段 `file`（PNG/JSON 文件）或 JSON 里的 `text`",
                    "invalid_request_error",
                    400,
                )
            ov = body.get("override")
            if isinstance(ov, dict):
                override = ov
            raw_conflict = body.get("conflict")
            if isinstance(raw_conflict, str) and raw_conflict.strip():
                conflict = raw_conflict.strip()
            try:
                parsed = parse_pasted_text(text)
            except CardImportError as exc:
                return _err(str(exc), "invalid_request_error", 400)

        if conflict not in CONFLICT_MODES:
            return _err(
                f"conflict 仅支持 {list(CONFLICT_MODES)}，当前 {conflict!r}",
                "invalid_request_error",
                400,
            )

        candidates = merge_import_sections(parsed, override)

        # PNG 卡自带立绘 → 只给第一个人设（一个 PNG 只可能有一张卡）
        avatar_name = ""
        if parsed.avatar_bytes:
            try:
                avatar_name = await _save_avatar(candidates[0].id, parsed.avatar_bytes)
            except Exception as exc:  # noqa: BLE001
                # ⚠️ 头像失败**不该让导入失败**（人设文本才是主体），
                #    但必须**留日志 + 告知用户**，绝不静默。
                log.warning("头像保存失败，人设仍导入", extra={"data": {"error": str(exc)}})
                parsed.notes.append(f"⚠️ 头像保存失败（人设已导入）：{exc}")

        existing = await repo.list()
        by_name = {p.name: p for p in existing}
        existing_names = set(by_name)

        created: list[dict[str, Any]] = []
        skipped: list[str] = []
        overwritten: list[str] = []

        for idx, persona in enumerate(candidates):
            if idx == 0 and avatar_name:
                persona.avatar_path = avatar_name

            same = by_name.get(persona.name)
            final = resolve_name_conflict(persona.name, existing_names, conflict)

            if final is None:
                skipped.append(persona.name)
                continue

            if same is not None and final == persona.name and conflict == CONFLICT_OVERWRITE:
                # 覆盖：更新已有那条（保留 id 与创建时间），而不是新建重复项
                try:
                    updated = await repo.update(
                        same.id, persona.to_dict(include_secrets=True)
                    )
                except (PersonaNotFound, ValueError, StorageError) as exc:
                    return _err(f"覆盖人设失败：{exc}", "invalid_request_error", 400)
                overwritten.append(updated.name)
                by_name[updated.name] = updated
                created.append(updated.to_dict())
                continue

            persona.name = final
            existing_names.add(final)
            try:
                made = await repo.create(persona)
            except (ValueError, StorageError) as exc:
                return _err(f"创建人设失败：{exc}", "invalid_request_error", 400)
            by_name[made.name] = made
            created.append(made.to_dict())

        if not created:
            return _err(
                "没有导入任何人设"
                + (f"（{len(skipped)} 个同名已跳过）" if skipped else ""),
                "invalid_request_error",
                400,
            )

        log.info(
            "人设已导入",
            extra={"data": {
                "format": parsed.source_format,
                "count": len(created),
                "overwritten": len(overwritten),
                "skipped": len(skipped),
                "conflict": conflict,
            }},
        )
        return (
            jsonify({
                "personas": created,
                "count": len(created),
                "source_format": parsed.source_format,
                "overwritten": overwritten,
                "skipped": skipped,
                "notes": parsed.notes,
                "extra_fields": parsed.extra_fields,
                "has_avatar": bool(avatar_name),
                "conflict": conflict,
            }),
            201,
        )

    @bp.get("/personas/export")
    async def export_all():  # type: ignore[no-untyped-def]
        """导出**全部**人设为爱语备份格式（`?format=aiyu`，默认）。

        照爱语 `ty.k()`：整份备份就是 `{"data":{"prompts":{...}}}`。
        ⚠️ 路由要排在 `/personas/<id>` **之前**，否则 `export` 会被当成 id。
        """
        denied = require_auth()
        if denied is not None:
            return denied
        repo, _, _ = _svc()
        personas = await repo.list()
        if not personas:
            return _err("还没有任何人设可导出", "invalid_request_error", 400)

        resp = jsonify(to_aiyu_backup(personas))
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="persona_backup_{int(time.time())}.json"'
        )
        return resp, 200

    @bp.get("/personas/<persona_id>/export")
    async def export_persona(persona_id: str):  # type: ignore[no-untyped-def]
        """导出单个人设。

        `?format=`：
          * `aiyu`（默认）—— **爱语备份格式**，给爱语用/做备份
          * `sillytavern`  —— SillyTavern V2，与角色卡生态互通（无损往返）

        ⚠️ 两种都不含 `api_key`。
        """
        denied = require_auth()
        if denied is not None:
            return denied
        repo, _, _ = _svc()
        try:
            p = await repo.get(persona_id)
        except PersonaNotFound:
            return _err(f"人设不存在：{persona_id}", "not_found", 404)

        fmt = (request.args.get("format") or "aiyu").strip().lower()
        if fmt not in ("aiyu", "sillytavern", "st"):
            return _err(
                f"format 仅支持 aiyu / sillytavern，当前 {fmt!r}",
                "invalid_request_error",
                400,
            )

        payload = to_aiyu_backup([p]) if fmt == "aiyu" else to_export_dict(p)
        resp = jsonify(payload)
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="{export_filename(p)}"'
        )
        return resp, 200

    @bp.get("/personas/<persona_id>/avatar")
    async def get_avatar(persona_id: str):  # type: ignore[no-untyped-def]
        """取人设头像。**需鉴权**（同生成图，属私有内容）。"""
        denied = require_auth()
        if denied is not None:
            return denied
        repo, _, _ = _svc()
        try:
            p = await repo.get(persona_id)
        except PersonaNotFound:
            return _err(f"人设不存在：{persona_id}", "not_found", 404)

        if not p.avatar_path:
            return _err("该人设没有头像", "not_found", 404)

        # 🔴 只允许头像目录下的**纯文件名**，防路径穿越
        name = Path(p.avatar_path).name
        if name != p.avatar_path or name.startswith("."):
            return _err("头像路径非法", "invalid_request_error", 400)
        if not (_avatar_dir() / name).is_file():
            return _err("头像文件不存在", "not_found", 404)
        return await send_from_directory(_avatar_dir(), name)

    # ==================================================================
    # 🆕 人设参考图（2026-09-25）—— 情绪「用户能填的就让用户改」
    # ==================================================================
    #
    # 🔴 为什么做成"上传"而不是"让用户填一个路径"：
    #   ① 手机 App 壳上**根本没有可填的文件系统路径**（那是后端机器的路径）；
    #   ② 让用户去改 `.env` = 要求他登服务器改配置再重启 —— 那不叫"用户能改"；
    #   ③ 人设是**一对多**的（一张卡一个角色）→ 参考图必须**跟着人设走**，
    #      全局一个值会让所有角色共用一张脸。
    #   → `.env` 的 `EMOTION_IMAGE_PERSONA_REFERENCE` 保留为**全局兜底**
    #     （只有一个角色时最省事），但不再是唯一入口。

    @bp.post("/personas/<persona_id>/reference")
    async def upload_persona_reference(persona_id: str):  # type: ignore[no-untyped-def]
        """上传某个人设的参考图。

        表单字段 `file`（png/jpg/jpeg/webp，≤ `MAX_PERSONA_REF_BYTES`）。

        ⚠️ **先校验再落盘**：格式/大小不对**不能**写进 `data/persona_refs/`
        （否则磁盘上留一堆永远用不到的文件，而面板显示"已配置"）。
        """
        denied = require_auth()
        if denied is not None:
            return denied
        repo, _, _ = _svc()

        try:
            await repo.get(persona_id)
        except PersonaNotFound:
            return _err(f"人设不存在：{persona_id}", "not_found", 404)

        files = await request.files
        upload = files.get("file") if files else None
        if upload is None:
            return _err(
                "请提供表单字段 `file`（PNG/JPEG/WebP 图片）",
                "invalid_request_error",
                400,
            )

        filename = getattr(upload, "filename", "") or ""
        suffix = Path(filename).suffix.lower()
        if suffix not in persona_ref_mod.ALLOWED_SUFFIXES:
            return _err(
                f"只支持 {list(persona_ref_mod.ALLOWED_SUFFIXES)}，"
                f"当前 {suffix or '(无扩展名)'}",
                "invalid_request_error",
                400,
            )

        try:
            data = upload.read()
        except Exception as exc:  # noqa: BLE001
            return _err(f"读取上传文件失败：{exc}", "invalid_request_error", 400)
        if not isinstance(data, bytes):
            data = str(data).encode("utf-8")
        if not data:
            return _err("上传的文件是空的", "invalid_request_error", 400)
        if len(data) > persona_ref_mod.MAX_PERSONA_REF_BYTES:
            return _err(
                f"参考图过大（{len(data)} > {persona_ref_mod.MAX_PERSONA_REF_BYTES}）",
                "invalid_request_error",
                400,
            )

        # 文件名 = `人设id` + 原扩展名：**一个人设只有一张**，
        # 重复上传直接覆盖（不累积垃圾文件）。用 id 不用名字 —— 名字可改、可重复。
        safe_id = persona_id
        if Path(safe_id).name != safe_id or safe_id.startswith("."):
            return _err("人设 id 非法", "invalid_request_error", 400)
        target = persona_ref_mod.ref_dir() / f"{safe_id}{suffix}"

        try:
            await asyncio.to_thread(target.write_bytes, data)
        except OSError as exc:
            # 🔴 写盘失败**必须响亮**（不能"看起来保存成功、其实没落地"）
            log.warning(
                "人设参考图落盘失败",
                extra={"data": {"persona": persona_id, "error": str(exc)}},
            )
            return _err(f"参考图保存失败：{exc}", "server_error", 500)

        # 换扩展名时要**清掉旧文件**，否则磁盘留垃圾、且与字段显示不一致
        old = str(getattr(await repo.get(persona_id), "reference_image_path", "") or "")
        if old and old != target.name and persona_ref_mod.is_safe_name(old):
            stale = persona_ref_mod.ref_dir() / old
            if stale.is_file():
                try:
                    await asyncio.to_thread(stale.unlink)
                    log.info(
                        "旧人设参考图已清理",
                        extra={"data": {"persona": persona_id, "file": stale.name}},
                    )
                except OSError as exc:  # noqa: BLE001
                    # 清理失败**不影响新图生效**（新名字已在字段里），但要留痕
                    log.warning(
                        "旧人设参考图清理失败",
                        extra={"data": {"file": stale.name, "error": str(exc)}},
                    )

        try:
            updated = await repo.update(
                persona_id, {"reference_image_path": target.name}
            )
        except (PersonaNotFound, ValueError, StorageError) as exc:
            return _err(f"保存参考图字段失败：{exc}", "invalid_request_error", 400)

        log.info(
            "人设参考图已上传",
            extra={"data": {
                "persona": persona_id,
                "bytes": len(data),
                "suffix": suffix,
            }},
        )
        return jsonify({"ok": True, "persona": updated.to_dict()}), 200

    @bp.get("/personas/<persona_id>/reference")
    async def get_persona_reference(persona_id: str):  # type: ignore[no-untyped-def]
        """取人设参考图。**需鉴权**（同头像，属私有内容）。"""
        denied = require_auth()
        if denied is not None:
            return denied
        repo, _, _ = _svc()
        try:
            p = await repo.get(persona_id)
        except PersonaNotFound:
            return _err(f"人设不存在：{persona_id}", "not_found", 404)

        name = str(getattr(p, "reference_image_path", "") or "")
        if not name:
            return _err("该人设没有参考图", "not_found", 404)
        # 🔴 只允许参考图目录下的**纯文件名**，防路径穿越
        if not persona_ref_mod.is_safe_name(name):
            return _err("参考图路径非法", "invalid_request_error", 400)
        if not (persona_ref_mod.ref_dir() / name).is_file():
            return _err("参考图文件不存在", "not_found", 404)
        return await send_from_directory(persona_ref_mod.ref_dir(), name)

    @bp.delete("/personas/<persona_id>/reference")
    async def delete_persona_reference(persona_id: str):  # type: ignore[no-untyped-def]
        """删除人设参考图（字段与文件一起清）。

        ⚠️ 即使文件已经不在，也**要把字段清掉** —— 否则面板会一直显示"已配置"
           而实际生图时读不到（那就是本项目最忌的"看着有、其实没有"）。
        """
        denied = require_auth()
        if denied is not None:
            return denied
        repo, _, _ = _svc()
        try:
            p = await repo.get(persona_id)
        except PersonaNotFound:
            return _err(f"人设不存在：{persona_id}", "not_found", 404)

        name = str(getattr(p, "reference_image_path", "") or "")
        removed = False
        if name and persona_ref_mod.is_safe_name(name):
            path = persona_ref_mod.ref_dir() / name
            if path.is_file():
                try:
                    await asyncio.to_thread(path.unlink)
                    removed = True
                except OSError as exc:  # noqa: BLE001
                    log.warning(
                        "人设参考图删除失败",
                        extra={"data": {"persona": persona_id, "error": str(exc)}},
                    )
                    return _err(f"参考图删除失败：{exc}", "server_error", 500)

        try:
            updated = await repo.update(persona_id, {"reference_image_path": ""})
        except (PersonaNotFound, ValueError, StorageError) as exc:
            return _err(f"清除参考图字段失败：{exc}", "invalid_request_error", 400)

        log.info(
            "人设参考图已删除",
            extra={"data": {"persona": persona_id, "file_removed": removed}},
        )
        return jsonify(
            {"ok": True, "file_removed": removed, "persona": updated.to_dict()}
        ), 200

    # ==================================================================
    # 提示词片段（人设内的 sections，单独管理便于面板编辑）
    # ==================================================================

    @bp.put("/personas/<persona_id>/sections")
    async def replace_sections(persona_id: str):  # type: ignore[no-untyped-def]
        """整体替换某人的提示词片段列表。"""
        denied = require_auth()
        if denied is not None:
            return denied
        repo, _, _ = _svc()

        body = await request.get_json(silent=True)
        if not isinstance(body, list):
            return _err("请求体必须是片段数组", "invalid_request_error", 400)

        try:
            sections = [PromptSection.from_dict(x) for x in body]
            for s in sections:
                s.validate()
        except (ValueError, KeyError, TypeError) as exc:
            return _err(f"片段格式错误：{exc}", "invalid_request_error", 400)

        try:
            updated = await repo.update(
                persona_id, {"prompt_sections": [s.to_dict() for s in sections]}
            )
        except PersonaNotFound:
            return _err(f"人设不存在：{persona_id}", "not_found", 404)

        return jsonify(updated.to_dict()), 200

    # ==================================================================
    # 会话（S3）
    # ==================================================================

    @bp.get("/sessions")
    async def list_sessions():  # type: ignore[no-untyped-def]
        denied = require_auth()
        if denied is not None:
            return denied
        repo = _sessions()
        if repo is None:
            return _err("会话服务未就绪", "server_error", 503)

        items = await repo.list()
        return jsonify(
            {"object": "list", "data": [_session_payload(s) for s in items]}
        ), 200

    @bp.post("/sessions")
    async def create_session():  # type: ignore[no-untyped-def]
        denied = require_auth()
        if denied is not None:
            return denied
        repo = _sessions()
        if repo is None:
            return _err("会话服务未就绪", "server_error", 503)

        body = await request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return _err("请求体必须是 JSON 对象", "invalid_request_error", 400)

        policy = None
        if isinstance(body.get("policy"), dict):
            try:
                policy = ContextPolicy.from_dict(body["policy"])
                policy.validate()
            except Exception as exc:  # noqa: BLE001
                return _err(f"policy 非法：{exc}", "invalid_request_error", 400)

        s = await repo.create(
            title=str(body.get("title") or ""),
            persona_id=str(body.get("persona_id") or ""),
            # 🔴 方案 B：会话可绑一个**插件用户 id**（= umo 第三段）。
            #    主动消息靠它找到"该落到哪个会话"，也是"哪个会话开主动消息"
            #    的粒度所在（插件按 user_id 存开关）。
            user_id=str(body.get("user_id") or ""),
            policy=policy,
        )
        return jsonify(_session_payload(s)), 201

    @bp.get("/sessions/<session_id>")
    async def get_session(session_id: str):  # type: ignore[no-untyped-def]
        denied = require_auth()
        if denied is not None:
            return denied
        repo = _sessions()
        try:
            s = await repo.get(session_id)
        except SessionNotFound:
            return _err(f"会话不存在：{session_id}", "not_found", 404)
        return jsonify(_session_payload(s)), 200

    @bp.get("/sessions/<session_id>/messages")
    async def get_session_messages(session_id: str):  # type: ignore[no-untyped-def]
        """会话消息。支持 `?limit=N` 只取最近 N 条。"""
        denied = require_auth()
        if denied is not None:
            return denied
        repo = _sessions()
        try:
            await repo.get(session_id)
        except SessionNotFound:
            return _err(f"会话不存在：{session_id}", "not_found", 404)

        all_msgs = await repo.messages(session_id)
        total = len(all_msgs)          # ⚠️ total 必须是**切之前**的总数

        raw_limit = request.args.get("limit", "")
        if raw_limit.isdigit():
            limit = max(0, int(raw_limit))
            all_msgs = all_msgs[-limit:] if limit else []

        return jsonify(
            {
                "object": "list",
                "data": [m.to_dict() for m in all_msgs],
                "total": total,                       # 会话里一共多少条
                "returned": len(all_msgs),            # 本次返回多少条
            }
        ), 200

    @bp.patch("/sessions/<session_id>")
    async def update_session(session_id: str):  # type: ignore[no-untyped-def]
        denied = require_auth()
        if denied is not None:
            return denied
        repo = _sessions()

        body = await request.get_json(silent=True)
        if not isinstance(body, dict):
            return _err("请求体必须是 JSON 对象", "invalid_request_error", 400)
        if "policy" in body and isinstance(body["policy"], dict):
            mode = body["policy"].get("mode")
            if mode is not None and mode not in VALID_MODES:
                return _err(
                    f"未知 mode：{mode!r}，允许 {VALID_MODES}", "invalid_request_error", 400
                )

        try:
            s = await repo.update(session_id, body)
        except SessionNotFound:
            return _err(f"会话不存在：{session_id}", "not_found", 404)
        except (ValueError, Exception) as exc:  # noqa: BLE001
            return _err(f"更新失败：{exc}", "invalid_request_error", 400)

        return jsonify(_session_payload(s)), 200

    @bp.delete("/sessions/<session_id>")
    async def delete_session(session_id: str):  # type: ignore[no-untyped-def]
        denied = require_auth()
        if denied is not None:
            return denied
        repo = _sessions()
        try:
            await repo.delete(session_id)
        except SessionNotFound:
            return _err(f"会话不存在：{session_id}", "not_found", 404)
        return jsonify({"ok": True, "deleted": session_id}), 200

    @bp.post("/sessions/<session_id>/clear")
    async def clear_session(session_id: str):  # type: ignore[no-untyped-def]
        """只清消息，保留会话（含策略与人设绑定）。"""
        denied = require_auth()
        if denied is not None:
            return denied
        repo = _sessions()
        try:
            await repo.clear_messages(session_id)
        except SessionNotFound:
            return _err(f"会话不存在：{session_id}", "not_found", 404)
        s = await repo.get(session_id)
        return jsonify({"ok": True, "session": _session_payload(s)}), 200

    @bp.post("/sessions/<session_id>/read")
    async def mark_session_read(session_id: str):  # type: ignore[no-untyped-def]
        """把该会话标记为**已读**（未读数清零）。

        🔴 谁调它：面板**真的打开了这个会话**时（`selectSession`）。
           为什么状态必须由服务端记而不是只在前端：未读要跨刷新/跨设备一致
           —— 手机上跳出来的红点，在电脑上打开该会话后也该消失。
        """
        denied = require_auth()
        if denied is not None:
            return denied
        repo = _sessions()
        if repo is None:
            return _err("会话服务未就绪", "server_error", 503)
        try:
            s = await repo.mark_read(session_id)
        except SessionNotFound:
            return _err(f"会话不存在：{session_id}", "not_found", 404)
        return jsonify({"ok": True, "session": _session_payload(s)}), 200

    @bp.post("/sessions/<session_id>/umo")
    async def regenerate_session_umo(session_id: str):  # type: ignore[no-untyped-def]
        """给该会话**换一个新的专属 umo 标签**（= 插件用户 id）。

        🔴 为什么要有这个按钮（而不是让用户自己编一个）：
           取值有三条硬约束（不能全数字 / 不能含冒号或空白 / 不能撞保留名
           `scheduler`），它们是**插件源码实测**出来的知识，
           不该指望用户背下来 —— 服务端一次生成到位。

        ⚠️ **这是"换库"级别的动作**：插件按这个值存记忆/关系/主动消息开关。
           换掉它，插件那边就是**另一个用户**（旧数据仍在旧标签下、不会丢，
           但这个会话之后写的东西全落在新标签下）。所以：
             * 只在用户**显式点击**时执行（不自动、不后台）；
             * 响应里如实带出 `previous`，让面板能显示"从 X 换成 Y"。
        """
        denied = require_auth()
        if denied is not None:
            return denied
        repo = _sessions()
        if repo is None:
            return _err("会话服务未就绪", "server_error", 503)
        try:
            previous = (await repo.get(session_id)).user_id
        except SessionNotFound:
            return _err(f"会话不存在：{session_id}", "not_found", 404)

        s = await repo.update(session_id, {"user_id": await repo.new_umo_user_id()})
        log.info(
            "会话换 umo 标签",
            extra={"data": {"session": session_id, "previous": previous, "now": s.user_id}},
        )
        return jsonify({"ok": True, "previous": previous, "session": _session_payload(s)}), 200

    @bp.get("/unread")
    async def unread_summary():  # type: ignore[no-untyped-def]
        """未读汇总（侧边栏「聊天」那个圆圈数字的来源）。

        形状：`{"total": N, "sessions": {<session_id>: <未读>}}`
        ⚠️ 只返回**未读 > 0** 的会话 —— 全量返回会让这个"只为显示一个数字"
           的接口随会话数线性变大。
        """
        denied = require_auth()
        if denied is not None:
            return denied
        repo = _sessions()
        if repo is None:
            return _err("会话服务未就绪", "server_error", 503)
        items = await repo.list()
        by_id = {s.id: s.unread for s in items if s.unread > 0}
        return jsonify({"total": sum(by_id.values()), "sessions": by_id}), 200

    @bp.post("/sessions/<session_id>/summarize")
    async def summarize_session(session_id: str):  # type: ignore[no-untyped-def]
        """手动触发一次总结（调试用；正常是后台自动做）。"""
        denied = require_auth()
        if denied is not None:
            return denied
        repo = _sessions()
        _, _, chat = _svc()
        if repo is None or chat is None or chat._summarizer is None:
            return _err("总结服务未就绪", "server_error", 503)

        try:
            session = await repo.get(session_id)
        except SessionNotFound:
            return _err(f"会话不存在：{session_id}", "not_found", 404)

        msgs = await repo.messages(session_id)
        # 🔴 **与后台总结走同一个解析器**（`_resolve_summary_target`）——
        #    这里原来写的是 `chat.resolve_target(None)`（**忽略会话**），
        #    那样手动总结与自动总结会**用不同的模型**，
        #    而用户看到的结果差异无从解释（"同一段对话，手动总结和自动总结不一样"）。
        #    ⚠️ 本项目「同一概念两条写入路径」的又一次预防：两处**必须同源**。
        target = chat.resolve_summary_target(session)
        body = await request.get_json(silent=True) or {}

        outcome = await chat._summarizer.maybe_summarize(
            session_id=session_id,
            messages=msgs,
            policy=session.policy,
            current_summary=session.summary,
            cursor=session.summarized_upto,
            base_url=target.base_url,
            api_key=target.api_key,
            timeout=target.timeout,
            # ⚠️ 同上：传上游真正的模型名，否则会用 Summarizer 的默认模型名
            model=target.model,
            force=bool(body.get("force", True)),
        )

        if outcome.ran and outcome.ok:
            await repo.update(session_id, {
                "summary": outcome.summary,
                "summarized_upto": outcome.cursor,
            })

        return jsonify({"ok": outcome.ok, "outcome": outcome.to_dict()}), 200

    @bp.get("/context-modes")
    async def list_context_modes():  # type: ignore[no-untyped-def]
        """可选上下文模式（给面板渲染下拉框）。"""
        denied = require_auth()
        if denied is not None:
            return denied
        return jsonify(
            {
                "modes": [
                    {"id": "recent_only", "label": "滚动上下文",
                     "hint": "固定窗口保留最近 N 条；记忆立即入库（爱语 key_scroll_context_enable）"},
                    {"id": "rolling_summary", "label": "动态上下文",
                     "hint": "累积到设定条数后总结旧消息，之后携带摘要 + 最近原文（key_dynamic_context_enable）"},
                    {"id": "semantic", "label": "混合模式",
                     "hint": "摘要 + 最近原文，并保持请求前缀稳定以提高缓存命中（key_mixed_context_enable）"},
                ]
            }
        ), 200

    return bp
