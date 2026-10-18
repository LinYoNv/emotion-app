/* providers —— 由原单文件 app.js 拆分（**原生 ES 模块，零构建**）
 *
 * 🔴 本文件只放**函数/常量声明**，**没有任何模块级副作用**
 *    （事件绑定统一在 `js/bootstrap.js`）。
 *    这是为了消除循环依赖在"模块顶层立即求值"时拿到未初始化绑定的风险。
 */

import { $, NAV_LOADERS, api, apiData, applyRoute, asArray, explainProviderError, go, state, toast } from "../core/core.js";


/** 一个源归到哪个类型（缺失视为对话 —— 与 `isChatSource` 同一条口径）。 */
export function sourceProviderType(s) {
  return (s && s.provider_type) || "chat_completion";
}

/** 一个源是不是「对话」类。缺失 provider_type 视为对话（迁移过来的老数据没有这个字段）。
 *
 * ⚠️ 现在**页面逻辑已经不直接用它**了（筛选走 `visibleProviderSources()`，
 *    归属走 `sourceProviderType()`）—— 但它是"缺失 = 对话"这条口径的**命名定义**，
 *    也是别的代码/判据可能引用的稳定入口，**留着**（删了以后有人会再写一遍
 *    略有出入的判断，那才会真出问题）。
 */
export function isChatSource(s) {
  return sourceProviderType(s) === "chat_completion";
}

/* ---------------- 类型筛选与徽标（契约 §5.1） ----------------
 *
 * 四类与后端 `SUPPORTED_PROVIDER_TYPES` 一一对应（契约 §1）：
 *   chat_completion → 对话 / text_to_speech → 语音合成 /
 *   speech_to_text  → 语音识别 / embedding → 嵌入
 *
 * 🔴 为什么用一张表而不是散落的 if：
 *    "筛选值 → 中文名" 这个映射要同时被**按钮**、**hint 文案**、**列表徽标**
 *    三处用到；散着写迟早会出现"按钮写语音、徽标写 TTS"这种对不上的情况。
 *    ⚠️ 这张表必须与后端 `SUPPORTED_PROVIDER_TYPES` **同增同减** ——
 *       后端加了类型而这里没加 → 该类源会落进"其他"、用户筛不到（静默不可见）。
 *
 * ⚠️ `speech_to_text`（2026-09-21 加入，情绪选"先只做配置层"）：
 *    **存得进、筛得到**，但 3 条 STT 预设的适配器我们**一个都执行不了** ——
 *    由后端 `executable_by_us` 如实标 false，前端照实显示"本平台暂不能执行"。
 *    别因为"筛得到"就以为"跑得了"。
 */
export const PROVIDER_TYPE_LABELS = {
  chat_completion: "对话",
  text_to_speech: "语音合成",
  speech_to_text: "语音识别",
  embedding: "嵌入",
};

/** 筛选控件里用的取值：`*` = 全部（不筛）。 */
export const PROVIDER_FILTER_ALL = "*";

/** 类型的展示名。**未知类型不假装认识**：原样回显后端给的字符串。 */
export function providerTypeLabel(t) {
  return PROVIDER_TYPE_LABELS[t] || t || "未知";
}

/** 当前筛选下**应该显示**的那些源。
 *
 * 🔴 这是"筛选"的唯一实现，也是 hint 里那个 N 的**唯一来源** ——
 *    两边各算一次就会出现"hint 说 3 个、列表画了 2 个"的谎话
 *    （本项目铁律：绝不让界面上的数字和实际渲染不一致）。
 */
export function visibleProviderSources() {
  const all = state.providerSources || [];
  const f = state.providerFilter || PROVIDER_FILTER_ALL;
  if (f === PROVIDER_FILTER_ALL) return all;
  return all.filter((s) => sourceProviderType(s) === f);
}

