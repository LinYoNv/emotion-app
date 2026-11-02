# 陪伴小屋

「陪伴小屋」是 7.0.0 唯一公开的 AstrBot Dashboard 页面。首页展示角色状态与能力入口；各专题按角色维护共享默认，私聊用户页和群聊页细调对应窗口。配置、迁移预览、授权、诊断与维护均在小屋内完成。

`index.html`、`app.js` 与相关样式是页面规范源。其他工作区使用各目录的 `workspace.html`，由 `tools/sync_framework_page.py` 构建成小屋内部资源；不要手工编辑生成的 `workspaces/` 或 `workspace-bundles/`。

修改页面后运行：

```powershell
python -X utf8 tools/sync_framework_page.py
python -X utf8 tools/sync_framework_page.py --check
```

需要本地预览时用 `--preview <扫描目录之外的路径>`。预览数据只用于开发，不表示已经保存进插件；正式权限、配置生效和数据归属以 AstrBot Dashboard 中的实际回执为准。
