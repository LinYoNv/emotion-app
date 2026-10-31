/* adapters —— 消息平台适配器（OneBot v11 / 内置 Web 端）（**原生 ES 模块，零构建**）
 *
 * 🔴 本文件只放**函数/常量声明**，**没有任何模块级副作用**
 *    （事件绑定统一在 `js/bootstrap.js`）。
 *    这是为了消除循环依赖在"模块顶层立即求值"时拿到未初始化绑定的风险。
 *
 * ## 后端契约（已实现，前端只消费）
 *   GET    /api/v1/adapters                 → data:{adapters:[...], types:[...]}
 *   GET    /api/v1/adapters/types           → data:{types:[...]}
 *   POST   /api/v1/adapters                 body {type,name,config,enabled} → 201
 *   PUT    /api/v1/adapters/<id>            body {name?,config?,enabled?}
 *   DELETE /api/v1/adapters/<id>            （内置的回 403 adapter_protected）
 *   GET    /api/v1/adapters/<id>/status     → data:{id,enabled,runtime}
 *   信封：成功 {status:"ok",message,data}；失败 {status:"error",message,reason,error:{message,type}}
 *
 * ## 🔴 三条本页必须守住的纪律
 *
 * ① **运行状态必须分三态显示**（OneBot 是**反向** WebSocket：我们起服务等协议端连过来）：
 *      running && connected      → 「已连接」（绿）
 *      running && !connected     → 「等待协议端连接」（黄）—— **这是正常待连，不是错误**
 *      !running                  → 「未运行」（灰）+ 若有 `runtime.last_error` 就显示它
 *    把"启动了"和"连上了"合成一个布尔值，用户会盯着一个绿色"已启用"发呆 ——
 *    那正是本项目最忌讳的静默失败（后端 `AdapterRuntime` 的注释也是这么写的）。
 *
 * ② **表单字段一律由后端 `fields[]` 驱动**（type: string/int/bool/secret/string_list），
 *    前端**不硬编码任何字段名**。在 JS 里再抄一份字段清单 = 早晚和后端漂移，
 *    而且"漏渲染"会伪装成"这个类型没有这项配置"。
 *
 * ③ **secret 只写不回显**：列表/详情接口给的是**打码值**（`•` 开头）。
 *    用户不动它 → 原样回传 → 后端识别打码值并保持原值；用户填了新的 → 就是新值。
 *    ⚠️ 绝不能在保存时把打码值当"新令牌"覆盖掉真值（那会把 token 洗成 `••••`）。
 */

import { $, api, apiData, state, toast } from "../core/core.js";

/* ---------------- 错误文案 ---------------- */

/** 后端错误 → 给用户看的一句话。
 *
 * 🔴 必须用信封里的 `message`，`reason` 存在时**一并显示** ——
 *    只写"保存失败"等于把后端说清的原因扔掉（用户没有任何可行动的线索）。
 *    例：`适配器配置无效（reason: adapter_invalid）`。
 * ⚠️ `api()` 对非 2xx 抛的 Error 上带着 `status` / `reason`（见 core.js 的 api）。
 */
export function adapterErrorText(e) {
  const msg = (e && e.message) || "未知错误";
  const reason = (e && e.reason) || "";
  const status = e && e.status ? e.status : 0;
  const bits = [msg];
  if (reason && !msg.includes(reason)) bits.push(`（reason: ${reason}）`);
  // 后端没给可读文案时 `api()` 会退成 `HTTP 500` —— 那种情况补一句是什么错
  if (status && /^HTTP \d+$/.test(msg)) {
    bits.push(`（HTTP ${status}）`);
  }
  return bits.join("");
}

/** 取一个**必须是数组**的字段；不是数组就抛错（**不许静默当空**）。
 *
 * 与 core 的 `asArray` 的区别：那个对 `undefined` 返回 `[]`。
 * 本页列表读不到形状时如果当空，用户会看到"还没有适配器"并去建一个重复的 ——
 * 这正是本项目最贵的静默失败类型，所以这里**缺字段也报错**。
 */
export function requireArray(obj, field) {
  const v = obj ? obj[field] : undefined;
  if (!Array.isArray(v)) {
    throw new Error(`响应里 ${field} 不是数组（后端形状不符）`);
  }
  return v;
}

