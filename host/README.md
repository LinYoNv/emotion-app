# `host/` —— AstrBot 兼容宿主（插件加载器骨架）

> **一句话**：让 AstrBot 插件（尤其是**陪伴系**）在**不改一行插件代码**的前提下，
> 跑在「情绪app」自己的后端上。
>
> 这是 `爱语/docs/PLATFORM_POSITIONING.md` 里那条「**造宿主，不搬插件**」路线的落地实现：
> 既不重写、也不移植，而是**提供一个与 AstrBot 等义的运行环境**。

---

## 0. 当前状态（实测数字，可复现）

| 指标 | 值 | 怎么验的 |
|---|---:|---|
| 磁盘插件加载成功 | **11 / 12** | `tests/test_loader_all_plugins.py` |
| 陪伴系插件 | **5 / 5 全部成功** | 同上（核心判据） |
| 累计 handler | **203** | 同上 |
| 累计 Web API | **266**（190 + 70 + 6） | 同上 |
| 主插件 `private_companion` | v6.6.2 · **109** handler · **190** API | 同上 |
| 单元测试 | **35 passed** | `pytest host/tests` |
| 差分验证（对真 AstrBot 4.27.5） | **92/92 + 46/46 + 56/56** | 见 §5 |
| 已挂载并**可调用**的 API | **266**（真 HTTP 打真插件） | §8，`tests/test_plugin_host_e2e.py` |
| **插件拓展页** | **3 个**（陪伴面板 / QQ空间与书柜 / 一起房间），真浏览器打开有数据 | §7 |
| 服务启动耗时（含插件） | **~20s**（修复前 181.6s） | §8「超时隔离」 |

唯一失败的插件是 `meme_manager`，原因是**它自己的依赖缺失**
（`ModuleNotFoundError: No module named 'requests'`），**不是我们的 API 缺口**。
判据 `test_only_known_env_failures` 专门守着这条线：
**不允许把"兼容层没实现"伪装成"插件有问题"**。

---

## 1. 怎么跑

```bash
cd /root/Ai-works/情绪app

# 单元测试（35 项）
.venv/bin/python -m pytest host/tests -q

# 差分验证（必须用【真 AstrBot 那个解释器】+ PYTHONPATH 指向 host/）
#   ⚠️ 两个实现要在同进程里共存才能对拍：
#      真包名 `astrbot` 归真实现；影子包实际包名是 `astrbot_host.compat`
PYTHONPATH=$PWD/host /root/.local/share/uv/tools/astrbot/bin/python host/tests/tools_verify_compat.py
PYTHONPATH=$PWD/host /root/.local/share/uv/tools/astrbot/bin/python host/tests/tools_verify_behavior.py
PYTHONPATH=$PWD/host /root/.local/share/uv/tools/astrbot/bin/python host/tests/tools_verify_router.py
```

手动加载全部插件看报告：

```python
import asyncio
from astrbot_host.compat.core.star.context import HostPorts
from astrbot_host.loader import PluginLoader
from astrbot_host.shadow import install_shadow_package

install_shadow_package()                       # 把 compat 注册成顶层 `astrbot`
# 🔴 mirror_root **别省**：插件会回写自己的源码目录，而下面这个目录
#    是别人 AstrBot 的生产目录（见 §8「源码镜像」）。
loader = PluginLoader(
    plugin_dirs=["/opt/astrbot/data/plugins"],
    mirror_root="data/plugin_src",
)
report = asyncio.run(loader.load_all(ports=HostPorts(config={})))
print(report.summary())
```

---

## 2. 架构：两块拼图

