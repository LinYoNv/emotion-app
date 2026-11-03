# -*- coding: utf-8 -*-
"""适配器配置存储 —— 消息平台适配器（OneBot v11 / 内置 Web 端）。

## 这个模块守的三条纪律（都有真实背景）

1. 🔴 **落盘在我们自己的数据目录**（`data/adapters.json`），
   **绝不写 `/opt/astrbot`** —— 那是别人正在跑的 AstrBot 生产目录。
2. 🔴 **损坏不静默**：文件缺失 = 正常（首次使用）；文件坏了 = **记 ERROR 并按空配置继续**。
   绝不静默改用户数据，也绝不静默吞掉。
3. 🔴 **凭据与展示分离**：`access_token` 读接口一律**打码**（`_mask()`），
   写接口收到打码值时**保持原值不改**（否则用户一保存就把 token 洗成 `••••`）。
   这条照 `providers_store` 的同款做法 —— 那是我在模型供应商页踩过的真 bug。

## 为什么适配器要独立一份文件，而不塞进 config.py

`config.py` 是**启动期读一次的静态配置**（`.env` 驱动），而适配器是
**运行期增删改**的资源（用户在面板上「添加 OneBot」「停用」）。
两者生命周期不同：塞进 `Config` 会让"改一条适配器"必须重启进程。
⇒ 照 `providers_store` 的做法：**独立 JSON + 运行期可改 + 变更后热重载**。

## 数据结构

```json
{
  "version": 1,
  "adapters": [
    {
      "id": "onebot-a1b2c3",
      "type": "onebot_v11",
      "name": "我的 QQ 机器人",
      "enabled": true,
      "config": {
        "ws_reverse_host": "0.0.0.0",
        "ws_reverse_port": 6199,
        "ws_reverse_token": "",
        "access_whitelist": ["123456789"],
        "whitelist_enabled": true
      }
    }
  ]
}
```

⚠️ `config` 的**具体字段由适配器类型决定**（见 `ADAPTER_TYPES`），
这里只做"是 dict"这一层检查 —— 逐字段校验交给各适配器的 `validate_config()`，
因为不同类型的必填项完全不同（OneBot 要端口，内置 Web 端一个都不用）。
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 落盘路径（相对运行目录；与 `data/providers.json` 同级）
DEFAULT_PATH = "data/adapters.json"

#: 当前存储格式版本。将来迁移时据此分支，**不猜**。
STORE_VERSION = 1

#: 适配器类型白名单。
#:
#: ⚠️ 新增类型时**必须同时**在这里登记 + 在 `adapters/__init__.py` 的
#:    `build_adapter()` 里实现构造，否则面板能存但起不来（"能存不能用"）。
ADAPTER_TYPES: dict[str, dict[str, Any]] = {
    "onebot_v11": {
        "label": "OneBot v11（QQ）",
        "description": "适用于 OneBot V11 标准的消息平台，支持反向 WebSocket。",
        #: 该类型配置项（面板据此渲染表单，**不用在前端硬编码字段**）
        "fields": [
            {
                "key": "ws_reverse_host",
                "label": "监听地址",
                "type": "string",
                "default": "0.0.0.0",
                "required": True,
                "help": "协议端（NapCat / LLOneBot / go-cqhttp）连接过来的地址。",
            },
            {
                "key": "ws_reverse_port",
                "label": "监听端口",
                "type": "int",
                "default": 6199,
                "required": True,
                "help": "协议端要配置成连到这里，形如 ws://<本机>:6199/ws",
            },
            {
                "key": "ws_reverse_token",
                "label": "访问令牌（可选）",
                "type": "secret",
                "default": "",
                "required": False,
                "help": "填了就必须与协议端配置的 token 一致，否则连接被拒。",
            },
            {
                "key": "whitelist_enabled",
                "label": "启用白名单",
                "type": "bool",
                "default": True,
                "required": False,
                "help": "开启后只有名单内的 QQ 号能触发机器人。",
            },
            {
                "key": "access_whitelist",
                "label": "QQ 白名单",
                "type": "string_list",
                "default": [],
                "required": False,
                "help": "每行一个 QQ 号。空白名单 + 启用白名单 = 谁都进不来（如实拒绝，不默认放行）。",
            },
            {
                "key": "image_path_map",
                "label": "图片路径映射（协议端在容器里时填）",
                "type": "string_list",
                "default": [],
                "required": False,
                "help": (
                    "每行一条 `容器内前缀=宿主机前缀`。协议端跑在 Docker 里时，"
                    "它返回的图片路径是**容器内**的，宿主进程读不到 —— 这里做映射。"
                    "例如 NapCat 把 /app/.config/QQ 挂到宿主机 /opt/napcat/bot1/ntqq，"
                    "就填 `/app/.config/QQ=/opt/napcat/bot1/ntqq`。留空 = 不做映射。"
                ),
            },
        ],
    },
    "webchat": {
        "label": "情绪app 内置 Web 端",
        "description": "面板里自带的聊天入口。它是内置平台，不需要连接参数。",
        "fields": [],
        #: 内置适配器不可删除（它是面板聊天的来源）
        "builtin": True,
    },
}


class AdapterError(Exception):
    """适配器配置类错误的基类。"""


class AdapterValidationError(AdapterError, ValueError):
    """字段校验失败（→ 400）。"""


class AdapterNotFound(AdapterError, KeyError):
    """按 id 找不到（→ 404）。"""


def _mask(value: str | None, *, show: int = 4) -> str:
    """把敏感值打码成 `••••abcd`（保留末几位便于用户确认填对了）。

    ⚠️ 空值**原样返回空串**，不要返回 `••••` —— 否则用户分不清
    "没填" 与 "填了但被藏起来"，保存时会把空值当成"保持不变"从而删不掉。
    """
    if not value:
        return ""
    text = str(value)
    if len(text) <= show:
        return "•" * 8
    return "•" * 8 + text[-show:]


def _is_mask(value: Any) -> bool:
    """判断是不是我们返回过的打码值（用它来识别"用户没改这一项"）。"""
    return isinstance(value, str) and value.startswith("•")


def _new_adapter_id(adapter_type: str) -> str:
    """生成适配器 id：`<类型前缀>-<随机>`。

    ⚠️ **不要用序号**（`onebot-1`）：删了再建会撞号，
    而 id 是会话数据里 umo 的一部分 —— 撞号等于两段历史混在一起。
    """
    prefix = "onebot" if adapter_type == "onebot_v11" else adapter_type
    return f"{prefix}-{secrets.token_hex(3)}"


def _coerce_str_list(raw: Any, *, where: str) -> list[str]:
    """把白名单规范成 `list[str]`：去空白、去空项、去重（保序）。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        # 允许面板提交 "123\\n456" 这种文本域形式
        # ⚠️ 这里必须是**真正的换行符**。写成 `"\\n"`（字面反斜杠+n）的话，
        #    `splitlines()` 根本不会在那里切开 —— 实测逗号分隔的白名单会被
        #    当成一个巨型 QQ 号。踩过一次，别再改回去。
        raw = [line for line in raw.replace(",", "\n").splitlines()]
    if not isinstance(raw, (list, tuple)):
        raise AdapterValidationError(f"{where} 必须是字符串列表")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


