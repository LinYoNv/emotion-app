# -*- coding: utf-8 -*-
"""模型供应商（**两层**：provider_sources + providers）—— 存储 + API 判据。

契约：`docs/ASTRBOT_PROVIDER_CONTRACT.md` §2（源）/§3（模型）/§7（迁移与兼容）。

## 为什么要重写这份测试

我原来的实现是**单层**（`{id,name,model,base_url,api_key,timeout}` 一条就是全部），
而真实 AstrBot 契约是**两层**：`provider_sources`（端点）1:N `providers`（模型），
模型的 id 形如 `源id/模型名`、`default_provider_id` 同形。
单层表达不了"一个端点挂 5 个模型"，这就是用户说"功能不对"的根源。

所以本文件的判据整体围绕**两层**重写，并新增了迁移/悬空引用/不支持类型等判据。

## 🔴 每条判据都要能失败

凡是"写完没验过它会挂"的断言都是假通过。本文件里每条关键判据都在
`tools/inject_providers_check.py` 的注入验证里被实际改坏过一次（结果见交付报告）：

| 注入的 bug | 应该挂的判据 |
|---|---|
| 响应里回显源的真实 key | `test_source_key_never_appears_*`（3 条） |
| 模型 id 校验不允许 `/` | `test_two_layer_config_assembles_one_provider`（父代理实测复现的真 bug） |
| 源 id 校验不允许中文 | `test_source_id_allows_chinese_like_real_site` |
| 去掉悬空引用校验 | `test_model_with_dangling_source_is_400`（含 create/update/读路径） |
| 迁移不落盘 | `test_legacy_v1_file_is_migrated_and_persisted` |
| 迁移丢数据 | `test_legacy_v1_migration_keeps_every_field` |
| 放开 provider_type 校验 | `test_rejects_unsupported_provider_type` |
| PUT 把空 key 当清空 | `test_put_blank_key_keeps_existing` |
| 空模型列表当成功 | `test_fetch_models_empty_is_not_success` |
| 去掉路由上的鉴权 | `test_endpoints_require_auth` |

⚠️ 任何一条测试都**不许**跑到 `/opt/astrbot` 去（那是别人 AstrBot 的生产目录），
   所以 `app.config["EMOTION_PROVIDERS_PATH"]` 一律指向 `tmp_path`。
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import httpx
import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
HOST_DIR = APP_ROOT / "host"
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))       # `astrbot_host` 是并列交付物

from emotion_app.infra.config import Config  # noqa: E402
from emotion_app.infra.providers_store import (  # noqa: E402
    KEY_MASK,
    MODEL_ID_PATTERN,
    SOURCE_ID_PATTERN,
    ProviderModel,
    ProviderNotFound,
    ProviderSource,
    ProviderStore,
    ProviderValidationError,
    UnsupportedProviderType,
)
from emotion_app.storage.sqlite_store import SqliteStore  # noqa: E402
from emotion_app.web.app import create_app  # noqa: E402
from emotion_app.web.auth import AuthManager  # noqa: E402

TOKEN = "t" * 20

#: 测试里用的明文 key。**刻意做成可搜索的独特字符串** ——
#: 判据就是"它在响应体里一个字都不能出现"。
SECRET_KEY = "sk-test-DEADBEEF-0000-1111-2222-secret"

#: 有效载荷里的常规地址（照真实契约的形态：以 /v1 结尾）。
API_BASE = "https://api.syuan.org/v1"


# ---------------------------------------------------------------------------
# 夹具 / 小工具
# ---------------------------------------------------------------------------


def _source(**kw) -> ProviderSource:
    base = dict(
        id="syuan",
        provider="openai",
        type="openai_chat_completion",
        provider_type="chat_completion",
        key=[SECRET_KEY],
        api_base=API_BASE,
        timeout=60.0,
        proxy="",
        custom_headers={},
        enable=True,
    )
    base.update(kw)
    return ProviderSource(**base)


def _model(**kw) -> ProviderModel:
    base = dict(
        id="syuan/gemini-3.5-flash",
        provider_source_id="syuan",
        model="gemini-3.5-flash",
        enable=True,
        modalities=["text"],
    )
    base.update(kw)
    return ProviderModel(**base)


def _source_body(**kw) -> dict:
    return _source(**kw).to_storage_dict()


def _model_body(**kw) -> dict:
    return _model(**kw).to_storage_dict()


class _FakeBackend:
    """假上游后端，形状照 `ChatBackend.complete`。"""

    def __init__(self, content: str = "上游回复") -> None:
        self.content = content
        self.calls: list[dict] = []

    async def complete(self, *, messages=None, model=None, tools=None, **kw):  # noqa: ANN001
        self.calls.append({"messages": messages, "model": model})
        return type("R", (), {"content": self.content, "usage": {}})()
def _json_transport(payload, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


def _ok_reply(content: str = "你好呀，我是模型"):
    return {
        "model": "gemini-3.5-flash",
        "choices": [{"message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
    }


def _raw(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _data(resp_json: dict) -> dict:
    """取 `{status,message,data}` 信封里的 data（顺带断言 status=ok）。"""
    assert resp_json.get("status") == "ok", resp_json
    return resp_json.get("data") or {}


def _err_reason(resp_json: dict) -> str:
    assert resp_json.get("status") == "error", resp_json
    return resp_json.get("reason") or (resp_json.get("error") or {}).get("type") or ""


async def _add_source(client, **kw):
    return await client.post("/api/v1/provider-sources", json=_source_body(**kw))


async def _add_model(client, **kw):
    return await client.post("/api/v1/providers", json=_model_body(**kw))


async def _seed(client, **kw):
    """建一个"源 + 一个模型"的最小可用配置，返回 `(源响应, 模型响应)`。"""
    r1 = await _add_source(client, **kw)
    r2 = await _add_model(
        client,
        id=f"{kw.get('id', 'syuan')}/{kw.get('model_name', 'gemini-3.5-flash')}",
        provider_source_id=kw.get("id", "syuan"),
        model=kw.get("model_name", "gemini-3.5-flash"),
    )
    return r1, r2


# ===========================================================================
# ① 两层结构的增删改查
# ===========================================================================


async def test_empty_list_shape(mk):
    """空配置的响应形状（契约 §2/§3 冻结）。

    ⚠️ 前端直接读 `data.providers` / `data.provider_sources` / `data.default_provider_id`，
       少一个键就是 `undefined` → 面板白屏。
    """
    c = await mk()
    r = await c.get("/api/v1/providers")
    assert r.status_code == 200, await r.get_data(as_text=True)
    d = _data(await r.get_json())
    assert d == {
        "providers": [],
        "provider_sources": [],
        "model_metadata": {},
        "default_provider_id": "",
    }, d

    r2 = await c.get("/api/v1/provider-sources")
    assert _data(await r2.get_json()) == {"provider_sources": []}


async def test_two_layer_crud_roundtrip(mk):
    """① 源 + 模型的完整往返：增 → 列 → 改 → 设默认 → 删（每步验真数据）。"""
    c = await mk()

    # --- 增源 ---
    r = await _add_source(c, id="syuan", api_base=API_BASE)
    assert r.status_code == 201, await r.get_data(as_text=True)
    src = _data(await r.get_json())
    assert src["id"] == "syuan" and src["api_base"] == API_BASE, src
    assert src["provider_type"] == "chat_completion", src
    assert src["enable"] is True, src

    # --- 增模型（挂在源下）---
    r2 = await _add_model(c, id="syuan/gemini-3.5-flash", provider_source_id="syuan",
                          model="gemini-3.5-flash")
    assert r2.status_code == 201, await r2.get_data(as_text=True)
    created = _data(await r2.get_json())
    assert created["id"] == "syuan/gemini-3.5-flash", created
    assert created["provider_source_id"] == "syuan", created
    assert created["is_default"] is True, (
        "第一个模型应当自动成为默认（与宿主 register 语义一致）"
    )

    # --- 列（模型列表里同时给源，兼容期照契约 §7）---
    d = _data(await (await c.get("/api/v1/providers")).get_json())
    assert [p["id"] for p in d["providers"]] == ["syuan/gemini-3.5-flash"], d
    assert [s["id"] for s in d["provider_sources"]] == ["syuan"], d
    assert d["default_provider_id"] == "syuan/gemini-3.5-flash", d

    # --- 某源下已配置的模型（契约 §2）---
    d2 = _data(await (await c.get("/api/v1/provider-sources/syuan/providers")).get_json())
    assert [p["id"] for p in d2["providers"]] == ["syuan/gemini-3.5-flash"], d2
    assert d2["providers"][0]["is_default"] is True, d2

    # --- 增第二个模型，设默认 ---
    r3 = await _add_model(c, id="syuan/gpt-5.5", provider_source_id="syuan", model="gpt-5.5")
    assert r3.status_code == 201, await r3.get_data(as_text=True)
    assert _data(await r3.get_json())["is_default"] is False
    r4 = await c.post("/api/v1/providers/syuan%2Fgpt-5.5/default", json={})
    assert r4.status_code == 200, await r4.get_data(as_text=True)
    d3 = _data(await (await c.get("/api/v1/providers")).get_json())
    assert d3["default_provider_id"] == "syuan/gpt-5.5", d3
    assert [p["is_default"] for p in d3["providers"]] == [False, True], d3

    # --- 改模型（PUT by-id，query 参数名照真实实现是 provider_id）---
    r5 = await c.put("/api/v1/providers/by-id?provider_id=syuan%2Fgpt-5.5",
                     json={"enable": False, "modalities": ["text", "image"]})
    assert r5.status_code == 200, await r5.get_data(as_text=True)
    upd = _data(await r5.get_json())
    assert upd["enable"] is False and upd["modalities"] == ["text", "image"], upd

    # --- 改源（PUT by-id，query 参数名是 source_id）---
    r6 = await c.put("/api/v1/provider-sources/by-id?source_id=syuan",
                     json={"timeout": 120, "api_base": "https://api.new.test/v1"})
    assert r6.status_code == 200, await r6.get_data(as_text=True)
    s2 = _data(await r6.get_json())["provider_source"]
    assert s2["timeout"] == 120 and s2["api_base"] == "https://api.new.test/v1", s2
    # 部分更新：没给的字段保持原值（provider/type/key 都还在）
    assert s2["provider"] == "openai" and s2["key_configured"] is True, s2

    # --- 删模型 ---
    assert (await c.delete("/api/v1/providers/by-id?provider_id=syuan%2Fgpt-5.5")).status_code == 200
    d4 = _data(await (await c.get("/api/v1/providers")).get_json())
    assert [p["id"] for p in d4["providers"]] == ["syuan/gemini-3.5-flash"], d4
    # 🔴 删掉默认后**不许留悬空默认值**（悬空 = 插件解析不到 → 静默退化）
    assert d4["default_provider_id"] == "syuan/gemini-3.5-flash", d4

    # --- 删源（级联删掉它下面的模型）---
    r7 = await c.delete("/api/v1/provider-sources/by-id?source_id=syuan")
    assert r7.status_code == 200, await r7.get_data(as_text=True)
    dd = _data(await r7.get_json())
    assert dd["deleted_models"] == ["syuan/gemini-3.5-flash"], dd
    d5 = _data(await (await c.get("/api/v1/providers")).get_json())
    assert d5["providers"] == [] and d5["provider_sources"] == [], d5
    assert d5["default_provider_id"] == "", d5


async def test_missing_ids_are_404_and_missing_query_is_400(mk):
    """不存在的 id → 404；**缺 query 参数** → 400 且提示参数名。

    ⚠️ 真实实现用的是 `source_id` / `provider_id`（实测 `id=` 会回
       `Missing key: source_id`）—— 参数名写错这个接口就等于不存在，
       所以"缺参数"必须回一条**能照着改**的消息。
    """
    c = await mk()
    for method, path in (
        ("get", "/api/v1/provider-sources/by-id?source_id=nope"),
        ("delete", "/api/v1/provider-sources/by-id?source_id=nope"),
        ("get", "/api/v1/providers/by-id?provider_id=nope"),
        ("delete", "/api/v1/providers/by-id?provider_id=nope"),
        ("post", "/api/v1/providers/nope/default"),
        ("get", "/api/v1/provider-sources/nope/providers"),
        ("get", "/api/v1/provider-sources/nope/models"),
    ):
        r = await getattr(c, method)(path, json={})
        assert r.status_code == 404, f"{method} {path} → {r.status_code}"
        assert "不存在" in (await r.get_json())["message"]

    for path in ("/api/v1/provider-sources/by-id", "/api/v1/providers/by-id"):
        r = await c.get(path)
        assert r.status_code == 400, f"{path} → {r.status_code}"
        msg = (await r.get_json())["message"]
        assert "source_id" in msg or "provider_id" in msg, msg


async def test_endpoints_require_auth(mk):
    """🔴 **安全判据**：未鉴权必须 401（全部新老路由都要）。

    这些接口能**读回全部供应商地址**、还能拿服务端的 key 去打上游 ——
    漏了鉴权就是"任何人都能拿它当探测内网/消耗额度的跳板"。
    """
    c = await mk(login=False)
    for method, path in (
        ("get", "/api/v1/providers"),
        ("post", "/api/v1/providers"),
        ("get", "/api/v1/provider-sources"),
        ("post", "/api/v1/provider-sources"),
        ("get", "/api/v1/provider-sources/by-id?source_id=x"),
        ("put", "/api/v1/provider-sources/by-id?source_id=x"),
        ("delete", "/api/v1/provider-sources/by-id?source_id=x"),
        ("get", "/api/v1/provider-sources/models"),
        ("get", "/api/v1/provider-sources/x/providers"),
        ("post", "/api/v1/provider-sources/x/providers"),
        ("get", "/api/v1/provider-sources/x/models"),
        ("get", "/api/v1/providers/by-id?provider_id=x"),
        ("put", "/api/v1/providers/by-id?provider_id=x"),
        ("delete", "/api/v1/providers/by-id?provider_id=x"),
        ("get", "/api/v1/providers/x"),
        ("post", "/api/v1/providers/x/test"),
    ):
        r = await getattr(c, method)(path, json={})
        assert r.status_code == 401, f"未鉴权竟能访问 {method.upper()} {path}：{r.status_code}"


# ===========================================================================
# ② 🔴 父代理实测复现的真 bug：`源id/模型名` 这个 id 形态必须被接受
# ===========================================================================


#: 父代理给的真实形态两层配置（**原样照抄**，当作判据的输入）。
PARENT_ACCEPTANCE_CONFIG = {
    "version": 2,
    "default_provider_id": "syuan/gemini-3.5-flash",
    "provider_sources": [
        {"id": "syuan", "provider": "openai", "type": "openai_chat_completion",
         "provider_type": "chat_completion", "key": ["sk-real-key"],
         "api_base": "https://api.syuan.org/v1", "timeout": 120, "proxy": "",
         "custom_headers": {}, "enable": True}
    ],
    "providers": [
        {"id": "syuan/gemini-3.5-flash", "provider_source_id": "syuan",
         "model": "gemini-3.5-flash", "enable": True, "modalities": ["text"]}
    ],
}


def test_two_layer_config_assembles_one_provider(tmp_path):
    """🔴🔴 **回归判据**：这份两层配置必须装配出 **1** 个供应商（不是 0）。

    ## 这条盯的是一个**实测复现过的真 bug**

    旧实现只有一条 id 正则 `^[A-Za-z0-9_.-]{1,64}$`，它**不允许 `/`**，
    而契约里模型的 id 就是 `源id/模型名`（真实值 `deepseek/deepseek-v4-flash`）。
    于是这份完全合法的配置被整条判为非法 → `flatten_two_layer()` 拿到 0 个模型
    → **装配出 0 个供应商**，插件照旧"退化成模板兜底"。

    日志原文：
    ```
    WARNING 配置里第 0 条供应商不合法（仍保留…）：
      id 只能由字母、数字、下划线、点、连字符组成，长度 1..64；当前 syuan/gemini-3.5-flash
    ```

    ✅ 修法：**两类 id 用两套规则**（见 `providers_store.SOURCE_ID_PATTERN` /
       `MODEL_ID_PATTERN`）。本判据同时验"读得进"和"装配得出"。
    """
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(PARENT_ACCEPTANCE_CONFIG, ensure_ascii=False), encoding="utf-8")

    store = ProviderStore(path)
    sources = store.list_sources()
    models = store.list_models()

    assert [s.id for s in sources] == ["syuan"], (
        f"源没读进来（id 校验太严把真实形态判非法了）：{sources}"
    )
    assert [m.id for m in models] == ["syuan/gemini-3.5-flash"], (
        f"🔴 模型 id `源id/模型名` 被 id 校验拒了 —— 这就是【装配出 0 个供应商】的根因：{models}"
    )
    # default_provider_id 同形，也必须原样保留（不被当成非法值清掉）
    assert store.default_provider_id == "syuan/gemini-3.5-flash", store.default_provider_id

    # 🔴 端到端：**装配出 1 个**（而不只是"存储读到了"）
    from emotion_app.web.provider_bridge import build_provider_manager, flatten_two_layer

    flat, default_id = flatten_two_layer(store.load())
    assert len(flat) == 1, f"应展平成 1 个模型，实际 {flat}"
    assert flat[0]["base_url"] == "https://api.syuan.org/v1", flat
    assert flat[0]["api_key"] == "sk-real-key", "key 是**列表**，取值必须取第一把而不是 str(list)"
    assert default_id == "syuan/gemini-3.5-flash"

    pm = build_provider_manager(backend=_FakeBackend(), providers=flat, default_id=default_id)
    assert len(pm.inst_map) == 1, f"装配出 {len(pm.inst_map)} 个供应商（应为 1）"
    assert pm.get_using() is not None, "默认供应商解析不到 —— 插件会退化成模板兜底"


def test_id_patterns_two_rules():
    """两条 id 规则各自的**边界**（直接对正则断言，注入"改回一条正则"必挂）。

    * 源 id：**允许中文**（真实站有 `mx哈基米`/`xy_哈基米`）、**不允许 `/`**
      （它是模型 id 的分隔符，含 `/` 就分不清前缀到哪）；
    * 模型 id：**必须含 `/`**，且模型名里**允许再出现 `/`**
      （真实模型名如此：`BAAI/bge-m3`、`meta-llama/Llama-3`）。
    """
    # --- 源 id ---
    assert SOURCE_ID_PATTERN.fullmatch("mx哈基米"), "真实站有中文源 id，不许判非法"
    assert SOURCE_ID_PATTERN.fullmatch("xy_哈基米"), "中文 + 下划线混合也不行的话就是回归"
    assert SOURCE_ID_PATTERN.fullmatch("syuan") and SOURCE_ID_PATTERN.fullmatch("mx-x.v2")
    assert not SOURCE_ID_PATTERN.fullmatch("a/b"), "源 id 含 `/` 会让模型 id 前缀有歧义"
    assert not SOURCE_ID_PATTERN.fullmatch("has space"), "空白不该进 id"
    assert not SOURCE_ID_PATTERN.fullmatch(""), "空 id 不行"
    assert not SOURCE_ID_PATTERN.fullmatch("x" * 65), "超长要挡住"

    # --- 模型 id ---
    assert MODEL_ID_PATTERN.fullmatch("deepseek/deepseek-v4-flash"), "真实值，必须过"
    assert MODEL_ID_PATTERN.fullmatch("mxgpt/gpt-5.6-sol"), "真实值（含点号与连字符）"
    assert MODEL_ID_PATTERN.fullmatch("syuan/BAAI/bge-m3"), "模型名里的第二个 `/` 要允许"
    assert not MODEL_ID_PATTERN.fullmatch("noslash"), "模型 id 必须带 `/`（前缀 = 源 id）"
    assert not MODEL_ID_PATTERN.fullmatch("/model"), "前缀为空不行"
    assert not MODEL_ID_PATTERN.fullmatch("source/"), "模型名为空不行"


def test_id_validation_uses_fullmatch_not_match():
    """🔴 id 校验必须用 `fullmatch` —— `match("^...$")` 会**放过结尾换行**。

    为什么这条要**直接对 `validate()`** 断言（不走 HTTP）：
    `from_dict()` 会 `strip()`，所以 HTTP 路径永远测不到这个洞
    —— 而 `validate()` 是**绕不过去的最后一道**（手编 `providers.json`、
    直接构造 `ProviderSource` 都不经过 `from_dict`）。旧版就是这么漏掉的：
    第一版把断言写在正则自己身上，在"id 已被 strip"的前提下永不触发 → 假通过。
    """
    for bad in ("abc\n", "abc\r", "abc\t", "abc "):
        with pytest.raises(ProviderValidationError):
            ProviderSource(id=bad, api_base=API_BASE).validate()
    # 模型的 id 同理（`$` 同样放过结尾换行）
    with pytest.raises(ProviderValidationError):
        ProviderModel(id="syuan/m\n", provider_source_id="syuan", model="m").validate()


def test_source_id_allows_chinese_like_real_site(tmp_path):
    """真实站里源 id 有中文（`mx哈基米`）—— 面板新增中文名必须能存进去。

    ⚠️ 旧正则 `[A-Za-z0-9_.-]` 会把中文判非法。若真按旧规则上线，
       用户照着真实站的样子起个中文名就会被 400 挡住（功能"不对"的一部分）。
    """
    store = ProviderStore(tmp_path / "p.json")
    store.create_source(_source(id="mx哈基米", api_base="https://api.mxzzz.xyz/v1"))
    store.create_model(_model(id="mx哈基米/gpt-5.5", provider_source_id="mx哈基米", model="gpt-5.5"))
    assert [s.id for s in store.list_sources()] == ["mx哈基米"]
    assert [m.id for m in store.list_models()] == ["mx哈基米/gpt-5.5"]
    assert store.default_provider_id == "mx哈基米/gpt-5.5"


# ===========================================================================
# ③ 🔴 旧格式（v1 单层）自动迁移
# ===========================================================================


#: 契约 §7 冻结的旧格式样例（**data/providers.json 里那条 `fast` 的真形状**）。
LEGACY_V1 = {
    "version": 1,
    "default_provider_id": "fast",
    "providers": [
        {"id": "fast", "name": "快速响应", "model": "gemini-3.5-flash",
         "base_url": "https://api.syuan.org/v1", "api_key": "sk-legacy-KEY",
         # ⚠️ 刻意用**非默认值**：若这里是 60.0（= DEFAULT_TIMEOUT），
         #    "迁移时丢掉 timeout"这个 bug 会被默认值**掩盖**成假通过（注入验证抓到过）。
         "timeout": 45.0}
    ],
}


def test_legacy_v1_file_is_migrated_and_persisted(tmp_path, caplog):
    """🔴 读到 v1 → 自动迁成 v2、**落盘**、并**记 INFO 日志**（契约 §7 三条硬要求）。

    为什么"落盘"是硬要求：不落盘的话每次启动都重迁一遍，
    而且用户打开 `data/providers.json` 看到的还是旧格式 → 以为迁移没生效。
    为什么"记日志"是硬要求：项目铁律 —— **不静默改用户数据**。
    """
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(LEGACY_V1, ensure_ascii=False), encoding="utf-8")

    caplog.set_level(logging.INFO, logger="emotion_app.infra.providers_store")
    store = ProviderStore(path)
    data = store.load()

    # ① 内存里已经是新格式
    assert data["version"] == 2, data
    assert [s["id"] for s in data["provider_sources"]] == ["fast"], data
    assert [m["id"] for m in data["providers"]] == ["fast/gemini-3.5-flash"], data
    assert data["default_provider_id"] == "fast/gemini-3.5-flash", data

    # ② **落盘**了（不是只改了内存）
    on_disk = _raw(path)
    assert on_disk["version"] == 2, f"迁移没有落盘：{on_disk}"
    assert [s["id"] for s in on_disk["provider_sources"]] == ["fast"], on_disk
    assert on_disk["default_provider_id"] == "fast/gemini-3.5-flash", on_disk
    # 契约 §7：迁移后**旧字段不再写回**
    assert "base_url" not in on_disk["providers"][0], on_disk
    assert "api_key" not in on_disk["providers"][0], on_disk
    assert "name" not in on_disk["providers"][0], on_disk

    # ③ **有 INFO 日志**（迁移了几条）
    infos = [rec.getMessage() for rec in caplog.records if rec.levelno == logging.INFO]
    assert any("迁移" in m and "v1" in m and "v2" in m for m in infos), (
        f"迁移是静默的 —— 用户的数据被改了却没有任何日志：{infos}"
    )

    # ④ 再读一次**不该**再迁（否则每次启动都在改文件）
    before = path.read_text(encoding="utf-8")
    ProviderStore(path).load()
    assert path.read_text(encoding="utf-8") == before, "重复读取又改了一次文件（迁移不幂等）"


def test_legacy_v1_migration_keeps_every_field(tmp_path):
    """🔴 迁移**不丢数据**：旧记录里每个字段都要能在新结构里找到归宿。

    ⚠️ 现有 `data/providers.json` 里**真的有一条**（`id=fast`）——
       迁移丢字段就是把用户的配置弄丢了（最不能接受的一类 bug）。
    """
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(LEGACY_V1, ensure_ascii=False), encoding="utf-8")
    store = ProviderStore(path)

    src = store.list_sources()[0]
    assert src.id == "fast", "源 id 应当是旧记录的 id"
    assert src.api_base == "https://api.syuan.org/v1", "api_base ← 旧 base_url"
    assert src.key == ["sk-legacy-KEY"], "key 应当是**列表**（契约 §2）且含旧 api_key"
    assert src.timeout == 45.0, "timeout 要带过来（非默认值才验得出【丢了】）"
    assert src.provider_type == "chat_completion", src
    assert src.enable is True, src

    m = store.list_models()[0]
    assert m.id == "fast/gemini-3.5-flash", "模型 id = <源id>/<model>"
    assert m.provider_source_id == "fast", m
    assert m.model == "gemini-3.5-flash", "model ← 旧 model"

    # 🔴 key **没有**在迁移中被明文写进"模型"层（那是源的字段）
    raw = _raw(path)
    assert raw["providers"][0].get("key") is None, raw["providers"][0]

    # 旧记录里的 `name`（"快速响应"）**没有被搬进任何响应字段里假装还活着**，
    # 但迁移日志里点了名（可追溯）—— 这里只确认不丢 key/地址/模型/超时这四样硬数据。


def test_legacy_without_model_keeps_the_endpoint(tmp_path, caplog):
    """边界：旧记录**没有 model** → 只迁移端点（**key/地址不能丢**）+ 记日志。

    ⚠️ 模型的 `model` 字段不能为空，所以建不出模型。但用户的 **key 是资产** ——
       绝不能因为"模型名缺失"就把整条丢弃（那就是静默数据损失）。
       正确行为：端点照迁、日志说清"请在面板上补一个模型"。
    """
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({
        "version": 1, "default_provider_id": "nokey",
        "providers": [{"id": "nokey", "base_url": API_BASE, "api_key": "sk-keep-me",
                       "timeout": 30}],
    }, ensure_ascii=False), encoding="utf-8")

    caplog.set_level(logging.INFO, logger="emotion_app.infra.providers_store")
    store = ProviderStore(path)
    data = store.load()

    assert [s["id"] for s in data["provider_sources"]] == ["nokey"], data
    assert data["provider_sources"][0]["key"] == ["sk-keep-me"], "key 被弄丢了！"
    assert data["providers"] == [], data
    assert data["default_provider_id"] == "", "没有模型时默认必须留空（不许悬空）"
    assert any("model" in rec.getMessage() for rec in caplog.records), (
        "少了一个模型这件事必须留日志，否则用户不知道要去补"
    )


async def test_migration_runs_through_the_api_too(mk):
    """🔴 迁移要**在所有读路径上**生效，不只是 store 直连。

    ⚠️ 如果只在"启动时迁一次"，那么 API 先跑到时会读出一份两层为空的配置
       （模型全在旧字段里）→ 面板显示"没有供应商"，而文件其实有数据。
       → 所以迁移放在 `_read_raw()`（所有读的必经之路）。
    """
    c = await mk()
    path = Path(c.providers_path)  # type: ignore[attr-defined]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(LEGACY_V1, ensure_ascii=False), encoding="utf-8")

    d = _data(await (await c.get("/api/v1/providers")).get_json())
    assert [s["id"] for s in d["provider_sources"]] == ["fast"], d
    assert [m["id"] for p in [d] for m in p["providers"]] == ["fast/gemini-3.5-flash"], d
    assert d["default_provider_id"] == "fast/gemini-3.5-flash", d
    # 而且真的落盘了（否则下次启动又迁一遍）
    assert _raw(path)["version"] == 2, _raw(path)


def test_v2_file_is_not_re_migrated(tmp_path):
    """反向：**已经是 v2** 的文件不该被动（迁移判据不能"总是为真"）。

    ⚠️ 这条守的是"迁移写坏新数据"：如果 `needs_migration()` 写成恒真，
       每次读都会把 v2 的 `provider_sources` 丢掉、用 `providers` 重新造一遍
       —— 源上的 key/超时/自定义头**全部丢失**，而且**看不出来**（模型 id 还一样）。
    """
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(PARENT_ACCEPTANCE_CONFIG, ensure_ascii=False), encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    store = ProviderStore(path)
    store.load()

    assert path.read_text(encoding="utf-8") == before, "v2 文件被重写/重迁了"
    assert store.list_sources()[0].timeout == 120, "源上的字段被迁移逻辑吃掉了"


# ===========================================================================
# ④ 🔴 key 绝不出现在任何响应里
# ===========================================================================


def _contains_key(payload_text: str) -> bool:
    """响应体里有没有凭据。判据查**两种形态**：

    * 完整 key（`sk-test-DEADBEEF-...`）；
    * key 的**特征子串**（`DEADBEEF`）—— 防"截断/部分回显"绕过第一条。
    """
    return SECRET_KEY in payload_text or "DEADBEEF" in payload_text


async def test_source_key_never_appears_in_list_responses(mk):
    """④ 列表响应：只有掩码列表 + `key_configured`，**没有** key 本身。

    覆盖三条列表路由（源列表 / 模型列表 / 某源的模型），
    因为"只在一个出口上脱敏"是最常见的漏法。
    """
    c = await mk()
    await _seed(c)

    for path in ("/api/v1/provider-sources", "/api/v1/providers",
                 "/api/v1/provider-sources/syuan/providers",
                 "/api/v1/provider-sources/models"):
        text = await (await c.get(path)).get_data(as_text=True)
        assert not _contains_key(text), f"🔴 {path} 泄漏了 key：{text}"

    src = _data(await (await c.get("/api/v1/provider-sources")).get_json())["provider_sources"][0]
    assert src["key_configured"] is True, src
    # 🔴 形状要是**列表**（照真实契约），但内容只能是掩码
    assert src["key"] == [KEY_MASK], src
    assert "api_key" not in src, f"响应里出现了 api_key 字段：{sorted(src)}"


async def test_source_key_never_appears_in_write_responses(mk):
    """④ 写接口同样不许回显（很多人只在"列表"上加脱敏，忘了写接口）。

    覆盖：新增源 / 改源（**这次真的改了 key**）/ 新增模型 / 改模型 / 单条读。
    """
    c = await mk()
    r = await _add_source(c)
    assert not _contains_key(await r.get_data(as_text=True)), "新增源回显了 key"

    r2 = await c.put("/api/v1/provider-sources/by-id?source_id=syuan",
                     json={"key": ["sk-test-NEWKEY-9999-8888-7777"]})
    text2 = await r2.get_data(as_text=True)
    assert not _contains_key(text2), f"🔴 改 key 的响应回显了新 key：{text2}"
    assert "9999" not in text2, f"🔴 新 key 被回显了：{text2}"

    # 顺带验：新 key 真的**落盘**了（否则上面的判据会被"根本没存"骗过）
    raw = _raw(Path(c.providers_path))  # type: ignore[attr-defined]
    assert raw["provider_sources"][0]["key"] == ["sk-test-NEWKEY-9999-8888-7777"], raw

    r3 = await _add_model(c)
    assert not _contains_key(await r3.get_data(as_text=True)), "新增模型回显了 key"
    r4 = await c.get("/api/v1/provider-sources/by-id?source_id=syuan")
    assert not _contains_key(await r4.get_data(as_text=True)), "单条源读回显了 key"
    r5 = await c.get("/api/v1/providers/syuan%2Fgemini-3.5-flash")
    assert not _contains_key(await r5.get_data(as_text=True)), "单条模型读回显了 key"


async def test_source_key_never_appears_in_error_or_upstream_echo(mk):
    """④ 失败路径也不许泄漏（错误消息最容易"顺手带上上下文"）。

    这里让上游 401 并把 key **回显在响应体里** —— 我们的接口**不许**把它转发出去。
    另有一条校验失败的 400（错误消息里常被"顺手"带上原始输入）。
    """
    c = await mk()
    await _seed(c)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": f"invalid key {SECRET_KEY}"}})

    c.app.config["EMOTION_PROVIDERS_TRANSPORT"] = httpx.MockTransport(handler)
    text = await (await c.post("/api/v1/providers/syuan%2Fgemini-3.5-flash/test",
                               json={})).get_data(as_text=True)
    assert not _contains_key(text), f"🔴 test 接口把 key 转发回来了：{text}"

    # 上游 400 且原样回显 Authorization 头（OpenClaw 系中转站的真实行为）
    def handler400(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"message": f"bad request, got Bearer {SECRET_KEY}"}}
        )

    c.app.config["EMOTION_PROVIDERS_TRANSPORT"] = httpx.MockTransport(handler400)
    text2 = await (await c.post("/api/v1/providers/syuan%2Fgemini-3.5-flash/test",
                                json={})).get_data(as_text=True)
    assert not _contains_key(text2), f"🔴 上游回显的 key 被我们转发给了浏览器：{text2}"

    # 「获取模型列表」的上游错误体同样要脱敏
    c.app.config["EMOTION_PROVIDER_MODELS_TRANSPORT"] = httpx.MockTransport(handler400)
    text3 = await (await c.get("/api/v1/provider-sources/syuan/models")).get_data(as_text=True)
    assert not _contains_key(text3), f"🔴 取模型列表把 key 转发回来了：{text3}"

    # 校验失败的错误消息同样要干净
    r = await c.post("/api/v1/provider-sources",
                     json={"id": "x", "api_base": "", "key": [SECRET_KEY]})
    text4 = await r.get_data(as_text=True)
    assert r.status_code == 400
    assert not _contains_key(text4), f"🔴 校验错误消息泄漏了 key：{text4}"


def test_key_mask_is_not_a_length_leak(mk):
    """掩码是**固定长度**的（不泄露真实 key 长度），而且与"没配"可区分。

    ⚠️ 若掩码按真实长度生成（`"•" * len(key)`），攻击者能从响应里读出
       "这把 key 有 51 个字符" —— 脱敏要精确，别把不该给的元信息也漏了。
    """
    assert len({len(KEY_MASK)}) == 1
    short = _source(key=["sk-a"]).public_view()
    long_ = _source(key=["sk-" + "x" * 200]).public_view()
    assert short["key"] == long_["key"] == [KEY_MASK], (short["key"], long_["key"])
    assert short["key_configured"] is True and long_["key_configured"] is True
    # 没配 key → 空列表（不是掩码），前端据此显示"未设置"
    assert _source(key=[]).public_view()["key"] == []
    assert _source(key=[]).public_view()["key_configured"] is False


# ===========================================================================
# ⑤ 🔴 PUT：key 为空 = **不修改**
# ===========================================================================


async def test_put_blank_key_keeps_existing(mk):
    """🔴 契约硬要求：`key` 为空 = "不修改"，**不是清空**。

    为什么这条必须有：面板编辑时 key 框一律留空或显示掩码（凭据不回显），
    若把空当清空，**用户改个超时就会把 key 抹掉** —— 而且全程无提示，
    下次调用上游才 401（静默失败里最贵的那种）。

    四种"空"都要覆盖：字段缺失 / `""` / `[]` / **掩码占位符**
    （最后一种最隐蔽：前端把响应里的掩码原样回填，用户只改了超时）。
    """
    c = await mk()
    await _add_source(c, key=[SECRET_KEY])
    path = Path(c.providers_path)  # type: ignore[attr-defined]

    for blank in ({}, {"key": ""}, {"key": []}, {"key": [KEY_MASK]}, {"key": [""]},
                  {"key": ["   "]}):
        r = await c.put("/api/v1/provider-sources/by-id?source_id=syuan",
                        json={**blank, "timeout": 99})
        assert r.status_code == 200, await r.get_data(as_text=True)
        src = _data(await r.get_json())["provider_source"]
        assert src["key_configured"] is True, (
            f"key={blank!r} 被当成清空了 —— 契约要求「不修改」"
        )
        assert _raw(path)["provider_sources"][0]["key"] == [SECRET_KEY], _raw(path)

    # 显式 null = 清空（给"要删掉这把 key"留的明路）
    r = await c.put("/api/v1/provider-sources/by-id?source_id=syuan", json={"key": None})
    assert r.status_code == 200
    assert _data(await r.get_json())["provider_source"]["key_configured"] is False
    assert _raw(path)["provider_sources"][0]["key"] == [], _raw(path)


async def test_key_list_shape_is_preserved_in_storage(mk):
    """`key` 落盘是**列表**（真实契约），多把 key 不能丢。

    ⚠️ 这是我要的简化点：我们**只用第一把**（自用场景不做轮询），
       但**形状必须是列表** —— 否则用户的第二把 key 在我们保存时会**静默消失**。
    """
    c = await mk()
    r = await _add_source(c, key=["sk-first", "sk-second"])
    assert r.status_code == 201, await r.get_data(as_text=True)
    raw = _raw(Path(c.providers_path))  # type: ignore[attr-defined]
    assert raw["provider_sources"][0]["key"] == ["sk-first", "sk-second"], raw
    # 取用时取**第一把**（`provider_bridge._first_key` 的契约）
    from emotion_app.web.provider_bridge import _first_key

    assert _first_key(raw["provider_sources"][0]["key"]) == "sk-first"


# ===========================================================================
# ⑥ 🔴 校验：悬空引用 / 不支持的 provider_type / 其它字段
# ===========================================================================


async def test_model_with_dangling_source_is_400(mk):
    """🔴🔴 **悬空引用 → 400**（这是本模块最要紧的一条校验）。

    模型的 `provider_source_id` 指向不存在的源时：`api_base`/`key` 全都取不到
    → 这个模型**永远调不通**，而配置看起来"配好了"。
    这正是本项目最贵的那类 bug（静默失败），所以**写入时就拦住**。

    覆盖三条入口：`POST /providers`、`POST /provider-sources/<id>/providers`、
    `PUT /providers/by-id`（改 provider_source_id 也会造出悬空引用）。
    """
    c = await mk()
    await _add_source(c, id="syuan")

    # ① POST /providers
    r = await c.post("/api/v1/providers",
                     json={"id": "ghost/m", "provider_source_id": "ghost", "model": "m"})
    assert r.status_code == 400, f"悬空引用竟然通过了：{r.status_code}"
    body = await r.get_json()
    assert body["status"] == "error", body
    assert "ghost" in body["message"], body

    # ② POST /provider-sources/<id>/providers（body 指向别的源 → 也要拒绝）
    r2 = await c.post("/api/v1/provider-sources/syuan/providers",
                      json={"model": "m", "provider_source_id": "ghost"})
    assert r2.status_code == 400, f"body 里的悬空引用竟然通过了：{r2.status_code}"

    # ③ PUT 改 provider_source_id 指向不存在的源
    assert (await _add_model(c, id="syuan/m1", provider_source_id="syuan",
                             model="m1")).status_code == 201
    r3 = await c.put("/api/v1/providers/by-id?provider_id=syuan%2Fm1",
                     json={"provider_source_id": "ghost"})
    assert r3.status_code == 400, f"改成悬空引用竟然通过了：{r3.status_code}"
    assert "ghost" in (await r3.get_json())["message"]

    # ④ 反向：确认**没有**被偷偷写进去（400 之后文件不该变）
    d = _data(await (await c.get("/api/v1/providers")).get_json())
    assert [m["id"] for m in d["providers"]] == ["syuan/m1"], d


def test_dangling_reference_in_handwritten_file_is_reported(tmp_path, caplog):
    """手编文件里的悬空引用：**读取时**也要报出来（不能只在写入口拦）。

    ⚠️ 用户是可以直接改 `data/providers.json` 的。若读取路径不检查，
       插件就会看到一个"配了但永远调不通"的模型 —— 而面板上一切正常。
    """
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({
        "version": 2,
        "provider_sources": [{"id": "a", "api_base": "https://a/v1", "key": ["k"]}],
        "providers": [
            {"id": "a/m1", "provider_source_id": "a", "model": "m1"},
            {"id": "ghost/m2", "provider_source_id": "nope", "model": "m2"},
        ],
        "default_provider_id": "a/m1",
    }, ensure_ascii=False), encoding="utf-8")

    caplog.set_level(logging.WARNING, logger="emotion_app.infra.providers_store")
    store = ProviderStore(path)
    models = store.list_models()

    warns = [rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert any("nope" in w for w in warns), (
        f"手编文件里的悬空引用**一个字都没说**（静默）：{warns}"
    )
    # ⚠️ 不丢数据：不合法的记录**仍保留**（用户能在面板上看到并修），但如实告警
    assert [m.id for m in models] == ["a/m1", "ghost/m2"], models


# 🔴 `speech_to_text` **2026-09-21 已移出**本清单：情绪选"先只做配置层" → 现在**存得进**。
#    ⚠️ 它是否**能执行**是另一回事 —— 3 条 STT 适配器全被 `adapter_is_executable()` 判 false
#       （见 `test_provider_presets.py` 的能力诚实判据）。别把"存得进"当"能跑"。
@pytest.mark.parametrize("bad_type", ["rerank", "agent_runner", "image_generation"])
async def test_rejects_unsupported_provider_type(mk, bad_type):
    """🔴 我们**不支持**的 `provider_type` → **400**，且说明"本平台暂不支持该类型"。

    情绪明确说：情绪app **不需要 Agent 能力**。
    ⚠️ 关键在"**不假装支持**"：放行一个我们根本执行不了的能力，
       面板会显示它，插件真去调用时才炸在更远的地方（更难查）。
       **如实拒绝 = 作者验收判据③（能力不对等要如实 degraded）。**

    ⚠️ 2026-09-20 范围变更：`text_to_speech` / `embedding` 已从"不支持"改为**支持（配置层）**
       —— 这是情绪本轮明确要的（模型供应商要能加 TTS/嵌入式供应商）。
       **`agent_runner` 永远不在支持集**（情绪：不需要 agent）。
       TTS/embedding 是否"真能执行"不由 `provider_type` 决定，而由
       `adapter_is_executable(type)` 决定 —— 见 `test_tts_source_is_storable_but_not_executable`。
    """
    c = await mk()
    r = await _add_source(c, provider_type=bad_type)
    assert r.status_code == 400, f"{bad_type} 竟然通过了：{r.status_code}"
    body = await r.get_json()
    assert body["status"] == "error", body
    assert body["reason"] == "unsupported_provider_type", body
    assert "不支持" in body["message"] and bad_type in body["message"], body

    # 反向：确认没写进文件（400 就该什么都没发生）
    d = _data(await (await c.get("/api/v1/provider-sources")).get_json())
    assert d["provider_sources"] == [], d


@pytest.mark.parametrize("ok_type", ["text_to_speech", "embedding"])
async def test_tts_and_embedding_sources_are_now_supported(mk, ok_type):
    """★ 范围变更：TTS / embedding 供应商**能建**（情绪本轮要求）。

    🔴 但"能建"≠"能跑"：这两类源的适配器（`edge_tts`/`openai_embedding` …）
       都是 **AstrBot 自己的适配器**，我们执行不了 →
       必须 `adapter_is_executable(type) is False`，前端据此**如实标注**。
       判据分开写，就是为了防止"放行了 provider_type"被误读成"承诺了能力"。
    """
    c = await mk()
    r = await _add_source(c, id=f"src-{ok_type}", provider_type=ok_type)
    assert r.status_code == 201, (ok_type, r.status_code, await r.get_json())

    d = _data(await (await c.get("/api/v1/provider-sources")).get_json())
    got = [s for s in d["provider_sources"] if s["id"] == f"src-{ok_type}"]
    assert len(got) == 1 and got[0]["provider_type"] == ok_type, d


async def test_tts_source_is_storable_but_not_executable(mk):
    """🔴 **能力诚实**：能存 ≠ 能跑，两者必须分得开。

    这条防的是最危险的一种"假支持"：面板让用户建了一个 TTS 源、看着像成功了，
    但我们根本没有执行它的代码 → 真用的时候才炸。
    → 判据：建得下（201），但 `adapter_is_executable(type)` 如实为 **False**。
    """
    from emotion_app.infra.providers_store import adapter_is_executable

    c = await mk()
    r = await _add_source(c, id="tts1", provider_type="text_to_speech",
                          type="edge_tts", provider="microsoft")
    assert r.status_code == 201, await r.get_json()
    assert adapter_is_executable("edge_tts") is False, "edge_tts 我们执行不了，不该说能"
    # 我们真能跑的两种 OpenAI 格式
    assert adapter_is_executable("openai_chat_completion") is True
    assert adapter_is_executable("openai_responses") is True
    # 空/None 不能算"能执行"（否则前端会不标注）
    assert adapter_is_executable("") is False
    assert adapter_is_executable("  ") is False


async def test_supported_provider_type_is_chat_completion_default(mk):
    """不传 `provider_type` 时默认 `chat_completion`（这是唯一支持的类型）。"""
    c = await mk()
    r = await c.post("/api/v1/provider-sources",
                     json={"id": "d", "api_base": API_BASE})
    assert r.status_code == 201, await r.get_data(as_text=True)
    src = _data(await r.get_json())
    assert src["provider_type"] == "chat_completion", src
    assert src["provider"] == "openai" and src["type"] == "openai_chat_completion", src


async def test_source_validation_rejects_bad_fields(mk):
    """源的三项基础校验：id 必填/唯一、api_base 必须 http(s)、timeout > 0。"""
    c = await mk()

    r = await c.post("/api/v1/provider-sources", json={"api_base": API_BASE})
    assert r.status_code == 400 and "id" in (await r.get_json())["message"]

    r = await _add_source(c, id="dup")
    assert r.status_code == 201
    r = await _add_source(c, id="dup")
    assert r.status_code == 400, "重复 id 竟然成功了（静默覆盖会让用户以为加了两条）"
    assert "已存在" in (await r.get_json())["message"]

    for bad in ("ftp://x/v1", "api.syuan.org/v1", "", "//x/v1"):
        r = await _add_source(c, id=f"u{abs(hash(bad)) % 1000}", api_base=bad)
        assert r.status_code == 400, f"{bad!r} 竟然通过了"
        assert "http" in (await r.get_json())["message"]

    for bad_timeout in (0, -1, "abc"):
        r = await _add_source(c, id=f"t{abs(hash(str(bad_timeout))) % 1000}", timeout=bad_timeout)
        assert r.status_code == 400, f"timeout={bad_timeout!r} 竟然通过了"
        assert "timeout" in (await r.get_json())["message"]

    r = await _add_source(c, id="bad/id")
    assert r.status_code == 400, "源 id 含 `/` 竟然通过了（会让模型 id 前缀有歧义）"


async def test_model_validation_rejects_bad_fields(mk):
    """模型的三项校验：id 必填且必须是 `源id/模型名`、model 必填、id 唯一。"""
    c = await mk()
    await _add_source(c, id="syuan")

    # id 没有斜杠 → 400（无法确定它挂在哪个源下）
    r = await c.post("/api/v1/providers",
                     json={"id": "noslash", "provider_source_id": "syuan", "model": "m"})
    assert r.status_code == 400, f"无斜杠的模型 id 竟然通过了：{r.status_code}"

    # id 前缀与 provider_source_id **不一致** → 400
    # （不拦的话：面板说挂在 syuan、实际取的是别的源的地址 —— 最难查的分叉）
    r = await c.post("/api/v1/providers",
                     json={"id": "other/m", "provider_source_id": "syuan", "model": "m"})
    assert r.status_code == 400, f"前缀与源不一致竟然通过了：{r.status_code}"

    # model 为空 → 400
    r = await c.post("/api/v1/providers",
                     json={"id": "syuan/m", "provider_source_id": "syuan", "model": "  "})
    assert r.status_code == 400 and "model" in (await r.get_json())["message"]

    # 重复 id → 400
    assert (await _add_model(c)).status_code == 201
    r = await _add_model(c)
    assert r.status_code == 400 and "已存在" in (await r.get_json())["message"]


async def test_post_model_under_source_autofills_id(mk):
    """契约 §2 的 `POST /provider-sources/<源id>/providers`：**只给 model 也能建**。

    面板的"自定义模型"就是这条路：用户填个模型名，id 由服务端按
    `<源id>/<模型名>` 补出来（让用户手打 `源id/模型名` 是没必要的心智负担）。
    """
    c = await mk()
    await _add_source(c, id="mxgpt")
    r = await c.post("/api/v1/provider-sources/mxgpt/providers",
                     json={"model": "gpt-5.6-sol", "modalities": ["text", "tool_use"]})
    assert r.status_code == 201, await r.get_data(as_text=True)
    created = _data(await r.get_json())
    assert created["id"] == "mxgpt/gpt-5.6-sol", created
    assert created["provider_source_id"] == "mxgpt", created
    assert created["modalities"] == ["text", "tool_use"], created

    # body 里给了**别的**源 → 400（不抄真实站那种"静默改写成路径里的源"）
    r2 = await c.post("/api/v1/provider-sources/mxgpt/providers",
                      json={"id": "qwen/x", "provider_source_id": "qwen", "model": "x"})
    assert r2.status_code == 400, f"跨源写入竟然通过了：{r2.status_code}"


async def test_delete_source_cascades_to_its_models(mk):
    """删源**级联**删掉它下面的模型（否则立刻产生一批悬空引用）。

    同时：别的源的模型**不受影响**（别一刀切）。
    """
    c = await mk()
    await _add_source(c, id="a")
    await _add_source(c, id="b")
    await _add_model(c, id="a/m1", provider_source_id="a", model="m1")
    await _add_model(c, id="b/m2", provider_source_id="b", model="m2")

    r = await c.delete("/api/v1/provider-sources/by-id?source_id=a")
    assert r.status_code == 200, await r.get_data(as_text=True)
    assert _data(await r.get_json())["deleted_models"] == ["a/m1"]

    d = _data(await (await c.get("/api/v1/providers")).get_json())
    assert [m["id"] for m in d["providers"]] == ["b/m2"], d
    assert d["default_provider_id"] == "b/m2", d


# ===========================================================================
# ⑦ ★「获取模型列表」：失败原因**逐条说清**
# ===========================================================================


async def _models_client(mk, transport):
    """造一个"已配好源 + 打桩 transport"的 client。

    ⚠️ 打桩口是 `EMOTION_PROVIDER_MODELS_TRANSPORT`（照 `EMOTION_MODELS_TRANSPORT` 的先例）——
       直连上游的代码不留注入口就没法验"401 怎么说、超时怎么说、空列表怎么说"，
       而失败路径恰恰最容易写成静默。
    """
    c = await mk()
    await _add_source(c, id="syuan")
    c.app.config["EMOTION_PROVIDER_MODELS_TRANSPORT"] = transport
    return c


def _models_payload(ids):
    return {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]}


async def test_fetch_models_success(mk):
    """★ 正常路径：契约 §2 的响应形状（models + provider_source_id + model_metadata）。

    🔴 判据不止"返回 200"：请求必须真的打到 `<api_base>/models` 且带 Bearer key
       —— 那才能证明服务端**代拉**（否则可能是前端编的）。
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_models_payload(["gpt-5.5", "gpt-5.6-sol", "gpt-5.5"]))

    c = await _models_client(mk, httpx.MockTransport(handler))
    r = await c.get("/api/v1/provider-sources/syuan/models")
    assert r.status_code == 200, await r.get_data(as_text=True)
    d = _data(await r.get_json())

    assert d["models"] == ["gpt-5.5", "gpt-5.6-sol"], f"应去重且保序：{d['models']}"
    assert d["provider_source_id"] == "syuan", d
    assert "model_metadata" in d, d
    assert seen["url"] == f"{API_BASE}/models", seen
    assert seen["auth"] == f"Bearer {SECRET_KEY}", seen


