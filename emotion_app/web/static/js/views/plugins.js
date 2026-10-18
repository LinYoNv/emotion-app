/* plugins —— 由原单文件 app.js 拆分（**原生 ES 模块，零构建**）
 *
 * 🔴 本文件只放**函数/常量声明**，**没有任何模块级副作用**
 *    （事件绑定统一在 `js/bootstrap.js`）。
 *    这是为了消除循环依赖在"模块顶层立即求值"时拿到未初始化绑定的风险。
 */

import { $, api, applyRoute, go, showPanel, state, toast } from "../core/core.js";
import { showHint } from "./session.js";
import { buildConfigField, makeConfigInput } from "./settings.js";


/* ================= 插件拓展页 =================
 *
 * 🔴 为什么有这个页面（情绪报的 bug）：
 *    「插件的配置入口没有喵」—— 根因是**宿主只挂了插件的数据 API、
 *      没挂插件的页面**。`metadata.yaml` 里声明了 `pages:` 的插件
 *      （陪伴面板 / QQ 空间与书柜 / 一起房间）在面板里完全没有入口。
 *    后端补了三条路由（见 `emotion_app/web/plugin_pages_web.py`），
 *    这里就是它的界面。
 *
 * 用 **iframe** 而不是把插件 HTML 直接注进来：
 *   * 插件页有自己的 `<style>`/全局变量（实测 `*` 选择器、`html{}` 主题），
 *     注进来会**污染整个面板**；
 *   * 插件页有自己的 JS 全局（`state`、`HTTP_API`…），与我们的 `state` 冲突；
 *   * iframe 天然隔离，且插件页的**相对资源路径**照常工作。
 *   ⚠️ 同源 iframe：cookie 会带上，所以插件页的 fetch 天然是已登录的。
 */

/* ---------------- 插件启停（照真 AstrBot 的 inactivated_plugins） ----------------
 *
 * 🔴 三条设计取舍（都会直接影响"用户看到的对不对"）：
 *
 * ① **勾选框显示的是"已保存的意图"，不是"当前运行态"**。
 *    若跟着运行态走：用户取消勾选 → 保存 → 运行态还没变（要重启）→
 *    勾选框自己跳回去，用户会以为"没保存成功"。
 *    所以：勾选 = 保存的名单，旁边**另用状态标签**说"现在跑没跑"。
 *
 * ② **"需重启生效"是后端算的**（`restart_required` = 保存集合 != 运行集合），
 *    不是这里写死的 true —— 所以重启后再打开这一页，它会自己消失。
 *    我们**没有**做"保存即生效"：真 AstrBot 的 `turn_on_plugin` 也是
 *    `await self.reload(plugin_name)`（要重载），我们照它，如实在界面上说明。
 *
 * ③ **失败不许显示成"没有插件"**：接口 503（宿主没起来）时，
 *    提示里写清原因并**清空列表** —— 绝不保留上一次的旧列表。
 */

/** 读插件清单 + 已保存的启停名单（进 `#/plugins` 时调）。 */
export async function loadPluginSwitch() {
  const hint = $("plugin-switch-hint");
  const host = $("plugin-switch-list");
  try {
    const d = await api("GET", "/api/v1/plugins");
    state.pluginSwitch = d;
    renderPluginSwitch();
    // 🔴 升级列表与启停列表**同一份数据**（`state.pluginSwitch`）——
    //    另开一条 `/api/v1/plugins` 请求会漂（本项目踩过"同源数据各取一份"）。
    renderPluginUpdate();
    // 🔴 侧边栏的插件分组**跟这一份数据同源**（同一次请求的结果）——
    //    它是"哪个插件有拓展页"的唯一数据来源，另开一条请求就会漂。
    renderNavPluginPages(d);
    hint.style.color = "";
  } catch (e) {
    // 🔴 读失败 → 说清原因 + 清空（不把"没读到"显示成"没有插件"）
    state.pluginSwitch = null;
    host.innerHTML = "";
    // 升级列表同样要跟着清（否则会留着上一次的插件，点"升级"打空）
    renderPluginUpdate();
    // ⚠️ 侧边栏也要清掉：留着上一次的分组会显示**已经不存在的插件**，
    //    点进去 404（比"没有"更糟 —— 用户会以为插件还在）。
    renderNavPluginPages(null);
    hint.textContent = `读取插件清单失败：${e.message}`;
    hint.style.color = "var(--danger)";
  }
}

/* ================================================================================
 * 侧边栏的「插件页面」分组（情绪 2026-10-09）
 *
 * 「在插件页点击就有了，不过做成 astrbot 那样也行，就是在侧边栏添加这个。」
 * ⇒ 照真 AstrBot：侧边栏「插件」下面按插件列出**有拓展页的那些**。
 *
 * 🔴 **完全不写死插件名**（情绪明确要求：「以后我自己添加了插件也能自己显示」）：
 *    数据来自 `GET /api/v1/plugins` 的 `plugins[].pages[]` + `display_name`，
 *    装了新插件且它带 `pages/`，刷新面板就自己出现。
 *
 * ## 真 AstrBot 的做法（对照，`InstalledPluginsTab-DSnl1LUd.js:1`）
 *    侧边栏那一项点的是 `pages[0]`（**只取第一个**）。
 *    ⚠️ 我们**列全部**：配套插件有 2 个页面（`companion-panel` + `陪伴面板`），
 *       只给一个会把另一个变成"只能从插件页进"的半残（真 AstrBot 就是这样）。
 *       我们的插件页本来就能列出全部，侧边栏对齐它更一致。
 * ================================================================================ */

export const NAV_PLUGIN_OPEN_KEY = "emotion.navPluginGroupOpen";

/** 画侧边栏的插件分组。`d` 为 `null` = 读失败/没数据 ⇒ 整块隐藏。 */
export function renderNavPluginPages(d) {
  const group = $("nav-plugin-group");
  const box = $("nav-plugin-items");
  if (!group || !box) return;

  // 🔴 **列的资格（两条，缺一不可）**：
  //    ① **真的有拓展页** —— 没页面的插件在侧边栏里没有可点的东西；
  //    ② **`state === "loaded"`（正在运行）** —— 2026-10-10 修，情绪当场指出。
  //
  // 我第一版只判了 ①，结果把 `meme_manager` 也列了出来 —— 而它 `state=failed`
  // （`ModuleNotFoundError: No module named 'requests'`），**从加载那天起就没跑起来过**
  // ⇒ 点进去 iframe 一片空白（页面由插件提供，插件没跑自然没有页面）。
  //
  // ⚠️ 更糟的是它**看起来像我们的功能坏了**：用户不会想到"这是别人 AstrBot 的插件、
  //    而且没装上依赖"，只会觉得"情绪app 的插件页面是坏的"。
  //    情绪原话：「表情管理器我们平台现在哪来的嘛」—— 他说得对，
  //    那不是我们平台装的插件，不该出现在我们的导航里。
  //
  // ⚠️ 停用（inactivated）/ 失败（failed）/ 未加载（not_loaded）**都不列**。
  //    它们在「插件」页里照样看得见（那里本来就该显示"为什么没跑"），
  //    所以**不是隐藏问题**，只是不给一个点不开的导航入口。
  const plugins = ((d && d.plugins) || []).filter(
    (p) => Array.isArray(p.pages) && p.pages.length > 0 && p.state === "loaded",
  );

  // 🔴 **分成两段：「本平台自带」与「外部插件」**（2026-10-10 情绪定）。
  //
  //    情绪原话：「只做目前有的插件然后给接口，**安装插件后有独立 ui 的才在这显示**」。
  //    我们的插件源目录是**可扩展**的（往 `plugins/` 里放 + 重启就会加载，
  //    见 `tests/test_plugin_dir_extensibility.py`），所以以后装进来的插件**也应该**
  //    出现在这里 —— 但那不是"我们平台自带的"，得让人一眼分得清。
  //
  //    ⚠️ 判据必须是**外部依据**（不能只做"和接口数据一样"的集合比较 ——
  //       本项目已记：那样两边同源、必然一致，证明不了"该不该列"）。
  //       这里的外部依据 = **模块路径**：我们的自带插件是**仓库内的顶层包**
  //       （`astrbot_plugin_xxx.main`，`plugins/` 就在仓库根），
  //       而外部装进来的插件一定带**命名空间前缀**（如 `data.plugins.xxx.main`）。
  //       于是"哪些是自带的"由**代码事实**决定，不由我看一眼名单拍板。
  const isBundled = (p) => {
    const mp = String(p.module_path || "");
    if (!mp) {
      // ⚠️ 拿不到模块路径时**不猜**：宁可归到"外部"，也不能把外来的说成我们的。
      return false;
    }
    return /^astrbot_plugin_[A-Za-z0-9_]+\.main$/.test(mp);
  };

  box.innerHTML = "";
  if (!plugins.length) {
    // 一个都没有 ⇒ 整块不显示（空分组是纯噪音）
    group.classList.add("hidden");
    return;
  }
  group.classList.remove("hidden");

  const subTitle = (text) => {
    const h = document.createElement("div");
    h.className = "nav-subhead";
    h.textContent = text;
    box.appendChild(h);
  };

  /** 画一个插件的全部页面为侧边栏子项。 */
  const renderOne = (p) => {
    // ⚠️ 显示名：`display_name` 可能为空（不是每个插件都声明）→ 退化成 name。
    //    **必须在显示时退化**：数据层保持"有没有声明"这个区别。
    const title = (p.display_name || "").trim() || p.name || p.id;
    // 一个插件可能有多个页面 → 生成多条子项（带页面名区分）
    const multi = p.pages.length > 1;
    for (const pg of p.pages) {
      const a = document.createElement("a");
      a.className = "nav-subitem";
      a.href = `#/plugins/${encodeURIComponent(p.id)}/${encodeURIComponent(pg.name)}`;
      a.dataset.navPage = `${p.id}/${pg.name}`;
      a.title = multi ? `${title} · ${pg.title || pg.name}` : title;
      const ico = document.createElement("span");
      ico.className = "nav-subico";
      ico.setAttribute("aria-hidden", "true");
      ico.textContent = "🧩";
      a.appendChild(ico);
      const lb = document.createElement("span");
      lb.className = "nav-sublabel";
      // 多页面时把页面名带上，否则两条同名分不出来
      lb.textContent = multi ? `${title} · ${pg.title || pg.name}` : title;
      a.appendChild(lb);
      box.appendChild(a);
    }
  };

  const bundled = plugins.filter(isBundled);
  const external = plugins.filter((p) => !isBundled(p));
  // 两段都只有一类时**不画小标题**（单个分组下的小标题是纯噪音）。
  if (bundled.length && external.length) {
    subTitle("本平台自带");
  }
  for (const p of bundled) {
    renderOne(p);
  }
  if (bundled.length && external.length) {
    subTitle("外部插件");
  }
  for (const p of external) {
    renderOne(p);
  }
  syncNavPluginGroupOpen();
}

