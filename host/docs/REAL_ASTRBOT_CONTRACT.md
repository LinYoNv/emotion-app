# 真 AstrBot API 契约清单（从真实源码逐行读出）

> 用途：核对「情绪app 宿主兼容层」是否与真 AstrBot 等义。
> 本文**不做任何推测性补全**——读不出来的地方明确写「未确认」。
> 机器可读版：`real_astrbot_contract.json`（同目录）。

## 0. 契约来源与读取口径

| 项 | 值 | 依据 |
|---|---|---|
| 源码位置（只读参考） | `/root/.local/share/uv/tools/astrbot/lib/python3.12/site-packages/astrbot/` | 磁盘实存 |
| 版本 | **4.27.5** | `astrbot/__init__.py:3`（`__version__ = "4.27.5"`） |
| 包元数据 | `AstrBot 4.27.5`，License `AGPL-3.0-or-later` | `astrbot-4.27.5.dist-info/METADATA`（Name/Version/License） |
| 本文行号口径 | 1-based，文件内行号 | — |
| 抽取方法 | ①Python `ast` 静态解析（保序保行号）②在真实 site-packages 上 import 做运行时探针 | — |

⚠️ **本文所有行号都是 4.27.5 这个具体版本的**。AstrBot 迭代很快，跨版本核对务必重新抽。

---

## A. `Star` 基类

**文件**：`core/star/base.py`，类定义在 **L19–L114**（依据：`core/star/base.py:19`）

### A.1 继承与类属性要求

```
class Star(CommandParserMixin, PluginKVStoreMixin):     # base.py:19
```
- 继承自 `CommandParserMixin`（依据：`core/star/base.py:8` 的 import）与 `PluginKVStoreMixin`（依据：`core/star/base.py:9`）。
- 类注解共 4 个，**均无默认值**（依据：`core/star/base.py:22-25`）：

| 属性 | 注解 | 行号 | 说明 |
|---|---|---|---|
| `author` | `str` | 22 | 由加载器注入 |
| `name` | `str` | 23 | 由加载器注入 |
| `context` | `Context` | 24 | 实际在 `__init__` 里赋为实例属性 |
| `logger` | `logging.Logger` | 25 | 插件专属 logger，与全局 `astrbot` logger 隔离 |

- `name`/`author` 不是插件自己写的：加载器在**实例化前**就 `setattr(metadata.star_cls_type, "name"/"author"/"plugin_id", ...)`，实例化后再往实例上 `setattr` 一次（依据：`core/star/star_manager.py:1212-1214` 与 `1234-1236`）。
- 类属性 `name`/`author` 的具体值来自 `plugin_id.split("/")` 的 `p_name`/`p_author`（依据：`core/star/star_manager.py:1212`）。

### A.2 完整方法列表

全部方法（依据：`core/star/base.py` 逐行）：

| 方法 | 签名 | 行号 | async |
|---|---|---|---|
| `__init__` | `def __init__(self, context: Context, config: dict \| None = None) -> None` | 28–52 | 否 |
| `_get_context_config` | `def _get_context_config(self) -> Any` | 54–62 | 否 |
| `__init_subclass__` | `def __init_subclass__(cls, **kwargs)` | 64–75 | 否 |
| `text_to_image` | `async def text_to_image(self, text: str, return_url = True) -> str` | 77–90 | 是 |
| `html_render` | `async def html_render(self, tmpl: str, data: dict, return_url = True, options: dict \| None = None) -> str` | 92–105 | 是 |
| `initialize` | `async def initialize(self) -> None` | 107–108 | 是 |
| `terminate` | `async def terminate(self) -> None` | 110–111 | 是 |
| `__del__` | `def __del__(self) -> None` | 113–114 | 否 |

- `initialize` / `terminate` / `__del__` 三者都是**空实现**（函数体只有 docstring）（依据：`core/star/base.py:107-114`）。
- `_get_context_config` 是私有方法：优先调 `context.get_config()`，异常返回 `None`；否则回退 `getattr(context, "_config", None)`（依据：`core/star/base.py:54-62`）。

### A.3 🔴 `__init__` 的行为（含一个反直觉点）

依据：`core/star/base.py:28-52`。

1. `self.context = context`（L29）。
2. 从 `star_map.get(self.__class__.__module__)` 取 `metadata.name` 作为 `plugin_name`；取不到则回退 `getattr(self, "name", None)`（L35-38）。
3. `self.logger = LogManager.get_plugin_logger(plugin_name)`；`plugin_name` 为空则退回全局 `logging.getLogger("astrbot")`（L40-44）。
4. 捕获 `AttributeError` —— 插件把 `logger` 定义成只读 property 时保留它自己的（L50-52）。

> 🔴 **反直觉 1：`config` 形参被完全丢弃。**
> 基类 `__init__` 的函数体里**从不使用 `config`**，`Star` 也**没有 `config` 属性**（依据：`core/star/base.py:28-52`，全文件无 `self.config`）。
> 插件要用配置必须自己 `self.config = config`，或走 `self.context.get_config()`。
> 兼容层若"帮忙"存了 `self.config`，那是**加行为**，不是等义。

### A.4 🔴 `__init_subclass__` 的副作用（重点）

依据：`core/star/base.py:64-75`。

```python
def __init_subclass__(cls, **kwargs):          # L64
    super().__init_subclass__(**kwargs)        # L65
    if not star_map.get(cls.__module__):       # L66  ← 以【模块路径】为 key 判重
        metadata = StarMetadata(               # L67
            star_cls_type=cls,                 # L68
            module_path=cls.__module__,        # L69
        )
        star_map[cls.__module__] = metadata    # L71
        star_registry.append(metadata)         # L72
    else:                                      # L73
        star_map[cls.__module__].star_cls_type = cls   # L74
        star_map[cls.__module__].module_path = cls.__module__  # L75
```

- **触发条件**：任何 `Star` 子类**被定义**时自动执行 —— 不需要任何装饰器（依据：`core/star/base.py:64`；另见 `core/star/register/star.py:17-19` 明确说 v3.5.19 之后不需要 `@register_star`）。
- **注册了什么**：只注册两个字段 —— `star_cls_type` 与 `module_path`（依据：`core/star/base.py:67-70`）。`name`/`author`/`version` 等**不在此时填**，是加载期由 `star_manager` 填的。
- **注册到哪**：
  - `star_map: dict[str, StarMetadata]`，key = 模块路径 `__module__`（依据：`core/star/star.py:10-11`）
  - `star_registry: list[StarMetadata]`，顺序为子类定义顺序（依据：`core/star/star.py:9`）

> 🔴 **反直觉 2：一个模块只能生效一个 Star 子类。**
> 判重 key 是 `cls.__module__`，同一模块内定义第二个 `Star` 子类时**不会新建 metadata、也不会 append `star_registry`**，只覆盖已存在 metadata 的 `star_cls_type`（依据：`core/star/base.py:66-75`）。
> 后果：`star_registry` 长度不变，先注册的类被"顶掉"。

### A.5 生命周期：宿主侧怎么调

| 钩子 | 宿主调用点 | 判据 | 语义 |
|---|---|---|---|
| 实例化 | `core/star/star_manager.py:1221-1237` | — | 有 `plugin_config` 时试 `star_cls_type(context=..., config=...)`（L1222-1226），捕获 `TypeError` 后回退 `star_cls_type(context=...)`（L1227-1229）；无 config 时直接 `star_cls_type(context=...)`（L1230-1232） |
| `initialize` | `core/star/star_manager.py:1417-1418` | **`hasattr`** | `if hasattr(metadata.star_cls, "initialize") and metadata.star_cls: await ...initialize()` |
| `terminate` / `__del__` | `core/star/star_manager.py:2022-2041` | **`__dict__`** | 见下 |

> 🔴 **反直觉 3（兼容层最容易踩的坑）：`terminate` 与 `__del__` 是 `elif` 关系，且存在性判断用 `__dict__`。**
>
> 依据：`core/star/star_manager.py:2022-2041`
> - 先判 `if "__del__" in star_metadata.star_cls_type.__dict__:`（L2022）→ 在线程池 `run_in_executor` 里跑 `__del__`（L2024-2026）；
> - **`elif`** `"terminate" in star_metadata.star_cls_type.__dict__:`（L2040）→ `await terminate()`（L2041）。
>
> 两个直接后果：
> 1. 用 `__dict__` 判断 ⇒ **必须是插件类自己命名空间里定义的**。继承自 `Star` 的空 `terminate` 不在 `__dict__` 里 ⇒ **不会被调用**。
> 2. `elif` ⇒ 插件类若同时定义了 `__del__`，**`terminate` 永远不会被调用**。

> ⚠️ **反直觉 4：同一对钩子用了两种存在性判断。**
> `initialize` 用 `hasattr`（会命中原类继承来的空实现，依据：`core/star/star_manager.py:1417`），`terminate` 用 `__dict__`（不继承，依据：`:2040`）。行为不一致。

> ⚠️ **反直觉 5：实例化只捕获 `TypeError`。**
> 依据：`core/star/star_manager.py:1221-1229`。若插件 `__init__` 内部因别的原因抛 `TypeError`，会被误判为"签名不兼容"而**重试一次**无 config 的构造，可能掩盖真实错误。

### A.6 `StarMetadata`（`core/star/star.py:18-88`）

`@dataclass`（依据：`core/star/star.py:17`），**21 个字段**（依据：逐字段行号如下）：

| 字段 | 注解 | 默认值 | 行号 |
|---|---|---|---|
| `name` | `str \| None` | `None` | 24 |
| `author` | `str \| None` | `None` | 26 |
| `desc` | `str \| None` | `None` | 28 |
| `short_desc` | `str \| None` | `None` | 30 |
| `version` | `str \| None` | `None` | 32 |
| `repo` | `str \| None` | `None` | 34 |
| `star_cls_type` | `type[Star] \| None` | `None` | 37 |
| `module_path` | `str \| None` | `None` | 39 |
| `star_cls` | `Star \| None` | `None` | 42 |
| `module` | `ModuleType \| None` | `None` | 44 |
| `root_dir_name` | `str \| None` | `None` | 46 |
| `reserved` | `bool` | `False` | 48 |
| `activated` | `bool` | `True` | 51 |
| `config` | `AstrBotConfig \| None` | `None` | 54 |
| `star_handler_full_names` | `list[str]` | `field(default_factory=list)` | 57 |
| `display_name` | `str \| None` | `None` | 60 |
| `logo_path` | `str \| None` | `None` | 63 |
| `support_platforms` | `list[str]` | `field(default_factory=list)` | 66 |
| `astrbot_version` | `str \| None` | `None` | 69 |
| `i18n` | `dict[str, dict]` | `field(default_factory=dict)` | 72 |
| `pages` | `list[dict]` | `field(default_factory=list)` | 75 |

- `plugin_id` 是 **property**：`f"{author.lower().replace('/','_')}/{name.lower().replace('/','_')}"`，缺省用 `"unknown"`（依据：`core/star/star.py:78-82`）。
- `__str__` / `__repr__` 都是 `f"Plugin {name} ({version}) by {author}: {desc}"`（依据：`core/star/star.py:84-88`）。