/** 高亮筛选按钮（`aria-pressed` 一起切，键盘/读屏用户才知道当前在哪一类）。 */
export function syncProviderFilterButtons() {
  const cur = state.providerFilter || PROVIDER_FILTER_ALL;
  document.querySelectorAll("#provider-type-filter .filter-btn").forEach((b) => {
    const on = b.dataset.ptype === cur;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

/** 切换筛选。**只重画列表 + hint**，不动右侧已选中的源
 *  （否则用户刚填了一半的表单会因为"想看一眼语音类"被清掉）。 */
export function setProviderFilter(t) {
  state.providerFilter = t;
  renderProviderSourceList();
  updateProviderSourcesHint();
}

/* ---------------- 自定义请求头（对象 ⇄ 行） ---------------- */

export function headersToRows(ch) {
  const out = [];
  if (ch && typeof ch === "object") {
    for (const k of Object.keys(ch)) out.push({ k, v: String(ch[k] ?? "") });
  }
  return out;
}

export function headersFromRows() {
  const out = {};
  $("ps-header-rows").querySelectorAll(".header-row").forEach((row) => {
    const k = row.querySelector(".h-key").value.trim();
    const v = row.querySelector(".h-val").value;
    // ⚠️ 空键的行直接丢（用户点了「添加更多」又没填）；值可以是空字符串，原样保留
    if (k) out[k] = v;
  });
  return out;
}

export function addHeaderRow(k, v) {
  const row = document.createElement("div");
  row.className = "header-row";
  const ki = document.createElement("input");
  ki.className = "h-key";
  ki.placeholder = "键，例如 X-Api-Version";
  ki.value = k || "";
  const vi = document.createElement("input");
  vi.className = "h-val";
  vi.placeholder = "值（字符串）";
  vi.value = v || "";
  const del = document.createElement("button");
  del.type = "button";
  del.className = "danger";
  del.textContent = "移除";
  del.onclick = () => { row.remove(); syncHeaderEmpty(); };
  row.append(ki, vi, del);
  $("ps-header-rows").appendChild(row);
  syncHeaderEmpty();
}

export function syncHeaderEmpty() {
  const n = $("ps-header-rows").querySelectorAll(".header-row").length;
  $("ps-header-empty").classList.toggle("hidden", n > 0);
}

export function renderHeaderRows(rows) {
  $("ps-header-rows").innerHTML = "";
  for (const r of rows) addHeaderRow(r.k, r.v);
  syncHeaderEmpty();
}

/* ---------------- 左侧：提供商源列表 ---------------- */

export function renderProviderSourceList() {
  const ul = $("provider-source-list");
  const empty = $("provider-source-empty");
  const all = state.providerSources || [];
  const list = visibleProviderSources();
  state.hiddenSourceCount = all.length - list.length;
  syncProviderFilterButtons();

  ul.innerHTML = "";
  if (!list.length) {
    empty.classList.remove("hidden");
    // 空态要分清两种"空"：**这一类里没有** vs **一个源都没有**（说的是两件事）
    if (!all.length) {
      empty.textContent = "还没有提供商源，点上面的「+ 新增」建一个。";
    } else if (state.providerFilter === PROVIDER_FILTER_ALL) {
      empty.textContent = "还没有提供商源，点上面的「+ 新增」建一个。";
    } else {
      empty.textContent = `没有「${providerTypeLabel(state.providerFilter)}」类的提供商源（切到「全部」可以看到其余 ${all.length} 个）。`;
    }
  } else {
    empty.classList.add("hidden");
  }

  for (const s of list) {
    const li = document.createElement("li");
    li.dataset.sourceId = s.id;
    li.classList.toggle("active", s.id === state.selectedSourceId);
    // 键盘可达（列表项也是可点的"按钮"）
    li.tabIndex = 0;
    li.setAttribute("role", "button");

    const text = document.createElement("div");
    text.className = "src-text";
    const idEl = document.createElement("div");
    idEl.className = "src-id";
    idEl.textContent = s.id;                       // ← 第一行：id
    // 类型徽标：**只看 provider_type，不看当前筛选**。
    //   "全部"视图里必须能一眼分出哪个是语音/嵌入（否则一排 id 长得一模一样）；
    //   相反，在「对话」筛选里给每条都挂个「对话」是噪音 —— 所以对话类不挂。
    const pt = sourceProviderType(s);
    if (pt !== "chat_completion") {
      const tag = document.createElement("span");
      tag.className = "src-type";
      // ⚠️ `data-ptype` 存**原值**（不是展示名）：判据/其它代码要能按真实类型取值，
      //    展示名只是给人看的，两者混在一起以后一定会有人拿它当过滤条件。
      tag.dataset.ptype = pt;
      tag.textContent = providerTypeLabel(pt);
      idEl.appendChild(tag);
    }
    const baseEl = document.createElement("div");
    baseEl.className = "src-base";
    baseEl.textContent = s.api_base || "（未设置 API Base URL）";   // ← 第二行：api_base
    text.append(idEl, baseEl);

    const del = document.createElement("button");
    del.type = "button";
    del.className = "src-del";
    del.dataset.del = s.id;
    del.title = `删除 ${s.id}`;
    del.setAttribute("aria-label", `删除 ${s.id}`);
    del.textContent = "🗑";

    li.append(text, del);
    li.onclick = (ev) => {
      if (ev.target.closest("[data-del]")) return;   // 点删除不算"选中"
      selectProviderSource(s.id);
    };
    li.onkeydown = (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); selectProviderSource(s.id); }
    };
    ul.appendChild(li);
  }
}

/** 左侧列表下方那句 hint —— **必须如实描述当前筛选**（契约 §5.1）。
 *
 * 🔴 这里的 N **必须**取自 `visibleProviderSources()`（与列表渲染同一个函数），
 *    不许另算一遍：两个计数各写各的，迟早出现"hint 说 3 个、列表画了 2 个"，
 *    而用户只会觉得"这页在骗我"。
 * ⚠️ 原来那句「另有 N 个非「对话」类源未显示（本页只做对话）」的尾注
 *    **本轮已不成立**（本页不再"只做对话"，能切类型了）→ 必须改掉，
 *    留着就是一句过时的假话。
 */
export function updateProviderSourcesHint() {
  const all = state.providerSources || [];
  const n = visibleProviderSources().length;
  // 🔴 读失败时 `providerSources` 被清成了 `[]`，这里**不能**再画一遍
  //    "还没有提供商源" —— 那等于把"读取失败"伪装成"你还没建过源"，
  //    用户会去建一个重复的源。失败的消息由 `loadProviderSources` 的 catch 出，
  //    这里只负责"数据是真的"时的文案。
  //    ⚠️ 必须有这个短路：失败后用户点一下筛选按钮就会调到这里，
  //       不拦的话那句真错误会被"当前筛选：对话（共 0 个）"**当场覆盖掉**。
  if (state.sourcesError) return;
  const cur = state.providerFilter || PROVIDER_FILTER_ALL;
  let msg;
  if (cur === PROVIDER_FILTER_ALL) {
    msg = all.length ? `当前筛选：全部（共 ${n} 个）。` : "还没有提供商源，点左上「+ 新增」建一个。";
  } else {
    msg = `当前筛选：${providerTypeLabel(cur)}（共 ${n} 个）。`;
  }
  if (state.hiddenSourceCount > 0) {
    msg += `另有 ${state.hiddenSourceCount} 个其他类型的源未显示（切到「全部」可以看到）。`;
  }
  toast($("providers-hint"), msg);
}

/** 读源列表（进 `#/providers` 时由 NAV_LOADERS 调）。 */
export async function loadProviderSources() {
  try {
    const d = await apiData("GET", "/api/v1/provider-sources");
    state.providerSources = asArray(d, "provider_sources");
    state.sourcesError = "";          // 读成功 → 清掉上次的失败态
    renderProviderSourceList();
    // ⚠️ 提示统一由 `updateProviderSourcesHint()` 出 —— 成功/失败两条路都走它，
    //    这样"hint 里的数字"与"列表里画出来的条数"永远出自同一个函数（不会对不上）。
    updateProviderSourcesHint();
    return true;
  } catch (e) {
    state.providerSources = [];
    state.sourcesError = explainProviderError(e);
    renderProviderSourceList();
    // 🔴 失败**必须**压过正常文案：成功/失败两条路都写同一句 hint，
    //    谁后写谁生效 —— 所以失败这条一定放在 `updateProviderSourcesHint()` 之后，
    //    并且给 `state.sourcesError` 置位，挡住后续（如点筛选）把它覆盖回去。
    toast($("providers-hint"), `读取提供商源失败：${state.sourcesError}`, true);
    return false;
  }
}

/** 同一次路由内**只读一次**源列表（重复调用复用上一次的 promise）。
 *
 * 🔴 为什么需要它（真浏览器实测抓到的真 bug，不是洁癖）：
 *    保存成功 → `loadProviderSources()` → `selectProviderSource()` 改 hash
 *    → hashchange → `applyRoute()` → loader **又读一次** →
 *    第二次的 `updateProviderSourcesHint()` 把「已新增 xxx。」**覆盖掉**。
 *    用户因此**永远看不到保存成功的确认**（只看到 hint 变成一句筛选说明）。
 *    → 这里把同一次路由内的重复读合并掉；**路由真正切换时**由
 *      `resetProviderSourcesOnce()` 复位，所以"重新进这个页面"照常会读到新数据。
 */
export let providerSourcesOnce = null;

export function loadProviderSourcesOnce() {
  if (!providerSourcesOnce) {
    providerSourcesOnce = loadProviderSources().catch((e) => {
      // 失败也复位：下次进入该页要能真的重试（不能把失败缓存住 —— 见 presets 的同款教训）
      providerSourcesOnce = null;
      throw e;
    });
  }
  return providerSourcesOnce;
}

export function resetProviderSourcesOnce() {
  providerSourcesOnce = null;
}

/** 用户**主动**要最新数据时用这个：先把 once 缓存清掉再读，保证真的打到后端。 */
export async function loadProviderSourcesFresh() {
  resetProviderSourcesOnce();
  return loadProviderSources();
}

/* ---------------- 右侧：选中源的详情 ---------------- */

/** 回到"未选中"状态：显示空态（**不能白屏**），清掉详情里的残留。 */
export function clearProviderDetail() {
  state.selectedSourceId = "";
  state.creatingSource = false;
  state.configuredModels = [];
  state.sourceModelMetadata = {};
  state.availableModels = null;

  $("ps-configured-list").innerHTML = "";
  $("ps-configured-empty").classList.add("hidden");
  $("ps-model-count").textContent = "已配置 0";
  $("ps-available-list").classList.add("hidden");
  $("ps-available-list").innerHTML = "";
  $("ps-model-search").value = "";
  $("ps-custom-wrap").classList.add("hidden");
  toast($("ps-available-hint"), "");
  // 🔴 「获取模型列表 / 测试」的结果现在住在弹层里（情绪 2026-09-21 要求），
  //    所以"清残留"必须**连弹层一起关**：不关的话，用户切到另一个源时，
  //    上一个源的候选/测试结果还浮在屏幕上，看起来像是**这个源**的结果。
  //    （原来是 `$("providers-test-result").classList.add("hidden")` —— 那个节点
  //      已经在 `#test-modal` 里，单隐藏它等于留下一个空弹窗。）
  closeModelsModal();
  closeTestModal();
  $("provider-source-panel").classList.add("hidden");
  $("provider-source-placeholder").classList.remove("hidden");
  $("tab-providers").classList.remove("is-detail");
  renderProviderSourceList();
}

/** 点左侧某个源 → 右侧加载该源的详情 + 已配置模型。 */
export async function selectProviderSource(id, opts) {
  const src = (state.providerSources || []).find((s) => s.id === id);
  if (!src) {
    // 找不到就说清楚（比如后端还没就绪，或者 URL 里的 id 已经不存在了）
    clearProviderDetail();
    toast($("providers-hint"), `找不到提供商源「${id}」。`, true);
    return false;
  }
  state.selectedSourceId = id;
  state.creatingSource = false;
  state.sourceEnable = src.enable === false ? false : true;   // 保存时保持原值，不擅自启用

  // 「点开 → 路由到详情」：写进 hash，返回键/刷新/分享都能回到同一个源
  if (!(opts && opts.skipHash)) go("providers/" + encodeURIComponent(id));

  renderProviderSourceList();                       // 高亮跟着走
  $("tab-providers").classList.add("is-detail");    // 手机端：列表收起 + 返回按钮
  $("provider-source-placeholder").classList.add("hidden");
  $("provider-source-panel").classList.remove("hidden");
  // 换了源 → 上一个源的测试结果与候选弹层一并收掉（同 clearProviderDetail 的理由）
  closeTestModal();

  // 标题 = 源 id；副标题 = api_base（照参考站）
  $("provider-source-title").textContent = src.id;
  $("provider-source-subtitle").textContent = src.api_base || "（未设置 API Base URL）";

  // ---- 设置 ----
  $("ps-id").value = src.id || "";
  $("ps-api-base").value = src.api_base || "";
  $("ps-timeout").value = (src.timeout === undefined || src.timeout === null) ? "" : src.timeout;
  $("ps-proxy").value = src.proxy || "";
  // 🔴 **绝不回显 key**：无论后端给的是什么形状（`key_configured` 布尔、
  //    还是照 AstrBot 那样把 `key` 数组也带回来），输入框**一律清空**，
  //    只用占位符说明"服务端已有一把，留空 = 不改"。
  $("ps-api-key").value = "";
  $("ps-api-key").placeholder = src.key_configured ? "已设置（留空 = 不修改）" : "未设置";
  renderHeaderRows(headersToRows(src.custom_headers));
  // 选中了一个真实存在的源 → 上一次的"列表读失败"已经不再是当前状态，
  // 清掉它，否则后面所有 hint 都会被那条旧错误一直挡着（永远刷不出新消息）。
  state.sourcesError = "";
  // 适配器类型 / 用途分类：**照后端真实值回填**（缺失才回落默认）
  setAdapterType(src.type || "");
  setProviderTypeSelect(sourceProviderType(src));

  // ---- 模型 ----
  state.configuredModels = [];
  state.sourceModelMetadata = {};
  state.availableModels = null;
  // 🔴 换了源 → 上一个源的候选弹层**必须收掉**：它是浮层，留着就会盖在**新源**的卡片上，
  //    而里面的候选是**旧源**的 —— 用户会以为自己正在给新源挑模型（选错源加错模型）。
  //    ⚠️ 节点本身不清空（留着无害），但 `state` 已复位 → 下次打开是干净的。
  closeModelsModal();
  $("ps-available-list").classList.add("hidden");
  $("ps-available-list").innerHTML = "";
  $("ps-model-search").value = "";
  $("ps-custom-wrap").classList.add("hidden");
  await loadConfiguredModels(id);
  toast($("ps-available-hint"), "点「获取模型列表」从该端点拉取可用模型。");
  // 🔴 选中/保存成功后的**确认语**放在这里显式补一次（真浏览器实测）：
  //    保存流程末尾原本只有 `loadProviderSources()`，而它会把 hint 写成
  //    「当前筛选：…」—— 那句是**列表状态**，不是**操作结果**，
  //    用户在保存后最需要看到的是"存进去了没有"。两句都要，且这句必须在**最后**写。
  if (opts && opts.saved) toast($("providers-hint"), opts.saved);
  return true;
}

/** 「+ 新增」的**空表单**那一半（还没落盘，保存才创建）。
 *
 *  ⚠️ 它**不再**由「+ 新增」按钮直接调用：按钮先弹**厂商预设**（契约 §5.2），
 *     选完预设由 `applyPreset()` 预填；点「不用预设，手动填」才走这里。
 *  ⚠️ `currentProvider` **不在这里重置**：`applyPreset()` 刚设过它。
 *     在这里清成 `openai` 会把预设带进来的厂商标签（`deepseek`/`microsoft`…）
 *     在用户还没保存前就悄悄抹掉 —— 那正是"硬编码 provider"的另一种表现形式。
 */
export function startNewProviderSource() {
  state.creatingSource = true;
  state.selectedSourceId = "";
  state.configuredModels = [];
  state.sourceModelMetadata = {};
  state.availableModels = null;

  renderProviderSourceList();
  $("tab-providers").classList.add("is-detail");
  $("provider-source-placeholder").classList.add("hidden");
  $("provider-source-panel").classList.remove("hidden");
  closeTestModal();

  $("provider-source-title").textContent = "新增提供商源";
  $("provider-source-subtitle").textContent = "填好 ID 与 API Base URL，再点右上角「保存配置」";
  $("ps-id").value = "";
  $("ps-api-base").value = "";
  $("ps-timeout").value = "120";
  $("ps-proxy").value = "";
  $("ps-api-key").value = "";
  $("ps-api-key").placeholder = "未设置";
  renderHeaderRows([]);
  // 新建时回到"两个 OpenAI 格式 + 对话"的默认；选预设会再覆盖一次
  setAdapterType("openai_chat_completion");
  setProviderTypeSelect("chat_completion");

  $("ps-model-count").textContent = "已配置 0";
  $("ps-configured-list").innerHTML = "";
  $("ps-configured-empty").classList.remove("hidden");
  $("ps-configured-empty").textContent = "先保存这个源，然后再「获取模型列表」。";
  // 新增源 = 还没有源可拉 → 把上一个源留下的候选弹层收掉（理由同 selectProviderSource）
  closeModelsModal();
  $("ps-available-list").classList.add("hidden");
  $("ps-available-list").innerHTML = "";
  $("ps-custom-wrap").classList.add("hidden");
  toast($("ps-available-hint"), "");
  toast($("providers-hint"), "填写后点右上角「保存配置」创建。");
  $("ps-api-key").placeholder = "未设置";
  $("ps-id").focus();
}

/** 读某个源**已配置**的模型（源级路由，不靠客户端过滤）。 */
export async function loadConfiguredModels(id) {
  const empty = $("ps-configured-empty");
  try {
    const d = await apiData("GET", `/api/v1/provider-sources/${encodeURIComponent(id)}/providers`);
    state.configuredModels = asArray(d, "providers");
    if (d && d.model_metadata) state.sourceModelMetadata = d.model_metadata;
    renderConfiguredModels();
  } catch (e) {
    state.configuredModels = [];
    renderConfiguredModels();
    // 🔴 失败要把这一行**变成错误说明**（不是留一句"还没有模型"骗人）
    empty.classList.remove("hidden");
    empty.textContent = `已配置模型读取失败：${explainProviderError(e)}`;
  }
}

export function renderConfiguredModels() {
  const ul = $("ps-configured-list");
  const empty = $("ps-configured-empty");
  const list = state.configuredModels || [];

  ul.innerHTML = "";
  $("ps-model-count").textContent = `已配置 ${list.length}`;
  if (!list.length) {
    empty.classList.remove("hidden");
    empty.textContent = "还没有配置模型，点上面的「自定义模型」加一个。";
  } else {
    empty.classList.add("hidden");
  }

  for (const p of list) {
    const pid = p.id || `${state.selectedSourceId}/${p.model || ""}`;
    const li = document.createElement("li");
    li.dataset.modelId = pid;
    if (p.enable === false) li.classList.add("disabled");

    const mid = document.createElement("span");
    mid.className = "mid";
    const code = document.createElement("code");
    code.textContent = pid;
    mid.appendChild(code);

    const sub = document.createElement("span");
    sub.className = "sub";
    sub.textContent = p.enable === false ? "已停用" : (p.model || "");

    const ops = document.createElement("div");
    ops.className = "row";
    const test = document.createElement("button");
    test.type = "button";
    test.dataset.op = "test";
    test.dataset.id = pid;
    test.textContent = "测试";
    const del = document.createElement("button");
    del.type = "button";
    del.className = "danger";
    del.dataset.op = "del";
    del.dataset.id = pid;
    del.textContent = "移除";
    ops.append(test, del);

    li.append(mid, sub, ops);
    ul.appendChild(li);
  }
  renderAvailableModels();   // 已配置的候选要打勾 / 禁用
}

/** 候选模型列表（「获取模型列表」的结果，可勾选添加）。 */
export function renderAvailableModels() {
  const ul = $("ps-available-list");
  const all = state.availableModels;
  ul.innerHTML = "";
  if (!all) { ul.classList.add("hidden"); return; }

  const q = ($("ps-model-search").value || "").trim().toLowerCase();
  const shown = q ? all.filter((m) => String(m).toLowerCase().includes(q)) : all;
  const configured = new Set((state.configuredModels || []).map((p) => p.id));
  const md = state.sourceModelMetadata || {};

  for (const name of shown) {
    const full = `${state.selectedSourceId}/${name}`;
    const added = configured.has(full) || configured.has(name);
    const li = document.createElement("li");
    if (added) li.classList.add("disabled");

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = added;
    cb.disabled = added;
    cb.setAttribute("aria-label", `添加模型 ${name}`);
    cb.onchange = () => addProviderModel(name);

    const mid = document.createElement("span");
    mid.className = "mid";
    const code = document.createElement("code");
    code.textContent = name;
    mid.appendChild(code);

    // 元数据摘要（AstrBot 也会带回来；没有就留空，不编造）
    const info = md[name] || {};
    const bits = [];
    if (info.reasoning) bits.push("推理");
    if (info.tool_call) bits.push("工具");
    if (info.limit && info.limit.context) bits.push(`上下文 ${info.limit.context}`);
    if (info.modalities && Array.isArray(info.modalities.input) && info.modalities.input.length) {
      bits.push(info.modalities.input.join("/"));
    }
    const sub = document.createElement("span");
    sub.className = "sub";
    sub.textContent = bits.join(" · ");

    li.append(cb, mid, sub);
    if (added) {
      const tag = document.createElement("span");
      tag.className = "sub";
      tag.textContent = "已添加";
      li.appendChild(tag);
    } else {
      const add = document.createElement("button");
      add.type = "button";
      add.textContent = "添加";
      add.onclick = () => addProviderModel(name);
      li.appendChild(add);
    }
    ul.appendChild(li);
  }

  if (!shown.length) {
    const li = document.createElement("li");
    li.className = "ps-none";
    li.textContent = q ? `没有匹配「${q}」的模型。` : "该源没有返回任何模型。";
    ul.appendChild(li);
  }
  ul.classList.remove("hidden");
}

/* ---------------- 设置块：适配器类型 / 用途分类（契约 §5.3） ----------------
 *
 * 🔴 为什么要有 `state.currentAdapterType` 这个"影子状态"（别删）：
 *    `<select id="ps-type">` 里**只有**我们能执行的两种 OpenAI 格式（契约 §5.3）。
 *    但预设会带来 `anthropic_chat_completion`/`edge_tts`/… 这些**能存不能跑**的类型。
 *    如果遇到不认识的类型就把 select 硬设成 `openai_chat_completion`，
 *    保存时就会**把用户选的 anthropic 悄悄改成 openai** ——
 *    这正是契约 §5.4 点名的那类硬编码 bug（只是换了个形式）。
 *    → 所以：**select 只表示"两个 OpenAI 格式"**；其余类型走只读展示，
 *      而"真的要提交什么 type"始终以 `state.currentAdapterType` 为准。
 */

/** 我们能执行的两种 OpenAI 格式（契约 §1 `EXECUTABLE_ADAPTER_TYPES`，前端一份镜像）。 */
export const ADAPTER_LABELS = {
  openai_chat_completion: "OpenAI Compatible（/chat/completions）",
  openai_responses: "OpenAI Responses（/responses）",
};

/** 设表单的适配器类型：两种 OpenAI 格式进下拉；其余**只读展示 + 标注不可执行**。 */
export function setAdapterType(t) {
  const v = (t || "").trim() || "openai_chat_completion";
  state.currentAdapterType = v;
  const sel = $("ps-type");
  const ro = $("ps-type-adapter");
  const isSelectable = Object.prototype.hasOwnProperty.call(ADAPTER_LABELS, v);
  if (isSelectable) {
    sel.value = v;
    sel.classList.remove("hidden");
    ro.classList.add("hidden");
    $("ps-type-hint").textContent = "决定“这个端点怎么调”（契约 §1 的适配器协议）。";
  } else {
    // 只读展示：**保留真实值**（不降级、不假装它是 OpenAI 格式）
    sel.classList.add("hidden");
    ro.classList.remove("hidden");
    $("ps-type-adapter-value").textContent = v;
    $("ps-type-adapter-note").textContent =
      "（该适配器来自预设，本平台不能执行；要换成可执行的格式，请选上面的下拉——选了就覆盖它）";
    $("ps-type-hint").textContent = "决定“这个端点怎么调”（契约 §1 的适配器协议）。当前这个来自预设，不可执行。";
  }
  updateNotExecutableNotice();
}

/** 「本平台暂不能执行该适配器，仅保存配置」提示的显隐 —— 只看**真实的** type。 */
export function updateNotExecutableNotice() {
  const bad = !Object.prototype.hasOwnProperty.call(ADAPTER_LABELS, state.currentAdapterType);
  const el = $("ps-not-executable");
  if (bad) {
    el.textContent =
      `⚠️ 本平台暂不能执行该适配器（type=${state.currentAdapterType}），仅保存配置。` +
      `真正要用它时会如实报不可用，不会假装成功。`;
  }
  el.classList.toggle("hidden", !bad);
}

/** 设用途分类（对话/语音/嵌入）。后端对不在支持集的值一律 400，所以这里只有三者。 */
export function setProviderTypeSelect(t) {
  const v = (t || "").trim() || "chat_completion";
  const sel = $("ps-provider-type");
  // 未知分类（后端将来多一类）：**不静默改成对话** —— 那样会把语音源存成对话源，
  // 正是本轮要修的 bug。加一个临时选项如实显示，保存时原样送出去让后端裁决。
  if (!Array.prototype.some.call(sel.options, (o) => o.value === v)) {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = `${v}（后端未列入支持集，保存会被拒）`;
    sel.appendChild(o);
  }
  sel.value = v;
}

/* ---------------- 厂商预设（契约 §2/§3/§5.2） ---------------- */

/** 拉预设目录（**失败要响亮** —— 绝不静默渲染成空列表）。 */
export async function loadProviderPresets() {
  if (state.presets !== null) return true;      // 已经拉到过（`[]` 也是"拉到过"）
  try {
    const d = await apiData("GET", "/api/v1/provider-presets");
    // 🔴 形状不对就抛（`asArray` 的职责）—— 不许 `d.presets || []` 那样把
    //    "没读懂"伪装成"没有预设"。这个接口的失败是最贵的那一类：
    //    用户会以为"这个平台就是没有预设"，然后手动敲一堆字段。
    state.presets = asArray(d, "presets");
    state.presetsError = "";
    return true;
  } catch (e) {
    // 🔴 失败时**保持 `null`**（不是 `[]`）—— 这是"下次还会重试"与
    //    "永远显示成没有预设"的分水岭。设成 `[]` 会让下一次点「+ 新增」
    //    直接命中上面的缓存短路，**静默渲染一个空列表**（本项目最贵的 bug 类型）。
    state.presets = null;
    state.presetsError = explainProviderError(e);
    return false;
  }
}

/** 打开预设选择弹层。**拉不到就如实报错**，并且仍然允许"不用预设、手动填"。 */
export async function openPresetPicker() {
  const box = $("preset-modal");
  box.classList.remove("hidden");
  const list = $("preset-list");
  const hint = $("preset-hint");
  list.innerHTML = "";
  // ⚠️ 先给一句话：`api()` 没有超时，后端若挂住这里会一直空着
  //    → 用户会以为"点了没反应"（本项目最贵的那类观感 bug）。
  hint.textContent = "正在读取厂商预设…";
  hint.style.color = "";
  const ok = await loadProviderPresets();
  if (!ok) {
    // ⚠️ 不要写成 `读取厂商预设失败：HTTP 500（HTTP 500：后端内部错误）` ——
    //    `explainProviderError` 对 5xx 已经补过一句 HTTP 说明，前缀再写一次就重了。
    hint.textContent = `⚠️ 读取厂商预设失败（${state.presetsError}）—— 这不代表"没有预设"，` +
      `是这次没读到；可以再点一次「+ 新增」重试。`;
    hint.style.color = "var(--danger)";
    // 给一条"仍然能干活"的路，而不是把用户堵死在一个空弹层上
    const li = document.createElement("li");
    li.className = "preset-none";
    li.textContent = "预设读不到，可以直接「不用预设，手动填」。";
    list.appendChild(li);
    return;
  }
  hint.style.color = "";
  renderPresetPicker();
}

/** 画预设选择器：**按 provider_type 分组**（契约 §5.2）。 */
export function renderPresetPicker() {
  const all = state.presets || [];
  const groups = $("preset-groups");
  const list = $("preset-list");
  const hint = $("preset-hint");
  groups.innerHTML = "";
  list.innerHTML = "";

  if (!all.length) {
    hint.textContent = "后端返回了 0 个预设 —— 可以「不用预设，手动填」。";
    return;
  }
  hint.textContent = `共 ${all.length} 个预设，按用途分组（这一步只是预填，选完还能改）。`;

  // 分组顺序固定：对话 → 语音 → 嵌入 → 其他（不让"其他"排在最前面抢注意力）
  const known = ["chat_completion", "text_to_speech", "embedding"];
  const present = [];
  for (const t of known) if (all.some((p) => p.provider_type === t)) present.push(t);
  for (const p of all) {
    const t = p.provider_type || "未知";
    if (!present.includes(t)) present.push(t);
  }
  if (!present.includes(state.presetGroup)) state.presetGroup = present[0];

  for (const t of present) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "preset-group-btn";
    const n = all.filter((p) => (p.provider_type || "未知") === t).length;
    b.textContent = `${providerTypeLabel(t)}（${n}）`;
    b.classList.toggle("active", t === state.presetGroup);
    b.setAttribute("aria-pressed", t === state.presetGroup ? "true" : "false");
    b.onclick = () => { state.presetGroup = t; renderPresetPicker(); };
    groups.appendChild(b);
  }

  const shown = all.filter((p) => (p.provider_type || "未知") === state.presetGroup);
  for (const p of shown) {
    const li = document.createElement("li");
    li.dataset.presetId = p.id;
    const main = document.createElement("div");
    main.className = "preset-main";
    const lab = document.createElement("div");
    lab.className = "preset-label";
    lab.textContent = p.label || p.id;
    const meta = document.createElement("div");
    meta.className = "preset-meta";
    // 把"会写进表单的东西"**先摆给用户看**：provider / type / api_base
    // （契约 §7.5：只预填这四个 + id 由用户自己填，专属字段一律不预填）
    meta.textContent = `${p.provider || "?"} · ${p.type || "?"}${p.api_base ? " · " + p.api_base : " · （无默认地址）"}`;
    main.append(lab, meta);
    li.appendChild(main);

    // 🔴 能力诚实（契约 §0）：我们只能执行两个 OpenAI 适配器。
    //    不可执行的**照实列出**（用户能看到、能建、字段照填），但必须显式标注。
    const tag = document.createElement("span");
    if (p.executable_by_us === true) {
      tag.className = "preset-tag ok";
      tag.textContent = "可执行";
    } else {
      tag.className = "preset-tag warn";
      tag.textContent = "不可执行";
      tag.title = "本平台暂不能执行该适配器，仅保存配置";
    }
    li.appendChild(tag);

    if (p.note) {
      const note = document.createElement("div");
      note.className = "preset-note";
      note.textContent = p.note;
      li.appendChild(note);
    }
    li.onclick = () => applyPreset(p);
    li.tabIndex = 0;
    li.setAttribute("role", "button");
    li.onkeydown = (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); applyPreset(p); }
    };
    list.appendChild(li);
  }
}

