"use strict";
/* 判据：**子代理配置页**（`emotion_app/web/static/js/views/subagents.js`）。
 *
 * ## 为什么用 node:test + vm 而不是只靠真浏览器
 * 真浏览器（`tools/subagents_panel_check.py`）证明"用户真的点得动"，
 * 但它跑一次要几十秒、且依赖服务与令牌；而本文件守的是**几条最容易被改回去的逻辑**：
 *   · 打码令牌原样回传（洗掉令牌 = 全 401 但界面仍显示"已配置"，本项目踩过）
 *   · `fields[]` 驱动渲染（抄一份字段名 = 早晚与后端漂移）
 *   · 三态与三种空态**互不相同**（合并成一个布尔 = 用户盯着绿点发呆）
 * 这些都能在内存里精确断言，所以两层都要有：
 *   **这里守"逻辑与接线"，真浏览器守"真的看得见、点得动"。**
 *
 * ## 装载方式
 * 被测文件是原生 ES 模块（`import {…} from "../core/core.js"`），无法直接 `require`。
 * 这里把它当**源码**读进来，剥掉 `import` / `export` 关键字后丢进 `vm` 执行 ——
 * 于是它变成一组合法声明，可以逐个取出（与仓库里其它 `.cjs` 判据的做法一致：
 * 读源码、喂合成上下文、断言可观察行为，**不复制一份期望实现**）。
 *
 * ## 🔴 判据的独立性
 * 期望值全部**现造**（自己定义两套 `fields`、自己造 config），
 * 不从产品侧读取同一份数据 —— 否则"两边同源 ⇒ 永远绿"，证明不了任何事。
 *
 * 运行：
 *     node --test tests/test_subagents_page.cjs
 */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const STATIC = path.resolve(__dirname, "../emotion_app/web/static");
const VIEW_SOURCE = fs.readFileSync(path.join(STATIC, "js/views/subagents.js"), "utf8");

/* =========================================================================
 * 最小 DOM（只实现被测代码真正用到的那一小块 API）
 *
 * ⚠️ 刻意**不引 jsdom**：本机没有这个包（`require('jsdom')` → MODULE_NOT_FOUND），
 *    而为了一个判据去装依赖，会让"跑判据"这件事依赖网络与版本漂移。
 *    这里只实现 `createElement / append / appendChild / classList / dataset /
 *    querySelector(All) / innerHTML = "" / textContent` 这些。
 *
 * ⚠️  `querySelector` 只支持本判据用到的选择器形状（`[attr="v"]` 与 `.class`）——
 *    够用即可，**不支持时抛错**而不是静默返回 null：静默 null 会让断言
 *    "element 不存在"这件事变成假绿。
 * ========================================================================= */

function matches(el, selector) {
  const m = selector.match(/^\[([a-zA-Z0-9_-]+)="([^"]*)"\]$/);
  if (m) {
    const [, attr, want] = m;
    const key = attr.startsWith("data-")
      ? attr.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())
      : null;
    const got = key ? el.dataset[key] : el.getAttribute(attr);
    return String(got) === want;
  }
  if (selector.startsWith(".")) return el.classList.contains(selector.slice(1));
  // 裸 tag（`input` / `textarea` …）—— 产品代码与判据都用它数控件
  if (/^[a-zA-Z][a-zA-Z0-9]*$/.test(selector)) {
    return el.tagName === selector.toUpperCase();
  }
  throw new Error(`判据的假 DOM 不支持这个选择器：${selector}（要实现就显式补）`);
}

class El {
  constructor(tag) {
    this.tagName = String(tag || "").toUpperCase();
    this.children = [];
    this.parent = null;
    this.dataset = {};
    this.attributes = {};
    this.style = {};
    this.listeners = new Map();
    this._textContent = "";
    this._value = "";
    this._html = "";
    this._id = "";
    // 动态创建的节点也要能被 `getElementById` 找到（真实 DOM 就是这么工作的）——
    // 少了这一步，`collectBackendForm()` 会读不到控件值，判据就会假红。
    this.registry = null;
    this.checked = false;
    this.disabled = false;
    this.className = "";
    this.classList = {
      add: (...names) => {
        const set = new Set(this.className.split(/\s+/).filter(Boolean));
        names.forEach((n) => set.add(n));
        this.className = [...set].join(" ");
      },
      remove: (...names) => {
        const drop = new Set(names);
        this.className = this.className.split(/\s+/).filter((n) => n && !drop.has(n)).join(" ");
      },
      contains: (name) => this.className.split(/\s+/).includes(name),
      toggle: (name, force) => {
        const has = this.classList.contains(name);
        const want = force === undefined ? !has : !!force;
        if (want && !has) this.classList.add(name);
        if (!want && has) this.classList.remove(name);
        return want;
      },
    };
  }

  get id() { return this._id; }
  set id(value) {
    const next = String(value == null ? "" : value);
    if (this.registry && this._id) this.registry.delete(this._id);
    this._id = next;
    if (this.registry && next) this.registry.set(next, this);
  }