---

## B. `StarHandlerRegistry` / `EventType` / `StarHandlerMetadata`

**文件**：`core/star/star_handler.py`

### B.1 `EventType` 全部成员（逐字、按定义顺序）

**16 个成员**，类定义 L219–L241（依据：`core/star/star_handler.py:219-241`）：

| # | 成员名 | 行号 | 源码注释 |
|---|---|---|---|
| 1 | `OnAstrBotLoadedEvent` | 225 | AstrBot 加载完成 |
| 2 | `OnPlatformLoadedEvent` | 226 | 平台加载完成 |
| 3 | `AdapterMessageEvent` | 228 | 收到适配器发来的消息 |
| 4 | `OnWaitingLLMRequestEvent` | 229 | 等待调用 LLM（在获取锁之前，仅通知） |
| 5 | `OnLLMRequestEvent` | 230 | 收到 LLM 请求（可以是用户也可以是插件） |
| 6 | `OnLLMResponseEvent` | 231 | LLM 响应后 |
| 7 | `OnAgentBeginEvent` | 232 | Agent 开始运行 |
| 8 | `OnAgentDoneEvent` | 233 | Agent 运行完成 |
| 9 | `OnDecoratingResultEvent` | 234 | 发送消息前 |
| 10 | `OnCallingFuncToolEvent` | 235 | 调用函数工具 |
| 11 | `OnUsingLLMToolEvent` | 236 | 使用 LLM 工具 |
| 12 | `OnLLMToolRespondEvent` | 237 | 调用函数工具后 |
| 13 | `OnAfterMessageSentEvent` | 238 | 发送消息后 |
| 14 | `OnPluginErrorEvent` | 239 | 插件处理消息异常时 |
| 15 | `OnPluginLoadedEvent` | 240 | 插件加载完成 |
| 16 | `OnPluginUnloadedEvent` | 241 | 插件卸载完成 |

- 全部是 `enum.auto()`，`enum.Enum`（不是 Flag），值即 1..16（依据：`core/star/star_handler.py:219-241`）。
- **运行时实测**：`len(list(EventType)) == 16`，顺序与上表完全一致（本次 import 实测）。
- L227 与 L242 是空行（分组用），无成员被跳过。

### B.2 `StarHandlerMetadata`（L248–L282，`@dataclass` 在 L247）

**9 个字段**（依据：`core/star/star_handler.py:248-275`）：

| 字段 | 注解 | 默认值 | 行号 |
|---|---|---|---|
| `event_type` | `EventType` | **无默认（必填）** | 251 |
| `handler_full_name` | `str` | **无默认（必填）** | 254 |
| `handler_name` | `str` | **无默认（必填）** | 257 |
| `handler_module_path` | `str` | **无默认（必填）** | 260 |
| `handler` | `H`（`TypeVar bound=Callable[..., Any]`） | **无默认（必填）** | 263 |
| `event_filters` | `list[HandlerFilter]` | **无默认（必填）** | 266 |
| `desc` | `str` | `""` | 269 |
| `extras_configs` | `dict` | `field(default_factory=dict)` | 272 |
| `enabled` | `bool` | `True` | 275 |

- `handler_full_name` 格式为 `f"{handler.__module__}_{handler.__name__}"`（依据：`core/star/star_handler.py:254-255` 及生成函数 `core/star/register/star_handler.py:31-35`）。
- 泛型 `Generic[H]`，`H = TypeVar("H", bound=Callable[..., Any])`（依据：`core/star/star_handler.py:244,248`）。

### B.3 `StarHandlerRegistry` 全部方法（L14–L213）

| 方法 | 签名 | 行号 |
|---|---|---|
| `__init__` | `def __init__(self) -> None` | 15 |
| `append` | `def append(self, handler: StarHandlerMetadata) -> None` | 19 |
| `_print_handlers` | `def _print_handlers(self) -> None`（私有） | 28 |
| `get_handlers_by_event_type` | `def get_handlers_by_event_type(self, event_type: EventType, only_activated=True, plugins_name: list[str] \| None = None) -> list[StarHandlerMetadata]` | **150**（另有 14 个 `@overload` 在 L33/41/49/59/67/75/83/91/99/109/117/125/133/141） |
| `get_handler_by_full_name` | `def get_handler_by_full_name(self, full_name: str) -> StarHandlerMetadata \| None` | 188 |
| `get_handlers_by_module_name` | `def get_handlers_by_module_name(self, module_name: str) -> list[StarHandlerMetadata]` | 191 |
| `clear` | `def clear(self) -> None` | 201 |
| `remove` | `def remove(self, handler: StarHandlerMetadata) -> None` | 205–207 |
| `__iter__` | `def __iter__(self)` → `iter(self._handlers)` | 209 |
| `__len__` | `def __len__(self) -> int` | 212 |

- 状态字段：`star_handlers_map: dict[str, StarHandlerMetadata]`（key = `handler_full_name`，L16）、`_handlers: list[StarHandlerMetadata]`（L17，**唯一被迭代的顺序来源**）。
- 模块级单例：`star_handlers_registry = StarHandlerRegistry()`（依据：`core/star/star_handler.py:216`）。
- `get_handlers_by_event_type` 的过滤顺序（依据：`core/star/star_handler.py:156-186`）：
  1. `handler.event_type != event_type` → 跳过（L159-160）
  2. `not handler.enabled` → 跳过（L161-162）
  3. `only_activated` 且 `star_map[handler.handler_module_path].activated` 为假 → 跳过（L164-167）
  4. `plugins_name` 非 `None` 且 ≠ `["*"]` 时：`plugin.name` 不在白名单、且事件类型不属 4 个加载事件、且 `not plugin.reserved` → 跳过（L169-184）
- `remove` 用 `h != handler` 比较 —— 这是 dataclass 自动生成的 `__eq__`（**字段值全等**，不是身份比较）（依据：`core/star/star_handler.py:207`）。

### B.4 🔴🔴 排序语义（本节是重点，已读代码 + 运行时实测）

**问题：`append()` 用的排序键是什么方向？和 `__lt__` 的方向一致吗？**

**答案：不一致，方向完全相反。**

**`append()` 实现（依据：`core/star/star_handler.py:19-26`）：**

```python
def append(self, handler: StarHandlerMetadata) -> None:      # L19
    if "priority" not in handler.extras_configs:             # L21
        handler.extras_configs["priority"] = 0               # L22
    self.star_handlers_map[handler.handler_full_name] = handler  # L24
    self._handlers.append(handler)                           # L25
    self._handlers.sort(key=lambda h: -h.extras_configs["priority"])  # L26  ★★
```

→ 排序键是 **`-priority`**，即 **降序（DESCENDING）**：`priority` **越大越靠前**，迭代时**越先执行**。

**`__lt__` 实现（依据：`core/star/star_handler.py:277-282`）：**

```python
def __lt__(self, other: StarHandlerMetadata):                # L277
    """定义小于运算符以支持优先队列"""
    return self.extras_configs.get("priority", 0) < other.extras_configs.get("priority", 0)  # L279-281
```

→ **升序（ASCENDING）**：`priority` **越小越"小"**。

| | 方向 | 依据 |
|---|---|---|
| `append()` 的 `_handlers` 顺序 | **降序**（大 priority 在前） | `core/star/star_handler.py:26` |
| `__lt__` 定义的顺序 | **升序**（小 priority 在前） | `core/star/star_handler.py:277-282` |
| **一致吗** | ❌ **完全相反** | — |

**运行时实测（本次在真实 site-packages 上 import 后跑）：**

- 依次 `append`：`A(1) B(5) C(3) D(5) E(0) F(-2)`
- `_handlers` 结果：`[B(5), D(5), C(3), A(1), E(0), F(-2)]` ← **降序**
- `sorted(用 __lt__ 升序)`：`[F, E, A, C, B, D]` ← **升序**
- 结论：`_handlers` 顺序 == `sorted(reverse=True)`，与 `__lt__` 的升序**相反**。

**其他要点：**

- **默认优先级 = 0**，由 `append` 在缺失时补（依据：`core/star/star_handler.py:21-22`）。
- **同优先级保序**：Python `list.sort` 是稳定的，同 priority 的 handler 保持 `append` 的先后（即注册/导入顺序）（依据：`:26` 的 `sort` 语义 + Python 标准库保证）。
- **消费侧**：`get_handlers_by_event_type()` 与 `__iter__` 都**直接遍历 `_handlers`**（依据：`:157` 与 `:210`），所以**运行时真正生效的是降序**。
- **`priority` 从哪来**：装饰器的 `**kwargs` 被原样塞进 `md.extras_configs`（依据：`core/star/register/star_handler.py:68`），所以 `@filter.command(priority=10)` 的 `priority` 走这条路径。

> 🔴 **反直觉 6：`__lt__` 是死代码。**
> 它的 docstring 写"定义小于运算符以支持优先队列"（依据：`core/star/star_handler.py:278`），但**全库 `grep -rn "heapq\|heappush\|heappop"` 零命中** —— 没有任何调用点使用 `__lt__`。
> 它当前**不生效**；真正决定顺序的只有 `append` 里那行 `sort(key=lambda h: -priority)`。

> ⚠️ **反直觉 7：`extras_configs["priority"]` 必须可取负。**
> `append` 的 sort key 是 `-h.extras_configs["priority"]`（依据：`:26`）。若插件塞了非数字的 `priority`，`append` 时会直接抛 `TypeError`。

### B.5 重复注册语义

`get_handler_or_create` 先按 `handler_full_name` 查重，**命中就直接返回已存在的 md**（依据：`core/star/register/star_handler.py:50-52`），即同名 handler **不会重复 append**。
同一个函数叠加多个装饰器时，它们共享同一个 `md`，只是往 `md.event_filters` 里追加（依据：`core/star/register/star_handler.py:123, 201, 243, 271 ...`）。

---

## C. 全部 `@filter.*` 装饰器

**定义文件**：`core/star/register/star_handler.py`
**再导出文件**：`api/event/filter/__init__.py`（L11–L40 逐个 `as` 别名；`__all__` 在 L42–L72）
**插件惯用写法**：`from astrbot.api.event import filter` → `@filter.xxx(...)`

### C.1 完整清单

`api/event/filter/__init__.py` 的 `__all__` 共 **29 个名字**（依据：`api/event/filter/__init__.py:42-72`），已 `import` 实测确认长度 = 29：

```
CustomFilter, EventMessageType, EventMessageTypeFilter,
PermissionType, PermissionTypeFilter, PlatformAdapterType, PlatformAdapterTypeFilter,
after_message_sent, command, command_group, custom_filter, event_message_type,
llm_tool, on_agent_begin, on_agent_done, on_astrbot_loaded, on_decorating_result,
on_llm_request, on_llm_response, on_plugin_error, on_plugin_loaded, on_plugin_unloaded,
on_platform_loaded, on_waiting_llm_request, permission_type, platform_adapter_type,
regex, on_using_llm_tool, on_llm_tool_respond
```

