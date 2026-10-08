# 陪伴插件「独立 WebUI」调研（只读静态分析）

> 调研对象：`/root/Ai-works/情绪app/data/plugin_src/astrbot_plugin_private_companion/`（v6.6.2，我们实际在跑的那份镜像）
> 结论口径：**先结论后证据**，每条给 `文件:行号`。查不到的一律写「未找到」+ 搜了什么。
> 禁忌遵守：全程**只读**。没有启动任何 standalone webui、没有绑定任何端口、没有写 `data/plugins/**`。
> 实测环境：服务在跑（`python -m emotion_app serve`，`0.0.0.0:6191`，pid 3905246）；端口 6190 **空闲**。

---

## 0. 一页结论

| 问题 | 结论 |
|---|---|
| 在哪 | `standalone_webui.py`（986 行），类 `StandaloneWebUIServer`（`:169`），别名 `StandaloneWebUIService`（`:978`） |
| 默认端口 | **`6190`**（三处默认值一致：`standalone_webui.py:202`、`plugin_bootstrap.py:337`、`_conf_schema.json:118`） |
| 默认监听地址 | **`127.0.0.1`**（`standalone_webui.py:195`、`_conf_schema.json:109`）—— 默认不对局域网开放 |
| 怎么启 | **不是命令行**。插件 `initialize()` → `_initialize_before_publication()` 里自动 `await standalone_webui.start()`（`main.py:8629-8638`），由配置项 `enable_standalone_webui` 开关 |
| 默认开还是关 | **默认关**（`_conf_schema.json:103` = `false`），且**必须**再填 ≥16 字符令牌，否则拒绝启动（`standalone_webui.py:317-321`） |
| 页面 | **就是同一套陪伴面板**（`pages/companion-panel/`），不是第二套前端；standalone 只是换传输层 + 加一个登录浮层 |
| 路由 | 自有 11 条（页面/静态/auth/错误）+ **复用插件 Page API 的 197 条业务路由**，挂在 `/api/v1/*` |
| 真 AstrBot 里的形式 | **插件内自带 Quart + Hypercorn 独立服务**（自己 `create_task(hypercorn_serve(...))`），**不走宿主派发**；AstrBot 界面**没有**入口，用户自己开浏览器访问独立端口 |
| 我们对它的关系 | **零代码引用**；我们走的是「插件拓展页」那条路。端口已让开（6191）。面板上无入口 |
| 6190 注释核实 | ✅ **与源码一致**，注释没错（只有措辞上"已占用端口"一处可挑，见 §5） |

---

## 1. 它在哪、默认端口多少、启动入口是什么

### 1.1 模块与类

- 模块：`standalone_webui.py`，**986 行**（`wc -l` 实测）。
- 类：`class StandaloneWebUIServer`（`standalone_webui.py:169`），文档串 `"""Serve the companion panel beside AstrBot's existing Page bridge."""`（`:170`）—— 一句话说明它与插件页桥**并存**。
- 别名：`StandaloneWebUIService = StandaloneWebUIServer`（`standalone_webui.py:978`，注释 `# Keep the service-oriented name available for callers and focused tests.`）。
- 导出：`__all__` 含 `API_PREFIX` / `SESSION_COOKIE_NAME` / 两个类名（`standalone_webui.py:981-986`）。

### 1.2 默认端口 = **6190**（三处源码默认值，均不是猜的）

| 位置 | 代码 | 值 |
|---|---|---|
| `standalone_webui.py:201-206` | `self.port = self._bounded_int(getattr(plugin, "standalone_webui_port", 6190) ... , default=6190, minimum=1, maximum=65535)` | `6190` |
| `plugin_bootstrap.py:337` | `self.standalone_webui_port = self._cfg_int(c, "standalone_webui_port", 6190, 1, 65535)` | `6190` |
| `_conf_schema.json:115-118` | `"standalone_webui_port": { ... "default": 6190 ... }` | `6190` |

旁证（插件自己钉死的判据）：`tests/test_standalone_webui.py:97` `self.assertEqual(items["standalone_webui_port"]["default"], 6190)`；文档 `README.md:201` 写 `http://127.0.0.1:6190/`；`docs/WORKING_PRINCIPLES.md:33` 目录项「22. 独立 WebUI：端口 6190 + Token 登录」、`:1877` `PORT = 6190  # line 202`。

默认 host = `127.0.0.1`：`standalone_webui.py:193-200`（`.strip() or "127.0.0.1"`）、`plugin_bootstrap.py:334-336`、`_conf_schema.json:109`。

### 1.3 启动入口：**由插件生命周期调用，不是命令行**

**未找到任何 CLI 入口**。搜法：`grep -rn "__main__\|argparse" --include=*.py .`（排除 `tests/`）→ 只命中 `scripts/` 下的运维脚本（`wardrobe_review.py`、`dependency_gate.py` 等）与 `bot_personal_contract.py:327`，**没有**任何一处在 `standalone_webui.py` 或它的调用链上。插件也没有 `python -m ...` 形式的文档（`README.md` 内 grep `python -m` 无命中）。

真实启动链（**四段，全部有源码位置**）：

```
宿主 plugin_loader.initialize_all()          host/astrbot_host/loader/plugin_loader.py:492（定义）
   └─ await rec.instance.initialize()              同上 :502（逐个调用；异常隔离 :504-507）
        └─ PrivateCompanionPlugin.initialize()          main.py:8461（定义）
             └─ await self._initialize_before_publication()   main.py:8484（调用）/ :8535（定义）
                  └─ standalone_webui = getattr(self, "standalone_webui", None)   main.py:8629
                     if standalone_webui is not None: await standalone_webui.start()  main.py:8630-8632
                     （异常 → warning「独立陪伴 WebUI 启动失败: %s」 main.py:8633-8638）
```

> ⚠️ 函数边界我实测过：`_initialize_before_publication` 定义在 `main.py:8535`，**下一个 `def` 在 `main.py:8640`** → 上面 `:8629-8638` 全在它体内。`:8484` 是同文件 `initialize()` 体内对它的调用。

- 对象在**插件构造阶段**就建好了：`plugin_bootstrap.py:2175 def initialize_plugin_runtime` → `:2300 self.page_api = None` → `:2301 self.standalone_webui = None` → `:2303 self._register_page_api_if_available()`；后者定义在 `integration_status.py:512`，尾部 `:530-536`：
  ```python
  try:
      from .standalone_webui import StandaloneWebUIServer
      self.standalone_webui = StandaloneWebUIServer(self, self.page_api)
  except Exception as e:
      self.standalone_webui = None
      logger.warning(f"独立陪伴 WebUI 初始化失败: {e}", exc_info=True)
  ```
  构造**只是存配置**（`standalone_webui.py:186-251`），**不绑定端口**；真起服务全在 `start()`。
- 关闭：`plugin_lifecycle.py:215-222` `await standalone_webui.stop()`（shutdown 路径，无条件执行，见 §3.4）。
- `initialize_plugin_runtime` 的调用点：`main.py:4687`（在 `__init__` 里，`main.py:4676-4692`）。

### 1.4 `start()` 的五道闸（**这就是"什么时候真的会绑端口"**）

`standalone_webui.py:311-365`：

| 顺序 | 行号 | 条件 | 不满足时 |
|---|---|---|---|
| ① | `:314-316` | `self.enabled`（= `enable_standalone_webui`） | `logger.debug("[独立WebUI] 独立 WebUI 未启用")` + `return False` |
| ② | `:317-321` | 有令牌且长度 ≥ 16 | `logger.warning("...未启动: 访问令牌至少需要 16 个字符")` |
| ③ | `:322-324` | `self.page_api is not None` | warning「Page API 不可用」 |
| ④ | `:325-331` | `dependencies_available()`（Quart + Hypercorn 七个符号，`:271-285`） | warning 带 `quart=%s hypercorn=%s` |
| ⑤ | `:332-334` | `pages/companion-panel/index.html` 存在 | warning「面板静态文件不存在」 |
| 起 | `:336-354` | `_Quart(...)` + `_HypercornConfig()` + `config.bind=[host:port]`（`:339`）+ `asyncio.create_task(_hypercorn_serve(...))`（`:346-349`） | 异常 → warning「启动失败: error_type=%s」 |
| 成 | `:364` | — | `logger.info("[独立WebUI] 独立 WebUI 已启动: %s", self.url)` |

