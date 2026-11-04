/* subagents —— 子代理接入（让用户**连接自己的 agent 平台**）（原生 ES 模块，零构建）
 *
 * 🔴 本文件只放**函数/常量声明**，**没有模块级副作用**
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
 *    后端识别打码值并保持原值。**绝不能把打码值当新令牌**（那会把 token 洗成 `••••`，
 *    之后所有请求 401 而界面还显示"已配置"——本项目真踩过）。
 *
 * ② **表单字段一律由后端 `fields[]` 驱动**，前端**不硬编码字段名**。
 *    抄一份字段清单 = 早晚和后端漂移，且"漏渲染"会伪装成"这个后端没有这项配置"。
 *
 * ③ **连接测试要读 `data.ok`**，不能只看 HTTP 状态码 ——
 *    后端把"业务失败"也用 200 返回（那不是请求格式错误，是"试了，对面不通"）。
 *
 * ④ **`applied=false` 必须显式告警**：后端说的是"配置保存了但没生效"。
 *    只弹"保存成功"会让用户以为改了没反应是界面坏了。
 */

import { $, api, apiData, state, toast } from "../core/core.js";

/* ---------------- 错误文案 ---------------- */

/** 后端错误 → 给用户看的一句话。
 *
 * 🔴 必须用信封里的 `message`，`reason` 存在时**一并显示** ——
 *    只写"保存失败"等于把后端说清的原因扔掉（用户没有任何可行动的线索）。
 */
export function subagentErrorText(e) {
  const msg = (e && e.message) || "未知错误";
  const reason = (e && e.reason) || "";
  const status = e && e.status ? e.status : 0;
  const bits = [msg];
  if (reason && !msg.includes(reason)) bits.push(`（reason: ${reason}）`);
  if (status && /^HTTP \d+$/.test(msg)) bits.push(`（HTTP ${status}）`);
  return bits.join("");
}

/** 当前是不是"还没读到"的初始态。 */
export function subagentConfigMissing() {
  return state.subagentConfig === null;
}

/* ---------------- 读 ---------------- */

/** 进 `#/subagents` 时由 `NAV_LOADERS` 调。
 *
 * 返回 `true` 表示**配置真的读到了**；失败时返回 `false` 并把原因写进提示区
 * （`state.subagentError` 供其它函数判断"当前是不是失败态"）。
 */
export async function loadSubagents() {
  const hint = $("subagents-hint");
  try {
    const d = await apiData("GET", "/api/v1/subagents");
    state.subagentConfig = (d && d.config) || {};
    state.subagentBackendTypes = (d && Array.isArray(d.backend_types)) ? d.backend_types : [];
    state.subagentError = "";
    renderSubagentForm();
    renderAgentList();
    await ensureBackendTypes();
    renderBackendFields();
    toast(hint, "");
    return true;
  } catch (e) {
    // 🔴 失败要**明确显示失败**，不能白屏、不能把"没读到"显示成"你没配"
    state.subagentConfig = null;
    state.subagentError = subagentErrorText(e);
    renderSubagentForm();
    renderAgentList();
    toast(hint, `读取子代理配置失败：${state.subagentError}`, true);
    return false;
  }
}

/** 确保 `state.subagentBackendTypes` 有数据。 */
export async function ensureBackendTypes() {
  if (Array.isArray(state.subagentBackendTypes) && state.subagentBackendTypes.length) return true;
  try {
    const d = await apiData("GET", "/api/v1/subagents/backends");
    state.subagentBackendTypes = (d && Array.isArray(d.types)) ? d.types : [];
    return true;
  } catch (e) {
    toast($("subagents-hint"), `读取后端目录失败：${subagentErrorText(e)}`, true);
    return false;
  }
}

/** 某个后端的元数据；找不到返回 null。 */
export function backendMeta(id) {
  const list = state.subagentBackendTypes || [];
  return list.find((t) => t && t.id === id) || null;
}

/* ---------------- 渲染：总开关 / 后端 / 状态 ---------------- */

/** 当前配置的"三态"（**不许合并成一个布尔**）。
 *
 * 未配置 / 已配置但未生效 / 已连通 —— 三件事的修法完全不同，
 * 合成一个"已启用"会让用户对着绿色发呆（照 adapters 的纪律）。
 */