其中 **22 个是装饰器工厂**，**7 个是类/枚举**（`CustomFilter`、`EventMessageType`、`EventMessageTypeFilter`、`PermissionType`、`PermissionTypeFilter`、`PlatformAdapterType`、`PlatformAdapterTypeFilter`）。

### C.2 22 个装饰器的签名（参数名与默认值，逐字）

依据：全部来自 `core/star/register/star_handler.py` 的函数定义行；签名已用 `inspect.signature` 实测复核。

| 装饰器 | 签名 | 定义行 | 注册的 EventType |
|---|---|---|---|
| `command` | `register_command(command_name: str \| None = None, sub_command: str \| None = None, alias: set \| None = None, **kwargs)` | 75 | `AdapterMessageEvent` |
| `command_group` | `register_command_group(command_group_name: str \| None = None, sub_command: str \| None = None, alias: set \| None = None, **kwargs)` | 208 | `AdapterMessageEvent` |
| `custom_filter` | `register_custom_filter(custom_type_filter, *args, **kwargs)` | 129 | `AdapterMessageEvent` |
| `event_message_type` | `register_event_message_type(event_message_type: EventMessageType, **kwargs)` | 262 | `AdapterMessageEvent` |
| `platform_adapter_type` | `register_platform_adapter_type(platform_adapter_type: PlatformAdapterType, **kwargs)` | 277 | `AdapterMessageEvent` |
| `regex` | `register_regex(regex: str \| re.Pattern, **kwargs)` | 297 | `AdapterMessageEvent` |
| `permission_type` | `register_permission_type(permission_type: PermissionType, raise_error: bool = True, **kwargs)` | 312 | `AdapterMessageEvent` |
| `llm_tool` | `register_llm_tool(name: str \| None = None, **kwargs)` | 586 | `OnCallingFuncToolEvent` |
| `on_astrbot_loaded` | `register_on_astrbot_loaded(**kwargs)` | 337 | `OnAstrBotLoadedEvent` |
| `on_platform_loaded` | `register_on_platform_loaded(**kwargs)` | 347 | `OnPlatformLoadedEvent` |
| `on_plugin_error` | `register_on_plugin_error(**kwargs)` | 357 | `OnPluginErrorEvent` |
| `on_plugin_loaded` | `register_on_plugin_loaded(**kwargs)` | 375 | `OnPluginLoadedEvent` |
| `on_plugin_unloaded` | `register_on_plugin_unloaded(**kwargs)` | 392 | `OnPluginUnloadedEvent` |
| `on_waiting_llm_request` | `register_on_waiting_llm_request(**kwargs)` | 409 | `OnWaitingLLMRequestEvent` |
| `on_llm_request` | `register_on_llm_request(**kwargs)` | 433 | `OnLLMRequestEvent` |
| `on_llm_response` | `register_on_llm_response(**kwargs)` | 456 | `OnLLMResponseEvent` |
| `on_agent_begin` | `register_on_agent_begin(**kwargs)` | 479 | `OnAgentBeginEvent` |
| `on_agent_done` | `register_on_agent_done(**kwargs)` | 507 | `OnAgentDoneEvent` |
| `on_using_llm_tool` | `register_on_using_llm_tool(**kwargs)` | 537 | `OnUsingLLMToolEvent` |
| `on_llm_tool_respond` | `register_on_llm_tool_respond(**kwargs)` | 561 | `OnLLMToolRespondEvent` |
| `on_decorating_result` | `register_on_decorating_result(**kwargs)` | 730 | `OnDecoratingResultEvent` |
| `after_message_sent` | `register_after_message_sent(**kwargs)` | 744 | `OnAfterMessageSentEvent` |

各装饰器宿主侧回调参数（依据：各自 docstring）：

| 装饰器 | 回调参数 | 依据行 |
|---|---|---|
| `on_llm_request` | `event, req`（`ProviderRequest`） | 445 |
| `on_llm_response` | `event, response`（`LLMResponse`） | 468 |
| `on_agent_begin` | `event, run_context` | 496 |
| `on_agent_done` | `event, run_context, response` | 526 |
| `on_using_llm_tool` | `event, tool, tool_args` | 550 |
| `on_llm_tool_respond` | `event, tool, tool_args, tool_result` | 575 |
| `on_plugin_error` | `event, plugin_name, handler_name, error, traceback_text` | 361 |
| `on_plugin_loaded` / `on_plugin_unloaded` | `metadata` | 379 / 396 |
| `on_waiting_llm_request` | `event` | 418 |

### C.3 不在 `filter` 命名空间、但同属 `core.star.register` 的两个

依据：`core/star/register/__init__.py:2-26`（`__all__` 在 L28-53）。

| 名字 | 签名 | 定义行 | 导出路径 |
|---|---|---|---|
| `register_agent` | `register_agent(name: str, instruction: str, tools: list[str \| FunctionTool] \| None = None, run_hooks: BaseAgentRunHooks[Any] \| None = None)` | 697 | `astrbot.api.agent`（依据：`api/__init__.py:8`） |
| `register_star` | `register_star(name: str, author: str, desc: str, version: str, repo: str \| None = None)` | `core/star/register/star.py:8` | `astrbot.api.star.register`（依据：`api/star/__init__.py:4`） |

- `register_star` **已废弃**：首次调用会发 `DeprecationWarning`（依据：`core/star/register/star.py:5,38-45`），docstring 说明 v3.5.19 之后继承 `Star` 即自动识别（依据：`:17-19`）。
- `register_agent` 的行为：构造 `Agent` + `HandoffTool`，把 `handoff_tool` 追加进 `llm_tools.func_list`，返回 `RegisteringAgent`（该对象上有 `.llm_tool` 方法，会注入 `registering_agent` kwarg）（依据：`core/star/register/star_handler.py:697-727, 686-694`）。

### C.4 装饰器公共机制

- **`get_handler_or_create`**（依据：`core/star/register/star_handler.py:38-72`）：
  - 按 `full_name` 查重（L49-52）；
  - `desc` 先取 `handler.__doc__.strip()`（L63-64），若 kwargs 里有 `desc` 则覆盖（L65-67）；
  - **其余 kwargs 全部塞进 `md.extras_configs = kwargs`**（L68）← 这就是 `priority` 等元数据的来源；
  - `dont_add=False` 时 `append` 进注册表（L70-71）。

- **`filter` 之间是 AND 关系**，`PermissionTypeFilter` 特殊处理（依据：`core/pipeline/waking_check/stage.py:185-193`）。

> ⚠️ **反直觉 8：没有 `event_filters` 的 `AdapterMessageEvent` handler 会被静默跳过。**
> 依据：`core/pipeline/waking_check/stage.py:182-183` —— `if len(handler.event_filters) == 0: continue`。

> ⚠️ **反直觉 9：`regex` 不受唤醒前缀制约，`command` 受。**
> `command.py:30` 注释明写"标准指令受到 wake_prefix 的制约"，且 `filter()` 首行要求 `event.is_at_or_wake_command`（依据：`core/star/filter/command.py:30, 192`）；
> `regex.py:9` 注释明写"正则表达式过滤器不会受到 wake_prefix 的制约"，`filter()` 直接 `search`（依据：`core/star/filter/regex.py:9, 17-18`）。

### C.5 命令参数解析（`CommandFilter`）

依据：`core/star/filter/command.py`。

- `init_handler_md`：用 `inspect.signature(handler, eval_str=True)`，**跳过前两个参数**（`self` 与 `event`），其余参数名 → 注解，或（有默认值时）默认值（依据：`:66-79`）。
- `filter()`：要求 `event.is_at_or_wake_command`（`:192`）→ 自定义 filter 全过（`:195`）→ 消息 strip 后按指令全名匹配（`:199-206`）→ 剩余文本按空格切分并去空串（`:209-211`）→ `validate_and_convert_params`（`:214`）→ **结果写入 `event.set_extra("parsed_params", params)`**（`:218`）。
- `GreedyStr`：必须是最后一个参数，否则 `ValueError`；命中时把剩余全部参数用空格 `join`（依据：`:15-16, 102-114`）。
- 类型转换（依据：`:129-172`）：`None` → 数字串转 `int`；`str` 默认值 → 原样；`bool` → `true/yes/1` 与 `false/no/0`；`int` → `int()`；`float` → `float()`；联合类型 → 取唯一非 `None` 类型构造；失败 `ValueError(f"参数 {param_name} 类型错误。完整参数: ...")`。
- 指令名匹配：`get_complete_command_names()` 把 `parent_command_names` 与命令名/别名做笛卡尔积（`"父 子"`）（依据：`:175-189`）。

### C.6 🔴 `register_llm_tool` 深度解析（特别重要）

**定义**：`core/star/register/star_handler.py:586-683`。

#### C.6.1 docstring 怎么解析

依据：`core/star/register/star_handler.py:631-665`。

```
llm_tool_name = name_ if name_ else awaitable.__name__     # L631  未传 name 就用函数名
func_doc = awaitable.__doc__ or ""                          # L632
docstring = docstring_parser.parse(func_doc)                # L633  第三方库 docstring_parser
args = []
for arg in docstring.params:                                # L635  只遍历 Args: 段的参数
```

逐参数处理（依据：`:636-665`）：

1. `type_name = arg.type_name`（L637）
2. **类型为空 → 抛错**（L638-641）：
   `ValueError(f"LLM 函数工具 {awaitable.__module__}_{llm_tool_name} 的参数 {arg.arg_name} 缺少类型注释。")`
3. **泛型拆分**（L643-646）：`re.match(r"(\w+)\[(\w+)\]", type_name)` → `type_name=match.group(1)`、`sub_type_name=match.group(2)`（即把 `list[string]` 拆成 `list` + `string`）
4. **Python 类型名映射**（L647-649）：两者分别过 `PY_TO_JSON_TYPE.get(x, x)`
5. **白名单校验 → 抛错**（L650-655）：
   若 `type_name not in SUPPORTED_TYPES`，或 `sub_type_name` 存在且不在 `SUPPORTED_TYPES`：
   `ValueError(f"LLM 函数工具 {awaitable.__module__}_{llm_tool_name} 不支持的参数类型：{arg.type_name}")`
6. **组 schema**（L657-661）：`{"type": type_name, "name": arg.arg_name, "description": arg.description}`
7. **仅当 `type_name == "array"` 时补 `items`**（L662-664）：`arg_json_schema["items"] = {"type": sub_type_name}`

#### C.6.2 支持哪些类型

**两个白名单**，都在 `core/provider/func_tool_manager.py`：

- `SUPPORTED_TYPES = ["string", "number", "object", "array", "boolean"]`（依据：`core/provider/func_tool_manager.py:128-134`）
- `PY_TO_JSON_TYPE = {"int":"number", "float":"number", "bool":"boolean", "str":"string", "dict":"object", "list":"array", "tuple":"array", "set":"array"}`（依据：`core/provider/func_tool_manager.py:136-145`）

所以 docstring 里可用的类型名（**实测确认**）：

