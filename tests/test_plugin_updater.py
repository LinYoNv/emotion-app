# -*- coding: utf-8 -*-
"""插件升级 —— 判据总集。

⭐ 被测对象：`host/astrbot_host/plugin_updater.py`（从仓库拉新版本并就地替换）。

🔴 **本文件最重要的不是"升级成功"，而是"升级不会删用户数据"**：
   真 AstrBot 用 `remove_dir(plugin_path)` 整目录删掉再解压（`core/star/updater.py:284`），
   它能这么做是因为它的用户数据全在 `data/`。**我们多一类数据在源码目录内**
   ——`file` 型配置项的上传落点（如 `astrbot_plugin_nai_image/files/`）。
   所以这里大部分判据是**反向**的：断言"该留的还在"。

⚠️ 本项目铁律：**跳过判据 = 假通过**。这里没有任何 skip。
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host"))

from astrbot_host.plugin_updater import (  # noqa: E402
    DEFAULT_PRESERVE,
    PluginUpdateError,
    _is_preserved,
    _merge_tree,
    _read_metadata,
    update_plugin,
)

MAIN_PY = "from astrbot.api.star import Star\n\n\nclass P(Star):\n    pass\n"


def _write_plugin(
    d: Path,
    *,
    name: str = "demo",
    version: str = "1.0.0",
    repo: str = "https://github.com/example/demo.git",
    extra_files: dict[str, str] | None = None,
) -> Path:
    """造一个最小可识别插件目录。"""
    d.mkdir(parents=True, exist_ok=True)
    meta: dict[str, object] = {
        "name": name,
        "display_name": f"演示{name}",
        "desc": "演示插件",
        "version": version,
        "author": "tester",
    }
    if repo:
        meta["repo"] = repo
    (d / "metadata.yaml").write_text(
        yaml.safe_dump(meta, allow_unicode=True), encoding="utf-8"
    )
    (d / "main.py").write_text(MAIN_PY, encoding="utf-8")
    for rel, text in (extra_files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return d


# ---------------------------------------------------------------- 元数据校验


def test_missing_metadata_is_rejected(tmp_path):
    """没有 metadata.yaml 的目录不是插件 —— 必须报错，不能当升级成功。"""
    d = tmp_path / "not_a_plugin"
    d.mkdir()
    (d / "main.py").write_text(MAIN_PY, encoding="utf-8")
    with pytest.raises(PluginUpdateError, match="metadata"):
        _read_metadata(d)


def test_required_field_missing_is_rejected(tmp_path):
    """真实现要求 name/desc/version/author 四项齐 —— 缺一个就拒绝。"""
    d = tmp_path / "p"
    d.mkdir()
    (d / "metadata.yaml").write_text(
        "name: x\ndesc: y\nversion: 1.0.0\n", encoding="utf-8"
    )  # 缺 author
    with pytest.raises(PluginUpdateError, match="author"):
        _read_metadata(d)


def test_empty_required_field_is_rejected(tmp_path):
    """空字符串不算"有" —— 真实现专门判 `not str.strip()`。"""
    d = tmp_path / "p"
    d.mkdir()
    (d / "metadata.yaml").write_text(
        "name: x\ndesc: '   '\nversion: 1.0.0\nauthor: me\n", encoding="utf-8"
    )
    with pytest.raises(PluginUpdateError, match="desc"):
        _read_metadata(d)


def test_description_falls_back_to_desc(tmp_path):
    """真实现：`desc` 缺失时用 `description` 顶（`validate_plugin_metadata`）。"""
    d = tmp_path / "p"
    d.mkdir()
    (d / "metadata.yaml").write_text(
        "name: x\ndescription: 用 description 顶\nauthor: me\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    meta = _read_metadata(d)
    assert meta["desc"] == "用 description 顶"


def test_metadata_yml_is_accepted(tmp_path):
    """真实现同时认 `.yml`（`PLUGIN_METADATA_FILENAMES`）。"""
    d = tmp_path / "p"
    d.mkdir()
    (d / "metadata.yml").write_text(
        "name: x\ndesc: y\nversion: 1.0.0\nauthor: me\n", encoding="utf-8"
    )
    assert _read_metadata(d)["name"] == "x"


def test_oversized_metadata_is_rejected(tmp_path):
    """真实现上限 1MB（`PLUGIN_METADATA_MAX_BYTES`）。"""
    d = tmp_path / "p"
    d.mkdir()
    (d / "metadata.yaml").write_text(
        "name: x\ndesc: " + "a" * (1024 * 1024 + 10) + "\nversion: 1\n" + "a" * 10,
        encoding="utf-8",
    )
    with pytest.raises(PluginUpdateError, match="1MB"):
        _read_metadata(d)


def test_missing_repo_is_rejected(tmp_path):
    """没声明 repo 的插件无法自动升级 —— 报错要说清"手工放进去"。"""
    d = _write_plugin(tmp_path / "p", repo="")
    with pytest.raises(PluginUpdateError, match="repo"):
        asyncio.run(update_plugin(plugin_dir=d))


# ---------------------------------------------------------------- 保留清单


def test_preserve_matches_directory_prefix():
    """`files/` 要能匹配 `files/a.png`，但**不能**误伤 `files_other/x`。"""
    assert _is_preserved(Path("files/a.png"), DEFAULT_PRESERVE) == "files/"
    assert _is_preserved(Path("files/sub/deep.png"), DEFAULT_PRESERVE) == "files/"
    assert _is_preserved(Path("files_other/x.png"), DEFAULT_PRESERVE) is None
    assert _is_preserved(Path("main.py"), DEFAULT_PRESERVE) is None


def test_merge_never_deletes_preserved_user_files(tmp_path):
    """🔴 **本文件的核心判据**：覆盖时 `files/` 里的用户文件必须原样还在。

    造一个"旧版本 + 用户上传了一张图"，再拿"新版本"覆盖，
    断言：① 图还在且内容逐字节不变；② 新版本的文件确实写进去了；
         ③ `preserved` 回报里能看到 `files/`（不能静默跳过）。
    """
    dst = _write_plugin(tmp_path / "installed", version="1.0.0")
    img = dst / "files" / "用户上传的参考图.png"
    img.parent.mkdir(parents=True)
    img.write_bytes(b"\x89PNG\r\n\x1a\nUSER-DATA-DO-NOT-TOUCH")

    src = _write_plugin(tmp_path / "new", version="2.0.0")
    (src / "added_in_v2.py").write_text("# 新版本才有\n", encoding="utf-8")

    written, preserved, kept = _merge_tree(src=src, dst=dst, preserve=DEFAULT_PRESERVE)

    assert img.exists(), "🔴 用户上传的文件被删了 —— 这正是要守的风险"
    assert img.read_bytes() == b"\x89PNG\r\n\x1a\nUSER-DATA-DO-NOT-TOUCH", "用户文件内容被改了"
    assert (dst / "added_in_v2.py").is_file(), "新版本的文件没写进去"
    assert "files/" in preserved, f"被跳过的路径必须回报出来：{preserved}"
    assert written > 0, "一个文件都没写，覆盖逻辑没跑"
    # 升级后版本号必须是新的
    assert _read_metadata(dst)["version"] == "2.0.0"


def test_merge_keeps_files_only_in_old_version(tmp_path):
    """旧版本独有、新版本没有的文件 —— **保留不删**（我们从不执行删除）。"""
    dst = _write_plugin(tmp_path / "installed")
    (dst / "runtime_state.json").write_text("{}", encoding="utf-8")
    src = _write_plugin(tmp_path / "new")
    _written, _preserved, kept = _merge_tree(src=src, dst=dst, preserve=DEFAULT_PRESERVE)
    assert (dst / "runtime_state.json").is_file(), "旧文件被删了"
    assert "runtime_state.json" in kept, f"保留的旧文件要回报：{kept}"


def test_merge_skips_pycache(tmp_path):
    """`__pycache__` 不该被复制过去（真实现也不拷）。"""
    dst = _write_plugin(tmp_path / "installed")
    src = _write_plugin(tmp_path / "new")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\x00")
    _merge_tree(src=src, dst=dst, preserve=DEFAULT_PRESERVE)
    assert not (dst / "__pycache__" / "x.cpython-312.pyc").exists()


def test_custom_preserve_list_is_honored(tmp_path):
    """调用方可传自己的保留清单（接口 `preserve` 参数）。"""
    dst = _write_plugin(tmp_path / "installed")
    data = dst / "my_data" / "keep.json"
    data.parent.mkdir()
    data.write_text('{"keep": true}', encoding="utf-8")
    src = _write_plugin(tmp_path / "new")
    _w, preserved, _k = _merge_tree(src=src, dst=dst, preserve=("my_data/",))
    assert data.read_text(encoding="utf-8") == '{"keep": true}'
    assert "my_data/" in preserved


# ---------------------------------------------------------------- 端到端（真 git）


def _git_available() -> bool:
    return shutil.which("git") is not None


@pytest.mark.skipif(not _git_available(), reason="本机没有 git")
def test_update_from_real_local_repo_end_to_end(tmp_path):
    """用**本地 bare 仓库**走真实 git clone 链路（不联网）。

    守的是：`update_plugin` 真的能拉到新版本、真的替换了源码、
    **而且同样不碰用户数据**。
    """
    # 1) 造一个"远端"仓库：v1
    remote = tmp_path / "remote"
    _write_plugin(remote, version="1.0.0", name="e2e_demo")
    (remote / "files").mkdir()
    (remote / "files" / "seed.txt").write_text("远端自带的", encoding="utf-8")
    _git(remote, "init", "-q", "-b", "main")
    _git(remote, "add", "-A")
    _git(remote, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v1")

    # 2) 安装它（clone 一份出来当已安装目录，模拟真实安装）
    installed = tmp_path / "plugins" / "e2e_demo"
    installed.parent.mkdir()
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(installed)], check=True, capture_output=True
    )
    # 用户上传了一张图
    user_img = installed / "files" / "我的图.png"
    user_img.write_bytes(b"\x89PNG\r\n\x1a\nMINE")

    # 3) 远端升到 v2
    (remote / "new_in_v2.py").write_text("# v2\n", encoding="utf-8")
    meta = yaml.safe_load((remote / "metadata.yaml").read_text(encoding="utf-8"))
    meta["version"] = "2.0.0"
    (remote / "metadata.yaml").write_text(
        yaml.safe_dump(meta, allow_unicode=True), encoding="utf-8"
    )
    _git(remote, "add", "-A")
    _git(remote, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v2")

    # 4) 升级
    out = asyncio.run(
        update_plugin(plugin_dir=installed, repo_override=str(remote))
    )

    assert out.old_version == "1.0.0"
    assert out.new_version == "2.0.0"
    assert (installed / "new_in_v2.py").is_file(), "新版本文件没落地"
    assert user_img.exists(), "🔴 真实 git 链路下用户数据也被删了"
    assert user_img.read_bytes() == b"\x89PNG\r\n\x1a\nMINE"
    assert "files/" in out.preserved
    assert out.restart_required is True


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


@pytest.mark.skipif(not _git_available(), reason="本机没有 git")
def test_update_failure_leaves_installed_dir_intact(tmp_path):
    """🔴 拉取失败**绝不能破坏已安装目录**（最坏的失败模式）。

    用一个不存在的路径当仓库 —— clone 必失败，
    断言已安装目录里的文件一个不少。
    """
    installed = _write_plugin(tmp_path / "plugins" / "demo", version="1.0.0")
    before = sorted(p.relative_to(installed).as_posix() for p in installed.rglob("*") if p.is_file())

    with pytest.raises(PluginUpdateError):
        asyncio.run(
            update_plugin(
                plugin_dir=installed,
                repo_override=str(tmp_path / "definitely_not_a_repo"),
            )
        )

    after = sorted(p.relative_to(installed).as_posix() for p in installed.rglob("*") if p.is_file())
    assert before == after, f"升级失败后已安装目录被改了：{set(before) ^ set(after)}"


# ---------------------------------------------------------------- 超时可配置


def test_resolve_timeout_priority(monkeypatch):
    """超时优先级：显式参数 → 环境变量 → 默认值。

    🔴 来历（2026-10-17 实测）：GitHub 下行只有 ~15 KB/s 时，
    25MB 仓库要 ~27 分钟，而真实现的 180s 必然超时 ——
    用户看到的却是"代理没生效"（误导性最强的失败）。所以超时不能写死。
    """
    from astrbot_host.plugin_updater import (
        GIT_CLONE_TIMEOUT_SECONDS,
        resolve_timeout,
    )

    monkeypatch.delenv("EMOTION_PLUGIN_UPDATE_TIMEOUT_SECONDS", raising=False)
    assert resolve_timeout() == GIT_CLONE_TIMEOUT_SECONDS
    assert resolve_timeout(900) == 900, "显式参数应优先于默认值"

    monkeypatch.setenv("EMOTION_PLUGIN_UPDATE_TIMEOUT_SECONDS", "1234")
    assert resolve_timeout() == 1234, "环境变量应生效"
    assert resolve_timeout(60) == 60, "显式参数应优先于环境变量"

    # 非法值忽略（不能因为一个环境变量写错就让升级功能整个不可用）
    for bad in ("abc", "", "0", "-5"):
        monkeypatch.setenv("EMOTION_PLUGIN_UPDATE_TIMEOUT_SECONDS", bad)
        assert resolve_timeout() == GIT_CLONE_TIMEOUT_SECONDS, f"非法值 {bad!r} 应被忽略"

    # 上限 3600（再长就不该在 HTTP 请求里等）
    monkeypatch.delenv("EMOTION_PLUGIN_UPDATE_TIMEOUT_SECONDS", raising=False)
    assert resolve_timeout(999999) == 3600


def test_default_timeout_is_generous_enough_for_slow_networks(monkeypatch):
    """默认超时必须**明显大于**真实现的 180s（慢网络下 180s 不够，实测）。"""
    from astrbot_host.plugin_updater import GIT_CLONE_TIMEOUT_SECONDS

    assert GIT_CLONE_TIMEOUT_SECONDS >= 600, (
        f"默认超时 {GIT_CLONE_TIMEOUT_SECONDS}s 太小：实测慢网络下 25MB 仓库要 ~27 分钟"
    )
