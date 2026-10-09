# -*- coding: utf-8 -*-
"""插件配置**读/写**的判据（情绪 2026-10-09：「有些插件没有内置的独立 webui
就可以打开插件的配置」）。

## 契约来源（照真实现，不是猜的）
真 AstrBot 4.27.5：
  * 读：`GET /api/v1/plugins/config?plugin_id=` —— `dashboard/api/plugins.py:720-732`
  * 写：`PUT /api/v1/plugins/config`，body `{plugin_id, config}` —— `:735-750`
  * 形状由 `ConfigDisplayService.get_plugin_config()` 产出 —— `services/config_service.py:844-863`
    即 `metadata = {插件名: {"description", "type": "object", "items": <_conf_schema.json>}}`

## 🔴 这些判据守的是什么
配置页最容易出的三类问题**全是静默的**：
  ① **schema 没接出来** → 前端画不出表单，用户看到"这个插件没有配置项"
     （而真相是宿主没把 `rec.config` 留住）
  ② **保存只写盘、不生效** → 用户看到"保存成功"，插件行为没变
  ③ **写错文件** → 写进 `cmd_config.json`（**全局配置**）或写进别人的 AstrBot 生产目录

所以下面的判据一律要求**真的落盘、真的能读回来**，且**绝不碰** `/opt/astrbot`。
"""
from __future__ import annotations

import json
import sys
import io
from pathlib import Path

# ⚠️ 用 `quart.datastructures` 那份（项目既有约定，见 `test_persona_ref_http.py`）——
#    不是 werkzeug 那份：Quart 的测试客户端认的是它自己的 FileStorage。
from quart.datastructures import FileStorage

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
HOST_DIR = APP_ROOT / "host"
PLUGIN_ROOT = Path("/opt/astrbot/data/plugins")
TOKEN = "test-token-plugin-config-0001"

