from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


PROJECT = Path(__file__).resolve().parents[2]
PLUGIN_NAME = "alias_fixture"


def _plugin(
    root: Path, *, extra: dict[str, str] | None = None, namespace_package: bool = False,
) -> Path:
    plugin = root / PLUGIN_NAME
    files = {
        "__init__.py": "",
        "main.py": "ENTRY_EXECUTED = True\nfrom .astrbot_host.alias_leaf import Marker\n",
        "astrbot_host/__init__.py": "",
        "astrbot_host/alias_leaf.py": (
            "import sys\n"
            "assert sys.modules['alias_fixture.main'].ENTRY_EXECUTED\n"
            "from enum import Enum\n"
            "class Marker(Enum):\n    READY = 'ready'\n"
        ),
        "astrbot_host/consumer.py": (
            "from astrbot_host.alias_leaf import Marker\n"
            "def get_marker():\n    return Marker.READY\n"
        ),
    }
    files.update(extra or {})
    if namespace_package:
        files.pop("__init__.py")
    for relative, content in files.items():
        target = plugin / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return plugin


def _probe(plugin: Path, code: str, *args: str) -> None:
    prelude = f"""
import sys, importlib
from pathlib import Path
sys.path.insert(0, {str(PROJECT / 'host')!r})
import astrbot_host
from astrbot_host.shadow import install_shadow_package
install_shadow_package()
from astrbot_host.loader.plugin_loader import PluginLoader
from astrbot_host.plugin_imports import install_plugin_aliases, retire_plugin_aliases
path = Path(sys.argv[1])
PluginLoader._ensure_on_sys_path(path.parent)
canonical_prefix = path.name + '.astrbot_host.'
"""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [sys.executable, "-B", "-c", textwrap.dedent(prelude) + textwrap.dedent(code), str(plugin), *args],
        cwd=plugin.parent,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_loader_installs_same_objects_after_entry_without_changing_host(tmp_path):
    plugin = _plugin(tmp_path)
    _probe(plugin, """
        from astrbot_host import registry, compat
        root_path = tuple(astrbot_host.__path__)
        root_file = astrbot_host.__file__
        PluginLoader._import_plugin(path)
        canonical = importlib.import_module(canonical_prefix + 'alias_leaf')
        spec, package = canonical.__spec__, canonical.__package__
        alias = importlib.import_module('astrbot_host.alias_leaf')
        consumer = importlib.import_module(canonical_prefix + 'consumer')
        assert alias is canonical is astrbot_host.alias_leaf
        assert consumer.Marker is canonical.Marker
        assert consumer.get_marker() is canonical.Marker.READY
        assert isinstance(consumer.get_marker(), canonical.Marker)
        assert canonical.__name__ == canonical_prefix + 'alias_leaf'
        assert canonical.__spec__ is spec and spec.name == canonical.__name__
        assert canonical.__package__ == package == canonical_prefix.rstrip('.')
        assert sys.modules['astrbot_host'] is astrbot_host
        assert tuple(astrbot_host.__path__) == root_path
        assert astrbot_host.__file__ == root_file
        assert astrbot_host.registry is registry and astrbot_host.compat is compat
    """)


def test_same_source_repeated_loading_preserves_module_and_enum_identity(tmp_path):
    plugin = _plugin(tmp_path)
    _probe(plugin, """
        PluginLoader._import_plugin(path)
        first = sys.modules['astrbot_host.alias_leaf']
        marker = first.Marker
        first_spec = first.__spec__
        PluginLoader._import_plugin(path)
        install_plugin_aliases(path, path.name)
        assert sys.modules['astrbot_host.alias_leaf'] is first
        assert sys.modules[canonical_prefix + 'alias_leaf'] is first
        assert astrbot_host.alias_leaf is first
        assert first.Marker is marker and first.__spec__ is first_spec
        assert [name for name, module in sys.modules.items()
                if getattr(module, '__file__', None) == first.__file__] == [
                    canonical_prefix + 'alias_leaf', 'astrbot_host.alias_leaf']
    """)


