# -*- coding: utf-8 -*-
"""生图：OpenAI 兼容 `/images/generations`。

⭐ 依据（实测）：
   * 爱语用的就是 **OpenAI 兼容 `/images/generations`** + 11 个配置键
     （`key_image_gen_custom_url` / `_custom_key` / `_custom_path` / `_use_custom_api` …）
   * 情绪 2026-09-18 已定：**接 OpenAI 兼容 `/images/generations`**

⚠️ 与爱语的关键差别：**工具下发方式**。
   爱语靠文本标签 `<generate_image:描述>`（易截断）；
   我们用 **function calling**（见 `tools.py` 与 `docs/IMAGE_TOOL_DISPATCH.md`）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..infra.logging_setup import get_logger

log = get_logger(__name__)

#: 图片类型（照陪伴插件的 `kind` 枚举，去掉 sticker 第一版不做）
KIND_TEXT2IMG = "text2img"
KIND_SELFIE = "selfie"
KIND_EDIT = "edit"
VALID_KINDS = (KIND_TEXT2IMG, KIND_SELFIE, KIND_EDIT)

#: 默认尺寸
DEFAULT_SIZE = "1024x1024"

#: 单张图最大字节（防上游返回巨物把内存吃光）
MAX_IMAGE_BYTES = 20 * 1024 * 1024

#: 单次请求参考图张数上限（照陪伴插件 §5.2 的 8 张）
MAX_REFERENCE_IMAGES = 8

#: 支持「精准参考」（`director_reference_*`）的模型。
#:
#: 🔴 **2026-09-25 实测（`api.syuan.org`，6 组探测）**：
#:   * `nai-diffusion-5-full` + 精准参考 → **500**
#:     `novelai adaptor: precise reference is only supported by NAI 4.5 models`
#:   * `nai-diffusion-5-curated` + 精准参考 → 同上 500
#:   * `nai-diffusion-4-5-full` + 精准参考 → **200 出图** ✅
#:   ⚠️ 陪伴插件 README 说 v5 全系可用 —— **那是另一套网关的结论，别照搬**。
NAI_DIRECTOR_MODELS = frozenset(
    {"nai-diffusion-4-5-full", "nai-diffusion-4-5-curated", "nai45", "nai45-curated"}
)

#: 精准参考不支持当前模型时**自动切换**到这个（照陪伴插件的先例）。
#: ⚠️ 切换会**改变画风**（4.5 vs 5）→ 必须打日志，不许静默。
NAI_DIRECTOR_FALLBACK_MODEL = "nai-diffusion-4-5-full"

#: 精准参考的描述词枚举（实测可用的三种）。
NAI_DIRECTOR_CAPTIONS = ("character", "style", "character&style")


def reference_data_uri(source: str, *, max_bytes: int = MAX_IMAGE_BYTES) -> str:
    """把参考图转成 **data URI**（上游要求的形式，2026-09-25 实测）。

    Args:
        source: 本地文件路径；若本身已是 `data:` URI 则原样返回。

    Raises:
        ImageError: 路径为空 / 不存在 / 是空文件 / 过大。
            🔴 **绝不静默返回空串** —— 本项目最贵的一课就是「参考图被静默丢弃」
            （aitag 的事故），那种 bug 用户只能凭感觉报"好像没用上参考图"。
    """
    text = (source or "").strip()
    if not text:
        raise ImageError("参考图为空", code="reference_unreadable")
    if text.startswith("data:"):
        return text

    path = Path(text)
    if not path.is_file():
        raise ImageError(
            f"参考图不存在或不是文件：{text}", code="reference_unreadable"
        )
    blob = path.read_bytes()
    if not blob:
        raise ImageError(f"参考图是空文件：{text}", code="reference_unreadable")
    if len(blob) > max_bytes:
        raise ImageError(
            f"参考图过大（{len(blob)} > {max_bytes}）：{text}",
            code="reference_unreadable",
        )

    suffix = path.suffix.lower().lstrip(".")
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(
        suffix, "png"
    )
    return f"data:image/{mime};base64,{base64.b64encode(blob).decode()}"


def _describe(exc: BaseException) -> str:
    """把异常渲染成**一定有内容**的一句话。

    🔴 为什么需要它（2026-09-25 实测踩到）：
       `httpx` 的网络类异常（`ConnectError`/`ReadError`/`RemoteProtocolError`/
       `ConnectTimeout`…）**`str()` 可以是空串** ——
       实测这 8 个类的 `str(cls(""))` **全是 `''`**。
       于是用户看到的是「生图端点连接失败：」后面**什么都没有**
       （真环境复跑 `ref_image_live_check.py` 时就是这么出现的），
       既不知道是超时、被重置、还是 DNS 挂了。

    ✅ 退化链：`str(exc)` → 空则用 `repr(exc)` → 再空则用类名。
       永远给出一段**可诊断**的文字（本项目最贵的一课：静默失败比报错贵）。
    """
    text = str(exc).strip()
    if text:
        return text
    return repr(exc) or type(exc).__name__


class ImageError(Exception):
    """生图失败。"""

    def __init__(self, message: str, *, code: str = "", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(slots=True)
class ImageTarget:
    """生图端点（可与聊天上游不同）。"""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    #: 生图超时（秒）。NAI 实测 7~26 秒波动 → 给足余量
    timeout: float = 180.0

    def url(self, path: str = "images/generations") -> str:
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"

    def headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h


@dataclass(slots=True)
class GeneratedImage:
    """一张生成结果。"""

    path: str = ""
    url: str = ""
    b64: str = ""
    size_bytes: int = 0
    revised_prompt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "url": self.url,
            "size_bytes": self.size_bytes,
            "revised_prompt": self.revised_prompt,
            "has_b64": bool(self.b64),
        }


@dataclass(slots=True)
class ImageResult:
    """一次生图的结果。"""

    images: list[GeneratedImage] = field(default_factory=list)
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.images)

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "model": self.model,
            "images": [i.to_dict() for i in self.images],
        }


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class ImageClient:
    """OpenAI 兼容的图片生成客户端。"""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def open(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(180.0, connect=10.0),  # 生图慢，给足时间
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def is_open(self) -> bool:
        return self._client is not None

    async def generate(
        self,
        target: ImageTarget,
        *,
        prompt: str,
        size: str = DEFAULT_SIZE,
        n: int = 1,
        extra: dict[str, Any] | None = None,
        reference_images: list[str] | None = None,
    ) -> ImageResult:
        """文生图；`reference_images` 非空时带**精准参考**（`director_reference_*`）。

        ⚠️ 参考图必须是 **data URI**（见 `reference_data_uri`），不是路径 ——
           路径在这里读不到，也绝不允许被静默丢掉。

        Raises:
            ImageError: 端点未配置 / 上游报错 / 返回不可解析。
        """
        body = self._base_body(target, prompt=prompt, size=size, n=n, extra=extra)

        refs = [r for r in (reference_images or []) if r]
        if len(refs) > MAX_REFERENCE_IMAGES:
            log.warning(
                "参考图超过上限，已截断",
                extra={"data": {"got": len(refs), "max": MAX_REFERENCE_IMAGES}},
            )
            refs = refs[:MAX_REFERENCE_IMAGES]
        if refs:
            self._apply_director_reference(body, refs)

        return await self._post(
            target, "images/generations", body, label="文生图", refs=len(refs)
        )

    async def edit(
        self,
        target: ImageTarget,
        *,
        prompt: str,
        image: str,
        size: str = DEFAULT_SIZE,
        n: int = 1,
        extra: dict[str, Any] | None = None,
    ) -> ImageResult:
        """图生图（`/images/edits`）；`image` 是待修改图的 **data URI**。

        ⭐ 2026-09-25 实测：该端点在本网关**可用**（200 出图）。

        Raises:
            ImageError: `image` 为空 / 端点未配置 / 上游报错。
        """
        if not (image or "").strip():
            raise ImageError("图生图缺少源图", code="reference_unreadable")
        body = self._base_body(target, prompt=prompt, size=size, n=n, extra=extra)
        body["image"] = image
        return await self._post(
            target, "images/edits", body, label="图生图", refs=1
        )

    def _base_body(
        self,
        target: ImageTarget,
        *,
        prompt: str,
        size: str,
        n: int,
        extra: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not target.base_url:
            raise ImageError("未配置生图端点", code="not_configured")
        if not prompt.strip():
            raise ImageError("prompt 不能为空", code="empty_prompt")
        if self._client is None:
            raise ImageError("客户端未 open()", code="not_open")

        body: dict[str, Any] = {
            "model": target.model or "dall-e-3",
            "prompt": prompt.strip(),
            "n": max(1, min(int(n), 4)),
            "size": size or DEFAULT_SIZE,
        }
        if extra:
            body.update(extra)
        return body

    def _apply_director_reference(
        self, body: dict[str, Any], refs: list[str]
    ) -> None:
        """把参考图写成**精准参考五数组**（严格等长、按下标一一对应）。

        照陪伴插件 `main.py:1812-1836` 的实测结论：
          * `director_reference_information_extracted` **必须是 1.0**
            （其他取值会被上游参数校验拒绝，HTTP 400）
          * 仅 NAI 4.5 系支持 → 不满足时**自动切换并打日志**
        """
        model = str(body.get("model") or "")
        if model.strip().casefold() not in NAI_DIRECTOR_MODELS:
            log.warning(
                "精准参考不支持当前生图模型，已自动切换",
                extra={
                    "data": {
                        "from": model,
                        "to": NAI_DIRECTOR_FALLBACK_MODEL,
                        "why": "该网关实测仅 NAI 4.5 系支持 director_reference_*",
                        "refs": len(refs),
                    }
                },
            )
            body["model"] = NAI_DIRECTOR_FALLBACK_MODEL

        params = body.setdefault("parameters", {})
        params["director_reference_images"] = refs
        params["director_reference_strength_values"] = [1.0] * len(refs)
        params["director_reference_secondary_strength_values"] = [0.5] * len(refs)
        params["director_reference_information_extracted"] = [1.0] * len(refs)
        params["director_reference_descriptions"] = [
            {
                "caption": {"base_caption": "character&style", "char_captions": []},
                "legacy_uc": False,
            }
            for _ in refs
        ]

    async def _post(
        self,
        target: ImageTarget,
        path: str,
        body: dict[str, Any],
        *,
        label: str,
        refs: int = 0,
    ) -> ImageResult:
        """发请求 + 错误编码 + 解析（文生图/图生图共用）。"""
        # ⚠️ 日志只记长度，**不记 prompt 全文**（可能很长，也可能含隐私）
        log.info(
            "生图请求",
            extra={
                "data": {
                    "url": target.url(path),
                    "label": label,
                    "model": body["model"],
                    "size": body["size"],
                    "prompt_chars": len(body["prompt"]),
                    # 🔴 参考图数量必须可见 —— "参考图被静默丢弃"是本项目最贵的一课
                    "refs": refs,
                }
            },
        )

        try:
            resp = await self._client.post(
                target.url(path), headers=target.headers(), json=body,
                timeout=target.timeout,
            )
        except httpx.TimeoutException as exc:
            # ⚠️ 生图**慢且波动大**（NAI 实测 7~26 秒）—— 超时要单独编码，
            #    这样上层能给出"稍后重试"而不是"失败"的措辞。
            raise ImageError(
                f"生图超时（{target.timeout if hasattr(target,'timeout') else '?'}）："
                f"{_describe(exc)}",
                code="timeout",
            ) from exc
        except httpx.HTTPError as exc:
            raise ImageError(
                f"生图端点连接失败：{_describe(exc)}",
                code="connect_failed",
            ) from exc

        if resp.status_code >= 400:
            detail = resp.text[:300]
            # ⚠️ 400/403 常见于内容策略拒绝 —— 单独编码，
            #    这样上层可以"不复述上游原文"（照陪伴插件的规则 8）
            code = (
                "provider_policy_refusal"
                if resp.status_code in (400, 403)
                else "provider_error"
            )
            raise ImageError(
                f"生图上游返回 {resp.status_code}（{detail}）",
                code=code,
                status=resp.status_code,
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise ImageError("生图上游返回的不是 JSON", code="bad_response") from exc

        return parse_image_response(data, model=body["model"])


def parse_image_response(data: dict[str, Any], *, model: str = "") -> ImageResult:
    """解析 OpenAI 兼容的图片响应。"""
    items = data.get("data")
    if not isinstance(items, list):
        raise ImageError("生图响应缺少 data 数组", code="bad_response")

    out: list[GeneratedImage] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append(
            GeneratedImage(
                url=str(item.get("url") or ""),
                b64=str(item.get("b64_json") or ""),
                revised_prompt=str(item.get("revised_prompt") or ""),
            )
        )

    if not out:
        raise ImageError("生图响应里没有图片", code="empty_result")

    return ImageResult(images=out, model=str(data.get("model") or model), raw=data)


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------


def save_image(
    image: GeneratedImage,
    out_dir: Path,
    *,
    prefix: str = "img",
) -> GeneratedImage:
    """把图片落盘（支持 base64 或 URL 二选一，URL 需调用方先下载）。

    ⚠️ 本函数**只处理 base64**；URL 形式由 `download_to` 处理。
    """
    if not image.b64:
        return image

    raw = image.b64
    if "," in raw[:100] and raw.strip().startswith("data:"):
        raw = raw.split(",", 1)[1]

    try:
        blob = base64.b64decode(raw, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise ImageError(f"base64 解码失败：{exc}", code="bad_b64") from exc

    if len(blob) > MAX_IMAGE_BYTES:
        raise ImageError(
            f"图片过大（{len(blob)} > {MAX_IMAGE_BYTES}）", code="too_large"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(blob).hexdigest()[:16]
    path = out_dir / f"{prefix}-{int(time.time())}-{digest}.png"
    path.write_bytes(blob)

    image.path = str(path)
    image.size_bytes = len(blob)
    image.b64 = ""          # 落盘后清掉，避免到处传大字符串
    return image


async def download_to(
    client: httpx.AsyncClient,
    image: GeneratedImage,
    out_dir: Path,
    *,
    prefix: str = "img",
) -> GeneratedImage:
    """把 URL 形式的图片下载落盘。"""
    if not image.url:
        return image

    try:
        resp = await client.get(image.url, timeout=60.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ImageError(f"图片下载失败：{exc}", code="download_failed") from exc

    blob = resp.content
    if len(blob) > MAX_IMAGE_BYTES:
        raise ImageError(f"图片过大（{len(blob)}）", code="too_large")

    out_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(blob).hexdigest()[:16]
    ext = ".png" if "png" in resp.headers.get("content-type", "") else ".jpg"
    path = out_dir / f"{prefix}-{int(time.time())}-{digest}{ext}"
    path.write_bytes(blob)

    image.path = str(path)
    image.size_bytes = len(blob)
    return image