  /* ---- 属性 ---- */
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] === undefined ? null : this.attributes[name]; }
  removeAttribute(name) { delete this.attributes[name]; }

  /* ---- 文本 / HTML ---- */
  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent).join("");
    return this._textContent;
  }
  set textContent(value) {
    // 与浏览器一致：赋值会**清空所有子节点**
    this.children.forEach((c) => { c.parent = null; });
    this.children = [];
    this._textContent = String(value == null ? "" : value);
  }
  get innerHTML() { return this._html; }
  set innerHTML(value) {
    this.children.forEach((c) => { c.parent = null; });
    this.children = [];
    this._textContent = "";
    this._html = String(value == null ? "" : value);
  }

  /* ---- 值 ----
   * ⚠️ `<select>` 的 `.value` 在真实 DOM 里由 `selected` 的 `<option>` 推导；
   *    假 DOM 没有这层推导，所以读 `.value` 时**若没显式设过值**，
   *    就回落到"第一个 selected 的 option"（再不行取第一项）——
   *    少了这一步，"下拉没选中当前后端"这类真 bug 会被假绿。
   */
  get value() {
    if (this.tagName === "SELECT" && this._value === "") {
      const opts = this.querySelectorAll("option");
      const picked = opts.find((o) => o.selected);
      if (picked) return picked.value;
      if (opts.length) return opts[0].value;
    }
    return this._value;
  }
  set value(v) { this._value = String(v == null ? "" : v); }

  /* ---- 树 ---- */
  appendChild(child) {
    if (!child) throw new Error("appendChild 收到了 null（上游有 bug）");
    child.parent = this;
    this.children.push(child);
    return child;
  }
  append(...nodes) {
    for (const n of nodes) {
      if (typeof n === "string") {
        const t = new El("#text");
        t._textContent = n;
        this.appendChild(t);
      } else {
        this.appendChild(n);
      }
    }
  }
  contains(node) {
    if (node === this) return true;
    return this.children.some((c) => c.contains(node));
  }

  /* ---- 查询 ---- */
  querySelector(selector) {
    for (const c of this.children) {
      if (c.tagName !== "#TEXT" && matches(c, selector)) return c;
      const deep = c.querySelector ? c.querySelector(selector) : null;
      if (deep) return deep;
    }
    return null;
  }
  querySelectorAll(selector) {
    const out = [];
    const walk = (node) => {
      for (const c of node.children) {
        if (c.tagName === "#TEXT") continue;
        if (matches(c, selector)) out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }

  /* ---- 事件 ---- */
  addEventListener(type, fn) {
    this.listeners.set(type, [...(this.listeners.get(type) || []), fn]);
  }
  dispatch(type) {
    (this.listeners.get(type) || []).forEach((fn) => fn({ target: this }));
    // `el.onclick` 这类属性式绑定（bootstrap 里用的是这种）
    const inline = this[`on${type}`];
    if (typeof inline === "function") inline({ target: this });
  }
  focus() { this.focused = true; }

  /* ---- 由测试驱动的“用户输入” ----
   * ⚠️ 方法名不叫 `type()`：`<input>` 的 `.type` 是**属性**（"text"/"number"…），
   *    同名方法会把属性覆盖掉 —— 判据自己踩过一次（`nameInput.type is not a function`
   *    与"读不到 input.type"两种报错轮流出现）。 */
  typeText(value) {
    this.value = value;
    this.dispatch("input");
  }
  toggleChecked(checked) {
    this.checked = checked;
    this.dispatch("change");
  }
  click() { this.dispatch("click"); }
}

/** 页面里那些**不是 div** 的节点（其余都按 div 造）。
 *
 * ⚠️ 必须写清 tag：`select` 的 `.value` 推导、`input` 的 `.checked`
 *    都依赖真实 tag（假 DOM 把它们都当 div 时，"下拉没选中当前后端"这种
 *    真 bug 会被判成假绿 —— 本判据踩过）。
 */
const TAGS = {
  "subagent-backend": "select",
  "subagent-enabled": "input",
};

/** 造一个按 id 索引的假 document（被测代码全部通过 `$()` = getElementById 取节点）。 */
function makeDocument(ids) {
  //: id → 节点。**动态创建的节点也进这里**（真实浏览器里 `el.id = "x"` 之后
  //  `getElementById("x")` 就能拿到它 —— 少这一步判据会假红）。
  const byId = new Map();
  const create = (tag) => {
    const el = new El(tag);
    el.registry = byId;
    return el;
  };
  for (const id of ids) {
    const el = create(TAGS[id] || "div");
    el.id = id;
    byId.set(id, el);
  }
  return {
    byId,
    document: {
      getElementById: (id) => byId.get(id) || null,
      createElement: (tag) => create(tag),
      querySelectorAll: () => [],
      querySelector: () => null,
    },
    el: (id) => {
      const found = byId.get(id);
      if (!found) throw new Error(`判据要的节点不存在：${id}（HTML 里没有它？）`);
      return found;
    },
  };
}

/** 从 index.html 里读出真实存在的 id（**不手抄清单**，避免与页面漂移）。 */
function htmlIds() {
  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  const ids = new Set();
  for (const m of html.matchAll(/\sid="([^"]+)"/g)) ids.add(m[1]);
  return ids;
}

/* =========================================================================
 * 装载被测源码
 * ========================================================================= */

/** 需要页面提供的那些 id（从 index.html 里实际存在的 id 取交集）。 */
const NEEDED_IDS = [
  "subagents-hint",
  "subagent-enabled",
  "subagent-status",
  "subagent-backend",
  "subagent-backend-desc",
  "subagent-backend-fields",
  "subagent-test-result",
  "subagent-agent-list",
  "subagent-agents-empty",
  "subagent-agents-empty-title",
  "subagent-agents-empty-text",
  "subagent-save",
  "subagent-test",
  "subagent-reload",
  "subagent-add-agent",
];

test("页面骨架含齐被测代码要用的每一个节点", () => {
  const ids = htmlIds();
  const missing = NEEDED_IDS.filter((id) => !ids.has(id));
  assert.deepEqual(missing, [], `index.html 缺少这些 id：${missing.join(", ")}`);
});

/** 把 `subagents.js` 交给 vm 执行，返回它的导出表。
 *
 * `state` 与 `$` 由判据注入（产品代码里是从 core.js `import` 的），
 * 其余（`apiData` / `toast`）用可观测的桩，**不真的发请求**。
 */
function loadView({ state, document, apiData, toast }) {
  // 剥掉 import 语句（它们在 vm 里无法解析）；
  // 再把 `export ` 关键字去掉 —— 声明本身在 vm 里是合法的，
  // 于是可以直接从上下文里按名字取出函数/常量（**不复制一份实现**）。
  const stripped = VIEW_SOURCE
    .replace(/^import\s*\{[^}]*\}\s*from\s*"[^"]+";?$/gm, "")
    .replace(/^export\s+(async\s+)?function/gm, "$1function")
    .replace(/^export\s+const\s+/gm, "const ");

  const sandbox = {
    // 🔴 产品代码用的名字（`$` / `state` / `apiData` / `toast`）
    $: (id) => document.getElementById(id),
    state,
    apiData,
    toast,
    document,
    console,
    setTimeout,
    clearTimeout,
    Array,
    Object,
    Number,
    String,
    Boolean,
    JSON,
    Math,
  };
  vm.createContext(sandbox);
  vm.runInContext(stripped, sandbox, { filename: "views/subagents.js" });

  const names = [
    "subagentErrorText", "isMasked", "agentsOf", "fetchBackendTypes", "loadSubagents",
    "reloadSubagents", "backendMeta", "backendLabel", "currentBackendFields",
    "fieldSatisfied", "missingRequiredFields", "subagentStateView", "renderStatus",
    "onSubagentEnabledChange", "renderSubagentForm", "renderFormLocks", "renderBackendFields",
    "buildBackendField", "collectBackendForm", "collectSavePayload", "agentsEmptyView",
    "agentToolName", "agentToolPreview", "renderAgentList", "buildAgentRow",
    "collectAgentList", "addAgent", "removeAgent", "onSubagentBackendChange",
    "onSubagentParamsChanged", "clearTestResult", "saveSubagents", "testSubagentConnection",
  ];
  const out = {};
  for (const name of names) {
    if (typeof sandbox[name] === "undefined") {
      throw new Error(`subagents.js 没有导出/声明 ${name}（判据与实现漂移了？）`);
    }
    out[name] = sandbox[name];
  }
  return out;
}