/** 侧边栏插件子项的**选中态**：按当前 hash 高亮对应的那一条。
 *
 * 🔴 为什么按 hash 判而不是按 `name`：`showPanel("plugins")` 这个 `name`
 *    同时服务「插件列表」和「某个插件页面」两种状态 ——
 *    光有 `name` 分不出用户在哪一条子项上。
 * ⚠️ 高亮子项时**同时点亮「插件」那一项**：用户视角是"我在插件这个大区里"，
 *    两个都暗着会让人以为导航没跟上。
 */
export function syncNavPluginActive() {
  const raw = (location.hash || "").replace(/^#\/?/, "");
  const parts = raw.split("/").filter(Boolean).map(decodeURIComponent);
  // `#/plugins/<id>/<page>` → 有 page 才是"在看某个插件页面"
  const want = (parts[0] === "plugins" && parts[1] && parts[2])
    ? `${parts[1]}/${parts[2]}` : "";
  document.querySelectorAll(".nav-subitem").forEach((a) => {
    const on = !!want && a.dataset.navPage === want;
    a.classList.toggle("active", on);
    if (on) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  if (want) {
    const parent = document.querySelector('.nav-item[data-nav="plugins"]');
    if (parent) parent.classList.add("active");
  }
}

/** 展开/收起态：**记在 localStorage**，切页与刷新都不该自己弹开。 */
export function syncNavPluginGroupOpen() {
  const group = $("nav-plugin-group");
  const btn = $("nav-plugin-toggle");
  const box = $("nav-plugin-items");
  if (!group || !btn || !box) return;
  const open = localStorage.getItem(NAV_PLUGIN_OPEN_KEY) === "1";
  group.classList.toggle("open", open);
  box.classList.toggle("hidden", !open);
  btn.setAttribute("aria-expanded", open ? "true" : "false");
}




/** 按 `state.pluginSwitch` 重画勾选列表 + 提示。 */
export function renderPluginSwitch() {
  const d = state.pluginSwitch;
  const hint = $("plugin-switch-hint");
  const host = $("plugin-switch-list");
  host.innerHTML = "";
  if (!d) return;

  const plugins = d.plugins || [];
  if (!plugins.length) {
    hint.textContent = "没有发现任何插件（检查 plugins.plugin_dirs）—— 这不是「读取失败」。";
    return;
  }

  const STATE_TEXT = {
    loaded: "运行中",
    inactivated: "已停用（未加载）",
    failed: "加载失败",
    not_loaded: "未加载",
  };
  for (const p of plugins) {
    const row = document.createElement("label");
    row.className = "switch-row";
    row.dataset.pluginId = p.id;
    row.dataset.modulePath = p.module_path;

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "switch-cb";
    // 勾选 = 启用（= 不在停用名单里）；这里用的是**保存的意图**
    cb.checked = p.enabled_saved !== false;
    row.appendChild(cb);

    const name = document.createElement("span");
    name.className = "switch-name";
    name.textContent = p.name || p.id;
    row.appendChild(name);

    const id = document.createElement("span");
    id.className = "switch-id";
    id.textContent = p.module_path;
    row.appendChild(id);

    const st = document.createElement("span");
    const bad = p.state === "failed";
    st.className = "switch-state" + (bad ? " bad" : "");
    st.textContent = STATE_TEXT[p.state] || p.state;
    // 失败原因要看得见（否则用户只知道"没跑"，不知道"为什么"）
    if (bad && p.error) st.title = p.error;
    row.appendChild(st);

    // 🔴 「详情」按钮：这是**唯一**进详情页的入口（照真 AstrBot：点卡片进详情）。
    //    ⚠️ 必须 `type="button"` + `preventDefault()` + `stopPropagation()`：
    //       整行是 `<label>`，点它**会切换那个勾选框** ——
    //       不挡的话"看详情"会顺手把插件停用/启用（静默改配置，本项目最忌）。
    const detail = document.createElement("button");
    detail.type = "button";
    detail.className = "link-btn switch-detail";
    detail.textContent = "详情";
    detail.onclick = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      go(`plugins/${encodeURIComponent(p.id)}`);
    };
    row.appendChild(detail);

    // 🔴 「配置」按钮（情绪 2026-10-09：「有些插件没有内置的独立 webui
    //    就可以打开插件的配置」）。
    //    ⚠️ 与「详情」按钮**同款三条防护**，一条都不能少：
    //       `type="button"` + `preventDefault()` + `stopPropagation()` ——
    //       整行是 `<label>`，不挡的话"打开配置"会**顺手把插件停用/启用**
    //       （静默改配置，本项目最忌的那类 bug）。
    const cfgBtn = document.createElement("button");
    cfgBtn.type = "button";
    cfgBtn.className = "link-btn switch-config";
    cfgBtn.textContent = "配置";
    cfgBtn.title = "打开这个插件的配置";
    cfgBtn.onclick = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      // ⚠️ 也走 hash 路由（与"详情"一致）：这样能后退、能分享、刷新还在
      go(`plugins/${encodeURIComponent(p.id)}/config`);
    };
    row.appendChild(cfgBtn);

    if (bad && p.error) {
      const err = document.createElement("small");
      err.className = "switch-err";
      err.textContent = p.error;
      row.appendChild(err);
    }
    host.appendChild(row);
  }

  // ---- 提示（生效时机 + 对不上的名单项）----
  const parts = [`共 ${plugins.length} 个插件。取消勾选 = 停用（不加载、不初始化、也不算失败）。`];
  if (d.restart_required) {
    parts.push("⚠️ 有改动**还没生效**：需要重启服务（./run.sh serve）后才会应用。");
  } else {
    parts.push("当前设置与运行状态一致（无需重启）。");
  }
  if ((d.unmatched || []).length) {
    parts.push(`⚠️ 名单里有对不上任何插件的项（拼写错误？）：${d.unmatched.join(", ")}`);
  }
  hint.textContent = parts.join(" ");
  // 有"待重启 / 名单对不上"⇒ 警告色；否则交还样式表（不写主题色）
  hint.style.color = d.restart_required || (d.unmatched || []).length ? "var(--warn)" : "";
}

/** 保存启停设置（整体替换停用名单）。 */
export async function savePluginSwitch() {
  const saved = $("plugin-switch-saved");
  const d = state.pluginSwitch;
  if (!d) {
    toast(saved, "还没读到插件清单，无法保存。", true);
    return;
  }
  // 未勾选 = 停用 → 提交的就是停用名单（用后端给的 module_path，不自己拼）
  const off = [];
  document.querySelectorAll("#plugin-switch-list .switch-row").forEach((row) => {
    const cb = row.querySelector("input.switch-cb");
    if (cb && !cb.checked) off.push(row.dataset.modulePath);
  });
  try {
    const out = await api("PUT", "/api/v1/plugins/inactivated", {
      inactivated_plugins: off,
    });
    state.pluginSwitch = out;
    renderPluginSwitch();          // 用后端回来的真值重画（不假设自己写对了）
    toast(saved, `已保存：停用 ${off.length} 个插件。` +
      (out.restart_required ? "重启服务后生效。" : "（当前状态已一致）"));
  } catch (e) {
    toast(saved, `保存失败：${e.message}`, true);
  }
}


/* ==================== 插件升级 ====================
 * 照真 AstrBot 的 `POST /plugins/{plugin_id}/update`
 * （`dashboard/api/plugins.py:1143`；实现 `core/star/updater.py:225`）。
 *
 * 🔴 **数据来源与 `loadPluginSwitch` 同一份**（`state.pluginSwitch`）——
 *    另开一条 `/api/v1/plugins` 请求会漂（本项目踩过"两处同源数据各取一份"）。
 * 🔴 **代理不落盘**：每次请求从输入框读，留空 = 直连（与真实现一致）。
 * 🔴 **升级完必须重启**：宿主没有热重载。端点返回的 `restart_required` 恒为真，
 *    但文案不许含糊 —— 用户最容易误解成"点完就生效了"。
 */

/** 画「插件升级」列表：一行一个已加载插件 + 版本 + 升级按钮。 */
export function renderPluginUpdate() {
  const host = $("plugin-update-list");
  const hint = $("plugin-update-hint");
  const d = state.pluginSwitch;
  if (!host) return;
  host.innerHTML = "";
  if (!d) {
    // 没读到清单 ≠ 没有插件（那是误导性最强的失败）
    if (hint) {
      hint.style.color = "var(--danger)";
      hint.textContent = "还没读到插件清单（见上方「插件启停」的失败原因）。";
    }
    return;
  }
  const plugins = d.plugins || [];
  if (!plugins.length) {
    if (hint) {
      hint.style.color = "";
      hint.textContent = "当前没有已发现的插件。";
    }
    return;
  }

  plugins.forEach((p) => {
    /* 🔴 结构照 AstrBot 的 `InstalledPluginsTab` 卡片（逐层对齐它的类名语义）：
     *     卡片
     *       ├ card-text
     *       │   └ content-row
     *       │       ├ image-container → logo (64×64, radius 10)
     *       │       └ meta-group
     *       │           ├ title-row → title__text + version
     *       │           ├ chip 组（category / tags）
     *       │           └ desc（2 行截断）
     *       └ actions（右下按钮组）
     *   真实现用 Vuetify 组件；我们是零构建原生 DOM，所以**自己搭同样的层次**。
     */
    const card = document.createElement("div");
    card.className = "plugin-card";
    card.dataset.pluginId = p.id;

    const body = document.createElement("div");
    body.className = "plugin-card-text";

    const row = document.createElement("div");
    row.className = "plugin-content-row";

    // ---- 左：logo ----
    const imgWrap = document.createElement("div");
    imgWrap.className = "plugin-image-container";
    const logo = document.createElement("img");
    logo.className = "plugin-logo";
    logo.alt = "";
    logo.loading = "lazy";
    // `icon` 是 metadata 里的**相对文件名**，走我们的 icon 路由取。
    // 没有 icon / 取不到 ⇒ **不显示破图**，退化成首字母方块（真实现也用占位逻辑）。
    logo.onerror = () => {
      logo.remove();
      if (!imgWrap.querySelector(".plugin-logo-fallback")) {
        imgWrap.appendChild(_pluginLogoFallback(p));
      }
    };
    if (p.icon) {
      logo.src = `/api/v1/plugins/${encodeURIComponent(p.id)}/icon`;
      imgWrap.appendChild(logo);
    } else {
      imgWrap.appendChild(_pluginLogoFallback(p));
    }
    row.appendChild(imgWrap);

    // ---- 右：元信息 ----
    const meta = document.createElement("div");
    meta.className = "plugin-meta-group";

    const titleRow = document.createElement("div");
    titleRow.className = "plugin-title-row";
    const title = document.createElement("h4");
    title.className = "plugin-title-text";
    title.textContent = p.display_name || p.name || p.id;
    title.title = p.id;
    titleRow.appendChild(title);
    // 版本号：用户判断"有没有更新"的唯一依据，紧跟标题（真实现同位置）
    if (p.version) {
      const ver = document.createElement("span");
      ver.className = "plugin-version";
      // ⚠️ 有的插件 metadata 里**自带 `v` 前缀**（实测 `astrbot_plugin_nai_image`
      //    写的是 `version: v2.7.3`），硬拼就变成 `vv2.7.3`。
      //    这里只在缺 `v` 时补 —— 原值原样显示，不擅自改用户的版本号。
      const raw = String(p.version).trim();
      ver.textContent = /^v/i.test(raw) ? raw : `v${raw}`;
      titleRow.appendChild(ver);
    }
    meta.appendChild(titleRow);

    // chip 组：分类 + 标签（照真实现的 extension-chip-group）
    // 🔴 **两列布局必须截断标签**：`astrbot_plugin_memory_companion` 有 8 个标签，
    //    全画出来在窄卡片里会堆成 4~5 行、把卡片撑得一屏高（实测）。
    //    规则：分类 + 前 3 个标签 + `+N`。`+N` 是**纯信息**、不是按钮。
    const MAX_TAGS = 3;
    const chips = [];
    if (p.category) chips.push({ text: p.category, kind: "primary" });
    const allTags = (p.tags || []).map((t) => String(t));
    allTags.slice(0, MAX_TAGS).forEach((t) => chips.push({ text: t, kind: "default" }));
    // 加载失败的插件必须一眼看出来（不许和正常插件长得一样）——它**优先占位**，
    // 不能被标签截断规则挤掉：先算它，再回退标签数量。
    const overflow = Math.max(0, allTags.length - MAX_TAGS);
    if (overflow > 0) chips.push({ text: `+${overflow}`, kind: "more" });
    if (p.state === "failed") chips.push({ text: "加载失败", kind: "danger" });
    if (chips.length) {
      const chipGroup = document.createElement("div");
      chipGroup.className = "plugin-chip-group";
      chips.forEach((c) => {
        const chip = document.createElement("span");
        chip.className = "plugin-chip" + (c.kind === "default" ? "" : ` plugin-chip--${c.kind}`);
        chip.textContent = c.text;
        // `+N` 用 title 补全，鼠标悬停能看到被藏起来的标签（不额外占空间）
        if (c.kind === "more") {
          chip.title = allTags.slice(MAX_TAGS).join("、");
        }
        chipGroup.appendChild(chip);
      });
      meta.appendChild(chipGroup);
    }

    // 描述：2 行截断（真实现是 -webkit-line-clamp:2）
    if (p.desc) {
      const desc = document.createElement("p");
      desc.className = "plugin-desc";
      desc.textContent = p.desc;
      desc.title = p.desc;
      meta.appendChild(desc);
    }

    row.appendChild(meta);
    body.appendChild(row);
    card.appendChild(body);

    // ---- 底：按钮组（真实现放右下角）----
    const actions = document.createElement("div");
    actions.className = "plugin-actions";

    // 「详情」—— 已经有的能力，顺手放在卡片里（真实现也有"信息"入口）
    const detailBtn = document.createElement("button");
    detailBtn.type = "button";
    detailBtn.className = "plugin-act-btn";
    detailBtn.textContent = "详情";
    detailBtn.title = "看这个插件注册了什么";
    detailBtn.onclick = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      go(`plugins/${encodeURIComponent(p.id)}`);
    };
    actions.appendChild(detailBtn);

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "plugin-act-btn plugin-act-btn--primary plugin-update-btn";
    btn.textContent = "升级";
    btn.title = "从这个插件的仓库拉取最新版本";
    btn.disabled = p.state === "failed";
    btn.onclick = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      upgradePlugin(p, btn);
    };
    actions.appendChild(btn);

    card.appendChild(actions);
    host.appendChild(card);
  });

  if (hint) hint.style.color = "";
}

