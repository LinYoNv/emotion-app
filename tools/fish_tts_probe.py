#!/usr/bin/env python3
"""Fish Audio TTS 连通性探针 —— 逐层诊断,失败时**说清卡在哪一层**。

为什么需要它(2026-09-21 实测教训):
    直接 `curl -X POST https://api.fish.audio/v1/tts` 会**超时 60 秒**,看起来像
    "key 不对"或"服务挂了"。实际上本机是 **DNS 被污染 + TLS 被 reset**:
      * `/etc/resolv.conf` → `127.0.0.53`(systemd-resolved stub)
      * `api.fish.audio` 解析到 `2a03:2880:f129:83:face:b00c:0:25de`(Meta/Facebook 段,`face:b00c` 是其标志)
      * 换 `--resolve` 到真实 Cloudflare IP 后:**TCP 能连,ClientHello 被 RST**
      * 同机 `cloudflare.com` 正常(200)→ 排除"整机不能上外网"
    → 所以**必须分层报告**,否则会把网络问题误判成凭据问题。

用法:
    python3 tools/fish_tts_probe.py                # 完整探针
    python3 tools/fish_tts_probe.py --text "你好"  # 自定义文本
    python3 tools/fish_tts_probe.py --resolve 104.18.1.100   # 指定 IP 绕开本地 DNS

退出码:0 = 全通;非 0 = 有层失败(逐条打印)。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---- 凭据:优先环境变量,其次 token.md(与项目其它探针一致:不把明文写进代码) ----
_TOKEN_MD = Path("/root/backup/secrets/token.md")
_DEFAULT_URL = "https://api.fish.audio/v1"
_DEFAULT_MODEL = "s2.1-pro-free"


def load_credentials() -> tuple[str, str, str]:
    """从 token.md 读 (key, base_url, model);读不到就报错(不静默用假值)。"""
    key = os.environ.get("FISH_AUDIO_KEY", "").strip()
    url = os.environ.get("FISH_AUDIO_URL", "").strip()
    model = os.environ.get("FISH_AUDIO_MODEL", "").strip()
    if not (key and url and model) and _TOKEN_MD.exists():
        text = _TOKEN_MD.read_text(encoding="utf-8")
        # ⚠️ 不能用 `(?=^\S)` 收尾 —— `key:`/`modul:` 本身就在行首,lookahead 会立刻命中
        # (实测踩过:`block` 只拿到 `'\n'`,于是"拿不到 key")。
        # 正确:从 `fish-audio:` 行起读到**下一个空行**为止(token.md 用空行分段)。
        m = re.search(r"^fish-audio:(\S+)[ \t]*\n((?:[^\n]*\n)*?)(?:\n|\Z)", text, re.M)
        if m:
            url = url or m.group(1).strip()
            block = m.group(2)
            if not key:
                km = re.search(r"^key:\s*(\S+)", block, re.M)
                if km:
                    key = km.group(1).strip()
            if not model:
                mm = re.search(r"^modul:\s*(\S+)", block, re.M)
                if mm:
                    model = mm.group(1).strip()
    if not key:
        sys.exit("❌ 拿不到 Fish Audio key(环境变量 FISH_AUDIO_KEY 与 token.md 都没有)")
    return key, (url or _DEFAULT_URL), (model or _DEFAULT_MODEL)


def step(n: int, title: str) -> None:
    print(f"\n{'=' * 68}\n【{n}】{title}\n{'=' * 68}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="你好，这是一次语音合成测试。")
    ap.add_argument("--resolve", default="", help="指定 IP,绕开本地 DNS(实测必需)")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    key, base_url, model = load_credentials()
    host = urllib.parse.urlsplit(base_url).hostname or "api.fish.audio"
    print(f"目标:{base_url}  model={model}  key={key[:12]}…{key[-4:]}(已脱敏)")

    failures: list[str] = []

    # ---- ① 本地 DNS:最容易骗人的一层 ----
    step(1, "本地 DNS 解析(⚠️ 本机实测被污染,这一步失败【不代表】凭据有问题)")
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        print(f"  解析到:{ips}")
        bogus = [ip for ip in ips if ip.startswith("2a03:2880") or "face:b00c" in ip]
        if bogus:
            print(f"  🔴 污染特征(Meta/Facebook 段):{bogus}")
            print("     → 这是 DNS 投毒,不是 fish.audio 的真实地址")
            failures.append(f"DNS 被污染 → {bogus}")
        else:
            print("  ✅ 未见明显污染特征")
    except OSError as e:
        print(f"  ❌ 解析失败:{e}")
        failures.append(f"DNS 解析失败:{e}")

    # ---- ② 真实 IP(经 DoH) ----
    step(2, "经 DoH 取真实 IP(用于 --resolve 绕开污染)")
    real_ips: list[str] = []
    try:
        req = urllib.request.Request(
            f"https://dns.alidns.com/resolve?name={host}&type=A",
            headers={"accept": "application/dns-json"},
        )
        with urllib.request.urlopen(req, timeout=args.timeout) as r:
            data = json.load(r)
        real_ips = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
        print(f"  ✅ 真实 A 记录:{real_ips or '(空)'}")
    except Exception as e:
        print(f"  ⚠️ DoH 查询失败({e});将只用 --resolve 传入的值")

    target_ip = args.resolve or (real_ips[0] if real_ips else "")
    if not target_ip:
        print("  ⚠️ 没有可用 IP,后续跳过带 --resolve 的测试")
    else:
        print(f"  后续将用 --resolve {host}:443:{target_ip}")

    # ---- ③ TCP ----
    step(3, f"TCP 连通性 → {target_ip or host}:443")
    if target_ip:
        try:
            t0 = time.time()
            with socket.create_connection((target_ip, 443), timeout=args.timeout):
                print(f"  ✅ TCP 通({(time.time() - t0) * 1000:.0f} ms)")
        except OSError as e:
            print(f"  ❌ TCP 不通:{e}")
            failures.append(f"TCP 不通:{e}")

    # ---- ④ TLS 握手(实测就是卡在这一层) ----
    step(4, "TLS 握手(🔴 本机实测被 RST —— 这是真正的拦路虎)")
    tls_ok = False
    if target_ip:
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((target_ip, 443), timeout=args.timeout) as raw:
                with ctx.wrap_socket(raw, server_hostname=host) as ss:
                    print(f"  ✅ TLS 握手成功:{ss.version()}")
                    tls_ok = True
        except ssl.SSLError as e:
            print(f"  ❌ TLS 失败(SSL):{e}")
            failures.append(f"TLS 被中断(疑似 SNI 阻断):{e}")
        except OSError as e:
            print(f"  ❌ TLS 失败(网络):{e}")
            failures.append(f"TLS 网络错误:{e}")

    # ---- ⑤ 对照实验:同机能否到别的站(证明"不是整机断网") ----
    step(5, "对照实验:同机访问 cloudflare.com(证明不是整机不能上网)")
    try:
        with urllib.request.urlopen("https://www.cloudflare.com", timeout=args.timeout) as r:
            print(f"  ✅ cloudflare.com → HTTP {r.status}")
    except Exception as e:
        print(f"  ⚠️ cloudflare.com 也不通({e})→ 可能是整机/出口问题,不是 fish.audio 专属")

    # ---- ⑥ 真实 TTS 调用(只有前面通了才有意义) ----
    step(6, "真实调用 POST /tts(合成一小段语音)")
    if not tls_ok:
        print("  ⏭️  跳过:第 ④ 步 TLS 未通,再试 HTTP 只会同样超时(实测 60s 无响应)")
        print("     这不是凭据问题 —— 请先解决 DNS/TLS 阻断(见文末建议)")
    else:
        body = json.dumps(
            {"text": args.text, "format": "mp3", "model": model},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/tts",
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "model": model,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=args.timeout) as r:
                audio = r.read()
            out = Path("/tmp/fish_tts_probe.mp3")
            out.write_bytes(audio)
            print(f"  ✅ HTTP {r.status},拿到 {len(audio)} 字节 → {out}")
            if len(audio) < 512:
                print(f"  🔴 但只有 {len(audio)} 字节,不像有效音频!")
                failures.append("返回体过小,疑似不是音频")
            else:
                print(f"  头部字节:{audio[:4].hex()} (ID3 或 MPEG 帧头即正常)")
        except urllib.error.HTTPError as e:
            detail = e.read()[:400].decode("utf-8", "replace")
            print(f"  ❌ HTTP {e.code}:{detail}")
            failures.append(f"HTTP {e.code}")
        except Exception as e:
            print(f"  ❌ 调用失败:{type(e).__name__}: {e}")
            failures.append(f"调用失败:{e}")

    # ---- 结论 ----
    print(f"\n{'=' * 68}\n结论\n{'=' * 68}")
    if failures:
        print("❌ 未通过,逐条原因:")
        for f in failures:
            print(f"   - {f}")
        print(
            "\n建议(按代价从低到高):\n"
            "   1) 用可达的网络跑本探针(如 hk3:`sshpass -e ssh root@45.207.220.205`)\n"
            "   2) 修好本机代理(当前 :7897 verge-mihomo 在跑但所有站点都 000 = 节点失效)\n"
            "   3) 给本机换一个不被投毒的 DNS(污染的是系统 stub 127.0.0.53 的返回)\n"
            "   4) 用 fish.audio 之外、本机能直连的 TTS(如果只是想验证 TTS 链路)"
        )
        return 1
    print("✅ 全部通过 —— Fish Audio TTS 凭据有效且链路可达")
    return 0


if __name__ == "__main__":
    sys.exit(main())
