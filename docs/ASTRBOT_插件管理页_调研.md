# 真 AstrBot「插件管理」页面调研（源码取证）


> 调研对象：本机只读源码副本
> `/root/.local/share/uv/tools/astrbot/lib/python3.12/site-packages/astrbot/`
> （包版本 `astrbot-4.27.5`；Dashboard 前端为**已构建产物** `dashboard/dist/assets/*.js`）。
> 调研方式：`grep -n` 定位 + 精读；对压缩产物用「**中文字符串与字段名原文照抄**」取证。
> **全程只读**，未修改任何文件、未启动服务、未向任何端口发请求、未访问 `/opt/astrbot/**`。
> 下文 `文件:行号` 均为实读；压缩产物若整文件只有 1 行，则只给文件名 + 字符串原文（并注明该文件只有 N 行）。

## 0. 先说三条会影响阅读的事实

1. **UI 文案不在插件那几个 JS 分片里。** 实测 CJK 字符数：
   `InstalledPluginsTab-DSnl1LUd.js`=0、`MarketPluginsTab-IGfL2ru-.js`=0、
   `PluginWorkspacePage-BWswqVUg.js`=0、`pluginI18n--LKOZc8C.js`=0、
   `PluginDetailPage-CULsaF9X.js`=22；`ExtensionPage-m-G1dJwH.js` 的 33237 个 CJK
   **是内置中文成语表，不是 UI 文案**。
   全部中文文案在**主包** `dashboard/dist/assets/index-BiTHkcB1.js`
   （CJK 27983，locale 表 `"zh-CN"` 起始偏移 **687122**，另有 `"en-US"`/`"ru-RU"`），
   命名空间变量 `Zge`，注册处原文：
   `oA={"zh-CN":{core:{...},features:{chat:Lge,extension:Zge,...}}}`。
   插件那一节在 `index-BiTHkcB1.js:24`（第 24 行，该文件共 1514 行）：
   `Tge="插件管理"`、`Dge="管理和配置系统插件"`、`Zge={title:Tge,subtitle:Dge,tabs:Age,...}`。
2. **API 路径也不在那几个分片里。** 全部在 `index-BiTHkcB1.js:16` 的生成式客户端
   （形如 `Sle=i=>((i==null?void 0:i.client)??we).get({...i,url:"/api/v1/plugins"})`）
   与方法表 `Cue={list(i){return Sle({query:i})},...}`（`index-BiTHkcB1.js:16`）。
3. **同一份表里有「声明了但从不引用」的键。** 判据：把 i18n 表里的键名当**带引号的字面串**
   去全量 `dist/assets/*.js` 里 grep。实测 0 命中的有：
   `"views.card"`、`"views.list"`、`"table.headers.name"`、
   `"buttons.enable"`、`"buttons.disable"`、`"buttons.configure"`、
   `"showSystemPlugins"`、`"hideSystemPlugins"`、`"table.headers.stars"`。
   （`"table.headers.actions"` 有 3 处命中 —— `ComponentsPage` / `ConversationPage` / `SessionManagementPage`；
   `"detail.handlerGroups.llm_tool"` 字面 0 命中，但那是**拼出来的键**，不是死键，见 §2.2。）
   → 所以**不能把 i18n 表当"功能清单"抄**；下面每条都注明它到底有没有被引用。

---

## 1. 插件列表页：字段清单 + 操作清单

### 1.1 结论

**每张卡片（默认是卡片视图）显示**：图标、显示名、版本、状态徽标（已打开/已关闭/系统）、
「有新版本」角标、启停开关、标签、支持平台、AstrBot 版本要求、简介。
**操作按钮**：置顶、文档、打开插件UI界面、配置、重载、⋯菜单（行为 / 更新到 x / 重新安装 / 更换插件源 / 删除插件）；
卡片本体点击进详情。页面级还有：搜索框、「已安装 / 插件市场」两个 tab、加载失败插件区块、
右下角两个悬浮按钮（安装插件 / 更新全部插件）。
→ **没有**独立的「查看日志」按钮（日志在配置弹窗内，见 §3.5）。

### 1.2 数据来源（后端给的字段就是全量清单）

`GET /api/v1/plugins`（`dashboard/api/plugins.py:678-689`）→
`PluginService.list_plugins()`（`dashboard/services/plugin_service.py:167-206`）。

每项的字段由 `serialize_plugin_base()` 固定，`plugin_service.py:622-659` **原文**：

```python
return {
    "name": plugin.name,
    "marketplace_name": (plugin.name or "").replace("_", "-"),
    "repo": "" if plugin.repo is None else str(plugin.repo),
    "author": plugin.author,
    "desc": plugin.desc,
    "version": plugin.version,
    "reserved": plugin.reserved,
    "activated": plugin.activated,
    "online_vesion": "",
    "display_name": plugin.display_name,
    "logo": logo_url,
    "support_platforms": plugin.support_platforms,
    "astrbot_version": plugin.astrbot_version,
    "installed_at": installed_at,
    "i18n": plugin.i18n,
    "root_dir_name": plugin.root_dir_name,
    "install_source": install_source,
    "updates_enabled": updates_enabled,
    "update_disabled_reason": "" if updates_enabled else PLUGIN_UPDATE_DISABLED_MESSAGE,
}
```

外加 `"pages": [page.name for page in pages]`（`plugin_service.py:203`）。

- ⚠️ `"online_vesion"` **是拼错的那个**（真实字符串如此），且**恒为空串**；
  真正被前端消费的是 `online_version`（前端自己算出来写进去的，见 §1.4）。
  实测 `"online_vesion"` 在全部 dist 产物里 **0 次**，`"online_version"` **5 次**。
- `installed_at` 来自插件目录 mtime（`plugin_service.py:309-323`），但实测
  `"installed_at"` 在前端产物里 **0 次** —— 后端给了，界面没显示。
- `root_dir_name` 同理：前端 **0 次**。
- `logo` 是 `/api/file/{token}`（`plugin_service.py:273-293`）。
- **幽灵插件被跳过**：`is_ghost_plugin()`（`plugin_service.py:610-619`），名字/作者/描述/版本/显示名全空则剔除（`:190-191`）。
- 响应信封：`_service_ok` → `ok(data, message)`；`data` 是数组，`message`（第二个元素）
  由 `list_plugins` 返回 `getattr(self.plugin_manager, "failed_plugin_info", None)`（`:206`）。
- **加载失败的插件不在这个数组里**，走另一个接口：`GET /api/v1/plugins/failed`（`api/plugins.py:424-429`）→
  `get_failed_plugins()` 返回 `plugin_manager.failed_plugin_dict`（`plugin_service.py:661-662`）；
  每条记录形状见 `core/star/star_manager.py:813-840`（`_build_failed_plugin_record`）：
  `{name, error, traceback, reserved, ...}` + 从 metadata.yaml 补 `author/desc/...`。

### 1.3 前端实际渲染的字段（含中文文案原文）

`InstalledPluginsTab-DSnl1LUd.js`（**该文件全文只有 1 行**，22,070 字节）中的 `ExtensionCard`：

| 界面元素 | 取值字段 | 证据 |
|---|---|---|
| 图标 | `extension.logo`，`onError` 时回退默认图标 | `n("img",{src:ie.value,alt:o.extension.name,class:"extension-logo",onError:…M.value=!0})` |
| 显示名 | `pluginI18n` 的 `pluginName()` → i18n `metadata.display_name` → `display_name` → `name` | `pluginI18n--LKOZc8C.js:1` `const e=(c=t==null?void 0:t.display_name)!=null&&c.length?…` |
| 显示名 tooltip | 显示名 ≠ name 时 `${display_name} (${name})` | `InstalledPluginsTab-DSnl1LUd.js:1` `text:(p=z.value)!=null&&p.length&&z.value!==o.extension.name?`${z.value} (${o.extension.name})`:o.extension.name` |
| 版本 | `extension.version`（渲染在标题旁的 `span`） | 同上 `o.extension.version?(…"span",Et,i(o.extension.version)…)` |
| 「系统」徽标 | `extension.reserved` → i18n `status.system` = **`系统`** | `Mge={…}` / `Oge={…,system:"系统",…}` |
| 启停状态徽标 | `extension.activated` → `status.loaded`=**`已打开`** / `status.stopped`=**`已关闭`**，图标 `mdi-check-circle` / `mdi-close-circle-outline` | 同上 |
| 「有新版本」角标 | `extension.has_update` + `extension.online_version`，tooltip = `card.status.hasUpdate`=**`有新版本可用`** + `": "+online_version` | 同上 |
| 启停开关 | `v-switch model-value=extension.activated`，`aria-label` = `buttons.stop`=**`关闭插件`** / `buttons.load`=**`打开插件`** | 同上 |
| 标签 | `extension.tags`，值为 `danger` 时文案 `tags.danger`=**`危险`**，颜色 error | `$ge={danger:"危险"}` |
| 支持平台 | `extension.support_platforms` → `PluginPlatformChip` | `n(kt,{platforms:se.value},…)` |
| AstrBot 版本要求 | `extension.astrbot_version` → 徽标 `" AstrBot: "+…` | 同上 |
| 简介 | `pluginI18n` 的 `pluginDesc()` → i18n `metadata.desc` → `desc`/`description` | `pluginI18n--LKOZc8C.js:1` |

