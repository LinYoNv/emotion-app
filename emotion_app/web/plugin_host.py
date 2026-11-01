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


def _component_type_name(comp: Any) -> str:
    """取消息段类型名（`"image"` / `"record"` / `"Plain"`…）。

    ⚠️ **三种取法按序**（宿主兼容层里 `type` 是 `ComponentType` 枚举，
       但插件也可能塞裸 dict 或自定义对象）：
       `comp.type.value` → `comp.type` → `type(comp).__name__`。
    """
    t = getattr(comp, "type", None)
    if t is None and isinstance(comp, dict):
        t = comp.get("type")
    if t is None:
        return type(comp).__name__
    return str(getattr(t, "value", t))


def plugin_reply_non_text(result: Any) -> list[dict[str, str]]:
    """列出插件回复里**不是文本**的部分（`[{"type": "image"}, …]`）。

    🔴 存在的唯一理由：**流式分支没有附件通道**（图片只能走非流式的
       `attachments`/`images` 字段，见 `web/app.py`）。而 `plugin_reply_text()`
       只返回文本 —— 于是"插件发了张图"在流式下就**没有下文**了。

    本项目最贵的一类 bug 就是这种"静默丢弃"（参考图裸 base64 事故：
    无日志、无报错，用户只能凭感觉报障）。所以：
      * 这里把"被降级掉的是什么"**如实列出来**（只列类型名，不搬运内容）；
      * 调用方（流式分支）据此打 `log.info`，非流式分支据此标进响应体。

    ⚠️ **判据只此一份**：别处不许再各写一套"怎么算非文本"
       （本项目「同一概念多条写入路径」的坑，已出现 3 次）。

    Returns:
        非文本段的列表；全是文本（或没有产出）时返回 `[]`。
    """
    if result is None:
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in getattr(result, "emitted", None) or []:
        # 大多数产出是 `MessageEventResult`（`MessageChain` 子类）；
        # 它也接受裸 list（插件的两种写法都有）。
        chain = getattr(item, "chain", None)
        if chain is None:
            chain = item if isinstance(item, (list, tuple)) else []
        for comp in chain:
            name = _component_type_name(comp)
            if name.lower() in ("plain", "text"):
                continue          # 文本 —— 正文那条通道已经带了
            if name in seen:
                continue
            seen.add(name)
            out.append({"type": name})
    return out


async def session_user_id(sessions: Any, session_id: str) -> str:
    """取该会话绑定的**插件用户 id**（方案 B）；取不到返回空串。

    🔴 **这是「会话 → 插件用户 id」的【唯一】判据**，两条路径共用它：
       * `web/app.py` 的**消息派发**（`_plugin_first_reply_payload` → `dispatch_message`）；
       * `web/plugin_host.py` 的**LLM 钩子**（`apply_llm_hooks`）。

    为什么必须共用一份（2026-09-26 修的真 bug）：钩子那边原来直接取
    `config.plugins.umo_user_id`（全局 `default`），于是**同一次聊天里**
    插件"建档时"用的是会话 A 的 user_id、"注入记忆/关系/被动状态时"
    用的是全局 `default` —— 两个用户的数据混在一起，
    而**两边各自都看起来正常**（正是本项目
    「同一概念有多条写入路径 → 判据要抽成一个函数」那类坑）。

    ⚠️ 取不到的每一种情况都返回空串（**不抛**），由调用方回落全局
       `plugins.umo_user_id`（**行为与以前一致**）：
       无 session_id / 无会话仓库 / 会话不存在 / 会话没绑。

    🔴 **但"存储坏了"不能混进"没绑"里静默处理**：前者回落会把这次聊天
       悄悄记到全局 `default` 名下（串进别人的记忆/关系），是最难查的一类脏数据。
       所以只有 `SessionNotFound` 才按"没绑"静默处理，其它异常**记 ERROR**。
    """
    sid = str(session_id or "").strip()
    if not sid or sessions is None:
        return ""
    try:
        session = await sessions.get(sid)
    except Exception as exc:  # noqa: BLE001 — 见下：只有"会话不存在"才是预期输入
        from emotion_app.domain.session_repo import SessionNotFound

        if not isinstance(exc, SessionNotFound):
            logger.error(
                "读取会话 %r 的插件用户 id 失败（本条按【未绑定】处理 → 会回落全局 id）",
                sid,
                exc_info=True,
            )
        return ""
    return str(getattr(session, "user_id", "") or "")


def fallback_user_id(config: Any) -> str:
    """取"没有会话绑定"时回落的插件用户 id（全局 `plugins.umo_user_id`）。

    ⚠️ 与 `session_user_id()` 是**同一件事的两半**（有绑定 → 会话的；没有 → 全局的），
       两处调用方都走这两个函数，不各写一份 `or "default"`。
    """
    plugins = getattr(config, "plugins", None)
    return str(getattr(plugins, "umo_user_id", "") or "default")