```
                     插件代码（陪伴 / 记忆 / 生图 …，一行不改）
                                 │
                  from astrbot.core.star import Star
                  from astrbot.api.event import filter
                                 │
        ┌────────────────────────┴────────────────────────┐
        │  ① shadow.py —— 影子包                            │
        │     把 `astrbot_host.compat` 注册成顶层 `astrbot`  │
        │     ⚠️ 遇到**外来** astrbot 包时**拒绝覆盖**       │
        └────────────────────────┬────────────────────────┘
                                 │
        ┌────────────────────────┴────────────────────────┐
        │  ② astrbot_host/compat/ —— 兼容层（57 个 .py）     │
        │     Star / Context / 22 个装饰器 / 事件 / 消息 …   │
        └────────────────────────┬────────────────────────┘
                                 │  HostPorts（依赖倒置）
        ┌────────────────────────┴────────────────────────┐
        │  情绪app 领域层（chat / persona / image / …）      │
        │     ⚠️ 领域层**绝不** import astrbot_host（硬边界）│
        └─────────────────────────────────────────────────┘
```

### ① `shadow.py` —— 影子包技术

插件的 import 语句写死了 `astrbot.*`。我们不 fork 插件，而是
**让 `import astrbot` 解析到我们的实现**：

* `install_shadow_package()` 把 `astrbot_host.compat` 及其 ~45 个子模块
  注册进 `sys.modules` 的顶层 `astrbot` 命名空间；
* 🔴 **遇到外来 `astrbot` 包时拒绝覆盖**（`test_shadow_refuses_to_clobber_foreign_astrbot`）——
  静默替换掉真的 AstrBot 是灾难性的；
* 幂等（`test_shadow_install_is_idempotent`），重复调用不出错。

### ② `compat/` —— 兼容层

按真 AstrBot 4.27.5 的**接口面**实现，粒度到「插件实测用到的那一层」：

| 模块 | 内容 |
|---|---|
| `core/star/context.py` | **`Context`** —— 插件与宿主之间的唯一通道（见 §3） |
| `core/star/base.py` | `Star` / `StarMetadata` / `star_map` / `star_registry` |
| `core/star/register/star_handler.py` | **22 个装饰器**（`@filter.llm_tool` / `command` / …） |
| `core/star/filter/command.py` | 命令解析（223 行忠实移植：`print_types` / `validate_and_convert_params` / `GreedyStr`） |
| `core/platform/astr_message_event.py` | `AstrMessageEvent` / `MessageChain` |
| `core/provider/entities.py` | `LLMResponse`（`completion_text` 是 **property**） |
| `api/web.py` | `request` 代理 / `PluginRequest` / `json_response` … |
| `api/all.py` | re-export 面（`astrbot.api.all`） |

### ③ `loader/plugin_loader.py` —— 插件加载器

关键设计（每条都是**踩过坑才写上去的**）：

* **模块命名空间用目录名**：把插件父目录加入 `sys.path`，让
  `astrbot_plugin_private_companion` 直接成为包名。陪伴是 **735 个 .py** 的多文件插件，
  内部全是相对 import 与跨模块绝对 import —— 没有这一步根本跑不起来。
* **必须显式 import `<包>.main`**：`import <包名>` 只执行 `__init__.py`（通常为空），
  **插件类根本不会被定义** → 报「模块里没有 Star 子类」，把人引向"插件写错了"的方向。
* **实例化前注入类属性**（`name` / `author` / `plugin_id`）：照真实现
  `star_manager.py:1208-1213`；插件的 `__init__` 会读 `self.name`。
* **配置必须传真对象**：传 `None` 会让 `config.get(...)` 在 4600 行构造逻辑深处
  `AttributeError`。配置从插件的 `_conf_schema.json` 默认值构建。
* **handler 按模块路径计数**，不用前后差值：模块已 import 过时装饰器不会重跑，
  差值是 0（看起来像"没注册 handler"，其实是统计方法错）。

---

## 3. `Context`：宿主最小面 = 6 个成员

AST 实测（`爱语/docs/PLATFORM_POSITIONING.md` §3.3.1）：主插件继承 **43 个 Mixin + `Star`**，
但实际只调用 `Context` 的 **6 个成员**：

