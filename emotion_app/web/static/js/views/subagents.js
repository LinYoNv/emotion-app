/* subagents —— 子代理接入（让用户**连接自己的 agent 平台**）（原生 ES 模块，零构建）
 *
 * 🔴 本文件只放**函数/常量声明**，**没有任何模块级副作用**
 *    （事件绑定统一在 `js/bootstrap.js`）。理由同 `adapters.js`：消除循环依赖
 *    在"模块顶层立即求值"时拿到未初始化绑定的风险。
 *
 * ## 语义（情绪 2026-11-03 定，原话）
 *
 * > 「我想要的是**平台本身不带 agent 能力**，是子代理。**她把任务发出去等回收就行**。
 * >   就像是我们是主人，把任务发给佣人，明白不。」
 * > 「方案 c 单独做个界面来 **让用户连接自己的 agent 平台**。」
 *
 * ⇒ 这一页描述的是「**佣人住哪儿、怎么找到他**」，不是「平台自己怎么当 agent」。
 *
 * ## 后端契约（已实现，前端只消费）
 *   GET  /api/v1/subagents            → data:{config, backend_types}
 *   GET  /api/v1/subagents/backends   → data:{types:[{id,label,description,fields}]}
 *   PUT  /api/v1/subagents            → data:{config, applied, reason?}
 *   POST /api/v1/subagents/test       → data:{ok, backend, reason, detail}
 *   信封：成功 {status:"ok",message,data}；失败 {status:"error",message,reason,error}
 *
 * ## 🔴 四条本页必须守住的纪律
 *
 * ① **secret 只写不回显**：拿到的是打码值（`•` 开头）。用户不动 → 原样回传 →
 *    后端识别打码值并保持原值。**绝不能把打码值当新令牌、也不能在保存时把它清空**
 *    （清空 = 后端按"用户主动删除"处理，令牌就没了；之后全 401 而界面还显示"已配置"
 *    —— 本项目真踩过这一类）。
 *
 * ② **表单字段一律由后端 `fields[]` 驱动**，前端**不硬编码字段名**。
 *    连"配没配好"的判定也必须从 `fields[]` 推（见 `missingRequiredFields`），
 *    在 JS 里再抄一份字段清单 = 早晚和后端漂移，且"漏渲染"会伪装成"这个平台没这项配置"。
 *
 * ③ **状态分三态**（`subagentStateView` 是唯一判定处）：
 *      未配置 / **已配置但未连通** / **已连通**
 *    另加两个如实态：未启用、已配置但还没测过。合成一个"已启用"布尔会让用户
 *    盯着一个绿点发呆 —— 那正是本项目最忌讳的静默失败。
 *
 * ④ **配置没读到就禁止保存**：那时 `state.subagentConfig` 是 `null`，
 *    保存会把"没读到"当成"空的"发回去 —— 一次误点就把用户的配置清空。
 *    同理，`/test` 的**业务失败也用 HTTP 200** 返回，必须读 `data.ok`；
 *    而 `PUT` 的 `applied:false` 必须显式告警（"保存了但没生效"不能只报"成功"）。
 */

import { $, apiData, state, toast } from "../core/core.js";

/** 后端目录里「未接入」那一项的 id（契约里约定的常量，**不是**可配置字段名）。 */
const BACKEND_NONE = "null";

/** 打码前缀：后端 `mask_secret()` 用的就是这个（`domain/subagents.py` 的 `MASK`）。 */
const MASK_PREFIX = "•";

/* ---------------- 错误文案 ---------------- */

/** 后端错误 → 给用户看的一句话。
 *
 * 🔴 必须用信封里的 `message`，`reason` 存在时**一并显示** ——
 *    只写"保存失败"等于把后端说清的原因扔掉（用户没有任何可行动的线索）。
 * ⚠️ `api()` 对非 2xx 抛的 Error 上带着 `status` / `reason`（见 core.js 的 api）。
 */
export function subagentErrorText(e) {
  const msg = (e && e.message) || "未知错误";
  const reason = (e && e.reason) || "";
  const status = e && e.status ? e.status : 0;
  const bits = [msg];
  if (reason && !msg.includes(reason)) bits.push(`（reason: ${reason}）`);
  // 后端没给可读文案时 `api()` 会退成 `HTTP 500` —— 那种情况补一句是什么错
  if (status && /^HTTP \d+$/.test(msg)) bits.push(`（HTTP ${status}）`);
  return bits.join("");
}

/** 这个值是不是后端给回来的打码值（用户没动过）。 */
export function isMasked(value) {
  return typeof value === "string" && value.startsWith(MASK_PREFIX);
}

/* ---------------- 形状校验（不许静默降级成空） ---------------- */

