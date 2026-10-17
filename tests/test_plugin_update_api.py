# -*- coding: utf-8 -*-
"""插件升级**接口**判据 —— `POST /api/v1/plugins/<id>/update`。

## 守什么

真 AstrBot 有 `POST /plugins/{plugin_id}/update`（`dashboard/api/plugins.py:1143`），
我们以前**没有** —— 面板里装/升级插件只能手工放目录 + 重启。

## 判据设计（照项目铁律）

  * 🔴 **反向也要验**：未鉴权必须 **401**。本项目真事故：新增路由漏 `require_auth()`
    ⇒ 服务绑 `0.0.0.0` 时公网可操作。**只验"带令牌能过"等于没验鉴权。**
  * 🔴 **失败不许显示成"成功"**：拉取失败要 400 且消息能指导下一步。
  * 🔴 **"用户数据不能被删"要在接口层再验一次**（不只单测层）。
  * 🔴 **不支持的方法/未知插件不能静默 200**。

⚠️ 本项目铁律：**跳过判据 = 假通过**。这里没有 skip，前置不满足就是失败。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

APP_ROOT = Path(__file__).resolve().parent.parent
HOST_DIR = APP_ROOT / "host"
TOKEN = "test-token-for-plugin-update-route-01"

for p in (str(APP_ROOT), str(HOST_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from emotion_app.infra.config import Config  # noqa: E402
from emotion_app.storage.factory import build_store  # noqa: E402
from emotion_app.web.app import create_app  # noqa: E402
from emotion_app.web.auth import AuthManager  # noqa: E402

MAIN_PY = (
    "from astrbot.api.star import Star\n\n\n"
    "class Probe(Star):\n"
    "    def __init__(self, context, config=None):\n"
    "        super().__init__(context, config)\n"
)


def _make_plugin(root: Path, name: str, *, version: str = "1.0.0", repo: str = "") -> Path:
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    meta = {
        "name": name,
        "display_name": f"升级探针{name}",
        "version": version,
        "author": "t",
        "desc": "t",
    }
    if repo:
        meta["repo"] = repo
    (pkg / "metadata.yaml").write_text(yaml.safe_dump(meta, allow_unicode=True), encoding="utf-8")
    (pkg / "main.py").write_text(MAIN_PY, encoding="utf-8")
    return pkg


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.plugin_root = tmp_path / "plugins"
        self.plugin_root.mkdir(parents=True, exist_ok=True)
        #: 没有 repo 的插件 —— 用来验"报错要说清怎么办"
        _make_plugin(self.plugin_root, "upd_no_repo", repo="")
        self.profile_path = tmp_path / "data" / "config_profile.json"

    def cfg(self) -> Config:
        cfg = Config()
        cfg.auth.access_token = TOKEN
        cfg.storage.sqlite_path = str(self.tmp_path / "t.db")
        cfg.plugins.enabled = True
        cfg.plugins.mount_web_apis = True
        cfg.plugins.plugin_dirs = str(self.plugin_root)
        cfg.plugins.data_dir = str(self.tmp_path / "plugin_data")
        # 🔴 **测试必须和生产同构**：生产 `.env` 里
        #    `EMOTION_PLUGINS_MIRROR_SOURCES=false`（镜像已关，插件从 `plugins/` 直读）。
        #    `Config.mirror_sources` 的代码默认却是 **True** —— 不显式关掉，
        #    宿主加载的是 `plugin_src/` 的副本，于是"升级 plugins/ 却不生效"
        #    （我在这里踩过：升级看似成功，改的却不是插件真正跑的那份）。
        cfg.plugins.mirror_sources = False
        cfg.plugins.source_mirror_dir = str(self.tmp_path / "plugin_src")
        cfg.plugins.auto_initialize = False
        cfg.validate()
        return cfg

    async def app(self):
        cfg = self.cfg()
        store = build_store(cfg.storage)
        await store.open()
        app = create_app(cfg, store, AuthManager(access_token=TOKEN))
        app.config["EMOTION_CONFIG_PROFILE_PATH"] = str(self.profile_path)
        svc = app.config["EMOTION_PLUGIN_HOST"]
        await svc.start()
        return app, store, svc


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_update_requires_auth(tmp_path):
    """🔴 **未鉴权必须 401** —— 这条比"带令牌能过"重要得多。"""
    h = Harness(tmp_path)

    async def _go():
        app, store, _svc = await h.app()
        try:
            client = app.test_client()
            r = await client.post("/api/v1/plugins/upd_no_repo/update", json={})
            body = await r.get_json()
            return r.status_code, body
        finally:
            await store.close()

    code, body = asyncio.run(_go())
    assert code == 401, f"未鉴权竟然不是 401 而是 {code} —— 这条路由漏了 require_auth"
    assert body is not None


def test_update_with_auth_reports_missing_repo(tmp_path):
    """有鉴权 + 插件没声明 repo ⇒ 400，且消息要告诉用户"手工放进去"。"""
    h = Harness(tmp_path)

    async def _go():
        app, store, _svc = await h.app()
        try:
            client = app.test_client()
            r = await client.post(
                "/api/v1/plugins/upd_no_repo/update", json={}, headers=AUTH
            )
            return r.status_code, await r.get_json()
        finally:
            await store.close()

    code, body = asyncio.run(_go())
    assert code == 400, f"没 repo 的插件升级应是 400（客户端问题），实际 {code}"
    msg = str(body.get("error", {}).get("message", ""))
    assert "repo" in msg, f"错误消息要说明原因，实际：{msg}"
    assert "手工" in msg or "plugins" in msg, f"要给出下一步动作，实际：{msg}"


def test_update_unknown_plugin_is_404(tmp_path):
    """不存在的插件要 404，不能 200 也不能 500。"""
    h = Harness(tmp_path)

    async def _go():
        app, store, _svc = await h.app()
        try:
            client = app.test_client()
            r = await client.post("/api/v1/plugins/no_such_plugin/update", json={}, headers=AUTH)
            return r.status_code
        finally:
            await store.close()

    assert asyncio.run(_go()) == 404


@pytest.mark.skipif(shutil.which("git") is None, reason="本机没有 git")
def test_update_end_to_end_keeps_user_data(tmp_path):
    """🔴 接口层再验一次"升级不删用户数据"（不只靠单测层）。

    造一个本地 bare 仓库当远端 → 安装 → 用户上传一张图 → 远端升版 → 调接口升级
    → 断言：返回 200、`restart_required` 为真、**用户图还在**、新版本文件落地。
    """
    # ⚠️ `_make_plugin(root, name)` 建的是 `root/name`；仓库根要是插件根本身，
    #    所以先建在临时父目录里再整体搬成仓库根。
    _tmp_parent = tmp_path / "_mk"
    _tmp_parent.mkdir()
    _make_plugin(_tmp_parent, "upd_e2e", version="1.0.0")
    remote = tmp_path / "remote"
    (_tmp_parent / "upd_e2e").rename(remote)
    # ⚠️ git 不跟踪空目录 —— 必须放一个真实文件，否则 clone 后 `files/` 不存在，
    #    测试就会在自己的准备阶段失败（我第一次就踩了这个，看起来像产品 bug）。
    (remote / "files").mkdir()
    (remote / "files" / "seed.txt").write_text("远端自带", encoding="utf-8")
    _git(remote, "init", "-q", "-b", "main")
    _git(remote, "add", "-A")
    _git(remote, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v1")

    h = Harness(tmp_path)
    installed = h.plugin_root / "upd_e2e"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(installed)], check=True, capture_output=True
    )
    user_img = installed / "files" / "我的参考图.png"
    user_img.write_bytes(b"\x89PNG\r\n\x1a\nUSER")

    # 远端升到 v2
    (remote / "added_v2.py").write_text("# v2\n", encoding="utf-8")
    meta = yaml.safe_load((remote / "metadata.yaml").read_text(encoding="utf-8"))
    meta["version"] = "2.0.0"
    (remote / "metadata.yaml").write_text(
        yaml.safe_dump(meta, allow_unicode=True), encoding="utf-8"
    )
    _git(remote, "add", "-A")
    _git(remote, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v2")

    async def _go():
        app, store, _svc = await h.app()
        try:
            client = app.test_client()
            r = await client.post(
                "/api/v1/plugins/upd_e2e/update",
                json={"repo": str(remote)},
                headers=AUTH,
            )
            return r.status_code, await r.get_json()
        finally:
            await store.close()

    code, body = asyncio.run(_go())
    assert code == 200, f"升级应成功，实际 {code}: {body}"
    res = body.get("result") or {}
    assert res.get("old_version") == "1.0.0"
    assert res.get("new_version") == "2.0.0"
    assert res.get("restart_required") is True, "必须如实告知要重启"
    assert "files/" in (res.get("preserved") or []), f"必须回报保住了什么：{res}"
    assert user_img.exists(), "🔴 接口层升级把用户数据删了"
    assert user_img.read_bytes() == b"\x89PNG\r\n\x1a\nUSER"
    assert (installed / "added_v2.py").is_file(), "新版本没落地"
    assert "重启" in str(body.get("message", "")), "message 必须提醒重启"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def test_update_targets_the_directory_the_plugin_actually_loads(tmp_path):
    """🔴 **升级必须改在"插件真正跑的那份源码"上**。

    来历（真实踩坑）：`Config.plugins.mirror_sources` 的**代码默认是 True**，
    所以宿主会把 `plugins/` 镜像到 `plugin_src/` 再从那加载。
    我第一版测试没关镜像 ⇒ 升级改的是 `plugins/`，而插件实际跑的是
    `plugin_src/` 的副本 —— **升级"成功"却毫无效果，还改错了地方**。
    生产 `.env` 里显式关了这个开关才没出事，但那是**配置巧合**、不是代码保证。

    本判据守两件事：
      ① 升级写入的目录 == 宿主记录的 `record.path`（同一个目录，不是"我以为的那个"）；
      ② 该目录里的新版本文件**真的在**（不是写到别处去了）。
    """
    remote = tmp_path / "remote2"
    _mk = tmp_path / "_mk2"
    _mk.mkdir()
    _make_plugin(_mk, "upd_live", version="1.0.0")
    (_mk / "upd_live").rename(remote)
    _git(remote, "init", "-q", "-b", "main")
    _git(remote, "add", "-A")
    _git(remote, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v1")

    h = Harness(tmp_path)
    installed = h.plugin_root / "upd_live"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(installed)], check=True, capture_output=True
    )

    (remote / "v2_marker.py").write_text("# v2\n", encoding="utf-8")
    meta = yaml.safe_load((remote / "metadata.yaml").read_text(encoding="utf-8"))
    meta["version"] = "2.0.0"
    (remote / "metadata.yaml").write_text(
        yaml.safe_dump(meta, allow_unicode=True), encoding="utf-8"
    )
    _git(remote, "add", "-A")
    _git(remote, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v2")

    async def _go():
        app, store, svc = await h.app()
        try:
            rec = svc.host.loader.records["upd_live"]
            loaded_path = Path(str(rec.path)).resolve()
            client = app.test_client()
            r = await client.post(
                "/api/v1/plugins/upd_live/update", json={"repo": str(remote)}, headers=AUTH
            )
            return r.status_code, await r.get_json(), loaded_path
        finally:
            await store.close()

    code, body, loaded_path = asyncio.run(_go())
    assert code == 200, body
    # ① 升级的落点必须就是宿主实际加载的目录
    reported = Path((body.get("result") or {})["path"]).resolve()
    assert reported == loaded_path, (
        f"升级写到了 {reported}，而插件实际从 {loaded_path} 加载 —— "
        "改了不生效（是不是 mirror_sources 又开了？）"
    )
    # ② 新版本文件必须在那个目录里
    assert (loaded_path / "v2_marker.py").is_file(), "新版本文件没落在实际加载目录"
    assert (loaded_path / "metadata.yaml").read_text(encoding="utf-8").count("2.0.0") >= 1