| 成员 | 调用次数 | 宿主怎么接 |
|---|---:|---|
| `get_config` | 19 | 读插件配置 |
| `conversation_manager` | 11 | 会话历史（**property**，未注入就抛错） |
| `send_message` | 9 | 消息投递 |
| `llm_generate` | 5 | 模型调用 |
| `get_using_tts_provider` | 4 | TTS（**如实返回 unavailable，不伪造**） |
| `get_llm_tool_manager` | 1 | 工具注册表 |

🔴 **未实现的能力显式抛 `NotImplementedError`**，绝不静默返回 `None`/空 ——
否则插件会以为"没配置/没历史"，产生最难排查的静默降级（项目铁律）。

能力通过 **`HostPorts`**（`ConversationManagerPort` / `LLMPort` / `DeliveryPort` …）
依赖倒置注入，`Context` 不知道宿主是 Quart 还是别的 ——
对应作者 §7「传输可换、语义不变」。

### `register_web_api` / `register_task` 的忠实度（**本轮修正**）

这两处曾与真实现有偏差，已对齐（都有**能抓住 bug 的回归测试**）：

| 项 | 真实现（4.27.5） | 我们（修正后） |
|---|---|---|
| `registered_web_apis` 形态 | **属性**，`list[(route, handler, methods, desc)]` | 同左（**活列表**，可直接 append） |
| 重复 `(route, methods)` | **替换**，不追加（`context.py:723-727`） | 同左 |
| `register_task` 存储 | `_register_tasks: list[Awaitable]`（**裸 awaitable**，`desc` 收下就丢） | 同左 |
| 大小写归一 | 在**消费侧** `.upper()`，登记时不动 | 同左 |

**我们额外做的**（只增不减，插件侧无感）：按下标平行的**归属表**，
让加载报告能回答「某个插件登记了几个 API」。

> 🔴 为什么必须做这个扩展：原实现用 `len(context.registered_web_apis)` 统计，
> 那是**全局累计值** → 报告里 `prompt_tools`（21 个 handler）显示 **266 个 API**，
> 而 266 其实是**前面所有插件加起来的数**。报告虚高且**无法定位**。
> 修好后 190 + 70 + 6 = **266** 自洽 —— 这同时也反证了修复正确。

---

## 4. 目录结构

```
host/
├── astrbot_host/
│   ├── shadow.py                  # 影子包安装（拒绝覆盖外来 astrbot）
│   ├── registry.py                # EventType(16) / StarHandlerRegistry / llm_tools
│   ├── loader/plugin_loader.py    # PluginLoader / PluginRecord / LoadReport
│   └── compat/                    # 兼容层（57 个 .py）
│       ├── core/{star,platform,provider,message,config,db,agent,utils}/
│       └── api/{web,all,event,provider,star,message_components}
├── examples/hello_plugin/         # 最小可跑插件（5 个装饰器，验全链路）
├── tests/
│   ├── test_shadow_and_loader.py      # 影子包 + 装饰器 + 事件语义
│   ├── test_loader_real_plugin.py     # 真陪伴主插件
│   ├── test_loader_all_plugins.py     # ⭐ 全部插件（多插件才暴露 API 缺口）
│   ├── tools_verify_compat.py         # 差分验证①（92 项）
│   └── tools_verify_behavior.py       # 差分验证②（46 项）
├── docs/                          # 真 AstrBot 契约清单（含机器可读 JSON）
└── data/, cache/                  # 插件运行期数据（plugin_data / __pycache__）
```

---

## 5. 验收：为什么"差分验证"才算数

⚠️ **只跑一个插件会漏掉大量 API 缺口。** 实测：另外 4 个插件曾因
**三种不同的**缺口加载失败（`register_star` 签名、缺 `astrbot.api.all`、
`command_group` 返回值），而只测陪伴主插件时**全绿**。
→ 所以兼容层的验收判据是「**多插件同时加载**」。

**差分验证**（`tools_verify_*`）把兼容层与**真 AstrBot 4.27.5** 放进同一进程，
喂同一份输入、比同一份输出：