/** 没有 icon 时的占位：**首字母方块**（不用破图图标 —— 那看起来像坏了）。 */
export function _pluginLogoFallback(p) {
  const el = document.createElement("div");
  el.className = "plugin-logo-fallback";
  const nm = String(p.display_name || p.name || p.id || "?");
  // 中文取第一个字，英文取首字母大写
  el.textContent = nm.slice(0, 1).toUpperCase();
  return el;
}

/** 升级一个插件。`btn` 用来做"进行中"状态，避免重复点。 */
export async function upgradePlugin(p, btn) {
  const out = $("plugin-update-result");
  const proxyEl = $("plugin-update-proxy");
  const proxy = proxyEl ? proxyEl.value.trim() : "";
  const name = p.display_name || p.name || p.id;

  // 二次确认 —— 它会**改写插件源码目录**，不是纯只读操作
  const ok = confirm(
    `升级「${name}」？\n\n` +
      `会从它的仓库拉取最新版本并覆盖插件源码。\n` +
      `· 不会删除插件数据与已上传文件（files/ 等会被跳过）\n` +
      `· 升级后需要重启服务才生效\n` +
      (proxy ? `· 使用代理：${proxy}\n` : `· 直连（未填代理）\n`)
  );
  if (!ok) return;

  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "升级中…";
  if (out) {
    out.style.color = "";
    out.textContent = `正在升级「${name}」…（拉取仓库可能要几十秒，勿关页面）`;
  }
  try {
    const res = await api("POST", `/api/v1/plugins/${encodeURIComponent(p.id)}/update`, {
      proxy,
    });
    const r = (res && res.result) || {};
    const bits = [res.message || "升级完成"];
    if (r.new_version) bits.push(`新版本 ${r.old_version || "?"} → ${r.new_version}`);
    if ((r.preserved || []).length) {
      bits.push(`已保护用户数据：${r.preserved.join(", ")}`);
    }
    if ((r.kept_extra || []).length) {
      bits.push(`保留旧文件 ${r.kept_extra.length} 个（新版本里没有，未删除）`);
    }
    if (out) {
      out.style.color = "var(--warn)";
      out.textContent = bits.join(" · ") + " ⚠️ 请重启服务（./run.sh serve）后再验证。";
    }
    // 版本号可能变了 —— 重取清单（不自己改 DOM，避免和真实状态漂）
    await loadPluginSwitch();
  } catch (e) {
    // 🔴 失败要说清"下一步怎么办"，不能只报一句"失败"
    if (out) {
      out.style.color = "var(--danger)";
      out.textContent = `升级失败：${e.message}`;
    }
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

// ⚠️ 不在这里打猴子补丁（`renderPluginSwitch = ...`）——那会让"改了函数名
//    就静默失效"。升级列表由 `loadPluginSwitch()` **显式**调用 `renderPluginUpdate()`。


export async function loadPluginPages() {
  const ul = $("plugin-page-list");
  const hint = $("plugins-hint");
  try {
    const d = await api("GET", "/api/v1/plugins/pages");
    const pages = d.pages || [];
    state.pluginPages = pages;

    const badge = $("plugins-badge");
    if (pages.length) { badge.textContent = String(pages.length); badge.classList.remove("hidden"); }
    else badge.classList.add("hidden");

    if (!pages.length) {
      hint.textContent = "没有发现插件拓展页。可能原因：插件宿主没启用（EMOTION_PLUGINS_ENABLED），或插件没有声明 pages。";
      ul.innerHTML = "";
      return;
    }
    hint.textContent = `共 ${pages.length} 个插件拓展页，点一个打开：`;
    ul.innerHTML = "";
    for (const p of pages) {
      const li = document.createElement("li");
      li.tabIndex = 0;
      const t = document.createElement("span");
      t.textContent = p.title || p.page_name;
      const pid = document.createElement("span");
      pid.className = "pid";
      pid.textContent = p.plugin_id;
      li.append(t, pid);
      const openIt = () => go(`plugins/${encodeURIComponent(p.plugin_id)}/${encodeURIComponent(p.page_name)}`);
      li.onclick = openIt;
      li.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openIt(); } };
      ul.appendChild(li);
    }
  } catch (e) {
    hint.textContent = "读取插件页失败：" + e.message;
    hint.style.color = "var(--danger)";
    ul.innerHTML = "";
  }
}

