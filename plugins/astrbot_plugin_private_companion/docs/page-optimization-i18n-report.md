# Page 响应式、加载性能与配置汉化改动报告

更新时间：2026-08-10

基线提交：`fa6fc48f42f7886306b20bdda30fd9ca4a25d3bf`

适用插件：`astrbot_plugin_private_companion`

目标上游：`menglimi/astrbot_plugin_private_companion:main`

## 一、改动背景

本次改动针对插件拓展页（Page）和配置界面三个相互关联的问题：

1. 页面在手机、平板和窄桌面窗口中存在固定宽度、长文本、粘性工具栏和弹窗溢出风险。
2. 页面首次打开时会同时加载体积较大的模型配置、QQ 空间脚本，并较早请求用户和群组名单，首屏交互会被非首屏内容拖慢。
3. 配置 Schema 中仍有少量纯英文说明，部分下拉选项只有机器值，用户难以理解含义；翻译不能改变配置键、默认值或实际保存值。

实现依据是本机 AstrBot 4.27.2 镜像中的官方 Page 指南和 Page 服务实现：Page 通过 `window.AstrBotPluginPage` bridge 通信，模块资源应使用相对路径，以便 Dashboard 注入当前 `asset_token`；国际化配置使用 `.astrbot-plugin/i18n/<locale>.json` 的嵌套 `config` 结构，`options` 保持机器值，显示文本使用 `labels`。

## 二、响应式布局改动

文件：

- `pages/companion-panel/css/polish.css`
- `pages/陪伴面板/css/polish.css`
- 两个 `index.html` 中的 CSS 缓存版本已同步为 `20260810-responsive-containment-v1`；合并上游照片上传改动后，`app.js` URL 也保留 `reference-upload-v2` 并追加 `page=lazy-bridge-v1`。

### 2.1 全局约束

- 为 Page shell、面板、网格子项和主要工作区补充 `min-width: 0`，避免 Flex/Grid 子项把页面整体撑宽。
- 对图片、视频、Canvas、输入控件和按钮增加父容器内最大宽度约束。
- 对代码块、Provider 卡片、诊断项、缓存行和 QQ 空间卡片启用长字符串断行。
- 表格、世界知识目录和导航保留内部触控横向滚动，不把滚动压力传给整个页面。
- 页面根节点使用 `100dvh`，兼容移动浏览器地址栏变化；弹窗和图片预览限制最大高度并允许内部滚动。
- 加入 `safe-area-inset-*`，避免刘海屏和底部手势条遮挡操作区。
- 修复实验开关的视觉隐藏 checkbox 在窄屏下被全局 `input` 宽度规则撑出容器的问题，隐藏控件固定为 1px，不影响可访问性和点击区域。

### 2.2 分级断点

| 断点 | 处理内容 | 预期效果 |
| --- | --- | --- |
| `max-width: 900px` | 图片缓存、书架等双栏工作区改为单栏；缓存列表限制为可控的内部高度 | 平板不再出现过窄的双栏和相互挤压 |
| `max-width: 760px` | 导航高度降为 58px；工具栏、筛选器、操作按钮允许换行；原本 sticky 的复杂工具栏降级为普通流布局 | 手机和平板竖屏不发生按钮重叠或 sticky 层叠 |
| `max-width: 480px` | 面板内边距、统计卡片和弹窗 footer 收紧；统计区改为单列 | 375px 等窄屏文字和按钮保持在容器内 |

## 三、Page 加载性能改动

文件：

- `pages/companion-panel/app.js`
- `pages/陪伴面板/app.js`
- 两个 `index.html` 的脚本引入方式

### 3.1 官方 bridge 初始化

新增 `getReadyPageBridge()`，所有正式请求在调用 `apiGet/apiPost` 前等待官方 `bridge.ready()`。`debug_http=1` 仍明确走原有 HTTP 调试回退，不会被 bridge 等待阻塞。

这样可以避免拓展页刚插入时 bridge 尚未完成注入就发出失败请求，也符合 AstrBot 官方 Page 示例的初始化顺序。

