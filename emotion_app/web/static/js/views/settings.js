/* settings —— 由原单文件 app.js 拆分（**原生 ES 模块，零构建**）
 *
 * 🔴 本文件只放**函数/常量声明**，**没有任何模块级副作用**
 *    （事件绑定统一在 `js/bootstrap.js`）。
 *    这是为了消除循环依赖在"模块顶层立即求值"时拿到未初始化绑定的风险。
 */

import { $, api, apiData, applyRoute, asArray, parseRoute, state, toast } from "../core/core.js";
import { renderSessionModelField, renderSessionPicker, renderSessionSummaryModelField } from "./session.js";




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
export async function loadSettings() {
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
export let logsTimer = 0;

/** 停掉自动刷新（幂等；离开页面时调）。 */
export function stopLogsAutoRefresh() {
  if (logsTimer) {
    clearInterval(logsTimer);
    logsTimer = 0;
  }
}

/** 拉一次日志并渲染。`startAuto` 只有从路由进来时才传。 */
export async function loadLogs(opts) {
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
export function startLogsAutoRefresh() {
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
export function renderLogsEmpty(text) {
  const box = $("logs-list");
  if (!box) return;
  const p = document.createElement("p");
  p.className = "hint";
  p.textContent = text;
  box.appendChild(p);
}

/** 渲染日志行。**全部 textContent**（日志内容是不可信文本，绝不拼 HTML）。 */
export function renderLogs(items) {
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
export async function copyLogs() {
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
export async function loadConfigProfile() {
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
export async function loadConfigModels() {
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
export function renderConfigSections() {
  const sch = state.cfgSchema || {};
  renderConfigSection("session", sch.session, $("cfg-session-fields"));
}

export function renderConfigSection(section, fields, host) {
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
export function buildConfigField(section, f, value) {
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

export function makeConfigInput(f, value) {
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
export function makeProviderSelect(f, value) {
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
export function collectConfigChanges(section, fields) {
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
export async function saveConfigSection(section) {
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
export async function resetConfigProfile() {
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
