/* bootstrap —— 全部**模块级事件绑定**（原 app.js 末尾的那些）
 *
 * 🔴 为什么单独一个文件：这些东西是**顶层立即执行**的（`$("x").onclick = ...`），
 *    而各视图模块之间存在**双向循环依赖**（core 调 views，views 调 core）。
 *    把副作用留在视图模块里，模块图初始化时就会撞上
 *    `ReferenceError: Cannot access '$' before initialization`（实测踩到）。
 *    ⇒ 视图模块只放纯声明，副作用统一收在这里，由 main.js 在最后导入。
 *
 * ⚠️ 执行时机：DOM 已经解析完（脚本是 `type="module"`，**天然 defer**）。
 */

import { send } from "./views/chat.js";
import { $, MODE_HINTS, api, applyRoute, boot, closeDrawer, go, hideLogin, isMobile, recheckLogin, showLogin, state, switchToTab, toast, toggleSidebar } from "./core/core.js";
import { downloadExport, loadPersonas, refreshPersonaInState, renderPersonaList, renderSections, selectPersona } from "./views/persona.js";
import { NAV_PLUGIN_OPEN_KEY, collectPluginConfigChanges, openPluginConfig, savePluginSwitch, syncNavPluginGroupOpen } from "./views/plugins.js";
import { PROVIDER_FILTER_ALL, addHeaderRow, addProviderModel, clearProviderDetail, closeModelsModal, closePresetPicker, closeTestModal, deleteProviderSource, fetchSourceModels, newProviderSourceFlow, removeProviderModel, renderAvailableModels, saveProviderSource, setAdapterType, setProviderFilter, showModal, testProviderModel } from "./views/providers.js";
import { currentUmo, deleteSessionById, fillSessionForm, loadSessions, refreshSessionInfo, renderSessionList, renderSessionPicker, renderSessionSelect, saveSessionConfig, selectSession, showHint } from "./views/session.js";
import { copyLogs, loadLogs, resetConfigProfile, saveConfigSection, stopLogsAutoRefresh } from "./views/settings.js";

window.addEventListener("hashchange", () => { applyRoute(); });

$("sidebar-toggle").onclick = toggleSidebar;

$("sidebar-scrim").onclick = closeDrawer;

if ($("mobile-menu")) $("mobile-menu").onclick = toggleSidebar;

window.addEventListener("resize", () => {
  if (!isMobile()) {
    document.querySelector(".app").classList.remove("drawer-open");
    $("sidebar-scrim").classList.add("hidden");
  }
});

window.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (document.querySelector(".app").classList.contains("drawer-open")) { closeDrawer(); return; }
  // 下面这些同一时刻通常只开一个，但都要在表里（顺序不影响正确性，仍按"后开先关"排）
  if (!$("models-modal").classList.contains("hidden")) { closeModelsModal(); return; }
  if (!$("test-modal").classList.contains("hidden")) { closeTestModal(); return; }
  if (!$("preset-modal").classList.contains("hidden")) { closePresetPicker(); return; }
  if (!$("import-modal").classList.contains("hidden")) { $("import-modal").classList.add("hidden"); return; }
  if (!$("modal").classList.contains("hidden")) { $("modal").classList.add("hidden"); }
});

$("send").onclick = send;

$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});

$("p-ref-file").onchange = () => {
  const f = $("p-ref-file").files && $("p-ref-file").files[0];
  $("p-ref-upload").disabled = !f;
  if (f) $("p-ref-hint").textContent = `待上传：${f.name}（${Math.round(f.size / 1024)} KB）`;
};