/** 一套完整的页面 + 视图。 */
function setup({ apiData, toast, state: initial } = {}) {
  const { document, el } = makeDocument(NEEDED_IDS);
  const calls = [];
  const state = {
    subagentConfig: null,
    subagentBackendTypes: null,
    subagentFormFields: [],
    subagentTest: null,
    subagentError: "",
    ...(initial || {}),
  };
  const api = apiData || (async (method, pathname, body) => {
    calls.push({ method, pathname, body });
    return {};
  });
  const said = [];
  const view = loadView({
    state,
    document,
    apiData: api,
    toast: (node, msg, bad) => {
      said.push({ id: node ? node.id : "", msg: String(msg == null ? "" : msg), bad: !!bad });
      if (node) node.textContent = msg == null ? "" : String(msg);
    },
  });
  // 判据侧的两个小工具：
  //   __el(id)      → 按 id 取节点（等价于产品里的 `$`，但**断言用它更直白**）
  //   __wrapText(id)→ 取那个控件**所在的行**（`.field-labeled`）的文字，
  //                   用来验"这一行的说明/提示写对了没"
  view.__el = (id) => el(id);
  view.__wrapText = (id) => {
    let node = el(id);
    while (node && !(node.classList && node.classList.contains("field-labeled"))) node = node.parent;
    return node ? node.textContent : "";
  };
  return { view, state, el, calls, said };
}

/* =========================================================================
 * ① 打码令牌：**原样回传**，绝不被当成新令牌，也绝不被清空
 * ========================================================================= */

const HTTP_FIELDS = [
  { key: "endpoint", label: "接口地址", type: "string", required: true },
  { key: "token", label: "访问令牌（可选）", type: "secret", required: false },
  { key: "timeout_seconds", label: "超时（秒）", type: "int", required: false, default: 120 },
];

/** 一份"已经配过令牌"的后端配置（打码值 + `token_configured:true`）。 */
function configuredHttp(extra) {
  return {
    enabled: true,
    backend: "http",
    endpoint: "https://agent.example.test/run",
    token: "••••••••（43 字符）",
    token_configured: true,
    timeout_seconds: 120,
    agents: [],
    ...(extra || {}),
  };
}

function withHttpBackend(extra) {
  return setup({
    state: {
      subagentConfig: configuredHttp(extra),
      subagentBackendTypes: [
        { id: "null", label: "未接入（默认）", description: "", fields: [] },
        { id: "http", label: "通用 HTTP agent", description: "", fields: HTTP_FIELDS },
      ],
    },
  });
}

test("打码令牌原样回传：保存请求体里的 token 就是那个打码值", () => {
  const { view, calls } = withHttpBackend();
  view.renderSubagentForm();
  view.renderBackendFields();

  const payload = view.collectSavePayload();
  assert.equal(
    payload.token,
    "••••••••（43 字符）",
    "打码值必须**原样**回传（后端据此保留真值）；改写或清空都会毁掉令牌",
  );
  // 而且它确实被送进了请求（守"序列化逻辑"，不是只守中间对象）
  assert.equal(calls.length, 0, "组装请求体本身不该发请求");
});

test("用户没动令牌 → 保存后后端仍认为已配置（整条链路走一遍）", async () => {
  const puts = [];
  const { view, state } = withHttpBackend();
  // 让 PUT 的响应模拟后端行为：收到打码值 ⇒ 保持原值（configured 仍为 true）
  const apiData = async (method, pathname, body) => {
    puts.push({ method, pathname, body });
    if (method === "PUT") {
      return {
        config: {
          ...body,
          token: "••••••••（43 字符）",
          token_configured: true,     // 后端保留了真值
          agents: body.agents,
        },
        applied: true,
      };
    }
    throw new Error(`判据没预期这个请求：${method} ${pathname}`);
  };
  view.__setApiData && view.__setApiData(apiData);   // 不存在时忽略（见下）
  // 重新装载一次，注入会记录 PUT 的 apiData
  const second = setup({
    apiData,
    state: {
      subagentConfig: configuredHttp(),
      subagentBackendTypes: [
        { id: "null", label: "未接入（默认）", description: "", fields: [] },
        { id: "http", label: "通用 HTTP agent", description: "", fields: HTTP_FIELDS },
      ],
    },
  });
  second.view.renderSubagentForm();
  second.view.renderBackendFields();
  const ok = await second.view.saveSubagents();

  assert.equal(ok, true, "保存应当成功");
  assert.equal(puts.length, 1, "应当只发一次 PUT");
  assert.equal(puts[0].method, "PUT");
  assert.equal(puts[0].pathname, "/api/v1/subagents");
  assert.equal(puts[0].body.token, "••••••••（43 字符）", "PUT 里带的必须是打码值，不是「（空）」");
  assert.equal(
    second.state.subagentConfig.token_configured,
    true,
    "保存后仍显示已配置（若把打码值清空，这里会变 false ⇒ 令牌被洗掉）",
  );
  // 反证：这正是本项目踩过的形态 —— 值被清空就等于删掉令牌
  assert.notEqual(puts[0].body.token, "", "清空打码值 = 后端按「用户主动删除」处理");
});

test("字段类型：secret 用可见输入框（打码值要看得见），且不硬编码字段名", () => {
  const { view } = withHttpBackend();
  // 传两套**完全虚构**的字段定义：前端只按 type 画，不认任何具体 key
  const secretField = view.buildBackendField(
    { key: "whatever_secret", label: "某个密钥", type: "secret" },
    { whatever_secret: "••••••••（9 字符）", whatever_secret_configured: true },
  );
  const input = secretField.querySelector("input");
  assert.equal(input.tagName, "INPUT");
  assert.equal(
    input.type,
    "text",
    "secret 必须用 text 而不是 password：用户要能**看见**打码值才敢不动它",
  );
  assert.equal(input.value, "••••••••（9 字符）", "打码值要原样放进 value（提交时原样回传）");
  // 提示要说清"不动它 = 保持原值"，否则用户会以为要重填
  assert.match(
    secretField.textContent,
    /不动它 = 保持原值/,
    "secret 字段必须说明「不动 = 保持」，这与「留空 = 不修改」是两条不同的语义",
  );
});

