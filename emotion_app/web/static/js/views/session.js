/* session —— 由原单文件 app.js 拆分（**原生 ES 模块，零构建**）
 *
 * 🔴 本文件只放**函数/常量声明**，**没有任何模块级副作用**
 *    （事件绑定统一在 `js/bootstrap.js`）。
 *    这是为了消除循环依赖在"模块顶层立即求值"时拿到未初始化绑定的风险。
 */

import { loadHistory, markSessionRead, renderUnreadBadge } from "./chat.js";
import { $, MODE_HINTS, api, state, switchToTab, toast } from "../core/core.js";
import { renderChatPersonaHint } from "./persona.js";
import { buildConfigField, makeProviderSelect, saveConfigSection } from "./settings.js";





/* ================= 会话 ================= */
export async function loadSessions() {
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

export function renderSessionList() {
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
export function openSessionInChat(id) {
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
export async function deleteSessionById(s) {
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
export function showHint(el, msg, bad) {
  if (!el) return;
  el.textContent = msg;
  el.style.color = bad ? "var(--danger)" : "";
}

export function renderSessionSelect() {
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
export function renderSessionPicker() {
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
export function renderSessionModelField(s) {
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
export async function saveSessionModel(sessionId, modelId) {
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

export async function selectSession(id) {
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
export function fillSessionForm(s) {
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
export function renderSessionSummaryModelField(s) {
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
export async function saveSessionSummaryModel(sessionId, modelId) {
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

export function refreshSessionInfo() {
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





/* 会话配置表单（现居「设置 → 配置文件 → 会话」）。
 * ⚠️ 它**不再**是 <form> 的 submit 事件 —— 见下面 `cfgSessionSaveBtn`：
 *    搬进设置页后它嵌在 `#cfg-session-form` 里，而那个 form 已经有自己的
 *    submit 处理器（保存全局配置）。两个 submit 处理器套在同一个 form 上
 *    会**互相干扰**（内层阻止冒泡与否都会有一边不执行）。
 *    ⇒ 所以这里改成**按钮点击**触发，语义更明确，也不与全局保存冲突。 */
export async function saveSessionConfig() {
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

/** 设置页的「删除这条会话」—— 与列表里那个行内删除**共用同一函数**
 *  （同一概念只许一条写入路径：同一句确认、同一个失败提示、同一次刷新）。 */

/** 设置页的「选择会话」下拉 → 切到那条会话（**只影响"正在配谁"**，
 *  与聊天页的 `#session-select` 互不干扰）。 */

/** 设置页的「+ 新建会话」—— 复用聊天页那个按钮的处理（`$("new-session-2")`）。
 *  ⚠️ 不复制一份逻辑：新建会话要同时更新 sessions 数组、当前会话、
 *     三个渲染函数与状态栏，复制必然漂移。 */

/* ⚠️ `#s-del` 这个按钮**已随「会话」页那一版表单一起删除**（情绪 2026-10-09
 *    「会话页只留跳转聊天/新建/删除」）—— 删除现在有两个入口，都在别处：
 *      * 会话列表里每行的「删除」按钮（`renderSessionList`）
 *      * 设置页的「删除这条会话」（`#cfg-session-del`）
 *    三者**共用** `deleteSessionById()`（同一概念只许一条写入路径）。
 *    这里保留一个存在性判断，而不是直接删掉这段：万一有旧书签/旧缓存页面
 *    还带着这个 id 的 DOM，`$("s-del").onclick = ...` 会**当场抛错**，
 *    把整个脚本后半段（含 umo 复制）一起带走。 */

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
export function currentUmo() {
  const s = state.currentSession;
  if (!s) return "";
  return s.umo || "";
}