$("p-ref-upload").onclick = async () => {
  if (!state.current) return;
  const f = $("p-ref-file").files && $("p-ref-file").files[0];
  // ⚠️ 没选文件就**明确说**，不静默 return（本项目铁律：失败必须看得见）
  if (!f) { toast($("p-ref-hint"), "请先选择一张图片", true); return; }
  const fd = new FormData();
  fd.append("file", f);
  $("p-ref-upload").disabled = true;
  try {
    // ⚠️ 上传要走 `fetch` + FormData（不能用 `api()` —— 那个只发 JSON）
    const r = await fetch(`/api/v1/personas/${encodeURIComponent(state.current.id)}/reference`, {
      method: "POST", body: fd, credentials: "same-origin",
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((d.error && d.error.message) || `HTTP ${r.status}`);
    $("p-ref-file").value = "";
    $("p-ref-upload").disabled = true;
    // 🔴 成功后**重新渲染持久状态**，不要往这个元素里塞"已上传" ——
    //    `#p-ref-hint` 的**唯一含义**是"当前配置是什么"。
    //    塞瞬时文案会把它盖掉，用户传完反而看不到"现在用的是哪张图"
    //    （实测踩到：真浏览器判据里 hint 变成 '已上传'，
    //     「已设置：xxx.png」消失 —— 判据抓的就是这个）。
    //    反馈由**预览图出现**给出，那比一行字更直接。
    refreshPersonaInState(d.persona);
  } catch (e) {
    $("p-ref-upload").disabled = false;
    // 失败**必须留下**（错误信息是有用的，不还原成状态文案）
    toast($("p-ref-hint"), "上传失败：" + e.message, true);
  }
};

$("p-ref-delete").onclick = async () => {
  if (!state.current) return;
  if (!confirm("删除这个角色的参考图？（生图将不再固定长相）")) return;
  try {
    const d = await api("DELETE", `/api/v1/personas/${state.current.id}/reference`);
    // 同上：重新渲染持久状态，而不是塞"已删除"
    refreshPersonaInState(d.persona);
    if (!d.file_removed) {
      // ⚠️ 字段清了但文件本就不在 —— 这种情况要**说一句**，
      //    否则用户会以为"我明明删了它怎么没反应"（其实是对的，只是没什么可删）
      toast($("p-ref-hint"), "已清除配置（参考图文件本就不存在）");
    }
  } catch (e) { toast($("p-ref-hint"), "删除失败：" + e.message, true); }
};

$("add-section").onclick = () => {
  state.sections.push({ title: "新片段", content: "", enabled: true, order: 100, source: "user", condition: "" });
  renderSections();
};

$("save-sections").onclick = async () => {
  if (!state.current) return;
  try {
    const p = await api("PUT", `/api/v1/personas/${state.current.id}/sections`, state.sections);
    Object.assign(state.current, p);
    toast($("chat-hint"), "片段已保存");
    loadPersonas();
  } catch (e) { toast($("chat-hint"), "保存失败：" + e.message, true); }
};

$("new-persona").onclick = async () => {
  const name = prompt("新角色卡的名字？", "新角色");
  if (!name) return;
  try {
    const p = await api("POST", "/api/v1/personas", { name });
    state.personas.unshift(p);
    selectPersona(p.id);   // 新建后直接进详情（会写 hash）
    renderPersonaList();
  } catch (e) { toast($("chat-hint"), "创建失败：" + e.message, true); }
};

$("persona-back").onclick = () => {
  $("tab-persona").classList.remove("is-detail");
  state.current = null;
  go("persona");
};

$("persona-form").onsubmit = async (e) => {
  e.preventDefault();
  if (!state.current) return;
  const patch = {
    name: $("p-name").value,
    description: $("p-description").value,
    personality: $("p-personality").value,
    scenario: $("p-scenario").value,
    creator_notes: $("p-creator-notes").value,
    api_url: $("p-api-url").value,
    model: $("p-model").value,
  };
  // 只在真填了 key 时才提交 —— 留空表示"不改"
  const k = $("p-api-key").value;
  if (k) patch.api_key = k;

  try {
    const p = await api("PATCH", `/api/v1/personas/${state.current.id}`, patch);
    state.current = p;
    const i = state.personas.findIndex((x) => x.id === p.id);
    if (i >= 0) state.personas[i] = p;
    renderPersonaList();
    $("p-api-key").value = "";
    $("p-api-key").placeholder = p.api_key_configured ? "已设置（留空 = 不变）" : "留空 = 不变";
    toast($("chat-hint"), "已保存");
  } catch (e2) { toast($("chat-hint"), "保存失败：" + e2.message, true); }
};

$("activate").onclick = async () => {
  if (!state.current) return;
  try {
    await api("POST", `/api/v1/personas/${state.current.id}/activate`);
    await loadPersonas();
  } catch (e) { toast($("chat-hint"), "设置失败：" + e.message, true); }
};

$("dup").onclick = async () => {
  if (!state.current) return;
  try {
    await api("POST", `/api/v1/personas/${state.current.id}/duplicate`, {});
    await loadPersonas();
  } catch (e) { toast($("chat-hint"), "复制失败：" + e.message, true); }
};

$("import-persona").onclick = () => {
  $("import-result").classList.add("hidden");
  $("import-hint").textContent = "";
  $("import-hint").style.color = "";
  $("import-modal").classList.remove("hidden");
};

$("import-close").onclick = () => $("import-modal").classList.add("hidden");

$("import-modal").onclick = (e) => {
  if (e.target.id === "import-modal") $("import-modal").classList.add("hidden");
};

$("import-form").onsubmit = async (e) => {
  e.preventDefault();
  const hint = $("import-hint");
  const file = $("import-file").files[0];
  const text = $("import-text").value.trim();
  const conflict = $("import-conflict").value;

  if (!file && !text) {
    hint.textContent = "请选一个文件，或粘贴内容";
    hint.style.color = "var(--danger)";
    return;
  }

  toast(hint, "导入中…");
  try {
    let r;
    if (file) {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("conflict", conflict);
      r = await fetch("/api/v1/personas/import", { method: "POST", body: fd });
    } else {
      r = await fetch("/api/v1/personas/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, conflict }),
      });
    }

    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      hint.textContent = d?.error?.message || `HTTP ${r.status}`;
      hint.style.color = "var(--danger)";
      return;
    }

    // 组装结果摘要 —— 逐条列出**导入了什么**，不让用户猜
    const lines = [`✅ 导入成功：${d.count} 个人设（格式 ${d.source_format}）`];
    for (const p of d.personas || []) lines.push(`   · ${p.name}`);
    if (d.overwritten?.length) lines.push(`♻️ 已覆盖：${d.overwritten.join("、")}`);
    if (d.skipped?.length) lines.push(`⏭️ 已跳过（同名）：${d.skipped.join("、")}`);
    if (d.has_avatar) lines.push("🖼️ 已提取头像");
    for (const n of d.notes || []) lines.push(`ℹ️ ${n}`);
    if (d.extra_fields?.length) {
      lines.push(`ℹ️ 未导入的字段：${d.extra_fields.join("、")}`);
    }
    $("import-result").textContent = lines.join("\n");
    $("import-result").classList.remove("hidden");
    toast(hint, `已导入 ${d.count} 个`, false);

    $("import-file").value = "";
    $("import-text").value = "";
    await loadPersonas();
  } catch (err) {
    hint.textContent = "导入失败：" + err.message;
    hint.style.color = "var(--danger)";
  }
};