test("留空 ≠ 保持原值：空串会被原样提交（提示文案不许骗人）", () => {
  // ⚠️ 必须走**渲染**这条路（`renderBackendFields` 才把字段表记进 state）；
  //    直接调 `buildBackendField` 拿不到 `collectBackendForm` 的字段清单 ——
  //    判据第一版就是这么写成假绿的。
  const { view } = withHttpBackend({ token: "", token_configured: false });
  view.renderSubagentForm();
  view.renderBackendFields();

  const input = view.__el("subagent-field-token");
  assert.equal(input.value, "", "没配令牌时输入框应当是空的");
  input.value = "";
  assert.equal(view.collectBackendForm().token, "", "空就是空 —— 后端据此删除令牌");

  // 未设置时**不能**写"留空 = 不修改"（那会让用户以为留空是安全的）
  const wrapText = view.__wrapText("subagent-field-token");
  assert.doesNotMatch(
    wrapText + (input.placeholder || ""),
    /留空 = 不修改/,
    "未设置时写「留空 = 不修改」是在骗人：空串真的会清掉令牌",
  );
});

/* =========================================================================
 * ② `fields[]` 驱动渲染：两套不同的 fields ⇒ 不同数量的控件
 * ========================================================================= */

/** 从 `#subagent-backend-fields` 里数出真正画出来的控件（**按 DOM 数，不按代码猜**）。 */
function countControls(host) {
  let n = 0;
  const walk = (node) => {
    for (const c of node.children) {
      if (c.tagName === "INPUT" || c.tagName === "SELECT" || c.tagName === "TEXTAREA") n += 1;
      walk(c);
    }
  };
  walk(host);
  return n;
}

test("fields[] 动态渲染：两套不同的 fields 渲染出**不同数量**的控件", () => {
  // 第一套：2 个字段
  const two = setup({
    state: {
      subagentConfig: { enabled: true, backend: "a", endpoint: "x", agents: [] },
      subagentBackendTypes: [{
        id: "a", label: "平台 A", description: "只有两项",
        fields: [
          { key: "endpoint", label: "地址", type: "string", required: true },
          { key: "limit", label: "上限", type: "int" },
        ],
      }],
    },
  });
  two.view.renderBackendFields();
  const n2 = countControls(two.el("subagent-backend-fields"));

  // 第二套：5 个字段（**不同的 key**，证明前端不认具体名字）
  const five = setup({
    state: {
      subagentConfig: {
        enabled: true, backend: "b", cmd: "/bin/x", args: [], dry_run: true,
        timeout: 30, secret_thing: "", agents: [],
      },
      subagentBackendTypes: [{
        id: "b", label: "平台 B", description: "有五项",
        fields: [
          { key: "cmd", label: "命令", type: "string", required: true },
          { key: "args", label: "参数", type: "string_list" },
          { key: "dry_run", label: "演练", type: "bool" },
          { key: "timeout", label: "超时", type: "int" },
          { key: "secret_thing", label: "密钥", type: "secret" },
        ],
      }],
    },
  });
  five.view.renderBackendFields();
  const n5 = countControls(five.el("subagent-backend-fields"));

  assert.equal(n2, 2, `平台 A 应渲染 2 个控件（实际 ${n2}）`);
  assert.equal(n5, 5, `平台 B 应渲染 5 个控件（实际 ${n5}）`);
  assert.notEqual(n2, n5, "两套不同的 fields 必须渲染出不同数量的控件（否则是硬编码）");
});

test("fields 为空的后端 → 明确说明「没有可配项」，不是一块空白", () => {
  const { view, el } = setup({
    state: {
      subagentConfig: { enabled: true, backend: "null", agents: [] },
      subagentBackendTypes: [{ id: "null", label: "未接入", description: "", fields: [] }],
    },
  });
  view.renderBackendFields();
  const host = el("subagent-backend-fields");
  assert.equal(countControls(host), 0, "无字段时不该画控件");
  assert.match(host.textContent, /没有需要填的连接参数/, "空字段区必须说清是哪一种空");
});

test("五种字段类型各画成对应控件（bool/string_list/int/secret/string）", () => {
  const { view } = setup();
  const kinds = [
    ["bool", "INPUT"],
    ["string_list", "TEXTAREA"],
    ["int", "INPUT"],
    ["secret", "INPUT"],
    ["string", "INPUT"],
  ];
  for (const [kind, tag] of kinds) {
    const wrap = view.buildBackendField({ key: `k_${kind}`, label: kind, type: kind }, {});
    const ctl = wrap.querySelector(tag === "TEXTAREA" ? "textarea" : "input");
    assert.equal(ctl.tagName, tag, `${kind} 应画成 ${tag}`);
  }
  // bool 走勾选框（不是文本框）
  const boolWrap = view.buildBackendField({ key: "flag", label: "开", type: "bool" }, { flag: true });
  assert.equal(boolWrap.querySelector("input").type, "checkbox", "bool 必须是勾选框");
  // int 走 number 输入（避免把 "abc" 当合法整数提交）
  const intWrap = view.buildBackendField({ key: "n", label: "数", type: "int" }, { n: 5 });
  assert.equal(intWrap.querySelector("input").type, "number", "int 必须是 number 输入");
});

/* =========================================================================
 * ③ 空态 / 状态：三种"空"与三种"态"必须互不相同
 * ========================================================================= */

test("空态三态互不相同：没读到 / 读取失败 / 读到了但没子代理", () => {
  const loading = setup({ state: { subagentConfig: null, subagentError: "" } });
  const failed = setup({ state: { subagentConfig: null, subagentError: "HTTP 500" } });
  const noBackend = setup({
    state: {
      subagentConfig: { enabled: false, backend: "null", agents: [] },
      subagentBackendTypes: [{ id: "null", label: "未接入", description: "", fields: [] }],
    },
  });
  const platformChosen = setup({
    state: {
      subagentConfig: { enabled: true, backend: "http", endpoint: "https://x", agents: [] },
      subagentBackendTypes: [{
        id: "http", label: "HTTP", description: "", fields: HTTP_FIELDS,
      }],
    },
  });

  const a = loading.view.agentsEmptyView();
  const b = failed.view.agentsEmptyView();
  const c = noBackend.view.agentsEmptyView();
  const d = platformChosen.view.agentsEmptyView();

  // 三种（其实是四种）空态**两两不同** —— 合并任意两种都会让用户找错方向
  const seen = new Map();
  for (const [label, view] of [["loading", a], ["failed", b], ["noBackend", c], ["platformChosen", d]]) {
    const key = `${view.title}|${view.text}`;
    assert.ok(!seen.has(key), `${label} 的空态与 ${seen.get(key)} 完全一样（用户分不清是哪一种空）`);
    seen.set(key, label);
  }
  // 各自说清"下一步做什么"
  assert.match(b.text, /重新读取/, "读取失败的空态要给出可执行的下一步");
  assert.match(c.text, /选一个平台/, "没选平台时要指向上面的平台选择");
  assert.match(d.text, /添加子代理/, "选了平台但没有 agent 时要指向「+ 添加子代理」");
});