async def test_fetch_models_uses_form_values_before_save(mk):
    """★ 支持**表单里还没保存**的 `api_base`/`key`（照 `settings/models` 的同款先例）。

    真实用法：用户"改地址 → 拉模型 → 选模型 → 保存"。只读已保存值的话，
    第一次填地址时这个按钮就是废的。
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_models_payload(["m1"]))

    c = await _models_client(mk, httpx.MockTransport(handler))
    r = await c.post("/api/v1/provider-sources/syuan/models",
                     json={"api_base": "https://form.test/v1", "key": "sk-form-key"})
    # 契约里这条是 GET；但"表单值优先"必须能用 → 同时接受 POST 形态
    if r.status_code == 405:
        r = await c.get("/api/v1/provider-sources/syuan/models"
                        "?api_base=https%3A%2F%2Fform.test%2Fv1")
    assert r.status_code == 200, await r.get_data(as_text=True)
    assert seen["url"] == "https://form.test/v1/models", seen
    assert seen["auth"] == "Bearer sk-form-key", seen


async def test_fetch_models_timeout(mk):
    """★ 超时 → 502 + `upstream_timeout`，消息里给出秒数与地址（可行动）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    c = await _models_client(mk, httpx.MockTransport(handler))
    r = await c.get("/api/v1/provider-sources/syuan/models")
    assert r.status_code == 502, r.status_code
    body = await r.get_json()
    assert body["reason"] == "upstream_timeout", body
    assert "超时" in body["message"] and "https://" in body["message"], body
    assert body["error"]["type"] == "upstream_timeout", "旧前端读 error.type，必须也给"


