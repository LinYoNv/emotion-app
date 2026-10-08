# -*- coding: utf-8 -*-
"""人设参考图：**真 HTTP** 端到端判据（2026-09-25）。

## 为什么单独一个文件、且必须走真 HTTP

情绪 2026-09-25：「**用户能填的就让用户改**」—— 参考图此前只有 `.env` 一个入口，
手机壳上等于没法配。这次把它做成面板上传（per-persona）。

🔴 这个能力的**失效模式全是"静默"的**，所以判据必须落到 HTTP 层：
   * 路由漏挂 → 面板 404（用户看到"上传失败"，但不知道为什么）；
   * 白名单漏字段 → 接口 **200**、图也落盘了，**但字段没存** → 重启后消失；
   * 落盘目录与解析目录不一致 → 面板显示"已配置"，生图时读不到；
   * 扩展名换了旧文件没清 → 磁盘留垃圾，且 `resolve` 可能认到旧的那张。

⚠️ 与 `tests/test_s6_image_reference.py` 的分工：
   那里测**函数**（`ImageToolService` 会不会用参考图）；
   这里测**接线**（面板能不能把图存进去、存完生图能不能取到）。
   本项目最贵的一课：「单测全绿 + 接口判据全绿 + 页面能打开 —— 全都不算证据」，
   而上一轮注入验证第一轮 3/10 没抓住，正是因为只测了函数没测接线。
"""
from __future__ import annotations

import base64

import pytest

from emotion_app.domain.persona import Persona
from emotion_app.domain.persona_ref import (
    MAX_PERSONA_REF_BYTES,
    is_safe_name,
    ref_dir,
    resolve_persona_reference,
)
from emotion_app.domain.persona_repo import PersonaRepository
from emotion_app.infra.config import Config
from emotion_app.storage.sqlite_store import SqliteStore
from emotion_app.web.app import create_app
from emotion_app.web.auth import AuthManager

TOKEN = "t" * 32

#: 一个**真的** 1×1 PNG（不是手拼的 hex —— 手拼的会被上游拒，本项目踩过）
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAF"
    "BQIAX8jx0gAAAABJRU5ErkJggg=="
)


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    """真 app + 真 sqlite + 真登录会话。

    ⚠️ `ref_dir()` 是相对运行目录的常量 —— 测试必须把它**指到 tmp_path**，
       否则会往项目 `data/` 里写垃圾（也会让判据之间互相污染）。
    """
    monkeypatch.setattr(
        "emotion_app.domain.persona_ref.PERSONA_REF_DIR", str(tmp_path / "refs")
    )
    cfg = Config()
    cfg.auth.access_token = TOKEN
    cfg.storage.sqlite_path = str(tmp_path / "ref.db")
    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()

    personas = PersonaRepository(store)
    app = create_app(
        cfg, store, AuthManager(access_token=TOKEN), None, personas=personas
    )
    client = app.test_client()
    await client.post("/api/v1/auth/login", json={"token": TOKEN})

    p = await personas.create(Persona(name="测试角色"))
    yield client, personas, p, tmp_path
    await store.close()


def _png_upload(name: str = "ref.png"):
    """构造一个 multipart 上传体。

    ⚠️ 必须用 `FileStorage` —— Quart 的测试客户端收 `{字段: FileStorage}`；
       直接传 `(BytesIO, name)` 元组会在测试框架里抛
       `AttributeError: 'tuple' object has no attribute 'filename'`
       （那是**测试写法**的问题，不是被测代码的问题）。
    """
    import io

    from quart.datastructures import FileStorage

    return {"file": FileStorage(io.BytesIO(PNG_1PX), filename=name)}


# ===========================================================================
# 鉴权（🔴 反向也要测 —— 本项目真事故：/api/v1/logs 漏了 require_auth）
# ===========================================================================


async def test_all_three_routes_require_auth(tmp_path, monkeypatch):
    """🔴 **未登录时三条路由都必须 401**。

    本项目真事故：我加的 `/api/v1/logs` 漏了 `require_auth()` → 未登录返 200，
    而服务绑 `0.0.0.0` = 公网可读运行日志。参考图是**私有内容**，同样不能漏。
    """
    monkeypatch.setattr(
        "emotion_app.domain.persona_ref.PERSONA_REF_DIR", str(tmp_path / "refs")
    )
    cfg = Config()
    cfg.auth.access_token = TOKEN
    cfg.storage.sqlite_path = str(tmp_path / "noauth.db")
    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()
    app = create_app(cfg, store, AuthManager(access_token=TOKEN))
    c = app.test_client()          # ⚠️ 故意**不登录**

    url = "/api/v1/personas/some-id/reference"
    r1 = await c.post(url, files=_png_upload())
    r2 = await c.get(url)
    r3 = await c.delete(url)
    assert r1.status_code == 401, f"POST 未鉴权也能上传：{r1.status_code}"
    assert r2.status_code == 401, f"GET 未鉴权也能读参考图：{r2.status_code}"
    assert r3.status_code == 401, f"DELETE 未鉴权也能删：{r3.status_code}"
    await store.close()


