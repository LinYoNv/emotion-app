/* core —— 由原单文件 app.js 拆分（**原生 ES 模块，零构建**）
 *
 * 🔴 本文件只放**函数/常量声明**，**没有任何模块级副作用**
 *    （事件绑定统一在 `js/bootstrap.js`）。
 *    这是为了消除循环依赖在"模块顶层立即求值"时拿到未初始化绑定的风险。
 */

import { loadChat, loadHistory, refreshUnread } from "../views/chat.js";
import { loadPersonas, renderPersonaList, selectPersona } from "../views/persona.js";
import { closePluginPage, loadPluginPages, loadPluginSwitch, openPluginConfig, openPluginDetail, openPluginPage, syncNavPluginActive } from "../views/plugins.js";
import { clearProviderDetail, isChatSource, loadProviderSources, loadProviderSourcesOnce, resetProviderSourcesOnce, saveProviderSource, selectProviderSource, updateProviderSourcesHint } from "../views/providers.js";
import { loadSessions, selectSession } from "../views/session.js";
import { loadConfigProfile, loadLogs, loadSettings, stopLogsAutoRefresh } from "../views/settings.js";


/* 情绪app 面板 —— 原生 JS，零构建工具（照 docs/REF_ASTRBOT_COMPANION_STACK.md §2.3 的选型） */

export const $ = (id) => document.getElementById(id);
export let state = {
  personas: [], activeId: "", current: null, sections: [],
  sessions: [], currentSessionId: "", currentSession: null,
  // 未读：`unread` = {会话id: 条数}（后端 `GET /api/v1/unread`），
  // `unreadTotal` = 侧边栏「聊天」那个圆圈显示的数字。
  // ⚠️ 只有**插件主动消息**会让它涨（见 `refreshUnread` 的注释）；
  //    读失败时**保持原值**，绝不清零（否则"读失败"会被显示成"已读完"）。
  unread: {}, unreadTotal: 0,
  // 模型供应商（两层：provider_sources + provider）—— 契约 docs/ASTRBOT_PROVIDER_CONTRACT.md
  providerSources: [],      // 端点层（列表接口已脱敏）
  selectedSourceId: "",     // 右侧当前展示的源
  creatingSource: false,    // 「+ 新增」态（空表单，保存才创建）
  configuredModels: [],     // 选中源下**已配置**的模型（源级路由拿的）
  sourceModelMetadata: {},  // 「获取模型列表」带回来的元数据（只读展示，不编造）
  availableModels: null,    // 该源的**可用**模型（null = 还没拉过）
  sourceEnable: true,       // 本页没有"停用源"的开关 → 保存时保持原值
  // 被**当前类型筛选**挡掉的数量（如实告诉用户，不静默丢数据）
  hiddenSourceCount: 0,
  // 源列表读取失败的原因（非空 = hint 正在显示这条错误，
  // 后续任何"重画 hint"的动作都必须让位，见 updateProviderSourcesHint）
  sourcesError: "",
  // ---- 类型筛选（契约 §5.1）----
  // 🔴 默认仍是 `chat_completion`（**有意不改默认行为**）：原来 `isChatSource()` 是
  //    写死的过滤，现在只是把它换成"可切换的筛选"，默认值原样保留。
  providerFilter: "chat_completion",
  // ---- 厂商预设（契约 §5.2）----
  // 🔴 `null` 与 `[]` **必须分开**：
  //    `null` = 还没拉过；`[]` = 真拉到了、后端就是没有预设。
  //    若失败也写成 `[]`，界面就会显示"没有预设" —— 这正是本项目最贵的静默失败类型。
  presets: null,
  presetsError: "",         // 非空 = 预设拉取失败的原因（必须显示出来，绝不静默当空列表）
  presetGroup: "",          // 预设弹层里当前选中的分组（provider_type）
  // 🔴 表单里**真实**的适配器类型（契约 §5.3/§5.4）。
  //    两个 OpenAI 格式走 `<select>`；预设带来的其他类型**只读展示**，
  //    但保存时必须原样送出去 —— 绝不悄悄降级成 `openai_chat_completion`。
  currentAdapterType: "openai_chat_completion",
  // 表单里真实的厂商标签（`provider`）。页面上**没有**独立的输入框 ——
  // 它由预设带进来（`openai`/`deepseek`/`microsoft`…），保存时原样提交。
  // 契约 §7.5：`provider` 只是厂商标签，**不参与执行决策**（决定能不能跑的是 `type`）。
  currentProvider: "openai",
  // 配置文件（会话 / 上下文管理策略）—— 契约 conductor/CONTRACT.md §4/§6
  cfgProfile: null,         // `GET /api/v1/config-profile` 的 profile（null = **还没读到**，不是"没有配置"）
  cfgSchema: null,          // 同一响应的 schema —— 渲染字段的**唯一**来源（后端是单一事实来源）
  // 日志页当前显示的那些条目（供「复制全部」用）
  logs: [],
  cfgModels: [],            // 真实**已配置**的模型（provider_select 的选项来源）
  cfgSourceTypes: {},       // 源 id → provider_type（provider_select 按类型过滤要用）
  cfgModelsError: "",       // 读模型失败的原因：非空时**不许**把"读失败"显示成"没有模型"
  // 插件启停（`GET /api/v1/plugins` 的整个响应；null = **还没读到**，
  // 不是"没有插件" —— 保存逻辑靠它判"读没读到"，所以两者必须分开）
  pluginSwitch: null,
  // 插件拓展页清单（`GET /api/v1/plugins/pages`；null = 还没读到）
  pluginPages: null,
  // 当前打开的插件详情（`GET /api/v1/plugins/<id>` 的响应；null = 没在看详情）
  pluginDetail: null,
  // 当前选中的插件 id（详情/拓展页用；空 = 在列表态）
  pluginCurrentId: "",
};