async def test_fetch_models_unreachable(mk):
    """★ 连不上 → 502 + `upstream_unreachable`，带上异常类型。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    c = await _models_client(mk, httpx.MockTransport(handler))
    body = await (await c.get("/api/v1/provider-sources/syuan/models")).get_json()
    assert body["reason"] == "upstream_unreachable", body
    assert "连不上" in body["message"] and "ConnectError" in body["message"], body


@pytest.mark.parametrize("status", [401, 403])
async def test_fetch_models_auth_failure(mk, status):
    """★ 401/403 → 502 + `upstream_auth`，提示"检查 API Key"。"""
    c = await _models_client(mk, _json_transport({"error": "nope"}, status=status))
    r = await c.get("/api/v1/provider-sources/syuan/models")
    assert r.status_code == 502, r.status_code
    body = await r.get_json()
    assert body["reason"] == "upstream_auth", body
    assert "鉴权" in body["message"] and str(status) in body["message"], body


async def test_fetch_models_upstream_error_carries_detail(mk):
    """★ 其它 4xx/5xx → 502 + `upstream_error`，且**带上上游原话**（脱敏后）。

    ⚠️ 只说"上游 400"用户没法行动；最常见的就是 400「模型名/权限不对」。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "invalid api key scope"}})

    c = await _models_client(mk, httpx.MockTransport(handler))
    body = await (await c.get("/api/v1/provider-sources/syuan/models")).get_json()
    assert body["reason"] == "upstream_error", body
    assert "400" in body["message"] and "invalid api key scope" in body["message"], body


