"use strict";

/* 情绪app 面板 —— 原生 JS，零构建工具（照 docs/REF_ASTRBOT_COMPANION_STACK.md §2.3 的选型） */

const $ = (id) => document.getElementById(id);
let state = {
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

const MODE_HINTS = {
  recent_only: "只保留最近 N 条原文，不注入摘要。适合短对话。",
  rolling_summary: "累积超过窗口后总结旧消息，之后携带摘要 + 最近原文。",
  semantic: "摘要 + 最近原文；system 只依赖人设，前缀稳定 → 上游缓存命中更好。",
};

/* ---------------- 通用请求 ---------------- */
async function api(method, path, body) {
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

function toast(el, msg, bad) {
  el.textContent = msg;
  // 🔴 成功/进度**不写颜色**（`""` = 交还给样式表那一流的颜色），
  //    只有失败才落到语义令牌 `--danger`。
  //    原来这里写的是 `var(--muted)` —— 那是**把主题色抄进了 JS**：
  //    样式表改主题时这一处不会跟着变，且它给"已保存"这类**成功**消息
  //    上了一个次级文字色，语义也不对。清空才是让 CSS 保持单一来源。
  el.style.color = bad ? "var(--danger)" : "";
}

/* ---------------- 健康状态 ---------------- */
async function refreshStatus() {
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

const NAV_LOADERS = {
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
let settingsLoadPromise = null;

/** 解析 `location.hash` → `{name, args[]}`。空 hash 默认 chat。 */
function parseRoute() {
  const raw = (location.hash || "").replace(/^#\/?/, "");
  const parts = raw.split("/").filter(Boolean).map(decodeURIComponent);
  const name = parts.shift() || "chat";
  return { name, args: parts };
}

/** 切到某个页面（只负责"显示哪个 panel + 高亮哪个导航"）。 */
function showPanel(name) {
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
async function applyRoute() {
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
function go(path) {
  const next = "#/" + String(path || "").replace(/^#?\/?/, "");
  if (location.hash === next) applyRoute();
  else location.hash = next;
}

window.addEventListener("hashchange", () => { applyRoute(); });

/* ---------------- 侧边栏：收起 / 展开 / 手机抽屉 ---------------- */

function isMobile() {
  return window.matchMedia("(max-width: 640px)").matches;
}

/* 🔴 两个开关的 `aria-expanded` 必须**一起**更新。
 *
 * 为什么抽成一个函数：`#sidebar-toggle`（抽屉里）与 `#mobile-menu`（顶栏）是
 * **同一件事的两个入口**，各自 set 一遍必然漂移（本项目反复踩的
 * "同一概念多条写入路径"）—— 抽出单一来源，任一路径改状态都走它。 */
function syncSidebarAria(expanded) {
  const v = expanded ? "true" : "false";
  ["sidebar-toggle", "mobile-menu"].forEach((id) => {
    const el = $(id);
    if (el) el.setAttribute("aria-expanded", v);
  });
}

function openDrawer() {
  $("sidebar").classList.remove("collapsed");
  document.querySelector(".app").classList.add("drawer-open");
  $("sidebar-scrim").classList.remove("hidden");
  syncSidebarAria(true);
}

function closeDrawer() {
  document.querySelector(".app").classList.remove("drawer-open");
  $("sidebar-scrim").classList.add("hidden");
  syncSidebarAria(false);
}

function toggleSidebar() {
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

$("sidebar-toggle").onclick = toggleSidebar;
$("sidebar-scrim").onclick = closeDrawer;

/* 🔴 手机顶栏的汉堡开关 —— 2026-09-27 修「没有开关，打不开侧边栏」。
 *
 * 为什么需要第二个开关：侧边栏里那个 `#sidebar-toggle` 住在抽屉**内部**，
 * 而手机端抽屉是 `translateX(-100%)` 跑到屏幕外的（实测 rect.x=-268）
 * ⇒ 抽屉关着时**用户手上没有任何入口**能把导航拉出来。
 * 顶栏这个开关在 `.content` 里，永远在视口内。
 *
 * ⚠️ 两个开关共用同一个 `toggleSidebar()` / `openDrawer()` / `closeDrawer()` ——
 *    不另写一份开关逻辑（两份必然漂移，本项目踩过：同一概念多条路径）。 */
if ($("mobile-menu")) $("mobile-menu").onclick = toggleSidebar;

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
window.addEventListener("resize", () => {
  if (!isMobile()) {
    document.querySelector(".app").classList.remove("drawer-open");
    $("sidebar-scrim").classList.add("hidden");
  }
});

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
window.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (document.querySelector(".app").classList.contains("drawer-open")) { closeDrawer(); return; }
  // 下面这些同一时刻通常只开一个，但都要在表里（顺序不影响正确性，仍按"后开先关"排）
  if (!$("models-modal").classList.contains("hidden")) { closeModelsModal(); return; }
  if (!$("test-modal").classList.contains("hidden")) { closeTestModal(); return; }
  if (!$("preset-modal").classList.contains("hidden")) { closePresetPicker(); return; }
  if (!$("import-modal").classList.contains("hidden")) { $("import-modal").classList.add("hidden"); return; }
  if (!$("modal").classList.contains("hidden")) { $("modal").classList.add("hidden"); }
});

/* 兼容旧调用：`switchToTab("persona")` → 路由跳转 */
function switchToTab(name) { go(name); }

/* ================= 聊天 ================= */
const messagesEl = $("messages");

function addMsg(role, text, meta) {
  const div = document.createElement("div");
  div.className = "msg " + role;

  // 🔴 关键：正文放进**独立子元素**，而不是直接写在 bubble 上。
  //    起因（实测踩到）：流式更新用 `bubble.textContent = acc`，
  //    而 `textContent` 赋值会**清空所有子节点** → 已渲染的图片被抹掉。
  const body = document.createElement("span");
  body.className = "body";
  body.textContent = text;
  div.appendChild(body);

  if (meta) {
    const s = document.createElement("span");
    s.className = "meta";
    s.textContent = meta;
    div.appendChild(s);
  }
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

/** 取/建气泡里的正文容器（图片与正文互不干扰）。 */
function msgBody(bubble) {
  let body = bubble.querySelector(":scope > .body");
  if (!body) {
    body = document.createElement("span");
    body.className = "body";
    bubble.insertBefore(body, bubble.firstChild);
  }
  return body;
}

async function send() {
  const input = $("input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  addMsg("user", text);
  const bubble = addMsg("assistant", "");

  try {
    const r = await fetch("/v1/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: [{ role: "user", content: text }],
        stream: true,
        session_id: state.currentSessionId || undefined,
      }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(d?.error?.message || `HTTP ${r.status}`);
    }
    await readSSE(r, bubble);
  } catch (e) {
    bubble.className = "msg error";
    msgBody(bubble).textContent = "出错：" + e.message;
  }
  refreshStatus();
  refreshSessionInfo();
}

function renderImages(bubble, urls, caption) {
  const host = bubble || addMsg("assistant", "");
  msgBody(host);            // 确保正文容器存在且在图片之前
  for (const u of urls) {
    const img = document.createElement("img");
    img.className = "gen-image";
    img.src = u;
    img.alt = caption || "生成的图片";
    img.loading = "lazy";
    img.onclick = () => window.open(u, "_blank");
    host.appendChild(img);
  }
  if (caption) {
    const cap = document.createElement("span");
    cap.className = "meta";
    cap.textContent = caption;
    host.appendChild(cap);
  }
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

async function readSSE(response, bubble) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buf = "", acc = "", gotError = null, model = "";

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop();

    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      const payload = line.slice(6).trim();
      if (payload === "[DONE]") continue;
      let obj;
      try { obj = JSON.parse(payload); } catch { continue; }

      if (obj.error) { gotError = obj.error.message; continue; }
      if (obj.model) model = obj.model;

      // ⭐ 生成的图片（我们扩展的事件；标准字段之外的 images）
      if (Array.isArray(obj.images) && obj.images.length) {
        renderImages(bubble, obj.images, obj.caption || "");
      }
      const d = obj.choices?.[0]?.delta;
      if (d?.content) {
        acc += d.content;
        // ⚠️ 只更新 body，**不能**用 bubble.textContent（会清掉图片）
        msgBody(bubble).textContent = acc;
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }
    }
  }

  if (gotError) {
    bubble.className = "msg error";
    msgBody(bubble).textContent = "出错：" + gotError;
  } else if (!acc) {
    // ⚠️ 有图无文是**正常**情况（工具产出图，模型没说正文）——
    //    不能报"没有内容返回"，否则一张成功的图会被当成失败。
    const hasImage = !!bubble.querySelector("img");
    if (!hasImage) {
      bubble.className = "msg error";
      msgBody(bubble).textContent = "（没有内容返回）";
    }
  } else if (model) {
    const s = document.createElement("span");
    s.className = "meta";
    s.textContent = model;
    bubble.appendChild(s);
  }
}

$("send").onclick = send;
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});

/* ================= 人设 ================= */
async function loadPersonas() {
  try {
    const d = await api("GET", "/api/v1/personas");
    state.personas = d.data || [];
    state.activeId = d.active_persona_id || "";
    renderPersonaList();
    // ⚠️ `skipHash`：这里只是"刷新当前选中项的显示"，
    //    不是在导航 —— 否则每次 loadPersonas 都会重写 hash 触发循环。
    if (state.current) selectPersona(state.current.id, { skipHash: true });
    renderChatPersonaHint();
  } catch (e) {
    $("persona-list").innerHTML = `<li>加载失败：${e.message}</li>`;
  }
}

/** 聊天页那行"当前人设"提示 —— **必须按聊天真正用的优先级说**。
 *
 * 🔴🔴 原来这里只读全局 `state.activeId`，于是**说谎**（情绪 2026-09-21 报障
 *    "聊天页面显示不带人格"）：
 *
 *      会话绑的人设       = 糯米茨        ← 聊天**真的**会用它
 *      全局 active_persona_id = （空）
 *      → 面板显示 "未设当前人设（聊天将不带人设）"   ← 与实际行为矛盾
 *
 *    真实优先级在 `ChatService.resolve_persona`（`domain/chat.py:266`）：
 *      **显式 id > 会话绑定的人设 > 全局当前人设**。
 *    而 `app.py:432` 调它时**带了 `session_id`** → 会话绑定那层是生效的。
 *    → 所以提示必须照**同一个优先级**算，不能只看全局。
 *
 * ⚠️ 拿不到会话（无状态聊天 / 还没加载完）时**如实**说"全局/未设"，
 *    不能因为"不知道会话"就报"未设人设"。
 */
function renderChatPersonaHint() {
  const el = $("chat-hint");
  if (!el) return;
  const byId = (id) => state.personas.find((p) => p.id === id);

  // ① 会话绑定的人设（与后端 `resolve_persona` 第 2 层一致）
  const sess = state.sessions.find((s) => s.id === state.currentSessionId);
  const sessPid = sess ? (sess.persona_id || "") : "";
  if (sessPid) {
    const p = byId(sessPid);
    el.textContent = p
      ? `当前人设：${p.name}（本会话绑定）`
      // 会话绑的人设**已被删掉**：后端会记 WARNING 并回落全局
      // （`domain/chat.py:313`）—— 所以这里也要说清"回落到哪个"，
      // 不能让用户以为还在用那个已删的人设。
      : `本会话绑定的人设已不存在（${sessPid}）→ 将回落到${
          byId(state.activeId) ? `当前人设：${byId(state.activeId).name}` : "无人设"
        }`;
    return;
  }

  // ② 全局"当前人设"
  const act = byId(state.activeId);
  el.textContent = act
    ? `当前人设：${act.name}`
    : "未设当前人设（聊天将不带人设）";
}

function renderPersonaList() {
  const ul = $("persona-list");
  ul.innerHTML = "";
  if (!state.personas.length) {
    ul.innerHTML = '<li style="cursor:default;color:var(--muted)">还没有人设</li>';
    return;
  }
  for (const p of state.personas) {
    const li = document.createElement("li");
    li.textContent = p.name || "(未命名)";
    if (p.id === state.activeId) {
      const b = document.createElement("span");
      b.className = "badge";
      b.textContent = "当前";
      li.appendChild(b);
    }
    if (state.current && p.id === state.current.id) li.classList.add("active");
    li.onclick = () => selectPersona(p.id);
    ul.appendChild(li);
  }
}

async function selectPersona(id, opts) {
  const p = state.personas.find((x) => x.id === id);
  if (!p) return;
  state.current = p;
  renderPersonaList();

  // 🔴 "点开之后再路由到详情"：把选中的 id 写进 hash，
  //    这样返回键/刷新/分享链接都能回到同一个人设。
  if (!(opts && opts.skipHash)) go("persona/" + encodeURIComponent(id));

  // 手机端：列表收起、只显示详情（+ 返回按钮）
  $("tab-persona").classList.add("is-detail");

  $("persona-empty").classList.add("hidden");
  $("persona-form").classList.remove("hidden");
  $("sections-block").classList.remove("hidden");

  $("p-name").value = p.name || "";
  $("p-description").value = p.description || "";
  $("p-personality").value = p.personality || "";
  $("p-scenario").value = p.scenario || "";
  $("p-creator-notes").value = p.creator_notes || "";
  $("p-api-url").value = p.api_url || "";
  $("p-model").value = p.model || "";
  $("p-api-key").value = "";
  $("p-api-key").placeholder = p.api_key_configured ? "已设置（留空 = 不变）" : "留空 = 不变";

  state.sections = (p.prompt_sections || []).map((s) => ({ ...s }));
  renderSections();
  renderPersonaRef(p);
}

/* ================= 人设参考图（2026-09-25） =================
 *
 * 🔴 情绪：「用户能填的就让用户改」—— 参考图不该只存在于 `.env` 里
 *    （那要求用户登服务器改配置再重启，手机壳上更是不可能）。
 *
 * 三级优先（后端 `domain/persona_ref.py` 是唯一判据）：
 *   ① 模型显式给的路径 → ② **本页传的这张（per-persona）** → ③ `.env` 全局兜底
 *   → 所以本页的图**优先于** `.env`，只在"没上传"时才轮到全局那份。
 *
 * ⚠️ 预览走 `GET /personas/<id>/reference` 时**不能**用 `?t=时间戳` 之外的缓存参数，
 *    但必须带时间戳：同一路径换了图（覆盖上传）后浏览器会拿磁盘缓存显示旧图
 *    （aitag 踩过同类坑：删除后仍从缓存回 200）。后端已发 `no-cache`，
 *    这里再加一道时间戳，双保险。
 */
function personaRefUrl(id) {
  return `/api/v1/personas/${encodeURIComponent(id)}/reference?t=${Date.now()}`;
}

function renderPersonaRef(p) {
  const img = $("p-ref-preview");
  const hint = $("p-ref-hint");
  const del = $("p-ref-delete");
  const up = $("p-ref-upload");
  // 换人设时把上一张的暂存清掉（否则会把 A 的图传到 B 上）
  $("p-ref-file").value = "";
  up.disabled = true;

  if (p && p.has_reference_image) {
    img.src = personaRefUrl(p.id);
    img.classList.remove("hidden");
    hint.textContent = `已设置：${p.reference_image_path || "(文件名未知)"}`;
    del.disabled = false;
  } else {
    // 🔴 清 `src` 之外还要移除属性：`src=""` 在某些浏览器会去请求当前页面 URL
    img.removeAttribute("src");
    img.classList.add("hidden");
    hint.textContent = "未设置（生图时不会固定长相）";
    del.disabled = true;
  }
}

/** 当前选中的人设（列表里的对象，可能不是最新 —— 上传后要用返回值刷新）。 */
function refreshPersonaInState(p) {
  const i = state.personas.findIndex((x) => x.id === p.id);
  if (i >= 0) state.personas[i] = p;
  if (state.current && state.current.id === p.id) state.current = p;
  renderPersonaRef(p);
}

$("p-ref-file").onchange = () => {
  const f = $("p-ref-file").files && $("p-ref-file").files[0];
  $("p-ref-upload").disabled = !f;
  if (f) $("p-ref-hint").textContent = `待上传：${f.name}（${Math.round(f.size / 1024)} KB）`;
};

$("p-ref-upload").onclick = async () => {
  if (!state.current) return;
  const f = $("p-ref-file").files && $("p-ref-file").files[0];
  // ⚠️ 没选文件就**明确说**，不静默 return（本项目铁律：失败必须看得见）
  if (!f) { toast($("p-ref-hint"), "请先选择一张图片", true); return; }
  const fd = new FormData();
  fd.append("file", f);
  $("p-ref-upload").disabled = true;
  try {
    // ⚠️ 上传要走 `fetch` + FormData（不能用 `api()` —— 那个只发 JSON）
    const r = await fetch(`/api/v1/personas/${encodeURIComponent(state.current.id)}/reference`, {
      method: "POST", body: fd, credentials: "same-origin",
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((d.error && d.error.message) || `HTTP ${r.status}`);
    $("p-ref-file").value = "";
    $("p-ref-upload").disabled = true;
    // 🔴 成功后**重新渲染持久状态**，不要往这个元素里塞"已上传" ——
    //    `#p-ref-hint` 的**唯一含义**是"当前配置是什么"。
    //    塞瞬时文案会把它盖掉，用户传完反而看不到"现在用的是哪张图"
    //    （实测踩到：真浏览器判据里 hint 变成 '已上传'，
    //     「已设置：xxx.png」消失 —— 判据抓的就是这个）。
    //    反馈由**预览图出现**给出，那比一行字更直接。
    refreshPersonaInState(d.persona);
  } catch (e) {
    $("p-ref-upload").disabled = false;
    // 失败**必须留下**（错误信息是有用的，不还原成状态文案）
    toast($("p-ref-hint"), "上传失败：" + e.message, true);
  }
};

$("p-ref-delete").onclick = async () => {
  if (!state.current) return;
  if (!confirm("删除这个角色的参考图？（生图将不再固定长相）")) return;
  try {
    const d = await api("DELETE", `/api/v1/personas/${state.current.id}/reference`);
    // 同上：重新渲染持久状态，而不是塞"已删除"
    refreshPersonaInState(d.persona);
    if (!d.file_removed) {
      // ⚠️ 字段清了但文件本就不在 —— 这种情况要**说一句**，
      //    否则用户会以为"我明明删了它怎么没反应"（其实是对的，只是没什么可删）
      toast($("p-ref-hint"), "已清除配置（参考图文件本就不存在）");
    }
  } catch (e) { toast($("p-ref-hint"), "删除失败：" + e.message, true); }
};

//: 🔴 与后端 `prompt.PERSONA_CORE_TITLES` **逐字一致**（4 个）。
//:
//: 为什么前端也要知道这 4 个标题（情绪 2026-09-21：
//: 「图二的人设栏这个提示词片段可以不要了」）：
//:   片段表里那 4 行（角色设定/性格/场景/创作者备注）**不是用户写的内容** ——
//:   它们是从上面那 4 个输入框（描述/性格/场景/创作者备注）**现算**出来的，
//:   后端的 `merge_persona_sections()` 每次都按字段重算。
//:   → 表格里显示它们 = 同一份内容在同一个页面上出现**两遍**，
//:     而且改表格那行**不会**改到字段（下次保存又被打回）→ 纯误导。
//: ✅ 所以只渲染**真正的额外片段**（用户自己加的 / 非核心标题的）。
//: ⚠️ 标题一旦与后端漂移，这里会**漏过滤**（那 4 行又冒出来）；
//:    判据 `tools/chat_persona_check.py` 有一条守它。
const PERSONA_CORE_TITLES = ["角色设定", "性格", "场景", "创作者备注"];

/** 判断一个片段是不是"从 6 字段现算的核心片段"（= 不该在表里重复展示）。 */
function isDerivedCoreSection(s) {
  return (s.source || "") === "persona" && PERSONA_CORE_TITLES.includes(s.title || "");
}

function renderSections() {
  const tb = $("sections-body");
  tb.innerHTML = "";
  // 只显示**额外片段**（核心的 4 段由上方字段决定，见 PERSONA_CORE_TITLES 注释）
  const rows = state.sections
    .map((s, i) => ({ s, i }))
    .filter(({ s }) => !isDerivedCoreSection(s));

  // 空态：说清"为什么这里是空的"，别让人以为片段丢了
  if (!rows.length) {
    const tr = document.createElement("tr");
    tr.className = "empty-row";
    const td = document.createElement("td");
    td.colSpan = 6;
    // ⚠️ 手机端表头被 CSS 隐藏，字段名靠 `data-label` + `::before` 显示
    //    （见 `app.css` 窄屏规则）。空态行也**必须给**，
    //    否则手机上这格没有标签、看着像渲染坏了
    //    （`tools/mobile_check.py` 有判据盯着"卡片有没有字段名"）。
    td.dataset.label = "说明";
    td.textContent =
      "（没有额外片段）上面的描述/性格/场景/创作者备注就是人设的全部内容。" +
      "需要追加额外规则时点下面的「+ 添加片段」。";
    tr.appendChild(td);
    tb.appendChild(tr);
    return;
  }

  rows.forEach(({ s, i }) => {
    const tr = document.createElement("tr");

    // ⚠️ `data-label` 是给**手机端**用的：窄屏时表头被隐藏，
    //    改用 CSS `::before { content: attr(data-label) }` 显示字段名。
    const mkTd = (label, cls) => {
      const td = document.createElement("td");
      if (cls) td.className = cls;
      td.dataset.label = label;
      return td;
    };

    const tdOn = mkTd("启用", "narrow");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = s.enabled !== false;
    cb.onchange = () => { state.sections[i].enabled = cb.checked; };
    tdOn.appendChild(cb);

    const tdTitle = mkTd("标题");
    const ti = document.createElement("input");
    ti.type = "text"; ti.value = s.title || "";
    ti.oninput = () => { state.sections[i].title = ti.value; };
    tdTitle.appendChild(ti);

    const tdOrder = mkTd("order", "narrow");
    const oi = document.createElement("input");
    oi.type = "text"; oi.value = s.order ?? 100;
    oi.oninput = () => { state.sections[i].order = parseInt(oi.value, 10) || 0; };
    tdOrder.appendChild(oi);

    const tdSrc = mkTd("来源", "narrow");
    const sel = document.createElement("select");
    for (const opt of ["user", "persona", "builtin", "memory", "module"]) {
      const o = document.createElement("option");
      o.value = opt; o.textContent = opt;
      if ((s.source || "user") === opt) o.selected = true;
      sel.appendChild(o);
    }
    sel.onchange = () => { state.sections[i].source = sel.value; };
    tdSrc.appendChild(sel);

    const tdContent = mkTd("内容");
    const ta = document.createElement("textarea");
    ta.rows = 2; ta.value = s.content || "";
    ta.oninput = () => { state.sections[i].content = ta.value; };
    tdContent.appendChild(ta);

    const tdDel = mkTd("", "narrow");
    const db = document.createElement("button");
    db.textContent = "删除"; db.className = "danger";
    db.title = "删除这个片段";
    db.onclick = () => { state.sections.splice(i, 1); renderSections(); };
    tdDel.appendChild(db);

    tr.append(tdOn, tdTitle, tdOrder, tdSrc, tdContent, tdDel);
    tb.appendChild(tr);
  });
}

$("add-section").onclick = () => {
  state.sections.push({ title: "新片段", content: "", enabled: true, order: 100, source: "user", condition: "" });
  renderSections();
};

$("save-sections").onclick = async () => {
  if (!state.current) return;
  try {
    const p = await api("PUT", `/api/v1/personas/${state.current.id}/sections`, state.sections);
    Object.assign(state.current, p);
    toast($("chat-hint"), "片段已保存");
    loadPersonas();
  } catch (e) { toast($("chat-hint"), "保存失败：" + e.message, true); }
};

$("new-persona").onclick = async () => {
  const name = prompt("新角色卡的名字？", "新角色");
  if (!name) return;
  try {
    const p = await api("POST", "/api/v1/personas", { name });
    state.personas.unshift(p);
    selectPersona(p.id);   // 新建后直接进详情（会写 hash）
    renderPersonaList();
  } catch (e) { toast($("chat-hint"), "创建失败：" + e.message, true); }
};

/* 「← 返回列表」：手机端从详情退回列表 */
$("persona-back").onclick = () => {
  $("tab-persona").classList.remove("is-detail");
  state.current = null;
  go("persona");
};

$("persona-form").onsubmit = async (e) => {
  e.preventDefault();
  if (!state.current) return;
  const patch = {
    name: $("p-name").value,
    description: $("p-description").value,
    personality: $("p-personality").value,
    scenario: $("p-scenario").value,
    creator_notes: $("p-creator-notes").value,
    api_url: $("p-api-url").value,
    model: $("p-model").value,
  };
  // 只在真填了 key 时才提交 —— 留空表示"不改"
  const k = $("p-api-key").value;
  if (k) patch.api_key = k;

  try {
    const p = await api("PATCH", `/api/v1/personas/${state.current.id}`, patch);
    state.current = p;
    const i = state.personas.findIndex((x) => x.id === p.id);
    if (i >= 0) state.personas[i] = p;
    renderPersonaList();
    $("p-api-key").value = "";
    $("p-api-key").placeholder = p.api_key_configured ? "已设置（留空 = 不变）" : "留空 = 不变";
    toast($("chat-hint"), "已保存");
  } catch (e2) { toast($("chat-hint"), "保存失败：" + e2.message, true); }
};

$("activate").onclick = async () => {
  if (!state.current) return;
  try {
    await api("POST", `/api/v1/personas/${state.current.id}/activate`);
    await loadPersonas();
  } catch (e) { toast($("chat-hint"), "设置失败：" + e.message, true); }
};

$("dup").onclick = async () => {
  if (!state.current) return;
  try {
    await api("POST", `/api/v1/personas/${state.current.id}/duplicate`, {});
    await loadPersonas();
  } catch (e) { toast($("chat-hint"), "复制失败：" + e.message, true); }
};

/* ================= 导入 / 导出 =================
   格式依据是爱语真 dex 实测（见 爱语/docs/PERSONA_IMPORT_FORMAT.md）：
   爱语备份 `{"data":{"prompts":{…}}}` **一个文件可含多个人设**。
   ⚠️ 手机上没有拖拽，所以入口是**文件选择器 + 粘贴框**两条。
   ⚠️ 导出用 `fetch` + `Blob` 触发下载 —— 直接 `<a href>` 带不了鉴权头。 */

$("import-persona").onclick = () => {
  $("import-result").classList.add("hidden");
  $("import-hint").textContent = "";
  $("import-hint").style.color = "";
  $("import-modal").classList.remove("hidden");
};

$("import-close").onclick = () => $("import-modal").classList.add("hidden");
$("import-modal").onclick = (e) => {
  if (e.target.id === "import-modal") $("import-modal").classList.add("hidden");
};

$("import-form").onsubmit = async (e) => {
  e.preventDefault();
  const hint = $("import-hint");
  const file = $("import-file").files[0];
  const text = $("import-text").value.trim();
  const conflict = $("import-conflict").value;

  if (!file && !text) {
    hint.textContent = "请选一个文件，或粘贴内容";
    hint.style.color = "var(--danger)";
    return;
  }

  toast(hint, "导入中…");
  try {
    let r;
    if (file) {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("conflict", conflict);
      r = await fetch("/api/v1/personas/import", { method: "POST", body: fd });
    } else {
      r = await fetch("/api/v1/personas/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, conflict }),
      });
    }

    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      hint.textContent = d?.error?.message || `HTTP ${r.status}`;
      hint.style.color = "var(--danger)";
      return;
    }

    // 组装结果摘要 —— 逐条列出**导入了什么**，不让用户猜
    const lines = [`✅ 导入成功：${d.count} 个人设（格式 ${d.source_format}）`];
    for (const p of d.personas || []) lines.push(`   · ${p.name}`);
    if (d.overwritten?.length) lines.push(`♻️ 已覆盖：${d.overwritten.join("、")}`);
    if (d.skipped?.length) lines.push(`⏭️ 已跳过（同名）：${d.skipped.join("、")}`);
    if (d.has_avatar) lines.push("🖼️ 已提取头像");
    for (const n of d.notes || []) lines.push(`ℹ️ ${n}`);
    if (d.extra_fields?.length) {
      lines.push(`ℹ️ 未导入的字段：${d.extra_fields.join("、")}`);
    }
    $("import-result").textContent = lines.join("\n");
    $("import-result").classList.remove("hidden");
    toast(hint, `已导入 ${d.count} 个`, false);

    $("import-file").value = "";
    $("import-text").value = "";
    await loadPersonas();
  } catch (err) {
    hint.textContent = "导入失败：" + err.message;
    hint.style.color = "var(--danger)";
  }
};

/** 下载导出的 JSON（带鉴权头，所以不能用 <a href>）。 */
async function downloadExport(url, fallbackName) {
  const r = await fetch(url);
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(d?.error?.message || `HTTP ${r.status}`);
  }
  const blob = await r.blob();
  // 文件名优先用服务端 Content-Disposition（爱语用 persona_<名字>_<时间戳>.json）
  let name = fallbackName;
  const cd = r.headers.get("Content-Disposition") || "";
  const m = cd.match(/filename="?([^"]+)"?/);
  if (m) name = m[1];

  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

$("export-persona").onclick = async () => {
  if (!state.current) return;
  const fmt = confirm(
    "导出格式：\n\n" +
    "「确定」= 爱语备份格式（给爱语用 / 做备份）\n" +
    "「取消」= SillyTavern 角色卡（与角色卡生态互通）"
  ) ? "aiyu" : "sillytavern";
  try {
    await downloadExport(
      `/api/v1/personas/${state.current.id}/export?format=${fmt}`,
      `${state.current.name}.json`
    );
    toast($("chat-hint"), "已导出");
  } catch (e) { toast($("chat-hint"), "导出失败：" + e.message, true); }
};

$("del").onclick = async () => {
  if (!state.current) return;
  if (!confirm(`删除「${state.current.name}」？不可恢复。`)) return;
  try {
    await api("DELETE", `/api/v1/personas/${state.current.id}`);
    state.current = null;
    $("persona-form").classList.add("hidden");
    $("sections-block").classList.add("hidden");
    $("persona-empty").classList.remove("hidden");
    await loadPersonas();
  } catch (e) { toast($("chat-hint"), "删除失败：" + e.message, true); }
};

$("preview").onclick = async () => {
  if (!state.current) return;
  try {
    const d = await api("GET", `/api/v1/personas/${state.current.id}/preview`);
    showModal(`提示词预览 · ${state.current.name}（${d.describe.total_chars} 字符 / ${d.describe.section_count} 段）`, d.text);
  } catch (e) { toast($("chat-hint"), "预览失败：" + e.message, true); }
};

/* ================= 会话 ================= */
async function loadSessions() {
  try {
    const d = await api("GET", "/api/v1/sessions");
    state.sessions = d.data || [];
    renderSessionList();
    renderSessionSelect();
    renderUnreadBadge();
    if (state.currentSessionId) selectSession(state.currentSessionId);
  } catch (e) {
    $("session-list").innerHTML = `<li>加载失败：${e.message}</li>`;
  }
}

/* ---------------- 未读（侧边栏「聊天」那个圆圈） ----------------
 *
 * 🔴 语义（与后端 `Session.unread` 的注释是**同一份约定**）：
 *   只有**插件主动消息**（陪伴在她自己的时间发过来的那些）会 +1；
 *   用户自己发的消息不算 —— 他此刻就在这个页面上看着。
 *   打开某个会话（`selectSession`）→ 该会话清零。
 *
 * ⚠️ 为什么要**轮询**：主动消息是插件在服务端后台产生的，
 *    浏览器这边没有任何事件可听（我们不做 WebSocket / SSE 推送）。
 *    30 秒一轮与状态栏那次刷新共用节奏，代价是一个很小的 GET。
 *    ⚠️ 离开页面也要继续（用户就是为了"不在时也能看到红点"才要它），
 *       所以**不像日志页那样停表**。
 */
async function refreshUnread() {
  try {
    const d = await api("GET", "/api/v1/unread");
    state.unread = d.sessions || {};
    state.unreadTotal = d.total || 0;
    renderUnreadBadge();
    renderSessionList();
  } catch (e) {
    // 静默：这只是个提示徽标，读不到就不显示，绝不用它打断用户
    // （⚠️ 但**不把已有数字清零** —— 那会把"读失败"伪装成"已读完"）
  }
}

function renderUnreadBadge() {
  const el = $("chat-badge");
  if (!el) return;
  const n = state.unreadTotal || 0;
  if (n > 0) {
    el.textContent = n > 99 ? "99+" : String(n);
    el.classList.remove("hidden");
  } else {
    el.classList.add("hidden");
  }
}

/** 聊天页的 loader —— **刷新后要能自己恢复上一次看的那条会话**。
 *
 * 🔴🔴 守的 bug（情绪 2026-09-21 报障「聊天页面刷新后初次点进去没有聊天记录，
 *    需要从『会话』点进去才有」）：
 *
 *    原来这里是 `chat: () => {}`（**空函数**）→ 刷新后 `#messages` 永远是空的。
 *    实测（真浏览器，记录所有 fetch）：
 *
 *      刷新后停在聊天页 → messages 子节点 0，`/messages` 请求 **[]**（一次都没发）
 *      去「会话」点一条   → messages 子节点 8，`/messages` 请求 1 次
 *
 *    根因：`state.currentSessionId` 只在内存里，**刷新即丢**；
 *    而唯一会调 `loadHistory()` 的路径是 `selectSession()`，
 *    它只由「会话」页点击 / 下拉 change / 路由带 id 触发 —— **都不在刷新路径上**。
 *
 * ✅ 修法：聊天页 loader 在"还没选中会话"时**自动选中最近的一条**。
 *    ⚠️ 三条边界：
 *      ① `state.sessions` 可能还没加载（`boot()` 是 Promise.all 后再 applyRoute，
 *         但 loader 也可能被单独调用）→ 先确保加载过，**不许假设**；
 *      ② **已经有选中会话时不动它**（否则用户在聊天页切页回来会被重置）；
 *      ③ 一条会话都没有（无状态聊天）→ **如实不选**，不发 `/messages`（那会 404）。
 */
async function loadChat() {
  if (!state.sessions.length) {
    await loadSessions();
  }
  if (state.currentSessionId) return;      // ② 已有选中，尊重它
  if (!state.sessions.length) return;      // ③ 真的没有会话
  // ① 选最近的一条（后端 `list()` 已按 updated_at 倒序）
  await selectSession(state.sessions[0].id);
}

function renderSessionList() {
  const ul = $("session-list");
  if (!ul) return;
  ul.innerHTML = "";
  if (!state.sessions.length) {
    // ⚠️ 空态有专门的块（`#session-empty`），这里**不再**插一行占位 ——
    //    两处都显示"还没有会话"就是同一句话说两遍。
    return;
  }
  for (const s of state.sessions) {
    const li = document.createElement("li");
    const main = document.createElement("div");
    main.className = "sess-main";
    const title = document.createElement("span");
    title.className = "sess-title";
    title.textContent = s.title || "(未命名)";
    main.appendChild(title);

    const meta = document.createElement("span");
    meta.className = "badge";
    // 🔴 把「这条会话用哪个模型」显示出来（情绪要的就是"每条会话各自的模型"）。
    //    ⚠️ 没选时如实说「未选模型」 —— 不能显示成某个具体模型名，
    //       那会让用户以为已经选好了（而聊天会明确拒绝）。
    const modelLabel = (s.model || "").trim()
      ? s.model
      : "未选模型";
    meta.textContent = `${s.message_count} 条 · ${s.policy.mode} · ${modelLabel}`;
    if (!(s.model || "").trim()) meta.classList.add("no-model");
    main.appendChild(meta);

    // 未读小标（与消息数那个 badge 并排；只在该会话有未读时出现）
    const unread = (state.unread || {})[s.id] || s.unread || 0;
    if (unread > 0) {
      const u = document.createElement("span");
      u.className = "badge unread-badge";
      u.textContent = String(unread);
      u.title = `${unread} 条未读`;
      main.appendChild(u);
    }
    li.appendChild(main);

    // 🔴 **点一下就进聊天**（情绪 2026-10-09：「单击所选会话跳转到聊天」）。
    //    配置不再在这里改 —— 全搬到「设置 → 配置文件 → 会话」。
    li.onclick = () => openSessionInChat(s.id);
    li.title = "点一下进这条会话聊天";

    // 删除：放在行**内部**（而不是全局一个"删除当前"按钮）——
    //   因为这一页不再有"当前选中的会话"这个概念了，删除必须指名道姓。
    const del = document.createElement("button");
    del.type = "button";
    del.className = "danger sess-del";
    del.textContent = "删除";
    del.title = "删除这条会话（消息会一起删掉，不可恢复）";
    del.onclick = (e) => {
      // 🔴 必须挡住冒泡：否则会**先触发 li 的 onclick 跳进聊天**，
      //    用户看到的是"点了删除却进了聊天"（静默做错事）。
      e.stopPropagation();
      deleteSessionById(s);
    };
    li.appendChild(del);

    if (s.id === state.currentSessionId) li.classList.add("active");
    ul.appendChild(li);
  }
  // 列表有内容时隐藏空态
  if ($("session-empty")) $("session-empty").classList.toggle("hidden", !!state.sessions.length);
}

/** 点会话 → 进聊天（「会话」页唯一的主动作）。 */
function openSessionInChat(id) {
  state.currentSessionId = id;
  selectSession(id);
  switchToTab("chat");
}

/** 按 id 删会话（会话页行内按钮 + 设置页的删除按钮共用）。
 *
 * 🔴 抽成一个函数是刻意的（本项目「同一概念多条写入路径」已踩 6 次）：
 *    两处入口必须做**同一件事** —— 同一句确认文案、同一个失败提示、
 *    同一次列表刷新。复制一份必然漂移。
 */
async function deleteSessionById(s) {
  const name = s.title || "(未命名)";
  if (!confirm(`删除会话「${name}」？\n\n它里面的 ${s.message_count} 条消息会一起删掉，且不可恢复。`)) {
    return;
  }
  try {
    await api("DELETE", `/api/v1/sessions/${s.id}`);
    // 删掉的正好是聊天页当前那条 → 清掉，否则聊天页会去读一个不存在的会话
    if (state.currentSessionId === s.id) {
      state.currentSessionId = "";
      state.currentSession = null;
    }
    await loadSessions();
    showHint($("session-page-hint"), `已删除「${name}」`);
    showHint($("cfg-session-hint"), `已删除「${name}」`);
  } catch (e) {
    showHint($("session-page-hint"), "删除失败：" + e.message, true);
    showHint($("cfg-session-hint"), "删除失败：" + e.message, true);
  }
}

/** 往提示元素里写一句话（`bad=true` 用危险色）。
 *  ⚠️ 与 `toast()` 的区别：这个**不依赖元素存在**（搬页面时最容易漏的就是某个
 *     hint 元素已被移走）—— 找不到元素就静默跳过，绝不因此抛错。
 */
function showHint(el, msg, bad) {
  if (!el) return;
  el.textContent = msg;
  el.style.color = bad ? "var(--danger)" : "";
}

function renderSessionSelect() {
  const sel = $("session-select");
  if (!sel) return;
  sel.innerHTML = "";
  if (!state.sessions.length) {
    const o = document.createElement("option");
    o.value = ""; o.textContent = "（无会话 · 无状态聊天）";
    sel.appendChild(o);
    return;
  }
  for (const s of state.sessions) {
    const o = document.createElement("option");
    o.value = s.id;
    o.textContent = `${s.title || "(未命名)"}（${s.message_count}）`;
    if (s.id === state.currentSessionId) o.selected = true;
    sel.appendChild(o);
  }
}

/** 设置页那个「选择会话」下拉（情绪 2026-10-09：Q1 选 A「加个下拉框」）。
 *
 * 🔴 **与聊天页的 `#session-select` 是两个独立控件**，刻意不复用：
 *    聊天页那个管"我现在跟谁聊"，这个管"我正在配谁"。共用一个的话，
 *    去设置里配 B 会把聊天对象也换掉（用户会看到聊天页莫名其妙变了）。
 */
function renderSessionPicker() {
  const sel = $("cfg-session-picker");
  if (!sel) return;
  const keep = sel.value;          // 保住用户当前选的那条（重绘不该把他弹走）
  sel.innerHTML = "";
  if (!state.sessions.length) {
    const o = document.createElement("option");
    o.value = ""; o.textContent = "（还没有会话 · 先用右边「+ 新建会话」开一条）";
    sel.appendChild(o);
    return;
  }
  for (const s of state.sessions) {
    const o = document.createElement("option");
    o.value = s.id;
    const model = (s.model || "").trim();
    o.textContent = `${s.title || "(未命名)"}（${s.message_count} 条${model ? " · " + model : " · 未选模型"}）`;
    sel.appendChild(o);
  }
  // 优先保住用户刚选的；否则用当前会话；再否则第一条
  const want = keep || state.currentSessionId || state.sessions[0].id;
  if (state.sessions.some((s) => s.id === want)) sel.value = want;
}

/** 渲染「对话模型」下拉 —— **复用 `makeProviderSelect`**，不另写一套。
 *
 * 🔴 为什么必须复用（本项目铁律：同一概念不许两条会漂移的路径）：
 *    那个函数已经处理了两个边界，而它们**各自都出过事**：
 *      ① 模型被删了 → 显式补一个"（已不在已配置模型里）"的选项，
 *         不让 select **静默跳到"不指定"**（那会让用户以为是他自己没选）；
 *      ② 一个模型都没配 → 区分"读失败"和"真的没有"，
 *         两者给用户的下一步动作完全不同。
 *    复制一份必然漏掉其中一条。
 */
function renderSessionModelField(s) {
  const host = $("cfg-session-model-fields");
  if (!host) return;
  host.innerHTML = "";
  host.appendChild(
    buildConfigField(
      "session",
      {
        key: "model",
        label: "这条会话用哪个模型",
        type: "provider_select",
        provider_type: "chat_completion",
        hint: "留空 = 这条会话还没选模型，聊天会明确提示你去选（不会偷偷用别的）。",
      },
      s.model || "",
    ),
  );
  // ⚠️ `buildConfigField` 生成的 id 是 `cfg-session-model` —— 但那是**每条会话各自**
  //    的字段，用 id 取它会在切会话时读到上一条的旧值。所以这里**只靠 DOM 引用**，
  //    每次重绘都新造一个，不存在"残留"。
  const el = host.querySelector("select");
  if (el) {
    el.dataset.sessionModel = "1";
    el.onchange = () => saveSessionModel(s.id, el.value);
  }
}

/** 保存"这条会话用哪个模型"（选了就立刻存，不需要再点保存）。
 *
 * 🔴 为什么**立刻存**而不是跟"保存"按钮一起提交：
 *    模型是这条会话的**关键属性**，用户选完就以为生效了。
 *    要再点一次"保存"的话，他很可能直接去聊天 —— 然后被拒绝（因为没存）。
 *    当时的观感会是"我明明选了模型，它说我没选"。
 */
async function saveSessionModel(sessionId, modelId) {
  const hint = $("cfg-session-model-hint") || $("cfg-session-hint");
  try {
    await api("PATCH", `/api/v1/sessions/${sessionId}`, { model: modelId || "" });
    state.currentSession = state.sessions.find((x) => x.id === sessionId) || state.currentSession;
    if (state.currentSession && state.currentSession.id === sessionId) {
      state.currentSession.model = modelId || "";
    }
    const s = state.sessions.find((x) => x.id === sessionId);
    if (s) s.model = modelId || "";
    showHint(hint, modelId ? `已保存：这条会话用 ${modelId}` : "已清空：这条会话没有选模型");
    renderSessionPicker();
    renderSessionList();
  } catch (e) {
    showHint(hint, "保存模型失败：" + e.message, true);
  }
}

async function selectSession(id) {
  const s = state.sessions.find((x) => x.id === id);
  if (!s) return;
  state.currentSessionId = id;
  state.currentSession = s;

  // ⚠️ 配置表单**已经搬到「设置」页**（情绪 2026-10-09），所以这里不再
  //    "显示表单" —— 它常驻在设置页。这里只负责**把数据灌进去**
  //    （用户切到设置页时要看到的是**当前这条会话**的配置）。
  // 🔴 全部用 `showHint` / 存在性判断：搬页面最容易漏的就是某个元素，
  //    而 `$("x").value = ...` 在元素不存在时会**当场抛错**，
  //    把 `selectSession` 后面的 `loadHistory`（聊天记录）一起带走。
  //    实测这正是"搬完页面聊天不显示历史"的成因。
  fillSessionForm(s);

  renderSessionList();
  renderSessionSelect();
  renderSessionPicker();
  // 🔴 切会话会改变"聊天实际用哪个人设"（会话绑定那层）→ 提示必须跟着重算。
  //    这是 `selectSession` 收敛全部切换入口的好处：只在这里接一次就够
  //    （下拉 / 列表点击 / 路由 / 新建后选中 都走这里）。
  renderChatPersonaHint();
  await loadHistory(id);
  refreshSessionInfo();
  await markSessionRead(id);
}

/** 把一条会话的数据灌进**设置页那个表单**。
 *
 * 🔴 抽出来是为了让"哪些元素可能不存在"只在一处处理 —— 搬页面时
 *    `selectSession` 里二十行裸 `$("x").value` 是全项目最容易断的地方。
 * ⚠️ 每个取值都先确认元素在，再赋值；缺一个**不抛错**（其余字段照样填）。
 */
function fillSessionForm(s) {
  const set = (id, v) => {
    const el = $(id);
    if (el) el.value = v;
  };
  set("s-title", s.title || "");
  // 🔴 插件用户 ID = **umo 第三段**（方案 B）：陪伴按它认人，
  //    记忆/关系/主动消息开关都挂在这一个值下面。
  //    ⚠️ 用 `??` 而不是 `||`：只有 null/undefined 才回落空串 ——
  //       空串本身仍然是**有意义**的（存量会话还没绑标签，走全局默认）。
  set("s-user-id", s.user_id ?? "");
  // 把**完整 umo** 显示出来（后端算好的，前端绝不自己拼 ——
  // 平台 id 属于宿主 `host/astrbot_umo` 的唯一定义）。
  if ($("s-umo-hint")) {
    toast(
      $("s-umo-hint"),
      s.umo ? `完整 umo：${s.umo}` : "这个会话还没有 umo（保存一个用户 ID 后就有了）",
    );
  }
  const policy = s.policy || {};
  set("s-mode", policy.mode || "recent_only");
  set("s-tail", policy.protected_tail ?? 20);
  set("s-trigger", policy.summary_trigger ?? 40);
  // 🔴 总结模型 / 总结提示词（情绪 2026-10-09 的会话级两项）
  set("s-summary-instruction", policy.summary_instruction || "");
  set("s-summary", s.summary || "");
  if ($("mode-hint")) {
    $("mode-hint").textContent = MODE_HINTS[policy.mode] || "";
  }

  // 人设下拉
  const sel = $("s-persona");
  if (sel) {
    sel.innerHTML = '<option value="">（当前人设）</option>';
    for (const p of state.personas) {
      const o = document.createElement("option");
      o.value = p.id; o.textContent = p.name;
      if (p.id === s.persona_id) o.selected = true;
      sel.appendChild(o);
    }
  }

  // 模型下拉（复用 provider_select 那套控件，见 renderSessionModelField）
  renderSessionModelField(s);
  // 总结模型下拉（同一套控件，独立的一个 select）
  renderSessionSummaryModelField(s);
}

/** 渲染「总结用哪个模型」下拉。
 *
 * 🔴 复用自己的 `makeProviderSelect` —— **不写第二套**（本项目「同一概念不要多条路径」）。
 * ⚠️ 与「对话模型」是**两个独立控件**：都叫 provider_select，但存进 policy 的不同键。
 */
function renderSessionSummaryModelField(s) {
  const host = $("s-summary-model");
  if (!host) return;
  const policy = s.policy || {};
  // ⚠️ `makeProviderSelect(f, value)` 收的是**字段对象**（不是类型字符串），
  //    返回的是**包着 select 的 span**，不是 select 本身
  //    —— 我第一版按 `makeProviderSelect("chat_completion", ...)` 写，是**编造签名**
  //    （本项目铁律：别编造 API）。这里照 `renderSessionModelField` 的真实用法。
  const box = makeProviderSelect(
    { key: "summary_model", provider_type: "chat_completion" },
    policy.summary_model || "",
  );
  host.innerHTML = "";
  host.appendChild(box);
  const sel = box.querySelector("select");
  if (sel) {
    // ⚠️ 去 id（会与「对话模型」那个撞），只靠 DOM 引用 —— 每次重绘都新造，无残留
    sel.removeAttribute("id");
    sel.dataset.sessionSummaryModel = "1";
    sel.onchange = () => saveSessionSummaryModel(s.id, sel.value);
  }
  if ($("s-summary-model-hint")) {
    $("s-summary-model-hint").textContent = "留空 = 用这条会话的对话模型。";
  }
}

/** 立刻保存"总结用哪个模型"（与「对话模型」同样的即时语义）。 */
async function saveSessionSummaryModel(sessionId, modelId) {
  const hint = $("s-summary-model-hint");
  try {
    // ⚠️ 必须**带上 policy 的其它键**：后端 PATCH policy 是整块替换，
    //    只发 summary_model 会把 mode/protected_tail 等冲成默认值。
    const cur = (state.currentSession || {}).policy || {};
    const s = await api("PATCH", `/api/v1/sessions/${encodeURIComponent(sessionId)}`, {
      policy: { ...cur, summary_model: modelId },
    });
    state.currentSession = s;
    const i = state.sessions.findIndex((x) => x.id === s.id);
    if (i >= 0) state.sessions[i] = s;
    showHint(hint, modelId ? `总结将使用：${modelId}` : "总结将使用这条会话的对话模型");
  } catch (e) {
    showHint(hint, "保存失败：" + e.message, true);
  }
}

/** 把某个会话标记为已读（清掉它的未读徽标）。
 *
 * 🔴 **放在 `selectSession` 里**（而不是"点一下列表项"那个监听器里）：
 *    选中有四个入口（下拉 / 列表点击 / 路由带 id / 新建后选中），
 *    挂在监听器上会漏掉另外三个 —— 那种"某些路径不生效"的 bug
 *    在本项目已经出现过一次（登录浮层的迟到解锁）。
 *
 * 🔴🔴 **不要用前端缓存决定"要不要发这个请求"**（真浏览器判据抓到的 bug）：
 *    我第一版写的是 `if (!(state.unread || {})[id]) return;` —— 本意是省一次
 *    请求，实际制造了一个**永远修不好的未读**：
 *      刷新页面 → `state.unread` 是空的（还没轮询）→ 选中会话时守卫直接 return
 *      → **不发清零**；而 `refreshUnread()` 随后又把服务端的 1 拉回来
 *      → 用户明明打开了这条会话，红点却一直亮着（要到下次轮询才可能消）。
 *    实测证据（`tools/session_umo_unread_check.py` 判据⑧）：
 *      `{hidden: False, text: '1', total: 1}` —— 打开会话后红点仍在。
 *    → 正确做法：**服务端才是未读的事实源**，前端缓存只用于渲染。
 *      清零是幂等的（服务端 `if s.unread: ...` 才写盘），该发就发。
 *
 * ⚠️ 失败**不弹窗**：已读只是个徽标状态，读接口挂了不该打断聊天。
 *    但也不假装成功 —— 不删本地数字，下次轮询会如实把它拿回来。
 */
async function markSessionRead(id) {
  if (!id) return;
  try {
    const d = await api("POST", `/api/v1/sessions/${id}/read`);
    // ⭐ 用**服务端返回的会话**更新本地状态（而不是自己减一）：
    //    自己减一在"本地有 1、服务端有 2"时会把 2 显示成 0（假清零）。
    const s = d && d.session;
    if (s) {
      const i = state.sessions.findIndex((x) => x.id === s.id);
      if (i >= 0) state.sessions[i] = s;
      if (state.currentSession && state.currentSession.id === s.id) {
        state.currentSession = s;
      }
    }
    if (state.unread) delete state.unread[id];
    // 未读总数**重新问服务端**（只在这一个动作后问，代价极小，
    // 换来的是"别的会话也一起被算对"——自己减一做不到这件事）
    const u = await api("GET", "/api/v1/unread");
    state.unread = u.sessions || {};
    state.unreadTotal = u.total || 0;
    renderUnreadBadge();
    renderSessionList();
  } catch (e) { /* 见上：不打断，不清零 */ }
}

async function loadHistory(id) {
  const box = $("messages");
  box.innerHTML = "";
  try {
    const d = await api("GET", `/api/v1/sessions/${id}/messages?limit=50`);
    for (const m of d.data) {
      if (m.role === "system" || m.role === "reasoning") continue;
      addMsg(m.role, m.content);
    }
  } catch (e) { /* 新会话没消息，正常 */ }
}

function refreshSessionInfo() {
  const s = state.currentSession;
  const el = $("session-info");
  if (!s) { el.textContent = "无状态模式（不存历史）"; return; }
  // 🔴 会话专属 umo 的提醒（2026-09-27，情绪问「主动消息只能给到 default 吗」）。
  //
  // 背景：新建会话会自动分配 `sess-<hex>`，但**存量会话**（建在该功能之前）
  // 仍沿用全局 `default`。两者在列表里**长得一样**（都只是一行标题），
  // 用户于是会以为"主动消息只能发给 default"—— 实际只是那条会话没换过标签。
  //
  // ⚠️ 判据**不用 `=== "default"` 硬编码**：全局回落值是配置项
  //    （`plugins.umo_user_id`，服务端可改），写死字面量会在配置一改后静默说错话。
  //    改用两条**现场可取得**的条件（任一成立即视为"共用/兜底"）：
  //      ① 这个 umo 被**多条**会话共用（≥2）→ 插件眼里它们是同一个用户；
  //      ② 这个 umo **不是**本会话独有形状（`sess-<hex>`）→ 它来自全局回落
  //         或用户手填，都不保证独属本条会话。
  //    ⚠️ 只写 ① 会漏掉真实场景（实测踩到）：`default` 只挂在**一条**会话上时
  //       ① 不成立，可它依然是**全局兜底**（别的会话将来也会落进来，
  //       插件的主动消息也只会认它）—— 那正是用户困惑的那条。
  const uid = String(s.user_id || "");
  const ownShape = /^sess-[0-9a-f]{12}$/.test(uid);
  const sharedBy = uid
    ? state.sessions.filter((x) => String(x.user_id || "") === uid).length
    : 0;
  const shared = !uid || !ownShape || sharedBy > 1;
  const why = !uid
    ? "它没有插件用户 ID（会落到全局兜底 umo）"
    : (sharedBy > 1
        ? `umo「${uid}」被 ${sharedBy} 条会话共用`
        : `umo「${uid}」不是本条会话专属（全局兜底 / 手填值）`);
  el.textContent = shared
    ? `${s.policy.mode} · 窗口 ${s.policy.protected_tail} · ${s.message_count} 条 · ⚠️ ${why}`
    : `${s.policy.mode} · 窗口 ${s.policy.protected_tail} · ${s.message_count} 条 · umo ${uid}`;
  el.classList.toggle("warn", shared);
  el.title = shared
    ? "这种会话在陪伴插件眼里不是独立用户：主动消息可能落到别处（或全局那条兜底会话）。"
      + "到「会话」页点「换一个专用 umo」可以给本条分配独立的。"
    : "";
}

$("session-select").onchange = (e) => selectSession(e.target.value);

$("new-session").onclick = $("new-session-2").onclick = async () => {
  try {
    const s = await api("POST", "/api/v1/sessions", {});
    state.sessions.unshift(s);
    state.currentSessionId = s.id;
    state.currentSession = s;
    $("messages").innerHTML = "";
    renderSessionList();
    renderSessionSelect();
    renderSessionPicker();
    refreshSessionInfo();
    // 🔴 「会话」页那个新建按钮 → 建完**直接进聊天**（用户在这一页的意图就是开聊）。
    //    设置页那个 → 留在原地（他是去配的，弹走反而打断）。
    if (document.activeElement && document.activeElement.id === "new-session-2") {
      switchToTab("chat");
    } else {
      fillSessionForm(s);
      showHint($("cfg-session-hint"), "已新建会话，去上面那条「对话模型」给它选个模型");
    }
  } catch (e) {
    showHint($("session-hint"), "创建失败：" + e.message, true);
    showHint($("cfg-session-hint"), "创建失败：" + e.message, true);
    showHint($("session-page-hint"), "创建失败：" + e.message, true);
  }
};

$("clear-session").onclick = async () => {
  // 清空消息在**聊天页**上（`#clear-session` 就在 `#session-select` 旁边），
  // 所以反馈落在同一条工具栏上的 `#chat-hint`，而不是会话页的 `#session-hint`。
  if (!state.currentSessionId) return toast($("chat-hint"), "当前没有会话", true);
  if (!confirm("清空这个会话的消息？（会话设置会保留）")) return;
  try {
    await api("POST", `/api/v1/sessions/${state.currentSessionId}/clear`);
    $("messages").innerHTML = "";
    await loadSessions();
  } catch (e) { toast($("chat-hint"), "失败：" + e.message, true); }
};

if ($("s-mode")) {
  $("s-mode").onchange = () => {
    const h = $("mode-hint");
    if (h) h.textContent = MODE_HINTS[$("s-mode").value] || "";
  };
}

/* 会话配置表单（现居「设置 → 配置文件 → 会话」）。
 * ⚠️ 它**不再**是 <form> 的 submit 事件 —— 见下面 `cfgSessionSaveBtn`：
 *    搬进设置页后它嵌在 `#cfg-session-form` 里，而那个 form 已经有自己的
 *    submit 处理器（保存全局配置）。两个 submit 处理器套在同一个 form 上
 *    会**互相干扰**（内层阻止冒泡与否都会有一边不执行）。
 *    ⇒ 所以这里改成**按钮点击**触发，语义更明确，也不与全局保存冲突。 */
async function saveSessionConfig() {
  if (!state.currentSession) {
    showHint($("cfg-session-hint"), "还没有选中会话（先在上面选一条）", true);
    return;
  }
  try {
    const s = await api("PATCH", `/api/v1/sessions/${state.currentSession.id}`, {
      title: $("s-title").value,
      persona_id: $("s-persona").value,
      // ⚠️ **必须 trim**：用户 ID 是插件 `data["users"]` 的**键**，
      //    尾随空格会造出一个看着一样、实际匹配不上的键
      //    → 主动消息"找不到用户"，且极难排查。
      user_id: $("s-user-id").value.trim(),
      summary: $("s-summary").value,
      policy: {
        mode: $("s-mode").value,
        protected_tail: parseInt($("s-tail").value, 10) || 20,
        summary_trigger: parseInt($("s-trigger").value, 10) || 40,
        // 🔴 这两项必须**一起提交** —— policy 是整块替换，
        //    漏了它们会把用户刚设的总结模型/提示词静默清空
        //    （而"保存"看起来是成功的）。
        summary_model: (state.currentSession || {}).policy?.summary_model || "",
        // ⚠️ 提示词是**用户原文**：不 trim（只判空），否则会静默改掉他写的尾部换行
        summary_instruction: $("s-summary-instruction").value,
      },
    });
    state.currentSession = s;
    const i = state.sessions.findIndex((x) => x.id === s.id);
    if (i >= 0) state.sessions[i] = s;
    renderSessionList();
    renderSessionSelect();
    renderSessionPicker();
    // 刚改了"会话绑定的人设" → 聊天页那行提示要立刻跟着变
    // （否则用户绑完人设、切回聊天页，看到的还是旧提示）。
    renderChatPersonaHint();
    showHint($("cfg-session-hint"), "已保存");
    refreshSessionInfo();
  } catch (e2) {
    showHint($("cfg-session-hint"), "保存失败：" + e2.message, true);
  }
}

/** 「保存这条会话的配置」按钮（在设置页里）。
 *  ⚠️ 用 `type="button"` 语义：它只保存**这条会话**，不提交外层那个全局配置 form。
 *     （外层 `#cfg-session-form` 的 submit 归 `saveConfigSection("session")`。） */
const _cfgSessionSaveBtn = $("cfg-session-save");
if (_cfgSessionSaveBtn) {
  // 🔴 先摘掉外层 form 的 submit 行为：按钮在 form 里，默认 type=submit
  //    会**同时**触发全局配置保存 —— 用户点"保存会话"却改了全局设置。
  _cfgSessionSaveBtn.type = "button";
  _cfgSessionSaveBtn.onclick = (e) => {
    e.preventDefault();
    saveSessionConfig();
  };
}

/** 设置页的「删除这条会话」—— 与列表里那个行内删除**共用同一函数**
 *  （同一概念只许一条写入路径：同一句确认、同一个失败提示、同一次刷新）。 */
const _cfgSessionDel = $("cfg-session-del");
if (_cfgSessionDel) {
  _cfgSessionDel.onclick = () => {
    const sid = $("cfg-session-picker") ? $("cfg-session-picker").value : "";
    const s = state.sessions.find((x) => x.id === sid) || state.currentSession;
    if (!s) {
      showHint($("cfg-session-hint"), "没有可删除的会话", true);
      return;
    }
    deleteSessionById(s);
  };
}

/** 设置页的「选择会话」下拉 → 切到那条会话（**只影响"正在配谁"**，
 *  与聊天页的 `#session-select` 互不干扰）。 */
const _cfgSessionPicker = $("cfg-session-picker");
if (_cfgSessionPicker) {
  _cfgSessionPicker.onchange = () => {
    const s = state.sessions.find((x) => x.id === _cfgSessionPicker.value);
    if (!s) return;
    // ⚠️ 这里**不调 `selectSession`** —— 那会把"聊天页当前会话"也换掉，
    //    而用户此刻只是在**改配置**，没说要换聊天对象。
    state.currentSession = s;
    fillSessionForm(s);
    showHint($("cfg-session-model-hint"), `正在配置：${s.title || "(未命名)"}`);
  };
}

/** 设置页的「+ 新建会话」—— 复用聊天页那个按钮的处理（`$("new-session-2")`）。
 *  ⚠️ 不复制一份逻辑：新建会话要同时更新 sessions 数组、当前会话、
 *     三个渲染函数与状态栏，复制必然漂移。 */
const _cfgSessionNew = $("cfg-session-new");
if (_cfgSessionNew) {
  _cfgSessionNew.onclick = () => {
    const btn = $("new-session");
    if (btn) btn.onclick ? btn.onclick() : btn.click();
  };
}

/* ⚠️ `#s-del` 这个按钮**已随「会话」页那一版表单一起删除**（情绪 2026-10-09
 *    「会话页只留跳转聊天/新建/删除」）—— 删除现在有两个入口，都在别处：
 *      * 会话列表里每行的「删除」按钮（`renderSessionList`）
 *      * 设置页的「删除这条会话」（`#cfg-session-del`）
 *    三者**共用** `deleteSessionById()`（同一概念只许一条写入路径）。
 *    这里保留一个存在性判断，而不是直接删掉这段：万一有旧书签/旧缓存页面
 *    还带着这个 id 的 DOM，`$("s-del").onclick = ...` 会**当场抛错**，
 *    把整个脚本后半段（含 umo 复制）一起带走。 */
if ($("s-del")) {
  $("s-del").onclick = () => {
    if (state.currentSession) deleteSessionById(state.currentSession);
  };
}

/* ---- umo（插件用户 ID）：复制 / 换一个 ----------------------------------
 *
 * ⭐ 这一组的存在理由（情绪 2026-09-26 的原话）：
 *    「每个会话都带一个唯一标签 umo，这样我就能自己到陪伴那边添加"用户"
 *      和测试主动消息等东西了，陪伴插件不就是靠 umo 去识别用户的嘛」
 *   → 面板必须让人**看得见、复制得走**这个标签（不再要求用户登服务器改 .env）。
 */

/** 拼完整的 umo（`11:FriendMessage:<user_id>`）。
 *
 * 🔴 **不在前端硬拼字面量**：格式的唯一定义处在宿主
 *    `host/astrbot_host/umo.py`（那里有判据禁止第二处手写）。
 *    前端拿的是后端 `GET /api/v1/sessions/<id>` 里已经算好的 `umo` 字段
 *    —— 见本文件的 `selectSession` 与 `api_admin` 的会话响应。
 */
function currentUmo() {
  const s = state.currentSession;
  if (!s) return "";
  return s.umo || "";
}

$("s-umo-copy") && ($("s-umo-copy").onclick = async () => {
  const umo = currentUmo();
  if (!umo) return toast($("s-umo-hint"), "这个会话还没有 umo", true);
  try {
    await navigator.clipboard.writeText(umo);
    toast($("s-umo-hint"), `已复制：${umo}`);
  } catch (e) {
    // 剪贴板 API 在非 https / 未聚焦时会拒绝 —— 如实提示，
    // 并把值显示出来让用户手动选（别只报一句"失败"）。
    toast($("s-umo-hint"), `复制失败，请手动选：${umo}`, true);
  }
});

$("s-umo-new") && ($("s-umo-new").onclick = async () => {
  if (!state.currentSession) return;
  const before = currentUmo();
  if (!confirm(
    "换一个专用 umo？\n\n" +
    "陪伴插件按这个值认人：换掉之后，这个会话在插件眼里就是**另一个用户**。\n" +
    "旧标签下的记忆/关系不会丢（还挂在旧标签下），但之后写的东西都记到新标签下。\n\n" +
    `当前：${before}`
  )) return;
  try {
    const d = await api("POST", `/api/v1/sessions/${state.currentSession.id}/umo`);
    state.currentSession = d.session;
    const i = state.sessions.findIndex((x) => x.id === d.session.id);
    if (i >= 0) state.sessions[i] = d.session;
    $("s-user-id").value = d.session.user_id ?? "";
    renderSessionList();
    renderSessionSelect();
    toast($("s-umo-hint"), `已换成 ${d.session.umo}`);
  } catch (e) { toast($("s-umo-hint"), "换 umo 失败：" + e.message, true); }
});

/* ================= 设置 ================= */
/** 设置页 loader。
 *
 * 🔴 「上游连接」那块表单**已删除**（情绪 2026-10-09）——
 *    所以这里不再取 `s-base-url` / `s-model` / `s-api-key`
 *    （那几个元素已不存在，取它们会**当场抛错**，把 `/health` 那步也带走）。
 *
 * ⚠️ 但**仍然要拉 `/api/v1/settings`**：那个端点还管着
 *    「当前激活人格」（`active_persona_id`），人格页依赖它。
 *    把整个请求删掉会让"当前人设"静默显示不出来。
 *
 * ⚠️ 保留 `#deploy-config`（部署配置只读视图，来自 `/health`）——
 *    它与"上游连接"是两件事，不受本次删除影响。
 */
async function loadSettings() {
  try {
    // 仍然要这个请求：`active_persona_id`（当前人设）从这里来。
    const d = await api("GET", "/api/v1/settings");
    state.settings = d.settings || null;
  } catch (e) {
    // ⚠️ 不吞：读到失败要留痕，但**不往已删元素里写**
    state.settings = null;
    state.settingsError = e.message;
  }

  try {
    const h = await api("GET", "/health");
    const el = $("deploy-config");
    if (el) {
      el.textContent = JSON.stringify(
        { server: h.config.server, storage: h.config.storage, auth: h.config.auth, logging: h.config.logging },
        null, 2
      );
    }
  } catch (e) {
    // 🔴 失败要说出来：否则用户看到一片空白会以为"这页没内容"
    const el = $("deploy-config");
    if (el) el.textContent = `读取失败：${e.message}`;
  }
}

/* ================================================================================
 * 日志（情绪 2026-09-21 要求）—— 后端 `GET /api/v1/logs`
 * ================================================================================
 *
 * 🔴 设计要点（每条都有理由，别"顺手"改掉）：
 *   ① **空列表 ≠ 服务干净**：读失败/缓冲不可用时，提示区必须说清原因。
 *      只给空列表是最误导人的失败 —— 用户会以为"没日志就是没出错"。
 *   ② **自动刷新**是开关，默认开；离开页面**必须停**（`applyRoute` 负责）。
 *   ③ **不在前端二次脱敏**：后端已在出口走同一条 `scrub()`；前端再脱只会
 *      把有用信息打掉（本项目踩过三次"过度脱敏"）。
 *   ④ 级别**写文字**也上颜色 —— 不靠颜色单独传达信息（色盲/黑白屏也读得懂）。
 */

/** 自动刷新定时器句柄（0 = 未在跑）。 */
let logsTimer = 0;

/** 停掉自动刷新（幂等；离开页面时调）。 */
function stopLogsAutoRefresh() {
  if (logsTimer) {
    clearInterval(logsTimer);
    logsTimer = 0;
  }
}

/** 拉一次日志并渲染。`startAuto` 只有从路由进来时才传。 */
async function loadLogs(opts) {
  const options = opts || {};
  const hint = $("logs-hint");
  const box = $("logs-list");
  if (!hint || !box) return;

  const limit = $("logs-limit") ? $("logs-limit").value : "200";
  const level = $("logs-level") ? $("logs-level").value : "";
  const qs = `?limit=${encodeURIComponent(limit)}` +
             (level ? `&level=${encodeURIComponent(level)}` : "");

  try {
    // 🔴 用 `apiData`（不是 `api`）—— `/api/v1/logs` 是 **AstrBot 信封**
    //    （`{status, data:{items,…}}`）。我第一版用 `api()` 拿到的是外层信封，
    //    于是 `data.items` 恒为 undefined → **一行都画不出来**，
    //    而提示区却写着"共 N 条"（因为读了信封里的 data.total？并不，读的是 undefined）
    //    → 真浏览器判据 ⑤ 当场抓出来："提示说有条数，列表却是 0 行"。
    //    **通则：这个项目有两套响应形状（裸对象 vs AstrBot 信封），
    //    取数时先确认是哪一套，别靠猜。**
    const data = (await apiData("GET", `/api/v1/logs${qs}`)) || {};
    const items = Array.isArray(data.items) ? data.items : [];
    state.logs = items;

    if (data.available === false) {
      // 🔴 如实说"日志不可用"，**绝不**显示成"没有日志"
      toast(hint, `日志不可用：${data.reason || "缓冲未初始化"}`, true);
      box.innerHTML = "";
      renderLogsEmpty("日志缓冲不可用，所以这里看不到任何记录（这**不是**说服务很干净）。");
      return;
    }

    const cap = data.capacity ? `（缓冲上限 ${data.capacity} 条）` : "";
    if (!items.length) {
      toast(hint, `没有符合条件的日志${cap}。`);
      box.innerHTML = "";
      renderLogsEmpty("没有符合条件的日志。试试把「级别」调成「全部」。");
      return;
    }
    toast(hint, `共 ${items.length} 条${cap}${level ? `，只看 ${level} 及以上` : ""}。`);
    renderLogs(items);

    if (options.startAuto) startLogsAutoRefresh();
  } catch (e) {
    // 🔴 读失败 → 说清原因，并**保留旧内容**（清空会让人以为"日志被清了"）
    toast(hint, `读取日志失败：${e.message}`, true);
    if (!box.childElementCount) renderLogsEmpty("读取失败，暂无内容。");
  }
}

/** 开自动刷新（幂等：重复调不会叠出多个定时器）。 */
function startLogsAutoRefresh() {
  stopLogsAutoRefresh();     // 先清，避免叠加（叠加会让请求数翻倍）
  const auto = $("logs-auto");
  if (!auto || !auto.checked) return;
  logsTimer = setInterval(() => {
    // 只在**还停在日志页**且勾选着的时候刷
    if (parseRoute().name !== "logs") { stopLogsAutoRefresh(); return; }
    if ($("logs-auto") && !$("logs-auto").checked) { stopLogsAutoRefresh(); return; }
    loadLogs({});
  }, 5000);
}

/** 空态（不用 `innerHTML = ""` —— 那会连一句人话都没有，用户看到白屏）。 */
function renderLogsEmpty(text) {
  const box = $("logs-list");
  if (!box) return;
  const p = document.createElement("p");
  p.className = "hint";
  p.textContent = text;
  box.appendChild(p);
}

/** 渲染日志行。**全部 textContent**（日志内容是不可信文本，绝不拼 HTML）。 */
function renderLogs(items) {
  const box = $("logs-list");
  box.innerHTML = "";
  const frag = document.createDocumentFragment();
  for (const it of items) {
    const row = document.createElement("div");
    row.className = "log-row log-" + String(it.level || "INFO").toLowerCase();

    const head = document.createElement("div");
    head.className = "log-head";

    const lv = document.createElement("span");
    lv.className = "log-level";
    lv.textContent = String(it.level || "");     // 级别**写文字**，不只靠颜色

    const ts = document.createElement("span");
    ts.className = "log-ts";
    ts.textContent = String(it.ts || "");

    const lg = document.createElement("span");
    lg.className = "log-logger";
    lg.textContent = String(it.logger || "");

    head.append(lv, ts, lg);

    const msg = document.createElement("div");
    msg.className = "log-msg";
    msg.textContent = String(it.msg || "");
    row.append(head, msg);

    if (it.data) {
      const extra = document.createElement("pre");
      extra.className = "log-data";
      extra.textContent = JSON.stringify(it.data, null, 2);
      row.appendChild(extra);
    }
    if (it.exc) {
      const exc = document.createElement("pre");
      exc.className = "log-exc";
      exc.textContent = String(it.exc);
      row.appendChild(exc);
    }
    frag.appendChild(row);
  }
  box.appendChild(frag);
}

/** 复制当前显示的日志为纯文本（便于排查时贴出来）。 */
async function copyLogs() {
  const hint = $("logs-hint");
  const items = state.logs || [];
  if (!items.length) { toast(hint, "没有可复制的日志。", true); return; }
  const text = items
    .map((i) => `${i.ts || ""} ${i.level || ""} ${i.logger || ""}: ${i.msg || ""}` +
                (i.data ? `  ${JSON.stringify(i.data)}` : "") +
                (i.exc ? "\n" + i.exc : ""))
    .join("\n");
  try {
    await navigator.clipboard.writeText(text);
    toast(hint, `已复制 ${items.length} 条到剪贴板。`);
  } catch (e) {
    // ⚠️ http（非 https）下浏览器可能拒绝剪贴板 —— **说清**，别静默失败
    toast(hint, `复制失败（浏览器可能要求 https）：${e.message}`, true);
  }
}

/* ================================================================================
 * 配置文件（会话 + 上下文管理策略）—— conductor/CONTRACT.md §4/§6
 * ================================================================================
 *
 * 🔴 **字段不是前端写死的**：`GET /api/v1/config-profile` 连 `schema` 一起回来，
 *    这里照着 schema 画控件。后端是**单一事实来源** ——
 *    在 JS 里再抄一份字段清单，迟早会出现"后端认这个键、前端画的是另一个键"。
 *
 * 🔴 **绝不让失败看起来像"没有设置"**（本项目最贵的 bug 类型）：
 *    读取失败 → 提示里写清原因；模型列表读失败 → 说"读不到"而**不是**"没有模型"。
 *    所以 `cfg-hint` 在有错时一定是红的、且带着原因。
 */

/** 读配置文件 + schema（进 `#/settings` 时调）。 */
async function loadConfigProfile() {
  const hint = $("cfg-hint");
  try {
    const d = await api("GET", "/api/v1/config-profile");
    if (!d || typeof d !== "object" || !d.profile || !d.schema) {
      // 形状不对 → 抛错，**不许** `d.profile || {}` 那样把"没读懂"装成"没配置"
      throw new Error("响应里缺少 profile / schema（后端形状不符）");
    }
    state.cfgProfile = d.profile;
    state.cfgSchema = d.schema;
    renderConfigSections();
    // 顺带把"已配置的模型"拉回来（provider_select 的选项来源）
    await loadConfigModels();

    // 🔴 **模型列表拉回来之后**，必须重渲染会话相关的两个控件：
    //      * 设置页的「选择会话」下拉（它要显示每条会话用的模型名）
    //      * 「这条会话用哪个模型」下拉（选项来自 `state.cfgModels`）
    //    ⚠️ 我第一版漏了这一步，后果是**下拉里一个模型都没有** ——
    //       因为 `renderSessionModelField` 在 `cfgModels` 还是空数组时就渲染了，
    //       之后**再没人重绘**。真机判据 B2 实测"选项 1 个"（只有"不指定"），
    //       而 `providers.json` 里其实配了 **3 个**。
    //       用户的观感是"我明明配了模型，这里却选不了" —— 极易误判成模型没配上。
    //    ⇒ 顺序：**先拿数据、再渲染**；反过来就是"渲染一个空列表然后不管了"。
    renderSessionPicker();
    if (state.currentSession) {
      renderSessionModelField(state.currentSession);
      // ⚠️ 同一个坑：总结模型下拉也在 `cfgModels` 到位**之前**渲染过
      //    ⇒ 拉回来之后必须**也**重绘它，否则它永远是空的（用户以为没配模型）。
      renderSessionSummaryModelField(state.currentSession);
    }

    const n = Object.keys(state.cfgProfile).length;
    // ⚠️ 段名要跟着 `SECTIONS` 走：后端多一段而这里写死两段，就成了**假信息**
    //    （本页只渲染会话/上下文两段，插件段在「插件」页里管）。
    let msg = `已读取配置（${n} 段：${Object.keys(state.cfgProfile).join(" / ")}；`
      + `本页渲染「会话」，插件启停在「插件」页）。字段由后端 schema 提供。`;
    if (state.cfgModelsError) msg += ` ⚠️ ${state.cfgModelsError}`;
    toast(hint, msg, !!state.cfgModelsError);
  } catch (e) {
    state.cfgProfile = null;
    state.cfgSchema = null;
    // 🔴 失败**不隐藏表单**，但要**说清原因**（隐藏了用户就以为"这页没功能"）
    toast(hint, `读取配置失败：${e.message}`, true);
    for (const id of ["cfg-session-fields"]) {
      const el = $(id);
      if (el) el.innerHTML = "";
    }
  }
}

/** 拉"已配置的模型"，供 `provider_select` 用。 */
async function loadConfigModels() {
  state.cfgModelsError = "";
  try {
    // 两个接口都要：源列表给 provider_type（按类型过滤），模型列表给可选项
    const [srcs, provs] = await Promise.all([
      apiData("GET", "/api/v1/provider-sources"),
      apiData("GET", "/api/v1/providers"),
    ]);
    const list = asArray(srcs, "provider_sources");
    const map = {};
    for (const s of list) map[s.id] = s.provider_type || "chat_completion";
    state.cfgSourceTypes = map;
    state.cfgModels = asArray(provs, "providers").filter((m) => m.enable !== false);
    return true;
  } catch (e) {
    // ⚠️ **必须记住失败**：否则界面上会显示"还没有配置该类模型"，
    //    而真相是"模型列表根本没读到" —— 用户会去重新配一遍模型。
    state.cfgModelsError = `已配置模型读取失败（${e.message}），下面「选择模型」可能不全。`;
    state.cfgModels = [];
    return false;
  }
}

/** 按 schema 渲染表单（**每次重渲染，避免残留旧值**）。
 *
 * 🔴 2026-10-09：原来渲染**两段**（session + context）。
 *    全局 `context` 段已删除（情绪：「上下文策略一样做成会话级的，不做全局」）
 *    ⇒ 这里只剩一段。
 *    ⚠️ **上下文策略的能力没丢** —— 它搬到了「设置 → 配置文件 → 会话」里的
 *       `会话策略` 一组（那是 `Session.policy`，**真在生效**的那份）。
 */
function renderConfigSections() {
  const sch = state.cfgSchema || {};
  renderConfigSection("session", sch.session, $("cfg-session-fields"));
}

function renderConfigSection(section, fields, host) {
  if (!host) return;
  host.innerHTML = "";
  if (!Array.isArray(fields)) {
    // 形状不对 → 如实说（不静默画个空表单）
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = `schema.${section} 不是数组，无法渲染（后端形状不符）。`;
    host.appendChild(p);
    return;
  }
  const values = (state.cfgProfile || {})[section] || {};
  for (const f of fields) {
    host.appendChild(buildConfigField(section, f, values[f.key]));
  }
}

/** 造一个字段控件（`f.type` 决定画什么）。 */
function buildConfigField(section, f, value) {
  const wrap = document.createElement("label");
  wrap.className = "cfg-field";
  wrap.dataset.key = f.key;
  wrap.dataset.section = section;

  const title = document.createElement("span");
  title.className = "cfg-label";
  title.textContent = f.label || f.key;
  wrap.appendChild(title);

  const input = makeConfigInput(f, value);
  input.dataset.key = f.key;
  // 🔴 id 必须落在**真正可读写的那个控件**上（实测踩到）：
  //    `provider_select` / `provider_multi` 返回的是一个**容器**（里面才是 select/textarea），
  //    原来无条件 `input.id = ...` → id 落到了外层 span 上，
  //    于是 `document.getElementById('cfg-session-default_tts_provider_id').value`
  //    取到的是 span 的 undefined，`collectConfigChanges` 也读不到真实值。
  //    → 让容器内的**可交互控件**继承同一个 id（容器自己不要 id，避免重复 id）。
  const control = input.matches("input, select, textarea")
    ? input
    : input.querySelector("input, select, textarea");
  if (control) {
    control.id = `cfg-${section}-${f.key}`;
  } else {
    input.id = `cfg-${section}-${f.key}`;   // 兜底：真没有控件时至少 id 存在
  }
  wrap.appendChild(input);

  if (f.hint) {
    const h = document.createElement("small");
    h.className = "hint cfg-hint-line";
    h.textContent = f.hint;
    wrap.appendChild(h);
  }
  return wrap;
}

function makeConfigInput(f, value) {
  const t = f.type;
  if (t === "bool") {
    const el = document.createElement("input");
    el.type = "checkbox";
    el.checked = value === undefined ? !!f.default : !!value;
    return el;
  }
  if (t === "enum") {
    const el = document.createElement("select");
    for (const o of f.options || []) {
      const op = document.createElement("option");
      op.value = o.value;
      op.textContent = o.label;
      el.appendChild(op);
    }
    el.value = value === undefined ? f.default : value;
    return el;
  }
  if (t === "provider_select") {
    return makeProviderSelect(f, value);
  }
  if (t === "persona_select") {
    const el = document.createElement("select");
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "（用当前激活的人格）";
    el.appendChild(empty);
    for (const p of state.personas || []) {
      const op = document.createElement("option");
      op.value = p.id;
      op.textContent = p.name || p.id;
      el.appendChild(op);
    }
    el.value = value || "";
    return el;
  }
  if (t === "provider_multi") {
    // 多选：用多行文本框最省事且不会漏（每行一个 id），并在下面说明格式
    const el = document.createElement("textarea");
    el.rows = 2;
    el.value = Array.isArray(value) ? value.join("\n") : "";
    el.placeholder = "每行一个模型 id，例如 fast/gemini-3.5-flash";
    return el;
  }
  if (t === "string_list") {
    // 通用字符串数组（如插件停用名单）：每行一项。
    // ⚠️ 取值方式与 `provider_multi` 一样，但**类型名必须分开** ——
    //    `provider_multi` 的语义是"模型 id 列表"，判据（`test_schema_shape_is_renderable`）
    //    也要求每种类型都有对应的渲染分支。
    const el = document.createElement("textarea");
    el.rows = 3;
    el.value = Array.isArray(value) ? value.join("\n") : "";
    el.placeholder = "每行一项，例如 astrbot_plugin_x.main";
    return el;
  }
  if (t === "text") {
    const el = document.createElement("textarea");
    el.rows = 3;
    el.value = value === undefined ? f.default : value;
    return el;
  }
  // string / int / float
  const el = document.createElement("input");
  el.type = (t === "int" || t === "float") ? "number" : "text";
  if (t === "float") el.step = "0.01";
  if (f.min !== undefined) el.min = f.min;
  if (f.max !== undefined) el.max = f.max;
  el.value = value === undefined ? f.default : value;
  return el;
}

/** `provider_select`：只列**真实已配置**的、且是该分类的模型。
 *
 * 🔴 没有任何可选时**不许留一个空下拉** —— 那看起来像"能选但没得选"。
 *    要显式说"还没有配置该类模型"并给去供应商页的提示。
 */
function makeProviderSelect(f, value) {
  const box = document.createElement("span");
  box.className = "cfg-provider-select";

  const want = f.provider_type || "chat_completion";
  const opts = (state.cfgModels || []).filter((m) => {
    const st = state.cfgSourceTypes[m.provider_source_id || ""] || "chat_completion";
    return st === want;
  });

  const el = document.createElement("select");
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = "（不指定）";
  el.appendChild(empty);
  for (const m of opts) {
    const op = document.createElement("option");
    op.value = m.id;
    op.textContent = m.id;
    el.appendChild(op);
  }
  el.value = value || "";
  // 值不在选项里（模型被删了）→ 显式补一个，别让 select 静默跳到"不指定"
  if (value && !opts.some((m) => m.id === value)) {
    const op = document.createElement("option");
    op.value = value;
    op.textContent = `${value}（已不在已配置模型里）`;
    el.appendChild(op);
    el.value = value;
  }
  box.appendChild(el);

  if (!opts.length) {
    const h = document.createElement("small");
    h.className = "hint cfg-empty-models";
    // ⚠️ 区分"读失败"与"真的没有" —— 这两者给用户的下一步动作完全不同
    h.textContent = state.cfgModelsError
      ? state.cfgModelsError
      : `还没有配置「${want}」类模型，请先到「模型供应商」添加。`;
    box.appendChild(h);
  }
  return box;
}

/** 收集某段**改动过的**字段（只发改动项 —— 契约 §6）。 */
function collectConfigChanges(section, fields) {
  const values = (state.cfgProfile || {})[section] || {};
  const out = {};
  for (const f of fields || []) {
    const el = document.getElementById(`cfg-${section}-${f.key}`);
    if (!el) continue;
    let v;
    if (f.type === "bool") v = el.checked;
    else if (f.type === "int") v = el.value === "" ? f.default : parseInt(el.value, 10);
    else if (f.type === "float") v = el.value === "" ? f.default : parseFloat(el.value);
    else if (f.type === "provider_multi") {
      v = el.value.split("\n").map((x) => x.trim()).filter(Boolean);
    } else if (f.type === "string_list") {
      v = el.value.split("\n").map((x) => x.trim()).filter(Boolean);
    } else v = el.value;

    const before = values[f.key];
    const same = Array.isArray(v) && Array.isArray(before)
      ? JSON.stringify(v) === JSON.stringify(before)
      : v === before;
    if (!same) out[f.key] = v;
  }
  return out;
}

/** 保存某一段。 */
async function saveConfigSection(section) {
  const sch = state.cfgSchema || {};
  const hintEl = $(`cfg-${section}-hint`);
  const fields = sch[section] || [];
  const changes = collectConfigChanges(section, fields);
  if (!Object.keys(changes).length) {
    toast(hintEl, "没有改动，无需保存。");
    return;
  }
  try {
    const d = await api("PATCH", "/api/v1/config-profile", { [section]: changes });
    state.cfgProfile = d.profile;
    renderConfigSections();       // 用后端回来的真值重画（不假设自己写对了）
    toast(hintEl, `已保存 ${Object.keys(changes).length} 项。`);
  } catch (e) {
    // 🔴 后端 400 会带 `error.field` —— 把它**标在对应控件旁**，
    //    只给一句"值不合法"，用户在几十个字段里找不到是哪一个。
    const f = e && e.field;
    if (f) {
      const el = document.getElementById(`cfg-${section}-${f}`);
      if (el) el.classList.add("cfg-invalid");
    }
    toast(hintEl, `保存失败：${e.message}${f ? `（字段：${f}）` : ""}`, true);
  }
}

/** 恢复默认（两段一起清，所以文案要说清）。 */
async function resetConfigProfile() {
  const ok = window.confirm("恢复默认会清掉「会话」与「上下文管理策略」的全部自定义值，确定？");
  if (!ok) return;
  try {
    const d = await api("DELETE", "/api/v1/config-profile");
    state.cfgProfile = d.profile;
    renderConfigSections();
    toast($("cfg-hint"), "已恢复默认（两段一起）。");
  } catch (e) {
    toast($("cfg-hint"), "恢复默认失败：" + e.message, true);
  }
}

// ---- 配置文件（会话 / 上下文管理策略）：两段各存各的，只发改动项 ----
// ⚠️ 这三条与「上游连接」**毫无关系** —— 我第一版删除时范围切宽了，
//    把这一整段连同日志页那 5 条一起切掉，重做时又漏补。
//    后果**完全静默**：配置页的「保存」按钮点了没反应、日志页刷新/复制全废，
//    而控制台一行错都不报（因为处理器压根没绑，不是绑错了）。
//    ⇒ 是 `tools/upstream_removed_check.py` 的 B2 判据抓住的
//      （它专门断言"点了之后**真发出请求**"，而不是"按钮存在"）。
$("cfg-session-form").onsubmit = (e) => { e.preventDefault(); saveConfigSection("session"); };
// 🔴 原来这里还有一行 `$("cfg-context-form").onsubmit = ...` —— 那个表单已随
//    全局 context 段一起删除。**不能留**：元素不存在时 `$()` 返回 null，
//    读 `.onsubmit` 会**当场抛 TypeError**，而它在脚本顶层 ⇒
//    后面所有接线（重置按钮、日志页 5 条）全部不执行 —— **零报错、全静默**。
//    （第十八轮那次真实事故就是这个形状，见 `tools/upstream_removed_check.py`）
$("cfg-reset").onclick = () => resetConfigProfile();

// ---- 日志：刷新 / 复制 / 自动刷新开关 ----
$("logs-refresh").onclick = () => loadLogs({});
$("logs-copy").onclick = () => copyLogs();
// 改级别/条数 → 立刻重拉（不用再点"立即刷新"，少一步）
$("logs-level").onchange = () => loadLogs({});
$("logs-limit").onchange = () => loadLogs({});
// 勾/取消自动刷新：勾上就立刻开表（并马上拉一次，用户不用等 5 秒）
$("logs-auto").onchange = () => {
  if ($("logs-auto").checked) loadLogs({ startAuto: true });
  else stopLogsAutoRefresh();
};

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
function explainProviderError(e) {
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
async function apiData(method, path, body) {
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
function asArray(obj, field) {
  const v = obj ? obj[field] : undefined;
  if (v === undefined || v === null) return [];
  if (!Array.isArray(v)) throw new Error(`响应里 ${field} 不是数组（后端形状不符）`);
  return v;
}

/** 一个源归到哪个类型（缺失视为对话 —— 与 `isChatSource` 同一条口径）。 */
function sourceProviderType(s) {
  return (s && s.provider_type) || "chat_completion";
}

/** 一个源是不是「对话」类。缺失 provider_type 视为对话（迁移过来的老数据没有这个字段）。
 *
 * ⚠️ 现在**页面逻辑已经不直接用它**了（筛选走 `visibleProviderSources()`，
 *    归属走 `sourceProviderType()`）—— 但它是"缺失 = 对话"这条口径的**命名定义**，
 *    也是别的代码/判据可能引用的稳定入口，**留着**（删了以后有人会再写一遍
 *    略有出入的判断，那才会真出问题）。
 */
function isChatSource(s) {
  return sourceProviderType(s) === "chat_completion";
}

/* ---------------- 类型筛选与徽标（契约 §5.1） ----------------
 *
 * 四类与后端 `SUPPORTED_PROVIDER_TYPES` 一一对应（契约 §1）：
 *   chat_completion → 对话 / text_to_speech → 语音合成 /
 *   speech_to_text  → 语音识别 / embedding → 嵌入
 *
 * 🔴 为什么用一张表而不是散落的 if：
 *    "筛选值 → 中文名" 这个映射要同时被**按钮**、**hint 文案**、**列表徽标**
 *    三处用到；散着写迟早会出现"按钮写语音、徽标写 TTS"这种对不上的情况。
 *    ⚠️ 这张表必须与后端 `SUPPORTED_PROVIDER_TYPES` **同增同减** ——
 *       后端加了类型而这里没加 → 该类源会落进"其他"、用户筛不到（静默不可见）。
 *
 * ⚠️ `speech_to_text`（2026-09-21 加入，情绪选"先只做配置层"）：
 *    **存得进、筛得到**，但 3 条 STT 预设的适配器我们**一个都执行不了** ——
 *    由后端 `executable_by_us` 如实标 false，前端照实显示"本平台暂不能执行"。
 *    别因为"筛得到"就以为"跑得了"。
 */
const PROVIDER_TYPE_LABELS = {
  chat_completion: "对话",
  text_to_speech: "语音合成",
  speech_to_text: "语音识别",
  embedding: "嵌入",
};

/** 筛选控件里用的取值：`*` = 全部（不筛）。 */
const PROVIDER_FILTER_ALL = "*";

/** 类型的展示名。**未知类型不假装认识**：原样回显后端给的字符串。 */
function providerTypeLabel(t) {
  return PROVIDER_TYPE_LABELS[t] || t || "未知";
}

/** 当前筛选下**应该显示**的那些源。
 *
 * 🔴 这是"筛选"的唯一实现，也是 hint 里那个 N 的**唯一来源** ——
 *    两边各算一次就会出现"hint 说 3 个、列表画了 2 个"的谎话
 *    （本项目铁律：绝不让界面上的数字和实际渲染不一致）。
 */
function visibleProviderSources() {
  const all = state.providerSources || [];
  const f = state.providerFilter || PROVIDER_FILTER_ALL;
  if (f === PROVIDER_FILTER_ALL) return all;
  return all.filter((s) => sourceProviderType(s) === f);
}

/** 高亮筛选按钮（`aria-pressed` 一起切，键盘/读屏用户才知道当前在哪一类）。 */
function syncProviderFilterButtons() {
  const cur = state.providerFilter || PROVIDER_FILTER_ALL;
  document.querySelectorAll("#provider-type-filter .filter-btn").forEach((b) => {
    const on = b.dataset.ptype === cur;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

/** 切换筛选。**只重画列表 + hint**，不动右侧已选中的源
 *  （否则用户刚填了一半的表单会因为"想看一眼语音类"被清掉）。 */
function setProviderFilter(t) {
  state.providerFilter = t;
  renderProviderSourceList();
  updateProviderSourcesHint();
}

/* ---------------- 自定义请求头（对象 ⇄ 行） ---------------- */

function headersToRows(ch) {
  const out = [];
  if (ch && typeof ch === "object") {
    for (const k of Object.keys(ch)) out.push({ k, v: String(ch[k] ?? "") });
  }
  return out;
}

function headersFromRows() {
  const out = {};
  $("ps-header-rows").querySelectorAll(".header-row").forEach((row) => {
    const k = row.querySelector(".h-key").value.trim();
    const v = row.querySelector(".h-val").value;
    // ⚠️ 空键的行直接丢（用户点了「添加更多」又没填）；值可以是空字符串，原样保留
    if (k) out[k] = v;
  });
  return out;
}

function addHeaderRow(k, v) {
  const row = document.createElement("div");
  row.className = "header-row";
  const ki = document.createElement("input");
  ki.className = "h-key";
  ki.placeholder = "键，例如 X-Api-Version";
  ki.value = k || "";
  const vi = document.createElement("input");
  vi.className = "h-val";
  vi.placeholder = "值（字符串）";
  vi.value = v || "";
  const del = document.createElement("button");
  del.type = "button";
  del.className = "danger";
  del.textContent = "移除";
  del.onclick = () => { row.remove(); syncHeaderEmpty(); };
  row.append(ki, vi, del);
  $("ps-header-rows").appendChild(row);
  syncHeaderEmpty();
}

function syncHeaderEmpty() {
  const n = $("ps-header-rows").querySelectorAll(".header-row").length;
  $("ps-header-empty").classList.toggle("hidden", n > 0);
}

function renderHeaderRows(rows) {
  $("ps-header-rows").innerHTML = "";
  for (const r of rows) addHeaderRow(r.k, r.v);
  syncHeaderEmpty();
}

/* ---------------- 左侧：提供商源列表 ---------------- */

function renderProviderSourceList() {
  const ul = $("provider-source-list");
  const empty = $("provider-source-empty");
  const all = state.providerSources || [];
  const list = visibleProviderSources();
  state.hiddenSourceCount = all.length - list.length;
  syncProviderFilterButtons();

  ul.innerHTML = "";
  if (!list.length) {
    empty.classList.remove("hidden");
    // 空态要分清两种"空"：**这一类里没有** vs **一个源都没有**（说的是两件事）
    if (!all.length) {
      empty.textContent = "还没有提供商源，点上面的「+ 新增」建一个。";
    } else if (state.providerFilter === PROVIDER_FILTER_ALL) {
      empty.textContent = "还没有提供商源，点上面的「+ 新增」建一个。";
    } else {
      empty.textContent = `没有「${providerTypeLabel(state.providerFilter)}」类的提供商源（切到「全部」可以看到其余 ${all.length} 个）。`;
    }
  } else {
    empty.classList.add("hidden");
  }

  for (const s of list) {
    const li = document.createElement("li");
    li.dataset.sourceId = s.id;
    li.classList.toggle("active", s.id === state.selectedSourceId);
    // 键盘可达（列表项也是可点的"按钮"）
    li.tabIndex = 0;
    li.setAttribute("role", "button");

    const text = document.createElement("div");
    text.className = "src-text";
    const idEl = document.createElement("div");
    idEl.className = "src-id";
    idEl.textContent = s.id;                       // ← 第一行：id
    // 类型徽标：**只看 provider_type，不看当前筛选**。
    //   "全部"视图里必须能一眼分出哪个是语音/嵌入（否则一排 id 长得一模一样）；
    //   相反，在「对话」筛选里给每条都挂个「对话」是噪音 —— 所以对话类不挂。
    const pt = sourceProviderType(s);
    if (pt !== "chat_completion") {
      const tag = document.createElement("span");
      tag.className = "src-type";
      // ⚠️ `data-ptype` 存**原值**（不是展示名）：判据/其它代码要能按真实类型取值，
      //    展示名只是给人看的，两者混在一起以后一定会有人拿它当过滤条件。
      tag.dataset.ptype = pt;
      tag.textContent = providerTypeLabel(pt);
      idEl.appendChild(tag);
    }
    const baseEl = document.createElement("div");
    baseEl.className = "src-base";
    baseEl.textContent = s.api_base || "（未设置 API Base URL）";   // ← 第二行：api_base
    text.append(idEl, baseEl);

    const del = document.createElement("button");
    del.type = "button";
    del.className = "src-del";
    del.dataset.del = s.id;
    del.title = `删除 ${s.id}`;
    del.setAttribute("aria-label", `删除 ${s.id}`);
    del.textContent = "🗑";

    li.append(text, del);
    li.onclick = (ev) => {
      if (ev.target.closest("[data-del]")) return;   // 点删除不算"选中"
      selectProviderSource(s.id);
    };
    li.onkeydown = (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); selectProviderSource(s.id); }
    };
    ul.appendChild(li);
  }
}

/** 左侧列表下方那句 hint —— **必须如实描述当前筛选**（契约 §5.1）。
 *
 * 🔴 这里的 N **必须**取自 `visibleProviderSources()`（与列表渲染同一个函数），
 *    不许另算一遍：两个计数各写各的，迟早出现"hint 说 3 个、列表画了 2 个"，
 *    而用户只会觉得"这页在骗我"。
 * ⚠️ 原来那句「另有 N 个非「对话」类源未显示（本页只做对话）」的尾注
 *    **本轮已不成立**（本页不再"只做对话"，能切类型了）→ 必须改掉，
 *    留着就是一句过时的假话。
 */
function updateProviderSourcesHint() {
  const all = state.providerSources || [];
  const n = visibleProviderSources().length;
  // 🔴 读失败时 `providerSources` 被清成了 `[]`，这里**不能**再画一遍
  //    "还没有提供商源" —— 那等于把"读取失败"伪装成"你还没建过源"，
  //    用户会去建一个重复的源。失败的消息由 `loadProviderSources` 的 catch 出，
  //    这里只负责"数据是真的"时的文案。
  //    ⚠️ 必须有这个短路：失败后用户点一下筛选按钮就会调到这里，
  //       不拦的话那句真错误会被"当前筛选：对话（共 0 个）"**当场覆盖掉**。
  if (state.sourcesError) return;
  const cur = state.providerFilter || PROVIDER_FILTER_ALL;
  let msg;
  if (cur === PROVIDER_FILTER_ALL) {
    msg = all.length ? `当前筛选：全部（共 ${n} 个）。` : "还没有提供商源，点左上「+ 新增」建一个。";
  } else {
    msg = `当前筛选：${providerTypeLabel(cur)}（共 ${n} 个）。`;
  }
  if (state.hiddenSourceCount > 0) {
    msg += `另有 ${state.hiddenSourceCount} 个其他类型的源未显示（切到「全部」可以看到）。`;
  }
  toast($("providers-hint"), msg);
}

/** 读源列表（进 `#/providers` 时由 NAV_LOADERS 调）。 */
async function loadProviderSources() {
  try {
    const d = await apiData("GET", "/api/v1/provider-sources");
    state.providerSources = asArray(d, "provider_sources");
    state.sourcesError = "";          // 读成功 → 清掉上次的失败态
    renderProviderSourceList();
    // ⚠️ 提示统一由 `updateProviderSourcesHint()` 出 —— 成功/失败两条路都走它，
    //    这样"hint 里的数字"与"列表里画出来的条数"永远出自同一个函数（不会对不上）。
    updateProviderSourcesHint();
    return true;
  } catch (e) {
    state.providerSources = [];
    state.sourcesError = explainProviderError(e);
    renderProviderSourceList();
    // 🔴 失败**必须**压过正常文案：成功/失败两条路都写同一句 hint，
    //    谁后写谁生效 —— 所以失败这条一定放在 `updateProviderSourcesHint()` 之后，
    //    并且给 `state.sourcesError` 置位，挡住后续（如点筛选）把它覆盖回去。
    toast($("providers-hint"), `读取提供商源失败：${state.sourcesError}`, true);
    return false;
  }
}

/** 同一次路由内**只读一次**源列表（重复调用复用上一次的 promise）。
 *
 * 🔴 为什么需要它（真浏览器实测抓到的真 bug，不是洁癖）：
 *    保存成功 → `loadProviderSources()` → `selectProviderSource()` 改 hash
 *    → hashchange → `applyRoute()` → loader **又读一次** →
 *    第二次的 `updateProviderSourcesHint()` 把「已新增 xxx。」**覆盖掉**。
 *    用户因此**永远看不到保存成功的确认**（只看到 hint 变成一句筛选说明）。
 *    → 这里把同一次路由内的重复读合并掉；**路由真正切换时**由
 *      `resetProviderSourcesOnce()` 复位，所以"重新进这个页面"照常会读到新数据。
 */
let providerSourcesOnce = null;

function loadProviderSourcesOnce() {
  if (!providerSourcesOnce) {
    providerSourcesOnce = loadProviderSources().catch((e) => {
      // 失败也复位：下次进入该页要能真的重试（不能把失败缓存住 —— 见 presets 的同款教训）
      providerSourcesOnce = null;
      throw e;
    });
  }
  return providerSourcesOnce;
}

function resetProviderSourcesOnce() {
  providerSourcesOnce = null;
}

/** 用户**主动**要最新数据时用这个：先把 once 缓存清掉再读，保证真的打到后端。 */
async function loadProviderSourcesFresh() {
  resetProviderSourcesOnce();
  return loadProviderSources();
}

/* ---------------- 右侧：选中源的详情 ---------------- */

/** 回到"未选中"状态：显示空态（**不能白屏**），清掉详情里的残留。 */
function clearProviderDetail() {
  state.selectedSourceId = "";
  state.creatingSource = false;
  state.configuredModels = [];
  state.sourceModelMetadata = {};
  state.availableModels = null;

  $("ps-configured-list").innerHTML = "";
  $("ps-configured-empty").classList.add("hidden");
  $("ps-model-count").textContent = "已配置 0";
  $("ps-available-list").classList.add("hidden");
  $("ps-available-list").innerHTML = "";
  $("ps-model-search").value = "";
  $("ps-custom-wrap").classList.add("hidden");
  toast($("ps-available-hint"), "");
  // 🔴 「获取模型列表 / 测试」的结果现在住在弹层里（情绪 2026-09-21 要求），
  //    所以"清残留"必须**连弹层一起关**：不关的话，用户切到另一个源时，
  //    上一个源的候选/测试结果还浮在屏幕上，看起来像是**这个源**的结果。
  //    （原来是 `$("providers-test-result").classList.add("hidden")` —— 那个节点
  //      已经在 `#test-modal` 里，单隐藏它等于留下一个空弹窗。）
  closeModelsModal();
  closeTestModal();
  $("provider-source-panel").classList.add("hidden");
  $("provider-source-placeholder").classList.remove("hidden");
  $("tab-providers").classList.remove("is-detail");
  renderProviderSourceList();
}

/** 点左侧某个源 → 右侧加载该源的详情 + 已配置模型。 */
async function selectProviderSource(id, opts) {
  const src = (state.providerSources || []).find((s) => s.id === id);
  if (!src) {
    // 找不到就说清楚（比如后端还没就绪，或者 URL 里的 id 已经不存在了）
    clearProviderDetail();
    toast($("providers-hint"), `找不到提供商源「${id}」。`, true);
    return false;
  }
  state.selectedSourceId = id;
  state.creatingSource = false;
  state.sourceEnable = src.enable === false ? false : true;   // 保存时保持原值，不擅自启用

  // 「点开 → 路由到详情」：写进 hash，返回键/刷新/分享都能回到同一个源
  if (!(opts && opts.skipHash)) go("providers/" + encodeURIComponent(id));

  renderProviderSourceList();                       // 高亮跟着走
  $("tab-providers").classList.add("is-detail");    // 手机端：列表收起 + 返回按钮
  $("provider-source-placeholder").classList.add("hidden");
  $("provider-source-panel").classList.remove("hidden");
  // 换了源 → 上一个源的测试结果与候选弹层一并收掉（同 clearProviderDetail 的理由）
  closeTestModal();

  // 标题 = 源 id；副标题 = api_base（照参考站）
  $("provider-source-title").textContent = src.id;
  $("provider-source-subtitle").textContent = src.api_base || "（未设置 API Base URL）";

  // ---- 设置 ----
  $("ps-id").value = src.id || "";
  $("ps-api-base").value = src.api_base || "";
  $("ps-timeout").value = (src.timeout === undefined || src.timeout === null) ? "" : src.timeout;
  $("ps-proxy").value = src.proxy || "";
  // 🔴 **绝不回显 key**：无论后端给的是什么形状（`key_configured` 布尔、
  //    还是照 AstrBot 那样把 `key` 数组也带回来），输入框**一律清空**，
  //    只用占位符说明"服务端已有一把，留空 = 不改"。
  $("ps-api-key").value = "";
  $("ps-api-key").placeholder = src.key_configured ? "已设置（留空 = 不修改）" : "未设置";
  renderHeaderRows(headersToRows(src.custom_headers));
  // 选中了一个真实存在的源 → 上一次的"列表读失败"已经不再是当前状态，
  // 清掉它，否则后面所有 hint 都会被那条旧错误一直挡着（永远刷不出新消息）。
  state.sourcesError = "";
  // 适配器类型 / 用途分类：**照后端真实值回填**（缺失才回落默认）
  setAdapterType(src.type || "");
  setProviderTypeSelect(sourceProviderType(src));

  // ---- 模型 ----
  state.configuredModels = [];
  state.sourceModelMetadata = {};
  state.availableModels = null;
  // 🔴 换了源 → 上一个源的候选弹层**必须收掉**：它是浮层，留着就会盖在**新源**的卡片上，
  //    而里面的候选是**旧源**的 —— 用户会以为自己正在给新源挑模型（选错源加错模型）。
  //    ⚠️ 节点本身不清空（留着无害），但 `state` 已复位 → 下次打开是干净的。
  closeModelsModal();
  $("ps-available-list").classList.add("hidden");
  $("ps-available-list").innerHTML = "";
  $("ps-model-search").value = "";
  $("ps-custom-wrap").classList.add("hidden");
  await loadConfiguredModels(id);
  toast($("ps-available-hint"), "点「获取模型列表」从该端点拉取可用模型。");
  // 🔴 选中/保存成功后的**确认语**放在这里显式补一次（真浏览器实测）：
  //    保存流程末尾原本只有 `loadProviderSources()`，而它会把 hint 写成
  //    「当前筛选：…」—— 那句是**列表状态**，不是**操作结果**，
  //    用户在保存后最需要看到的是"存进去了没有"。两句都要，且这句必须在**最后**写。
  if (opts && opts.saved) toast($("providers-hint"), opts.saved);
  return true;
}

/** 「+ 新增」的**空表单**那一半（还没落盘，保存才创建）。
 *
 *  ⚠️ 它**不再**由「+ 新增」按钮直接调用：按钮先弹**厂商预设**（契约 §5.2），
 *     选完预设由 `applyPreset()` 预填；点「不用预设，手动填」才走这里。
 *  ⚠️ `currentProvider` **不在这里重置**：`applyPreset()` 刚设过它。
 *     在这里清成 `openai` 会把预设带进来的厂商标签（`deepseek`/`microsoft`…）
 *     在用户还没保存前就悄悄抹掉 —— 那正是"硬编码 provider"的另一种表现形式。
 */
function startNewProviderSource() {
  state.creatingSource = true;
  state.selectedSourceId = "";
  state.configuredModels = [];
  state.sourceModelMetadata = {};
  state.availableModels = null;

  renderProviderSourceList();
  $("tab-providers").classList.add("is-detail");
  $("provider-source-placeholder").classList.add("hidden");
  $("provider-source-panel").classList.remove("hidden");
  closeTestModal();

  $("provider-source-title").textContent = "新增提供商源";
  $("provider-source-subtitle").textContent = "填好 ID 与 API Base URL，再点右上角「保存配置」";
  $("ps-id").value = "";
  $("ps-api-base").value = "";
  $("ps-timeout").value = "120";
  $("ps-proxy").value = "";
  $("ps-api-key").value = "";
  $("ps-api-key").placeholder = "未设置";
  renderHeaderRows([]);
  // 新建时回到"两个 OpenAI 格式 + 对话"的默认；选预设会再覆盖一次
  setAdapterType("openai_chat_completion");
  setProviderTypeSelect("chat_completion");

  $("ps-model-count").textContent = "已配置 0";
  $("ps-configured-list").innerHTML = "";
  $("ps-configured-empty").classList.remove("hidden");
  $("ps-configured-empty").textContent = "先保存这个源，然后再「获取模型列表」。";
  // 新增源 = 还没有源可拉 → 把上一个源留下的候选弹层收掉（理由同 selectProviderSource）
  closeModelsModal();
  $("ps-available-list").classList.add("hidden");
  $("ps-available-list").innerHTML = "";
  $("ps-custom-wrap").classList.add("hidden");
  toast($("ps-available-hint"), "");
  toast($("providers-hint"), "填写后点右上角「保存配置」创建。");
  $("ps-api-key").placeholder = "未设置";
  $("ps-id").focus();
}

/** 读某个源**已配置**的模型（源级路由，不靠客户端过滤）。 */
async function loadConfiguredModels(id) {
  const empty = $("ps-configured-empty");
  try {
    const d = await apiData("GET", `/api/v1/provider-sources/${encodeURIComponent(id)}/providers`);
    state.configuredModels = asArray(d, "providers");
    if (d && d.model_metadata) state.sourceModelMetadata = d.model_metadata;
    renderConfiguredModels();
  } catch (e) {
    state.configuredModels = [];
    renderConfiguredModels();
    // 🔴 失败要把这一行**变成错误说明**（不是留一句"还没有模型"骗人）
    empty.classList.remove("hidden");
    empty.textContent = `已配置模型读取失败：${explainProviderError(e)}`;
  }
}

function renderConfiguredModels() {
  const ul = $("ps-configured-list");
  const empty = $("ps-configured-empty");
  const list = state.configuredModels || [];

  ul.innerHTML = "";
  $("ps-model-count").textContent = `已配置 ${list.length}`;
  if (!list.length) {
    empty.classList.remove("hidden");
    empty.textContent = "还没有配置模型，点上面的「自定义模型」加一个。";
  } else {
    empty.classList.add("hidden");
  }

  for (const p of list) {
    const pid = p.id || `${state.selectedSourceId}/${p.model || ""}`;
    const li = document.createElement("li");
    li.dataset.modelId = pid;
    if (p.enable === false) li.classList.add("disabled");

    const mid = document.createElement("span");
    mid.className = "mid";
    const code = document.createElement("code");
    code.textContent = pid;
    mid.appendChild(code);

    const sub = document.createElement("span");
    sub.className = "sub";
    sub.textContent = p.enable === false ? "已停用" : (p.model || "");

    const ops = document.createElement("div");
    ops.className = "row";
    const test = document.createElement("button");
    test.type = "button";
    test.dataset.op = "test";
    test.dataset.id = pid;
    test.textContent = "测试";
    const del = document.createElement("button");
    del.type = "button";
    del.className = "danger";
    del.dataset.op = "del";
    del.dataset.id = pid;
    del.textContent = "移除";
    ops.append(test, del);

    li.append(mid, sub, ops);
    ul.appendChild(li);
  }
  renderAvailableModels();   // 已配置的候选要打勾 / 禁用
}

/** 候选模型列表（「获取模型列表」的结果，可勾选添加）。 */
function renderAvailableModels() {
  const ul = $("ps-available-list");
  const all = state.availableModels;
  ul.innerHTML = "";
  if (!all) { ul.classList.add("hidden"); return; }

  const q = ($("ps-model-search").value || "").trim().toLowerCase();
  const shown = q ? all.filter((m) => String(m).toLowerCase().includes(q)) : all;
  const configured = new Set((state.configuredModels || []).map((p) => p.id));
  const md = state.sourceModelMetadata || {};

  for (const name of shown) {
    const full = `${state.selectedSourceId}/${name}`;
    const added = configured.has(full) || configured.has(name);
    const li = document.createElement("li");
    if (added) li.classList.add("disabled");

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = added;
    cb.disabled = added;
    cb.setAttribute("aria-label", `添加模型 ${name}`);
    cb.onchange = () => addProviderModel(name);

    const mid = document.createElement("span");
    mid.className = "mid";
    const code = document.createElement("code");
    code.textContent = name;
    mid.appendChild(code);

    // 元数据摘要（AstrBot 也会带回来；没有就留空，不编造）
    const info = md[name] || {};
    const bits = [];
    if (info.reasoning) bits.push("推理");
    if (info.tool_call) bits.push("工具");
    if (info.limit && info.limit.context) bits.push(`上下文 ${info.limit.context}`);
    if (info.modalities && Array.isArray(info.modalities.input) && info.modalities.input.length) {
      bits.push(info.modalities.input.join("/"));
    }
    const sub = document.createElement("span");
    sub.className = "sub";
    sub.textContent = bits.join(" · ");

    li.append(cb, mid, sub);
    if (added) {
      const tag = document.createElement("span");
      tag.className = "sub";
      tag.textContent = "已添加";
      li.appendChild(tag);
    } else {
      const add = document.createElement("button");
      add.type = "button";
      add.textContent = "添加";
      add.onclick = () => addProviderModel(name);
      li.appendChild(add);
    }
    ul.appendChild(li);
  }

  if (!shown.length) {
    const li = document.createElement("li");
    li.className = "ps-none";
    li.textContent = q ? `没有匹配「${q}」的模型。` : "该源没有返回任何模型。";
    ul.appendChild(li);
  }
  ul.classList.remove("hidden");
}

/* ---------------- 设置块：适配器类型 / 用途分类（契约 §5.3） ----------------
 *
 * 🔴 为什么要有 `state.currentAdapterType` 这个"影子状态"（别删）：
 *    `<select id="ps-type">` 里**只有**我们能执行的两种 OpenAI 格式（契约 §5.3）。
 *    但预设会带来 `anthropic_chat_completion`/`edge_tts`/… 这些**能存不能跑**的类型。
 *    如果遇到不认识的类型就把 select 硬设成 `openai_chat_completion`，
 *    保存时就会**把用户选的 anthropic 悄悄改成 openai** ——
 *    这正是契约 §5.4 点名的那类硬编码 bug（只是换了个形式）。
 *    → 所以：**select 只表示"两个 OpenAI 格式"**；其余类型走只读展示，
 *      而"真的要提交什么 type"始终以 `state.currentAdapterType` 为准。
 */

/** 我们能执行的两种 OpenAI 格式（契约 §1 `EXECUTABLE_ADAPTER_TYPES`，前端一份镜像）。 */
const ADAPTER_LABELS = {
  openai_chat_completion: "OpenAI Compatible（/chat/completions）",
  openai_responses: "OpenAI Responses（/responses）",
};

/** 设表单的适配器类型：两种 OpenAI 格式进下拉；其余**只读展示 + 标注不可执行**。 */
function setAdapterType(t) {
  const v = (t || "").trim() || "openai_chat_completion";
  state.currentAdapterType = v;
  const sel = $("ps-type");
  const ro = $("ps-type-adapter");
  const isSelectable = Object.prototype.hasOwnProperty.call(ADAPTER_LABELS, v);
  if (isSelectable) {
    sel.value = v;
    sel.classList.remove("hidden");
    ro.classList.add("hidden");
    $("ps-type-hint").textContent = "决定“这个端点怎么调”（契约 §1 的适配器协议）。";
  } else {
    // 只读展示：**保留真实值**（不降级、不假装它是 OpenAI 格式）
    sel.classList.add("hidden");
    ro.classList.remove("hidden");
    $("ps-type-adapter-value").textContent = v;
    $("ps-type-adapter-note").textContent =
      "（该适配器来自预设，本平台不能执行；要换成可执行的格式，请选上面的下拉——选了就覆盖它）";
    $("ps-type-hint").textContent = "决定“这个端点怎么调”（契约 §1 的适配器协议）。当前这个来自预设，不可执行。";
  }
  updateNotExecutableNotice();
}

/** 「本平台暂不能执行该适配器，仅保存配置」提示的显隐 —— 只看**真实的** type。 */
function updateNotExecutableNotice() {
  const bad = !Object.prototype.hasOwnProperty.call(ADAPTER_LABELS, state.currentAdapterType);
  const el = $("ps-not-executable");
  if (bad) {
    el.textContent =
      `⚠️ 本平台暂不能执行该适配器（type=${state.currentAdapterType}），仅保存配置。` +
      `真正要用它时会如实报不可用，不会假装成功。`;
  }
  el.classList.toggle("hidden", !bad);
}

/** 设用途分类（对话/语音/嵌入）。后端对不在支持集的值一律 400，所以这里只有三者。 */
function setProviderTypeSelect(t) {
  const v = (t || "").trim() || "chat_completion";
  const sel = $("ps-provider-type");
  // 未知分类（后端将来多一类）：**不静默改成对话** —— 那样会把语音源存成对话源，
  // 正是本轮要修的 bug。加一个临时选项如实显示，保存时原样送出去让后端裁决。
  if (!Array.prototype.some.call(sel.options, (o) => o.value === v)) {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = `${v}（后端未列入支持集，保存会被拒）`;
    sel.appendChild(o);
  }
  sel.value = v;
}

/* ---------------- 厂商预设（契约 §2/§3/§5.2） ---------------- */

/** 拉预设目录（**失败要响亮** —— 绝不静默渲染成空列表）。 */
async function loadProviderPresets() {
  if (state.presets !== null) return true;      // 已经拉到过（`[]` 也是"拉到过"）
  try {
    const d = await apiData("GET", "/api/v1/provider-presets");
    // 🔴 形状不对就抛（`asArray` 的职责）—— 不许 `d.presets || []` 那样把
    //    "没读懂"伪装成"没有预设"。这个接口的失败是最贵的那一类：
    //    用户会以为"这个平台就是没有预设"，然后手动敲一堆字段。
    state.presets = asArray(d, "presets");
    state.presetsError = "";
    return true;
  } catch (e) {
    // 🔴 失败时**保持 `null`**（不是 `[]`）—— 这是"下次还会重试"与
    //    "永远显示成没有预设"的分水岭。设成 `[]` 会让下一次点「+ 新增」
    //    直接命中上面的缓存短路，**静默渲染一个空列表**（本项目最贵的 bug 类型）。
    state.presets = null;
    state.presetsError = explainProviderError(e);
    return false;
  }
}

/** 打开预设选择弹层。**拉不到就如实报错**，并且仍然允许"不用预设、手动填"。 */
async function openPresetPicker() {
  const box = $("preset-modal");
  box.classList.remove("hidden");
  const list = $("preset-list");
  const hint = $("preset-hint");
  list.innerHTML = "";
  // ⚠️ 先给一句话：`api()` 没有超时，后端若挂住这里会一直空着
  //    → 用户会以为"点了没反应"（本项目最贵的那类观感 bug）。
  hint.textContent = "正在读取厂商预设…";
  hint.style.color = "";
  const ok = await loadProviderPresets();
  if (!ok) {
    // ⚠️ 不要写成 `读取厂商预设失败：HTTP 500（HTTP 500：后端内部错误）` ——
    //    `explainProviderError` 对 5xx 已经补过一句 HTTP 说明，前缀再写一次就重了。
    hint.textContent = `⚠️ 读取厂商预设失败（${state.presetsError}）—— 这不代表"没有预设"，` +
      `是这次没读到；可以再点一次「+ 新增」重试。`;
    hint.style.color = "var(--danger)";
    // 给一条"仍然能干活"的路，而不是把用户堵死在一个空弹层上
    const li = document.createElement("li");
    li.className = "preset-none";
    li.textContent = "预设读不到，可以直接「不用预设，手动填」。";
    list.appendChild(li);
    return;
  }
  hint.style.color = "";
  renderPresetPicker();
}

/** 画预设选择器：**按 provider_type 分组**（契约 §5.2）。 */
function renderPresetPicker() {
  const all = state.presets || [];
  const groups = $("preset-groups");
  const list = $("preset-list");
  const hint = $("preset-hint");
  groups.innerHTML = "";
  list.innerHTML = "";

  if (!all.length) {
    hint.textContent = "后端返回了 0 个预设 —— 可以「不用预设，手动填」。";
    return;
  }
  hint.textContent = `共 ${all.length} 个预设，按用途分组（这一步只是预填，选完还能改）。`;

  // 分组顺序固定：对话 → 语音 → 嵌入 → 其他（不让"其他"排在最前面抢注意力）
  const known = ["chat_completion", "text_to_speech", "embedding"];
  const present = [];
  for (const t of known) if (all.some((p) => p.provider_type === t)) present.push(t);
  for (const p of all) {
    const t = p.provider_type || "未知";
    if (!present.includes(t)) present.push(t);
  }
  if (!present.includes(state.presetGroup)) state.presetGroup = present[0];

  for (const t of present) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "preset-group-btn";
    const n = all.filter((p) => (p.provider_type || "未知") === t).length;
    b.textContent = `${providerTypeLabel(t)}（${n}）`;
    b.classList.toggle("active", t === state.presetGroup);
    b.setAttribute("aria-pressed", t === state.presetGroup ? "true" : "false");
    b.onclick = () => { state.presetGroup = t; renderPresetPicker(); };
    groups.appendChild(b);
  }

  const shown = all.filter((p) => (p.provider_type || "未知") === state.presetGroup);
  for (const p of shown) {
    const li = document.createElement("li");
    li.dataset.presetId = p.id;
    const main = document.createElement("div");
    main.className = "preset-main";
    const lab = document.createElement("div");
    lab.className = "preset-label";
    lab.textContent = p.label || p.id;
    const meta = document.createElement("div");
    meta.className = "preset-meta";
    // 把"会写进表单的东西"**先摆给用户看**：provider / type / api_base
    // （契约 §7.5：只预填这四个 + id 由用户自己填，专属字段一律不预填）
    meta.textContent = `${p.provider || "?"} · ${p.type || "?"}${p.api_base ? " · " + p.api_base : " · （无默认地址）"}`;
    main.append(lab, meta);
    li.appendChild(main);

    // 🔴 能力诚实（契约 §0）：我们只能执行两个 OpenAI 适配器。
    //    不可执行的**照实列出**（用户能看到、能建、字段照填），但必须显式标注。
    const tag = document.createElement("span");
    if (p.executable_by_us === true) {
      tag.className = "preset-tag ok";
      tag.textContent = "可执行";
    } else {
      tag.className = "preset-tag warn";
      tag.textContent = "不可执行";
      tag.title = "本平台暂不能执行该适配器，仅保存配置";
    }
    li.appendChild(tag);

    if (p.note) {
      const note = document.createElement("div");
      note.className = "preset-note";
      note.textContent = p.note;
      li.appendChild(note);
    }
    li.onclick = () => applyPreset(p);
    li.tabIndex = 0;
    li.setAttribute("role", "button");
    li.onkeydown = (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); applyPreset(p); }
    };
    list.appendChild(li);
  }
}

/** 选中一个预设 → **只预填** provider / type / provider_type / api_base（契约 §7.5）。
 *
 *  ⚠️ **不预填** `voice`/`rate`/`embedding_dimensions` 这类专属字段：
 *     本轮只做配置层，那些字段**没有消费方** —— 存了也没人读，
 *     属于"看起来支持、其实没人用"的假能力（契约 §7.5 明确不做）。
 *  ⚠️ id 由用户自己填：预设 id（如 `edge_tts`）是**预设的**标识，
 *     源 id 是用户自己的命名空间，替他决定会让"两个 OpenAI 源"没法共存。
 */
function applyPreset(p) {
  setAdapterType(p.type || "");
  setProviderTypeSelect(p.provider_type || "chat_completion");
  $("ps-api-base").value = p.api_base || "";
  // `provider` 是厂商标签，页面上没有独立输入框 → 存进状态，保存时原样提交
  state.currentProvider = p.provider || "openai";
  const switched = applyPendingProviderTypeFilter(p.provider_type || "chat_completion");

  $("preset-modal").classList.add("hidden");

  // 用户可改 → 焦点直接落到 id 上（那是唯一还需要他填的必填项）
  const hint = $("providers-hint");
  const tail = switched
    ? ` 左侧筛选已切到「${providerTypeLabel(p.provider_type || "chat_completion")}」，保存后能立刻看见。`
    : "";
  if (p.executable_by_us === true) {
    toast(hint, `已套用预设「${p.label || p.id}」（${p.type}）。填个 ID 就能保存。${tail}`);
  } else {
    // 🔴 如实说：**能存，但跑不了**。绝不写成"已就绪/可用"。
    toast(hint,
      `已套用预设「${p.label || p.id}」（${p.type}）：本平台暂不能执行该适配器，仅保存配置。` +
      `填个 ID 可以存下来，但测试连通/会话选模型时会如实报不可用。${tail}`, true);
  }
  $("ps-id").focus();
}

/** 新建源之后，让左侧筛选自动切到"刚建的那一类"（返回 true = 真的切了）。
 *
 * 🔴 为什么要有这一步：用户选了「语音」预设新建一个 TTS 源，
 *    保存成功后如果筛选还停在「对话」，**刚建的源在列表里根本看不见**
 *    —— 用户会以为"没保存成功"，然后再存一遍。这类"东西存进去了但看不见"
 *    的困惑，就是 `hiddenSourceCount` 那句提示要解决的问题的近亲。
 * ⚠️ 这里**不发提示**：调用方（`applyPreset`）要把这句话和"已套用预设"
 *    合并成一条说，否则后一条 toast 会把前一条直接盖掉（实测过这种"消息打架"）。
 */
function applyPendingProviderTypeFilter(pt) {
  if (state.providerFilter === pt) return false;
  state.providerFilter = pt;
  renderProviderSourceList();
  return true;
}

/* ---------------- 写操作：保存 / 新增 / 删除 ---------------- */

/** 表单 → 请求体。🔴 key **只在本次真的填了**时才带上（留空 = 不修改）。
 *
 * 🔴 这里曾经**硬编码** `provider_type:"chat_completion"` / `provider:"openai"` /
 *    `type:"openai_chat_completion"` —— 后果是**一个 TTS 源被静默存成对话源**
 *    （用户看到"保存成功"，库里却是另一个东西；契约 §5.4 点名的就是这个）。
 *    现在三个字段一律取自**表单/预设的真实值**：
 *      · `type`          ← `state.currentAdapterType`（两个 OpenAI 格式来自下拉，
 *                          其余来自预设只读展示，**原样送出、不降级**）
 *      · `provider_type` ← `#ps-provider-type` 下拉
 *      · `provider`      ← `state.currentProvider`（预设带的厂商标签；手填时默认 openai）
 */
function collectSourcePayload() {
  const timeoutRaw = $("ps-timeout").value.trim();
  const timeout = timeoutRaw === "" ? 120 : Number(timeoutRaw);
  const key = $("ps-api-key").value.trim();
  const body = {
    id: $("ps-id").value.trim(),
    // ★ 真实值（不再硬编码）
    provider_type: $("ps-provider-type").value || "chat_completion",
    provider: state.currentProvider || "openai",
    type: state.currentAdapterType || "openai_chat_completion",
    api_base: $("ps-api-base").value.trim(),
    timeout: Number.isFinite(timeout) && timeout > 0 ? timeout : 120,
    proxy: $("ps-proxy").value.trim(),
    custom_headers: headersFromRows(),
    // 本页没有"停用源"的开关 → 保持它原来的值（新建默认启用）
    enable: state.sourceEnable === false ? false : true,
  };
  if (key) body.key = [key];     // ★ 列表形状（照真实契约：key 是数组）
  return body;
}

async function saveProviderSource() {
  const hint = $("providers-hint");
  const body = collectSourcePayload();

  // 客户端先校验一遍：能当场说清的错误不必绕一圈后端（后端仍会再校验）
  if (!body.id) { toast(hint, "ID 必填。", true); $("ps-id").focus(); return; }
  if (!/^https?:\/\//i.test(body.api_base)) {
    toast(hint, "API Base URL 必填，且必须以 http:// 或 https:// 开头。", true);
    $("ps-api-base").focus();
    return;
  }

  const btn = $("provider-save-config");
  const creating = !!state.creatingSource;
  const oldId = state.selectedSourceId;
  btn.disabled = true;
  toast(hint, "保存中…");
  try {
    if (creating) {
      await api("POST", "/api/v1/provider-sources", body);
    } else {
      // ⚠️ 用**原来的 id** 定位（表单里改 ID = 重命名）、body 里带新 id
      await api("PUT", `/api/v1/provider-sources/by-id?source_id=${encodeURIComponent(oldId)}`, body);
    }
    state.creatingSource = false;
    // 用户主动保存 → 必须**真的**重读一次（不能复用 once 缓存）
    await loadProviderSourcesFresh();
    const savedMsg = creating ? `已新增 ${body.id}。` : `已保存 ${body.id}。`;
    // ⚠️ 把确认语**交给 selectProviderSource 在最后写**（它内部还有 load/渲染，
    //    现在写会被后续的 hint 更新盖掉）。见该函数的 `opts.saved`。
    if (!(await selectProviderSource(body.id, { skipHash: false, saved: savedMsg }))) {
      // 保存成功了但列表里没读到（后端可能只写了一半）→ 如实说，别假装成功
      toast(hint, `已提交 ${body.id}，但列表里读不到它 —— 请刷新或检查后端。`, true);
      return;
    }
  } catch (e) {
    toast(hint, `保存失败：${explainProviderError(e)}`, true);
  } finally {
    btn.disabled = false;
  }
}

async function deleteProviderSource(id, btn) {
  if (!id) return;
  if (!confirm(`删除提供商源「${id}」？挂在它下面的模型也会一起失效。`)) return;
  const hint = $("providers-hint");
  if (btn) btn.disabled = true;
  try {
    await api("DELETE", `/api/v1/provider-sources/by-id?source_id=${encodeURIComponent(id)}`);
    if (state.selectedSourceId === id) { clearProviderDetail(); go("providers"); }
    await loadProviderSourcesFresh();
    toast(hint, `已删除 ${id}。`);
  } catch (e) {
    toast(hint, `删除失败：${explainProviderError(e)}`, true);
    if (btn) btn.disabled = false;
  }
}

/* ---------------- 模型：获取列表 / 添加 / 移除 / 测试 ----------------
 *
 * 🔴 「获取模型列表」与「测试」的结果都走**弹窗**（情绪 2026-09-21：
 *    「获取到的模型，与测试都用弹窗形式发送」）。改成弹窗要守住三条，否则会退回旧毛病：
 *      ① 弹层里放的是**原来那些节点**（`ps-model-search` / `ps-available-hint` /
 *         `ps-available-list` / `providers-test-result`），不是复制一份 ——
 *         复制一份 = 两个 id、两处真相，`$()` 只认第一份，另一份永远不更新；
 *      ② **失败也必须打开**弹层并把原因写进 hint —— 不许"失败 → 什么都没弹"，
 *         那正是本项目最贵的静默失败；
 *      ③ 卡片里 `#ps-configured-list`（已配置的模型）**留在原处** ——
 *         那是用户的工作列表，不是一次性的查询结果。
 */

/** 打开「可用模型」弹层。
 *
 *  ⚠️ 弹层先开、再拉数据（不是拉完才开）：拉取要等上游，用户点完按钮若界面毫无变化，
 *     第一反应是"按钮坏了"，然后就会去连点。
 *  ⚠️ 副标题里带上源 id：弹层是浮层，脱离卡片上下文，不写清楚就不知道这是**哪个源**的结果。
 */
function openModelsModal() {
  $("models-subtitle").textContent = state.selectedSourceId
    ? `源：${state.selectedSourceId}（拉取的是该源 API Base URL 下的模型）`
    : "还没选中提供商源。";
  $("models-modal").classList.remove("hidden");
}
function closeModelsModal() { $("models-modal").classList.add("hidden"); }

/** 打开「模型测试」弹层，并把进度/结果节点复位。
 *
 *  ⚠️ 这里**明写白色**（`#providers-test-result` 的 color）：它复用的是 `.preview`
 *     的深色代码块皮肤，而 `toast()` 会把它染成 `var(--danger)` 表示失败；
 *     不还原的话，下一次**成功**的测试会继承上次的红色（"✅ 连通正常"是红的），
 *     用户会以为又失败了。progress 用 muted、结果由 toast 上色。
 */
function openTestModal(pid) {
  $("test-subtitle").textContent = `模型：${pid}`;
  const out = $("providers-test-result");
  out.textContent = "";
  out.style.color = "";
  $("test-modal").classList.remove("hidden");
}
function closeTestModal() { $("test-modal").classList.add("hidden"); }

async function fetchSourceModels() {
  const id = state.selectedSourceId;
  const hint = $("ps-available-hint");
  const btn = $("ps-fetch-models");
  // 🔴 把上一次的结果清干净再开：留着的话"新一次拉取"会先显示旧候选，
  //    用户分不清屏幕上的是这次的还是上次的（尤其是刚换了搜索词的场景）。
  state.availableModels = null;
  renderAvailableModels();
  $("ps-model-search").value = "";
  // 弹层**先开**（见 openModelsModal 注释）；失败路径也不会关它，原因写在 hint 里。
  openModelsModal();
  if (!id) { toast(hint, "先选中（或保存）一个提供商源。", true); return; }
  btn.disabled = true;
  toast(hint, `正在从 ${id} 拉取模型列表…`);
  try {
    const d = await apiData("GET", `/api/v1/provider-sources/${encodeURIComponent(id)}/models`);
    state.sourceModelMetadata = (d && d.model_metadata) || {};
    state.availableModels = asArray(d, "models");
    renderConfiguredModels();       // 顺带刷新勾选态（已配置的候选要打勾/锁定）
    if (!state.availableModels.length) {
      // 🔴 空列表**不算成功**（本项目最贵的 bug 类型就是"静默成功"）
      toast(hint, `端点返回了 0 个模型（${id}）—— 检查 API Base URL 与 Key 是否正确。`, true);
    } else {
      toast(hint, `可用模型 ${state.availableModels.length} 个（已配置的会打勾）。`);
    }
  } catch (e) {
    state.availableModels = [];
    renderAvailableModels();
    toast(hint, `获取模型列表失败：${explainProviderError(e)}`, true);
  } finally {
    btn.disabled = false;
  }
}

async function addProviderModel(name) {
  const id = state.selectedSourceId;
  const hint = $("ps-available-hint");
  if (!id) { toast(hint, "先选中一个提供商源。", true); return; }
  try {
    // 两种后端形状都兼容：`model`（源下加模型）和 `id`（完整 `源/模型`）
    await api("POST", `/api/v1/provider-sources/${encodeURIComponent(id)}/providers`, {
      id: `${id}/${name}`,
      model: name,
      provider_source_id: id,
      enable: true,
    });
    // 🔴 `loadConfiguredModels()` 内部会重画**卡片里那份**已配置列表（`#ps-configured-list`）
    //    并回调 `renderAvailableModels()` 刷新勾选态 —— 所以添加完，卡片的列表
    //    和弹层里的勾选是**同时**更新的，不会让用户对着一张过期的列表发呆。
    await loadConfiguredModels(id);
    toast(hint, `已添加 ${id}/${name}。`);
  } catch (e) {
    toast(hint, `添加「${name}」失败：${explainProviderError(e)}`, true);
  }
}

async function removeProviderModel(pid) {
  if (!confirm(`移除模型「${pid}」？`)) return;
  const hint = $("ps-available-hint");
  try {
    await api("DELETE", `/api/v1/providers/${encodeURIComponent(pid)}`);
    await loadConfiguredModels(state.selectedSourceId);
    toast(hint, `已移除 ${pid}。`);
  } catch (e) {
    toast(hint, `移除「${pid}」失败：${explainProviderError(e)}`, true);
  }
}

/** 测试某个模型：成功显示延迟/样例，失败**逐条说清原因**。 */
async function testProviderModel(pid, btn) {
  const out = $("providers-test-result");
  if (btn) btn.disabled = true;
  // 🔴 弹层在**发请求之前**打开（情绪要求测试走弹窗）：
  //    测试要等上游，几秒钟里界面必须已经有反馈，否则用户只会重复点按钮。
  //    ⚠️ 用户可能在等待期间关掉弹层 —— 结果照样写回 `#providers-test-result`
  //      （节点没被销毁，只是父层隐藏），下次打开就是最新结果，不会丢。
  openTestModal(pid);
  toast(out, `正在测试 ${pid} …`);
  try {
    // 🔴 用 `apiData`（它会拆掉 `{status,message,data}` 信封）——
    //    ⚠️ 我原来用 `api()` 拿到的是**整个信封**，于是：
    //      ① `d.status` 读到的其实是**信封的 status**（恒为 "ok"）→ 判定全乱；
    //      ② 失败时 `d.reason` 不存在，回退到 `d.status` → 界面显示「原因：ok」
    //         （用户看到的就是"测试失败 / 原因：ok"这种莫名其妙的话）。
    //    真实契约（`ASTRBOT_PROVIDER_CONTRACT.md` §3）：data 里是
    //      `{id, model, status: "available"|"unavailable", error}`。
    const d = await apiData("POST", `/api/v1/providers/${encodeURIComponent(pid)}/test`, {});
    const okStatus = !!(d && d.status === "available");
    // 兼容 `{ok:true}` 形态（万一后端同时给）——但**以 status 为准**
    const passed = okStatus || (d && d.ok === true);
    if (passed) {
      out.textContent =
        `✅ ${pid} 连通正常\n` +
        `模型：${(d && (d.model || d.id)) || "?"}\n` +
        `延迟：${(d && d.latency_ms) ?? "?"} ms\n` +
        `样例：${(d && d.sample) || "（无样例）"}`;
    } else {
      // 失败原因**如实**取 `error`（契约字段），取不到才退到 status
      const reason = (d && (d.error || d.message)) || "";
      out.textContent =
        `❌ ${pid} 测试失败\n` +
        `原因：${reason || (d && d.status) || "unknown"}\n` +
        `说明：${reason ? "" : "（后端未给出说明）"}`;
    }
  } catch (e) {
    out.textContent = `❌ ${pid} 测试失败\n原因：HTTP/网络错误\n说明：${explainProviderError(e)}`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ---------------- 事件绑定（页面级，脚本只加载一次） ---------------- */

// 左侧列表：删除用**一个**委托处理器（列表会随重渲染重建，逐项直接绑会丢）
$("provider-source-list").onclick = (ev) => {
  const btn = ev.target.closest("button[data-del]");
  if (!btn) return;
  deleteProviderSource(btn.dataset.del, btn);
};

// 已配置模型行内的「测试 / 移除」同样用委托
$("ps-configured-list").onclick = (ev) => {
  const btn = ev.target.closest("button[data-op]");
  if (!btn) return;
  if (btn.dataset.op === "test") testProviderModel(btn.dataset.id, btn);
  else if (btn.dataset.op === "del") removeProviderModel(btn.dataset.id);
};

// 「+ 新增」= ①先把**空表单**摆好 + ②再在上面弹**厂商预设**选择（契约 §5.2）。
//
// 🔴 顺序与"为什么不只弹弹层"（这点很容易做错，写清楚）：
//    ① 必须**先**把空表单摆好（`creatingSource=true` + 清空字段）。
//       否则用户在弹层里点「不用预设，手动填」之前，右侧还停在上一个源的详情上，
//       此时如果点了「保存配置」，就会被当成**改那个源**（PUT 改名）——
//       明明是"新增"，结果改掉了别人。这是数据损坏级的错误。
//    ② 弹层只是**预填的捷径**：它盖在上面，选完就消失，表单一直是可编辑的真身。
//       → 用户点「关闭」/点遮罩，等价于"不用预设"，不会卡在半路。
function newProviderSourceFlow() {
  startNewProviderSource();
  openPresetPicker();
}

/** 关闭预设弹层（关闭 = 不用预设；表单已经摆好了，用户直接手填）。 */
function closePresetPicker() {
  $("preset-modal").classList.add("hidden");
  // 🔴 「不用预设」= 用户明确表示"我要自己填" → 厂商标签回落到通用默认。
  //    ⚠️ 必须在这里清：`startNewProviderSource()` 里**不能**清（那会把刚
  //       套用的预设标签抹掉，见该函数的注释）。两处职责不同，别合并。
  state.currentProvider = "openai";
  $("ps-id").focus();
}

$("provider-source-new").onclick = () => newProviderSourceFlow();
$("provider-source-back").onclick = () => {
  clearProviderDetail();
  go("providers");
};
$("provider-save-config").onclick = () => saveProviderSource();
$("provider-delete").onclick = () => deleteProviderSource(state.selectedSourceId, $("provider-delete"));
$("ps-fetch-models").onclick = () => fetchSourceModels();
$("ps-header-add").onclick = () => addHeaderRow("", "");
$("ps-model-search").oninput = () => renderAvailableModels();

// ---- 类型筛选（契约 §5.1）：四个按钮一个委托处理器 ----
// ⚠️ 用委托而不是逐按钮绑定：以后加一类（如 STT）只需在 HTML 里加个按钮，
//    不会出现"新按钮忘了绑事件、点了没反应"这种静默失效。
$("provider-type-filter").onclick = (ev) => {
  const b = ev.target.closest(".filter-btn");
  if (!b) return;
  setProviderFilter(b.dataset.ptype || PROVIDER_FILTER_ALL);
};

// ---- 适配器类型下拉：切换后**同步影子状态**（保存时用的是它，不是 select 的 value）----
$("ps-type").onchange = () => setAdapterType($("ps-type").value);

// ---- 厂商预设弹层（契约 §5.2）----
$("preset-close").onclick = () => closePresetPicker();
$("preset-modal").onclick = (e) => {
  if (e.target.id === "preset-modal") closePresetPicker();
};
$("preset-skip").onclick = () => closePresetPicker();

// ---- 「可用模型」/「模型测试」弹层（情绪 2026-09-21：结果走弹窗）----
// 关闭三件套与页面既有弹层**完全同构**：关闭按钮 + 点遮罩（判 `e.target.id` 是**遮罩本身**
// 才关，否则点弹层内部任何空白都会误关）+ Esc（见上面的 keydown 表）。
$("models-close").onclick = () => closeModelsModal();
$("models-modal").onclick = (e) => {
  if (e.target.id === "models-modal") closeModelsModal();
};
$("test-close").onclick = () => closeTestModal();
$("test-modal").onclick = (e) => {
  if (e.target.id === "test-modal") closeTestModal();
};

// 「自定义模型」：点开一个内联输入框（**不用** window.prompt —— 阻塞且不可测）
$("ps-add-custom").onclick = () => {
  const wrap = $("ps-custom-wrap");
  wrap.classList.toggle("hidden");
  if (!wrap.classList.contains("hidden")) $("ps-custom-model").focus();
};
$("ps-custom-add").onclick = async () => {
  const name = ($("ps-custom-model").value || "").trim();
  const hint = $("ps-available-hint");
  if (!name) { toast(hint, "填一个模型 ID，例如 gpt-5.6-sol。", true); return; }
  await addProviderModel(name);
  $("ps-custom-model").value = "";
};
$("ps-custom-model").onkeydown = (ev) => {
  if (ev.key === "Enter") { ev.preventDefault(); $("ps-custom-add").click(); }
};

// 表单里回车 = 保存（否则会触发表单默认提交 → 整页刷新，丢掉未保存状态）
$("provider-source-form").onsubmit = (ev) => {
  ev.preventDefault();
  saveProviderSource();
};

/* ================= 弹层 ================= */
function showModal(title, body) {
  $("modal-title").textContent = title;
  $("modal-body").textContent = body;
  $("modal").classList.remove("hidden");
}
$("modal-close").onclick = () => $("modal").classList.add("hidden");
$("modal").onclick = (e) => { if (e.target.id === "modal") $("modal").classList.add("hidden"); };

/* ================= 插件拓展页 =================
 *
 * 🔴 为什么有这个页面（情绪报的 bug）：
 *    「插件的配置入口没有喵」—— 根因是**宿主只挂了插件的数据 API、
 *      没挂插件的页面**。`metadata.yaml` 里声明了 `pages:` 的插件
 *      （陪伴面板 / QQ 空间与书柜 / 一起房间）在面板里完全没有入口。
 *    后端补了三条路由（见 `emotion_app/web/plugin_pages_web.py`），
 *    这里就是它的界面。
 *
 * 用 **iframe** 而不是把插件 HTML 直接注进来：
 *   * 插件页有自己的 `<style>`/全局变量（实测 `*` 选择器、`html{}` 主题），
 *     注进来会**污染整个面板**；
 *   * 插件页有自己的 JS 全局（`state`、`HTTP_API`…），与我们的 `state` 冲突；
 *   * iframe 天然隔离，且插件页的**相对资源路径**照常工作。
 *   ⚠️ 同源 iframe：cookie 会带上，所以插件页的 fetch 天然是已登录的。
 */

/* ---------------- 插件启停（照真 AstrBot 的 inactivated_plugins） ----------------
 *
 * 🔴 三条设计取舍（都会直接影响"用户看到的对不对"）：
 *
 * ① **勾选框显示的是"已保存的意图"，不是"当前运行态"**。
 *    若跟着运行态走：用户取消勾选 → 保存 → 运行态还没变（要重启）→
 *    勾选框自己跳回去，用户会以为"没保存成功"。
 *    所以：勾选 = 保存的名单，旁边**另用状态标签**说"现在跑没跑"。
 *
 * ② **"需重启生效"是后端算的**（`restart_required` = 保存集合 != 运行集合），
 *    不是这里写死的 true —— 所以重启后再打开这一页，它会自己消失。
 *    我们**没有**做"保存即生效"：真 AstrBot 的 `turn_on_plugin` 也是
 *    `await self.reload(plugin_name)`（要重载），我们照它，如实在界面上说明。
 *
 * ③ **失败不许显示成"没有插件"**：接口 503（宿主没起来）时，
 *    提示里写清原因并**清空列表** —— 绝不保留上一次的旧列表。
 */

/** 读插件清单 + 已保存的启停名单（进 `#/plugins` 时调）。 */
async function loadPluginSwitch() {
  const hint = $("plugin-switch-hint");
  const host = $("plugin-switch-list");
  try {
    const d = await api("GET", "/api/v1/plugins");
    state.pluginSwitch = d;
    renderPluginSwitch();
    // 🔴 侧边栏的插件分组**跟这一份数据同源**（同一次请求的结果）——
    //    它是"哪个插件有拓展页"的唯一数据来源，另开一条请求就会漂。
    renderNavPluginPages(d);
    hint.style.color = "";
  } catch (e) {
    // 🔴 读失败 → 说清原因 + 清空（不把"没读到"显示成"没有插件"）
    state.pluginSwitch = null;
    host.innerHTML = "";
    // ⚠️ 侧边栏也要清掉：留着上一次的分组会显示**已经不存在的插件**，
    //    点进去 404（比"没有"更糟 —— 用户会以为插件还在）。
    renderNavPluginPages(null);
    hint.textContent = `读取插件清单失败：${e.message}`;
    hint.style.color = "var(--danger)";
  }
}

/* ================================================================================
 * 侧边栏的「插件页面」分组（情绪 2026-10-09）
 *
 * 「在插件页点击就有了，不过做成 astrbot 那样也行，就是在侧边栏添加这个。」
 * ⇒ 照真 AstrBot：侧边栏「插件」下面按插件列出**有拓展页的那些**。
 *
 * 🔴 **完全不写死插件名**（情绪明确要求：「以后我自己添加了插件也能自己显示」）：
 *    数据来自 `GET /api/v1/plugins` 的 `plugins[].pages[]` + `display_name`，
 *    装了新插件且它带 `pages/`，刷新面板就自己出现。
 *
 * ## 真 AstrBot 的做法（对照，`InstalledPluginsTab-DSnl1LUd.js:1`）
 *    侧边栏那一项点的是 `pages[0]`（**只取第一个**）。
 *    ⚠️ 我们**列全部**：配套插件有 2 个页面（`companion-panel` + `陪伴面板`），
 *       只给一个会把另一个变成"只能从插件页进"的半残（真 AstrBot 就是这样）。
 *       我们的插件页本来就能列出全部，侧边栏对齐它更一致。
 * ================================================================================ */

const NAV_PLUGIN_OPEN_KEY = "emotion.navPluginGroupOpen";

/** 画侧边栏的插件分组。`d` 为 `null` = 读失败/没数据 ⇒ 整块隐藏。 */
function renderNavPluginPages(d) {
  const group = $("nav-plugin-group");
  const box = $("nav-plugin-items");
  if (!group || !box) return;

  // 🔴 **列的资格（两条，缺一不可）**：
  //    ① **真的有拓展页** —— 没页面的插件在侧边栏里没有可点的东西；
  //    ② **`state === "loaded"`（正在运行）** —— 2026-10-10 修，情绪当场指出。
  //
  // 我第一版只判了 ①，结果把 `meme_manager` 也列了出来 —— 而它 `state=failed`
  // （`ModuleNotFoundError: No module named 'requests'`），**从加载那天起就没跑起来过**
  // ⇒ 点进去 iframe 一片空白（页面由插件提供，插件没跑自然没有页面）。
  //
  // ⚠️ 更糟的是它**看起来像我们的功能坏了**：用户不会想到"这是别人 AstrBot 的插件、
  //    而且没装上依赖"，只会觉得"情绪app 的插件页面是坏的"。
  //    情绪原话：「表情管理器我们平台现在哪来的嘛」—— 他说得对，
  //    那不是我们平台装的插件，不该出现在我们的导航里。
  //
  // ⚠️ 停用（inactivated）/ 失败（failed）/ 未加载（not_loaded）**都不列**。
  //    它们在「插件」页里照样看得见（那里本来就该显示"为什么没跑"），
  //    所以**不是隐藏问题**，只是不给一个点不开的导航入口。
  const plugins = ((d && d.plugins) || []).filter(
    (p) => Array.isArray(p.pages) && p.pages.length > 0 && p.state === "loaded",
  );

  // 🔴 **分成两段：「本平台自带」与「外部插件」**（2026-10-10 情绪定）。
  //
  //    情绪原话：「只做目前有的插件然后给接口，**安装插件后有独立 ui 的才在这显示**」。
  //    我们的插件源目录是**可扩展**的（往 `plugins/` 里放 + 重启就会加载，
  //    见 `tests/test_plugin_dir_extensibility.py`），所以以后装进来的插件**也应该**
  //    出现在这里 —— 但那不是"我们平台自带的"，得让人一眼分得清。
  //
  //    ⚠️ 判据必须是**外部依据**（不能只做"和接口数据一样"的集合比较 ——
  //       本项目已记：那样两边同源、必然一致，证明不了"该不该列"）。
  //       这里的外部依据 = **模块路径**：我们的自带插件是**仓库内的顶层包**
  //       （`astrbot_plugin_xxx.main`，`plugins/` 就在仓库根），
  //       而外部装进来的插件一定带**命名空间前缀**（如 `data.plugins.xxx.main`）。
  //       于是"哪些是自带的"由**代码事实**决定，不由我看一眼名单拍板。
  const isBundled = (p) => {
    const mp = String(p.module_path || "");
    if (!mp) {
      // ⚠️ 拿不到模块路径时**不猜**：宁可归到"外部"，也不能把外来的说成我们的。
      return false;
    }
    return /^astrbot_plugin_[A-Za-z0-9_]+\.main$/.test(mp);
  };

  box.innerHTML = "";
  if (!plugins.length) {
    // 一个都没有 ⇒ 整块不显示（空分组是纯噪音）
    group.classList.add("hidden");
    return;
  }
  group.classList.remove("hidden");

  const subTitle = (text) => {
    const h = document.createElement("div");
    h.className = "nav-subhead";
    h.textContent = text;
    box.appendChild(h);
  };

  /** 画一个插件的全部页面为侧边栏子项。 */
  const renderOne = (p) => {
    // ⚠️ 显示名：`display_name` 可能为空（不是每个插件都声明）→ 退化成 name。
    //    **必须在显示时退化**：数据层保持"有没有声明"这个区别。
    const title = (p.display_name || "").trim() || p.name || p.id;
    // 一个插件可能有多个页面 → 生成多条子项（带页面名区分）
    const multi = p.pages.length > 1;
    for (const pg of p.pages) {
      const a = document.createElement("a");
      a.className = "nav-subitem";
      a.href = `#/plugins/${encodeURIComponent(p.id)}/${encodeURIComponent(pg.name)}`;
      a.dataset.navPage = `${p.id}/${pg.name}`;
      a.title = multi ? `${title} · ${pg.title || pg.name}` : title;
      const ico = document.createElement("span");
      ico.className = "nav-subico";
      ico.setAttribute("aria-hidden", "true");
      ico.textContent = "🧩";
      a.appendChild(ico);
      const lb = document.createElement("span");
      lb.className = "nav-sublabel";
      // 多页面时把页面名带上，否则两条同名分不出来
      lb.textContent = multi ? `${title} · ${pg.title || pg.name}` : title;
      a.appendChild(lb);
      box.appendChild(a);
    }
  };

  const bundled = plugins.filter(isBundled);
  const external = plugins.filter((p) => !isBundled(p));
  // 两段都只有一类时**不画小标题**（单个分组下的小标题是纯噪音）。
  if (bundled.length && external.length) {
    subTitle("本平台自带");
  }
  for (const p of bundled) {
    renderOne(p);
  }
  if (bundled.length && external.length) {
    subTitle("外部插件");
  }
  for (const p of external) {
    renderOne(p);
  }
  syncNavPluginGroupOpen();
}

/** 侧边栏插件子项的**选中态**：按当前 hash 高亮对应的那一条。
 *
 * 🔴 为什么按 hash 判而不是按 `name`：`showPanel("plugins")` 这个 `name`
 *    同时服务「插件列表」和「某个插件页面」两种状态 ——
 *    光有 `name` 分不出用户在哪一条子项上。
 * ⚠️ 高亮子项时**同时点亮「插件」那一项**：用户视角是"我在插件这个大区里"，
 *    两个都暗着会让人以为导航没跟上。
 */
function syncNavPluginActive() {
  const raw = (location.hash || "").replace(/^#\/?/, "");
  const parts = raw.split("/").filter(Boolean).map(decodeURIComponent);
  // `#/plugins/<id>/<page>` → 有 page 才是"在看某个插件页面"
  const want = (parts[0] === "plugins" && parts[1] && parts[2])
    ? `${parts[1]}/${parts[2]}` : "";
  document.querySelectorAll(".nav-subitem").forEach((a) => {
    const on = !!want && a.dataset.navPage === want;
    a.classList.toggle("active", on);
    if (on) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  if (want) {
    const parent = document.querySelector('.nav-item[data-nav="plugins"]');
    if (parent) parent.classList.add("active");
  }
}

/** 展开/收起态：**记在 localStorage**，切页与刷新都不该自己弹开。 */
function syncNavPluginGroupOpen() {
  const group = $("nav-plugin-group");
  const btn = $("nav-plugin-toggle");
  const box = $("nav-plugin-items");
  if (!group || !btn || !box) return;
  const open = localStorage.getItem(NAV_PLUGIN_OPEN_KEY) === "1";
  group.classList.toggle("open", open);
  box.classList.toggle("hidden", !open);
  btn.setAttribute("aria-expanded", open ? "true" : "false");
}

const _navPluginToggle = $("nav-plugin-toggle");
if (_navPluginToggle) {
  _navPluginToggle.onclick = (ev) => {
    ev.preventDefault();
    const open = localStorage.getItem(NAV_PLUGIN_OPEN_KEY) === "1";
    localStorage.setItem(NAV_PLUGIN_OPEN_KEY, open ? "0" : "1");
    syncNavPluginGroupOpen();
  };
}

/** 按 `state.pluginSwitch` 重画勾选列表 + 提示。 */
function renderPluginSwitch() {
  const d = state.pluginSwitch;
  const hint = $("plugin-switch-hint");
  const host = $("plugin-switch-list");
  host.innerHTML = "";
  if (!d) return;

  const plugins = d.plugins || [];
  if (!plugins.length) {
    hint.textContent = "没有发现任何插件（检查 plugins.plugin_dirs）—— 这不是「读取失败」。";
    return;
  }

  const STATE_TEXT = {
    loaded: "运行中",
    inactivated: "已停用（未加载）",
    failed: "加载失败",
    not_loaded: "未加载",
  };
  for (const p of plugins) {
    const row = document.createElement("label");
    row.className = "switch-row";
    row.dataset.pluginId = p.id;
    row.dataset.modulePath = p.module_path;

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "switch-cb";
    // 勾选 = 启用（= 不在停用名单里）；这里用的是**保存的意图**
    cb.checked = p.enabled_saved !== false;
    row.appendChild(cb);

    const name = document.createElement("span");
    name.className = "switch-name";
    name.textContent = p.name || p.id;
    row.appendChild(name);

    const id = document.createElement("span");
    id.className = "switch-id";
    id.textContent = p.module_path;
    row.appendChild(id);

    const st = document.createElement("span");
    const bad = p.state === "failed";
    st.className = "switch-state" + (bad ? " bad" : "");
    st.textContent = STATE_TEXT[p.state] || p.state;
    // 失败原因要看得见（否则用户只知道"没跑"，不知道"为什么"）
    if (bad && p.error) st.title = p.error;
    row.appendChild(st);

    // 🔴 「详情」按钮：这是**唯一**进详情页的入口（照真 AstrBot：点卡片进详情）。
    //    ⚠️ 必须 `type="button"` + `preventDefault()` + `stopPropagation()`：
    //       整行是 `<label>`，点它**会切换那个勾选框** ——
    //       不挡的话"看详情"会顺手把插件停用/启用（静默改配置，本项目最忌）。
    const detail = document.createElement("button");
    detail.type = "button";
    detail.className = "link-btn switch-detail";
    detail.textContent = "详情";
    detail.onclick = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      go(`plugins/${encodeURIComponent(p.id)}`);
    };
    row.appendChild(detail);

    // 🔴 「配置」按钮（情绪 2026-10-09：「有些插件没有内置的独立 webui
    //    就可以打开插件的配置」）。
    //    ⚠️ 与「详情」按钮**同款三条防护**，一条都不能少：
    //       `type="button"` + `preventDefault()` + `stopPropagation()` ——
    //       整行是 `<label>`，不挡的话"打开配置"会**顺手把插件停用/启用**
    //       （静默改配置，本项目最忌的那类 bug）。
    const cfgBtn = document.createElement("button");
    cfgBtn.type = "button";
    cfgBtn.className = "link-btn switch-config";
    cfgBtn.textContent = "配置";
    cfgBtn.title = "打开这个插件的配置";
    cfgBtn.onclick = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      // ⚠️ 也走 hash 路由（与"详情"一致）：这样能后退、能分享、刷新还在
      go(`plugins/${encodeURIComponent(p.id)}/config`);
    };
    row.appendChild(cfgBtn);

    if (bad && p.error) {
      const err = document.createElement("small");
      err.className = "switch-err";
      err.textContent = p.error;
      row.appendChild(err);
    }
    host.appendChild(row);
  }

  // ---- 提示（生效时机 + 对不上的名单项）----
  const parts = [`共 ${plugins.length} 个插件。取消勾选 = 停用（不加载、不初始化、也不算失败）。`];
  if (d.restart_required) {
    parts.push("⚠️ 有改动**还没生效**：需要重启服务（./run.sh serve）后才会应用。");
  } else {
    parts.push("当前设置与运行状态一致（无需重启）。");
  }
  if ((d.unmatched || []).length) {
    parts.push(`⚠️ 名单里有对不上任何插件的项（拼写错误？）：${d.unmatched.join(", ")}`);
  }
  hint.textContent = parts.join(" ");
  // 有"待重启 / 名单对不上"⇒ 警告色；否则交还样式表（不写主题色）
  hint.style.color = d.restart_required || (d.unmatched || []).length ? "var(--warn)" : "";
}

/** 保存启停设置（整体替换停用名单）。 */
async function savePluginSwitch() {
  const saved = $("plugin-switch-saved");
  const d = state.pluginSwitch;
  if (!d) {
    toast(saved, "还没读到插件清单，无法保存。", true);
    return;
  }
  // 未勾选 = 停用 → 提交的就是停用名单（用后端给的 module_path，不自己拼）
  const off = [];
  document.querySelectorAll("#plugin-switch-list .switch-row").forEach((row) => {
    const cb = row.querySelector("input.switch-cb");
    if (cb && !cb.checked) off.push(row.dataset.modulePath);
  });
  try {
    const out = await api("PUT", "/api/v1/plugins/inactivated", {
      inactivated_plugins: off,
    });
    state.pluginSwitch = out;
    renderPluginSwitch();          // 用后端回来的真值重画（不假设自己写对了）
    toast(saved, `已保存：停用 ${off.length} 个插件。` +
      (out.restart_required ? "重启服务后生效。" : "（当前状态已一致）"));
  } catch (e) {
    toast(saved, `保存失败：${e.message}`, true);
  }
}

$("plugin-switch-save").onclick = savePluginSwitch;

async function loadPluginPages() {
  const ul = $("plugin-page-list");
  const hint = $("plugins-hint");
  try {
    const d = await api("GET", "/api/v1/plugins/pages");
    const pages = d.pages || [];
    state.pluginPages = pages;

    const badge = $("plugins-badge");
    if (pages.length) { badge.textContent = String(pages.length); badge.classList.remove("hidden"); }
    else badge.classList.add("hidden");

    if (!pages.length) {
      hint.textContent = "没有发现插件拓展页。可能原因：插件宿主没启用（EMOTION_PLUGINS_ENABLED），或插件没有声明 pages。";
      ul.innerHTML = "";
      return;
    }
    hint.textContent = `共 ${pages.length} 个插件拓展页，点一个打开：`;
    ul.innerHTML = "";
    for (const p of pages) {
      const li = document.createElement("li");
      li.tabIndex = 0;
      const t = document.createElement("span");
      t.textContent = p.title || p.page_name;
      const pid = document.createElement("span");
      pid.className = "pid";
      pid.textContent = p.plugin_id;
      li.append(t, pid);
      const openIt = () => go(`plugins/${encodeURIComponent(p.plugin_id)}/${encodeURIComponent(p.page_name)}`);
      li.onclick = openIt;
      li.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openIt(); } };
      ul.appendChild(li);
    }
  } catch (e) {
    hint.textContent = "读取插件页失败：" + e.message;
    hint.style.color = "var(--danger)";
    ul.innerHTML = "";
  }
}

/** 在 iframe 里打开一个插件拓展页。 */
function openPluginPage(pluginId, pageName, opts) {
  const pages = state.pluginPages || [];
  const meta = pages.find((p) => p.plugin_id === pluginId && p.page_name === pageName);
  // 🔴 末尾必须有 `/` —— 插件页里的 `./app.js` 是按"当前 URL 的目录"解析的，
  //    少了斜杠会打到上一级去（后端对无斜杠的请求做了 308 重定向兜底）。
  const url = `/api/v1/plugins/${encodeURIComponent(pluginId)}/pages/${encodeURIComponent(pageName)}/`;

  $("plugin-frame-title").textContent = meta ? (meta.title || pageName) : pageName;
  $("plugin-frame-open").href = url;
  $("plugin-frame").src = url;
  state.pluginCurrentId = pluginId;
  showPluginsView("frame");
  // 🔴 **必须补这两句**：点侧边栏子项是**直接调**这个函数的（不经过 `applyRoute`），
  //    而侧边栏的「哪个子项被选中」是 `showPanel` 里算的 ⇒ 不补的话
  //    点侧边栏进来**选中态不会跟上**（真机判据 B2 抓到的：
  //    "选中子项 0 条，而父项点亮了" —— 父项是另一条路径恰好点亮的）。
  //    ⚠️ 顺序：先 `showPanel` 再 `go` —— `go` 会写 hash 触发 `applyRoute`，
  //       那条路径也会 `showPanel`，两边结果一致（幂等，不会闪）。
  showPanel("plugins");
  syncNavPluginActive();

  if (!(opts && opts.updateHash === false)) {
    go(`plugins/${encodeURIComponent(pluginId)}/${encodeURIComponent(pageName)}`);
  }
}

/* ==================== 插件详情 ====================
 *
 * 照真 AstrBot 的「插件详情页」（`GET /api/v1/plugins/by-id`，见
 * `docs/ASTRBOT_插件管理页_调研.md` §2）：它按
 * `page / skill / command / llm_tool / listener / hook` 六类渲染「插件行为」。
 *
 * 🔴 为什么这页值得存在（不是抄界面）：我们的插件是**跑在兼容层上的**，
 *    "某个插件到底注册上了什么"是我们最常要回答的问题 ——
 *    以前只能看 `handler_count` 一个**数字**，看不出"注册的是哪几条指令"。
 *    本项目最贵的 bug 类型是**静默失败**（插件 catch 成一行 info 日志），
 *    所以"逐条列出来"本身就是判据。
 *
 * ⚠️ 六类里 `skill` 我们**没有**对应体系 → 后端返回空数组，这里也如实不显示
 *    （绝不显示一个永远空的分类，那会让用户以为"坏了"）。
 */

/** 六类的标题与渲染顺序（照真实现 `PLUGIN_COMPONENT_TYPE_ORDER`）。 */
const PLUGIN_COMPONENT_GROUPS = [
  ["page", "页面"],
  ["skill", "技能"],
  ["command", "指令/指令组"],
  ["llm_tool", "LLM 工具"],
  ["listener", "事件监听器"],
  ["hook", "钩子"],
];

/** 打开某个插件的详情（`#/plugins/<id>`）。 */
async function openPluginDetail(pluginId, opts) {
  const wrap = $("plugin-detail-wrap");
  const body = $("plugin-detail-body");
  const hint = $("plugin-detail-hint");
  $("plugin-detail-title").textContent = pluginId;
  state.pluginCurrentId = pluginId;
  showPluginsView("detail");

  // 🔴 每次进入都**清空并说明正在读** —— 绝不保留上一个插件的残留内容
  //    （否则用户以为看到的是当前插件的，其实还没加载完）。
  body.innerHTML = "";
  hint.textContent = "正在读取…";
  hint.style.color = "";

  try {
    const d = await api("GET", `/api/v1/plugins/${encodeURIComponent(pluginId)}`);
    // ⚠️ 异步回来时用户可能已经切走/返回了 —— 迟到的响应**不许**改写当前视图
    if (state.pluginCurrentId !== pluginId) return;
    state.pluginDetail = d;
    $("plugin-detail-title").textContent = d.name || d.id || pluginId;
    renderPluginDetail(d);
    hint.textContent = "";
  } catch (e) {
    if (state.pluginCurrentId !== pluginId) return;
    hint.textContent = `读取失败：${e.message}`;
    hint.style.color = "var(--danger)";
    body.innerHTML = "";
  }

  if (!(opts && opts.updateHash === false)) {
    go(`plugins/${encodeURIComponent(pluginId)}`);
  }
}

/** 画一个「键: 值」小行（空值**如实显示**为「—」，不隐藏 —— 隐藏会让人以为字段不存在）。 */
function detailRow(label, value, opts) {
  const row = document.createElement("div");
  row.className = "detail-row";
  const k = document.createElement("span");
  k.className = "detail-k";
  k.textContent = label;
  const v = document.createElement("span");
  v.className = "detail-v";
  if (opts && opts.mono) v.classList.add("mono");
  if (value === undefined || value === null || value === "") {
    v.textContent = "—";
    v.classList.add("muted");
  } else {
    v.textContent = String(value);
  }
  row.append(k, v);
  return row;
}

/** 渲染一个组件分类（`components` 里 type 相同的那批）。 */
function renderComponentGroup(title, items) {
  const box = document.createElement("section");
  box.className = "detail-group";
  const h = document.createElement("h4");
  h.textContent = `${title}（${items.length}）`;
  box.appendChild(h);

  const list = document.createElement("div");
  list.className = "detail-items";
  for (const c of items) {
    const it = document.createElement("div");
    it.className = "detail-item";
    const head = document.createElement("div");
    head.className = "detail-item-head";
    const nm = document.createElement("strong");
    nm.textContent = c.name || c.cmd || "（无名）";
    head.appendChild(nm);
    // 类型徽标（`指令组` / `正则匹配` / `事件监听器` …）
    if (c.display_type) {
      const badge = document.createElement("span");
      badge.className = "detail-badge";
      badge.textContent = c.display_type;
      head.appendChild(badge);
    }
    if (c.has_admin) {
      const badge = document.createElement("span");
      badge.className = "detail-badge admin";
      badge.textContent = "需管理员";
      head.appendChild(badge);
    }
    it.appendChild(head);

    for (const [k, val] of [["指令", c.cmd], ["触发", c.event_type_h],
                            ["说明", c.description], ["处理函数", c.handler_name]]) {
      if (val === undefined || val === null || val === "") continue;
      it.appendChild(detailRow(k, val, { mono: k === "指令" || k === "处理函数" }));
    }
    // 子指令（指令组的树）—— 有才画，逐层缩进
    if (Array.isArray(c.subcommands) && c.subcommands.length) {
      const sub = document.createElement("div");
      sub.className = "detail-sub";
      for (const s of c.subcommands) {
        const line = document.createElement("div");
        line.className = "detail-sub-line";
        line.textContent = `↳ ${s.cmd || s.name || "?"}` +
          (s.description && s.description !== "无描述" ? ` — ${s.description}` : "");
        sub.appendChild(line);
      }
      it.appendChild(sub);
    }
    list.appendChild(it);
  }
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "hint";
    empty.textContent = "（没有）";
    list.appendChild(empty);
  }
  box.appendChild(list);
  return box;
}

/** 按 `GET /api/v1/plugins/<id>` 的响应画详情。 */
function renderPluginDetail(d) {
  const body = $("plugin-detail-body");
  body.innerHTML = "";

  // ---- ① 概览 ----
  const info = document.createElement("section");
  info.className = "detail-group";
  const h = document.createElement("h4");
  h.textContent = "信息";
  info.appendChild(h);
  info.appendChild(detailRow("显示名", d.name));
  info.appendChild(detailRow("插件 ID", d.id, { mono: true }));
  info.appendChild(detailRow("模块路径", d.module_path, { mono: true }));
  info.appendChild(detailRow("版本", d.version));
  info.appendChild(detailRow("作者", d.author));
  info.appendChild(detailRow("简介", d.desc));
  info.appendChild(detailRow("状态", d.state_text || d.state));
  info.appendChild(detailRow("目录", d.dir_name, { mono: true }));
  if (d.error) info.appendChild(detailRow("错误", d.error));
  body.appendChild(info);

  // ---- ② 完整堆栈（失败时才有；**单独一块**，别塞进"错误"那一行） ----
  if (d.traceback) {
    const tb = document.createElement("section");
    tb.className = "detail-group";
    const th = document.createElement("h4");
    th.textContent = "完整堆栈";
    tb.appendChild(th);
    const pre = document.createElement("pre");
    pre.className = "detail-pre";
    pre.textContent = d.traceback;
    tb.appendChild(pre);
    body.appendChild(tb);
  }

  // ---- ③ 计数（一眼看清"注册上了多少东西"） ----
  const counts = document.createElement("section");
  counts.className = "detail-group";
  const ch = document.createElement("h4");
  ch.textContent = "注册数量";
  counts.appendChild(ch);
  const grid = document.createElement("div");
  grid.className = "detail-counts";
  for (const [label, val] of [
    ["事件处理器", d.handler_count],
    ["LLM 工具（已绑实例）", d.llm_tool_bind_count],
    ["Web API", d.web_api_count],
    ["定时任务", d.task_count],
  ]) {
    const cell = document.createElement("div");
    cell.className = "detail-count";
    const num = document.createElement("strong");
    num.textContent = val === undefined || val === null ? "—" : String(val);
    const lab = document.createElement("span");
    lab.textContent = label;
    cell.append(num, lab);
    grid.appendChild(cell);
  }
  counts.appendChild(grid);
  // ⚠️ 这个暗示必须写出来：工具数为 0 而插件"有工具"= 执行必错（本项目踩过）
  if (d.llm_tool_bind_count === 0) {
    const warn = document.createElement("p");
    warn.className = "hint";
    warn.textContent = "⚠️ 没有绑定任何 LLM 工具。若这个插件本该有工具，说明它注册的工具没有被绑上插件实例 —— 调用时必然失败。";
    counts.appendChild(warn);
  }
  body.appendChild(counts);

  // ---- ④ 插件行为（六类，按真实现的顺序） ----
  const comps = Array.isArray(d.components) ? d.components : [];
  const bh = document.createElement("h4");
  bh.className = "detail-section-title";
  bh.textContent = `插件行为（共 ${comps.length} 项）`;
  body.appendChild(bh);
  if (!comps.length) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "暂无行为 —— 这个插件没有注册指令、工具或事件监听器。";
    body.appendChild(empty);
  } else {
    for (const [type, title] of PLUGIN_COMPONENT_GROUPS) {
      const items = comps.filter((c) => c.type === type);
      // 🔴 空分类**不画**（除了我们确实支持的几类）：画一堆"（没有）"等于噪音
      if (!items.length) continue;
      body.appendChild(renderComponentGroup(title, items));
    }
  }

  // ---- ⑤ README（有才画） ----
  if (d.readme) {
    const rd = document.createElement("section");
    rd.className = "detail-group";
    const rh = document.createElement("h4");
    rh.textContent = "README" + (d.readme_truncated ? "（已截断）" : "");
    rd.appendChild(rh);
    const pre = document.createElement("pre");
    pre.className = "detail-pre readme";
    pre.textContent = d.readme;
    rd.appendChild(pre);
    body.appendChild(rd);
  }
}

/** 显示/隐藏「插件启停」那块（打开插件页/详情时让位）。 */
function setPluginSwitchVisible(visible) {
  for (const id of ["plugin-switch-hint", "plugin-switch-list"]) {
    const el = $(id);
    if (el) el.classList.toggle("hidden", !visible);
  }
}

/**
 * 插件页的**三视图互斥** —— 唯一入口。
 *
 * 🔴 为什么必须收敛成一处（2026-09-27 修的真实缺陷）：
 *    以前"谁可见"散在 `openPluginPage()` 与 `closePluginPage()` 两处各写一半，
 *    `closePluginPage()` 只还原了列表/hint，**没还原成对的可见性状态**，
 *    而 `openPluginPage()` 里那句 `go(...)` 还会**再触发一次路由**
 *    → `applyRoute` → `openPluginPage(updateHash:false)` → 又走一遍显示逻辑。
 *    只要任何一条路径漏掉一半，用户就会看到"详情页没了"这种
 *    **没有任何报错**的空白（本项目最贵的 bug 类型）。
 *
 * 现在改成：**任何**切换都只调这一个函数，三种视图在一个地方被同时决定。
 * 新增视图时只改这里 —— 这比"记得在 N 个地方都补一行"可靠。
 *
 * @param {"list"|"frame"|"detail"|"config"} which
 */
function showPluginsView(which) {
  // 🔴 四块主体：**互斥**。新增了 "config"（插件配置，情绪 2026-10-09）——
  //    正如这里的注释所说，新增视图**只改这一处**就够。
  $("plugin-frame-wrap").classList.toggle("hidden", which !== "frame");
  $("plugin-detail-wrap").classList.toggle("hidden", which !== "detail");
  $("plugin-config-wrap").classList.toggle("hidden", which !== "config");
  $("plugin-page-list").classList.toggle("hidden", which !== "list");
  $("plugins-hint").classList.toggle("hidden", which !== "list");
  // 启停那块只在**列表态**显示（其余状态整个面板让给主体）
  setPluginSwitchVisible(which === "list");

  if (which !== "frame") {
    // 清 src：否则插件页的定时器/连接继续在后台跑
    $("plugin-frame").src = "about:blank";
  }
  if (which !== "detail") {
    // 离开详情就丢掉缓存，并清 id —— 迟到的详情响应靠它判断"用户已经走了"
    state.pluginDetail = null;
  }
  if (which !== "config") {
    // 🔴 同理：离开配置页要把**已加载的配置**丢掉。
    //    不丢的话，下次打开另一个插件的配置页会先闪出上一个插件的值
    //    （而用户很可能就在那一瞬间点了保存 → **把 A 的配置存进 B**）。
    state.pluginConfig = null;
    // 🔴 同理清掉 file 值缓存（键路径跨插件重名，见 `openPluginConfig` 里的注释）
    state.pluginFileValues = {};
  }
  if (which === "list") state.pluginCurrentId = "";
}

/** 回到插件列表（唯一的"返回"实现）。 */
function closePluginPage() {
  showPluginsView("list");
}

$("plugin-frame-back").onclick = () => go("plugins");
$("plugin-detail-back").onclick = () => go("plugins");

/* ================================================================================
 * 插件配置（情绪 2026-10-09：「有些插件没有内置的独立 webui 就可以打开插件的配置」）
 *
 * ## 数据形状（照真 AstrBot，见 `host/astrbot_host/host_app.py:plugin_config`）
 *
 *     { plugin_name, metadata: {<id>: {description, type:"object", items: <_conf_schema.json>}},
 *       config: {<当前值>}, i18n: {} }
 *
 * 🔴 **渲染器复用设置页那一套**（`buildConfigField` / `makeConfigInput`），
 *    不另写第二套 —— 本项目「同一概念两条写入路径」已踩 6 次。
 *    做法是加一个**适配器**把插件 schema 的项翻译成我们的字段格式，
 *    而不是复制渲染代码。
 *
 * 🔴 **我们只渲染能真正保存的控件**。插件 schema 里有几种类型我们**没有**对应控件
 *    （`list`/`dict`/`file`/`template_list`、以及各种 `_special`），
 *    对它们如实显示一句"这个插件项本面板暂不支持编辑，请直接改配置文件"，
 *    **不画一个存不进去的控件**（那会让用户以为改了会生效 —— 假能力）。
 * ================================================================================ */

/** 插件 schema 的 `type` → 我们 `makeConfigInput` 认识的类型。
 *
 * ⚠️ 认不出来时返回 `null`（调用方据此画"不支持"提示），**不硬塞成 string** ——
 *    把 `list` 当 string 画会显示成 "[object Object]" 或丢内容，
 *    而用户点保存就把**列表写成了字符串**（数据被改坏且不报错）。
 */
function pluginFieldType(spec) {
  const t = spec && spec.type;
  if (t === "bool") return "bool";
  if (t === "int" || t === "float") return "string";   // 数字用文本输入（见下）
  if (t === "string" || t === "text") return "string";
  if (t === "object") return null;                     // 分组，单独递归
  return null;
}

/* ================================================================================
 * 插件的 `file` 型配置项（2026-10-09，照真 AstrBot 的 config-files）
 *
 * 情绪：「3 也可以做」—— 插件配置文件的列出/上传/删除。
 *
 * ## 真实现语义（`dashboard/api/plugins.py:762-814`）
 *   一个 `type: "file"` 的配置项，值是一个**路径数组**；文件真实落在
 *   **插件目录的 `files/<配置键路径>/`** 下。上传**只落盘、不写配置值** ——
 *   用户再在配置页把路径填进那个数组，两个动作是分开的。
 *   ⇒ 这个控件要同时呈现**两件事**：① 盘上有哪些文件 ② 配置值里引用了哪些。
 *      它们**可能不一致**（传了但没填 / 填了但文件被删），
 *      而"不一致"恰恰是用户最需要看见的 —— 所以下面显式标出来。
 *
 * ## 为什么值要自己维护在 state 里
 *   普通控件的值能从 DOM 直接读（`input.value`）。这个控件是"按钮 + 清单"，
 *   没有单一控件承载值 ⇒ 把**当前路径数组**存在
 *   `state.pluginFileValues[<键路径>]`，收集器从那里取。
 *   ⚠️ 不用 DOM 属性存：属性只能存字符串，来回 JSON 编解码容易出错且难查。
 * ================================================================================ */

/** 当前各 file 型配置项的值（键路径 → 路径数组）。收集器从这里读。 */
if (!state.pluginFileValues) state.pluginFileValues = {};

/** 画一个 `file` 型配置项。返回容纳它的元素（调用方会打 data 属性）。 */
function buildPluginFileField(pluginId, key, spec, value, pathPrefix) {
  const path = pathPrefix ? `${pathPrefix}.${key}` : key;
  const box = document.createElement("div");
  box.className = "cfg-field plugin-cfg-file";

  const title = document.createElement("span");
  title.className = "cfg-label";
  title.textContent = spec.description || key;
  box.appendChild(title);

  if (spec.hint) {
    const h = document.createElement("small");
    h.className = "hint";
    h.textContent = spec.hint;
    box.appendChild(h);
  }

  // 值规整成字符串数组（插件声明 default 可能是 []、也可能缺失）
  const initial = Array.isArray(value) ? value.map((x) => String(x)) : [];
  state.pluginFileValues[path] = initial.slice();

  const list = document.createElement("div");
  list.className = "plugin-file-list";
  box.appendChild(list);

  const actions = document.createElement("div");
  actions.className = "row plugin-file-actions";
  const pick = document.createElement("input");
  pick.type = "file";
  pick.multiple = true;
  // ⚠️ `accept` 用插件声明的 `file_types` 生成 —— 让系统文件框**先**过滤一道，
  //    但**不能**只靠它（那只是提示，用户仍可选"所有文件"）⇒ 服务端照样校验。
  const types = Array.isArray(spec.file_types) ? spec.file_types : [];
  if (types.length) pick.accept = types.map((t) => "." + String(t).replace(/^\./, "")).join(",");
  pick.className = "plugin-file-pick";
  const upBtn = document.createElement("button");
  upBtn.type = "button";
  upBtn.textContent = "上传";
  upBtn.className = "plugin-file-upload";
  const hint = document.createElement("small");
  hint.className = "hint plugin-file-hint";

  actions.appendChild(pick);
  actions.appendChild(upBtn);
  box.appendChild(actions);

  upBtn.onclick = async () => {
    const files = Array.from(pick.files || []);
    if (!files.length) {
      showHint(hint, "先选文件再点上传", true);
      return;
    }
    showHint(hint, "正在上传…");
    const fd = new FormData();
    for (const f of files) fd.append("file", f, f.name);
    try {
      const r = await fetch(
        `/api/v1/plugins/${encodeURIComponent(pluginId)}/config-files`
          + `?config_key=${encodeURIComponent(key)}`,
        { method: "POST", body: fd },
      );
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        // ⚠️ 后端逐条收集了错误（"5 张里这张不行"）→ **原样透出**，
        //    不简化成一句"上传失败"（那样用户得反复试）
        throw new Error((d.error && d.error.message) || `HTTP ${r.status}`);
      }
      const okN = (d.uploaded || []).length;
      const errs = d.errors || [];
      showHint(
        hint,
        `已上传 ${okN} 个${errs.length ? "；" + errs.join("；") : ""}`
          + "。⚠️ 上传只是把文件放进插件目录，**还要把它填进下面的列表并保存**才会生效。",
        errs.length > 0,
      );
      pick.value = "";
      await refreshPluginFileList(pluginId, key, path, list, hint, spec);
    } catch (e) {
      showHint(hint, "上传失败：" + e.message, true);
    }
  };

  box.appendChild(hint);
  // 首次渲染就去拉一次清单（异步；失败会在 hint 里说明）
  refreshPluginFileList(pluginId, key, path, list, hint, spec);
  return box;
}

/** 拉取"盘上有哪些文件"并重画清单。 */
async function refreshPluginFileList(pluginId, key, path, list, hint, spec) {
  try {
    const d = await api(
      "GET",
      `/api/v1/plugins/${encodeURIComponent(pluginId)}/config-files`
        + `?config_key=${encodeURIComponent(key)}`,
    );
    renderPluginFileList(pluginId, key, path, list, hint, d.files || [], spec);
  } catch (e) {
    // 🔴 读不到**不能**显示成"没有文件" —— 那会让用户重复上传
    list.innerHTML = "";
    const p = document.createElement("small");
    p.className = "hint";
    p.textContent = `读取已上传文件失败：${e.message}`;
    list.appendChild(p);
  }
}

/** 重画清单：**盘上的文件** + **配置值引用了哪些**，并标出不一致。 */
function renderPluginFileList(pluginId, key, path, list, hint, files, spec) {
  list.innerHTML = "";
  const referenced = state.pluginFileValues[path] || [];

  if (!files.length) {
    const p = document.createElement("small");
    p.className = "hint";
    p.textContent = "（还没有上传任何文件）";
    list.appendChild(p);
  }

  for (const rel of files) {
    const row = document.createElement("div");
    row.className = "plugin-file-row";

    const name = document.createElement("span");
    name.className = "plugin-file-name";
    // ⚠️ 显示**文件名**而不是整条相对路径：路径里那段目录名（= 配置键）
    //    对用户没有信息量，还长到把按钮挤出去。
    name.textContent = rel.split("/").pop();
    name.title = rel;               // 完整路径放 title（悬停可见）
    row.appendChild(name);

    // 配置值里引用了它吗？—— 这是"传了但没填"最直观的提示
    const used = referenced.includes(rel);
    const badge = document.createElement("span");
    badge.className = "badge " + (used ? "ok" : "no-model");
    badge.textContent = used ? "已启用" : "未启用";
    badge.title = used
      ? "配置里引用了这个文件"
      : "文件已上传，但配置的列表里还没有它 —— 需要点下面的「加入列表」再保存";
    row.appendChild(badge);

    if (!used) {
      const add = document.createElement("button");
      add.type = "button";
      add.className = "plugin-file-add";
      add.textContent = "加入列表";
      add.onclick = () => {
        // ⚠️ 改的是**我们维护的那份值**，然后把控件重画一遍让徽标跟上。
        //    不直接改 DOM 文本 —— 那样两处状态会漂。
        state.pluginFileValues[path] = (state.pluginFileValues[path] || []).concat([rel]);
        renderPluginFileList(pluginId, key, path, list, hint, files, spec);
        showHint(hint, "已加入列表。⚠️ 还要点页面上的「保存」才会写进插件配置。");
      };
      row.appendChild(add);
    } else {
      const rm = document.createElement("button");
      rm.type = "button";
      rm.className = "plugin-file-remove";
      rm.textContent = "移出列表";
      rm.onclick = () => {
        state.pluginFileValues[path] = (state.pluginFileValues[path] || []).filter((x) => x !== rel);
        renderPluginFileList(pluginId, key, path, list, hint, files, spec);
        showHint(hint, "已移出列表。⚠️ 还要点「保存」才会写进插件配置。");
      };
      row.appendChild(rm);
    }

    const del = document.createElement("button");
    del.type = "button";
    del.className = "plugin-file-del";
    del.textContent = "删除文件";
    del.onclick = async () => {
      if (!confirm(`确定删除 ${rel}？此操作不可恢复。`)) return;
      try {
        await api("DELETE", `/api/v1/plugins/${encodeURIComponent(pluginId)}/config-files`, {
          key,
          path: rel,
        });
        // ⚠️ 列表里还引用着它 → 一并移掉，否则保存后插件读一个不存在的文件
        //    （那种失败在插件那边多半是一行日志 = 静默）
        state.pluginFileValues[path] = (state.pluginFileValues[path] || []).filter((x) => x !== rel);
        showHint(hint, `已删除 ${rel}（并已从列表移除）。⚠️ 还要点「保存」才会写进插件配置。`);
      } catch (e) {
        showHint(hint, "删除失败：" + e.message, true);
      }
      await refreshPluginFileList(pluginId, key, path, list, hint, spec);
    };
    row.appendChild(del);
    list.appendChild(row);
  }
}

/** 把一个插件 schema 项渲染成一个字段控件（复用 `buildConfigField`）。 */
function buildPluginField(pluginId, key, spec, value, pathPrefix) {
  // 🔴 **`file` 型单独走一条路**（2026-10-09 补，照真 AstrBot 的 config-files）。
  //    它跟别的类型**根本不是同一类控件**：值是一个**路径数组**
  //    （`["files/<key>/x.png", ...]`），而界面要给的是
  //    **上传/删除按钮 + 已上传文件的清单**。
  //    ⇒ 早于 `pluginFieldType()` 判断 —— 那个函数对 `file` 返回 null，
  //      会在下面被当成"不支持"而画成一句"请直接改配置文件"
  //      （那正是本轮之前的行为）。
  if (spec && spec.type === "file") {
    const box = buildPluginFileField(pluginId, key, spec, value, pathPrefix);
    // ⚠️ 这两个 data 属性是**收集器认领控件的凭据**（`collectPluginConfigChanges`
    //    靠 `[data-plugin-key]` 找）。
    //    ⚠️ 但 file 型**不能**让收集器按普通控件去读 —— 它的值由
    //       `buildPluginFileField` 自己维护在 `state` 里（见那里的注释），
    //       所以这里打的是**另一个**标记 `data-plugin-file`，
    //       收集器会跳过它、改用专门的分支。
    if (box) {
      box.dataset.pluginKey = key;
      box.dataset.pluginPath = pathPrefix ? `${pathPrefix}.${key}` : key;
      box.dataset.pluginFile = "1";
    }
    return box;
  }
  const ourType = pluginFieldType(spec);
  if (ourType === null) {
    // 🔴 如实说"不支持"，**不画假控件**
    const wrap = document.createElement("label");
    wrap.className = "cfg-field plugin-cfg-unsupported";
    const title = document.createElement("span");
    title.className = "cfg-label";
    title.textContent = spec.description || key;
    wrap.appendChild(title);
    const h = document.createElement("small");
    h.className = "hint";
    const kind = spec.type === "object" ? "（分组，见下面各项）" : `（类型 ${spec.type || "未知"}）`;
    h.textContent = `本面板暂不支持编辑 ${kind}，请直接改插件的配置文件。`;
    wrap.appendChild(h);
    return wrap;
  }

  const field = {
    key,
    label: spec.description || key,
    type: ourType,
    hint: spec.hint || "",
    default: spec.default,
  };
  // 枚举：我们已有 `enum` 分支，但插件 schema 用的是 `options` + `labels`
  // （labels 下标与 options 配对，照真实现 `ConfigItemRenderer`）
  if (Array.isArray(spec.options) && spec.options.length) {
    field.type = "enum";
    const labels = Array.isArray(spec.labels) ? spec.labels : [];
    field.options = spec.options.map((o, i) => ({ value: o, label: labels[i] || String(o) }));
  }

  const wrap = buildConfigField("plugin", field, value);
  wrap.dataset.pluginKey = key;
  wrap.dataset.pluginPath = pathPrefix ? `${pathPrefix}.${key}` : key;
  return wrap;
}

/** 递归渲染一份插件 schema 段。
 *
 * ⚠️ 递归深度设上限：插件的 `_conf_schema.json` 是**外部数据**，
 *    有环或超深嵌套会让页面卡死（浏览器栈溢出）。
 */
function renderPluginSchemaItems(host, pluginId, schema, values, pathPrefix, depth) {
  if (depth > 6) {
    const h = document.createElement("small");
    h.className = "hint";
    h.textContent = "（嵌套过深，本面板不再展开）";
    host.appendChild(h);
    return;
  }
  for (const [key, spec] of Object.entries(schema || {})) {
    if (!spec || typeof spec !== "object") continue;
    // 🔴 `invisible: true` 的项**不画**（插件自己标了"别在界面上显示"）
    if (spec.invisible) continue;

    if (spec.type === "object" && spec.items) {
      // 分组：画个小标题 + 递归
      const group = document.createElement("div");
      group.className = "plugin-cfg-group";
      const gt = document.createElement("h5");
      gt.textContent = spec.description || key;
      group.appendChild(gt);
      if (spec.hint) {
        const gh = document.createElement("small");
        gh.className = "hint";
        gh.textContent = spec.hint;
        group.appendChild(gh);
      }
      const inner = document.createElement("div");
      inner.className = "cfg-fields";
      // ⚠️ 嵌套值要**逐层下钻**：`values[key]` 可能不是对象（旧配置/类型不符），
      //    那就按空对象渲染（让用户看到可编辑的默认项），而不是整段不显示。
      const sub = values && typeof values[key] === "object" && values[key] !== null
        ? values[key] : {};
      renderPluginSchemaItems(inner, pluginId, spec.items, sub,
                              pathPrefix ? `${pathPrefix}.${key}` : key, depth + 1);
      group.appendChild(inner);
      host.appendChild(group);
    } else {
      host.appendChild(buildPluginField(pluginId, key, spec, values ? values[key] : undefined, pathPrefix));
    }
  }
}

/** 打开某个插件的配置页。 */
async function openPluginConfig(pluginId, opts) {
  const keepHint = !!(opts && opts.keepHint);
  state.pluginCurrentId = pluginId;
  showPluginsView("config");
  const title = $("plugin-config-title");
  if (title) title.textContent = `插件配置 · ${pluginId}`;
  const hint = $("plugin-config-hint");
  const body = $("plugin-config-body");
  if (body) body.innerHTML = "";
  // 🔴 `keepHint`：保存成功后要把后端的提示**留住**。
  //    我第一版没这个参数，保存时 `toast(hint, message)` 之后紧接着调
  //    `openPluginConfig(id)` 重新拉数据 —— 而那个函数**第一件事就是**
  //    `toast(hint, "正在读取配置…")` 再 `toast(hint, "")`，
  //    于是那句"已保存，需要重启服务后生效"**存在不到一帧**、用户根本看不到。
  //    真机判据 C2 抓到了它（hint 读出来是空串）。
  if (!keepHint) toast(hint, "正在读取配置…");

  try {
    const d = await api("GET", `/api/v1/plugins/${encodeURIComponent(pluginId)}/config`);
    // 迟到的响应不要覆盖用户已经切走的视图（与本文件别处同款守卫）
    if (state.pluginCurrentId !== pluginId) return;
    state.pluginConfig = d;
    // 🔴 **每个插件开一次都要清空 file 值缓存** ——
    //    键路径（`path`）在不同插件之间是**重名**的（`openai_director_fallback_images`
    //    这类名字，两个插件都可能叫同一个）。
    //    不清的话，切插件后收集器会把**上一个插件**的路径数组当成这个插件的值提交，
    //    而界面上看着一切正常（本项目最忌的"静默用了别的东西"）。
    state.pluginFileValues = {};
    renderPluginConfig(d);
    if (!keepHint) toast(hint, "");
  } catch (e) {
    if (state.pluginCurrentId !== pluginId) return;
    // 🔴 失败要说清是"读不到"还是"没配置"，别留一片空白
    if (body) {
      body.innerHTML = "";
      const p = document.createElement("p");
      p.className = "hint";
      p.textContent = `读取插件配置失败：${e.message}`;
      body.appendChild(p);
    }
    toast(hint, `读取失败：${e.message}`, true);
  }
}

/** 把读到的配置画出来。 */
function renderPluginConfig(d) {
  const body = $("plugin-config-body");
  if (!body) return;
  body.innerHTML = "";
  const pluginId = d.plugin_name;
  const md = d.metadata && d.metadata[pluginId];
  const save = $("plugin-config-save");

  if (!md || !md.items || !Object.keys(md.items).length) {
    // 🔴 三种"没有配置"要分清（用户的下一步动作完全不同）：
    //    ① 插件根本没有 schema（没写 _conf_schema.json）
    //    ② 有 schema 但内容为空
    //    ③ 宿主构造配置失败（那种情况后端会说 metadata=null 且日志里有 ERROR）
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = "这个插件没有声明任何配置项（它没有 _conf_schema.json，或内容为空）。";
    body.appendChild(p);
    // 没有配置项时**把保存按钮禁用**（点了也没东西可存）
    if (save) save.disabled = true;
    return;
  }
  if (save) save.disabled = false;

  const root = document.createElement("div");
  root.className = "cfg-fields";
  renderPluginSchemaItems(root, pluginId, md.items, d.config || {}, "", 0);
  body.appendChild(root);
}

/** 收集插件配置的**改动项**（只发改动 —— 与设置页同款口径）。
 *
 * ⚠️ 返回**扁平键**（`a.b.c` 路径），保存时后端按浅合并处理。
 *    我们的存储是 JSON，嵌套分组在键上就是嵌套 dict；
 *    但后端的 `save_config` 是**浅合并**（顶层键整体替换），
 *    所以嵌套分组的改动要**整组提交**（下面按顶层归并）。
 */
function collectPluginConfigChanges() {
  const body = $("plugin-config-body");
  const loaded = (state.pluginConfig || {}).config || {};
  if (!body) return {};

  const changes = {};
  for (const wrap of body.querySelectorAll("[data-plugin-key]")) {
    const key = wrap.dataset.pluginKey;
    const path = wrap.dataset.pluginPath || key;

    // 🔴 **`file` 型没有单一控件承载值** —— 它是"按钮 + 清单"，
    //    值由 `buildPluginFileField` 维护在 `state.pluginFileValues` 里。
    //    ⚠️ 这里必须**先于** `querySelector("input")` 判断：
    //       那个 file 选择框（`type=file`）也在里面，会被误当成值控件
    //       ⇒ 读出来是 `C:\fakepath\x.png` 这种垃圾，保存进配置后
    //         插件永远找不到文件，而**界面上看起来"保存成功"**。
    if (wrap.dataset.pluginFile === "1") {
      const v = state.pluginFileValues[path] || [];
      const parts0 = path.split(".");
      let before0 = loaded;
      for (const seg of parts0) {
        if (before0 && typeof before0 === "object") before0 = before0[seg];
        else { before0 = undefined; break; }
      }
      if (JSON.stringify(v) === JSON.stringify(before0)) continue;
      if (parts0.length === 1) {
        changes[key] = v;
      } else {
        const top = parts0[0];
        const base = JSON.parse(JSON.stringify(loaded[top] ?? {}));
        let cur = base;
        for (let i = 1; i < parts0.length - 1; i += 1) {
          if (typeof cur[parts0[i]] !== "object" || cur[parts0[i]] === null) cur[parts0[i]] = {};
          cur = cur[parts0[i]];
        }
        cur[parts0[parts0.length - 1]] = v;
        changes[top] = base;
      }
      continue;
    }

    const ctrl = wrap.querySelector("input, select, textarea");
    if (!ctrl) continue;

    let v;
    if (ctrl.type === "checkbox") v = ctrl.checked;
    else if (ctrl.tagName === "SELECT") v = ctrl.value;
    else v = ctrl.value;

    // 与加载时的值比：**只有真改了才提交**
    const parts = path.split(".");
    let before = loaded;
    for (const seg of parts) {
      if (before && typeof before === "object") before = before[seg];
      else { before = undefined; break; }
    }
    if (JSON.stringify(v) === JSON.stringify(before)) continue;

    // ⚠️ 归并到**顶层键**（后端是浅合并：嵌套 dict 得整份给）
    if (parts.length === 1) {
      changes[key] = v;
    } else {
      // 深路径 → 从已加载的顶层那份**拷贝后**逐层写入，
      // 这样同组其它键不会丢（丢键是静默数据损失）
      const top = parts[0];
      if (!changes[top]) {
        const base = loaded[top];
        changes[top] = (base && typeof base === "object")
          ? JSON.parse(JSON.stringify(base)) : {};
      }
      let cur = changes[top];
      for (let i = 1; i < parts.length - 1; i++) {
        const seg = parts[i];
        if (!cur[seg] || typeof cur[seg] !== "object") cur[seg] = {};
        cur = cur[seg];
      }
      cur[parts[parts.length - 1]] = v;
    }
  }
  return changes;
}

$("plugin-config-back").onclick = () => go("plugins");
$("plugin-config-close").onclick = () => go("plugins");
$("plugin-config-save").onclick = async () => {
  const id = state.pluginCurrentId;
  const hint = $("plugin-config-hint");
  const btn = $("plugin-config-save");
  if (!id) return;
  const changes = collectPluginConfigChanges();
  if (!Object.keys(changes).length) {
    toast(hint, "没有改动，无需保存。");
    return;
  }
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "保存中…";
  try {
    const r = await api("PUT", `/api/v1/plugins/${encodeURIComponent(id)}/config`, { config: changes });
    // ⚠️ 后端的文案里有"需要重启服务才生效"——**原样透出**，
    //    不在这里简化成"已保存"（那会掩盖生效时机）
    // ⚠️ 顺序要紧：**先重新拉数据**（界面上的值要等于服务端的真实值），
    //    再写提示 —— 反过来的话那句提示会被重拉过程清掉（见 openPluginConfig）。
    await openPluginConfig(id, { keepHint: true });
    toast(hint, (r && r.message) || "已保存");
  } catch (e) {
    toast(hint, `保存失败：${e.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
};

/* ================= 启动 ================= */

/* ---------------- 登录闸门 ----------------
   🔴 为什么需要它（实测踩到）：面板页面本身**免鉴权**（只是壳），
      但所有数据接口都要鉴权 —— 而此前**界面上根本没有输入令牌的地方**，
      只能靠 CDP/curl 手动 POST /api/v1/auth/login 绕过。
      用户打开浏览器只会看到空列表 + 401，不知道该怎么办。
   令牌**不落任何浏览器存储**：登录后由服务端下发 HttpOnly cookie。 */
async function isLoggedIn() {
  try {
    const r = await fetch("/api/v1/status");
    return r.ok;
  } catch { return false; }
}

function showLogin(msg) {
  $("login").classList.remove("hidden");
  if (msg) {
    $("login-hint").textContent = msg;
    $("login-hint").style.color = "var(--danger)";
  }
  $("login-token").focus();
}

function hideLogin() { $("login").classList.add("hidden"); }

$("login-form").onsubmit = async (e) => {
  e.preventDefault();
  const token = $("login-token").value.trim();
  if (!token) return;
  try {
    const r = await fetch("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      showLogin("登录失败：" + (d?.error?.message || `HTTP ${r.status}`));
      return;
    }
    $("login-token").value = "";   // 用完即弃，不留在 DOM 里
    hideLogin();
    boot();
  } catch (err) {
    showLogin("登录失败：" + err.message);
  }
};

/** 页面数据加载（登录成功后调用）。
 *
 *  ⚠️ **必须在这里隐掉登录浮层**（实测踩到）：
 *     浮层在 HTML 里默认可见（fail-closed：JS 挂了也不会露出裸面板），
 *     但"刷新时 cookie 还有效"这条路径**不经过表单提交** → 没人调 hideLogin()
 *     → 页面数据其实已加载，用户却被永久挡在登录框上。
 *     判据来自 tools/login_check.py 的第 4 项（真浏览器测出来的）。 */
function boot() {
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
async function recheckLogin() {
  if ($("login").classList.contains("hidden")) return;   // 已经进去了
  if (await isLoggedIn()) boot();
}

setInterval(recheckLogin, 30000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) recheckLogin();
});
window.addEventListener("focus", recheckLogin);

/* 先探一次：已登录（cookie 还在）就直接进，否则弹登录框 */
isLoggedIn().then((ok) => {
  if (ok) { boot(); } else { showLogin(); }
});