for p in (str(APP_ROOT), str(HOST_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from emotion_app.infra.config import Config  # noqa: E402
from emotion_app.storage.factory import build_store  # noqa: E402
from emotion_app.web.app import create_app  # noqa: E402
from emotion_app.web.auth import AuthManager  # noqa: E402

#: 一个有 `_conf_schema.json`、且依赖简单的插件（**不用陪伴插件**：
#: 它 1000+ 配置项，加载慢且与别的判据抢资源；这里要的是"能加载 + 有 schema"）
PLUGIN_ID = "astrbot_plugin_splitter"


def _cfg(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.auth.access_token = TOKEN
    cfg.storage.sqlite_path = str(tmp_path / "t.db")
    cfg.plugins.enabled = True
    cfg.plugins.mount_web_apis = True
    cfg.plugins.plugin_dirs = str(PLUGIN_ROOT)
    # 🔴 数据目录必须是**我们自己的临时目录** ——
    #    绝不能指向 /opt/astrbot/data（那是别人 AstrBot 的生产数据）
    cfg.plugins.data_dir = str(tmp_path / "plugin_data")
    cfg.plugins.source_mirror_dir = str(tmp_path / "plugin_src")
    cfg.plugins.auto_initialize = False
    cfg.validate()
    return cfg


async def _app(tmp_path: Path, *, start: bool = True):
    cfg = _cfg(tmp_path)
    store = build_store(cfg.storage)
    await store.open()
    app = create_app(cfg, store, AuthManager(access_token=TOKEN))
    app.config["EMOTION_CONFIG_PROFILE_PATH"] = str(tmp_path / "config_profile.json")
    svc = app.config["EMOTION_PLUGIN_HOST"]
    if start:
        await svc.start()
    client = app.test_client()
    await client.post("/api/v1/auth/login", json={"token": TOKEN})
    return app, store, svc, client


@pytest.fixture
async def make(tmp_path):
    opened: list = []

    async def _make(*, start: bool = True, login: bool = True):
        cfg = _cfg(tmp_path)
        store = build_store(cfg.storage)
        await store.open()
        opened.append(store)
        app = create_app(cfg, store, AuthManager(access_token=TOKEN))
        app.config["EMOTION_CONFIG_PROFILE_PATH"] = str(tmp_path / "config_profile.json")
        svc = app.config["EMOTION_PLUGIN_HOST"]
        if start:
            await svc.start()
        c = app.test_client()
        c.app = app            # type: ignore[attr-defined]
        c.svc = svc            # type: ignore[attr-defined]
        if login:
            await c.post("/api/v1/auth/login", json={"token": TOKEN})
        return c

    try:
        yield _make
    finally:
        for s in opened:
            try:
                await s.close()
            except Exception:  # noqa: BLE001
                pass


# ===========================================================================
# ① 读：schema 与当前值都要出来
# ===========================================================================


async def test_config_reads_schema_and_values(make):
    """🔴 核心：`metadata.items` 必须**就是插件的 `_conf_schema.json`**。

    这条守的是"宿主把配置对象留住了没有"。`PluginRecord.config` 没加之前，
    这个接口**根本无从实现**（只能重新读盘，那会与插件手里那份漂移）。
    """
    c = await make()
    r = await c.get(f"/api/v1/plugins/{PLUGIN_ID}/config")
    assert r.status_code == 200, await r.get_data(as_text=True)
    d = await r.get_json()

    assert d["plugin_name"] == PLUGIN_ID
    assert isinstance(d["metadata"], dict), f"metadata 是 {type(d['metadata']).__name__}，应当是 dict"
    md = d["metadata"][PLUGIN_ID]
    assert md["type"] == "object"
    assert md["description"] == f"{PLUGIN_ID} 配置"
    items = md["items"]
    assert isinstance(items, dict) and items, "schema items 是空的"

    # 🔴 **与磁盘上的 `_conf_schema.json` 逐字比**（两边同源 ⇒ 判据不漂）
    #    ⚠️ 读的是**镜像**里那份（加载器镜像到 data/plugin_src 后从镜像加载），
    #       所以这里也从镜像读，而不是从 /opt/astrbot 读。
    # ⚠️ 路径从**注入的 Config 对象**取（不是 app.config —— 那里没有这两个键。
    #    我第一版写了 `app.config["EMOTION_PLUGINS_SOURCE_MIRROR_DIR"]` 直接 KeyError）
    cfg_obj = c.app.config["EMOTION_CONFIG"]
    mirror = cfg_obj.plugins.source_mirror_dir
    schema_file = Path(mirror) / PLUGIN_ID / "_conf_schema.json"
    assert schema_file.is_file(), f"镜像里没有 schema：{schema_file}"
    on_disk = json.loads(schema_file.read_text(encoding="utf-8-sig"))
    assert items == on_disk, "接口给的 schema 与磁盘上的 _conf_schema.json 不一致"

    # 当前值：至少要是一个 dict（真实值来自 `<data>/config/<id>_config.json`）
    assert isinstance(d["config"], dict)


async def test_config_requires_auth(make):
    """🔴 未鉴权必须 401（本项目真事故：漏了鉴权 → 未登录返 200，而服务绑 0.0.0.0）。

    ⚠️ 这条配置读接口会吐出插件的**完整配置**（可能含 cookie/api_key），
       比日志更不能漏鉴权。
    """
    c = await make(login=False)
    r = await c.get(f"/api/v1/plugins/{PLUGIN_ID}/config")
    assert r.status_code == 401, f"未鉴权竟然返回 {r.status_code}"


async def test_config_unknown_plugin_is_404_with_available(make):
    """未知插件 → 404，**且带上可用 id 清单**（只说"没有"会让用户不知道自己拼错哪个字）。"""
    c = await make()
    r = await c.get("/api/v1/plugins/definitely_not_a_plugin/config")
    assert r.status_code == 404
    d = await r.get_json()
    assert "available" in d["error"], d
    assert PLUGIN_ID in d["error"]["available"], "可用清单里应当有真实插件"


async def test_config_host_down_is_503_not_empty(make):
    """🔴 宿主没起来 → **503，不是空配置**。

    差别很大：空配置会让用户以为"这个插件没有配置项"，
    而真相是"服务还没就绪"。两者的下一步动作完全不同。
    """
    c = await make(start=False)
    r = await c.get(f"/api/v1/plugins/{PLUGIN_ID}/config")
    assert r.status_code == 503, f"实得 {r.status_code}"
    d = await r.get_json()
    assert d["error"]["type"] == "unavailable"


# ===========================================================================
# ② 写：真的落盘、真的能读回来
# ===========================================================================


async def test_config_save_persists_to_plugin_config_file(make):
    """🔴 保存必须**落到该插件自己的配置文件**里，并能读回来。

    守的是"写对文件"：
      * 写进 `cmd_config.json`（**全局配置**）→ 插件的 `get_config()` 读的却是自己那份，
        等于**改了没用**；
      * 写进 `/opt/astrbot/**` → 污染别人的生产目录（本项目的红线）。
    """
    c = await make()
    # 🔴 改一个**真实存在**的键。
    #    ⚠️ 不能假设"一定有布尔键" —— 我第一版就是 `isinstance(v, bool)` 筛，
    #       而 `astrbot_plugin_splitter` 顶层只有 string + object（**没有 bool**），
    #       判据直接以"这个插件没有布尔配置项"失败。那是**我的假设错了**，不是产品问题。
    #    改成从磁盘 schema 里找一个**有 `options` 的 string 键**，并在它的合法值里换 ——
    #    这样既真实、又不会写出非法值（把"校验"这件事排除在判据之外）。
    cfg_obj = c.app.config["EMOTION_CONFIG"]
    schema_file = Path(cfg_obj.plugins.source_mirror_dir) / PLUGIN_ID / "_conf_schema.json"
    schema = json.loads(schema_file.read_text(encoding="utf-8-sig"))
    cands = [
        (k, v["options"]) for k, v in schema.items()
        if isinstance(v, dict) and v.get("type") == "string" and isinstance(v.get("options"), list) and len(v["options"]) >= 2
    ]
    assert cands, "这个插件没有【带 options 的 string 键】，换一个插件或换判据"
    key, options = cands[0]

    r0 = await c.get(f"/api/v1/plugins/{PLUGIN_ID}/config")
    d0 = await r0.get_json()
    cur = d0["config"].get(key)
    want = next(o for o in options if o != cur)

    r = await c.put(f"/api/v1/plugins/{PLUGIN_ID}/config", json={"config": {key: want}})
    assert r.status_code == 200, await r.get_data(as_text=True)
    body = await r.get_json()
    assert body["ok"] is True
    # ⚠️ 文案必须**如实**说需要重启（我们没实现热重载）—— 不许照抄真实现的
    #    "正在热重载插件"（那是假信息）
    assert "重启" in body["message"], f"文案没说清生效时机：{body['message']}"

    # 读回来（走**接口**）
    r2 = await c.get(f"/api/v1/plugins/{PLUGIN_ID}/config")
    d2 = await r2.get_json()
    assert d2["config"][key] == want, "改了但读回来还是旧值"

    # 🔴 再**直接看盘上的文件** —— 只走接口可能被内存缓存骗过去
    cfg_obj = c.app.config["EMOTION_CONFIG"]
    data_dir = cfg_obj.plugins.data_dir
    cfg_file = Path(data_dir) / "config" / f"{PLUGIN_ID}_config.json"
    assert cfg_file.is_file(), f"配置文件没落盘：{cfg_file}"
    on_disk = json.loads(cfg_file.read_text(encoding="utf-8-sig"))
    assert on_disk[key] == want, f"盘上还是旧值：{on_disk.get(key)!r}"

    # 🔴 红线：绝不能写到别人的 AstrBot 生产目录
    assert "/opt/astrbot" not in str(cfg_file.resolve()), "写进了生产目录！"


async def test_config_save_merges_not_replaces(make):
    """🔴 保存是**浅合并**，不是整体替换。

    ⚠️ 这条守的是一个**极易踩的静默 bug**：如果实现成"先清空再写"，
       那么前端只提交一个键时，其余几百个键会**全部丢失** ——
       而接口返回 200、用户看到"保存成功"。
       真实现是 `self.update(replace_config)`（`astrbot_config.py:225-235`）。
    """
    c = await make()
    cfg_obj = c.app.config["EMOTION_CONFIG"]
    schema_file = Path(cfg_obj.plugins.source_mirror_dir) / PLUGIN_ID / "_conf_schema.json"
    schema = json.loads(schema_file.read_text(encoding="utf-8-sig"))
    cands = [
        (k, v["options"]) for k, v in schema.items()
        if isinstance(v, dict) and v.get("type") == "string" and isinstance(v.get("options"), list) and len(v["options"]) >= 2
    ]
    assert cands, "这个插件没有【带 options 的 string 键】"
    key, options = cands[0]

    r0 = await c.get(f"/api/v1/plugins/{PLUGIN_ID}/config")
    d0 = await r0.get_json()
    before = d0["config"]
    want = next(o for o in options if o != before.get(key))

    await c.put(f"/api/v1/plugins/{PLUGIN_ID}/config", json={"config": {key: want}})

    r2 = await c.get(f"/api/v1/plugins/{PLUGIN_ID}/config")
    after = (await r2.get_json())["config"]
    # 其余键必须**一个不少**（用集合比较，别用长度 —— 长度相等 ≠ 组成相同）
    lost = set(before) - set(after)
    assert not lost, f"保存后丢了这些键（说明是整体替换而不是合并）：{sorted(lost)[:10]}"


async def test_config_save_requires_auth(make):
    """未鉴权不能写（写接口能改插件行为，比读更不能漏）。"""
    c = await make(login=False)
    r = await c.put(f"/api/v1/plugins/{PLUGIN_ID}/config", json={"config": {"x": 1}})
    assert r.status_code == 401, f"未鉴权竟然返回 {r.status_code}"


async def test_config_save_bad_body_is_400(make):
    """形状不对 → 400，且**说清要什么形状**（不是一句"参数错误"）。"""
    c = await make()
    r = await c.put(f"/api/v1/plugins/{PLUGIN_ID}/config", json={"nope": 1})
    assert r.status_code == 400
    d = await r.get_json()
    assert "config" in d["error"]["message"], d["error"]["message"]


# ===========================================================================
# ③ 侧边栏插件分组要的两个字段（`display_name` / `pages`）
#     情绪 2026-10-09：「要按我们平台实际有的插件添加，就算以后我自己添加了
#     插件也能自己显示」⇒ 数据必须来自接口，不能写死。
# ===========================================================================


async def test_plugins_view_exposes_display_name_and_pages(make):
    """🔴 `GET /api/v1/plugins` 必须给出 `display_name` 与 `pages`。

    守的是**侧边栏插件分组能不能动态生成**：
      * 没有 `display_name` → 侧边栏只能显示目录名（`meme_manager`），
        而真 AstrBot 显示「表情包管理器」；
      * 没有 `pages` → 前端得另开一条请求、或**写死插件名**
        （情绪明确禁止写死："以后我自己添加了插件也能自己显示"）。
    """
    c = await make()
    r = await c.get("/api/v1/plugins")
    assert r.status_code == 200, await r.get_data(as_text=True)
    d = await r.get_json()
    ps = d["plugins"]
    assert ps, "插件清单是空的"

    # 每个插件都要有这两个键（值可以为空 —— 不是每个插件都声明）
    for p in ps:
        assert "display_name" in p, f"{p['id']} 缺 display_name"
        assert "pages" in p, f"{p['id']} 缺 pages"
        assert isinstance(p["pages"], list), f"{p['id']} 的 pages 不是 list"

    # 🔴 至少有一个插件**真的有** display_name（否则等于没读）
    with_dn = [p for p in ps if (p.get("display_name") or "").strip()]
    assert with_dn, "没有任何插件读到 display_name —— 说明根本没读 metadata 的那个字段"

    # 🔴 至少有一个插件**真的有** pages（否则侧边栏分组是空的）
    with_pages = [p for p in ps if p["pages"]]
    assert with_pages, "没有任何插件有拓展页（本机实测有 6 个，全空说明 pages 没接出来）"
    for p in with_pages:
        for pg in p["pages"]:
            assert pg.get("name"), f"{p['id']} 的页面项缺 name：{pg}"


async def test_display_name_is_not_the_directory_name(make):
    """🔴 `display_name` 必须是 `metadata.yaml` 里的那个，**不是目录名**。

    ⚠️ 这条守的是一次真实差点漏掉的事：`host_app.py` 原来把
    `rec.name`（= 目录名）赋给 `display_name` ⇒ 字段**存在但值没意义**。
    只断言"字段存在"抓不住它（值照样有），必须**断言两者不同**。

    ⚠️ 用集合比较而不是写死某个插件名（情绪要求不能写死）。
    """
    c = await make()
    r = await c.get("/api/v1/plugins")
    ps = (await r.get_json())["plugins"]
    pairs = [
        (p["id"], p["display_name"]) for p in ps
        if (p.get("display_name") or "").strip()
    ]
    same = [pid for pid, dn in pairs if dn == pid]
    assert not same, (
        f"这些插件的 display_name 与目录名相同 ⇒ 多半是拿目录名冒充的：{same}"
    )


# ===========================================================================
# ④ 插件「配置文件」管理（2026-10-09 情绪「3 也可以做」）
#     照真实现 `ConfigFileService`：`type: "file"` 的配置项，
#     文件落在**插件目录**的 `files/<键路径>/` 下。
# ===========================================================================


def test_config_key_folder_matches_real_implementation():
    """`_config_key_to_folder` 与真实现同语义（点分 → 目录、白名单清洗）。

    ⚠️ 这条守的是**路径安全**：`../` 必须被洗掉，否则就是把用户的
       上传写到插件目录外面去。
    """
    from astrbot_host.host_app import PluginHost

    assert PluginHost._config_key_to_folder("openai_director_fallback_images") == (
        "openai_director_fallback_images"
    )
    assert PluginHost._config_key_to_folder("a.b.c") == "a/b/c"
    assert PluginHost._config_key_to_folder("") == "_"
    # 🔴 穿越字符必须被洗掉
    for evil in ("../evil", "..", "a/../../b", "a\\..\\b"):
        got = PluginHost._config_key_to_folder(evil)
        assert ".." not in got, f"{evil!r} 洗出了 {got!r}，含 .."


def test_schema_item_finds_nested_file_fields():
    """`_schema_item` 要能找到**嵌在 object 里**的 file 字段。

    ⚠️ 我第一版只处理了"顶层键"那一种形状，`a.b`（`a` 是 object、
       `b` 在它的 `items` 里）**找不到** —— 而那正是插件里最常见的嵌套形状。
       ⇒ 这条判据专门守嵌套。
    """
    from astrbot_host.host_app import PluginHost

    sch = {
        "top": {"type": "file", "file_types": ["png"]},
        "a": {"type": "object", "items": {"b": {"type": "file", "file_types": ["jpg"]}}},
    }
    assert PluginHost._schema_item(sch, "top")["type"] == "file"
    nested = PluginHost._schema_item(sch, "a.b")
    assert nested is not None, "嵌套的 file 字段没找到"
    assert nested["type"] == "file"
    assert nested["file_types"] == ["jpg"]
    assert PluginHost._schema_item(sch, "nope") is None


#: 真有 `type: "file"` 配置项的插件（本机实测只有一个）。
#: ⚠️ 不复用 `PLUGIN_ID`（`astrbot_plugin_splitter` 没有 file 字段）——
#:    拿它测 config-files 只能测到"不是 file 型"那条分支。
FILE_PLUGIN_ID = "astrbot_plugin_nai_image"
FILE_CONFIG_KEY = "openai_director_fallback_images"


async def test_config_file_routes_require_auth(make):
    """🔴 三条 config-files 路由**都必须鉴权**。

    本项目真事故：我加的路由漏了 `require_auth()` → 未登录也能读/写，
    而服务绑 `0.0.0.0` = 公网可访问。
    ⚠️ 断言 **401**（不是"不是 200"）—— 后者在 500 时也会过。
    """
    c = await make(login=False)
    q = f"?config_key={FILE_CONFIG_KEY}"
    r = await c.get(f"/api/v1/plugins/{FILE_PLUGIN_ID}/config-files{q}")
    assert r.status_code == 401, r.status_code
    r = await c.post(f"/api/v1/plugins/{FILE_PLUGIN_ID}/config-files{q}")
    assert r.status_code == 401, r.status_code
    r = await c.delete(f"/api/v1/plugins/{FILE_PLUGIN_ID}/config-files")
    assert r.status_code == 401, r.status_code


async def test_config_files_rejects_non_file_config_item(make):
    """⚠️ 拿一个**不是 file 型**的配置项去要文件清单 → 409（不是空列表）。

    🔴 为什么必须是 409 而不是"空列表"：
       "这个配置项不收文件" 与 "这个配置项还没有文件" 是**两件事**，
       给用户的下一步动作完全不同（前者要改配置项，后者要上传）。
       显示成空列表 = 用户一直传、一直失败、不知道为什么。
    """
    c = await make()
    r = await c.get(
        f"/api/v1/plugins/{PLUGIN_ID}/config-files?config_key=__definitely_not_a_key__"
    )
    assert r.status_code == 409, await r.get_data(as_text=True)
    d = await r.get_json()
    assert "file 类型" in d["error"]["message"] or "不存在" in d["error"]["message"]


async def test_config_files_roundtrip_on_real_plugin(make):
    """在有真 file 字段的插件上跑一遍**列表 → 上传 → 列表 → 删除**。

    ⚠️ 用 `make()` 的 tmp_path 作数据目录（`_cfg` 已经这么配了）——
       **绝不碰** `/opt/astrbot/data/`（那是别人 AstrBot 的生产数据）。
    """
    c = await make()
    base = f"/api/v1/plugins/{FILE_PLUGIN_ID}/config-files"
    q = f"?config_key={FILE_CONFIG_KEY}"

    # ① 空清单：必须带 `file_types`（前端要用它生成 accept）
    r = await c.get(base + q)
    assert r.status_code == 200, await r.get_data(as_text=True)
    d = await r.get_json()
    assert d["files"] == []
    assert d["file_types"] == ["png", "jpg", "jpeg", "webp"]

    # ② 上传一张真 png
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
    r = await c.post(
        base + q,
        files={"file": FileStorage(io.BytesIO(png), filename="unit_probe.png")},
    )
    assert r.status_code == 200, await r.get_data(as_text=True)
    up = (await r.get_json())["uploaded"]
    assert len(up) == 1 and up[0]["uploaded"].endswith("unit_probe.png")

    # ③ 清单里真有了
    r = await c.get(base + q)
    files = (await r.get_json())["files"]
    assert any(f.endswith("unit_probe.png") for f in files), files

    # ④ 删掉
    rel = next(f for f in files if f.endswith("unit_probe.png"))
    r = await c.delete(base, json={"key": FILE_CONFIG_KEY, "path": rel})
    assert r.status_code == 200, await r.get_data(as_text=True)
    r = await c.get(base + q)
    assert not any(f.endswith("unit_probe.png") for f in (await r.get_json())["files"])


async def test_config_file_upload_rejects_bad_extension(make):
    """⚠️ 扩展名不符要**拒绝**，且理由是人话。

    🔴 为什么这条重要：不校验的话用户能把任意文件传进"只收图片"的配置项，
       而插件读图失败时**大概率只打一行日志**（本项目最贵的 bug 类型）——
       用户只会觉得"图没生效"，无从归因。
    """
    c = await make()
    base = f"/api/v1/plugins/{FILE_PLUGIN_ID}/config-files"
    q = f"?config_key={FILE_CONFIG_KEY}"
    r = await c.post(
        base + q,
        files={"file": FileStorage(io.BytesIO(b"not an image"), filename="evil.txt")},
    )
    assert r.status_code == 400, await r.get_data(as_text=True)
    msg = (await r.get_json())["error"]["message"]
    # ⚠️ 断言消息里**真的说了**允许哪些类型（不是一句泛泛的"上传失败"）
    assert ".png" in msg and ".txt" in msg, msg


async def test_config_file_delete_rejects_path_escape(make):
    """🔴 删除必须挡住路径穿越与"删别人文件"。

    两道校验缺一不可（`delete_plugin_config_file`）：
      ① 路径必须在**这个配置项自己的目录**里 —— 否则能删同插件别的项的文件；
      ② 不能有 `..` —— 否则能删插件本体（`../../main.py`）。
    """
    c = await make()
    base = f"/api/v1/plugins/{FILE_PLUGIN_ID}/config-files"
    for bad in (
        "files/../../main.py",                      # 穿越到插件本体
        "files/some_other_key/x.png",               # 别的配置项的目录
        FILE_CONFIG_KEY + "/../x.png",              # 相对穿越
    ):
        r = await c.delete(base, json={"key": FILE_CONFIG_KEY, "path": bad})
        assert r.status_code == 400, f"{bad!r} 居然没被拒：{r.status_code}"

    # 🔴 **必须断言拒绝的【理由】**，不能只看状态码。
    #    ⚠️ 这条是我被注入验证逼出来的：我第一版只断言 `== 400`，
    #       而把归属校验停用后，这些路径会走到"文件不存在"那个分支 ——
    #       **照样是 400、测试照样绿**，于是注入"C3 抓不住"。
    #       两者对用户的意义完全不同：一个是"你越界了（安全问题）"，
    #       另一个是"这文件没有（正常的空操作）"。
    r = await c.delete(base, json={"key": FILE_CONFIG_KEY, "path": "files/some_other_key/x.png"})
    msg = (await r.get_json())["error"]["message"]
    assert "不属于这个配置项" in msg, f"拒绝理由不对（可能是走到了「文件不存在」）：{msg}"