字段清单（i18n 表里声明、且被引用的）原文：
`Bge={headers:{name:"名称",description:"描述",version:"版本",author:"作者",status:"状态",actions:"操作",stars:"Star数",lastUpdate:"最近更新",tags:"标签",eventType:"行为类型",specificType:"具体类型",trigger:"触发方式"}}`
—— ⚠️ 但 `"table.headers.name"` 等键实测 **0 次引用**，实际渲染用的是上面的硬编码布局；
只有 `"table.headers.version"`（2 次：`InstalledPluginsTab`、`ExtensionPage`）与
`"buttons.actions"`（1 次，失败插件表头）真的被用上。

**加载失败插件区块**（在列表页顶部，条件 `failedPluginItems.length>0`）：
`Ege={title:"加载失败插件（{count}）",hint:"这些插件加载失败，仍可尝试重载或直接删除。",columns:{plugin:"插件",error:"错误"}}`；
每行显示 `display_name` + `dir_name` + `error`（空则 `status.unknown`=**`未知`**）；
两个按钮：`buttons.reload`=**`重载`**、`buttons.uninstall`=**`删除`**（`disabled: s.reserved`）。
接口：`GET /api/v1/plugins/failed` + `POST /api/v1/plugins/failed/{plugin_id}/reload` + `DELETE /api/v1/plugins/failed/{plugin_id}`。

**空态**：`Vge={noPlugins:"暂无插件",noPluginsDesc:"尝试安装插件或调整搜索条件"}`。
**搜索**：`Pge={placeholder:"搜索插件...",marketPlaceholder:"搜索市场插件..."}` 经 `inputValue` 的 `n()` 做防抖。
**视图切换**：`Rge={card:"卡片视图",list:"列表视图"}` —— ⚠️ `"views.card"`/`"views.list"` 实测 **0 次引用**，这一版 UI **没有**卡片/列表切换（实测模板里只有 `mdi-alert-outline/mdi-magnify/mdi-refresh/mdi-shuffle-variant/mdi-source-branch` 这些图标，没有 view-grid/list 图标）。
**系统插件过滤**：`Mge.showSystemPlugins:"显示系统插件"` / `hideSystemPlugins:"隐藏系统插件"` —— ⚠️ 这一版在**插件列表页 0 次引用**（只在 `ComponentsPage-CoAT-rLp.js` 的「管理行为」页用了）。

### 1.4 「有新版本」是前端算的

`InstalledPluginsTab-DSnl1LUd.js:1`（同 `ExtensionPage-m-G1dJwH.js`）里的 `checkUpdate` 逻辑原文要点：

```js
h.online_version="", h.has_update=!1, h.update_market_plugin=null;
const r=h.install_source;
if(!h.updates_enabled||!r||r.implicit===!0||r.install_method!=="market")return;   // 只对"市场安装"的插件查
…
const Wi=/^v?\d+/.test(xi)&&/^v?\d+/.test(Yn)&&Yn!==u("status.unknown");
x.online_version=Yn, x.has_update=Wi&&(Y<0||Y===0&&xi.includes("-")&&!Yn.includes("-"))
```

即：**只有 `install_source.install_method==="market"` 且未被 `implicit` 标记的插件才比对版本**；
比对基准是市场源里的 `version`。这与后端的
`PLUGIN_UPDATE_DISABLED_MESSAGE = "该插件不是通过插件市场安装，无法检测或执行更新。"`
（`plugin_service.py:43`）是同一套规则。

### 1.5 操作按钮清单（含每个动作打的接口）

来自 i18n `Mge`（buttons）+ `Kge.card.actions` 原文，以及 `ExtensionCard` 的 `emits` 列表：

```js
emits:["configure","update","reload","uninstall","toggle-activation","view-handlers",
       "view-readme","view-changelog","toggle-pin","open-webui","change-source"]
```

| 界面文案（原文） | 键 | 条件 | 接口 |
|---|---|---|---|
| `置顶` / `取消置顶` | `buttons.pin` / `buttons.unpin` | 恒有 | **纯前端**：`localStorage`，键 `astrbot.pinnedExtensions`（`InstalledPluginsTab-DSnl1LUd.js:1` `$e="astrbot.pinnedExtensions"`） |
| `文档` | `buttons.viewDocs` | 恒有 | `GET /api/v1/plugins/readme?plugin_id=`（弹 `ReadmeDialog`） |
| `打开插件UI界面` | `buttons.openWebui` | 仅 `extension.pages.length>0`；`disabled = !extension.activated` | 路由跳转 `PluginPage`，见 §4 |
| `插件配置` | `card.actions.pluginConfig` | 恒有 | `GET /api/v1/plugins/config?plugin_id=` → 弹配置窗，见 §3 |
| `重载插件` | `card.actions.reloadPlugin` | 恒有 | `POST /api/v1/plugins/reload` body `{plugin_id}` |
| `行为`（⋯菜单） | `buttons.viewInfo` | 恒有 | 路由跳转 `ExtensionDetails` + `hash:"#plugin-components"` |
| `更新到 {online_version}` / `重新安装` | `card.actions.updateTo` / `reinstall` | ⋯菜单；`disabled: !U`（U=可更新） | `POST /api/v1/plugins/update` body `{plugin_id, proxy}` |
| `更换插件源` | `card.actions.changeSource` | ⋯菜单；`disabled: !E`（E=有 repo） | `POST /api/v1/plugins/{plugin_id}/source` |
| `删除插件` | `card.actions.uninstallPlugin` | ⋯菜单 | `DELETE /api/v1/plugins/by-id?plugin_id=`（走确认框） |
| 启停开关 | `buttons.load`=打开插件 / `buttons.stop`=关闭插件 | 恒有 | `PATCH /api/v1/plugins/enabled` body `{plugin_id, enabled}` |
| 卡片本体点击 | — | 有 `name` 即跳 | 路由 `ExtensionDetails`（`params:{pluginId:c.name}`） |
| `查看更新日志` | `Xge={menuTitle:"查看更新日志"}` | — | `GET /api/v1/plugins/changelog?plugin_id=` |

页面级悬浮按钮：
`market.installPlugin`=**`安装插件`**（打开安装对话框）、
`buttons.updateAll`=**`更新全部插件`**（`POST /api/v1/plugins/update` + body `{names:[…]}`，
见 `ExtensionPage-m-G1dJwH.js` 的 `he=async()=>{…const n=Ii.value.map(x=>x.name); …O.updateMany({names:n,proxy:w()})}`）。

**「更新全部」的真实语义**（`ExtensionPage-m-G1dJwH.js`）：
候选 = `Ii=V(()=>(Array.isArray(E?.data)?E.data:[]).filter(e=>e.has_update))`，
即**只更新 `has_update===true` 的那些**；候选为空时提示 `messages.noUpdatesAvailable`=**`当前没有可更新的插件`**；
先行弹确认框 `dialogs.updateAllConfirm`=**`确认更新全部插件` / `确定要更新全部 {count} 个插件吗？此操作可能需要一些时间。` / `确认更新`**。

**通知文案（原文摘录）**：`正在删除`、`正在刷新插件列表...`、`插件列表已刷新！`、
`刷新插件列表时发生错误`、`操作失败`、`重载成功`、`重载失败`、`更新成功!`、
`删除成功!`、`保存成功!`、`正在从文件安装插件`、`正在从链接安装插件...`、
`安装插件失败:`、`获取插件市场数据失败:`、`有新版本:`、`确定要删除插件吗？`、
`所有可更新的插件都已更新！`、`有 {failed}/{total} 个插件更新失败:`、
**`该插件缺少可用的安装源或仓库地址，无法更新或重新安装`**、
`当前插件缺少可用仓库地址，无法更换插件源`。
删除确认框：`dialogs.uninstall`=**`删除插件` / `你确定要删除当前插件吗？` /
`同时删除插件配置文件` / `同时删除插件持久化数据` /
`配置文件位于 data/config 目录` /
`删除 data/plugin_data、plugins_data 目录下的数据，以及数据库中插件的 KV 数据`**
—— 对应 body 字段 `delete_config` / `delete_data`（`dashboard/schemas.py:611-613` `PluginUninstallRequest`）。

### 1.6 后端「启停」到底是什么（不是改开关变量）

`PATCH /api/v1/plugins/enabled`（`api/plugins.py:850-863`）→
`PluginService.set_plugin_enabled()`（`plugin_service.py:1879-1894`）→
`plugin_manager.turn_on_plugin / turn_off_plugin`（`core/star/star_manager.py:2056-2074` / `:1980-2004`）：

- **停用**：调 `terminate()` → 把 `module_path` 追加进 shared_preferences 的
  `inactivated_plugins` → 该插件的 LLM 工具 `active=False` → `plugin.activated=False`。
- **启用**：从 `inactivated_plugins` 里移除 → 工具 `active` 复位 → **`await self.reload(plugin_name)`** → `activated=True`。
- 消息原文：`"启用成功。"` / `"停用成功。"`（`plugin_service.py:1885-1891`）。

→ 即**真实现的启停是立即生效的**（当场 reload），而**不是**"存盘等重启"。

---

## 2. 插件详情页：看到什么 + 数据从哪来

### 2.1 结论

详情页（路由 `ExtensionDetails`，路径 `plugins/:pluginId`，见 `index-BiTHkcB1.js:121`）
是一整页，不是抽屉。四块：

1. **标题区**：图标 + 显示名（`detail-title__current`）+ 简介（`plugin-summary-card__desc`）。
2. **插件行为**（`detail.contents`=**`插件行为`**，锚点 `id="plugin-components"`）：
   按 6 个分组渲染 —— **页面 / 技能 / 指令·指令组 / LLM 工具 / 事件监听器 / 钩子**。
