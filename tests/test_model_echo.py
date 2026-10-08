# -*- coding: utf-8 -*-
"""响应体里回显的模型名，必须**等于实际发出去的那个**（情绪 2026-09-27 报障）。

## 报障
「我切了对话模型怎么聊天那还是用着 `gemini-3.1-flash-lite`。」

## 真因（实测锁定）
**实际请求上游用的是新模型，回显的是旧名字。** 两处各自解析，各说各话：

| 位置 | 取值方式 | 结果 |
|---|---|---|
| 真正发请求 `chat.py:322 resolve_target` | 人设覆盖 > 请求指定 > **运行时设置** > 环境变量 | ✅ 新模型 |
| 响应体回显（`app.py` 4 处） | `req.model or config.upstream.model` | ❌ **启动快照**（旧模型） |

`config` 是服务启动那一刻读 `.env` 的结果；用户在面板改的模型落在**运行时设置**
（`EMOTION_SETTINGS`）里 ⇒ `config.upstream.model` **永远是旧值**。

真机取证：面板切到 `gemini-3.8-flash-high` 后，
服务端日志 `调用上游(流式) {"model": "gemini-3.8-flash-high"}`（**真的在用新模型**），
而 SSE 里每个 chunk 都是 `"model": "gemini-3.1-flash-lite"`（**假的标签**）。

🔴 **为什么这是最坏的那类 bug**：模型标签**唯一**的用途就是告诉用户
"这条回复是谁生成的"。它说错话时，用户会去改一个**本来就对**的地方
（情绪就是这么来报障的），而真因在另一个文件里 —— 与「错误信息会撒谎」同源。

## 判据（每条都能失败）
  ① `GET /v1/models` 回**运行时设置**里的模型（不是启动快照）
  ② **非流式**响应的 `model` == 运行时设置
  ③ **流式**每个 chunk 的 `model` == 运行时设置（面板**永远走流式**）
  ④ 改了设置之后回显**跟着变**（防恒真：证明取的不是启动值）
  ⑤ 调用方显式传 `model` 时，回显它自己那个（OpenAI 语义不许改坏）
  ⑥ 源码级：`app.py` 里不许再有 `config.upstream.model` 当回显值
     （这次就是 4 条写入路径里有 3 条写错）
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from emotion_app.domain.backends import OpenAIChatBackend
from emotion_app.domain.chat import ChatService
from emotion_app.domain.persona_repo import PersonaRepository
from emotion_app.domain.settings import SettingsService
from emotion_app.infra.config import Config
from emotion_app.storage.sqlite_store import SqliteStore
from emotion_app.web.app import create_app
from emotion_app.web.auth import AuthManager

TOKEN = "t" * 20
#: 启动配置里的模型（模拟 `.env` 的旧值 —— 就是情绪看到那个）
BOOT_MODEL = "gemini-3.1-flash-lite"
#: 用户在面板切成的模型
RUNTIME_MODEL = "gemini-3.8-flash-high"

def _requested_model(req: httpx.Request) -> str:
    """从**请求体**里取调用方要的模型名。

    🔴 为什么 mock 必须回显请求里的模型（而不是写死一个）：
       真机实测（2026-09-27，`api.syuan.org`）—— 上游响应里的 `model`
       是**回显我们发过去的那个**：
           POST model=gemini-3.8-flash-high   → 响应 model=gemini-3.8-flash-high
           POST model=gemini-3.1-flash-lite   → 响应 model=gemini-3.1-flash-lite
       mock 若写死一个模型名，就会把「调用方显式指定 model」那条路径测成**假红**
       （实测踩到：第一版写死 RUNTIME_MODEL ⇒ 判据⑤ 报"被吃掉"，产品其实是对的）。
    """
    try:
        return str(json.loads(req.content.decode("utf-8")).get("model") or "")
    except Exception:  # noqa: BLE001
        return ""


def _ok(req: httpx.Request) -> httpx.Response:
    """非流式 mock 上游（照真实上游行为：回显请求里的 model）。"""
    return httpx.Response(200, json={
        "model": _requested_model(req),
        "choices": [{"message": {"content": "收到"}, "finish_reason": "stop"}],
    })


def _stream_ok(req: httpx.Request) -> httpx.Response:
    """流式 mock 上游（真实上游**每个 chunk** 都带 model）。"""
    m = json.dumps(_requested_model(req))

    def chunk(delta: str, finish: str | None) -> bytes:
        return (
            'data: {"model": ' + m + ', "choices":[{"delta":' + delta
            + ',"finish_reason":' + json.dumps(finish) + '}]}\n\n'
        ).encode()

    return httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        stream=httpx.ByteStream(b"".join([
            chunk('{"content":"你"}', None),
            chunk('{"content":"好"}', None),
            chunk("{}", "stop"),
            b"data: [DONE]\n\n",
        ])),
    )

async def _build(tmp_path: Path, db_name: str, transport):
    """造 app（真 ChatService + mock 上游），并把运行时模型切到 RUNTIME_MODEL。"""
    cfg = Config()
    cfg.auth.access_token = TOKEN
    cfg.storage.sqlite_path = str(tmp_path / db_name)
    cfg.upstream.base_url = "https://upstream.test/v1"
    cfg.upstream.api_key = "sk-env"
    cfg.upstream.model = BOOT_MODEL
    cfg.plugins.enabled = False

    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()
    settings = SettingsService(store, cfg)
    await settings.load()
    personas = PersonaRepository(store)
    # ⚠️ handler 签名是 `(Request) -> Response`；`_ok` 是内容工厂，要包一层 lambda
    #    （踩过：直接传 `_ok` 会把 Request 当 content 塞进 JSON → TypeError）
    upstream = OpenAIChatBackend(transport=httpx.MockTransport(transport))
    await upstream.open()
    chat = ChatService(cfg, upstream, settings=settings, personas=personas)
    app = create_app(cfg, store, AuthManager(access_token=TOKEN), chat,
                     personas=personas, settings=settings)
    client = app.test_client()
    await client.post("/api/v1/auth/login", json={"token": TOKEN})

    # 用户在面板切成新模型 → 落进**运行时设置**
    await settings.update({"upstream_model": RUNTIME_MODEL})

    # 🔴 前置断言：两边**必须不同**，否则这个 bug 测不出来（错误实现也会回对值）
    assert cfg.upstream.model == BOOT_MODEL, f"启动配置该是旧值，实际 {cfg.upstream.model!r}"
    assert settings.effective_upstream()["model"] == RUNTIME_MODEL, "运行时设置没生效"
    assert BOOT_MODEL != RUNTIME_MODEL

    return client, store, settings


# ===========================================================================
# ① /v1/models
# ===========================================================================


async def test_models_endpoint_returns_runtime_model(tmp_path):
    """① `GET /v1/models` 必须回**运行时设置**的模型。"""
    client, store, _ = await _build(tmp_path, "a.db", _ok)
    try:
        r = await client.get("/v1/models")
        assert r.status_code == 200, await r.get_data(as_text=True)
        d = await r.get_json()
        ids = [m["id"] for m in d["data"]]
        assert RUNTIME_MODEL in ids, f"`/v1/models` 回的是启动快照而不是运行时设置：{ids}"
        assert BOOT_MODEL not in ids, (
            f"`/v1/models` 还在报旧的启动模型 {BOOT_MODEL}（用户会以为切换没生效）"
        )
    finally:
        await store.close()


# ===========================================================================
# ② 非流式
# ===========================================================================


async def test_non_stream_response_echoes_runtime_model(tmp_path):
    """② **非流式**响应的 `model` 必须是运行时那个。"""
    client, store, _ = await _build(tmp_path, "b.db", _ok)
    try:
        r = await client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hi"}], "stream": False,
        })
        assert r.status_code == 200, await r.get_data(as_text=True)
        d = await r.get_json()
        assert d.get("model") == RUNTIME_MODEL, (
            f"响应回显 {d.get('model')!r}，但实际用的是 {RUNTIME_MODEL!r} —— "
            "用户会以为模型没切成功（情绪就是这么报障的）"
        )
    finally:
        await store.close()


# ===========================================================================
# ③ 流式（🔴 面板永远走这条）
# ===========================================================================


async def test_stream_chunks_echo_runtime_model(tmp_path):
    """③ **流式**每个 chunk 的 `model` 都必须是运行时那个。

    ⚠️ 流式有**两处** envelope 构造（普通 chunk + 图片 chunk）——
       只修非流式 = 用户看到的仍然是错的（面板**永远走流式**）。
    ⚠️ 本项目踩过一模一样的坑：第五轮"派发只写在 `if not req.stream:` 里"，
       而面板永远走流式 ⇒ 用户说的每句话插件都没收到过。
    """
    client, store, _ = await _build(tmp_path, "c.db", _stream_ok)
    try:
        r = await client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hi"}], "stream": True,
        })
        assert r.status_code == 200, await r.get_data(as_text=True)
        body = await r.get_data(as_text=True)
        models = [
            json.loads(ln[6:]).get("model")
            for ln in body.splitlines()
            if ln.startswith("data: ") and ln[6:].strip() not in ("[DONE]", "")
        ]
        assert models, f"没解析到任何 chunk：{body[:300]}"
        bad = sorted({m for m in models if m != RUNTIME_MODEL})
        assert not bad, (
            f"流式 chunk 回显了 {bad}，应为 {RUNTIME_MODEL!r} —— "
            "面板永远走流式，所以这条错了用户看到的就是错的"
        )
    finally:
        await store.close()


# ===========================================================================
# ④ 跟随变化（防恒真）
# ===========================================================================


async def test_echo_tracks_setting_change(tmp_path):
    """④ 改了设置之后回显**跟着变** —— 证明取的不是启动值。

    ⚠️ 这是上面几条的**防恒真**补充：若实现里两个模型名写反了、
       或启动值与运行值恰好相同，上面几条都会假绿。
    """
    client, store, settings = await _build(tmp_path, "d.db", _ok)
    try:
        r1 = await client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hi"}], "stream": False,
        })
        assert (await r1.get_json())["model"] == RUNTIME_MODEL

        # 再切成**第三个**模型（与启动值、上一个运行值都不同）
        await settings.update({"upstream_model": "gemini-9.9-probe"})
        r2 = await client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hi"}], "stream": False,
        })
        got = (await r2.get_json())["model"]
        assert got == "gemini-9.9-probe", (
            f"设置改了但回显没跟着变（{got!r}）—— 说明回显取的不是运行时设置"
        )
    finally:
        await store.close()


# ===========================================================================
# ⑤ 调用方显式指定仍然优先
# ===========================================================================


async def test_request_specified_model_still_wins(tmp_path):
    """⑤ 调用方**显式传 `model`** 时仍回显它自己那个（OpenAI 客户端语义）。

    ⚠️ 防"修 bug 改坏语义"：修复不能把调用方的显式指定吃掉。
    """
    client, store, _ = await _build(tmp_path, "e.db", _ok)
    try:
        r = await client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "model": "caller-picked-model",
        })
        assert r.status_code == 200, await r.get_data(as_text=True)
        got = (await r.get_json())["model"]
        assert got == "caller-picked-model", (
            f"调用方显式指定的 model 被吃掉了：{got!r}"
        )


    finally:
        await store.close()

async def test_stream_request_specified_model_still_wins(tmp_path):
    """⑤b **流式**路径下，调用方显式指定的 `model` 也要被回显。

    🔴 为什么必须**单独**测流式（真踩到）：`_effective_model` 的
       `if req_model: return req_model` 那个分支**只有流式路径会走到**
       （非流式走 `result.model`，由上游回显决定）。
       ⇒ 只测非流式的话，"忽略调用方指定"这个注入**抓不住**
         （第一版注入 ④ 就是这样：6 passed，看着像判据弱，其实是覆盖面不够）。
    """
    client, store, _ = await _build(tmp_path, "f.db", _stream_ok)
    try:
        r = await client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "model": "caller-picked-stream",
        })
        assert r.status_code == 200, await r.get_data(as_text=True)
        body = await r.get_data(as_text=True)
        models = sorted({
            json.loads(ln[6:]).get("model")
            for ln in body.splitlines()
            if ln.startswith("data: ") and ln[6:].strip() not in ("[DONE]", "")
        })
        assert models == ["caller-picked-stream"], (
            f"流式下调用方显式指定的 model 被吃掉了：{models}（应为 caller-picked-stream）"
        )
    finally:
        await store.close()


# ===========================================================================
# ⑥ 源码级：不许再有第二条写入路径
# ===========================================================================


def test_no_direct_config_model_echo_in_source():
    """⑥ 源码级：`app.py` 里不许再把 `config.upstream.model` 当**回显值**。

    这次 bug 的形态就是「同一概念 4 条写入路径」：非流式 1 处、流式 2 处、
    `/v1/models` 1 处 —— 修一处看着就好了，而**用户走的恰好是没修的那条**。
    ⇒ 判据必须**扫源码逐条查**，而不是只测"某一条路径对不对"。
    """
    src = (Path(__file__).resolve().parent.parent
           / "emotion_app" / "web" / "app.py").read_text(encoding="utf-8")
    # 去掉 docstring / 注释（它们在讲这个 bug，不该算违规）
    code_lines: list[str] = []
    in_doc = False
    for ln in src.splitlines():
        st = ln.strip()
        if st.startswith('"""') or st.startswith("'''"):
            if st.count('"""') + st.count("'''") == 1:
                in_doc = not in_doc
            continue
        if in_doc or st.startswith("#"):
            continue
        code_lines.append(ln)
    code = "\n".join(code_lines)

    # 🔴 `_effective_model()` **自己内部**那句 `return config.upstream.model`
    #    是**合法**的兜底（chat 服务不可用时才走到它，且同函数里已先试运行时设置）。
    #    要排除的是"绕过它、直接读启动快照"的那些调用点。
    #    ⚠️ 第一版判据没排除自身 ⇒ **假红**（产品是对的）。
    def _outside_helper(lines: list[str]) -> str:
        out: list[str] = []
        inside = False
        for ln in lines:
            if ln.lstrip().startswith("def _effective_model("):
                inside = True
                continue
            if inside:
                # 函数体结束 = 回到同级的 `def `（顶格四空格的 def）
                if ln.startswith("    def "):
                    inside = False
                else:
                    continue
            out.append(ln)
        return "\n".join(out)

    outside = _outside_helper(code_lines)
    assert "config.upstream.model" not in outside, (
        "app.py 里有**绕过 `_effective_model`**、直接读启动快照当模型名的代码 —— "
        "那就是这个 bug 的形态（运行时设置被绕过）"
    )
    # 而且必须真的走了统一函数（不是删掉就算修）
    assert code.count("_effective_model(") >= 4, (
        f"`_effective_model(` 调用点只有 {code.count('_effective_model(')} 处，"
        "应覆盖 /v1/models + 非流式 + 两条流式路径"
    )
