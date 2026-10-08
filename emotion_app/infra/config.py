# -*- coding: utf-8 -*-
"""配置：嵌套 schema、环境变量覆盖、启动前校验。

设计要点（见 docs/BACKEND_FRAMEWORK.md §6.2/§6.3）：
  * 起步配置项刻意少（< 20），防止重蹈陪伴插件 747 项的覆辙；
  * **默认只绑 127.0.0.1**；
  * **访问令牌必填且 >= 16 字符，否则拒绝启动**（照 AstrBot 陪伴插件的硬校验）；
  * 令牌不落日志、不进 schema 的明文回显。
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 访问令牌最小长度。判据：与 AstrBot 陪伴插件 standalone_webui 的校验一致。
MIN_TOKEN_LENGTH = 16

#: 允许的绑定地址白名单。默认仅本地；要局域网必须显式写内网地址或 0.0.0.0。
LOOPBACK = "127.0.0.1"

#: 环境变量前缀
ENV_PREFIX = "EMOTION_"


class ConfigError(Exception):
    """配置非法 —— 调用方应打印原因并以非零码退出，绝不"带病启动"。"""


# ---------------------------------------------------------------------------
# 配置结构
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ServerConfig:
    """HTTP 服务。"""

    host: str = LOOPBACK
    #: 🔴 **默认端口必须避开陪伴体系**（2026-09-25 实测改，原为 6190）。
    #:
    #: 起因：陪伴插件「独立 WebUI」的默认端口**就是 6190**
    #: (`standalone_webui_port`)，与我们**完全撞车** →
    #: 用户在插件面板上打开那个开关，就会和我们抢端口，
    #: 而**失败在插件那一侧**（表现是"插件面板打不开"）→ 极易误判成我们的 bug。
    #:
    #: 已占用的端口（实测 + 官方 wiki）：6190 插件独立 WebUI ·
    #: 6314 screen WebUI · 6321 together 房间 · 6322 reality 网关 ·
    #: 8765 nai 代理；本机另有 astrbot 的 6185/6199。
    #: → 取 6191：紧挨原值便于记忆，且与上述全部不冲突。
    port: int = 6191

    #: 会话 cookie 是否带 `Secure`。
    #: ⚠️ 本机 http 访问必须 False，否则浏览器**直接不收 cookie**（登录表现为"点了没反应"）。
    #: 挂了 TLS 反代之后应置 True（`EMOTION_SERVER_HTTPS_ONLY=true`）。
    https_only: bool = False

    def validate(self) -> None:
        if not self.host:
            raise ConfigError("server.host 不能为空")
        if not (1 <= self.port <= 65535):
            raise ConfigError(f"server.port 非法：{self.port}")

    def is_public(self) -> bool:
        """是否绑定了非回环地址（= 对网络开放）。"""
        return self.host not in (LOOPBACK, "localhost", "::1")


@dataclass(slots=True)
class AuthConfig:
    """鉴权。"""

    access_token: str = ""
    session_ttl_hours: int = 24

    def validate(self) -> None:
        if not self.access_token:
            raise ConfigError(
                "未设置访问令牌（auth.access_token）—— 拒绝启动。\n"
                f"  请设置至少 {MIN_TOKEN_LENGTH} 个字符的令牌，例如：\n"
                f'    export {ENV_PREFIX}AUTH_ACCESS_TOKEN="$(openssl rand -hex 16)"'
            )
        if len(self.access_token) < MIN_TOKEN_LENGTH:
            raise ConfigError(
                f"访问令牌过短（{len(self.access_token)} < {MIN_TOKEN_LENGTH} 字符）—— 拒绝启动。"
            )
        if not (1 <= self.session_ttl_hours <= 168):
            raise ConfigError(f"session_ttl_hours 应在 1..168，当前 {self.session_ttl_hours}")

    def public_view(self) -> dict[str, Any]:
        """给面板/日志看的**脱敏**视图 —— 绝不回显令牌本身。"""
        return {
            "token_configured": bool(self.access_token),
            "token_length": len(self.access_token),
            "session_ttl_hours": self.session_ttl_hours,
        }


@dataclass(slots=True)
class StorageConfig:
    """存储。默认 SQLite（情绪 2026-09-18 拍板）；JSON 作为可插拔备选。"""

    backend: str = "sqlite"  # sqlite | json
    sqlite_path: str = "data/emotion.db"
    json_path: str = "data/emotion.json"

    _ALLOWED = ("sqlite", "json")

    def validate(self) -> None:
        if self.backend not in self._ALLOWED:
            raise ConfigError(f"storage.backend 仅支持 {self._ALLOWED}，当前 {self.backend!r}")

    def path(self) -> Path:
        return Path(self.sqlite_path if self.backend == "sqlite" else self.json_path)


@dataclass(slots=True)
class UpstreamConfig:
    """上游 AI 服务。实测：new-api 系、完全兼容 OpenAI、无需适配层。"""

    base_url: str = "https://api.syuan.org/v1"
    api_key: str = ""
    model: str = "gemini-3.1-flash-lite"
    timeout_seconds: float = 60.0

    def validate(self) -> None:
        if not self.base_url.startswith(("http://", "https://")):
            raise ConfigError(f"upstream.base_url 必须是 http(s) URL，当前 {self.base_url!r}")
        if self.timeout_seconds <= 0:
            raise ConfigError("upstream.timeout_seconds 必须为正数")

    def public_view(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "api_key_configured": bool(self.api_key),
        }


@dataclass(slots=True)
class ImageConfig:
    """生图端点。

    ⚠️ 与聊天上游**分开配**：实测爱语就是分开的
       （`key_image_gen_custom_url` / `_custom_key` / `_use_custom_api`）。
       留空 = 复用聊天上游的 base_url 与 key（多数中转站都提供 /images/generations）。
    """

    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    model: str = "dall-e-3"
    size: str = "1024x1024"
    output_dir: str = "data/images"

    #: 后端专有参数的默认值（JSON 字符串）。
    #: ⚠️ 实测 NAI 认 `steps` / `scale` / `sampler` / `negative_prompt`，
    #:    而它们**不是 OpenAI 标准字段**。放这里避免为 NAI 单独写后端。
    #: 例：{"steps": 28, "scale": 5.0, "negative_prompt": "lowres, bad anatomy"}
    extra_params: str = ""

    #: 🔴 工具下发方式（情绪：「接口别写死了」）
    #:   `function_call`（默认，实测可用）/ `text_tag`（退路，照爱语的标签方式）
    #: ⚠️ 之所以做成配置：将来对接陪伴插件的工具协议时，只改这一项。
    dispatch: str = "function_call"

    #: 🆕 **人设固定参考图**（2026-09-25）—— 生图时的**默认参考图**。
    #:
    #: 为什么需要它：光把参考图"能发出去"还不够，每次还得模型自己传路径
    #: → 同一个角色每次长得都不一样。把它设成人设图后，
    #: `kind="selfie"` 且模型没指定参考图时**自动带上**，长相/画风才稳定。
    #:
    #: ⚠️ **只在 `selfie` 上生效**：`edit` 的语义是"改用户给的那张图"，
    #:    拿角色参考图兜底会**静默换掉语义**（本项目最忌那类）。
    #: ⚠️ 路径读不到 → **响亮失败**（`reference_unreadable`），
    #:    不静默降级成纯文生图（那样用户只会觉得"怎么长得不像"）。
    persona_reference: str = ""

    def effective_base(self, fallback: str) -> str:
        return self.base_url or fallback

    def effective_key(self, fallback: str) -> str:
        return self.api_key or fallback

    _DISPATCHES = ("function_call", "text_tag")

    def validate(self) -> None:
        if self.base_url and not self.base_url.startswith(("http://", "https://")):
            raise ConfigError(f"image.base_url 必须是 http(s) URL，当前 {self.base_url!r}")
        if self.dispatch not in self._DISPATCHES:
            raise ConfigError(
                f"image.dispatch 仅支持 {self._DISPATCHES}，当前 {self.dispatch!r}"
            )
        if self.extra_params:
            try:
                parsed = json.loads(self.extra_params)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"image.extra_params 不是合法 JSON：{exc}") from exc
            if not isinstance(parsed, dict):
                raise ConfigError("image.extra_params 必须是 JSON 对象")

    def public_view(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "base_url": self.base_url or "(复用聊天上游)",
            "model": self.model,
            "size": self.size,
            "api_key_configured": bool(self.api_key),
            "output_dir": self.output_dir,
            "dispatch": self.dispatch,
            "extra_params": sorted(json.loads(self.extra_params)) if self.extra_params else [],
            "persona_reference_configured": bool(self.persona_reference),
        }


@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"
    json_output: bool = False

    _ALLOWED = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

    def validate(self) -> None:
        if self.level.upper() not in self._ALLOWED:
            raise ConfigError(f"logging.level 非法：{self.level!r}")


@dataclass(slots=True)
class PluginsConfig:
    """AstrBot 插件宿主（把插件装进本服务）。

    🔴 **默认关闭**（`enabled=False`）—— 这是刻意的：
       插件会**执行任意代码**、写自己的数据目录，属于"高信任"能力。
       默认关闭意味着**不显式开启就不会因为配置缺失而意外加载插件**
       （也避免测试/CI 里突然去读生产插件目录）。

    ⚠️ `plugin_dirs` 默认指向**只读的插件源码目录**（`/opt/astrbot/data/plugins`），
       而 `data_dir` 指向**我们自己的可写数据目录**（`data/plugins`）。
       两者**必须分开**：否则插件会把新数据写进别人（AstrBot）的生产目录。
    """

    enabled: bool = False
    #: 插件源码目录（只读；分号分隔多个）
    plugin_dirs: str = "/opt/astrbot/data/plugins"
    #: 插件**数据**目录（可写；我们的，不是 AstrBot 的）
    data_dir: str = "data/plugins"
    #: 加载后是否调用插件的 `initialize()`
    auto_initialize: bool = True
    #: 🔴 单个插件 `initialize()` 的等待上限（秒）。
    #: 实测教训：media_parser 的 initialize() 会联网下载 16MB+17MB 字体
    #: （超时设 180s），把服务启动卡了 **181.6 秒**；无外网时会一直卡。
    #: 20s 足够区分"正常插件"（实测全部 ≤0.01s）与"在做网络请求的插件"。
    initialize_timeout: float = 20.0
    #: 🔴 **把插件源码镜像到我们自己的可写目录后再加载**（默认开）。
    #: 为什么必须有：插件会回写自己的**源码目录**（实测 comfyui_pro 把
    #: workflow 列表写回 `_conf_schema.json`），而那是**两边共用**的
    #: —— 很可能就是别人正在跑的 AstrBot 生产目录，会被我们覆盖。
    #: 镜像后源码目录对我们只读，插件怎么写都落在副本里。
    mirror_sources: bool = True
    #: 源码镜像的落盘位置（可写，我们自己的）
    source_mirror_dir: str = "data/plugin_src"
    #: 🔴 **AstrBot 全局配置**（`cmd_config.json`）的路径 —— 插件
    #: `context.get_config(umo)` 读的就是它（P-N5，2026-09-22 补）。
    #:
    #: ⚠️ 为什么要显式可配：原来宿主给插件的是 `HostPorts(config={})`，
    #:    `get_config()` **恒返回空字典** → 20 处调用 / 12 个文件**静默**降级
    #:    （TTS 判为"未配置" / `timezone` 回落 / `tool_call_timeout` 回落 120s /
    #:    联网搜索判为不可用），并且是那句**假根因**「上游 base_url 为空」的来源。
    #:
    #: 默认与原 AstrBot 数据目录布局一致（与 `data_dir="data/plugins"` 同一层 `data/`）。
    #: ⚠️ 该文件**带 UTF-8 BOM** —— 读它必须 `utf-8-sig`（见
    #:    `astrbot_host.compat.core.config.astrbot_config.load_global_config`）。
    global_config_path: str = "data/cmd_config.json"
    #: 🔴 **本机用户的唯一 ID** —— 用来拼 umo 的 `session_id` 段。
    #:
    #: umo 格式（`host/astrbot_host/umo.py` 固化）：`11:FriendMessage:<这个值>`
    #:   * 第一段 `11` 是**平台实例 id** —— 照抄 AstrBot 现有连接，与生产
    #:     `companions.json` 里的 `11:FriendMessage:3412642082` 逐字同构；
    #:   * `session_id` 段是**插件存用户数据的主键** —— 一改就等于换库，
    #:     用户的历史/关系/记忆全都对不上。**所以要可配置 + 长期稳定**。
    #:
    #: ⚠️ 默认 `"default"`：对"一个人自用"的场景够用且**不会因为机器/端口变化
    #:    而漂移**（用 hostname/IP 之类的会漂）。想与 QQ 侧数据对齐时，
    #:    把它设成自己的 QQ 号即可（如 `3412642082`）。
    umo_user_id: str = "default"
    #: 🔴 **额外的平台实例**（情绪 2026-09-21：「以后可能会加入 onebot 机器人」）。
    #:
    #: 格式：JSON 数组，每项 `{"id": "...", "name": "...", "description": "...",
    #: "label": "..."}`。`id` 是 **umo 第一段**（插件数据主键的一半），
    #: `name` 是平台**类型**名（决定插件的**能力表**）。
    #:
    #: 例（加一个 OneBot/QQ 平台）::
    #:
    #:     [{"id": "21", "name": "aiocqhttp", "description": "QQ 机器人", "label": "QQ"}]
    #:
    #: 🔴 **默认平台（`11`/`webchat`）永远在，不受这里影响** ——
    #:    本项只**追加**；留空 = 只有 Web 端（与以前行为完全一致）。
    #:
    #: ⚠️ 三项校验在**启动时**执行（`host/astrbot_host/platforms.py`），
    #:    配错会**拒绝启动**而不是运行中静默出怪事。实测的三个坑：
    #:    ① `name` 不能解析成 `generic`（会让插件主动消息整链失效）；
    #:    ② `description` 不能含平台别名子串（`"非 OneBot"` 会被判成 onebot
    #:       → 插件去 `int(user_id)` 崩）；
    #:    ③ `description` 不能是 `cronjob`（会被当定时任务伪事件 → 正文发两次）。
    extra_platforms: str = ""
    #: 默认平台实例 id（新会话默认绑哪个平台）。空 = 用内置默认（`11`）。
    #: ⚠️ 改它**不**改已有会话的绑定。
    default_platform_id: str = ""
    #: 是否挂载插件的 Web API 路由（`/api/plugins/extensions/...`）
    mount_web_apis: bool = True

    def dirs(self) -> list[str]:
        return [d.strip() for d in self.plugin_dirs.split(";") if d.strip()]

    def platform_specs(self) -> list[dict[str, Any]]:
        """解析 `extra_platforms`（JSON 数组）→ 列表。

        Raises:
            ConfigError: JSON 非法 / 不是数组 / 元素不是对象。
                🔴 **不静默当空** —— 配错了却"看起来启动正常"，
                会让用户以为新平台已经生效（本项目最贵的那类 bug）。
        """
        raw = (self.extra_platforms or "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"plugins.extra_platforms 不是合法 JSON：{exc}"
                f"（原文前 120 字：{raw[:120]!r}）"
            ) from exc
        if not isinstance(parsed, list):
            raise ConfigError(
                f"plugins.extra_platforms 必须是**数组**，得到 {type(parsed).__name__}"
            )
        for i, item in enumerate(parsed):
            if not isinstance(item, dict):
                raise ConfigError(
                    f"plugins.extra_platforms[{i}] 必须是对象，得到 {type(item).__name__}"
                )
        return parsed

    def validate(self) -> None:
        if self.enabled and not self.dirs():
            raise ConfigError("plugins.enabled=true 但 plugins.plugin_dirs 为空")
        if self.enabled and not self.data_dir.strip():
            raise ConfigError("plugins.enabled=true 但 plugins.data_dir 为空")
        # 🔴 平台配置**在这里就校验**（JSON 形状 + 平台规格三条硬约束）——
        #    配错 = 拒绝启动，而不是"起来了但平台行为不对"（那类问题全是静默的）。
        specs = self.platform_specs()
        if specs or self.default_platform_id.strip():
            # 延迟 import：`astrbot_host` 只在插件宿主启用时才在 sys.path 上。
            from emotion_app.web.plugin_host import ensure_host_on_path

            ensure_host_on_path()
            from astrbot_host.platforms import PlatformSpecError, build_registry

            try:
                registry = build_registry(specs)
                if self.default_platform_id.strip():
                    registry.set_default(self.default_platform_id.strip())
            except PlatformSpecError as exc:
                raise ConfigError(f"平台配置不合法：{exc}") from exc

    def public_view(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "plugin_dirs": self.dirs(),
            "data_dir": self.data_dir,
            "auto_initialize": self.auto_initialize,
            "initialize_timeout": self.initialize_timeout,
            "mirror_sources": self.mirror_sources,
            "source_mirror_dir": self.source_mirror_dir,
            "mount_web_apis": self.mount_web_apis,
        }


@dataclass(slots=True)
class Config:
    """总配置。刻意保持扁平、项少、可读。"""

    server: ServerConfig = field(default_factory=ServerConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)
    image: ImageConfig = field(default_factory=ImageConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    plugins: PluginsConfig = field(default_factory=PluginsConfig)

    def validate(self) -> None:
        """逐层校验。**任何一层不合法都要拒绝启动**，不静默降级。"""
        self.server.validate()
        self.auth.validate()
        self.storage.validate()
        self.upstream.validate()
        self.image.validate()
        self.logging.validate()
        self.plugins.validate()

    def public_view(self) -> dict[str, Any]:
        """脱敏全量视图（供 /health 与日志）。**不含任何密钥明文**。"""
        return {
            "server": {
                "host": self.server.host,
                "port": self.server.port,
                "https_only": self.server.https_only,
                "exposed": self.server.is_public(),
            },
            "auth": self.auth.public_view(),
            "storage": {"backend": self.storage.backend, "path": str(self.storage.path())},
            "upstream": self.upstream.public_view(),
            "image": self.image.public_view(),
            "logging": {"level": self.logging.level, "json_output": self.logging.json_output},
            "plugins": self.plugins.public_view(),
        }


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------

#: 环境变量 → (配置分组, 字段名, 转换函数)
_ENV_MAP: dict[str, tuple[str, str, str]] = {
    "SERVER_HOST": ("server", "host", "str"),
    "SERVER_PORT": ("server", "port", "int"),
    "SERVER_HTTPS_ONLY": ("server", "https_only", "bool"),
    "AUTH_ACCESS_TOKEN": ("auth", "access_token", "str"),
    "AUTH_SESSION_TTL_HOURS": ("auth", "session_ttl_hours", "int"),
    "STORAGE_BACKEND": ("storage", "backend", "str"),
    "STORAGE_SQLITE_PATH": ("storage", "sqlite_path", "str"),
    "STORAGE_JSON_PATH": ("storage", "json_path", "str"),
    "UPSTREAM_BASE_URL": ("upstream", "base_url", "str"),
    "UPSTREAM_API_KEY": ("upstream", "api_key", "str"),
    "UPSTREAM_MODEL": ("upstream", "model", "str"),
    "UPSTREAM_TIMEOUT_SECONDS": ("upstream", "timeout_seconds", "float"),
    "IMAGE_ENABLED": ("image", "enabled", "bool"),
    "IMAGE_BASE_URL": ("image", "base_url", "str"),
    "IMAGE_API_KEY": ("image", "api_key", "str"),
    "IMAGE_MODEL": ("image", "model", "str"),
    "IMAGE_SIZE": ("image", "size", "str"),
    "IMAGE_OUTPUT_DIR": ("image", "output_dir", "str"),
    "IMAGE_DISPATCH": ("image", "dispatch", "str"),
    "IMAGE_EXTRA_PARAMS": ("image", "extra_params", "str"),
    "IMAGE_PERSONA_REFERENCE": ("image", "persona_reference", "str"),
    "LOGGING_LEVEL": ("logging", "level", "str"),
    "LOGGING_JSON": ("logging", "json_output", "bool"),
    "PLUGINS_ENABLED": ("plugins", "enabled", "bool"),
    "PLUGINS_PLUGIN_DIRS": ("plugins", "plugin_dirs", "str"),
    "PLUGINS_DATA_DIR": ("plugins", "data_dir", "str"),
    "PLUGINS_AUTO_INITIALIZE": ("plugins", "auto_initialize", "bool"),
    "PLUGINS_INITIALIZE_TIMEOUT": ("plugins", "initialize_timeout", "float"),
    "PLUGINS_MIRROR_SOURCES": ("plugins", "mirror_sources", "bool"),
    "PLUGINS_SOURCE_MIRROR_DIR": ("plugins", "source_mirror_dir", "str"),
    "PLUGINS_GLOBAL_CONFIG_PATH": ("plugins", "global_config_path", "str"),
    "PLUGINS_MOUNT_WEB_APIS": ("plugins", "mount_web_apis", "bool"),
    #: umo 的 `session_id` 段（插件存用户数据的主键，见 `PluginsConfig.umo_user_id`）
    "PLUGINS_UMO_USER_ID": ("plugins", "umo_user_id", "str"),
    #: 额外平台实例（JSON 数组）—— 加 onebot 等新平台走这里
    "PLUGINS_EXTRA_PLATFORMS": ("plugins", "extra_platforms", "str"),
    #: 默认平台实例 id（新会话默认绑哪个平台）
    "PLUGINS_DEFAULT_PLATFORM_ID": ("plugins", "default_platform_id", "str"),
}

_CASTERS = {
    "str": lambda v: v,
    "int": int,
    "float": float,
    "bool": lambda v: v.strip().lower() in ("1", "true", "yes", "on"),
}


def load_config(env: dict[str, str] | None = None, *, validate: bool = True) -> Config:
    """从环境变量加载配置。

    Args:
        env: 环境变量字典，默认取 ``os.environ``。
        validate: 是否立即校验（测试里可关掉以单独测某层）。

    Raises:
        ConfigError: 校验不通过。
    """
    env = os.environ if env is None else env
    cfg = Config()

    for env_key, (group, attr, kind) in _ENV_MAP.items():
        raw = env.get(ENV_PREFIX + env_key)
        if raw is None or raw == "":
            continue
        try:
            value = _CASTERS[kind](raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"环境变量 {ENV_PREFIX}{env_key} 的值 {raw!r} 无法转换为 {kind}"
            ) from exc
        setattr(getattr(cfg, group), attr, value)

    if validate:
        cfg.validate()
    return cfg


def generate_token() -> str:
    """生成一个合规令牌（32 位 hex = 128 bit 熵）。供首次部署用。"""
    return secrets.token_hex(16)
