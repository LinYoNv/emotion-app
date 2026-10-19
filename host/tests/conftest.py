# -*- coding: utf-8 -*-
"""pytest 公共配置：把 host 目录放进 sys.path。

⚠️ 不在这里安装影子包 —— 因为安装是**进程级副作用**，
   某些测试需要验证"未安装时的行为"，交给各测试自己管。
"""
from __future__ import annotations

import sys
from pathlib import Path

HOST_DIR = Path(__file__).resolve().parent.parent
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))


# ---------------------------------------------------------------------------
# 🔴 共享镜像根（2026-10-19，陪伴 7.0.0 引入的硬限制）
# ---------------------------------------------------------------------------
#
# ## 为什么需要它
#
# 陪伴 7.0.0 的 `astrbot_host/import_lifecycle.py:21-24` 有一道**正确**的保护：
#
#     RuntimeError: another companion_v2 package is already loaded
#                   from a different source root
#
# 它拒绝"`companion_v2` 已从**另一个源根**加载过"的再次加载 —— 目的是防
# 「两份代码混用」（那会让 data 模型与 handler 对不上，且极难查）。
#
# ## 它和我们的测试怎么冲突的
#
# 各测试文件**各自** `tempfile.mkdtemp()` 造一个新 mirror 目录再加载 ⇒
# 同一个 pytest 进程里 `companion_v2` 会被要求从 5、6 个不同的源根加载
# ⇒ 从第二个开始全部失败。**单独跑每个文件都绿，全量跑就红**。
#
# ## 解法
#
# 让所有测试**复用同一个镜像根**（内容本来就相同，都是同一个 `plugins/` 的副本）。
# 这样"源根"始终只有一个，保护照常生效、测试也不再互相打架。
#
# ⚠️ **为什么不在产品代码里加"同源复用"逻辑**：那是为了让测试过而改产品行为，
#    而且会削弱那道保护（一旦真有第二份代码，它应该拒绝）。
#    正确的做法是在**测试侧**造出确定前提。
import atexit as _atexit
import shutil as _shutil
import tempfile as _tempfile

from pathlib import Path as _Path

#: 进程内唯一的插件镜像根（首次使用时创建，进程退出时删除）
_SHARED_MIRROR_ROOT = _Path(_tempfile.mkdtemp(prefix="host-tests-shared-mirror-"))
_atexit.register(lambda: _shutil.rmtree(_SHARED_MIRROR_ROOT, ignore_errors=True))


def shared_mirror_root() -> _Path:
    """取**进程级共享**的镜像根（同一次 pytest 运行里所有测试复用）。

    ⚠️ 返回的是**同一个目录**：测试不要往里写自己的东西，
       它只作为"插件源码镜像"的落点。
    """
    return _SHARED_MIRROR_ROOT
