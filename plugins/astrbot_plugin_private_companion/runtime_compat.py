from __future__ import annotations

import importlib
import importlib.metadata
from dataclasses import asdict, dataclass, field
from typing import Any


_REQUIRED_CAPABILITIES = (
    "star_base",
    "data_dir",
    "event_hooks",
    "provider_request",
    "message_components",
)


@dataclass(frozen=True)
class RuntimeCapabilities:
    plugin_name: str
    plugin_version: str
    astrbot_version: str = "unknown"
    api_generation: str = "unknown"
    compatibility_level: str = "unsupported"
    capabilities: dict[str, bool] = field(default_factory=dict)
    missing_required: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["missing_required"] = list(self.missing_required)
        result["warnings"] = list(self.warnings)
        return result


def _probe_import(module_name: str, attribute_name: str) -> bool:
    try:
        module = importlib.import_module(module_name)
        return hasattr(module, attribute_name)
    except Exception:
        return False


def _version_from_module() -> str:
    for module_name in ("astrbot", "astrbot.core"):
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        for attr_name in ("__version__", "VERSION", "version"):
            value = getattr(module, attr_name, "")
            if value:
                return str(value)
    for distribution_name in ("astrbot", "AstrBot"):
        try:
            return importlib.metadata.version(distribution_name)
        except importlib.metadata.PackageNotFoundError:
            continue
        except Exception:
            break
    return "unknown"


def _probe_event_hooks() -> dict[str, bool]:
    try:
        module = importlib.import_module("astrbot.api.event")
        event_filter = getattr(module, "filter", None)
    except Exception:
        event_filter = None
    return {
        "event_hooks": event_filter is not None,
        "llm_request_hook": callable(getattr(event_filter, "on_llm_request", None)),
        "llm_response_hook": callable(getattr(event_filter, "on_llm_response", None)),
        "event_message_type": callable(getattr(event_filter, "event_message_type", None)),
        "command_group": callable(getattr(event_filter, "command_group", None)),
    }


def probe_runtime_capabilities(
    *,
    context: Any = None,
    event: Any = None,
    plugin_name: str = "",
    plugin_version: str = "",
) -> RuntimeCapabilities:
    """Probe optional AstrBot APIs without making import-time decisions."""

    capabilities = {
        "star_base": _probe_import("astrbot.api.star", "Star"),
        "data_dir": _probe_import("astrbot.api.star", "StarTools"),
        "provider_request": _probe_import("astrbot.api.provider", "ProviderRequest"),
        "message_components": _probe_import("astrbot.api.message_components", "Plain")
        or _probe_import("astrbot.core.message.components", "Plain"),
    }
    capabilities.update(_probe_event_hooks())
    capabilities["web_api"] = callable(getattr(context, "register_web_api", None))
    capabilities["platform_manager"] = getattr(context, "platform_manager", None) is not None
    capabilities["event_unified_msg_origin"] = bool(event is not None and hasattr(event, "unified_msg_origin"))
    capabilities["event_send"] = any(
        callable(getattr(event, name, None)) for name in ("send", "send_message", "plain_result")
    )
    capabilities["event_stop"] = callable(getattr(event, "stop_event", None))

    missing_required = tuple(name for name in _REQUIRED_CAPABILITIES if not capabilities.get(name, False))
    warnings: list[str] = []
    if not capabilities.get("web_api"):
        warnings.append("runtime_web_api_unavailable")
    if not capabilities.get("platform_manager"):
        warnings.append("runtime_platform_manager_unavailable")
    if not capabilities.get("event_unified_msg_origin"):
        warnings.append("runtime_event_scope_unavailable_until_event")
    if missing_required:
        level = "unsupported"
    elif warnings:
        level = "degraded"
    else:
        level = "full"
    version = _version_from_module()
    api_generation = "4.x" if version.startswith("4.") else "unknown"
    return RuntimeCapabilities(
        plugin_name=plugin_name,
        plugin_version=plugin_version,
        astrbot_version=version,
        api_generation=api_generation,
        compatibility_level=level,
        capabilities=capabilities,
        missing_required=missing_required,
        warnings=tuple(warnings),
    )


__all__ = ["RuntimeCapabilities", "probe_runtime_capabilities"]