/* ---------------- 运行状态（三态 + 内置） ---------------- */

/** 一个适配器 → 界面上的状态描述。**这是三态的唯一判定处**（列表只消费它）。
 *
 * 返回 `{key, text, cls, detail, error}`：
 *   key   ∈ connected / waiting / stopped / builtin（给 `data-state` 用，判据/样式都靠它）
 *   text  = 给人看的**原文**（判据里直接断言这四个字）
 *   cls   = CSS 类后缀（ok / warn / "" / builtin）
 *   detail= 第二行的补充信息（监听地址 / 收发计数 / 待连说明）
 *   error = 非空表示要**用危险色显示**（未运行时的 last_error）
 */
export function adapterStatusView(a) {
  const rt = (a && a.runtime) || {};

  // 🔴 内置适配器（webchat）**先判**：它本来就不需要连接，
  //    后端对它的 runtime 是 `{running:false,connected:false,last_error:null}` ——
  //    不先判就会显示成「未运行」，用户会去修一个根本不存在的问题。
  if (a && a.builtin) {
    return {
      key: "builtin",
      text: "内置",
      cls: "builtin",
      detail: a.type_description || "内置平台，始终可用，不受启停开关影响。",
      error: "",
    };
  }

  const listen = rt.listen ? `监听 ${rt.listen}` : "";
  const counts = [];
  if (typeof rt.received === "number") counts.push(`收 ${rt.received}`);
  if (typeof rt.sent === "number") counts.push(`发 ${rt.sent}`);
  if (typeof rt.rejected === "number" && rt.rejected > 0) counts.push(`拒 ${rt.rejected}`);

  if (rt.running === true && rt.connected === true) {
    const bits = [listen].concat(counts).filter(Boolean);
    if (rt.last_event_at) bits.push(`最近消息 ${rt.last_event_at}`);
    return { key: "connected", text: "已连接", cls: "ok", detail: bits.join(" · "), error: "" };
  }

  if (rt.running === true) {
    const bits = [listen, "协议端（NapCat / LLOneBot 等）还没连上来 —— 这是 OneBot 的正常待连状态，不是错误。"]
      .filter(Boolean);
    return {
      key: "waiting",
      text: "等待协议端连接",
      cls: "warn",
      detail: bits.join(" · "),
      error: rt.last_error || "",
    };
  }

  // 未运行：区分"用户自己停用的"与"启用着但没起来"（后者才需要看错误）
  const detail = a && a.enabled === false
    ? "已停用：不会启动，也不会收消息。"
    : "适配器没有在运行。";
  return { key: "stopped", text: "未运行", cls: "", detail, error: rt.last_error || "" };
}

/* ---------------- 列表 ---------------- */

/** 一行适配器（名字 / 类型标签 / 启停开关 / 运行状态 / 编辑 / 删除）。
 *
 * ⚠️ 删除按钮对 `builtin` **不渲染**（后端会 403，画一个点了必失败的按钮是假能力）。
 * ⚠️ 启停开关对 `builtin` **置灰**（后端明确拒绝停用内置 Web 端：面板聊天依赖它）。
 */