3. **信息**（`detail.info.title`=**`信息`**）：版本、作者、类别、Star数、更新时间、标签、
   AstrBot 版本要求、支持平台、作者网站、仓库（空值自动不显示）。
4. **文档** + **更新日志**：Markdown 渲染。

### 2.2 详情接口

`GET /api/v1/plugins/by-id?plugin_id=`（`api/plugins.py:692-703`）或
`GET /api/v1/plugins/{plugin_id}`（`api/plugins.py:916-927`）→
`PluginService.get_plugin_detail()`（`plugin_service.py:223-256`）：

```python
return {
    **self.serialize_plugin_base(plugin, ...),          # 同列表页那 20 个字段
    "components": await self.get_plugin_components_info(plugin, serialize_pages),
}
```

`get_plugin_components_info()`（`plugin_service.py:664-677`）把三类拼起来并按类型排序
（顺序表 `PLUGIN_COMPONENT_TYPE_ORDER = {"page":0,"skill":1,"command":2,"llm_tool":3,"listener":4,"hook":5}`，`plugin_service.py:45-52`）：

```python
components = [
    *await self.get_plugin_page_components(plugin, serialize_pages),   # :679-697
    *self.get_plugin_skill_components(plugin),                        # :795-826
    *await self.get_plugin_handler_components(plugin.star_handler_full_names),  # :699-793
]
```

**各组件的字段**：

| type | 判据（源码） | 字段 |
|---|---|---|
| `page` | 有 `pages/` 页面（`:679-697`） | `{type:"page", name:<title>, title, page_name, i18n_key:"pages.<name>", description:"Plugin Page entry", plugin_name, plugin_marketplace_name}` |
| `skill` | `SkillManager().list_skills(...)` 里 `skill.source_type=="plugin"` 且 `plugin_name` 命中（`:795-826`） | `{type:"skill", name, description, path}` |
| `command` | `EventType.AdapterMessageEvent` + `CommandFilter`/`CommandGroupFilter`/`RegexFilter`（`:724-756`） | `{type:"command", name, description, display_type:"指令"/"指令组"/"正则匹配", cmd, command, event_type, event_type_h, handler_name, has_admin, subcommands?}` |
| `llm_tool` | `handler.event_type == EventType.OnCallingFuncToolEvent`（`:768-769`） | 基础式 `{type:"llm_tool", name:handler_name, description, event_type, event_type_h, handler_full_name, handler_name, cmd:"自动触发", display_type:"无"}` |
| `listener` | `AdapterMessageEvent` 但过滤器里没有 Command/Regex（`:762-764`） | `display_type:"事件监听器"` |
| `hook` | 其它事件类型 | 基础式 `{type:"hook", name, description}` + info |

- 描述缺省文案：**`无描述`**（`:719`、`:754`、`:775`、`:839`）。
- `cmd` 缺省：**`未知`**（`:761`）。
- 事件类型中文映射表在 `plugin_service.py:99-109` 原文：
  `平台消息下发时` / `LLM 请求时` / `LLM 响应后` / `Agent 开始运行时` / `Agent 运行完成后` / `回复消息前` / `函数工具` / `发送消息后` / `插件报错时`。
- 子指令树：`_build_command_group_component`（`:859-881`）递归 `sub_command_filters`；
  `_wrap_command_component`（`:905-914`）按命令名里的空格拆父级；
  同名指令会**合并**（`_merge_command_components` `:916-965`）。

**前端分组与图标**（`PluginDetailPage-CULsaF9X.js:1`，该文件只有 1 行）原文：

```js
I=["page","skill","command","llm_tool","listener","hook"],
Ie={page:"mdi-monitor-dashboard",skill:"mdi-lightning-bolt",command:"mdi-console-line",
    llm_tool:"mdi-tools",listener:"mdi-broadcast",hook:"mdi-hook"}
...
I.map(t=>({key:t,title:r(`detail.handlerGroups.${t}`),icon:Ie[t],components:e.get(t)||[]}))
```

分类文案在 i18n 里（键 `detail.handlerGroups`，`index-BiTHkcB1.js:24`）原文：
`{page:"页面",skill:"技能",command:"指令/指令组",llm_tool:"LLM 工具",listener:"事件监听器",hook:"钩子",commands:"指令/指令组",hooks:"钩子",functionTools:"函数工具",eventListeners:"事件监听器"}`
—— ⚠️ 这里有个**真实的存在不一致**：模板取的是 `detail.handlerGroups.llm_tool`，
而表里确实有 `llm_tool` 这个子键，所以 `"detail.handlerGroups.llm_tool"` 那个「0 次引用」是**正常现象**
（键是拼出来的，不是字面串）。我把它列在 §0.3 只是为了说明**"字面 grep 为 0" 不等于死键**。
其他几个（`buttons.enable` 等）是**完整字面串**，那才是真死键。
`PluginDetailPage-CULsaF9X.js` 里另有把中文 `event_type` 反查回 type 的逻辑原文：
`["指令","指令组","正则匹配"].includes(…)`、`t==="事件监听器"`、`s==="函数工具"`。
页面组件那一行还带一个按钮：`buttons.openPage`=**`打开`**（`prepend-icon:"mdi-open-in-new"`），
点击跳 `PluginPage`。另有「子指令」计数文案 `{count} 个子指令`、空态 `detail.noContents`=**`暂无行为`**。

### 2.3 README / 更新日志

**已安装插件**（`PluginDetailPage-CULsaF9X.js:1`）：
`const p=await te.readme(e.name); … const s=await te.changelog(e.name);`
→ 即 `GET /api/v1/plugins/readme?plugin_id=`（`api/plugins.py:813-822`）
与 `GET /api/v1/plugins/changelog?plugin_id=`（`api/plugins.py:825-834`）。

后端实现 `plugin_service.py:1924-1981`：

- README：**只找 `README.md`**（`:1930`）；没有 → `PluginServiceError(f"插件 {plugin_name} 没有README文件")`；
  成功 → `({"content": …}, "成功获取README内容")`。
- 更新日志：依次找 **`["CHANGELOG.md","changelog.md","CHANGELOG","changelog"]`**（`plugin_service.py:1960`）；
  都没有 → **不抛错**，返回 `({"content": None}, "该插件没有更新日志文件")`。

**市场插件**则不同：若市场条目里有 `readme_url`/`changelog_url`，
前端直接 `axios.get(url,{responseType:"text"})` 从**远端**拉文本（`PluginDetailPage-CULsaF9X.js:1` 的 `_e=async e=>{const t=await ot.get(e,{responseType:"text",…})…}`），
不走后端。渲染用 `markdown-it`（`html:!0,linkify:!0,typographer:!0,breaks:!1`，启用 `["table","strikethrough"]`）
+ DOMPurify 白名单（`ALLOWED_TAGS` 26 个 / `ALLOWED_ATTR` 7 个）。

**日志**：详情页里**没有**日志；日志在**配置弹窗**里（见 §3.5）。
数据源 `GET /api/v1/logs/history`（`api/logs.py:66-69`）+ SSE `GET /api/v1/logs/live`（`api/logs.py:74-78`，
用 `Last-Event-ID` 头续传）。
⚠️ 日志接口的 scope 是 **`system`**（`api/logs.py:20 require_system_scope`），**不是 `plugin`**。

### 2.4 侧栏入口

侧栏「插件UI」分组由 `ReadmeDialog-Dx33vi8d.js:1`（该文件 8 行）里的原文生成：

```js
const n=l.filter(a=>a.activated&&Array.isArray(a.pages)&&a.pages.length>0);
const u=a.pages[0];                       // ← 只取第一个页面
{title:p, icon:Nn, to:`/plugin-page/${encodeURIComponent(a.name)}/${encodeURIComponent(u)}`}
```

分组标题键 `In="core.navigation.pluginWebui"`，图标 `Nn="mdi-puzzle"`。
→ **侧栏只给每个插件第一个页面一个入口**；要打开第二个页面只能从详情页的 `页面` 分组。
i18n（`index-BiTHkcB1.js:22`）：`pluginWebui:Ffe`。

---

## 3. 插件配置怎么渲染 / 怎么存

### 3.1 结论（字段名收敛）

- schema 文件：**`_conf_schema.json`**，**放在插件源码目录下**。
- 读：`AstrBotConfig(config_path=<data/config>/<root_dir_name>_config.json, schema=<_conf_schema.json 的内容>)`。
- 后端下发字段名：**`metadata`**（schema 包成的表单结构）+ **`config`**（当前值）+ **`i18n`** + **`log_level`** + **`plugin_name`**。
- 保存字段名：**`config`**。
- 前端渲染器：`AstrBotConfig` 组件（`ProviderChatCompletionPanel-VG8ITzfN.js` 里的 `Ed={__name:"AstrBotConfig",...}`）。

### 3.2 schema 从哪来

`core/star/star_manager.py:210`：
```python
self.conf_schema_fname = "_conf_schema.json"
""""插件配置 Schema 文件名"""
self.logo_fname = "logo.png"
```
加载处 `star_manager.py:1151-1165`：
```python
# 检查 _conf_schema.json
plugin_config = None
plugin_schema_path = os.path.join(plugin_dir_path, self.conf_schema_fname)
if os.path.exists(plugin_schema_path):
    plugin_config = AstrBotConfig(
        config_path=os.path.join(self.plugin_config_path, f"{root_dir_name}_config.json"),
        schema=self._load_plugin_config_schema(plugin_schema_path),
    )
```
- `_load_plugin_config_schema()`（`star_manager.py:603-...`）用 `encoding="utf-8-sig"` 读，**允许 BOM**；
  非 UTF-8 → `插件配置 schema 必须使用 UTF-8 编码: {path}`；
  非合法 JSON → `插件配置 schema 不是有效的 JSON: {path} (line L, column C)`。
