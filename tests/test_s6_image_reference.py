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


# ===========================================================================
# 6. 🆕 人设固定参考图（2026-09-25）
# ===========================================================================
#
# 起因：光让参考图"能发出去"还不够 —— 每次还得模型自己传路径，同一个角色
# 画出来每次都不一样。把人设图设成默认值，`selfie` 才会稳定。
#
# 🔴 两条边界（都写进了 docstring，判据也盯着）：
#   * **只给 `selfie`**：`edit` 的语义是"改用户给的那张图"，拿角色参考图兜底
#     会**静默换掉语义**（画出来完全不是用户要改的图）。
#   * **必须留痕**：参考图"从哪来的"要有日志，静默替换 = 本项目最贵那类 bug。


def test_persona_reference_is_used_for_selfie_without_explicit_path(tmp_path):
    """⭐ 核心判据：selfie 没给路径 → 自动带人设参考图，且**真的发到了 HTTP**。"""
    cap = _Capture()
    backend = _backend(cap)
    persona_ref = _ref_file(tmp_path)

    async def go():
        client = backend._client  # noqa: SLF001
        await client.open()
        try:
            svc = ImageToolService(
                backend, output_dir=tmp_path / "out", persona_reference=str(persona_ref)
            )
            return await svc({"prompt": "自拍", "kind": "selfie", "send": False})
        finally:
            await client.close()

    r = asyncio.run(go())
    assert r.success is True, r
    assert cap.last["url"].endswith("/images/generations")
    refs = cap.last["body"]["parameters"]["director_reference_images"]
    assert refs[0].startswith("data:image/png;base64,"), "人设参考图没被带上去"


def test_explicit_path_wins_over_persona_reference(tmp_path):
    """模型**显式**给了路径时，人设图不许抢 —— 显式优先。"""
    cap = _Capture()
    backend = _backend(cap)
    persona_ref = _ref_file(tmp_path)
    explicit = tmp_path / "explicit.png"
    explicit.write_bytes(base64.b64decode(PNG_1PX) + b"\x00" * 8)  # 与 persona 不同的内容

    async def go():
        client = backend._client  # noqa: SLF001
        await client.open()
        try:
            svc = ImageToolService(
                backend, output_dir=tmp_path / "out", persona_reference=str(persona_ref)
            )
            return await svc({
                "prompt": "自拍", "kind": "selfie",
                "reference_image_path": str(explicit), "send": False,
            })
        finally:
            await client.close()

    r = asyncio.run(go())
    assert r.success is True, r
    got = cap.last["body"]["parameters"]["director_reference_images"][0]
    assert got == reference_data_uri(str(explicit)), "显式路径被人设图顶掉了"


def test_persona_reference_does_not_leak_into_edit(tmp_path):
    """🔴 **edit 不许**吃人设参考图 —— 那会静默把"改这张图"变成"画角色"。"""
    cap = _Capture()
    backend = _backend(cap)
    persona_ref = _ref_file(tmp_path)

    async def go():
        client = backend._client  # noqa: SLF001
        await client.open()
        try:
            svc = ImageToolService(
                backend, output_dir=tmp_path / "out", persona_reference=str(persona_ref)
            )
            return await svc({"prompt": "改成红裙子", "kind": "edit", "send": False})
        finally:
            await client.close()

    r = asyncio.run(go())
    assert r.success is False, "edit 缺参考图竟然成功了 —— 语义被静默换掉"
    # ⚠️ 这里是**工具自身的前置守卫**先命中（`edit 需要 reference_image_path`
    #    → `invalid_args`），不是后端那条 `reference_unreadable`。
    #    先查清"哪个值才对"再这么断言（本项目铁律：断言失败先查正确值，别改代码迁就）。
    assert r.status == "invalid_args", r.status
    assert not cap.calls, "edit 不该发出任何请求"


def test_persona_reference_does_not_leak_into_text2img(tmp_path):
    """🔴 text2img（风景/物件）也不许吃人设图 —— 会画错东西。"""
    cap = _Capture()
    backend = _backend(cap)

    async def go():
        client = backend._client  # noqa: SLF001
        await client.open()
        try:
            svc = ImageToolService(
                backend, output_dir=tmp_path / "out", persona_reference=str(_ref_file(tmp_path))
            )
            return await svc({"prompt": "一片海", "kind": "text2img", "send": False})
        finally:
            await client.close()

    r = asyncio.run(go())
    assert r.success is True, r
    assert "parameters" not in cap.last["body"], "text2img 不该带参考图字段"