export function buildAdapterRow(a) {
  const li = document.createElement("li");
  li.className = "adapter-row";
  li.dataset.adapterId = a.id || "";
  li.dataset.adapterType = a.type || "";
  li.dataset.builtin = a.builtin ? "1" : "0";

  const main = document.createElement("div");
  main.className = "adapter-row-main";

  const title = document.createElement("div");
  title.className = "adapter-row-title";

  const nameEl = document.createElement("span");
  nameEl.className = "adapter-name";
  nameEl.textContent = a.name || a.id || "（未命名）";

  const typeEl = document.createElement("span");
  typeEl.className = "adapter-type";
  typeEl.dataset.adapterType = a.type || "";
  typeEl.textContent = a.type_label || a.type || "未知类型";

  const st = adapterStatusView(a);
  const statusEl = document.createElement("span");
  statusEl.className = "adapter-status" + (st.cls ? " " + st.cls : "");
  statusEl.dataset.state = st.key;
  statusEl.textContent = st.text;

  title.append(nameEl, typeEl, statusEl);
  main.appendChild(title);

  if (st.detail) {
    const sub = document.createElement("div");
    sub.className = "adapter-sub";
    sub.textContent = st.detail;
    main.appendChild(sub);
  }
  if (st.error) {
    const err = document.createElement("div");
    err.className = "adapter-err";
    err.textContent = st.error;
    main.appendChild(err);
  }

  const ops = document.createElement("div");
  ops.className = "adapter-ops";

  // 启停开关：列表里直接切（PUT {enabled}），不逼用户进表单
  const sw = document.createElement("label");
  sw.className = "adapter-switch";
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.className = "adapter-enabled";
  cb.dataset.adapterId = a.id || "";
  cb.checked = a.enabled !== false;
  cb.setAttribute("aria-label", `启用 ${a.name || a.id}`);
  if (a.builtin) {
    cb.disabled = true;
    cb.title = "内置适配器不受启停开关影响（面板聊天依赖它）";
  }
  const swText = document.createElement("span");
  swText.textContent = "启用";
  sw.append(cb, swText);

  const edit = document.createElement("button");
  edit.type = "button";
  edit.dataset.op = "edit";
  edit.dataset.adapterId = a.id || "";
  edit.textContent = "编辑";

  ops.append(sw, edit);
  if (!a.builtin) {
    const del = document.createElement("button");
    del.type = "button";
    del.className = "danger";
    del.dataset.op = "del";
    del.dataset.adapterId = a.id || "";
    del.textContent = "删除";
    ops.appendChild(del);
  }

  li.append(main, ops);
  return li;
}

/** 按 `state.adapters` 重画列表（`null` = 还没读到，与"读到 0 个"是两件事）。 */
export function renderAdapters() {
  const host = $("adapter-list");
  const empty = $("adapters-empty");
  const emptyText = $("adapters-empty-text");
  if (!host) return;

  // ⚠️ 用 innerHTML="" 清空列表容器（不是 textContent=）：
  //    后者会连子节点一起抹掉，而这里本来就是要重建，用 innerHTML 语义更明确。
  host.innerHTML = "";

  const list = state.adapters;
  if (list === null) {
    // 还没读到 → 空态说"正在读取"，绝不显示成"还没有适配器"
    empty.classList.remove("hidden");
    if (emptyText) emptyText.textContent = "正在读取适配器…";
    return;
  }

  if (!list.length) {
    empty.classList.remove("hidden");
    if (emptyText) {
      emptyText.textContent = state.adaptersError
        ? "适配器列表读取失败（原因见上方提示）。"
        : "还没有适配器。点右上角「+ 添加适配器」建一个。";
    }
  } else {
    empty.classList.add("hidden");
  }

  for (const a of list) host.appendChild(buildAdapterRow(a));
}

/** 读适配器列表（进 `#/adapters` 时由 NAV_LOADERS 调）。
 *
 * 返回 `true` 表示**列表真的读到了**；失败时返回 `false` 且把原因显示出来
 * （`state.adaptersError` 供其它函数判断"当前是不是失败态"）。
 */
export async function loadAdapters() {
  const hint = $("adapters-hint");
  try {
    const d = await apiData("GET", "/api/v1/adapters");
    state.adapters = requireArray(d, "adapters");
    // types 与列表同一个响应带回；单独接口是给"只想要类型目录"的场景兜底
    if (d && Array.isArray(d.types)) state.adapterTypes = d.types;
    state.adaptersError = "";
    renderAdapters();
    toast(hint, `共 ${state.adapters.length} 个适配器。`);
    return true;
  } catch (e) {
    // 🔴 失败要**清空**列表并说明原因 —— 不把"没读到"显示成"你没有适配器"
    state.adapters = [];
    state.adaptersError = adapterErrorText(e);
    renderAdapters();
    toast(hint, `读取适配器失败：${state.adaptersError}`, true);
    return false;
  }
}

/* ---------------- 类型目录 ---------------- */

/** 确保 `state.adapterTypes` 有数据（没有就走 `GET /api/v1/adapters/types`）。 */
export async function ensureAdapterTypes() {
  if (Array.isArray(state.adapterTypes) && state.adapterTypes.length) return true;
  try {
    const d = await apiData("GET", "/api/v1/adapters/types");
    state.adapterTypes = requireArray(d, "types");
    return true;
  } catch (e) {
    toast($("adapters-hint"), `读取适配器类型失败：${adapterErrorText(e)}`, true);
    return false;
  }
}