# ===========================================================================
# 上传 → 字段 → 落盘 → 生图取得到（端到端）
# ===========================================================================


async def test_upload_stores_field_and_file(ctx):
    """⭐ 核心：上传后**字段与文件都在**，且 `resolve` 能解析出绝对路径。

    ⚠️ 只断言 200 是**不够的**（本项目踩过"接口全绿、界面空白"）：
       必须落到"字段真的存了 + 文件真的在那 + 生图那条路真的解析得到"。
    """
    client, personas, p, tmp = ctx

    r = await client.post(
        f"/api/v1/personas/{p.id}/reference", files=_png_upload("face.png")
    )
    assert r.status_code == 200, await r.get_data(as_text=True)
    body = await r.get_json()
    assert body["ok"] is True

    # ① 字段真的存进去了（重启也不会丢）
    fresh = await personas.get(p.id)
    assert fresh.reference_image_path == f"{p.id}.png", fresh.reference_image_path

    # ② 文件真的在解析函数认的目录里
    resolved = resolve_persona_reference(fresh)
    assert resolved, "上传成功但 resolve 解析不出来 —— 面板会说'已配置'而生图读不到"
    from pathlib import Path

    blob = Path(resolved).read_bytes()
    assert blob == PNG_1PX, "落盘内容与上传的不一致"

    # ③ 面板靠这个布尔画预览
    assert fresh.to_dict()["has_reference_image"] is True


async def test_uploaded_file_is_served(ctx):
    """面板要能把图**显示出来**（否则用户无法确认传对没有）。"""
    client, _, p, _ = ctx
    await client.post(f"/api/v1/personas/{p.id}/reference", files=_png_upload())

    r = await client.get(f"/api/v1/personas/{p.id}/reference")
    assert r.status_code == 200, r.status_code
    assert (await r.get_data()) == PNG_1PX


async def test_upload_replaces_and_cleans_old_extension(ctx):
    """换扩展名重传 → 旧文件要被清掉（否则磁盘留垃圾，且可能与字段不一致）。"""
    client, personas, p, tmp = ctx

    await client.post(f"/api/v1/personas/{p.id}/reference", files=_png_upload("a.png"))
    assert (ref_dir() / f"{p.id}.png").is_file()

    r = await client.post(
        f"/api/v1/personas/{p.id}/reference", files=_png_upload("a.jpg")
    )
    assert r.status_code == 200
    fresh = await personas.get(p.id)
    assert fresh.reference_image_path == f"{p.id}.jpg", fresh.reference_image_path
    assert not (ref_dir() / f"{p.id}.png").exists(), "旧扩展名的文件没被清掉"
    assert (ref_dir() / f"{p.id}.jpg").is_file()


async def test_delete_clears_both_field_and_file(ctx):
    """删除要**字段与文件一起清**。

    ⚠️ 只清文件不干净字段 → 面板永远显示"已配置"而实际读不到
       （本项目最忌的"看着有、其实没有"）。
    """
    client, personas, p, _ = ctx
    await client.post(f"/api/v1/personas/{p.id}/reference", files=_png_upload())

    r = await client.delete(f"/api/v1/personas/{p.id}/reference")
    assert r.status_code == 200
    body = await r.get_json()
    assert body["file_removed"] is True

    fresh = await personas.get(p.id)
    assert fresh.reference_image_path == "", "字段没清 → 面板会一直显示已配置"
    assert not (ref_dir() / f"{p.id}.png").exists()
    assert resolve_persona_reference(fresh) == ""


async def test_delete_when_file_already_gone_still_clears_field(ctx):
    """文件已经不在了 → 删除仍必须**成功并清字段**（不能因为文件没了就 500）。"""
    client, personas, p, _ = ctx
    await client.post(f"/api/v1/personas/{p.id}/reference", files=_png_upload())
    # 模拟"文件被手工删了"
    (ref_dir() / f"{p.id}.png").unlink()

    r = await client.delete(f"/api/v1/personas/{p.id}/reference")
    assert r.status_code == 200, await r.get_data(as_text=True)
    assert (await r.get_json())["file_removed"] is False
    fresh = await personas.get(p.id)
    assert fresh.reference_image_path == ""


# ===========================================================================
# 拒绝路径（都要有能失败的判据）
# ===========================================================================


async def test_upload_rejects_bad_extension(ctx):
    """非图片扩展名 → 400，且**不能**留下文件。"""
    client, personas, p, _ = ctx
    r = await client.post(
        f"/api/v1/personas/{p.id}/reference", files=_png_upload("evil.svg")
    )
    assert r.status_code == 400, r.status_code
    assert not list(ref_dir().glob(f"{p.id}*")), "被拒的上传竟然落了盘"
    assert (await personas.get(p.id)).reference_image_path == ""


