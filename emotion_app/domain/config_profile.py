# -*- coding: utf-8 -*-
"""「配置文件」——会话段 + 上下文段（照 AstrBot 的配置页分组，剔除 agent/群聊/管理员）。

## 这是什么 / 为什么单独一个模块

情绪要把「设置」页从"几个散字段"改成 AstrBot 那种**配置文件式**页面。
AstrBot 的配置页是分组的（「模型」「文本转语音」「人格」「上下文管理策略」「其他配置」…），
每个分组下若干字段。

🔴 **我们只做单用户聊天平台**，所以照 `docs/ASTRBOT_CONFIG_PAGE.md` 的实测结论
   **剔除**：agent 执行器 / 子代理编排 / 群聊 / 多平台 / 管理面板与管理员权限。
   本轮落地两段：**「会话」+「上下文管理策略」**（情绪选的）。

## 为什么字段清单写在**代码**里，而不是"照 AstrBot 抄一份 JSON"

因为这份清单同时是：①默认值来源 ②校验规则 ③**前端渲染的唯一事实来源**
（`schema()` 下发给前端，前端照着画控件，**不在 JS 里再抄一份**）。
三处合成一处，才不会出现"后端认这个键、前端画的是另一个键"的漂移。

## 落盘

`data/config_profile.json`（**不是** `/opt/astrbot` —— 那是别人 AstrBot 的生产目录）：
```json
{"version": 1, "session": {...}, "context": {...}}
```
⚠️ 与 `infra/providers_store.py` 同款质量要求：**原子写**、**损坏不静默**、**未知键保留**。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("emotion_app.config_profile")

#: 相对**运行目录**（与 `providers_store.DEFAULT_PATH` 同一约定）。
DEFAULT_PATH = "data/config_profile.json"

#: 文档版本。将来加段/改语义时递增，并写迁移。
SCHEMA_VERSION = 1

SECTION_SESSION = "session"
#: 🔴 **原来的全局 `context` 段已删除**（情绪 2026-10-09：「上下文策略一样做成会话级的，
#: 不做全局」）。那段共 10 个字段，其中 7 个**零消费**（`context_limit_reached_strategy`/
#: `llm_compress_*` 三个/`fallback_max_context_tokens` 全库 0 处读取；
#: `max_context_length`/`dequeue_context_length` 那"2 处"引用是**兼容层里同名的另一个东西**，
#: 不是消费），另外 3 个（`mode`/`protected_tail`/`summary_trigger`）与**会话级**那套重复
#: —— 而真正生效的一直是会话级（`Session.policy`）。
#:
#: ⇒ 删掉整段，改为把**真能生效的两项**加进会话级 `ContextPolicy`：
#:    `summary_model`（用哪个模型总结）与 `summary_instruction`（总结提示词）。
#: ⚠️ 旧配置文件里若还留着 `"context": {...}`，`parse()` 会当**未知段忽略**
#:    （store 的"未知键保留"行为）—— 不会报错，也不会被读。
#: 🆕 **插件段**（2026-09-27）：插件宿主的**启停名单**。
#:
#: 🔴 为什么放这里（而不是 `.env` 或 `data/cmd_config.json`）：
#:   真 AstrBot 把 `inactivated_plugins` 放在 `shared_preferences`（`data_v4.db`
#:   的 preferences 表）—— 那是一个**面板能改、重启后仍在**的落盘位置；
#:   我们对应的东西就是本 store（面板配置的唯一落盘处：原子写 + 损坏另存 +
#:   未知键保留 + 逐字段校验）。放 `.env` 面板改不动（且要从 HTTP 路由写 600
#:   权限的密钥文件），放 `cmd_config.json` 则是**发明一个真实现没有的落点**、
#:   还会污染插件可见的 `get_config()`。
SECTION_PLUGINS = "plugins"
SECTIONS: tuple[str, ...] = (SECTION_SESSION, SECTION_PLUGINS)

#: 上下文模式的合法取值（**我们已有的三种**，不是 AstrBot 的那套）。
#: ⚠️ 这三条是 `domain/chat` 里既有的组装逻辑，**本配置文件只是它们的"默认值来源"**，
#:    不替换会话级的 policy（那是每条会话自己的覆盖）。
CONTEXT_MODES: tuple[str, ...] = ("recent_only", "rolling_summary", "semantic")

#: 超限策略：照 AstrBot 的两个取值（截断 / 交给 LLM 压缩）。
CONTEXT_STRATEGIES: tuple[str, ...] = ("truncate", "llm_compress")

#: AstrBot 的默认压缩提示词（**英文原文**，照抄不翻译 —— 它是给模型看的）。
DEFAULT_COMPRESS_INSTRUCTION = (
    "Based on our full conversation history, produce a concise summary "
    "of key takeaways and/or project progress."
)


class ConfigProfileError(ValueError):
    """配置文件校验/读取失败（API 层转 400 并**点名**是哪个字段）。"""

    def __init__(self, message: str, *, field_name: str = "") -> None:
        super().__init__(message)
        self.field_name = field_name


@dataclass(frozen=True)
class FieldSpec:
    """一个配置项的元数据（**前端就照它渲染**）。"""

    key: str
    label: str
    #: 控件类型：string / int / float / bool / enum / text / provider_select
    type: str
    default: Any
    hint: str = ""
    #: enum 的选项（`[(value, label)]`）
    options: tuple[tuple[str, str], ...] = ()
    #: provider_select 用：只列这个分类下的模型
    provider_type: str = ""
    #: 数值范围（闭区间）；None = 不限制
    minimum: float | None = None
    maximum: float | None = None

    def to_public(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "default": self.default,
            "hint": self.hint,
        }
        if self.options:
            out["options"] = [{"value": v, "label": lb} for v, lb in self.options]
        if self.provider_type:
            out["provider_type"] = self.provider_type
        if self.minimum is not None:
            out["min"] = self.minimum
        if self.maximum is not None:
            out["max"] = self.maximum
        return out


#: ===========================================================================
#: 字段清单（**权威**）—— 改这里就等于改默认值/校验/前端控件
#: ===========================================================================
SPECS: dict[str, tuple[FieldSpec, ...]] = {
    SECTION_SESSION: (
        FieldSpec(
            "default_chat_provider_id", "默认对话模型", "provider_select", "",
            hint="留空时使用第一个可用模型。插件与面板都按它选模型。",
            provider_type="chat_completion",
        ),
        FieldSpec(
            "fallback_chat_provider_ids", "回退对话模型", "provider_multi", [],
            hint="主对话模型请求失败时，按顺序切换到这些模型。",
            provider_type="chat_completion",
        ),
        FieldSpec(
            "tts_enable", "启用文本转语音", "bool", False,
            hint="⚠️ 本轮只保存配置，**暂不出声**（做出声要多做音频链路）。",
        ),
        FieldSpec(
            "default_tts_provider_id", "默认文本转语音模型", "provider_select", "",
            hint="留空 = 不指定。", provider_type="text_to_speech",
        ),
        FieldSpec(
            "tts_trigger_probability", "TTS 触发概率", "float", 1.0,
            hint="0.0–1.0；1.0 = 每条都触发（将来真出声时生效）。",
            minimum=0.0, maximum=1.0,
        ),
        # ---- 语音识别（STT，2026-09-21 加；与 TTS 同为**配置层**） ----
        FieldSpec(
            "stt_enable", "启用语音识别", "bool", False,
            hint="⚠️ 只保存配置，**暂不转写** —— 我们还没实现 STT 适配器"
                 "（whisper / xinference / mimo 三条预设均标注为不可执行）。",
        ),
        FieldSpec(
            "default_stt_provider_id", "默认语音识别模型", "provider_select", "",
            hint="留空 = 不指定。⚠️ 目前列出的模型本平台都还不能真跑（配置先行）。",
            provider_type="speech_to_text",
        ),
        FieldSpec(
            "default_persona_id", "默认采用的人格", "persona_select", "",
            hint="留空 = 用当前激活的人格。",
        ),
        FieldSpec(
            "request_max_retries", "请求最大重试次数", "int", 3,
            hint="单次模型请求遇到可重试错误时的最大尝试次数。",
            minimum=0, maximum=10,
        ),
        FieldSpec(
            "display_reasoning_text", "显示思考内容", "bool", False,
            hint="开启后会显示模型的推理内容（如果模型支持）。",
        ),
        FieldSpec(
            "datetime_system_prompt", "现实世界时间感知", "bool", True,
            hint="开启后会在系统提示词中附带当前时间信息。",
        ),
        FieldSpec(
            "streaming_response", "流式输出", "bool", True,
            hint="⚠️ 我们默认**开**（本来就有流式）—— 与 AstrBot 默认值不同，是有意的。",
        ),
    ),
    SECTION_PLUGINS: (
        # 🔴 元素是**模块路径**（`<插件目录名>.main`），判据在
        #    `astrbot_host.loader.plugin_loader.normalize_module_path`（只此一份）。
        #    这里**只校验"是字符串数组"**，不校验插件存不存在 ——
        #    后者要扫插件目录，属于宿主加载器的职责（它会把对不上的项**响亮**报出来）。
        FieldSpec(
            "inactivated_plugins", "停用的插件", "string_list", [],
            hint="名单里的插件**不会被加载**（不 import、不实例化、不 initialize），"
                 "也**不算加载失败**。改完需要重启服务才生效。"
                 "元素写法：插件目录名 或 <目录名>.main 或真 AstrBot 的 "
                 "data.plugins.<目录名>.main。",
        ),
    ),
}


def _defaults(section: str) -> dict[str, Any]:
    """取某段的默认值（**深拷贝**可变默认值，防止调用方改坏 SPECS）。"""
    out: dict[str, Any] = {}
    for spec in SPECS[section]:
        out[spec.key] = json.loads(json.dumps(spec.default))
    return out


def default_profile() -> dict[str, dict[str, Any]]:
    return {sec: _defaults(sec) for sec in SECTIONS}


def schema() -> dict[str, list[dict[str, Any]]]:
    """字段元数据（**前端渲染的唯一事实来源**）。"""
    return {sec: [spec.to_public() for spec in SPECS[sec]] for sec in SECTIONS}


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def _coerce(spec: FieldSpec, value: Any) -> Any:
    """把一个外部值转成该字段的类型；不合法 → `ConfigProfileError`（**点名字段**）。

    🔴 **不静默修正**：类型不符/越界一律拒绝。
       "顺手改成合法值"会让用户以为保存成功了，实际他填的东西被悄悄改掉 ——
       本项目把这类静默失败视为最贵的 bug。
    """
    key = spec.key
    if spec.type == "bool":
        if isinstance(value, bool):
            return value
        raise ConfigProfileError(f"{spec.label}（{key}）必须是 true/false", field_name=key)

    if spec.type == "int":
        # ⚠️ 不接受 `True`（它是 int 的子类）—— bool 混进来是调用方写错了
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigProfileError(f"{spec.label}（{key}）必须是整数", field_name=key)
        n = value
    elif spec.type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigProfileError(f"{spec.label}（{key}）必须是数字", field_name=key)
        n = float(value)
    else:
        n = None

    if n is not None:
        if spec.minimum is not None and n < spec.minimum:
            raise ConfigProfileError(
                f"{spec.label}（{key}）不能小于 {spec.minimum}（收到 {n}）", field_name=key
            )
        if spec.maximum is not None and n > spec.maximum:
            raise ConfigProfileError(
                f"{spec.label}（{key}）不能大于 {spec.maximum}（收到 {n}）", field_name=key
            )
        return n

    if spec.type == "enum":
        if not isinstance(value, str):
            raise ConfigProfileError(f"{spec.label}（{key}）必须是字符串", field_name=key)
        allowed = [v for v, _ in spec.options]
        if value not in allowed:
            raise ConfigProfileError(
                f"{spec.label}（{key}）只能是 {allowed} 之一（收到 {value!r}）", field_name=key
            )
        return value

    if spec.type == "provider_multi":
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ConfigProfileError(
                f"{spec.label}（{key}）必须是字符串数组", field_name=key
            )
        return [v.strip() for v in value if v.strip()]

    if spec.type == "string_list":
        # 通用字符串数组（如插件停用名单）。⚠️ 去重但**保留书写顺序** ——
        # 顺序对用户有意义（他按自己的理解排的），我们没理由重排。
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ConfigProfileError(
                f"{spec.label}（{key}）必须是字符串数组", field_name=key
            )
        out: list[str] = []
        for v in value:
            s = v.strip()
            if s and s not in out:
                out.append(s)
        return out

    if spec.type in ("string", "text") or spec.type.endswith("_select"):
        if not isinstance(value, str):
            raise ConfigProfileError(f"{spec.label}（{key}）必须是字符串", field_name=key)
        return value if spec.type == "text" else value.strip()

    raise ConfigProfileError(f"{key} 的控件类型未知：{spec.type}", field_name=key)


def validate_patch(section: str, patch: dict[str, Any]) -> dict[str, Any]:
    """校验一段的部分更新，返回"只含合法键"的结果。

    🔴 未知键 → **400**（不静默忽略）：静默忽略会让用户以为改了、其实没改。
       ⚠️ 注意与"**落盘时保留未知键**"不冲突：那是为了不丢**别的版本**写的数据，
          这里是拒绝**用户/前端**提交我们不认识的键。
    """
    if section not in SPECS:
        raise ConfigProfileError(f"未知的配置段：{section}", field_name=section)
    if not isinstance(patch, dict):
        raise ConfigProfileError(f"{section} 必须是 JSON 对象", field_name=section)

    by_key = {s.key: s for s in SPECS[section]}
    unknown = sorted(set(patch) - set(by_key))
    if unknown:
        raise ConfigProfileError(
            f"{section} 里有不认识的配置项：{unknown}；"
            f"可用的是 {sorted(by_key)}",
            field_name=unknown[0],
        )
    return {k: _coerce(by_key[k], v) for k, v in patch.items()}


# ---------------------------------------------------------------------------
# 存取
# ---------------------------------------------------------------------------


@dataclass
class ConfigProfileStore:
    """配置文件读写（原子写 + 损坏不静默 + 未知键保留）。

    ⚠️ 用 `RLock`：`load()` 在迁移/修复时会**写盘**，
    `Lock` 会遇到"读里调写"的自锁（`providers_store` 踩过同一个坑）。
    """

    path: str | Path = DEFAULT_PATH
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    # ---- 读 ----

    def load(self) -> dict[str, dict[str, Any]]:
        """读配置；**任何缺失/损坏都回落到默认值，但绝不静默**。"""
        with self._lock:
            raw = self._read_raw()
            return self._merge_defaults(raw)

    def _read_raw(self) -> dict[str, Any]:
        if not self.path.is_file():
            # ⚠️ 文件不存在 = **正常首次运行**，不是错误（别打 ERROR 吓人）
            logger.debug("配置文件不存在（按默认值）：%s", self.path)
            return {}
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("配置文件读取失败（按默认值继续）：%s → %s", self.path, exc)
            return {}
        if not text.strip():
            # ⚠️ 空文件与"JSON 损坏"一样会**丢配置**（用户看到设置全回默认），
            #    所以处置必须一致：既留 ERROR，也留现场备份。
            #    （原来这里只打日志不备份 → 判据当场抓出不一致。）
            self._preserve_corrupt()
            logger.error("配置文件是空文件（按默认值继续，已另存 .corrupt）：%s", self.path)
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            self._preserve_corrupt()
            logger.error("配置文件 JSON 损坏（按默认值继续，已另存 .corrupt）：%s → %s",
                         self.path, exc)
            return {}
        if not isinstance(data, dict):
            self._preserve_corrupt()
            logger.error("配置文件顶层不是 JSON 对象（按默认值继续，已另存 .corrupt）：%s",
                         self.path)
            return {}
        return data

    def _preserve_corrupt(self) -> None:
        """把坏文件另存 `*.corrupt`，**别让下一次写入直接覆盖掉现场**。"""
        try:
            bak = self.path.with_suffix(self.path.suffix + ".corrupt")
            bak.write_bytes(self.path.read_bytes())
            logger.error("已把损坏的配置另存为：%s", bak)
        except OSError as exc:  # 另存失败不该让"按默认值继续"也失败
            logger.error("另存损坏配置失败：%s", exc)

    def _merge_defaults(self, raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """默认值 ← 落盘值（**逐字段**合并：落盘里缺的键用默认补齐）。

        ⚠️ 落盘值**不重新校验**（它已经过校验才写进去）；
           但**类型明显不对的**要剔掉并告警 —— 比如有人手工把 `bool` 写成了字符串，
           原样下发给前端会让控件渲染错乱。
        """
        out = default_profile()
        for sec in SECTIONS:
            got = raw.get(sec)
            if got is None:
                continue
            if not isinstance(got, dict):
                logger.error("配置段 %s 不是对象（该段按默认值）：%r", sec, type(got).__name__)
                continue
            by_key = {s.key: s for s in SPECS[sec]}
            for k, v in got.items():
                spec = by_key.get(k)
                if spec is None:
                    # ⚠️ **未知键照原样保留**（可能是更新版本写的），但要能看见
                    out[sec][k] = v
                    logger.debug("配置段 %s 里有本版本不认识的键（保留）：%s", sec, k)
                    continue
                try:
                    out[sec][k] = _coerce(spec, v)
                except ConfigProfileError as exc:
                    logger.error(
                        "配置段 %s 的 %s 值不合法（该字段按默认值 %r）：%s",
                        sec, k, spec.default, exc,
                    )
        return out

    # ---- 写 ----

    def patch(self, changes: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """按段做部分更新（**只覆盖传进来的键**），返回更新后的完整配置。"""
        with self._lock:
            current_raw = self._read_raw()
            for sec, patch in changes.items():
                validated = validate_patch(sec, patch)
                current_raw.setdefault(sec, {})
                if not isinstance(current_raw[sec], dict):
                    current_raw[sec] = {}
                current_raw[sec].update(validated)
            self._write_all(current_raw)
            return self._merge_defaults(current_raw)

    def reset(self) -> dict[str, dict[str, Any]]:
        """恢复默认（**删掉覆盖**，保留未知键与 version）。"""
        with self._lock:
            current_raw = self._read_raw()
            for sec in SECTIONS:
                current_raw.pop(sec, None)
            self._write_all(current_raw)
            return self._merge_defaults(current_raw)

    def _write_all(self, data: dict[str, Any]) -> None:
        """原子写：同目录临时文件 → fsync → `os.replace`。

        ⚠️ 半截 JSON 会让下次启动读出一份**缺字段**的配置，
           而"缺字段"用的是默认值 —— 用户会以为自己的设置丢了。
        """
        payload = dict(data)
        payload["version"] = SCHEMA_VERSION
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            # 失败要把临时文件收掉，否则 data/ 会攒一地 .tmp
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        logger.info("配置文件已保存：%s", self.path)


__all__ = [
    "ConfigProfileError",
    "ConfigProfileStore",
    "CONTEXT_MODES",
    "CONTEXT_STRATEGIES",
    "DEFAULT_PATH",
    "FieldSpec",
    "SCHEMA_VERSION",
    "SECTION_PLUGINS",
    "SECTION_SESSION",
    "SECTIONS",
    "SPECS",
    "default_profile",
    "schema",
    "validate_patch",
]
