# -*- coding: utf-8 -*-
"""`astrbot.core.config.astrbot_config` 兼容层 —— 宿主配置对象。

⭐ 依据：真 AstrBot `core/config/astrbot_config.py`（实测 337 行）。

⚠️ **相对真实现省略了什么（这是本文件最大的一处取舍，必读）**：

   ① **`DEFAULT_CONFIG` 没有搬**。真实现 `from .default import DEFAULT_CONFIG,
      DEFAULT_VALUE_MAP`，而 `default.py` 实测 **4,536 行 / 226 KB**，
      里面是 AstrBot 全量配置元数据（`CONFIG_METADATA_2`、各家 provider 模板…）
      —— 那是**宿主自己的配置清单**，与陪伴插件无关，搬过来只是把 226 KB
      死数据塞进兼容层。因此 `default_config` 的**默认值是空字典**
      （真实现是那份全量默认配置）。
      🔴 **后果（不是 bug，是必须知道的行为边界）**：
        * 真实现里「配置缺项 → 自动补默认值并落盘」依赖 `DEFAULT_CONFIG`；
          这里默认无项可补，**不会凭空写入任何默认配置**。
        * 宿主**必须**用自己的配置字典实例化：
          `AstrBotConfig(config_path=..., default_config=<宿主默认配置>)`，
          或走 `schema=` 分支。**这本来就是陪伴的真实用法** ——
          插件从 `astrbot.api` 拿到的是**宿主注入**的配置对象，
          插件自己从不构造它（实测：插件里 0 处 `AstrBotConfig(...)` 调用）。
        * `DEFAULT_VALUE_MAP` **照搬**（10 项，真源码 L4525-4536）：
          它决定 `schema` 解析时每种类型的兜底值，属**行为**而非数据。

   ② **`core/utils/auth_password.py` 未引模块，只内联了 4 个函数**
      （`_generate_dashboard_password` / `_hash_dashboard_password` /
      `_hash_md5_dashboard_password` / `_validate_dashboard_password`）。
      **为什么保留而不是整段删掉**：真实现里这段逻辑在 `__init__` 的**主路径上**
      —— 只要配置里有 `dashboard` 段且没有密码哈希，就会**生成密码并改写配置文件**。
      整个删掉会让行为与真实现分叉（例如宿主本来该看到"首次部署生成了密码"）。
      **为什么只留 4 个函数**：`verify_dashboard_password` / `totp` 等属于
      **宿主面板的鉴权职责**，不是插件契约；宿主有自己的鉴权体系
      （本项目是 `EMOTION_AUTH_ACCESS_TOKEN`），兼容层不该替它做决定。
      两者**算法保持等价**（PBKDF2-SHA256 600k 迭代 + 兼容 MD5），
      万一将来要接真 AstrBot 的 dashboard 配置也能对上。

   ③ **无新依赖**：真实现只用标准库（`asyncio/copy/enum/json/logging/os/
      tempfile/threading`）+ 上面两个 astrbot 内部模块，这里同样只用标准库。

🔴 **三个必须照抄、不能"顺手优化"的细节**：

   * **落盘用 `encoding="utf-8-sig"`（带 BOM），读入也是**。这不是笔误：
     真实现明确处理了 BOM（`if conf_str.startswith("\ufeff")`）。
     改成 `utf-8` 会让**真 AstrBot 的既有配置文件**在我们这里读成
     `\ufeff{...}` 而 `json.loads` 失败（BOM 会让 JSON 解析直接报错）。
   * `__init__` 里 **`self.update(conf)` 出现了两次**（L101 与 L105）。
     照抄 —— 第二次是为了覆盖 `check_config_integrity` 重排后的键序。
   * `__getattr__` 取不到键时**返回 `None` 而不是抛 AttributeError**。
     插件大量依赖 `config.xxx` 这种点号访问，抛异常会让"未配置项"
     变成崩溃而不是走默认值分支。这是**故意的宽松语义**。
"""
from __future__ import annotations

import asyncio
import copy
import enum
import hashlib
import json
import logging
import os
import re
import secrets
import string
import tempfile
import threading

from astrbot_host.compat.core.utils.astrbot_path import get_astrbot_data_path

