# -*- coding: utf-8 -*-
"""子代理后端的**具体实现**：把任务发到用户自己的 agent 平台。

## 分层

| 层 | 文件 | 职责 |
|---|---|---|
| 协议 | `host/astrbot_host/subagent.py` | `SubagentBackend.run()` 的形状 |
| 实现 | **本文件** | HTTP / DSH 的具体发包 |
| 配置 | `emotion_app/domain/subagents.py` | 用户填的 endpoint/token |

⚠️ `host/` 不许 import `emotion_app`（单向依赖铁律），
   所以**实现放在 web 层**，由装配层注入给宿主。

## 契约（我们自己定的，写清楚给用户）

请求：`POST <endpoint>`
```json
{"input": "任务描述", "system": "子代理指令", "tools": ["a","b"], "agent": "名字"}
```
响应：任意 2xx，body 是 JSON
```json
{"text": "结果"}
```
也接受纯文本 body（很多人会直接 `return "结果"`）。

## 🔴 为什么失败必须抛异常

`HandoffTool` 会把返回值原样当工具结果交给主模型。
返回空串 = 告诉模型「佣人说没事」⇒ 它会**编造一个结果**。
所以这里所有失败路径都抛，且带上**可行动**的原因（对面 401？超时？没配地址？）。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _host() -> Any:
    """延迟取宿主模块。

    🔴 为什么不在顶层 import：`tests/` 的 sys.path 里没有 `host/`
       （`pyproject.toml` 的 `testpaths` 只加 `tests`），顶层导入会让
       **整个模块**在测试里 import 失败。生产由 `__main__` 侧保证路径。
    """
    import astrbot_host.subagent as module

    return module


def _types() -> tuple[Any, Any, Any]:
    module = _host()
    return module.SubagentRequest, module.SubagentResult, module.SubagentUnavailable


class HttpSubagentBackend:
    """通用 HTTP：把任务 POST 给用户的 agent 服务。

    这是**最通用**的一种 —— 自己写的服务、n8n、Dify、Coze 都能接。
    """

    def __init__(self, *, endpoint: str, token: str = "", timeout: float = 120.0) -> None:
        self.name = "http"
        self._endpoint = endpoint
        self._token = token
        self._timeout = timeout

    async def run(self, request: SubagentRequest) -> SubagentResult:
        import httpx

        if not self._endpoint:
            raise _types()[2](
                "子代理后端是 HTTP，但没有配置接口地址：请在面板里填「接口地址」。"
            )

        payload = {
            "input": request.prompt,
            "system": request.system_prompt,
            "tools": list(request.tools),
            "agent": request.name,
        }
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(self._endpoint, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise _types()[2](
                f"子代理在 {self._timeout:.0f} 秒内没有返回（可以调大超时，或检查对方是否卡住）。"
            ) from exc
        except httpx.HTTPError as exc:
            raise _types()[2](
                f"连不上子代理接口：{type(exc).__name__}: {exc}"
            ) from exc

        if response.status_code == 401 or response.status_code == 403:
            raise _types()[2](
                f"子代理接口拒绝访问（HTTP {response.status_code}）："
                "检查访问令牌是否正确、是否有权限。"
            )
        if response.status_code >= 400:
            body = (response.text or "")[:200]
            raise _types()[2](
                f"子代理接口返回 HTTP {response.status_code}：{body or '（空响应体）'}"
            )

        return _types()[1](text=_extract_text(response), backend=self.name)


class DshSubagentBackend(HttpSubagentBackend):
    """DSH 会话桥接。

    ⚠️ **这一版是预留**：接口还没定稿，行为等同通用 HTTP。
       等 DSH 的会话桥接协议确定后在这里补真实适配，**不影响其它后端**。
    """

    def __init__(self, *, endpoint: str, token: str = "", timeout: float = 300.0) -> None:
        super().__init__(endpoint=endpoint, token=token, timeout=timeout)
        self.name = "dsh"


def _extract_text(response: Any) -> str:
    """从响应里取结果文本。

    🔴 取不到就**抛**，不返回空串（理由见模块头）。
    """
    body = None
    try:
        body = response.json()
    except ValueError:
        # 不是 JSON —— 很多人会直接返回纯文本，这是合法用法
        text = (response.text or "").strip()
        if text:
            return text
        raise _types()[2](
            "子代理返回了空响应：对面服务跑完了，但没有任何输出。"
        ) from None

    if isinstance(body, dict):
        for key in ("text", "output", "result", "content", "message"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value
        raise _types()[2](
            "子代理返回的 JSON 里没有可识别的结果字段"
            "（支持 text / output / result / content / message）。"
            f"实际拿到的键：{', '.join(sorted(body)) or '无'}"
        )
    if isinstance(body, str) and body.strip():
        return body
    raise _types()[2]("子代理返回的 JSON 结构无法识别（既不是对象也不是字符串）。")


def build_backend(connection: dict[str, Any]) -> Any:
    """按用户配置造一个后端实例。**未知类型如实回退到 Null**并记日志。"""
    NullBackend = _host().NullBackend

    backend_id = str(connection.get("backend") or "null")
    endpoint = str(connection.get("endpoint") or "").strip()
    token = str(connection.get("token") or "").strip()
    timeout = float(connection.get("timeout_seconds") or 120)

    if backend_id == "null":
        return NullBackend()
    if backend_id == "http":
        return HttpSubagentBackend(endpoint=endpoint, token=token, timeout=timeout)
    if backend_id == "dsh":
        return DshSubagentBackend(endpoint=endpoint, token=token, timeout=timeout)

    logger.warning(
        "未知的子代理后端 %r，已回退为「未接入」（不猜）", backend_id
    )
    return NullBackend()


async def probe_backend(connection: dict[str, Any]) -> dict[str, Any]:
    """连通性探测 —— **真连一次**，把结果原样讲清楚。

    返回 `{"ok": bool, "backend": str, "reason": str, "detail": {...}}`。
    ⚠️ `ok=False` 时 `reason` 必须是**人能照着修**的话。
    """
    backend_id = str(connection.get("backend") or "null")
    endpoint = str(connection.get("endpoint") or "").strip()

    if backend_id == "null":
        return {
            "ok": False,
            "backend": "null",
            "reason": "当前是「未接入」：还没有选择要连接的 agent 平台。",
            "detail": {},
        }
    if not endpoint:
        return {
            "ok": False,
            "backend": backend_id,
            "reason": "没有填接口地址，无法测试。",
            "detail": {},
        }

    probe = _types()[0](
        name="__probe__",
        prompt="这是一次连通性测试，请回复任意内容即可。",
        system_prompt="你是连通性测试端点，请简短回复确认收到。",
    )
    try:
        result = await build_backend(connection).run(probe)
    except _types()[2] as exc:
        return {"ok": False, "backend": backend_id, "reason": str(exc), "detail": {}}
    except Exception as exc:  # noqa: BLE001 — 探测要覆盖一切，如实回报
        logger.exception("子代理连通性探测异常")
        return {
            "ok": False,
            "backend": backend_id,
            "reason": f"探测时出错：{type(exc).__name__}: {exc}",
            "detail": {},
        }
    return {
        "ok": True,
        "backend": backend_id,
        "reason": f"已连通，对方返回 {len(result.text or '')} 个字符。",
        "detail": {"elapsed_ms": result.elapsed_ms, "preview": (result.text or "")[:200]},
    }