/** 某个类型的元数据（`{type,label,description,fields,builtin}`）；找不到返回 null。 */
export function adapterTypeMeta(type) {
  const list = state.adapterTypes || [];
  return list.find((t) => t && t.type === type) || null;
}

/** 可添加的类型 = **排除 builtin**（内置的已经存在，后端也会拒绝重复添加）。 */
export function addableAdapterTypes() {
  return (state.adapterTypes || []).filter((t) => t && !t.builtin);
}

/** 表单上方那句类型说明（切类型时跟着变）。 */
export function renderAdapterTypeDesc() {
  const el = $("adapter-type-desc");
  if (!el) return;
  const sel = $("adapter-type");
  const type = state.adapterCreating ? ((sel && sel.value) || "") : state.adapterEditingType;
  const meta = adapterTypeMeta(type);
  if (!meta) {
    el.textContent = "";
    return;
  }
  const bits = [meta.label || meta.type];
  if (meta.description) bits.push(meta.description);
  if (meta.builtin) bits.push("（内置类型，不能重复添加）");
  el.textContent = bits.join(" —— ");
}

/* ---------------- 表单：字段渲染（**按 fields[].type**） ---------------- */

/** 造一个字段控件（`f.type` 决定画什么）。**不硬编码字段名**。 */
export function buildAdapterField(f, values) {
  const key = f.key;
  const kind = f.type || "string";
  const id = "adapter-field-" + key;
  const hasValue = values && Object.prototype.hasOwnProperty.call(values, key);
  const raw = hasValue ? values[key] : f.default;

  const wrap = document.createElement("div");
  wrap.className = "field-labeled";
  wrap.dataset.key = key;
  wrap.dataset.fieldType = kind;

  const text = document.createElement("div");
  text.className = "fl-text";
  const label = document.createElement("label");
  label.htmlFor = id;
  label.textContent = f.label || key;
  if (f.required) {
    const req = document.createElement("span");
    req.className = "adapter-required";
    req.textContent = "必填";
    label.appendChild(req);
  }
  text.appendChild(label);
  if (f.help) {
    const help = document.createElement("span");
    help.className = "hint";
    help.textContent = f.help;
    text.appendChild(help);
  }

  const ctl = document.createElement("div");
  ctl.className = "fl-ctl";

  let input;
  if (kind === "bool") {
    const box = document.createElement("label");
    box.className = "adapter-check";
    input = document.createElement("input");
    input.type = "checkbox";
    input.id = id;
    input.checked = raw === undefined ? !!f.default : !!raw;
    const t = document.createElement("span");
    t.textContent = input.checked ? "开启" : "关闭";
    input.addEventListener("change", () => { t.textContent = input.checked ? "开启" : "关闭"; });
    box.append(input, t);
    ctl.appendChild(box);
  } else if (kind === "int") {
    input = document.createElement("input");
    input.type = "number";
    input.step = "1";
    input.id = id;
    input.value = raw === undefined || raw === null ? "" : String(raw);
    ctl.appendChild(input);
  } else if (kind === "string_list") {
    input = document.createElement("textarea");
    input.rows = 3;
    input.id = id;
    input.placeholder = "每行一个值";
    input.value = Array.isArray(raw) ? raw.join("\n") : (raw === undefined || raw === null ? "" : String(raw));
    ctl.appendChild(input);
    const note = document.createElement("span");
    note.className = "hint";
    note.textContent = "一行一个值，提交时会按行拆成数组。";
    ctl.appendChild(note);
  } else if (kind === "secret") {
    input = document.createElement("input");
    input.type = "text";   // ⚠️ 用 text 而不是 password：要让人**看得见**打码值（`••••` 开头）
    input.id = id;
    input.autocomplete = "off";
    input.spellcheck = false;
    input.value = raw === undefined || raw === null ? "" : String(raw);
    ctl.appendChild(input);
    if (typeof raw === "string" && raw.startsWith("•")) {
      const note = document.createElement("span");
      note.className = "hint";
      note.textContent = "已设置（这里显示的是打码值）。不改动 = 保持原值；填新的就覆盖它。";
      ctl.appendChild(note);
    } else {
      input.placeholder = "未设置（留空 = 不修改）";
    }
  } else {
    // string 与**未来新增的未知类型**：一律当文本输入，不静默丢字段
    input = document.createElement("input");
    input.type = "text";
    input.id = id;
    input.value = raw === undefined || raw === null ? "" : String(raw);
    ctl.appendChild(input);
    if (kind !== "string") {
      const note = document.createElement("span");
      note.className = "hint";
      note.textContent = `（未知字段类型 ${kind}，按文本渲染）`;
      ctl.appendChild(note);
    }
  }

  wrap.append(text, ctl);
  return wrap;
}

