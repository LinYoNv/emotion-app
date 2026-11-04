/* 潮汐引力 面板 —— 入口（原生 ES 模块，**零构建**）
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
import { $, NAV_LOADERS, api, applyRoute, boot, go, isLoggedIn, loginState, recheckLogin, showLogin, state, syncSidebarAria, toast, toggleSidebar } from "./core/core.js";
import { renderPromptStats, selectPersona } from "./views/persona.js";
import { openPluginConfig, renderNavPluginPages } from "./views/plugins.js";
import { loadProviderSources, selectProviderSource } from "./views/providers.js";
import { loadSessions, saveSessionConfig, selectSession } from "./views/session.js";
import { loadConfigProfile, loadLogs, resetConfigProfile } from "./views/settings.js";

/* 事件绑定（顶层立即执行）—— 必须在各视图模块之后 */
import "./bootstrap.js";

/* 🔴 **这份清单是外部契约** —— 对应 `tools/*.py` 里判据直接调用的名字。
 *    改动它等于改动判据契约，必须同时跑全套浏览器判据。 */
const GLOBALS = {
  $, state, api, toast, NAV_LOADERS, applyRoute, go, syncSidebarAria, toggleSidebar, send, selectPersona, renderPromptStats, loadSessions, refreshUnread, selectSession, saveSessionConfig, loadLogs, loadConfigProfile, resetConfigProfile, loadProviderSources, selectProviderSource, renderNavPluginPages, openPluginConfig, boot, recheckLogin, isLoggedIn, showLogin,
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

/* 先探一次：已登录（cookie 还在）就直接进，否则弹登录框。
 *
 * 🔴 用 `loginState()` 而不是 `isLoggedIn()`（2026-11-02）：
 *    只有服务端**明确 401** 才该弹登录框。启动时网络还没就绪、
 *    或者服务正在重启，`fetch` 会直接抛 —— 那种情况下弹框等于骗用户去
 *    重填一个其实还有效的令牌。`unknown` 一律按"先放行"处理：
 *    数据接口真过期了会各自 401，`recheckLogin()` 那边会把人送回登录框。 */
/* 🔴 **必须加超时兜底**（2026-11-04，真机实测）：
 *
 *   生产上直接打开 `/#/settings` 并刷新，**偶发整页空白**
 *   （`#cfg-hint` 永远停在「正在读取配置…」，实测 10 次里 4~6 次）。
 *
 *   抓到的时序：页面加载到发**第一个请求**之间花了 **4.4 秒** ——
 *   11 个 ES 模块约 385KB 要走完整 import 链才执行到这一行；
 *   网络毛刺时更慢，`loginState()` 的 fetch 一直不返回，
 *   于是 `boot()` **永远不执行**（`.then` 只挂成功分支，没有兜底）。
 *
 *   ⇒ 3 秒内没结论就**先按"已登录"放行**（与 `loginState()` 里
 *     `unknown` 一律放行的口径一致：宁可带着可能过期的 cookie 去试，
 *     也不要因为一次网络慢就把人挡在空白页）。
 *     真过期了数据接口会 401，`recheckLogin()` 会把人送回登录框。
 */
let booted = false;
function startOnce(why) {
  if (booted) return;
  booted = true;
  if (why === "timeout") {
    console.warn("[boot] 登录态探测超时，先按已登录放行（真过期会 401）");
  }
  boot();
}
const bootGuard = setTimeout(() => startOnce("timeout"), 3000);
loginState()
  .then((state) => {
    clearTimeout(bootGuard);
    if (state === "out") { showLogin(); return; }
    startOnce("probe");
  })
  .catch(() => {
    clearTimeout(bootGuard);
    // 探测本身失败（不该发生，loginState 内部已 catch）⇒ 兜底也放行
    startOnce("probe-failed");
  });