/** 响应里的 `config` 必须是一个对象，`agents`（若有）必须是数组。
 *
 * ⚠️ 缺字段当空对象的后果："读取失败"会被显示成"你没配"，
 *    用户于是去配一个已经配好的东西（本项目最贵的静默失败类型）。
 */
function requireConfig(d) {
  const cfg = d && d.config;
  if (!cfg || typeof cfg !== "object" || Array.isArray(cfg)) {
    throw new Error("响应里缺少 config（后端形状不符）");
  }
  if (cfg.agents !== undefined && !Array.isArray(cfg.agents)) {
    throw new Error("响应里 config.agents 不是数组（后端形状不符）");
  }
  return cfg;
}

/** 子代理清单（`cfg` 可能还没读到 → 空数组）。 */
export function agentsOf(cfg) {
  return cfg && Array.isArray(cfg.agents) ? cfg.agents : [];
}

/* ---------------- 读 ---------------- */

/** 读后端目录（`GET /api/v1/subagents/backends`）。形状不符就抛。 */
export async function fetchBackendTypes() {
  const d = await apiData("GET", "/api/v1/subagents/backends");
  const types = d && d.types;
  if (!Array.isArray(types)) throw new Error("响应里 types 不是数组（后端形状不符）");
  return types;
}

/** 进 `#/subagents` 时由 `NAV_LOADERS` 调。
 *
 * 返回 `true` 表示**配置真的读到了**；失败时返回 `false` 且把原因写进
 * `#subagents-hint`（`state.subagentError` 供其它函数判断"当前是不是失败态"）。
 * ⚠️ 与 adapters 的 loader 同款：**自己吞错误、写进提示区、返回 false，不抛**。
 */
export async function loadSubagents() {
  const hint = $("subagents-hint");
  // 重新读取后，上一次的连通性结论不再成立（它可能是针对旧参数的）
  state.subagentTest = null;
  try {
    const d = await apiData("GET", "/api/v1/subagents");
    const cfg = requireConfig(d);
    // 目录优先用列表接口带回来的那份；没带就问目录接口（两条路都不猜）
    const types = Array.isArray(d.backend_types)
      ? d.backend_types
      : await fetchBackendTypes();

    state.subagentConfig = cfg;
    state.subagentBackendTypes = types;
    state.subagentError = "";
    renderSubagentForm();
    renderAgentList();
    renderBackendFields();

    const count = agentsOf(cfg).length;
    toast(hint, `已读取：${backendLabel(cfg.backend)} · ${count} 个子代理`
      + (cfg.enabled ? "。" : "（总开关关着，角色不会拿到 transfer_to_* 工具）。"));
    return true;
  } catch (e) {
    // 🔴 失败要**明确显示失败**：不白屏、不把"没读到"显示成"你没配"
    state.subagentConfig = null;
    state.subagentBackendTypes = null;
    state.subagentFormFields = [];
    state.subagentError = subagentErrorText(e);
    renderSubagentForm();
    renderAgentList();
    renderBackendFields();
    toast(hint, `读取子代理配置失败：${state.subagentError}。`
      + "现在**不能保存**（保存会把没读到的配置覆盖成空），修好后点「重新读取」。", true);
    return false;
  }
}

/** 「重新读取」按钮：失败原因已经由 `loadSubagents` 落到提示区，这里只负责重试。 */
export function reloadSubagents() {
  return loadSubagents();
}

/* ---------------- 目录 / 后端元数据 ---------------- */

/** 某个后端的元数据（`{id,label,description,fields}`）；找不到返回 null。 */
export function backendMeta(id) {
  const list = state.subagentBackendTypes;
  if (!Array.isArray(list)) return null;
  return list.find((t) => t && t.id === id) || null;
}

/** 后端 id → 给人看的名字（读不到目录时退回 id 本身）。 */
export function backendLabel(id) {
  const meta = backendMeta(id);
  if (meta) return meta.label || meta.id;
  return id ? String(id) : "（还没选 agent 平台）";
}

/** 当前后端的 `fields[]`（**渲染与校验的唯一来源**）。 */
export function currentBackendFields() {
  const cfg = state.subagentConfig || {};
  const meta = backendMeta(cfg.backend || BACKEND_NONE);
  return meta && Array.isArray(meta.fields) ? meta.fields : [];
}

/* ---------------- 配置完整性（**从 fields[] 推，不写字段名**） ---------------- */

/** 这个字段算不算"已经配好了"。
 *
 * 🔴 secret 字段**不能**靠"值非空"判断：后端给的是打码值还是空串取决于它配没配，
 *    而更权威的信号是后端同时给出的 `<key>_configured` 布尔（如 `token_configured`）。
 *    这里按"有这个布尔就用它"的通用规则推，**不枚举字段名**（枚举就跟纪律 ② 冲突了）。
 */
