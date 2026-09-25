# -*- coding: utf-8 -*-
"""注入 bug 验证：把实现**改坏** → 判据必须挂 → 改回。

## 为什么必须做这个

本项目铁律：**"写完没验过它会挂"的断言 = 假通过**（比不写还糟）。
所以本脚本对每一条关键判据，都把对应的实现改动成"错误但看起来合理"的版本，
跑测试确认**至少有一条判据挂掉**，然后精确恢复。

## 与旧版的差别（两层改造后重写）

旧版是**单层**时代的注入清单。本轮判据围绕两层重写，注入点也跟着换：
ID 正则 / 迁移落盘 / 悬空引用 / provider_type / 掩码语义 / 路由形态 / 宿主同步。

⚠️ 每条注入都用**精确字符串替换**：替换不到就报"注入失败"（避免"静默没注入"
   然后误判成"判据抓不住"）。每条跑完立即从备份恢复。

## 🔴 被中断会**改坏产品代码**（2026-09-20 真实事故，已修）

本脚本是**原地改产品文件**的，所以有两种翻车方式，我都踩过：

1. **中途被 SIGTERM/超时打断** → 注入没来得及 `restore()`，
   `api_providers.py` 里留下 `except httpx.TimeoutException if False else ():`
   —— 异常被吞、超时被误报成 `upstream_unreachable`，**基线直接挂了 1 条**。
2. **更阴的：`backup()` 先 `rmtree` 再复制【磁盘上当前的】文件** ——
   第二次运行时，它把**上一次残留的坏文件**当成"干净基线"备份下来
   → 从此 `restore()` 恢复的就是坏文件，**损坏被固化**，怎么跑都修不回来。

→ 现在的对策：①`backup()` 拒绝在"看起来已经被注入过"的文件上建基线
  （扫 `if False` / `INJECT` 标记），宁可**拒绝运行**也不固化损坏；
  ②`atexit` + 信号处理，**任何**退出路径都恢复；
  ③每次注入前后比 sha256，恢复后必须与基线**逐字节相同**，否则大声报错。
"""
from __future__ import annotations

import atexit
import hashlib
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path("/root/Ai-works/情绪app")
STORE = ROOT / "emotion_app/infra/providers_store.py"
API = ROOT / "emotion_app/web/api_providers.py"
APP = ROOT / "emotion_app/web/app.py"
TESTS = ROOT / "tests/test_providers_api.py"

BACKUP = Path("/tmp/prov_backup_2layer")
PY = str(ROOT / ".venv/bin/python")