@pytest.mark.parametrize("location", ["module", "attribute"])
def test_conflicts_never_replace_host_modules_or_attributes(tmp_path, location):
    plugin = _plugin(tmp_path)
    _probe(plugin, """
        import types
        occupied = types.ModuleType('occupied')
        root_path = tuple(astrbot_host.__path__)
        location = sys.argv[2]
        if location == 'module':
            sys.modules['astrbot_host.alias_leaf'] = occupied
        else:
            astrbot_host.alias_leaf = occupied
        try:
            PluginLoader._import_plugin(path)
        except RuntimeError as error:
            assert 'conflicts with an existing' in str(error)
        else:
            raise AssertionError('existing host name was overwritten')
        if location == 'module':
            assert sys.modules['astrbot_host.alias_leaf'] is occupied
            assert 'alias_leaf' not in vars(astrbot_host)
        else:
            assert astrbot_host.alias_leaf is occupied
            assert 'astrbot_host.alias_leaf' not in sys.modules
        assert tuple(astrbot_host.__path__) == root_path
    """, location)


def test_failed_batch_rolls_back_new_aliases_and_preserves_previous_binding(tmp_path):
    plugin = _plugin(tmp_path, extra={
        "astrbot_host/b_added.py": "VALUE = 'new'\n",
        "astrbot_host/z_broken.py": "raise RuntimeError('canonical import exploded')\n",
        "astrbot_host/consumer.py": (
            "from astrbot_host.alias_leaf import Marker\n"
            "from astrbot_host.b_added import VALUE\n"
            "from astrbot_host.z_broken import VALUE as broken\n"
        ),
    })
    _probe(plugin, """
        pkg = importlib.import_module(path.name)
        main = importlib.import_module(path.name + '.main')
        previous = sys.modules[canonical_prefix + 'alias_leaf']
        sys.modules['astrbot_host.alias_leaf'] = previous
        astrbot_host.alias_leaf = previous
        try:
            install_plugin_aliases(path, path.name)
        except RuntimeError as error:
            assert str(error) == 'canonical import exploded'
        else:
            raise AssertionError('broken canonical module was accepted')
        assert sys.modules['astrbot_host.alias_leaf'] is previous
        assert astrbot_host.alias_leaf is previous
        assert 'astrbot_host.b_added' not in sys.modules
        assert 'b_added' not in vars(astrbot_host)
        assert 'astrbot_host.z_broken' not in sys.modules
        assert 'z_broken' not in vars(astrbot_host)
        assert sys.modules[canonical_prefix + 'b_added'].VALUE == 'new'
    """)


def test_source_switch_removes_old_alias_and_parent_attribute(tmp_path):
    first = _plugin(tmp_path / "first")
    second = _plugin(tmp_path / "second", extra={
        "astrbot_host/alias_leaf.py": "from enum import Enum\nclass Marker(Enum):\n    READY = 'second'\n",
    })
    _probe(first, """
        host = sys.modules['astrbot_host']
        registry = importlib.import_module('astrbot_host.registry')
        PluginLoader._import_plugin(path)
        old = astrbot_host.alias_leaf
        next_path = Path(sys.argv[2])
        PluginLoader._ensure_on_sys_path(next_path.parent)
        PluginLoader._import_plugin(next_path)
        current = sys.modules[canonical_prefix + 'alias_leaf']
        assert current is not old
        assert current.Marker is not old.Marker
        assert current.Marker.READY.value == 'second'
        assert Path(current.__file__).is_relative_to(next_path)
        assert sys.modules['astrbot_host.alias_leaf'] is current
        assert astrbot_host.alias_leaf is current
        assert sys.modules['astrbot_host'] is host
        assert sys.modules['astrbot_host.registry'] is registry
        assert not any(module is old for module in sys.modules.values())
    """, str(second))


def test_retirement_removes_only_owned_aliases(tmp_path):
    plugin = _plugin(tmp_path)
    _probe(plugin, """
        import types
        PluginLoader._import_plugin(path)
        canonical = astrbot_host.alias_leaf
        root_path = tuple(astrbot_host.__path__)
        newer = types.ModuleType('newer_owner')
        astrbot_host.alias_leaf = newer
        retire_plugin_aliases(path.name)
        assert 'astrbot_host.alias_leaf' not in sys.modules
        assert astrbot_host.alias_leaf is newer
        assert sys.modules[canonical_prefix + 'alias_leaf'] is canonical
        assert tuple(astrbot_host.__path__) == root_path
        assert sys.modules['astrbot_host'] is astrbot_host
        retire_plugin_aliases('unrelated_plugin')
        assert astrbot_host.alias_leaf is newer
    """)