| docstring 写法 | 生成的 JSON schema type | 备注 |
|---|---|---|
| `string` | `string` | |
| `number` | `number` | |
| `boolean` | `boolean` | |
| `object` | `object` | |
| `array` | `array` | |
| `str` / `int` / `float` / `bool` / `dict` / `list` / `tuple` / `set` | 经 `PY_TO_JSON_TYPE` 映射 | 与上表等价 |
| `list[str]` / `list[string]` | `array` + `items:{type:string}` | 泛型拆分生效 |
| `List[str]` | 未实测（正则 `(\w+)\[(\w+)\]` 能匹配 `List[str]`，理论上可） | 未确认 |

#### C.6.3 解析失败会怎样（**实测**）

**结论：抛 `ValueError`，且发生在【装饰器执行时】= 插件 import / 加载阶段，不是调用工具时。**（依据：`core/star/register/star_handler.py:639-655` 位于装饰器体内；本次实测复现）

实测结果：

| 情形 | 结果 |
|---|---|
| docstring 类型 = `integer` | ❌ `ValueError: LLM 函数工具 <mod>_<name> 不支持的参数类型：integer` |
| docstring 类型 = `Any` | ❌ `ValueError: ... 不支持的参数类型：Any` |
| docstring 类型 = `Foo`（任意未知名） | ❌ `ValueError: ... 不支持的参数类型：Foo` |
| docstring 类型 = `list[Foo]` | ❌ `ValueError: ... 不支持的参数类型：list[Foo]`（外层 `list` 已映射为 `array`，但 `sub_type Foo` 不合法） |
| 参数**缺类型**（写成 `location: 地点`） | ❌ `ValueError: LLM 函数工具 <mod>_<name> 的参数 location 缺少类型注释。` |

> 🔴 **反直觉 10：白名单比 JSON Schema 窄。**
> JSON Schema 支持的 `integer` **会被 AstrBot 拒绝**（`integer` 既不在 `SUPPORTED_TYPES`，也不在 `PY_TO_JSON_TYPE`）。插件作者只能写 `number`。
> 依据：`core/provider/func_tool_manager.py:128-145` + `core/star/register/star_handler.py:647-655`；实测确认。

实测**不报错**的情形：

| 情形 | 结果 |
|---|---|
| **完全没有 docstring** | ✅ 注册出无参工具：`parameters={'type':'object','properties':{}}`，`desc=''`（`docstring.params` 为空，`docstring.description` 为 `None`） |
| **有 docstring 但没有 `Args:` 段** | ✅ 同上，无参工具 |
| **把 `event` 写进 `Args:`** | ✅ 不报错，且会把 `event` **也注册成工具参数** —— 源码**不排除前两个位置参数**（`docstring.params` 里有什么就注册什么） |

> 🔴 **反直觉 11：`register_llm_tool` 不会自动跳过 `event` 参数。**
> 实测 `q_eventparam` 的 `properties` 里出现了 `event`。插件作者须自己不要在 `Args:` 里写 `event`。

#### C.6.4 注册动作

依据：`core/star/register/star_handler.py:667-679`。

- **无 `registering_agent` 时**（常规插件路径，L667-670）：
  `doc_desc = docstring.description.strip()`（L668）→
  `md = get_handler_or_create(awaitable, EventType.OnCallingFuncToolEvent)`（L669）← **注意：这里不传 kwargs，所以 extras_configs 为空、priority 默认 0** →
  `llm_tools.add_func(llm_tool_name, args, doc_desc, md.handler)`（L670）
- **有 `registering_agent` 时**（由 `RegisteringAgent.llm_tool` 注入，L671-679）：`spec_to_func` 后 append 到 `agent.tools`，**不**注册进全局 `llm_tools`。

参数 schema 的最终组装（依据：`core/provider/func_tool_manager.py:343-363`）：

```python
params = {"type": "object", "properties": {}}     # L346-349
for param in func_args:
    p = copy.deepcopy(param); p.pop("name", None)  # L351-352
    params["properties"][param["name"]] = p        # L353
return FuncTool(name=name, parameters=params, description=desc, handler=handler)  # L354-359
```

- `add_func` 会先 `self.remove_func(name)`（依据：`core/provider/func_tool_manager.py:380`），**同名工具后者覆盖前者**（`:365-390`）。

#### C.6.5 返回值语义（源码 docstring 原文）

依据：`core/star/register/star_handler.py:604-616`。

- 返回 `str`：结果会被加入**下一次** LLM 请求的 prompt 中，用于让 LLM 总结工具返回的结果
- 返回 `None`：结果**不会**被加入下一次 LLM 请求的 prompt 中
- 可以使用 `yield` 发送消息、终止事件（`event.stop_event(); yield`）
- 参数类型说明原文：**"可接受的参数类型有：string, number, object, array, boolean。"**

### C.7 关联枚举

| 枚举 | 文件:行 | 类型 | 成员 |
|---|---|---|---|
| `EventMessageType` | `core/star/filter/event_message_type.py:10` | `enum.Flag` | `GROUP_MESSAGE=1`, `PRIVATE_MESSAGE=2`, `OTHER_MESSAGE=4`, `ALL=GROUP_MESSAGE\|PRIVATE_MESSAGE\|OTHER_MESSAGE`（=7） |
| `PermissionType` | `core/star/filter/permission.py:9` | `enum.Flag` | `ADMIN=1`, `MEMBER=2` |
| `PlatformAdapterType` | `core/star/filter/platform_adapter_type.py:9` | `enum.Flag` | **21 个**：`AIOCQHTTP, QQOFFICIAL, QQOFFICIAL_WEBHOOK, TELEGRAM, WECOM, WECOM_AI_BOT, LARK, DINGTALK, DISCORD, SLACK, KOOK, VOCECHAT, WEIXIN_OFFICIAL_ACCOUNT, SATORI, MISSKEY, LINE, MATRIX, WEIXIN_OC, MATTERMOST, WEBCHAT, ALL` |

- `ADAPTER_NAME_2_TYPE`（字符串 → 枚举）有 **20 个 key**（依据：`core/star/filter/platform_adapter_type.py:33-54`）：`aiocqhttp, qq_official, qq_official_webhook, telegram, wecom, wecom_ai_bot, lark, dingtalk, discord, slack, kook, vocechat, weixin_official_account, satori, misskey, line, matrix, weixin_oc, mattermost, webchat`。
- `MESSAGE_TYPE_2_EVENT_MESSAGE_TYPE` 映射在 `event_message_type.py:17-21`。

> ⚠️ **反直觉 12：`PlatformAdapterType.ALL` 不是"所有位的或"。**
> 它是 `enum.auto()`，即第 21 个**独立位**（依据：`core/star/filter/platform_adapter_type.py:30`）。
> `filter()` 里 `if self.platform_type & PlatformAdapterType.ALL: return True`（依据：`:64-69`）—— **只有显式传 `ALL` 时才走这条捷径**；传具体类型时靠 `ADAPTER_NAME_2_TYPE` 映射正常工作。

> ⚠️ **反直觉 13：`EventMessageType.ALL` 是 3 个位的或 = 7。**
> 依据：`core/star/filter/event_message_type.py:14`。`filter()` 用 `event_message_type & self.event_message_type`（依据：`:32`），所以传 `ALL` 能匹配任意消息类型；但 `list(EventMessageType)` 只遍历 3 个单比特成员（`ALL` 是组合值，不出现在迭代里）。

> 🔴 **反直觉 14：`PermissionTypeFilter` 用 `==` 而不是位与。**
> 依据：`core/star/filter/permission.py:23-31` —— 只判断 `if self.permission_type == PermissionType.ADMIN`。
> 传 `MEMBER` 时**恒返回 `True`**（docstring 说"当选择 MEMBER，ADMIN 也可以通过"，即 `MEMBER` 不做任何过滤，而不是"仅普通成员"）。

---

## D. `Context` 类

**文件**：`core/star/context.py`，类定义 L123–L894（依据：`core/star/context.py:123`），docstring「暴露给插件的接口上下文。」（`:124`）。

### D.1 类属性（注意：是类属性，跨实例共享）

| 属性 | 注解 | 默认 | 行号 |
|---|---|---|---|
| `registered_web_apis` | `list[RegisteredWebApi]` | `[]` | 126 |
| `_register_tasks` | `list[Awaitable]` | `[]` | 129 |
| `_star_manager` | — | `None` | 130 |

> ⚠️ **反直觉 15：这三个写在类体里 ⇒ 是类属性，所有 `Context` 实例共享同一个 list。**
> 依据：`core/star/context.py:126,129,130`。注释标为"向后兼容的变量"（`:128`）。

### D.2 `__init__` 与实例属性

依据：`core/star/context.py:132-169`。**12 个位置参数**（最后 1 个可选）：

```
def __init__(self, event_queue: Queue, config: AstrBotConfig, db: BaseDatabase,
             provider_manager: ProviderManager, platform_manager: PlatformManagerProtocol,
             conversation_manager: ConversationManager,
             message_history_manager: PlatformMessageHistoryManager,
             persona_manager: PersonaManager, astrbot_config_mgr: AstrBotConfigManager,
             knowledge_base_manager: KnowledgeBaseManager, cron_manager: CronJobManager,
             subagent_orchestrator: SubAgentOrchestrator | None = None) -> None
```

赋值的实例属性（依据：`:147-169`）：`_event_queue`(147)、`_config`(149)、`_db`(151)、`provider_manager`(153)、`platform_manager`(155)、`conversation_manager`(157)、`message_history_manager`(159)、`persona_manager`(161)、`astrbot_config_mgr`(163)、`kb_manager`(165)、`cron_manager`(167)、`subagent_orchestrator`(169)。

- `PlatformManagerProtocol`（`Protocol`）只有两个成员：`platform_insts: list[Platform]`、`get_insts: Callable[[], list[Platform]]`（依据：`core/star/context.py:118-120`）。
- ⚠️ 兼容层若自建 `Context`，必须提供前 11 个位置参数，或改签名（依据：`:132-146`）。

### D.3 全部公开方法（**34 个**）

依据：`core/star/context.py` 逐个方法定义行。

