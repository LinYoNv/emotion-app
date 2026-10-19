/* 插件页桥接垫片（同源版）—— 让插件的拓展页在我们这里"能拿到数据"。
 *
 * ## 为什么需要它
 *
 * 插件的页面 JS 有**两条**取数路径（实测 `companion-panel/app.js`）：
 *
 *   ① `window.AstrBotPluginPage` 桥（`.apiGet/.apiPost/.ready`）——**首选**
 *   ② HTTP 直连（`HTTP_API` 硬编码路径）—— 仅在 standalone/debug_http 模式
 *
 * 真 AstrBot 的做法是：把插件页塞进 **iframe**，用 `postMessage` 把①做成
 * 跨源桥（`plugin_page_bridge.js`）。那套是为了隔离与鉴权。
 *
 * 我们是**同源**（页面就是自己发的），所以不需要 postMessage 那一层——
 * 直接提供一个**同源实现**的桥即可，插件代码完全不用改。
 *
 * ⚠️ 不提供桥会怎样（我实测过这条）：插件会退到 `isHttpApiMode()`，
 *    而那个模式要求 URL 带 `?debug_http=1` 或 standalone 标记，
 *    两者都没有 → 抛「未检测到 AstrBot 官方插件 Page 桥接」→ **页面空白**。
 *
 * ## 它做的事（就这一件）
 *
 *     插件的 `apiGet(endpoint)` → 本垫片 fetch
 *     `/api/plugins/extensions/<endpoint>` → 我们已挂载的插件派发器
 *
 * 端点候选的**重试逻辑仍在插件那边**（`bridgeEndpointCandidates` 会依次试
 * `page/x` → `x` → `/x` → `<插件名>/page/x`）。所以本垫片只需：
 *   * 命中 → 返回 JSON；
 *   * 404 → **返回带「未找到该路由」字样的对象**，让插件识别并试下一个候选。
 *     （照它的 `isRouteMissingPayload` 正则：`未找到.*路由|route.*not.*found|404`）
 */