export function fieldSatisfied(cfg, field) {
  const key = field && field.key;
  if (!key) return true;
  if ((field.type || "string") === "secret") {
    const flag = cfg ? cfg[key + "_configured"] : undefined;
    if (typeof flag === "boolean") return flag;
    // 兜底：打码值或真值都算已设置（只有空串 = 没设置）
    return typeof cfg[key] === "string" && cfg[key] !== "";
  }
  const value = cfg ? cfg[key] : undefined;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "string") return value.trim() !== "";
  return value !== undefined && value !== null;
}

/** 必填但还空着的字段（空数组 = 配全了）。 */
export function missingRequiredFields(cfg, fields) {
  return (fields || []).filter((f) => f && f.required && !fieldSatisfied(cfg, f));
}

/* ---------------- 状态：三态的唯一判定处 ---------------- */

/** 当前配置 → 界面上的状态描述。
 *
 * 返回 `{key, text, tone, detail}`：
 *   key    ∈ unknown / error / off / unconfigured / incomplete /
 *            configured（已配置但**还没测**） / unreachable（**已配置但未连通**） /
 *            connected（**已连通**）
 *   text   = 给人看的**原文**（判据直接断言这几个字）
 *   tone   = ok / warn / bad / muted（给 `data-tone`，样式与判据都靠它）
 *   detail = 徽标的 `title`：为什么是这个状态、下一步该做什么
 *
 * ⚠️ "已配置"与"已连通"**必须分开**：配置全了不等于对面收得到活。
 */
export function subagentStateView() {
  const cfg = state.subagentConfig;

  if (cfg === null) {
    return state.subagentError
      ? { key: "error", text: "读取失败", tone: "bad", detail: state.subagentError }
      : { key: "unknown", text: "读取中…", tone: "muted", detail: "正在读取子代理配置。" };
  }
  if (!cfg.enabled) {
    return {
      key: "off",
      text: "未启用",
      tone: "muted",
      detail: "总开关关着：角色不会拿到 transfer_to_* 工具，任务不会被派出去。",
    };
  }
  if (!cfg.backend || cfg.backend === BACKEND_NONE) {
    return {
      key: "unconfigured",
      text: "未配置",
      tone: "warn",
      detail: "还没选 agent 平台 —— 选一个并填好连接参数。",
    };
  }
  const missing = missingRequiredFields(cfg, currentBackendFields());
  if (missing.length) {
    return {
      key: "incomplete",
      text: "未配置",
      tone: "warn",
      detail: `还缺必填项：${missing.map((f) => f.label || f.key).join("、")}。`,
    };
  }
  const tested = state.subagentTest;
  if (tested && tested.ok) {
    return {
      key: "connected",
      text: "已连通",
      tone: "ok",
      detail: "按当前参数测试通过。这个结论只在本次打开有效（参数一改就作废）。",
    };
  }
  if (tested) {
    return {
      key: "unreachable",
      text: "已配置 · 未连通",
      tone: "bad",
      detail: tested.reason || "测试没有通过（原因见下面的测试结果）。",
    };
  }
  return {
    key: "configured",
    text: "已配置 · 未测试",
    tone: "muted",
    detail: "参数配全了，但还没测过能不能连上 —— 点「测试连接」。",
  };
}

/** 只重画状态徽标（**不动表单**：重画表单会抢走输入焦点）。 */
export function renderStatus() {
  const el = $("subagent-status");
  if (!el) return;
  const view = subagentStateView();
  el.textContent = view.text;
  el.dataset.tone = view.tone;
  el.dataset.state = view.key;
  el.title = view.detail;
}

/** 总开关联动：只改本地状态 + 徽标，**不发请求**（保存才落盘）。 */
export function onSubagentEnabledChange() {
  if (state.subagentConfig === null) return;
  const el = $("subagent-enabled");
  state.subagentConfig = { ...state.subagentConfig, enabled: !!(el && el.checked) };
  renderStatus();
}

/* ---------------- 渲染：后端下拉 / 字段 ---------------- */

/** 画总开关 + 后端下拉 + 状态徽标（**重进页面 / 保存成功后**调）。 */
export function renderSubagentForm() {
  const cfg = state.subagentConfig;
  const readOnly = cfg === null;

  const enabledEl = $("subagent-enabled");
  if (enabledEl) {
    enabledEl.checked = cfg ? !!cfg.enabled : false;
    enabledEl.disabled = readOnly;
  }

  const select = $("subagent-backend");
  if (select) {
    const types = Array.isArray(state.subagentBackendTypes) ? state.subagentBackendTypes : [];
    const want = cfg ? (cfg.backend || BACKEND_NONE) : "";
    select.innerHTML = "";
    if (readOnly) {
      // 没读到配置时**明确说**，不留一个看着能点的空下拉（选了也写不进任何东西）
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "（配置未读到）";
      select.appendChild(opt);
    } else {
      for (const t of types) {
        const opt = document.createElement("option");
        opt.value = t.id;
        opt.textContent = t.label || t.id;
        if (t.id === want) opt.selected = true;
        select.appendChild(opt);
      }
      // 目录里没有当前值 → **如实显示**，不静默换成别的后端
      if (!types.some((t) => t.id === want)) {
        const opt = document.createElement("option");
        opt.value = want;
        opt.textContent = `${want || "（空）"}（后端目录里没有这一项）`;
        opt.selected = true;
        select.appendChild(opt);
      }
    }
    select.disabled = readOnly;
  }

  renderStatus();
  renderFormLocks();
}