⚠️ **第①道闸只打 DEBUG**（`:315`）。我们服务日志里 `grep -c DEBUG logs/serve.out` = **0**（日志级别不含 DEBUG）→ 插件确实走到过这里、也确实"未启用"，但**日志里查不到任何痕迹**。这是"想确认它没在跑"时的取证盲区（不是 bug，是级别问题）。

---

## 2. 它提供哪些页面/路由，与「插件拓展页」是什么关系

### 2.1 结论：**同一套前端**，不是另一套

- 静态根目录硬指向插件拓展页那份：`standalone_webui.py:236-238`
  ```python
  self.static_root = Path(static_root or Path(__file__).parent / "pages" / "companion-panel").resolve()
  ```
- `GET /` 返回的就是那份 `index.html`，只做两处**注入**（`_load_panel_html`，`:833-855`）：
  1. `<meta name="private-companion-mode" content="standalone" />` + `<meta name="private-companion-api-base" content="/api/v1" />`（常量定义 `:69-72`，注入点 `:840-844`）；
  2. `<script src="./standalone.js?v=1"></script>`（常量 `:73`，注入点 `:845-853`，插在 `./app.js` 那行之前）。
- 注入是**幂等**的：已含 `name="private-companion-mode"` / `./standalone.js` 就不重复插（`:840`、`:845`）。当前磁盘上的 `pages/companion-panel/index.html` 里这两样**都没有**（`grep -c "standalone.js" index.html` = **0**；`grep -c "enable_standalone_webui|standalone_webui_port" index.html` = **0**）→ 每次都由这个函数现注入。
- `app.js`（2,290,185 B / 约 2.0 MB 字符）**明白地同时支持两条传输**：
  - `pages/companion-panel/app.js:1-3`
    ```js
    const HTTP_API = "/astrbot_plugin_private_companion/page";
    const STANDALONE_HTTP_API = "/api/v1";
    const STANDALONE_MODE_META = "private-companion-mode";
    ```
  - `app.js:6513 standaloneRuntimeConfig()` —— 认 `window.__PRIVATE_COMPANION_STANDALONE__`（`true` 或对象）/ `window.__PRIVATE_COMPANION_WEBUI__` / `window.__PRIVATE_COMPANION_WEBUI_CONFIG__`，或 `mode === "standalone"`；
  - `app.js:6528 isStandaloneHttpMode()` —— 再兜底读 `<meta name="private-companion-mode">`；
  - `app.js:6539 isHttpApiMode()` = `isStandaloneHttpMode() || isDebugHttpMode()`（`isDebugHttpMode` 读 `?debug_http=1`，`:6509-6511`）；
  - `app.js:6550 httpApiRequestUrl()` —— standalone 走 `standaloneHttpApiBase()`（`app.js:6543-6548`，meta 里的 `/api/v1`），否则走 `HTTP_API`（插件页桥）。
- `standalone.js`（14,660 B）是**额外的那一层**：登录浮层 + 会话管理。入口判定 `standalone.js:8-19`（`markedStandalone` 一路 `||` 到 meta 判断），**`standalone.js:19` `if (!markedStandalone) return;`** —— 即：**同一份 static 目录，在插件页里 standalone.js 虽然被加载，但进来就 return，什么都不做**。
- 插件自己的文档也是这个口径：`docs/WORKING_PRINCIPLES.md:1911-1912`「**共存**：默认独立 WebUI 关闭；AstrBot 插件扩展页继续可用 · **业务复用**：独立入口复用原陪伴面板的业务处理函数和数据锁，**不会复制数据或启动额外轮询**」；`README.md:196-198`「### 独立 WebUI / 独立 WebUI 与 AstrBot 插件扩展页共存，默认关闭」。

> 一句话：**不是两套前端，是一套前端 + 两套传输**。`pages/companion-panel/` 是唯一真身；`(我们)` 那份同名目录 `pages/陪伴面板/` 是**逐字节相同的冗余副本**（`diff -rq` 退出码 **0**），而 `metadata.yaml:101` 只声明了 `companion-panel` —— 所以 `陪伴面板/` 在两条路上**都不会被服务**。

### 2.2 自有路由（11 条，`standalone_webui.py` 内注册）

| 路由 | 方法 | 行号 | 说明 |
|---|---|---|---|
| `/` | GET | `:505-507` | 陪伴面板入口（注入后的 HTML） |
| `/index.html` | GET | `:508-513` | 同上（另一个入口名） |
| `/app.css` | GET | `:530-535` | 面板样式 |
| `/app.js` | GET | `:536-538` | 面板主脚本 |
| `/standalone.js` | GET | `:539-544` | standalone 专属登录/会话层 |
| `/css/<path:filename>` | GET | `:545-550` | 面板 css 子目录 |
| `/js/<path:filename>` | GET | `:551-556` | 面板 js 子目录 |
| `/api/v1/auth/login` | POST | `:479-484` | 令牌换会话（唯一写会话的入口） |
| `/api/v1/auth/status` | GET | `:485-490` | 会话状态 |
| `/api/v1/auth/session` | GET | `:491-496` | 同一 handler（`self._auth_status`） |
| `/api/v1/auth/logout` | POST | `:497-502` | 吊销会话 |

错误处理（非路由，但行为可观察）：`404`（`:440-446`，`/api/v1` 前缀返 JSON `error=not_found`，否则纯文本）、`405`（`:448-458`）、`413`（`:460-462`）、`500`（`:464-476`，warning 记 path）。公共鉴权路径白名单 `PUBLIC_AUTH_PATHS`（`:54-60`）= login/status/session 三条，**logout 不在内**（要先有会话）。

### 2.3 业务路由 = **复用插件 Page API 的 197 条**，挂在 `/api/v1/*`

- 复用机制：`_register_page_api_routes()`（`:558-576`）遍历 `_page_route_bindings()`（`:578-603`），把每条路由**去掉桥前缀后**挂到 `{API_PREFIX}{suffix}`（`:565`）。
  - 优先走 `page_api.route_bindings()`（`:581-591`）—— 即 `page_api.py:1242-1441`；
  - 退化路径：若没有 `route_bindings`，就用 `_ContextRouteCollector`（`:144-166`）**临时替换 `plugin.context`** 去"钓"`register_web_api` 的调用（`:593-603`），再恢复原 context（`:602`）。**不会 mutate AstrBot**（`:145` 的文档串明说 "without mutating AstrBot"）。
- 前缀改写：`_standalone_route_suffix()`（`:926-932`）—— 把 `BRIDGE_API_PREFIX = "/astrbot_plugin_private_companion/page"`（`:51`）剥掉：
  - `/astrbot_plugin_private_companion/page/overview` → `/api/v1/overview`
  - 剥完是空或非 `/` 开头 → 返回 `""` → **跳过该条**（`:563-564`）。
- 与 `/auth/*` 冲突的处理：`:566-570` 会跳过并 `logger.warning("...跳过冲突的 Page API 路由: %s", rule)`。**实测这分支不会被触发** —— 我用 `page_api.py:1242-1441` 的 197 条做过前缀统计，`startswith("/auth")` 的 = **0 条**。
- **静态条数 197，与运行日志实测吻合**：`logs/serve.out` 最终那次启动（00:54:49）里 `插件登记 Web API` 中带 `astrbot_plugin_private_companion/page/` 前缀的行共 **197** 条（区间 `logs/serve.out:6183-6379`，197 条；紧接着 `:6380` 是 `[集成状态] 插件拓展页面 API 已注册`）。→ 这是"静态计数 == 运行时事实"的交叉验证。
- 明细：197 条 = **65 GET + 132 POST**（`/bookshelf/session` 同时支持 GET/POST，`page_api.py` 里是 `["GET", "POST"]`）。完整清单见 **附录 A**。