async def test_upload_rejects_empty_file(ctx):
    """空文件 → 400（不能让"空参考图"变成一张隐形配置）。"""
    import io

    client, _, p, _ = ctx
    from quart.datastructures import FileStorage

    r = await client.post(
        f"/api/v1/personas/{p.id}/reference",
        files={"file": FileStorage(io.BytesIO(b""), filename="empty.png")},
    )
    assert r.status_code == 400, r.status_code


async def test_upload_rejects_oversize(ctx, monkeypatch):
    """超过上限 → 400，且不落盘。

    ⚠️ 用 `monkeypatch` 把上限调小，**不要真造一个 12MB 的文件**
       （测试要快，而且真造大文件会让这个判据变成磁盘压力测试）。
    """
    import io

    monkeypatch.setattr(
        "emotion_app.domain.persona_ref.MAX_PERSONA_REF_BYTES", 16
    )
    client, _, p, _ = ctx
    from quart.datastructures import FileStorage

    r = await client.post(
        f"/api/v1/personas/{p.id}/reference",
        files={"file": FileStorage(io.BytesIO(PNG_1PX), filename="big.png")},
    )
    assert r.status_code == 400, r.status_code
    assert not list(ref_dir().glob(f"{p.id}*")), "超限的上传竟然落了盘"


async def test_upload_missing_field_is_400_not_500(ctx):
    """没带 `file` 字段 → 400 且**说清楚要什么**（不是 500 崩掉）。"""
    client, _, p, _ = ctx
    r = await client.post(f"/api/v1/personas/{p.id}/reference", json={})
    assert r.status_code == 400, r.status_code
    msg = (await r.get_json())["error"]["message"]
    assert "file" in msg, f"错误信息没说清要哪个字段：{msg}"


async def test_upload_unknown_persona_is_404(ctx):
    """人设不存在 → 404（不能给它凭空建一个）。"""
    client, _, _, _ = ctx
    r = await client.post(
        "/api/v1/personas/nope/reference", files=_png_upload()
    )
    assert r.status_code == 404, r.status_code


# ===========================================================================
# 路径穿越 / 名字安全（与 `get_avatar` 同款威胁）
# ===========================================================================


async def test_download_rejects_path_traversal(ctx):
    """🔴 字段被手工改成 `../x.png` 时，**不许**把文件读出去。

    ⚠️ 不能只信"上传时校验过"：存储里的值也可能被手工改过，或来自旧备份。
    """
    client, personas, p, tmp = ctx
    await personas.update(p.id, {"reference_image_path": "../evil.png"})

    r = await client.get(f"/api/v1/personas/{p.id}/reference")
    assert r.status_code == 400, (
        f"越权路径竟然没被拒（{r.status_code}）—— 可能读到 data/ 外的文件"
    )


async def test_resolve_rejects_unsafe_names_and_logs(caplog):
    """`resolve_persona_reference` 对非法名要**响亮拒绝**（留日志），返回空串。"""
    import logging

    caplog.set_level(logging.WARNING, logger="emotion_app.domain.persona_ref")
    bad = Persona(name="x", reference_image_path="../../etc/passwd")
    assert resolve_persona_reference(bad) == ""
    assert any("非法" in r.getMessage() for r in caplog.records), (
        "非法文件名没留日志 —— 静默失败是本项目最贵的一类 bug"
    )


def test_is_safe_name_contract():
    """名字安全判据本身要对（正例反例都要有）。

    ⚠️ 我先写错过这条：一度断言 `is_safe_name("abc.JPG") is False`。
       **查清后确认代码是对的、断言是错的** —— 扩展名判断**必须大小写不敏感**
       （否则用户传一张 `FACE.PNG` 就被拒，那是误杀合法图）。
       上传路由存的是 `suffix.lower()`，所以存储值恒为小写；
       这个函数守的是"存储值被手工改坏 / 来自旧备份 / 路径穿越"。
       （本项目铁律：断言失败先查清哪个值才对，别条件反射改代码。）
    """
    assert is_safe_name("abc.png")
    assert is_safe_name("abc.JPG") is True, "大小写不敏感（拒绝它 = 误杀合法图）"
    assert is_safe_name("abc.WebP") is True
    # 反例：这些必须全部为 False
    assert is_safe_name("../x.png") is False
    assert is_safe_name("a/b.png") is False
    assert is_safe_name(".hidden.png") is False
    assert is_safe_name("") is False
    assert is_safe_name("x.txt") is False
    assert is_safe_name("x.png.exe") is False


# ===========================================================================
# 上限常量本身（防止有人把它调成 0 或负数 —— 那会让上传永远失败）
# ===========================================================================


def test_limits_are_sane():
    assert 0 < MAX_PERSONA_REF_BYTES <= 20 * 1024 * 1024
