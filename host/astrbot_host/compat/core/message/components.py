# -*- coding: utf-8 -*-
"""`astrbot.core.message.components` —— 消息段模型。

⭐⭐ **字段照真 AstrBot `core/message/components.py` 逐个对齐**（不是猜的）。

⚠️ **重要差异（必须记录）**：真实现基于 **pydantic `BaseModel`**；
   本项目依赖刻意只有 quart/hypercorn/aiosqlite/httpx，**不为兼容引入 pydantic**，
   所以这里用 `dataclass` 等价替代。差异与影响：

   | 能力 | 真实现(pydantic) | 我们(dataclass) |
   |---|---|---|
   | 构造时校验/强转 | ✅ 自动 | ❌ 不校验（**按传入原样**） |
   | `.toDict()` | ✅ | ✅ 已实现 |
   | `model_dump()` | ✅ | ⚠️ 显式 `NotImplementedError` |
   | 未知字段 | 忽略 | 忽略 |

   → **不校验**是刻意的：宁可让插件拿到它自己传的值，
     也不要在宿主侧静默改数据（项目铁律「别静默改用户数据」）。

覆盖 24 个组件中的**全部类名**（缺一个就可能让 `import *` 失败），
字段只对齐**被实际用到**的那些。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ComponentType(str, Enum):
    """组件类型（照真实现，值逐字一致 —— 插件会拿字符串比）。"""

    Plain = "Plain"
    Image = "Image"
    Record = "Record"
    Video = "Video"
    File = "File"
    Face = "Face"
    At = "At"
    Node = "Node"
    Nodes = "Nodes"
    Poke = "Poke"
    Reply = "Reply"
    Forward = "Forward"
    RPS = "RPS"
    Dice = "Dice"
    Shake = "Shake"
    Share = "Share"
    Contact = "Contact"
    Location = "Location"
    Music = "Music"
    Json = "Json"
    Unknown = "Unknown"


@dataclass
class BaseMessageComponent:
    """所有消息段的基类（照真实现的最小等价物）。"""

    type: ComponentType = ComponentType.Unknown

    def toDict(self) -> dict[str, Any]:
        return {"type": str(self.type.value), "data": {}}

    async def to_dict(self) -> dict[str, Any]:
        return self.toDict()

    def model_dump(self, **_: Any) -> dict[str, Any]:
        """⚠️ pydantic 的 API —— 我们**不静默模拟**，直接报错。

        理由：假装支持会让"该方法的真实语义"含糊；插件的调用点很少，
        报错后能立刻定位并补一个真实等价物。
        """
        raise NotImplementedError(
            "宿主兼容层用 dataclass 替代 pydantic，未提供 `model_dump`。"
            "若插件依赖它，请在宿主侧按需补实现（而不是让它拿到假数据）。"
        )


# ---------------------------------------------------------------------------
# 基本段
# ---------------------------------------------------------------------------


@dataclass
class Plain(BaseMessageComponent):
    """纯文本（**用得最多**）。"""

    text: str = ""
    type: ComponentType = ComponentType.Plain

    def __init__(self, text: str = "", convert: bool = True, **_: Any) -> None:
        # ⚠️ 真实现接受并忽略 `convert` —— 保留它以免插件传参报错
        self.text = text
        self.type = ComponentType.Plain

    def toDict(self) -> dict[str, Any]:
        return {"type": "text", "data": {"text": self.text}}

    async def to_dict(self) -> dict[str, Any]:
        return self.toDict()

    def __str__(self) -> str:
        return self.text


@dataclass
class Face(BaseMessageComponent):
    """QQ 表情（照真实现）。"""

    id: int = 0
    type: ComponentType = ComponentType.Face

    def __init__(self, id: int = 0, **_: Any) -> None:
        self.id = id
        self.type = ComponentType.Face


@dataclass
class Record(BaseMessageComponent):
    """语音（**27 次** —— 陪伴的 TTS 会发它）。"""

    file: str | None = ""
    url: str | None = ""
    text: str | None = None
    type: ComponentType = ComponentType.Record

    def __init__(
        self,
        file: str | None = "",
        url: str | None = "",
        text: str | None = None,
        **_: Any,
    ) -> None:
        self.file = file
        self.url = url
        self.text = text
        self.type = ComponentType.Record

    def toDict(self) -> dict[str, Any]:
        return {"type": "record", "data": {"file": self.file, "url": self.url}}


@dataclass
class Video(BaseMessageComponent):
    file: str | None = ""
    url: str | None = ""
    type: ComponentType = ComponentType.Video

    def __init__(self, file: str | None = "", url: str | None = "", **_: Any) -> None:
        self.file = file
        self.url = url
        self.type = ComponentType.Video


@dataclass
class File(BaseMessageComponent):
    name: str | None = ""
    file: str | None = ""
    url: str | None = ""
    type: ComponentType = ComponentType.File

    def __init__(
        self,
        name: str | None = "",
        file: str | None = "",
        url: str | None = "",
        **_: Any,
    ) -> None:
        self.name = name
        self.file = file
        self.url = url
        self.type = ComponentType.File


# ---------------------------------------------------------------------------
# IM 相关段
# ---------------------------------------------------------------------------


@dataclass
class At(BaseMessageComponent):
    """@ 某人（**35 次**）。"""

    qq: int | str = ""
    name: str | None = ""
    type: ComponentType = ComponentType.At

    def __init__(
        self, qq: int | str = "", name: str | None = "", **_: Any
    ) -> None:
        self.qq = qq
        self.name = name
        self.type = ComponentType.At

    def toDict(self) -> dict[str, Any]:
        return {"type": "at", "data": {"qq": self.qq, "name": self.name}}


@dataclass
class AtAll(At):
    """@ 全体。"""

    def __init__(self, **_: Any) -> None:
        super().__init__(qq="all", name="全体成员")


@dataclass
class Reply(BaseMessageComponent):
    """引用回复（**33 次**）。"""

    id: str | None = ""
    text: str | None = ""
    type: ComponentType = ComponentType.Reply

    def __init__(
        self, id: str | None = "", text: str | None = "", **_: Any
    ) -> None:
        self.id = id
        self.text = text
        self.type = ComponentType.Reply


@dataclass
class Image(BaseMessageComponent):
    """图片（**41 次**）—— 陪伴的生图结果会以它投递。"""

    file: str | None = ""
    url: str | None = ""
    path: str | None = ""
    type: ComponentType = ComponentType.Image

    def __init__(
        self,
        file: str | None = "",
        url: str | None = "",
        path: str | None = "",
        **_: Any,
    ) -> None:
        self.file = file
        self.url = url
        self.path = path
        self.type = ComponentType.Image

    @staticmethod
    def fromURL(url: str, **kwargs: Any) -> "Image":
        """从 URL 构造（照真实现的静态方法）。"""
        return Image(file=url, url=url, **kwargs)

    @staticmethod
    def fromFileSystem(path: str, **kwargs: Any) -> "Image":
        """从本地文件构造（照真实现）。"""
        return Image(file=path, path=path, **kwargs)

    def toDict(self) -> dict[str, Any]:
        return {
            "type": "image",
            "data": {"file": self.file, "url": self.url, "path": self.path},
        }


@dataclass
class Poke(BaseMessageComponent):
    """拍一拍（陪伴的 `reaction_expression` 可能用）。"""

    type: ComponentType = ComponentType.Poke
    id: int | str = ""

    def __init__(self, id: int | str = "", **_: Any) -> None:
        self.id = id
        self.type = ComponentType.Poke


@dataclass
class Forward(BaseMessageComponent):
    """转发消息（陪伴有 `forward_message` 模块）。"""

    id: str | None = ""
    type: ComponentType = ComponentType.Forward

    def __init__(self, id: str | None = "", **_: Any) -> None:
        self.id = id
        self.type = ComponentType.Forward


@dataclass
class Node(BaseMessageComponent):
    """合并转发的单个节点。"""

    name: str = ""
    uin: str = ""
    content: list[Any] = field(default_factory=list)
    type: ComponentType = ComponentType.Node

    def __init__(
        self,
        name: str = "",
        uin: str = "",
        content: list[Any] | None = None,
        **_: Any,
    ) -> None:
        self.name = name
        self.uin = uin
        self.content = content or []
        self.type = ComponentType.Node


@dataclass
class Nodes(BaseMessageComponent):
    """合并转发（多个 Node）。"""

    nodes: list[Any] = field(default_factory=list)
    type: ComponentType = ComponentType.Nodes

    def __init__(self, nodes: list[Any] | None = None, **_: Any) -> None:
        self.nodes = nodes or []
        self.type = ComponentType.Nodes


@dataclass
class Json(BaseMessageComponent):
    json: str | dict | None = None
    type: ComponentType = ComponentType.Json

    def __init__(self, json: str | dict | None = None, **_: Any) -> None:
        self.json = json
        self.type = ComponentType.Json


@dataclass
class Unknown(BaseMessageComponent):
    """未知段（宿主无法识别时用它兜住，**不丢消息**）。"""

    type: ComponentType = ComponentType.Unknown
    raw: Any = None


# ---- 其余类型：真实现里标了 TODO，这里保留类名以免 import * 失败 ----

@dataclass
class RPS(BaseMessageComponent):
    type: ComponentType = ComponentType.RPS


@dataclass
class Dice(BaseMessageComponent):
    type: ComponentType = ComponentType.Dice


@dataclass
class Shake(BaseMessageComponent):
    type: ComponentType = ComponentType.Shake


@dataclass
class Share(BaseMessageComponent):
    url: str | None = ""
    title: str | None = ""
    type: ComponentType = ComponentType.Share

    def __init__(
        self, url: str | None = "", title: str | None = "", **_: Any
    ) -> None:
        self.url = url
        self.title = title
        self.type = ComponentType.Share


@dataclass
class Contact(BaseMessageComponent):
    type: ComponentType = ComponentType.Contact


@dataclass
class Location(BaseMessageComponent):
    lat: float | None = None
    lon: float | None = None
    type: ComponentType = ComponentType.Location


@dataclass
class Music(BaseMessageComponent):
    url: str | None = ""
    type: ComponentType = ComponentType.Music


__all__ = [
    "At", "AtAll", "BaseMessageComponent", "ComponentType", "Contact", "Dice",
    "Face", "File", "Forward", "Image", "Json", "Location", "Music", "Node",
    "Nodes", "Plain", "Poke", "RPS", "Record", "Reply", "Shake", "Share",
    "Unknown", "Video",
]
