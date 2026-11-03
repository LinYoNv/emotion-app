# -*- coding: utf-8 -*-
"""判据：OneBot **图片段必须落成真能取到字节的定位符**。

## 背景（情绪 2026-11-03 报障）

> 「我发现了一个bug就是发图片给她她没回应。」

实测现场（生产日志 + 现场诊断）：

```
组件: cls=Image  type=Image
  file: 7EA9ABE3193C21FCCD56240C7C37C3DB.png      ← 只有文件名，磁盘上没有
  url:  https://multimedia.nt.qq.com.cn/download?appid=1406&fileid=E…
```

那个 url **裸请求直接失败**：

```
HTTP 400 {"retcode":-55030022,"retmsg":"appid is not supported"}
```

它是给 QQ 客户端用的下载入口，要凭据；而 `enableLocalFile2Url: false`，
NapCat 也没把图落到磁盘。于是这个段原样带着 url 往上走 ⇒ 上游去下载 ⇒
拿到 JSON 错误 ⇒ 判定「不是图片」⇒

```
400 unsupported content part: image_url
```

⇒ **整轮回复失败**，用户只看到"她不回我"（她其实回了一句瞎猜的话，
因为请求根本没带图）。

## 本文件守什么

| # | 判据 | 抓的是 |
|---|---|---|
| ① | 调用 `get_image` 后，`data` 里的定位符被换成可用的 | 修复本身 |
| ② | 已有可用定位符时不浪费一次 `call_action` | 别为已好的东西付代价 |
| ③ | `get_image` 全部失败时**不抛异常**、保留原值 | 不能让一条坏图毁掉整条消息 |
| ④ | 多种参数名都要试到（`file` / `file_id` / `id`） | 不同协议端参数名不同 |

⚠️ ③ 尤其重要：这条消息里可能还有文本。图片解析失败就整条丢弃，
    会让"发图配一句话"的场景连字都收不到。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

HOST_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOST_DIR))

from astrbot_host.adapters.onebot import OneBotAdapter  # noqa: E402

QQ_URL = "https://multimedia.nt.qq.com.cn/download?appid=1406&fileid=E1234567890"


class FakeBot:
    """记录调用，并按预设返回。"""

    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def call_action(self, action: str, **params: Any) -> Any:
        self.calls.append({"action": action, **params})
        if not self.replies:
            raise RuntimeError("no more replies")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def adapter_with(bot: FakeBot) -> OneBotAdapter:
    adapter = OneBotAdapter(adapter_id="t", config={"ws_reverse_port": 6399})
    adapter._bot = bot  # 判据直接注入，不起真 WS
    return adapter


async def resolve(data: dict[str, Any], bot: FakeBot) -> dict[str, Any]:
    adapter = adapter_with(bot)
    await adapter._resolve_image_segment(data, {"post_type": "message"}, {})
    return data


# ---------------------------------------------------------------------------
# ① 修复本身：必须真的换成可用的定位符
# ---------------------------------------------------------------------------


async def test_qq_download_url_is_replaced_by_get_image_result():
    """NapCat 的鉴权下载口 → `get_image` 换成本地路径。"""
    bot = FakeBot([{"file": "/app/.config/QQ/nt_data/Pic/2026-11/Ori/abc.png",
                    "url": "http://example.invalid/abc.png"}])
    data = {"file": "7EA9ABE3193C21FCCD56240C7C37C3DB.png", "url": QQ_URL}

    await resolve(data, bot)

    assert bot.calls, "必须调用过 get_image"
    assert bot.calls[0]["action"] == "get_image"
    # 关键：原来的鉴权下载口必须不再被使用
    assert data.get("url") != QQ_URL, "QQ 的鉴权下载 URL 必须被替换掉"
    assert data["file"].startswith("/app/"), f"应当拿到真实路径，实际 {data['file']!r}"


async def test_resolved_file_uri_is_accepted_by_to_media_part():
    """端到端：解析结果必须真的能被 `to_media_part` 认下来。

    ⚠️ 只断言"data 被改了"不够 —— 改成一个下游仍然不认的值等于没修。
    """
    import base64

    from astrbot_host.provider_content import to_media_part

    payload = b"\x89PNG\r\n\x1a\n" + b"x" * 32
    bot = FakeBot([{"file": "base64://" + base64.b64encode(payload).decode()}])
    data = {"file": "x.png", "url": QQ_URL}

    await resolve(data, bot)

    # 下游按 url → file → path 取值，这里模拟同样的顺序
    locator = next((data.get(k) for k in ("url", "file", "path") if data.get(k)), "")
    part = to_media_part("image", locator)
    assert part["type"] == "image_url"
    assert part["image_url"]["url"].startswith("data:image/")


# ---------------------------------------------------------------------------
# ② 已经有可用定位符时不要白问一次
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://multimedia.example.com/real.png",   # 普通 https，不是 QQ 那个口径
    "base64://AAAA",
    "file:///tmp/real.png",
])
async def test_already_usable_locator_skips_get_image(url):
    bot = FakeBot([{"file": "/should/not/matter.png"}])
    data = {"file": "x.png", "url": url}

    await resolve(data, bot)

    assert bot.calls == [], f"{url} 本来就能用，不该再调 get_image"


# ---------------------------------------------------------------------------
# ③ 失败要降级，不能把整条消息毁掉
# ---------------------------------------------------------------------------


async def test_all_get_image_calls_failing_keeps_original_and_does_not_raise():
    bot = FakeBot([RuntimeError("boom")] * 12)
    data = {"file": "x.png", "url": QQ_URL}

    await resolve(data, bot)          # 不抛异常就是通过

    assert data["url"] == QQ_URL, "拿不到新值时保留原样，交给下游给明确原因"
    assert data["file"] == "x.png"


# ---------------------------------------------------------------------------
# ④ 多种参数名都要试（不同协议端不一样）
# ---------------------------------------------------------------------------


async def test_all_supported_parameter_names_are_attempted():
    """前两次都返回空，第三次才成功 —— 必须一直试到成功。"""
    bot = FakeBot([{}, {}, {"file": "/real/path.png"}])
    data = {"file": "abc.png", "url": QQ_URL}

    await resolve(data, bot)

    assert len(bot.calls) >= 3, f"应当依次尝试多种参数名，实际只试了 {len(bot.calls)} 次"
    # 用过的参数名必须互不相同（否则等于同一个请求打三遍）
    keysets = [frozenset(k for k in c if k != "action") for c in bot.calls]
    assert len(set(keysets)) == len(keysets), f"参数名有重复尝试：{keysets}"
    assert data["file"] == "/real/path.png"


# ---------------------------------------------------------------------------
# ⑤ 🔴 **装配判据**：解析必须真的接在消息转换链路上
#
# 这一条是被**注入反证当场抓出来的**：我把 `_convert_message_event` 里的
# `await self._resolve_image_segment(...)` 换成 `pass`，上面 7 条判据**全绿**
# —— 因为它们只测方法本身。这正是本项目反复记的
# 「模块能工作 ≠ 被接上了」：能力写好了却忘了接线，功能根本没生效。
# ---------------------------------------------------------------------------


async def test_convert_message_event_actually_resolves_images():
    """走完整转换入口，断言图片段的定位符被换掉了。

    ⚠️ 必须从 `_convert_message_event` 进来，不能直接调 `_resolve_image_segment`
        —— 否则删掉接线也照样绿（这就是本条存在的理由）。
    """
    bot = FakeBot([{"file": "/app/.config/QQ/nt_data/Pic/2026-11/Ori/abc.png"}])
    adapter = adapter_with(bot)

    from aiocqhttp import Event

    # ⚠️ `_convert_message_event` 读的是 `event.self_id`（属性），
    #    传 dict 会 AttributeError —— 必须用真 Event 构造。
    event = Event.from_payload({
        "post_type": "message",
        "message_type": "private",
        "self_id": 3979182137,
        "message_id": 1,
        "user_id": 3412642082,
        "sender": {"user_id": 3412642082, "nickname": "tester"},
        "raw_message": "",
        "message": [{"type": "image", "data": {"file": "abc.png", "url": QQ_URL}}],
    })

    abm = await adapter._convert_message_event(event, get_reply=False)

    assert bot.calls, "转换消息时必须真的调用过 get_image（说明接线还在）"
    images = [c for c in abm.message if type(c).__name__ == "Image"]
    assert len(images) == 1, f"图片段必须在 abm.message 里，实际 {abm.message}"
    assert "multimedia.nt.qq.com.cn/download" not in str(getattr(images[0], "url", "")), (
        "转换后不能还带着那个不可用的 QQ 下载口"
    )
    assert "Ori/abc.png" in str(getattr(images[0], "file", "")), (
        f"应当带上解析后的真实路径，实际 {getattr(images[0], 'file', '')!r}"
    )
