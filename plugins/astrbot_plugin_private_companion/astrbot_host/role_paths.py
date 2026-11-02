"""Shared per-role migration route paths for host and setup inventories."""

from companion_v2.execution.models import digest


HOST_PREFIX = "/astrbot_plugin_private_companion/host"


def role_legacy_paths(target_id, role_id, scope_kind):
    """Return the migration routes owned by one target role."""
    base = HOST_PREFIX + "/targets/" + target_id + "/roles/" + digest(role_id)
    group = scope_kind == "group"
    return {"legacy_api": None if group else base + "/legacy",
            "legacy_group_data_api": base + "/legacy-group-data" if group else None}