export const MODE_HINTS = {
  recent_only: "只保留最近 N 条原文，不注入摘要。适合短对话。",
  rolling_summary: "累积超过窗口后总结旧消息，之后携带摘要 + 最近原文。",
  semantic: "摘要 + 最近原文；system 只依赖人设，前缀稳定 → 上游缓存命中更好。",
};

/* ---------------- 通用请求 ---------------- */
export async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (!r.ok) {
    const msg = data?.error?.message || `HTTP ${r.status}`;
    const e = new Error(msg);
    e.status = r.status;
    e.type = data?.error?.type || "";
    // 🔴 把**出错字段名**也带出来（后端 400 会带 `error.field`）：
    //    配置文件页要把红框标在**对应的那个控件**上；只给一句"值不合法"，
    //    用户在几十个字段里根本找不到是哪一个。
    //    ⚠️ 我第一版漏了这一行 → 界面上"提示点名了字段"但红框一个都没有
    //       （判据 ④ 当场抓出来）。**错误信息在传递中被丢掉**是很容易犯的错。
    e.field = data?.error?.field || "";
    throw e;
  }
  return data;
}

export function toast(el, msg, bad) {
  el.textContent = msg;
  // 🔴 成功/进度**不写颜色**（`""` = 交还给样式表那一流的颜色），
  //    只有失败才落到语义令牌 `--danger`。
  //    原来这里写的是 `var(--muted)` —— 那是**把主题色抄进了 JS**：
  //    样式表改主题时这一处不会跟着变，且它给"已保存"这类**成功**消息
  //    上了一个次级文字色，语义也不对。清空才是让 CSS 保持单一来源。
  el.style.color = bad ? "var(--danger)" : "";
}

/* ---------------- 健康状态 ---------------- */
export async function refreshStatus() {
  const el = $("status");
  try {
    const h = await api("GET", "/health");
    el.textContent = `存储 ${h.storage.backend} · 会话 ${h.auth.active_sessions} · 聊天${h.chat_ready ? "就绪" : "未就绪"}`;
    el.className = "status ok";
  } catch (e) {
    el.textContent = "服务异常";
    el.className = "status bad";
  }
}

/* ================= 导航：hash 路由 + 侧边栏 =================
 *
 * 🔴 设计（情绪的要求）：
 *    「顶栏就不要了喵做成侧边栏模式可收起展开的那种。点开之后再路由到详情」
 *
 *   * 侧边栏可收起（桌面变图标条；手机变抽屉）
 *   * 用 **hash 路由**（`#/persona/<id>`）：所以"点开进详情"是**真的可后退、
 *     可刷新、可分享**的 URL，而不是一个内存里的 tab 变量。
 *     ⚠️ 为什么不用 history API：面板是**免构建的原生页面**，
 *        `pushState` 的 URL 一旦被直接打开（或刷新）需要服务端配合 rewrite；
 *        hash 不需要服务端任何支持，刷新也稳定。
 *
 *   路由表：
 *     #/chat            聊天
 *     #/session         会话（#/session/<id> 选中某个会话）
 *     #/persona         人设列表
 *     #/persona/<id>    人设详情 ← "点开之后再路由到详情"
 *     #/plugins         插件列表
 *     #/plugins/<id>/<page>  打开某个插件拓展页
 *     #/providers           模型供应商（提供商源列表）
 *     #/providers/<源id>     某个提供商源的详情 ← 同人设页的"点开路由到详情"
 *     #/settings        设置
 */

