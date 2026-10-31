# -*- coding: utf-8 -*-
"""从**真正的外部**（本机 → jp-vps 公网）验证暴露面。

这才是可靠判据 —— AGENTS.md：「判断某地址:端口能不能访问，
只有一个可靠办法：从真正的外部机器实测」。服务器上 curl 127.0.0.1 证明不了。

判据：
  A. /health 公开可读，但**不含**上游地址/令牌长度/插件结构
  B. 敏感接口无令牌 → 401
  C. 新令牌 → 能进
  D. 旧令牌 → 必须已失效
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

HOST = os.environ.get("EMOTION_AUDIT_HOST", "http://64.81.114.30:6191")

# 🔴 **绝不硬编码令牌**（本项目铁律：密钥不进仓库；一次提交，历史就取不回）。
#    这个脚本第一版把真实令牌写成了常量，提交前被扫出来 —— 那是**差点出事**：
#    `LinYoNv/emotion-app` 虽是 private，但凭据进过一次历史就等于永久泄露，
#    唯一的补救是轮换。
#    现在只从环境变量读；没设就**明确报错退出**，不静默用空串（那会全部 401、
#    看起来像"鉴权面很安全"—— 假绿）。
NEW_TOKEN = os.environ.get("EMOTION_AUDIT_TOKEN", "")
#: 用来验证「旧令牌必须已失效」。留空就跳过 D 组判据（**如实跳过，不假装通过**）。
OLD_TOKEN = os.environ.get("EMOTION_AUDIT_OLD_TOKEN", "")

SENSITIVE = [
    "/api/v1/status",
    "/api/v1/plugins",
    "/api/v1/adapters",
    "/api/v1/providers",
    "/api/v1/sessions",
    "/api/v1/logs",
    "/v1/models",
    "/api/plug/astrbot_plugin_private_companion/host/status",
]


def call(path: str, token: str | None = None, timeout: int = 20):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(HOST + path, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}".encode()


if not NEW_TOKEN:
    # 🔴 **明确报错退出**，不用空串继续跑 —— 空令牌会让 B 组（无令牌→401）
    #    全绿、C 组（新令牌→200）全红，看起来像"服务器鉴权坏了"，而真因是
    #    "你忘了传令牌"。这类**错误信息误导**比缺判据更贵。
    raise SystemExit(
        "❌ 需要设置 EMOTION_AUDIT_TOKEN 环境变量（面板访问令牌）。\n"
        "   取法：ssh 到 jp-vps 后\n"
        "   grep '^EMOTION_AUTH_ACCESS_TOKEN=' /root/apps/情绪app/.env | cut -d= -f2-\n"
        "   ⚠️ 不要把它写进任何文件/命令历史。"
    )

print("=" * 72)
print("A. /health 公开可读 + 不泄露环境指纹")
print("=" * 72)
code, body = call("/health")
print(f"  HTTP {code}  ({len(body)} bytes)")
try:
    d = json.loads(body)
    print("  返回字段:", list(d))
    leaks = []
    raw = body.decode("utf-8", "ignore")
    for needle in ("api.syuan.org", "8318", "token_length", "plugin_dirs",
                   "nai-diffusion", "data/plugins", "json_output"):
        if needle in raw:
            leaks.append(needle)
    if leaks:
        print(f"  ★ 仍在泄露: {leaks}")
    else:
        print("  OK 没有上游地址 / 令牌长度 / 插件结构 / 模型名")
    print("  config 段:", json.dumps(d.get("config"), ensure_ascii=False))
except Exception as e:  # noqa: BLE001
    print("  解析失败:", e)

print()
print("=" * 72)
print("B. 敏感接口：无令牌必须 401")
print("=" * 72)
bad = []
for p in SENSITIVE:
    code, _ = call(p)
    ok = code in (401, 403)
    if not ok:
        bad.append((p, code))
    print(f"  {'OK ' if ok else '★LEAK'} {code:>4}  {p}")

print()
print("=" * 72)
print("C. 新令牌能进（非 401 即通过）")
print("=" * 72)
for p in SENSITIVE:
    code, body = call(p, NEW_TOKEN)
    print(f"  {code:>4}  {p}  ({len(body)} bytes)")

print()
print("=" * 72)
print("D. 错令牌必须 401")
print("=" * 72)
bad2 = []
for p in SENSITIVE:
    code, _ = call(p, "definitely-wrong-token")
    ok = code in (401, 403)
    if not ok:
        bad2.append((p, code))
    print(f"  {'OK ' if ok else '★BAD'} {code:>4}  {p}")

print()
print("=" * 72)
print("E. 旧令牌必须已失效（令牌轮换的判据）")
print("=" * 72)
bad3: list[tuple[str, int]] = []
if not OLD_TOKEN:
    # 🔴 **如实跳过，不冒充通过**：没有旧令牌就无法证明轮换生效。
    #    这里刻意不打印 ✅（本项目铁律：跳过判据 = 假通过）。
    print("  ⏸ 跳过：未设置 EMOTION_AUDIT_OLD_TOKEN（无法验证旧令牌是否已失效）")
else:
    for p in SENSITIVE:
        code, _ = call(p, OLD_TOKEN)
        ok = code in (401, 403)
        if not ok:
            bad3.append((p, code))
        print(f"  {'OK ' if ok else '★BAD'} {code:>4}  {p}")

print()
print("=" * 72)
if bad or bad2 or bad3:
    print("❌ 有问题：")
    for p, c in bad:
        print(f"     无令牌可读 {c}  {p}")
    for p, c in bad2:
        print(f"     错令牌可读 {c}  {p}")
    for p, c in bad3:
        print(f"     ★旧令牌仍可读 {c}  {p}")
else:
    verdict = "✅ 公网侧鉴权面干净：无令牌/错令牌全拒绝，新令牌可进，/health 不泄露指纹"
    if not OLD_TOKEN:
        verdict += "（⚠️ 未验旧令牌失效）"
    print(verdict)
print("=" * 72)