/** 按某个类型的 `fields` 重画表单字段区。`values` 缺失时用各字段的 `default`。 */
export function renderAdapterFields(fields, values) {
  const host = $("adapter-fields");
  if (!host) return;
  host.innerHTML = "";
  state.adapterFormFields = Array.isArray(fields) ? fields : [];
  if (!state.adapterFormFields.length) {
    // 该类型本来就没有配置项（如内置 Web 端）→ 如实说，不留一块空白让人猜
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = "这个适配器类型没有可配置项。";
    host.appendChild(p);
    return;
  }
  for (const f of state.adapterFormFields) host.appendChild(buildAdapterField(f, values));
}

/** 读表单 → `{name, enabled, config, type}`。**提交前唯一的读取点**。 */
export function collectAdapterForm() {
  const fields = state.adapterFormFields || [];
  const config = {};
  for (const f of fields) {
    const el = $("adapter-field-" + f.key);
    if (!el) continue;
    const kind = f.type || "string";
    if (kind === "bool") {
      config[f.key] = !!el.checked;
    } else if (kind === "int") {
      const raw = String(el.value == null ? "" : el.value).trim();
      // 空 → 交 "" 给后端（它会回落 default）；非数字 → 原样送，让后端报"必须是整数"
      if (raw === "") config[f.key] = "";
      else {
        const n = Number(raw);
        config[f.key] = Number.isFinite(n) ? n : raw;
      }
    } else if (kind === "string_list") {
      // 一行一个值 → 数组（去空白、去空行；顺序保持用户输入的）
      config[f.key] = String(el.value == null ? "" : el.value)
        .split(/\r?\n/)
        .map((s) => s.trim())
        .filter(Boolean);
    } else {
      // string / secret / 未知类型：**原文提交**（不 trim —— 密钥里可能有前导空格）
      config[f.key] = el.value == null ? "" : String(el.value);
    }
  }
  const nameEl = $("adapter-name");
  const enabledEl = $("adapter-enabled");
  return {
    name: nameEl ? nameEl.value : "",
    enabled: enabledEl ? !!enabledEl.checked : true,
    config,
    type: state.adapterCreating ? (($("adapter-type") || {}).value || "") : state.adapterEditingType,
  };
}

/* ---------------- 表单：显隐 / 打开 ---------------- */

export function adapterFormVisible() {
  const card = $("adapter-form-card");
  return !!card && !card.classList.contains("hidden");
}

function showAdapterForm() {
  const card = $("adapter-form-card");
  if (!card) return;
  card.classList.remove("hidden");
  if (card.scrollIntoView) card.scrollIntoView({ block: "nearest" });
  const name = $("adapter-name");
  if (name && name.focus) name.focus();
}

/** 关掉表单并**清干净状态**（下次打开不会带上一个适配器的残留）。 */
export function closeAdapterForm() {
  state.adapterCreating = false;
  state.adapterEditingId = "";
  state.adapterEditingType = "";
  state.adapterFormFields = [];
  const card = $("adapter-form-card");
  if (card) card.classList.add("hidden");
  const fields = $("adapter-fields");
  if (fields) fields.innerHTML = "";
  const hint = $("adapter-form-hint");
  if (hint) toast(hint, "");
}

