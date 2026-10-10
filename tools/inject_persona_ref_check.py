#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入验证（人设参考图）：**每条新判据都必须能被证明会失败**。

分两代：
  **第一代（①-⑩）** 2026-09-25 上午 —— `EMOTION_IMAGE_PERSONA_REFERENCE`
    全局兜底。最要紧的两条：③ 兜底**漏进 `edit`**（把"改用户给的这张图"
    静默变成"画角色"）；⑦ 用兜底图时**不打日志**（无从追溯，本项目最贵的一课）。

  **第二代（⑪-㉓）** 2026-09-25 下午 —— 情绪「**用户能填的就让用户改**」：
    参考图做成**面板上传、每人设一张**。这一代的失效模式**全是静默的**：
      * 白名单漏字段 → 接口 200、图也落盘，**但字段没存** → 重启后消失；
      * chat 不传 persona → per-persona **完全失效**，而模块单测照样全绿；
      * 上传路由漏 `require_auth` → 未登录可读写**私有参考图**
        （本项目真事故：`/api/v1/logs` 漏鉴权 + 服务绑 `0.0.0.0`）。

  ⚠️ 这就是为什么**上一轮第一轮注入 3/10 没抓住**：判据只测函数没测接线。
     第二代每组都点名"哪条判据该红"。

