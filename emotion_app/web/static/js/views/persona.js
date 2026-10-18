/* persona —— 由原单文件 app.js 拆分（**原生 ES 模块，零构建**）
 *
 * 🔴 本文件只放**函数/常量声明**，**没有任何模块级副作用**
 *    （事件绑定统一在 `js/bootstrap.js`）。
 *    这是为了消除循环依赖在"模块顶层立即求值"时拿到未初始化绑定的风险。
 */

import { $, api, go, state } from "../core/core.js";



/* ================= 人设 ================= */
export async function loadPersonas() {
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
export function renderChatPersonaHint() {
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

export function renderPersonaList() {
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

export async function selectPersona(id, opts) {
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
export function personaRefUrl(id) {
  return `/api/v1/personas/${encodeURIComponent(id)}/reference?t=${Date.now()}`;
}

export function renderPersonaRef(p) {
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
export function refreshPersonaInState(p) {
  const i = state.personas.findIndex((x) => x.id === p.id);
  if (i >= 0) state.personas[i] = p;
  if (state.current && state.current.id === p.id) state.current = p;
  renderPersonaRef(p);
}




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
export const PERSONA_CORE_TITLES = ["角色设定", "性格", "场景", "创作者备注"];

/** 判断一个片段是不是"从 6 字段现算的核心片段"（= 不该在表里重复展示）。 */
export function isDerivedCoreSection(s) {
  return (s.source || "") === "persona" && PERSONA_CORE_TITLES.includes(s.title || "");
}

export function renderSections() {
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




/* 「← 返回列表」：手机端从详情退回列表 */




/* ================= 导入 / 导出 =================
   格式依据是爱语真 dex 实测（见 爱语/docs/PERSONA_IMPORT_FORMAT.md）：
   爱语备份 `{"data":{"prompts":{…}}}` **一个文件可含多个人设**。
   ⚠️ 手机上没有拖拽，所以入口是**文件选择器 + 粘贴框**两条。
   ⚠️ 导出用 `fetch` + `Blob` 触发下载 —— 直接 `<a href>` 带不了鉴权头。 */




/** 下载导出的 JSON（带鉴权头，所以不能用 <a href>）。 */
export async function downloadExport(url, fallbackName) {
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