/** 表单锁：配置没读到就**不许**保存 / 测试 / 加子代理。
 *
 * 🔴 不加锁的后果是一次误点把用户的配置覆盖成空（`collectSavePayload` 那时
 *    只能拿到全空 + `backend:"null"`），而界面还会显示"已保存"。
 */
export function renderFormLocks() {
  const readOnly = state.subagentConfig === null;
  for (const id of ["subagent-save", "subagent-test", "subagent-add-agent"]) {
    const el = $(id);
    if (el) el.disabled = readOnly;
  }
}

/** 按选中的后端渲染它的 `fields[]`。**前端不硬编码任何字段名。** */
export function renderBackendFields() {
  const host = $("subagent-backend-fields");
  if (!host) return;
  host.innerHTML = "";

  const cfg = state.subagentConfig;
  const desc = $("subagent-backend-desc");
  const meta = cfg ? backendMeta(cfg.backend || BACKEND_NONE) : null;

  state.subagentFormFields = [];

  if (cfg === null) {
    if (desc) desc.textContent = "";
    host.appendChild(hintLine("配置没读到，先点「重新读取」。"));
    return;
  }
  if (!Array.isArray(state.subagentBackendTypes)) {
    if (desc) desc.textContent = "";
    host.appendChild(hintLine("还没读到 agent 平台目录，所以不知道这个平台要填什么。"));
    return;
  }
  if (!meta) {
    if (desc) desc.textContent = "";
    host.appendChild(hintLine("选中的 agent 平台不在后端目录里 —— 后端可能刚改过，重新读取即可。"));
    return;
  }
  if (desc) desc.textContent = meta.description || "";
  if (!Array.isArray(meta.fields)) {
    // 形状不对 → 如实说（不静默画个空表单，那会被当成"这个平台没有配置项"）
    host.appendChild(hintLine("后端目录里这个平台的 fields 不是数组（后端形状不符）。"));
    return;
  }
  state.subagentFormFields = meta.fields;
  if (!meta.fields.length) {
    host.appendChild(hintLine("这个平台没有需要填的连接参数。"));
    return;
  }
  for (const f of meta.fields) host.appendChild(buildBackendField(f, cfg));
}

function hintLine(text) {
  const p = document.createElement("p");
  p.className = "hint";
  p.textContent = text;
  return p;
}

/** 造一个字段控件（`f.type` 决定画什么）。**结构照 adapters.js 的两列网格**。 */
export function buildBackendField(f, values) {
  const key = f.key;
  const kind = f.type || "string";
  const id = "subagent-field-" + key;
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

  let el;
  if (kind === "bool") {
    const box = document.createElement("label");
    box.className = "adapter-check";
    el = document.createElement("input");
    el.type = "checkbox";
    el.checked = raw === undefined ? !!f.default : !!raw;
    const t = document.createElement("span");
    t.textContent = el.checked ? "开启" : "关闭";
    el.addEventListener("change", () => { t.textContent = el.checked ? "开启" : "关闭"; });
    box.append(el, t);
    ctl.appendChild(box);
  } else if (kind === "int") {
    el = document.createElement("input");
    el.type = "number";
    el.step = "1";
    el.value = raw === undefined || raw === null ? "" : String(raw);
    if (f.default !== undefined && f.default !== null) el.placeholder = String(f.default);
    ctl.appendChild(el);
  } else if (kind === "string_list") {
    el = document.createElement("textarea");
    el.rows = 3;
    el.placeholder = "每行一个值";
    el.value = Array.isArray(raw) ? raw.join("\n") : (raw === undefined || raw === null ? "" : String(raw));
    ctl.appendChild(el);
    ctl.appendChild(hintLine("一行一个值，提交时会按行拆成数组。"));
  } else if (kind === "secret") {
    // ⚠️ 用 text 而不是 password：要让人**看得见**打码值（`•` 开头），
    //    才能判断"它是配过的、我不该动它"（照 adapters.js 的同款决定）。
    el = document.createElement("input");
    el.type = "text";
    el.autocomplete = "off";
    el.spellcheck = false;
    el.value = raw === undefined || raw === null ? "" : String(raw);
    ctl.appendChild(el);
    if (isMasked(el.value)) {
      ctl.appendChild(hintLine(
        "已设置（这里显示的是打码值）：不动它 = 保持原值；清空 = 删除令牌；"
        + "换新令牌请先全选清空再填（保留开头的 • 会被当成「没改」）。"
      ));
    } else {
      el.placeholder = "未设置（填了才生效）";
    }
  } else {
    // string 与**未来新增的未知类型**：一律当文本输入，不静默丢字段
    el = document.createElement("input");
    el.type = "text";
    el.value = raw === undefined || raw === null ? "" : String(raw);
    if (f.placeholder) el.placeholder = f.placeholder;
    ctl.appendChild(el);
    if (kind !== "string") ctl.appendChild(hintLine(`（未知字段类型 ${kind}，按文本渲染）`));
  }
  el.id = id;
  el.dataset.fieldKey = key;

  wrap.append(text, ctl);
  return wrap;
}