- `AstrBotConfig` 真签名（`core/config/astrbot_config.py:43-48`）：
  `def __init__(self, config_path: str = ASTRBOT_CONFIG_PATH, default_config: dict = DEFAULT_CONFIG, schema: dict | None = None)`；
  `:61-62`：`if schema is not None: default_config = self._config_schema_to_default_config(schema)`
  —— **传了 schema 就从 schema 推默认值**，默认值由 `schema` 各字段的 `default` 决定。
  ⚠️ 这个"参数名必须对"的坑本项目 §"插件配置存不下来"记过一次（`config=` 会 TypeError）。
- `metadata.pages`（拓展页）与 `_conf_schema.json` **无关**，见 §4.1。

### 3.3 拉取配置的接口与返回结构

- `GET /api/v1/plugins/config?plugin_id=`（`api/plugins.py:720-732`）
- `GET /api/v1/plugins/{plugin_id}/config`（`api/plugins.py:970-982`，等价）
- `GET /api/v1/plugins/config/schema?plugin_id=`（`api/plugins.py:753-759`）
- `GET /api/v1/plugins/{plugin_id}/config/schema`（`api/plugins.py:1020-1026`，等价）

三个"取配置"路由返回的是**同一个东西**（`ConfigDisplayService.get_configs`），原文：

```python
return ok({
    "plugin_name": plugin_id,
    "log_level": LogManager.get_plugin_log_level(plugin_id),
    **await service.get_configs(plugin_id),      # {"metadata":…, "config":…, "i18n":…}
})
```

`ConfigDisplayService.get_plugin_config()`（`dashboard/services/config_service.py:844-863`）**原文**：

```python
result: dict = {"metadata": None, "config": None, "i18n": {}}
for plugin_md in star_registry:
    if plugin_md.name != plugin_name: continue
    if not plugin_md.config: break
    result["config"] = plugin_md.config
    result["metadata"] = {
        plugin_name: {
            "description": f"{plugin_name} 配置",
            "type": "object",
            "items": plugin_md.config.schema,
        },
    }
    result["i18n"] = plugin_md.i18n
    break
```

→ 表单结构就是 `{"<plugin_name>": {description, type:"object", items:<_conf_schema.json>}}`。
**没有配置的插件**：`metadata` 为 `None`，前端渲染 `dialogs.config.noConfig`=**`这个插件没有其他配置项`**。

### 3.4 保存链路

- `PUT /api/v1/plugins/config`（`api/plugins.py:735-750`）：body 里的字段名就是 **`config`**
  （`schemas.py:591-592` `PluginConfigUpdateRequest(PluginByIdRequest): config: dict | None`）。
- `PUT /api/v1/plugins/{plugin_id}/config`（`api/plugins.py:1002-1017`）：`config = body.get("config"); config = config if isinstance(config, dict) else body`
  —— 即**带 `config` 包装或直接把配置对象当 body 都收**（旧前端兼容）。
- 落到 `ConfigFileService.save_plugin_configs()`（`config_service.py:989-1008`）**原文**：

```python
metadata = self.get_plugin_metadata_by_name(plugin_name)
if not metadata:               raise ValueError(f"插件 {plugin_name} 不存在")
if not metadata.config:        raise ValueError(f"插件 {plugin_name} 没有注册配置")
errors, post_configs = validate_config(post_configs, getattr(metadata.config, "schema", {}), is_core=False)
if errors:                     raise ValueError(f"格式校验未通过: {errors}")
metadata.config.save_config(post_configs)
await self.core_lifecycle.plugin_manager.reload(plugin_name)     # ← 保存即热重载
```

- 成功文案（`config_service.py:1010-1018`）原文：
  **`保存插件 {plugin_name} 成功~ 机器人正在热重载插件。`**
- `validate_config()`（`config_service.py:205-…`）：按 schema 逐项校验；
  `value is None` 时用 `DEFAULT_VALUE_MAP[meta["type"]]` 兜底（`:216-218`）；
  `int`/`float` 会尝试 `try_cast`（`:266-274`）；`object` 递归校验 `meta["items"]`（`:263-264`）；
  未知键**跳过不报错**（`:210-211`）。
- 落盘位置：`<data>/config/<root_dir_name>_config.json`（`star_manager.py:1160-1163`）。

### 3.5 前端渲染（含"日志级别"这一块）

`ExtensionPage-m-G1dJwH.js`（4 行，370,866 字节）第 4 行原文：

```js
l(du,{metadata:g(gn).metadata,iterable:g(gn).config,metadataKey:g(ui),
      pluginName:g(ui),pluginI18n:g(gn).i18n,"enable-default-reset":""})
```

`du` = `import{A as du}from"./ProviderChatCompletionPanel-VG8ITzfN.js"`，
而该文件里 `export{ft as A,…}`、`ft=Ie(Ed,…)`、`Ed={__name:"AstrBotConfig",props:{metadata,iterable,metadataKey,pluginName,pluginI18n,pathPrefix,isEditing,enableDefaultReset}…}`。
→ **确认：插件配置表单 = 与「模型供应商」页共用同一个 `AstrBotConfig` schema 渲染器**，
`enable-default-reset` 打开"恢复默认值"能力。

配置弹窗里除表单外还有**「日志级别」下拉**，文案原文：
`dialogs.config.coreSettings` = `{logLevel:"日志级别", logLevelHint:"仅影响该插件的日志输出，立即生效，无需重启", followGlobal:"跟随全局"}`
→ 接口 `PUT /api/v1/plugins/{plugin_id}/log-level`（`api/plugins.py:985-999`），
body `{level}`（`PluginLogLevelPayload`，`:599-601`，`null` = 跟随全局）；
成功文案 `Log level of plugin {plugin_id} set to {LEVEL}.`，前端 toast `日志级别已更新并实时生效`。
弹窗还内嵌**日志窗**：`l(yu,{historyNum:"10",style:{height:"200px",…}})`，`yu` = `ConsoleDisplayer`。
标题 `dialogs.config.title`=**`插件配置`**；按钮 `buttons.saveAndClose`=**`保存并关闭`** / `buttons.close`=**`关闭`**。

### 3.6 配置项里的「文件」类型

schema 项 `type == "file"` 有专门的三个接口（`api/plugins.py:762-810` 与 `:1029-1077` 两套等价路由）：

- `GET /api/v1/plugins/config-files?plugin_id=&config_key=`
- `POST /api/v1/plugins/config-files?plugin_id=&config_key=`（multipart）
- `DELETE /api/v1/plugins/config-files`

实现 `config_service.py:1020-1079`：schema 项必须 `type=="file"`（否则 `Config item not found or not file type`）；
落盘相对路径固定为 **`files/<folder>/<filename>`**（`:1063`，`folder = _config_key_to_folder(key_path)`）；
上传有扩展名白名单 `_allowed_file_extensions(meta)`（`:1039`）与 `MAX_FILE_BYTES` 上限（`:1054`、`:1071`）；
保存的值必须是以 `files/` 开头的相对路径，否则校验报
`Invalid file path {path}{key}[{idx}]: {item}`（`:233-247`）。
前端 i18n 侧文案键：`fileUpload.addToConfig` / `fileUpload.uploadSuccess` / `fileUpload.fileTooLarge` / …。

### 3.7 配置项文案的 i18n

`pluginI18n--LKOZc8C.js:1`（该文件只有 1 行）原文：

```js
configText:(t,e,c,n="")=>{const m=e?`config.${e}.${c}`:`config.${c}`;return r(t,m,n)}
```

→ 插件配置项的说明文案键是 **`config.<key>`**，嵌套一层时是 **`config.<group>.<key>`**，
数据来自插件自己的 `.astrbot-plugin/i18n/<locale>.json`。

---

## 4. 插件拓展页（`pages/`）在真 Dashboard 里怎么进入

### 4.1 声明方式：**目录约定优先**

- `dashboard/services/plugin_page_service.py:25-29`：
  `PLUGIN_PAGE_ROOT_DIR_NAME = "pages"`、`PLUGIN_PAGE_ENTRY_FILE_NAME = "index.html"`、
  `PLUGIN_PAGE_BRIDGE_FILE = <dashboard>/plugin_page_bridge.js`。
- 唯一发现逻辑 `discover_plugin_pages()`（`plugin_page_service.py:508-538`）：
  遍历 `pages/` 下的**子目录**（`:516-519`）→ 目录名过 `normalize_plugin_page_name`（`:525`）→
  **必须有 `index.html`**（`:528-530`，否则 `continue`）→ `title` 直接取**目录名**（`:534`）。
- `metadata.yaml` 的 `pages:` 字段**只是元数据**：`star_manager.py:551-553` 读进 `StarMetadata.pages`
  （`core/star/star.py:75-76`），`:1187` 由 yaml 覆盖；
  **但 dashboard 侧从不读它**（`grep "\.pages" dashboard/ --include=*.py` 无命中于页面发现逻辑）。
- 实例佐证：`astrbot_plugin_private_companion` 的 `metadata.yaml` 声明了
  `pages: [{name: companion-panel, title: 陪伴面板}]`，而 `pages/` 下同时存在
  `companion-panel/` 与 `陪伴面板/` 两个目录（各含 `index.html`）——
  真实现会把**两个都列出来**（title=目录名），而我们只认 metadata 里那一个。