$("export-persona").onclick = async () => {
  if (!state.current) return;
  const fmt = confirm(
    "导出格式：\n\n" +
    "「确定」= 爱语备份格式（给爱语用 / 做备份）\n" +
    "「取消」= SillyTavern 角色卡（与角色卡生态互通）"
  ) ? "aiyu" : "sillytavern";
  try {
    await downloadExport(
      `/api/v1/personas/${state.current.id}/export?format=${fmt}`,
      `${state.current.name}.json`
    );
    toast($("chat-hint"), "已导出");
  } catch (e) { toast($("chat-hint"), "导出失败：" + e.message, true); }
};

$("del").onclick = async () => {
  if (!state.current) return;
  if (!confirm(`删除「${state.current.name}」？不可恢复。`)) return;
  try {
    await api("DELETE", `/api/v1/personas/${state.current.id}`);
    state.current = null;
    $("persona-form").classList.add("hidden");
    $("sections-block").classList.add("hidden");
    $("persona-empty").classList.remove("hidden");
    await loadPersonas();
  } catch (e) { toast($("chat-hint"), "删除失败：" + e.message, true); }
};

$("preview").onclick = async () => {
  if (!state.current) return;
  try {
    const d = await api("GET", `/api/v1/personas/${state.current.id}/preview`);
    showModal(`提示词预览 · ${state.current.name}（${d.describe.total_chars} 字符 / ${d.describe.section_count} 段）`, d.text);
  } catch (e) { toast($("chat-hint"), "预览失败：" + e.message, true); }
};