/** 选中一个预设 → **只预填** provider / type / provider_type / api_base（契约 §7.5）。
 *
 *  ⚠️ **不预填** `voice`/`rate`/`embedding_dimensions` 这类专属字段：
 *     本轮只做配置层，那些字段**没有消费方** —— 存了也没人读，
 *     属于"看起来支持、其实没人用"的假能力（契约 §7.5 明确不做）。
 *  ⚠️ id 由用户自己填：预设 id（如 `edge_tts`）是**预设的**标识，
 *     源 id 是用户自己的命名空间，替他决定会让"两个 OpenAI 源"没法共存。
 */
export function applyPreset(p) {
  setAdapterType(p.type || "");
  setProviderTypeSelect(p.provider_type || "chat_completion");
  $("ps-api-base").value = p.api_base || "";
  // `provider` 是厂商标签，页面上没有独立输入框 → 存进状态，保存时原样提交
  state.currentProvider = p.provider || "openai";
  const switched = applyPendingProviderTypeFilter(p.provider_type || "chat_completion");

  $("preset-modal").classList.add("hidden");

  // 用户可改 → 焦点直接落到 id 上（那是唯一还需要他填的必填项）
  const hint = $("providers-hint");
  const tail = switched
    ? ` 左侧筛选已切到「${providerTypeLabel(p.provider_type || "chat_completion")}」，保存后能立刻看见。`
    : "";
  if (p.executable_by_us === true) {
    toast(hint, `已套用预设「${p.label || p.id}」（${p.type}）。填个 ID 就能保存。${tail}`);
  } else {
    // 🔴 如实说：**能存，但跑不了**。绝不写成"已就绪/可用"。
    toast(hint,
      `已套用预设「${p.label || p.id}」（${p.type}）：本平台暂不能执行该适配器，仅保存配置。` +
      `填个 ID 可以存下来，但测试连通/会话选模型时会如实报不可用。${tail}`, true);
  }
  $("ps-id").focus();
}

