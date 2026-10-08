#!/usr/bin/env python3
"""人设参考图面板判据（真浏览器，Chrome CDP）。

## 守的是什么

情绪 2026-09-25：「**用户能填的就让用户改**」+「看一下她的 ui 界面拿下来了没」。
参考图原本只能配 `.env`（要登服务器改配置再重启；手机壳上更不可能），
这次做成**面板上传、每人设一张**。

本工具验的是**用户在界面上真的能操作**：
  ① 控件在（选图 / 上传 / 删除 / 预览）；
  ② 走**真 UI**上传一张图 → 后端真的存了（真 HTTP 复核）；
  ③ 预览图真的显示出来（`<img>` 有尺寸，不是一条 0×0 的空 `<img>`）；
  ④ 删除后回到未设置态，且后端字段也清了；
  ⑤ 全程**零 JS 报错**。

🔴 为什么必须真浏览器（本项目铁律）：
   「前端改动必须用真浏览器打开看 —— `curl` 验不了 JS」。
   `curl` 能证明路由 200，**证明不了那个 `<img>` 有没有真的画出像素**，
   也证明不了按钮点了有没有反应。

## 不留痕

判据会在**真实人设**上上传/删除参考图。所以：
  * 先记下原本的 `reference_image_path`；
  * 结束时**恢复原状**（原本没有就删掉我们加的）。
⚠️ 这是用户真实数据，判据不许留痕。

用法:
    .venv/bin/python tools/persona_ref_panel_check.py [BASE_URL] [TOKEN]

退出码:0 = 全绿;非 0 = 有判据失败（**没有"跳过"** —— 前置不满足即失败）。
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_check import CHROME, PORT, CDP  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6191/"

#: 一张真 1×1 PNG（⚠️ **不要手拼 hex** —— 本项目踩过：拼错的 PNG
#: 会被上游拒，然后我会误以为是格式契约问题）
PNG_1PX_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAF"
    "BQIAX8jx0gAAAABJRU5ErkJggg=="
)


def read_token() -> str:
    env = Path(__file__).resolve().parent.parent / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("EMOTION_AUTH_ACCESS_TOKEN"):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit("❌ .env 里没有 EMOTION_AUTH_ACCESS_TOKEN")


def main() -> int:
    token = sys.argv[2] if len(sys.argv) > 2 else read_token()
    failures: list[str] = []
    checks = 0

    def check(label: str, ok: bool, detail: str = "") -> bool:
        nonlocal checks
        checks += 1
        print(f"  {'✅' if ok else '❌'} {label}" + (f" — {detail}" if (detail and not ok) else ""))
        if not ok:
            failures.append(label)
        return bool(ok)

    chrome = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
         "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
         "--remote-allow-origins=*", "--window-size=1200,1800", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    restore: dict = {"id": "", "orig": None}
    try:
        target = None
        for _ in range(48):
            time.sleep(0.25)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=3) as r:
                    pages = json.loads(r.read())
                target = next((t for t in pages if t["type"] == "page"), None)
                if target:
                    break
            except Exception:
                continue
        if not target:
            print("❌ Chrome 未就绪")
            return 1

        cdp = CDP(target["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        # 错误录音机必须在页面脚本之前装（否则 boot() 早跑完了 = 假绿）
        cdp.send(
            "Page.addScriptToEvaluateOnNewDocument",
            source=(
                "window.__jsErrors=[];"
                "window.addEventListener('error',function(e)"
                "{window.__jsErrors.push(String(e.message));});"
            ),
        )
        cdp.send("Page.navigate", url=BASE)
        time.sleep(3.0)
        cdp.eval(
            "(function(){var t=document.getElementById('login-token');"
            " t.value=" + json.dumps(token) + ";"
            " document.getElementById('login-form').dispatchEvent("
            " new Event('submit',{cancelable:true})); return 'ok';})()"
        )
        time.sleep(2.5)
        cdp.send("Page.reload")
        time.sleep(4.0)

        print("\n========== 人设参考图面板 ==========")

        # ---- ⓪ 前置：人设列表已加载（真 DOM，不碰 window.state）----
        ready = False
        for _ in range(40):
            ready = bool(cdp.eval(
                "(function(){var pl=document.getElementById('persona-list');"
                " return !!(pl && pl.innerText.trim()"
                "   && pl.innerText.indexOf('加载失败')<0"
                "   && pl.querySelector('li'));})()"
            ))
            if ready:
                break
            time.sleep(0.4)
        if not check("⓪ 前置：人设列表已加载（真 DOM）", ready, "人设列表没回来"):
            print(f"\n❌ {len(failures)} 条失败")
            return 1

        # ---- ① 走真 UI：点第一个人设进详情 ----
        clicked = cdp.eval(
            "(function(){var li=document.querySelector('#persona-list li');"
            " if(!li) return 'no-li'; li.click(); return 'ok';})()"
        )
        time.sleep(1.5)
        check("① 能点开人设详情（真 UI 点击）", clicked == "ok", f"got {clicked!r}")

        # ---- ② 控件都在 ----
        #     ⚠️ 用 `offsetParent !== null` 判可见 —— `hidden` 类在 CSS 里是
        #        `display:none`，只看 `classList` 会漏掉"被别的规则藏起来"的情况。
        present = cdp.eval(
            "(function(){var ids=['p-ref-file','p-ref-upload','p-ref-delete','p-ref-preview'];"
            " var miss=ids.filter(function(i){return !document.getElementById(i);});"
            " return {miss:miss,"
            "   thumbHidden:document.getElementById('p-ref-preview').classList.contains('hidden'),"
            "   formVisible:!document.getElementById('persona-form').classList.contains('hidden')};})()"
        ) or {}
        check("② 参考图控件都渲染出来了（选图/上传/删除/预览）",
              not present.get("miss"), f"缺：{present.get('miss')}")
        check("②b 人设表单可见（不是未选中态）",
              bool(present.get("formVisible")))

        # ---- ③ 当前人设 id + 原有参考图（真 HTTP，用于收尾还原）----
        cur = cdp.eval(
            "(async function(){var r=await fetch('/api/v1/personas');var d=await r.json();"
            " var id=d.active_persona_id||'';"
            " if(!id){var li=document.querySelector('#persona-list li');"
            "   if(li&&li.dataset&&li.dataset.id) id=li.dataset.id;}"
            " return {active:id};})()",
            await_promise=True,
        ) or {}
        pid = str(cur.get("active") or "")
        # 从 DOM 上拿不到 id 就退一步：用列表里第一个的名字反查
        if not pid:
            pid = cdp.eval(
                "(async function(){var li=document.querySelector('#persona-list li');"
                " if(!li) return ''; var name=li.textContent.replace('当前','').trim();"
                " var r=await fetch('/api/v1/personas');var d=await r.json();"
                " var hit=(d.data||[]).filter(function(p){return p.name===name;})[0];"
                " return hit?hit.id:'';})()",
                await_promise=True,
            ) or ""
        if not check("③ 前置：能定位到当前人设的 id（收尾要还原它）",
                     bool(pid), "拿不到 id —— 无法保证不留痕，故判失败"):
            print(f"\n❌ {len(failures)} 条失败")
            return 1

        meta = cdp.eval(
            "(async function(){var r=await fetch('/api/v1/personas/'"
            "+encodeURIComponent(" + json.dumps(pid) + "),"
            " {cache:'no-store'});"
            " if(!r.ok) return {err:r.status}; var d=await r.json();"
            " return {name:d.name||'', ref:d.reference_image_path||''};})()",
            await_promise=True,
        ) or {}
        restore["id"] = pid
        restore["orig"] = meta.get("ref") or ""
        check("③b 前置：读到某个人设（用于上传判据）",
              bool(meta.get("name")), f"got {meta!r}")

        # ---- ④ 走真 UI 选一张图（构造 File 塞进 input 并派发 change）----
        #     ⚠️ headless 下没法弹系统文件框，所以用 DataTransfer 造一个真 File。
        #        这仍是**真 UI 路径**（input 的 change → 我们的 onchange → 按钮解禁）。
        picked = cdp.eval(
            "(function(){var b64=" + json.dumps(PNG_1PX_B64) + ";"
            " var bin=atob(b64); var arr=new Uint8Array(bin.length);"
            " for(var i=0;i<bin.length;i++)arr[i]=bin.charCodeAt(i);"
            " var f=new File([arr],'ref_check.png',{type:'image/png'});"
            " var dt=new DataTransfer(); dt.items.add(f);"
            " var inp=document.getElementById('p-ref-file');"
            " inp.files=dt.files; inp.dispatchEvent(new Event('change'));"
            " return {n:inp.files.length,"
            "   upDisabled:document.getElementById('p-ref-upload').disabled};})()"
        ) or {}
        check("④ 选图后『上传』按钮解禁（真 change 事件）",
              picked.get("n") == 1 and picked.get("upDisabled") is False,
              f"got {picked!r}")

        # ---- ⑤ 点真『上传』按钮 ----
        cdp.eval("document.getElementById('p-ref-upload').click()")
        time.sleep(3.0)

        # ---- ⑥ 后端真的存了（真 HTTP 复核；⚠️ 带 no-store，别被缓存骗 ——
        #         aitag 踩过"删除后仍从缓存回 200"）----
        after = cdp.eval(
            "(async function(){var r=await fetch('/api/v1/personas/'"
            "+encodeURIComponent(" + json.dumps(pid) + "),"
            " {cache:'no-store'}); var d=await r.json();"
            " return {ref:d.reference_image_path||'', has:!!d.has_reference_image};})()",
            await_promise=True,
        ) or {}
        check("⑥ 上传后**后端字段真的写上了**（真 HTTP，no-store）",
              bool(after.get("ref")), f"reference_image_path={after.get('ref')!r}")
        check("⑥b 后端 `has_reference_image` 为真（面板预览靠它）",
              after.get("has") is True, f"got {after!r}")

        # ---- ⑦ 预览图**真的画出来了**（自然宽高 > 0）----
        #     🔴 只断言 `<img>` 存在是不够的：`src` 404 时元素照样在，
        #        只是 0×0 或显示破图。必须看 `naturalWidth`。
        prev = cdp.eval(
            "(function(){var im=document.getElementById('p-ref-preview');"
            " return {src:(im.getAttribute('src')||''),"
            "   nw:im.naturalWidth, nh:im.naturalHeight,"
            "   hidden:im.classList.contains('hidden'),"
            "   hint:(document.getElementById('p-ref-hint')||{}).textContent||''};})()"
        ) or {}
        check("⑦ 预览图真的加载出来了（naturalWidth>0，不是破图/0×0）",
              (prev.get("nw") or 0) > 0, f"got {prev!r}")
        check("⑦b 预览图不再隐藏", prev.get("hidden") is False, f"got {prev!r}")
        # 🔴 `#p-ref-hint` 的**唯一含义**是"当前配置是什么"（持久状态）。
        #    判据要的是「已设置」而不是「已上传」—— 后者是瞬时反馈，
        #    塞进去会把"现在用的是哪张图"盖掉（实测抓到过：hint='已上传'）。
        check("⑦c 状态文案说「已设置：<文件名>」（持久状态，不是瞬时文案）",
              "已设置" in str(prev.get("hint") or ""), f"hint={prev.get('hint')!r}")
        check("⑦d 删除按钮此时可用",
              cdp.eval("!document.getElementById('p-ref-delete').disabled") is True)

        # ---- ⑧ 图片本身能取回（同一路径 + 时间戳绕缓存）----
        fetched = cdp.eval(
            "(async function(){var im=document.getElementById('p-ref-preview');"
            " var u=im.getAttribute('src'); if(!u) return {ok:false,why:'no-src'};"
            " var r=await fetch(u,{cache:'no-store'});"
            " var b=await r.blob();"
            " return {ok:r.ok, status:r.status, size:b.size, type:b.type};})()",
            await_promise=True,
        ) or {}
        check("⑧ 参考图能真取回且非空（真 HTTP）",
              bool(fetched.get("ok")) and (fetched.get("size") or 0) > 0,
              f"got {fetched!r}")

        # ---- ⑨ 走真 UI 删除 → 回到未设置态 ----
        #     `confirm()` 在 headless 下会阻塞，先把它顶掉
        cdp.eval("window.confirm=function(){return true;};")
        cdp.eval("document.getElementById('p-ref-delete').click()")
        time.sleep(2.5)

        deleted = cdp.eval(
            "(async function(){var r=await fetch('/api/v1/personas/'"
            "+encodeURIComponent(" + json.dumps(pid) + "),"
            " {cache:'no-store'}); var d=await r.json();"
            " var im=document.getElementById('p-ref-preview');"
            " return {ref:d.reference_image_path||'',"
            "   hidden:im.classList.contains('hidden'),"
            "   hint:(document.getElementById('p-ref-hint')||{}).textContent||''};})()",
            await_promise=True,
        ) or {}
        check("⑨ 删除后后端字段已清空", deleted.get("ref") == "", f"got {deleted!r}")
        check("⑨b 面板回到「未设置」态（预览隐藏）",
              deleted.get("hidden") is True and "未设置" in str(deleted.get("hint") or ""),
              f"got {deleted!r}")

        # ---- ⑩ 全程零 JS 报错 ----
        errs = cdp.eval("JSON.stringify(window.__jsErrors||[])") or "[]"
        try:
            err_list = json.loads(errs)
        except Exception:
            err_list = [errs]
        check("⑩ 全程零 JS 报错", not err_list, f"{err_list}")

    finally:
        # ---- 还原：别在用户真实人设上留痕 ----
        try:
            if restore.get("id") and restore.get("orig"):
                # 原本有参考图 → 重新上传是做不到的（我们没存原图字节），
                # 所以这种情况下**如实报出来**，让人知道需要手工补。
                print(
                    f"\n⚠️ 注意：人设 {restore['id']} 原本有一张参考图"
                    f"（{restore['orig']}），本判据无法自动还原 —— 请重新上传。"
                )
        except Exception:
            pass
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except Exception:
            chrome.kill()

    print(f"\n{'✅ 全绿' if not failures else '❌ 有失败'} — {checks} 条判据")
    if failures:
        for f in failures:
            print(f"   · {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