### 2.4 鉴权与安全（standalone 独有的部分）

| 机制 | 位置 |
|---|---|
| `/api/v1/*` 全量前置鉴权闸（`PUBLIC_AUTH_PATHS` 除外） | `:393-416`（`before_request`），失败 401 + `WWW-Authenticate: Bearer realm="private-companion"` |
| Cookie 来源的写方法要过 CSRF Origin 校验 | `:407-415`（`identity["source"]=="cookie"` 且 Origin 不合法 → 403 `origin_rejected`） |
| 支持两种凭据：长期令牌（Bearer）或短期会话（Bearer/Cookie） | `:708-745 _authenticate_request` |
| 令牌比较用**常量时间**：SHA-256 + `hmac.compare_digest` | `:868-873` + `:958-960` |
| 会话 Cookie：`HttpOnly` + `SameSite=Strict` + `path=/api/v1` | `:663-671`（`secure` 由 `:827-831 _cookie_secure()` 决定，https 时自动 True） |
| 登录限流：5 次失败 / 300 秒 | 常量 `:65-66`；实现 `:788-818`；超限返 429 + `Retry-After`（`:641-646`） |
| 登录体上限 4 KB（`Content-Length` 与实读**双查**） | `:62`、`:628-637` |
| 会话上限 512，满时逐出最旧 | `:64`、`:747-757` |
| 安全响应头：`nosniff` / `Referrer-Policy` / `X-Frame-Options: DENY` / CSP / `Permissions-Policy` | `:418-438`（`/`、`/index.html`、`/api/v1/*` 额外 `Cache-Control: no-store`，`:435-437`） |
| 静态路径目录穿越防护（`resolve()` + `relative_to(static_root)`） | `:857-866` |
| 请求体全局上限 24 MB | `:67`、`:302` |
| 令牌**不入 URL**（README 明说） | `README.md:212`「服务不接受 URL 查询参数中的访问令牌，避免令牌进入浏览历史、代理日志或分享链接」 |

⚠️ **`_LOGIN_PAGE_HTML`（`:75-141`）是死代码**：整个文件里只出现 1 次（就是定义那一行）。`GET /` 走的是 `_root()`（`:605-608`）→ `_load_panel_html()`，登录 UI 实际由 `standalone.js` 渲染。这是**插件自己的**遗留，不是我们的问题，但读源码时容易误以为登录页是那个 HTML。

---

## 3. 真 AstrBot 里它是什么形式、界面上有没有入口

### 3.1 形式：**插件内自带的 Quart + Hypercorn 独立服务，不走宿主派发**

- 自家 app：`standalone_webui.py:301`
  ```python
  app = _Quart("private_companion_standalone_webui", static_folder=None)
  ```
- 自家 server：`standalone_webui.py:338-349`
  ```python
  config = _HypercornConfig()
  config.bind = [self._hypercorn_bind()]        # :339 → :906-910 拼 f"{host}:{port}"
  config.use_reloader = False
  task = asyncio.create_task(_hypercorn_serve(app, config, shutdown_trigger=shutdown_event.wait),
                             name="private-companion-standalone-webui")
  ```
- 依赖声明：`requirements.txt` 里带 quart/hypercorn 的话就说明这是官方用法——但我们**实测到更强的证据**：真 AstrBot 的运行环境自带这两个包
  `/root/.local/share/uv/tools/astrbot/lib/python3.12/site-packages/` 下 `quart-0.22.0.dist-info`、`hypercorn-0.18.0.dist-info` 都在；我们的 `.venv` 里也是同版本（`quart 0.22.0` / `hypercorn 0.18.0`）。
- **与宿主派发是并行两路，不是二选一**：

| | 插件拓展页（宿主派发） | 独立 WebUI（自带服务） |
|---|---|---|
| 注册 | `page_api.py:1464-1468` `register(...)` 用 `plugin.context.register_web_api` | 直接 `app.add_url_rule`（`:571-576`） |
| 真 AstrBot 侧落点 | `astrbot/core/star/context.py:705-727` 存进 `registered_web_apis`；由 `dashboard/api/plugins.py:166-205 _match_registered_web_api` 正则匹配派发 | 无 —— 宿主**完全不知道**它存在 |
| 前缀 | `/astrbot_plugin_private_companion/page/*`（`page_api.py:222`） | `/api/v1/*`（`standalone_webui.py:50`） |
| 页面 | `dashboard/api/plugins.py:1171` `/plugins/{id}/pages/{name}` + `:1188` `.../assets/{path}` | 自己的 `/`（`:505-507`） |
| 鉴权 | AstrBot dashboard 的用户体系（`require_plugin_scope`） | 自己的令牌 + HttpOnly 会话（§2.4） |

- 两路**共用同一批 handler 对象**（`_page_route_bindings()` 直接拿 `page_api.route_bindings()` 的 handler，`:581-591`）→ 所以业务逻辑与数据锁只有一份，这是真的（`docs/WORKING_PRINCIPLES.md:1912` 的"业务复用"）。

### 3.2 真 AstrBot 界面上有没有入口：**没有**

- **真 AstrBot 源码全树 grep `standalone` / `6190` → 0 命中**（搜法：`grep -rni "standalone" --include=*.py /root/.local/share/uv/tools/astrbot/lib/python3.12/site-packages/astrbot/`，以及同目录 grep `6190`）。同目录 grep `private_companion` 也是 **0**（宿主对具体插件零特判）。
  → **结论：独立 WebUI 是纯插件自带，宿主不认识它，AstrBot 界面上不可能有入口。**
- AstrBot 界面**只**认 `metadata.yaml` 的 `pages:`（`metadata.yaml:100-102`：`pages: - name: companion-panel / title: 陪伴面板`），落到 `dashboard/api/plugins.py:1171-1200` 的两条路由。这条是插件拓展页，不是独立 WebUI。
- 用户怎么找到它：插件自己的 cfg hint（`_conf_schema.json:104`「开启后，插件会在独立端口提供不依赖 AstrBot 插件页 Bridge 的陪伴面板」）+ `README.md:196-201`（给 URL `http://127.0.0.1:6190/`）。**纯文档引导，无 UI 入口。**
- 插件文档对它的定位也是"可选管理入口"：`docs/ARCHITECTURE_RESET.md:102`「陪伴独立 WebUI | `private_companion/standalone_webui.py`，依赖 Quart/Hypercorn | **保留为可选管理入口**，复用 Gateway，不复制业务路由」。

### 3.3 与之相关的宿主契约（对我们有信息量的两条）

- `standalone_webui.py:322-324`：**`page_api` 必须可用**，否则独立 WebUI 拒绝启动 → 与 wiki 的「独立陪伴 WebUI 必须 Page API 可用」一致（`docs/WIKI_REVIEW_reference.md:65`）。
- `standalone_webui.py:381-384`：stop 时 `except Exception: pass` —— **静默吞异常**（本项目铁律里点名的那类写法），但只影响 shutdown，不产生功能性风险。

### 3.4 `stop()` 的行为（无条件执行，没有 enabled 判断）

`standalone_webui.py:367-391`：先 `shutdown_event.set()`，再 `wait_for(shield(task), 10s)`；超时 → warning + `task.cancel()`。**没有 `if not self.enabled: return`** —— 未启用时 `_serve_task` 是 `None`，两个分支都不进，只是把 `_sessions`/`_login_failures` 清空（`:389-391`），是 no-op。调用点 `plugin_lifecycle.py:215-222`。

---

## 4. 我们项目现在对它的处理

