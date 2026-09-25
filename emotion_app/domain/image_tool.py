# -*- coding: utf-8 -*-
"""生图工具：注册到 function calling 的工具表。

⭐ **设计要点（全部有实测依据）**：

1. **用 function calling，不用文本标签**
   爱语用 `<generate_image:描述>`（真 dex 实测正则 `^<generate_image:.+>$`），
   情绪指出「容易被截断」—— 确认是结构性问题：正则带锚点 + 流式半截失效 + 失败无反馈。

2. **上游已验证支持**
   实测 `api.syuan.org`：非流式 `finish_reason=tool_calls` ✅；流式 `tool_calls` 分片可拼装 ✅；
   枚举参数模型能正确理解（说"自拍"→ `kind="selfie"`）✅。

3. **必须带防幻觉契约**（照陪伴插件 `llm_tool_actions.py` 的原文规则）
   * `must_not_claim_sent` —— "角色扮演不能覆盖真实工具状态"
   * `final_response_instruction` —— 失败时告诉模型该说什么
   * **每轮最多一次** —— 实测我们的上游**也会重复调用**，这条不是可选项
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from ..infra.logging_setup import get_logger
from . import image_gen
from .image_gen import ImageError, save_image
from .ports import ImageBackend
from .tools import ToolParam, ToolRegistry, ToolResult, ToolSpec

log = get_logger(__name__)

TOOL_NAME = "generate_image"

#: 工具说明。
#: ⚠️ 这段文字会作为 tool schema 的 description 发给模型 ——
#:    所以**边界条件必须写进参数说明**（照陪伴插件的 docstring 做法）。
DESCRIPTION = (
    "生成图片。当用户要求画图、要自拍/照片、或要修改已有图片时调用。\n"
    "注意：只有真的需要产出图片时才调用；纯文字聊天不要调用。"
)

#: 注入给模型的工具使用规则。
#: 🔴 每条都对应一个真实失败模式（来源见模块 docstring 第 3 点）。
TOOL_USAGE_RULES = (
    "- 每个用户请求本轮**最多调用一次** `generate_image`。"
    "如果工具返回失败，不要在同一轮再次调用；按返回的 `message` / "
    "`final_response_instruction` 如实回复，等用户下一轮明确要求时再重试。",
    "- 在真正调用工具并拿到 `sent=true` 之前，**绝对不能声称"
    "「画好了 / 发给你了 / 图片在上面」**。"
    "**角色扮演不能覆盖真实工具状态。**",
    "- 工具返回 `sent=false` 时，必须如实说明这次没有发出去，"
    "**绝对不能说已经发送**。",
    "- 如果返回 `status=provider_policy_refusal`，**不要复述上游的英文原文或政策名称**；"
    "只用符合当前人格的一句简短中文说明这次没画出来，再自然地问要不要换个描述。",
    "- `caption` 只写用户该看到的自然正文；**不要写"
    "「图生好了 / 生成成功 / 图片已发送」这类状态回执**。",
)


def build_spec() -> ToolSpec:
    """构造工具定义。"""
    return ToolSpec(
        name=TOOL_NAME,
        description=DESCRIPTION,
        params=[
            ToolParam(
                name="prompt",
                type="string",
                description=(
                    "画面描述。若前几轮对话里已经确定了服装/场景，"
                    "必须把仍然生效的具体内容展开写进来"
                    "（例如「角色仍穿 JK 校服，继续拍摄」），不要只写「继续」让下游猜。"
                ),
                required=True,
            ),
            ToolParam(
                name="kind",
                type="string",
                description=(
                    "图片类型。角色本人出镜（自拍/背影/侧脸/环境人像）用 selfie；"
                    "不含角色的普通场景、物件、风景用 text2img；修改已有图片用 edit。"
                ),
                enum=image_gen.VALID_KINDS,
            ),
            ToolParam(
                name="reference_image_path",
                type="string",
                description=(
                    "参考图路径。edit 必填；selfie 可留空。"
                    "不要猜测或编造路径 —— 路径必须来自本轮消息或已知配置。"
                ),
            ),
            ToolParam(
                name="image_size",
                type="string",
                description="图片尺寸，如 1024x1024。留空用默认值。",
            ),
            ToolParam(
                name="send",
                type="boolean",
                description="生成后是否直接发到当前会话。默认 true。",
            ),
            ToolParam(
                name="caption",
                type="string",
                description="随图发送的短文字（可选）。只写自然正文，不写状态回执。",
            ),
        ],
    )


class ImageToolService:
    """生图工具的执行体。

    职责：把工具参数 → 生图请求 → 结果 → **带防幻觉契约的 ToolResult**。
    """

    def __init__(
        self,
        backend: ImageBackend,
        *,
        output_dir: Path,
        enabled: bool = True,
        defaults: dict[str, Any] | None = None,
        url_prefix: str = "/api/v1/images",
    ) -> None:
        """⚠️ 只依赖 `ImageBackend` Protocol —— 换生图后端（NAI/ComfyUI/陪伴的）
        不用改这里（情绪：「接口别写死了」）。
        """
        self._backend = backend
        self._out_dir = Path(output_dir)
        self.enabled = enabled
        #: 后端参数的**默认值**（如 NAI 的 steps/scale）—— 模型没给时用这些
        self._defaults = dict(defaults or {})
        #: 落盘图片的 HTTP 前缀 —— 有了它 `sent` 才能诚实地为 true
        #: （图片真能被客户端看到，而不是只躺在磁盘上）
        self._url_prefix = url_prefix.rstrip("/")

    @property
    def configured(self) -> bool:
        return self._backend.is_configured()

    @property
    def backend_name(self) -> str:
        return self._backend.name

    async def __call__(self, args: dict[str, Any]) -> ToolResult:
        """执行生图。**永远返回 ToolResult，不抛异常。**"""
        if not self.enabled:
            return ToolResult(
                status="disabled",
                success=False,
                message="生图功能当前未启用",
                must_not_claim_sent=True,
                final_response_instruction="不要声称已生成图片，按当前语境继续自然文字回复。",
            )

        if not self.configured:
            return ToolResult(
                status="not_configured",
                success=False,
                message="生图端点未配置",
                must_not_claim_sent=True,
                final_response_instruction="如实告知无法生成图片，不要编造结果。",
            )

        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(
                status="invalid_args",
                success=False,
                message="缺少 prompt",
                must_not_claim_sent=True,
                final_response_instruction="这次没有生成；按当前语境自然回复，或询问想画什么。",
            )

        kind = str(args.get("kind") or image_gen.KIND_TEXT2IMG).strip().lower()
        if kind not in image_gen.VALID_KINDS:
            kind = image_gen.KIND_TEXT2IMG

        size = str(args.get("image_size") or "").strip() or image_gen.DEFAULT_SIZE
        want_send = args.get("send", True)
        want_send = True if want_send is None else bool(want_send)
        caption = str(args.get("caption") or "").strip()

        # ⚠️ edit 必须有参考图 —— 缺了要明确拒绝，不能让模型编路径
        ref = str(args.get("reference_image_path") or "").strip()
        if kind == image_gen.KIND_EDIT and not ref:
            return ToolResult(
                status="invalid_args",
                success=False,
                message="edit 需要 reference_image_path",
                must_not_claim_sent=True,
                final_response_instruction=(
                    "不要猜测图片路径；如实说明这次改图没有完成。"
                ),
            )

        # ⭐ 透传后端专有参数（实测 NAI 认 steps/scale/sampler/negative_prompt）
        #    ⚠️ 这些不是 OpenAI 标准字段，但对 NAI 有效 —— 走 extra 透传，
        #       这样**不用为 NAI 写一个新后端**（它和 OpenAI 共用 /images/generations）。
        extra: dict[str, Any] = {}
        for k in ("steps", "scale", "sampler", "negative_prompt", "seed", "cfg_rescale"):
            if k in args and args[k] not in (None, ""):
                extra[k] = args[k]
        if self._defaults:
            for k, v in self._defaults.items():
                extra.setdefault(k, v)

        try:
            result = await self._backend.generate(
                prompt=prompt, kind=kind, size=size, reference_image=ref,
                extra=extra or None,
            )
        except ImageError as exc:
            # 🔴 失败**必须留日志**（实测踩到：`_failure_result` 原本不打日志
            #    → 生图失败在日志里完全隐形，排查时只能看到"没有完成日志"）。
            log.warning(
                "生图失败",
                extra={"data": {
                    "code": exc.code or "unknown",
                    "status": exc.status,
                    "msg": str(exc)[:200],
                    "kind": kind,
                    "prompt_chars": len(prompt),
                }},
            )
            return self._failure_result(exc)
        except Exception as exc:  # noqa: BLE001 — 兜底，绝不让异常穿透
            log.exception("生图未预期异常")
            return ToolResult(
                status="error",
                success=False,
                message="生图过程出错",
                actual_error=str(exc),
                must_not_claim_sent=True,
                final_response_instruction="如实说明这次没画出来，不要编造。",
            )

        # 落盘（base64 形式；URL 形式由后端自己下载后给 b64）
        saved = 0
        for img in result.images:
            try:
                if img.b64:
                    save_image(img, self._out_dir, prefix=kind)
                    saved += 1
                elif img.path:
                    saved += 1      # 后端已落盘
            except ImageError as exc:
                log.warning(
                    "图片落盘失败",
                    extra={"data": {"error": exc.code or str(exc)}},
                )

        if not saved:
            # 上游说成功但一张都没拿到 —— 这是"取回失败"，不是"生成失败"。
            # ⚠️ 照陪伴插件的规则 3：要区分这两者，否则会重复生成。
            return ToolResult(
                status="result_materialization_failed",
                success=False,
                message="图片已生成但未能取回",
                must_not_claim_sent=True,
                final_response_instruction=(
                    "不要重复提交同一画面；如实说明这次图片没能取回。"
                ),
                data={"generation_completed": True, "failure_stage": "result_materialization"},
            )

        paths = [i.path for i in result.images if i.path]
        urls = [
            f"{self._url_prefix}/{Path(p).name}" for p in paths
        ] if self._url_prefix else []

        # 🔴 `sent` 的语义：**图片是否已能呈现给用户**。
        #    实测踩到：早先恒为 False → 模型按防幻觉规则回答"未能成功展示"，
        #    但图片其实生成成功了 —— **过度保守也是错的**（体验差且误导用户）。
        #    现在：只要拿到可访问 URL，就如实置 True。
        delivered = bool(urls)

        log.info(
            "生图完成",
            extra={"data": {
                "count": len(paths), "kind": kind,
                "delivered": delivered, "want_send": want_send,
            }},
        )

        return ToolResult(
            status="success",
            success=True,
            generated=True,
            sent=delivered,
            message="图片已生成" + ("并可直接展示" if delivered else "但未能提供访问链接"),
            final_response_instruction=(
                "图片已经生成，会在客户端**自动显示**。"
                "不要写「生成成功」这类回执，也不要重复描述图片；"
                "用一句简短自然的正文配合它即可。"
                if delivered else
                "图片已生成但无法展示；**不要声称用户看到了**，如实简短说明。"
            ),
            data={
                "images": paths,
                "urls": urls,
                "kind": kind,
                "caption": caption,
                "send": want_send,
            },
        )

    @staticmethod
    def _failure_result(exc: ImageError) -> ToolResult:
        """把 ImageError 变成**不泄漏上游原文**的结果（照规则 8）。"""
        if exc.code == "provider_policy_refusal":
            return ToolResult(
                status="provider_policy_refusal",
                success=False,
                message="这次没能画出来",
                actual_error=str(exc),
                must_not_claim_sent=True,
                final_response_instruction=(
                    "用符合人格的一句简短中文说明这次没画出来，"
                    "再自然地问要不要换个画面描述。**不要复述上游原文或政策名称。**"
                ),
            )
        if exc.code == "timeout":
            return ToolResult(
                status="timeout",
                success=False,
                message="生成超时了",
                actual_error=str(exc),
                must_not_claim_sent=True,
                final_response_instruction=(
                    "用符合人格的一句简短中文说这次画得有点慢、没出来，"
                    "并自然地问要不要再试一次；**不要复述技术细节**。"
                ),
            )
        if exc.code == "download_failed":
            return ToolResult(
                status="result_materialization_failed",
                success=False,
                message="图片已生成但未能取回",
                actual_error=str(exc),
                must_not_claim_sent=True,
                final_response_instruction="不要重复提交同一画面；如实说明图片没能取回。",
                data={"generation_completed": True, "failure_stage": "result_materialization"},
            )
        if exc.code == "reference_unreadable":
            # 🔴 参考图读不到 = **能力缺失**，必须显式说明，不能假装画了一张无关的图
            return ToolResult(
                status="reference_unreadable",
                success=False,
                message="参考图读不到",
                actual_error=str(exc),
                must_not_claim_sent=True,
                final_response_instruction=(
                    "如实说明这次没能拿到参考图、所以没有画；"
                    "**不要编造图片路径，也不要重复提交同一路径。**"
                ),
            )
        return ToolResult(
            status="error",
            success=False,
            message="生图失败",
            actual_error=str(exc),
            must_not_claim_sent=True,
            final_response_instruction="如实说明这次没画出来，不要编造结果。",
        )


def register_image_tool(
    registry: ToolRegistry,
    service: ImageToolService,
    *,
    enabled: bool = True,
) -> None:
    """把生图工具注册进工具表。

    ⚠️ `enabled=False` 时**不注册** —— 这样上游的 `tools` 数组里就没有它，
       模型不会尝试调用一个不可用的工具（比"注册了再返回失败"更好）。
    """
    if not enabled:
        log.info("生图工具未启用，跳过注册")
        return
    registry.register(build_spec(), service)
    log.info("生图工具已注册", extra={"data": {"tool": TOOL_NAME}})