test("配置没读到（null）≠ 读到了但空：前者不许显示成「你没配」", () => {
  const unknown = setup({ state: { subagentConfig: null, subagentError: "" } });
  const emptyButKnown = setup({
    state: {
      subagentConfig: { enabled: false, backend: "null", agents: [] },
      subagentBackendTypes: [{ id: "null", label: "未接入", description: "", fields: [] }],
    },
  });
  const s1 = unknown.view.subagentStateView();
  const s2 = emptyButKnown.view.subagentStateView();
  assert.equal(s1.key, "unknown", "没读到 = unknown（读取中）");
  assert.notEqual(s2.key, "unknown", "读到了就是读到了，不能还显示读取中");
  assert.notEqual(s1.text, s2.text, "两种状态的文案必须不同");
});

test("三态：未配置 / 已配置但未连通 / 已连通 —— 三者互不相同", () => {
  const types = [{
    id: "http", label: "HTTP", description: "", fields: HTTP_FIELDS,
  }];

  // ① 未配置：启用了但没选平台（或必填没填）
  const unconfigured = setup({
    state: {
      subagentConfig: { enabled: true, backend: "null", agents: [] },
      subagentBackendTypes: [{ id: "null", label: "未接入", description: "", fields: [] }, ...types],
    },
  });
  const v1 = unconfigured.view.subagentStateView();
  assert.equal(v1.key, "unconfigured");
  assert.equal(v1.text, "未配置");

  // ② 已配置但**未连通**：必填都填了，但测试失败
  const unreachable = setup({
    state: {
      subagentConfig: configuredHttp(),
      subagentBackendTypes: types,
      subagentTest: { ok: false, reason: "连不上子代理接口：ConnectError" },
    },
  });
  const v2 = unreachable.view.subagentStateView();
  assert.equal(v2.key, "unreachable");
  assert.match(v2.text, /未连通/, "配置齐了但测不通 —— 必须**明说**未连通");
  assert.equal(v2.tone, "bad", "未连通要用危险色（它是真问题，不是待办）");
  assert.match(v2.detail, /ConnectError/, "失败原因要能回看到（不能只写「未连通」）");

  // ③ 已连通
  const connected = setup({
    state: {
      subagentConfig: configuredHttp(),
      subagentBackendTypes: types,
      subagentTest: { ok: true, reason: "已连通，对方返回 12 个字符。" },
    },
  });
  const v3 = connected.view.subagentStateView();
  assert.equal(v3.key, "connected");
  assert.equal(v3.text, "已连通");
  assert.equal(v3.tone, "ok");

  // ④ 已配置但**还没测过** —— 与②必须分开（不能因为"没测过"就说"未连通"）
  const untested = setup({
    state: { subagentConfig: configuredHttp(), subagentBackendTypes: types },
  });
  const v4 = untested.view.subagentStateView();
  assert.equal(v4.key, "configured");
  assert.match(v4.text, /未测试/, "配全了但没测过时要如实说「未测试」，不能说已连通");

  // 四种文案 / key 两两不同
  const keys = [v1.key, v2.key, v3.key, v4.key];
  assert.equal(new Set(keys).size, 4, `四种状态必须互不相同，实际：${keys.join(", ")}`);
  const texts = [v1.text, v2.text, v3.text, v4.text];
  assert.equal(new Set(texts).size, 4, `四种状态文案必须互不相同，实际：${texts.join(" / ")}`);
});

test("「已配置」的判定从 fields[] 推（不硬编码 endpoint 之类的字段名）", () => {
  // 用**后端没声明过的字段名**造一套字段：如果前端写死了 `cfg.endpoint`，这里会判成"未配置"
  const types = [{
    id: "weird", label: "另类平台", description: "",
    fields: [{ key: "completely_different_key", label: "自定义地址", type: "string", required: true }],
  }];
  const filled = setup({
    state: {
      subagentConfig: {
        enabled: true, backend: "weird",
        completely_different_key: "https://ok.example.test", agents: [],
      },
      subagentBackendTypes: types,
    },
  });
  assert.equal(
    filled.view.subagentStateView().key,
    "configured",
    "必填填了就该是「已配置」—— 写死字段名的话这里会误判成未配置",
  );

  const empty = setup({
    state: {
      subagentConfig: { enabled: true, backend: "weird", completely_different_key: "", agents: [] },
      subagentBackendTypes: types,
    },
  });
  assert.equal(empty.view.subagentStateView().key, "incomplete", "必填空着就是没配全");
  assert.match(empty.view.subagentStateView().detail, /自定义地址/, "要说清缺的是哪一项");
});

test("secret 的「有没有配」看 <key>_configured，而不是打码值本身", () => {
  const secret = { key: "tok", label: "令牌", type: "secret", required: true };
  // 打码值 + configured=false（后端明确说没有）→ 没配
  assert.equal(
    (function () {
      const { view } = setup();
      return view.fieldSatisfied({ tok: "••••••••（9 字符）", tok_configured: false }, secret);
    })(),
    false,
    "后端说没配就是没配，不能被一个打码值骗成「已配置」",
  );
  // 有真值但没有 configured 布尔 → 兜底按"非空即已设置"
  assert.equal(
    (function () {
      const { view } = setup();
      return view.fieldSatisfied({ tok: "真令牌" }, secret);
    })(),
    true,
    "没有 configured 布尔时，非空值应算已设置",
  );
});

/* =========================================================================
 * ④ 连接测试：读 `data.ok`（业务失败是 HTTP 200），成败都如实显示
 * ========================================================================= */

test("测试连接读 data.ok：200 + ok:false 必须显示失败（不能当成功）", async () => {
  const { view, el } = setup({
    apiData: async () => ({
      ok: false, backend: "http",
      reason: "连不上子代理接口：ConnectError: All connection attempts failed",
    }),
    state: { subagentConfig: configuredHttp(), subagentBackendTypes: [] },
  });
  view.renderBackendFields();
  const ok = await view.testSubagentConnection();

  assert.equal(ok, false, "ok:false 必须返回 false（HTTP 状态码是 200，不能拿它判成败）");
  const hint = el("subagent-test-result");
  assert.equal(hint.dataset.tone, "bad", "失败要用危险色");
  assert.match(hint.textContent, /连不上子代理接口/, "后端的失败原因必须**原样显示**出来");
});