/** 在 iframe 里打开一个插件拓展页。 */
export function openPluginPage(pluginId, pageName, opts) {
  const pages = state.pluginPages || [];
  const meta = pages.find((p) => p.plugin_id === pluginId && p.page_name === pageName);
  // 🔴 末尾必须有 `/` —— 插件页里的 `./app.js` 是按"当前 URL 的目录"解析的，
  //    少了斜杠会打到上一级去（后端对无斜杠的请求做了 308 重定向兜底）。
  const url = `/api/v1/plugins/${encodeURIComponent(pluginId)}/pages/${encodeURIComponent(pageName)}/`;

  $("plugin-frame-title").textContent = meta ? (meta.title || pageName) : pageName;
  $("plugin-frame-open").href = url;
  $("plugin-frame").src = url;
  state.pluginCurrentId = pluginId;
  showPluginsView("frame");
  // 🔴 **必须补这两句**：点侧边栏子项是**直接调**这个函数的（不经过 `applyRoute`），
  //    而侧边栏的「哪个子项被选中」是 `showPanel` 里算的 ⇒ 不补的话
  //    点侧边栏进来**选中态不会跟上**（真机判据 B2 抓到的：
  //    "选中子项 0 条，而父项点亮了" —— 父项是另一条路径恰好点亮的）。
  //    ⚠️ 顺序：先 `showPanel` 再 `go` —— `go` 会写 hash 触发 `applyRoute`，
  //       那条路径也会 `showPanel`，两边结果一致（幂等，不会闪）。
  showPanel("plugins");
  syncNavPluginActive();

  if (!(opts && opts.updateHash === false)) {
    go(`plugins/${encodeURIComponent(pluginId)}/${encodeURIComponent(pageName)}`);
  }
}

/* ==================== 插件详情 ====================
 *
 * 照真 AstrBot 的「插件详情页」（`GET /api/v1/plugins/by-id`，见
 * `docs/ASTRBOT_插件管理页_调研.md` §2）：它按
 * `page / skill / command / llm_tool / listener / hook` 六类渲染「插件行为」。
 *
 * 🔴 为什么这页值得存在（不是抄界面）：我们的插件是**跑在兼容层上的**，
 *    "某个插件到底注册上了什么"是我们最常要回答的问题 ——
 *    以前只能看 `handler_count` 一个**数字**，看不出"注册的是哪几条指令"。
 *    本项目最贵的 bug 类型是**静默失败**（插件 catch 成一行 info 日志），
 *    所以"逐条列出来"本身就是判据。
 *
 * ⚠️ 六类里 `skill` 我们**没有**对应体系 → 后端返回空数组，这里也如实不显示
 *    （绝不显示一个永远空的分类，那会让用户以为"坏了"）。
 */

/** 六类的标题与渲染顺序（照真实现 `PLUGIN_COMPONENT_TYPE_ORDER`）。 */
export const PLUGIN_COMPONENT_GROUPS = [
  ["page", "页面"],
  ["skill", "技能"],
  ["command", "指令/指令组"],
  ["llm_tool", "LLM 工具"],
  ["listener", "事件监听器"],
  ["hook", "钩子"],
];

/** 打开某个插件的详情（`#/plugins/<id>`）。 */
export async function openPluginDetail(pluginId, opts) {
  const wrap = $("plugin-detail-wrap");
  const body = $("plugin-detail-body");
  const hint = $("plugin-detail-hint");
  $("plugin-detail-title").textContent = pluginId;
  state.pluginCurrentId = pluginId;
  showPluginsView("detail");

  // 🔴 每次进入都**清空并说明正在读** —— 绝不保留上一个插件的残留内容
  //    （否则用户以为看到的是当前插件的，其实还没加载完）。
  body.innerHTML = "";
  hint.textContent = "正在读取…";
  hint.style.color = "";

  try {
    const d = await api("GET", `/api/v1/plugins/${encodeURIComponent(pluginId)}`);
    // ⚠️ 异步回来时用户可能已经切走/返回了 —— 迟到的响应**不许**改写当前视图
    if (state.pluginCurrentId !== pluginId) return;
    state.pluginDetail = d;
    $("plugin-detail-title").textContent = d.name || d.id || pluginId;
    renderPluginDetail(d);
    hint.textContent = "";
  } catch (e) {
    if (state.pluginCurrentId !== pluginId) return;
    hint.textContent = `读取失败：${e.message}`;
    hint.style.color = "var(--danger)";
    body.innerHTML = "";
  }

  if (!(opts && opts.updateHash === false)) {
    go(`plugins/${encodeURIComponent(pluginId)}`);
  }
}