| # | 方法 | 签名 | 行号 | async | deprecated |
|---|---|---|---|---|---|
| 1 | `llm_generate` | `async def llm_generate(self, *, chat_provider_id: str, prompt: str \| None = None, image_urls: list[str] \| None = None, audio_urls: list[str] \| None = None, tools: ToolSet \| None = None, system_prompt: str \| None = None, contexts: list[Message] \| None = None, **kwargs: Any) -> LLMResponse` | 171 | ✔ | |
| 2 | `tool_loop_agent` | `async def tool_loop_agent(self, *, event: AstrMessageEvent, chat_provider_id: str, prompt=None, image_urls=None, audio_urls=None, tools=None, system_prompt=None, contexts=None, max_steps: int = 30, tool_call_timeout: int = 120, **kwargs) -> LLMResponse` | 215 | ✔ | |
| 3 | `get_current_chat_provider_id` | `async def get_current_chat_provider_id(self, umo: str) -> str` | 329 | ✔ | |
| 4 | `get_registered_star` | `def get_registered_star(self, star_name: str) -> StarMetadata \| None` | 346 | | |
| 5 | `get_all_stars` | `def get_all_stars(self) -> list[StarMetadata]` | 352 | | |
| 6 | `get_llm_tool_manager` | `def get_llm_tool_manager(self) -> FunctionToolManager` | 356 | | |
| 7 | `activate_llm_tool` | `def activate_llm_tool(self, name: str) -> bool` | 361 | | ✔ |
| 8 | `activate_llm_tool_async` | `async def activate_llm_tool_async(self, name: str) -> bool` | 375 | ✔ | |
| 9 | `deactivate_llm_tool` | `def deactivate_llm_tool(self, name: str) -> bool` | 393 | | ✔ |
| 10 | `deactivate_llm_tool_async` | `async def deactivate_llm_tool_async(self, name: str) -> bool` | 404 | ✔ | |
| 11 | `get_provider_by_id` | `def get_provider_by_id(self, provider_id: str) -> Provider \| TTSProvider \| STTProvider \| EmbeddingProvider \| RerankProvider \| None` | 415 | | |
| 12 | `get_all_providers` | `def get_all_providers(self) -> list[Provider]` | 440 | | |
| 13 | `get_all_tts_providers` | `def get_all_tts_providers(self) -> list[TTSProvider]` | 444 | | |
| 14 | `get_all_stt_providers` | `def get_all_stt_providers(self) -> list[STTProvider]` | 448 | | |
| 15 | `get_all_embedding_providers` | `def get_all_embedding_providers(self) -> list[EmbeddingProvider]` | 452 | | |
| 16 | `get_using_provider` | `def get_using_provider(self, umo: str \| None = None) -> Provider \| None` | 457 | | ✔ |
| 17 | `get_using_provider_async` | `async def get_using_provider_async(self, umo: str \| None = None) -> Provider \| None` | 482 | ✔ | |
| 18 | `get_using_tts_provider` | `def get_using_tts_provider(self, umo: str \| None = None) -> TTSProvider \| None` | 510 | | ✔ |
| 19 | `get_using_tts_provider_async` | `async def get_using_tts_provider_async(self, umo: str \| None = None) -> TTSProvider \| None` | 530 | ✔ | |
| 20 | `get_using_stt_provider` | `def get_using_stt_provider(self, umo: str \| None = None) -> STTProvider \| None` | 554 | | ✔ |
| 21 | `get_using_stt_provider_async` | `async def get_using_stt_provider_async(self, umo: str \| None = None) -> STTProvider \| None` | 574 | ✔ | |
| 22 | `get_config` | `def get_config(self, umo: str \| None = None) -> AstrBotConfig` | 597 | | |
| 23 | `send_message` | `async def send_message(self, session: str \| MessageSesion, message_chain: MessageChain) -> bool` | 614 | ✔ | |
| 24 | `add_llm_tools` | `def add_llm_tools(self, *tools: FunctionTool) -> None` | 679 | | |
| 25 | `register_web_api` | `def register_web_api(self, route: str, view_handler: WebApiHandler, methods: list[str], desc: str) -> None` | 705 | | |
| 26 | `get_event_queue` | `def get_event_queue(self) -> Queue` | 733 | | |
| 27 | `get_platform` | `def get_platform(self, platform_type: PlatformAdapterType \| str) -> Platform \| None` | 738 | | ✔（`version="4.0.0"`） |
| 28 | `get_platform_inst` | `def get_platform_inst(self, platform_id: str) -> Platform \| None` | 761 | | |
| 29 | `get_db` | `def get_db(self) -> BaseDatabase` | 777 | | |
| 30 | `register_provider` | `def register_provider(self, provider: Provider) -> None` | 785 | | |
| 31 | `register_llm_tool` | `def register_llm_tool(self, name: str, func_args: list, desc: str, func_obj: Callable[..., Awaitable[Any]]) -> None` | 794 | | ✔ |
| 32 | `unregister_llm_tool` | `def unregister_llm_tool(self, name: str) -> None` | 827 | | ✔ |
| 33 | `register_commands` | `def register_commands(self, star_name: str, command_name: str, desc: str, priority: int, awaitable: Callable[..., Awaitable[Any]], use_regex=False, ignore_prefix=False) -> None` | 840 | | ✔ |
| 34 | `register_task` | `def register_task(self, task: Awaitable, desc: str) -> None` | 884 | | ✔ |

### D.4 🔴 六个重点方法的完整签名与返回值语义

#### ① `get_config` —— **是方法（同步）**

- **签名**：`def get_config(self, umo: str | None = None) -> AstrBotConfig`（依据：`core/star/context.py:597`，实现 L597-612）
- **返回值语义**：
  - `umo` 为空/`None` → 返回 `self._config`（AstrBot **默认全局配置**，`AstrBotConfig`）（依据：`:609-611`）
  - `umo` 非空 → 返回 `self.astrbot_config_mgr.get_conf(umo)`（**该会话来源的配置**）（依据：`:612`）
  - **不会返回 `None`**（两条分支都返回对象）
- **异常**：源码内**未显式捕获异常**；`umo` 非法时的行为由 `astrbot_config_mgr.get_conf` 决定 —— **未确认**（未读 `core/astrbot_config_mgr.py`）
- **旁证**：`Star._get_context_config` 用 `getattr(self.context, "get_config", None)` 并 `except Exception` 兜底（依据：`core/star/base.py:54-62`）—— 说明真源码作者预期它可能抛异常。

#### ② `conversation_manager` —— **是【实例属性】，不是方法**

- 依据：`core/star/context.py:157` —— `self.conversation_manager = conversation_manager`
- 构造形参在 `:139`（`conversation_manager: ConversationManager`），import 在 `:16`（`from astrbot.core.conversation_mgr import ConversationManager`）
- **`Context` 里没有 `conversation_manager()` 这个方法**（依据：D.3 的 34 个方法列表里无此项）
- 访问形态：`ctx.conversation_manager.<方法>`，例如 `await ctx.conversation_manager.get_curr_conversation_id(umo)`

#### ③ `send_message` —— **是方法（异步）**

- **签名**：`async def send_message(self, session: str | MessageSesion, message_chain: MessageChain) -> bool`（依据：`core/star/context.py:614`，实现 L614-677）
- **返回值语义**：`bool` = **"是否找到匹配的平台"**（docstring L626 原文）
  - 找到 `platform.meta().id == session.platform_name` → `await platform.send_by_session(session, message_chain)` 后 **`return True`**（依据：`:641-643, 673`）
  - 遍历完都没匹配 → `logger.warning(f"cannot find platform for session {str(session)}, message not sent")` 并 **`return False`**（依据：`:674-677`）
- **`session` 形态**：`str` 时用 `MessageSesion.from_str(session)` 解析；解析失败 `raise ValueError("不合法的 session 字符串: " + str(e))`（依据：`:635-639`）
- **副作用**：群聊 + 平台 `meta().name != "webchat"` + 配置 `provider_ltm_settings.group_message_history_enable` 为真时，会把该消息写入 `message_history_manager.insert_message_chain(...)`（**异常被吞并 `logger.exception`**）（依据：`:644-672`）
- **注意**：`qq_official`（QQ 官方 API 平台）**不支持**此方法（依据：docstring `:633`）；类名 `MessageSesion` 是历史拼写错误（依据：docstring `:632`）

> ⚠️ **反直觉 16：返回的 `bool` 是"找到平台"，不是"发送成功"。**
> 只要 `platform.meta().id` 匹配到就 `return True`（依据：`:641-643, 673`）；底层 `send_by_session` 是否真送达不在返回值里体现。

#### ④ `llm_generate` —— **是方法（异步）**

- **签名**（依据：`core/star/context.py:171`，实现 L171-213）：
  `async def llm_generate(self, *, chat_provider_id: str, prompt: str | None = None, image_urls: list[str] | None = None, audio_urls: list[str] | None = None, tools: ToolSet | None = None, system_prompt: str | None = None, contexts: list[Message] | None = None, **kwargs: Any) -> LLMResponse`
- 🔴 **全部参数 keyword-only**：签名里有裸 `*`（依据：`:173`），必须 `ctx.llm_generate(chat_provider_id=..., prompt=...)`
- **返回值语义**：返回 `LLMResponse`（`astrbot.core.provider.entities.LLMResponse`，依据：`:25` 的 import）
  - 🔴 **不会自动执行 tool calls**；需要工具循环请用 `tool_loop_agent()`（依据：docstring `:183`）
- **参数透传**：`**kwargs` 直接透传给 `prov.text_chat(...)`（**OpenAI 兼容参数**）（依据：`:204-212`）
- **`contexts` 与 `prompt` 的关系**：两者都提供时，`prompt` 会作为**最后一条 user message 追加**（依据：docstring `:189`）
- **异常**：
  - provider 不存在或类型不符 → `raise ProviderNotFoundError(f"Provider {chat_provider_id} not found")`（依据：`:201-203`）
  - 其他错误由 `prov.text_chat` 抛出
- **版本标记**：`.. versionadded:: 4.5.7 (sdk)`（依据：docstring `:185`）

#### ⑤ `get_using_tts_provider` —— **是方法（同步，且已废弃）**

- **签名**：`def get_using_tts_provider(self, umo: str | None = None) -> TTSProvider | None`（依据：`core/star/context.py:510`，实现 L510-528）
- **废弃标记**：`@deprecated(reason="Use get_using_tts_provider_async() instead.")` 在 **L509**
- **返回值语义**：
  - 返回 `TTSProvider`，或**未设置时返回 `None`**（依据：docstring `:517-518`）
  - 若 `provider_manager` 返回的不是 `TTSProvider` → **`raise ValueError("返回的 Provider 不是 TTSProvider 类型")`**（依据：`:526-527`）
- **异步版本**：`async def get_using_tts_provider_async(self, umo: str | None = None) -> TTSProvider | None`（依据：`:530`，实现 L530-551）

> 🔴 **反直觉 17：同步版已废弃，但兼容层两个都必须要。**
> 老插件（陪伴插件实测用了 4 处）调的是同步版（依据：`/opt/astrbot/data/plugins/astrbot_plugin_private_companion` 词频统计 `get_using_tts_provider` × 4）。只实现 `_async` 版会让老插件直接挂。`@deprecated` 装饰器在调用时会发 `DeprecationWarning`（依据：`:509-510`）。

#### ⑥ `get_llm_tool_manager` —— **是方法（同步）**

- **签名**：`def get_llm_tool_manager(self) -> FunctionToolManager`（依据：`core/star/context.py:356`，实现 L356-358）
- **返回值语义**：`return self.provider_manager.llm_tools`（依据：`:358`），类型 `FunctionToolManager`，**永不为 `None`**（docstring `:357` 说明其用于管理注册的所有的 Function-calling tools）
- **`FunctionToolManager` 关键成员**（依据：`core/provider/func_tool_manager.py:288`）：
  - `func_list: list[FuncTool]` —— 所有工具（含 MCP 与插件工具，**不含** astrbot 内置工具）（依据：`:290-291`）
  - `builtin_func_list: dict[type[FuncTool], FuncTool]`（依据：`:292-293`）
  - `def spec_to_func(self, name: str, func_args: list[dict], desc: str, handler) -> FuncTool`（依据：`:343`）
  - `def add_func(self, name: str, func_args: list, desc: str, handler) -> None`（依据：`:365`）
  - `def remove_func(self, name: str) -> None`（依据：`:392`）

