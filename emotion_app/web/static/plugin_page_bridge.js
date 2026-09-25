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

  function apiUrl(endpoint, params) {
    var raw = String(endpoint == null ? "" : endpoint).trim();
    // 候选里既有 "page/x" 也有 "/plugin/page/x" —— 统一成一个绝对路径
    raw = raw.replace(/^\/+/, "");
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

  /** 登录/鉴权状态检查：页面加载时先确认自己拿得到数据。 */
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
  }

  window.AstrBotPluginPage = {
    ready: ready,
    apiGet: function (endpoint, params) { return call(endpoint, params, undefined); },
    apiPost: function (endpoint, body) { return call(endpoint, undefined, body); },
  };
})();