export const NAV_LOADERS = {
  // 🔴 聊天页**不能是空 loader**：刷新后 `state.currentSessionId` 丢了，
  //    而唯一调 `loadHistory()` 的路径是 `selectSession()`（只由「会话」页触发）
  //    → 刷新进来永远"没有聊天记录"，必须先去「会话」点一下。
  //    实测证据与边界见 `loadChat()` 的 docstring。
  chat: () => loadChat(),
  session: () => loadSessions(),
  persona: () => loadPersonas(),
  plugins: () => Promise.all([loadPluginSwitch(), loadPluginPages()]),
  // ⚠️ 供应商页的 loader 走 `loadProviderSourcesOnce()` 而不是直接调：
  //    `saveProviderSource()` 保存成功后**自己已经 load 过一次**了，
  //    紧接着它调 `selectProviderSource()` 改 hash → 触发 hashchange → applyRoute()
  //    → 这里**又** load 一次。实测后果（真浏览器抓到的）：
  //    第二次 load 会调 `updateProviderSourcesHint()`，把刚写好的
  //    「已新增 xxx。」**当场覆盖**成「当前筛选：语音（共 N 个）。」——
  //    用户看不到"保存成功"四个字，只看到列表数字变了。
  //    → 同一次路由内的重复读直接复用上一次的结果（幂等、也更省一次请求）。
  providers: () => loadProviderSourcesOnce(),
  // ⚠️ 设置页要拉**两样**：上游连接（本服务自己用）+ 配置文件（会话/上下文）。
  //    两个都失败也各自提示（`loadConfigProfile` 内部自己处理错误，不会互相吞）。
  settings: () => Promise.all([loadSettings(), loadConfigProfile()]),
  // ⚠️ 日志页的 loader **顺便管自动刷新的开关**：进来开、离开必须关 ——
  //    否则用户切走后定时器还在跑（白刷 + 用户看不到却仍在发请求）。
  //    停表在 `applyRoute`（见那里的 `stopLogsAutoRefresh`）。
  logs: () => loadLogs({ startAuto: true }),
};

/** 上一轮 `loadSettings()` 的 promise（供"只等一次"的调用方复用；见 loadSettings 注释）。 */
export let settingsLoadPromise = null;

