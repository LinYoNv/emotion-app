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
from .domain.subagents import SubagentService
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
        # 🔴 登录会话要落盘（2026-11-02）：否则**每次重启服务所有人都被踢下线**
        #    （浏览器还带着 cookie，而服务端已经不认识它 ⇒ 401 ⇒ 弹回登录框）。
        #    情绪原话：「说 24 小时但是我切出去干别的半小时就要重新刷新界面，
        #    填 token」—— cookie 的 Max-Age 一直是好的（生产实测 7 天），
        #    真凶是会话只活在内存字典里。
        store=store,
    )
    await auth.load()

    # 领域服务
    settings = SettingsService(store, config)
    await settings.load()
    # 🔴 子代理接入配置（情绪 2026-11-03：「平台本身不带 agent 能力，是子代理」）。
    #    ⚠️ 独立 section，不塞进 settings —— 换上游模型和换子代理平台是两件事。
    subagents = SubagentService(store)
    await subagents.load()
    # 🔴 **多配置文件注册表**（情绪 2026-11-04：「设置以配置文件分级，
    #    给会话选择用什么配置文件」）。对标 AstrBot 的 `abconf`。
    from .domain.config_profile import ConfigProfileRegistry

    config_profiles = ConfigProfileRegistry()
    personas = PersonaRepository(store)
    await personas._ensure()  # 预热，让"载入失败"在启动时就暴露

    # 🔴 会话仓库要能把「人设 id → name」翻译出来（2026-10-31）：
    #    改会话人设时会把值同步进 `plugin_conversations`（插件读那里），
    #    而 AstrBot 的规范是 **`persona_id` 就等于人设的 `name`**
    #    （`multi_controller.py:244` 校验 `native["name"] == persona_id`）。
    #    ⚠️ 必须是**同步**回调（它跑在 store 事务里）⇒ 读上面刚预热好的 `_cache`。
    def _persona_name_of(persona_id: str) -> str:
        pid = str(persona_id or "").strip()
        if not pid:
            return ""
        found = personas._cache.get(pid) if isinstance(personas._cache, dict) else None
        if found is not None:
            return str(getattr(found, "name", "") or "")
        for item in (personas._cache or {}).values():
            if str(getattr(item, "name", "") or "") == pid:
                return pid
        return ""

    sessions = SessionRepository(store, persona_name_of=_persona_name_of)

    # 上游客户端：进程级复用连接池
    upstream = OpenAIChatBackend()
    await upstream.open()
    chat = ChatService(config, upstream, settings=settings, personas=personas)
    # 🔴 回退模型从「会话绑定的那份配置文件」里读（不是会话字段）
    chat._profiles = config_profiles
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
        # 🆕 人设固定参考图：selfie 没给路径时自动带上（画风/长相才稳定）
        persona_reference=config.image.persona_reference,
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

    # 🔴 子代理：把配置服务和"应用器"挂进 app.config，API 与插件宿主都从这里取。
    #
    #    为什么应用器是回调而不是让 API 直接改注册表：
    #      * API 层不该知道宿主的内部对象（低耦合）；
    #      * 装配顺序上插件宿主可能晚于蓝图注册，回调保证拿到**当时最新的那个**。
    _install_subagents(app, subagents)
    app.config["EMOTION_CONFIG_PROFILES"] = config_profiles
    # 插件宿主也要它：`context.get_config(umo)` 按 umo 查会话绑定的那份配置
    # （对标 AstrBot `abconf`）。桥可能在 `create_app` 之后才建好，所以用回调
    # 兜一遍 —— 与 `_install_subagents` 里同一个理由。
    _bridge = app.config.get("EMOTION_PLUGIN_BRIDGE")
    if _bridge is not None:
        _bridge.config_profiles = config_profiles

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


def _install_subagents(app: Any, service: Any) -> None:
    """把子代理配置服务挂进 `app.config`，并告诉 API「怎么把配置应用到运行时」。

    🔴 **为什么要有 applier**（而不是让 API 自己改注册表）：
      * API 层不该知道宿主内部对象（低耦合）；
      * 装配顺序上插件宿主可能晚于蓝图注册 —— 回调保证拿到**当时最新的那个**。

    ⚠️ 宿主还没装配时，applier 会**如实返回失败**（API 会把它变成
       "配置已保存，但尚未生效"），**不假装成功**。本项目最忌的就是那个。
    """
    from astrbot_host.subagent import SubagentOrchestrator, SubagentRegistry
    from .web.subagent_backends import build_backend

    registry = SubagentRegistry()
    orchestrator = SubagentOrchestrator(registry)

    app.config["EMOTION_SUBAGENTS_SERVICE"] = service
    app.config["EMOTION_SUBAGENTS"] = registry
    app.config["EMOTION_SUBAGENTS_ORCHESTRATOR"] = orchestrator

    async def apply_config(target: Any) -> None:
        """把最新配置灌进注册表 + 后端，并刷新 `transfer_to_*` 工具表。"""
        connection = target.connection()
        # 总开关关掉时**显式回到 NullBackend**：用户关掉就该真的不派活，
        # 而不是留着上一次的后端继续偷偷工作。
        registry.set_backend(
            build_backend(connection) if target.config.enabled else None
        )
        orchestrator.reload_from_config(target.orchestrator_config())
        # 宿主那边的端口持有的是**同一个 registry 实例**，所以装配后自动生效。
        bridge = app.config.get("EMOTION_PLUGIN_BRIDGE")
        if bridge is not None:
            bridge.subagents = registry

    app.config["EMOTION_SUBAGENTS_APPLY"] = apply_config


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