### D.5 `StarTools`（`core/star/star_tools.py:20`）

全 `@classmethod` 的工具类，导出在 `astrbot.api.star`（依据：`api/star/__init__.py:1,7`）。

| 方法 | 签名 | 行号 |
|---|---|---|
| `initialize` | `@classmethod def initialize(cls, context: Context) -> None` | 26 |
| `send_message` | `@classmethod async def send_message(...)`（多行签名，**未展开**） | 35 |
| `create_message` | `@classmethod async def create_message(...)`（**未展开**） | 57 |
| `create_event` | `@classmethod async def create_event(...)`（**未展开**） | 104 |
| `activate_llm_tool` | `@classmethod def activate_llm_tool(cls, name: str) -> bool` | 138 |
| `activate_llm_tool_async` | `@classmethod async def activate_llm_tool_async(cls, name: str) -> bool` | 155 |
| `deactivate_llm_tool` | `@classmethod def deactivate_llm_tool(cls, name: str) -> bool` | 173 |
| `deactivate_llm_tool_async` | `@classmethod async def deactivate_llm_tool_async(cls, name: str) -> bool` | 190 |
| `register_llm_tool` | `@classmethod def register_llm_tool(...)`（**未展开**） | 207 |
| `unregister_llm_tool` | `@classmethod def unregister_llm_tool(cls, name: str) -> None` | 230 |
| `get_data_dir` | `@classmethod def get_data_dir(cls, plugin_name: str \| None = None) -> Path` | 244 |

> ⚠️ 标「未展开」的 4 个方法签名含多行参数，本次**未逐字抽取** —— 需要时请重新读源码，**不要凭印象补**。

---

## E. `AstrMessageEvent`

**文件**：`core/platform/astr_message_event.py`，类定义 L37–L512（依据：`:37`），基类 `abc.ABC`（`:37`）。
**抽象方法**：无（虽然继承 `abc.ABC`，但全文件无 `@abstractmethod`）（依据：通读 L37-512）。

### E.1 实例属性

依据：`core/platform/astr_message_event.py` 的 `__init__`（L38-103）。

| 属性 | 注解 | 初值 | 行号 | 说明 |
|---|---|---|---|---|
| `message_str` | `str` | 构造入参 | 45 | 纯文本的消息 |
| `message_obj` | `AstrBotMessage` | 构造入参 | 47 | 完整消息结构 |
| `platform_meta` | `PlatformMetadata` | 构造入参 | 49 | `name` 是平台类型（如 `aiocqhttp`） |
| `role` | `str` | `"member"` | 51 | 管理员时是 `"admin"` |
| `is_wake` | `bool` | `False` | 53 | 是否通过 WakingStage |
| `is_at_or_wake_command` | `bool` | `False` | 55 | 是否 At 机器人/带唤醒词/私聊 |
| `_extras` | `dict[str, Any]` | `{}` | 57 | |
| `_force_stopped` | `bool` | `False` | 58 | **独立停止标志，不被 `clear_result()` 重置** |
| `session` | `MessageSession` | 构造 | 70 | platform_name / message_type / session_id |
| `_result` | `MessageEventResult \| None` | `None` | 77 | |
| `created_at` | `float` | `time()` | 80 | Unix timestamp |
| `trace` / `span` | `TraceSpan` | 构造 | 82 / 89 | `span` 是 `trace` 的别名 |
| `_has_send_oper` | `bool` | `False` | 92 | 本次事件是否至少发过一次消息 |
| `call_llm` | `bool` | `False` | 94 | 是否禁止默认 LLM 请求 |
| `_temporary_local_files` | `list[str]` | `[]` | 96 | |
| `plugins_name` | `list[str] \| None` | `None` | 99 | `None`=所有插件启用；空列表=一个都不启用 |
| `platform` | `PlatformMetadata` | `= platform_meta` | 103 | 向后兼容别名 |

### E.2 两个 property

| property | getter | setter | 语义 |
|---|---|---|---|
| `unified_msg_origin` | L106-108 | L110-114 | `return str(self.session)`，格式 `platform_name:message_type:session_id`（依据：`:106-108`）；**setter 会整体替换 `self.session`**（依据：`:113-114`） |
| `session_id` | L117-119 | L121-124 | `return self.session.session_id`；setter 直接改 `self.session.session_id`（依据：`:117-124`） |

### E.3 全部方法（42 个，含 2 个 deprecated）

依据：`core/platform/astr_message_event.py` 逐行。

| 方法 | 签名 | 行号 |
|---|---|---|
| `__init__` | `def __init__(self, message_str: str, message_obj: AstrBotMessage, platform_meta: PlatformMetadata, session_id: str) -> None` | 38 |
| `get_platform_name` | `def get_platform_name(self)` | 126 |
| `get_platform_id` | `def get_platform_id(self)` | 133 |
| `get_message_str` | `def get_message_str(self) -> str` | 140 |
| `_outline_chain` | `def _outline_chain(self, chain: list[BaseMessageComponent] \| None) -> str`（私有） | 144 |
| `get_message_outline` | `def get_message_outline(self) -> str` | 173 |
| `get_messages` | `def get_messages(self) -> list[BaseMessageComponent]` | 180 |
| `get_message_type` | `def get_message_type(self) -> MessageType` | 184 |
| `get_session_id` | `def get_session_id(self) -> str` | 191 |
| `get_group_id` | `def get_group_id(self) -> str` | 195 |
| `get_self_id` | `def get_self_id(self) -> str` | 199 |
| `get_sender_id` | `def get_sender_id(self) -> str` | 203 |
| `get_sender_name` | `def get_sender_name(self) -> str` | 210 |
| `set_extra` | `def set_extra(self, key, value) -> None` | 222 |
| `get_extra` | `def get_extra(self, key: str \| None = None, default = None) -> Any` | 226 |
| `clear_extra` | `def clear_extra(self) -> None` | 232 |
| `track_temporary_local_file` | `def track_temporary_local_file(self, path: str) -> None` | 237 |
| `cleanup_temporary_local_files` | `def cleanup_temporary_local_files(self) -> None` | 241 |
| `is_private_chat` | `def is_private_chat(self) -> bool` | 255 |
| `is_wake_up` | `def is_wake_up(self) -> bool` | 259 |
| `is_admin` | `def is_admin(self) -> bool` | 263 |
| `process_buffer` | `async def process_buffer(self, buffer: str, pattern: re.Pattern) -> str` | 267 |
| `send_streaming` | `async def send_streaming(self, generator: AsyncGenerator[MessageChain, None], use_fallback: bool = False) -> None` | 280 |
| `send_typing` | `async def send_typing(self) -> None` | 294 |
| `stop_typing` | `async def stop_typing(self) -> None` | 300 |
| `_pre_send` | `async def _pre_send(self) -> None` | 307 | **deprecated**（3.5.18） |
| `_post_send` | `async def _post_send(self) -> None` | 311 | **deprecated**（3.5.18） |
| `set_result` | `def set_result(self, result: MessageEventResult \| str) -> None` | 314 |
| `stop_event` | `def stop_event(self) -> None` | 343 |
| `continue_event` | `def continue_event(self) -> None` | 351 |
| `is_stopped` | `def is_stopped(self) -> bool` | 359 |
| `should_call_llm` | `def should_call_llm(self, call_llm: bool) -> None` | 367 |
| `get_result` | `def get_result(self) -> MessageEventResult \| None` | 374 |
| `clear_result` | `def clear_result(self) -> None` | 378 |
| `make_result` | `def make_result(self) -> MessageEventResult` | 384 |
| `plain_result` | `def plain_result(self, text: str) -> MessageEventResult` | 399 |
| `image_result` | `def image_result(self, url_or_path: str) -> MessageEventResult` | 403 |
| `chain_result` | `def chain_result(self, chain: list[BaseMessageComponent]) -> MessageEventResult` | 412 |
| `request_llm` | `def request_llm(self, prompt: str, func_tool_manager = None, tool_set: ToolSet \| None = None, session_id: str = '', image_urls: list[str] \| None = None, audio_urls: list[str] \| None = None, contexts: list \| None = None, system_prompt: str = '', conversation: Conversation \| None = None) -> ProviderRequest` | 420 |
| `send` | `async def send(self, message: MessageChain) -> None` | 478 |
| `react` | `async def react(self, emoji: str) -> None` | 497 |
| `get_group` | `async def get_group(self, group_id: str \| None = None, **kwargs) -> Group \| None` | 506 |

### E.4 🔴 被点名的 6 个成员：是否存在、什么形态

| 成员 | 存在 | 形态 | 依据 | 语义要点 |
|---|---|---|---|---|
| `message_str` | ✅ | **实例属性**（普通 `str`，非 property、非方法） | `:45` | 构造时传入的纯文本；等价接口 `event.get_message_str()`（`:140`） |
| `stop_event` | ✅ | **方法**（不是属性） | `:343-349` | 置 `self._force_stopped = True`（`:345`）；`_result` 为 `None` 时 `set_result(MessageEventResult().stop_event())`（`:346-347`），否则对已有 result 调 `.stop_event()`（`:348-349`） |
| `send` | ✅ | **异步方法** | `:478-495` | `async def send(self, message: MessageChain) -> None`；**参数是 `MessageChain` 不是 `str`**；**返回值是 `None` 不是 `bool`** |
| `is_admin` | ✅ | **方法**（不是属性） | `:263-265` | `return self.role == "admin"` —— 判据是实例属性 `role`，平台适配器可改写 |
| `get_sender_id` | ✅ | 方法 | `:203-208` | 取 `message_obj.sender.user_id`；`sender` 不存在或 `user_id` 不是 `str` 时返回 **空字符串** |
| `unified_msg_origin` | ✅ | **property（有 getter 与 setter）**，不是方法 | getter `:106-108`，setter `:110-114` | `str(self.session)`，格式 `platform_name:message_type:session_id`；赋值会整体替换 `session` |

> 🔴 **反直觉 18：`AstrMessageEvent.send` 的基类实现并不真的发消息。**
> 基类 `send()` 只做两件事：`asyncio.create_task(Metric.upload(...))` 与 `self._has_send_oper = True`（依据：`:485-495`）。**真正发送靠平台子类覆写。**
> 兼容层的 `send()` 若直接实现"塞进回复链"，语义上比基类**更强** —— 这通常是对的（子类就该这样），但要明确知道这是子类语义而非基类语义。

> ⚠️ **反直觉 19：`clear_result()` 不会重置停止标志。**
> `_force_stopped` 是独立字段（依据：`:58-59` 注释原文"独立的停止标志，不依赖 _result，不会被 clear_result() 重置"）。`stop_event()` 置真后 `clear_result()`（`:378-380`）只清 `_result`，`is_stopped()`（`:359-365`）仍返回 `True`。

