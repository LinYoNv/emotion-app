"""Expose the checkout under its canonical package name for tests only."""
from __future__ import annotations

import importlib.machinery
import sys
import types
from pathlib import Path


PACKAGE_NAME = "astrbot_plugin_memory_companion"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def bootstrap_package(plugin_root: Path = PLUGIN_ROOT) -> Path:
    """Register this checkout as a package without executing the plugin entrypoint."""
    root = plugin_root.resolve()
    existing = sys.modules.get(PACKAGE_NAME)
    if existing is not None:
        search_paths = {str(Path(path).resolve()) for path in getattr(existing, "__path__", ())}
        if str(root) not in search_paths:
            raise RuntimeError(f"{PACKAGE_NAME} is already loaded from a different checkout")
        return root

    package = types.ModuleType(PACKAGE_NAME)
    package.__file__ = str(root / "__init__.py")
    package.__loader__ = None
    package.__package__ = PACKAGE_NAME
    package.__path__ = [str(root)]
    package_spec = importlib.machinery.ModuleSpec(PACKAGE_NAME, loader=None, is_package=True)
    package_spec.submodule_search_locations = [str(root)]
    package.__spec__ = package_spec
    sys.modules[PACKAGE_NAME] = package
    return root