/** 「+ 添加适配器」：先摆好空表单，**再**按第一个可添加类型渲染字段。 */
export async function openAdapterCreate() {
  const ok = await ensureAdapterTypes();
  if (!ok) return false;
  const types = addableAdapterTypes();
  if (!types.length) {
    toast($("adapters-hint"), "后端没有「可添加」的适配器类型（只剩内置类型）。", true);
    return false;
  }

  state.adapterCreating = true;
  state.adapterEditingId = "";
  state.adapterEditingType = types[0].type;

  const title = $("adapter-form-title");
  if (title) title.textContent = "添加适配器";

  const sel = $("adapter-type");
  if (sel) {
    sel.innerHTML = "";
    for (const t of types) {
      const o = document.createElement("option");
      o.value = t.type;
      o.textContent = t.label ? `${t.label}` : t.type;
      sel.appendChild(o);
    }
    sel.value = types[0].type;
    sel.disabled = false;
  }
  const typeRow = $("adapter-type-row");
  if (typeRow) typeRow.classList.remove("hidden");

  const nameEl = $("adapter-name");
  if (nameEl) {
    nameEl.value = "";
    nameEl.placeholder = "例如：我的QQ（留空 = 用类型名）";
  }
  const enEl = $("adapter-enabled");
  if (enEl) {
    enEl.checked = true;
    enEl.disabled = false;
  }
  const delBtn = $("adapter-delete");
  if (delBtn) delBtn.classList.add("hidden");

  renderAdapterTypeDesc();
  renderAdapterFields(types[0].fields || [], {});
  const hint = $("adapter-form-hint");
  if (hint) toast(hint, "填好后点「保存」。");
  showAdapterForm();
  return true;
}

/** 点某一行的「编辑」：把该适配器**真实的**配置回填进表单。 */
export function openAdapterEdit(id) {
  const a = (state.adapters || []).find((x) => x.id === id);
  if (!a) {
    toast($("adapters-hint"), `找不到适配器「${id}」。`, true);
    return false;
  }
  state.adapterCreating = false;
  state.adapterEditingId = id;
  state.adapterEditingType = a.type || "";

  const title = $("adapter-form-title");
  if (title) title.textContent = `编辑适配器 · ${a.name || a.id}`;

  // 类型创建后不可改（PUT 不接受 type）→ 下拉只放它自己且置灰，
  // 比"隐藏起来让用户猜"更诚实（能看见它是什么类型）。
  const sel = $("adapter-type");
  if (sel) {
    sel.innerHTML = "";
    const o = document.createElement("option");
    o.value = a.type || "";
    o.textContent = a.type_label || a.type || "";
    sel.appendChild(o);
    sel.value = a.type || "";
    sel.disabled = true;
  }
  const typeRow = $("adapter-type-row");
  if (typeRow) typeRow.classList.remove("hidden");

  const nameEl = $("adapter-name");
  if (nameEl) nameEl.value = a.name || "";
  const enEl = $("adapter-enabled");
  if (enEl) {
    enEl.checked = a.enabled !== false;
    enEl.disabled = !!a.builtin;   // 内置的停不了（后端会拒），如实置灰
    enEl.title = a.builtin ? "内置适配器不能停用（面板聊天依赖它）" : "";
  }
  const delBtn = $("adapter-delete");
  if (delBtn) delBtn.classList.toggle("hidden", !!a.builtin);

  // 字段定义优先用该适配器自带的 `fields`（后端按它的类型给），
  // 没有才回落到类型目录 —— 两条路都不猜字段名。
  const meta = adapterTypeMeta(a.type);
  const fields = Array.isArray(a.fields) && a.fields.length
    ? a.fields
    : ((meta && meta.fields) || []);
  renderAdapterTypeDesc();
  renderAdapterFields(fields, a.config || {});
  const hint = $("adapter-form-hint");
  if (hint) toast(hint, a.builtin
    ? "内置适配器：只能改名字（配置项由平台自带）。"
    : "改完点「保存」。密钥类字段显示的是打码值，不动它就保持原值。");
  showAdapterForm();
  return true;
}

/** 类型下拉切换 → 重画字段（用新类型的 default，不带上一个类型的值）。 */
export function onAdapterTypeChange() {
  if (!state.adapterCreating) return;
  const sel = $("adapter-type");
  const type = (sel && sel.value) || "";
  state.adapterEditingType = type;
  const meta = adapterTypeMeta(type);
  renderAdapterTypeDesc();
  renderAdapterFields((meta && meta.fields) || [], {});
  const hint = $("adapter-form-hint");
  if (hint) toast(hint, "已切换类型，下面按这个类型自己的字段重新渲染。");
}

/* ---------------- 写操作：保存 / 删除 / 启停 ---------------- */