* **`tools_verify_compat.py` —— 92/92 ✅**
  枚举成员名/值/位序逐字相等 · `MessageSession.umo` 往返（含 session_id 带冒号）·
  路径函数同根目录结果相等 · `request_retry` 常量与重试曲线 ·
  `AstrBotConfig` 同一配置下最终 dict、BOM 写盘、点号访问、`_conf_schema.json` 默认值产出
* **`tools_verify_behavior.py` —— 46/46 ✅**
  `CommandGroupFilter` / `PermissionTypeFilter` / `PlatformAdapterTypeFilter`
  的 `filter()` 行为（喂假 event 比输出）· 错误类型与**文案逐字**相同 ·
  `print_cmd_tree` 输出逐字相同

> 这些工具**只在验证时**依赖真 AstrBot（它在 `uv tools` 里）。
> **生产宿主不依赖它** —— 靠 `sys.path` 手工把两个实现 import 成不同名字，
> 避免 `sys.modules` 冲突。

---

## 6. 已知边界（如实记录，不粉饰）

> 🎯 **下一步做什么、按什么顺序、凭什么** → **`../docs/NEXT_STEPS.md`**
> （含 203 个 handler 的类型分布、`AstrMessageEvent` 的实现清单、
> 以及"插件配置存不下来"那个真 bug 的完整根因）。

* **已合体（2026-09-20 更新）**：266 个 Web API 已挂进主服务
  （`/api/plugins/extensions/<path>`），真 HTTP 打真插件已验证（见 §8）；
  **插件拓展页**也已能打开（3 个，见 §7）。
  ⚠️ 但**事件派发仍未实现**：插件注册的消息 handler（`@filter.command` 等）
  **还没有入口**——插件面板可用，聊天的指令/钩子暂时不会被触发。
* **`initialize()` 已对全部真插件跑过**（2026-09-20）：10 个成功，
  `media_parser` 超时（它要联网下载 26MB 字体，本机下载不通）——见 §8。
* 第三方依赖靠环境装（`aiohttp` / `Pillow` / `pyyaml` / `qrcode` / `boto3` 已装）；
  `meme_manager` 仍缺 `requests`。
* **同名插件会冲突**：我们直接用目录名作包名（真实现用 `data.plugins.<dir>` 重写）。
  自用场景可接受，真冲突会**显式报错**（`_check_name_conflict`）。
* 真实现的 45~54 个**私有 API** 还没逐个补齐 —— 目前只覆盖到"能加载 + 已用到的面"。
* 插件页的**主题/locale 注入是真实现的一个子集**：真 AstrBot 会按 dashboard 主题
  改写 `<html data-theme>` 并注入完整 i18n context；我们只注入 `color-scheme`。
  插件自己的主题脚本（`localStorage.pc_theme`）照常工作，所以观感一致。

---

## 7. 插件**拓展页**（2026-09-20 新增）

情绪报的「**插件的配置入口没有喵**」——根因不是插件没写页面，而是
**宿主只挂了插件的数据 API、没挂页面**。`metadata.yaml` 里声明 `pages:` 的
插件（共 3 个）在面板里完全没有入口。

### 三条路由（缺一不可）

| 路由 | 作用 |
|---|---|
| `GET /api/v1/plugins/pages` | 列出所有可打开的拓展页（面板渲染入口用） |
| `GET /api/v1/plugins/<id>/pages/<name>/` | 页面本体与静态资源（**必须以 `/` 结尾**） |
| `GET /<插件目录名>/<path>` | 🔴 **根路径兼容** —— 插件页 JS **硬编码**了它 |

**两条容易踩的硬约束**（都是实测出来的）：

1. **URL 必须以 `/` 结尾。** 插件页 HTML 用相对路径（`./app.css`、`./js/features/x.js`），
   浏览器按"当前 URL 的目录"解析：
   * `/pages/companion-panel` → 目录是 `/pages/` → `./app.css` 打到上一级 ❌
   * `/pages/companion-panel/` → 目录是页自己 → 正确 ✅
   所以无斜杠那条**必须 308 重定向**，否则插件页所有资源 404、**白屏**。