⚠️ 本脚本会**临时改写源码**再还原；结尾用 sha256 逐字节校验还原。
"""
from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys

ROOT = pathlib.Path("/root/Ai-works/情绪app")
PY = str(ROOT / ".venv/bin/python")

TOOL = ROOT / "emotion_app/domain/image_tool.py"
MAIN = ROOT / "emotion_app/__main__.py"
CFG = ROOT / "emotion_app/infra/config.py"
CHAT = ROOT / "emotion_app/domain/chat.py"
ADMIN = ROOT / "emotion_app/web/api_admin.py"
REPO = ROOT / "emotion_app/domain/persona_repo.py"
PERSONA = ROOT / "emotion_app/domain/persona.py"
REF = ROOT / "emotion_app/domain/persona_ref.py"
IMG = ROOT / "emotion_app/domain/image_gen.py"
CTX = ROOT / "emotion_app/domain/tool_context.py"

T = "tests/test_s6_image_reference.py"
TA = "tests/test_assembly.py"
TH = "tests/test_persona_ref_http.py"
TE = "tests/test_persona_ref_e2e.py"

#: selfie 分支的入口判据（第一代 ①-⑥ 都用它当锚点）
SELFIE_GATE = "        if not ref and kind == image_gen.KIND_SELFIE:"
LOG_LINE = '                    "selfie 未指定参考图，使用配置的固定参考图",'

INJECTIONS: list[tuple[str, pathlib.Path, str, str, list[str]]] = [
    # ---------------- 第一代：全局兜底 ----------------
    (
        "① 兜底逻辑整个去掉（selfie 不再带参考图）",
        TOOL,
        SELFIE_GATE,
        "        if False:  # 注入",
        [f"{T}::test_persona_reference_is_used_for_selfie_without_explicit_path"],
    ),
    (
        "② 显式路径被顶掉（顺序反了）",
        TOOL,
        SELFIE_GATE,
        "        if kind == image_gen.KIND_SELFIE:  # 注入：不看 ref",
        [f"{T}::test_explicit_path_wins_over_persona_reference"],
    ),
    (
        "③ 兜底逻辑漏进 edit（静默换掉「改这张图」的语义）",
        TOOL,
        SELFIE_GATE,
        "        if not ref:  # 注入：连 edit 也兜底",
        [f"{T}::test_persona_reference_does_not_leak_into_edit"],
    ),
    (
        "④ 兜底逻辑漏进 text2img（给风景图注入角色）",
        TOOL,
        SELFIE_GATE,
        "        if not ref and kind != image_gen.KIND_EDIT:  # 注入",
        [f"{T}::test_persona_reference_does_not_leak_into_text2img"],
    ),
    (
        # ⚠️ 这条注入**改过两版**，值得记：
        #    第一版是「把空串硬塞进 ref」（`ref = self._persona_reference or ref`）——
        #    **抓不住，而且"抓不住"是正确结论**：那行语义上等价于原代码
        #    （`"" or ref` 还是 `ref`），没有任何可观察差异。
        #    → 真正该守的风险是「**没配却声称用了**」：去掉 `if ref:` 这道闸，
        #      日志就会在参考图为空时照样打出来 = 现场证据是**假的**。
        "⑤ 没配参考图时也打『用了固定参考图』日志（现场证据是假的）",
        TOOL,
        "            if ref:\n                # 🔴 **必须留痕**：这是\"参考图从哪来的\"唯一现场证据",
        "            if True:  # 注入：不判空就声明用了\n                # 🔴 必须留痕",
        [f"{T}::test_persona_reference_falls_back_cleanly_when_unset"],
    ),
    (
        "⑥ 参考图读不到时静默降级成纯文生图",
        TOOL,
        SELFIE_GATE,
        "        if (not ref and kind == image_gen.KIND_SELFIE\n                and __import__('pathlib').Path(self._persona_reference or '').is_file()):  # 注入",
        [f"{T}::test_persona_reference_missing_file_is_loud_not_silent"],
    ),
    (
        "⑦ 用兜底图时不打日志（参考图从哪来无从追溯）",
        TOOL,
        LOG_LINE,
        '                    None and "x",  # 注入：不留痕',
        [f"{T}::test_persona_reference_use_is_logged"],
    ),
    (
        "⑧ 装配处不传 persona_reference（配置白填）",
        MAIN,
        "        persona_reference=config.image.persona_reference,",
        '        persona_reference="",  # 注入',
        [f"{TA}::test_persona_reference_is_wired_from_config"],
    ),
    (
        "⑨ set_persona_reference 改成空操作（切全局兜底不生效）",
        TOOL,
        '        self._persona_reference = str(path or "").strip()\n        log.info(',
        "        log.info(  # 注入：不真的换\n",
        [f"{T}::test_set_persona_reference_switches_default"],
    ),
    (
        "⑩ config 的 env 绑定去掉（env 配了也没用）",
        CFG,
        '    "IMAGE_PERSONA_REFERENCE": ("image", "persona_reference", "str"),',
        "    # 注入：env 绑定删除",
        [f"{TA}::test_persona_reference_env_key_is_wired"],
    ),
    # ---------------- 第二代：per-persona（面板上传） ----------------
    (
        "⑪ 白名单不放行 reference_image_path（存了但没存进去）",
        REPO,
        '            "reference_image_path",',
        "            # 注入：白名单删除",
        [
            f"{TH}::test_upload_stores_field_and_file",
            f"{TA}::test_persona_reference_field_is_writable",
        ],
    ),
    (
        "⑫ to_dict 不带该字段（面板看不到、重启即丢）",
        PERSONA,
        '            "reference_image_path": self.reference_image_path,',
        '            "reference_image_path": "",  # 注入',
        [f"{TA}::test_persona_reference_serialized_roundtrip"],
    ),
    (
        "⑬ from_dict 不读该字段（存了又读不回来）",
        PERSONA,
        '            reference_image_path=d.get("reference_image_path", ""),',
        "            # 注入：不读",
        [f"{TA}::test_persona_reference_serialized_roundtrip"],
    ),
    (
        "⑭ 上传路由漏 require_auth（未登录可写私有参考图）",
        ADMIN,
        '''        denied = require_auth()
        if denied is not None:
            return denied
        repo, _, _ = _svc()

        try:
            await repo.get(persona_id)
        except PersonaNotFound:
            return _err(f"人设不存在：{persona_id}", "not_found", 404)

        files = await request.files''',
        '''        repo, _, _ = _svc()

        try:
            await repo.get(persona_id)
        except PersonaNotFound:
            return _err(f"人设不存在：{persona_id}", "not_found", 404)

        files = await request.files''',
        [f"{TH}::test_all_three_routes_require_auth"],
    ),
    (
        "⑮ 下载路由漏 require_auth（未登录可读私有参考图）",
        ADMIN,
        '''    @bp.get("/personas/<persona_id>/reference")
    async def get_persona_reference(persona_id: str):  # type: ignore[no-untyped-def]
        """取人设参考图。**需鉴权**（同头像，属私有内容）。"""
        denied = require_auth()
        if denied is not None:
            return denied
        repo, _, _ = _svc()''',
        '''    @bp.get("/personas/<persona_id>/reference")
    async def get_persona_reference(persona_id: str):  # type: ignore[no-untyped-def]
        repo, _, _ = _svc()''',
        [f"{TH}::test_all_three_routes_require_auth"],
    ),
    (
        "⑯ 上传成功但不写字段（只落盘 = 重启即丢）",
        ADMIN,
        '''            updated = await repo.update(
                persona_id, {"reference_image_path": target.name}
            )''',
        "            updated = await repo.get(persona_id)  # 注入",
        [f"{TH}::test_upload_stores_field_and_file"],
    ),
    (
        "⑰ 删除只清文件不清字段（面板永远显示已配置）",
        ADMIN,
        '''            updated = await repo.update(persona_id, {"reference_image_path": ""})''',
        "            updated = await repo.get(persona_id)  # 注入",
        [f"{TH}::test_delete_clears_both_field_and_file"],
    ),
    (
        "⑱ 下载不做路径穿越校验（可读 data/ 外文件）",
        ADMIN,
        '        if not persona_ref_mod.is_safe_name(name):\n            return _err("参考图路径非法", "invalid_request_error", 400)',
        '        if False:  # 注入：不校验\n            return _err("参考图路径非法", "invalid_request_error", 400)',
        [f"{TH}::test_download_rejects_path_traversal"],
    ),
    (
        "⑲ chat 不把当前人设放进上下文（per-persona 完全失效）",
        CHAT,
        "        with tool_context.tool_scope(persona=persona):",
        "        with tool_context.tool_scope():  # 注入：不传人设",
        [
            f"{TE}::test_tool_loop_uses_the_persona_of_this_request",
            f"{TA}::test_nonstream_tool_loop_receives_persona",
        ],
    ),
    (
        "⑳ complete() 不把 req.persona 传进工具循环（参数白加）",
        CHAT,
        "            target, messages, result, body_extra, persona=req.persona",
        "            target, messages, result, body_extra  # 注入",
        [
            f"{TE}::test_tool_loop_uses_the_persona_of_this_request",
            f"{TA}::test_nonstream_tool_loop_receives_persona",
        ],
    ),
    (
        "㉑ 人设图不优先于全局兜底（多角色全被盖成一张脸）",
        TOOL,
        "            persona_ref = persona_ref_mod.resolve_persona_reference(persona)",
        "            persona_ref = ''  # 注入：跳过人设那一级",
        [f"{TE}::test_persona_wins_over_global"],
    ),
    (
        "㉒ 事后不还原上下文（下一个请求拿到上一个人的脸）",
        CTX,
        "    finally:\n        current_persona.reset(token)",
        "    finally:\n        pass  # 注入：不还原",
        [f"{TE}::test_context_does_not_leak_after_request"],
    ),
    (
        # 🆕 2026-09-25 真环境复跑时发现：`httpx` 网络类异常 `str()` 可为空
        #    → 用户看到「生图端点连接失败：」后面**什么都没有**。
        "㉔ httpx 空消息异常不再退化（报错里没有信息 = 静默失败的变体）",
        IMG,
        '''    text = str(exc).strip()
    if text:
        return text
    return repr(exc) or type(exc).__name__''',
        '''    return str(exc)  # 注入：退回原写法''',
        [
            f"{T}::test_describe_never_returns_empty",
            f"{T}::test_connect_error_message_is_not_empty",
        ],
    ),
    (
        "㉓ `is_safe_name` 放行任意扩展名（把 txt 当参考图）",
        REF,
        "    return name.lower().endswith(ALLOWED_SUFFIXES)",
        "    return True  # 注入",
        [f"{TH}::test_is_safe_name_contract"],
    ),
]


def sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


#: 当前正在被改写的文件 → 它的原始内容（用于被信号打断时**抢救还原**）。
_INFLIGHT: dict[pathlib.Path, str] = {}


def _emergency_restore(*_a: object) -> None:
    """被 SIGTERM/SIGINT 打断时把在飞的那个文件写回去。

    ⚠️ 仍然不保证 100%（SIGKILL 拦不住）—— 所以**落盘基线 + 启动自检**
       才是真正的保险，这里只是尽量少留残留。
    """
    for path, text in list(_INFLIGHT.items()):
        try:
            path.write_text(text, encoding="utf-8")
            print(f"\n⚠️ 被中断：已抢先把 {path.name} 还原")
        except Exception:
            pass
    _INFLIGHT.clear()
    raise SystemExit(130)


#: 单组判据的墙钟上限。⚠️ **必须有** —— 实测踩到：某一组注入会让被测代码
#: 走进一条**挂住**的路径（不是失败，是不返回），没有超时的话整个脚本
#: 会一直卡在那里（我第一次跑就卡了 10 分钟被杀）。
#: 超时按"**抓到了**"处理（判据没有全绿 = 有效），但要在输出里写清楚是超时。
JUDGE_TIMEOUT = 120


def run_judges(nodes: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [PY, "-m", "pytest", *nodes, "-q", "--no-header", "--tb=no"],
            cwd=ROOT, capture_output=True, text=True, timeout=JUDGE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"⚠️ 判据超时(>{JUDGE_TIMEOUT}s) —— 按「抓到了」计"
    tail = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()][-1:]
    return proc.returncode == 0, (tail[0] if tail else "?")


#: 还原基线的落盘位置。
#:
#: 🔴🔴 **为什么必须有它**（2026-09-25 真实事故）：
#:   第一版脚本把"注入前"的哈希**只放在内存里**，然后我被 10 分钟墙钟杀掉。
#:   杀掉时源码正停在**注入态** → 下一次运行时，"注入前"读到的就是**已被改坏的文件**
#:   → 结尾的"还原校验 ✅ 逐字节一致"是**假绿**（它比对的是自己的注入产物）。
#:   后果：`api_admin.py` 的 `require_auth()` **真的丢了**，
#:   直到全量测试报 `POST 未鉴权也能上传：500` 才暴露。
#:
#:   **教训（通则）：任何"改了再还原"的工具，基线必须落盘到工具之外**，
#:   否则"还原成功"这句话本身就无法被验证 —— 这正是本项目说的
#:   "判据只有验过和失败两种，且判据必须能被证伪"。
BASELINE = ROOT / "tools/.inject_persona_ref_baseline.json"


def load_baseline(files: list[pathlib.Path]) -> dict[pathlib.Path, str]:
    """读落盘基线；缺失或不完整时**以当前内容建立**并警告。"""
    import json

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


def check_dirty(files: list[pathlib.Path], baseline: dict[pathlib.Path, str]) -> list[str]:
    """启动时自检：有没有上次被杀留下的**注入残留**。

    🔴 这是上面那场事故的直接补丁 —— 下次再被墙钟杀掉，重跑时会**立刻报出来**，
       而不是安静地把坏文件当成基线继续跑。
    """
    dirty = [f for f in files if sha(f) != baseline.get(f)]
    if dirty:
        # 🔴 2026-10-10：用 git 区分**基线过期**与**真的停在注入态**。
        #    基线文件不在 git 里 ⇒ 源码每正常提交一次，"当前 ≠ 基线"就成立。
        #    实测一次批量跑，13 套工具全是这个假报警，而产品完全正常。
        #    ⚠️ 不能直接删掉这个保护（它是 2026-09-25 真事故的产物：
        #       脚本被杀在注入态、下次那句"还原校验"比对的是自己的注入产物）。
        #    判据：工作区**干净** ⇒ 内容是提交过的正常内容 ⇒ 基线过期、继续跑；
        #          工作区**有未提交差异** ⇒ 真可能停在注入态 ⇒ 停下来。
        import sys as _sys
        _sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        from _inject_guard import git_is_clean as _git_clean  # noqa: E402
        import json as _json   # ⚠️ `json` 在本文件是**函数内局部导入**（见 load_baseline），
                               #    模块级没有 —— 这里必须自己来一份，否则 NameError

        if _git_clean(ROOT):
            print(f"ℹ️ {len(dirty)} 个文件与基线不一致，但工作区干净 ⇒ "
                  f"是正常提交造成基线过期，按当前源码刷新基线")
            new_base = {f: sha(f) for f in files}
            # 🔴 **必须原地更新调用方那个 dict**（2026-10-10 踩到）：
            #    `main` 里 `before = baseline` 拿的是**同一个对象**，
            #    只写文件不更新它 ⇒ 后面"还原校验"拿旧 sha 比对 ⇒ **假红**。
            baseline.clear()
            baseline.update(new_base)
            BASELINE.write_text(
                _json.dumps({str(k): v for k, v in new_base.items()},
                            ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return []
        print("\n🔴 检测到上次运行留下的注入残留（源码与基线不一致）：")
        for f in dirty:
            print(f"   · {f.relative_to(ROOT)}")
        print("   → 请先 `git diff` 看清楚，再决定还原或重建基线。**本次不继续跑。**")
    return dirty


def main() -> int:
    files = sorted({inj[1] for inj in INJECTIONS})
    baseline = load_baseline(files)
    if check_dirty(files, baseline):
        return 1
    before = baseline
    print("还原基线 sha256（**落盘**，不是内存里的）：")
    for f, h in before.items():
        print(f"  {f.relative_to(ROOT)}  {h[:16]}…")

    import signal

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
    for f, h in before.items():
        same = sha(f) == h
        ok_restore &= same
        print(f"  {'✅' if same else '🔴'} {f.relative_to(ROOT)}")

    print("\n恢复后全量（main + host）：")
    try:
        proc = subprocess.run(
            [PY, "-m", "pytest", "tests", "host/tests", "-q", "--no-header",
             "--deselect", "tests/test_plugin_host_e2e.py::test_router_matches_real_implementation_live"],
            cwd=ROOT, capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        print("  🔴 全量测试超时")
        return 1
    print("  " + [ln for ln in proc.stdout.strip().splitlines() if ln.strip()][-1])

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