INJECTIONS: list[tuple[str, Path, str, str]] = [
    # ---- ②b id 规则：这是父代理实测复现过的真 bug ----
    (
        "②b-a 模型 id 正则改回【不允许 /】（真 bug 的原样复现）",
        STORE,
        'MODEL_ID_PATTERN = re.compile(r"^[^\\s/]+/\\S+$")',
        'MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")',
    ),
    (
        "②b-b 源 id 正则不允许中文",
        STORE,
        'SOURCE_ID_PATTERN = re.compile(r"^[^\\s/]{1,64}$")',
        'SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")',
    ),
    (
        "②b-c 源 id 正则允许 /（前缀歧义）",
        STORE,
        'SOURCE_ID_PATTERN = re.compile(r"^[^\\s/]{1,64}$")',
        'SOURCE_ID_PATTERN = re.compile(r"^[^\\s]{1,64}$")',
    ),
    (
        "②b-d 模型 id 不要求斜杠",
        STORE,
        'MODEL_ID_PATTERN = re.compile(r"^[^\\s/]+/\\S+$")',
        'MODEL_ID_PATTERN = re.compile(r"^\\S+$")',
    ),
    (
        "②b-g 模型 id 用 . 且 DOTALL（换行也能过 fullmatch）",
        STORE,
        'MODEL_ID_PATTERN = re.compile(r"^[^\\s/]+/\\S+$")',
        'MODEL_ID_PATTERN = re.compile(r"^[^\\s/]+/.+$", re.DOTALL)',
    ),
    (
        "②b-e id 校验用 match 而不是 fullmatch（$ 放过结尾换行）",
        STORE,
        "        if not SOURCE_ID_PATTERN.fullmatch(self.id):",
        "        if not SOURCE_ID_PATTERN.match(self.id):",
    ),
    (
        "②b-f 不校验 id 前缀 == provider_source_id",
        STORE,
        "        if head != self.provider_source_id:",
        "        if False:",
    ),
    # ---- ④ key 泄漏 ----
    (
        "④-a 源的 public_view 回显真实 key",
        STORE,
        '            "key": [KEY_MASK] if self.key else [],',
        '            "key": list(self.key),',
    ),
    (
        "④-b 掩码按真实长度生成（泄漏 key 长度）",
        STORE,
        '            "key": [KEY_MASK] if self.key else [],',
        '            "key": ["•" * len(self.key[0])] if self.key else [],',
    ),
    (
        "④-c 上游错误体不脱敏（直接转发 key）",
        API,
        '            detail = scrub_text((resp.text or "")[:200]).replace("\\n", " ")',
        '            detail = (resp.text or "")[:200].replace("\\n", " ")',
    ),
    # ---- ⑤ key 为空 = 不修改 ----
    (
        "⑤-a PUT 把空 key 当成清空",
        STORE,
        "                    if new_keys:\n                        target.key = new_keys",
        "                    if True:\n                        target.key = new_keys",
    ),
    (
        "⑤-b PUT 忽略 key 字段（永远不改 key）",
        STORE,
        '            if "key" in patch:',
        '            if False:',
    ),
    (
        "⑤-c 掩码占位符被当成真 key 存下来（静默毁数据）",
        STORE,
        "        if _is_key_mask(text):",
        "        if False:",
    ),
    # ---- ⑥ 悬空引用 ----
    (
        "⑥-a 不校验 provider_source_id 是否存在",
        STORE,
        "        if self.provider_source_id not in source_ids:",
        "        if False:",
    ),
    (
        "⑥-b 读取路径不报悬空引用（只有写入口拦）",
        STORE,
        "                m.validate_against_sources(source_ids)",
        "                pass  # 注入：读路径不校验",
    ),
    (
        "⑥-c POST 模型的路径源不存在也放行",
        API,
        '        given = str(body.get("provider_source_id") or "").strip()',
        '        given = ""  # 注入：不比对路径源',
    ),
    # ---- ⑦ provider_type ----
    (
        "⑦-a 去掉 provider_type 白名单（假装支持 embedding）",
        STORE,
        "        if self.provider_type not in SUPPORTED_PROVIDER_TYPES:",
        "        if False:",
    ),
    (
        "⑦-b 把不支持的类型降级成 chat_completion（静默改写用户输入）",
        STORE,
        "        if self.provider_type not in SUPPORTED_PROVIDER_TYPES:",
        "        if False:  # 注入\n        self.provider_type = PROVIDER_TYPE_CHAT\n        if False:",
    ),
    # ---- ⑧ 迁移 ----
    (
        "⑧-a 读到 v1 不迁移（直接当新格式用）",
        STORE,
        "        if needs_migration(data):",
        "        if False:",
    ),
    (
        "⑧-b 迁移不落盘（只改内存）",
        STORE,
        "                with self._lock:\n                    payload = dict(migrated)\n                    payload[\"version\"] = SCHEMA_VERSION\n                    self._write_payload(payload, corrupt=False)",
        "                pass  # 注入：不落盘",
    ),
    (
        "⑧-c 迁移不记日志",
        STORE,
        '            log.info(\n                "供应商配置从 v1（单层）迁移到 v2（两层）：%s —— 迁移 %d 个端点 / %d 个模型",',
        '            log.debug(\n                "供应商配置从 v1（单层）迁移到 v2（两层）：%s —— 迁移 %d 个端点 / %d 个模型",',
    ),
    (
        "⑧-d 迁移丢掉旧 api_key",
        STORE,
        "        if isinstance(raw_key, str) and raw_key.strip():\n            key_list = [raw_key.strip()]",
        "        if False:\n            key_list = [raw_key.strip()]",
    ),
    (
        "⑧-e 迁移丢掉旧 timeout",
        STORE,
        '                "timeout": float(item.get("timeout") or DEFAULT_TIMEOUT)\n                if isinstance(item.get("timeout"), (int, float))\n                and not isinstance(item.get("timeout"), bool)\n                else DEFAULT_TIMEOUT,',
        '                "timeout": DEFAULT_TIMEOUT,',
    ),
    (
        "⑧-f 迁移把旧默认 id 原样留下（悬空默认）",
        STORE,
        "    elif old_default in legacy_to_new:\n        new_default = legacy_to_new[old_default]",
        "    elif False:\n        new_default = legacy_to_new[old_default]",
    ),
    (
        "⑧-g needs_migration 恒真（每次读都重迁 → 丢源上的 key/超时）",
        STORE,
        "def needs_migration(data: dict[str, Any]) -> bool:",
        "def needs_migration(data: dict[str, Any]) -> bool:\n    return True  # 注入",
    ),
    (
        "⑧-h 迁移的临时文件扔到 /tmp（失去原子性）",
        STORE,
        '        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")',
        '        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp")',
    ),
    # ---- ⑨ 取模型列表 ----
    (
        "⑨-a 空模型列表当成功",
        API,
        "        if not ordered:",
        "        if False:",
    ),
    (
        "⑨-b 超时走通用错误（原因不逐条）",
        API,
        "        except httpx.TimeoutException:",
        "        except httpx.TimeoutException if False else ():",
    ),
    (
        "⑨-c 401 走通用错误（原因不逐条）",
        API,
        "        if resp.status_code in (401, 403):",
        "        if False:",
    ),
    (
        "⑨-d 非 JSON 当成功（不报 bad_payload）",
        API,
        '                "上游返回的不是 JSON —— 确认地址填的是 OpenAI 兼容端点（通常以 /v1 结尾）",\n                502,\n                "upstream_bad_payload",',
        '                "x",\n                200,\n                "upstream_bad_payload",',
    ),
    # ---- ⑩ test 端点 ----
    (
        "⑩-a 空回复当 available",
        API,
        "        if not content.strip():",
        "        if False:",
    ),
    (
        "⑩-b test 结果不带 status 字段",
        API,
        '                "status": "unavailable",',
        '                "state": "unavailable",',
    ),
    (
        "⑩-c test 成功时不置 available",
        API,
        '        return _result(\n            status="available",',
        '        return _result(\n            status="unavailable",',
    ),
    (
        "⑩-d sample 不截断（回整段）",
        API,
        '            sample=content.strip()[:SAMPLE_CHARS],',
        '            sample=content.strip(),',
    ),
    # ---- ⑪ 宿主同步 ----
    (
        "⑪-a 新增模型后不同步宿主（POST /providers 那条入口）",
        API,
        '        is_default = created.id == _store().default_provider_id\n        _sync_model(created, make_default=is_default)\n        return _ok({**created.public_view(), "is_default": is_default}, "新增模型成功", 201)',
        '        is_default = created.id == _store().default_provider_id\n        return _ok({**created.public_view(), "is_default": is_default}, "新增模型成功", 201)',
    ),
    (
        "⑪-a2 新增模型后不同步宿主（契约 §2 子资源那条入口）",
        API,
        '        is_default = created.id == store.default_provider_id\n        _sync_model(created, make_default=is_default)\n        return _ok(\n            {**created.public_view(), "is_default": is_default},',
        '        is_default = created.id == store.default_provider_id\n        return _ok(\n            {**created.public_view(), "is_default": is_default},',
    ),
    (
        "⑪-b 改源后不同步它下面的模型",
        API,
        "            _sync_models_of_source(source_id)",
        "            pass  # 注入：不同步",
    ),
    (
        "⑪-c 改模型后不同步宿主",
        API,
        "        pm = _sync_model(updated, make_default=is_default)",
        "        pm = None  # 注入：不同步",
    ),
    (
        "⑪-d 改名后不清掉宿主的旧 id 条目（僵尸条目）",
        API,
        "        if updated.id != provider_id:\n            _sync_unregister(provider_id)",
        "        if False:\n            _sync_unregister(provider_id)",
    ),
    (
        "⑪-e 删源不级联摘掉模型",
        API,
        "        for mid in doomed:\n            _sync_unregister(mid)",
        "        pass  # 注入：不摘",
    ),
    (
        "⑪-f 宿主缺席时抛异常（面板直接打不开）",
        API,
        '            log.info("跳过 ProviderManager 同步（%s）：%s", why, model.id)\n            return None',
        '            raise RuntimeError(f"宿主不可用：{why}")',
    ),
    (
        "⑪-g 宿主拒绝设默认时静默（不记 WARNING）",
        API,
        "            if not pm.set_default(provider_id):",
        "            if False:",
    ),
    (
        "⑪-h 用空壳端口冒充【已接入】",
        API,
        "        port = None\n        if backend is not None:",
        "        port = None\n        if True:  # 注入：没有后端也接空壳",
    ),
    (
        "⑪-i 形状不对的后端也硬接（不检查 complete）",
        API,
        '        if not hasattr(backend, "complete"):',
        "        if False:",
    ),
    # ---- 装配 ----
    (
        "装配-a 忘记在 app.py 挂蓝图（全部路由 404）",
        APP,
        "    app.register_blueprint(build_providers_blueprint(_require_auth_or_api_key))",
        "    pass  # 注入：不挂蓝图",
    ),
    (
        "装配-b 漏掉源列表的鉴权（裸奔）",
        API,
        '    @bp.get("/provider-sources")\n    async def list_provider_sources():  # type: ignore[no-untyped-def]',
        '    @bp.get("/provider-sources")\n    async def list_provider_sources():  # type: ignore[no-untyped-def]\n        return _ok(_store().snapshot_sources())  # 注入：无鉴权',
    ),
    (
        "装配-c 单条模型只支持编码形态（未编码斜杠 → 405）",
        API,
        '    @bp.route("/providers/<path:provider_id>", methods=["GET", "PUT", "DELETE", "PATCH", "POST"])',
        '    @bp.route("/providers/<provider_id>", methods=["GET", "PUT", "DELETE", "PATCH", "POST"])',
    ),
    (
        "装配-d 路由不吃 /default 后缀（设默认 405）",
        API,
        '            if provider_id.endswith("/default"):\n                provider_id, suffix = provider_id[: -len("/default")], "default"',
        "            pass  # 注入：不分派 default 后缀",
    ),
    # ---- 写接口不回显 ----
    (
        "⑫-a 新增源响应回显 key",
        API,
        '        return _ok(created.public_view(), "创建 provider source 成功", 201)',
        '        return _ok({**created.public_view(), "key": list(created.key)}, "创建 provider source 成功", 201)',
    ),
    (
        # 注入目标选 **api_base 那条错误消息**：判据
        # `test_source_key_never_appears_in_error_or_upstream_echo` 里那个 400
        # 正好走的就是它（id 那条走不到，注进去也不会挂 → 属于"注入无效"而非"判据弱"）。
        "⑫-b 校验失败的错误消息带上整条记录（含 key）",
        STORE,
        '                "api_base 必须以 http:// 或 https:// 开头"',
        '                f"api_base 非法：{self!r} —— 必须以 http:// 或 https:// 开头"',
    ),
    # ---- 落盘 ----
    (
        "⑬-a 写入改成非原子（直接 write_text）",
        STORE,
        "            os.chmod(tmp, 0o600)\n            os.replace(tmp, path)",
        '            os.chmod(tmp, 0o600)\n            path.write_text(text, encoding="utf-8")',
    ),
    (
        # ⚠️ 注入方式说明（与旧版同一个坑）：只**加**一行 chmod 0o644 是**等价改写**
        #    —— 后面那句 `os.chmod(tmp, 0o600)` 会把它盖回去，一条判据都不会挂。
        #    必须整段替换成"一个人随手写出来的最朴素版本"（普通 open + 不设权限）。
        "⑬-b 写入退回朴素版本（普通 open + 不设权限 + 不 fsync）",
        STORE,
        '        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")\n'
        '        try:\n'
        '            with os.fdopen(fd, "w", encoding="utf-8") as fh:\n'
        '                fh.write(text)\n'
        '                fh.flush()\n'
        '                os.fsync(fh.fileno())        # 先让内容真的落盘，再换名\n'
        '            # 🔴 文件里有**明文 api_key** → 只给属主读写（与 `.env` 同级处理）\n'
        '            os.chmod(tmp, 0o600)\n'
        '            os.replace(tmp, path)',
        '        tmp = str(path) + ".tmp"\n'
        '        try:\n'
        '            with open(tmp, "w", encoding="utf-8") as fh:\n'
        '                fh.write(text)\n'
        '            os.replace(tmp, path)',
    ),
    (
        "⑬-c 损坏时静默（不记 ERROR）",
        STORE,
        '            log.error("供应商配置 JSON 损坏（按空配置继续）：%s → %s", path, exc)',
        "            pass  # 注入：静默",
    ),
    (
        "⑬-d 缺失文件也报 ERROR（把正常当错误）",
        STORE,
        "        if not path.is_file():\n            return empty, False",
        '        if not path.is_file():\n            log.error("供应商配置不存在：%s", path)\n            return empty, False',
    ),
    (
        "⑬-e 损坏时不备份，直接覆盖",
        STORE,
        "        if corrupt and path.is_file():\n            self._preserve_corrupt(path)",
        "        pass  # 注入：不备份",
    ),
    (
        "⑬-f 落盘不写 provider_sources（只写模型层）",
        STORE,
        '        payload["provider_sources"] = [s.to_storage_dict() for s in sources]',
        "        pass  # 注入：丢端点层",
    ),
]