/** 新建源之后，让左侧筛选自动切到"刚建的那一类"（返回 true = 真的切了）。
 *
 * 🔴 为什么要有这一步：用户选了「语音」预设新建一个 TTS 源，
 *    保存成功后如果筛选还停在「对话」，**刚建的源在列表里根本看不见**
 *    —— 用户会以为"没保存成功"，然后再存一遍。这类"东西存进去了但看不见"
 *    的困惑，就是 `hiddenSourceCount` 那句提示要解决的问题的近亲。
 * ⚠️ 这里**不发提示**：调用方（`applyPreset`）要把这句话和"已套用预设"
 *    合并成一条说，否则后一条 toast 会把前一条直接盖掉（实测过这种"消息打架"）。
 */
export function applyPendingProviderTypeFilter(pt) {
  if (state.providerFilter === pt) return false;
  state.providerFilter = pt;
  renderProviderSourceList();
  return true;
}

/* ---------------- 写操作：保存 / 新增 / 删除 ---------------- */

/** 表单 → 请求体。🔴 key **只在本次真的填了**时才带上（留空 = 不修改）。
 *
 * 🔴 这里曾经**硬编码** `provider_type:"chat_completion"` / `provider:"openai"` /
 *    `type:"openai_chat_completion"` —— 后果是**一个 TTS 源被静默存成对话源**
 *    （用户看到"保存成功"，库里却是另一个东西；契约 §5.4 点名的就是这个）。
 *    现在三个字段一律取自**表单/预设的真实值**：
 *      · `type`          ← `state.currentAdapterType`（两个 OpenAI 格式来自下拉，
 *                          其余来自预设只读展示，**原样送出、不降级**）
 *      · `provider_type` ← `#ps-provider-type` 下拉
 *      · `provider`      ← `state.currentProvider`（预设带的厂商标签；手填时默认 openai）
 */