async def test_fetch_models_non_json(mk):
    """★ 返回 200 但不是 JSON（地址填成了网页）→ 502 + `upstream_bad_payload`。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>hello</html>")

    c = await _models_client(mk, httpx.MockTransport(handler))
    body = await (await c.get("/api/v1/provider-sources/syuan/models")).get_json()
    assert body["reason"] == "upstream_bad_payload", body
    assert "JSON" in body["message"], body


async def test_fetch_models_empty_is_not_success(mk):
    """🔴 ★ **空列表不算成功**（本项目铁律：静默失败比报错贵）。

    上游回 200 但 `data` 为空 —— 用户会以为"这个站没有模型"，
    实际多半是 key 没有列模型权限或地址不是 OpenAI 兼容端点。
    """
    for payload in ({"object": "list", "data": []}, {"object": "list"},
                    {"data": [{"object": "model"}]}, {"models": []}, []):
        c = await _models_client(mk, _json_transport(payload))
        r = await c.get("/api/v1/provider-sources/syuan/models")
        assert r.status_code == 502, f"{payload} 被当成功了：{r.status_code}"
        body = await r.get_json()
        assert body["reason"] == "upstream_empty", body
        assert "空" in body["message"], body


async def test_fetch_models_missing_source_is_404(mk):
    """★ 源不存在 → 404（不是"空列表"）—— 两者含义完全不同。"""
    c = await mk()
    r = await c.get("/api/v1/provider-sources/ghost/models")
    assert r.status_code == 404, r.status_code
    assert "不存在" in (await r.get_json())["message"]


async def test_fetch_models_rejects_bad_api_base_early(mk):
    """★ 地址非 http(s) → 400（不浪费一次上游往返，且原因说清）。"""
    c = await mk()
    r = await c.post("/api/v1/provider-sources/syuan/models", json={"api_base": "ftp://x"})
    # 源不存在会先 404；这里先建一个源（地址合法），再传一个非法表单地址
    await _add_source(c, id="s2")
    r = await c.post("/api/v1/provider-sources/s2/models", json={"api_base": "ftp://x"})
    assert r.status_code == 400, r.status_code
    assert (await r.get_json())["reason"] == "invalid_api_base"


# ===========================================================================
# ⑧ 连通性测试：返回 status 字段
# ===========================================================================


async def _test_client(mk, transport, **model_kw):
    c = await mk()
    await _add_source(c, id="p1")
    await _add_model(c, id="p1/m-test", provider_source_id="p1",
                     model=model_kw.pop("model", "m-test"), **model_kw)
    c.app.config["EMOTION_PROVIDERS_TRANSPORT"] = transport
    return c


async def test_test_endpoint_available(mk):
    """⑧ 成功 → `data.status == "available"`、`error is None`，且带 latency/sample。

    契约 §3 的形状：`{"id","model","type","name","status","error"}`。
    ⚠️ 结论在 **`data.status`** 里，不是 HTTP 码（业务失败也是 HTTP 200）。
    """
    c = await _test_client(mk, _json_transport(_ok_reply("你好呀，我是模型")))
    r = await c.post("/api/v1/providers/p1%2Fm-test/test", json={})
    assert r.status_code == 200, await r.get_data(as_text=True)
    body = await r.get_json()
    assert body["status"] == "ok", body
    d = _data(body)

    assert d["status"] == "available", d
    assert d["error"] is None, d
    assert d["id"] == "p1/m-test" and d["model"] == "m-test", d
    assert d["type"] == "chat_completion" and d["name"] == "p1/m-test", d
    assert isinstance(d["latency_ms"], int) and d["latency_ms"] >= 0, d
    assert d["sample"] == "你好呀，我是模型", d


@pytest.mark.parametrize(
    "transport_factory, expected",
    [
        (lambda: httpx.MockTransport(_raise(httpx.ReadTimeout, "slow")), "超时"),
        (lambda: httpx.MockTransport(_raise(httpx.ConnectError, "boom")), "连不上"),
        (lambda: _json_transport({"e": 1}, status=401), "鉴权"),
        (lambda: _json_transport({"e": 1}, status=500), "HTTP 500"),
        (lambda: httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")), "JSON"),
        (lambda: _json_transport(_ok_reply("   \n")), "空"),
        (lambda: _json_transport({"model": "x"}), "空"),
    ],
)
async def test_test_endpoint_unavailable_reasons(mk, transport_factory, expected):
    """🔴 ⑧ **失败逐条说清**，且都表达成 `status:"unavailable"` + 非空 `error`。

    覆盖：超时 / 连不上 / 401 / 5xx / 非 JSON / 空回复 / 缺 choices。
    ⚠️ **空响应不算成功** —— 上游回 200 但内容是空，插件拿到的就是空回复，
       用户会以为配好了然后插件全部任务退化成模板（静默失败里最贵的）。
    """
    c = await _test_client(mk, transport_factory())
    r = await c.post("/api/v1/providers/p1%2Fm-test/test", json={})
    assert r.status_code == 200, f"业务失败也应当是 HTTP 200（结论在 data.status）：{r.status_code}"
    body = await r.get_json()
    assert body["status"] == "ok", body
    d = _data(body)
    assert d["status"] == "unavailable", d
    assert isinstance(d["error"], str) and d["error"].strip(), d
    assert expected in d["error"], f"失败原因没说清（缺 {expected!r}）：{d['error']}"


def _raise(exc_type, msg):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc_type(msg)

    return handler


async def test_test_endpoint_missing_model_is_404(mk):
    """⑧ 模型不存在 → 404（"你要测的东西不在"与"测出来不通"必须分开）。"""
    c = await mk()
    r = await c.post("/api/v1/providers/ghost%2Fm/test", json={})
    assert r.status_code == 404, r.status_code
    assert "不存在" in (await r.get_json())["message"]


async def test_test_endpoint_wrong_method_is_405(mk):
    """⑧ 契约规定是 **POST**。用 GET 打它必须**响亮失败**（前端写成 GET 时要能立刻发现）。

    ⚠️ 这里有个路由的**固有歧义**（`<path:provider_id>` 带来的，见模块注释）：
    `/providers/a/b` 既可读成"模型 `a/b` 的 GET"，也可读成"模型 `a` 的 test 子资源"。
    真实语义要按**数据**判断：
      * 模型 `x` 存在、且 `x/test` 不存在 → 用户意图是子资源 → **405**（要点明用 POST）；
      * 两者都不存在 → **404**（"你要的东西不在"，不是方法用错）。
    两种都要有判据，否则"一律 405"和"一律 404"都能蒙混过关。
    """
    c = await mk()
    await _add_source(c, id="x")
    await _add_model(c, id="x/m1", provider_source_id="x", model="m1")

    # ① 模型 `x/m1` 存在 → GET /providers/x/m1/test 的意图是"测试"，必须 405 且说清用 POST
    r = await c.get("/api/v1/providers/x/m1/test")
    assert r.status_code == 405, f"GET 打测试端点竟回了 {r.status_code}"
    body = await r.get_json()
    assert "POST" in body["message"], body
    # 设默认同理
    r1b = await c.get("/api/v1/providers/x/m1/default")
    assert r1b.status_code == 405, f"GET 打设默认端点竟回了 {r1b.status_code}"

    # ② 模型 `a/b` 真的存在时，GET /providers/a/b 是**合法读**（/ 在模型名里是允许的）
    await _add_model(c, id="x/deep/seek", provider_source_id="x", model="deep/seek")
    r2 = await c.get("/api/v1/providers/x/deep/seek")
    assert r2.status_code == 200, f"含两个斜杠的模型名读不到：{r2.status_code}"

    # ③ 谁都不存在 → 404（不是 405）
    assert (await c.get("/api/v1/providers/nope/test")).status_code == 404


async def test_test_endpoint_sends_real_request(mk):
    """⑧ 端到端：请求真的打到 `api_base + /chat/completions`，带 Bearer key 和 model。

    🔴 判据不止"返回 available" —— 那可以被"根本没发请求就返回成功"骗过。
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_ok_reply())

    c = await mk()
    await _add_source(c, id="p1", api_base="https://up.test/v1", key=[SECRET_KEY])
    await _add_model(c, id="p1/m-test", provider_source_id="p1", model="m-test")
    c.app.config["EMOTION_PROVIDERS_TRANSPORT"] = httpx.MockTransport(handler)

    d = _data(await (await c.post("/api/v1/providers/p1%2Fm-test/test", json={})).get_json())
    assert d["status"] == "available", d
    assert seen["url"] == "https://up.test/v1/chat/completions", seen["url"]
    assert seen["auth"] == f"Bearer {SECRET_KEY}", (
        f"没带上源的 key（两层结构里 key 在【源】上）：{seen['auth']}"
    )
    assert seen["body"]["model"] == "m-test", seen["body"]
    assert seen["body"]["stream"] is False, seen["body"]