export function subagentStateView() {
  const cfg = state.subagentConfig || {};
  if (state.subagentConfig === null) return { key: "unknown", label: "读取中…", tone: "muted" };
  if (state.subagentError) return { key: "error", label: "读取失败", tone: "bad" };
  if (!cfg.enabled) return { key: "off", label: "未启用", tone: "muted" };
  if (!cfg.backend || cfg.backend === "null") {
    return { key: "no-backend", label: "已启用，但没选后端", tone: "warn" };
  }
  if (!cfg.endpoint) return { key: "no-endpoint", label: "已启用，但没填地址", tone: "warn" };
  return { key: "configured", label: "已配置", tone: "ok" };
}

export function renderSubagentForm() {
  const cfg = state.subagentConfig || {};

  const enabledEl = $("subagent-enabled");
  if (enabledEl) enabledEl.checked = !!cfg.enabled;

  const statusEl = $("subagent-status");
  if (statusEl) {
    const view = subagentStateView();
    statusEl.textContent = view.label;
    statusEl.dataset.tone = view.tone;
  }

  // 后端下拉：由目录驱动（后端给什么画什么）
  const select = $("subagent-backend");
  if (select) {
    const current = cfg.backend || "null";
    const types = state.subagentBackendTypes || [];
    // 只重建 options，不动 select 本身（保留焦点）
    select.innerHTML = "";
    for (const t of types) {
      const opt = document.createElement("option");
      opt.value = t.id;
      opt.textContent = t.label || t.id;
      if (t.id === current) opt.selected = true;
      select.appendChild(opt);
    }
    // 目录里没有当前值时**如实显示**（不静默留空 —— 那会让人以为是后端丢了）
    if (types.length && !types.some((t) => t.id === current)) {
      const opt = document.createElement("option");
      opt.value = current;
      opt.textContent = `${current}（后端目录里没有这一项）`;
      opt.selected = true;
      select.appendChild(opt);
    }
  }
}

/** 按选中的后端渲染它的 `fields[]`。**前端不硬编码任何字段名。** */
export function renderBackendFields() {
  const host = $("subagent-backend-fields");
  if (!host) return;
  host.innerHTML = "";

  const cfg = state.subagentConfig || {};
  const meta = backendMeta(cfg.backend || "null");
  state.subagentFormFields = (meta && Array.isArray(meta.fields)) ? meta.fields : [];

  const desc = $("subagent-backend-desc");
  if (desc) {
    desc.textContent = meta ? (meta.description || "") : "";
  }

  if (!state.subagentFormFields.length) {
    // 空态要说清是"这个后端没有可配项"，不是"渲染失败"
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = meta
      ? "这个后端没有需要填的连接参数。"
      : "还没有读到后端目录。";
    host.appendChild(empty);
    return;
  }

  for (const f of state.subagentFormFields) {
    host.appendChild(buildBackendField(f, cfg));
  }
}

/** 画一个字段。`type` 只认 string/int/bool/secret/string_list。 */
export function buildBackendField(f, values) {
  const key = f.key;
  const kind = f.type || "string";
  const id = "subagent-field-" + key;
  const raw = Object.prototype.hasOwnProperty.call(values, key) ? values[key] : f.default;

  // 🔴 结构必须照 `.field-labeled` 的两列网格来：
  //    左列 `.fl-text`（label + hint）、右列直接放控件。
  //    ⚠️ 少了 `.fl-text` 包裹层，label 和控件会各自独占一格 → 整行错位。
  //    真浏览器截图抓到过（标签在左、输入框跑到下一行、复选框飘到中间）。
  const wrap = document.createElement("div");
  wrap.className = "field-labeled";
  wrap.dataset.key = key;
  wrap.dataset.fieldType = kind;

  const textWrap = document.createElement("div");
  textWrap.className = "fl-text";
  const label = document.createElement("label");
  label.htmlFor = id;
  label.textContent = f.label || key;
  if (f.required) {
    const req = document.createElement("span");
    req.className = "adapter-required";
    req.textContent = "必填";
    label.appendChild(req);
  }
  textWrap.appendChild(label);
  if (f.help) {
    const help = document.createElement("span");
    help.className = "hint";
    help.textContent = f.help;
    textWrap.appendChild(help);
  }
  wrap.appendChild(textWrap);

  let el;
  if (kind === "bool") {
    el = document.createElement("input");
    el.type = "checkbox";
    el.checked = !!raw;
  } else if (kind === "string_list") {
    el = document.createElement("textarea");
    el.rows = 4;
    el.value = Array.isArray(raw) ? raw.join("\n") : String(raw == null ? "" : raw);
  } else if (kind === "int") {
    el = document.createElement("input");
    el.type = "number";
    el.value = raw == null ? "" : String(raw);
    if (f.default != null) el.placeholder = String(f.default);
  } else if (kind === "secret") {
    el = document.createElement("input");
    el.type = "password";
    // ⚠️ 打码值原样放进 value：用户不动它 → 原样回传 → 后端保持原值。
    //    故意用 password 类型，避免有人从屏幕上抄走。
    el.value = raw == null ? "" : String(raw);
    el.autocomplete = "new-password";
    if (/^•/.test(el.value)) el.placeholder = "留空或保持不动表示不修改";
  } else {
    el = document.createElement("input");
    el.type = "text";
    el.value = raw == null ? "" : String(raw);
    if (f.placeholder) el.placeholder = f.placeholder;
  }
  el.id = id;
  el.dataset.fieldKey = key;
  el.dataset.fieldType = kind;
  wrap.appendChild(el);
  return wrap;
}