### 4.1 代码引用：**零**

搜了这些：`grep -rn "enable_standalone_webui" emotion_app/ host/ --include=*.py --include=*.js --include=*.html` → **0 命中**（唯一命中在数据文件 `host/data/plugins/config/astrbot_plugin_private_companion_config.json:16-20`，那是**数据**不是代码）。`grep -rn "standalone_webui" tools/` → 0 命中。`grep -rn "standalone_webui" host/*.py host/**/*.py` → 同上，只有那个 json。

→ **我们既没开它、也没关它、也没为它做任何适配。**它就是"潜伏在插件里的另一个监听面"。

### 4.2 端口：已让开（6190 → 6191）

| 项 | 位置 | 值 |
|---|---|---|
| 我们的默认端口 + 起因此注释 | `emotion_app/infra/config.py:58`（`port: int = 6191`）；注释 `:47-56` | `6191` |
| `.env` | `.env:10` | `EMOTION_SERVER_PORT=6191` |
| 运行时实测 | `python3 -c "from emotion_app.infra.config import ServerConfig; print(ServerConfig().port)"` | `6191` |
| 服务实况 | `ss -ltnp` | `0.0.0.0:6191`（`python` pid 3905246） |
| 待办留档 | `docs/NEXT_STEPS.md:357`（W1：情绪 2026-09-25 拍板「换端口」，✅ 已解决） | — |

注释原文（`emotion_app/infra/config.py:47-56`）：
```
#: 🔴 **默认端口必须避开陪伴体系**（2026-09-25 实测改，原为 6190）。
#: 起因：陪伴插件「独立 WebUI」的默认端口**就是 6190**
#: (`standalone_webui_port`)，与我们**完全撞车** →
...
#: 已占用的端口（实测 + 官方 wiki）：6190 插件独立 WebUI ·
#: 6314 screen WebUI · 6321 together 房间 · 6322 reality 网关 ·
#: 8765 nai 代理；本机另有 astrbot 的 6185/6199。
#: → 取 6191：紧挨原值便于记忆，且与上述全部不冲突。
```

### 4.3 我们面板（`emotion_app/web/static/`）上有没有入口：**没有**

- `grep -rni "6190|standalone|webui" emotion_app/web/static/` 只命中 **2 行注释**：
  - `plugin_page_bridge.js:8`「② HTTP 直连（`HTTP_API` 硬编码路径）—— 仅在 standalone/debug_http 模式」
  - `plugin_page_bridge.js:17`「而那个模式要求 URL 带 `?debug_http=1` 或 standalone 标记」
  这两句是在解释**为什么必须给插件页装同源桥**，不是"打开独立 WebUI"的入口。
- 我们面板的「插件」页只列 `metadata.yaml` 声明过的拓展页：`emotion_app/web/static/app.js:3523` `const d = await api("GET", "/api/v1/plugins/pages")`（键位声明在 `app.js:57`），配 `emotion_app/web/plugin_pages_web.py` 的三条路由 + `emotion_app/web/static/index.html:616` 的**同源 iframe**（`<iframe id="plugin-frame" class="plugin-frame">`，标题「插件拓展页」见 `index.html:586`）。
  > ⚠️ 这几处**行号会漂**：本轮调研期间有另一个子代在改我们面板（`emotion_app/web/static/{app.js,index.html,app.css}` 都在动）。上面是**我最后一次实测时**的行号；引用时以 grep 为准，别照抄行号。
- 实况日志：`logs/serve.out:6393`「发现 3 个插件拓展页：`astrbot_plugin_content_companion/qzone-companion`, `astrbot_plugin_private_companion/companion-panel`, `astrbot_plugin_together_companion/一起房间`」。
- 我们页面的服务端还会**注入我们自己的桥**（`plugin_pages_web.py:138-144`，`bridge_src="/static/plugin_page_bridge.js"`；helper 从 `:59 inject_page_context` 导入），并**只注入 `color-scheme`**，**不注入** `private-companion-mode` meta、**不发** `standalone.js` → 所以我们那里永远是"插件页模式"，插件也**不会**误判成 standalone。这点是**对的、不需要改**。

### 4.4 它现在的运行时状态：**未启动**（有正向证据链，不必只靠推理）

**正向证据链（三段，全部来自 `logs/serve.out` 的最终那次启动 00:54:49）**：

1. `:6380` `[集成状态] 插件拓展页面 API 已注册` —— 这行由 `integration_status.py:524` 打出，而**紧随其后的 `:530-536` 就是建 `StandaloneWebUIServer` 那段** → 证明 `self.standalone_webui` **不是 None**（真的建出来了）。
2. `:6419` `[主链] 主动消息循环已启动` —— 这行由 `main.py:8607` 打出，而 `main.py:8607` 到 `:8629`（`standalone_webui = getattr(...)` / `:8632 await standalone_webui.start()`）之间**没有任何 `return`**（我逐行看过 `main.py:8606-8639`）→ 证明 `start()` **确实被调用了**。
3. 于是 `start()` 在五道闸里被**第①道**拦下：`enable_standalone_webui=false` → `standalone_webui.py:314-316` → 只打一行 DEBUG → `return False`。
   **排他性验证**（这道推理能被证伪，所以我把它验了）：五道闸里**只有第①道是 DEBUG**，②～⑤全是 WARNING/Observable。而我在 `logs/serve.out` 里逐条数过：

   | 串 | 出处 | 命中数 |
   |---|---|---|
   | `独立 WebUI 未启用` | `standalone_webui.py:315`（DEBUG） | **0** |
   | `独立 WebUI 未启动` | `:319`/`:323`/`:327`/`:333`（WARNING，②～⑤） | **0** |
   | `独立 WebUI 已启动` | `:364`（INFO，成功） | **0** |
   | `独立陪伴 WebUI 启动失败` | `main.py:8635`（WARNING） | **0** |
   | `独立陪伴 WebUI 初始化失败` | `integration_status.py:536`（WARNING） | **0** |
   | `独立 WebUI 服务任务已结束` | `standalone_webui.py:921`（WARNING） | **0** |

   → ②～⑤ **一条 warning 都没有** + 成功那句 INFO 也没有 + 6190 没在听 = **唯一自洽的解释就是被第①道 DEBUG 拦下**。这不是"日志里查不到只好猜"，而是"用可见的那几道闸排除掉，剩下的必是它"。

**旁证（状态面）**：

- 配置值：`data/plugins/config/astrbot_plugin_private_companion_config.json:16-20`（`basic_config` 段）
  ```json
  "enable_standalone_webui": false,
  "standalone_webui_host": "127.0.0.1",
  "standalone_webui_port": 6190,
  "standalone_webui_access_token": "",
  "standalone_webui_session_ttl_hours": 24,
  ```
- `ss -ltnp` 实测：**6190 无监听**（`ss -ltn | awk '{print $4}' | grep -c ':6190$'` = **0**）。顺带对照：`6185`、`6199`、`6321` 在听（AstrBot 与 together 房间），`6314`/`6322`/`8765` 未监听（那三个扩展的 WebUI 也没开）。
- 依赖不缺：`standalone_webui.py:271-285 dependencies_available()` 在我们这边**会通过** —— `.venv/bin/python -c "import quart, hypercorn"` 两个都 OK（quart 0.22.0 / hypercorn 0.18.0，与服务进程同一套 venv，见 `/proc/3905246/cmdline` = `.venv/bin/python -m emotion_app serve`）。
- **判据提醒**：将来若真开它，"起了没"要用**肯定式**判据 —— 查有没有 `INFO [独立WebUI] 独立 WebUI 已启动: http://...`（`standalone_webui.py:364`），或者直接 `ss` 看 6190；**别去 grep「未启用」**，那句是 DEBUG，永远看不到。

### 4.5 一条**容易误判的日志**：`Config key removed: enable_standalone_webui …`