/** 画一个「键: 值」小行（空值**如实显示**为「—」，不隐藏 —— 隐藏会让人以为字段不存在）。 */
export function detailRow(label, value, opts) {
  const row = document.createElement("div");
  row.className = "detail-row";
  const k = document.createElement("span");
  k.className = "detail-k";
  k.textContent = label;
  const v = document.createElement("span");
  v.className = "detail-v";
  if (opts && opts.mono) v.classList.add("mono");
  if (value === undefined || value === null || value === "") {
    v.textContent = "—";
    v.classList.add("muted");
  } else {
    v.textContent = String(value);
  }
  row.append(k, v);
  return row;
}

/** 渲染一个组件分类（`components` 里 type 相同的那批）。 */
export function renderComponentGroup(title, items) {
  const box = document.createElement("section");
  box.className = "detail-group";
  const h = document.createElement("h4");
  h.textContent = `${title}（${items.length}）`;
  box.appendChild(h);

  const list = document.createElement("div");
  list.className = "detail-items";
  for (const c of items) {
    const it = document.createElement("div");
    it.className = "detail-item";
    const head = document.createElement("div");
    head.className = "detail-item-head";
    const nm = document.createElement("strong");
    nm.textContent = c.name || c.cmd || "（无名）";
    head.appendChild(nm);
    // 类型徽标（`指令组` / `正则匹配` / `事件监听器` …）
    if (c.display_type) {
      const badge = document.createElement("span");
      badge.className = "detail-badge";
      badge.textContent = c.display_type;
      head.appendChild(badge);
    }
    if (c.has_admin) {
      const badge = document.createElement("span");
      badge.className = "detail-badge admin";
      badge.textContent = "需管理员";
      head.appendChild(badge);
    }
    it.appendChild(head);

    for (const [k, val] of [["指令", c.cmd], ["触发", c.event_type_h],
                            ["说明", c.description], ["处理函数", c.handler_name]]) {
      if (val === undefined || val === null || val === "") continue;
      it.appendChild(detailRow(k, val, { mono: k === "指令" || k === "处理函数" }));
    }
    // 子指令（指令组的树）—— 有才画，逐层缩进
    if (Array.isArray(c.subcommands) && c.subcommands.length) {
      const sub = document.createElement("div");
      sub.className = "detail-sub";
      for (const s of c.subcommands) {
        const line = document.createElement("div");
        line.className = "detail-sub-line";
        line.textContent = `↳ ${s.cmd || s.name || "?"}` +
          (s.description && s.description !== "无描述" ? ` — ${s.description}` : "");
        sub.appendChild(line);
      }
      it.appendChild(sub);
    }
    list.appendChild(it);
  }
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "hint";
    empty.textContent = "（没有）";
    list.appendChild(empty);
  }
  box.appendChild(list);
  return box;
}

/** 按 `GET /api/v1/plugins/<id>` 的响应画详情。 */
export function renderPluginDetail(d) {
  const body = $("plugin-detail-body");
  body.innerHTML = "";

  // ---- ① 概览 ----
  const info = document.createElement("section");
  info.className = "detail-group";
  const h = document.createElement("h4");
  h.textContent = "信息";
  info.appendChild(h);
  info.appendChild(detailRow("显示名", d.name));
  info.appendChild(detailRow("插件 ID", d.id, { mono: true }));
  info.appendChild(detailRow("模块路径", d.module_path, { mono: true }));
  info.appendChild(detailRow("版本", d.version));
  info.appendChild(detailRow("作者", d.author));
  info.appendChild(detailRow("简介", d.desc));
  info.appendChild(detailRow("状态", d.state_text || d.state));
  info.appendChild(detailRow("目录", d.dir_name, { mono: true }));
  if (d.error) info.appendChild(detailRow("错误", d.error));
  body.appendChild(info);

  // ---- ② 完整堆栈（失败时才有；**单独一块**，别塞进"错误"那一行） ----
  if (d.traceback) {
    const tb = document.createElement("section");
    tb.className = "detail-group";
    const th = document.createElement("h4");
    th.textContent = "完整堆栈";
    tb.appendChild(th);
    const pre = document.createElement("pre");
    pre.className = "detail-pre";
    pre.textContent = d.traceback;
    tb.appendChild(pre);
    body.appendChild(tb);
  }

  // ---- ③ 计数（一眼看清"注册上了多少东西"） ----
  const counts = document.createElement("section");
  counts.className = "detail-group";
  const ch = document.createElement("h4");
  ch.textContent = "注册数量";
  counts.appendChild(ch);
  const grid = document.createElement("div");
  grid.className = "detail-counts";
  for (const [label, val] of [
    ["事件处理器", d.handler_count],
    ["LLM 工具（已绑实例）", d.llm_tool_bind_count],
    ["Web API", d.web_api_count],
    ["定时任务", d.task_count],
  ]) {
    const cell = document.createElement("div");
    cell.className = "detail-count";
    const num = document.createElement("strong");
    num.textContent = val === undefined || val === null ? "—" : String(val);
    const lab = document.createElement("span");
    lab.textContent = label;
    cell.append(num, lab);
    grid.appendChild(cell);
  }
  counts.appendChild(grid);
  // ⚠️ 这个暗示必须写出来：工具数为 0 而插件"有工具"= 执行必错（本项目踩过）
  if (d.llm_tool_bind_count === 0) {
    const warn = document.createElement("p");
    warn.className = "hint";
    warn.textContent = "⚠️ 没有绑定任何 LLM 工具。若这个插件本该有工具，说明它注册的工具没有被绑上插件实例 —— 调用时必然失败。";
    counts.appendChild(warn);
  }
  body.appendChild(counts);

  // ---- ④ 插件行为（六类，按真实现的顺序） ----
  const comps = Array.isArray(d.components) ? d.components : [];
  const bh = document.createElement("h4");
  bh.className = "detail-section-title";
  bh.textContent = `插件行为（共 ${comps.length} 项）`;
  body.appendChild(bh);
  if (!comps.length) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "暂无行为 —— 这个插件没有注册指令、工具或事件监听器。";
    body.appendChild(empty);
  } else {
    for (const [type, title] of PLUGIN_COMPONENT_GROUPS) {
      const items = comps.filter((c) => c.type === type);
      // 🔴 空分类**不画**（除了我们确实支持的几类）：画一堆"（没有）"等于噪音
      if (!items.length) continue;
      body.appendChild(renderComponentGroup(title, items));
    }
  }

  // ---- ⑤ README（有才画） ----
  if (d.readme) {
    const rd = document.createElement("section");
    rd.className = "detail-group";
    const rh = document.createElement("h4");
    rh.textContent = "README" + (d.readme_truncated ? "（已截断）" : "");
    rd.appendChild(rh);
    const pre = document.createElement("pre");
    pre.className = "detail-pre readme";
    pre.textContent = d.readme;
    rd.appendChild(pre);
    body.appendChild(rd);
  }
}

/** 显示/隐藏「插件启停」那块（打开插件页/详情时让位）。 */
export function setPluginSwitchVisible(visible) {
  for (const id of ["plugin-switch-hint", "plugin-switch-list"]) {
    const el = $(id);
    if (el) el.classList.toggle("hidden", !visible);
  }
}

/**
 * 插件页的**三视图互斥** —— 唯一入口。
 *
 * 🔴 为什么必须收敛成一处（2026-09-27 修的真实缺陷）：
 *    以前"谁可见"散在 `openPluginPage()` 与 `closePluginPage()` 两处各写一半，
 *    `closePluginPage()` 只还原了列表/hint，**没还原成对的可见性状态**，
 *    而 `openPluginPage()` 里那句 `go(...)` 还会**再触发一次路由**
 *    → `applyRoute` → `openPluginPage(updateHash:false)` → 又走一遍显示逻辑。
 *    只要任何一条路径漏掉一半，用户就会看到"详情页没了"这种
 *    **没有任何报错**的空白（本项目最贵的 bug 类型）。
 *
 * 现在改成：**任何**切换都只调这一个函数，三种视图在一个地方被同时决定。
 * 新增视图时只改这里 —— 这比"记得在 N 个地方都补一行"可靠。
 *
 * @param {"list"|"frame"|"detail"|"config"} which
 */