/** 保存表单：新增走 POST，编辑走 PUT。 */
export async function saveAdapter() {
  const hint = $("adapter-form-hint");
  const btn = $("adapter-save");
  const payload = collectAdapterForm();
  const creating = !!state.adapterCreating;

  if (creating && !payload.type) {
    toast(hint, "请先选择一个适配器类型。", true);
    return false;
  }
  if (!creating && !state.adapterEditingId) {
    toast(hint, "没有正在编辑的适配器（可能已被删除），请返回列表重试。", true);
    return false;
  }

  if (btn) btn.disabled = true;
  toast(hint, "保存中…");
  try {
    let d;
    if (creating) {
      d = await api("POST", "/api/v1/adapters", {
        type: payload.type,
        name: payload.name,
        config: payload.config,
        enabled: payload.enabled,
      });
    } else {
      d = await api("PUT", `/api/v1/adapters/${encodeURIComponent(state.adapterEditingId)}`, {
        name: payload.name,
        config: payload.config,
        enabled: payload.enabled,
      });
    }
    const msg = (d && d.message) || (creating ? "适配器已添加" : "适配器已更新");
    closeAdapterForm();
    const ok = await loadAdapters();
    // 🔴 顺序要紧：**先重拉列表、再写成功提示** —— 反过来的话
    //    `loadAdapters()` 会把"已保存"覆盖成"共 N 个适配器"（本项目踩过同款）。
    if (ok) toast($("adapters-hint"), msg);
    else toast($("adapters-hint"), `${msg}；但重新读取列表失败：${state.adaptersError}`, true);
    return true;
  } catch (e) {
    // 失败**留在表单里**：用户填的东西不能因为一次报错就没了
    toast(hint, `保存失败：${adapterErrorText(e)}`, true);
    return false;
  } finally {
    if (btn) btn.disabled = false;
  }
}

/** 删除（内置的在列表里没有按钮；这里再挡一次，防止别的入口调进来）。 */
export async function deleteAdapter(id) {
  const a = (state.adapters || []).find((x) => x.id === id);
  if (!a) {
    toast($("adapters-hint"), `找不到适配器「${id}」。`, true);
    return false;
  }
  if (a.builtin) {
    toast($("adapters-hint"), "内置适配器不能删除（面板聊天依赖它）。", true);
    return false;
  }
  const label = a.name || a.id;
  if (!confirm(`删除适配器「${label}」？\n\n删除后协议端（NapCat 等）就再也连不上了，配置会一起删掉。`)) {
    return false;
  }
  try {
    const d = await api("DELETE", `/api/v1/adapters/${encodeURIComponent(id)}`);
    if (state.adapterEditingId === id) closeAdapterForm();
    const msg = (d && d.message) || `已删除 ${label}。`;
    const ok = await loadAdapters();
    if (ok) toast($("adapters-hint"), msg);
    else toast($("adapters-hint"), `${msg}；但重新读取列表失败：${state.adaptersError}`, true);
    return true;
  } catch (e) {
    // 403 adapter_protected 会走到这里 —— 原因会照实显示出来
    toast($("adapters-hint"), `删除失败：${adapterErrorText(e)}`, true);
    return false;
  }
}

/** 列表里的启停开关：直接 PUT {enabled}（不用进表单）。 */
export async function setAdapterEnabled(id, enabled, cbEl) {
  const a = (state.adapters || []).find((x) => x.id === id);
  if (a && a.builtin) {
    // 理论上开关是 disabled 的；真被触发就如实说，并把勾选状态改回去
    if (cbEl) cbEl.checked = true;
    toast($("adapters-hint"), "内置适配器不受启停开关影响。", true);
    return false;
  }
  try {
    const d = await api("PUT", `/api/v1/adapters/${encodeURIComponent(id)}`, { enabled: !!enabled });
    const msg = (d && d.message) || (enabled ? "已启用" : "已停用");
    const ok = await loadAdapters();
    if (ok) toast($("adapters-hint"), msg);
    else toast($("adapters-hint"), `${msg}；但重新读取列表失败：${state.adaptersError}`, true);
    return true;
  } catch (e) {
    // 失败要把开关**拨回去**（否则界面在撒谎：看起来停用了，后端其实没变）
    if (cbEl) cbEl.checked = !enabled;
    toast($("adapters-hint"), `切换启停失败：${adapterErrorText(e)}`, true);
    return false;
  }
}