$("session-select").onchange = (e) => selectSession(e.target.value);

$("new-session").onclick = $("new-session-2").onclick = async () => {
  try {
    const s = await api("POST", "/api/v1/sessions", {});
    state.sessions.unshift(s);
    state.currentSessionId = s.id;
    state.currentSession = s;
    $("messages").innerHTML = "";
    renderSessionList();
    renderSessionSelect();
    renderSessionPicker();
    refreshSessionInfo();
    // 🔴 「会话」页那个新建按钮 → 建完**直接进聊天**（用户在这一页的意图就是开聊）。
    //    设置页那个 → 留在原地（他是去配的，弹走反而打断）。
    if (document.activeElement && document.activeElement.id === "new-session-2") {
      switchToTab("chat");
    } else {
      fillSessionForm(s);
      showHint($("cfg-session-hint"), "已新建会话，去上面那条「对话模型」给它选个模型");
    }
  } catch (e) {
    showHint($("session-hint"), "创建失败：" + e.message, true);
    showHint($("cfg-session-hint"), "创建失败：" + e.message, true);
    showHint($("session-page-hint"), "创建失败：" + e.message, true);
  }
};

$("clear-session").onclick = async () => {
  // 清空消息在**聊天页**上（`#clear-session` 就在 `#session-select` 旁边），
  // 所以反馈落在同一条工具栏上的 `#chat-hint`，而不是会话页的 `#session-hint`。
  if (!state.currentSessionId) return toast($("chat-hint"), "当前没有会话", true);
  if (!confirm("清空这个会话的消息？（会话设置会保留）")) return;
  try {
    await api("POST", `/api/v1/sessions/${state.currentSessionId}/clear`);
    $("messages").innerHTML = "";
    await loadSessions();
  } catch (e) { toast($("chat-hint"), "失败：" + e.message, true); }
};

if ($("s-mode")) {
  $("s-mode").onchange = () => {
    const h = $("mode-hint");
    if (h) h.textContent = MODE_HINTS[$("s-mode").value] || "";
  };
}

if ($("s-del")) {
  $("s-del").onclick = () => {
    if (state.currentSession) deleteSessionById(state.currentSession);
  };
}

$("s-umo-copy") && ($("s-umo-copy").onclick = async () => {
  const umo = currentUmo();
  if (!umo) return toast($("s-umo-hint"), "这个会话还没有 umo", true);
  try {
    await navigator.clipboard.writeText(umo);
    toast($("s-umo-hint"), `已复制：${umo}`);
  } catch (e) {
    // 剪贴板 API 在非 https / 未聚焦时会拒绝 —— 如实提示，
    // 并把值显示出来让用户手动选（别只报一句"失败"）。
    toast($("s-umo-hint"), `复制失败，请手动选：${umo}`, true);
  }
});

$("s-umo-new") && ($("s-umo-new").onclick = async () => {
  if (!state.currentSession) return;
  const before = currentUmo();
  if (!confirm(
    "换一个专用 umo？\n\n" +
    "陪伴插件按这个值认人：换掉之后，这个会话在插件眼里就是**另一个用户**。\n" +
    "旧标签下的记忆/关系不会丢（还挂在旧标签下），但之后写的东西都记到新标签下。\n\n" +
    `当前：${before}`
  )) return;
  try {
    const d = await api("POST", `/api/v1/sessions/${state.currentSession.id}/umo`);
    state.currentSession = d.session;
    const i = state.sessions.findIndex((x) => x.id === d.session.id);
    if (i >= 0) state.sessions[i] = d.session;
    $("s-user-id").value = d.session.user_id ?? "";
    renderSessionList();
    renderSessionSelect();
    toast($("s-umo-hint"), `已换成 ${d.session.umo}`);
  } catch (e) { toast($("s-umo-hint"), "换 umo 失败：" + e.message, true); }
});