test("测试连接成功：显示成功原因与后端名（不是光秃秃一句「测试完成」）", async () => {
  const { view, el } = setup({
    apiData: async () => ({
      ok: true, backend: "http",
      reason: "已连通，对方返回 12 个字符。",
      detail: { elapsed_ms: 87, preview: "收到，这是一次连通性测试。" },
    }),
    state: { subagentConfig: configuredHttp(), subagentBackendTypes: [] },
  });
  view.renderBackendFields();
  const ok = await view.testSubagentConnection();

  assert.equal(ok, true);
  const text = el("subagent-test-result").textContent;
  assert.equal(el("subagent-test-result").dataset.tone, "ok");
  assert.match(text, /已连通，对方返回 12 个字符。/, "要显示后端给的原因");
  assert.match(text, /87 ms/, "延迟是有用信息，要带上");
  assert.match(text, /收到，这是一次连通性测试。/, "对方返回的样例要能看到");
});

test("测试请求本身失败（网络/鉴权）也要如实显示，并落到「未连通」", async () => {
  const { view, el, state } = setup({
    apiData: async () => {
      const e = new Error("未登录");
      e.status = 401;
      e.reason = "unauthorized";
      throw e;
    },
    state: {
      subagentConfig: configuredHttp(),
      subagentBackendTypes: [{
        id: "http", label: "HTTP", description: "", fields: HTTP_FIELDS,
      }],
    },
  });
  view.renderBackendFields();
  const ok = await view.testSubagentConnection();
  assert.equal(ok, false);
  assert.equal(el("subagent-test-result").dataset.tone, "bad");
  assert.match(el("subagent-test-result").textContent, /401|unauthorized/, "HTTP/原因都要露出来");
  assert.equal(state.subagentTest.ok, false, "测试结论要落到 state（徽标据此显示未连通）");
  assert.equal(view.subagentStateView().key, "unreachable");
});

test("参数一改，旧的测试结论作废（否则徽标会一直显示过期的「已连通」）", async () => {
  const { view, state, el } = setup({
    apiData: async () => ({ ok: true, backend: "http", reason: "已连通。" }),
    state: {
      subagentConfig: configuredHttp(),
      subagentBackendTypes: [{
        id: "http", label: "HTTP", description: "", fields: HTTP_FIELDS,
      }],
    },
  });
  view.renderBackendFields();
  await view.testSubagentConnection();
  assert.equal(view.subagentStateView().key, "connected");

  view.onSubagentParamsChanged();
  assert.equal(state.subagentTest, null, "改过参数，旧结论必须清掉");
  assert.equal(view.subagentStateView().key, "configured", "回到「已配置 · 未测试」");
  assert.match(el("subagent-test-result").textContent, /不再有效/, "要告诉用户为什么旧结论没了");
});

/* =========================================================================
 * ⑤ applied:false 必须显式告警
 * ========================================================================= */

test("保存成功但 applied:false → 明确告警「尚未生效」并带上原因", async () => {
  const { view, said } = setup({
    apiData: async () => ({
      config: configuredHttp(),
      applied: false,
      reason: "运行时应用器未装配（需要重启服务）",
    }),
    state: {
      subagentConfig: configuredHttp(),
      subagentBackendTypes: [{ id: "http", label: "HTTP", description: "", fields: HTTP_FIELDS }],
    },
  });
  view.renderSubagentForm();
  view.renderBackendFields();
  const ok = await view.saveSubagents();
  assert.equal(ok, true, "配置确实保存了（后端 200 + applied:false）");

  const last = said[said.length - 1];
  assert.equal(last.bad, true, "未生效是**要处理的**，必须用危险色，不能当成功提示");
  assert.match(last.msg, /尚未生效/, "必须显式说「保存了但没生效」");
  assert.match(last.msg, /运行时应用器未装配/, "后端给的原因要原样带出");
});

test("保存成功且 applied:true → 报「已保存并生效」并说清当前平台与数量", async () => {
  const { view, said } = setup({
    apiData: async () => ({
      config: configuredHttp({ agents: [{ name: "coder" }] }),
      applied: true,
    }),
    state: {
      subagentConfig: configuredHttp(),
      subagentBackendTypes: [{ id: "http", label: "通用 HTTP agent", description: "", fields: HTTP_FIELDS }],
    },
  });
  view.renderSubagentForm();
  view.renderBackendFields();
  await view.saveSubagents();
  const last = said[said.length - 1];
  assert.equal(last.bad, false);
  assert.match(last.msg, /已保存并生效/);
  assert.match(last.msg, /通用 HTTP agent/, "用的是平台**标签**，不是 id");
  assert.match(last.msg, /1 个子代理/);
});

test("配置没读到（null）时禁止保存 —— 否则会把配置覆盖成空", async () => {
  let putCount = 0;
  const { view, said } = setup({
    apiData: async () => { putCount += 1; return { config: {}, applied: true }; },
    state: { subagentConfig: null, subagentError: "HTTP 503" },
  });
  const ok = await view.saveSubagents();
  assert.equal(ok, false, "没读到配置就不许保存");
  assert.equal(putCount, 0, "**一个请求都不该发**（发了就等于把配置清空）");
  assert.equal(said[said.length - 1].bad, true);
  assert.match(said[said.length - 1].msg, /不能保存/);
});

test("配置没读到时机按钮是 disabled 的（不只是嘴上说）", () => {
  const { view, el } = setup({ state: { subagentConfig: null, subagentError: "boom" } });
  view.renderSubagentForm();
  for (const id of ["subagent-save", "subagent-test", "subagent-add-agent"]) {
    assert.equal(el(id).disabled, true, `${id} 应当被锁住`);
  }
  assert.equal(el("subagent-enabled").disabled, true, "总开关也要锁（否则改的是不存在的配置）");
});

/* =========================================================================
 * ⑥ 读取失败要落到提示区，不能白屏
 * ========================================================================= */

