#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入验证（会话 umo 标签 + 未读徽标）：**每条新判据都必须能被证明会失败**。

情绪 2026-09-26 要的两个功能：
  * 「每个会话都带一个唯一标签 umo，这样我就能自己到陪伴那边添加"用户"和
     测试主动消息等东西了」
  * 「还有给"聊天"加一个功能：用户没有阅读的时候出个提示，就像"插件"
     旁边那个圆圈 3 一样」

## 这一轮要守的失效模式（全都**静默**）

| 注入 | 真实后果（用户看到什么） |
|---|---|
| ① 自动分配去掉 | 新会话 umo 是空串 → 拿不到标签，陪伴面板里建不了用户 |
| ② 唯一性检查去掉 | 多个会话共用一个标签 → 插件眼里它们是**同一个用户**（记忆/关系串台） |
| ③ `/umo` 路由漏鉴权 | 未登录就能改 umo = 把用户的记忆/关系**指向别人** |
| ④ `/read` 漏鉴权 | 未登录能把未读清零（"消息不见了"） |
| ⑤ 投递不 +1 | 主动消息**永久不提示**（功能等于没做） |
| ⑥ 用户聊天也 +1 | 每说一句自己都蹦红点（假阳性，比不做还烦） |
| ⑦ `/unread` 只报 total 丢了明细 | 徽标有数字，但**不知道是哪条会话** |
| ⑧ 会话响应的 `umo` 字段被删 | 面板上"完整 umo"永远空 → 用户拿不走那个值 |
| ⑨ `_new_umo_user_id` 用 uuid 全长/含冒号 | 破坏 umo 的 A:B:C 结构（静默错位） |

⚠️ **基线落盘在工具之外**（`tools/.inject_umo_unread_baseline.json`）——
   这是 2026-09-25 那场事故的直接补丁：脚本被杀时源码停在注入态，
   下次跑会把"已改坏的文件"当成基线，结尾的"还原校验 ✅"是假绿。
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import signal
import subprocess
import sys

ROOT = pathlib.Path("/root/Ai-works/情绪app")
PY = str(ROOT / ".venv/bin/python")

REPO = ROOT / "emotion_app/domain/session_repo.py"
ADMIN = ROOT / "emotion_app/web/api_admin.py"
DELIVERY = ROOT / "emotion_app/web/delivery.py"

T = "tests/test_session_umo_unread.py"
TP = "tests/test_session_persona.py"
TD = "tests/test_delivery.py"

#: 单组判据的墙钟上限。**必须有** —— 某一组注入可能让代码走进挂住的路径
#: （不是失败、是不返回），没有超时整个脚本会一直卡着。
JUDGE_TIMEOUT = 150