export function showPluginsView(which) {
  // 🔴 四块主体：**互斥**。新增了 "config"（插件配置，情绪 2026-10-09）——
  //    正如这里的注释所说，新增视图**只改这一处**就够。
  $("plugin-frame-wrap").classList.toggle("hidden", which !== "frame");
  $("plugin-detail-wrap").classList.toggle("hidden", which !== "detail");
  $("plugin-config-wrap").classList.toggle("hidden", which !== "config");
  $("plugin-page-list").classList.toggle("hidden", which !== "list");
  $("plugins-hint").classList.toggle("hidden", which !== "list");
  // 启停那块只在**列表态**显示（其余状态整个面板让给主体）
  setPluginSwitchVisible(which === "list");

  if (which !== "frame") {
    // 清 src：否则插件页的定时器/连接继续在后台跑
    $("plugin-frame").src = "about:blank";
  }
  if (which !== "detail") {
    // 离开详情就丢掉缓存，并清 id —— 迟到的详情响应靠它判断"用户已经走了"
    state.pluginDetail = null;
  }
  if (which !== "config") {
    // 🔴 同理：离开配置页要把**已加载的配置**丢掉。
    //    不丢的话，下次打开另一个插件的配置页会先闪出上一个插件的值
    //    （而用户很可能就在那一瞬间点了保存 → **把 A 的配置存进 B**）。
    state.pluginConfig = null;
    // 🔴 同理清掉 file 值缓存（键路径跨插件重名，见 `openPluginConfig` 里的注释）
    state.pluginFileValues = {};
  }
  if (which === "list") state.pluginCurrentId = "";
}

/** 回到插件列表（唯一的"返回"实现）。 */
export function closePluginPage() {
  showPluginsView("list");
}


/* ================================================================================
 * 插件配置（情绪 2026-10-09：「有些插件没有内置的独立 webui 就可以打开插件的配置」）
 *
 * ## 数据形状（照真 AstrBot，见 `host/astrbot_host/host_app.py:plugin_config`）
 *
 *     { plugin_name, metadata: {<id>: {description, type:"object", items: <_conf_schema.json>}},
 *       config: {<当前值>}, i18n: {} }
 *
 * 🔴 **渲染器复用设置页那一套**（`buildConfigField` / `makeConfigInput`），
 *    不另写第二套 —— 本项目「同一概念两条写入路径」已踩 6 次。
 *    做法是加一个**适配器**把插件 schema 的项翻译成我们的字段格式，
 *    而不是复制渲染代码。
 *
 * 🔴 **我们只渲染能真正保存的控件**。插件 schema 里有几种类型我们**没有**对应控件
 *    （`list`/`dict`/`file`/`template_list`、以及各种 `_special`），
 *    对它们如实显示一句"这个插件项本面板暂不支持编辑，请直接改配置文件"，
 *    **不画一个存不进去的控件**（那会让用户以为改了会生效 —— 假能力）。
 * ================================================================================ */

/** 插件 schema 的 `type` → 我们 `makeConfigInput` 认识的类型。
 *
 * ⚠️ 认不出来时返回 `null`（调用方据此画"不支持"提示），**不硬塞成 string** ——
 *    把 `list` 当 string 画会显示成 "[object Object]" 或丢内容，
 *    而用户点保存就把**列表写成了字符串**（数据被改坏且不报错）。
 */
export function pluginFieldType(spec) {
  const t = spec && spec.type;
  if (t === "bool") return "bool";
  if (t === "int" || t === "float") return "string";   // 数字用文本输入（见下）
  if (t === "string" || t === "text") return "string";
  if (t === "object") return null;                     // 分组，单独递归
  return null;
}

/* ================================================================================
 * 插件的 `file` 型配置项（2026-10-09，照真 AstrBot 的 config-files）
 *
 * 情绪：「3 也可以做」—— 插件配置文件的列出/上传/删除。
 *
 * ## 真实现语义（`dashboard/api/plugins.py:762-814`）
 *   一个 `type: "file"` 的配置项，值是一个**路径数组**；文件真实落在
 *   **插件目录的 `files/<配置键路径>/`** 下。上传**只落盘、不写配置值** ——
 *   用户再在配置页把路径填进那个数组，两个动作是分开的。
 *   ⇒ 这个控件要同时呈现**两件事**：① 盘上有哪些文件 ② 配置值里引用了哪些。
 *      它们**可能不一致**（传了但没填 / 填了但文件被删），
 *      而"不一致"恰恰是用户最需要看见的 —— 所以下面显式标出来。
 *
 * ## 为什么值要自己维护在 state 里
 *   普通控件的值能从 DOM 直接读（`input.value`）。这个控件是"按钮 + 清单"，
 *   没有单一控件承载值 ⇒ 把**当前路径数组**存在
 *   `state.pluginFileValues[<键路径>]`，收集器从那里取（惰性初始化）。
 *   ⚠️ 不用 DOM 属性存：属性只能存字符串，来回 JSON 编解码容易出错且难查。
 * ================================================================================ */

/** 当前各 file 型配置项的值（键路径 → 路径数组）。收集器从这里读。
 *
 * ⚠️ 初始化**不能放在模块顶层**（原来那句 `if (!state.pluginFileValues) ...`
 *    是顶层立即执行，而本模块与 core.js 循环依赖 ⇒ 初始化时 `state` 还在 TDZ，
 *    实测报 `ReferenceError: Cannot access 'state' before initialization`）。
 *    改到第一次使用时惰性初始化。 */
function _ensurePluginFileValues() {
  if (!state.pluginFileValues) state.pluginFileValues = {};
  return state.pluginFileValues;
}

/** 画一个 `file` 型配置项。返回容纳它的元素（调用方会打 data 属性）。 */
export function buildPluginFileField(pluginId, key, spec, value, pathPrefix) {
  const path = pathPrefix ? `${pathPrefix}.${key}` : key;
  const box = document.createElement("div");
  box.className = "cfg-field plugin-cfg-file";

  const title = document.createElement("span");
  title.className = "cfg-label";
  title.textContent = spec.description || key;
  box.appendChild(title);

  if (spec.hint) {
    const h = document.createElement("small");
    h.className = "hint";
    h.textContent = spec.hint;
    box.appendChild(h);
  }

  // 值规整成字符串数组（插件声明 default 可能是 []、也可能缺失）
  const initial = Array.isArray(value) ? value.map((x) => String(x)) : [];
  _ensurePluginFileValues()[path] = initial.slice();

  const list = document.createElement("div");
  list.className = "plugin-file-list";
  box.appendChild(list);

  const actions = document.createElement("div");
  actions.className = "row plugin-file-actions";
  const pick = document.createElement("input");
  pick.type = "file";
  pick.multiple = true;
  // ⚠️ `accept` 用插件声明的 `file_types` 生成 —— 让系统文件框**先**过滤一道，
  //    但**不能**只靠它（那只是提示，用户仍可选"所有文件"）⇒ 服务端照样校验。
  const types = Array.isArray(spec.file_types) ? spec.file_types : [];
  if (types.length) pick.accept = types.map((t) => "." + String(t).replace(/^\./, "")).join(",");
  pick.className = "plugin-file-pick";
  const upBtn = document.createElement("button");
  upBtn.type = "button";
  upBtn.textContent = "上传";
  upBtn.className = "plugin-file-upload";
  const hint = document.createElement("small");
  hint.className = "hint plugin-file-hint";

  actions.appendChild(pick);
  actions.appendChild(upBtn);
  box.appendChild(actions);

  upBtn.onclick = async () => {
    const files = Array.from(pick.files || []);
    if (!files.length) {
      showHint(hint, "先选文件再点上传", true);
      return;
    }
    showHint(hint, "正在上传…");
    const fd = new FormData();
    for (const f of files) fd.append("file", f, f.name);
    try {
      const r = await fetch(
        `/api/v1/plugins/${encodeURIComponent(pluginId)}/config-files`
          + `?config_key=${encodeURIComponent(key)}`,
        { method: "POST", body: fd },
      );
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        // ⚠️ 后端逐条收集了错误（"5 张里这张不行"）→ **原样透出**，
        //    不简化成一句"上传失败"（那样用户得反复试）
        throw new Error((d.error && d.error.message) || `HTTP ${r.status}`);
      }
      const okN = (d.uploaded || []).length;
      const errs = d.errors || [];
      showHint(
        hint,
        `已上传 ${okN} 个${errs.length ? "；" + errs.join("；") : ""}`
          + "。⚠️ 上传只是把文件放进插件目录，**还要把它填进下面的列表并保存**才会生效。",
        errs.length > 0,
      );
      pick.value = "";
      await refreshPluginFileList(pluginId, key, path, list, hint, spec);
    } catch (e) {
      showHint(hint, "上传失败：" + e.message, true);
    }
  };

  box.appendChild(hint);
  // 首次渲染就去拉一次清单（异步；失败会在 hint 里说明）
  refreshPluginFileList(pluginId, key, path, list, hint, spec);
  return box;
}

