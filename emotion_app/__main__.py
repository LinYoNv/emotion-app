# -*- coding: utf-8 -*-
"""入口：加载配置 → 校验 → 起服务。

🔴 **关键行为**：配置不合法（尤其"没有访问令牌"）时
**打印原因并以非零码退出**，绝不带病启动。

用法::

    export EMOTION_AUTH_ACCESS_TOKEN="$(python -m emotion_app token)"
    python -m emotion_app serve
"""
from __future__ import annotations

import asyncio
import sys

from .domain.chat import ChatService
from .domain.persona_repo import PersonaRepository
from .domain.session_repo import SessionRepository
from .domain.summarizer import Summarizer
from .domain.settings import SettingsService
import json
from pathlib import Path

from .domain.backends import OpenAIChatBackend, OpenAIImageBackend, build_dispatch
from .domain.image_gen import ImageClient, ImageTarget
from .domain.image_tool import ImageToolService, register_image_tool
from .domain.tools import ToolRegistry
from .infra.config import ConfigError, generate_token, load_config
from .infra.logging_setup import get_logger, setup_logging
from .storage.factory import build_store
from .web.app import create_app
from .web.auth import AuthManager

log = get_logger(__name__)

EXIT_CONFIG_ERROR = 2
EXIT_STORAGE_ERROR = 3


async def _serve() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        # 配置错误走 stderr —— 与 --json-only 之类的"纯输出"模式兼容
        print(f"❌ 配置错误，拒绝启动：\n{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    setup_logging(config.logging.level, json_output=config.logging.json_output)
    log.info("配置已加载", extra={"data": config.public_view()})

    # ⚠️ 绑定非回环地址 = 对整个网络开放。**必须显式告警**，不能静默。
    #    判据：日志里要能一眼看出"当前是对外开放的"以及缺什么防护。
    if config.server.is_public():
        warnings = []
        if not config.server.https_only:
            warnings.append("未启用 https_only（会话 cookie 不带 Secure，明文 http 下可被嗅探）")
        warnings.append("登录接口已做失败限流（10 次/5 分钟），但令牌仍是唯一屏障")
        log.warning(
            "服务绑定在非回环地址，已对网络开放",
            extra={"data": {
                "host": config.server.host,
                "port": config.server.port,
                "notes": warnings,
            }},
        )

    store = build_store(config.storage)
    try:
        await store.open()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 存储初始化失败：{exc}", file=sys.stderr)
        return EXIT_STORAGE_ERROR

    auth = AuthManager(
        access_token=config.auth.access_token,
        ttl_hours=config.auth.session_ttl_hours,
    )

    # 领域服务
    settings = SettingsService(store, config)
    await settings.load()
    personas = PersonaRepository(store)
    await personas._ensure()  # 预热，让"载入失败"在启动时就暴露
    sessions = SessionRepository(store)

    # 上游客户端：进程级复用连接池
    upstream = OpenAIChatBackend()
    await upstream.open()
    chat = ChatService(config, upstream, settings=settings, personas=personas)
    chat.bind_sessions(sessions)
    # ⭐ 总结器（S6b）：后台异步跑，**不阻塞回复**
    #    ⚠️ 实测踩到：写完模块与测试却**忘了在这里绑定** →
    #       自动总结完全没生效，而单测全绿（因为单测自己绑了）。
    #       判据：**新增能力必须有"装配测试"**，光测模块不够。
    chat.bind_summarizer(Summarizer(upstream, model=config.upstream.model))

    # ---- 工具（S6）：可替换的后端 + 可切换的下发方式 ----
    tools = ToolRegistry(max_calls_per_turn=1)   # ⚠️ 每轮最多一次（实测上游会重复调用）
    image_client = ImageClient()
    if config.image.enabled:
        await image_client.open()
    image_backend = OpenAIImageBackend(
        image_client,
        ImageTarget(
            base_url=config.image.effective_base(config.upstream.base_url),
            api_key=config.image.effective_key(config.upstream.api_key),
            model=config.image.model,
        ),
        default_size=config.image.size,
    )
    image_defaults = (
        json.loads(config.image.extra_params) if config.image.extra_params else None
    )
    image_service = ImageToolService(
        image_backend,
        output_dir=Path(config.image.output_dir),
        enabled=config.image.enabled,
        defaults=image_defaults,
        url_prefix="/api/v1/images",
    )
    register_image_tool(tools, image_service, enabled=config.image.enabled)

    dispatch = build_dispatch(config.image.dispatch)
    chat.bind_tools(tools, dispatch)
    log.info(
        "工具已装配",
        extra={"data": {
            "count": len(tools.specs()),
            "dispatch": dispatch.name,
            "image_enabled": config.image.enabled,
        }},
    )

    effective = settings.effective_upstream()
    if not effective.get("api_key"):
        log.warning(
            "未配置上游 api_key —— 可在面板「设置」里填，或由调用方在 Authorization 头提供"
        )

    app = create_app(
        config, store, auth, chat,
        personas=personas, settings=settings, sessions=sessions,
        # ⭐ 供应商要打到**同一个上游**，所以把 backend 也传进去
        #    （否则插件的模型调用没有出口 —— 会退化成模板兜底）
        chat_backend=upstream,
    )

    from hypercorn.asyncio import serve
    from hypercorn.config import Config as HypercornConfig

    hc = HypercornConfig()
    hc.bind = [f"{config.server.host}:{config.server.port}"]

    log.info(
        "开始监听",
        extra={"data": {"url": f"http://{config.server.host}:{config.server.port}"}},
    )
    try:
        await serve(app, hc)  # type: ignore[arg-type]
    finally:
        await upstream.close()
        await image_client.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "serve"

    if cmd == "token":
        print(generate_token())
        return 0

    if cmd == "check":
        # 只校验配置，不起服务 —— 便于部署前自检
        try:
            config = load_config()
        except ConfigError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        print("✅ 配置合法")
        print(f"  监听      {config.server.host}:{config.server.port}")
        print(f"  存储      {config.storage.backend} → {config.storage.path()}")
        print(f"  上游      {config.upstream.base_url} ({config.upstream.model})")
        return 0

    if cmd in ("serve", "run"):
        try:
            return asyncio.run(_serve())
        except KeyboardInterrupt:
            print("已中断", file=sys.stderr)
            return 130

    print(f"未知命令：{cmd}\n用法：python -m emotion_app [serve|check|token]", file=sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