@dataclass
class AdapterRecord:
    """一条适配器配置（**内部表示**，含真实凭据）。"""

    id: str
    type: str
    name: str
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict)

    def type_meta(self) -> dict[str, Any]:
        return ADAPTER_TYPES.get(self.type, {})

    def validate(self) -> None:
        """校验 `type` 与 `config`。**逐字段规则来自 `ADAPTER_TYPES`**。

        🔴 这里只做"通用形状"检查（类型在不在白名单、config 是不是 dict、
           字段有没有多余/缺失、类型对不对）。**不判断"值合不合理"**
           （端口范围、host 合法域名等）—— 那些是运行期连不上时如实报错更准确，
           而且不同适配器的取值域完全不同。
        """
        if self.type not in ADAPTER_TYPES:
            raise AdapterValidationError(
                f"未知的适配器类型 {self.type!r}；支持：{sorted(ADAPTER_TYPES)}"
            )
        if not isinstance(self.config, dict):
            raise AdapterValidationError("config 必须是一个对象")

        meta = self.type_meta()
        known = {f["key"]: f for f in meta.get("fields", [])}
        # ① 未知字段：**响亮拒绝**。静默丢弃会让用户以为"填了但没生效"。
        unknown = set(self.config) - set(known)
        if unknown:
            raise AdapterValidationError(
                f"适配器 {self.type} 不认识这些配置项：{sorted(unknown)}"
            )
        # ② 逐项按声明的类型转换 + 必填检查
        normalized: dict[str, Any] = {}
        for key, spec in known.items():
            raw = self.config.get(key, spec.get("default"))
            kind = spec.get("type", "string")
            if kind == "int":
                try:
                    value: Any = int(raw if raw not in (None, "") else spec.get("default") or 0)
                except (TypeError, ValueError):
                    raise AdapterValidationError(f"{spec['label']} 必须是整数") from None
            elif kind == "bool":
                value = bool(raw)
            elif kind == "string_list":
                value = _coerce_str_list(raw, where=spec["label"])
            elif kind == "secret":
                # 打码值 = 用户没改 → 保持原值（还原在 `merge_secret()` 里做）
                value = "" if raw is None else str(raw)
            else:
                value = "" if raw is None else str(raw)
            if spec.get("required") and value in ("", None, 0):
                raise AdapterValidationError(f"{spec['label']} 是必填项")
            normalized[key] = value
        self.config = normalized

    def to_public(self) -> dict[str, Any]:
        """给前端的表示：**凭据一律打码**。"""
        meta = self.type_meta()
        secrets = {f["key"] for f in meta.get("fields", []) if f.get("type") == "secret"}
        public_config = {
            k: (_mask(v) if k in secrets else v) for k, v in self.config.items()
        }
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "enabled": self.enabled,
            "config": public_config,
            # 前端据此渲染表单，不需要硬编码字段清单
            "fields": meta.get("fields", []),
            "type_label": meta.get("label", self.type),
            "type_description": meta.get("description", ""),
            "builtin": bool(meta.get("builtin")),
        }

    def to_storage(self) -> dict[str, Any]:
        """落盘表示：**明文**（文件权限由运行环境保证；它本机自用）。"""
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "enabled": self.enabled,
            "config": dict(self.config),
        }