/** 拉取"盘上有哪些文件"并重画清单。 */
export async function refreshPluginFileList(pluginId, key, path, list, hint, spec) {
  try {
    const d = await api(
      "GET",
      `/api/v1/plugins/${encodeURIComponent(pluginId)}/config-files`
        + `?config_key=${encodeURIComponent(key)}`,
    );
    renderPluginFileList(pluginId, key, path, list, hint, d.files || [], spec);
  } catch (e) {
    // 🔴 读不到**不能**显示成"没有文件" —— 那会让用户重复上传
    list.innerHTML = "";
    const p = document.createElement("small");
    p.className = "hint";
    p.textContent = `读取已上传文件失败：${e.message}`;
    list.appendChild(p);
  }
}

/** 重画清单：**盘上的文件** + **配置值引用了哪些**，并标出不一致。 */
export function renderPluginFileList(pluginId, key, path, list, hint, files, spec) {
  list.innerHTML = "";
  const referenced = _ensurePluginFileValues()[path] || [];

  if (!files.length) {
    const p = document.createElement("small");
    p.className = "hint";
    p.textContent = "（还没有上传任何文件）";
    list.appendChild(p);
  }

  for (const rel of files) {
    const row = document.createElement("div");
    row.className = "plugin-file-row";

    const name = document.createElement("span");
    name.className = "plugin-file-name";
    // ⚠️ 显示**文件名**而不是整条相对路径：路径里那段目录名（= 配置键）
    //    对用户没有信息量，还长到把按钮挤出去。
    name.textContent = rel.split("/").pop();
    name.title = rel;               // 完整路径放 title（悬停可见）
    row.appendChild(name);

    // 配置值里引用了它吗？—— 这是"传了但没填"最直观的提示
    const used = referenced.includes(rel);
    const badge = document.createElement("span");
    badge.className = "badge " + (used ? "ok" : "no-model");
    badge.textContent = used ? "已启用" : "未启用";
    badge.title = used
      ? "配置里引用了这个文件"
      : "文件已上传，但配置的列表里还没有它 —— 需要点下面的「加入列表」再保存";
    row.appendChild(badge);

    if (!used) {
      const add = document.createElement("button");
      add.type = "button";
      add.className = "plugin-file-add";
      add.textContent = "加入列表";
      add.onclick = () => {
        // ⚠️ 改的是**我们维护的那份值**，然后把控件重画一遍让徽标跟上。
        //    不直接改 DOM 文本 —— 那样两处状态会漂。
        _ensurePluginFileValues()[path] = (_ensurePluginFileValues()[path] || []).concat([rel]);
        renderPluginFileList(pluginId, key, path, list, hint, files, spec);
        showHint(hint, "已加入列表。⚠️ 还要点页面上的「保存」才会写进插件配置。");
      };
      row.appendChild(add);
    } else {
      const rm = document.createElement("button");
      rm.type = "button";
      rm.className = "plugin-file-remove";
      rm.textContent = "移出列表";
      rm.onclick = () => {
        _ensurePluginFileValues()[path] = (_ensurePluginFileValues()[path] || []).filter((x) => x !== rel);
        renderPluginFileList(pluginId, key, path, list, hint, files, spec);
        showHint(hint, "已移出列表。⚠️ 还要点「保存」才会写进插件配置。");
      };
      row.appendChild(rm);
    }

    const del = document.createElement("button");
    del.type = "button";
    del.className = "plugin-file-del";
    del.textContent = "删除文件";
    del.onclick = async () => {
      if (!confirm(`确定删除 ${rel}？此操作不可恢复。`)) return;
      try {
        await api("DELETE", `/api/v1/plugins/${encodeURIComponent(pluginId)}/config-files`, {
          key,
          path: rel,
        });
        // ⚠️ 列表里还引用着它 → 一并移掉，否则保存后插件读一个不存在的文件
        //    （那种失败在插件那边多半是一行日志 = 静默）
        _ensurePluginFileValues()[path] = (_ensurePluginFileValues()[path] || []).filter((x) => x !== rel);
        showHint(hint, `已删除 ${rel}（并已从列表移除）。⚠️ 还要点「保存」才会写进插件配置。`);
      } catch (e) {
        showHint(hint, "删除失败：" + e.message, true);
      }
      await refreshPluginFileList(pluginId, key, path, list, hint, spec);
    };
    row.appendChild(del);
    list.appendChild(row);
  }
}

/** 把一个插件 schema 项渲染成一个字段控件（复用 `buildConfigField`）。 */
export function buildPluginField(pluginId, key, spec, value, pathPrefix) {
  // 🔴 **`file` 型单独走一条路**（2026-10-09 补，照真 AstrBot 的 config-files）。
  //    它跟别的类型**根本不是同一类控件**：值是一个**路径数组**
  //    （`["files/<key>/x.png", ...]`），而界面要给的是
  //    **上传/删除按钮 + 已上传文件的清单**。
  //    ⇒ 早于 `pluginFieldType()` 判断 —— 那个函数对 `file` 返回 null，
  //      会在下面被当成"不支持"而画成一句"请直接改配置文件"
  //      （那正是本轮之前的行为）。
  if (spec && spec.type === "file") {
    const box = buildPluginFileField(pluginId, key, spec, value, pathPrefix);
    // ⚠️ 这两个 data 属性是**收集器认领控件的凭据**（`collectPluginConfigChanges`
    //    靠 `[data-plugin-key]` 找）。
    //    ⚠️ 但 file 型**不能**让收集器按普通控件去读 —— 它的值由
    //       `buildPluginFileField` 自己维护在 `state` 里（见那里的注释），
    //       所以这里打的是**另一个**标记 `data-plugin-file`，
    //       收集器会跳过它、改用专门的分支。
    if (box) {
      box.dataset.pluginKey = key;
      box.dataset.pluginPath = pathPrefix ? `${pathPrefix}.${key}` : key;
      box.dataset.pluginFile = "1";
    }
    return box;
  }
  const ourType = pluginFieldType(spec);
  if (ourType === null) {
    // 🔴 如实说"不支持"，**不画假控件**
    const wrap = document.createElement("label");
    wrap.className = "cfg-field plugin-cfg-unsupported";
    const title = document.createElement("span");
    title.className = "cfg-label";
    title.textContent = spec.description || key;
    wrap.appendChild(title);
    const h = document.createElement("small");
    h.className = "hint";
    const kind = spec.type === "object" ? "（分组，见下面各项）" : `（类型 ${spec.type || "未知"}）`;
    h.textContent = `本面板暂不支持编辑 ${kind}，请直接改插件的配置文件。`;
    wrap.appendChild(h);
    return wrap;
  }

  const field = {
    key,
    label: spec.description || key,
    type: ourType,
    hint: spec.hint || "",
    default: spec.default,
  };
  // 枚举：我们已有 `enum` 分支，但插件 schema 用的是 `options` + `labels`
  // （labels 下标与 options 配对，照真实现 `ConfigItemRenderer`）
  if (Array.isArray(spec.options) && spec.options.length) {
    field.type = "enum";
    const labels = Array.isArray(spec.labels) ? spec.labels : [];
    field.options = spec.options.map((o, i) => ({ value: o, label: labels[i] || String(o) }));
  }

  const wrap = buildConfigField("plugin", field, value);
  wrap.dataset.pluginKey = key;
  wrap.dataset.pluginPath = pathPrefix ? `${pathPrefix}.${key}` : key;
  return wrap;
}

/** 递归渲染一份插件 schema 段。
 *
 * ⚠️ 递归深度设上限：插件的 `_conf_schema.json` 是**外部数据**，
 *    有环或超深嵌套会让页面卡死（浏览器栈溢出）。
 */