async def resolve_user_id(config: Any, sessions: Any, session_id: str) -> str:
    """**派发与钩子共用的"这次该用哪个插件用户 id"**。

    顺序：会话绑的 `Session.user_id` → 全局 `plugins.umo_user_id` → `"default"`。
    """
    uid = await session_user_id(sessions, session_id)
    return uid or fallback_user_id(config)


def _messages_to_provider_request(messages: Any, req_cls: Any) -> tuple[Any, list]:
    """保留系统提示、结构化历史与本轮 multipart，prompt 只提供文本视图。"""
    system_parts: list[str] = []
    contexts: list[dict] = []
    for message in messages or []:
        if isinstance(message, dict):
            from copy import deepcopy

            item = deepcopy(message)
        else:
            convert = getattr(message, "to_upstream", None)
            if callable(convert):
                item = convert()
                if item is None:
                    continue
            else:
                item = {"role": message.role, "content": message.content}
        if item.get("role") == "system":
            if item.get("content"):
                system_parts.append(str(item["content"]))
        else:
            contexts.append(item)
    req = req_cls(prompt=_last_user_text_from(contexts), contexts=contexts)
    req.system_prompt = "\n\n".join(system_parts)
    ensure_host_on_path()
    from astrbot_host.provider_content import content_media_locators

    current = next((item for item in reversed(contexts) if item.get("role") == "user"), None)
    if current is not None:
        req.image_urls, req.audio_urls = content_media_locators(current.get("content"))
    return req, contexts


def _last_user_text_from(contexts: list[dict]) -> str:
    """取最后一条 user 的文本视图，纯媒体本轮不能回退到历史文本。"""
    ensure_host_on_path()
    from astrbot_host.provider_content import content_text

    for context in reversed(contexts or []):
        if context.get("role") == "user":
            return content_text(context.get("content"))
    return ""


