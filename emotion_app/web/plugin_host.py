# -*- coding: utf-8 -*-
"""把「AstrBot 插件宿主」接进情绪app 主服务（**可开关、可缺席**）。

## 三道边界（都是刻意设计的）

① **默认关闭**：`plugins.enabled=False` 时本模块**完全不 import `astrbot_host`**。
   理由：插件会执行任意代码 + 写自己的数据目录，属高信任能力；
   而 `emotion_app` 的 401 项测试、CI、只想聊天的部署**都不该被迫依赖它**。

② **依赖方向单向**：只允许 `emotion_app → astrbot_host`。
   `emotion_app/domain/` **绝不** import 插件宿主（照作者 §7「便携层不能依赖宿主」判据）。

③ **数据目录分离**：插件源码从 `plugin_dirs`（只读，可能是别人 AstrBot 的目录）读，
   但**数据写到我们的 `data_dir`** —— 绝不往 `/opt/astrbot/data/` 里写东西。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("emotion_app.plugins")


def ensure_host_on_path() -> None:
    """把仓库里的 `host/` 加入 `sys.path`，让 `import astrbot_host` 可用。

    ⚠️ `astrbot_host` 是**并列的交付物**（不在 `emotion_app` 包内），
       所以要显式加路径。做成函数而不是顶层 import，
       这样 `enabled=False` 时不会因为路径问题报错。

    🔴 **任何在 `start()` 之前 import `astrbot_host` 的地方都必须先调它**：
       `plugin_pages_web.mount_plugin_page_routes()` 在**挂路由阶段**（比
       `start()` 更早）就要 import —— 漏调会让整个服务起不来
       （实测 `ModuleNotFoundError: No module named 'astrbot_host'`）。
    """
    host_dir = Path(__file__).resolve().parents[2] / "host"
    if not host_dir.is_dir():
        raise RuntimeError(f"找不到插件宿主目录：{host_dir}")
    p = str(host_dir)
    if p not in sys.path:
        sys.path.insert(0, p)


#: 旧名保留（同包内已有调用点），新代码请用公开名。
_ensure_host_on_path = ensure_host_on_path


#: 接管型指令（插件主动终止、但没有同步产出）时的**如实**占位文案。
#: ⚠️ 刻意**不编造内容** —— 我们不知道插件异步回了什么，
#:    所以只说"已被接管"，不假装知道回复内容。
PLUGIN_TAKEOVER_PLACEHOLDER = "（该指令已由插件接管处理）"


def plugin_reply_text(result: Any) -> str | None:
    """判定**插件这轮有没有说话**；说了就返回文本，没说返回 `None`。

    🔴 这是集成层最关键的一个判定，两条规则缺一不可：

    1. **有产出**（`emitted` 里有非空文本）→ 用插件产出；
    2. **或者被接管**（`stopped=True`）→ 即使产出为空也算"插件说话了"。

    ⚠️ **规则 2 为什么必需**：实测 `陪伴` 这类接管型指令
       产出是**空字符串**（它走流式异步回，不走返回值），但 `stopped=True`。
       只看 `emitted` 会把它判成"没说话" → 再去调一次 LLM
       → **用户收到两条互相矛盾的回复**。

    ⚠️ **反过来更危险**：普通聊天（"你好呀"）派发后
       `emitted=[]` 且 `stopped=False` → 必须返回 `None`。
       这里若判错，**整个聊天功能会被吞掉**（最严重的一类回归）。

    Returns:
        插件回复文本；`None` = "插件没说话，请照常走 LLM"。
    """
    if result is None:
        return None
    texts = [str(x) for x in (getattr(result, "emitted", None) or []) if str(x).strip()]
    if texts:
        return "\n".join(texts)
    if bool(getattr(result, "stopped", False)):
        return PLUGIN_TAKEOVER_PLACEHOLDER
    return None


def _messages_to_provider_request(messages: Any, req_cls: Any) -> tuple[Any, list]:
    """我们的 `list[Message]` → AstrBot `ProviderRequest`。

    映射规则（照真实现语义）：
      * `role == "system"` 的消息 **合并进 `req.system_prompt`**
        （真实现的 system_prompt 就是系统提示词那一段）；
      * 其它消息进 `req.contexts`（OpenAI 格式，原样保留 role/content）。

    ⚠️ 多个 system 消息也合并（我们组装时会产出"人设"+"摘要"两条 system，
       见 `domain/chat.py` 的 `build_messages`）—— 用 `\n\n` 连接，
       保持它们在真实现里"同一段系统提示"的语义。

    Returns:
        `(req, contexts)` —— contexts 原样返回，便于转回时复用结构。
    """
    system_parts: list[str] = []
    contexts: list[dict] = []
    for m in messages or []:
        role = getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else "")
        content = getattr(m, "content", None)
        if content is None and isinstance(m, dict):
            content = m.get("content")
        if role == "system":
            if content:
                system_parts.append(str(content))
        else:
            contexts.append({"role": role, "content": content})
    req = req_cls(prompt=_last_user_text_from(contexts), contexts=contexts)
    req.system_prompt = "\n\n".join(system_parts)
    return req, contexts


def _last_user_text_from(contexts: list[dict]) -> str:
    """取最后一条 user 消息的纯文本（插件的指令/上下文判断要用）。"""
    for c in reversed(contexts or []):
        if c.get("role") != "user":
            continue
        content = c.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                str(x.get("text", ""))
                for x in content
                if isinstance(x, dict) and x.get("type") == "text"
            ]
            if parts:
                return "".join(parts)
    return ""


def _provider_request_to_messages(req: Any, contexts: list[dict]) -> list:
    """AstrBot `ProviderRequest` → 我们的 `list[Message]`（**带回插件注入的内容**）。

    🔴 **这个函数是"注入是否生效"的关键**：
       * `req.system_prompt` 被插件改过 → 作为**第一条 system 消息**放回去；
       * `req.contexts` 被插件改过（增删改）→ 用**插件版**（不是原来的）；
       * `req.extra_user_content_parts` 非空 → **追加为一条 system 消息**
         （真实现语义是"在用户消息之后追加的内容块"，我们宿主用 system 承载，
          因为它对模型的效果等价、且不污染 user 角色）。

    ⚠️ 我们自己构造 Message，不 import 领域层的类 ——
       这样插件宿主与领域层**零耦合**（只要对象有 `.role`/`.content` 就行，
       而 `Message` 正好是 dataclass，可直接构造）。
    """
    from emotion_app.domain.message import Message

    out: list[Any] = []
    system_prompt = getattr(req, "system_prompt", "") or ""
    if system_prompt.strip():
        out.append(Message(role="system", content=system_prompt))
    for c in getattr(req, "contexts", None) or []:
        if not isinstance(c, dict):
            continue
        out.append(Message(role=c.get("role", "user"), content=c.get("content")))
    extra = getattr(req, "extra_user_content_parts", None) or []
    if extra:
        texts = []
        for part in extra:
            if isinstance(part, dict):
                texts.append(str(part.get("text", "")))
            else:
                texts.append(str(getattr(part, "text", "") or ""))
        joined = "".join(t for t in texts if t)
        if joined.strip():
            out.append(Message(role="system", content=joined))
    return out


def _load_provider_config() -> dict:
    """读供应商配置（**容错**：坏了就按空处理，但记 ERROR）。

    🔴 **为什么放在这里而不是直接调 store**：
       存储实现（`emotion_app/infra/providers_store.py`）与宿主装配是**并行开发**的，
       这里只依赖一个**最小读取契约**（返回 `{"providers": [...], "default_provider_id": str}`）。
       存储模块一旦就绪就自动用上；没就绪时退回"读 JSON"，**不阻塞装配**。

    ⚠️ 三条都**不静默**：文件缺失=正常（首次使用）；JSON 坏=记 ERROR 并按空处理
       （项目铁律：静默失败比报错贵）。
    """
    import json

    # 优先用正式存储实现（它还有校验/原子写）
    try:
        from emotion_app.infra.providers_store import ProviderStore  # type: ignore

        store = ProviderStore()
        data = store.load()
        if isinstance(data, dict):
            return data
    except ImportError:
        pass          # 还没写好 → 走下面的直接读取
    except Exception:  # noqa: BLE001
        logger.error("ProviderStore 读取失败，退回直接读 JSON", exc_info=True)

    from pathlib import Path as _P

    path = _P("data/providers.json")
    if not path.is_file():
        return {"providers": [], "default_provider_id": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.error("供应商配置 %s 解析失败（按未配置处理）", path, exc_info=True)
        return {"providers": [], "default_provider_id": ""}
    if not isinstance(data, dict):
        logger.error("供应商配置 %s 不是对象（按未配置处理）", path)
        return {"providers": [], "default_provider_id": ""}
    return data


class PluginHostService:
    """插件宿主的**生命周期包装**（启动/停止/诊断）。

    单独一层的好处：web 层拿到的永远是这个小对象，
    不直接依赖 `astrbot_host` 的内部结构（将来宿主换实现不动 web 层）。
    """

    def __init__(self, config: Any) -> None:
        self.config = config
        self.host: Any = None
        self.report: Any = None
        #: 源码镜像结果（`None` = 没开镜像/还没启动）
        self.mirror: Any = None
        #: 取"领域层上游客户端"的回调（由装配层注入）。
        #: ⚠️ 用回调而不是直接持有对象：装配顺序上 backend 可能晚于宿主创建，
        #:    回调保证拿到的是**当时最新的那个**。
        self._backend_getter: Any = None
        #: 会话仓库 + 设置（**投递端口**要它们才能把主动消息写进会话）。
        #: ⚠️ 同样由装配层注入：宿主服务不自己去构造存储（那是装配层的职责）。
        self.sessions: Any = None
        self.settings: Any = None
        #: 🔴 人设仓库（**人格管理器**要它才能让插件读到人格）。
        #: ⚠️ 与 `sessions`/`settings` 同款：由装配层注入，缺席时如实降级。
        self.personas: Any = None
        #: 投递端口实例（`SessionDelivery`；诊断/测试可读）
        self.delivery: Any = None
        #: 🔴 平台注册表（装配时建好；`None` = 还没启动）。
        #: ⚠️ **缓存**而不是每次派发重建：注册表是"配了什么就是什么"的
        #:    静态事实，重建既浪费又会让"启动时的那个"与"派发时的那个"
        #:    有机会不一致（两个对象 != 同源）。
        self.platform_registry: Any = None

    def _load_global_config(self) -> dict:
        """读 AstrBot 全局配置（`cmd_config.json`）—— 插件的 `get_config()` 就靠它。

        P-N5（2026-09-22）：这里以前直接给 `{}`，导致插件读不到任何全局配置
        （20 处调用 / 12 个文件静默降级，见 `astrbot_config.load_global_config` 的表格）。

        ⚠️ **路径解析**：优先用显式配置 `plugins.global_config_path`
           （相对路径按 cwd 解析，与 `plugins.data_dir` 同一套约定）；
           留空则回落到兼容层的 `ASTRBOT_CONFIG_PATH`（照真实现约定）。

        ⚠️ **返回 plain `dict`**：插件多处 `config.get(...)`、且有 12 处按位置传 umo
           调 `get_config`；plain dict 是最保守的形状。
           ⚠️ 不是 `AstrBotConfig`：那个是 **插件自己的**配置对象
           （带 `save_config()`，落在 `data/plugins/config/`），**与全局配置不是一回事** ——
           混用会让插件的"保存配置"去改写全局配置。
        """
        from astrbot_host.compat.core.config.astrbot_config import load_global_config

        raw_path = str(getattr(self.config.plugins, "global_config_path", "") or "")
        if not raw_path:
            return load_global_config()
        path = Path(raw_path).resolve()
        if not path.is_file():
            logger.error(
                "AstrBot 全局配置不存在：%s（plugins.global_config_path=%r）—— "
                "插件的 get_config() 会拿到空字典，TTS/联网搜索/timezone/"
                "tool_call_timeout 将**静默降级**。请检查该路径。",
                path,
                raw_path,
            )
        else:
            logger.info("插件全局配置：%s", path)
        return load_global_config(path)

    async def start(self) -> Any:
        """加载插件（失败**不阻止主服务启动**，如实记日志）。"""
        _ensure_host_on_path()

        from astrbot_host.compat.core.star.context import HostPorts
        from astrbot_host.host_app import PluginHost
        from astrbot_host.compat.core.star.star_tools import set_data_root
        from astrbot_host.platform_manager import build_platform_manager

        # 🔴 数据目录指向**我们自己的**（可写），不是插件源码目录
        data_dir = Path(self.config.plugins.data_dir).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        set_data_root(data_dir)

        # 🔴🔴 **关键一步：先镜像源码，再从镜像加载。**
        #     否则插件会回写"源码目录"里的文件（实测 comfyui_pro 的
        #     `_conf_schema.json`），而那个目录很可能是**别人正在跑的
        #     AstrBot 生产目录** —— 我们的 data_dir 内容会覆盖它的配置。
        #     镜像之后，源码目录对我们的运行**只读**。
        #
        #     ⚠️ 镜像逻辑放在**加载器**里（`PluginHost(mirror_root=...)`），
        #        不在这里 —— 否则任何**直接用加载器**的调用方（如 `host/tests`）
        #        都会绕过它继续污染源目录（实测踩到：`pytest host/tests`
        #        每跑一次就改写一次生产配置，而 35 项测试全绿、毫无提示）。
        source_dirs = self.config.plugins.dirs()
        mirror_root = self.config.plugins.source_mirror_dir
        if not self.config.plugins.mirror_sources:
            logger.warning(
                "插件源码镜像已**关闭**（mirror_sources=false）：将直接从源码目录"
                "加载 %s —— 插件若回写自己的源码目录，会污染该目录！",
                source_dirs,
            )

        self.host = PluginHost(
            plugin_dirs=source_dirs,
            mirror_root=mirror_root if self.config.plugins.mirror_sources else None,
            auto_initialize=self.config.plugins.auto_initialize,
            initialize_timeout=self.config.plugins.initialize_timeout,
        )
        try:
            # 🔴 **投递端口**：让插件的主动消息真的能落到用户看得见的地方。
            #
            # ⚠️ 原来这里**故意传 None**（"等主动消息功能落地时再替换"）。
            #    后果是插件的两条投递路径全断：
            #      A `context.send_message` → `NotImplementedError`
            #      B `platform.send_by_session` → 如实返回 False
            #    → 陪伴的主动消息**一条也发不出来**。
            #
            # 🔴 **边界**：`SessionDelivery` 只写进**我们自己的会话存储**
            #    （面板上用户能看到），**不接任何外部推送**。
            #    要加推送是"再加一个消费者"，不是改这里。
            #
            # ⚠️ 会话仓库没注入时**如实降级**：端口为 None →
            #    `send_by_session` 返回 False（不伪造送达，插件的降级路径本来就有）。
            delivery = self._build_delivery()

            # 🔴 **平台管理器**：让插件能按 umo 找到我们的平台并投递
            #    （主动消息链的前提，见 `host/astrbot_host/platform_manager.py`）。
            #
            # 🔴🔴 **平台列表来自配置注册表**（情绪 2026-09-21：
            #    「别写死了，以后可能会加入 onebot 机器人」）：
            #    默认平台（`11`/`webchat`）永远在，配置可**追加**额外平台。
            #    ⚠️ 三项硬约束在 `PlatformSpec` 构造时就校验（配错 = 起不来），
            #       理由见 `host/astrbot_host/platforms.py` 的模块 docstring。
            registry = self._build_platform_registry()
            platform_manager = build_platform_manager(
                registry=registry, delivery=delivery
            )
            logger.info(
                "平台实例已装载：%s",
                ", ".join(
                    f"{s.display}[{s.id}/{s.kind}]" for s in registry.all()
                ),
            )

            # 🔴 **供应商管理器**：让插件能按 ID 选模型（陪伴有 20+ 个 `*_PROVIDER_ID`）。
            #    没有它 → `get_using_provider` 拿不到 → 记忆抽取/日程/关系分析
            #    **全部退化成模板兜底**（实测日志有这条 WARNING）。
            #
            # ⚠️ 后端从 `EMOTION_CHAT_BACKEND` 取（领域层的上游客户端）——
            #    与面板聊天**同一套上游配置**，不另起一个。
            provider_manager = self._build_providers()

            # 🔴🔴 **`llm=` 必须接上**（2026-09-20 修）。
            #
            # 实测生产日志里有 **38 条**：
            #   `[Token预算] LLM 调用失败: task=daily_plan|daily_review|diary
            #    error=宿主未接入模型调用能力（HostPorts.llm 为空）`
            # → 陪伴的**记忆抽取 / 每日计划 / 日记 / 每日回顾**全部跑不动。
            #
            # ⚠️ 根因是这里**从来没传过 `llm=`**（`HostPorts.llm` 默认 None），
            #    而 `Context.llm_generate` 原来第一步就检查它 → 必然抛错。
            #    现在 `llm_generate` 改走**供应商层**（照真实现），
            #    但本端口仍要接：①它是"没指定 provider 时"的兜底；
            #    ②`capabilities()["llm"]` 如实反映模型能力是否可用。
            #
            # ⚠️ 没配供应商时**如实返回 None**（不凭空造一个默认上游）——
            #    插件会看到"宿主没接模型"，而不是拿到一个假的能用的端口。
            llm_port = self._build_default_llm_port()

            # 🔴 **人格管理器**（2026-09-21 修情绪报障"陪伴获取不到平台默认人设"）。
            #
            # 之前 `HostPorts` **根本没有 `persona_manager`** →
            # `context.persona_manager` 恒为 None → 插件面板的
            # 「快速从已有人格导入」永远报
            # 「还没有读取到可用的主回复人格文本，请先让 Bot 触发一次对话或检查人格配置」。
            # ⚠️ 那句文案是**误诊**：缺这个属性时"触发对话"也不会写缓存
            #    （两个缓存写入口都在 `manager` 分支之后），所以只能靠补桥修好。
            persona_manager = await self._build_persona_manager()

            self.report = await self.host.start(
                ports=HostPorts(
                    # 🔴🔴 **全局配置必须真读进来**（P-N5，2026-09-22 修）。
                    #
                    #    这里原来写的是 `config={}` —— 于是插件的
                    #    `context.get_config(umo)` **恒返回空字典**，
                    #    20 处调用 / 12 个文件全部**静默降级**：
                    #      * `provider_settings.tool_call_timeout` → 静默回落 120s
                    #      * `provider_tts_settings.*`（11 键）→ TTS 判为"未配置"
                    #      * `timezone` → 静默回落 `Asia/Shanghai`
                    #      * `provider_settings.web_search*` → 联网搜索判为不可用
                    #    而且那句**假根因**「上游 base_url 为空」正是从这条链路报出来的。
                    #
                    #    ⚠️ `load_global_config` 内部用 `utf-8-sig` 读
                    #       （`cmd_config.json` **带 BOM**），且读不到会记 ERROR
                    #       —— 不静默给空字典（否则就是我们又造了一遍同类 bug）。
                    config=self._load_global_config(),
                    llm=llm_port,
                    # 🔴 两条投递路径都要接（插件不同调用点走不同路）：
                    #   * `delivery=` → `context.send_message`（路径 A）
                    #   * `platform_manager=` 里的端口 → `send_by_session`（路径 B）
                    #   只接一个 = 一半调用点仍走不通。
                    delivery=delivery,
                    platform_manager=platform_manager,
                    provider_manager=provider_manager,
                    persona_manager=persona_manager,
                )
            )
        except Exception as exc:  # noqa: BLE001
            # ⚠️ 插件装配失败**不该拖垮聊天服务** —— 如实记录并继续
            logger.error("插件宿主启动失败：%s: %s", type(exc).__name__, exc, exc_info=True)
            self.host = None
            self.report = None
            return None
        logger.info("插件宿主已接入\n%s", self.report.summary())
        return self.report

    async def stop(self) -> None:
        if self.host is not None:
            try:
                await self.host.stop()
            except Exception:  # noqa: BLE001
                logger.warning("插件宿主停止时出错", exc_info=True)

    def _build_providers(self) -> Any:
        """造供应商管理器（读配置 → 建 Provider → 给每个接上上游端口）。

        ⚠️ 三种降级都**如实**（不伪造）：
          * 没启用插件宿主 → 不建；
          * 没配任何供应商 → 返回**空管理器**（插件 `get_using_provider()` 得 None，
            走它自己的降级）；
          * 没有上游后端 → 供应商照建但 `llm=None`，
            调 `text_chat` 时会**抛错**（插件能看见，不是静默空回复）。
        """
        try:
            from emotion_app.web.provider_bridge import (
                build_provider_manager,
                flatten_two_layer,
            )
            from emotion_app.infra.providers_store import PROVIDER_TYPE_CHAT
        except Exception:  # noqa: BLE001
            logger.error("供应商装配模块导入失败（插件将拿不到模型）", exc_info=True)
            return None

        try:
            store = _load_provider_config()
        except Exception:  # noqa: BLE001
            logger.error("供应商配置读取失败（按未配置处理）", exc_info=True)
            store = {"providers": [], "default_provider_id": ""}

        backend = self._chat_backend_or_none()

        # 🔴 配置是**两层**（provider_sources 端点 + providers 模型，照 AstrBot 真实契约），
        #    而 AstrBot 的 `Provider` 是**一个模型一个实例** →
        #    必须展平：每个模型继承它所属源的 base_url / key / timeout。
        # 🔴🔴 **必须显式要 `PROVIDER_TYPE_CHAT`**（2026-09-21 修 N0）：
        #    原来不传 → 语音/嵌入模型**一起进了插件的对话供应商列表**，
        #    插件会把语音模型当对话模型调（实测 `items` 里混进 `tts-probe/…`）。
        flat, default_id = flatten_two_layer(store, provider_type=PROVIDER_TYPE_CHAT)

        pm = build_provider_manager(
            backend=backend,
            providers=flat,
            default_id=default_id,
        )
        logger.info(
            "供应商已装配：%d 个（默认=%s，后端=%s）",
            len(getattr(pm, "inst_map", {}) or {}),
            getattr(pm, "default_provider_id", "") or "无",
            type(backend).__name__ if backend else "无",
        )
        return pm

    def _chat_backend_or_none(self) -> Any:
        """取领域层的聊天后端（`EMOTION_CHAT_BACKEND`）；拿不到就 **None**。

        ⚠️ 用回调而不是直接持有对象：装配顺序上 backend 可能晚于宿主创建。
        """
        getter = self._backend_getter
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:  # noqa: BLE001
            logger.error("获取聊天后端失败（模型能力将不可用）", exc_info=True)
            return None

    def _build_platform_registry(self) -> Any:
        """造平台注册表（**默认平台永远在**，配置可追加额外平台）。

        🔴 为什么要有这一层（情绪 2026-09-21：「别写死了，以后可能会加入
           onebot 机器人，那边也可以获取 umo」）：

           以前"我们是哪个平台"写死在 `umo.py` 的三个常量上，加第二个平台
           要改多处、改一处漏一处 = **umo 写进孤儿会话**（静默数据分裂）。
           现在平台实例是 `PlatformSpec`（带校验），注册表是唯一事实源。

        ⚠️ **读配置失败不静默**：
           * `extra_platforms` JSON 非法 → `config.platform_specs()` 抛
             `ConfigError`（启动时就拦住，见 `PluginsConfig.validate`）；
           * 平台规格违反三条硬约束 → `PlatformSpecError`；
           这里再包一层是为了**打清楚是"平台装配"这一步失败**
           （裸的 JSONDecodeError 在现场离根因很远）。

        Returns:
            `astrbot_host.platforms.PlatformRegistry`（至少含默认平台）。
        """
        from astrbot_host.platforms import build_registry

        specs = self.config.plugins.platform_specs()
        registry = build_registry(specs)
        default_id = (self.config.plugins.default_platform_id or "").strip()
        if default_id:
            registry.set_default(default_id)
        # ⚠️ 缓存下来供 `dispatch_message` 用（同源，不重建）
        self.platform_registry = registry
        return registry

    def _build_default_llm_port(self) -> Any:
        """造 `HostPorts.llm` —— 「没指定供应商时」的兜底模型端口。

        🔴 为什么需要（2026-09-20 实测，修掉 38 条生产 ERROR）：

        `Context.llm_generate` 现在照真实现**先按 `chat_provider_id` 找供应商**，
        找不到才用默认供应商。两条路都要能走通：
          * 指定了 id → 走该 Provider 自己的端口；
          * **没指定** → 走这里给的端口（默认源）。

        ⚠️ 三种情况都**如实返回 None**，绝不凭空造一个能用的假端口：
          * 没有上游后端（聊天后端未装配）；
          * 一个供应商都没配；
          * 配置读不出来。
        插件拿到 None 会看到"宿主没接模型"，而不是"模型答了空话"。
        """
        backend = self._chat_backend_or_none()
        if backend is None:
            logger.warning(
                "未取到聊天后端 → `HostPorts.llm` 为 None（"
                "插件的模型任务会如实报『宿主未接入模型调用能力』）"
            )
            return None

        try:
            from emotion_app.web.provider_bridge import (
                UpstreamLLMPort,
                flatten_two_layer,
            )
            from emotion_app.infra.providers_store import PROVIDER_TYPE_CHAT

            store = _load_provider_config()
            # 🔴 `HostPorts.llm` 是**对话**端口 → 只要对话类模型（同 N0 的修法）。
            #    不传的话语音/嵌入模型会参与"挑默认"，可能把 llm 端口接到语音模型上。
            flat, default_id = flatten_two_layer(store, provider_type=PROVIDER_TYPE_CHAT)
        except Exception:  # noqa: BLE001
            logger.error("供应商配置读取失败（llm 端口不接）", exc_info=True)
            return None

        if not flat:
            logger.warning(
                "一个【对话】模型供应商都没配 → `HostPorts.llm` 为 None（如实降级）"
            )
            return None

        # 用**默认供应商**那条源的连接信息（与面板聊天同一套上游配置）
        chosen = next(
            (p for p in flat if str(p.get("id") or "") == default_id),
            flat[0],
        )
        try:
            return UpstreamLLMPort(
                backend=backend,
                model=str(chosen.get("model") or ""),
                base_url=str(chosen.get("base_url") or ""),
                api_key=str(chosen.get("api_key") or ""),
                timeout=float(chosen.get("timeout") or 60.0),
            )
        except Exception:  # noqa: BLE001
            logger.error("构造 llm 端口失败（按未接入处理）", exc_info=True)
            return None

    async def _build_persona_manager(self) -> Any:
        """造人格管理器（把**我们的人设库**接到 `context.persona_manager`）。

        🔴 为什么需要（2026-09-21 情绪报障，已实测复现）：

            插件面板「快速从已有人格导入」→ 点「生成导入草稿」永远报
            「还没有读取到可用的主回复人格文本，请先让 Bot 触发一次对话或检查人格配置」。

        根因不是"没触发过对话"，而是宿主**完全没有 `persona_manager`**。
        ⚠️ 那句报错文案是**误诊**（缺该属性时触发对话也修不好，见
           `astrbot_host/persona_manager.py` 模块头的完整链路）。
        实测证据：服务日志里那行
           `[主链] 跳过已删除人格对账: AstrBot 人格列表不可验证 reason=persona_manager_unavailable`

        ⚠️ **如实降级**：没有人设仓库（某些测试/未装配）时返回**未初始化**的
           管理器 —— 插件读不到人格，但**不会**误删它的档案
           （"空列表 + 双 None"是插件的"不可验证"安全闸门，见该模块头）。
           绝不凭空造一个人格（伪造能力，作者判据③不许）。
        """
        try:
            from astrbot_host.persona_manager import HostPersonaManager
        except Exception:  # noqa: BLE001
            logger.error("人格管理器模块导入失败（插件将读不到人格）", exc_info=True)
            return None

        mgr = HostPersonaManager(
            repo=self.personas,
            default_persona_id=self._default_persona_id(),
        )
        if self.personas is None:
            logger.warning(
                "没有人设仓库 → `context.persona_manager` 保持未初始化"
                "（插件读不到人格；面板「快速从已有人格导入」会如实报错）"
            )
            return mgr           # 仍给出去：让"能力存在但为空"可观测

        await mgr.refresh()
        if not mgr.initialized:
            # 已经打过 ERROR（见 `refresh()`），这里只补一句"后果是什么"
            logger.error("人格管理器未装载成功：插件将读不到任何人格")
        return mgr

    def _default_persona_id(self) -> str:
        """读「设置 → 配置文件 → 会话 → 默认采用的人格」（`default_persona_id`）。

        🔴 **这个字段以前是死配置**（全仓 0 个消费方）—— 现在它是
           `context.persona_manager.get_default_persona_v3()` 的来源，
           也就是"插件认为哪个是默认人格"。

        ⚠️ 读失败**如实回落空串**（= 用第一个人设）并记日志 ——
           不编造、不抛错：人格读不出来**不该**让整个插件宿主起不来。
        """
        try:
            from emotion_app.domain.config_profile import ConfigProfileStore

            profile = ConfigProfileStore().load()
            session = profile.get("session") or {}
            return str(session.get("default_persona_id") or "").strip()
        except Exception:  # noqa: BLE001
            logger.error(
                "读取 default_persona_id 失败（按未指定处理：插件将用第一个人设）",
                exc_info=True,
            )
            return ""

    def _build_delivery(self) -> Any:
        """造投递端口（把插件主动消息写进我们的会话）。

        🔴 为什么需要（2026-09-20）：原来 `delivery=` **一直传 None**，
           导致插件的两条投递路径全断（`send_message` 抛 `NotImplementedError`、
           `send_by_session` 返回 False）→ **主动消息一条也发不出来**。

        ⚠️ **如实降级**：没有会话仓库时不造端口（返回 None）——
           插件会走它自己的降级路径，而不是拿到一个假的"能投递"端口。
           这比"造个只会打日志的桩"诚实，也符合作者判据③。

        ⚠️ 本端口**只写我们的会话存储**，不接任何外部推送 ——
           那是最需要显式授权的动作，不该由宿主服务偷偷做。
        """
        if self.sessions is None:
            logger.warning(
                "没有会话仓库 → 投递端口不接（插件主动消息将如实失败；"
                "插件的降级路径本来就有）"
            )
            return None
        try:
            from emotion_app.web.delivery import SessionDelivery

            self.delivery = SessionDelivery(
                sessions=self.sessions, settings=self.settings
            )
            return self.delivery
        except Exception:  # noqa: BLE001
            logger.error("构造投递端口失败（按未接入处理）", exc_info=True)
            return None

    # ---- LLM 钩子（P0-4：让插件改"发给模型的提示词"） ----

    async def apply_llm_hooks(self, messages: Any) -> Any:
        """把**已组装好的消息列表**交给插件钩子，返回可能被改过的列表。

        适配层职责（把宿主的两套表示对接起来）：
          * 我们的 `messages` 是 `list[Message]`（role/content）；
          * AstrBot 的钩子要一个 `ProviderRequest`（`system_prompt` + `contexts` + …）。
        所以这里做**双向转换**：
          `list[Message]` → `ProviderRequest` → 跑钩子 → 再转回 `list[Message]`。

        🔴 **关键：插件的注入必须真的回到 messages 里**。
           真实现是把同一个 `ProviderRequest` 传给所有钩子、之后直接拿它请求模型；
           我们这里等价地：跑完钩子后**把 `req` 的最终状态转回去**。

        ⚠️ 失败一律**原样返回**（插件是可选增强），但要**记日志**（不许静默）。
        """
        if self.host is None:
            return None
        try:
            from astrbot_host.compat.core.provider.entities import ProviderRequest
            from astrbot_host.llm_hooks import run_llm_request_hooks
            from astrbot_host.umo import build_inbound_event
        except Exception:  # noqa: BLE001
            logger.error("LLM 钩子模块导入失败（跳过注入）", exc_info=True)
            return None

        try:
            req, contexts = _messages_to_provider_request(messages, ProviderRequest)
            uid = str(self.config.plugins.umo_user_id or "default")
            event = build_inbound_event(_last_user_text_from(contexts), uid)
            await run_llm_request_hooks(event, req)
            out = _provider_request_to_messages(req, contexts)
            return out
        except Exception:  # noqa: BLE001
            logger.error("LLM 钩子执行异常（本条按未注入处理）", exc_info=True)
            return None

    # ---- 事件派发（把"用户说了句话"交给插件） ----

    async def dispatch_message(
        self,
        text: str,
        *,
        user_id: str = "",
        group: bool = False,
        platform_id: str = "",
    ) -> Any:
        """把一条入站消息派发给插件 handler（**P0-2/P0-3 的接线处**）。

        这是"插件能在聊天里回话"真正生效的地方：
        聊天请求进来 → 造一个标准 `AstrMessageEvent` → 派发 → 看插件产出了什么。

        设计取舍（每条都有理由）：

        * **宿主没开/没起来 → 返回 None**（调用方据此走原有 LLM 链路）。
          插件是**可选增强**，它的缺席绝不能让聊天挂掉。
        * **派发本身抛错 → 记 ERROR 并返回 None**，同样不阻断聊天。
          但**必须响亮**（项目铁律：静默失败比报错贵）。
        * **不在这里投递**：投递由调用方决定（面板把文本回给用户）。
          派发器只负责"跑插件、收结果"。

        Args:
            text: 用户消息原文。
            user_id: 插件用户 id（umo 第三段）。空 → 回落全局 `umo_user_id`。
            group: 群聊（umo 第二段 + `message_obj.type` 同源切换）。
            platform_id: 🔴 **平台实例 id**（umo 第一段）。空 → 用**默认平台**。
                ⚠️ 取不到指定平台时**如实失败**（返回 None），
                不静默回落到默认平台 —— 那会把"另一个平台的消息"
                写进默认平台的用户数据（串号），是最难查的一类脏数据。

        Returns:
            `DispatchResult`；不可用时返回 `None`。
        """
        if self.host is None:
            return None
        try:
            from astrbot_host.dispatch import dispatch
            from astrbot_host.umo import build_inbound_event
        except Exception:  # noqa: BLE001
            logger.error("派发模块导入失败（宿主将不参与聊天）", exc_info=True)
            return None

        try:
            uid = user_id or str(self.config.plugins.umo_user_id or "default")
            spec = None
            if platform_id:
                from astrbot_host.platforms import PlatformSpecError

                # 复用装配时那份（`None` = 宿主没起来，那也不该走到这；
                # 兜底建一份保证行为正确，但记一条 WARNING 说明状态异常）
                registry = self.platform_registry
                if registry is None:
                    logger.warning(
                        "平台注册表尚未装配（派发时兜底重建一份）—— 这不该发生，"
                        "请检查宿主启动是否成功"
                    )
                    registry = self._build_platform_registry()
                try:
                    spec = registry.require(platform_id)
                except PlatformSpecError:
                    # 🔴 如实失败，**不回落默认平台**（那会串号）
                    logger.error(
                        "派发指定的平台 %r 未注册（本条消息按【无插件】处理）；"
                        "已注册：%s。**刻意不回落默认平台** —— "
                        "那会把这条消息写进默认平台的用户数据",
                        platform_id,
                        [s.id for s in registry.all()],
                    )
                    return None
            event = build_inbound_event(text, uid, group=group, platform=spec)
            result = await dispatch(event)
        except Exception:  # noqa: BLE001
            logger.error("插件事件派发失败（本条消息按【无插件】处理）", exc_info=True)
            return None

        if result.errors:
            # ⚠️ 不吞：插件挂了要能看见（但已经回给用户了，见 dispatch 的错误路径）
            logger.warning(
                "插件派发中有 %d 个 handler 抛错：%s",
                len(result.errors),
                "; ".join(f"{n}: {e}" for n, e in result.errors[:3]),
            )
        return result

    # ---- 诊断 ----

    def status(self) -> dict[str, Any]:
        """脱敏状态（供 `/api/v1/status` 展示）。"""
        if self.report is None:
            return {"running": False}
        return {
            "running": True,
            "loaded": self.report.loaded,
            "failed": [{"plugin": n, "error": e} for n, e in self.report.failed],
            "mounted_apis": self.report.mounted_count,
            "initialized": len(self.report.initialized),
            "init_failed": [{"plugin": n, "error": e} for n, e in self.report.init_failed],
            # 🔴 **必须暴露**：非空 = 有插件回写了插件源码目录。
            #    那是"只读共用"目录（可能就是别人的生产目录），
            #    以前这件事是**静默**的（靠人工 diff 才发现）。
            "source_writes": [
                {"path": w.path, "kind": w.kind, "detail": w.detail}
                for w in self.report.source_writes
            ],
            # 插件拓展页（面板的"插件"页用它渲染入口列表）
            "pages": [
                {
                    "plugin_id": p.plugin_id,
                    "page_name": p.page_name,
                    "title": p.title,
                    "url": f"/api/v1/plugins/{p.plugin_id}/pages/{p.page_name}",
                }
                for p in self.report.pages
            ],
        }