ASTRBOT_CONFIG_PATH = os.path.join(get_astrbot_data_path(), "cmd_config.json")
DASHBOARD_INITIAL_PASSWORD_ENV = "ASTRBOT_DASHBOARD_INITIAL_PASSWORD"
DASHBOARD_RESET_PASSWORD_ENV = "ASTRBOT_RESET_DASHBOARD_PASSWORD"
logger = logging.getLogger("astrbot")

#: 真实现 `core/config/default.py` 的 `DEFAULT_VALUE_MAP`（真源码 L4525-4536，逐字照搬）。
#: 决定 `schema=` 解析时每种配置类型的兜底默认值 —— 属**行为**，所以必须搬。
DEFAULT_VALUE_MAP = {
    "int": 0,
    "float": 0.0,
    "bool": False,
    "string": "",
    "text": "",
    "list": [],
    "file": [],
    "object": {},
    "template_list": [],
    "dict": {},
}

#: 真实现的 `DEFAULT_CONFIG` 是 4,536 行的全量配置元数据，**未搬**（见文件头 ①）。
#: 保持空字典：不替宿主决定默认配置。
DEFAULT_CONFIG: dict = {}


# ---------------------------------------------------------------------------
# 以下 4 个函数内联自真实现 `core/utils/auth_password.py`（见文件头 ②）
# ---------------------------------------------------------------------------

_PBKDF2_ITERATIONS = 600_000
_PBKDF2_SALT_BYTES = 16
_PBKDF2_ALGORITHM = "pbkdf2_sha256"
_PBKDF2_FORMAT = f"{_PBKDF2_ALGORITHM}$"
_DASHBOARD_PASSWORD_MIN_LENGTH = 8
_GENERATED_DASHBOARD_PASSWORD_LENGTH = 24


def _generate_dashboard_password() -> str:
    """生成满足复杂度策略的强密码（照真实现）。"""
    alphabet = string.ascii_letters + string.digits
    password_chars = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        *(
            secrets.choice(alphabet)
            for _ in range(_GENERATED_DASHBOARD_PASSWORD_LENGTH - 3)
        ),
    ]
    secrets.SystemRandom().shuffle(password_chars)
    return "".join(password_chars)


def _hash_dashboard_password(raw_password: str) -> str:
    """PBKDF2-HMAC-SHA256 加盐哈希（照真实现）。"""
    if not isinstance(raw_password, str) or raw_password == "":
        raise ValueError("Password cannot be empty")

    salt = secrets.token_hex(_PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        raw_password.encode("utf-8"),
        bytes.fromhex(salt),
        _PBKDF2_ITERATIONS,
    ).hex()
    return f"{_PBKDF2_FORMAT}{_PBKDF2_ITERATIONS}${salt}${digest}"


def _hash_md5_dashboard_password(raw_password: str) -> str:
    """历史遗留的 MD5 存储（照真实现，供旧配置回退）。"""
    if not isinstance(raw_password, str) or raw_password == "":
        raise ValueError("Password cannot be empty")
    return hashlib.md5(raw_password.encode("utf-8")).hexdigest()


def _validate_dashboard_password(raw_password: str) -> None:
    """最小复杂度校验（照真实现）。"""
    if not isinstance(raw_password, str) or raw_password == "":
        raise ValueError("Password cannot be empty")
    if len(raw_password) < _DASHBOARD_PASSWORD_MIN_LENGTH:
        raise ValueError(
            f"Password must be at least {_DASHBOARD_PASSWORD_MIN_LENGTH} characters long"
        )

    if not re.search(r"[A-Z]", raw_password):
        raise ValueError("Password must include at least one uppercase letter")
    if not re.search(r"[a-z]", raw_password):
        raise ValueError("Password must include at least one lowercase letter")
    if not re.search(r"\d", raw_password):
        raise ValueError("Password must include at least one digit")


class RateLimitStrategy(enum.Enum):
    STALL = "stall"
    DISCARD = "discard"