- i18n 覆盖标题：`.astrbot-plugin/i18n/<locale>.json`（`star_manager.py:560-600`），
  页面标题取 `pages.<name>.title`（`plugin_page_service.py:206-208`）。

### 4.2 接口清单

| 路径 | 方法 | 行号 | 用途 |
|---|---|---|---|
| `/api/v1/plugins` | GET | `api/plugins.py:678-689` | 列表，每项带 `"pages":[<name>,…]` |
| `/api/v1/plugins/pages?plugin_id=` | GET | `api/plugins.py:866-877` | 列页面（返回的就是插件详情，页面在 `components[]` 里 `type=="page"`） |
| `/api/v1/plugins/{plugin_id}/pages` | GET | `api/plugins.py:1157-1168` | 同上（RESTful 别名） |
| `/api/v1/plugins/page?plugin_id=&page_name=` | GET | `api/plugins.py:880-894` | **取页面入口配置** |
| `/api/v1/plugins/{plugin_id}/pages/{page_name}` | GET | `api/plugins.py:1171-1185` | 同上（RESTful 别名） |
| `/api/v1/plugins/page/assets?…&asset_path=` | GET | `api/plugins.py:897-913` | **取静态资源** |
| `/api/v1/plugins/{plugin_id}/pages/{page_name}/assets/{asset_path:path}` | GET | `api/plugins.py:1188-1204` | 同上 |
| `/api/v1/plugins/page-bridge-sdk.js` | GET | `api/plugins.py:666-675` | **桥 SDK** |
| `/api/v1/plugins/extensions/{plugin_path:path}` | GET/POST/PUT/PATCH/DELETE | `api/plugins.py:379-421` | 插件自注册 API 的转发口 |

**入口配置返回结构**（`plugin_page_service.py:871-884`）：
`{name, title, i18n_key:"pages.<name>", content_path?}`，其中
`content_path = /api/plugin/page/content/<plugin>/<page>/?asset_token=<JWT>`（`:641-666`）。
错误文案原文：`缺少插件名`(`:230`)、`缺少 Page 名称`(`:232`)、`插件不存在`(`:236`)、
`插件未启用`(`:238`)、`插件 Page 不存在`(`:249`)；内容侧英文：
`Plugin not found`(404) / `Plugin is disabled`(403) / `Plugin Page asset not found`(404)（`:357-374`）。

**旧版对等路由**（`legacy_router`，`include_in_schema=False`，鉴权 `require_dashboard_user`）：
`/api/plugin/page/entry`（`:1244-1256`）、`/api/plugin/page/content/{plugin_id}/{page_name}/`（`:1469-1484`）、
`/api/plugin/page/content/{plugin_id}/{page_name}/{asset_path:path}`（`:1487-1503`）、
`/api/plugin/page/bridge-sdk.js`（`:1457-1466`）、`/api/plug/{plugin_path:path}`（`:1506-1512`，GET/POST 两种方法）。
⚠️ **iframe 真正加载的是旧版那条**（`content_path` 就是这个形状）。

### 4.3 iframe 怎么搭的

前端产物 `PluginPagePage-DvXayMlZ.js`（7 行，9,324 字节），路由定义在
`index-BiTHkcB1.js:121`：`{name:"PluginPage",path:"/plugin-page/:pluginName/:pageName",…}`。

1. 取详情：`$.get(S.value)` → `/api/v1/plugins/by-id`；若 `!d.activated` 显示
   `messages.pluginDisabled`=**`该插件当前已关闭，请先打开后再访问页面`**。
2. 取入口：`$.page(S.value,k.value)` → `/api/v1/plugins/page?plugin_id=&page_name=`；
   没有 `content_path` 则显示 `messages.pluginPageNotFound`=**`未找到该插件页面`**。
3. 拼 src：`const u=new URL(r.content_path,window.location.origin); u.searchParams.set("theme",Q.value)`
   → **`/api/plugin/page/content/<plugin>/<page>/?asset_token=<JWT>&theme=dark|light`**。
4. iframe 属性（原文，`PluginPagePage-DvXayMlZ.js:7`）：
   `{key:2,ref_key:"iframeRef",ref:j,src:T.value,class:"plugin-page-frame",referrerpolicy:"no-referrer",sandbox:"allow-scripts allow-forms allow-downloads"}`
   ⚠️ **sandbox 不含 `allow-same-origin`** ⇒ iframe 内是 opaque origin（`event.origin === "null"`）。
   CSS：`.plugin-page-frame{width:100%;flex:1;border:0}`（`PluginPagePage-u-sSYJb7.css`，1 行）。

**postMessage 桥（双向协议）**

- 通道常量：**`"astrbot-plugin-page"`**（`plugin_page_bridge.js:2`，与宿主 `PluginPagePage` 里的 `J` 同值）。
- iframe → 宿主过滤（`PluginPagePage-DvXayMlZ.js:1` 原文）：
  `if(!n||e.source!==n||e.origin!==Z&&e.origin!=="null"||h&&e.origin!==h)return; h=e.origin;`
  即**首次合法消息锁定 origin**。
- 消息 `kind`（原文）：`"ready"`（子→父，`plugin_page_bridge.js:287`）、`"request"`（`:61`）、
  `"context"`（`:162-165`）、`"response"`（`:167-181`）、`"sse_message"`（`:183-194`）、
  `"sse_state"`（`:196-204`，state 取 `"open"`/`"error"`；宿主还会发 `"closed"`，桥忽略）。
- 请求 `action`（原文）：`"api:get"`(`:239`)、`"api:post"`(`:242`)、`"files:upload"`(`:250`)、
  `"files:download"`(`:263`)、`"sse:subscribe"`(`:270`)、`"sse:unsubscribe"`(`:283`)。
- 请求 id：`const requestId = \`plugin_req_${requestCounter}\``（`:58`）；
  SSE 订阅 id：`\`plugin_sse_${subscriptionCounter}\``（`:267`）；
  响应形状：`b=(e,n,t)=>{P({kind:"response",requestId:e,ok:n,…n?{data:t}:{error:t}})}`。
- context 载荷（宿主原文）：
  `{pluginName, displayName, pageName, pageTitle, locale, i18n, isDark}`。
- **没有任何 nonce / token / HMAC**：`grep -ni "nonce|secret_token|hmac"` 在
  `plugin_page_bridge.js`、`plugin_page_auth.py`、`plugin_page_service.py` **零命中**；
  隔离靠的是 `event.source===iframe.contentWindow` + origin 锁定 + `sandbox`。
- endpoint 清洗（宿主原文报错）：
  `"Plugin bridge endpoint must be a string."` / `"Plugin bridge endpoint cannot be empty."` /
  `"Plugin bridge endpoint is invalid."`，随后拼成
  **`/api/v1/plugins/extensions/${encodeURIComponent(pluginName)}/${endpoint}`**
  （`PluginPagePage-DvXayMlZ.js:1` 的 `B=e=>{const n=te(e);return \`/api/v1/plugins/extensions/…\`}`）。
- **未找到**：调整 iframe 高度、`openLink`/`window.open`、主题主动切换 API、loading 状态接口 ——
  `grep -ni "height|resize|openLink|window.open|focus" plugin_page_bridge.js` **零命中**。

### 4.4 `dashboard/plugin_page_bridge.js` 做什么（288 行，逐条）

挂载全局 **`window.AstrBotPluginPage`**（`:207-285`），IIFE 末尾 `send("ready")`（`:287`）。

| 方法 | 行号 | 语义 |
|---|---|---|
| `ready()` | `:208-210` | 返回 Promise，resolve 首次 context |
| `getContext()` | `:211-213` | 当前 context 对象 |
| `getLocale()` | `:214-216` | 默认 `"zh-CN"` |
| `getI18n()` | `:217-219` | 插件 i18n 字典 |
| `t(key, fallback)` | `:220-222` | 点号路径取词条，回退序 `[context.locale,"zh-CN","en-US"]`（`:98-113`） |
| `onContext(handler)` | `:223-234` | 订阅 context（立即回调一次），返回退订函数 |
| `__setInitialContext(ctx)` | `:235-237` | 服务端注入脚本调用；`isDark` 会写 `document.documentElement[data-theme]`（`:133-138`） |
| `apiGet(endpoint, params)` | `:238-240` | 宿主代发 GET |
| `apiPost(endpoint, body)` | `:241-243` | 宿主代发 POST |
| `upload(endpoint, file)` | `:244-261` | `file.arrayBuffer()` 后以 transferable 传 `fileBuffer` |
| `download(endpoint, params, filename)` | `:262-264` | 宿主下载（宿主侧解析 `content-disposition` 的 `filename*=UTF-8''…`） |
| `subscribeSSE(endpoint, handlers, params)` | `:265-280` | 宿主建 SSE，返回 `subscriptionId` |
| `unsubscribeSSE(id)` | `:281-284` | 退订 |

**SDK 怎么发**：`serve_bridge_sdk()`（`plugin_page_service.py:315-343`）读 `plugin_page_bridge.js` 原文，
`content_type="application/javascript; charset=utf-8"`，并在**末尾追加一行注入上下文**（`:337-339`）原文：
`\n;window.AstrBotPluginPage?.__setInitialContext({context_json});\n`。
**HTML 会自动注入它**：`rewrite_plugin_page_html` 若发现 HTML 里没有 `/api/plugin/page/bridge-sdk.js`，
就往 `</body>` 前塞一个 `<script src=…>`（`plugin_page_service.py:741-748`）。
平台能力限制 `:439-443`：CSP `object-src 'none'; base-uri 'self'`；
非 `ASTRBOT_LAUNCHER` 环境再加 `X-Frame-Options: SAMEORIGIN` + `frame-ancestors 'self'`。