2. **插件页不接受 HTTP 直连取数，它要"桥"。** 实测 `companion-panel/app.js`：
   首选 `window.AstrBotPluginPage`（`.apiGet/.apiPost/.ready`）；没有桥就退到
   `isHttpApiMode()`，而那个模式要求 `?debug_http=1` 或 standalone 标记 ——
   两者都没有 → 抛「未检测到 AstrBot 官方插件 Page 桥接」→ **白屏**。
   → 我们提供**同源版垫片** `emotion_app/web/static/plugin_page_bridge.js`
   （真 AstrBot 用 iframe+postMessage 做跨源桥；我们同源，直接 fetch 就够），
   并在入口 HTML 的 `<head>` **最前面**注入它。

3. **`HTTP_API` 是根路径。** 实测 `companion-panel/app.js:1`：
   `const HTTP_API = "/astrbot_plugin_private_companion/page"` —— **不带任何前缀**。
   真 AstrBot 靠桥绕开它；我们同源直连，所以根路径上也得能派发
   （`plugin_pages_web.py` 里那条兜底路由）。

### 安全

`resolve_page_asset()` 是唯一的安全关键点：请求路径 `resolve()` 后必须
**仍在页面根目录之下**（同时挡住 `../` 与**符号链接逃逸**）。
判据：`test_page_asset_blocks_directory_traversal` + `..._symlink_escape`
（注入 bug 验证过：去掉校验后测试立刻失败，且真的会吐出 `outside.txt`）。

---

## 8. 三个实测事故与对策（2026-09-20，**改这个目录前务必读**）

> 三条都是**真跑出来**的，不是假想担忧。共同点：**症状都很安静**
> —— 一个让服务启动慢 9 倍，一个**静默改坏别人的生产配置**，
> 一个让用户「**改完前端看不见**」。
>
> ⚠️ 第 ③ 条**不在 `host/` 里**，但它和插件页强相关（插件页资源同样不能长缓存），
> 所以一并记在这儿。

### ③ 面板静态资源被缓存 12 小时 → 用户「改完看不见」

Quart 的 `send_from_directory` 默认 `max-age=12 小时`。后果：**改完前端、
用户手机上还是旧的**。情绪报的「移动端适配没有喵 / 导入没有喵」就是这个 ——
**代码其实早就生效了**，浏览器一直在用 12 小时前的缓存。

这类问题极难自查：服务端一切正常、`curl` 也对、单测全绿，
**只有真浏览器 + 真设备才看得到**。

对策：`/` 与 `/static/*` 一律 `Cache-Control: no-cache, must-revalidate`
（靠 ETag 走 304，开销可忽略）。
🔴 **另一个更隐蔽的坑**：`Quart(__name__)` 会**自动注册**一条
`/static/<path:filename>`，优先级**高于**我们自己加的同路径路由 ——
就算写了 no-cache 也会被它**静默盖掉**。必须 `Quart(__name__, static_folder=None)`。
判据：`test_panel_static_assets_are_not_cached`（注入 bug 验证过）。

### ① `initialize()` 会拖死服务启动 → 超时隔离

`plugins.auto_initialize` 默认 `True`，而它在**主服务启动路径**上
（`before_serving` → `host.start()`）。实测 `astrbot_plugin_media_parser.initialize()`
会调用 `ensure_default_fonts()` **联网下载 16MB + 17MB 字体**（它自己超时 180s），
而那个下载地址的重定向目标（`objects.githubusercontent.com`）本机**不通**：

| | 启动耗时 |
|---|---:|
| 修复前 | **181.6 秒**（撞满插件的 180s 下载超时） |
| 修复后（`initialize_timeout=20`） | **20.8 秒** |

**一个插件的网络抖动 = 整个聊天服务起不来。**

对策：`PluginHost(initialize_timeout=...)` 用 `asyncio.wait_for` 逐个隔离。
⚠️ 超时**不是跳过**：它会进 `report.init_failed` 并打 ERROR（本项目铁律）。
且"慢但没超时"（≥上限一半）也会打 WARNING —— 否则下次就超时了没人知道。