class AstrBotConfig(dict):
    """从配置文件中加载的配置，支持直接通过点号操作符访问根配置项。

    - 初始化时会将传入的 default_config 与配置文件进行比对，如果配置文件中缺少配置项则会自动插入默认值并进行一次写入操作。会递归检查配置项。
    - 如果配置文件路径对应的文件不存在，则会自动创建并写入默认配置。
    - 如果传入了 schema，将会通过 schema 解析出 default_config，此时传入的 default_config 会被忽略。
    """

    config_path: str
    default_config: dict
    schema: dict | None

    def __init__(
        self,
        config_path: str = ASTRBOT_CONFIG_PATH,
        default_config: dict = DEFAULT_CONFIG,
        schema: dict | None = None,
    ) -> None:
        super().__init__()

        # 调用父类的 __setattr__ 方法，防止保存配置时将此属性写入配置文件
        object.__setattr__(self, "config_path", config_path)
        object.__setattr__(self, "default_config", default_config)
        object.__setattr__(self, "schema", schema)
        object.__setattr__(self, "_save_state_lock", threading.Lock())
        object.__setattr__(self, "_save_commit_lock", threading.Lock())
        object.__setattr__(self, "_save_revision", 0)
        object.__setattr__(self, "_save_committed_revision", 0)

        # An empty schema ({}) is falsy but valid: zero config items, not the global defaults.
        if schema is not None:
            default_config = self._config_schema_to_default_config(schema)

        if not self.check_exist():
            """不存在时载入默认配置"""
            self.update(default_config)
            self.save_config(indent=4)
            object.__setattr__(self, "first_deploy", True)  # 标记第一次部署

        with open(config_path, encoding="utf-8-sig") as f:
            conf_str = f.read()
            # Handle UTF-8 BOM if present
            if conf_str.startswith("\ufeff"):
                conf_str = conf_str[1:]
            conf = json.loads(conf_str)
        dashboard_conf = conf.get("dashboard")
        stored_dashboard_password_change_required = bool(
            isinstance(dashboard_conf, dict)
            and dashboard_conf.get("password_change_required", False)
        )
        if stored_dashboard_password_change_required:
            object.__setattr__(
                self,
                "_dashboard_password_change_required_from_config",
                True,
            )
        # 检查配置完整性，并插入
        has_new = self.check_config_integrity(default_config, conf)
        reset_dashboard_password = self._consume_reset_dashboard_password_flag()
        if reset_dashboard_password and "dashboard" in conf:
            self._reset_generated_dashboard_password(conf)
            has_new = True
        elif (
            "dashboard" in conf
            and isinstance(conf["dashboard"], dict)
            and not conf["dashboard"].get("pbkdf2_password")
            and not conf["dashboard"].get("password")
        ):
            self._reset_generated_dashboard_password(conf)
            has_new = True
        self.update(conf)
        if has_new:
            self.save_config()

        self.update(conf)

    def _reset_generated_dashboard_password(self, conf: dict) -> None:
        generated_password = self._resolve_initial_dashboard_password()
        conf["dashboard"]["pbkdf2_password"] = _hash_dashboard_password(
            generated_password
        )
        conf["dashboard"]["password"] = _hash_md5_dashboard_password(generated_password)
        conf["dashboard"]["password_storage_upgraded"] = True
        conf["dashboard"]["password_change_required"] = True
        object.__setattr__(
            self,
            "_generated_dashboard_password",
            generated_password,
        )
        object.__setattr__(
            self,
            "_generated_dashboard_password_change_required",
            True,
        )

    @staticmethod
    def _consume_reset_dashboard_password_flag() -> bool:
        raw_value = os.environ.pop(DASHBOARD_RESET_PASSWORD_ENV, "")
        return raw_value.strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _resolve_initial_dashboard_password() -> str:
        env_password = os.environ.get(DASHBOARD_INITIAL_PASSWORD_ENV)
        if env_password is None:
            return _generate_dashboard_password()
        _validate_dashboard_password(env_password)
        return env_password

    def _config_schema_to_default_config(self, schema: dict) -> dict:
        """将 Schema 转换成 Config"""
        conf = {}

        def _parse_schema(schema: dict, conf: dict) -> None:
            for k, v in schema.items():
                if v["type"] not in DEFAULT_VALUE_MAP:
                    raise TypeError(
                        f"不受支持的配置类型 {v['type']}。支持的类型有：{DEFAULT_VALUE_MAP.keys()}",
                    )
                if "default" in v:
                    default = v["default"]
                else:
                    default = DEFAULT_VALUE_MAP[v["type"]]

                if v["type"] == "object":
                    conf[k] = {}
                    _parse_schema(v["items"], conf[k])
                elif v["type"] == "template_list":
                    conf[k] = default
                else:
                    conf[k] = default

        _parse_schema(schema, conf)

        return conf

    def check_config_integrity(self, refer_conf: dict, conf: dict, path=""):
        """检查配置完整性，如果有新的配置项或顺序不一致则返回 True"""
        has_new = False

        # 创建一个新的有序字典以保持参考配置的顺序
        new_conf = {}

        # 先按照参考配置的顺序添加配置项
        for key, value in refer_conf.items():
            if key not in conf:
                # 配置项不存在，插入默认值
                path_ = path + "." + key if path else key
                logger.info("Config key missing; added default.")
                new_conf[key] = value
                has_new = True
            elif conf[key] is None:
                # 配置项为 None，使用默认值
                new_conf[key] = value
                has_new = True
            elif isinstance(value, dict):
                # 递归检查子配置项
                if not isinstance(conf[key], dict):
                    # 类型不匹配，使用默认值
                    new_conf[key] = value
                    has_new = True
                else:
                    # 递归检查并同步顺序
                    child_has_new = self.check_config_integrity(
                        value,
                        conf[key],
                        path + "." + key if path else key,
                    )
                    new_conf[key] = conf[key]
                    has_new |= child_has_new
            else:
                # 直接使用现有配置
                new_conf[key] = conf[key]

        # 检查是否存在参考配置中没有的配置项
        for key in list(conf.keys()):
            if key not in refer_conf:
                path_ = path + "." + key if path else key
                logger.info("Config key removed: %s", path_)
                has_new = True

        # 顺序不一致也算作变更
        if list(conf.keys()) != list(new_conf.keys()):
            if path:
                logger.info("Config key order fixed: %s", path)
            else:
                logger.info("Config key order fixed")
            has_new = True

        # 更新原始配置
        conf.clear()
        conf.update(new_conf)

        return has_new

    def save_config(
        self, replace_config: dict | None = None, *, indent: int = 2
    ) -> None:
        """Persist the current configuration synchronously.

        Args:
            replace_config: Values to merge into the configuration before saving.
            indent: Number of spaces used to indent the JSON output.
        """
        snapshot, revision = self._prepare_config_snapshot(replace_config)
        self._write_config_snapshot(snapshot, revision, indent)

    async def save_config_async(
        self, replace_config: dict | None = None, *, indent: int = 2
    ) -> bool:
        """Persist a stable configuration snapshot without blocking the event loop.

        Args:
            replace_config: Values to merge into the configuration before saving.
            indent: Number of spaces used to indent the JSON output.

        Returns:
            Whether this snapshot was committed. A newer committed snapshot supersedes
            an older snapshot.
        """
        snapshot, revision = self._prepare_config_snapshot(replace_config)
        return await asyncio.to_thread(
            self._write_config_snapshot,
            snapshot,
            revision,
            indent,
        )

    def _prepare_config_snapshot(self, replace_config: dict | None) -> tuple[dict, int]:
        """Create an isolated snapshot and allocate its save revision.

        Args:
            replace_config: Values to merge into the configuration before snapshotting.

        Returns:
            The isolated configuration snapshot and its monotonically increasing
            revision.
        """
        with self._save_state_lock:
            if replace_config:
                self.update(replace_config)
            snapshot = copy.deepcopy(dict(self))
            revision = self._save_revision + 1
            object.__setattr__(self, "_save_revision", revision)
        return snapshot, revision

    def _write_config_snapshot(
        self, snapshot: dict, revision: int, indent: int
    ) -> bool:
        """Write and conditionally commit a prepared configuration snapshot.

        Args:
            snapshot: Isolated configuration data to serialize.
            revision: Revision allocated when the snapshot was prepared.
            indent: Number of spaces used to indent the JSON output.

        Returns:
            Whether the snapshot replaced the current configuration file.
        """
        directory = os.path.dirname(os.path.abspath(self.config_path)) or "."
        fd, temp_path = tempfile.mkstemp(
            dir=directory,
            prefix=f".{os.path.basename(self.config_path)}.",
            suffix=".tmp",
        )
        committed = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
                json.dump(snapshot, f, indent=indent, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            with self._save_commit_lock:
                if revision > self._save_committed_revision:
                    os.replace(temp_path, self.config_path)
                    object.__setattr__(
                        self,
                        "_save_committed_revision",
                        revision,
                    )
                    committed = True
        finally:
            if not committed:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass
        return committed

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError:
            return None

    def __delattr__(self, key) -> None:
        try:
            del self[key]
            self.save_config()
        except KeyError:
            raise AttributeError(f"没有找到 Key: '{key}'")

    def __setattr__(self, key, value) -> None:
        self[key] = value

    def check_exist(self) -> bool:
        if not self.config_path:  # 加判空
            return False
        return os.path.exists(self.config_path)


__all__ = [
    "ASTRBOT_CONFIG_PATH",
    "DASHBOARD_INITIAL_PASSWORD_ENV",
    "DASHBOARD_RESET_PASSWORD_ENV",
    "DEFAULT_CONFIG",
    "DEFAULT_VALUE_MAP",
    "AstrBotConfig",
    "RateLimitStrategy",
    "load_global_config",
]


def load_global_config(path: str | os.PathLike[str] | None = None) -> dict:
    """读 **AstrBot 全局配置**（`cmd_config.json`）为 **plain `dict`**。

    ⭐⭐ 为什么必须有这个函数（P-N5，2026-09-22 实测）：

    插件读全局配置的唯一入口是 `context.get_config(umo)`，而宿主原来给它的是
    `HostPorts(config={})` —— 于是 `get_config()` **恒返回空字典**。
    实测后果（20 处调用 / 12 个文件，全是**静默**降级）：

    | 配置键 | 读不到时 |
    |---|---|
    | `provider_settings.tool_call_timeout` | 静默回落 `120`（`private_image.py:6413`） |
    | `provider_tts_settings.*`（11 键） | TTS 判为"未配置" |
    | `timezone` | 静默回落 `Asia/Shanghai` |
    | `provider_settings.web_search*` | 联网搜索判为不可用 |

    ⚠️ 而且它还是那个**假根因**的来源：本轮实测里那句
       「上游 base_url 为空」正是从这条链路报出来的（读不到配置 → 拿到默认值 → 走空 base_url）。

    ## 🔴 四条必须照做的细节（每条都对应一个踩过的坑）

    ① **必须用 `encoding="utf-8-sig"` 读** —— 真 AstrBot 的 `cmd_config.json`
       **带 BOM**（本文件头已记录该契约）。实测：用 `utf-8` 读会
       `JSONDecodeError: Unexpected UTF-8 BOM` → 如果我们把它 `except` 成 `{}`，
       就**又回到"配置恒为空"** 那个 bug（同一类错误换个马甲）。
    ② **返回 plain `dict`**，不是 `AstrBotConfig`：插件有 12 处按位置传 umo 调它，
       并且多处直接 `config.get(...)`；plain dict 是**最保守**的形状
       （`proactive_engine.py:9929`、`proactive_message.py:10484` 的
       `dict(config.get("provider_tts_settings", {}) or {})` 对它成立）。
    ③ **失败要响亮**：读不到/解析失败 → 记 ERROR 并**说明后果**，
       不静默给 `{}`（本项目最贵的 bug 类型就是静默降级）。
       返回 `{}` 只是为了让调用方不崩，**不代表成功**。
    ④ **不缓存**：被改后下次读就能拿到新的（真实现也是每次读文件/内存对象）。
       这里每次调用都重新读盘 —— 配置读得不多，正确性优先。

    Args:
        path: 显式路径；`None` → 用 `ASTRBOT_CONFIG_PATH`（照真实现约定）。

    Returns:
        配置字典；读不到时返回 `{}`（**同时已记 ERROR**）。
    """
    target = os.fspath(path) if path is not None else ASTRBOT_CONFIG_PATH
    try:
        with open(target, encoding="utf-8-sig") as fp:
            data = json.load(fp)
    except FileNotFoundError:
        logger.error(
            "AstrBot 全局配置不存在：%s —— 插件的 get_config() 将返回空字典，"
            "依赖全局配置的机制（TTS/联网搜索/timezone/tool_call_timeout）会**静默降级**。"
            "请检查 plugins.global_config_path 指向是否正确。",
            target,
        )
        return {}
    except (OSError, ValueError) as exc:
        # ValueError 覆盖 json.JSONDecodeError（它是 ValueError 子类）
        logger.error(
            "读 AstrBot 全局配置失败：%s（%s: %s）—— 同上的静默降级后果",
            target,
            type(exc).__name__,
            exc,
        )
        return {}
    if not isinstance(data, dict):
        logger.error(
            "AstrBot 全局配置不是对象（%s）：%s —— 按空配置处理",
            target,
            type(data).__name__,
        )
        return {}
    return data