test("读取失败：状态徽标 = 读取失败、提示区有原因、按钮锁死、清单说明是哪一种空", async () => {
  const { view, el } = setup({
    apiData: async () => {
      const e = new Error("Not Found: /api/v1/subagents");
      e.status = 404;
      throw e;
    },
  });
  const ok = await view.loadSubagents();
  assert.equal(ok, false);

  const status = el("subagent-status");
  assert.equal(status.textContent, "读取失败");
  assert.equal(status.dataset.tone, "bad");
  assert.equal(status.dataset.state, "error");

  assert.match(el("subagents-hint").textContent, /Not Found/, "原因要露出来（HTTP 404 也不许吞）");
  assert.match(el("subagents-hint").textContent, /不能保存/, "还要告诉用户现在不能保存");

  assert.equal(el("subagent-save").disabled, true, "失败后必须锁住保存");
  assert.match(el("subagent-agents-empty-text").textContent, /读取失败|没读到/, "空态要说清是哪一种空");
});

test("读取成功：状态、下拉、字段都到位", async () => {
  const { view, el } = setup({
    apiData: async () => ({
      config: configuredHttp(),
      backend_types: [
        { id: "null", label: "未接入（默认）", description: "", fields: [] },
        { id: "http", label: "通用 HTTP agent", description: "把任务 POST 出去", fields: HTTP_FIELDS },
      ],
    }),
  });
  const ok = await view.loadSubagents();
  assert.equal(ok, true);
  assert.equal(el("subagent-enabled").checked, true);
  assert.equal(el("subagent-backend").value, "http", "下拉要选中当前后端");
  assert.match(el("subagent-backend-desc").textContent, /把任务 POST 出去/, "平台说明要显示");
  assert.equal(el("subagent-status").dataset.state, "configured");
  assert.equal(countControls(el("subagent-backend-fields")), 3, "http 的三个字段都要画出来");
});

test("响应形状不对（缺 config）→ 当成读取失败，不静默当空", async () => {
  const { view, el } = setup({ apiData: async () => ({ backend_types: [] }) });
  const ok = await view.loadSubagents();
  assert.equal(ok, false, "缺 config 必须算失败");
  assert.match(el("subagents-hint").textContent, /config/, "要说清是形状不符");
});

/* =========================================================================
 * ⑦ transfer_to_<name> 预览
 * ========================================================================= */

test("加一个子代理 → 出现 transfer_to_<名字> 预览，且改名字后跟着变", () => {
  const { view, el } = setup({
    state: {
      subagentConfig: configuredHttp({ agents: [] }),
      subagentBackendTypes: [{ id: "http", label: "HTTP", description: "", fields: HTTP_FIELDS }],
    },
  });
  view.renderAgentList();
  assert.match(
    el("subagent-agents-empty-text").textContent,
    /添加子代理/,
    "没有子代理时要说下一步",
  );

  view.addAgent();
  const list = el("subagent-agent-list");
  const cards = list.querySelectorAll(".subagent-agent-card");
  assert.equal(cards.length, 1, "应当出现一张卡片");

  const nameInput = cards[0].querySelector('[data-field="name"]');
  nameInput.typeText("coder");
  const preview = cards[0].querySelector(".subagent-agent-preview");
  assert.match(
    preview.textContent,
    /transfer_to_coder/,
    "名字一改，预览必须立刻跟着变（否则用户以为工具名没生效）",
  );

  // 收集回来的清单里也要是这个名字
  const agents = view.collectAgentList();
  assert.equal(agents.length, 1);
  assert.equal(agents[0].name, "coder");
  assert.equal(agents[0].enabled, true);
});

test("工具名（拼给模型看的那个）不带空白、前缀固定", () => {
  const { view } = setup();
  assert.equal(view.agentToolName("coder"), "transfer_to_coder");
  assert.equal(view.agentToolName("  coder  "), "transfer_to_coder", "前后空白要去掉");
  assert.match(view.agentToolPreview(""), /transfer_to_<名字>/, "没名字时给格式提示，不拼出半截工具名");
});

test("增删子代理不会丢掉其它卡片里正在编辑的文字", () => {
  const { view, el, state } = setup({
    state: {
      subagentConfig: configuredHttp({ agents: [{ name: "first", system_prompt: "原始" }] }),
      subagentBackendTypes: [{ id: "http", label: "HTTP", description: "", fields: HTTP_FIELDS }],
    },
  });
  view.renderAgentList();
  const list = el("subagent-agent-list");
  const firstTextarea = list.querySelector('[data-field="system_prompt"]');
  firstTextarea.value = "我刚刚打了还没保存的内容";

  view.addAgent();   // 这一步如果直接读 state，上面那行文字就丢了
  view.renderAgentList();
  const again = el("subagent-agent-list").querySelectorAll(".subagent-agent-card");
  assert.equal(again.length, 2, "应当有两张卡");
  assert.equal(
    again[0].querySelector('[data-field="system_prompt"]').value,
    "我刚刚打了还没保存的内容",
    "加一个空白 agent 不能把上一张卡未保存的编辑冲掉",
  );
  // 再验一次删除路径
  view.removeAgent(1);
  const after = el("subagent-agent-list").querySelectorAll(".subagent-agent-card");
  assert.equal(after.length, 1);
  assert.equal(
    after[0].querySelector('[data-field="system_prompt"]').value,
    "我刚刚打了还没保存的内容",
    "删除另一张卡也不能冲掉这张的编辑内容",
  );
  assert.equal(state.subagentConfig.agents.length, 1);
});

test("tools 一行一个：textarea 与数组互转（空行被丢掉）", () => {
  const { view, el } = setup({
    state: {
      subagentConfig: configuredHttp({
        agents: [{ name: "a", tools: ["shell", "read_file"] }],
      }),
      subagentBackendTypes: [{ id: "http", label: "HTTP", description: "", fields: HTTP_FIELDS }],
    },
  });
  view.renderAgentList();
  const card = el("subagent-agent-list").querySelector(".subagent-agent-card");
  const textarea = card.querySelector('[data-field="tools"]');
  assert.equal(textarea.tagName, "TEXTAREA", "tools 必须是 textarea（一行一个）");
  assert.equal(textarea.value, "shell\nread_file", "数组要按行展开进 textarea");

  // 用户改成带空行 / 带空白的形式 → 提交时按行拆分并去空
  textarea.value = "shell\n\n  read_file  \nbrowser\n";
  const agents = view.collectAgentList();
  // ⚠️ 不能直接 `deepEqual(agents[0].tools, [...])`：那些数组由 vm 里的 realm 创建，
  //    `deepEqual` 会按"原型不同"判不等（实测 `Values have same structure but are
  //    not reference-equal`）。先摊成本 realm 的数组再比 —— 判的是**内容**，
  //    顺手也证明了它是"真数组"而不是普通对象。
  const tools = Array.from(agents[0].tools);
  assert.ok(Array.isArray(agents[0].tools), "tools 必须是数组（不是字符串）");
  assert.deepEqual(
    tools,
    ["shell", "read_file", "browser"],
    "空白行要丢掉、每行首尾空白要去掉（否则会注册出带空格或空名的工具）",
  );
});