> ⚠️ **反直觉 20：`get_sender_id()` 可能返回空字符串**（不抛异常、不返回 `None`）（依据：`:203-208`）。

### E.5 真实插件高频使用排行（插件侧证据）

对磁盘上的**陪伴插件**（`/opt/astrbot/data/plugins/astrbot_plugin_private_companion/`）全部 `.py` 做 `event.<成员>` 词频统计：

| 成员 | 命中次数 |
|---|---|
| `get_sender_id` | **112** |
| `stop_event` | **83** |
| `send` | **59** |
| `message_str` | **57** |
| `get_result` | **43** |
| `set_result` | **36** |
| `unified_msg_origin` | **33** |
| `chain_result` | **23** |
| `plain_result` | **18** |
| `is_private_chat` | 14 |
| `get_extra` | 11 |
| `is_stopped` | 7 |
| `set_extra` | 4 |
| `send_streaming` | 4 |
| `get_self_id` | 3 |
| `get_platform_name` | 2 |
| `get_messages` | 2 |
| `should_call_llm` | 1 |
| `get_sender_name` | 1 |
| `get_message_str` | 1 |

→ **兼容层最小必需集**：`get_sender_id` / `stop_event` / `send` / `message_str` / `get_result` / `set_result` / `unified_msg_origin` / `chain_result` / `plain_result` / `is_private_chat`。

---

## F. 消息组件

**真实定义文件**：`core/message/components.py`（944 行）
**API 再导出文件**：`api/message_components.py`，**全文仅 1 行**：`from astrbot.core.message.components import *`（依据：`api/message_components.py:1`）

> 所以查组件的字段与构造参数，**必须去 `core/message/components.py`**，`api/message_components.py` 里什么都没有。

### F.1 基类与枚举

- `ComponentType(str, Enum)`，L46–L70，**21 个成员**，值 = 类名字符串（依据：`:46-70`）：
  `Plain, Image, Record, Video, File, Face, At, Node, Nodes, Poke, Reply, Forward, RPS, Dice, Shake, Share, Contact, Location, Music, Json, Unknown`
- `BaseMessageComponent(BaseModel)`，L73–L110（依据：`:73`）
  - pydantic 版本分流：Python ≥ 3.14 用 `pydantic.BaseModel`，否则用 `pydantic.v1.BaseModel`（依据：`:35-38`）
  - 字段：`type: ComponentType`（依据：`:74`）
  - `def __init__(self, **kwargs) -> None`（依据：`:76`）
  - `def toDict(self)`（依据：`:98-106`）：跳过 `type` 与 `None` 值；把 `_type` 改名回 `type`；返回 `{"type": self.type.lower(), "data": data}`
  - `async def to_dict(self) -> dict`（依据：`:108-110`）：默认回退到 `toDict()`
  - `__repr_args__` 会截断超长/base64 字段（依据：`:79-96`）

### F.2 全部组件类与构造参数（22 个）

依据：`core/message/components.py` 逐类逐行。

| 类 | 定义行 | 构造签名 | 构造行 | 字段（名: 注解 / 默认，行号） |
|---|---|---|---|---|
| `Plain` | 113 | `def __init__(self, text: str, convert: bool = True, **_) -> None` | 117 | `type=ComponentType.Plain`(114), `text: str`(115) |
| `Face` | 127 | `def __init__(self, **_) -> None` | 131 | `type=ComponentType.Face`(128), `id: int`(129) |
| `Record` | 135 | `def __init__(self, file: str \| None, **_) -> None` | 144 | `type`(136), `file: str\|None=''`(137), `url: str\|None=''`(138), `text: str\|None=None`(140), `path: str\|None=None`(142) |
| `Video` | 288 | `def __init__(self, file: str, **_) -> None` | 296 | `type`(289), `file: str`(290), `url: str\|None=''`(291), `cover: str\|None=''`(292), `path: str\|None=''`(294) |
| `At` | 410 | `def __init__(self, **_) -> None` | 415 | `type`(411), `qq: int\|str`（str 为 `all` 时代表所有人）(412), `name: str\|None=''`(413) |
| `AtAll` | 425 | `def __init__(self, **_) -> None` | 428 | 继承 `At`；`qq: str = "all"`(426) |
| `RPS` | 432 | `def __init__(self, **_) -> None` | 435 | `type`(433) —— 源码标 `# TODO` |
| `Dice` | 439 | `def __init__(self, **_) -> None` | 442 | `type`(440) —— `# TODO` |
| `Shake` | 446 | `def __init__(self, **_) -> None` | 449 | `type`(447) —— `# TODO` |
| `Share` | 453 | `def __init__(self, **_) -> None` | 460 | `type`(454), `url: str`(455), `title: str`(456), `content: str\|None=''`(457), `image: str\|None=''`(458) |
| `Contact` | 464 | `def __init__(self, **_) -> None` | 469 | `type`(465), `_type: str`(466), `id: int\|None=0`(467) —— `# TODO` |
| `Location` | 473 | `def __init__(self, **_) -> None` | 480 | `type`(474), `lat: float`(475), `lon: float`(476), `title: str\|None=''`(477), `content: str\|None=''`(478) —— `# TODO` |
| `Music` | 484 | `def __init__(self, **_) -> None` | 494 | `type`(485), `_type: str`(486), `id: int\|None=0`(487), `url=''`(488), `audio=''`(489), `title=''`(490), `content=''`(491), `image=''`(492) |
| `Image` | 501 | `def __init__(self, file: str \| None, **_) -> None` | 509 | `type`(502), `file: str\|None=''`(503), `_type: str\|None=''`(504), `url: str\|None=''`(505), `path: str\|None=''`(507) |
| `Reply` | 584 | `def __init__(self, **_) -> None` | 606 | `type`(585), **`id: str\|int`（必填）**(586), `chain: list[BaseMessageComponent]\|None=[]`(588), `sender_id: int\|None\|str=0`(590), `sender_nickname: str\|None=''`(592), `time: int\|None=0`(594), `message_str: str\|None=''`(596), `text: str\|None=''`(599, deprecated), `qq: int\|None=0`(601, deprecated), `seq: int\|None=0`(603, deprecated) |
| `Poke` | 614 | `def __init__(self, poke_type: str \| int \| None = None, **_) -> None` | 620 | `type`(615), `_type: str\|int='126'`(616), `id: int\|str\|None=0`(617), `qq: int\|str\|None=0`(618, deprecated) |
| `Forward` | 647 | `def __init__(self, **_) -> None` | 651 | `type`(648), **`id: str`（必填）**(649) |
| `Node` | 655 | `def __init__(self, content: list[BaseMessageComponent], **_) -> None` | 666 | `type`(658), `id: int\|None=0`（忽略）(659), `name: str\|None=''`(660), `uin: str\|None='0'`(661), **`content: list[BaseMessageComponent]=[]`（构造要求传入）**(662), `seq: str\|list\|None=''`（忽略）(663), `time: int\|None=0`（忽略）(664) |
| `Nodes` | 709 | `def __init__(self, nodes: list[Node], **_) -> None` | 713 | `type`(710), **`nodes: list[Node]`（必填）**(711) |
| `Json` | 736 | `def __init__(self, data: str \| dict, **_) -> None` | 740 | `type`(737), **`data: dict`（必填）**(738) |
| `Unknown` | 746 | *（无自定义 `__init__`，继承 `BaseMessageComponent(**kwargs)`）* | — | `type`(747), **`text: str`（必填）**(748) |
| `File` | 764 | `def __init__(self, name: str, file: str = '', url: str = '') -> None` | 772 | `type`(767), `name: str\|None=''`(768), `file_: str\|None=''`（本地路径）(769), `url: str\|None=''`(770) |

**`File` 的特别之处**（依据：`:764-816`）：
- 构造参数叫 `file`，但内部字段是 `file_`（依据：`:774` 的 `super().__init__(name=name, file_=file, url=url)`）
- `file` 是 **property**（getter `:777`，setter `:815`）；getter 在 `file_` 不存在但有 `url` 时会**同步下载文件**（依据：`:777-814`）

**各组件额外的静态/类方法**（依据：逐行）：

| 类 | 方法 | 行号 |
|---|---|---|
| `Record` | `fromFileSystem(path, **_)` / `fromURL(url, **_)` / `fromBase64(bs64_data, **_)` | 151 / 156 / 162 |
| `Video` | `fromFileSystem` / `fromURL` / `fromBase64` / `async to_dict` | 300 / 305 / 311 / 390 |
| `Image` | `fromURL` / `fromFileSystem` / `fromBase64` / `fromBytes` / `fromIO` | 513 / 519 / 524 / 528 / 532 |
| `Node` | `async to_dict` | 672 |
| `Nodes` | `toDict`（已 deprecated）/ `async to_dict` | 717 / 727 |
| `File` | `file` property getter/setter / `async to_dict` | 777 / 815 / 898 |

**`toDict()` 实测输出**（本次 import 实跑）：

| 调用 | 输出 |
|---|---|
| `Plain("x").toDict()` | `{'type': 'text', 'data': {'text': 'x'}}` |
| `At(qq=1).toDict()` | `{'type': 'at', 'data': {'qq': '1'}}`（**qq 被转成字符串**） |
| `Reply(id='9').toDict()` | `{'type': 'reply', 'data': {'id': '9'}}`（**只输出 id**，依据：`:609-612`） |
| `Poke().toDict()` | `{'type': 'poke', 'data': {'type': '126'}}` |

**构造实测**（本次 import 实跑，全部 OK）：

```
Plain('hi')                        -> OK
Image('http://x/y.png')            -> OK（file 是第一个位置参数）
At(qq=123)                         -> OK
AtAll()                            -> OK -> qq='all'
Reply(id='1')                      -> OK
Record('x.mp3') / Record(file=...) -> OK
Face(id=1) / Video(file='v.mp4')   -> OK
File(name='a.txt', file='/tmp/a')  -> OK -> 内部 file_='/tmp/a'
Poke()                             -> OK -> _type='126'
Node(content=[Plain('x')])         -> OK
Nodes(nodes=[...])                 -> OK
Json(data='{"a":1}')               -> OK（自动 json.loads）
Music()                            -> OK（全字段有默认值）
```

> ⚠️ **`Unknown` 无法直接无参构造**：它没有自定义 `__init__`，`text` 是 pydantic 必填字段（依据：`:746-748`）。**未实测**，标为待验证。

### F.3 `MessageChain` 与 `MessageEventResult`

依据：`core/message/message_event_result.py`。

**`MessageChain`**（`@dataclass`，L18）：

- 构造：`MessageChain(chain: list[BaseMessageComponent] = <factory>, use_t2i_: bool | None = None, use_markdown_: bool | None = None, type: str | None = None)`