export function collectSourcePayload() {
  const timeoutRaw = $("ps-timeout").value.trim();
  const timeout = timeoutRaw === "" ? 120 : Number(timeoutRaw);
  const key = $("ps-api-key").value.trim();
  const body = {
    id: $("ps-id").value.trim(),
    // ★ 真实值（不再硬编码）
    provider_type: $("ps-provider-type").value || "chat_completion",
    provider: state.currentProvider || "openai",
    type: state.currentAdapterType || "openai_chat_completion",
    api_base: $("ps-api-base").value.trim(),
    timeout: Number.isFinite(timeout) && timeout > 0 ? timeout : 120,
    proxy: $("ps-proxy").value.trim(),
    custom_headers: headersFromRows(),
    // 本页没有"停用源"的开关 → 保持它原来的值（新建默认启用）
    enable: state.sourceEnable === false ? false : true,
  };
  if (key) body.key = [key];     // ★ 列表形状（照真实契约：key 是数组）
  return body;
}

export async function saveProviderSource() {
  const hint = $("providers-hint");
  const body = collectSourcePayload();

  // 客户端先校验一遍：能当场说清的错误不必绕一圈后端（后端仍会再校验）
  if (!body.id) { toast(hint, "ID 必填。", true); $("ps-id").focus(); return; }
  if (!/^https?:\/\//i.test(body.api_base)) {
    toast(hint, "API Base URL 必填，且必须以 http:// 或 https:// 开头。", true);
    $("ps-api-base").focus();
    return;
  }

  const btn = $("provider-save-config");
  const creating = !!state.creatingSource;
  const oldId = state.selectedSourceId;
  btn.disabled = true;
  toast(hint, "保存中…");
  try {
    if (creating) {
      await api("POST", "/api/v1/provider-sources", body);
    } else {
      // ⚠️ 用**原来的 id** 定位（表单里改 ID = 重命名）、body 里带新 id
      await api("PUT", `/api/v1/provider-sources/by-id?source_id=${encodeURIComponent(oldId)}`, body);
    }
    state.creatingSource = false;
    // 用户主动保存 → 必须**真的**重读一次（不能复用 once 缓存）
    await loadProviderSourcesFresh();
    const savedMsg = creating ? `已新增 ${body.id}。` : `已保存 ${body.id}。`;
    // ⚠️ 把确认语**交给 selectProviderSource 在最后写**（它内部还有 load/渲染，
    //    现在写会被后续的 hint 更新盖掉）。见该函数的 `opts.saved`。
    if (!(await selectProviderSource(body.id, { skipHash: false, saved: savedMsg }))) {
      // 保存成功了但列表里没读到（后端可能只写了一半）→ 如实说，别假装成功
      toast(hint, `已提交 ${body.id}，但列表里读不到它 —— 请刷新或检查后端。`, true);
      return;
    }
  } catch (e) {
    toast(hint, `保存失败：${explainProviderError(e)}`, true);
  } finally {
    btn.disabled = false;
  }
}