def _provider_request_to_messages(req: Any, contexts: list[dict]) -> list:
    """复用宿主媒体组装，回写当前 user 一次并保留真实协议字段。"""
    ensure_host_on_path()
    from astrbot_host.provider_content import provider_request_to_messages
    from emotion_app.domain.message import from_upstream_message

    request_contexts = getattr(req, "contexts", None) or []
    current_user = next((i for i in range(len(request_contexts) - 1, -1, -1)
                         if isinstance(request_contexts[i], dict)
                         and request_contexts[i].get("role") == "user"), None)
    return [from_upstream_message(item) for item in provider_request_to_messages(
        req, current_user_index=current_user,
    )]


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
        #: 🔴 存储后端（**对话历史端口**要它才能让插件存取自己的对话）。
        #: ⚠️ 与上面几个同款：由装配层注入；缺席时对话端口**如实不接**
        #:    （插件的主动消息存档会失败，但投递不受影响）。
        self.store: Any = None
        #: 对话历史端口实例（`StoreConversationManager`；诊断/测试可读）
        self.conversation_manager: Any = None
        #: 投递端口实例（`SessionDelivery`；诊断/测试可读）
        self.delivery: Any = None
        #: 🔴 平台注册表（装配时建好；`None` = 还没启动）。
        #: ⚠️ **缓存**而不是每次派发重建：注册表是"配了什么就是什么"的
        #:    静态事实，重建既浪费又会让"启动时的那个"与"派发时的那个"
        #:    有机会不一致（两个对象 != 同源）。
        self.platform_registry: Any = None
        #: 平台管理器（`OurPlatformManager`）。**外部适配器晚于它启动**，
        #: 所以这里要留引用，供 `attach_platforms()` 补装新平台。
        self.platform_manager: Any = None
        #: 🔴 **消息平台适配器运行时**（OneBot 等）—— 由装配层注入。
        #:
        #: ⚠️ `None` = 没有外部平台（只有内置 Web 端），行为与加这个能力**之前
        #:    逐字一致**。这是刻意的：不装插件的部署不该被迫依赖 `host/`。
        self.adapters: Any = None
        #: 🔴 **配置文件（`data/config_profile.json`）路径解析器** —— 由装配层注入。
        #: 与 `_backend_getter` 同款：路由装配那一刻 `app.config` 可能还没被测试
        #: 覆盖（`create_app` 之后才设 `EMOTION_CONFIG_PROFILE_PATH`），
        #: 所以用**回调延迟取值**；真实部署下回落 `DEFAULT_PATH`。
        self._config_profile_path: Any = None

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
            # 🔴 **停用名单从配置读出来传进去**（宿主侧不反向依赖本模块）。
            #    读不到 = 空名单（全部启用），那时行为与加这个能力**之前逐字一致**。
            inactivated=self.saved_inactivated(),
            # 🔴 **停用名单的写端口**（2026-10-31，卸载插件要用）：
            #    插件目录被移走后，名单里那条 module_path 就成了"对不上任何插件"
            #    的残留 —— 下次启动会报一条**指向已删插件**的假警告。
            #    宿主不许反过来 import 本模块（单向依赖铁律），所以从这里注入。
            inactivated_writer=self._forget_inactivated,
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
            # 留引用：后到的外部平台也要用**同一份**投递端口
            # （否则新装的平台能查到、却发不出消息 —— 又一类静默失败）
            self.delivery = delivery

            # 🔴 **平台管理器**：让插件能按 umo 找到我们的平台并投递
            #    （主动消息链的前提，见 `host/astrbot_host/platform_manager.py`）。
            #
            # 🔴🔴 **平台列表来自配置注册表**（情绪 2026-09-21：
            #    「别写死了，以后可能会加入 onebot 机器人」）：
            #    默认平台（`11`/`webchat`）永远在，配置可**追加**额外平台。
            #    ⚠️ 三项硬约束在 `PlatformSpec` 构造时就校验（配错 = 起不来），
            #       理由见 `host/astrbot_host/platforms.py` 的模块 docstring。
            registry = self._build_platform_registry()
            # 🔴 **把外部适配器的平台注册进注册表**（OneBot 等）。
            #    不注册的话：插件能收到 QQ 的消息（派发时靠事件自带的
            #    platform_meta），但**主动消息发不出去** ——
            #    `context.send_message` 按 `meta().id` 查表，查不到就静默失败。
            #    这是"三条链路通了两条"的典型，必须在这里补上。
            if self.adapters is not None:
                for spec in self.adapters.specs():
                    try:
                        registry.register(spec)
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "适配器平台 %s 注册失败：%s", getattr(spec, "id", "?"), exc
                        )

            platform_manager = build_platform_manager(
                registry=registry,
                delivery=delivery,
                # 🔴 按平台 id 注入外部发送器：有真实外部连接的平台
                #    （OneBot）走它，内置 Web 端返回 None → 走 delivery。
                sender_for=(
                    self.adapters.sender_for if self.adapters is not None else None
                ),
                # 🔴 协议端客户端（`Platform.get_client()` 的来源）。
                #    陪伴 7.0.0 的 `startup_readiness()` 靠它读 `_wsr_api_clients`
                #    判断"Bot 账号真的连上来了没"。不注入 ⇒ 插件的
                #    「开始使用」第二步（选模型）永远失败，且报的是
                #    `host_platform_unavailable`（与真实原因无关）。
                client_for=(
                    self.adapters.client_for if self.adapters is not None else None
                ),
            )
            logger.info(
                "平台实例已装载：%s",
                ", ".join(
                    f"{s.display}[{s.id}/{s.kind}]" for s in registry.all()
                ),
            )
            # 🔴 保存引用：外部适配器（OneBot）是在**这之后**才启动的，
            #    它们的平台要能补进这份管理器 —— 否则
            #    `context.get_platform_inst("onebot-napcat1")` 恒为 None
            #    （陪伴 7.0.0 的 `startup_readiness()` 因此恒失败）。
            #    见 `attach_platforms()`。
            self.platform_manager = platform_manager

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
            # 🔴 预热人设缓存：`_persona_name_of()` 要在**同步**事务里读它
            #    （把面板的 persona_id 翻译成插件认的 name，见那里的说明）。
            await self._warm_persona_cache()

            # 🔴 **让 ProviderManager 能读到人格**（2026-10-19 补）。
            #
            # 真 AstrBot 的 `ProviderManager.personas` 是 **property**，
            # 转发给 `persona_mgr.personas_v3`（`core/provider/manager.py:136-138`）。
            # 我们两个管理器各自独立构造，不接线的话
            # `context.provider_manager.personas` 永远是空列表
            # ⇒ 插件拿不到人格（实测 `meme_manager/main.py:821` 直接 AttributeError）。
            #
            # ⚠️ 顺序上必须在 `persona_manager` 建好**之后**接线
            #    （`provider_manager` 在 `:490` 就建了）。
            if provider_manager is not None and persona_manager is not None:
                provider_manager.persona_manager = persona_manager

            # 🔴 **对话历史端口**（2026-09-27 第八轮续，情绪明确要求修）。
            #
            # 之前 `conversation_manager=` **从来没传过** →
            # `context.conversation_manager` 抛 NotImplementedError →
            # 插件每轮主动消息都报：
            #   「主动消息主链生成失败: 宿主未接入会话历史能力」
            #   「主动消息写入会话历史失败: 同上」
            # 后果：消息**发得出去**（有兜底），但插件**存不下自己刚说过的话**
            # → 下一轮读不到上文 → **可能重复话题**。
            #
            # ⚠️ 根因不是"忘了传"，是**契约写错了**：原来的
            #    `ConversationManagerPort` 只有 `get_history`/`append`，
            #    而插件真正调的是 `get_curr_conversation_id`/`get_conversation`/
            #    `new_conversation`/`update_conversation`/`add_message_pair`
            #    （15 个调用点，真源码 `core/conversation_mgr.py`）。
            #    契约错 = 谁也没法实现它。已按真实现重写 + 本端口落地。
            conversation_manager = self._build_conversation_manager()

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
                    conversation_manager=conversation_manager,
                )
            )
        except Exception as exc:  # noqa: BLE001
            # ⚠️ 插件装配失败**不该拖垮聊天服务** —— 如实记录并继续
            logger.error("插件宿主启动失败：%s: %s", type(exc).__name__, exc, exc_info=True)
            self.host = None
            self.report = None
            return None
        logger.info("插件宿主已接入\n%s", self.report.summary())
        # 🔴 **能力自检（启动时打一次）** —— 2026-09-27 第八轮续补。
        #
        # 为什么必须有：`HostPorts.capabilities()` 早就能如实列出"哪项能力没接"
        # （`context.py:207`），但**生产启动路径从来没调用过它** ⇒
        # 这个"可观测"机制**形同虚设**，缺口只能靠翻插件日志才发现。
        #
        # 实证：`conversation_manager` 缺了**好几轮**才被情绪从截图里翻出来
        # —— 插件自己在日志里刷的是「宿主未接入会话历史能力」，
        # 那是**插件视角**的报错，我们的启动日志里一个字都没有。
        #
        # ⚠️ 缺能力**只警告不中断**（照作者判据：能力不对等要如实 degraded，
        #    不该让整个宿主起不来）。
        self._log_capability_selfcheck()
        return self.report

    def _log_capability_selfcheck(self) -> None:
        """把 `HostPorts.capabilities()` 的结果打进启动日志（缺什么一眼可见）。"""
        host = self.host
        ctx = getattr(host, "context", None) if host is not None else None
        ports = getattr(ctx, "_ports", None)
        if ports is None or not hasattr(ports, "capabilities"):
            # 拿不到就如实说"没能自检"，**不假装检查过了**
            logger.warning("能力自检跳过：拿不到 HostPorts（诊断信息缺失）")
            return
        try:
            caps = ports.capabilities()
        except Exception:  # noqa: BLE001
            logger.error("能力自检失败", exc_info=True)
            return

        missing = sorted(k for k, v in caps.items() if not v)
        present = sorted(k for k, v in caps.items() if v)
        logger.info(
            "宿主能力自检：可用 %d 项 %s%s",
            len(present),
            present,
            f" · 缺失 {len(missing)} 项 {missing}" if missing else " · 无缺失",
        )
        if missing:
            # ⚠️ 缺失项**逐条列出**（不是只给个数）—— 个数看不出是哪个能力
            logger.warning(
                "以下宿主能力未接入（插件相关功能会**如实降级**，不是静默失败）：%s",
                "、".join(missing),
            )

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

    def attach_platforms(self, specs: list[Any]) -> list[str]:
        """把**后到的**平台补进已存在的 `platform_manager`（返回新装上的 id）。

        🔴🔴 **为什么必须有这个**（2026-10-29 修的真 bug）：

        时序是这样的：

        ```
        PluginHost.start()
          └─ build_platform_manager(registry=当时的注册表)   # ← 只有 Web 端
          └─ 交给 HostPorts(platform_manager=...)            # ← 之后不再更新
        app._start_adapters()
          └─ await runtime.reload()        # ← 适配器这时才启动
          └─ runtime.specs()               # ← 现在才有 onebot-napcat1/2
        ```

        `platform_manager.__init__` 里 `self.platform_insts = list(insts or [])`
        —— 是**一份固定列表**。所以补注册到 `registry` 对它**毫无影响**。

        实测后果（2026-10-29 15:02）：

        ```
        平台实例已装载：情绪app Web 端[11/webchat]          ← 只有 1 个
        [DIAG] readiness pid='onebot-napcat1' platform=None  ← 插件查不到
        ```

        ⇒ 陪伴 7.0.0 的 `startup_readiness()` 恒返回
        `host_platform_unavailable` ⇒ 「开始使用」第二步（选模型）**永远失败**，
        而面板上显示的是「共享宿主配置、原生路由或权限无法校验」
        —— 和真实原因**毫无关系**，是本项目最贵的那类静默 bug。

        Args:
            specs: 新出现的平台规格（来自 `AdapterRuntime.specs()`）。

        Returns:
            本次真正新装上的平台 id 列表（**已存在的不重复装**）。
            幂等：重复调用不会产生重复实例。
        """
        pm = self.platform_manager
        if pm is None or not specs:
            return []

        from astrbot_host.platform_manager import OurPlatform

        insts = getattr(pm, "platform_insts", None)
        if not isinstance(insts, list):
            logger.error(
                "platform_manager 没有 platform_insts 列表，外部平台无法接入"
                "（插件将看不到这些平台）"
            )
            return []

        existing: set[str] = set()
        for inst in insts:
            try:
                existing.add(str(getattr(inst.meta(), "id", "") or ""))
            except Exception:  # noqa: BLE001
                continue

        adapters = getattr(self, "adapters", None)
        added: list[str] = []
        for spec in specs:
            sid = str(getattr(spec, "id", "") or "")
            if not sid or sid in existing:
                continue
            # 🔑 关键：这两个 getter 必须与 `build_platform_manager` 里
            #    用的是**同一份**，否则新装的平台能"被查到"但发不出消息。
            sender = adapters.sender_for(sid) if adapters is not None else None
            client_getter = (
                (lambda s=sid: adapters.client_for(s)) if adapters is not None else None
            )
            insts.append(
                OurPlatform(
                    spec=spec,
                    delivery=self.delivery,
                    sender=sender,
                    client_getter=client_getter,
                )
            )
            existing.add(sid)
            added.append(sid)
        return added

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

    def _persona_name_of(self, persona_id: str) -> str:
        """人设 id → **名字**（AstrBot 的 `persona_id` 规范就是 name）。

        🔴 为什么需要翻译（2026-10-31）：
           面板存的是 UUID（`8981024ac6e8…`），而 AstrBot 与陪伴插件
           认的是**人设名**（`_persona_to_v3`：「`name` 是插件的主键」；
           `multi_controller.py:244` 还会校验 `native["name"] == 绑定的 persona_id`）。
           直接把 UUID 写进 `plugin_conversations.persona_id`，插件必然
           抛 `host_persona_unavailable` —— **看起来存了，其实用不上**。

        ⚠️ **必须是同步的**：它从 `ConversationManager._project()` 里被调，
           而那里正处在 store 的 `update_sections` 事务回调中（同步函数）。
           所以读 `PersonaRepo._cache` 这份**已经载入好的内存快照**，
           不发请求、不 await。
           ⇒ 前提是装配时先 `await personas.list()` 预热过一次
             （见 `start()` 里 `_warm_persona_cache()`）。

        Returns:
            人设的名字；查不到返回**空串**（调用方按"没有"处理，不猜）。
        """
        pid = str(persona_id or "").strip()
        if not pid:
            return ""
        repo = self.personas
        cache = getattr(repo, "_cache", None) if repo is not None else None
        if not isinstance(cache, dict):
            # 还没预热 —— 如实返回空，**不在这里偷偷 await**（也不猜名字）
            logger.debug(
                "人设缓存未预热，persona_id=%s 无法翻译成 name（本轮不写）", pid
            )
            return ""
        found = cache.get(pid)
        if found is not None:
            return str(getattr(found, "name", "") or "")
        # 也接受"传进来的本身就是 name"（插件配置里可能存的是 name）
        for persona in cache.values():
            if str(getattr(persona, "name", "") or "") == pid:
                return pid
        return ""

    async def _warm_persona_cache(self) -> None:
        """预热人设缓存（`PersonaRepo._cache`），供 `_persona_name_of` 同步读取。

        ⚠️ 失败**不致命**：预热不了就退回"不写 persona_id"（如实），
           不能让整个插件宿主起不来。
        """
        repo = self.personas
        if repo is None:
            return
        try:
            await repo.list()
        except Exception:  # noqa: BLE001
            logger.warning("预热人设缓存失败（persona_id 将无法翻译成 name）", exc_info=True)

    def _build_conversation_manager(self) -> Any:
        """造对话历史端口（让插件能存/取自己那份对话历史）。

        🔴 为什么需要（2026-09-27 第八轮续，情绪要求）：见 `start()` 里的注释 —— 
           插件每轮主动消息都因为缺这个端口而**存不下自己说过的话**，
           下一轮读不到上文 ⇒ **可能重复话题**。

        ⚠️ **如实降级**（照 `_build_delivery` 同款）：没有 store 时不造端口
           （返回 None）—— `context.conversation_manager` 会**显式抛错**，
           插件走它自己的降级路径。这比造一个只会打日志的桩诚实得多
           （本项目最忌"看起来能用其实没用"）。
        """
        store = getattr(self, "store", None)
        if store is None:
            logger.warning(
                "没有 store → 对话历史端口不接（插件的主动消息存档将如实失败；"
                "它自己的降级路径本来就有）"
            )
            return None
        try:
            from emotion_app.domain.conversation_manager import (
                StoreConversationManager,
            )

            self.conversation_manager = StoreConversationManager(
                store, persona_name_of=self._persona_name_of
            )
            return self.conversation_manager
        except Exception:  # noqa: BLE001
            logger.error("构造对话历史端口失败（按未接入处理）", exc_info=True)
            return None

    # ---- LLM 钩子（P0-4：让插件改"发给模型的提示词"） ----

    async def apply_llm_hooks(
        self, messages: Any, *, session_id: str = "", event: Any = None
    ) -> Any:
        """把**已组装好的消息列表**交给插件钩子，返回可能被改过的列表。

        适配层职责（把宿主的两套表示对接起来）：
          * 我们的 `messages` 是 `list[Message]`（role/content）；
          * AstrBot 的钩子要一个 `ProviderRequest`（`system_prompt` + `contexts` + …）。
        所以这里做**双向转换**：
          `list[Message]` → `ProviderRequest` → 跑钩子 → 再转回 `list[Message]`。

        🔴 **关键：插件的注入必须真的回到 messages 里**。
           真实现是把同一个 `ProviderRequest` 传给所有钩子、之后直接拿它请求模型；
           我们这里等价地：跑完钩子后**把 `req` 的最终状态转回去**。

        🔴 **`session_id` 决定钩子看到的是【哪个用户】**（2026-09-26 修的真 bug）：
           钩子拿到的 `event.unified_msg_origin` 就是 umo，而 umo 第三段 =
           插件 `data["users"]` 的键。原来这里恒取全局 `plugins.umo_user_id`
           → **会话 A 聊天时，插件的记忆/关系/被动状态按 `default` 那个用户算**
           （与派发路径用的会话 user_id **不是同一个**）→ 串台。
           ⚠️ 判据在 `session_user_id()`（与派发路径**同一份**，不许各写一套）。

        ⚠️ 传空 `session_id`（无状态单轮 / 旧调用方）→ 回落全局配置，
           **行为与以前逐字一致**（向后兼容）。

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

        from emotion_app.domain.chat import LLMRequestCancelled

        original_event = event
        try:
            req, contexts = _messages_to_provider_request(messages, ProviderRequest)
            # 🔴 与消息派发**同一个判据**：会话绑了就用会话的，没绑才回落全局
            if event is None:
                uid = await resolve_user_id(self.config, self.sessions, session_id)
                event = build_inbound_event(_last_user_text_from(contexts), uid)
            req.session_id = str(getattr(event, "unified_msg_origin", "") or session_id)
            conversations = getattr(self, "conversation_manager", None)
            if original_event is not None and conversations is not None:
                captured = event.get_extra("_emotion_native_conversation_id")
                guard = event.get_extra("_emotion_native_history_guard")
                if callable(guard):
                    await guard()
                cid = captured or await conversations.get_curr_conversation_id(req.session_id)
                if cid:
                    req.conversation = await conversations.get_conversation(req.session_id, cid)
                    if captured and req.conversation is None:
                        raise LLMRequestCancelled("本轮原生会话已不可用")
            if original_event is not None:
                context = getattr(self.host, "context", None)
                if context is not None:
                    from astrbot_host.compat.core.astr_main_agent import _merge_plugin_tools

                    _merge_plugin_tools(req, context)
            req, cancelled = await run_llm_request_hooks(event, req)
            if cancelled:
                raise LLMRequestCancelled("LLM 请求已由插件终止")
            out = _provider_request_to_messages(req, contexts)
            if original_event is not None:
                from emotion_app.web.llm_turn import prepare_llm_turn

                return prepare_llm_turn(out, req, event, context=getattr(self.host, "context", None))
            return out
        except LLMRequestCancelled:
            raise
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
            # ⚠️ 空 = 回落全局配置（与 `resolve_user_id()` 里**同一个**回落函数，
            #    不在这里另写一份 `or "default"`）。调用方通常已经用
            #    `resolve_user_id()` 算好了（会话绑定的优先）。
            #    🔴 参数名是 `user_id` —— 别写成 `uid = uid or …`
            #       （那样 `uid` 未定义 → UnboundLocalError → 被下面的
            #        `except` 吞成一条"派发失败"日志，**整条派发静默失效**。
            #        这正是本项目铁律说的：宽 except 会把真因换掉。
            #        抓它的是真机判据，不是假宿主判据。）
            uid = user_id or fallback_user_id(self.config)
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

    async def dispatch_event(self, event: Any) -> Any:
        """把一条**已经构造好的**事件派发给插件 handler。

        与 `dispatch_message()` 的分工：
        * `dispatch_message()` 从**纯文本**重建事件（面板聊天走它）；
        * 本方法接受**现成事件**（外部平台走它）。

        🔴 为什么外部平台必须用本方法：OneBot 的消息带**图片、引用、@**
           —— 那些是 `message_obj.message` 里的消息段。若走
           `dispatch_message(text)`，这些段会被丢掉，插件只看到一个纯字符串
           ⇒ 「引用回复」失效、图片看不见。**那是静默降级**，最难查。

        Returns:
            `DispatchResult`；宿主没起来时返回 `None`。
        """
        if self.host is None:
            return None
        try:
            from astrbot_host.dispatch import dispatch
        except Exception:  # noqa: BLE001
            logger.error("派发模块导入失败（外部消息无法派发给插件）", exc_info=True)
            return None

        try:
            result = await dispatch(event)
        except Exception:  # noqa: BLE001
            logger.error("插件事件派发失败（本条消息按【无插件】处理）", exc_info=True)
            return None

        if result.errors:
            logger.warning(
                "插件派发中有 %d 个 handler 抛错：%s",
                len(result.errors),
                "; ".join(f"{n}: {e}" for n, e in result.errors[:3]),
            )
        return result

    # ---- 插件启停（照真实现的 `inactivated_plugins`）----

    def _config_profile_store(self) -> Any:
        """造配置文件 store（路径可由装配层覆盖 —— 测试必须隔离到 tmp）。

        🔴 与 `api_admin._cfg_profile()` **同一个约定**（`EMOTION_CONFIG_PROFILE_PATH`）：
           单测绝不能写进项目的 `data/`，更不能碰 `/opt/astrbot`。
        """
        from emotion_app.domain.config_profile import DEFAULT_PATH, ConfigProfileStore

        path = DEFAULT_PATH
        getter = self._config_profile_path
        if callable(getter):
            path = getter() or DEFAULT_PATH
        return ConfigProfileStore(path)

    def saved_inactivated(self) -> list[str]:
        """读**已保存**的停用名单（面板的勾选状态以它为准，不是运行态）。

        ⚠️ 为什么勾选要看"已保存"而不是"当前运行态"：保存之后重启前，运行态
           还是旧的 —— 若勾选跟着运行态走，用户刚取消勾选、一保存就看见它自己
           跳回去了，会以为"没保存成功"。（"需重启生效"另用 `restart_required` 表达。）

        ⚠️ 读失败**不静默当空**：空名单 = "全部启用"，那是个有力的断言。
           `ConfigProfileStore` 对损坏/空文件会记 ERROR 并另存 `.corrupt`
           （见它的 `_preserve_corrupt`），所以这里不需要再造一层。
        """
        from emotion_app.domain.config_profile import SECTION_PLUGINS

        raw = self._config_profile_store().load()
        section = raw.get(SECTION_PLUGINS) or {}
        value = section.get("inactivated_plugins")
        if not isinstance(value, list):
            # 形状不对（store 已记 ERROR 并回落默认值）→ 这里再点名一次后果
            logger.error(
                "配置里的插件停用名单不是数组（%r）→ 本条按【全部启用】处理",
                type(value).__name__,
            )
            return []
        return [str(x) for x in value if str(x).strip()]

    def save_inactivated(self, values: Any) -> dict[str, Any]:
        """保存停用名单（**面板唯一的写入口**）。

        Returns:
            `plugin_activation_view()` —— 保存后的完整状态。

        Raises:
            ConfigProfileError: 形状不对 / 元素不是字符串（路由层转 400 并点名字段）。
        """
        from emotion_app.domain.config_profile import (
            SECTION_PLUGINS,
            ConfigProfileError,
            validate_patch,
        )

        if not isinstance(values, list):
            raise ConfigProfileError(
                "inactivated_plugins 必须是字符串数组（插件模块路径）",
                field_name="inactivated_plugins",
            )
        # 🔴 判据只用 store 的 `validate_patch`（与其它配置项**同一套校验**），
        #    不在这里另写一套"什么算合法"。
        validated = validate_patch(
            SECTION_PLUGINS, {"inactivated_plugins": values}
        )
        self._config_profile_store().patch({SECTION_PLUGINS: validated})
        return self.plugin_activation_view()

    def _forget_inactivated(self, plugin_id: str) -> str:
        """把某个插件从**已保存的停用名单**里剔掉，返回被剔掉的 module_path。

        这是注入给 `PluginHost` 的 `inactivated_writer` 端口
        （见 `PluginHost.__init__` 的说明：宿主不许反向 import 本模块）。

        ⚠️ 为什么卸载插件必须调它：名单元素是 module_path。目录被移走后那条
           就成了"对不上任何插件"的残留 —— 下次启动加载器会报
           `停用名单里有 N 项对不上任何插件目录（拼写错误？）`，
           用户看到一条**指向已删插件**的警告，还得自己去翻配置文件。

        ⚠️ 走 `save_inactivated()` 而不是自己写盘：那是**面板唯一的写入口**，
           校验（`validate_patch`）只在那一处。另开一条写路径 = 本项目
           最忌的"同一概念两条写入路径"。

        Returns:
            被剔掉的 module_path（**空串** = 它本来就不在名单里）。
        """
        from astrbot_host.loader.plugin_loader import normalize_module_path

        target = normalize_module_path(plugin_id)
        saved = self.saved_inactivated()
        hits = [x for x in saved if normalize_module_path(x) == target]
        if not hits:
            return ""
        remaining = [x for x in saved if normalize_module_path(x) != target]
        try:
            self.save_inactivated(remaining)
        except Exception:  # noqa: BLE001
            # 🔴 写配置失败**不许静默**：名单没清掉是"下次启动会看到假警告"，
            #    而不是"操作失败" —— 必须让用户在日志里能看到。
            logger.error(
                "从停用名单里剔除 %s 失败（名单已保留原样，下次启动可能报 unmatched）",
                target,
                exc_info=True,
            )
            return ""
        return hits[0]

    def plugin_activation_ids(self) -> list[str]:
        """当前宿主认识的**全部**插件 id（目录名）。

        用途：详情路由 404 时回一份"可用 id"清单 —— 只说「没有这个插件」
        会让用户不知道自己拼错了哪个字（那是**误导性**的报错，
        本项目对错误文案的要求是"能直接指向下一步动作"）。

        ⚠️ 宿主没起来时返回**空列表**是诚实的：那时我们确实一个都不认识
        （调用方在宿主为空时会先返回 503，走不到这里）。
        """
        if self.host is None:
            return []
        return [p["id"] for p in self.host.plugins_view()]

    def plugin_activation_view(self) -> dict[str, Any]:
        """面板「插件启停」需要的**全部**信息（一次给全，前端不再自己算）。

        三种状态必须**分开**表达，否则用户会看到自相矛盾的界面：

        * `state`（来自宿主）—— 插件**现在**跑没跑；
        * `enabled_saved` —— 用户**保存的意图**（勾选框显示它）；
        * `restart_required` —— 两者不一致 = 有改动还没生效（必须重启服务）。

        🔴 `restart_required` 是**算出来的**，不是恒 `true` 的装饰：
           它就是"保存集合 != 运行集合"。所以保存完、重启后再打开这一页，
           它会自己变回 `false` —— 这句话是可证伪的，而不是一句场面话。
        """
        from astrbot_host.loader.plugin_loader import normalize_module_path

        saved_raw = self.saved_inactivated()
        saved = [p for p in (normalize_module_path(x) for x in saved_raw) if p]

        running = self.host is not None
        plugins: list[dict[str, Any]] = self.host.plugins_view() if running else []

        known = {p["module_path"] for p in plugins}
        saved_set = set(saved)
        # 名单里对不上任何插件的项（拼错 / 插件已删）—— 必须回给面板，别静默丢
        unmatched = sorted(x for x in saved_set if x not in known)

        for p in plugins:
            p["enabled_saved"] = p["module_path"] not in saved_set

        active = {p["module_path"] for p in plugins if p["state"] == "inactivated"}
        if running:
            restart_required = saved_set != active
        else:
            # 宿主没起来 → 运行态未知，但"存了却肯定没生效"是确定的
            # （空名单则无所谓，没什么要生效的）。
            restart_required = bool(saved_set)

        return {
            "running": running,
            "plugins": plugins,
            "inactivated_saved": saved,
            "inactivated_active": sorted(active),
            "unmatched": unmatched,
            "restart_required": restart_required,
        }

    # ---- 诊断 ----

    def status(self) -> dict[str, Any]:
        """脱敏状态（供 `/api/v1/status` 展示）。"""
        if self.report is None:
            return {"running": False}
        return {
            "running": True,
            "loaded": self.report.loaded,
            # 🔴 停用的插件**单独一列**（与 `failed` 是两条不同的路径）。
            #    以前只有 loaded/failed 两项，"没跑"这件事分不清是停用还是坏了。
            "inactivated": list(getattr(self.report, "inactivated", []) or []),
            "unmatched_inactivated": list(
                getattr(self.report, "unmatched_inactivated", []) or []
            ),
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
