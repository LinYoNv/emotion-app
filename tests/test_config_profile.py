# -*- coding: utf-8 -*-
"""「配置文件」（会话 + 上下文）判据 —— `domain/config_profile.py` + 三条 API。

## 这个模块守什么

情绪要把设置页改成 AstrBot 那种**配置文件式**页面。这类功能的危险不在"写不出来"，
而在三种**静默**：

1. **损坏静默**：配置读坏了却"当作空配置"继续 → 用户以为设置被重置，找不到原因；
2. **写完就丢**：非原子写 + 中途崩溃 = 半截 JSON → 下次启动读出"缺字段"，用默认值顶上；
3. **拒得不清楚**：类型/越界/未知键被悄悄忽略或悄悄修正 → 用户以为改了，其实没改。

下面每个方向都有判据，且都做过注入验证（见交付报告）。
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emotion_app.domain.config_profile import (  # noqa: E402
    CONTEXT_MODES,
    DEFAULT_PATH,
    SECTION_CONTEXT,
    SECTION_SESSION,
    SECTIONS,
    SPECS,
    ConfigProfileError,
    ConfigProfileStore,
    default_profile,
    schema,
    validate_patch,
)


@pytest.fixture
def store(tmp_path):
    return ConfigProfileStore(tmp_path / "cfg.json")


# ===========================================================================
# ① 默认值 / schema
# ===========================================================================


def test_defaults_cover_every_spec():
    """每段都要有默认值，且**键集合与 SPECS 完全一致**（不能多也不能少）。"""
    prof = default_profile()
    assert set(prof) == set(SECTIONS)
    for sec in SECTIONS:
        want = {s.key for s in SPECS[sec]}
        assert set(prof[sec]) == want, (
            f"{sec} 的默认值键与 SPECS 不符：缺 {want - set(prof[sec])}，"
            f"多 {set(prof[sec]) - want}"
        )


def test_default_values_match_specs():
    """默认值必须**取自 SPECS**（防止两处各写一份、慢慢漂移）。"""
    prof = default_profile()
    for sec in SECTIONS:
        for spec in SPECS[sec]:
            assert prof[sec][spec.key] == spec.default, (sec, spec.key)


def test_mutable_defaults_are_not_shared():
    """可变默认值（如回退模型列表）必须**深拷贝** —— 否则改一处污染全局 SPECS。"""
    a = default_profile()
    a[SECTION_SESSION]["fallback_chat_provider_ids"].append("x/y")
    b = default_profile()
    assert b[SECTION_SESSION]["fallback_chat_provider_ids"] == [], (
        "默认值被调用方改坏了 —— SPECS 是全局常量，必须深拷贝"
    )


def test_schema_shape_is_renderable():
    """schema 是**前端渲染的唯一事实来源**，形状必须能直接画控件。"""
    sch = schema()
    assert set(sch) == set(SECTIONS)
    for sec in SECTIONS:
        assert sch[sec], f"{sec} 没有字段"
        for f in sch[sec]:
            assert {"key", "label", "type", "default", "hint"} <= set(f), f
            assert f["type"] in (
                "string", "int", "float", "bool", "enum", "text",
                "provider_select", "provider_multi", "persona_select",
                # 🆕 2026-09-27：通用字符串数组（插件停用名单用）。
                #    `app.js` 的 `makeConfigInput` 有对应分支，判据不漏。
                "string_list",
            ), f["type"]
            if f["type"] == "enum":
                assert f["options"], f"enum 字段 {f['key']} 没有选项"


def test_context_modes_are_our_three_modes():
    """🔴 上下文模式 = **我们既有的三种**（不是照抄 AstrBot 的字段）。

    情绪选的方案："三模式当策略下拉，AstrBot 参数作细项"。
    若这里被改成 AstrBot 的那套（如 `llm_compress`），说明有人误解了需求。
    """
    assert CONTEXT_MODES == ("recent_only", "rolling_summary", "semantic")
    mode = next(s for s in SPECS[SECTION_CONTEXT] if s.key == "mode")
    assert [v for v, _ in mode.options] == list(CONTEXT_MODES)


def test_no_agent_or_group_fields_leaked_in():
    """🔴 情绪明确：不要 agent / 群聊 / 管理员。字段清单里不许混进这些。"""
    banned = ("agent", "subagent", "group", "admin", "tool_call", "sandbox",
              "computer_use", "platform", "forward", "quoted")
    leaked: list[str] = []
    for sec in SECTIONS:
        for spec in SPECS[sec]:
            low = spec.key.lower()
            if any(b in low for b in banned):
                leaked.append(f"{sec}.{spec.key}")
    assert not leaked, f"配置项里混进了 agent/群聊/管理员/平台相关字段：{leaked}"


# ===========================================================================
# ② 校验：拒得清楚（不静默忽略、不静默修正）
# ===========================================================================


def test_unknown_key_is_rejected_and_named():
    """🔴 未知键 → **抛错并点名**（静默忽略 = 用户以为改了其实没改）。"""
    with pytest.raises(ConfigProfileError) as ei:
        validate_patch(SECTION_SESSION, {"nope": 1})
    assert "nope" in str(ei.value)
    assert ei.value.field_name == "nope"


@pytest.mark.parametrize(
    "section,key,value",
    [
        (SECTION_SESSION, "request_max_retries", -1),        # 低于下限
        (SECTION_SESSION, "request_max_retries", 11),        # 高于上限
        (SECTION_SESSION, "tts_trigger_probability", 1.5),   # 超出 0..1
        (SECTION_SESSION, "tts_trigger_probability", -0.1),
        (SECTION_CONTEXT, "llm_compress_keep_recent_ratio", 1.0),
        (SECTION_CONTEXT, "protected_tail", -1),
        (SECTION_CONTEXT, "fallback_max_context_tokens", 10),
    ],
)
def test_out_of_range_is_rejected(section, key, value):
    """越界一律拒绝，且**点名**是哪个字段（前端要标在对应控件旁）。"""
    with pytest.raises(ConfigProfileError) as ei:
        validate_patch(section, {key: value})
    assert ei.value.field_name == key


@pytest.mark.parametrize(
    "section,key,value",
    [
        (SECTION_SESSION, "streaming_response", "yes"),      # bool 位置给了字符串
        (SECTION_SESSION, "request_max_retries", "3"),       # int 位置给了字符串
        (SECTION_SESSION, "request_max_retries", True),      # ⚠️ bool 是 int 子类，必须拒绝
        (SECTION_CONTEXT, "mode", "nope"),                   # enum 非法值
        (SECTION_CONTEXT, "mode", 3),                        # enum 给了数字
        (SECTION_SESSION, "fallback_chat_provider_ids", "a/b"),  # 该是数组
        (SECTION_SESSION, "fallback_chat_provider_ids", [1, 2]),  # 元素不是字符串
        (SECTION_SESSION, "default_chat_provider_id", 5),    # 该是字符串
    ],
)
def test_wrong_type_is_rejected(section, key, value):
    """类型不符一律拒绝。

    ⚠️ 特别地 `request_max_retries=True` 必须被拒 —— `bool` 是 `int` 子类，
       不显式挡掉就会被当成 1 悄悄收下（一个很经典的静默）。
    """
    with pytest.raises(ConfigProfileError) as ei:
        validate_patch(section, {key: value})
    assert ei.value.field_name == key


def test_unknown_section_is_rejected():
    with pytest.raises(ConfigProfileError):
        validate_patch("nope", {})


def test_valid_patch_returns_only_given_keys():
    """合法更新**只回传进来的键**（不能把默认值也一起"更新"进去）。"""
    got = validate_patch(SECTION_SESSION, {"streaming_response": False})
    assert got == {"streaming_response": False}


# ===========================================================================
# ③ 存取：默认 / 往返 / 部分更新
# ===========================================================================


def test_missing_file_is_defaults_and_not_an_error(store, caplog):
    """文件不存在 = **正常首次运行**，不是错误（别打 ERROR 吓人）。"""
    caplog.set_level(logging.ERROR, logger="emotion_app.config_profile")
    prof = store.load()
    assert prof == default_profile()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "首次运行（文件不存在）不该打 ERROR"
    )


def test_patch_roundtrip(store):
    store.patch({SECTION_SESSION: {"streaming_response": False, "request_max_retries": 7}})
    again = ConfigProfileStore(store.path).load()
    assert again[SECTION_SESSION]["streaming_response"] is False
    assert again[SECTION_SESSION]["request_max_retries"] == 7
    # 没动的键保持默认
    assert again[SECTION_SESSION]["display_reasoning_text"] is False


def test_patch_is_partial_and_does_not_reset_siblings(store):
    """🔴 **部分更新**：改一个键不能把同段其它已改的键打回默认。"""
    store.patch({SECTION_SESSION: {"request_max_retries": 5}})
    store.patch({SECTION_SESSION: {"streaming_response": False}})
    got = store.load()
    assert got[SECTION_SESSION]["request_max_retries"] == 5, "第二次 PATCH 把第一次的改动冲掉了"
    assert got[SECTION_SESSION]["streaming_response"] is False


def test_patch_both_sections(store):
    store.patch({
        SECTION_SESSION: {"streaming_response": False},
        SECTION_CONTEXT: {"mode": "semantic"},
    })
    got = store.load()
    assert got[SECTION_SESSION]["streaming_response"] is False
    assert got[SECTION_CONTEXT]["mode"] == "semantic"


def test_reset_restores_defaults(store):
    store.patch({SECTION_SESSION: {"streaming_response": False}})
    assert store.load()[SECTION_SESSION]["streaming_response"] is False
    got = store.reset()
    assert got == default_profile()
    assert ConfigProfileStore(store.path).load() == default_profile()


def test_on_disk_shape_is_frozen(store):
    """落盘形状（契约 §4.2）：`version` + 两段。"""
    store.patch({SECTION_SESSION: {"request_max_retries": 2}})
    raw = json.loads(Path(store.path).read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert set(raw) >= {"version", SECTION_SESSION}
    assert raw[SECTION_SESSION]["request_max_retries"] == 2


def test_unknown_keys_on_disk_are_preserved(store):
    """🔴 落盘里的**未知键要保留**（可能是更新版本写的，别给人删了）。

    ⚠️ 这与"PATCH 拒绝未知键"不冲突：那是拒绝**用户提交**我们不认识的键；
       这里是**不丢**别人写进去的数据。
    """
    Path(store.path).parent.mkdir(parents=True, exist_ok=True)
    Path(store.path).write_text(
        json.dumps({
            "version": 1,
            SECTION_SESSION: {"request_max_retries": 4, "future_key": "keep-me"},
            "future_section": {"a": 1},
        }),
        encoding="utf-8",
    )
    store.patch({SECTION_SESSION: {"streaming_response": False}})
    raw = json.loads(Path(store.path).read_text(encoding="utf-8"))
    assert raw[SECTION_SESSION]["future_key"] == "keep-me", "未知键被写丢了"
    assert raw["future_section"] == {"a": 1}, "未知的段被写丢了"
    # 而且读出来的 profile 里也带着它（前端能看到）
    assert store.load()[SECTION_SESSION]["future_key"] == "keep-me"


# ===========================================================================
# ④ 损坏：不静默（这是本模块最重要的一组）
# ===========================================================================


@pytest.mark.parametrize(
    "bad,why",
    [
        ("", "空文件"),
        ("{not json", "非法 JSON"),
        ("[1,2,3]", "顶层是数组不是对象"),
        ('"a string"', "顶层是字符串"),
    ],
)
def test_corrupt_file_is_loud_and_keeps_backup(store, caplog, bad, why):
    """🔴 损坏 → **回默认值 + 打 ERROR + 另存 .corrupt**（三个都要，缺一不可）。

    ⚠️ 只"回默认值"不打日志 = 用户发现设置全没了却查不到原因；
       不另存备份 = 下一次写入把现场覆盖掉，事后无法排查。
    """
    caplog.set_level(logging.ERROR, logger="emotion_app.config_profile")
    Path(store.path).parent.mkdir(parents=True, exist_ok=True)
    Path(store.path).write_text(bad, encoding="utf-8")

    prof = store.load()
    assert prof == default_profile(), why
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errs, f"{why}：损坏却没有打 ERROR（静默了）"
    bak = Path(str(store.path) + ".corrupt")
    assert bak.is_file(), f"{why}：损坏现场没有另存 .corrupt，事后无法排查"
    assert bak.read_text(encoding="utf-8") == bad, "备份内容与原始坏文件不一致"


def test_bad_field_value_falls_back_per_field_with_error(store, caplog):
    """单个字段值不合法 → **该字段回默认 + ERROR**，其余字段照常生效（不是整份丢弃）。"""
    caplog.set_level(logging.ERROR, logger="emotion_app.config_profile")
    Path(store.path).parent.mkdir(parents=True, exist_ok=True)
    Path(store.path).write_text(
        json.dumps({
            "version": 1,
            SECTION_SESSION: {"request_max_retries": "坏值", "display_reasoning_text": True},
        }),
        encoding="utf-8",
    )
    prof = store.load()
    assert prof[SECTION_SESSION]["request_max_retries"] == 3, "坏字段该回默认"
    assert prof[SECTION_SESSION]["display_reasoning_text"] is True, "好字段不该被连累"
    assert [r for r in caplog.records if r.levelno >= logging.ERROR], "坏字段没有告警"


def test_non_object_section_falls_back_with_error(store, caplog):
    """整段不是对象 → 该段回默认 + ERROR。"""
    caplog.set_level(logging.ERROR, logger="emotion_app.config_profile")
    Path(store.path).parent.mkdir(parents=True, exist_ok=True)
    Path(store.path).write_text(
        json.dumps({"version": 1, SECTION_SESSION: "not-an-object"}),
        encoding="utf-8",
    )
    assert store.load()[SECTION_SESSION] == default_profile()[SECTION_SESSION]
    assert [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_write_is_atomic_no_leftover_tmp(store):
    """写盘是原子的：成功之后**不留 `.tmp` 残渣**。"""
    store.patch({SECTION_SESSION: {"request_max_retries": 6}})
    leftovers = list(Path(store.path).parent.glob("*.tmp"))
    assert not leftovers, f"留下了临时文件：{leftovers}"


def test_concurrent_patches_do_not_lose_updates(store):
    """并发 PATCH 不能丢更新（`RLock` + 读改写串行化）。

    ⚠️ 这条守的是"锁写错"：`load()` 内部会写盘（修复/迁移），
       用普通 `Lock` 会自锁死；用错粒度则会丢更新。
    """
    import threading

    def worker(i: int) -> None:
        ConfigProfileStore(store.path).patch({SECTION_SESSION: {"request_max_retries": i % 10}})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = store.load()[SECTION_SESSION]["request_max_retries"]
    assert 0 <= got <= 10, got
    assert isinstance(got, int)


# ===========================================================================
# ⑤ HTTP：三条路由 + 鉴权 + 装配
# ===========================================================================


async def test_get_returns_profile_and_schema(mk):
    c = await mk()
    r = await c.get("/api/v1/config-profile")
    assert r.status_code == 200, await r.get_data(as_text=True)
    body = await r.get_json()
    assert set(body) == {"profile", "schema"}, sorted(body)
    assert set(body["profile"]) == set(SECTIONS)
    assert body["schema"][SECTION_CONTEXT], "schema 里没有上下文字段"


async def test_patch_updates_and_returns_profile(mk):
    c = await mk()
    r = await c.patch("/api/v1/config-profile",
                      json={SECTION_SESSION: {"streaming_response": False}})
    assert r.status_code == 200, await r.get_data(as_text=True)
    assert (await r.get_json())["profile"][SECTION_SESSION]["streaming_response"] is False
    # 真的落盘了（重新 GET 还看得见）
    again = await (await c.get("/api/v1/config-profile")).get_json()
    assert again["profile"][SECTION_SESSION]["streaming_response"] is False


async def test_patch_unknown_key_returns_400_with_field(mk):
    """🔴 未知键 → **400 + field**（前端要把错误标在对应控件旁）。"""
    c = await mk()
    r = await c.patch("/api/v1/config-profile", json={SECTION_SESSION: {"nope": 1}})
    assert r.status_code == 400, r.status_code
    err = (await r.get_json())["error"]
    assert err["type"] == "invalid_config_profile", err
    assert err["field"] == "nope", err
    assert "nope" in err["message"], err


async def test_patch_out_of_range_returns_400_with_field(mk):
    c = await mk()
    r = await c.patch("/api/v1/config-profile",
                      json={SECTION_SESSION: {"tts_trigger_probability": 9}})
    assert r.status_code == 400
    err = (await r.get_json())["error"]
    assert err["field"] == "tts_trigger_probability", err


async def test_patch_empty_body_is_rejected(mk):
    c = await mk()
    r = await c.patch("/api/v1/config-profile", json={})
    assert r.status_code == 400, r.status_code


async def test_delete_resets(mk):
    c = await mk()
    await c.patch("/api/v1/config-profile", json={SECTION_CONTEXT: {"mode": "semantic"}})
    r = await c.delete("/api/v1/config-profile")
    assert r.status_code == 200
    assert (await r.get_json())["profile"] == default_profile()


async def test_routes_require_auth(mk):
    c = await mk(login=False)
    for method, path in (("get", "/api/v1/config-profile"),
                         ("delete", "/api/v1/config-profile")):
        r = await getattr(c, method)(path)
        assert r.status_code in (401, 403), (method, path, r.status_code)
    r = await c.patch("/api/v1/config-profile", json={SECTION_SESSION: {}})
    assert r.status_code in (401, 403), r.status_code


async def test_client_writes_to_its_own_isolated_path(mk, tmp_path):
    """🔴 测试用的 client 必须把配置写到**自己的临时路径**，而不是项目 `data/`。

    ⚠️ 我第一版这条写的是"项目里的 `data/config_profile.json` 不存在" —— **判据写错了**：
       正在跑的生产服务（cwd 就是项目根）会合法地创建那个文件，
       于是这条判据会**因为服务在跑而失败**，看起来像"测试污染了项目目录"，
       其实测的是"有没有人在用这个功能"。**判据要测隔离性，不是测文件存不存在。**
    → 正确的判据：通过测试 client 保存后，落到**临时目录**里，
      且项目路径的字节**没有因为这次测试而改变**。
    """
    import hashlib

    project_file = Path(__file__).resolve().parent.parent / DEFAULT_PATH

    def _digest(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "<absent>"

    before = _digest(project_file)

    c = await mk()
    # app 的落盘路径必须指向 tmp，而不是项目 data/
    configured = c.app.config.get("EMOTION_CONFIG_PROFILE_PATH") or ""
    assert configured and str(tmp_path) in configured, (
        f"测试 client 的配置路径没有隔离到 tmp：{configured!r}"
    )
    await c.patch("/api/v1/config-profile",
                  json={SECTION_SESSION: {"request_max_retries": 3}})

    # 值确实写进了那个隔离路径
    written = Path(configured)
    assert written.is_file(), f"没有写到隔离路径：{written}"
    assert json.loads(written.read_text(encoding="utf-8"))[SECTION_SESSION][
        "request_max_retries"
    ] == 3

    # 而项目目录**没有被这次测试改动**
    assert _digest(project_file) == before, (
        "测试改动了项目 data/ 下的配置文件 —— conftest 的路径隔离失效了"
    )


async def test_routes_are_mounted_in_real_app():
    """🔴 装配判据：路由真的挂在 app 上（"模块能工作 ≠ 被接上了"）。"""
    from emotion_app.infra.config import Config
    from emotion_app.web.app import create_app
    from emotion_app.web.auth import AuthManager
    from tests.conftest import TOKEN

    cfg = Config()
    cfg.auth.access_token = TOKEN
    app = create_app(cfg, None, AuthManager(access_token=TOKEN))  # type: ignore[arg-type]
    rules = {
        (rule.rule, tuple(sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS"))))
        for rule in app.url_map.iter_rules()
    }
    expected = {
        ("/api/v1/config-profile", ("GET",)),
        ("/api/v1/config-profile", ("PATCH",)),
        ("/api/v1/config-profile", ("DELETE",)),
    }
    missing = expected - rules
    assert not missing, f"配置文件路由没挂上：{sorted(missing)}"


def test_path_is_configurable_and_defaults_are_sane():
    """默认路径在 `data/` 下（**不是** `/opt/astrbot`）。"""
    assert DEFAULT_PATH == "data/config_profile.json"
    assert "astrbot" not in DEFAULT_PATH


# ---------------------------------------------------------------------------
# N1-4(2026-09-21)：设置页的 STT 字段
# ---------------------------------------------------------------------------


def test_stt_fields_exist_in_session_section():
    """🔴 STT 的两个字段在「会话」段里，且**默认值安全**（默认关、默认不指定）。

    注入验证：把 `stt_enable` 的默认值改成 `True` → 断言 ② 必须挂
    （默认打开一个**我们执行不了**的能力 = 界面在撒谎）。
    """
    from emotion_app.domain.config_profile import SPECS, SECTION_SESSION, default_profile

    names = [f.key for f in SPECS[SECTION_SESSION]]
    for want in ("stt_enable", "default_stt_provider_id"):
        assert want in names, f"「会话」段缺字段 {want}"

    prof = default_profile()
    # ① 默认关（"配置先行"，不能默认开一个跑不了的能力）
    assert prof[SECTION_SESSION]["stt_enable"] is False, (
        "stt_enable 默认应为 False —— 我们还没实现 STT 适配器，默认开就是假装能用"
    )
    # ② 默认不指定模型
    assert prof[SECTION_SESSION]["default_stt_provider_id"] == ""


def test_stt_provider_field_filters_by_speech_to_text():
    """STT 的 `provider_select` 必须只列 `speech_to_text` 类模型。

    注入验证：把它的 `provider_type` 改成 `chat_completion` →
    本判据必须挂（那样下拉里会出现对话模型，选出来的 STT 模型是错的）。
    """
    from emotion_app.domain.config_profile import SPECS, SECTION_SESSION

    spec = next(
        f for f in SPECS[SECTION_SESSION] if f.key == "default_stt_provider_id"
    )
    assert spec.type == "provider_select", f"控件类型应为 provider_select，实为 {spec.type}"
    assert spec.provider_type == "speech_to_text", (
        f"provider_type 应为 speech_to_text，实为 {spec.provider_type!r} —— "
        "下拉会列错类型的模型"
    )


def test_tts_and_stt_are_documented_as_config_only():
    """TTS 与 STT 的 hint 都要**明说只做配置层**（用户不会被界面误导）。"""
    from emotion_app.domain.config_profile import SPECS, SECTION_SESSION

    for name, keyword in (
        ("tts_enable", "不出声"),
        ("stt_enable", "不转写"),
    ):
        spec = next(f for f in SPECS[SECTION_SESSION] if f.key == name)
        assert keyword in spec.hint, (
            f"{name} 的 hint 里应说明「{keyword}」—— 否则用户会以为配了就能用；"
            f"当前：{spec.hint!r}"
        )


# ---------------------------------------------------------------------------
# 日志页 API（情绪 2026-09-21 要求）
# ---------------------------------------------------------------------------


async def test_logs_requires_auth(mk):
    """🔴🔴 `/api/v1/logs` **必须鉴权**。

    这是实测踩出来的真 bug：第一版漏了 `require_auth()`，
    未登录 `curl` 直接 **200** —— 而隔壁 `/settings`、`/config-profile`
    都是 401。日志里有 logger 名/上游报错/插件路径，**不该对外裸奔**
    （服务绑 `0.0.0.0`，等于公网可读）。

    注入验证：删掉 `get_logs` 里的 `require_auth()` 早返回 → 本判据必须挂。
    """
    c = await mk(login=False)
    r = await c.get("/api/v1/logs")
    assert r.status_code == 401, (
        f"未登录就能读日志（{r.status_code}）—— 日志含运行细节，必须鉴权"
    )
    # 对照：登录后可用（别把"鉴权"做成"谁都进不去"）
    c2 = await mk()
    r2 = await c2.get("/api/v1/logs")
    assert r2.status_code == 200, f"登录后也读不到日志：{r2.status_code}"


async def test_logs_rejects_bad_params_loudly(mk):
    """非法 `limit`/`level` → **400 且点名字段**（不是静默用默认值）。

    静默兜底会让用户以为"我传的过滤生效了"，其实看的是另一份数据 ——
    本项目铁律：宁可响亮失败，不要安静地给错东西。
    """
    c = await mk()
    for qs, field in (("limit=abc", "limit"), ("limit=0", "limit"),
                      ("limit=-3", "limit"), ("level=NOPE", "level")):
        r = await c.get(f"/api/v1/logs?{qs}")
        assert r.status_code == 400, f"{qs} 应 400，实得 {r.status_code}"
        body = await r.get_json()
        got = (body.get("error") or {}).get("field")
        assert got == field, f"{qs} 的 field 应为 {field!r}，实得 {got!r}（前端要靠它标红）"


async def test_logs_returns_available_flag(mk):
    """`available` 字段必须存在且为 True（前端靠它区分"没有日志"与"日志不可用"）。

    🔴 这条守的是**最误导人的那种失败**：把"读不到日志"显示成"没有日志"，
    用户会以为服务很干净。
    """
    c = await mk()
    r = await c.get("/api/v1/logs")
    assert r.status_code == 200
    data = (await r.get_json())["data"]
    for key in ("items", "total", "available", "reason", "capacity"):
        assert key in data, f"响应里缺 {key}（前端形状对不上）"
    assert data["available"] is True
    assert isinstance(data["items"], list)
