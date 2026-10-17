# -*- coding: utf-8 -*-
"""插件升级 —— 从插件 `metadata.yaml` 声明的仓库拉取新版本并就地替换。

⭐ **契约照抄 AstrBot 真实现**（v4.27.5，只读源码）：

    * `dashboard/services/plugin_service.py:1775 update_plugin()`  —— 服务层
    * `core/star/updater.py:225 update()`                          —— 下载/校验/替换
    * `core/star/updater.py:374 inspect_plugin_directory()`        —— 合法性校验
    * `dashboard/api/plugins.py:1143 POST /plugins/{id}/update`    —— 路由形状

🔴 **与真实现唯一的差异（本项目特有，不是可选优化）**：
   真实现用 `remove_dir(plugin_path)` **整目录删掉再解压** —— 它能这么做是因为
   AstrBot 的用户数据全在 `data/`（`plugin_store_path` 之外），插件目录里只有源码。
   **我们这里多一类文件在源码目录内**：`file` 型配置项的上传落点
   （如 `astrbot_plugin_nai_image/files/`，见 `_CONF_SCHEMA` 的 `file_types`）。
   ⇒ 覆盖时必须**跳过这些路径**，否则升级会删掉用户上传的文件。

🔴 **代理是【每次请求传】的，后端不存**（与真实现一致）：
   真实现的 `update_plugin(data)` 从 payload 取 `proxy`，不是全局配置。
   本项目面向多用户自部署，「用什么出口」由部署者自己决定 —— 所以我们照抄，
   **不写死任何代理地址、也不读环境变量做默认**（那会让"换个地方部署就行为不同"）。

⚠️ **没有热重载**：我们的宿主 `PluginLoader` 只在启动时 `discover()`。
   ⇒ 升级成功后**必须重启服务**才生效，返回值里显式说明这一点，不假装已生效。
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("astrbot_host.plugin_updater")

# ---- 与真实现逐字对齐的常量（`core/star/updater.py:25-30`）----

#: 插件元数据文件名（按顺序找，第一个命中即用）
METADATA_FILENAMES: tuple[str, ...] = ("metadata.yaml", "metadata.yml")

#: 必填字段（真实现 `PLUGIN_METADATA_REQUIRED_FIELDS`）
METADATA_REQUIRED_FIELDS: tuple[str, ...] = ("name", "desc", "version", "author")

#: 元数据文件体积上限（真实现 `PLUGIN_METADATA_MAX_BYTES` = 1024 * 1024）
METADATA_MAX_BYTES: int = 1024 * 1024

#: 仓库探测超时（真实现 `PLUGIN_REPOSITORY_TIMEOUT_SECONDS` = 15）
REPOSITORY_TIMEOUT_SECONDS: int = 15

#: clone 超时（真实现 `PLUGIN_GIT_CLONE_TIMEOUT_SECONDS` = 180）。
#:
#: 🔴 **真实现的 180s 在慢网络下不够 —— 实测踩到**（2026-10-17）：
#:   当时 GitHub 下行只有 **~15 KB/s**，`astrbot_plugin_memory_companion`（25 MB）
#:   需要约 **27 分钟** ⇒ 180s 必然超时，而**用户看到的是"代理没生效"**（误导）。
#:   真实现能留 180s 是因为它面向"服务器 VPS 直连 GitHub"；我们没有那个前提。
#:
#: ⇒ 默认放宽到 **600s**，并允许**每次请求覆盖**（`timeout` 参数 / 环境变量）。
#:   超时不再是"写死的值"，因为最佳值取决于部署地的网速。
GIT_CLONE_TIMEOUT_SECONDS: int = 600

#: 允许部署者用环境变量覆盖（不改代码）。非法值忽略、不报错（不该因为一个
#: 环境变量写错就让升级功能整个不可用）。
_ENV_TIMEOUT = "EMOTION_PLUGIN_UPDATE_TIMEOUT_SECONDS"


def resolve_timeout(override: int | None = None) -> int:
    """本次 clone 的超时秒数。优先级：显式参数 → 环境变量 → 默认值。

    ⚠️ 上限 **3600s**（1 小时）：再长就不该在 HTTP 请求里等了 ——
       真需要那么久说明网络不可用，早失败比挂一小时更好。
    """
    candidates: list[object] = []
    if override is not None:
        candidates.append(override)
    candidates.append(os.environ.get(_ENV_TIMEOUT))
    for raw in candidates:
        if raw is None:
            continue
        try:
            val = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if val > 0:
            return min(val, 3600)
    return GIT_CLONE_TIMEOUT_SECONDS

#: 插件入口模块名 —— 我们宿主的 `discover()` 依赖它（真实现没有这条，是我们的额外校验）
ENTRY_MODULE: str = "main.py"

#: 🔴 **升级时必须保留的路径**（相对插件目录）。
#:
#: 这些是**用户数据落在源码目录里**的那一类 —— 真实现没有这个清单，
#: 因为它的用户数据全在 `data/`。我们这里必须显式保护。
#:
#: `files/` 的来路：`file` 型配置项的上传落点（`astrbot_plugin_nai_image` 的
#: `openai_director_fallback_images` 就写在这里，实测 `_CONF_SCHEMA` 的 `file_types`）。
#: ⚠️ **即使当前为空也要保护** —— 用户一旦上传，升级就再也删不回来了。
DEFAULT_PRESERVE: tuple[str, ...] = ("files/",)

#: 保留清单里**额外排除**的运行时垃圾（覆盖不删、但也不该被当成"要保护的数据"）
_IGNORED_NAMES = frozenset({".git", "__pycache__", ".pytest_cache"})


class PluginUpdateError(Exception):
    """升级失败 —— `message` 直接给用户看，必须能指出下一步怎么做。"""


@dataclass
class UpdateResult:
    """一次升级的结果（全部字段都会进接口响应，**不吞掉任何一项**）。"""

    plugin: str
    dir_name: str
    path: str
    old_version: str = ""
    new_version: str = ""
    repo: str = ""
    #: 真正被写入的文件数
    written: int = 0
    #: 被保护清单跳过的路径（**必须回报**，否则用户以为全量覆盖了）
    preserved: list[str] = field(default_factory=list)
    #: 旧目录残留（保护清单之外、但新版本里没有的文件）——我们是**保留**，不是删
    kept_extra: list[str] = field(default_factory=list)
    #: 是否需要重启才生效（本项目恒为 True，**显式返回**而不是让调用方猜）
    restart_required: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "plugin": self.plugin,
            "dir_name": self.dir_name,
            "path": self.path,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "repo": self.repo,
            "written": self.written,
            "preserved": list(self.preserved),
            "kept_extra": list(self.kept_extra),
            "restart_required": self.restart_required,
        }


# ---------------------------------------------------------------- 元数据


def _read_metadata(plugin_dir: Path) -> dict[str, Any]:
    """读并校验插件元数据 —— 校验规则**逐条对齐真实现**。

    Raises:
        PluginUpdateError: 找不到 / 太大 / 编码错 / 格式错 / 缺必填字段。
    """
    try:
        import yaml
    except Exception as exc:  # noqa: BLE001
        raise PluginUpdateError("缺少 PyYAML，无法读取插件元数据") from exc

    for name in METADATA_FILENAMES:
        p = plugin_dir / name
        if not p.is_file():
            continue
        # 真实现：`metadata_path.stat().st_size > PLUGIN_METADATA_MAX_BYTES` → 报错
        size = p.stat().st_size
        if size > METADATA_MAX_BYTES:
            raise PluginUpdateError(f"{name} 超过 1MB（{size} 字节）")
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise PluginUpdateError(f"{name} 必须使用 UTF-8 编码") from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise PluginUpdateError(f"{name} 格式错误：{exc}") from exc

        if not isinstance(data, dict):
            raise PluginUpdateError(f"{name} 格式错误：顶层不是映射")
        # 真实现：`desc` 缺失时用 `description` 顶（`validate_plugin_metadata` 里那段）
        normalized = dict(data)
        if "desc" not in normalized and "description" in normalized:
            normalized["desc"] = normalized["description"]

        missing = [f for f in METADATA_REQUIRED_FIELDS if f not in normalized]
        if missing:
            raise PluginUpdateError(f"{name} 中缺少必需字段: {', '.join(missing)}")
        invalid = [
            f
            for f in METADATA_REQUIRED_FIELDS
            if not isinstance(normalized[f], str) or not normalized[f].strip()
        ]
        if invalid:
            raise PluginUpdateError(
                f"{name} 中字段 {', '.join(invalid)} 必须是非空字符串"
            )
        return normalized

    raise PluginUpdateError(
        "未在仓库根目录找到 metadata.yaml 或 metadata.yml —— 这不是一个插件仓库"
    )


def _validate_plugin_tree(root: Path) -> dict[str, Any]:
    """校验一个目录是**能跑在我们宿主上**的插件。

    比真实现多一条：必须有 `main.py`（我们 `PluginLoader.discover()` 的要求）。
    """
    meta = _read_metadata(root)
    if not (root / ENTRY_MODULE).is_file():
        raise PluginUpdateError(
            f"插件目录缺少入口模块 {ENTRY_MODULE} —— 我们的宿主按它加载，装了也起不来"
        )
    return meta


# ---------------------------------------------------------------- 下载


async def _run(cmd: list[str], *, timeout: int, cwd: Path | None = None) -> tuple[int, str]:
    """跑一条外部命令，返回 `(returncode, 合并输出)`。

    ⚠️ 用 `create_subprocess_exec` 而不是 `shell=True` —— `proxy` 是用户输入，
    绝不能拼进 shell。
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise PluginUpdateError(f"命令超时（{timeout}s）：{cmd[0]}") from None
    return proc.returncode, (out or b"").decode("utf-8", errors="replace")