def test_private_module_dependencies_are_aliased_before_import(tmp_path):
    plugin = _plugin(tmp_path, extra={
        "astrbot_host/a_dependent.py": "from astrbot_host.z_dependency import VALUE\n",
        "astrbot_host/z_dependency.py": "VALUE = object()\n",
        "astrbot_host/consumer.py": "from astrbot_host.a_dependent import VALUE\n",
    })
    _probe(plugin, """
        PluginLoader._import_plugin(path)
        dependent = importlib.import_module(canonical_prefix + 'a_dependent')
        dependency = importlib.import_module(canonical_prefix + 'z_dependency')
        assert dependent.VALUE is dependency.VALUE
        assert sys.modules['astrbot_host.a_dependent'] is dependent
        assert sys.modules['astrbot_host.z_dependency'] is dependency
    """)


def test_canonical_module_from_another_source_is_rejected(tmp_path):
    plugin = _plugin(tmp_path)
    _probe(plugin, """
        import types
        importlib.import_module(path.name + '.main')
        canonical_name = canonical_prefix + 'alias_leaf'
        other = types.ModuleType(canonical_name)
        other.__file__ = str(path.parent / 'unrelated.py')
        sys.modules[canonical_name] = other
        try:
            install_plugin_aliases(path, path.name)
        except RuntimeError as error:
            assert 'unexpected source' in str(error)
        else:
            raise AssertionError('canonical module from another source was accepted')
        assert 'astrbot_host.alias_leaf' not in sys.modules
        assert 'alias_leaf' not in vars(astrbot_host)
    """)


def test_unloaded_private_test_package_is_not_a_runtime_alias_source(tmp_path):
    plugin = _plugin(tmp_path, extra={
        "tests/__init__.py": "",
        "tests/helper.py": "raise AssertionError('test helper imported during production load')\n",
        "tests/test_example.py": "from tests.helper import VALUE\n",
    })
    _probe(plugin, """
        import types
        unrelated_tests = types.ModuleType('tests')
        unrelated_tests.__path__ = []
        sys.modules['tests'] = unrelated_tests
        PluginLoader._import_plugin(path)
        assert 'alias_fixture.tests' not in sys.modules
        assert 'tests.helper' not in sys.modules
        assert sys.modules['tests'] is unrelated_tests
        assert sys.modules['astrbot_host.alias_leaf'] is sys.modules[canonical_prefix + 'alias_leaf']
    """)


@pytest.mark.parametrize("has_class", [True, False])
def test_failed_plugin_load_rolls_back_aliases_after_successful_import(tmp_path, has_class):
    main = "ENTRY_EXECUTED = True\nfrom .astrbot_host.alias_leaf import Marker\n"
    if has_class:
        main += (
            "from astrbot.api.star import Star\n"
            "class Broken(Star):\n"
            "    def __init__(self, context, config):\n"
            "        from astrbot_host.alias_leaf import Marker as BoundMarker\n"
            "        assert BoundMarker is Marker\n"
            "        raise RuntimeError('constructor failed after aliases were installed')\n"
        )
    plugin = _plugin(tmp_path, extra={"main.py": main})
    _probe(plugin, """
        import asyncio
        loader = PluginLoader(plugin_dirs=[path.parent])
        record = asyncio.run(loader.load_one(path))
        assert not record.ok
        assert record.error
        if sys.argv[2] == 'True':
            assert 'constructor failed after aliases were installed' in record.error
        else:
            assert 'Star' in record.error
        assert 'astrbot_host.alias_leaf' not in sys.modules
        assert 'alias_leaf' not in vars(astrbot_host)
    """, str(has_class))


@pytest.mark.parametrize("keep_alias_entry", [True, False])
def test_same_source_orphaned_aliases_do_not_keep_a_second_class_graph(tmp_path, keep_alias_entry):
    plugin = _plugin(tmp_path)
    _probe(plugin, """
        PluginLoader._import_plugin(path)
        previous = astrbot_host.alias_leaf
        for name in tuple(sys.modules):
            if name == path.name or name.startswith(path.name + '.'):
                del sys.modules[name]
        if sys.argv[2] == 'False':
            del sys.modules['astrbot_host.alias_leaf']
        assert astrbot_host.alias_leaf is previous
        PluginLoader._import_plugin(path)
        canonical = sys.modules[canonical_prefix + 'alias_leaf']
        assert canonical is not previous
        assert canonical.Marker is not previous.Marker
        assert sys.modules['astrbot_host.alias_leaf'] is canonical
        assert astrbot_host.alias_leaf is canonical
        assert not any(module is previous for module in sys.modules.values())
    """, str(keep_alias_entry))



