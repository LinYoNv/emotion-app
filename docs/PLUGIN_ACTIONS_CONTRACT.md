# 插件动作面契约（照 AstrBot 官方实现，2026-10-31）

> 本文是**后端与前端共用的唯一契约**。改动任何一个字段都要同时改这份文档。
> 权威依据全部是**真实 AstrBot 源码/构建产物**，不是我们的旧内部构建：
>
> * `astrbot/core/star/star_manager.py:1010` `reload()`
> * `astrbot/core/star/star_manager.py:1753` `uninstall_plugin()`
> * `astrbot/core/star/star_manager.py:1813` `uninstall_failed_plugin()`
> * `astrbot/dashboard/api/plugins.py:813/930/1104`（readme / uninstall / reload）
> * `astrbot/dashboard/services/plugin_service.py:154` `reload_plugin()`、`:1737` `uninstall_plugin()`
> * 前端 `dashboard/dist/assets/InstalledPluginsTab-DSnl1LUd.js`（卡片动作）
> * 文案 `dashboard/dist/assets/index-BiTHkcB1.js` 的 `features/extension` 命名空间

## 0. 与官方**刻意不同**的三处（都是为了不制造静默失败）

| 项 | 官方 | 我们 | 理由 |
|---|---|---|---|
| 删除落盘 | `remove_dir()` **直接删** | **与官方一致：直接删** | 我一度加过"回收站"，被情绪否掉：插件代码是**可再生的**（`repo:` + 重新安装），真正不可再生的是配置与数据，而那两样由两个勾选项显式决定。给可再生资源做回收站 = 解决错了对象，还多造一个用户不知道怎么清理的目录 |
| 删除 `reserved` 插件 | 抛 `该插件是 AstrBot 保留插件，无法卸载` | 我们**没有** reserved 概念（全部是用户自己放的插件） | 不编造一个我们并不遵守的字段；`can_uninstall` 恒为可用性真值 |
| 重载 | `reload()` 内部 `self.load(specified_module_path)` | 走我们自己的 `PluginLoader.load_one()` + 明确的拆解步骤 | 我们的加载器有整树清 `sys.modules`、别名安装、`companion_v2` 归零等官方没有的硬约束 |

## 1. `GET /api/v1/plugins`（列表，已有，**新增字段**）

`plugins[]` 每项在现有字段基础上**追加**（只增不改，避免打断现有面板与判据）：

```jsonc
{
  "can_reload":    true,   // 现在能不能重载（= 有 main.py + 不是 inactivated）
  "can_uninstall": true,   // 现在能不能删除（= 能解析出源码目录）
  "reloadable_reason": "",  // can_reload=false 时**必须**说明原因，否则前端只能显示一个灰按钮
  "uninstallable_reason": "",
  "source_path": "/root/Ai-works/情绪app/plugins/astrbot_plugin_x",  // 源码目录（= 会被删掉的那个）
  "config_path": "/root/Ai-works/情绪app/data/plugins/config/astrbot_plugin_x_config.json",  // 勾"删配置"时会删的**具体文件**
  "data_path":   "/root/Ai-works/情绪app/data/plugins/astrbot_plugin_x"                       // 勾"删数据"时会删的**具体目录**
}
```

## 2. `POST /api/v1/plugins/<plugin_id>/reload`

* 鉴权：`_plugin_guard()`（必经 `require_auth()`）。
* 请求体：`{}`（忽略多余字段）。
* 语义（**顺序固定**，每一步失败都要能说清是哪一步）：

  1. 若插件 `state == "inactivated"` → **409** `{"error":{"type":"inactivated","message":"该插件处于停用状态，重载不会让它跑起来 —— 请先启用并重启服务"}}`
  2. 拆解旧实例：`await rec.instance.terminate()`（异常只 warning，不中断）
  3. 摘掉该插件的登记物：handlers（`star_handlers_registry.remove_plugin(前缀)`）、
     LLM 工具（按 `handler_module_path` 前缀）、Web API（含归属表 `_web_api_owners` 同步缩位）、
     tasks、`star_map` / `star_registry` 中该模块的条目
  4. `PluginLoader._purge_plugin_modules(dir_name, path)` 整树清 `sys.modules`
  5. `rec2 = await loader.load_one(path)`；成功则 `await rec2.instance.initialize()`（带超时，复用宿主现有超时配置）
  6. 用新 `rec2` 替换 `loader.records[dir_name]`，重建 `host.report`（loaded/failed/mounted/pages）
* 200：`{"ok": true, "message": "插件「<显示名>」已重载（handlers=N）", "result": <与列表项同形的最小快照>, "restart_required": false}`
* 400：`{"error": {"type": "reload_failed", "message": "<插件自己的 error>", "traceback": "..."}}`
  —— **加载失败的插件重载失败是 400 不是 500**：这是插件代码的问题，不是我们的服务坏了。
* 重载**不需要重启服务**。这是它与「升级」的关键区别，文案里必须体现。

## 3. `DELETE /api/v1/plugins/<plugin_id>`

