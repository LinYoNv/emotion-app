/* 情绪app 面板 —— 入口（原生 ES 模块，**零构建**）
 *
 * ════════════════════════════════════════════════════════════════════════
 * 结构
 * ════════════════════════════════════════════════════════════════════════
 *   js/core/core.js      基础设施：state / api / 路由 / 登录 / toast
 *   js/views/*.js        各页面：纯声明，**无模块级副作用**
 *   js/bootstrap.js      全部事件绑定（顶层立即执行的那些）
 *   js/main.js           本文件：加载顺序 + 兼容层 + 启动
 *
 * **为什么视图模块里没有事件绑定**
 *   core 与 views 是**双向**依赖。若把 `$("x").onclick = ...` 留在视图模块顶层，
 *   模块图初始化时会撞 `ReferenceError: Cannot access '$' before initialization`
 *   （实测踩到，`providers.js` 第一行绑定就炸）。所以副作用全收在 bootstrap.js。
 *
 * **为什么要有兼容层**
 *   拆分前是单文件，165 个声明天然挂全局；而 `tools/*.py` 里 **24 个名字**
 *   是直接在真浏览器里调用的。少了任何一个，对应判据会拿不到东西 ——
 *   最坏是变成**静默假绿**。所以这里逐个挂，undefined 时**必须报错**。
 *
 * ⚠️ 不引入打包器：浏览器原生 `import` 就够，部署方式完全不变
 *    （`run.sh serve` 起 Python；静态路由 `@app.get("/static/<path:filename>")`
 *     本来就支持子目录，后端零改动）。
 */
import { refreshUnread, send } from "./views/chat.js";
import { $, NAV_LOADERS, api, applyRoute, boot, go, isLoggedIn, recheckLogin, showLogin, state, syncSidebarAria, toast, toggleSidebar } from "./core/core.js";
import { renderSections, selectPersona } from "./views/persona.js";
import { openPluginConfig, renderNavPluginPages } from "./views/plugins.js";
import { loadProviderSources, selectProviderSource } from "./views/providers.js";
import { loadSessions, saveSessionConfig, selectSession } from "./views/session.js";
import { loadConfigProfile, loadLogs, resetConfigProfile } from "./views/settings.js";

/* 事件绑定（顶层立即执行）—— 必须在各视图模块之后 */
import "./bootstrap.js";

/* 🔴 **这份清单是外部契约** —— 对应 `tools/*.py` 里判据直接调用的名字。
 *    改动它等于改动判据契约，必须同时跑全套浏览器判据。 */
const GLOBALS = {
  $, state, api, toast, NAV_LOADERS, applyRoute, go, syncSidebarAria, toggleSidebar, send, selectPersona, renderSections, loadSessions, refreshUnread, selectSession, saveSessionConfig, loadLogs, loadConfigProfile, resetConfigProfile, loadProviderSources, selectProviderSource, renderNavPluginPages, openPluginConfig, boot, recheckLogin, isLoggedIn, showLogin,
};

const missing = [];
for (const [name, value] of Object.entries(GLOBALS)) {
  if (typeof value === "undefined") { missing.push(name); continue; }
  globalThis[name] = value;
}
if (missing.length) {
  console.error(`[main] 兼容层挂载失败（${missing.length} 个）：${missing.join(", ")}`);
}

/* ── 启动（原 app.js 末尾，逐行搬来；漏掉就整页不启动）──────────────
 * ⚠️ `recheckLogin` 的监听器必须在**模块作用域**注册：写进 `boot()` 里的话，
 *    "没登录"时根本不会调 `boot()` → 监听器从来没装上（原注释记着这个坑）。
 */
setInterval(recheckLogin, 30000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) recheckLogin();
});
window.addEventListener("focus", recheckLogin);

/* 先探一次：已登录（cookie 还在）就直接进，否则弹登录框 */
isLoggedIn().then((ok) => {
  if (ok) { boot(); } else { showLogin(); }
});
