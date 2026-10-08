# -*- coding: utf-8 -*-
"""人设参考图：**走真实 chat 链路**的端到端判据（2026-09-25）。

## 为什么还要这一个文件

`test_persona_ref_http.py` 证明"面板能存进去"；
`test_s6_image_reference.py` 证明"函数会用参考图"。
**但两者都绕过了 `ChatService`** —— 而 per-persona 参考图**唯一**的生效路径
就是"chat 执行工具时把当前人设放进上下文"。

🔴 本项目最贵的一课（`AGENTS.md`）：
   **函数对 ≠ 接线对。** 上一轮注入验证第一轮 **3/10 没抓住**，
   正是因为判据只测函数没测接线。所以这里走**真 `ChatService.complete()`**，
   只把上游 HTTP 打桩 —— 其余全是真实对象。

判据只有一条核心：**同一个工具、同一句 prompt，换人设 → 发出去的参考图跟着换。**
反例（必须为假）：用了 B 人设却发 A 的图 / 换了人设仍发全局兜底图。
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest

from emotion_app.domain.backends import OpenAIImageBackend
from emotion_app.domain.chat import ChatRequest, ChatService
from emotion_app.domain.image_gen import ImageClient, ImageTarget
from emotion_app.domain.image_tool import ImageToolService, register_image_tool
from emotion_app.domain.message import Message
from emotion_app.domain.persona import Persona
from emotion_app.domain.persona_repo import PersonaRepository
from emotion_app.domain.tools import ToolRegistry
from emotion_app.infra.config import Config
from emotion_app.storage.sqlite_store import SqliteStore

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAF"
    "BQIAX8jx0gAAAABJRU5ErkJggg=="
)

#: 一张内容不同的图（用来分辨"到底发了谁的图"）
PNG_OTHER = PNG_1PX + b"\x00" * 8


def _chat_response(tool_name: str = "generate_image") -> httpx.Response:
    """让"模型"要求调用生图工具的响应。"""
    return httpx.Response(
        200,
        json={
            "id": "cmpl-1",
            "model": "m",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(
                                {"prompt": "自拍", "kind": "selfie", "send": False},
                                ensure_ascii=False,
                            ),
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


class _Recorder:
    """按 URL 分流打桩：聊天端点返回"要调工具"，生图端点记录 body。

    ⚠️ "这轮还要不要调工具"的判断**必须看请求体本身**（对话里有没有
       `role=tool` 的回填消息），**不能用一个累计计数器** ——
       我第一版就是计数器（`chat_calls > 1` 就给正文），
       结果第二次 `complete()` 的**第一个**请求就拿到了正文 →
       生图根本没发生 → 判据读到的是**上一次**的图，报出一个假失败。
       （教训：打桩的状态要跟被测语义对齐，别用"全局第几次调用"。
         本项目铁律：断言失败先查清哪个值才对。）
    """

    def __init__(self) -> None:
        self.image_calls: list[dict] = []
        self.chat_calls: int = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "chat/completions" in url:
            self.chat_calls += 1
            body = json.loads(request.content.decode())
            has_tool_result = any(
                m.get("role") == "tool" for m in (body.get("messages") or [])
            )
            if has_tool_result:
                return httpx.Response(200, json={
                    "id": "cmpl-2", "model": "m",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": "好，拍好了"},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                })
            return _chat_response()
        self.image_calls.append(
            {"url": url, "body": json.loads(request.content.decode())}
        )
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG_1PX).decode()}]})

    @property
    def last_refs(self) -> list[str]:
        assert self.image_calls, "生图请求根本没发出去"
        params = self.image_calls[-1]["body"].get("parameters") or {}
        return params.get("director_reference_images") or []


@pytest.fixture
async def wiring(tmp_path, monkeypatch):
    """真 ChatService + 真工具表 + 打桩上游。"""
    monkeypatch.setattr(
        "emotion_app.domain.persona_ref.PERSONA_REF_DIR", str(tmp_path / "refs")
    )
    cfg = Config()
    cfg.upstream.base_url = "https://up/v1"
    cfg.upstream.api_key = "k"
    cfg.upstream.model = "m"
    cfg.storage.sqlite_path = str(tmp_path / "w.db")
    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()

    rec = _Recorder()
    chat_backend = type(
        "B", (), {
            "open": staticmethod(lambda: asyncio.sleep(0)),
        }
    )()

    from emotion_app.domain.backends import OpenAIChatBackend

    real_backend = OpenAIChatBackend(transport=httpx.MockTransport(rec.handler))
    await real_backend.open()

    personas = PersonaRepository(store)
    svc = ChatService(cfg, real_backend, personas=personas)

    # 生图工具：真 `ImageToolService`，只有 HTTP 是打桩的
    img_client = ImageClient(transport=httpx.MockTransport(rec.handler))
    await img_client.open()
    img_backend = OpenAIImageBackend(
        img_client,
        ImageTarget(base_url="https://img/v1", api_key="k", model="nai-diffusion-5-full"),
    )
    registry = ToolRegistry(max_calls_per_turn=1)
    image_service = ImageToolService(
        img_backend, output_dir=tmp_path / "out", persona_reference=""
    )
    register_image_tool(registry, image_service)
    svc.bind_tools(registry)

    yield svc, personas, rec, tmp_path, image_service

    await real_backend.close()
    await img_client.close()
    await store.close()


def _write_ref(tmp_path: Path, name: str, blob: bytes = PNG_1PX) -> str:
    from emotion_app.domain import persona_ref as pr

    d = pr.ref_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(blob)
    return name


async def test_tool_loop_uses_the_persona_of_this_request(wiring):
    """⭐⭐ **本文件的核心判据**：请求带哪个 `persona`，就发哪个角色的参考图。

    这正是"per-persona"的意义 —— 全局一个值做不到这条。
    """
    svc, personas, rec, tmp_path, _ = wiring
    a_name = _write_ref(tmp_path, "a.png", PNG_1PX)
    b_name = _write_ref(tmp_path, "b.png", PNG_OTHER)

    pa = await personas.create(Persona(name="甲", reference_image_path=a_name))
    pb = await personas.create(Persona(name="乙", reference_image_path=b_name))

    # ---- 用「甲」发一次 ----
    res_a = await svc.complete(
        ChatRequest(
            messages=[Message(role="user", content="拍张自拍")],
            persona=pa, persist=False,
        )
    )
    refs_a = rec.last_refs
    assert refs_a, "甲 那次没发参考图"
    got_a = base64.b64decode(refs_a[0].split(",", 1)[1])
    assert got_a == PNG_1PX, "甲 那次发的不是甲自己的图"

    # ---- 换「乙」再发一次 ----
    res_b = await svc.complete(
        ChatRequest(
            messages=[Message(role="user", content="拍张自拍")],
            persona=pb, persist=False,
        )
    )
    refs_b = rec.last_refs
    assert refs_b, "乙 那次没发参考图"
    got_b = base64.b64decode(refs_b[0].split(",", 1)[1])
    assert got_b == PNG_OTHER, (
        "换了人设却仍发上一张图 —— 人设上下文没按请求隔离（并发下会串脸）"
    )
    assert got_a != got_b, "两个人设发了同一张图 = per-persona 根本没生效"


async def test_no_persona_falls_back_to_global(wiring):
    """没人设 → 落到**全局兜底**（`.env` 那份），而不是报错或什么都不发。"""
    svc, _, rec, tmp_path, image_service = wiring
    global_ref = tmp_path / "global.png"
    global_ref.write_bytes(PNG_1PX)
    image_service.set_persona_reference(str(global_ref))

    await svc.complete(
        ChatRequest(messages=[Message(role="user", content="自拍")], persona=None, persist=False)
    )
    assert rec.last_refs, "全局兜底没生效"


async def test_persona_wins_over_global(wiring):
    """🔴 人设自己那张 **优先于** 全局兜底（否则多角色时全局会把所有人盖成一张脸）。"""
    svc, personas, rec, tmp_path, image_service = wiring
    global_ref = tmp_path / "global.png"
    global_ref.write_bytes(PNG_OTHER)
    image_service.set_persona_reference(str(global_ref))

    p_name = _write_ref(tmp_path, "p.png", PNG_1PX)
    p = await personas.create(Persona(name="甲", reference_image_path=p_name))

    await svc.complete(
        ChatRequest(messages=[Message(role="user", content="自拍")], persona=p, persist=False)
    )
    got = base64.b64decode(rec.last_refs[0].split(",", 1)[1])
    assert got == PNG_1PX, "全局兜底把人设自己的图顶掉了"


async def test_context_does_not_leak_after_request(wiring):
    """请求结束后上下文必须**还原** —— 否则下一个请求会看到上一个人的脸。"""
    from emotion_app.domain import tool_context

    svc, personas, rec, tmp_path, _ = wiring
    p_name = _write_ref(tmp_path, "a.png")
    p = await personas.create(Persona(name="甲", reference_image_path=p_name))

    await svc.complete(
        ChatRequest(messages=[Message(role="user", content="自拍")], persona=p, persist=False)
    )
    assert tool_context.get_persona() is None, (
        "请求结束后上下文没还原 —— 之后的请求会拿到上一个请求的人设（串脸）"
    )


async def test_edit_does_not_use_persona_reference(wiring):
    """🔴 `edit` 不吃人设参考图（官方文档三处独立背书：
    「改图原图缺失时按设计**失败关闭**，不应悄悄生成一张新图」）。"""
    svc, personas, rec, tmp_path, _ = wiring
    p_name = _write_ref(tmp_path, "a.png")
    p = await personas.create(Persona(name="甲", reference_image_path=p_name))

    # 让"模型"这次请求 edit 且**不给**参考图
    rec2 = rec
    calls_before = len(rec.image_calls)

    def _edit_response(request: httpx.Request) -> httpx.Response:
        if "chat/completions" in str(request.url):
            return httpx.Response(200, json={
                "id": "c", "model": "m",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant", "content": "",
                        "tool_calls": [{
                            "id": "call_1", "type": "function",
                            "function": {
                                "name": "generate_image",
                                "arguments": json.dumps(
                                    {"prompt": "改成红裙子", "kind": "edit", "send": False},
                                    ensure_ascii=False,
                                ),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG_1PX).decode()}]})

    # 换掉上游打桩
    new_backend = type(svc._client)(transport=httpx.MockTransport(_edit_response))  # noqa: SLF001
    await new_backend.open()
    old = svc._client  # noqa: SLF001
    svc._client = new_backend  # noqa: SLF001

    try:
        await svc.complete(
            ChatRequest(messages=[Message(role="user", content="改成红裙子")], persona=p, persist=False)
        )
    finally:
        await new_backend.close()
        svc._client = old  # noqa: SLF001

    assert len(rec.image_calls) == calls_before, (
        "edit 缺参考图却仍然发出了生图请求 —— 语义被静默换掉了"
    )