`logs/serve.out:5899-5903` 出现过：
```
00:54:49 INFO astrbot: Config key removed: enable_standalone_webui
00:54:49 INFO astrbot: Config key removed: standalone_webui_host
00:54:49 INFO astrbot: Config key removed: standalone_webui_port
00:54:49 INFO astrbot: Config key removed: standalone_webui_access_token
00:54:49 INFO astrbot: Config key removed: standalone_webui_session_ttl_hours
```
**根因已查到底，不是 bug、不需要动作**：
1. 输出来自 `host/astrbot_host/compat/core/config/astrbot_config.py:343`（`check_config_integrity` 的「参考配置里没有的键」分支，`:337-345`），调用点是**同一个 `__init__`** 的 `:223 def`（`has_new = self.check_config_integrity(default_config, conf)`）。
2. 日志顺序佐证：紧跟着的最后一行是 `Config key removed: wardrobe_outfits`（`:6180`），再下一行 `Config key order fixed`（`:6181`），然后是 **`[插件引导] 已将旧版扁平配置迁移到新版分组配置: 291 项`（`:6182`）**，再才是 `插件登记 Web API：['GET'] /astrbot_plugin_private_companion/page/overview`（`:6183`）。
3. 也就是说：那次是"**配置文件还是旧版扁平布局**"的现场 —— 顶层有 `enable_standalone_webui`，而当前 `_conf_schema.json` 把它放在 `basic_config.items`（`:14` `"type": "object"`、`:100`）里 → 顶层那 5 个键被判定"schema 里没有"→ 剔除 + 记日志；随后插件的引导逻辑把它们**迁进分组**。
4. **现在磁盘上已经是迁移后的正确布局**（我实测比对）：`basic_config` schema 86 项 == 文件 86 项（差异集为空）、顶层无任何 `standalone_*` 键、`Config key removed: basic_config` 出现 **0** 次。
   → 所以那 5 行是**历史现场**（本轮以前的一次启动），当前**不需要任何处理**。两处日志各出现 2 次（`:552` 那轮 + `:5899` 那轮），属同一模式。

---

## 5. 核实「6190→6191 是为了避开陪伴插件独立 WebUI 的默认端口」

**核实结论：注释与插件源码完全一致，注释是对的。**

| 事实 | 证据 |
|---|---|
| 插件默认 `standalone_webui_port` = `6190` | `standalone_webui.py:202`（含 `default=6190`）、`plugin_bootstrap.py:337`、`_conf_schema.json:118` |
| 插件默认地址 = `http://127.0.0.1:6190/` | `README.md:201`（`standalone_webui.py:263-269 url` 属性拼出来的形状一致） |
| 插件自己的测试把 6190 钉成判据 | `tests/test_standalone_webui.py:97` |
| 我们把默认端口改成 6191 | `emotion_app/infra/config.py:58`、`.env:10` |
| 改动理由正是这条 | `emotion_app/infra/config.py:47-51`、`docs/NEXT_STEPS.md:357`、`docs/WIKI_REVIEW_结论.md:21-24`、`docs/WIKI_REVIEW_reference.md:281` |

**没有任何不一致。** 只有一处**措辞**值得挑（不是错，是歧义）：

- `emotion_app/infra/config.py:54` 把 6190 列在「**已占用**的端口（实测 + 官方 wiki）」一句里。但实测 6190 **此刻是空闲的**（`Enabled=false` ⇒ `start()` 在 `standalone_webui.py:314-316` 就 return 了）。它准确的语义是「**插件一旦开启就会占的保留位**」。
- 影响面：只影响读注释的人对"现在谁占着"的判断，不影响任何行为。**建议不改**（除非将来有人据此排查端口，那句话会误导他先去看 6190 的进程）。若哪天要动，改成「**待占用（插件开关打开即占）**」即可。

---

## 6. 有没有「独立 WebUI 开关」这类配置项、名字与默认值

**有，共 5 项，全在 `_conf_schema.json` 的 `basic_config.items`（`:14` 声明为 `type: "object"`）。**

| 配置项 | 类型 | 默认值 | `condition` | 行号 | 备注 |
|---|---|---|---|---|---|
| `enable_standalone_webui` | bool | **`false`** | —（无条件显示） | `_conf_schema.json:100-105` | **总开关，默认关** |
| `standalone_webui_host` | string | `"127.0.0.1"` | `enable_standalone_webui: true` | `:106-114` | hint 明说「局域网访问需明确填写 `0.0.0.0`」 |
| `standalone_webui_port` | int | **`6190`** | 同上 | `:115-123` | hint「修改后需重载插件」 |
| `standalone_webui_access_token` | string | `""` | 同上 | `:124-134` | `password: true` + `obvious_hint: true`；hint「**至少 16 个字符**」 |
| `standalone_webui_session_ttl_hours` | int | `24` | 同上 | `:135-143` | 范围 1–168 |

配套实现（三处默认值必须一致，实测一致）：

- 读配置：`plugin_bootstrap.py:333-342`（`_cfg_bool(..., False)` / `_cfg_str(..., "127.0.0.1")` / `_cfg_int(..., 6190, 1, 65535)` / `_cfg_str(..., "")` / `_cfg_int(..., 24, 1, 168)`），位于 `_initialize_core_and_relationship_config`（`:290`；调用链 `:276-278 initialize_plugin_config` ← `main.py:4686`）。
- 取值兜底：`main.py:1954 _cfg_bool`（字符串 `"true"/"1"/"是"/"开"…` 也认，并会 `_set_into_config` 回写）；`helpers.py:1064-1091 _flat_get` 支持**扁平与分组两种布局**（先递归找 dict 子层，再退回顶层）—— 这正是为什么配置从扁平迁到 `basic_config` 后仍然读得到。
- 构造期默认值：`standalone_webui.py:188-206`（同样的 false / 127.0.0.1 / 6190）。

**默认是开还是关：关。** 两个独立证据面：schema 默认 `false`（`:103`）+ 我们磁盘配置文件实际值 `false`（`data/plugins/config/..._config.json:16`）+ 实测 6190 未监听。

### 6.1 ⚠️ 一个对我们最实际的坑：**令牌在面板上够不着**

插件有意把令牌做成"面板不可见、不可写"：

- `page_api.py:307-310`
  ```python
  # These values configure the page/API trust boundary itself. They must remain
  # available to AstrBot's native config editor, but must never be projected back
  # through the companion panel or accepted by its migration importer.
  PAGE_PRIVATE_CONFIG_KEYS = frozenset({"standalone_webui_access_token"})
  ```
- 生效点（我实测过归属，不是看行号猜的）：`page_api.py:23814` 与 `:23983-23984` **都在 `def _runtime_settings` 里**（该函数定义在 `:23253`）—— 前者从 `overview.settings` 的键清单里剔掉，后者 `values.pop(key)` 兜底；`:27086-27088` 在 **`def _allowed_setting_keys`**（定义 `:26529`）里 `difference_update` → 连 `/settings/update` 的写入口也一起关掉。
- 我实测了这 5 个键在**面板可写集合**里的归属（照 `_schema_key_index` 的 public 规则 + `PAGE_PRIVATE_CONFIG_KEYS` 复算）：`enable_standalone_webui` / `standalone_webui_host` / `standalone_webui_port` / `standalone_webui_session_ttl_hours` **4 个可经 `/settings/update` 写**，`standalone_webui_access_token` **被排除**。
- **但更前面一层就没通**：陪伴面板**根本没有渲染这 5 个键的 UI**。`pages/companion-panel/app.js` 里 `enable_standalone_webui` / `standalone_webui_host` / `standalone_webui_port` / `standalone_webui_session_ttl_hours` / `standalone_webui_access_token` 全部 **0 命中**（`grep -c` 逐个验过）；`const configLabels = {`（`app.js:1956`，702 条，块长 31,651 字符）里也没有它们。面板是**按固定清单**渲染配置项的（`renderConfig()` 在 `app.js:24283`，只调一组具名渲染函数如 `renderStorageConfig`/`renderFeatureSwitches`；`renderFeatureSwitches` 用 `featureGroups` + `Object.keys(featureDraft)` 的"其他"兜底），**不是按 schema 全量铺开**。→ 少数没进固定分组的开关会落进"其他"，但**前提是它先进了 `overview.features`**；而 `page_api.py:20945 _feature_flags`（178 项）里**没有** `standalone*` → 连"其他"都进不去。
  → 真要开这个独立 WebUI，**陪伴面板这条路走不通**，必须去 AstrBot 原生配置编辑页（真 AstrBot 的 `dashboard/api/plugins.py:721`/`:753` 那套）。