* 鉴权：`_plugin_guard()`。
* 请求体：`{"delete_config": false, "delete_data": false}`（字段名照官方；都可选，**默认都 false**）。
* 语义（顺序固定）：

  1. 解析**源码目录**：在 `loader.requested_dirs`（**不是** `loader.plugin_dirs` —— 后者在开镜像时指向副本）里
     找第一个含 `<plugin_id>/main.py` 的根，取 `<root>/<plugin_id>`。
     找不到 → **409** `{"error":{"type":"unsupported","message":"解析不到这个插件的源码目录（可能开了源码镜像）。为避免删错目录，已拒绝。"}}`
  2. **先**摘内存（不会失败）：终止实例（异常只 warning）→ 摘登记物（与 reload 第 3 步**同一份实现**）
     → 整树清 `sys.modules` → 从 `loader.records`/`report` 移除 → 从停用名单剔除其 module_path
  3. **再**删磁盘（可能失败）：`rmtree` 掉源码目录。符号链接**只 unlink 链接本身**，绝不跟进去删真身。
     失败 → **500** `uninstall_failed`，但插件**已从宿主摘干净**（可再删一次），不是"半拆"。
  4. `delete_config: true` → 删 `data/plugins/config/<plugin_id>_config.json`
     `delete_data: true` → 删数据目录（**我们的** `data/plugins/<plugin_id>/`，
     外加官方那两个名字 `plugin_data/<id>`、`plugins_data/<id>` 做兼容，避免删不到）
     这两项失败**只 warning**（源码已删，插件确实卸载了）。
* 200：
  ```jsonc
  {"ok": true,
   "message": "插件「X」已卸载；配置与数据**已保留**，重新安装后可以继续使用",
   "source_path": ".../plugins/astrbot_plugin_x",
   "removed": {"handlers": 21, "llm_tools": 7, "web_apis": 5, "tasks": 0,
               "config_file": null, "data_dir": null, "inactivated_entry": null},
   "note": "源码目录已删除。要恢复请用「⋯ → 重新安装」从仓库重新拉取"}
  ```

### 🔴 为什么**没有**回收站 / 不自动备份（情绪 2026-10-31 定）

> 「备份什么啊，什么都要备份那不就慢了。回收站也不要了，astrbot 卸载插件的时候
>  有二次选择，如果插件的配置文件没有删除重新安装就可以继续使用。」

* 插件代码**可再生**：`metadata.yaml` 有 `repo:`，「重新安装」一条命令拉回同一版本。
* 配置与持久化数据**不可再生**（记忆库/关系库/用户设置/上传的参考图）——
  这两样由两个勾选项**显式**决定，默认都不删 ⇒ 「卸载 → 重新安装」后开箱即用。
* 回收站会多造一个**用户永远不知道该何时清理**的目录：不清理占盘、清理又怕删错。
  这是净负债，不是保险。

### 前端二次确认（照官方弹窗）

标题「删除插件」+「你确定要删除当前插件「X」吗？」+ 源码目录（等宽）+「操作：」两项：

| 勾选项 | 副标题 | 默认 |
|---|---|---|
| 同时删除插件配置文件 | 具体文件路径 | **不勾** |
| 同时删除插件持久化数据 | 具体目录路径 | **不勾** |

底部「取消 / 确认」。Esc、点遮罩空白都等于取消；**取消时一个请求都不发**。
焦点默认给「取消」——误按回车不该删东西。

## 4. `GET /api/v1/plugins/<plugin_id>/readme`

* 200：`{"readme": "<markdown 原文>", "truncated": false}`
* 没有 README → 200 + `{"readme": null, "truncated": false}`（**不 404**：插件存在，只是没写文档）
* 未知插件 → 404（走 `_plugin_guard()`）
* 复用 `PluginHost.plugin_readme()`，**不新写一套读文件逻辑**。

## 5. 「更换插件源」

**不新增接口**。前端弹一个输入仓库地址的框，然后调**已有的**
`POST /api/v1/plugins/<plugin_id>/update`，body 带 `{"repo": "<新地址>", "proxy": "..."}`。
后端已有 `repo_override` 分支。为什么这样做：官方那套 `plugin-sources` 依赖"安装源"这套持久化，
我们**没有**（插件是直接放目录的）—— 硬造一套会变成"写进去不生效"的假能力。

## 6. 前端约定

### 6.1 视图互斥（修 2026-10-31 情绪报的图 3 缺陷）

`#tab-plugins` 里必须有两个**互斥**的顶层容器：

* `#plugins-list-view` —— 列表态：启停标题+提示+列表+保存行、升级标题+提示+代理行+卡片列表+结果、
  拓展页标题+提示+列表。**整块**一个容器，不是逐个 `hidden`。
* 详情 / 拓展页 iframe / 配置 —— 沿用现有的 `#plugin-detail-wrap` / `#plugin-frame-wrap` / `#plugin-config-wrap`。

`showPluginsView(which)` 是**唯一**决定显隐的地方：`which === "list"` 才显示 `#plugins-list-view`。
👉 这才是根因修法：原来是逐个元素 `setPluginSwitchVisible()`，**新增一块就漏一块**，
漏掉的那块在插件独立页里依旧可见（就是图 3 的红框）。

### 6.2 卡片动作（照官方卡片的 ⋯ 弹出层）

卡片右下角按钮组：`详情` / `⋯`。`⋯` 弹出菜单，条目顺序固定：

| 条目 | 调什么 | 不可用时 |
|---|---|---|
| 行为 | `#/plugins/<id>`（详情页） | 恒可用 |
| README | 同上，但详情页滚到 README 区块 | 恒可用（没 README 时详情页显示"暂无文档"） |
| 插件设置 | `#/plugins/<id>/config` | 恒可用 |
| 重载 | `POST .../reload` | `can_reload=false` → 灰 + title 显示 `reloadable_reason` |
| 重新安装 | `POST .../update`（与「升级」同一接口） | `state=failed` → 灰 |
| 更换插件源… | 输入框 → `POST .../update` 带 `repo` | 同上 |
| 删除插件 | `DELETE ...`（红色，二次确认） | `can_uninstall=false` → 灰 + title |

* 删除的二次确认文案**必须**包含：目标目录路径、会落到哪个回收站、数据目录默认**不删**。
* 菜单打开时点外部/按 Esc 关闭；同一时刻只允许一个菜单打开。
