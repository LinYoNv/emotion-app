"use strict";
window.CompanionMigrationConfiguration = (() => {
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char =>
    ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[char]));
  const GROUPS = [
    {id: "role", title: "角色与模型", note: "角色设置、回复方式与模型路由"},
    {id: "life", title: "主动与生活", note: "主动联系、日程、作息与天气"},
    {id: "memory", title: "记忆与表达", note: "记忆、表达学习与关系偏好"},
    {id: "external", title: "外部能力", note: "语音、生图、设备与其他拓展"},
  ];
  let plan = null, rows = [], decisions = new Map(), filter = "all", query = "", page = 1;
  const PAGE_SIZE = 40;
  // Every settings page publishes its own field names; the review table must
  // borrow all of them, not just the generic editor, or the operator only sees
  // dotted internal paths for whole sections.
  const LABEL_SOURCES = ["CompanionConfigEditors", "CompanionReplyPage", "CompanionHostOptionsPage",
    "CompanionStateMaintenance", "CompanionMemorySettingsSection", "CompanionNativePerceptionPage"];
  function labelIndex() {
    const index = new Map();
    for (const name of LABEL_SOURCES) {
      let entries = [];
      try { entries = window[name]?.searchEntries?.() || []; } catch (error) { entries = []; }
      for (const entry of entries) {
        if (!entry || !entry.field) continue;
        const key = `${entry.group}.${entry.field}`;
        if (!index.has(key)) index.set(key, entry);
      }
    }
    return index;
  }
  // Generic editors publish groups relative to their domain (a `busy_gate`
  // group under `conversation.*`), so fall back to dropping leading namespaces.
  function describeField(index, field) {
    const parts = String(field || "").split(".");
    for (let start = 0; start < parts.length - 1; start += 1) {
      const entry = index.get(parts.slice(start).join("."));
      if (entry && entry.label && entry.label !== entry.field) return entry;
    }
    return null;
  }
  const supported = () => plan?.configuration_review?.version === 1;
  const groupFor = field => /^(proactive|role\.state|life|weather|calendar|humanized_state|life_schedule|proactive_message)/.test(field)
    ? "life" : /^(memory|semantic|role\.expression|relationship|knowledge|expression|emotion_relationship)/.test(field)
      ? "memory" : /^(image|reality|game|together|live|screen|content|conversation\.(voice|image|reaction)|qzone|poke)/.test(field)
        ? "external" : "role";
  const display = (value, known = true) => {
    if (!known) return "未提供当前值";
    if (value === true || value === "True") return "开启";
    if (value === false || value === "False") return "关闭";
    if (value === null || value === undefined || value === "") return "未设置";
    if (typeof value === "object") {
      if (Object.hasOwn(value, "configured")) return value.configured ? "已配置" : "未配置";
      if (Array.isArray(value)) return value.length ? `${value.length} 项：${value.slice(0, 3).map(item =>
        typeof item === "object" ? item.name || item.model || item.id || "已配置" : String(item)).join("、")}${value.length > 3 ? "…" : ""}` : "未设置";
      return Object.entries(value).slice(0, 4).map(([key, item]) =>
        key.endsWith("_set") ? `${key === "api_key_set" ? "密钥" : "参数"}${item ? "已配置" : "未配置"}`
          : `${key}：${typeof item === "object" ? "已配置" : display(item)}`).join(" · ") || "未设置";
    }
    const text = String(value).replace(/\bV1\b/g, "旧版").replace(/\bV2\b/g, "7.0.0");
    return text.length > 160 ? text.slice(0, 160) + "…" : text;
  };
  function update(next) {
    if (next === plan) return;
    const same = plan?.plan_token && next?.plan_token === plan.plan_token;
    plan = next;
    if (!same) { decisions = new Map(); filter = "all"; query = ""; page = 1; }
    const labels = labelIndex();
    rows = (supported() ? plan.configuration_review.rows || [] : []).map(item => {
      const label = describeField(labels, item.field);
      const reviewed = item.audit ? window.CompanionMigrationConfigReview?.describe(item) : null;
      const noteOutcome = !item.selection_id && !["already_present", "preserved_v2", "already_represented", "host_managed", "mode_preserved"].includes(item.kind)
        ? ["changed_semantics", "partial_mapping", "limit_changed", "mapped_to_custom", "source_precedence", "alias_shadowed"].includes(item.kind)
          ? "mechanism" : ["unmapped", "preserved_not_executable"].includes(item.kind) ? "unsupported" : item.kind ? "unknown" : "" : "";
      const name = item.label || label?.label || reviewed?.title || item.field;
      const status = !reviewed ? item.status : reviewed.outcome === "settings" ? "manual"
        : ["retained", "inapplicable"].includes(reviewed.outcome) ? "archive" : "keep";
      return {...item, status, outcome: reviewed?.outcome || noteOutcome, editor: reviewed?.editor, label: name, group: groupFor(item.field),
        key: name === item.field ? "" : item.field,
        help: String(label?.help || "").replace(/\bV1\b/g, "旧版").replace(/\bV2\b/g, "7.0.0"),
        oldText: item.before === undefined ? "—" : display(item.before),
        currentText: reviewed && !item.current_known ? "未自动接续" : item.current_known ? display(item.after) : "—",
        note: String(reviewed?.summary || item.note || "").replace(/\bV1\b/g, "旧版").replace(/\bV2\b/g, "7.0.0")};
    });
  }
  const decision = row => decisions.has(row.id) ? decisions.get(row.id)
    : row.status === "confirm" ? null : row.selection_id ? "merge" : "keep";
  function stats() {
    const result = {total: rows.length, pending: 0, merge: 0, keep: 0, archive: 0, deferred: 0};
    for (const row of rows) {
      const choice = decision(row);
      if (choice === null) result.pending++;
      else if (row.status === "archive") result.archive++;
      else if (choice === "merge") result.merge++;
      else { result.keep++; if (row.status === "manual") result.deferred++; }
    }
    return result;
  }
  const selection = () => rows.filter(row => row.selection_id && decision(row) === "merge").map(row => row.selection_id);
  const matches = row => {
    const choice = decision(row);
    if (filter === "pending" && choice !== null || filter === "merge" && choice !== "merge"
        || filter === "keep" && (choice !== "keep" || row.status === "archive")) return false;
    const needle = query.trim().toLocaleLowerCase();
    return !needle || [row.label, row.field, row.oldText, row.currentText, row.note, row.source_key || ""]
      .join(" ").toLocaleLowerCase().includes(needle);
  };
  function choose(row, value) {
    if (row.selection_id) {
      for (const linked of rows.filter(item => item.selection_id && item.selection_unit === row.selection_unit)) {
        decisions.set(linked.id, value);
      }
    } else if (row.status === "manual" && value === "keep") decisions.set(row.id, "keep");
  }
  function click(data, locked = false) {
    if (locked) return;
    const action = data.migrationConfigAction;
    if (action === "filter") { filter = data.filter; page = 1; }
    if (action === "reset") decisions.clear();
    if (action === "page") page += Number(data.direction) || 0;
    if (action === "clear") { query = ""; page = 1; }
    if (action === "choose") {
      const row = rows.find(item => item.id === data.row);
      if (row && ["merge", "keep"].includes(data.choice)) choose(row, data.choice);
    }
    if (action === "bulk") {
      // Required confirmations remain explicit, including other rows in their connection bundle.
      const protectedUnits = new Set(rows.filter(row => ["confirm", "manual"].includes(row.status))
        .map(row => row.selection_unit));
      for (const row of rows.filter(row => row.group === data.group && matches(row)
          && row.selection_id && !protectedUnits.has(row.selection_unit))) choose(row, data.choice);
    }
  }
  function badge(row) {
    const choice = decision(row);
    const outcomeLabels = {unsupported: "暂不支持", mechanism: "新版机制", unknown: "待核实", inapplicable: "不适用"};
    const [label, tone] = outcomeLabels[row.outcome] ? [outcomeLabels[row.outcome], "muted"]
      : choice === null ? ["需确认", "pending"] : row.status === "archive" ? ["仅保留原件", "muted"]
      : row.status === "same" ? ["无变化", "muted"] : row.status === "manual" ? ["稍后设置", "muted"]
        : choice === "merge" ? ["可沿用", "ready"] : ["保留当前", "muted"];
    return `<span class="migration-config-badge ${tone}">${label}</span>`;
  }
  function controls(row, locked) {
    const disabled = locked ? "disabled" : "";
    if (!row.selection_id) return row.status === "manual"
      ? `<div class="migration-config-manual">${window.CompanionMigrationConfigReview?.editor(row.editor) ? `<button type="button" class="migration-config-mini" data-setup-legacy-settings="${esc(row.editor)}" ${disabled}>去设置</button>` : ""}<button type="button" class="migration-config-mini" data-migration-config-action="choose" data-row="${esc(row.id)}" data-choice="keep" ${disabled}>${decisions.has(row.id) ? "已选稍后设置" : "稍后设置"}</button></div>`
      : `<span class="migration-config-locked">${row.outcome ? "无需操作" : row.status === "archive" ? "不写入运行配置" : "保持当前"}</span>`;
    return `<div class="migration-config-segment" role="group" aria-label="${esc(row.label)}处理方式">${[["merge", "沿用旧版"], ["keep", "保留当前"]].map(([value, label]) =>
      `<button type="button" data-migration-config-action="choose" data-row="${esc(row.id)}" data-choice="${value}" aria-pressed="${decision(row) === value}" ${disabled}>${label}</button>`).join("")}</div>`;
  }
  function render({locked = false} = {}) {
    const count = stats();
    const filtered = GROUPS.flatMap(group => rows.filter(row => row.group === group.id && matches(row)));
    const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
    page = Math.min(pages, Math.max(1, page));
    const visible = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
    const groupRows = GROUPS.map(group => {
      const items = visible.filter(row => row.group === group.id);
      if (!items.length) return "";
      const hasChoices = items.some(row => row.selection_id && row.status !== "confirm");
      return `<tr class="migration-config-group"><th colspan="4" scope="colgroup"><div><strong>${group.title}</strong><small>${group.note}</small>${hasChoices ? `<span>${[["merge", "全部沿用旧版"], ["keep", "全部保留当前"]].map(([choice, label]) => `<button type="button" class="migration-config-mini" data-migration-config-action="bulk" data-group="${group.id}" data-choice="${choice}" ${locked ? "disabled" : ""}>${label}</button>`).join("")}</span>` : ""}</div></th></tr>`
        + items.map(row => `<tr><th scope="row" class="migration-config-field"><strong>${esc(row.label)}</strong>${row.key ? `<code class="migration-config-key">${esc(row.key)}</code>` : ""}${row.help ? `<small>${esc(row.help)}</small>` : ""}${row.note ? `<small>${esc(row.note)}</small>` : ""}</th>
          <td data-label="旧版 → 当前"><div class="migration-config-diff"><span class="old">${esc(row.oldText)}</span><span class="arrow" aria-label="到">→</span><strong>${esc(row.currentText)}</strong></div></td>
          <td data-label="状态">${badge(row)}</td><td data-label="处理方式">${controls(row, locked)}</td></tr>`).join("");
    }).join("");
    return `<div class="migration-config-review"><header class="migration-config-heading"><h3>配置对照</h3><span>已检查 ${count.total} 项</span></header>
      <div class="migration-config-toolbar"><nav aria-label="筛选配置">${[["all", "全部", count.total], ["pending", "需确认", count.pending], ["merge", "可沿用", count.merge], ["keep", "保持当前", count.keep]].map(([id, label, number]) =>
        `<button type="button" data-migration-config-action="filter" data-filter="${id}" aria-pressed="${filter === id}">${label} ${number}</button>`).join("")}</nav>
        <div class="migration-config-tools"><label class="migration-config-search"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/></svg><input type="search" data-migration-config-search value="${esc(query)}" placeholder="字段、旧值或当前值" aria-label="搜索配置字段"></label><button type="button" class="migration-config-reset" data-migration-config-action="reset" ${locked ? "disabled" : ""}>恢复推荐选择</button></div>
      </div>
      ${visible.length ? `<div class="migration-config-table-wrap" tabindex="0" aria-label="配置对照"><table class="migration-config-table"><colgroup><col class="field"><col class="values"><col class="status"><col class="actions"></colgroup><thead><tr><th scope="col">字段</th><th scope="col">旧版 → 当前</th><th scope="col">状态</th><th scope="col">处理方式</th></tr></thead><tbody>${groupRows}</tbody></table></div>` : `<div class="migration-config-empty">没有匹配的配置，试试其他关键词或筛选。</div>`}
      ${pages > 1 ? `<nav class="migration-config-pagination" aria-label="配置对照分页"><span>共 ${filtered.length} 项 · 第 ${page} / ${pages} 页</span><button type="button" class="migration-config-mini" data-migration-config-action="page" data-direction="-1" ${page === 1 ? "disabled" : ""}>上一页</button><button type="button" class="migration-config-mini" data-migration-config-action="page" data-direction="1" ${page === pages ? "disabled" : ""}>下一页</button></nav>` : ""}
      <div class="migration-config-summary" role="status"><span>将沿用 <strong>${count.merge}</strong> 项 · 保持 <strong>${count.keep}</strong> 项 · 仅保留原件 <strong>${count.archive}</strong> 项</span><span class="${count.pending ? "pending" : "ready"}">${count.pending ? `还有 ${count.pending} 项需要逐项确认` : "配置选择已就绪，可以继续"}</span></div>
      <p class="migration-config-hint">密钥只显示是否已配置；关联的连接参数会一同沿用或保留。${count.deferred ? ` ${count.deferred} 项需手动设置，可稍后从迁移结果进入。` : ""}</p></div>`;
  }
  return {update, supported, stats, selection, render, click,
    rows: () => rows.map(row => ({...row})),
    /* “沿用旧版配置”是整批选择：包含需要确认的行，由一次明确的二选一确认。 */
    allSelection: () => [...new Set(rows.filter(row => row.selection_id).map(row => row.selection_id))],
    search(value) { query = String(value || ""); page = 1; },
    reset() { plan = null; rows = []; decisions.clear(); filter = "all"; query = ""; page = 1; }};
})();