/** 收集后端连接表单（只收当前后端 `fields[]` 里声明的字段）。 */
export function collectBackendForm() {
  const out = {};
  for (const f of state.subagentFormFields || []) {
    const el = $("subagent-field-" + f.key);
    if (!el) continue;
    const kind = f.type || "string";
    if (kind === "bool") out[f.key] = !!el.checked;
    else if (kind === "int") {
      const raw = String(el.value == null ? "" : el.value).trim();
      if (raw === "") out[f.key] = "";
      else {
        const n = Number(raw);
        out[f.key] = Number.isFinite(n) ? n : raw;
      }
    } else if (kind === "string_list") {
      out[f.key] = String(el.value == null ? "" : el.value)
        .split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    } else {
      // string / secret：**原文提交**（不 trim —— 密钥里可能有前导空格）
      out[f.key] = el.value == null ? "" : String(el.value);
    }
  }
  return out;
}

/* ---------------- 渲染：子代理清单 ---------------- */

export function renderAgentList() {
  const host = $("subagent-agent-list");
  const empty = $("subagent-agents-empty");
  if (!host) return;
  host.innerHTML = "";

  const cfg = state.subagentConfig;
  if (cfg === null) {
    if (empty) {
      empty.classList.remove("hidden");
      empty.textContent = state.subagentError
        ? "配置读取失败（原因见上方提示）。"
        : "正在读取…";
    }
    return;
  }

  const agents = Array.isArray(cfg.agents) ? cfg.agents : [];
  if (!agents.length) {
    if (empty) {
      empty.classList.remove("hidden");
      empty.textContent = "还没有子代理。点「+ 添加子代理」建一个 —— "
        + "建好后角色就能通过 transfer_to_<名字> 把活派给它。";
    }
    return;
  }
  if (empty) empty.classList.add("hidden");

  agents.forEach((agent, index) => host.appendChild(buildAgentRow(agent, index)));
}

/** `transfer_to_<name>` 的可读预览。
 *
 * ⚠️ 前缀是官方约定（`handoff.py:26` 拼的就是 `transfer_to_`），换掉会对不上。
 */
export function agentToolPreview(rawName) {
  const name = String(rawName == null ? "" : rawName).trim();
  return `角色会看到工具：transfer_to_${name || "（未命名）"}`;
}