def _git_env_with_proxy(proxy: str) -> dict[str, str]:
    """把用户传的 `proxy` 变成 git 的环境变量（**不改全局 git 配置**）。

    支持 `http://…` / `https://…` / `socks5://…`（git 认 `ALL_PROXY`）。
    ⚠️ 只作用于本次子进程 —— 不影响机器上其它 git 操作。
    """
    env = dict(os.environ)
    # 🔴 私有仓库时 git 会想交互式要账号 ⇒ 子进程里等于永久挂起。禁掉。
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "echo"
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["ALL_PROXY"] = proxy
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
        env["all_proxy"] = proxy
    return env


async def _clone(repo_url: str, dest: Path, proxy: str, timeout: int | None = None) -> None:
    """浅克隆到 `dest`（真实现同样是 shallow 语义的临时目录 checkout）。

    ⚠️ 额外设 `GIT_TERMINAL_PROMPT=0`：仓库若是**私有**的，git 会尝试弹
       交互式用户名提示，在子进程里那就是**永久挂起**（比超时更糟）。
       禁止交互 ⇒ 立刻报"认证失败"，用户能看懂。
    """
    limit = resolve_timeout(timeout)
    cmd = ["git", "clone", "--depth", "1", repo_url, str(dest)]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=_git_env_with_proxy(proxy),
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=limit)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise PluginUpdateError(
            f"拉取超时（{limit}s）。仓库较大或网速较慢时会这样："
            f"① 在「代理」里填一个更快的出口再试；"
            f"② 或用环境变量 {_ENV_TIMEOUT} 放宽超时（当前 {limit}s）"
        ) from None
    if proc.returncode != 0:
        text = (out or b"").decode("utf-8", errors="replace").strip()
        # 🔴 把 git 的原话带上 —— 用户需要知道是网络、认证还是仓库不存在
        raise PluginUpdateError(f"拉取仓库失败：{text or f'git 退出码 {proc.returncode}'}")