INJECTIONS: list[tuple[str, pathlib.Path, str, str, list[str]]] = [
    # ---------------- ① umo 自动分配 ----------------
    (
        "① 新建会话不再自动分配 umo（退回「空串=跟随全局」）",
        REPO,
        """        uid = _normalize_user_id(user_id, session_id=session_id)
        if not uid:
            # 走与 `new_umo_user_id()` **同一份**规则（唯一性检查也在那里）
            uid = await self.new_umo_user_id()""",
        """        uid = _normalize_user_id(user_id, session_id=session_id)
        if False:  # 注入：不自动分配
            uid = ''""",
        [f"{T}::test_new_session_gets_its_own_umo"],
    ),
    (
        "② 唯一性检查去掉（并发/连建时可能撞同一个标签）",
        REPO,
        """        for _ in range(8):
            candidate = _new_umo_user_id()
            if candidate not in taken:
                return candidate""",
        """        for _ in range(8):
            candidate = "sess-000000000000"  # 注入：恒定值（等价于不查重）
            if candidate not in taken:
                return candidate""",
        [f"{T}::test_new_session_gets_its_own_umo"],
    ),
    (
        "③ 标签取值约束破坏：改成 `uuid4().hex`（32 位，含连字符风险的另一版）",
        REPO,
        '    return f"sess-{uuid.uuid4().hex[:12]}"',
        '    return uuid.uuid4().hex  # 注入：不带前缀、不带约束',
        [f"{T}::test_new_session_gets_its_own_umo"],
    ),
    (
        "④ 换 umo 的校验被绕过：`update` 不再规整 user_id",
        REPO,
        """                if k == "user_id":
                    v = _normalize_user_id(v, session_id=session_id)""",
        """                if k == "user_id":
                    v = str(v)  # 注入：不校验""",
        [f"{T}::test_patch_still_rejects_illegal_user_id"],
    ),
    # ---------------- ② 未读语义 ----------------
    (
        "⑤ 投递端口不再标记未读（主动消息不提示）",
        DELIVERY,
        "                unread_delta=1,",
        "                unread_delta=0,  # 注入：不 +1",
        [f"{T}::test_delivery_port_marks_unread"],
    ),
    (
        "⑥ `append` 不再累加未读（入库丢了）",
        REPO,
        """        if unread_delta > 0:
            s.unread += int(unread_delta)""",
        """        if False:  # 注入：不累加
            s.unread += int(unread_delta)""",
        [f"{T}::test_proactive_message_marks_unread_then_read_clears_it"],
    ),
    (
        "⑦ 负数不夹取（计数可能被压成负数 → 徽标永远消不掉）",
        REPO,
        """        if unread_delta > 0:
            s.unread += int(unread_delta)""",
        """        s.unread += int(unread_delta)  # 注入：不判正负""",
        [f"{T}::test_unread_delta_cannot_go_negative"],
    ),
    (
        "⑧ `/read` 不清零（打开会话红点不消失）",
        REPO,
        """        if s.unread:
            s.unread = 0
            await self._save_sessions(sessions)""",
        """        if False:  # 注入：不清零
            s.unread = 0
            await self._save_sessions(sessions)""",
        [f"{T}::test_proactive_message_marks_unread_then_read_clears_it"],
    ),
    (
        "⑨ `/unread` 丢明细（只剩 total → 面板不知道是哪条会话）",
        ADMIN,
        '        by_id = {s.id: s.unread for s in items if s.unread > 0}',
        '        by_id = {}  # 注入：丢明细',
        [f"{T}::test_unread_endpoint_shape"],
    ),
    # ---------------- ③ 鉴权（反向判据） ----------------
    (
        "⑩ `/umo` 路由漏鉴权（未登录可改别人的 umo）",
        ADMIN,
        """        denied = require_auth()
        if denied is not None:
            return denied
        repo = _sessions()
        if repo is None:
            return _err("会话服务未就绪", "server_error", 503)
        try:
            previous = (await repo.get(session_id)).user_id
        except SessionNotFound:""",
        """        denied = None  # 注入：漏鉴权
        if denied is not None:
            return denied
        repo = _sessions()
        if repo is None:
            return _err("会话服务未就绪", "server_error", 503)
        try:
            previous = (await repo.get(session_id)).user_id
        except SessionNotFound:""",
        [f"{T}::test_umo_route_requires_auth"],
    ),
    (
        "⑪ `/unread` 路由漏鉴权（未登录可读会话 id 列表）",
        ADMIN,
        """        denied = require_auth()
        if denied is not None:
            return denied
        repo = _sessions()
        if repo is None:
            return _err("会话服务未就绪", "server_error", 503)
        items = await repo.list()
        by_id = {s.id: s.unread for s in items if s.unread > 0}""",
        """        denied = None  # 注入：漏鉴权
        if denied is not None:
            return denied
        repo = _sessions()
        if repo is None:
            return _err("会话服务未就绪", "server_error", 503)
        items = await repo.list()
        by_id = {s.id: s.unread for s in items if s.unread > 0}""",
        [f"{T}::test_unread_route_requires_auth"],
    ),
    (
        "⑫ 会话响应不再带 `umo`（面板上那个值永远是空的）",
        ADMIN,
        '        d["umo"] = umo',
        '        d["umo"] = ""  # 注入：不给完整 umo',
        [f"{T}::test_new_session_gets_its_own_umo"],
    ),
    # ---------------- ④ 落点反查 ----------------
    (
        "⑬ `by_user_id` 不再匹配 → 主动消息落进孤儿会话",
        REPO,
        """        matches = [s for s in (await self._load_sessions()).values() if s.user_id == uid]""",
        """        matches = []  # 注入：永远找不到""",
        [f"{T}::test_by_user_id_finds_session_from_umo"],
    ),
]

