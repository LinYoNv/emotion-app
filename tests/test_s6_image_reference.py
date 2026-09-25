# -*- coding: utf-8 -*-
"""A1：生图**参考图真发上游**（2026-09-25 修）。

修之前：`OpenAIImageBackend.generate` 对参考图只打一行
「后端不支持参考图，已忽略」就把它丢了 —— 正是本项目最贵的那类 bug
（参考图静默丢弃；aitag 的同类事故用户只能凭感觉报"好像没用上参考图"）。

修之后两条路，**都经真上游实测**（2026-09-25，`api.syuan.org`）：

| kind | 走哪条 | 实测 |
|---|---|---|
| `edit` | `/images/edits` 真 img2img | **200 出图** |
| `selfie` | `/images/generations` + `director_reference_*` | **200 出图** |

🔴 两条**实测出来的硬约束**（照抄陪伴插件会踩）：

1. **参考图必须放进 `parameters` 对象**，且是 **data URI**；
   实测非法 base64 会被点名拒绝：
   `normalize reference_image_multiple[0]: invalid image: failed to decode base64`。
2. **该网关的 NAI 5 通道不支持精准参考** —— `nai-diffusion-5-full` + director
   → 500 `precise reference is only supported by NAI 4.5 models`；
   `nai-diffusion-4-5-full` + director → 200。
   ⚠️ 陪伴插件 README 说 v5 全系可用，**那是另一套网关的结论**。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging

import httpx
import pytest

from emotion_app.domain.backends import OpenAIImageBackend
from emotion_app.domain.image_gen import (
    MAX_REFERENCE_IMAGES,
    NAI_DIRECTOR_FALLBACK_MODEL,
    ImageClient,
    ImageError,
    ImageTarget,
    reference_data_uri,
)
from emotion_app.domain.image_tool import ImageToolService

PNG_1PX = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _ok() -> httpx.Response:
    return httpx.Response(200, json={"model": "nai", "data": [{"b64_json": PNG_1PX}]})


class _Capture:
    """截住真正发出去的请求（URL + body），供判据检查。"""

    def __init__(self, response=None):
        self.calls: list[dict] = []
        self._response = response or _ok()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(
            {"url": str(request.url), "body": json.loads(request.content.decode())}
        )
        return self._response

    @property
    def last(self) -> dict:
        assert self.calls, "没有发出任何请求"
        return self.calls[-1]


def _backend(cap: _Capture, *, model: str = "nai-diffusion-5-full") -> OpenAIImageBackend:
    client = ImageClient(transport=httpx.MockTransport(cap.handler))
    return OpenAIImageBackend(
        client, ImageTarget(base_url="https://img/v1", api_key="k", model=model)
    )


def _ref_file(tmp_path):
    p = tmp_path / "ref.png"
    p.write_bytes(base64.b64decode(PNG_1PX))
    return p


# ===========================================================================
# 1. reference_data_uri —— 读不到必须**响亮报错**，绝不静默
# ===========================================================================


def test_reference_data_uri_reads_real_file(tmp_path):
    p = _ref_file(tmp_path)
    uri = reference_data_uri(str(p))
    assert uri.startswith("data:image/png;base64,")
    # 往返一致：解回来必须是同一份字节
    assert base64.b64decode(uri.split(",", 1)[1]) == p.read_bytes()


def test_reference_data_uri_keeps_jpeg_mime(tmp_path):
    p = tmp_path / "a.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-bytes")
    assert reference_data_uri(str(p)).startswith("data:image/jpeg;base64,")


def test_reference_data_uri_passthrough(tmp_path):
    already = "data:image/png;base64,AAAA"
    assert reference_data_uri(already) == already


@pytest.mark.parametrize(
    "bad, why",
    [
        ("", "空路径"),
        ("   ", "空白路径"),
        ("/nonexistent/nope.png", "文件不存在"),
    ],
)
def test_reference_data_uri_raises_on_unreadable(bad, why):
    with pytest.raises(ImageError) as ei:
        reference_data_uri(bad)
    assert ei.value.code == "reference_unreadable", why


def test_reference_data_uri_raises_on_directory(tmp_path):
    with pytest.raises(ImageError) as ei:
        reference_data_uri(str(tmp_path))
    assert ei.value.code == "reference_unreadable"


def test_reference_data_uri_raises_on_empty_file(tmp_path):
    p = tmp_path / "empty.png"
    p.write_bytes(b"")
    with pytest.raises(ImageError) as ei:
        reference_data_uri(str(p))
    assert ei.value.code == "reference_unreadable"


# ===========================================================================
# 2. 精准参考五数组（形状错 = 上游 400/500）
# ===========================================================================


def test_generate_sends_director_five_arrays_of_equal_length(tmp_path):
    cap = _Capture()
    backend = _backend(cap)

    async def go():
        client = backend._client  # noqa: SLF001 - 测试需要 open
        await client.open()
        try:
            await backend.generate(
                prompt="1girl", kind="selfie", reference_image=str(_ref_file(tmp_path))
            )
        finally:
            await client.close()

    asyncio.run(go())
    body = cap.last["body"]
    assert cap.last["url"].endswith("/images/generations")
    params = body["parameters"]
    five = [
        "director_reference_images",
        "director_reference_strength_values",
        "director_reference_secondary_strength_values",
        "director_reference_information_extracted",
        "director_reference_descriptions",
    ]
    for key in five:
        assert key in params, f"缺 {key}"
        assert len(params[key]) == 1, f"{key} 长度必须与参考图数一致"
    # 参考图必须是 data URI（上游按 base64 解）
    assert params["director_reference_images"][0].startswith("data:image/png;base64,")


def test_director_information_extracted_must_be_one(tmp_path):
    """🔴 上游实测：该值非 1.0 会被参数校验拒绝（HTTP 400）。"""
    cap = _Capture()
    backend = _backend(cap)

    async def go():
        client = backend._client  # noqa: SLF001
        await client.open()
        try:
            await backend.generate(
                prompt="1girl", kind="selfie", reference_image=str(_ref_file(tmp_path))
            )
        finally:
            await client.close()

    asyncio.run(go())
    assert cap.last["body"]["parameters"]["director_reference_information_extracted"] == [1.0]


def test_generate_switches_model_off_nai5_and_is_loud(tmp_path, caplog):
    """NAI 5 + director 实测 500 → 必须自动切到 4.5，**且打日志**（切画风不能静默）。"""
    caplog.set_level(logging.WARNING, logger="emotion_app.domain.image_gen")
    cap = _Capture()
    backend = _backend(cap, model="nai-diffusion-5-full")

    async def go():
        client = backend._client  # noqa: SLF001
        await client.open()
        try:
            await backend.generate(
                prompt="1girl", kind="selfie", reference_image=str(_ref_file(tmp_path))
            )
        finally:
            await client.close()

    asyncio.run(go())
    assert cap.last["body"]["model"] == NAI_DIRECTOR_FALLBACK_MODEL
    recs = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert recs, "模型被切换却没有任何 WARNING 日志 = 静默改画风"
    assert any("精准参考" in r.getMessage() for r in recs)


def test_generate_keeps_model_when_already_nai45(tmp_path):
    cap = _Capture()
    backend = _backend(cap, model="nai-diffusion-4-5-full")

    async def go():
        client = backend._client  # noqa: SLF001
        await client.open()
        try:
            await backend.generate(
                prompt="1girl", kind="selfie", reference_image=str(_ref_file(tmp_path))
            )
        finally:
            await client.close()

    asyncio.run(go())
    assert cap.last["body"]["model"] == "nai-diffusion-4-5-full"


def test_too_many_references_are_truncated_and_logged(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="emotion_app.domain.image_gen")
    cap = _Capture()
    client = ImageClient(transport=httpx.MockTransport(cap.handler))

    async def go():
        await client.open()
        try:
            await client.generate(
                ImageTarget(base_url="https://img/v1", model="nai-diffusion-4-5-full"),
                prompt="1girl",
                reference_images=["data:image/png;base64,AAAA"] * (MAX_REFERENCE_IMAGES + 3),
            )
        finally:
            await client.close()

    asyncio.run(go())
    assert (
        len(cap.last["body"]["parameters"]["director_reference_images"])
        == MAX_REFERENCE_IMAGES
    )
    assert any("截断" in r.getMessage() for r in caplog.records)


def test_no_reference_means_no_parameters_key(tmp_path):
    """不带参考图时**不许**凭空造一个 parameters（免得上游误判）。"""
    cap = _Capture()
    client = ImageClient(transport=httpx.MockTransport(cap.handler))

    async def go():
        await client.open()
        try:
            await client.generate(
                ImageTarget(base_url="https://img/v1", model="nai-diffusion-5-full"),
                prompt="1girl",
            )
        finally:
            await client.close()

    asyncio.run(go())
    assert "parameters" not in cap.last["body"]


# ===========================================================================
# 3. img2img（/images/edits）
# ===========================================================================


def test_edit_posts_to_images_edits_with_image():
    cap = _Capture()
    client = ImageClient(transport=httpx.MockTransport(cap.handler))

    async def go():
        await client.open()
        try:
            await client.edit(
                ImageTarget(base_url="https://img/v1", model="nai-diffusion-4-5-full"),
                prompt="改一下",
                image="data:image/png;base64,AAAA",
            )
        finally:
            await client.close()

    asyncio.run(go())
    assert cap.last["url"].endswith("/images/edits")
    assert cap.last["body"]["image"] == "data:image/png;base64,AAAA"


def test_edit_without_image_raises():
    client = ImageClient(transport=httpx.MockTransport(_Capture().handler))

    async def go():
        await client.open()
        try:
            await client.edit(
                ImageTarget(base_url="https://img/v1", model="m"),
                prompt="改一下",
                image="",
            )
        finally:
            await client.close()

    with pytest.raises(ImageError) as ei:
        asyncio.run(go())
    assert ei.value.code == "reference_unreadable"


# ===========================================================================
# 4. 后端层：两条路各自走对 + 读不到参考图**不许静默降级**
# ===========================================================================


def test_backend_edit_uses_img2img_endpoint(tmp_path):
    cap = _Capture()
    backend = _backend(cap)

    async def go():
        client = backend._client  # noqa: SLF001
        await client.open()
        try:
            await backend.generate(
                prompt="改一下", kind="edit", reference_image=str(_ref_file(tmp_path))
            )
        finally:
            await client.close()

    asyncio.run(go())
    assert cap.last["url"].endswith("/images/edits"), "edit 必须走真 img2img"
    assert cap.last["body"]["image"].startswith("data:image/png;base64,")


def test_backend_selfie_uses_director_endpoint(tmp_path):
    cap = _Capture()
    backend = _backend(cap)

    async def go():
        client = backend._client  # noqa: SLF001
        await client.open()
        try:
            await backend.generate(
                prompt="自拍", kind="selfie", reference_image=str(_ref_file(tmp_path))
            )
        finally:
            await client.close()

    asyncio.run(go())
    assert cap.last["url"].endswith("/images/generations")
    assert "director_reference_images" in cap.last["body"]["parameters"]


def test_backend_reference_unreadable_raises_not_silently_dropped(tmp_path):
    """🔴 **反静默判据**：参考图读不到时**不许**继续画一张无关的图。"""
    cap = _Capture()
    backend = _backend(cap)

    async def go():
        client = backend._client  # noqa: SLF001
        await client.open()
        try:
            await backend.generate(
                prompt="自拍", kind="selfie",
                reference_image=str(tmp_path / "does-not-exist.png"),
            )
        finally:
            await client.close()

    with pytest.raises(ImageError) as ei:
        asyncio.run(go())
    assert ei.value.code == "reference_unreadable"
    assert not cap.calls, "读不到参考图却仍然发了请求（= 静默降级成纯文生图）"


def test_backend_edit_without_reference_raises():
    cap = _Capture()
    backend = _backend(cap)

    async def go():
        client = backend._client  # noqa: SLF001
        await client.open()
        try:
            await backend.generate(prompt="改一下", kind="edit", reference_image="")
        finally:
            await client.close()

    with pytest.raises(ImageError) as ei:
        asyncio.run(go())
    assert ei.value.code == "reference_unreadable"


def test_backend_logs_reference_mode(tmp_path, caplog):
    """参考图**必须留下痕迹** —— 这是"有没有真的用上"的唯一现场证据。"""
    caplog.set_level(logging.INFO, logger="emotion_app.domain.backends")
    cap = _Capture()
    backend = _backend(cap)

    async def go():
        client = backend._client  # noqa: SLF001
        await client.open()
        try:
            await backend.generate(
                prompt="自拍", kind="selfie", reference_image=str(_ref_file(tmp_path))
            )
        finally:
            await client.close()

    asyncio.run(go())
    msgs = [r.getMessage() for r in caplog.records]
    assert any("带参考图" in m for m in msgs), msgs


# ===========================================================================
# 5. 接线判据：从【工具入口】进去，参考图真的传到了 HTTP 层
#    （教训：函数对 ≠ 接线对）
# ===========================================================================


def test_tool_entry_passes_reference_all_the_way_to_http(tmp_path):
    cap = _Capture()
    backend = _backend(cap)
    ref = _ref_file(tmp_path)

    async def go():
        client = backend._client  # noqa: SLF001
        await client.open()
        try:
            svc = ImageToolService(backend, output_dir=tmp_path / "out")
            return await svc(
                {"prompt": "改成红裙子", "kind": "edit",
                 "reference_image_path": str(ref), "send": False}
            )
        finally:
            await client.close()

    r = asyncio.run(go())
    assert r.success is True, r
    assert cap.last["url"].endswith("/images/edits")
    assert cap.last["body"]["image"].startswith("data:image/png;base64,")


def test_tool_reports_reference_unreadable_to_model(tmp_path):
    cap = _Capture()
    backend = _backend(cap)

    async def go():
        client = backend._client  # noqa: SLF001
        await client.open()
        try:
            svc = ImageToolService(backend, output_dir=tmp_path / "out")
            return await svc(
                {"prompt": "改成红裙子", "kind": "edit",
                 "reference_image_path": str(tmp_path / "nope.png"), "send": False}
            )
        finally:
            await client.close()

    r = asyncio.run(go())
    assert r.success is False
    assert r.status == "reference_unreadable"
    assert r.must_not_claim_sent is True
    payload = r.to_payload()
    assert "不要编造" in payload or "不要编造图片路径" in payload