class AdapterStore:
    """适配器配置的读写门面。**所有落盘都走这里**（单一写入路径）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or DEFAULT_PATH)
        self._records: dict[str, AdapterRecord] = {}
        self._loaded = False

    # ---- 读 ----

    def load(self) -> None:
        """从磁盘读；缺失=空配置（正常），损坏=记 ERROR 并用空配置（不静默）。"""
        self._records = {}
        self._loaded = True
        if not self.path.exists():
            logger.info("适配器配置不存在（首次使用）：%s", self.path)
            self._bootstrap_builtin()
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            # 🔴 损坏**不静默**：记 ERROR 继续，让用户能从面板看到"配置读不出来"
            logger.error(
                "适配器配置损坏，已按空配置继续（原文件保留未动）：%s → %s",
                self.path,
                exc,
            )
            self._bootstrap_builtin()
            return

        if not isinstance(raw, dict):
            logger.error("适配器配置格式不对（顶层不是对象），已按空配置继续：%s", self.path)
            self._bootstrap_builtin()
            return

        version = raw.get("version")
        if version != STORE_VERSION:
            # ⚠️ 版本不认识时**不要猜**：明确记 ERROR，并按"尽力读取"处理
            logger.error(
                "适配器配置版本是 %r，本机只认识 %r —— 会尽力读取，但可能漏字段",
                version,
                STORE_VERSION,
            )

        items = raw.get("adapters")
        if not isinstance(items, list):
            logger.error("适配器配置里 `adapters` 不是列表，已按空配置继续")
            self._bootstrap_builtin()
            return

        for item in items:
            if not isinstance(item, dict):
                logger.error("跳过一条格式不对的适配器配置：%r", item)
                continue
            try:
                rec = AdapterRecord(
                    id=str(item["id"]),
                    type=str(item["type"]),
                    name=str(item.get("name") or item["type"]),
                    enabled=bool(item.get("enabled", True)),
                    config=dict(item.get("config") or {}),
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.error("跳过一条残缺的适配器配置（%s）：%r", exc, item)
                continue
            self._records[rec.id] = rec

        self._bootstrap_builtin()

    def _bootstrap_builtin(self) -> None:
        """确保内置 Web 端适配器**始终存在且启用**。

        🔴 为什么必须自动补：面板聊天走的就是它。用户把它删了，
           整个面板聊天会静默失效 —— 那是"删了一个看不见的按钮"。
           ⇒ 内置适配器只允许改名字，不允许删除（真删由 `delete()` 拒绝）。
        """
        if any(r.type == "webchat" for r in self._records.values()):
            return
        rec = AdapterRecord(
            id="webchat",
            type="webchat",
            name="情绪app 内置 Web 端",
            enabled=True,
            config={},
        )
        self._records[rec.id] = rec

    def all(self) -> list[AdapterRecord]:
        if not self._loaded:
            self.load()
        # 内置的排前面，其余按名字
        return sorted(
            self._records.values(),
            key=lambda r: (not r.type_meta().get("builtin"), r.name, r.id),
        )

    def get(self, adapter_id: str) -> AdapterRecord:
        if not self._loaded:
            self.load()
        rec = self._records.get(str(adapter_id))
        if rec is None:
            raise AdapterNotFound(adapter_id)
        return rec

    # ---- 写 ----

    def save(self) -> None:
        """落盘。**原子写**（先写 `.tmp` 再 `replace`），避免半截文件。

        ⚠️ 半截 JSON 会让我们下次启动时"配置损坏"，而那是我自己造成的 ——
           所以必须原子替换。
        """
        payload = {
            "version": STORE_VERSION,
            "adapters": [r.to_storage() for r in self.all()],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)
        logger.info("适配器配置已保存：%s（%d 条）", self.path, len(self._records))

    def add(self, *, adapter_type: str, name: str, config: dict[str, Any],
            enabled: bool = True) -> AdapterRecord:
        if not self._loaded:
            self.load()
        rec = AdapterRecord(
            id=_new_adapter_id(adapter_type),
            type=adapter_type,
            name=str(name or "").strip() or ADAPTER_TYPES.get(adapter_type, {}).get("label", adapter_type),
            enabled=bool(enabled),
            config=dict(config or {}),
        )
        if rec.type_meta().get("builtin"):
            # 内置类型只允许一条
            raise AdapterValidationError(
                f"{rec.type_meta().get('label')} 是内置适配器，已经有了，不能重复添加"
            )
        rec.validate()
        self._records[rec.id] = rec
        self.save()
        return rec

    def update(self, adapter_id: str, *, name: Any = None, config: Any = None,
               enabled: Any = None) -> AdapterRecord:
        """更新。**打码的 secret 要还原成原值**，否则一保存就把 token 洗掉。"""
        rec = self.get(adapter_id)
        if name is not None:
            text = str(name).strip()
            if text:
                rec.name = text
        if enabled is not None:
            if rec.type_meta().get("builtin") and not enabled:
                raise AdapterValidationError("内置 Web 端适配器不能停用（面板聊天依赖它）")
            rec.enabled = bool(enabled)
        if config is not None:
            if not isinstance(config, dict):
                raise AdapterValidationError("config 必须是一个对象")
            merged = dict(rec.config)
            for key, value in config.items():
                # 🔴 打码值 = 用户没改这一项 → **保持原值**
                if _is_mask(value):
                    continue
                merged[key] = value
            rec.config = merged
        rec.validate()
        self.save()
        return rec

    def delete(self, adapter_id: str) -> None:
        rec = self.get(adapter_id)
        if rec.type_meta().get("builtin"):
            raise AdapterValidationError(
                f"{rec.type_meta().get('label')} 是内置适配器，不能删除"
            )
        self._records.pop(rec.id, None)
        self.save()


__all__ = [
    "ADAPTER_TYPES",
    "STORE_VERSION",
    "DEFAULT_PATH",
    "AdapterError",
    "AdapterValidationError",
    "AdapterNotFound",
    "AdapterRecord",
    "AdapterStore",
    "_mask",
    "_is_mask",
]
