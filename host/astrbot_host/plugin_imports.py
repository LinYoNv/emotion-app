"""Resolve plugin-local absolute imports without loading a second module graph."""
from __future__ import annotations

import ast
import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType


_MISSING = object()


@dataclass
class PluginAliasInstallation:
    names: tuple[str, ...] = ()
    changes: list[tuple[str, ModuleType, ModuleType, object, object]] = field(default_factory=list)

    def rollback(self) -> None:
        for name, canonical, parent, previous, previous_attr in reversed(self.changes):
            child = name.rsplit(".", 1)[-1]
            if sys.modules.get(name) is canonical:
                if previous is _MISSING:
                    del sys.modules[name]
                else:
                    sys.modules[name] = previous
            if vars(parent).get(child, _MISSING) is canonical:
                if previous_attr is _MISSING:
                    delattr(parent, child)
                else:
                    setattr(parent, child, previous_attr)
        self.changes.clear()


def _module_file(module: object) -> Path | None:
    source = getattr(module, "__file__", None)
    return Path(source).resolve() if isinstance(source, str) and source else None


def _absolute_imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{item.name}" for item in node.names if item.name != "*")
    return names


def _alias_targets(root: Path, namespace: str) -> dict[str, Path]:
    targets: dict[str, Path] = {}
    for package in sorted(root.iterdir()):
        if not package.is_dir() or not package.name.isidentifier():
            continue
        host = sys.modules.get(package.name)
        if host is None or not hasattr(host, "__path__"):
            continue
        canonical = sys.modules.get(f"{namespace}.{package.name}")
        if canonical is None:
            continue
        if host is canonical or _module_file(host) == (package / "__init__.py").resolve():
            continue
        if not (package / "__init__.py").is_file():
            continue
        requested: set[str] = set()
        for source in sorted(package.rglob("*.py")):
            tree = ast.parse(source.read_bytes(), filename=str(source))
            requested.update(_absolute_imports(tree))
        for name in sorted(requested):
            parts = name.split(".")
            if parts[0] != package.name or len(parts) < 2 or not all(p.isidentifier() for p in parts):
                continue
            candidate = package.joinpath(*parts[1:]).with_suffix(".py")
            if not candidate.is_file():
                continue
            source = candidate.resolve()
            if not source.is_relative_to(package.resolve()):
                raise RuntimeError(f"Plugin import escapes its private package: {name}: {source}")
            targets[name] = source
    return targets


def install_plugin_aliases(plugin_root: Path, namespace: str) -> PluginAliasInstallation:
    """Bind requested leaf names to canonical modules; roll back the batch on failure."""
    root = Path(plugin_root).resolve()
    targets = _alias_targets(root, namespace)
    installation = PluginAliasInstallation()
    changes = installation.changes
    installed: list[str] = []
    visiting: set[str] = set()

    def bind(name: str) -> None:
        if name in installed:
            return
        if name in visiting:
            raise RuntimeError(f"Cyclic plugin absolute imports: {name}")
        visiting.add(name)
        source = targets[name]
        canonical_name = f"{namespace}.{name}"
        canonical = sys.modules.get(canonical_name)
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is None or not hasattr(parent, "__path__"):
            raise RuntimeError(f"Plugin alias requires an existing host parent package: {name}")
        previous = sys.modules.get(name, _MISSING)
        previous_attr = vars(parent).get(child, _MISSING)
        if previous is not _MISSING and (canonical is None or previous is not canonical):
            raise RuntimeError(f"Plugin import alias conflicts with an existing module: {name}")
        if previous_attr is not _MISSING and (canonical is None or previous_attr is not canonical):
            raise RuntimeError(f"Plugin import alias conflicts with an existing parent attribute: {name}")
        if canonical is None:
            dependencies = _absolute_imports(ast.parse(source.read_bytes(), filename=str(source)))
            for dependency in sorted(dependencies & targets.keys()):
                bind(dependency)
            canonical = importlib.import_module(canonical_name)
        if _module_file(canonical) != source or canonical.__name__ != canonical_name:
            raise RuntimeError(
                f"Plugin canonical module has an unexpected source: {canonical_name}: "
                f"expected {source}, got {_module_file(canonical)}"
            )
        # Imports may have installed a name while resolving the canonical module.
        if sys.modules.get(name, _MISSING) is not previous or vars(parent).get(child, _MISSING) is not previous_attr:
            raise RuntimeError(f"Plugin import alias changed during installation: {name}")
        if previous is not canonical or previous_attr is not canonical:
            changes.append((name, canonical, parent, previous, previous_attr))
            sys.modules[name] = canonical
            setattr(parent, child, canonical)
        installed.append(name)
        visiting.remove(name)

    try:
        for name in sorted(targets):
            bind(name)
    except BaseException:
        installation.rollback()
        raise
    installation.names = tuple(installed)
    return installation


def retire_plugin_aliases(namespace: str, *, keep_root: Path | None = None) -> None:
    """Release this plugin's old aliases while preserving host modules and newer owners."""
    keep = Path(keep_root).resolve() if keep_root is not None else None
    candidates = list(sys.modules.items())
    for parent_name, parent in tuple(sys.modules.items()):
        if not issubclass(type(parent), ModuleType) or "__path__" not in vars(parent):
            continue
        for child, module in tuple(vars(parent).items()):
            alias = f"{parent_name}.{child}"
            if issubclass(type(module), ModuleType) and module.__name__ == f"{namespace}.{alias}":
                candidates.append((alias, module))
    for name, module in candidates:
        if "." not in name or getattr(module, "__name__", None) != f"{namespace}.{name}":
            continue
        source = _module_file(module)
        if (keep is not None and source is not None and source.is_relative_to(keep)
                and sys.modules.get(module.__name__) is module):
            continue
        if sys.modules.get(name) is module:
            del sys.modules[name]
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None and vars(parent).get(child, _MISSING) is module:
            delattr(parent, child)