export async function deleteProviderSource(id, btn) {
  if (!id) return;
  if (!confirm(`删除提供商源「${id}」？挂在它下面的模型也会一起失效。`)) return;
  const hint = $("providers-hint");
  if (btn) btn.disabled = true;
  try {
    await api("DELETE", `/api/v1/provider-sources/by-id?source_id=${encodeURIComponent(id)}`);
    if (state.selectedSourceId === id) { clearProviderDetail(); go("providers"); }
    await loadProviderSourcesFresh();
    toast(hint, `已删除 ${id}。`);
  } catch (e) {
    toast(hint, `删除失败：${explainProviderError(e)}`, true);
    if (btn) btn.disabled = false;
  }
}

/* ---------------- 模型：获取列表 / 添加 / 移除 / 测试 ----------------
 *
 * 🔴 「获取模型列表」与「测试」的结果都走**弹窗**（情绪 2026-09-21：
 *    「获取到的模型，与测试都用弹窗形式发送」）。改成弹窗要守住三条，否则会退回旧毛病：
 *      ① 弹层里放的是**原来那些节点**（`ps-model-search` / `ps-available-hint` /
 *         `ps-available-list` / `providers-test-result`），不是复制一份 ——
 *         复制一份 = 两个 id、两处真相，`$()` 只认第一份，另一份永远不更新；
 *      ② **失败也必须打开**弹层并把原因写进 hint —— 不许"失败 → 什么都没弹"，
 *         那正是本项目最贵的静默失败；
 *      ③ 卡片里 `#ps-configured-list`（已配置的模型）**留在原处** ——
 *         那是用户的工作列表，不是一次性的查询结果。
 */