$("cfg-session-form").onsubmit = (e) => { e.preventDefault(); saveConfigSection("session"); };

$("cfg-reset").onclick = () => resetConfigProfile();

$("logs-refresh").onclick = () => loadLogs({});

$("logs-copy").onclick = () => copyLogs();

$("logs-level").onchange = () => loadLogs({});

$("logs-limit").onchange = () => loadLogs({});

$("logs-auto").onchange = () => {
  if ($("logs-auto").checked) loadLogs({ startAuto: true });
  else stopLogsAutoRefresh();
};

$("provider-source-list").onclick = (ev) => {
  const btn = ev.target.closest("button[data-del]");
  if (!btn) return;
  deleteProviderSource(btn.dataset.del, btn);
};

$("ps-configured-list").onclick = (ev) => {
  const btn = ev.target.closest("button[data-op]");
  if (!btn) return;
  if (btn.dataset.op === "test") testProviderModel(btn.dataset.id, btn);
  else if (btn.dataset.op === "del") removeProviderModel(btn.dataset.id);
};

$("provider-source-new").onclick = () => newProviderSourceFlow();

$("provider-source-back").onclick = () => {
  clearProviderDetail();
  go("providers");
};

$("provider-save-config").onclick = () => saveProviderSource();

$("provider-delete").onclick = () => deleteProviderSource(state.selectedSourceId, $("provider-delete"));

$("ps-fetch-models").onclick = () => fetchSourceModels();

$("ps-header-add").onclick = () => addHeaderRow("", "");

$("ps-model-search").oninput = () => renderAvailableModels();

$("provider-type-filter").onclick = (ev) => {
  const b = ev.target.closest(".filter-btn");
  if (!b) return;
  setProviderFilter(b.dataset.ptype || PROVIDER_FILTER_ALL);
};

$("ps-type").onchange = () => setAdapterType($("ps-type").value);

$("preset-close").onclick = () => closePresetPicker();

$("preset-modal").onclick = (e) => {
  if (e.target.id === "preset-modal") closePresetPicker();
};

$("preset-skip").onclick = () => closePresetPicker();

$("models-close").onclick = () => closeModelsModal();

$("models-modal").onclick = (e) => {
  if (e.target.id === "models-modal") closeModelsModal();
};

$("test-close").onclick = () => closeTestModal();

$("test-modal").onclick = (e) => {
  if (e.target.id === "test-modal") closeTestModal();
};

$("ps-add-custom").onclick = () => {
  const wrap = $("ps-custom-wrap");
  wrap.classList.toggle("hidden");
  if (!wrap.classList.contains("hidden")) $("ps-custom-model").focus();
};

$("ps-custom-add").onclick = async () => {
  const name = ($("ps-custom-model").value || "").trim();
  const hint = $("ps-available-hint");
  if (!name) { toast(hint, "填一个模型 ID，例如 gpt-5.6-sol。", true); return; }
  await addProviderModel(name);
  $("ps-custom-model").value = "";
};

$("ps-custom-model").onkeydown = (ev) => {
  if (ev.key === "Enter") { ev.preventDefault(); $("ps-custom-add").click(); }
};

$("provider-source-form").onsubmit = (ev) => {
  ev.preventDefault();
  saveProviderSource();
};

$("modal-close").onclick = () => $("modal").classList.add("hidden");

$("modal").onclick = (e) => { if (e.target.id === "modal") $("modal").classList.add("hidden"); };

$("plugin-switch-save").onclick = savePluginSwitch;

$("plugin-frame-back").onclick = () => go("plugins");

$("plugin-detail-back").onclick = () => go("plugins");

$("plugin-config-back").onclick = () => go("plugins");

$("plugin-config-close").onclick = () => go("plugins");

