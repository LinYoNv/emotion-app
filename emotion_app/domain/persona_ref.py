# -*- coding: utf-8 -*-
"""人设参考图：落盘目录 + **唯一**的路径解析判据（2026-09-25）。

## 为什么单独一个模块

参考图这个概念的**写入路径有两条**（存文件名 / 解析成绝对路径），
而本项目反复踩过同一个坑：**"同一概念有多条写入路径"时，判据必须抽成一个函数**
（见 `AGENTS.md`：供应商类型过滤 / `LLMResponse` 三条转换路径 / 平台元数据三处来源）。

所以这里只做两件事：
* `PERSONA_REF_DIR` —— 落盘位置（和人设数据同源，备份 `data/` 一起带走）；
* `resolve_persona_reference()` —— **唯一**的"取哪个文件"的判据。

## 三级优先（有且只有这一个地方定）

```
① 模型显式给的 reference_image_path      （在 image_tool 里，最高）
② 人设自己的 reference_image_path        ← 本模块解析（用户面板上传的）
③ 全局 EMOTION_IMAGE_PERSONA_REFERENCE   （兜底，给"只有一个角色"的场景）
```

⚠️ ② 存的是**纯文件名**（理由同 `avatar_path`：不把宿主机目录结构写进数据）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..infra.logging_setup import get_logger

log = get_logger(__name__)

#: 人设参考图落盘目录（相对运行目录）。
#: 与人设数据同源 —— 备份 `data/` 时参考图跟着一起走，不会"人设还在、脸没了"。
PERSONA_REF_DIR = "data/persona_refs"

#: 单图上限。与 `image_gen.MAX_IMAGE_BYTES`(20MB) 取齐 —— 但**更严**：
#: 参考图只是"长相锚点"，不是作品本体；上游实测要 data URI，
#: 太大既拖慢请求又容易被网关拒绝。
MAX_PERSONA_REF_BYTES = 12 * 1024 * 1024

#: 允许的格式。与 `image_gen.reference_data_uri` 认的 mime 一致（png/jpeg/webp）。
ALLOWED_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


def ref_dir() -> Path:
    """参考图目录（不存在则建）。"""
    d = Path(PERSONA_REF_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_safe_name(name: str) -> bool:
    """文件名是否安全（必须是不含路径的纯文件名，且不是隐藏文件）。

    防路径穿越 —— 与 `api_admin.get_avatar` 同款判断，**不要只信上传时校验过**：
    存储里的值也可能被手工改过（或来自旧备份）。
    """
    if not name:
        return False
    if Path(name).name != name:
        return False
    if name.startswith("."):
        return False
    return name.lower().endswith(ALLOWED_SUFFIXES)


def resolve_persona_reference(persona: Any | None) -> str:
    """把人设的 `reference_image_path` 解析成**绝对路径**；解析不出来返回 `""`。

    这是"人设参考图"的**唯一判据**。返回 `""` 的每一种情况都**留日志** ——
    本项目最贵的一课就是"参考图被静默丢弃/替换"（aitag 事故），
    那种 bug 用户只能凭感觉报「好像没用上参考图」。

    Args:
        persona: `Persona` 或 `None`（没人设 → 空）。

    Returns:
        可读文件的绝对路径；没有 / 不安全 / 文件不在 → `""`。
    """
    if persona is None:
        return ""
    name = str(getattr(persona, "reference_image_path", "") or "").strip()
    if not name:
        return ""

    if not is_safe_name(name):
        # 🔴 响亮失败（不静默回落）—— 这要么是数据被改坏，要么是路径穿越尝试
        log.warning(
            "人设参考图文件名非法，按未配置处理",
            extra={"data": {"persona": getattr(persona, "id", ""), "name": name[:80]}},
        )
        return ""

    path = ref_dir() / name
    if not path.is_file():
        # 🔴 记录"名字有、文件没了"—— 和"从来没配过"是**两件不同的事**，
        #    排查时必须能区分（否则用户报"画得不像"时无从下手）。
        log.warning(
            "人设参考图文件不存在，按未配置处理",
            extra={"data": {"persona": getattr(persona, "id", ""), "file": name}},
        )
        return ""

    return str(path.resolve())


def public_view(persona: Any | None) -> dict[str, Any]:
    """给面板的脱敏视图（**不暴露宿主机路径**）。"""
    if persona is None:
        return {"configured": False, "file": ""}
    name = str(getattr(persona, "reference_image_path", "") or "")
    return {
        "configured": bool(name and (ref_dir() / name).is_file()),
        "file": Path(name).name if name else "",
    }