/** 打开「可用模型」弹层。
 *
 *  ⚠️ 弹层先开、再拉数据（不是拉完才开）：拉取要等上游，用户点完按钮若界面毫无变化，
 *     第一反应是"按钮坏了"，然后就会去连点。
 *  ⚠️ 副标题里带上源 id：弹层是浮层，脱离卡片上下文，不写清楚就不知道这是**哪个源**的结果。
 */
export function openModelsModal() {
  $("models-subtitle").textContent = state.selectedSourceId
    ? `源：${state.selectedSourceId}（拉取的是该源 API Base URL 下的模型）`
    : "还没选中提供商源。";
  $("models-modal").classList.remove("hidden");
}
export function closeModelsModal() { $("models-modal").classList.add("hidden"); }

/** 打开「模型测试」弹层，并把进度/结果节点复位。
 *
 *  ⚠️ 这里**明写白色**（`#providers-test-result` 的 color）：它复用的是 `.preview`
 *     的深色代码块皮肤，而 `toast()` 会把它染成 `var(--danger)` 表示失败；
 *     不还原的话，下一次**成功**的测试会继承上次的红色（"✅ 连通正常"是红的），
 *     用户会以为又失败了。progress 用 muted、结果由 toast 上色。
 */
export function openTestModal(pid) {
  $("test-subtitle").textContent = `模型：${pid}`;
  const out = $("providers-test-result");
  out.textContent = "";
  out.style.color = "";
  $("test-modal").classList.remove("hidden");
}
export function closeTestModal() { $("test-modal").classList.add("hidden"); }

export async function fetchSourceModels() {
  const id = state.selectedSourceId;
  const hint = $("ps-available-hint");
  const btn = $("ps-fetch-models");
  // 🔴 把上一次的结果清干净再开：留着的话"新一次拉取"会先显示旧候选，
  //    用户分不清屏幕上的是这次的还是上次的（尤其是刚换了搜索词的场景）。
  state.availableModels = null;
  renderAvailableModels();
  $("ps-model-search").value = "";
  // 弹层**先开**（见 openModelsModal 注释）；失败路径也不会关它，原因写在 hint 里。
  openModelsModal();
  if (!id) { toast(hint, "先选中（或保存）一个提供商源。", true); return; }
  btn.disabled = true;
  toast(hint, `正在从 ${id} 拉取模型列表…`);
  try {
    const d = await apiData("GET", `/api/v1/provider-sources/${encodeURIComponent(id)}/models`);
    state.sourceModelMetadata = (d && d.model_metadata) || {};
    state.availableModels = asArray(d, "models");
    renderConfiguredModels();       // 顺带刷新勾选态（已配置的候选要打勾/锁定）
    if (!state.availableModels.length) {
      // 🔴 空列表**不算成功**（本项目最贵的 bug 类型就是"静默成功"）
      toast(hint, `端点返回了 0 个模型（${id}）—— 检查 API Base URL 与 Key 是否正确。`, true);
    } else {
      toast(hint, `可用模型 ${state.availableModels.length} 个（已配置的会打勾）。`);
    }
  } catch (e) {
    state.availableModels = [];
    renderAvailableModels();
    toast(hint, `获取模型列表失败：${explainProviderError(e)}`, true);
  } finally {
    btn.disabled = false;
  }
}