/* =========================================================================
 * ⑧ 事件绑定（bootstrap）与零模块级副作用
 * ========================================================================= */

test("subagents.js 没有任何模块级副作用（事件绑定只在 bootstrap）", () => {
  // 🔴 判据口径：**只在顶层（花括号深度 0）**找"非声明语句"。
  //    ⚠️ 第一版按"行的缩进/形态"猜，把函数体内部的 `if (...) {` 全当成顶层语句，
  //    一条真实现被判成 200+ 处违规（假红）。按花括号深度才算得准。
  // 允许的顶层形态：注释、import、以及 `export function` / `function` / `const` / `let`。
  const offenders = [];
  let depth = 0;
  let inBlockComment = false;
  let atLineStart = true;

  const source = VIEW_SOURCE;
  for (let i = 0; i < source.length; i += 1) {
    const ch = source[i];
    const next = source[i + 1];

    if (inBlockComment) {
      if (ch === "*" && next === "/") { inBlockComment = false; i += 1; }
      continue;
    }
    if (ch === "/" && next === "*") { inBlockComment = true; i += 1; continue; }
    if (ch === "/" && next === "/") {
      while (i < source.length && source[i] !== "\n") i += 1;
      atLineStart = true;
      continue;
    }
    if (ch === "'" || ch === '"' || ch === "`") {
      // 跳过字符串 / 模板字面量（里面的 `{` 不算深度）
      const quote = ch;
      i += 1;
      while (i < source.length) {
        if (source[i] === "\\") { i += 2; continue; }
        if (source[i] === quote) break;
        i += 1;
      }
      continue;
    }

    if (atLineStart && depth === 0 && /[^\s]/.test(ch)) {
      atLineStart = false;
      const lineNo = source.slice(0, i).split("\n").length;
      const line = source.slice(i).split("\n")[0].trim();
      const allowed = /^(import\s|export\s+(async\s+)?function|(async\s+)?function\s|const\s|let\s|var\s)/;
      if (!allowed.test(line)) offenders.push(`第 ${lineNo} 行：${line}`);
    }

    if (ch === "\n") { atLineStart = true; continue; }
    if (ch === "{" || ch === "(" || ch === "[") depth += 1;
    else if (ch === "}" || ch === ")" || ch === "]") depth = Math.max(0, depth - 1);
  }

  assert.deepEqual(
    offenders,
    [],
    `subagents.js 顶层出现了非声明语句（模块级副作用）：\n${offenders.join("\n")}`,
  );
});

test("bootstrap.js 绑定了本页每一个控件（漏一个 = 点了没反应且零报错）", () => {
  const bootstrap = fs.readFileSync(path.join(STATIC, "js/bootstrap.js"), "utf8");
  const bindings = [
    ['$("subagent-save")', "保存"],
    ['$("subagent-test")', "测试连接"],
    ['$("subagent-reload")', "重新读取"],
    ['$("subagent-backend")', "平台下拉"],
    ['$("subagent-enabled")', "总开关"],
    ['$("subagent-add-agent")', "添加子代理"],
    ['$("subagent-agent-list")', "移除（委托）"],
  ];
  const missing = bindings
    .filter(([needle]) => !bootstrap.includes(needle))
    .map(([, label]) => label);
  assert.deepEqual(missing, [], `bootstrap.js 没绑定这些控件：${missing.join("、")}`);
  // 字段区的「参数改动」委托也必须存在（否则徽标会一直显示过期结论）
  assert.match(
    bootstrap,
    /subagent-backend-fields"\)\.addEventListener/,
    "字段区要有 input/change 委托（参数改了要让旧测试结论作废）",
  );
});

test("core.js：NAV_LOADERS 有 subagents，且 loader 选「自己吞错」并写明理由", () => {
  const core = fs.readFileSync(path.join(STATIC, "js/core/core.js"), "utf8");
  assert.match(core, /subagents:\s*\(\)\s*=>\s*loadSubagents\(\)/, "NAV_LOADERS 缺 subagents");
  assert.match(core, /import\s*\{[^}]*loadSubagents[^}]*\}\s*from\s*"\.\.\/views\/subagents\.js"/);
  assert.match(core, /subagentConfig:\s*null/, "state.subagentConfig 初值必须是 null（= 还没读到）");
  assert.match(core, /subagentTest:\s*null/, "state.subagentTest 初值必须是 null（= 本次没测过）");
  // loader 的选择必须写清楚（否则下一个人不知道这是有意的还是漏了 try）
  const idx = core.indexOf("subagents: () => loadSubagents()");
  const context = core.slice(Math.max(0, idx - 900), idx);
  assert.match(context, /吞|不抛/, "loader 注释必须写明「自己吞错误、不抛」这个选择");
});

test("index.html：nav-item 结构与既有条目逐字一致（class/层级/nav-ico/nav-label）", () => {
  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  const m = html.match(
    /<a class="nav-item" href="#\/subagents" data-nav="subagents" data-nav-title="子代理">\s*<span class="nav-ico" aria-hidden="true">[^<]*<\/span><span class="nav-label">子代理<\/span>\s*<\/a>/,
  );
  assert.ok(m, "子代理的 nav-item 结构必须与其它条目逐字一致（class/层级/nav-ico/nav-label）");

  // 图标不许与已有条目重复（侧边栏收起态只剩图标，重复就分不出来）
  const icons = [...html.matchAll(/<span class="nav-ico" aria-hidden="true">([^<]*)<\/span>/g)].map((x) => x[1]);
  assert.equal(new Set(icons).size, icons.length, `侧边栏图标有重复：${icons.join(" ")}`);
});

test("index.html：面板是 .panel.hidden，且含字段区空容器（骨架里不许写死后端字段）", () => {
  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  assert.match(html, /<section id="tab-subagents" class="panel hidden">/);
  assert.match(html, /<div id="subagent-backend-fields"><\/div>/, "字段区必须是空容器（由 JS 渲染）");
  // 反向：骨架里不该出现后端专有字段名（抄进来就是纪律 ② 被破坏）
  const section = html.slice(html.indexOf('id="tab-subagents"'), html.indexOf('id="tab-adapters"'));
  for (const banned of ["timeout_seconds", "provider_id", "ws_reverse_host"]) {
    assert.ok(
      !new RegExp(`id="subagent-field-${banned}"`).test(section),
      `骨架里写死了后端字段 ${banned}（应当由 fields[] 渲染）`,
    );
  }
});