### ② 插件会回写**自己的源码目录** → 源码镜像（**最严重的一条**）

我们原以为"数据目录分开（`StarTools.get_data_dir` → 我们的 `data/plugins`）就安全"。
**错。** 实测 `astrbot_plugin_comfyui_pro.__init__` → `_auto_update_schema()`
写的是 **`PLUGIN_DIR / "_conf_schema.json"`**（插件**源码**目录），内容取自
`self.data_dir / "workflow"` 的文件名。于是：

```
我们的 data_dir（1 个 workflow）
   → 写进【两边共用】的源码目录 schema
   → 覆盖掉 AstrBot 自己那份（它写着 3 个 workflow）
```

**这不是"脏了一个文件"，是把别人正在跑的 bot 配置改坏了，而且完全静默。**

对策（两道防线，互补）：

| 防线 | 位置 | 作用 |
|---|---|---|
| **源码镜像**（治本） | `source_mirror.py` + `PluginLoader(mirror_root=...)` | 先把源码复制到我们的可写副本，**从副本加载** → 源目录只读，写入落在副本 |
| **写保护哨兵**（兜底） | `write_guard.py` | 启动前后比对源目录 mtime/size，**有改动就响亮报警**（排除 `__pycache__`） |

🔴 **镜像逻辑必须放在加载器里，不能只放在服务层**：实测 `pytest host/tests`
**直接用 `PluginLoader`**，绕过了服务层的镜像 → **每跑一次测试就改写一次生产配置**，
而 35 项测试全绿、毫无提示。已把 `mirror_root` 下沉到 `PluginLoader`，
`host/tests` 与 `tests/` 现在都走镜像。

**判据**（都能失败，且注入 bug 验证过）：
* `test_mirror_isolates_plugin_source_writes` —— 源目录**一字未改**，
  同时验证插件**确实写成功了**（否则"没改源"可能只是"根本没写"= 假通过）；
* `test_real_plugins_produce_no_production_writes` —— 真插件跑完整启动，
  源目录快照**前后完全一致**（该测试带**自愈**：万一回归，它会还原被改的文件）；
* `test_initialize_has_timeout_isolation` —— 一个"永不返回"的假插件必须
  被按时放弃，且**不许挂死测试**（外层还有 5s 总闸，把挂死变成点名失败）。

📌 **当时的对照事实**：修复前同一条流程会把
`/opt/astrbot/data/plugins/astrbot_plugin_comfyui_pro/_conf_schema.json`
的 `options`/`enum` 从 3 个 workflow 覆盖成我们这边的 1 个。

---

## 9. 铁律（改这个目录前必读）

1. **绝不静默**：未实现的能力 → 抛 `NotImplementedError`；过滤/丢弃 → **必须留日志**。
2. **兼容层不许自创名字**：私有属性、元组顺序、返回值形状都照真实现，
   否则"照真实现写的消费者"会在解包处炸。
3. **判据只有"验过"和"失败"两种，没有"跳过"**：跳过 = 假通过，比不写还糟。
4. **测试必须能失败**：新判据要**人为注入 bug 验证它真会挂**（本轮 3 个新判据都验过）。
5. **领域层不许 import `astrbot_host`**（`emotion_app/domain/` 是干净的另一半）。
6. 行号锚点只在 **AstrBot 4.27.5** 有效；跨版本必须重新抽契约
   （`docs/REAL_ASTRBOT_CONTRACT.md`）。
7. 🔴 **任何"直接用 `PluginLoader`"的代码都必须传 `mirror_root`**
   —— 否则它就是一个静默污染生产目录的入口（§8② 就是这么踩到的）。
8. 🔴 **测"外部目录是否被改动"的检查，别只比 mtime 就下结论**：
   我一度因为目录 mtime 变了就以为 `storage/` 被污染，实际只是 Python
   导入包时创建再清理 `__pycache__` 的副作用（内容一字未改）。
   **要比 (mtime, size) 并列出具体文件**，或直接比字节。
