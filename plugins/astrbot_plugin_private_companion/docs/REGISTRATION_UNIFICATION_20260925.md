# 单插件注册与页面入口

当前发布合同：插件目录、`metadata.name`、`main.py` 中的 `PLUGIN_NAME` 和 `@register` 均为 `astrbot_plugin_private_companion`，内部机器版本为 `7.0.0`。历史代码中的 `companion_v2` 是领域包名，不能当成第二个可安装插件。

唯一公开 Dashboard Page 是「陪伴小屋」。`pages/宿主管理/`、`pages/日记与生活/`、`pages/关系与记忆/`、`pages/关系与权限/`、`pages/人物设定工坊/` 使用 `workspace.html`，由 `tools/sync_framework_page.py` 生成并嵌入小屋。设计预览和旧页面快照不参与页面发现及安装。

Content、Image、Screen、Reality、Together、Game、Live 是本插件的七项原生内部能力。`astrbot_host/native_extensions/builtins.py` 负责装配，生命周期沿用本体；外部兼容提供方只作为来源或只读状态，不能变成这些能力的必装依赖。

旧版 6.7.0 以只读来源参与迁移；新版运行数据独立保存。旧版备份、可加载工作树和测试副本须置于 AstrBot 一级插件扫描目录之外，以免重复注册。发布时核对交付包内唯一根级 Python 入口、唯一页面、七项能力、注册名及升级/回退合同。