### 4.5 `dashboard/plugin_page_auth.py` 做什么（64 行，逐函数）

常量：`:3 PLUGIN_PAGE_CONTENT_PREFIX="/api/plugin/page/content/"`、
`:4 PLUGIN_PAGE_BRIDGE_PATH="/api/plugin/page/bridge-sdk.js"`、
`:5 PLUGIN_PAGE_TOKEN_TYPE="plugin_page_asset"`。

| 函数 | 行号 | 行为 |
|---|---|---|
| `is_protected_path(path)` | `:9-13` | `startswith(内容前缀) or startswith(bridge 路径)`。**只识别旧版路径**（新版 `/api/v1` 在中间件里直接放行，见下） |
| `is_asset_token(payload)` | `:15-17` | `payload.get("token_type")=="plugin_page_asset"` |
| `extract_asset_token(query_params)` | `:19-22` | 取 query 参数 **`asset_token`** |
| `extract_plugin_name_from_path(path)` | `:24-30` | 前缀后**第一段**，`unquote()` |
| `extract_page_name_from_path(path)` | `:32-39` | 前缀后**第二段**，`unquote()` |
| `is_scope_valid(payload, path)` | `:41-64` | 非受保护→False；**bridge 路径→True**；否则要求 token 的 `plugin_name`/`page_name` 与 URL 解出的**完全相等**（`:61-63`） |

**登录态怎么传给插件页（两段式）**：

1. Dashboard 本体 JWT（HS256，密钥 `dashboard.jwt_secret`，`dashboard/server.py:427-434`）走
   `Authorization: Bearer` **或** cookie `astrbot_dashboard_jwt`（`api/auth.py:94-104`；
   cookie 属性 `HttpOnly`/`SameSite=strict`，`api/auth.py:239-245`、`services/auth_service.py:82-83`）。
2. `get_plugin_page_entry_config` 用**同一密钥**签发**短时效 scoped token** 放进 URL query
   （`plugin_page_service.py:894-919`）原文：
   ```python
   payload = {"username":…, "token_type": PLUGIN_PAGE_ASSET_TOKEN_TYPE,
              "plugin_name":…, "page_name":…, "locale":…,
              "iat": now, "exp": now + timedelta(seconds=60)}   # TTL=60s（:24）
   jwt.encode(payload, jwt_secret, algorithm="HS256")
   ```
3. 校验在 `dashboard/server.py:238-272`：受保护路径允许**两个候选 token**（dashboard JWT 优先，再 asset_token，`:245-249`）；
   逐个 `_validate_dashboard_token`（`:274-306`），**asset token 额外做 scope 校验**（`:296-300`）；
   失败统一 `未授权`(401)/`Token 过期`/`Token 无效`。
   ⚠️ 该中间件 `:217-218` 对 `/api/v1` **直接 `return None`**（放行），
   所以新版路由的鉴权完全靠各自的 `Depends(require_plugin_scope)`；
   `plugin_page_auth.py` 只管**旧版**那条 iframe 真正用的内容 URL。

⚠️ **一个真实的越权面**：桥的 `api:get`/`api:post` 由**宿主页面**用 axios 代发，
带的是**宿主自己的完整 Bearer JWT**（`PluginPagePage-DvXayMlZ.js:1` 的请求拦截器
`Authorization: Bearer ${localStorage.getItem("token")}`），**不是** scoped asset token；
`api:post` 可写。宿主只把 endpoint 前缀限制在 `/api/v1/plugins/extensions/<pluginName>/…`。

### 4.6 插件页能调哪些扩展 API

`_call_plugin_extension()`（`api/plugins.py:190-229`）原文要点：

```python
registered_web_apis = request.app.state.core_lifecycle.star_context.registered_web_apis
matched_api = _match_registered_web_api(registered_web_apis, plugin_path, request.method)
if not matched_api:
    return {"status": "error", "message": "未找到该路由", "data": {}}
```

匹配规则 `_plugin_api_route_pattern()`（`:153-163`）：`<name>` → `[^/]+`，`<path:name>` → `.*`，
`re.fullmatch`。插件侧注册方式：`context.register_web_api(route, view_handler, methods, desc)`
（`core/star/context.py:705-727`，存储 `:126 registered_web_apis`）。
真实实例：`astrbot_plugin_private_companion/page_api.py:222` `PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"`，
`:1465-1467` 批量 `register(f"{PAGE_API_PREFIX}{path}", handler, methods, desc)`。

插件页 JS 的**两条取数路径**（`pages/companion-panel/app.js`）：
① 官方桥 `bridge.apiGet/apiPost`（`:6470-6472`，`getBridge()` 先查 `window.AstrBotPluginPage`
再查 `window.parent.AstrBotPluginPage`，`:6497-6509`）；
② HTTP 直连退路 `const HTTP_API = "/astrbot_plugin_private_companion/page";`（`:1`）
（独立 WebUI 模式用 `const STANDALONE_HTTP_API = "/api/v1";`，`:2`）。
两条都没有时抛**`未检测到 AstrBot 官方插件 Page 桥接，请从 AstrBot 后台的插件拓展页打开`**。

### 4.7 安全边界（目录穿越 / 软链）

| 函数 | 行号 | 做法 |
|---|---|---|
| `normalize_plugin_page_path` | `plugin_page_service.py:446-467` | `\`→`/`、`posixpath.join`+`normpath`；拒绝 `../`、`===".."`、以 `/` 开头 → `Invalid plugin Page asset path` |
| `normalize_plugin_page_name` | `:469-483` | `normpath` 后必须与原值相同、非 `.`/`..`、不以 `.` 开头、不含 `/`、`\` → `Invalid plugin Page name` |
| `get_plugin_root_dir` | `:485-496` | `.resolve(strict=False)` 后 `plugin_root.relative_to(base_dir)`（`:495`） |
| `resolve_plugin_pages_root` | `:498-506` | `pages_root.relative_to(plugin_root)`（`:501`）+ `isdir` |
| `resolve_plugin_page_root` | `:551-562` | `page_root.relative_to(pages_root)`（`:559`） |
| `resolve_plugin_page_file` | `:564-580` | `normalize(asset_path) or entry_file` → `resolve()` → `relative_to(page_root)`（`:577`）+ `isfile` |

- **符号链接**：`Path.resolve()` 会解析软链，配合 `relative_to` 断言 ⇒ 指向 pages 根**之外**的软链会被拦下；
  代码里**没有**显式 `is_symlink()` / `realpath` / `follow_symlinks=False`
  （`grep -ni "symlink|follow_symlinks|is_symlink|realpath"` 零命中）。指向**根内**的软链不拦。
- 响应头 `build_security_headers()`（`:430-444`）：`Cache-Control: no-store`、
  `Referrer-Policy: no-referrer`、`X-Content-Type-Options: nosniff`、
  `Cross-Origin-Resource-Policy: cross-origin`、`Access-Control-Allow-Origin: "*"`；
  CSP `object-src 'none'; base-uri 'self'`（+ 非 launcher 时 `frame-ancestors 'self'`）。
- HTML/CSS/JS 会被**改写相对资源 URL**（`:387-423`，正则表在 `:31-48`），
  把 `./app.js` 之类改写成带 `asset_token` 的完整内容路径。

---

## 5. 有没有「插件市场 / 在线安装」页？边界在哪

**有，而且和「已安装」是同一页面的两个 tab。** 页面标题 `插件管理` / `管理和配置系统插件`，
tab 原文：`Age={installedPlugins:"插件",market:"插件市场",installedMcpServers:"MCP",skills:"技能",handlersOperation:"管理行为"}`。
路由：`Extensions` → `plugins`（已安装）、`ExtensionMarketplace` → `plugins/market`（市场），
另有重定向 `/extension-marketplace` → `ExtensionMarketplace`、`/extension/market` → `ExtensionMarketplace`（`index-BiTHkcB1.js:121`）。

### 5.1 数据来源（确切常量）

`plugin_service.py:1051-1070` `build_registry_source()`：

- 默认源：缓存 `get_astrbot_data_path()/plugins.json`（`:1064`）、md5 校验 `https://api.soulter.top/astrbot/plugins-md5`（`:1065`）、
  候选 URL（**按序尝试**，`:1066-1069`）：
  1. `https://api.soulter.top/astrbot/plugins`
  2. `https://github.com/AstrBotDevs/AstrBot_Plugins_Collection/raw/refs/heads/main/plugin_cache_original.json`
- 自定义源：缓存 `plugins_custom_<md5(url)[:8]>.json`，md5 地址 = URL 去 `.json` 拼 `-md5.json`（`:1055-1062`）。
- 取数 `get_online_plugins()`（`:967-1038`）：先比缓存 md5（远端取不到时**直接信任缓存**，`:1116-1120`）；
  否则 aiohttp 按序 GET，首个 200 且非空者入选并落缓存；
  **全失败 → 退回缓存并回 message `使用缓存数据，可能不是最新版本`**（`:1029-1038`）；
  连缓存都没有 → `PluginServiceError("获取插件列表失败，且没有可用的缓存数据")`。

### 5.2 接口清单