def test_retirement_does_not_resolve_unbound_proxy_package_attributes(tmp_path):
    plugin = _plugin(tmp_path)
    _probe(plugin, """
        from werkzeug.local import LocalProxy
        proxy = LocalProxy(lambda: (_ for _ in ()).throw(AssertionError('proxy was resolved')))
        astrbot_host.unbound_proxy = proxy
        PluginLoader._import_plugin(path)
        retire_plugin_aliases(path.name)
        assert vars(astrbot_host)['unbound_proxy'] is proxy
    """)


def test_bridge_discovers_package_and_module_names_without_a_fixed_list(tmp_path):
    plugin = _plugin(tmp_path, extra={
        "main.py": "from .shared_runtime.alpha import VALUE\n",
        "shared_runtime/__init__.py": "",
        "shared_runtime/alpha.py": "VALUE = object()\n",
        "shared_runtime/consumer.py": "from shared_runtime.alpha import VALUE\n",
    })
    _probe(plugin, """
        import types
        host = types.ModuleType('shared_runtime')
        host.__path__ = []
        sys.modules['shared_runtime'] = host
        PluginLoader._import_plugin(path)
        canonical = importlib.import_module(path.name + '.shared_runtime.alpha')
        consumer = importlib.import_module(path.name + '.shared_runtime.consumer')
        assert sys.modules['shared_runtime'] is host
        assert tuple(host.__path__) == ()
        assert host.alpha is canonical is sys.modules['shared_runtime.alpha']
        assert consumer.VALUE is canonical.VALUE
        assert canonical.__spec__.name == path.name + '.shared_runtime.alpha'
    """)



def test_same_source_reconciles_alias_after_canonical_only_cache_restore(tmp_path):
    plugin = _plugin(tmp_path)
    _probe(plugin, """
        PluginLoader._import_plugin(path)
        previous = astrbot_host.alias_leaf
        canonical_name = canonical_prefix + 'alias_leaf'
        del sys.modules[canonical_name]
        restored = importlib.import_module(canonical_name)
        assert restored is not previous
        assert sys.modules['astrbot_host.alias_leaf'] is previous
        assert PluginLoader._same_source(path.name, path)
        PluginLoader._import_plugin(path)
        assert sys.modules['astrbot_host.alias_leaf'] is restored
        assert astrbot_host.alias_leaf is restored
        assert sys.modules[path.name + '.main'].Marker is restored.Marker
    """)


def test_namespace_path_expansion_does_not_misclassify_old_cached_modules(tmp_path):
    first = _plugin(tmp_path / "first", namespace_package=True)
    second = _plugin(tmp_path / "second", namespace_package=True, extra={
        "astrbot_host/alias_leaf.py": "from enum import Enum\nclass Marker(Enum):\n    READY = 'second'\n",
    })
    _probe(first, """
        PluginLoader._import_plugin(path)
        old = astrbot_host.alias_leaf
        next_path = Path(sys.argv[2])
        PluginLoader._ensure_on_sys_path(next_path.parent)
        assert str(next_path) in tuple(sys.modules[path.name].__path__)
        assert not PluginLoader._same_source(path.name, next_path)
        PluginLoader._import_plugin(next_path)
        current = sys.modules[canonical_prefix + 'alias_leaf']
        assert current is not old and current.Marker.READY.value == 'second'
        assert Path(current.__file__).is_relative_to(next_path)
        assert sys.modules['astrbot_host.alias_leaf'] is current
        assert astrbot_host.alias_leaf is current
    """, str(second))


def test_shared_package_owner_discovery_order_is_stable(tmp_path):
    from astrbot_host.loader.plugin_loader import PluginLoader

    first, second = tmp_path / "first", tmp_path / "second"

    def add(root, name, shared=()):
        plugin = root / name
        plugin.mkdir(parents=True)
        (plugin / "main.py").write_text("raise AssertionError('discover executed plugin')\n")
        for package in shared:
            (plugin / package).mkdir()
        return plugin

    core = add(second, "a_core", ("companion_v2",))
    plain_z = add(second, "z_plain")
    host = add(first, "b_host", ("astrbot_host",))
    plain_a = add(first, "a_plain")
    both = add(first, "c_both", ("companion_v2", "astrbot_host"))
    named = add(first, "astrbot_host_named")
    (named / "companion_v2").write_text("a file is not a shared package directory")
    add(first, "_hidden", ("companion_v2",))
    expected = [plain_a, named, plain_z, host, both, core]
    loader = PluginLoader(plugin_dirs=[second, first])
    assert loader.discover() == expected
    assert loader.discover() == expected