$("plugin-config-save").onclick = async () => {
  const id = state.pluginCurrentId;
  const hint = $("plugin-config-hint");
  const btn = $("plugin-config-save");
  if (!id) return;
  const changes = collectPluginConfigChanges();
  if (!Object.keys(changes).length) {
    toast(hint, "没有改动，无需保存。");
    return;
  }
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "保存中…";
  try {
    const r = await api("PUT", `/api/v1/plugins/${encodeURIComponent(id)}/config`, { config: changes });
    // ⚠️ 后端的文案里有"需要重启服务才生效"——**原样透出**，
    //    不在这里简化成"已保存"（那会掩盖生效时机）
    // ⚠️ 顺序要紧：**先重新拉数据**（界面上的值要等于服务端的真实值），
    //    再写提示 —— 反过来的话那句提示会被重拉过程清掉（见 openPluginConfig）。
    await openPluginConfig(id, { keepHint: true });
    toast(hint, (r && r.message) || "已保存");
  } catch (e) {
    toast(hint, `保存失败：${e.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
};

$("login-form").onsubmit = async (e) => {
  e.preventDefault();
  const token = $("login-token").value.trim();
  if (!token) return;
  try {
    const r = await fetch("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      showLogin("登录失败：" + (d?.error?.message || `HTTP ${r.status}`));
      return;
    }
    $("login-token").value = "";   // 用完即弃，不留在 DOM 里
    hideLogin();
    boot();
  } catch (err) {
    showLogin("登录失败：" + err.message);
  }
};

setInterval(recheckLogin, 30000);

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) recheckLogin();
});

window.addEventListener("focus", recheckLogin);
export const _cfgSessionSaveBtn = $("cfg-session-save");
if (_cfgSessionSaveBtn) {
  // 🔴 先摘掉外层 form 的 submit 行为：按钮在 form 里，默认 type=submit
  //    会**同时**触发全局配置保存 —— 用户点"保存会话"却改了全局设置。
  _cfgSessionSaveBtn.type = "button";
  _cfgSessionSaveBtn.onclick = (e) => {
    e.preventDefault();
    saveSessionConfig();
  };
}

export const _cfgSessionDel = $("cfg-session-del");
if (_cfgSessionDel) {
  _cfgSessionDel.onclick = () => {
    const sid = $("cfg-session-picker") ? $("cfg-session-picker").value : "";
    const s = state.sessions.find((x) => x.id === sid) || state.currentSession;
    if (!s) {
      showHint($("cfg-session-hint"), "没有可删除的会话", true);
      return;
    }
    deleteSessionById(s);
  };
}

export const _cfgSessionPicker = $("cfg-session-picker");
if (_cfgSessionPicker) {
  _cfgSessionPicker.onchange = () => {
    const s = state.sessions.find((x) => x.id === _cfgSessionPicker.value);
    if (!s) return;
    // ⚠️ 这里**不调 `selectSession`** —— 那会把"聊天页当前会话"也换掉，
    //    而用户此刻只是在**改配置**，没说要换聊天对象。
    state.currentSession = s;
    fillSessionForm(s);
    showHint($("cfg-session-model-hint"), `正在配置：${s.title || "(未命名)"}`);
  };
}

export const _cfgSessionNew = $("cfg-session-new");
if (_cfgSessionNew) {
  _cfgSessionNew.onclick = () => {
    const btn = $("new-session");
    if (btn) btn.onclick ? btn.onclick() : btn.click();
  };
}

/* ── 侧边栏「插件页面」分组折叠（原 plugins.js 顶层）──────────────
 * ⚠️ 惰性取值：模块顶层调 `$()` 会在循环依赖初始化时拿到未定义。 */
function _navPluginToggle() { return $("nav-plugin-toggle"); }
if (_navPluginToggle()) {
  _navPluginToggle().onclick = (ev) => {
    ev.preventDefault();
    const open = localStorage.getItem(NAV_PLUGIN_OPEN_KEY) === "1";
    localStorage.setItem(NAV_PLUGIN_OPEN_KEY, open ? "0" : "1");
    syncNavPluginGroupOpen();
  };
}