### 3.2 GET 并发去重

`fetchJson()` 增加仅针对并发 GET 的 in-flight 去重：

- key 使用已加入人格作用域的路径，因此不同 persona 不会互相复用请求。
- 只复用尚未完成的 Promise，不缓存结果，避免配置或运行态数据长期过期。
- 请求成功或失败都会从 Map 清理；POST 和显式 `dedupe: false` 不受影响。

### 3.3 首屏按需数据

用户和群组名单不再在总览完成后立即进入空闲队列，而是延后 1600ms 再交给 `requestIdleCallback`（无该 API 时使用定时器回退）。

- 总览先获得可交互状态。
- 离开总览会取消待执行的名单预取。
- 用户、群聊、学习、观察、主动和实验等需要名单的页面仍会在切换时立即加载。
- 新一轮 `loadAll()` 会取消旧的预取任务，避免旧请求更新当前页面。

### 3.4 大型面板脚本动态导入

`provider-tree.js` 和 `qzone-panel.js` 从 HTML 的 eager `<script>` 改为 `app.js` 中的官方可重写相对 `import()`：

- 总览首屏不再下载或执行这两个模块。
- 打开“模型”或“QQ 空间”页面时才加载对应模块。
- 保留 `window.PrivateCompanionProviderTree` 和 `window.PrivateCompanionQzonePanel` 兼容出口，未改变既有面板 API。
- 每个模块保留 3 个字面量 query 版本，失败后使用新的 query 重试，避开浏览器失败模块缓存。
- 失败时显示转义后的错误和重试按钮；全部重试耗尽后提示重新打开拓展页。

当前两个模块共 `67,269` 字节从首屏脚本路径移出。与基线相比，加入加载编排代码后首屏 JS 仍净减少约 `60,842` 字节。

### 3.5 View Transition 异常收口

快速连续切换 Tab 时，Chromium 可能拒绝被取消的 View Transition Promise。新增统一 watcher 消费 `ready`、`updateCallbackDone` 和 `finished` 的 rejection，清理方向状态，避免正常切换被报告为页面异常。

## 四、配置 Schema 与汉化

文件：

- `_conf_schema.json`
- `.astrbot-plugin/i18n/zh-CN.json`
- `.astrbot-plugin/i18n/en-US.json`

### 4.1 Schema 展示文案

直接在 Schema 中汉化了 2 个可见的纯英文配置项：

| 配置路径 | 原文问题 | 当前中文显示 |
| --- | --- | --- |
| `basic_config.items.enable_p4_b_legacy_score_isolation` | 英文描述和提示 | 隔离旧版关系分数写入；说明兼容开关和 P4 权威边界 |
| `humanized_state_config.items.enable_group_cycle_awareness` | 英文描述和提示 | 允许在已授权群聊中使用最低限度的 Bot 周期语气边界；说明不记录用户健康或私密身体信息 |

另外将 4 个隐藏兼容字段的英文描述改为中文回退文案：

- `enable_proactive_message_review`
- `photo_reference_catalog`
- `photo_reference_catalog_version`
- `photo_reference_catalog_user_cleared`

这些字段的 `key`、`type`、`default`、`condition`、`invisible` 和其他行为属性均未改动。与基线递归比较时，去除展示属性后的 Schema 差异为 0。

### 4.2 中文下拉标签

`zh-CN.json` 新增 17 组嵌套 `labels`，覆盖以下配置路径：

| 配置组 | 配置项 |
| --- | --- |
| `basic_config` | `storage_backend`、`worldview_adaptation_mode` |
| `message_debounce_config` | `private_image_vision_provider_priority` |
| `proactive_generation_config` | `proactive_history_context_mode`、`proactive_chat_bridge_review_mode` |
| `memory_habit_config` | `expression_learning_mode` |
| `emotion_relationship_config` | `smart_silence_judge_mode`、`passive_review_mode`、`passive_review_strength`、`proactive_review_mode`、`proactive_review_strength` |
| `schedule_detail_config` | `daily_diary_form`、`daily_diary_length`、`daily_diary_creativity` |
| `photo_action_config` | `natural_language_photo_generation_mode`、`photo_generation_prompt_format` |
| `legacy_compat_config` | `segmented_proactive_scope` |