/* ---------------- 表单 → 请求体 ---------------- */

/** 收集当前后端 `fields[]` 里声明的字段值（**只收渲染过的那些**）。 */
export function collectBackendForm() {
  const out = {};
  for (const f of state.subagentFormFields || []) {
    const el = $("subagent-field-" + f.key);
    if (!el) continue;
    const kind = f.type || "string";
    if (kind === "bool") {
      out[f.key] = !!el.checked;
    } else if (kind === "int") {
      const raw = String(el.value == null ? "" : el.value).trim();
      // 空 → 交 "" 给后端（它会回落 default）；非数字 → 原样送，让后端报"必须是整数"
      if (raw === "") out[f.key] = "";
      else {
        const n = Number(raw);
        out[f.key] = Number.isFinite(n) ? n : raw;
      }
    } else if (kind === "string_list") {
      out[f.key] = String(el.value == null ? "" : el.value)
        .split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    } else {
      // string / secret：**原文提交**（不 trim —— 密钥里可能有前导空格；
      // 打码值必须原样回传，见纪律 ①）
      out[f.key] = el.value == null ? "" : String(el.value);
    }
  }
  return out;
}

/** 组装 `PUT /api/v1/subagents` 的请求体。
 *
 * 🔴 拆成独立函数是为了**可判据**：打码值原样回传这件事必须能被测到，
 *    而不是埋在 `saveSubagents()` 的请求里。
 * ⚠️ 结构键放在字段 spread **之后**：万一某个平台声明了同名配置字段，
 *    也不许它覆盖掉 `enabled` / `backend` 这两个本页的语义键。
 */
export function collectSavePayload() {
  const cfg = state.subagentConfig || {};
  const enabledEl = $("subagent-enabled");
  const backendEl = $("subagent-backend");
  return {
    ...collectBackendForm(),
    enabled: enabledEl ? !!enabledEl.checked : !!cfg.enabled,
    backend: backendEl && backendEl.value ? backendEl.value : (cfg.backend || BACKEND_NONE),
    agents: collectAgentList(),
  };
}

/* ---------------- 子代理清单 ---------------- */

/** 清单为空时的那两句话 —— **必须说清是哪一种空**（互不相同）。
 *
 * 返回 `{title, text}`：`title` 是粗体的那一行（"是什么状态"），
 * `text` 是下面的说明（"下一步做什么"）。照 `.empty` 的设计：
 * 一条好的空态要说三件事，而不是丢一句灰字。
 */
export function agentsEmptyView() {
  if (state.subagentConfig === null) {
    return state.subagentError
      ? {
        title: "读取失败",
        text: "配置没读到，所以这里也读不到清单（原因见上方提示）。修好后点「重新读取」。",
      }
      : { title: "正在读取…", text: "正在读取子代理配置。" };
  }
  const cfg = state.subagentConfig;
  if (!cfg.backend || cfg.backend === BACKEND_NONE) {
    return {
      title: "还没选 agent 平台",
      text: "先在上面选一个平台、填好连接参数，再回来加子代理。",
    };
  }
  return {
    title: "还没有子代理",
    text: "点「+ 添加子代理」建一个 —— 建好后角色就能通过 transfer_to_<名字> 把活派给它。",
  };
}

/** `transfer_to_<name>` 的工具名（官方 `handoff.py:26` 就是这么拼的）。 */
export function agentToolName(rawName) {
  const name = String(rawName == null ? "" : rawName).trim();
  return `transfer_to_${name}`;
}

/** 一行工具名预览：让用户**看得懂它怎么被调用**。 */
export function agentToolPreview(rawName) {
  const name = String(rawName == null ? "" : rawName).trim();
  return name
    ? `角色会看到的工具：${agentToolName(name)}`
    : "填上名字后，角色会看到一个 transfer_to_<名字> 的工具";
}

