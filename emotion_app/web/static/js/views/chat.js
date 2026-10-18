/* chat —— 由原单文件 app.js 拆分（**原生 ES 模块，零构建**）
 *
 * 🔴 本文件只放**函数/常量声明**，**没有任何模块级副作用**
 *    （事件绑定统一在 `js/bootstrap.js`）。
 *    这是为了消除循环依赖在"模块顶层立即求值"时拿到未初始化绑定的风险。
 */

import { $, api, applyRoute, boot, refreshStatus, state } from "../core/core.js";
import { loadSessions, refreshSessionInfo, renderSessionList, selectSession } from "./session.js";


/* ================= 聊天 ================= */
/* 消息容器。⚠️ **不能用 `const messagesEl = $("messages")`** ——
 * 那是**模块顶层立即执行**，而本模块与 core.js 循环依赖，
 * 初始化顺序不保证 DOM 也保证不了 `$` 已就绪。改成惰性取值，
 * 调用点代码一个字都不用改。 */
function messagesEl() { return $("messages"); }

export function addMsg(role, text, meta) {
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
  messagesEl().appendChild(div);
  messagesEl().scrollTop = messagesEl().scrollHeight;
  return div;
}

/** 取/建气泡里的正文容器（图片与正文互不干扰）。 */
export function msgBody(bubble) {
  let body = bubble.querySelector(":scope > .body");
  if (!body) {
    body = document.createElement("span");
    body.className = "body";
    bubble.insertBefore(body, bubble.firstChild);
  }
  return body;
}

export async function send() {
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

export function renderImages(bubble, urls, caption) {
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
  messagesEl().scrollTop = messagesEl().scrollHeight;
}

export async function readSSE(response, bubble) {
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
        messagesEl().scrollTop = messagesEl().scrollHeight;
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
export async function refreshUnread() {
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

export function renderUnreadBadge() {
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
export async function loadChat() {
  if (!state.sessions.length) {
    await loadSessions();
  }
  if (state.currentSessionId) return;      // ② 已有选中，尊重它
  if (!state.sessions.length) return;      // ③ 真的没有会话
  // ① 选最近的一条（后端 `list()` 已按 updated_at 倒序）
  await selectSession(state.sessions[0].id);
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
export async function markSessionRead(id) {
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

export async function loadHistory(id) {
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