/** 一行 = 一个子代理的编辑卡。 */
export function buildAgentRow(agent, index) {
  const card = document.createElement("div");
  card.className = "subagent-agent-card";
  card.dataset.index = String(index);

  const head = document.createElement("div");
  head.className = "subagent-agent-head";

  const name = document.createElement("input");
  name.type = "text";
  name.className = "subagent-agent-name";
  name.placeholder = "子代理名字（英文，如 coder）";
  name.value = agent.name || "";
  name.dataset.field = "name";
  head.appendChild(name);

  const enabledLabel = document.createElement("label");
  enabledLabel.className = "subagent-agent-enabled";
  const enabled = document.createElement("input");
  enabled.type = "checkbox";
  enabled.checked = agent.enabled !== false;
  enabled.dataset.field = "enabled";
  enabledLabel.appendChild(enabled);
  // 🔴 文字必须包在 `<span>` 里，**不能直接 `createTextNode`**：
  //    flex 容器里的裸文本节点是一个匿名 flex item，`white-space: nowrap`
  //    对它不起作用 —— 实测宽度被压到 26px、文字竖排成两行
  //    （真浏览器截图 + getBoundingClientRect 双双抓到）。
  const enabledText = document.createElement("span");
  enabledText.textContent = "启用";
  enabledLabel.appendChild(enabledText);
  head.appendChild(enabledLabel);

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "btn btn-ghost subagent-agent-remove";
  remove.textContent = "移除";
  remove.dataset.action = "remove-agent";
  remove.dataset.index = String(index);
  head.appendChild(remove);

  card.appendChild(head);

  // 🔴 工具名预览：让用户**看得懂它怎么被调用**。
  //    `transfer_to_` 前缀是官方约定（`handoff.py:26`），换掉会对不上。
  const preview = document.createElement("p");
  preview.className = "hint subagent-agent-preview";
  preview.textContent = agentToolPreview(agent.name);
  card.appendChild(preview);
  // 🔴 名字一改预览就要跟着变：真浏览器判据抓到过"改了名字预览还是（未命名）"。
  //    没有这条，用户会以为工具名没生效。
  name.addEventListener("input", () => {
    preview.textContent = agentToolPreview(name.value);
  });

  card.appendChild(buildAgentTextarea(agent, "system_prompt", "子代理的指令（它会按这个身份干活）", 4));
  card.appendChild(buildAgentTextarea(
    agent, "public_description",
    "对外描述（角色看到的一行说明，留空用默认）", 2,
  ));
  card.appendChild(buildAgentTextarea(agent, "tools", "允许它用的工具名（一行一个，可留空）", 3));

  const providerField = document.createElement("div");
  providerField.className = "field-labeled";
  const providerText = document.createElement("div");
  providerText.className = "fl-text";
  const providerLabel = document.createElement("label");
  providerLabel.textContent = "指定模型供应商（可选）";
  providerText.appendChild(providerLabel);
  providerField.appendChild(providerText);
  const provider = document.createElement("input");
  provider.type = "text";
  provider.placeholder = "留空 = 用默认";
  provider.value = agent.provider_id || "";
  provider.dataset.field = "provider_id";
  providerField.appendChild(provider);
  card.appendChild(providerField);

  return card;
}

function buildAgentTextarea(agent, key, labelText, rows) {
  // 结构同 `buildBackendField`：左 `.fl-text`、右控件（见那里的长注释）
  const wrap = document.createElement("div");
  wrap.className = "field-labeled";
  const textWrap = document.createElement("div");
  textWrap.className = "fl-text";
  const label = document.createElement("label");
  label.textContent = labelText;
  textWrap.appendChild(label);
  wrap.appendChild(textWrap);
  const el = document.createElement("textarea");
  el.rows = rows;
  el.dataset.field = key;
  if (key === "tools") {
    el.value = Array.isArray(agent.tools) ? agent.tools.join("\n") : "";
  } else {
    el.value = agent[key] == null ? "" : String(agent[key]);
  }
  wrap.appendChild(el);
  return wrap;
}

/** 从 DOM 读回子代理清单（**保留用户刚打的字**，不走 state）。 */
export function collectAgentList() {
  const host = $("subagent-agent-list");
  if (!host) return [];
  const out = [];
  for (const card of host.querySelectorAll(".subagent-agent-card")) {
    const read = (sel) => {
      const el = card.querySelector(sel);
      return el ? String(el.value == null ? "" : el.value) : "";
    };
    const toolsRaw = read('[data-field="tools"]');
    const enabledEl = card.querySelector('[data-field="enabled"]');
    out.push({
      name: read('[data-field="name"]').trim(),
      system_prompt: read('[data-field="system_prompt"]').trim(),
      public_description: read('[data-field="public_description"]').trim(),
      tools: toolsRaw.split(/\r?\n/).map((s) => s.trim()).filter(Boolean),
      provider_id: read('[data-field="provider_id"]').trim(),
      enabled: enabledEl ? !!enabledEl.checked : true,
    });
  }
  return out;
}

/* ---------------- 动作 ---------------- */