#: 还原基线的落盘位置（工具**之外** —— 见模块 docstring 的事故说明）
BASELINE = ROOT / "tools/.inject_umo_unread_baseline.json"
_INFLIGHT: dict[pathlib.Path, str] = {}


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _emergency_restore(*_args: object) -> None:
    """SIGTERM/SIGINT 抢救还原（SIGKILL 拦不住，所以基线落盘才是真保险）。"""
    for path, text in list(_INFLIGHT.items()):
        try:
            path.write_text(text, encoding="utf-8")
            print(f"\n⚠️ 收到终止信号，已抢救还原 {path.relative_to(ROOT)}")
        except Exception as exc:  # noqa: BLE001
            print(f"\n🔴 抢救还原失败：{exc}")
    sys.exit(130)


def load_baseline(files: list[pathlib.Path]) -> dict[pathlib.Path, str]:
    if BASELINE.exists():
        try:
            raw = json.loads(BASELINE.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        got = {pathlib.Path(k): v for k, v in raw.items()}
        missing = [f for f in files if str(f) not in raw]
        if not missing:
            return got
        print(f"⚠️ 基线文件存在但缺 {len(missing)} 个文件，按当前内容补齐")
        for f in missing:
            got[f] = sha(f)
        BASELINE.write_text(
            json.dumps({str(k): v for k, v in got.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return got
    base = {f: sha(f) for f in files}
    BASELINE.write_text(
        json.dumps({str(k): v for k, v in base.items()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"（首次运行：已把当前内容记为还原基线 → {BASELINE.relative_to(ROOT)}）")
    return base


def check_dirty(files: list[pathlib.Path], baseline: dict[pathlib.Path, str]) -> list[pathlib.Path]:
    dirty = [f for f in files if sha(f) != baseline.get(f)]
    if dirty:
        # 🔴 2026-10-10：用 git 区分**基线过期**与**真的停在注入态**。
        #    基线文件不在 git 里 ⇒ 源码每正常提交一次，"当前 ≠ 基线"就成立。
        #    实测一次批量跑，13 套工具全是这个假报警，而产品完全正常。
        #    ⚠️ 不能删掉这个保护（2026-09-25 真事故的产物）。
        import sys as _sys
        _sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        from _inject_guard import git_is_clean as _git_clean  # noqa: E402

        if _git_clean(ROOT):
            print(f"ℹ️ {len(dirty)} 个文件与基线不一致，但工作区干净 ⇒ "
                  f"是正常提交造成基线过期，按当前源码刷新基线")
            base = {f: sha(f) for f in files}
            BASELINE.write_text(
                json.dumps({str(k): v for k, v in base.items()}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return []
        print("\n🔴 检测到上次运行留下的注入残留（源码与基线不一致）：")
        for f in dirty:
            print(f"   · {f.relative_to(ROOT)}")
        print("   → 请先 `git diff` 看清楚，再决定还原或重建基线。**本次不继续跑。**")
    return dirty


def _purge_pycache() -> int:
    """删掉被测包下的 `__pycache__`（**注入验证必须做，实测踩到**）。

    🔴 为什么必须删（2026-09-26 实测，本项目第一次记录这个坑）：
       CPython 判 `.pyc` 是否有效用的是 **(源文件 mtime 的整秒, 源文件字节数)**。
       而本脚本是"**改一次 → 跑一次 → 还原 → 再改**"，两次改写常发生在
       **同一秒**内；只要两次改动**字节数相同**，第二次的 `.pyc` 就会被判为
       "有效"，于是 pytest 加载到的还是**上一次注入**的字节码。

       实测症状（非常迷惑）：⑩⑪ 两组注入都是把 `denied = require_auth()`
       改成 `denied = None  # 注入…`（**字节数逐字相同**）→ ⑩ 抓住、⑪ 显示
       "仍然全绿"；而把 ⑪ 的改动手工跑一次**必红**。
       最小复现：同秒内把 `VALUE = "A1"` 改成同长度的 `"B2"` →
       `import` 拿到的仍是 `A1`。

       ⚠️ 这条会让**所有**"改了再还原"的工具产生假结果，方向是**假绿**
        （注入没生效却说"抓不住"），比假红更危险 —— 它会让你去改本来正确的判据。
    """
    n = 0
    for d in ROOT.rglob("__pycache__"):
        if ".venv" in d.parts:
            continue
        for f in d.glob("*.pyc"):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    return n


def run_judges(nodes: list[str]) -> tuple[bool, str]:
    # 🔴 **每跑一次判据前先清字节码缓存**（见 `_purge_pycache` 的实测说明）
    purged = _purge_pycache()
    try:
        proc = subprocess.run(
            [PY, "-m", "pytest", *nodes, "-q", "-p", "no:cacheprovider",
             "--no-header", "--tb=no"],
            cwd=ROOT, capture_output=True, text=True, timeout=JUDGE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"⚠️ 判据超时(>{JUDGE_TIMEOUT}s) —— 按「抓到了」计"
    out = proc.stdout + proc.stderr
    tail = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()][-1:]

    # 🔴 **必须看见真的 failed**，不能只看 returncode：
    #    注入把源码改出语法/收集错误时 pytest 也返回非 0，但那是**假抓**
    #    （判据根本没跑起来）。我第一版就是这么被坑的：⑩⑪ 两组显示 ✅，
    #    实际是 SyntaxError 冒烟 —— 那种注入**永远**"抓得住"，
    #    等于把两组验证变成了仪式。
    ran = ("failed" in out) or ("error" in out and "errors" in out)
    if proc.returncode != 0 and not ran:
        return True, f"⚠️ 判据没能跑起来（疑似注入把源码改坏了）—— 记为**未抓住**：{tail}"
    return proc.returncode == 0, f"{tail[0] if tail else '?'}（清理 pyc {purged} 个）"


def main() -> int:
    files = sorted({inj[1] for inj in INJECTIONS})
    baseline = load_baseline(files)
    if check_dirty(files, baseline):
        return 1

    print("还原基线 sha256（**落盘**，不是内存里的）：")
    for f, h in baseline.items():
        print(f"  {f.relative_to(ROOT)}  {h[:16]}…")

    signal.signal(signal.SIGTERM, _emergency_restore)
    signal.signal(signal.SIGINT, _emergency_restore)

    failures: list[str] = []
    unmatched: list[str] = []
    for name, path, old, new, nodes in INJECTIONS:
        text = path.read_text(encoding="utf-8")
        if text.count(old) != 1:
            print(f"\n❌ {name}：原文匹配 {text.count(old)} 次（应为 1），跳过")
            unmatched.append(name)
            continue
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        _INFLIGHT[path] = text
        try:
            green, summary = run_judges(nodes)
        finally:
            path.write_text(text, encoding="utf-8")
            _INFLIGHT.pop(path, None)
        if green:
            print(f"\n❌ {name}：注入后判据**仍然全绿** —— 判据抓不到这个 bug！")
            failures.append(name)
        else:
            print(f"\n✅ {name}\n     {summary}")

    print("\n还原校验（sha256 必须与改前一致）：")
    ok_restore = True
    for f, h in baseline.items():
        same = sha(f) == h
        ok_restore &= same
        print(f"  {'✅' if same else '🔴'} {f.relative_to(ROOT)}")

    print("\n恢复后全量（main）：")
    proc = subprocess.run(
        [PY, "-m", "pytest", "tests", "-q", "--no-header",
         "--deselect", "tests/test_plugin_host_e2e.py::test_router_matches_real_implementation_live"],
        cwd=ROOT, capture_output=True, text=True, timeout=900,
    )
    tail = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()][-1:]
    print("  " + (tail[0] if tail else "?"))

    if failures or unmatched or not ok_restore or proc.returncode != 0:
        print(
            f"\n🔴 未通过：注入未抓住 = {failures}；锚点没匹配上 = {unmatched}；"
            f"还原一致 = {ok_restore}"
        )
        return 1
    print(f"\n🎉 全部 {len(INJECTIONS)} 组注入都被抓住；还原逐字节一致；恢复后全绿。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