/** 按 `state.subagentConfig.agents` 重画清单（`null` = 还没读到，与"读到 0 个"是两件事）。 */
export function renderAgentList() {
  const host = $("subagent-agent-list");
  const empty = $("subagent-agents-empty");
  const emptyTitle = $("subagent-agents-empty-title");
  const emptyText = $("subagent-agents-empty-text");
  if (!host) return;

  host.innerHTML = "";
  const agents = agentsOf(state.subagentConfig);

  if (!agents.length) {
    if (empty) empty.classList.remove("hidden");
    // ⚠️ 只改**文字那两个子节点**：`empty.textContent = …` 会把里面的
    //    `.empty-ico` 一起抹掉（本项目通则：`textContent=` 会清空子节点）。
    const view = agentsEmptyView();
    if (emptyTitle) emptyTitle.textContent = view.title;
    if (emptyText) emptyText.textContent = view.text;
    return;
  }
  if (empty) empty.classList.add("hidden");
  agents.forEach((agent, index) => host.appendChild(buildAgentRow(agent, index)));
}

/** 一张子代理编辑卡。 */
export function buildAgentRow(agent, index) {
  const card = document.createElement("div");
  card.className = "providers-card subagent-agent-card";
  card.dataset.index = String(index);

  const head = document.createElement("div");
  head.className = "subagent-agent-head";

  const name = document.createElement("input");
  name.type = "text";
  name.id = `subagent-agent-${index}-name`;
  name.className = "subagent-agent-name";
  name.placeholder = "名字（英文/下划线，如 coder）";
  name.value = agent.name || "";
  name.dataset.field = "name";
  // `aria-label` 而不是可见的 label：卡片头部是一行紧凑布局，
  // 多一个文字标签会把名字框挤窄（「启用」「移除」已经被压过，见 CSS 注释）。
  name.setAttribute("aria-label", "子代理名字");
  head.appendChild(name);

  const enabledLabel = document.createElement("label");
  enabledLabel.className = "subagent-agent-enabled";
  const enabled = document.createElement("input");
  enabled.type = "checkbox";
  enabled.id = `subagent-agent-${index}-enabled`;
  enabled.checked = agent.enabled !== false;
  enabled.dataset.field = "enabled";
  enabledLabel.htmlFor = enabled.id;
  enabledLabel.appendChild(enabled);
  // ⚠️ 文字必须包在 `<span>` 里，**不能直接 createTextNode**：
  //    flex 容器里的裸文本节点是一个匿名 flex item，`white-space: nowrap` 对它不起作用
  //    （实测被压成两行，"启用"竖排）。
  const enabledText = document.createElement("span");
  enabledText.textContent = "启用";
  enabledLabel.appendChild(enabledText);
  head.appendChild(enabledLabel);

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "subagent-agent-remove";
  remove.textContent = "移除";
  remove.dataset.action = "remove-agent";
  remove.dataset.index = String(index);
  head.appendChild(remove);

  card.appendChild(head);

  // 工具名预览：让用户看得懂它怎么被调用；名字一改就跟着变
  // （没有这条，用户会以为工具名没生效）
  const preview = document.createElement("p");
  preview.className = "hint subagent-agent-preview";
  preview.textContent = agentToolPreview(agent.name);
  card.appendChild(preview);
  name.addEventListener("input", () => { preview.textContent = agentToolPreview(name.value); });

  card.appendChild(agentTextarea(
    agent, index, "system_prompt",
    "指令", "这个子代理按什么身份干活（作为它的 system 提示词）", 5,
  ));
  card.appendChild(agentTextarea(
    agent, index, "public_description",
    "对外描述", "角色看到的一行说明；留空 = 用默认描述", 2,
  ));
  card.appendChild(agentTextarea(
    agent, index, "tools",
    "可用工具", "允许它用的工具名，一行一个；留空 = 不限制（由对面平台决定）", 3,
  ));

  const provider = document.createElement("input");
  provider.type = "text";
  provider.id = `subagent-agent-${index}-provider`;
  provider.placeholder = "留空 = 用默认模型";
  provider.value = agent.provider_id || "";
  provider.dataset.field = "provider_id";
  card.appendChild(labeledField("模型供应商", provider, "可选：让这个子代理用指定的模型供应商", provider.id));

  return card;
}