(function installEmotionPluginPageBridge() {
  "use strict";

  /** 我们挂载插件 API 的前缀（与 emotion_app/web/app.py 保持一致）。 */
  var EXT_PREFIX = "/api/plugins/extensions";

  /* ------------------------------------------------------------------
   * 🔴 本页属于哪个插件（2026-09-27 补的**根因修复**）
   *
   * 服务端注入页面时把插件目录名写进 `<meta name="emotion-plugin-id">`
   * （`astrbot_host/plugin_pages.py` 的 `inject_page_context`）。
   *
   * ## 为什么需要它（改成实测的根因，不是给日志降噪）
   *
   * 插件的候选列表（`companion-panel/app.js:6702`）是：
   *
   *     page/x → x → /x → <插件名>/page/x → /<插件名>/page/x
   *
   * 垫片从前只拿得到**没有插件名**的 `page/x`，拼出来是
   * `/api/plugins/extensions/page/x` → **必然 404** → 靠插件自己一级级重试。
   * 实测日志里成组的「插件路由未命中」（19 条，每组 3 条 = 前三级）就是这么来的。
   *
   * ⚠️ **真损失不是噪音**：`一起房间` 的 `launcher.js:47` 只生成
   *    `page/x` **一种候选且没有重试** ⇒ 它**永远 404**、页面永远取不到数。
   *
   * ✅ 有了插件名，垫片把**任何**形状的候选都折算成
   *    `<插件名>/page/x`（我们的派发器要的正是这个）→ 一次命中。
   *
   * ⚠️ 读不到 meta（老页面/别的宿主）→ 退回原行为（原样拼），
   *    插件那边的重试逻辑仍在，**不会更差**。
   * ------------------------------------------------------------------ */

  var PLUGIN_ID = (function readMeta() {
    try {
      var el = document.querySelector('meta[name="emotion-plugin-id"]');
      var v = el && el.getAttribute("content");
      return v ? String(v).trim().replace(/^\/+|\/+$/g, "") : "";
    } catch (e) {
      return "";
    }
  })();

  /**
   * 把插件给的 endpoint 折算成我们派发器认识的形状。
   *
   * 派发器（`astrbot_host/dispatch.py`）的路径是 `<插件名>/<子路径>`，
   * 所以这里把候选的三种前缀都剥掉，再补上插件名：
   *
   *   "page/x"                  → "<插件名>/page/x"
   *   "/page/x"                 → "<插件名>/page/x"
   *   "x"                       → "<插件名>/x"      （裸形状）
   *   "<插件名>/page/x"          → 原样（已带名，别重名）
   *   "<别的插件名>/page/x"      → 原样（**跨插件调用要尊重**，不硬掰）
   */
  function normalizeEndpoint(endpoint) {
    var raw = String(endpoint == null ? "" : endpoint).trim();
    raw = raw.replace(/^\/+/, "");
    if (!PLUGIN_ID || !raw) return raw;

    var firstSeg = raw.split("/")[0];
    // 已经带插件名（含别的插件名）→ 原样，不重复补
    if (firstSeg === PLUGIN_ID || /^astrbot_plugin_|^astrbot_/.test(firstSeg)) {
      return raw;
    }
    return PLUGIN_ID + "/" + raw;
  }

  function apiUrl(endpoint, params) {
    var raw = normalizeEndpoint(endpoint);
    var url = EXT_PREFIX + "/" + raw;
    if (params && typeof params === "object") {
      var qs = new URLSearchParams();
      Object.keys(params).forEach(function (k) {
        var v = params[k];
        if (v === undefined || v === null) return;
        if (Array.isArray(v)) {
          v.forEach(function (item) { qs.append(k, String(item)); });
        } else {
          qs.append(k, String(v));
        }
      });
      var s = qs.toString();
      if (s) url += (url.indexOf("?") === -1 ? "?" : "&") + s;
    }
    return url;
  }

  /** 统一的请求执行：返回 JSON；404 变成"可被识别的未命中"。 */
  async function call(endpoint, params, body) {
    var init = {
      method: body === undefined ? "GET" : "POST",
      credentials: "same-origin",     // 复用主服务会话 cookie
      cache: "no-store",              // 插件数据永远要最新（别走缓存）
      headers: { Accept: "application/json" },
    };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body === null ? {} : body);
    }

    var resp;
    try {
      resp = await fetch(apiUrl(endpoint, params), init);
    } catch (err) {
      // 网络层失败要**抛**（插件会把它当"真错误"，不会盲目试下一个候选）
      throw new Error("插件接口请求失败：" + (err && err.message ? err.message : err));
    }

    var text = await resp.text();
    var payload;
    try {
      payload = text ? JSON.parse(text) : {};
    } catch (e) {
      payload = { error: "返回内容不是 JSON", detail: text.slice(0, 200) };
    }

    if (resp.status === 404) {
      // 🔴 关键：让插件认出"这条候选不对"，去试下一个端点形状。
      return {
        error: "未找到该路由",
        message: (payload && (payload.message || payload.error)) || "未找到该路由",
        detail: "404",
        status: "error",
        data: {},
      };
    }
    if (!resp.ok) {
      // 4xx/5xx 是**插件自己的业务错误**（如 missing id / admin_required），
      // 必须原样传回去 —— 吞掉它会让插件误以为"成功但没数据"。
      throw new Error(
        (payload && (payload.message || payload.error)) || ("HTTP " + resp.status),
      );
    }
    return payload;
  }

  /**
   * 登录/鉴权状态检查 + **返回上下文对象**。
   *
   * ## 🔴 `ready()` 必须**返回上下文**，不能只做检查（2026-10-19 修）
   *
   * 陪伴 7.0.0 的页面（`pages/陪伴小屋/product-page.js:499-501`）是这样用的：
   *
   *     const context = await bounded(bridge.ready());
   *     if (context?.pluginName !== HOST) throw fail("dashboard_identity_required");
   *
   * 也就是说它把 `ready()` 的**返回值**当作上下文，并校验
   * `pluginName === "astrbot_plugin_private_companion"`。
   *
   * 我们原来只做 fetch 检查、**不返回任何值** ⇒ `context` 是 `undefined`
   * ⇒ `context?.pluginName` 恒为 `undefined` ⇒ 页面永远判失败，
   * 显示「请从已登录的 AstrBot 管理界面打开陪伴小屋」。
   *
   * 实测现场（2026-10-19）：页面 59 个脚本、104 个资源全 200、零 JS 异常，
   * **但一次插件 API 都没调** —— 因为它在 `ready()` 这一步就放弃了。
   *
   * ## 返回什么
   *
   * 照真 AstrBot 插件页桥的上下文形状，给出页面真正会读的字段：
   *   * `pluginName` —— 本页所属插件目录名（页面拿它做身份校验）
   *   * `loggedIn`   —— 会话可用（能取到数据）
   *   * `username`   —— 面板用户名；我们没这个概念，给空串（页面按 falsy 处理）
   *
   * ⚠️ **不认识的字段一律不编**：多塞字段会让"看起来有、其实是假的"
   *    这类问题更难查。只给页面确实会读的那几个。
   */
  async function ready() {
    try {
      var r = await fetch("/api/v1/status", {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
    } catch (err) {
      throw new Error(
        "面板会话不可用（请先在情绪app 面板登录）：" +
          (err && err.message ? err.message : err),
      );
    }
    return {
      pluginName: PLUGIN_ID,
      loggedIn: true,
      // 我们没有"面板用户名"这个概念（单用户自用），给空串而不是编一个。
      username: "",
    };
  }

  window.AstrBotPluginPage = {
    ready: ready,
    apiGet: function (endpoint, params) { return call(endpoint, params, undefined); },
    apiPost: function (endpoint, body) { return call(endpoint, undefined, body); },
  };
})();
