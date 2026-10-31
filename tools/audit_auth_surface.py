# -*- coding: utf-8 -*-
"""公网暴露前的鉴权面实测（**在黑盒层面**验证，不只读代码）。

判据：
  A. 敏感接口**无令牌 → 必须 401**（当前有几条是 200 = 泄露）
  B. 带正确令牌 → 必须 200（否则等于把功能也锁死了）
  C. 带错误令牌 → 必须 401

为什么必须实测：AGENTS.md 记着真事故「新增 API 路由漏 require_auth()
→ 未登录返 200，而服务绑 0.0.0.0 = 公网可读运行日志」。
静态 grep 数不出"这条路由到底有没有真的拦住"。
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

TOKEN = ""
for line in Path("/root/apps/情绪app/.env").read_text(encoding="utf-8").splitlines():
    if line.startswith("EMOTION_AUTH_ACCESS_TOKEN"):
        TOKEN = line.split("=", 1)[1].strip().strip('"').strip("'")
        break

BASE = "http://127.0.0.1:6191"

#: 需要鉴权的接口（这些一旦公开可读，就是数据泄露）
PROTECTED = [
    "/api/v1/status",
    "/api/v1/plugins",
    "/api/v1/adapters",
    "/api/v1/providers",
    "/api/v1/images/nonexistent.png",
    "/v1/models",
    "/api/plug/astrbot_plugin_private_companion/setup/status",
    "/api/plug/astrbot_plugin_private_companion/host/status",
    "/api/v1/sessions",
    "/api/v1/logs",
]

#: 设计上公开的（页面壳 + 健康检查）
PUBLIC = ["/health", "/"]


def call(path: str, token: str | None) -> tuple[int, int, str]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(BASE + path, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            body = resp.read()
            return resp.status, len(body), body[:120].decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, 0, ""
    except Exception as e:  # noqa: BLE001
        return 0, 0, f"{type(e).__name__}: {e}"


print("=" * 72)
print("A. 敏感接口：无令牌必须 401")
print("=" * 72)
leaks = []
for p in PROTECTED:
    code, _, _ = call(p, None)
    ok = code in (401, 403)
    if not ok:
        leaks.append((p, code))
    print(f"  {'OK ' if ok else '★LEAK'} {code:>4}  {p}")

print()
print("=" * 72)
print("B. 敏感接口：正确令牌应非 401")
print("=" * 72)
for p in PROTECTED:
    code, n, _ = call(p, TOKEN)
    print(f"  {code:>4}  {p}  ({n} bytes)")

print()
print("=" * 72)
print("C. 错误令牌必须 401")
print("=" * 72)
for p in PROTECTED:
    code, _, _ = call(p, "wrong-token-should-fail")
    ok = code in (401, 403)
    print(f"  {'OK ' if ok else '★BAD'} {code:>4}  {p}")

print()
print("=" * 72)
print("D. 设计上公开的接口")
print("=" * 72)
for p in PUBLIC:
    code, n, snippet = call(p, None)
    print(f"  {code:>4}  {p}  ({n} bytes)  {snippet[:60]!r}")

print()
print("=" * 72)
if leaks:
    print(f"❌ 有 {len(leaks)} 条敏感接口**无令牌可访问**，绑公网前必须修：")
    for p, c in leaks:
        print(f"     {c}  {p}")
else:
    print("✅ 所有敏感接口在无令牌时都被拒绝")
print("=" * 72)