/** 左说明 / 右控件（复用全站的 `.field-labeled` 两列网格）。 */
function labeledField(labelText, control, helpText, htmlFor) {
  const wrap = document.createElement("div");
  wrap.className = "field-labeled";
  const text = document.createElement("div");
  text.className = "fl-text";
  const label = document.createElement("label");
  if (htmlFor) label.htmlFor = htmlFor;
  label.textContent = labelText;
  text.appendChild(label);
  if (helpText) {
    const h = document.createElement("span");
    h.className = "hint";
    h.textContent = helpText;
    text.appendChild(h);
  }
  const ctl = document.createElement("div");
  ctl.className = "fl-ctl";
  ctl.appendChild(control);
  wrap.append(text, ctl);
  return wrap;
}

function agentTextarea(agent, index, key, labelText, helpText, rows) {
  const el = document.createElement("textarea");
  el.id = `subagent-agent-${index}-${key}`;
  el.rows = rows;
  el.dataset.field = key;
  el.value = key === "tools"
    ? (Array.isArray(agent.tools) ? agent.tools.join("\n") : "")
    : (agent[key] == null ? "" : String(agent[key]));
  return labeledField(labelText, el, helpText, el.id);
}

/** 从 DOM 读回清单（**保留用户刚打的字**，包括还没保存的）。
 *
 * ⚠️ 只 trim `name` / `provider_id`（它们是 id 类，带空白会被当成另一个名字），
 *    正文类字段**原文返回** —— `strip()` 静默改掉用户原文是本项目记过的一条坑
 *    （判断空可以 trim，保存必须是原文）。
 */
export function collectAgentList() {
  const host = $("subagent-agent-list");
  if (!host) return [];
  return Array.from(host.querySelectorAll(".subagent-agent-card")).map((card) => {
    const read = (key) => {
      const el = card.querySelector(`[data-field="${key}"]`);
      return el ? String(el.value == null ? "" : el.value) : "";
    };
    const enabledEl = card.querySelector('[data-field="enabled"]');
    return {
      name: read("name").trim(),
      system_prompt: read("system_prompt"),
      public_description: read("public_description"),
      tools: read("tools").split(/\r?\n/).map((s) => s.trim()).filter(Boolean),
      provider_id: read("provider_id").trim(),
      enabled: enabledEl ? !!enabledEl.checked : true,
    };
  });
}

/** 把 DOM 里的编辑结果写回 `state`（**增删之前必须先做**，否则会丢用户刚打的字）。 */
function syncAgentsFromDom() {
  if (state.subagentConfig === null) return;
  state.subagentConfig = { ...state.subagentConfig, agents: collectAgentList() };
}

/** 「+ 添加子代理」：先保住已打的字，再追加一个空白项。 */
export function addAgent() {
  if (state.subagentConfig === null) {
    toast($("subagents-hint"), "配置还没读到，先点「重新读取」再添加。", true);
    return;
  }
  syncAgentsFromDom();
  const agents = agentsOf(state.subagentConfig).slice();
  agents.push({
    name: "", system_prompt: "", public_description: "",
    tools: [], provider_id: "", enabled: true,
  });
  state.subagentConfig = { ...state.subagentConfig, agents };
  renderAgentList();
  // 焦点落到新卡片的「名字」上（否则用户还要自己找一遍）
  const cards = $("subagent-agent-list").querySelectorAll(".subagent-agent-card");
  const last = cards[cards.length - 1];
  const input = last && last.querySelector('[data-field="name"]');
  if (input && input.focus) input.focus();
}

/** 移除第 `index` 个子代理（只改 DOM/state，**保存才落盘**）。 */
export function removeAgent(index) {
  if (state.subagentConfig === null) return;
  syncAgentsFromDom();
  const agents = agentsOf(state.subagentConfig).slice();
  if (!Number.isInteger(index) || index < 0 || index >= agents.length) return;
  agents.splice(index, 1);
  state.subagentConfig = { ...state.subagentConfig, agents };
  renderAgentList();
}

/* ---------------- 后端切换 / 参数变更 ---------------- */

/** 后端下拉变化 → 只重画字段区（**不重建下拉**，否则会把用户的选择弹回旧值）。 */
export function onSubagentBackendChange() {
  const select = $("subagent-backend");
  if (!select || state.subagentConfig === null) return;
  const chosen = select.value;
  state.subagentConfig = { ...state.subagentConfig, backend: chosen };
  // 换了平台，上一次的测试结论不再成立
  state.subagentTest = null;
  clearTestResult();
  renderBackendFields();
  renderStatus();
  if (select.value !== chosen) select.value = chosen;
}

/** 连接参数被改动 → 之前的测试结论作废（否则徽标会显示一个过期结论）。 */
export function onSubagentParamsChanged() {
  if (state.subagentConfig === null) return;
  if (!state.subagentTest) return;
  state.subagentTest = null;
  clearTestResult("参数改过了，之前的测试结论不再有效 —— 请重新测试。");
  renderStatus();
}