SUMMARY_RE = re.compile(r"^(FAILED|ERROR) (\S+)", re.M)


def run_tests() -> tuple[int, int, list[str]]:
    proc = subprocess.run(
        [PY, "-m", "pytest", "-q", "tests/test_providers_api.py", "-p", "no:cacheprovider"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    out = proc.stdout + proc.stderr
    failed = sorted({m.group(2).split("::")[-1].split("[")[0] for m in SUMMARY_RE.finditer(out)})
    passed = re.search(r"(\d+) passed", out)
    errs = re.search(r"(\d+) error", out)
    npassed = int(passed.group(1)) if passed else 0
    if errs:
        npassed = -int(errs.group(1))       # 收集阶段就炸了
    return npassed, len(failed), failed


#: 注入留下的**指纹**。`backup()` 拿它拒绝"在坏文件上建基线"。
#: ⚠️ 三种都见过：`if False`（本次事故）、`INJECT` 注释标记、
#:    以及"被我改短/改坏的正则"不好通用识别 → 只认前两种**确定性**指纹。
INJECTION_MARKERS = ("if False", "if False else", "# INJECT",  # noqa: E501
                     "#INJECT", "INJECTED")

#: 所有被本脚本原地改写的产品文件。
TARGETS: tuple[Path, ...] = (STORE, API, APP, TESTS)

#: 备份文件的 sha256（backup 时填），用于证明"恢复后与基线逐字节相同"。
_BASELINE_SHA: dict[str, str] = {}
#: 当前是否有注入处于"已写入未恢复"状态（供 atexit 兜底判断）。
_DIRTY = False


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def backup() -> int:
    """建基线并**拒绝在已被注入过的文件上建基线**（否则会把损坏固化）。

    🔴 这是本次真实事故的教训：原来是 `rmtree` + 复制当前磁盘文件，
       第二次运行时把**上一次残留的坏文件**当干净基线备份了，
       `restore()` 从此恢复的就是坏文件 —— 损坏被固化、怎么跑都修不回来。
       → 宁可**拒绝运行**，也不要固化损坏。
    """
    dirty = []
    for p in TARGETS:
        text = p.read_text(encoding="utf-8", errors="replace")
        hits = [m for m in INJECTION_MARKERS if m in text]
        if hits:
            dirty.append(f"{p.relative_to(ROOT)}（含 {hits}）")
    if dirty:
        print("🔴 拒绝建基线：下列文件**看起来已经被注入过**（残留指纹）：")
        for d in dirty:
            print("     -", d)
        print("\n   先手工把这几个文件恢复到干净版本，再跑本脚本。")
        print("   ⚠️ 别直接重跑：那样会把损坏当作基线**固化**下来。")
        return 1

    shutil.rmtree(BACKUP, ignore_errors=True)
    BACKUP.mkdir(parents=True)
    for p in TARGETS:
        shutil.copy2(p, BACKUP / p.name)
        _BASELINE_SHA[p.name] = _sha(BACKUP / p.name)
    return 0


def restore() -> None:
    """恢复并**自证**恢复正确（不比 sha256 就只是"我以为恢复了"）。"""
    global _DIRTY
    for p in TARGETS:
        shutil.copy2(BACKUP / p.name, p)
        got = _sha(p)
        if got != _BASELINE_SHA.get(p.name):
            print(f"🔴 恢复后与基线**不一致**：{p.name}"
                  f"\n    基线 {_BASELINE_SHA.get(p.name)}"
                  f"\n    现在 {got}")
    _DIRTY = False


def _emergency_restore(*_a: object) -> None:
    """任何退出路径（含 Ctrl-C / SIGTERM / 未捕获异常）都要恢复。

    🔴 没有这个兜底时，一次 SIGTERM 就会把注入留在产品文件里 —— 实测发生过。
    """
    if _DIRTY and BACKUP.is_dir():
        print("\n⚠️ 检测到注入未恢复（中断/异常）→ 正在紧急恢复…")
        try:
            restore()
            print("✅ 已恢复到基线")
        except Exception as exc:            # pragma: no cover - 兜底路径
            print(f"🔴 紧急恢复失败：{exc}\n    干净副本仍在 {BACKUP}")


def main() -> int:
    global _DIRTY
    if backup() != 0:
        return 1
    # 兜底：正常退出、异常退出、SIGINT/SIGTERM 都要恢复
    atexit.register(_emergency_restore)
    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(_sig, lambda *a: sys.exit(1))
        except (ValueError, OSError):       # 非主线程等场景
            pass

    baseline_pass, baseline_fail, baseline_names = run_tests()
    print(f"基线：{baseline_pass} passed, {baseline_fail} failed")
    if baseline_fail:
        print("🔴 基线就不干净，中止：", baseline_names)
        return 1

    results = []
    for title, path, old, new in INJECTIONS:
        text = path.read_text(encoding="utf-8")
        if old not in text:
            results.append((title, "❌ 注入失败（找不到锚点）", []))
            print(f"\n=== {title}\n    ❌ 注入失败：锚点找不到")
            continue

        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        _DIRTY = True        # ← 从这里到 restore() 之间被中断，兜底才会救
        npass, nfail, names = run_tests()
        if nfail:
            verdict = f"✅ 挂了 {nfail} 条"
        else:
            verdict = "🔴 没挂任何判据（假通过！）"
        results.append((title, verdict, names))
        print(f"\n=== {title}\n    {verdict}（{npass} passed）")
        for n in names[:6]:
            print(f"      - {n}")
        restore()

    # 最终确认：恢复后仍然是全绿
    final_pass, final_fail, final_names = run_tests()
    print(f"\n{'='*70}\n恢复后：{final_pass} passed, {final_fail} failed {final_names}")

    print(f"\n{'='*70}\n汇总（注入 → 判据反应）")
    bad = 0
    for title, verdict, _ in results:
        if "🔴" in verdict or "❌" in verdict:
            bad += 1
        print(f"  {verdict:<28} {title}")
    print(f"\n未挂判据/注入失败的条数：{bad}")
    return 0 if (bad == 0 and final_fail == 0 and final_pass == baseline_pass) else 1


if __name__ == "__main__":
    sys.exit(main())