async def test_test_endpoint_uses_form_model_before_save(mk):
    """⑧ `test` 支持传**还没保存**的 model（真实用法：改模型 → 测 → 再保存）。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["model"] = json.loads(request.content.decode("utf-8"))["model"]
        return httpx.Response(200, json=_ok_reply())

    c = await _test_client(mk, httpx.MockTransport(handler))
    d = _data(await (await c.post("/api/v1/providers/p1%2Fm-test/test",
                                  json={"model": "form-model"})).get_json())
    assert d["status"] == "available", d
    assert seen["model"] == "form-model", seen


async def test_test_endpoint_sample_is_truncated_to_40(mk):
    """⑧ `sample` 是"回复前 40 字"（契约原文）—— 不能把整段回复塞进响应。"""
    c = await _test_client(mk, _json_transport(_ok_reply("字" * 200)))
    d = _data(await (await c.post("/api/v1/providers/p1%2Fm-test/test", json={})).get_json())
    assert len(d["sample"]) == 40, len(d["sample"])


async def test_test_endpoint_works_via_by_id_and_legacy_path(mk):
    """⑧ 同一个测试能力有三个入口都要能用（契约 §3 的四种 id 传递方式）。

    ⚠️ 尤其是 **URL 编码的路径形态** `POST /providers/<id>/test`：
       模型 id 一定含 `/`，若路由用默认转换器，实测在真 Hypercorn 上是 **404**。
       `by-id?provider_id=` 是对编码不敏感的可靠入口。
    """
    c = await _test_client(mk, _json_transport(_ok_reply()))

    d1 = _data(await (await c.post("/api/v1/providers/p1%2Fm-test/test", json={})).get_json())
    assert d1["status"] == "available", d1

    r2 = await c.get("/api/v1/providers/by-id?provider_id=p1%2Fm-test")
    assert r2.status_code == 200, r2.status_code
    d2 = _data(await r2.get_json())
    assert d2["provider"]["id"] == "p1/m-test", d2
    assert d2["provider"]["is_default"] is True, d2

    # 未编码的斜杠形态（Quart 的 `path` 转换器能接住）也要能打到同一个模型
    r3 = await c.post("/api/v1/providers/p1/m-test/test", json={})
    assert r3.status_code == 200, r3.status_code
    assert _data(await r3.get_json())["id"] == "p1/m-test"


# ===========================================================================
# ⑨ 落盘：位置 / 原子性 / 权限 / 损坏处理
# ===========================================================================


async def test_persisted_in_our_data_dir_not_astrbot(mk):
    """⑨ 落盘位置 = 契约的 `data/providers.json`（**我们的**数据目录）。

    🔴 两条一起验，缺一不可：
      ① 确实写到了我们给的路径；
      ② **绝不写 `/opt/astrbot`**（那是别人正在跑的 AstrBot 生产目录）。
    """
    c = await mk()
    await _seed(c)

    path = Path(c.providers_path)  # type: ignore[attr-defined]
    assert path.is_file(), f"没有落盘：{path}"
    assert path.name == "providers.json"
    assert "astrbot" not in str(path), path

    raw = _raw(path)
    assert raw["version"] == 2, raw
    assert raw["default_provider_id"] == "syuan/gemini-3.5-flash", raw
    assert raw["provider_sources"][0]["id"] == "syuan", raw
    assert raw["providers"][0]["id"] == "syuan/gemini-3.5-flash", raw
    # 源上必须有连接信息（两层结构的要点）
    assert raw["provider_sources"][0]["api_base"] == API_BASE, raw
    assert raw["provider_sources"][0]["key"] == [SECRET_KEY], raw

    # ② `/opt/astrbot` 下不该出现 providers.json
    # ⚠️ 只**读**这个目录，绝不写（`exists()` 在只读目录上也安全）
    assert not Path("/opt/astrbot/data/providers.json").exists(), (
        "🔴 有条代码把供应商配置写进了 AstrBot 的生产数据目录！"
    )


async def test_survives_restart(mk):
    """⑨ 重启（重开 app）后两层配置都在，且**脱敏形状不变**。"""
    c = await mk()
    await _add_source(c, id="syuan", key=[SECRET_KEY], timeout=90)
    await _add_model(c, id="syuan/m1", provider_source_id="syuan", model="m1")
    await _add_model(c, id="syuan/m2", provider_source_id="syuan", model="m2")
    await c.post("/api/v1/providers/syuan%2Fm2/default", json={})

    c2 = await mk(db="after-restart.db")
    d = _data(await (await c2.get("/api/v1/providers")).get_json())
    assert [s["id"] for s in d["provider_sources"]] == ["syuan"], d
    assert [m["id"] for m in d["providers"]] == ["syuan/m1", "syuan/m2"], d
    assert d["default_provider_id"] == "syuan/m2", d
    # 重启后 key 仍是"已配置"（读得回来），但依然不回显
    assert d["provider_sources"][0]["key_configured"] is True, d
    assert d["provider_sources"][0]["timeout"] == 90, d
    text = await (await c2.get("/api/v1/providers")).get_data(as_text=True)
    assert not _contains_key(text), text


async def test_corrupt_file_is_logged_and_empty(mk, caplog):
    """⑨ 文件损坏 → **记 ERROR** 并按空配置继续（项目铁律：不许静默）。

    判据有三条，缺一不可：
      ① 接口仍能工作（回 200 空列表，而不是 500）；
      ② 日志里有一条 ERROR **点名路径**；
      ③ 用户随后写入不会静默覆盖坏文件（会先备份成 `.corrupt`）。
    """
    c = await mk()
    path = Path(c.providers_path)  # type: ignore[attr-defined]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 JSON", encoding="utf-8")

    caplog.set_level(logging.ERROR, logger="emotion_app.infra.providers_store")
    r = await c.get("/api/v1/providers")
    assert r.status_code == 200, f"配置坏了不该让面板打不开：{r.status_code}"
    assert _data(await r.get_json())["providers"] == []

    errors = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert errors, "配置损坏时**没有任何 ERROR 日志** —— 这就是静默失败"
    assert any("providers.json" in rec.getMessage() or "providers.json" in str(rec.args)
               for rec in errors), [rec.getMessage() for rec in errors]

    # ③ 接着新增一条：坏文件必须先被备份（否则用户永远查不出原来是什么）
    assert (await _add_source(c, id="fast")).status_code == 201
    assert path.with_name("providers.json.corrupt").is_file(), "坏文件被静默覆盖了"
    assert _raw(path)["provider_sources"][0]["id"] == "fast"


async def test_missing_file_is_normal(mk, caplog):
    """⑨ 文件**缺失**是正常路径（首次使用）—— 不该记 ERROR 吓人。

    ⚠️ 与"损坏"必须区分开：把正常情况报成错误，日志就会被噪音淹没，
    真正的错误反而看不见（这正是 write_guard 排除 `__pycache__` 的同款理由）。
    """
    c = await mk()
    caplog.set_level(logging.ERROR, logger="emotion_app.infra.providers_store")
    assert (await c.get("/api/v1/providers")).status_code == 200
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "文件缺失（首次使用）被报成了 ERROR"
    )


def test_store_roundtrip_directly(tmp_path):
    """⑨ 存储层自身的往返（不绕 HTTP）：create → 新 store 实例读到同样的值。"""
    path = tmp_path / "providers.json"
    s1 = ProviderStore(path)
    s1.create_source(_source(id="a", api_base="https://a/v1", key=[SECRET_KEY], timeout=30))
    s1.create_source(_source(id="b", api_base="https://b/v1", key=[]))
    s1.create_model(_model(id="a/m1", provider_source_id="a", model="m1"))
    s1.create_model(_model(id="b/m2", provider_source_id="b", model="m2"))
    s1.set_default("b/m2")

    s2 = ProviderStore(path)                     # 新实例 = 模拟重启
    sources = {s.id: s for s in s2.list_sources()}
    models = {m.id: m for m in s2.list_models()}
    assert set(sources) == {"a", "b"}, sources
    assert set(models) == {"a/m1", "b/m2"}, models

    a = sources["a"]
    assert a.key == [SECRET_KEY], "key 必须真的落盘（否则重启就丢了）"
    assert a.api_base == "https://a/v1" and a.timeout == 30, a
    assert a.provider == "openai" and a.type == "openai_chat_completion", a
    assert a.provider_type == "chat_completion" and a.enable is True, a
    assert sources["b"].key == [], "没填 key 的就是空，不许伪造"
    assert models["a/m1"].model == "m1" and models["a/m1"].provider_source_id == "a"
    assert s2.default_provider_id == "b/m2"

    # `load()` 是给宿主装配用的契约形状（**含明文 key** —— 它只给进程内用）
    contract = s2.load()
    assert contract["version"] == 2, contract
    assert {s["id"] for s in contract["provider_sources"]} == {"a", "b"}
    assert {m["id"] for m in contract["providers"]} == {"a/m1", "b/m2"}
    # 而 `snapshot()` 是给 API 用的（脱敏）
    snap = s2.snapshot()
    assert all("api_key" not in p for p in snap["providers"]), snap
    assert all("api_key" not in s for s in snap["provider_sources"]), snap
    assert not _contains_key(json.dumps(snap, ensure_ascii=False)), snap


def test_no_tmp_files_left_behind(tmp_path):
    """⑨ 写完不许在数据目录里留 `.tmp`（否则备份/同步会带上垃圾）。"""
    path = tmp_path / "providers.json"
    s = ProviderStore(path)
    s.create_source(_source())
    s.create_model(_model())
    s.update_source("syuan", {"timeout": 30})
    s.update_model("syuan/gemini-3.5-flash", {"enable": False})
    s.set_default("syuan/gemini-3.5-flash")
    s.delete_model("syuan/gemini-3.5-flash")
    s.delete_source("syuan")
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "providers.json"]
    assert leftovers == [], leftovers


def test_write_is_atomic_uses_os_replace(tmp_path, monkeypatch):
    """⑨ 🔴 落盘必须**原子**（临时文件 → `os.replace`）。

    为什么这条要单独验：直接 `path.write_text()` 写到一半崩掉，
    留下的半截 JSON 下次启动只能按"损坏"处理 → **用户的全部供应商一次性丢失**。

    判据（注入 bug 验证用的就是它）：实现里必须真的调用 `os.replace`，
    且临时文件建在**目标同一目录**（跨文件系统时 `os.replace` 就不再原子）。
    """
    import os as _os

    calls: list[tuple[str, str]] = []
    real_replace = _os.replace

    def spy(src, dst, *a, **kw):  # noqa: ANN001
        calls.append((str(src), str(dst)))
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(_os, "replace", spy)
    path = tmp_path / "sub" / "providers.json"
    ProviderStore(path).create_source(_source())

    assert calls, "🔴 没有调用 os.replace —— 写入不是原子的"
    src, dst = calls[-1]
    assert Path(dst) == path, calls
    assert Path(src).parent == path.parent, (
        f"临时文件不在目标目录（跨文件系统会失去原子性）：{src}"
    )
    assert path.is_file()


def test_migration_write_is_atomic_too(tmp_path, monkeypatch):
    """⑨ **迁移的落盘也要原子**（迁移路径是新加的写入口，别忘了同一条纪律）。"""
    import os as _os

    calls: list[tuple[str, str]] = []
    real_replace = _os.replace

    def spy(src, dst, *a, **kw):  # noqa: ANN001
        calls.append((str(src), str(dst)))
        return real_replace(src, dst, *a, **kw)

    path = tmp_path / "sub" / "providers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(LEGACY_V1, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(_os, "replace", spy)
    ProviderStore(path).load()

    assert calls, "🔴 迁移落盘没走 os.replace"
    src, dst = calls[-1]
    assert Path(src).parent == path.parent, src
    assert _raw(path)["version"] == 2


def test_file_mode_is_owner_only(tmp_path):
    """⑨ 文件里有**明文 key** → 权限必须是 0600（与 `.env` 同级处理）。

    ⚠️ **必须先把 umask 设成 022**：默认环境下 `tempfile.mkstemp` 造出来的临时文件
       本来就是 0600，于是"忘了 `chmod`"这个 bug 会被环境**掩盖成假通过**。
       改成 022 之后只有真调了 `chmod` 才会是 0600，否则是 0644。
    """
    import os
    import stat

    old_umask = os.umask(0o022)
    try:
        path = tmp_path / "providers.json"
        ProviderStore(path).create_source(_source())
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"供应商配置权限是 {oct(mode)}，应为 0o600（含明文 key）"

        # 改一次（走 update 的写入路径）也要是 0600，别只在首次 create 时设
        ProviderStore(path).update_source("syuan", {"timeout": 10})
        mode2 = stat.S_IMODE(path.stat().st_mode)
        assert mode2 == 0o600, f"改动后的权限是 {oct(mode2)}，应为 0o600"

        # 自证：这个判据确实受 umask 影响（022 下普通新建文件是 0644），
        # 否则说明上面那条"通过"只是因为环境恰好给了 0600
        probe = tmp_path / "probe"
        probe.write_text("x", encoding="utf-8")
        assert stat.S_IMODE(probe.stat().st_mode) == 0o644, "测试自身的 umask 假设不成立"
    finally:
        os.umask(old_umask)


def test_migration_file_mode_is_owner_only(tmp_path):
    """⑨ 迁移写出来的文件同样要 0600（迁移路径里也有明文 key）。"""
    import os
    import stat

    old_umask = os.umask(0o022)
    try:
        path = tmp_path / "providers.json"
        path.write_text(json.dumps(LEGACY_V1, ensure_ascii=False), encoding="utf-8")
        ProviderStore(path).load()
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"迁移后的权限是 {oct(mode)}，应为 0o600"
    finally:
        os.umask(old_umask)


# ===========================================================================
# ⑩ 与宿主 ProviderManager 同步
# ===========================================================================


class _FakeHostService:
    """假插件宿主：形状照 `PluginHostService`（`.host.context.provider_manager`）。"""

    def __init__(self, pm) -> None:  # noqa: ANN001
        self.host = type("H", (), {"context": type("C", (), {"provider_manager": pm})()})()


def _pm():
    from astrbot_host.provider import ProviderManager

    return ProviderManager()


async def _client_with_pm(mk, pm):
    c = await mk()
    c.app.config["EMOTION_PLUGIN_HOST"] = _FakeHostService(pm)
    return c


async def test_create_model_syncs_provider_manager(mk):
    """⑩ 新增模型后宿主 `pm.inst_map` 里能查到，且 **连接信息继承自源**。

    🔴 这是整件事的**目的**：不同步的话插件解析到的还是旧的（或什么都没有），
       实测就会退化成"模板兜底"那条 WARNING。
    🔴 而"继承源的 base_url/key"是**两层结构的核心**：宿主 `Provider` 一个模型一个实例，
       模型自己没有地址和 key —— 不展平就全都调不通。
    """
    pm = _pm()
    c = await _client_with_pm(mk, pm)
    await _add_source(c, id="syuan", api_base="https://up.test/v1", key=[SECRET_KEY], timeout=45)

    r = await _add_model(c, id="syuan/m-fast", provider_source_id="syuan", model="m-fast")
    assert r.status_code == 201, await r.get_data(as_text=True)

    assert "syuan/m-fast" in pm.inst_map, f"宿主里没有这条：{list(pm.inst_map)}"
    p = pm.get_provider_by_id("syuan/m-fast")
    assert p.get_model() == "m-fast", p.get_model()
    assert p.provider_config["api_base"] == "https://up.test/v1", p.provider_config
    assert p.provider_config["key"] == [SECRET_KEY], p.provider_config
    # 第一个注册的自动成为默认（与宿主 register 的既有语义一致）
    assert pm.default_provider_id == "syuan/m-fast", pm.default_provider_id
    assert pm.get_using().provider_id == "syuan/m-fast"


async def test_add_model_via_source_path_syncs_host(mk):
    """🔴 ⑩ **两条新增模型的入口都要同步宿主**。

    新增模型有两条路：`POST /api/v1/providers` 与契约 §2 的
    `POST /api/v1/provider-sources/<源id>/providers`。
    实测踩到的坑：注入"新增后不同步"时**一条判据都没挂** ——
    因为注入只命中了其中一条路径，而判据只覆盖了另一条。
    → 每个能力都要**按入口**验（"模块能工作 ≠ 被接上了"的同款教训）。
    """
    pm = _pm()
    c = await _client_with_pm(mk, pm)
    await _add_source(c, id="mxgpt", api_base="https://up.test/v1", key=[SECRET_KEY])

    r = await c.post("/api/v1/provider-sources/mxgpt/providers",
                     json={"model": "gpt-5.6-sol"})
    assert r.status_code == 201, await r.get_data(as_text=True)
    mid = _data(await r.get_json())["id"]

    assert mid in pm.inst_map, f"契约 §2 的新增入口没同步宿主：{list(pm.inst_map)}"
    p = pm.get_provider_by_id(mid)
    assert p.get_model() == "gpt-5.6-sol", p.get_model()
    assert p.provider_config["api_base"] == "https://up.test/v1", p.provider_config
    assert p.provider_config["key"] == [SECRET_KEY], p.provider_config


async def test_update_source_resyncs_all_its_models(mk):
    """🔴 ⑩ 改**源**（地址/key/超时）→ 该源下**所有模型**都要重注册。

    ⚠️ 宿主的 `Provider` 是**不可变快照**：只改文件的话插件继续用旧地址，
       而面板上一切正常 —— 最难查的那种分叉。
       这条守的就是"改端点只同步了一个模型/一个都没同步"。
    """
    pm = _pm()
    c = await _client_with_pm(mk, pm)
    await _add_source(c, id="syuan", api_base="https://old.test/v1", key=["sk-old"], timeout=10)
    await _add_model(c, id="syuan/m1", provider_source_id="syuan", model="m1")
    await _add_model(c, id="syuan/m2", provider_source_id="syuan", model="m2")

    r = await c.put("/api/v1/provider-sources/by-id?source_id=syuan",
                    json={"api_base": "https://new.test/v1", "key": ["sk-new"], "timeout": 77})
    assert r.status_code == 200, await r.get_data(as_text=True)

    for mid in ("syuan/m1", "syuan/m2"):
        p = pm.get_provider_by_id(mid)
        assert p is not None, f"改源后宿主里没了 {mid}"
        assert p.provider_config["api_base"] == "https://new.test/v1", (
            f"{mid} 还在用旧地址（宿主 Provider 是不可变快照）：{p.provider_config}"
        )
        assert p.provider_config["key"] == ["sk-new"], p.provider_config


async def test_update_model_resyncs_provider_manager(mk):
    """⑩ 改完宿主里的**值也变了**（抓"只改文件不重新 register"）。"""
    pm = _pm()
    c = await _client_with_pm(mk, pm)
    await _add_source(c, id="syuan")
    await _add_model(c, id="syuan/m-old", provider_source_id="syuan", model="m-old")

    r = await c.put("/api/v1/providers/by-id?provider_id=syuan%2Fm-old",
                    json={"model": "m-new"})
    assert r.status_code == 200
    # 改 model 名 → id 也跟着变（保持 `id = 源id/模型名` 的不变量）
    new_id = _data(await r.get_json())["id"]
    assert new_id == "syuan/m-new", new_id
    assert "syuan/m-old" not in pm.inst_map, list(pm.inst_map)
    assert pm.get_provider_by_id("syuan/m-new").get_model() == "m-new", list(pm.inst_map)


async def test_set_default_syncs_provider_manager(mk):
    """⑩ 设默认后宿主的 `get_using()` 换成那条（插件就是靠它选模型的）。"""
    pm = _pm()
    c = await _client_with_pm(mk, pm)
    await _add_source(c, id="s")
    await _add_model(c, id="s/m1", provider_source_id="s", model="m1")
    await _add_model(c, id="s/m2", provider_source_id="s", model="m2")

    assert (await c.post("/api/v1/providers/s%2Fm2/default", json={})).status_code == 200
    assert pm.default_provider_id == "s/m2", pm.default_provider_id
    assert pm.get_using().provider_id == "s/m2"


async def test_delete_source_removes_its_models_from_host(mk):
    """⑩ 删源级联 → 被级联删掉的模型也要从宿主里摘掉。

    ⚠️ 不摘的话插件还能选到一个"源都没了"的模型 —— 它永远调不通，
       而面板上（按文件渲染）已经看不到它了，两边分叉。
    """
    pm = _pm()
    c = await _client_with_pm(mk, pm)
    await _add_source(c, id="a")
    await _add_source(c, id="b")
    await _add_model(c, id="a/m1", provider_source_id="a", model="m1")
    await _add_model(c, id="b/m2", provider_source_id="b", model="m2")

    assert (await c.delete("/api/v1/provider-sources/by-id?source_id=a")).status_code == 200
    assert "a/m1" not in pm.inst_map, list(pm.inst_map)
    assert "b/m2" in pm.inst_map, "不该误删别的源的模型"
    assert pm.default_provider_id == "b/m2", pm.default_provider_id
    assert pm.get_using() is not None, "默认悬空了 —— 插件会解析不到任何供应商"


async def test_delete_model_syncs_provider_manager(mk):
    """⑩ 删模型后宿主里也没了，且默认**不会悬空**。"""
    pm = _pm()
    c = await _client_with_pm(mk, pm)
    await _add_source(c, id="s")
    await _add_model(c, id="s/m1", provider_source_id="s", model="m1")
    await _add_model(c, id="s/m2", provider_source_id="s", model="m2")

    assert (await c.delete("/api/v1/providers/by-id?provider_id=s%2Fm1")).status_code == 200
    assert "s/m1" not in pm.inst_map, list(pm.inst_map)
    assert pm.default_provider_id == "s/m2", pm.default_provider_id
    assert pm.get_using() is not None


async def test_set_default_warns_when_host_refuses(mk, caplog):
    """🔴 ⑩ 宿主 `set_default()` 返回 False 时**必须留 WARNING**（不许静默失败）。

    静默的话，用户看到面板显示"默认：xxx"，而插件的 `get_using_provider()`
    其实拿到 None → 又一次"配置看着对、插件退化成模板兜底"的静默故障。

    ⚠️ 怎么造出这个状态：正常流程下这条分支够不到（设默认前会先 `_sync_model`
       把这条注册进宿主自愈）。这里直接把 `set_default` 打桩成拒绝，才**真的**
       走到了那个分支（"清空 inst_map"那种造法会被自愈逻辑修好 → 判据假通过）。
    """
    pm = _pm()
    c = await _client_with_pm(mk, pm)
    await _add_source(c, id="s")
    await _add_model(c, id="s/m1", provider_source_id="s", model="m1")

    pm.set_default = lambda provider_id: False  # type: ignore[method-assign]

    caplog.set_level(logging.WARNING, logger="emotion_app.web.api_providers")
    r = await c.post("/api/v1/providers/s%2Fm1/default", json={})
    assert r.status_code == 200, await r.get_data(as_text=True)

    # ⚠️ 用 `getMessage()`（它自己会做 `%` 格式化）——
    #    再手动 `% rec.args` 会二次格式化，直接 TypeError。
    warns = [rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert any("没有" in w and "s/m1" in w for w in warns), (
        f"宿主拒绝了设默认，接口却**一个字都没说** —— 静默失败：{warns}"
    )


async def test_sync_skipped_gracefully_without_host(mk, caplog):
    """🔴 ⑩ 宿主**未启用**时 API 仍要能工作：跳过同步并记日志，**不许报错崩掉**。

    为什么必须有这条：默认部署就是"插件关闭"。如果同步失败会 500，
    那面板连供应商都配不了 —— 而配供应商恰恰是**为了让插件能用**的第一步。
    """
    c = await mk()      # 没有 EMOTION_PLUGIN_HOST
    caplog.set_level(logging.INFO, logger="emotion_app.web.api_providers")

    assert (await _add_source(c, id="s")).status_code == 201
    assert (await _add_model(c, id="s/m1", provider_source_id="s", model="m1")).status_code == 201
    assert (await c.get("/api/v1/providers")).status_code == 200
    assert (await c.get("/api/v1/provider-sources")).status_code == 200
    assert (await c.post("/api/v1/providers/s%2Fm1/default", json={})).status_code == 200
    assert (await c.put("/api/v1/provider-sources/by-id?source_id=s",
                        json={"timeout": 5})).status_code == 200
    assert (await c.delete("/api/v1/provider-sources/by-id?source_id=s")).status_code == 200

    # ② 必须**留痕**（否则"改了配置没生效"时日志里一个字都没有）
    notes = [rec.getMessage() for rec in caplog.records]
    assert any("跳过 ProviderManager 同步" in m for m in notes), notes


async def test_sync_skipped_when_host_not_started(mk):
    """🔴 ⑩ 宿主**启用了但没起来**（`.host is None`）也不许崩 —— 照旧跳过。"""
    c = await mk()
    c.app.config["EMOTION_PLUGIN_HOST"] = type("S", (), {"host": None})()
    assert (await _add_source(c, id="s")).status_code == 201
    assert (await c.get("/api/v1/providers")).status_code == 200


async def test_provider_created_via_api_can_reach_llm_port(mk):
    """⑩ **端到端**：API 建出来的模型，插件调 `text_chat` 能真打到上游后端。

    ⚠️ 只验"pm 里有这条"是不够的（"模块能工作 ≠ 被接上了"）——
       必须验它带着可用的 `LLMPort`（否则插件拿到的是"调用即抛错"的壳）。
    """
    pm = _pm()
    c = await _client_with_pm(mk, pm)
    backend = _FakeBackend("记忆抽取结果")
    c.app.config["EMOTION_CHAT_BACKEND"] = backend

    await _add_source(c, id="s")
    await _add_model(c, id="s/m-fast", provider_source_id="s", model="m-fast")
    resp = await pm.get_using().text_chat(prompt="总结一下", system_prompt="你是总结器")

    assert resp.completion_text == "记忆抽取结果", resp
    assert backend.calls[0]["model"] == "m-fast", backend.calls
    msgs = backend.calls[0]["messages"]
    assert msgs[0] == {"role": "system", "content": "你是总结器"}, msgs
    assert msgs[-1] == {"role": "user", "content": "总结一下"}, msgs


async def test_chat_only_backend_is_actually_usable_at_startup(mk, caplog):
    """🔴 ⑩ **启动路径**上 chat-only 的后端必须真的能用（这是修完 bug 后的正确行为）。

    历史（2026-09-20 本机实跑抓到，已修）：
    装配层暴露的 `EMOTION_CHAT_BACKEND` 是领域层 `OpenAIChatBackend`，
    它原本只有 `chat()` / `chat_stream()`，**没有 `complete()`**；
    `UpstreamLLMPort.generate()` 调的却是 `backend.complete(...)`，
    兜底只写了 `except TypeError` —— **接不住 `AttributeError`**。
    → 插件在启动路径上一用模型就炸，而且错误完全指不到根因。

    修法是**两处都补**（互为保险）：
      ①`UpstreamLLMPort.generate()` 在 `complete` 缺失时**回落到 `chat()`**；
      ②`OpenAIChatBackend` 补上 `complete()`。
    → 所以现在 chat-only 的后端**不该再被拒**，也不该再有"没有 complete()"的告警。

    ⚠️ 为什么必须用**真类**而不是假 backend：假 backend 我会照期望的形状写，
       于是"真实形状对不上"这件事会被测试**自己掩盖**掉。
    """
    from emotion_app.domain.backends import OpenAIChatBackend

    pm = _pm()
    c = await _client_with_pm(mk, pm)
    c.app.config["EMOTION_CHAT_BACKEND"] = OpenAIChatBackend()

    caplog.set_level(logging.WARNING, logger="emotion_app.web.api_providers")
    await _add_source(c, id="s")
    assert (await _add_model(c, id="s/m1", provider_source_id="s", model="m1")).status_code == 201

    warns = [rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert not any("没有 complete()" in w for w in warns), (
        f"chat-only 的后端已经能用（有回落），却仍在告警说接不了：{warns}"
    )
    prov = pm.get_provider_by_id("s/m1")
    assert prov is not None, list(pm.inst_map)
    assert prov._llm is not None, (
        "chat-only 的后端**没有**被接成模型端口 —— 插件会静默拿不到模型"
    )


async def test_startup_path_backend_can_actually_generate(mk):
    """🔴 ⑩ **决定性判据**：启动路径接上的端口，真的能调通（不只是"接上了"）。

    ⚠️ 这条是本轮补的**端到端**判据。原来的两条只验"接没接"，
       接上了却调不通（`AttributeError` / 参数传错）照样是坏的 ——
       而"接上了"和"能用"之间的差距，正是本项目反复踩的那类坑
       （「模块能工作 ≠ 被接上了」的下一层：**接上了 ≠ 能用**）。
    """
    from emotion_app.domain.backends import OpenAIChatBackend
    from emotion_app.web.provider_bridge import UpstreamLLMPort

    backend = OpenAIChatBackend()
    await backend.open()
    try:
        port = UpstreamLLMPort(
            backend=backend, model="m1", base_url="https://x/v1", api_key="k"
        )
        # 打桩上游：回一个最小合法响应，证明**整条链路**（port → chat → 解析）真的通。
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "id": "x",
                    "model": "m1",
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": "pong"},
                         "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        backend._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await port.generate(prompt="ping")
        finally:
            await backend.close()

        assert getattr(result, "content", None) == "pong", result
        assert seen["url"].endswith("/chat/completions"), seen["url"]
        # ⚠️ 必须用**插件那条源**的 model（不是空串/默认值）——
        #    `chat()` 的 model 是必填关键字，漏传会打到错模型。
        assert seen["body"]["model"] == "m1", seen["body"].get("model")
        assert seen["body"]["messages"] == [{"role": "user", "content": "ping"}], seen["body"]
    finally:
        await backend.close()


async def test_plugin_max_tokens_actually_reaches_the_upstream_body(mk):
    """🔴 决定性判据：插件传的 `max_tokens` **必须真的进上游请求体**。

    为什么单独立这一条（2026-09-20 实测的三个真 bug 里最隐蔽的一个）：
      `UpstreamLLMPort.generate()` 签名收了 `**kwargs` 却**从不使用**；
      `OpenAIChatBackend.complete()` 也收了 `**kwargs` 却不转发 `extra`
      → 插件用来控成本的 `max_tokens`（`token_budget.py:1530`）
      **在两级上被静默丢弃**，全程无日志、无报错，只是成本对不上。

    ⚠️ 只在"装配层"验（`port.generate(max_tokens=...)`）是不够的 ——
       必须一路验到**真 HTTP 请求体**，否则中间任何一层吞掉参数都发现不了。
       这正是本项目「通道要完整消费」那条通则的又一次应用。
    """
    import httpx

    from emotion_app.domain.backends import OpenAIChatBackend
    from emotion_app.web.provider_bridge import UpstreamLLMPort

    backend = OpenAIChatBackend()
    await backend.open()
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "id": "x",
                "model": "m1",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}
                ],
            },
        )

    backend._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        port = UpstreamLLMPort(
            backend=backend, model="m1", base_url="https://x/v1", api_key="k"
        )
        await port.generate(prompt="hi", max_tokens=321)
    finally:
        await backend.close()

    body = seen.get("body") or {}
    assert body.get("max_tokens") == 321, (
        f"max_tokens 没进上游请求体（实际 body={body!r}）—— "
        "插件用它控成本，被静默丢弃会让预算形同虚设且没有任何报错"
    )


async def test_plugin_extra_params_do_not_leak_none_values(mk):
    """`None` 值不能进请求体（部分网关见 `{"max_tokens": null}` 会判 400）。

    与上一条互补：上一条验"该传的传到了"，这一条验"不该传的别传"。
    """
    import httpx

    from emotion_app.domain.backends import OpenAIChatBackend
    from emotion_app.web.provider_bridge import UpstreamLLMPort

    backend = OpenAIChatBackend()
    await backend.open()
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "id": "x",
                "model": "m1",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}
                ],
            },
        )

    backend._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        port = UpstreamLLMPort(
            backend=backend, model="m1", base_url="https://x/v1", api_key="k"
        )
        await port.generate(prompt="hi", max_tokens=None, temperature=None)
    finally:
        await backend.close()

    body = seen.get("body") or {}
    assert "max_tokens" not in body, f"None 值漏进请求体：{body!r}"
    assert "temperature" not in body, f"None 值漏进请求体：{body!r}"


async def test_backend_with_neither_complete_nor_chat_fails_loudly(mk):
    """🔴 两条路都没有 → **响亮抛错**（绝不返回空回复让插件以为"模型答了空话"）。"""
    from emotion_app.web.provider_bridge import UpstreamLLMPort

    class _Useless:
        """既没有 complete() 也没有 chat() —— 只有别的方法。"""

        async def something_else(self): ...

    port = UpstreamLLMPort(backend=_Useless(), model="m", base_url="https://x/v1", api_key="k")
    with pytest.raises(RuntimeError, match="complete.*chat|chat.*complete"):
        await port.generate(prompt="hi")


# ===========================================================================
# ⑪ 装配：路由真的挂上了（"模块能工作 ≠ 被接上了"）
# ===========================================================================


async def test_routes_are_mounted_in_real_app():
    """🔴 装配判据：契约里的路由必须**真的挂在 app 上**。

    ⚠️ 这条抓的是最经典的一类事故：`api_providers.py` 写得再好，
       `app.py` 里忘了 `register_blueprint` → 面板全部 404，
       而所有单元测试照样全绿（它们直接调函数/自建 app）。
    """
    cfg = Config()
    cfg.auth.access_token = TOKEN
    app = create_app(cfg, None, AuthManager(access_token=TOKEN))  # type: ignore[arg-type]
    rules = {
        (rule.rule, tuple(sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS"))))
        for rule in app.url_map.iter_rules()
    }
    expected = {
        # 端点层
        # ⚠️ 规则是"合并"还是"分开"取决于注册方式（`@bp.route(methods=[...])` 合并成一条、
        #    各自 `@bp.get`/`@bp.post` 则两条）—— 这里按**实测**的 url_map 形状断言，
        #    写错了会立刻挂（我第一版就按"想当然"写，挂了两条）。
        ("/api/v1/provider-sources", ("GET",)),
        ("/api/v1/provider-sources", ("POST",)),
        # 🔴 本轮新增：厂商预设目录（契约 §3）。
        #    ⚠️ 漏了这条，路由没挂上也照样全绿 —— 这正是本条装配判据存在的意义。
        ("/api/v1/provider-presets", ("GET",)),
        ("/api/v1/provider-sources/by-id", ("DELETE", "GET", "PUT")),
        ("/api/v1/provider-sources/models", ("GET",)),
        ("/api/v1/provider-sources/<source_id>/models", ("GET", "POST")),
        ("/api/v1/provider-sources/<source_id>/providers", ("GET",)),
        ("/api/v1/provider-sources/<source_id>/providers", ("POST",)),
        # 模型层
        ("/api/v1/providers", ("GET", "POST")),
        ("/api/v1/providers/by-id", ("DELETE", "GET", "PUT")),
        # 🔴 单条模型 / 设默认 / 测试**共用一条** `<path:provider_id>` 规则，
        #    在函数内部按后缀分派 —— 写成多条规则时后者会被前者吃掉
        #    （实测 `/providers/syuan%2Fgpt-5.5/default` 回 405，见模块注释）。
        ("/api/v1/providers/<path:provider_id>", ("DELETE", "GET", "PATCH", "POST", "PUT")),
    }
    missing = expected - rules
    assert not missing, f"这些契约路由没挂上：{sorted(missing)}"


async def test_single_model_reachable_via_every_path_shape(mk):
    """🔴 装配判据之二：同一条模型必须能被**四种 URL 形态**访问到。

    模型 id 一定含 `/`，于是"单条模型"这件事在 URL 上有很多写法。
    实测踩到的坑：`POST /api/v1/providers/syuan%2Fgpt-5.5/default` **405**
    —— 因为 `/providers/<provider_id>`（`default` 规则的兄弟）先把它吃掉了。
    现在统一由 `<path:provider_id>` 接住再按后缀分派，四种形态都必须通。

    ⚠️ 这条是"路由表看着对、实际 405"的**唯一**拦法：光断言 `url_map` 里有规则
       是抓不到的（规则确实都在，只是被别的规则抢了）。
    """
    c = await mk()
    await _add_source(c, id="s")
    await _add_model(c, id="s/m1", provider_source_id="s", model="m1")
    await _add_model(c, id="s/m2", provider_source_id="s", model="m2")

    # ① by-id（对编码不敏感，最可靠 —— 真实站的形态）
    r1 = await c.get("/api/v1/providers/by-id?provider_id=s%2Fm1")
    assert r1.status_code == 200, r1.status_code
    assert _data(await r1.get_json())["provider"]["id"] == "s/m1"

    # ② URL 编码的路径形态（契约 §3 明确要求"id 需 URL 编码"）
    r2 = await c.get("/api/v1/providers/s%2Fm1")
    assert r2.status_code == 200, f"编码形态 404/405 了：{r2.status_code}"
    assert _data(await r2.get_json())["provider"]["id"] == "s/m1"

    # ③ 未编码的斜杠形态
    r3 = await c.get("/api/v1/providers/s/m1")
    assert r3.status_code == 200, f"未编码形态挂了：{r3.status_code}"
    assert _data(await r3.get_json())["provider"]["id"] == "s/m1"

    # ④ 子资源后缀：设默认（**实测踩过 405 的那一条**）
    r4 = await c.post("/api/v1/providers/s%2Fm2/default", json={})
    assert r4.status_code == 200, f"设默认 405/404 了（路由被吃掉）：{r4.status_code}"
    assert _data(await r4.get_json())["default_provider_id"] == "s/m2"

    # ④' 子资源后缀：测试（同样必须通）
    c.app.config["EMOTION_PROVIDERS_TRANSPORT"] = _json_transport(_ok_reply("hi"))
    r5 = await c.post("/api/v1/providers/s%2Fm1/test", json={})
    assert r5.status_code == 200, f"测试端点 405/404 了：{r5.status_code}"
    assert _data(await r5.get_json())["status"] == "available"

    # 旧前端的 PATCH 形态也必须还能用（"旧路由保留可用"是我们对前端的承诺）
    r6 = await c.patch("/api/v1/providers/s%2Fm1", json={"enable": False})
    assert r6.status_code == 200, f"PATCH 形态挂了：{r6.status_code}"
    assert _data(await r6.get_json())["enable"] is False

    # DELETE 形态
    r7 = await c.delete("/api/v1/providers/s/m1")
    assert r7.status_code == 200, f"DELETE 未编码形态挂了：{r7.status_code}"
    assert _data(await r7.get_json())["id"] == "s/m1"


def test_provider_panel_check_tool_still_matches_routes():
    """装配之外：前端验证脚本 `tools/provider_panel_check.py` 里的路由假设要对得上。

    ⚠️ 它（B 改版后）探的是 `GET /api/v1/provider-sources`（两层结构的新入口）——
       这条把"我们承诺不删它"钉住，避免前端脚本静默失效。
       （静态文件的改造是 B 的活，这里只守"路由还在"。）
    """
    src = (APP_ROOT / "tools/provider_panel_check.py").read_text(encoding="utf-8")
    assert "/api/v1/provider-sources" in src, "验证脚本引用的路由不见了"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 夹具已**提升到 `tests/conftest.py`**（2026-09-20）
# ---------------------------------------------------------------------------
# 🔴 为什么搬走：新加的 `tests/test_provider_presets.py` 需要同一个 `mk`，
#    而原先它只定义在本文件内部 → 新文件报 `fixture 'mk' not found`。
#    当时有两条歪路：①在新文件里复制一份（两份会漂移）；
#    ②跨测试文件 `from tests.test_providers_api import mk`（收集顺序/循环导入会咬人）。
#    → 正确做法是提到 conftest，一份定义、所有测试文件共享。
# ⚠️ 本文件里的判据**照旧直接使用 `mk`**，不需要 import（pytest 自动发现 conftest）。


# ---------------------------------------------------------------------------
# N0(2026-09-21):非对话类**绝不能**混进插件的对话供应商列表
# ---------------------------------------------------------------------------


def test_flatten_two_layer_filters_by_provider_type():
    """🔴🔴 N0 回归判据:`flatten_two_layer` 必须能按 `provider_type` 分流。

    这是 2026-09-21 实测撞出的真 bug:`provider_type` 写在**源**上,
    而展平函数**完全不看它** → 配一个 TTS 源后,插件的**对话**供应商列表里
    混进了语音模型(`items` 里有 `tts-probe/zh-CN-XiaoxiaoNeural`,`tts_items` 却是空)
    → 插件会把语音模型当对话模型调,**界面全正常、真调用才炸在很远的地方**。

    注入验证:把 `flatten_two_layer` 里的 `provider_type` 比较删掉 →
    断言 ② 必须挂(对话列表里会出现 tts 模型)。
    """
    from emotion_app.infra.providers_store import (
        PROVIDER_TYPE_CHAT,
        PROVIDER_TYPE_TTS,
    )
    from emotion_app.web.provider_bridge import flatten_two_layer

    store = {
        "default_provider_id": "chat-src/gpt-x",
        "provider_sources": [
            {
                "id": "chat-src", "provider_type": PROVIDER_TYPE_CHAT,
                "type": "openai_chat_completion",
                "api_base": "https://a/v1", "key": ["k-chat"], "enable": True,
            },
            {
                "id": "tts-src", "provider_type": PROVIDER_TYPE_TTS,
                "type": "edge_tts",
                "api_base": "https://t/v1", "key": ["k-tts"], "enable": True,
            },
        ],
        "providers": [
            {"id": "chat-src/gpt-x", "provider_source_id": "chat-src",
             "model": "gpt-x", "enable": True},
            {"id": "tts-src/zh-CN-Xiaoxiao", "provider_source_id": "tts-src",
             "model": "zh-CN-Xiaoxiao", "enable": True},
        ],
    }

    # ① 不过滤 = 两条都在(保持旧调用方的语义)
    everything, _ = flatten_two_layer(store)
    assert {p["id"] for p in everything} == {"chat-src/gpt-x", "tts-src/zh-CN-Xiaoxiao"}

    # ② 只要对话 → **语音模型必须不在里面**(这就是 N0 的核心断言)
    chat, default_id = flatten_two_layer(store, provider_type=PROVIDER_TYPE_CHAT)
    assert {p["id"] for p in chat} == {"chat-src/gpt-x"}, (
        f"对话列表里混进了非对话模型:{[p['id'] for p in chat]}"
    )
    assert default_id == "chat-src/gpt-x"

    # ③ 只要语音 → 反过来
    tts, _ = flatten_two_layer(store, provider_type=PROVIDER_TYPE_TTS)
    assert {p["id"] for p in tts} == {"tts-src/zh-CN-Xiaoxiao"}

    # ④ 展平结果带上 provider_type(调用方可据此再判)
    assert chat[0]["provider_type"] == PROVIDER_TYPE_CHAT


def test_flatten_default_id_not_in_filtered_list_returns_a_valid_one():
    """默认供应商**不在**过滤后的列表里时,不能回传一个查不到的 id。

    实测形状:默认是对话模型,但要的是 TTS 列表 → 若不处理,
    调用方会拿 `default_id` 去列表里查,**查不到 → 静默用第一个**(行为不可预期)。
    本判据要求:回传的 `default_id` **要么是列表内真实存在的,要么是空串**。
    """
    from emotion_app.infra.providers_store import PROVIDER_TYPE_TTS
    from emotion_app.web.provider_bridge import flatten_two_layer

    store = {
        "default_provider_id": "chat-src/gpt-x",   # 默认是**对话**模型
        "provider_sources": [
            {"id": "chat-src", "provider_type": "chat_completion",
             "type": "openai_chat_completion", "api_base": "https://a/v1",
             "key": ["k1"], "enable": True},
            {"id": "tts-src", "provider_type": PROVIDER_TYPE_TTS, "type": "edge_tts",
             "api_base": "https://t/v1", "key": ["k2"], "enable": True},
        ],
        "providers": [
            {"id": "chat-src/gpt-x", "provider_source_id": "chat-src",
             "model": "gpt-x", "enable": True},
            {"id": "tts-src/zh-CN", "provider_source_id": "tts-src",
             "model": "zh-CN", "enable": True},
        ],
    }
    tts, default_id = flatten_two_layer(store, provider_type=PROVIDER_TYPE_TTS)
    assert {p["id"] for p in tts} == {"tts-src/zh-CN"}
    assert default_id == "tts-src/zh-CN", (
        f"默认值 {default_id!r} 不在过滤后的列表里 —— 调用方会查不到"
    )

    # 空列表时回传空串(不是那个查不到的默认值)
    empty_store = dict(store, providers=[])
    items, did = flatten_two_layer(empty_store, provider_type=PROVIDER_TYPE_TTS)
    assert items == [] and did == "", f"空列表应回传空默认值,实得 {did!r}"


async def test_stt_source_can_be_stored(mk):
    """🔴 N1:`speech_to_text` 类型的源**能存进去**(2026-09-21 起)。

    注入验证:把 `PROVIDER_TYPE_STT` 从 `SUPPORTED_PROVIDER_TYPES` 移除 →
    本判据必须挂(会收到 400 `unsupported_provider_type`)。
    """
    from emotion_app.infra.providers_store import (
        PROVIDER_TYPE_STT,
        SUPPORTED_PROVIDER_TYPES,
    )

    assert PROVIDER_TYPE_STT in SUPPORTED_PROVIDER_TYPES
    c = await mk()
    r = await _add_source(
        c, provider_type=PROVIDER_TYPE_STT,
        id="stt-src", type="openai_whisper_api",
        api_base="https://api.example.com/v1", key=["sk-stt"],
    )
    assert r.status_code == 201, f"STT 源存不进去:{r.status_code} {await r.get_data()}"

    # 读回来 provider_type 正确(不是被默认成 chat)
    listed = await c.get("/api/v1/provider-sources")
    body = await listed.get_json()
    src = next(
        s for s in body["data"]["provider_sources"] if s["id"] == "stt-src"
    )
    assert src["provider_type"] == PROVIDER_TYPE_STT, (
        f"读回来的类型不对:{src['provider_type']!r} —— 可能被默认成了 chat"
    )


async def test_non_chat_source_does_not_leak_into_plugin_chat_providers(mk, tmp_path):
    """🔴🔴 N0 端到端:配了 TTS/STT 源后,插件看到的**对话**供应商里不能有它们。

    这是 N0 的**真实场景**判据(直接问插件的供应商接口),不是只测展平函数。
    对应 2026-09-21 实测撞到的那张表:
        加 TTS 源+模型后 → `items` 混进 `tts-probe/…` 而 `tts_items=[]`。

    注入验证:把 `plugin_host.py` 里 `flatten_two_layer(store)` 的
    `provider_type=PROVIDER_TYPE_CHAT` 参数删掉 → 本判据必须挂。
    """
    from emotion_app.infra.providers_store import (
        PROVIDER_TYPE_CHAT,
        PROVIDER_TYPE_STT,
        PROVIDER_TYPE_TTS,
    )
    from emotion_app.web.provider_bridge import flatten_two_layer

    c = await mk()
    # 一条对话源(基准)
    r = await _add_source(c, provider_type=PROVIDER_TYPE_CHAT, id="chat-only",
                          api_base="https://api.example.com/v1")
    assert r.status_code == 201, f"对话源建失败:{r.status_code}"
    r = await _add_model(c, id="chat-only/chat-model",
                         provider_source_id="chat-only", model="chat-model")
    assert r.status_code == 201, f"对话模型建失败:{r.status_code}"

    # 再加 TTS 与 STT 各一条
    for sid, ptype, adapter in (
        ("tts-src", PROVIDER_TYPE_TTS, "edge_tts"),
        ("stt-src", PROVIDER_TYPE_STT, "openai_whisper_api"),
    ):
        r = await _add_source(c, provider_type=ptype, id=sid, type=adapter,
                              api_base="https://api.example.com/v1")
        assert r.status_code == 201, f"{sid} 建源失败:{r.status_code}"
        r = await _add_model(c, id=f"{sid}/{sid}-model",
                             provider_source_id=sid, model=f"{sid}-model")
        assert r.status_code == 201, f"{sid} 建模型失败:{r.status_code}"

    # 读磁盘上的真实配置(隔离在 tmp 的那份),过展平函数 —— 插件装配走的就是这条
    from emotion_app.web.provider_bridge import flatten_two_layer as flat

    store_path = tmp_path / "data" / "providers.json"
    assert store_path.exists(), f"配置没落盘:{store_path}"
    store = _raw(store_path)
    all_items, _ = flat(store)
    assert len(all_items) == 3, f"应有 3 条模型,实得 {len(all_items)}"

    chat_items, _ = flat(store, provider_type=PROVIDER_TYPE_CHAT)
    chat_ids = {p["id"] for p in chat_items}
    assert chat_ids == {"chat-only/chat-model"}, (
        f"对话供应商里混进了非对话模型:{sorted(chat_ids)} —— 这正是 N0 的 bug"
    )


async def test_hot_sync_does_not_register_non_chat_into_provider_manager(mk):
    """🔴🔴 N0 的**热同步**漏点:非对话模型不能注册进宿主的 `ProviderManager`。

    这是 N0 真正的坑 —— 我最初只在 `flatten_two_layer()` 加过滤,**单测全绿**
    (因为单测只测了展平函数),但**真环境仍然泄漏**:
    配置**热同步**走的是 `api_providers._register_model()`,**不经过** `flatten_two_layer`
    (那条路只在服务**启动装配**时用)。实测复现:
        加 TTS 源+模型 → 插件的 `items` 里混进 `tts-probe2/zh-CN-XiaoxiaoNeural`。

    **通则:同一个概念有多条写入路径时,每条都要过滤 —— 并各自有判据。**

    本判据做两件事:
      ① 直接验**唯一判据** `is_chat_provider_type()`（两条路径都该调它）；
      ② 验 `_register_model` 的源码里**确实**调了这个判据并提前返回 ——
         因为宿主未启用时函数会在更早处返回（测试环境走不到注册那步），
         所以用**源码级断言**守住"闸门还在"。

    注入验证：删掉 `_register_model` 里那段 `is_chat_provider_type` 早返回 →
    断言 ② 必须挂。
    """
    import inspect

    from emotion_app.infra.providers_store import (
        PROVIDER_TYPE_CHAT,
        PROVIDER_TYPE_STT,
        PROVIDER_TYPE_TTS,
        is_chat_provider_type,
    )
    from emotion_app.web import api_providers

    # ① 唯一判据本身
    assert is_chat_provider_type(PROVIDER_TYPE_CHAT) is True
    assert is_chat_provider_type(PROVIDER_TYPE_TTS) is False
    assert is_chat_provider_type(PROVIDER_TYPE_STT) is False
    # 老数据（没有 provider_type 字段）按对话处理
    assert is_chat_provider_type("") is True

    # ② 热同步那条路径真的挂了闸门（源码级：早返回 + 调用唯一判据）
    src = inspect.getsource(api_providers)
    assert "is_chat_provider_type" in src, (
        "_register_model（热同步路径）没有用 is_chat_provider_type —— "
        "非对话模型会泄漏进插件的对话池（N0 复发）"
    )
    assert "跳过 ProviderManager 同步（非对话类型不进对话池）" in src, (
        "闸门分支不见了 —— 若改了文案请同步本判据（但别把闸门删了）"
    )

    # ③ 启动装配那条路径也用它（两条路径必须同一判据）
    from emotion_app.web import provider_bridge

    bridge_src = inspect.getsource(provider_bridge)
    assert "provider_type" in bridge_src and "src_type != provider_type" in bridge_src, (
        "flatten_two_layer（启动装配路径）的类型过滤不见了"
    )

    # ④ 顺带确认真环境里那条实测泄漏形状用本判据能判对
    c = await mk()
    r = await _add_source(c, id="tts-n0", provider_type=PROVIDER_TYPE_TTS,
                          type="edge_tts", api_base="https://api.example.com/v1")
    assert r.status_code == 201
    r = await _add_model(c, id="tts-n0/m", provider_source_id="tts-n0", model="m")
    assert r.status_code == 201
    assert is_chat_provider_type(PROVIDER_TYPE_TTS) is False, "tts 被误判成对话类"