/** 清空并复位测试结果区（`msg` 为空就是彻底清掉）。 */
export function clearTestResult(msg) {
  const hint = $("subagent-test-result");
  if (!hint) return;
  hint.textContent = msg || "";
  hint.dataset.tone = msg ? "muted" : "";
}

/* ---------------- 写操作：保存 / 测试 ---------------- */

/** 保存配置（**局部更新**：只发改过的结构键 + 当前后端的字段）。 */
export async function saveSubagents() {
  const hint = $("subagents-hint");
  const btn = $("subagent-save");
  if (state.subagentConfig === null) {
    // 界面上的按钮已经 disable 了；这里再挡一次（别让人绕过，把配置覆盖成空）
    toast(hint, "配置还没读到，不能保存（保存会把没读到的配置覆盖成空）。先点「重新读取」。", true);
    return false;
  }
  const payload = collectSavePayload();
  const oldText = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "保存中…";
  }
  toast(hint, "保存中…");
  try {
    const d = await apiData("PUT", "/api/v1/subagents", payload);
    const cfg = requireConfig(d);
    state.subagentConfig = cfg;
    state.subagentError = "";
    // 配置变了 → 旧测试结论不再成立
    state.subagentTest = null;
    clearTestResult();
    renderSubagentForm();
    renderAgentList();
    renderBackendFields();

    if (d.applied === false) {
      // 🔴 后端明说"没生效"：必须原样讲出来，不能只报"保存成功"
      toast(hint, `已保存，但**尚未生效**：${d.reason || "后端没给原因"}`
        + "。要让改动真的起作用，需要先处理这条原因（例如重启服务）。", true);
    } else {
      const count = agentsOf(cfg).length;
      toast(hint, `已保存并生效：${backendLabel(cfg.backend)} · ${count} 个子代理`
        + (cfg.enabled ? "。" : "（总开关关着，工具不会注册）。"));
    }
    return true;
  } catch (e) {
    // 失败**留在表单里**：用户填的东西不能因为一次报错就没了
    toast(hint, `保存失败：${subagentErrorText(e)}`, true);
    return false;
  } finally {
    if (btn) {
      btn.disabled = state.subagentConfig === null;
      btn.textContent = oldText;
    }
  }
}

/** 连通性测试。
 *
 * 🔴 **必须读 `data.ok`**：后端把"业务失败"也用 HTTP 200 返回
 *    （那不是请求格式错误，是"试了，对面不通"）。只看 HTTP 状态码会把
 *    "连不上"显示成"成功" —— 本项目最贵的那类假成功。
 * 🔴 成败都**原样显示 reason**，不能只写"测试完成"。
 */
export async function testSubagentConnection() {
  const hint = $("subagent-test-result");
  const btn = $("subagent-test");
  if (state.subagentConfig === null) {
    toast($("subagents-hint"), "配置还没读到，先点「重新读取」再测试。", true);
    return false;
  }
  const backendEl = $("subagent-backend");
  const payload = {
    ...collectBackendForm(),
    // ⚠️ 结构键最后写：字段里万一同名，也不许覆盖掉真正的后端 id
    backend: backendEl && backendEl.value ? backendEl.value : BACKEND_NONE,
  };
  const oldText = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "测试中…";
  }
  if (hint) {
    hint.textContent = "正在测试…";
    hint.dataset.tone = "muted";
  }
  try {
    const d = await apiData("POST", "/api/v1/subagents/test", payload);
    const ok = !!(d && d.ok);
    const reason = (d && d.reason) || (ok ? "连通正常（后端没给细节）。" : "连接失败（后端没给原因）。");
    const backend = (d && d.backend) || payload.backend;
    const extra = [];
    const detail = d && d.detail;
    if (detail && typeof detail.elapsed_ms === "number" && detail.elapsed_ms > 0) {
      extra.push(`${Math.round(detail.elapsed_ms)} ms`);
    }
    if (detail && detail.preview) extra.push(`对方返回：${String(detail.preview).slice(0, 120)}`);
    state.subagentTest = { ok, reason };
    if (hint) {
      hint.textContent = (ok ? `✅ ${backend} 已连通：` : `❌ ${backend} 连不上：`)
        + reason
        + (extra.length ? `（${extra.join("；")}）` : "");
      hint.dataset.tone = ok ? "ok" : "bad";
    }
    renderStatus();
    return ok;
  } catch (e) {
    // 请求本身失败（网络/鉴权/形状）—— 同样如实显示，并让徽标落到"未连通"
    const reason = subagentErrorText(e);
    state.subagentTest = { ok: false, reason };
    if (hint) {
      hint.textContent = `❌ 测试请求失败：${reason}`;
      hint.dataset.tone = "bad";
    }
    renderStatus();
    return false;
  } finally {
    if (btn) {
      btn.disabled = state.subagentConfig === null;
      btn.textContent = oldText;
    }
  }
}