| 路径 | 方法 | 行号 | 备注 |
|---|---|---|---|
| `/api/v1/plugins/market` | GET | `api/plugins.py:588-600` | query `custom_registry`、`force_refresh` |
| `/api/v1/plugins/market/categories` | GET | `api/plugins.py:603-607` | **硬编码桩**：`return ok({"categories": []})`；实测前端产物里 **0 次出现** |
| `/api/v1/plugin-sources` | GET/POST/PUT | `api/plugins.py:610-639` | 自定义源；前端只用 GET 与 PUT（整表替换） |
| `/api/v1/plugin-sources/by-id`、`/api/v1/plugin-sources/{source_id}` | DELETE | `api/plugins.py:642-663` | 删源 |
| `/api/v1/plugins/install/github` + `/install/git` | POST | `api/plugins.py:502-541` | **同一 handler 两个别名**（`repository_transport` 取 URL 末段）；本副本**没有** `/plugins/install/repository` 这个路径 |
| `/api/v1/plugins/install/url` | POST | `api/plugins.py:544-572` | 任意 URL 安装 |
| `/api/v1/plugins/install/upload` | POST | `api/plugins.py:575-585` | multipart 字段 `file`；缺文件 `缺少插件文件` |
| `/api/v1/plugins/validate/repo` | POST | `api/plugins.py:490-499` | 只校验仓库是不是合法 AstrBot 插件，不落盘 |
| `/api/v1/plugins/version-support/check` | POST | `api/plugins.py:481-487` | 只校验**传进来的版本范围字符串**，不校验插件本身 |
| `/api/v1/plugins/update` | POST | `api/plugins.py:432-458` | 有 `plugin_id` → 单个；否则 `names`/`plugin_ids` → 批量 |

鉴权：全部 `Depends(require_plugin_scope)`（`api/plugins.py:59` `ScopeDependency("plugin")`）。

`plugin-sources` 存储：shared_preferences 全局键 **`custom_plugin_sources`**
（`plugin_service.py:1994-2041`；读 `:1994-1998`，`_custom_sources_from_payload` `:2006-2010`，删除匹配 `:2033-2038`）；**内置自定义源 = 0 个**，
但官方「默认源」永远在（前端写死首项 `market.defaultSource`=**`默认插件源`**，
选中时内部值为 `null`，取数不带 `custom_registry`）⇒ **1 个官方内置（不可删）+ N 个用户自定义**。
前端另把选择的源记在 `localStorage["selectedPluginSource"]`。

### 5.3 市场页长什么样（文案原文）

`Hge` 命名空间（`index-BiTHkcB1.js:24`）原文摘录：
`recommended:"🥳 推荐"`、`allPlugins:"📦 全部插件"`、`category:"分类"`、
`categories:{all:"全部",ai_tools:"AI 增强",entertainment:"娱乐",productivity:"效率",integrations:"外部集成",utilities:"生活实用",other:"其他"}`、
`showFullName:"完整名称"`、`devDocs:"插件开发文档"`、`submitRepo:"提交插件仓库"`、
`customSource:"自定义插件源"`、`source:"插件源"`、`availableSources:"可用源"`、
`sourceManagement:"插件源管理"`、`addSource:"添加插件源"`、`sourceName:"源名称"`、
`sourceUrl:"源地址"`、`defaultSource:"默认插件源"`、`removeSource:"删除插件源"`、
`confirmRemoveSource:"确定要删除此插件源吗？"`、`sourceAdded:"插件源添加成功"`、
`sourceRemoved:"插件源删除成功"`、`sourceError:"操作失败"`、`selectSource:"选择插件源"`、
`currentSource:"当前插件源"`、`editSource:"编辑插件源"`、`sourceUpdated:"插件源更新成功"`、
`defaultOfficialSource:"默认官方源"`、`sourceExists:"该插件源已存在"`、`sourceResolved:"插件源可用"`、
`installPlugin:"安装插件"`、`randomPlugins:"🎲 随机插件"`、
**`sourceSafetyWarning:"即使是默认插件源，我们也不能完全保证插件的稳定性和安全性，使用前请谨慎核查。"`**。

排序（`zge`）：`by:"排序方式"`、`default:"默认排序"`、`installTime:"最后修改时间"`、
`name:"名称"`、`stars:"Star数"`、`downloads:"下载量"`、`author:"作者名"`、
`updated:"更新时间"`、`updateStatus:"更新状态"`、`ascending:"升序"`、`descending:"降序"`。
⚠️ 实测 `MarketPluginsTab-IGfL2ru-.js` 里实际用的排序项只有
`sort.default / sort.stars / sort.downloads / sort.author / sort.updated`（`"sort.by"`、`"sort.updateStatus"` 在该页模板里未引用）。

**市场卡片字段**（`MarketPluginsTab-IGfL2ru-.js`，1 行，14,934 字节）：
封面 `logo`、`display_name`（否则 `showFullName? name : trimmedName`）、`pinned`→`🥳 推荐` 角标、
`astrbot_version_supported===false`→`status.unsupported`=**`不支持`** 角标、
作者（有 `social_link` 时渲染成外链）、`stars`、`download_count`、简介、
`support_platforms` chip、`repo`→按钮 `buttons.viewRepo`=**`仓库`**（`mdi-github` 外链）、
已安装则显示 `status.installed`=**`已安装`**（disabled），否则 `buttons.install`=**`安装`**。
`market_plugin_id` 为空时按钮 disabled，tooltip `messages.missingMarketPluginId`=**`缺少合法的插件标识，无法从该插件市场源安装`**。
另有 `buttons.reshuffle`=**`随机一发`** / `market.randomPlugins`=**`🎲 随机插件`**。
**空态**：市场页**没有**独立空态文案（实测该分片引用的键里不含 `empty.*`）。

**安装确认弹窗**（`Uge.dialogs.install`）原文：
`从文件安装` / `从链接安装` / `支持 {count} 个平台` / `安装` / `将从以下位置安装插件包体：` /
**`AstrBot 不能保证从代码仓库下载的插件的安全性。请确认你信任该插件来源后再安装。`**。
带 `danger` 标签的插件多一层 `dialogs.danger_warning`：
**`该插件可能包含不安全的代码或功能，可能导致系统异常或数据损失等。请确认是否继续安装？`** / `继续` / `取消`。
版本不兼容时 `dialogs.versionSupport`：
**`该插件声明的 AstrBot 版本范围与当前版本不匹配。你可以无视警告继续安装，但可能无法正常运行。`** /
`无视警告，继续安装` / `取消安装`（对应后端 `{"status":"warning","warning_type":"astrbot_version_unsupported","can_ignore":true}`，
`plugin_service.py:1628-1636`）。

### 5.4 安装的实际边界（我们不做，只需知道分界）

- **要落地文件、要 zip。** 链路：`install_plugin`（`plugin_service.py:1563-…`）→
  `star_manager.install_plugin()`（`star_manager.py:1614-1731`）→
  `_updater.install()`（`core/star/updater.py:200-222`）：
  有 `download_url` 直接下 `.zip`；`transport=="git"` 走 `git clone --depth …`
  （`:52-77`，**环境无 git 则 `GitUnavailableError`「安装此仓库需要 Git，但当前运行环境中未找到 git 命令。」**）；
  否则从 archive URL 下 zip（`core/repository.py:102-113`：
  `https://github.com/{owner}/{name}/archive/refs/heads/{branch}.zip`）。
  上传路径同样是 zip（`star_manager.py:2076-2130`，解压后删 zip）。
- **版本兼容检查有两处写入路径**：首载 `star_manager.py:1195-1205` 与**重载路径** `:1345-1355`；
  `ignore_version_check=true` 可绕。
- **插件列表页的"更新"直接依赖市场源**：非市场安装的插件按
  `PLUGIN_UPDATE_DISABLED_MESSAGE` 一律禁用更新入口。

---

## 6. 与我们面板的差异（**只列事实，不提方案**）

### 6.1 我们**没有**、而真实现有的