| 方法 | 签名 | 行号 |
|---|---|---|
| `derive` | `def derive(self, chain: list[BaseMessageComponent] \| None = None) -> 'MessageChain'` | 36 |
| `message` | `def message(self, message: str)` | 49 |
| `at` | `def at(self, name: str, qq: str \| int)` | 60 |
| `at_all` | `def at_all(self)` | 71 |
| `error` | `def error(self, message: str)` | 82（**deprecated**：请使用 `message` 方法代替） |
| `url_image` | `def url_image(self, url: str)` | 93 |
| `file_image` | `def file_image(self, path: str)` | 106 |
| `base64_image` | `def base64_image(self, base64_str: str)` | 118 |
| `use_t2i` | `def use_t2i(self, use_t2i: bool)` | 127 |
| `use_markdown` | `def use_markdown(self, use: bool \| None = True)` | 137 |
| `get_plain_text` | `def get_plain_text(self, with_other_comps_mark: bool = False) -> str` | 149 |
| `squash_plain` | `def squash_plain(self)` | 170 |

（这些方法基本都 `return self`，可链式调用。）

**`MessageEventResult(MessageChain)`**（L224）：

| 方法 | 签名 | 行号 |
|---|---|---|
| `stop_event` | `def stop_event(self) -> 'MessageEventResult'` | 246 |
| `continue_event` | `def continue_event(self) -> 'MessageEventResult'` | 251 |
| `is_stopped` | `def is_stopped(self) -> bool` | 256 |
| `set_async_stream` | `def set_async_stream(self, stream: AsyncGenerator) -> 'MessageEventResult'` | 260 |
| `set_result_content_type` | `def set_result_content_type(self, typ: ResultContentType) -> 'MessageEventResult'` | 265 |
| `is_llm_result` | `def is_llm_result(self) -> bool` | 275 |
| `is_model_result` | `def is_model_result(self) -> bool` | 279 |

- `EventResultType`（L195）：`CONTINUE`, `STOP`
- `ResultContentType`（L208）：`LLM_RESULT`, `AGENT_RUNNER_ERROR`, `GENERAL_RESULT`, `STREAMING_RESULT`, `STREAMING_FINISH`

---

## G. 插件实际导入面（兼容层最小集）

对 `/opt/astrbot/data/plugins` 全量 `.py` 统计的真实 import 形态：

| import 语句 | 次数 |
|---|---|
| `from astrbot.api import logger` | 27 |
| `from astrbot.api.star import Context, Star, StarTools, register` | 19 |
| `from astrbot.api.message_components import Plain` | 18 |
| `from astrbot.api.event import AstrMessageEvent` | 17 |
| `from astrbot.api.provider import ProviderRequest` | 16 |
| `from astrbot.api.event import AstrMessageEvent, MessageChain, filter` | 14 |
| `from astrbot.api.message_components import Reply` | 14 |
| `from astrbot.api.message_components import At, Image, Plain` | 14 |
| `from astrbot.api import AstrBotConfig` | 14 |
| `from astrbot.api.message_components import At, Image, Plain, Record, Reply` | 13 |

装饰器真实使用频次：

| 装饰器 | 次数 |
|---|---|
| `@filter.llm_tool` | **39** |
| `@filter.on_decorating_result` | 33 |
| `@filter.command` | 26 |
| `@filter.on_llm_request` | 25 |
| `@filter.event_message_type` | 14 |
| `@filter.on_llm_response` | 13 |
| `@filter.permission_type` | 12 |
| `@filter.after_message_sent` | 12 |
| `@filter.on_agent_done` | 3 |
| `@filter.command_group` | 3 |
| `@filter.on_astrbot_loaded` | 2 |
| `@filter.on_agent_begin` | 2 |
| `@filter.on_waiting_llm_request` | 1 |
| `@filter.on_plugin_unloaded` | 1 |
| `@filter.on_plugin_loaded` | 1 |
| `@filter.on_llm_tool_respond` | 1 |

**从未在这些插件里出现的装饰器**：`custom_filter`、`platform_adapter_type`、`regex`、`on_platform_loaded`、`on_plugin_error`、`on_using_llm_tool`。

---

## H. 与直觉不符的发现（汇总，共 24 条）

| # | 主题 | 一句话 | 依据 |
|---|---|---|---|
| 1 | **排序方向相反** | `append` 用 `-priority` 降序；`__lt__` 定义升序。**两者完全相反** | `core/star/star_handler.py:26` vs `:277-282` |
| 2 | `__lt__` 是死代码 | docstring 说"支持优先队列"，但全库无 heapq 调用点 | `core/star/star_handler.py:278`；全库 grep 零命中 |
| 3 | `Star.__init__` 的 `config` 被丢弃 | 基类函数体从不使用 `config`，`Star` 也没有 `config` 属性 | `core/star/base.py:28-52` |
| 4 | **`terminate` 与 `__del__` 是 `elif`** | 插件类若定义 `__del__`，`terminate` 永不执行；判断用 `__dict__` ⇒ 继承来的 `terminate` 不会被调用 | `core/star/star_manager.py:2022-2041` |
| 5 | 两个生命周期钩子判断方式不一致 | `initialize` 用 `hasattr`（会继承），`terminate` 用 `__dict__`（不继承） | `star_manager.py:1417` vs `:2040` |
| 6 | 实例化只捕获 `TypeError` | `__init__` 内部抛的 `TypeError` 会被误判为"签名不兼容"而重试 | `star_manager.py:1221-1229` |
| 7 | 一个模块只能生效一个 Star 子类 | 判重 key 是 `cls.__module__`，第二个子类只覆盖 `star_cls_type` | `core/star/base.py:66-75` |
| 8 | `EventMessageType.ALL` 是组合值 = 7 | 不在 `list(EventMessageType)` 迭代里，但 `&` 判断能匹配任意类型 | `core/star/filter/event_message_type.py:14, 32` |
| 9 | `PlatformAdapterType.ALL` 是独立位 | 不是"所有位的或"；只有显式传 `ALL` 才走 `& ALL` 捷径 | `core/star/filter/platform_adapter_type.py:30, 64-69` |
| 10 | `PermissionTypeFilter` 用 `==` 不用位与 | 传 `MEMBER` 恒返回 `True`（不做任何过滤） | `core/star/filter/permission.py:23-31` |
| 11 | `context.conversation_manager` 是属性 | 不是方法；`__init__` 里直接赋值 | `core/star/context.py:139, 157` |
| 12 | `get_using_tts_provider` 已废弃但必须保留 | 老插件（陪伴插件 4 处）调同步版；只实现 async 版会让它挂 | `core/star/context.py:509-510` vs `:530` |
| 13 | 无 filter 的消息 handler 被静默跳过 | `if len(handler.event_filters) == 0: continue` | `core/pipeline/waking_check/stage.py:182-183` |
| 14 | `llm_tool` 类型白名单比 JSON Schema 窄 | `integer` 会被拒绝抛 `ValueError`；只能写 `number` | `func_tool_manager.py:128-145` + `register/star_handler.py:647-655`；实测 |
| 15 | **`llm_tool` 不排除 `event` 参数** | 写进 `Args:` 就会被注册成工具参数 | `register/star_handler.py:635-665`；实测 |
| 16 | `AstrMessageEvent.send` 基类不真发消息 | 只上报 Metric 并置 `_has_send_oper`；返回 `None` 不是 `bool` | `core/platform/astr_message_event.py:478-495` |
| 17 | `get_sender_id()` 可能返回 `""` | 不抛异常、不返回 `None` | `:203-208` |
| 18 | `clear_result()` 不重置停止标志 | `_force_stopped` 独立于 `_result` | `:58-59, 343-349, 359-365, 378-380` |
| 19 | `api/message_components.py` 只有一行 | 真实定义全在 `core/message/components.py` | `api/message_components.py:1` |
| 20 | `llm_tool` 解析失败发生在 import 期 | 不是调用工具时 | `register/star_handler.py:639-655`；实测 |
| 21 | `Context.registered_web_apis` 是类属性 | 跨实例共享同一个 list | `core/star/context.py:126, 129, 130` |
| 22 | `regex` 不受唤醒前缀制约，`command` 受 | 两处源码注释明写 | `command.py:30, 192` vs `regex.py:9, 17-18` |
| 23 | `llm_generate` 全部参数 keyword-only | 签名有裸 `*`；且**不自动执行 tool calls** | `core/star/context.py:171-182`，docstring `:183` |
| 24 | `send_message` 的 `bool` 是"找到平台" | 不代表发送成功 | `core/star/context.py:641-643, 673` |

---

## I. 未确认项（**不要凭印象补**）

| 项 | 原因 | 状态 |
|---|---|---|
| `astrbot_config_mgr.get_conf(umo)` 在 `umo` 非法时的行为 | 未读 `core/astrbot_config_mgr.py` | **未确认** |
| `StarTools.send_message` / `create_message` / `create_event` / `register_llm_tool` 的完整参数列表 | 本次未逐字展开多行签名（仅知起始行 35/57/104/207） | **未确认** |
| `ConversationManager` 的公开方法 | 未读 `core/conversation_mgr.py` | **未确认** |
| `FunctionTool` / `ToolSet` 的字段 | 未读 `core/agent/tool.py` | **未确认** |
| `MessageSesion.from_str` 的解析与容错细则 | 未读 `core/platform/message_session.py` | **未确认** |
| `Unknown(text=...)` 能否构造 | pydantic v1 校验未实测 | **未确认** |
| `AstrBotConfig` 的字典接口细节 | 未读 `core/config/astrbot_config.py` | **未确认** |
| `list[str]` 以外的泛型写法（如 `List[str]`）是否被 `llm_tool` 接受 | 正则 `(\w+)\[(\w+)\]` 理论上匹配，未实测 | **未确认** |

---

## J. 给兼容层的行动清单（据本文结论推导）

**最小必需（否则插件加载即失败）**
1. `astrbot.api.event.filter` 的 **29 个导出名**（22 装饰器 + 7 类/枚举）—— 依据 C.1。
2. 22 个装饰器的**精确签名**（尤其 `**kwargs` 必须接收并作为 `extras_configs`，因为 `priority` 走这条路）—— 依据 C.2、C.4。
3. `AstrMessageEvent` 的高频 10 个成员（依据 E.5）。
4. `Context` 的 6 个重点方法 + `conversation_manager` **属性**（依据 D.4）。
5. `api.message_components` 的组件类（依据 F.2）。

**语义必须对齐（错了会静默出问题）**
6. handler **排序：`priority` 大的先执行**（依据 B.4）—— 千万别照 `__lt__` 写成升序。
7. `terminate` 的 `elif __del__` 陷阱与 `__dict__` 判据（依据 A.5）。
8. `llm_tool` 的类型白名单（**没有 `integer`**）与 import 期报错（依据 C.6.3）。
9. `stop_event` 的 `_force_stopped` 独立标志语义（依据 E.4）。
10. `send_message` 返回 `True` 只代表"找到平台"（依据 D.4③）。

**建议做但需确认**
11. `StarTools` 的 11 个 classmethod（4 个签名未展开，需要时重新读）。
12. `get_config(umo)` 的会话级配置隔离（依赖 `astrbot_config_mgr`，未确认）。

---

*本文由子 agent 依据真实源码生成；所有结论均可回溯到 `文件:行号`。未确认项已单列于 §I，未做任何推测性补全。*
