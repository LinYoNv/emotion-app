# -*- coding: utf-8 -*-
"""共享夹具（`tests/` 全局）。

## 为什么要单独抽出来

原来 `mk` 夹具**只定义在 `tests/test_providers_api.py` 内部**，
于是新加的测试文件（如 `tests/test_provider_presets.py`）**拿不到它** ——
实测报 `fixture 'mk' not found`。

⚠️ 当时的两个"顺手"做法都不好：
  ① 在新文件里**复制一份** `mk` → 两份会漂移（改了 A 忘了 B），
     而这类夹具恰恰是"装配口径"的唯一来源，漂移会让判据测的不是同一个 app；
  ② 在新文件里 `from tests.test_providers_api import mk` → 依赖**测试文件之间**导入，
     收集顺序/循环导入都会咬人。
→ 正确做法就是**提升到 conftest**：一份定义，所有测试文件共享。
   pytest 会自动发现 `tests/conftest.py`，无需 import。

## 夹具口径（与原来逐字一致，没改行为）

`mk` 是**工厂**夹具：`c = await mk()`。
- `login=False` → 造一个**未登录** client（用于验鉴权）；
- `db="x.db"` → 换一个库文件（用于验"重启后仍在"）。
⚠️ 所有 store 在收尾时统一关闭 —— 否则 aiosqlite 的工作线程会在事件循环关闭后
   抛 `RuntimeError: Event loop is closed`，把测试输出淹在噪音里。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emotion_app.infra.config import Config  # noqa: E402
from emotion_app.storage.sqlite_store import SqliteStore  # noqa: E402
from emotion_app.web.app import create_app  # noqa: E402
from emotion_app.web.auth import AuthManager  # noqa: E402

#: 与 `test_providers_api.py` 用同一个令牌（20 字符，满足最小长度校验）。
TOKEN = "t" * 20


class FakeBackend:
    """假上游后端，形状照 `ChatBackend.complete`。

    ⚠️ 命名**不带下划线**：它现在是被多个测试文件共享的公共夹具组件，
       外部会 import 它（`tests/test_provider_presets.py` 就用它造 app）。
    """

    def __init__(self, content: str = "上游回复") -> None:
        self.content = content
        self.calls: list[dict] = []

    async def complete(self, *, messages=None, model=None, tools=None, **kw):  # noqa: ANN001
        self.calls.append({"messages": messages, "model": model})
        return type("R", (), {"content": self.content, "usage": {}})()


@pytest.fixture
async def mk(tmp_path):
    """造 client 的**工厂夹具**：`c = await mk()`。

    ⚠️ 为什么是工厂而不是单例夹具：好几条判据要造**两个** client
       （如"模拟重启"要重开 app），单例夹具做不到。
    """
    opened: list[SqliteStore] = []

    async def _make(*, login: bool = True, db: str = "providers.db"):
        cfg = Config()
        cfg.auth.access_token = TOKEN
        cfg.storage.sqlite_path = str(tmp_path / db)
        store = SqliteStore(cfg.storage.sqlite_path)
        await store.open()
        opened.append(store)

        app = create_app(cfg, store, AuthManager(access_token=TOKEN))
        # 🔴 绝不写进项目 `data/`，更不碰 `/opt/astrbot`（别人 AstrBot 的生产目录）
        app.config["EMOTION_PROVIDERS_PATH"] = str(tmp_path / "data" / "providers.json")
        #: 本轮新增：配置文件页的落盘路径也要隔离到 tmp（否则测试会写项目的 data/）
        app.config["EMOTION_CONFIG_PROFILE_PATH"] = str(
            tmp_path / "data" / "config_profile.json"
        )
        app.config["EMOTION_CHAT_BACKEND"] = FakeBackend()
        c = app.test_client()
        # 测试专用的两个挂件（test_client 是普通对象，可以挂属性）
        c.providers_path = Path(app.config["EMOTION_PROVIDERS_PATH"])  # type: ignore[attr-defined]
        c.app = app                                                     # type: ignore[attr-defined]
        if login:
            await c.post("/api/v1/auth/login", json={"token": TOKEN})
        return c

    try:
        yield _make
    finally:
        for s in opened:
            try:
                await s.close()
            except Exception:  # noqa: BLE001 — 收尾失败不该掩盖真正的断言失败
                pass