/** 解析 `location.hash` → `{name, args[]}`。空 hash 默认 chat。 */
export function parseRoute() {
  const raw = (location.hash || "").replace(/^#\/?/, "");
  const parts = raw.split("/").filter(Boolean).map(decodeURIComponent);
  const name = parts.shift() || "chat";
  return { name, args: parts };
}

/** 切到某个页面（只负责"显示哪个 panel + 高亮哪个导航"）。 */
export function showPanel(name) {
  document.querySelectorAll(".panel").forEach((p) => p.classList.add("hidden"));
  const el = $("tab-" + name);
  if (el) el.classList.remove("hidden");
  else $("tab-chat").classList.remove("hidden");

  document.querySelectorAll(".nav-item").forEach((a) => {
    a.classList.toggle("active", a.dataset.nav === name);
    if (a.dataset.nav === name) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });

  // 🔴 侧边栏插件子项的选中态（情绪 2026-10-09）。
  //    ⚠️ 由**路由**决定，不在这里自己算：`showPanel("plugins")` 同时被
  //       「插件列表」与「某个插件页面」用到，只有 `location.hash` 分得清。
  //    ⚠️ 不在这里 `return` 或改动 `name` 的逻辑 —— 那只负责"哪个 panel 可见"。
  syncNavPluginActive();

  // 🔴 手机端顶栏标题跟随当前页（2026-09-27 加）。
  //    手机上抽屉是浮层、关着时看不见导航，若顶栏只写死"情绪app"，
  //    用户**没法知道自己现在在哪一页** —— 顶栏存在的意义就只剩个汉堡。
  //    ⚠️ 放在 `showPanel` 这个**唯一**的面板切换点，而不是各 `switchToTab` 调用处：
  //       十几处调用点各写一遍必然漂移（本项目反复踩）。
  const bar = $("mobile-bar-title");
  if (bar) {
    const active = document.querySelector(`.nav-item[data-nav="${name}"]`);
    const label = active ? (active.querySelector(".nav-label") || {}).textContent : "";
    bar.textContent = (label || "").trim() || "情绪app";
  }
}

/** 路由总入口：显示页面 + 按需拉数据 + 处理子参数（详情）。 */
export async function applyRoute() {
  const { name, args } = parseRoute();
  // ⚠️ 每次**路由入口**都复位供应商页的"只读一次"缓存：
  //    否则离开再回来会一直吃旧数据（用户手动在后端改了源、切页回来却看不到）。
  //    复位的是"是否读过"，不是数据本身 —— 页面重进就会真的重读。
  resetProviderSourcesOnce();
  // 🔴 离开日志页就**停表**（否则定时器会一直跑：用户看不见却仍在发请求）。
  if (name !== "logs") stopLogsAutoRefresh();
  showPanel(name);

  // 手机：点导航后自动收起抽屉（否则挡住了内容）
  if (isMobile()) closeDrawer();

  const loader = NAV_LOADERS[name];
  if (loader) {
    try { await loader(); } catch (e) { /* 各 loader 自己会提示 */ }
  }

  if (name === "persona") {
    const id = args[0];
    if (id) {
      if (!state.personas.length) { try { await loadPersonas(); } catch (e) {} }
      // 🔴🔴 **必须传 `skipHash: true`** —— 否则是**无限循环**
      //    （2026-09-21 实测抓到，服务端被自己人打爆）：
      //
      //      applyRoute(#/persona/<id>)
      //        └─ selectPersona(id)            ← 这里没传 skipHash
      //             └─ go("persona/<id>")      ← 又去写 location.hash
      //                  └─ hashchange
      //                       └─ applyRoute()  ← 回到第一行
      //
      //    ⚠️ 实测后果：进人设详情后**每 3 秒约 2000 次**
      //       `/api/v1/personas`（完全失控）；浏览器卡死、页面点不动，
      //       而且**一行报错都没有**（每次 fetch 都是 200）。
      //       我的真浏览器判据是因为"页面永远不静止"才撞见它的
      //       （`window.__fetchSeq` 一直涨）—— 单测/curl 都看不见。
      //
      //    `skipHash` 正是为断这个环而设（`loadPersonas` 里已有先例）：
      //    "刷新当前选中项的显示"不等于"导航"。
      await selectPersona(id, { skipHash: true });
    } else {
      // 回到列表：手机端退出"详情态"
      $("tab-persona").classList.remove("is-detail");
      $("persona-form").classList.add("hidden");
      $("sections-block").classList.add("hidden");
      $("persona-empty").classList.remove("hidden");
      state.current = null;
      renderPersonaList();
    }
  }

  if (name === "session") {
    const id = args[0];
    if (id) {
      if (!state.sessions.length) { try { await loadSessions(); } catch (e) {} }
      await selectSession(id);
    }
  }

  if (name === "plugins") {
    const [pid, page] = args;
    // 🔴 四态路由：`#/plugins` = 列表；`#/plugins/<id>` = 详情；
    //    `#/plugins/<id>/config` = **插件配置**（情绪 2026-10-09）；
    //    `#/plugins/<id>/<page>` = 插件拓展页 iframe。
    //    ⚠️ 详情那条必须**放在前面**判断（`page` 为空就是详情），
    //       否则 `#/plugins/<id>` 会落进 `else` 被当成"回列表"。
    //    ⚠️🔴 `config` 必须在**拓展页之前**判：否则会被当成一个叫 "config"
    //       的拓展页去打开（而那种页面不存在 → 用户看到"打不开"）。
    //       目前**没有**插件把页面起名叫 `config`（实测 7 个页面名都对不上），
    //       但**不能靠这个巧合** —— 插件是第三方代码，明天就可能有人这么起名。
    if (pid && page === "config") openPluginConfig(pid);
    else if (pid && page) openPluginPage(pid, page, { updateHash: false });
    else if (pid) openPluginDetail(pid, { updateHash: false });
    else closePluginPage();
  }

  if (name === "providers") {
    const id = args[0];
    if (id) {
      // 直接打开/刷新 `#/providers/<源id>` 也要能把详情load出来
      // （列表可能刚加载失败 → 再试一次，仍然失败就如实报错，不留白屏）
      //
      // 🔴 这里**必须看返回值**：`loadProviderSources()` 失败时是 `return false`
      //    而**不抛异常**，原来写成 `try { await … } catch {}` → 失败被吞掉，
      //    紧接着的 `selectProviderSource(id)` 在空列表里找不到该源，
      //    又用一句「找不到提供商源「x」。」**盖掉真正的根因**（后端 500 / 形状不符）。
      //    → 用户看到的是"这个源不存在"，真相是"列表根本没读出来"。
      //    **通则：返回 false 的函数不能当会抛异常的函数用。**
      if (!(state.providerSources || []).length) {
        const loadedOk = await loadProviderSources();
        if (!loadedOk) return;      // 根因已如实显示，别再覆盖它
      }
      await selectProviderSource(id, { skipHash: true });
    } else {
      // 回到列表态：手机端退出"详情"，右侧回到空态（**不是白屏**）
      clearProviderDetail();
    }
  }
}

/** 程序化导航（等价于点那个链接）。 */
export function go(path) {
  const next = "#/" + String(path || "").replace(/^#?\/?/, "");
  if (location.hash === next) applyRoute();
  else location.hash = next;
}


/* ---------------- 侧边栏：收起 / 展开 / 手机抽屉 ---------------- */

export function isMobile() {
  return window.matchMedia("(max-width: 640px)").matches;
}

/* 🔴 两个开关的 `aria-expanded` 必须**一起**更新。
 *
 * 为什么抽成一个函数：`#sidebar-toggle`（抽屉里）与 `#mobile-menu`（顶栏）是
 * **同一件事的两个入口**，各自 set 一遍必然漂移（本项目反复踩的
 * "同一概念多条写入路径"）—— 抽出单一来源，任一路径改状态都走它。 */
export function syncSidebarAria(expanded) {
  const v = expanded ? "true" : "false";
  ["sidebar-toggle", "mobile-menu"].forEach((id) => {
    const el = $(id);
    if (el) el.setAttribute("aria-expanded", v);
  });
}

export function openDrawer() {
  $("sidebar").classList.remove("collapsed");
  document.querySelector(".app").classList.add("drawer-open");
  $("sidebar-scrim").classList.remove("hidden");
  syncSidebarAria(true);
}

export function closeDrawer() {
  document.querySelector(".app").classList.remove("drawer-open");
  $("sidebar-scrim").classList.add("hidden");
  syncSidebarAria(false);
}

export function toggleSidebar() {
  if (isMobile()) {
    // 手机：抽屉开/关
    if (document.querySelector(".app").classList.contains("drawer-open")) closeDrawer();
    else openDrawer();
    return;
  }
  // 桌面：图标条 ↔ 完整
  const app = document.querySelector(".app");
  app.classList.toggle("collapsed");
  const collapsed = app.classList.contains("collapsed");
  syncSidebarAria(!collapsed);
  try { localStorage.setItem("emotion_sidebar_collapsed", collapsed ? "1" : "0"); } catch (e) {}
}


/* 🔴 手机顶栏的汉堡开关 —— 2026-09-27 修「没有开关，打不开侧边栏」。
 *
 * 为什么需要第二个开关：侧边栏里那个 `#sidebar-toggle` 住在抽屉**内部**，
 * 而手机端抽屉是 `translateX(-100%)` 跑到屏幕外的（实测 rect.x=-268）
 * ⇒ 抽屉关着时**用户手上没有任何入口**能把导航拉出来。
 * 顶栏这个开关在 `.content` 里，永远在视口内。
 *
 * ⚠️ 两个开关共用同一个 `toggleSidebar()` / `openDrawer()` / `closeDrawer()` ——
 *    不另写一份开关逻辑（两份必然漂移，本项目踩过：同一概念多条路径）。 */

// 记住桌面端的收起状态（刷新后保持）
try {
  if (localStorage.getItem("emotion_sidebar_collapsed") === "1" && !isMobile()) {
    document.querySelector(".app").classList.add("collapsed");
    syncSidebarAria(false);
  }
} catch (e) {}

// 手机端初次进入：抽屉默认关闭
if (isMobile()) closeDrawer();

// 视口变化时清理状态（手机↔桌面切换别留半开状态）

/* Esc：关抽屉 / 关弹层（键盘可达性）
 *
 * 🔴 顺序 = **从最上面那层往下关**（先关最后打开的那个）。
 *    ⚠️ 新增弹层必须**同时**加进这张表：漏了不会报错，只是那个弹层按 Esc 关不掉
 *      ——静默失效，本项目最忌讳的类型（判据里有一条专门按 Esc 验它）。
 * 🔴 刻意**不**关 `#login`（别"顺手补上"）：
 *    它是 fail-closed 的闸门，而页面上**没有第二个打开它的入口**
 *    （`showLogin()` 只在首次加载与登录失败时调）→ 未登录时按一下 Esc
 *    就会被永久挡在裸面板外面（数据全 401，却连输入令牌的地方都没有了）。
 *    这正是本项目踩过的"每条已登录路径都要显式关它"的镜像问题：
 *    闸门只能由"真的登录成功了"关闭，不能由键盘关闭。
 */

/* 兼容旧调用：`switchToTab("persona")` → 路由跳转 */
export function switchToTab(name) { go(name); }

// ---- 配置文件（会话 / 上下文管理策略）：两段各存各的，只发改动项 ----
// ⚠️ 这三条与「上游连接」**毫无关系** —— 我第一版删除时范围切宽了，
//    把这一整段连同日志页那 5 条一起切掉，重做时又漏补。
//    后果**完全静默**：配置页的「保存」按钮点了没反应、日志页刷新/复制全废，
//    而控制台一行错都不报（因为处理器压根没绑，不是绑错了）。
//    ⇒ 是 `tools/upstream_removed_check.py` 的 B2 判据抓住的
//      （它专门断言"点了之后**真发出请求**"，而不是"按钮存在"）。
// 🔴 原来这里还有一行 `$("cfg-context-form").onsubmit = ...` —— 那个表单已随
//    全局 context 段一起删除。**不能留**：元素不存在时 `$()` 返回 null，
//    读 `.onsubmit` 会**当场抛 TypeError**，而它在脚本顶层 ⇒
//    后面所有接线（重置按钮、日志页 5 条）全部不执行 —— **零报错、全静默**。
//    （第十八轮那次真实事故就是这个形状，见 `tools/upstream_removed_check.py`）

// ---- 日志：刷新 / 复制 / 自动刷新开关 ----
// 改级别/条数 → 立刻重拉（不用再点"立即刷新"，少一步）
// 勾/取消自动刷新：勾上就立刻开表（并马上拉一次，用户不用等 5 秒）

/* ================================================================================
 * 🔴 「上游连接」那一整块 JS **已删除**（情绪 2026-10-09）。
 *    连同删掉的四个处理器，它们只服务于那块已被移除的表单：
 *      * `$("fetch-models").onclick`    —— 「获取模型」（打 /api/v1/settings/models）
 *      * `$("settings-form").onsubmit`  —— 保存 API 地址/模型/key
 *      * `$("clear-overrides").onclick` —— 恢复环境变量默认
 *      * `$("ping").onclick`            —— 「测试连接」（打 /api/v1/settings/ping）
 *
 *    ⚠️ 为什么**必须一起删**（不能只删 HTML）：
 *       这些用的是 `$("x").onclick = ...` —— 元素不存在时 `$` 返回 null，
 *       直接取 `.onclick` 会**当场抛 TypeError**，把整个脚本后半段
 *       （模型供应商页、插件页、日志页的全部接线）一起带走。
 *       症状是"设置页之后的页面全都点不动"，控制台只有一行错，很难往这里想。
 *
 *   🔴 但**后端端点全部保留** —— 前端不调用 ≠ 要删掉后端能力：
 *      * `/api/v1/settings` 仍要读：它管着**当前激活人格**（`active_persona_id`）
 *      * `/settings/models` + `/settings/ping` 有完整回归测试
 *        （`tests/test_settings_ping.py` 18 条 + 注入验证 6/6），
 *        是"服务端代拉上游、key 不下发浏览器"的既有实现。
 *        将来「模型供应商」页要"测试这个源通不通"可直接复用，不必重写一套。
 *      * 配 key/地址/拉模型 → 归「模型供应商」页
 *        （`POST /api/v1/provider-sources/<源id>/models`，真打上游，见 api_providers.py:716）
 * ================================================================================ */

/* ================= 模型供应商（两层结构） =================
 *
 * 契约：`docs/ASTRBOT_PROVIDER_CONTRACT.md`（★ 从本机真跑的 AstrBot v4.27.5 实测抓的）。
 *
 * 🔴 两层结构 —— 这就是用户说「功能也不对」的根源（我原来做成了**单层**
 *    `{id,name,model,base_url,api_key}`，表达不了"一个端点挂多个模型"）：
 *
 *        provider_sources   ← 端点层：id + api_base + key + timeout + proxy + custom_headers
 *              ↑ 1 : N
 *        provider           ← 模型层：挂在某个源下面，id = `源id/模型id`
 *
 * 用到的路由（形状照 AstrBot）：
 *   GET    /api/v1/provider-sources                     列表（**脱敏**，只回 key_configured）
 *   POST   /api/v1/provider-sources                     新增
 *   GET    /api/v1/provider-sources/by-id?source_id=<id>       单条
 *   PUT    /api/v1/provider-sources/by-id?source_id=<id>       保存（key 留空 = **不修改**）
 *   DELETE /api/v1/provider-sources/by-id?source_id=<id>       删除
 *
 *   🔴 query 参数名**必须**与真实 AstrBot 一致（实测 `Query(...)`）：
 *      源 = `source_id`、模型 = `provider_id`。
 *      ⚠️ 我原来写的是 `?id=` → 后端 400「缺少 query 参数 source_id」
 *         → 保存功能**完全不可用**（而界面上只显示一句后端报错）。
 *   GET    /api/v1/provider-sources/<id>/models         「获取模型列表」
 *   GET    /api/v1/provider-sources/<id>/providers      该源**已配置**的模型
 *   POST   /api/v1/provider-sources/<id>/providers      给该源加一个模型
 *   DELETE /api/v1/providers/<urlencoded 源id/模型id>    移除某个模型
 *   POST   /api/v1/providers/<urlencoded 源id/模型id>/test  测试连通
 *   GET    /api/v1/provider-presets                     厂商预设（契约 §3；可带 ?provider_type= 过滤）
 *
 * 🔴 四条硬约定：
 *   1. **key 永不回显**。接口只给 `key_configured`，输入框**每次进来都清空**，
 *      占位符负责说明"已设置（留空 = 不修改）"。
 *   2. **类型筛选**（契约 §5.1）：左列表顶部可切 全部/对话/语音/嵌入，
 *      **默认仍是「对话」**（不改原行为）。hint 必须如实说"当前筛选：X（共 N 个）"，
 *      而且 N 必须**等于列表里真实渲染的条数** —— 数字对不上就是谎话。
 *   3. **能力诚实**（契约 §0/§5.2）：我们只能执行 `openai_chat_completion` /
 *      `openai_responses` 两种适配器。预设里的其他 `type` **能存不能跑**，
 *      界面必须显式标注「本平台暂不能执行该适配器，仅保存配置」，
 *      **绝不允许假装成功或静默降级**。
 *   4. **后端可能还没就绪**（并行开发中）→ 404/500/网络错误**必须显示给用户**，
 *      不许静默吞掉（项目铁律：静默失败比报错贵）。
 */

/** 后端错误 → 给用户看的一句话（404 特别标注，因为后端可能还没落地）。 */
export function explainProviderError(e) {
  const msg = (e && e.message) || "未知错误";
  if (e && e.status === 404) return `${msg}（HTTP 404：接口不存在 —— 后端可能还没就绪）`;
  if (e && e.status === 401) return `${msg}（HTTP 401：未登录或登录已过期）`;
  if (e && e.status >= 500) return `${msg}（HTTP ${e.status}：后端内部错误）`;
  return msg;
}

/** 取数：兼容 AstrBot 的 `{status,message,data}` 信封 **与**裸对象两种形状。
 *
 * 🔴 为什么必须做这一层（不是洁癖）：
 *    契约（docs/ASTRBOT_PROVIDER_CONTRACT.md §1）规定**照 AstrBot 的信封**，
 *    而本项目其它接口是裸对象。若只认一种形状，另一种会被读成 `undefined`
 *    → 界面显示"还没有提供商源" —— **这就是最贵的静默失败**（看着像没数据，
 *    实际是没读懂）。所以这里两种都认，且**形状不对就抛错**，绝不静默当空。
 */
export async function apiData(method, path, body) {
  const d = await api(method, path, body);
  if (d && typeof d === "object" && !Array.isArray(d)) {
    // AstrBot 信封：业务失败也可能带 HTTP 200 → 必须按 status 判失败
    if (typeof d.status === "string" && d.status !== "ok" && d.status !== "success") {
      const e = new Error(d.message || `后端返回 status=${d.status}`);
      e.status = 200;
      e.type = "envelope";
      throw e;
    }
    if (d.data && typeof d.data === "object" && !Array.isArray(d.data)) return d.data;
  }
  return d;
}

/** 从响应里取一个**必须是数组**的字段；不是数组就抛错（不静默当空）。 */
export function asArray(obj, field) {
  const v = obj ? obj[field] : undefined;
  if (v === undefined || v === null) return [];
  if (!Array.isArray(v)) throw new Error(`响应里 ${field} 不是数组（后端形状不符）`);
  return v;
}


/* ================= 启动 ================= */

/* ---------------- 登录闸门 ----------------
   🔴 为什么需要它（实测踩到）：面板页面本身**免鉴权**（只是壳），
      但所有数据接口都要鉴权 —— 而此前**界面上根本没有输入令牌的地方**，
      只能靠 CDP/curl 手动 POST /api/v1/auth/login 绕过。
      用户打开浏览器只会看到空列表 + 401，不知道该怎么办。
   令牌**不落任何浏览器存储**：登录后由服务端下发 HttpOnly cookie。 */
export async function isLoggedIn() {
  try {
    const r = await fetch("/api/v1/status");
    return r.ok;
  } catch { return false; }
}

export function showLogin(msg) {
  $("login").classList.remove("hidden");
  if (msg) {
    $("login-hint").textContent = msg;
    $("login-hint").style.color = "var(--danger)";
  }
  $("login-token").focus();
}

export function hideLogin() { $("login").classList.add("hidden"); }


/** 页面数据加载（登录成功后调用）。
 *
 *  ⚠️ **必须在这里隐掉登录浮层**（实测踩到）：
 *     浮层在 HTML 里默认可见（fail-closed：JS 挂了也不会露出裸面板），
 *     但"刷新时 cookie 还有效"这条路径**不经过表单提交** → 没人调 hideLogin()
 *     → 页面数据其实已加载，用户却被永久挡在登录框上。
 *     判据来自 tools/login_check.py 的第 4 项（真浏览器测出来的）。 */
export function boot() {
  hideLogin();
  refreshStatus();
  refreshUnread();
  // 先预热人设/会话（聊天页要用它们渲染下拉），再按当前 hash 落地页面。
  Promise.all([loadPersonas().catch(() => {}), loadSessions().catch(() => {})])
    .then(() => applyRoute())
    .catch(() => applyRoute());
  if (!window.__emotionTimer) {
    window.__emotionTimer = setInterval(refreshStatus, 30000);
  }
  // 🔴 未读**单独一个定时器**，且**只开一次**（`boot()` 可能被多次调用：
  //    登录成功 / 迟到解锁都会走它）—— 漏了这个守卫会叠出 N 个定时器。
  //
  // ⚠️ 与状态栏那个 30 秒不同步：主动消息到达的时刻是插件决定的，
  //    我们只能轮询；两者共用一个定时器会让"未读出现"的延迟变成
  //    最长 60 秒（两个 tick 才轮到一次），用户等不起。
  if (!window.__emotionUnreadTimer) {
    window.__emotionUnreadTimer = setInterval(refreshUnread, 15000);
  }
}

/** 🔴 **登录浮层的"迟到解锁"**（实测抓到的真 bug）。
 *
 *  场景：**在另一个标签页登录**（或 cookie 被其它方式置上）后回到本页 ——
 *  本页的 `isLoggedIn()` 早在加载时就判过"没登录"并弹了浮层，
 *  之后再没人重新判 → 数据其实是通的，**用户却永远卡在登录框上**。
 *
 *  这与 `login_check.py` 第 4 项抓到的"刷新时 cookie 仍有效"是**同一类**问题：
 *  浮层是 fail-closed 设计（默认可见，JS 挂了也不露裸面板），
 *  所以**每条"已登录"路径都必须显式关掉它**。
 *
 *  ⚠️ 只在浮层**可见时**才探，成功后走一次完整 `boot()`；已进入则直接返回。
 *  ⚠️ 监听器必须在**模块作用域**注册（实测踩到）：我一开始把它写在 `boot()` 里，
 *     而"没登录"时根本不会调 `boot()` → **监听器从来没装上**，修复形同虚设。
 *     这正是"写完模块+单测全绿却没接线"的老坑（本项目已固化 `test_assembly.py` 防它）。
 */
export async function recheckLogin() {
  if ($("login").classList.contains("hidden")) return;   // 已经进去了
  if (await isLoggedIn()) boot();
}
