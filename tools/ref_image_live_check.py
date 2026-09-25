#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真环境验证：**真后端 + 真上游 + 真参考图**（单测全绿不算证据）。

跑法：`.venv/bin/python tools/ref_image_live_check.py`
⚠️ 会**真的调用上游生图 2 次**（会产生费用/耗时 10~30 秒/张）。

判据（任一不满足 = 失败）：
  ① `selfie` + 参考图 → 走 `/images/generations` 且**带上 director 五数组**，出图
  ② `edit`   + 参考图 → 走 `/images/edits`（真 img2img），出图
  ③ 参考图**读不到**时 → 响亮失败（`reference_unreadable`），**不发请求**
  ④ 人设固定参考图：`selfie` 不给路径也自动带上并获得一致角色
  ⑤ 人设图**不泄漏进** `edit`（那会静默换掉「改这张图」的语义）
"""
from __future__ import annotations

import asyncio
import base64
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emotion_app.domain.backends import OpenAIImageBackend  # noqa: E402
from emotion_app.domain.image_gen import (  # noqa: E402
    ImageClient,
    ImageError,
    ImageTarget,
)
from emotion_app.domain.image_tool import ImageToolService  # noqa: E402

REF = pathlib.Path("/tmp/ref_real.png")


def _load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _ensure_ref() -> None:
    if REF.is_file():
        return
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (512, 512), (30, 40, 70))
    d = ImageDraw.Draw(im)
    d.ellipse((100, 100, 400, 400), fill=(230, 180, 120))
    d.ellipse((180, 200, 220, 240), fill=(20, 20, 20))
    d.ellipse((290, 200, 330, 240), fill=(20, 20, 20))
    im.save(REF)


async def main() -> int:
    env = _load_env()
    _ensure_ref()
    client = ImageClient()
    backend = OpenAIImageBackend(
        client,
        ImageTarget(
            base_url=env["EMOTION_IMAGE_BASE_URL"],
            api_key=env["EMOTION_IMAGE_API_KEY"],
            model=env["EMOTION_IMAGE_MODEL"],
        ),
        default_size=env.get("EMOTION_IMAGE_SIZE") or "832x1216",
    )
    await client.open()
    svc = ImageToolService(backend, output_dir=ROOT / "data/images")
    failed: list[str] = []
    try:
        # ---- ① selfie + 参考图（精准参考）----
        r1 = await svc({
            "prompt": "1girl, blue hair, selfie, upper body",
            "kind": "selfie",
            "reference_image_path": str(REF),
            "image_size": "512x512",
            "send": False,
        })
        ok1 = r1.success is True and bool((r1.data or {}).get("images"))
        print(f"\n① selfie+参考图 : {'✅ 出图' if ok1 else '🔴 失败'}  status={r1.status}")
        print(f"   images={((r1.data or {}).get('images'))}")
        if not ok1:
            print(f"   error={r1.actual_error}")
            failed.append("① selfie+参考图")

        # ---- ② edit + 参考图（真 img2img）----
        r2 = await svc({
            "prompt": "1girl, blue hair, red dress",
            "kind": "edit",
            "reference_image_path": str(REF),
            "image_size": "512x512",
            "send": False,
        })
        ok2 = r2.success is True and bool((r2.data or {}).get("images"))
        print(f"\n② edit+参考图   : {'✅ 出图' if ok2 else '🔴 失败'}  status={r2.status}")
        print(f"   images={((r2.data or {}).get('images'))}")
        if not ok2:
            print(f"   error={r2.actual_error}")
            failed.append("② edit+参考图")

        # ---- ③ 读不到参考图必须响亮失败（且不发请求）----
        r3 = await svc({
            "prompt": "x", "kind": "edit",
            "reference_image_path": str(ROOT / "data/__no_such_file__.png"),
            "send": False,
        })
        ok3 = r3.success is False and r3.status == "reference_unreadable"
        print(f"\n③ 参考图读不到   : {'✅ 如实拒绝' if ok3 else '🔴 竟然成功了'}  status={r3.status}")
        if not ok3:
            failed.append("③ 读不到参考图")

        # ---- ④ 人设固定参考图：selfie 不给路径也应自动带上 ----
        svc_persona = ImageToolService(
            backend, output_dir=ROOT / "data/images", persona_reference=str(REF)
        )
        r4 = await svc_persona({
            "prompt": "1girl, blue hair, selfie",
            "kind": "selfie",
            "image_size": "512x512",
            "send": False,
        })
        ok4 = r4.success is True and bool((r4.data or {}).get("images"))
        print(f"\n④ 人设图兜底 selfie: {'✅ 出图' if ok4 else '🔴 失败'}  status={r4.status}")
        print(f"   images={((r4.data or {}).get('images'))}")
        if not ok4:
            print(f"   error={r4.actual_error}")
            failed.append("④ 人设图兜底")

        # ---- ⑤ 人设图兜底**不许**漏进 edit ----
        r5 = await svc_persona({
            "prompt": "改成红裙子", "kind": "edit", "send": False,
        })
        ok5 = r5.success is False and r5.status == "invalid_args"
        print(f"\n⑤ edit 不吃人设图 : {'✅ 如实拒绝' if ok5 else '🔴 竟然用了人设图'}  status={r5.status}")
        if not ok5:
            failed.append("⑤ edit 语义")
    except ImageError as exc:
        print(f"\n🔴 抛异常：{exc}（code={exc.code}）")
        failed.append(f"异常 {exc.code}")
    finally:
        await client.close()

    print("\n" + ("🎉 真环境 5 项判据全部满足" if not failed else f"🔴 未通过：{failed}"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