async def _fetch_archive(url: str, dest: Path, proxy: str) -> None:
    """下载 zip 包（真实现的 `download_url` 分支）。"""
    limit = resolve_timeout()
    cmd = ["curl", "-fsSL", "--max-time", str(limit), "-o", str(dest)]
    if proxy:
        cmd[1:1] = ["-x", proxy]
    cmd.append(url)
    code, out = await _run(cmd, timeout=limit + 10)
    if code != 0:
        raise PluginUpdateError(f"下载压缩包失败：{out.strip() or f'curl 退出码 {code}'}")


# ---------------------------------------------------------------- 覆盖


def _is_preserved(rel: Path, preserve: tuple[str, ...]) -> str | None:
    """`rel` 是否落在保留清单里？返回命中的那一条，便于回报。"""
    posix = rel.as_posix()
    for item in preserve:
        p = item.rstrip("/")
        if posix == p or posix.startswith(p + "/"):
            return item
    return None


def _merge_tree(
    *, src: Path, dst: Path, preserve: tuple[str, ...]
) -> tuple[int, list[str], list[str]]:
    """把 `src` 覆盖到 `dst`，**跳过保留清单**。

    🔴 与真实现的关键差异：真实现 `remove_dir(dst)` 后整解压；我们
    **不删目录、逐文件覆盖** —— 这样保留清单之外、但新版本里没有的文件
    （插件自己生成的运行产物）也不会凭空消失。这是"不删用户数据"的最强保证：
    **我们从不执行任何删除**，只写新文件。

    Returns:
        `(写入文件数, 被跳过的保留路径, 旧版本独有的文件)`
    """
    written = 0
    preserved: list[str] = []
    src_files: set[Path] = set()

    # 🔴 **先记一遍"新版本里也带了保留路径"的情况** —— 那些文件会被跳过。
    #    ⚠️ 但只有这一步不够：如果新版本里**没有** `files/`（最常见，因为它是
    #    用户数据不是源码），就一个都没记上 ⇒ `preserved` 为空 ⇒ 用户以为
    #    "全量覆盖了"。下面的尾部检查会把"本地存在但被保护"的路径也补记上。
    for root, dirs, files in os.walk(src):
        root_p = Path(root)
        rel_root = root_p.relative_to(src)
        dirs[:] = [d for d in dirs if d not in _IGNORED_NAMES]
        for fn in files:
            rel = (rel_root / fn) if rel_root.parts else Path(fn)
            hit = _is_preserved(rel, preserve)
            if hit is not None and hit not in preserved:
                preserved.append(hit)

    for root, dirs, files in os.walk(src):
        root_p = Path(root)
        rel_root = root_p.relative_to(src)
        # 不进 .git / 缓存
        dirs[:] = [d for d in dirs if d not in _IGNORED_NAMES]

        for fn in files:
            rel = (rel_root / fn) if rel_root.parts else Path(fn)
            src_files.add(rel)
            hit = _is_preserved(rel, preserve)
            if hit is not None:
                continue
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            # 先写临时名再 replace：中途失败不会留下半截文件
            tmp = target.with_name(target.name + ".upgrade-tmp")
            shutil.copy2(root_p / fn, tmp)
            os.replace(tmp, target)
            written += 1

    # 旧版本独有（新版本里没有）—— **保留不删**，但回报给用户知道
    kept_extra: list[str] = []
    for root, dirs, files in os.walk(dst):
        root_p = Path(root)
        rel_root = root_p.relative_to(dst)
        dirs[:] = [d for d in dirs if d not in _IGNORED_NAMES]
        hit_here = None
        for fn in files:
            rel = (rel_root / fn) if rel_root.parts else Path(fn)
            if rel.name.endswith(".upgrade-tmp"):
                continue
            hit = _is_preserved(rel, preserve)
            if hit is not None:
                # 🔴 本地**确实有**被保护的文件 ⇒ 必须回报，哪怕新版本里没有它。
                #    这条是"跳过要留痕"的具体落地：用户需要知道哪些东西没被覆盖。
                if hit not in preserved:
                    preserved.append(hit)
                continue
            if rel in src_files:
                continue
            kept_extra.append(rel.as_posix())
    return written, preserved, sorted(kept_extra)