export async function addProviderModel(name) {
  const id = state.selectedSourceId;
  const hint = $("ps-available-hint");
  if (!id) { toast(hint, "先选中一个提供商源。", true); return; }
  try {
    // 两种后端形状都兼容：`model`（源下加模型）和 `id`（完整 `源/模型`）
    await api("POST", `/api/v1/provider-sources/${encodeURIComponent(id)}/providers`, {
      id: `${id}/${name}`,
      model: name,
      provider_source_id: id,
      enable: true,
    });
    // 🔴 `loadConfiguredModels()` 内部会重画**卡片里那份**已配置列表（`#ps-configured-list`）
    //    并回调 `renderAvailableModels()` 刷新勾选态 —— 所以添加完，卡片的列表
    //    和弹层里的勾选是**同时**更新的，不会让用户对着一张过期的列表发呆。
    await loadConfiguredModels(id);
    toast(hint, `已添加 ${id}/${name}。`);
  } catch (e) {
    toast(hint, `添加「${name}」失败：${explainProviderError(e)}`, true);
  }
}

export async function removeProviderModel(pid) {
  if (!confirm(`移除模型「${pid}」？`)) return;
  const hint = $("ps-available-hint");
  try {
    await api("DELETE", `/api/v1/providers/${encodeURIComponent(pid)}`);
    await loadConfiguredModels(state.selectedSourceId);
    toast(hint, `已移除 ${pid}。`);
  } catch (e) {
    toast(hint, `移除「${pid}」失败：${explainProviderError(e)}`, true);
  }
}

/** 测试某个模型：成功显示延迟/样例，失败**逐条说清原因**。 */
export async function testProviderModel(pid, btn) {
  const out = $("providers-test-result");
  if (btn) btn.disabled = true;
  // 🔴 弹层在**发请求之前**打开（情绪要求测试走弹窗）：
  //    测试要等上游，几秒钟里界面必须已经有反馈，否则用户只会重复点按钮。
  //    ⚠️ 用户可能在等待期间关掉弹层 —— 结果照样写回 `#providers-test-result`
  //      （节点没被销毁，只是父层隐藏），下次打开就是最新结果，不会丢。
  openTestModal(pid);
  toast(out, `正在测试 ${pid} …`);
  try {
    // 🔴 用 `apiData`（它会拆掉 `{status,message,data}` 信封）——
    //    ⚠️ 我原来用 `api()` 拿到的是**整个信封**，于是：
    //      ① `d.status` 读到的其实是**信封的 status**（恒为 "ok"）→ 判定全乱；
    //      ② 失败时 `d.reason` 不存在，回退到 `d.status` → 界面显示「原因：ok」
    //         （用户看到的就是"测试失败 / 原因：ok"这种莫名其妙的话）。
    //    真实契约（`ASTRBOT_PROVIDER_CONTRACT.md` §3）：data 里是
    //      `{id, model, status: "available"|"unavailable", error}`。
    const d = await apiData("POST", `/api/v1/providers/${encodeURIComponent(pid)}/test`, {});
    const okStatus = !!(d && d.status === "available");
    // 兼容 `{ok:true}` 形态（万一后端同时给）——但**以 status 为准**
    const passed = okStatus || (d && d.ok === true);
    if (passed) {
      out.textContent =
        `✅ ${pid} 连通正常\n` +
        `模型：${(d && (d.model || d.id)) || "?"}\n` +
        `延迟：${(d && d.latency_ms) ?? "?"} ms\n` +
        `样例：${(d && d.sample) || "（无样例）"}`;
    } else {
      // 失败原因**如实**取 `error`（契约字段），取不到才退到 status
      const reason = (d && (d.error || d.message)) || "";
      out.textContent =
        `❌ ${pid} 测试失败\n` +
        `原因：${reason || (d && d.status) || "unknown"}\n` +
        `说明：${reason ? "" : "（后端未给出说明）"}`;
    }
  } catch (e) {
    out.textContent = `❌ ${pid} 测试失败\n原因：HTTP/网络错误\n说明：${explainProviderError(e)}`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ---------------- 事件绑定（页面级，脚本只加载一次） ---------------- */

// 左侧列表：删除用**一个**委托处理器（列表会随重渲染重建，逐项直接绑会丢）

// 已配置模型行内的「测试 / 移除」同样用委托

// 「+ 新增」= ①先把**空表单**摆好 + ②再在上面弹**厂商预设**选择（契约 §5.2）。
//
// 🔴 顺序与"为什么不只弹弹层"（这点很容易做错，写清楚）：
//    ① 必须**先**把空表单摆好（`creatingSource=true` + 清空字段）。
//       否则用户在弹层里点「不用预设，手动填」之前，右侧还停在上一个源的详情上，
//       此时如果点了「保存配置」，就会被当成**改那个源**（PUT 改名）——
//       明明是"新增"，结果改掉了别人。这是数据损坏级的错误。
//    ② 弹层只是**预填的捷径**：它盖在上面，选完就消失，表单一直是可编辑的真身。
//       → 用户点「关闭」/点遮罩，等价于"不用预设"，不会卡在半路。
export function newProviderSourceFlow() {
  startNewProviderSource();
  openPresetPicker();
}

/** 关闭预设弹层（关闭 = 不用预设；表单已经摆好了，用户直接手填）。 */
export function closePresetPicker() {
  $("preset-modal").classList.add("hidden");
  // 🔴 「不用预设」= 用户明确表示"我要自己填" → 厂商标签回落到通用默认。
  //    ⚠️ 必须在这里清：`startNewProviderSource()` 里**不能**清（那会把刚
  //       套用的预设标签抹掉，见该函数的注释）。两处职责不同，别合并。
  state.currentProvider = "openai";
  $("ps-id").focus();
}


// ---- 类型筛选（契约 §5.1）：四个按钮一个委托处理器 ----
// ⚠️ 用委托而不是逐按钮绑定：以后加一类（如 STT）只需在 HTML 里加个按钮，
//    不会出现"新按钮忘了绑事件、点了没反应"这种静默失效。

// ---- 适配器类型下拉：切换后**同步影子状态**（保存时用的是它，不是 select 的 value）----

// ---- 厂商预设弹层（契约 §5.2）----

// ---- 「可用模型」/「模型测试」弹层（情绪 2026-09-21：结果走弹窗）----
// 关闭三件套与页面既有弹层**完全同构**：关闭按钮 + 点遮罩（判 `e.target.id` 是**遮罩本身**
// 才关，否则点弹层内部任何空白都会误关）+ Esc（见上面的 keydown 表）。

// 「自定义模型」：点开一个内联输入框（**不用** window.prompt —— 阻塞且不可测）

// 表单里回车 = 保存（否则会触发表单默认提交 → 整页刷新，丢掉未保存状态）

/* ================= 弹层 ================= */
export function showModal(title, body) {
  $("modal-title").textContent = title;
  $("modal-body").textContent = body;
  $("modal").classList.remove("hidden");
}
