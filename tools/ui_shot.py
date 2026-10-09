#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给面板各页拍**真实截图**（真 Chrome + CDP），用于人工核对观感。

🔴 判据只证明"某个属性等于某值"，证明不了"看起来对不对"。
   本项目通则「验证要看到实物」——CSS 改版必须人眼看图，
   所以这个工具产出 PNG 而不是 ok/fail。

用法：python3 tools/ui_shot.py [base] [outdir]
"""
from __future__ import annotations
import base64, json, re, subprocess, sys, time, urllib.request
from pathlib import Path
import websocket  # type: ignore

CHROME = "/usr/bin/google-chrome"
ROOT = Path(__file__).resolve().parent.parent


def token() -> str:
    env = (ROOT / ".env").read_text(encoding="utf-8")
    return re.search(r"^EMOTION_AUTH_ACCESS_TOKEN=(.+)$", env, re.M).group(1).strip().strip("'")


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6191/"
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/uishot")
    out.mkdir(parents=True, exist_ok=True)
    port = 9251
    ch = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={port}",
                           "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                           "--remote-allow-origins=*", "--window-size=1440,900", "about:blank"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        t = None
        for _ in range(40):
            time.sleep(.25)
            try:
                t = next((x for x in json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list", timeout=5).read()) if x["type"] == "page"), None)
                if t:
                    break
            except Exception:
                pass
        ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=120)
        I = [0]

        def send(m, **p):
            I[0] += 1
            ws.send(json.dumps({"id": I[0], "method": m, "params": p}))
            while True:
                r = json.loads(ws.recv())
                if r.get("id") == I[0]:
                    return r.get("result", {})

        def ev(e, aw=False):
            return send("Runtime.evaluate", expression=e, awaitPromise=aw,
                        returnByValue=True).get("result", {}).get("value")

        send("Page.enable"); send("Runtime.enable")
        send("Page.navigate", url=base); time.sleep(2.2)
        ev("document.getElementById('login-token').value=%s;document.getElementById('login-form').requestSubmit()"
           % json.dumps(token()))
        time.sleep(2.5)

        shots = [
            ("01-chat", "#/chat", 1440, 900, False),
            ("02-session", "#/session", 1440, 900, False),
            ("03-persona", "#/persona", 1440, 900, False),
            ("04-plugins", "#/plugins", 1440, 900, False),
            ("05-providers", "#/providers", 1440, 900, False),
            ("06-logs", "#/logs", 1440, 900, False),
            ("07-settings", "#/settings", 1440, 900, False),
            ("10-mobile-chat", "#/chat", 390, 780, True),
            ("11-mobile-nav", "#/persona", 390, 780, True),
        ]
        for name, route, w, h, mobile in shots:
            send("Emulation.setDeviceMetricsOverride", width=w, height=h,
                 deviceScaleFactor=1, mobile=mobile)
            if mobile and name == "11-mobile-nav":
                pass
            send("Page.navigate", url=base); time.sleep(1.0)
            ev("document.getElementById('login-token').value=%s;document.getElementById('login-form').requestSubmit()"
               % json.dumps(token())) if False else None
            time.sleep(0.8)
            ev(f"location.hash={json.dumps(route)}"); time.sleep(1.6)
            if mobile and name == "11-mobile-nav":
                ev("document.getElementById('mobile-menu').click()"); time.sleep(0.7)
            r = send("Page.captureScreenshot", format="png")
            (out / f"{name}.png").write_bytes(base64.b64decode(r["data"]))
            print(f"  {out / (name + '.png')}")
        ws.close()
    finally:
        ch.terminate()
        try:
            ch.wait(timeout=5)
        except Exception:
            ch.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
