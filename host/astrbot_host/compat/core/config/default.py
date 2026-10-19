# -*- coding: utf-8 -*-
"""`astrbot.core.config.default` 兼容层 —— 默认配置元数据。

⭐ 依据：真 AstrBot `core/config/default.py`（v4.27.5，只读参考）。

## 为什么必须补（2026-10-19，陪伴 7.0.0 实测）

陪伴 `astrbot_host/model_api.py:932` 用它读**模型供应商的 schema 模板**：

```python
from astrbot.core.config.default import CONFIG_METADATA_2
provider_metadata = (CONFIG_METADATA_2.get("provider_group", {})
                     .get("metadata", {}).get("provider", {}))
config_templates = deepcopy(provider_metadata.get("config_template", {}) or {})
field_metadata   = deepcopy(provider_metadata.get("items", {}) or {})
```

⇒ 它读的是 `CONFIG_METADATA_2["provider_group"]["metadata"]["provider"]`，
用来**动态渲染"新建供应商"表单**（模板 + 每字段的说明）。缺了它那段有
`except Exception` 兜底，只是退化成"从现有配置反推" ⇒ **新建表单不完整**。

## 与真实现的差异（如实记录 —— 这一点很重要）

真实现的 `CONFIG_METADATA_2` **4536 行**，含 `platform_group` / `provider_group` /
`misc_config_group`，而且顶层就 `import astrbot.core.computer.booters.cua_defaults`
（一个我们完全没有的子系统）。

⇒ 我们**只提取 `provider_group.metadata`**，落成同目录的
`provider_metadata.json`（**57,718 字节，从真 4.27.5 源码 AST 直接求值得到，
不是手抄的** —— 手抄必漂）。

🔴 **只保留插件真正读的键**：
`provider` / `provider_settings` / `provider_stt_settings` /
`provider_tts_settings` / `provider_ltm_settings`（5 个）。
`CONFIG_METADATA_2` 里**没有** `platform_group` 与 `misc_config_group` ——
如果将来有插件读它们，`get(...)` 会拿到 `{}` 而不是崩溃，
但**那意味着模板缺失**，所以这里在取值时**显式记一条 debug 日志**（不静默）。

⚠️ 数据文件缺失时的行为：返回**空结构**并**响亮地 warning 一次**。
不抛异常 —— 真实现有 `try/except`，抛了反而改变插件控制流；
但也不静默 —— 否则"新建供应商表单缺模板"会变成一个查不出来的怪现象。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from astrbot_host.compat import __version__ as _SHADOW_VERSION

logger = logging.getLogger("astrbot")

#: 照真实现（`core/config/default.py:9`：`VERSION = __version__`）
VERSION = _SHADOW_VERSION

#: 元数据文件名（与本模块同目录）
_METADATA_FILE = Path(__file__).with_name("provider_metadata.json")

#: 🔴 真实现 `CONFIG_METADATA_2` 的**顶层键**（供判据核对"我们只提供了哪一层"）
MISSING_GROUPS = ("platform_group", "misc_config_group")


def _load_provider_metadata() -> dict:
    """读 provider 元数据（失败时返回空结构 + warning，见模块 docstring）。"""
    try:
        with _METADATA_FILE.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as error:
        logger.warning(
            "provider 元数据不可用（%s: %s）→ 供应商表单将退化为从现有配置反推。"
            "文件应为 %s",
            type(error).__name__,
            error,
            _METADATA_FILE,
        )
        return {}
    if not isinstance(data, dict):
        logger.warning("provider 元数据形状异常（不是对象）→ 按空处理：%s", _METADATA_FILE)
        return {}
    return data


_PROVIDER_METADATA = _load_provider_metadata()

#: 照真实现的顶层形状 —— **只含 provider_group**（差异见模块 docstring）
CONFIG_METADATA_2: dict = {
    "provider_group": {
        "name": "模型供应商",
        "metadata": _PROVIDER_METADATA,
    },
}


def get_provider_metadata() -> dict:
    """取 `provider_group.metadata`（插件真正读的那一层）。"""
    return CONFIG_METADATA_2.get("provider_group", {}).get("metadata", {})


__all__ = ["CONFIG_METADATA_2", "VERSION", "MISSING_GROUPS", "get_provider_metadata"]