| # | 真实现有 | 我们的现状（证据） |
|---|---|---|
| 1 | **插件市场页 + 在线安装**（市场 / 自定义源 / 版本兼容告警 / 危险插件确认） | 无。我们只有 `GET /api/v1/plugins`、`PUT /api/v1/plugins/inactivated`、`GET /api/v1/plugins/pages` 与页面三条路由（`emotion_app/web/app.py:944`、`:966`、`plugin_pages_web.py:70`、`:159`、`:166`、`:173`、`:183`） |
| 2 | **详情页**（`GET /api/v1/plugins/by-id`）：`components[]` 里 page/skill/command/llm_tool/listener/hook 六类 | 无详情接口。`plugins_view()` 每条只有 `{id, module_path, name, version, state, error}`（`host/astrbot_host/host_app.py:328-358`） |
| 3 | **指令 / 指令组 / 正则匹配 / 事件监听器 / 钩子 的清单**（`plugin_service.py:699-793`） | 无对等物。我们的 `PluginRecord` 只有 `handler_count`（个数，`host/astrbot_host/loader/plugin_loader.py:105-166`），没有逐条 `cmd`/`description`/`display_type`/`has_admin` |
| 4 | **LLM 工具（function calling）列表**，type 为 `"llm_tool"`，来自 `EventType.OnCallingFuncToolEvent`（`plugin_service.py:768-769`） | 无对等物。我们只有 `llm_tool_bind_count`（绑定成功的**个数**，`plugin_loader.py:121-124`） |
| 5 | **技能（skills）清单**（`plugin_service.py:795-826`） | 无 |
| 6 | **按插件配置 schema 渲染的表单**（`_conf_schema.json` → `AstrBotConfig` 组件）+ 恢复默认值 | 无。我们的插件配置是插件**自己的**拓展页承载（747 项），面板侧没有 schema 驱动表单；也没有 `GET/PUT /api/v1/plugins/config` 这类路由 |
| 7 | **单插件日志级别**（`PUT …/log-level`，文案 `日志级别` / `仅影响该插件的日志输出，立即生效，无需重启` / `跟随全局`） | 无 |
| 8 | **插件日志窗口**（配置弹窗内 `ConsoleDisplayer`，`historyNum="10"`，走 `/api/v1/logs/history` + SSE `/api/v1/logs/live`） | 我们有全局日志页（`/api/v1/logs` + 内存环形缓冲），但**没有**按插件过滤/按插件取日志 |
| 9 | **README / 更新日志查看**（`/plugins/readme`、`/plugins/changelog`，Markdown 渲染） | 无对应接口 |
| 10 | **单插件重载**（`POST /plugins/reload`） | 无。我们的启停保存后**需重启服务**（面板明写"需要重启服务"，`static/app.js:3452-3454`） |
| 11 | **单插件更新 / 批量更新 / 卸载 / 更换安装源** | 无 |
| 12 | **置顶**（`localStorage` 键 `astrbot.pinnedExtensions`） | 无 |
| 13 | **搜索 / 排序（Star数·下载量·作者·更新时间）/ 分页（每页 9）/ 随机插件 / 分类过滤** | 无（`plugin_pages_web.py` 的列表是平铺 `<ul>`） |
| 14 | **失败插件专用区块**（独立接口 `/plugins/failed`，列 `插件`/`错误`，带 `重载`/`删除`） | 我们把失败并进同一列表的 `state="failed"`（`plugin_loader.py:147-165`），无独立接口/区块 |
| 15 | **插件 i18n**（`.astrbot-plugin/i18n/<locale>.json`，标题/描述/配置项文案全覆盖） | 无。我们只读 `metadata.yaml` 的 `pages[].title`（`host/astrbot_host/plugin_pages.py:89-133`） |
| 16 | **iframe 的 `sandbox="allow-scripts allow-forms allow-downloads"` + `referrerpolicy="no-referrer"`**（`PluginPagePage-DvXayMlZ.js:7`） | 无。我们的 `<iframe id="plugin-frame" class="plugin-frame" title="插件拓展页">` **没有任何 sandbox / referrerpolicy**（`static/index.html:597`） |
| 17 | **页面内容的 6 个安全响应头 + CSP**（`plugin_page_service.py:430-444`） | 我们只设 `Cache-Control: no-cache, must-revalidate`（`plugin_pages_web.py:150`） |
| 18 | **一次性 `asset_token`（60s TTL，绑定 plugin+page+locale）**（`plugin_page_service.py:894-919`、`server.py:238-306`） | 无。我们复用主服务会话鉴权（`plugin_pages_web.py:71-73`、`:161-163`、`:167-169`、`:175-177` 等 `require_auth()`） |
| 19 | **postMessage 桥的扩展能力**：`getContext/getLocale/getI18n/t/onContext/upload/download/subscribeSSE/unsubscribeSSE`（13 个方法） | 我们的同源垫片只有 **3 个**：`window.AstrBotPluginPage = { ready, apiGet, apiPost }`（`static/plugin_page_bridge.js:124-128`） |
| 20 | **页面发现只认目录约定** `pages/<name>/index.html`（`plugin_page_service.py:508-538`） | 我们**以 `metadata.yaml` 的 `pages:` 为准**，再去查 `pages/<name>/index.html` 是否存在（`host/astrbot_host/plugin_pages.py:89-133`）⇒ metadata 声明了但目录不存在时我们不列（打 WARNING），而真实现会**列出目录、不列 metadata** |
| 21 | **`pages` 字段的 title 用 i18n `pages.<name>.title` 覆盖** | 我们用 `spec.get("title")`（metadata 里的原值） |

### 6.2 我们**有**、而真实现**没有**的

| # | 我们的 | 证据 |
|---|---|---|
| 1 | `restart_required`（保存集合 ≠ 运行集合，算出来的） | `emotion_app/web/plugin_host.py:1052-1057`；在真前端产物里 `"restart_required"` **0 次** |
| 2 | `enabled_saved`（用户保存的意图，与运行态分开） | `plugin_host.py:1048-1049`；真前端 `"enabled_saved"` **0 次** |
| 3 | `unmatched`（停用名单里对不上任何插件的项，回给面板） | `plugin_host.py:1045-1046`；真前端 `"unmatched"` 0 次（仅在 shiki 里出现同名子串，与插件无关） |
| 4 | `inactivated_saved` / `inactivated_active` / `running` 三个并列字段 | `plugin_host.py:1059-1066`；真前端 `"inactivated_saved"` 0 次 |
| 5 | `state` 四态穷尽 `loaded / inactivated / failed / not_loaded`（**"停用"与"加载失败"是两条路径**） | `plugin_loader.py:147-165`（`state` 四态判据）；真实现是 `activated` 布尔 + metadata 里的 `error`，没有四态枚举 |
| 6 | `error` 只留最后一行 + `traceback` 另存 | `plugin_loader.py:118-120`、`host_app.py:353-355` |
| 7 | 面板明写"需重启服务才生效"（因为我们的启停不 reload） | `static/app.js:3452-3454`；真实现是 `turn_on_plugin` 里 `await self.reload(...)` 立即生效 |
| 8 | 插件页 URL 无斜杠时 **308 重定向**到带斜杠（相对路径解析兜底） | `plugin_pages_web.py:159-164` |
| 9 | **根路径兼容路由** `/<plugin_dir>/<path:rest>`（插件页 JS 硬编码 `HTTP_API` 用） | `plugin_pages_web.py:183-247`；真实现靠 iframe 桥绕开，**没有**这条 |

### 6.3 两边**做法不同**的点（同一件事，两条路）

| 事项 | 真实现 | 我们 |
|---|---|---|
| 插件页承载 | 跨源 iframe（`sandbox` 无 `allow-same-origin`）+ postMessage 桥 | **同源 iframe** + 同源垫片（`static/plugin_page_bridge.js`） |
| 页面鉴权 | 一次性 scoped JWT `asset_token`（60s） | 复用主服务会话 cookie |
| 页面清单来源 | 目录约定 `pages/*/index.html` | `metadata.yaml` 的 `pages:` |
| 插件启停落点 | shared_preferences `inactivated_plugins`（`star_manager.py:1087`、`:1995-2002`） | `data/config_profile.json` 的 `plugins` 段 |
| 启停生效 | 立即 `reload` | 需重启服务 |
| 加载失败插件 | 独立接口 + 独立 UI 区块 | 并进同一列表的 `state="failed"` |
| 平台 chip / 版本要求 chip | `PluginPlatformChip` + `AstrBot: {ver}` | 无 |

---

## 7. 未找到 / 没证到的（如实记录）

1. **`GET /api/v1/plugins/market/categories` 是个桩**：后端硬编码 `return ok({"categories": []})`（`api/plugins.py:603-607`），
   且该路径在**任何 dist 产物里 0 次出现** —— 前端没调它。
2. **`/plugins/install/repository` 这个路径在本副本里不存在**：对应能力是
   `/api/v1/plugins/install/github` 与 `/api/v1/plugins/install/git`（同一 handler，`:502-541`）。
3. **`views.card` / `views.list` / `table.headers.name` / `buttons.enable` / `buttons.disable` /
   `buttons.configure` / `showSystemPlugins` / `hideSystemPlugins` 在本版 UI 中未被引用**（判据同 §0.3）。
   这些键在 i18n 表里存在，但当前构建的模板里没有对应调用点。
4. **`online_vesion`（拼错那个）在全部产物里 0 次**；真前端用的是自己算的 `online_version`。
   我们**不能**照抄 `online_vesion` 这个字段名。
5. **未找到**桥的 nonce/token/HMAC 机制（`grep -ni "nonce|secret_token|hmac"` 零命中）；
   也**未找到** iframe 高度自适应 / 打开链接 / 主题主动切换的桥 API。
6. **未找到** `pages` 字段的 schema 校验：`core/star/updater.py:335-371 validate_plugin_metadata`
   只校验 `("name","desc","version","author")`（`PLUGIN_METADATA_REQUIRED_FIELDS`，`updater.py:26`），
   `pages` 是自由字段。
7. **未验证**（超出只读范围，不该做）：真实运行时的接口返回样例 —— 本机 6185 端口是生产 Dashboard，
   按任务禁令未发任何请求。上面所有返回结构都是从**代码里的构造点**读出来的，不是线上抓包。
8. **未找到**那个"中文 `event_type` 反查"用到的 `fn` 之外的东西；`PluginDetailPage` 里的
   22 个 CJK 字符全部是 `"指令"` / `"指令组"` / `"正则匹配"` / `"事件监听器"` / `"函数工具"`（原文）。

---

## 附：本次调研用到的检索模式（便于复核）

`/api/v1/plugins`、`plugins/extensions`、`_conf_schema`、`conf_schema`、`serialize_plugin_base`、
`get_plugin_components_info`、`discover_plugin_pages`、`PLUGIN_PAGE_*`、`PluginPageAuth`、
`asset_token`、`page-bridge-sdk`、`register_web_api`、`_match_registered_web_api`、
`turn_on_plugin`/`turn_off_plugin`、`build_registry_source`、`market/categories`、
`plugin_page_bridge`、`nonce|secret_token|hmac`、`symlink|follow_symlinks|realpath`、
`sandbox`、`views\.`、`table\.headers`、`detail\.handlerGroups`、
以及在 `dist/assets/*.js` 上对每条 i18n 键做**带引号字面串**的计数。