export function renderPluginSchemaItems(host, pluginId, schema, values, pathPrefix, depth) {
  if (depth > 6) {
    const h = document.createElement("small");
    h.className = "hint";
    h.textContent = "（嵌套过深，本面板不再展开）";
    host.appendChild(h);
    return;
  }
  for (const [key, spec] of Object.entries(schema || {})) {
    if (!spec || typeof spec !== "object") continue;
    // 🔴 `invisible: true` 的项**不画**（插件自己标了"别在界面上显示"）
    if (spec.invisible) continue;

    if (spec.type === "object" && spec.items) {
      // 分组：画个小标题 + 递归
      const group = document.createElement("div");
      group.className = "plugin-cfg-group";
      const gt = document.createElement("h5");
      gt.textContent = spec.description || key;
      group.appendChild(gt);
      if (spec.hint) {
        const gh = document.createElement("small");
        gh.className = "hint";
        gh.textContent = spec.hint;
        group.appendChild(gh);
      }
      const inner = document.createElement("div");
      inner.className = "cfg-fields";
      // ⚠️ 嵌套值要**逐层下钻**：`values[key]` 可能不是对象（旧配置/类型不符），
      //    那就按空对象渲染（让用户看到可编辑的默认项），而不是整段不显示。
      const sub = values && typeof values[key] === "object" && values[key] !== null
        ? values[key] : {};
      renderPluginSchemaItems(inner, pluginId, spec.items, sub,
                              pathPrefix ? `${pathPrefix}.${key}` : key, depth + 1);
      group.appendChild(inner);
      host.appendChild(group);
    } else {
      host.appendChild(buildPluginField(pluginId, key, spec, values ? values[key] : undefined, pathPrefix));
    }
  }
}

/** 打开某个插件的配置页。 */
export async function openPluginConfig(pluginId, opts) {
  const keepHint = !!(opts && opts.keepHint);
  state.pluginCurrentId = pluginId;
  showPluginsView("config");
  const title = $("plugin-config-title");
  if (title) title.textContent = `插件配置 · ${pluginId}`;
  const hint = $("plugin-config-hint");
  const body = $("plugin-config-body");
  if (body) body.innerHTML = "";
  // 🔴 `keepHint`：保存成功后要把后端的提示**留住**。
  //    我第一版没这个参数，保存时 `toast(hint, message)` 之后紧接着调
  //    `openPluginConfig(id)` 重新拉数据 —— 而那个函数**第一件事就是**
  //    `toast(hint, "正在读取配置…")` 再 `toast(hint, "")`，
  //    于是那句"已保存，需要重启服务后生效"**存在不到一帧**、用户根本看不到。
  //    真机判据 C2 抓到了它（hint 读出来是空串）。
  if (!keepHint) toast(hint, "正在读取配置…");

  try {
    const d = await api("GET", `/api/v1/plugins/${encodeURIComponent(pluginId)}/config`);
    // 迟到的响应不要覆盖用户已经切走的视图（与本文件别处同款守卫）
    if (state.pluginCurrentId !== pluginId) return;
    state.pluginConfig = d;
    // 🔴 **每个插件开一次都要清空 file 值缓存** ——
    //    键路径（`path`）在不同插件之间是**重名**的（`openai_director_fallback_images`
    //    这类名字，两个插件都可能叫同一个）。
    //    不清的话，切插件后收集器会把**上一个插件**的路径数组当成这个插件的值提交，
    //    而界面上看着一切正常（本项目最忌的"静默用了别的东西"）。
    state.pluginFileValues = {};
    renderPluginConfig(d);
    if (!keepHint) toast(hint, "");
  } catch (e) {
    if (state.pluginCurrentId !== pluginId) return;
    // 🔴 失败要说清是"读不到"还是"没配置"，别留一片空白
    if (body) {
      body.innerHTML = "";
      const p = document.createElement("p");
      p.className = "hint";
      p.textContent = `读取插件配置失败：${e.message}`;
      body.appendChild(p);
    }
    toast(hint, `读取失败：${e.message}`, true);
  }
}

/** 把读到的配置画出来。 */
export function renderPluginConfig(d) {
  const body = $("plugin-config-body");
  if (!body) return;
  body.innerHTML = "";
  const pluginId = d.plugin_name;
  const md = d.metadata && d.metadata[pluginId];
  const save = $("plugin-config-save");

  if (!md || !md.items || !Object.keys(md.items).length) {
    // 🔴 三种"没有配置"要分清（用户的下一步动作完全不同）：
    //    ① 插件根本没有 schema（没写 _conf_schema.json）
    //    ② 有 schema 但内容为空
    //    ③ 宿主构造配置失败（那种情况后端会说 metadata=null 且日志里有 ERROR）
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = "这个插件没有声明任何配置项（它没有 _conf_schema.json，或内容为空）。";
    body.appendChild(p);
    // 没有配置项时**把保存按钮禁用**（点了也没东西可存）
    if (save) save.disabled = true;
    return;
  }
  if (save) save.disabled = false;

  const root = document.createElement("div");
  root.className = "cfg-fields";
  renderPluginSchemaItems(root, pluginId, md.items, d.config || {}, "", 0);
  body.appendChild(root);
}

/** 收集插件配置的**改动项**（只发改动 —— 与设置页同款口径）。
 *
 * ⚠️ 返回**扁平键**（`a.b.c` 路径），保存时后端按浅合并处理。
 *    我们的存储是 JSON，嵌套分组在键上就是嵌套 dict；
 *    但后端的 `save_config` 是**浅合并**（顶层键整体替换），
 *    所以嵌套分组的改动要**整组提交**（下面按顶层归并）。
 */
export function collectPluginConfigChanges() {
  const body = $("plugin-config-body");
  const loaded = (state.pluginConfig || {}).config || {};
  if (!body) return {};

  const changes = {};
  for (const wrap of body.querySelectorAll("[data-plugin-key]")) {
    const key = wrap.dataset.pluginKey;
    const path = wrap.dataset.pluginPath || key;

    // 🔴 **`file` 型没有单一控件承载值** —— 它是"按钮 + 清单"，
    //    值由 `buildPluginFileField` 维护在 `_ensurePluginFileValues()` 里。
    //    ⚠️ 这里必须**先于** `querySelector("input")` 判断：
    //       那个 file 选择框（`type=file`）也在里面，会被误当成值控件
    //       ⇒ 读出来是 `C:\fakepath\x.png` 这种垃圾，保存进配置后
    //         插件永远找不到文件，而**界面上看起来"保存成功"**。
    if (wrap.dataset.pluginFile === "1") {
      const v = _ensurePluginFileValues()[path] || [];
      const parts0 = path.split(".");
      let before0 = loaded;
      for (const seg of parts0) {
        if (before0 && typeof before0 === "object") before0 = before0[seg];
        else { before0 = undefined; break; }
      }
      if (JSON.stringify(v) === JSON.stringify(before0)) continue;
      if (parts0.length === 1) {
        changes[key] = v;
      } else {
        const top = parts0[0];
        const base = JSON.parse(JSON.stringify(loaded[top] ?? {}));
        let cur = base;
        for (let i = 1; i < parts0.length - 1; i += 1) {
          if (typeof cur[parts0[i]] !== "object" || cur[parts0[i]] === null) cur[parts0[i]] = {};
          cur = cur[parts0[i]];
        }
        cur[parts0[parts0.length - 1]] = v;
        changes[top] = base;
      }
      continue;
    }

    const ctrl = wrap.querySelector("input, select, textarea");
    if (!ctrl) continue;

    let v;
    if (ctrl.type === "checkbox") v = ctrl.checked;
    else if (ctrl.tagName === "SELECT") v = ctrl.value;
    else v = ctrl.value;

    // 与加载时的值比：**只有真改了才提交**
    const parts = path.split(".");
    let before = loaded;
    for (const seg of parts) {
      if (before && typeof before === "object") before = before[seg];
      else { before = undefined; break; }
    }
    if (JSON.stringify(v) === JSON.stringify(before)) continue;

    // ⚠️ 归并到**顶层键**（后端是浅合并：嵌套 dict 得整份给）
    if (parts.length === 1) {
      changes[key] = v;
    } else {
      // 深路径 → 从已加载的顶层那份**拷贝后**逐层写入，
      // 这样同组其它键不会丢（丢键是静默数据损失）
      const top = parts[0];
      if (!changes[top]) {
        const base = loaded[top];
        changes[top] = (base && typeof base === "object")
          ? JSON.parse(JSON.stringify(base)) : {};
      }
      let cur = changes[top];
      for (let i = 1; i < parts.length - 1; i++) {
        const seg = parts[i];
        if (!cur[seg] || typeof cur[seg] !== "object") cur[seg] = {};
        cur = cur[seg];
      }
      cur[parts[parts.length - 1]] = v;
    }
  }
  return changes;
}