def test_persona_reference_falls_back_cleanly_when_unset(tmp_path):
    """没配人设图时，`selfie` **照旧**是纯文生图（不能因此报错）。

    ⚠️ 光断言"不带 parameters"是**抓不住** "不判空就硬塞空串" 那种注入的 ——
       因为 `reference_images=[""]` 会被 `ImageClient.generate` 里的
       `[r for r in ... if r]` 过滤掉，**照样**不带 parameters。
       → 所以必须**从工具视角**断言：`ref` 为空，且**没有**打出"用了人设图"的日志。
    """
    cap = _Capture()
    backend = _backend(cap)
    logs: list[str] = []

    async def go():
        client = backend._client  # noqa: SLF001
        await client.open()
        try:
            svc = ImageToolService(backend, output_dir=tmp_path / "out")  # 不传 persona_reference
            return await svc({"prompt": "自拍", "kind": "selfie", "send": False})
        finally:
            await client.close()

    import logging as _logging

    class _H(_logging.Handler):
        def emit(self, record):  # noqa: D102
            logs.append(record.getMessage())

    lg = _logging.getLogger("emotion_app.domain.image_tool")
    h = _H()
    lg.addHandler(h)
    try:
        r = asyncio.run(go())
    finally:
        lg.removeHandler(h)

    assert r.success is True, r
    assert "parameters" not in cap.last["body"]
    # 🔴 用与真实日志**一致**的子串。
    #    ⚠️ 2026-09-25 踩到：这条原来写的是 `"人设固定参考图"`，而日志文案后来
    #       改成了「使用配置的固定参考图」→ **这个断言永远不可能为真**，
    #       变成了假判据（注入 ⑤ 因此抓不住）。
    #       **通则：断言里的字面串必须与产品里真正会打的串对齐**；
    #       写"否定式"判据时尤其危险 —— 它永远绿，看不出自己失效了。
    assert not any("固定参考图" in m for m in logs), (
        f"没配参考图却声称用了它：{logs}"
    )


def test_persona_reference_missing_file_is_loud_not_silent(tmp_path):
    """配了人设图但**文件不在** → 响亮失败，不静默降级成纯文生图。"""
    cap = _Capture()
    backend = _backend(cap)

    async def go():
        client = backend._client  # noqa: SLF001
        await client.open()
        try:
            svc = ImageToolService(
                backend, output_dir=tmp_path / "out",
                persona_reference=str(tmp_path / "gone.png"),
            )
            return await svc({"prompt": "自拍", "kind": "selfie", "send": False})
        finally:
            await client.close()

    r = asyncio.run(go())
    assert r.success is False
    assert r.status == "reference_unreadable", r.status
    assert not cap.calls, "人设图读不到却仍然发了请求（= 静默降级）"


def test_persona_reference_use_is_logged(tmp_path, caplog):
    """🔴 **留痕判据**：参考图从哪来的必须有日志（本项目最贵的一课）。

    ⚠️ 2026-09-25 加强：参考图现在有**三级来源**（模型显式 / 人设自己 / 全局兜底），
       光断言"有一行日志"已经不够 —— 必须能**区分是哪一级**，
       否则用户报"画得不像"时无从判断是"人设图没上传"还是"全局兜底在顶"。
    """
    caplog.set_level(logging.INFO, logger="emotion_app.domain.image_tool")
    cap = _Capture()
    backend = _backend(cap)

    async def go():
        client = backend._client  # noqa: SLF001
        await client.open()
        try:
            svc = ImageToolService(
                backend, output_dir=tmp_path / "out",
                persona_reference=str(_ref_file(tmp_path)),
            )
            return await svc({"prompt": "自拍", "kind": "selfie", "send": False})
        finally:
            await client.close()

    asyncio.run(go())
    recs = [r for r in caplog.records if "固定参考图" in r.getMessage()]
    assert recs, [r.getMessage() for r in caplog.records]
    # 全局兜底那条必须**点名 source=global** —— 这是"哪一级生效"的唯一现场证据。
    # ⚠️ 取值层级是 `record.data["source"]`：本项目所有结构化日志都走
    #    `extra={"data": {...}}`（见 `logging_setup` 的 JsonFormatter / TextFormatter），
    #    不是 `extra={"source": ...}` —— 写错层级会得到一个"永远为 None"的假判据。
    assert (getattr(recs[0], "data", None) or {}).get("source") == "global", (
        f"日志没标出参考图来源：{[getattr(r, 'data', None) for r in recs]}"
    )