- **我们的面板**（`emotion_app/web/static/`）也没有对应的插件配置编辑入口：`emotion_app/web/app.py` 只挂了 `/api/v1/plugins`（列表）、`/api/v1/plugins/inactivated`（启停）与 `/api/plugins/extensions/<path>`（派发）三类，**没有**读写某个插件自己 `_conf_schema.json` 的配置表单（grep `config/schema`、`plugin_config` in `emotion_app/web/*.py` → 0 命中）。我们的面板是"schema 驱动"的，但驱动的对象是**我们自己的** `config_profile`（`emotion_app/web/api_admin.py:478-489 get_config_profile`，`:486` `from ..domain.config_profile import schema`），**不是插件的**。

---

## 7. 缺口与可动作项（**本次只调研，未改任何代码**）

按"实际会不会咬人"排序：

| # | 缺口 | 证据 | 影响 | 若要做 |
|---|---|---|---|---|
| **G1** | **令牌够不着 → 开了等于没开** | `page_api.py:310`、`:23814`/`:23983`/`:27086-27088`；面板 `app.js` 0 命中；我们面板无插件配置表单 | 即使把 `enable_standalone_webui` 设 true，令牌为空 → `standalone_webui.py:317-321` 直接拒启（只有一行 WARNING） | 要么不碰（现状最省事），要么在**我们面板**加一个「独立 WebUI」卡片，直写 `data/plugins/config/..._config.json`（⚠️ 写配置是**插件自己的领域**，改前先想清楚是否值得；且面板路由必须照隔壁行抄 `require_auth()`） |
| **G2** | **开启了没有重启/重载路径** | `_conf_schema.json:122` hint「修改后需重载插件」；`persona_config.py:427-436 _is_restart_required` 把 5 个键全标 `restart_required`（`hot_apply=False`，`:477-478`）；我们 `emotion_app/web/*.py` 里 grep `reload` **只在注释**（`app.py:994`），**没有** reload 路由；真 AstrBot 有 `core/star/star_manager.py:1010 async def reload()` | 用户改了开关 → 面板上不会生效，必须重启整个服务 | 现状「保存即落盘、需重启才生效」是**如实**的（`app.py:993-995` 注释也这么写）。若要补，机制是 `plugin_loader.load_all()` 同进程重载（`:598-618` 已支持 reload 语义），但属新能力，需单独评估 |
| **G3** | **"它到底起没起"在日志里查不到（负向）** | `standalone_webui.py:315` 只 `logger.debug`；我们 `logs/serve.out` DEBUG 行 = 0 | 常规 grep「独立WebUI」**必然 0 命中**，容易误判成"代码没跑到"（实测我第一遍就差点这么判） | 已给正向证据链（§4.4：`:6380` 建对象 + `:6419` 走到 start 调用点 + 6190 未监听）。判据用肯定式：`INFO [独立WebUI] 独立 WebUI 已启动`（`:364`）或 `ss` 看 6190 |
| **G4** | 若真开启：**端口冲突的失败形态** | `standalone_webui.py:339 config.bind=[host:port]`；失败回落在 `:355-362`（warning「启动失败: error_type=…」）与 `:919-923 _consume_serve_result`（warning「服务任务已结束: error_type=…」） | 两边都在 WARNING，可见；且 `:352-354`（`await asyncio.sleep(0)` 后查 `task.done()`）是**竞态**：bind 失败可能晚于这一 tick → `start()` 可能**返回 True 却并未监听** | 记住判据：冲突时**必须**能在日志里找到上述两条 warning 之一；**别只信"已启动"那句 INFO** |
| **G5** | `pages/陪伴面板/` 是**冗余副本** | `diff -rq pages/companion-panel pages/陪伴面板` 退出码 0（逐字节相同）；`metadata.yaml:101` 只声明 `companion-panel`；我们 `plugin_pages.py:89 discover_pages` 按 `metadata.yaml` 取名 | 3.4 MB 死重量（`项目更新.md:781` 已记 24 文件 / 3.4 M）。**两条路都不会服务它** | 只读发现，**不建议我们动**（那是插件源码镜像，我们是消费者；动了会与上游分叉） |
| — | 现状**无需动作**的两条 | §4.5 的 `Config key removed` 日志（历史迁移现场，磁盘已正确）；§4.3 我们页面**不注入** standalone 标记（这是对的，插件不会被误判） | — | — |

---

## 8. 搜过但**未找到**的东西（如实记录）

1. **命令行启动入口**：搜 `__main__` / `argparse`（全树 `.py`，排除 `tests/`）、`README.md` 里 `python -m`、`scripts/` 目录清单（21 个文件，全是 wardrobe/contracts/package/依赖门禁类）→ **未找到**任何能"起 standalone webui"的 CLI。唯一入口是 §1.3 的宿主调用链。
2. **真 AstrBot 侧对它的任何特判**：`grep -rni "standalone"` 与 `grep -rn "6190"`、`grep -rn "private_companion"` 于 `/root/.local/share/uv/tools/astrbot/lib/python3.12/site-packages/astrbot/`（全树 `.py`）→ **全部 0 命中**。
3. **一个现成的 `standalone.js` 服务端钩子/常量引用**：`_LOGIN_PAGE_HTML` 除定义外 0 引用（死代码，§2.4）—— 不是"未找到"，而是"找到但确认无用"。
4. **我们项目里任何"打开独立 WebUI"的代码/配置/入口**：搜 `6190` / `standalone` / `webui` / `WEBUI`（我们自己的 `emotion_app/`、`host/`、`tools/`、`.env`）→ 只有**注释与文档**（§4.1/§4.3），无实现。
5. **本机 `6190` 的监听进程**：`ss -ltnp | grep 6190` → 0 行（同时确认 6190 不在任何已知其他服务的端口清单里）。
6. **wiki 原文里关于独立 WebUI 的更细章节**：wiki 归档在 `/root/.dsh/attachments/v1/files/60/60f593…/陪伴wiki（只建议给ai使用）.7z`（**未解包、未展开**——本次只读范围限定在插件源码与已有审读报告）。已有审读结论在同目录 `docs/WIKI_REVIEW_reference.md:65`/`:121-126`/`:281`。**未找到**比"默认 false / host 127.0.0.1 / port 6190 / 令牌≥16"更细的官方行为描述（例如"port 被占时 start 的返回值"），所以 §7 G4 的竞态结论是**读源码得出的**，没有官方文档背书。

---

## 附录 A：独立 WebUI 的完整业务路由表（197 条，`/api/v1` + 下表路径）

> 来源：`page_api.py:1242-1441`（`route_bindings()` 的 197 条）。独立 WebUI 把它们挂成 `/api/v1<path>`（`standalone_webui.py:558-576` + `:926-932`）；插件拓展页把它们挂成 `/astrbot_plugin_private_companion/page<path>`（`page_api.py:222` + `:1464-1468`）。**同一批 handler，两个前缀。**
> 计数实测：65 GET + 132 POST（`/bookshelf/session` 为 GET/POST 双方法，此处按 2 条计）。