所有标签严格按 Schema `options` 顺序一一对应。保存时仍写入原始机器值，例如 `json`、`sqlite`、`auto`、`regex` 等，不会把中文显示文本写回配置。

`en-US.json` 为新增的两组 Schema 文案提供英文覆盖；没有新增机器值，也没有改变 Page 原有标题回退。

## 五、预期行为与兼容性

### 用户侧预期

1. 手机竖屏打开 Page 时，面板、表格、按钮、统计卡和弹窗不会被截断或横向撑开。
2. 平板打开时，书架和图片缓存等复杂区域自动改为单栏，操作区可以换行。
3. 首屏先显示总览和运行状态，用户/群组名单在页面稳定后后台补齐；切换到相关页面时不会等待延迟任务。
4. 模型和 QQ 空间模块只在首次进入对应页面时加载，失败可以重试。
5. 从 AstrBot 官方拓展页打开时使用官方 bridge；本地 `debug_http=1` 调试行为保留。
6. 中文界面能直接理解下拉选项含义，配置保存值和旧版本兼容行为不变。

### 兼容性边界

- 两个 Page 目录继续保持字节级镜像，避免不同语言目录产生行为分叉。
- 未修改后端 API、数据结构、配置键名或默认行为。
- 官方 Page 服务会重写相对 HTML/CSS/JS 资源并注入短期 `asset_token`；动态导入路径已在 AstrBot 4.27.2 中实测可被正确重写。
- 本次没有改变官方的 `Cache-Control: no-store` 策略，也没有引入依赖或构建工具。

## 六、测试与验证结果

### 静态检查

| 检查 | 结果 |
| --- | --- |
| Python AST | 409 个文件通过 |
| `node --check` | 10 个 Page JS 文件通过 |
| JSON 解析 | `_conf_schema.json`、`zh-CN.json`、`en-US.json` 通过；重复键检查通过 |
| PostCSS | 8 个 CSS 文件通过 |
| 双目录镜像 | 10 个对应资源字节一致 |
| `git diff --check` | 通过 |

### 自动化测试

- 新增专项测试：`13 passed`。
- 合并上游最新代码后的相关页面、Schema、图片模型、布局和配置测试：`151 passed`，另有 1 项基线已有的学习页目的地断言失败。资源版本和 eager 脚本断言已同步为当前实现，并未放宽功能校验。
- AstrBot 官方镜像实际加载 `zh-CN` / `en-US`：通过，嵌套中文 labels 可被 PluginManager 读取。
- 全量测试在 10 分钟时达到 `2077 passed, 14 failed, 461 subtests passed` 后中止。失败集中在仓库基线已有的旧资源版本断言、未修改的后端并发/关系分析测试和既有配置权威断言；本次新增测试及本次修改覆盖的行为均通过。

### 真实浏览器烟测

使用 Chromium 对 `375x812`、`768x1024`、`1024x1366`、`1280x800` 四种视口逐一切换 14 个面板：

- 所有面板的 `scrollWidth == clientWidth`。
- 页面根节点和 body 没有横向溢出。
- 首屏 API 请求只有 `page/overview`。
- Provider Tree 和 QQ 空间脚本初始未加载，进入对应 Tab 后才加载。
- 控制台错误和 `pageerror` 均为 0。
- 资产守卫最终进入 ready 状态。

## 七、提交与 PR 范围

本报告与实现代码放在同一 PR，源分支为 `siyuanmc/astrbot_plugin_private_companion` 的独立功能分支，目标为上游 `menglimi/astrbot_plugin_private_companion:main`。工作区不会直接覆盖上游主分支，也不会自动修改正在运行的 AstrBot 数据目录。