def test_set_persona_reference_switches_default(tmp_path):
    """切人设时要能换默认参考图（`set_persona_reference`）。"""
    cap = _Capture()
    backend = _backend(cap)
    a, b = _ref_file(tmp_path), tmp_path / "b.png"
    b.write_bytes(base64.b64decode(PNG_1PX) + b"\x00" * 4)

    async def go():
        client = backend._client  # noqa: SLF001
        await client.open()
        try:
            svc = ImageToolService(backend, output_dir=tmp_path / "out", persona_reference=str(a))
            assert svc.persona_reference == str(a)
            svc.set_persona_reference(str(b))
            assert svc.persona_reference == str(b)
            return await svc({"prompt": "自拍", "kind": "selfie", "send": False})
        finally:
            await client.close()

    r = asyncio.run(go())
    assert r.success is True, r
    got = cap.last["body"]["parameters"]["director_reference_images"][0]
    assert got == reference_data_uri(str(b)), "换过之后用的还是旧的人设图"


# ===========================================================================
# 7. 🆕 网络类异常**必须给出可诊断的文本**（2026-09-25 实测补）
# ===========================================================================
#
# 🔴 起因：真环境复跑 `tools/ref_image_live_check.py` 时看到
#     「生图端点连接失败：」**后面什么都没有**。
#     查根因：`httpx` 的网络类异常 `str()` **可以是空串**
#     （实测 `ConnectError`/`ReadError`/`RemoteProtocolError`/`ConnectTimeout`
#       等 8 个类的 `str(cls(""))` 全是 `''`）。
#     → 用户既不知道是超时、被重置、还是 DNS 挂了。
#     这是本项目最贵那类 bug（**静默失败**）的变体：有报错，但报错里**没有信息**。


def test_describe_never_returns_empty():
    """`_describe()` 对"空消息异常"也必须给出内容（退化链：str → repr → 类名）。"""
    import httpx

    from emotion_app.domain.image_gen import _describe

    for cls in (
        httpx.ConnectError,
        httpx.ReadError,
        httpx.WriteError,
        httpx.RemoteProtocolError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.PoolTimeout,
    ):
        got = _describe(cls(""))
        assert got.strip(), f"{cls.__name__} 渲染成了空串 —— 用户看不到任何线索"
        # 退化到 repr 时应当能看出是哪个类（否则"有内容"也没用）
        assert cls.__name__ in got, f"{cls.__name__} 的退化文本里看不出异常类型：{got!r}"

    # 有正常消息时**原样透传**（不要无谓地套 repr）
    assert _describe(httpx.ConnectError("connection reset")) == "connection reset"


def test_connect_error_message_is_not_empty(tmp_path):
    """端到端：连接类异常 → 工具返回的 `actual_error` 里必须有可诊断信息。"""
    import httpx

    from emotion_app.domain.backends import OpenAIImageBackend
    from emotion_app.domain.image_gen import ImageClient, ImageTarget
    from emotion_app.domain.image_tool import ImageToolService

    def boom(request: httpx.Request) -> httpx.Response:
        # ⚠️ 传**空消息**——正是真环境里出现的那种（httpx 的 str() 为空）
        raise httpx.ConnectError("")

    async def go():
        client = ImageClient(transport=httpx.MockTransport(boom))
        await client.open()
        try:
            backend = OpenAIImageBackend(
                client, ImageTarget(base_url="https://img/v1", api_key="k", model="m")
            )
            svc = ImageToolService(backend, output_dir=tmp_path / "out")
            return await svc({"prompt": "画", "kind": "text2img", "send": False})
        finally:
            await client.close()

    r = asyncio.run(go())
    assert r.success is False
    # ⚠️ 我第一版断言 `status == "connect_failed"` —— **查清后确认断言是错的、代码是对的**：
    #    `_failure_result` 只为 `provider_policy_refusal`/`timeout`/`download_failed`/
    #    `reference_unreadable` 开了专门分支，`connect_failed` **有意**落到通用 `error`
    #    （对用户措辞一样是"这次没画出来"）。
    #    `connect_failed` 这个更精确的码在**日志**里（`extra.data.code`），供排查用。
    #    （本项目铁律：断言失败先查清哪个值才对，别条件反射改代码。）
    assert r.status == "error", r.status
    assert r.actual_error.strip(), "actual_error 是空的 —— 排查时没有任何线索"
    assert "ConnectError" in r.actual_error, (
        f"退化文本里看不出异常类型：{r.actual_error!r}"
    )
    assert len(r.actual_error) > len("生图端点连接失败："), (
        f"只有前缀没有内容：{r.actual_error!r}"
    )