| # | 路径（前缀 `/api/v1`） | 方法 | handler |
|---|---|---|---|
| 1 | `/overview` | GET | `get_overview` |
| 2 | `/calendar` | GET | `get_calendar` |
| 3 | `/calendar/conflicts` | GET | `get_calendar_conflicts` |
| 4 | `/calendar/preview` | POST | `preview_calendar` |
| 5 | `/calendar/upsert` | POST | `upsert_calendar` |
| 6 | `/calendar/cancel` | POST | `cancel_calendar` |
| 7 | `/calendar/delete` | POST | `cancel_calendar` |
| 8 | `/calendar/candidates/confirm` | POST | `confirm_calendar_candidate` |
| 9 | `/calendar/candidates/reject` | POST | `reject_calendar_candidate` |
| 10 | `/extension-migration-notice` | GET | `get_extension_migration_notice` |
| 11 | `/extension-migration-notice/update` | POST | `update_extension_migration_notice` |
| 12 | `/task-prompts` | GET | `get_task_prompts` |
| 13 | `/task-prompts/update` | POST | `update_task_prompts` |
| 14 | `/expression-library` | GET | `get_expression_library` |
| 15 | `/expression-library/update` | POST | `update_expression_library` |
| 16 | `/expression-library/share` | POST | `share_expression_library` |
| 17 | `/expression-library/import/preview` | POST | `preview_expression_library_import` |
| 18 | `/expression-library/import/apply` | POST | `apply_expression_library_import` |
| 19 | `/users` | GET | `list_users` |
| 20 | `/user` | GET | `get_user` |
| 21 | `/user/update` | POST | `update_user` |
| 22 | `/user/delete` | POST | `delete_user` |
| 23 | `/user/identity/link` | POST | `link_unified_identity` |
| 24 | `/user/identity/unlink` | POST | `unlink_unified_identity` |
| 25 | `/user/identity/archive` | POST | `archive_unified_person` |
| 26 | `/user/identity/delete` | POST | `delete_unified_person` |
| 27 | `/user/identity/pending` | POST | `update_pending_identity_review` |
| 28 | `/user/identity/merge-preview` | POST | `preview_unified_identity_merge` |
| 29 | `/groups` | GET | `list_groups` |
| 30 | `/group` | GET | `get_group` |
| 31 | `/group/update` | POST | `update_group` |
| 32 | `/group/delete` | POST | `delete_group` |
| 33 | `/group/slang/update` | POST | `update_group_slang` |
| 34 | `/group/member-safety` | GET | `get_group_member_safety` |
| 35 | `/group/member-safety/action` | POST | `update_group_member_safety` |
| 36 | `/settings/update` | POST | `update_settings` |
| 37 | `/reality-touch` | GET | `get_reality_touch` |
| 38 | `/reality-touch/update` | POST | `update_reality_touch` |
| 39 | `/settings/swap_image_api` | POST | `swap_image_api_settings` |
| 40 | `/extensions/image/status` | GET | `get_image_extension_status` |
| 41 | `/image/debug` | GET | `get_image_debug` |
| 42 | `/image_api/status` | GET | `get_image_api_status` |
| 43 | `/image_api/test` | POST | `test_image_api_endpoint` |
| 44 | `/config/export` | GET | `export_migration_config` |
| 45 | `/config/backups` | GET | `list_migration_backups` |
| 46 | `/config/restore` | POST | `restore_migration_backup` |
| 47 | `/config/import/preview` | POST | `preview_migration_config_import` |
| 48 | `/config/import/apply` | POST | `apply_migration_config_import` |
| 49 | `/proactive_only/unlock` | POST | `update_proactive_only_unlock` |
| 50 | `/proactive/candidate/delete` | POST | `delete_proactive_candidate` |
| 51 | `/proactive/candidate/prune` | POST | `prune_proactive_candidates` |
| 52 | `/extensions/status` | GET | `get_extension_control_plane_status` |
| 53 | `/diagnostics` | GET | `get_diagnostics` |
| 54 | `/troubleshooting` | GET | `get_troubleshooting` |
| 55 | `/daily-review` | GET | `get_daily_review` |
| 56 | `/daily-review/run` | POST | `run_daily_review` |
| 57 | `/daily-review/guidance` | POST | `update_daily_review_guidance` |
| 58 | `/troubleshooting/warnings/update` | POST | `update_troubleshooting_warning_suppression` |
| 59 | `/troubleshooting/test` | POST | `run_troubleshooting_test` |
| 60 | `/token/stats` | GET | `get_token_stats` |
| 61 | `/token/reset` | POST | `reset_token_stats` |
| 62 | `/image_cache/list` | GET | `list_image_cache` |
| 63 | `/image_cache/preview` | GET | `get_image_cache_preview` |
| 64 | `/image_cache/preview_data` | GET | `get_image_cache_preview_data` |
| 65 | `/image_cache/thumbnail_data` | GET | `get_image_cache_thumbnail_data` |
| 66 | `/image_cache/update` | POST | `update_image_cache_item` |
| 67 | `/image_cache/delete` | POST | `delete_image_cache_item` |
| 68 | `/image_cache/bulk_delete` | POST | `bulk_delete_image_cache_items` |
| 69 | `/reaction_library/list` | GET | `list_reaction_library` |
| 70 | `/reaction_library/image_data` | GET | `get_reaction_library_image_data` |
| 71 | `/reaction_library/import` | POST | `import_reaction_library` |
| 72 | `/reaction_library/analyze` | POST | `analyze_reaction_library` |
| 73 | `/reaction_library/update` | POST | `update_reaction_library` |
| 74 | `/reaction_library/delete` | POST | `delete_reaction_library` |
| 75 | `/reaction_library/rescan` | POST | `rescan_reaction_library` |
| 76 | `/reaction_assets/list` | GET | `list_owned_reaction_assets` |
| 77 | `/reaction_assets/image_data` | GET | `get_owned_reaction_asset_image_data` |
| 78 | `/photo_reference/list` | GET | `list_photo_references` |
| 79 | `/photo_reference/image_data` | GET | `get_photo_reference_image_data` |
| 80 | `/photo_reference/upload` | POST | `upload_photo_reference` |
| 81 | `/wardrobe/describe` | POST | `describe_wardrobe_image` |
| 82 | `/wardrobe/outfit-preview` | POST | `preview_wardrobe_outfit` |
| 83 | `/wardrobe/drafts` | POST | `list_wardrobe_drafts` |
| 84 | `/wardrobe/draft-apply` | POST | `confirm_wardrobe_draft` |
| 85 | `/wardrobe/draft-reject` | POST | `reject_wardrobe_draft` |
| 86 | `/wardrobe/asset-image` | POST | `get_wardrobe_asset_image` |
| 87 | `/wardrobe/intent` | POST | `get_wardrobe_intent` |
| 88 | `/wardrobe/intent-clear` | POST | `clear_wardrobe_intent` |
| 89 | `/photo_reference/metadata/compile` | POST | `compile_photo_reference_metadata` |
| 90 | `/photo_reference/metadata/review` | POST | `review_photo_reference_metadata` |
| 91 | `/photo_reference/selection_trial` | POST | `run_photo_reference_selection_trial` |
| 92 | `/reference_asset/list` | GET | `list_reference_assets` |
| 93 | `/reference_asset/image_data` | GET | `get_reference_asset_image_data` |
| 94 | `/reference_asset/upload` | POST | `upload_reference_asset` |
| 95 | `/reference_asset/update` | POST | `update_reference_asset` |
| 96 | `/reference_asset/delete` | POST | `delete_reference_asset` |
| 97 | `/worldbook/member/reference/list` | GET | `list_reference_assets` |
| 98 | `/worldbook/member/reference/upload` | POST | `upload_reference_asset` |
| 99 | `/worldbook/member/reference/update` | POST | `update_reference_asset` |
| 100 | `/worldbook/member/reference/delete` | POST | `delete_reference_asset` |
| 101 | `/knowledge/reference/list` | GET | `list_reference_assets` |
| 102 | `/knowledge/reference/upload` | POST | `upload_reference_asset` |
| 103 | `/knowledge/reference/update` | POST | `update_reference_asset` |
| 104 | `/knowledge/reference/delete` | POST | `delete_reference_asset` |
| 105 | `/relationship/role/reference/list` | GET | `list_reference_assets` |
| 106 | `/relationship/role/reference/image_data` | GET | `get_reference_asset_image_data` |
| 107 | `/relationship/role/reference/upload` | POST | `upload_reference_asset` |
| 108 | `/relationship/role/reference/update` | POST | `update_reference_asset` |
| 109 | `/relationship/role/reference/delete` | POST | `delete_reference_asset` |
| 110 | `/photo_reference/assets` | GET | `list_photo_reference_assets` |
| 111 | `/photo_reference/assets/list` | GET | `list_photo_reference_assets` |
| 112 | `/photo_reference/assets/image_data` | GET | `get_photo_reference_asset_image_data` |
| 113 | `/photo_reference/assets/upload` | POST | `upload_photo_reference_asset` |
| 114 | `/photo_reference/assets/update` | POST | `update_photo_reference_asset` |
| 115 | `/photo_reference/assets/delete` | POST | `delete_photo_reference_asset` |
| 116 | `/daily_outfit/image` | GET | `get_daily_outfit_image` |
| 117 | `/daily_outfit/image_data` | GET | `get_daily_outfit_image_data` |
| 118 | `/bookshelf/unlock` | POST | `unlock_bookshelf` |
| 119 | `/bookshelf/session` | GET/POST | `get_bookshelf_session` |
| 120 | `/bookshelf/image` | GET | `get_bookshelf_image` |
| 121 | `/bookshelf/image_data` | GET | `get_bookshelf_image_data` |
| 122 | `/bookshelf/delete` | POST | `delete_bookshelf_item` |
| 123 | `/bookshelf/rate` | POST | `rate_bookshelf_item` |
| 124 | `/bookshelf/tags` | POST | `update_bookshelf_item_tags` |
| 125 | `/bookshelf/comments/update` | POST | `update_bookshelf_item_comments` |
| 126 | `/bookshelf/reading_state` | POST | `update_bookshelf_reading_state` |
| 127 | `/memo/list` | GET | `list_memo_notes` |
| 128 | `/memo/update` | POST | `update_memo_note` |
| 129 | `/qzone/status` | GET | `get_qzone_status` |
| 130 | `/qzone/health` | GET | `get_qzone_status` |
| 131 | `/qzone/summary` | GET | `get_qzone_status` |
| 132 | `/qzone/state` | GET | `get_qzone_status` |
| 133 | `/qzone/feed` | GET | `get_qzone_feed` |
| 134 | `/qzone/feeds` | GET | `get_qzone_feed` |
| 135 | `/qzone/list` | GET | `get_qzone_feed` |
| 136 | `/qzone/detail` | GET | `get_qzone_detail` |
| 137 | `/qzone/post` | GET | `get_qzone_detail` |
| 138 | `/qzone/item` | GET | `get_qzone_detail` |
| 139 | `/qzone/refresh_cookies` | POST | `refresh_qzone_cookies` |
| 140 | `/qzone/refresh-cookies` | POST | `refresh_qzone_cookies` |
| 141 | `/qzone/cookies/refresh` | POST | `refresh_qzone_cookies` |
| 142 | `/qzone/cookie/refresh` | POST | `refresh_qzone_cookies` |
| 143 | `/qzone/refresh` | POST | `refresh_qzone_cookies` |
| 144 | `/qzone/publish` | POST | `publish_qzone_post` |
| 145 | `/qzone/post/publish` | POST | `publish_qzone_post` |
| 146 | `/qzone/post` | POST | `publish_qzone_post` |
| 147 | `/qzone/like` | POST | `like_qzone_post` |
| 148 | `/qzone/post/like` | POST | `like_qzone_post` |
| 149 | `/qzone/comment` | POST | `comment_qzone_post` |
| 150 | `/qzone/post/comment` | POST | `comment_qzone_post` |
| 151 | `/qzone/delete` | POST | `delete_qzone_post` |
| 152 | `/qzone/post/delete` | POST | `delete_qzone_post` |
| 153 | `/creative/project` | GET | `get_creative_project` |
| 154 | `/creative/project/cover` | GET | `get_creative_project_cover` |
| 155 | `/creative/project/cover_data` | GET | `get_creative_project_cover_data` |
| 156 | `/creative/project/update` | POST | `update_creative_project` |
| 157 | `/creative/project/chunk/update` | POST | `update_creative_chunk` |
| 158 | `/creative/project/outline/update` | POST | `update_creative_outline` |
| 159 | `/creative/project/characters/update` | POST | `update_creative_characters` |
| 160 | `/creative/project/reanalyze` | POST | `reanalyze_creative_project` |
| 161 | `/creative/project/rebuild_memory` | POST | `rebuild_creative_memory` |
| 162 | `/creative/project/delete` | POST | `delete_creative_project` |
| 163 | `/worldbook/import` | POST | `import_worldbook` |
| 164 | `/worldbook/member/livingmemory` | GET | `get_worldbook_member_livingmemory` |
| 165 | `/worldbook/member/update` | POST | `update_worldbook_member` |
| 166 | `/worldbook/observations/clear` | POST | `clear_worldbook_pending_observations` |
| 167 | `/worldbook/group/update` | POST | `update_worldbook_group` |
| 168 | `/skill/update` | POST | `update_skill_growth` |
| 169 | `/personal_goal/update` | POST | `update_personal_goal` |
| 170 | `/food_menu/update` | POST | `update_food_menu` |
| 171 | `/food_menu/bulk_update` | POST | `bulk_update_food_menu` |
| 172 | `/food_menu/bulk_delete` | POST | `bulk_delete_food_menu` |
| 173 | `/external_ability/update` | POST | `update_external_ability` |
| 174 | `/setup/apply` | POST | `apply_setup_guide` |
| 175 | `/setup/daily/run` | POST | `run_setup_daily_generation` |
| 176 | `/daily/detail/regenerate` | POST | `regenerate_daily_detail_segment` |
| 177 | `/roleplay/personas` | GET | `list_roleplay_personas` |
| 178 | `/persona/migrate` | POST | `migrate_persona_profile` |
| 179 | `/persona/reset-current` | POST | `reset_current_persona` |
| 180 | `/persona/config-state` | GET | `get_persona_config_state` |
| 181 | `/persona/config/create` | POST | `create_persona_config` |
| 182 | `/persona/settings/update` | POST | `update_persona_settings` |
| 183 | `/persona/config/detach-preview` | POST | `preview_persona_config_detach` |
| 184 | `/persona/config/detach-apply` | POST | `apply_persona_config_detach` |
| 185 | `/roleplay/draft_from_persona` | POST | `generate_roleplay_draft_from_persona` |
| 186 | `/roleplay/standardize_persona` | POST | `standardize_persona_from_questionnaire` |
| 187 | `/roleplay/persona_style_scenarios` | POST | `generate_persona_style_scenarios` |
| 188 | `/roleplay/persona_style_scenario_retry` | POST | `retry_persona_style_scenario` |
| 189 | `/roleplay/persona_style_summary` | POST | `generate_persona_style_summary` |
| 190 | `/preset/apply` | POST | `apply_preset` |
| 191 | `/providers/available` | GET | `list_available_providers` |
| 192 | `/provider/test` | POST | `test_provider` |
| 193 | `/tts/providers` | GET | `list_tts_provider_configs` |
| 194 | `/tts/provider/create` | POST | `create_tts_provider_config` |
| 195 | `/tts/provider/clone` | POST | `clone_tts_provider_config` |
| 196 | `/tts/provider/update` | POST | `update_tts_provider_config` |
| 197 | `/tts/provider/test` | POST | `test_tts_provider_config` |