/** 保存配置。 */
export async function saveSubagents() {
  const hint = $("subagents-hint");
  const enabledEl = $("subagent-enabled");
  const backendEl = $("subagent-backend");
  const payload = {
    enabled: enabledEl ? !!enabledEl.checked : false,
    backend: backendEl ? backendEl.value : "null",
    agents: collectAgentList(),
    ...collectBackendForm(),
  };
  try {
    const d = await apiData("PUT", "/api/v1/subagents", payload);
    if (d && d.config) state.subagentConfig = d.config;
    state.subagentError = "";
    renderSubagentForm();
    renderAgentList();
    renderBackendFields();
    if (d && d.applied === false) {
      // 🔴 后端明确说了"没生效" —— 必须原样告诉用户，不能只弹"保存成功"
      toast(hint, `已保存，但**尚未生效**：${d.reason || "原因未知"}`, true);
    } else {
      toast(hint, "子代理配置已保存。");
    }
    return true;
  } catch (e) {
    toast(hint, `保存失败：${subagentErrorText(e)}`, true);
    return false;
  }
}

/** 连通性测试 —— **读 `data.ok`**，只看 HTTP 状态码会漏掉业务失败。 */
export async function testSubagentConnection() {
  const hint = $("subagent-test-result");
  const backendEl = $("subagent-backend");
  // ⚠️ 顺序不能反：`backend` 必须**最后**写，不能被字段里同名键覆盖。
  //    真浏览器判据抓到过一次：切到 null 后仍报"没有填接口地址"，
  //    原因是旧后端的字段还留在 payload 里（那时 backend 已被选成 null）。
  const payload = {
    ...collectBackendForm(),
    backend: backendEl ? backendEl.value : "null",
  };
  if (hint) {
    hint.textContent = "正在测试…";
    hint.dataset.tone = "muted";
  }
  try {
    const d = await apiData("POST", "/api/v1/subagents/test", payload);
    const ok = !!(d && d.ok);
    const reason = (d && d.reason) || (ok ? "连通性正常。" : "连接失败（后端没给原因）。");
    if (hint) {
      hint.textContent = reason;
      hint.dataset.tone = ok ? "ok" : "bad";
    }
    return ok;
  } catch (e) {
    if (hint) {
      hint.textContent = `测试失败：${subagentErrorText(e)}`;
      hint.dataset.tone = "bad";
    }
    return false;
  }
}

/** 后端下拉变化 → 重画它的字段（不同后端的字段不一样）。 */
export function onSubagentBackendChange() {
  const select = $("subagent-backend");
  if (!select) return;
  // 🔴 **只更新 state，绝不再调 `renderSubagentForm()`**。
  //    它会重建 `<option>` 列表，而重建时用 `state.subagentConfig.backend`
  //    去挑 `selected` —— 一旦 state 与用户刚选的值不同，下拉会**弹回旧值**。
  //    真浏览器判据抓到过：选 null 后读回仍是 http，于是测试打到错的后端。
  const chosen = select.value;
  state.subagentConfig = { ...(state.subagentConfig || {}), backend: chosen };
  renderBackendFields();
  // 兜底：渲染完再确认一次，防止任何重建把值改掉
  if (select.value !== chosen) select.value = chosen;
  const hint = $("subagent-test-result");
  if (hint) {
    hint.textContent = "";
    hint.dataset.tone = "";
  }
}

/** 新增一个空白子代理。 */
export function addAgent() {
  if (state.subagentConfig === null) {
    toast($("subagents-hint"), "配置还没读到，先等一下再添加。", true);
    return;
  }
  const agents = Array.isArray(state.subagentConfig.agents)
    ? state.subagentConfig.agents.slice() : [];
  agents.push({
    name: "", system_prompt: "", public_description: "",
    tools: [], provider_id: "", enabled: true,
  });
  state.subagentConfig = { ...state.subagentConfig, agents };
  renderAgentList();
}

/** 移除第 index 个子代理（只改 DOM/state，**保存才落盘**）。 */
export function removeAgent(index) {
  if (state.subagentConfig === null) return;
  const agents = Array.isArray(state.subagentConfig.agents)
    ? state.subagentConfig.agents.slice() : [];
  if (index < 0 || index >= agents.length) return;
  agents.splice(index, 1);
  state.subagentConfig = { ...state.subagentConfig, agents };
  renderAgentList();
}