async def update_plugin(
    *,
    plugin_dir: Path,
    proxy: str = "",
    repo_override: str = "",
    preserve: tuple[str, ...] = DEFAULT_PRESERVE,
    timeout: int | None = None,
) -> UpdateResult:
    """把 `plugin_dir` 升级到它 `metadata.yaml` 里 `repo:` 指向的最新版本。

    Args:
        plugin_dir: 已安装插件的目录（`plugins/<dir_name>`）。
        proxy: 本次请求使用的代理；**空串 = 直连**（不读环境变量兜底）。
        repo_override: 显式指定仓库地址（通常留空，走 metadata 的 `repo`）。
        preserve: 覆盖时跳过的相对路径清单。
        timeout: clone 超时秒数（`None` = 走 `resolve_timeout()` 的默认/环境变量）。

    Raises:
        PluginUpdateError: 任何一步失败，消息可直接展示给用户。
    """
    if not plugin_dir.is_dir():
        raise PluginUpdateError(f"插件目录不存在：{plugin_dir}")

    old_meta = _read_metadata(plugin_dir)
    repo = (repo_override or str(old_meta.get("repo") or "")).strip()
    if not repo:
        raise PluginUpdateError(
            "该插件的 metadata.yaml 没有声明 repo:，无法自动升级。"
            "请手工把新版本放进 plugins/ 目录"
        )

    result = UpdateResult(
        plugin=str(old_meta.get("name") or ""),
        dir_name=plugin_dir.name,
        path=str(plugin_dir),
        old_version=str(old_meta.get("version") or ""),
        repo=repo,
    )
    logger.info(
        "[插件升级] 开始 plugin=%s dir=%s repo=%s proxy=%s",
        result.plugin,
        result.dir_name,
        repo,
        "已设置" if proxy else "直连",
    )

    # 临时目录放在插件目录**旁边**（同一文件系统 ⇒ `os.replace` 是原子的）
    parent = plugin_dir.parent
    with tempfile.TemporaryDirectory(prefix=".upgrade-", dir=parent) as tmp:
        tmp_root = Path(tmp)
        checkout = tmp_root / plugin_dir.name

        if repo.endswith(".zip"):
            archive = tmp_root / "plugin.zip"
            await _fetch_archive(repo, archive, proxy)
            try:
                with zipfile.ZipFile(archive) as z:
                    z.extractall(tmp_root / "unzipped")
            except zipfile.BadZipFile as exc:
                raise PluginUpdateError("下载到的不是合法 zip 包") from exc
            unz = tmp_root / "unzipped"
            # 压缩包可能带一层顶层目录
            entries = [p for p in unz.iterdir() if p.name not in _IGNORED_NAMES]
            root = entries[0] if len(entries) == 1 and entries[0].is_dir() else unz
            new_meta = _validate_plugin_tree(root)
            written, preserved, kept = _merge_tree(src=root, dst=plugin_dir, preserve=preserve)
        else:
            await _clone(repo, checkout, proxy, timeout)
            new_meta = _validate_plugin_tree(checkout)
            written, preserved, kept = _merge_tree(
                src=checkout, dst=plugin_dir, preserve=preserve
            )

    result.new_version = str(new_meta.get("version") or "")
    result.written = written
    result.preserved = preserved
    result.kept_extra = kept
    logger.info(
        "[插件升级] 完成 plugin=%s %s → %s 写入 %d 个文件，保护 %s，保留旧文件 %d 个",
        result.plugin,
        result.old_version or "(未知)",
        result.new_version or "(未知)",
        result.written,
        result.preserved or "无",
        len(result.kept_extra),
    )
    return result
