# 上游协作记录（陪伴插件 menglimi/astrbot_plugin_private_companion）

> 🔴 **这份文档存在的唯一理由**：我们和上游的每一次交互（PR / issue）都是**跨会话、跨天**的事，
> 靠记忆必然漏。**提完立刻登记，状态变化就更新。**
>
> 判断"要不要提"的规矩见 `AGENTS.md` 铁律第 9 条：
> **复现点在陪伴最新版、且原生 AstrBot 上同样会犯 ⇒ 提**；
> 宿主兼容缺口（Starlette/Quart 形状差异那类）**不提**，那是我们自己的事。

最后更新：2026-11-03

---

## 一、总览

| 编号 | 类型 | 标题 | 状态 | 结果 |
|---|---|---|---|---|
| [#443](https://github.com/menglimi/astrbot_plugin_private_companion/pull/443) | PR | 让已确认的核心管理员窗口能从「开始使用」重置 | ✅ **已合并** | 进 7.0.14 |
| [#476](https://github.com/menglimi/astrbot_plugin_private_companion/pull/476) | PR | 保存角色档案后不再误报「还有未保存的改动」 | ✅ **已合并** | 进 7.0.14 |
| [#467](https://github.com/menglimi/astrbot_plugin_private_companion/issues/467) | issue | 人设在上游改名后，核心管理员窗口永久失配 | ✅ **已修** | 作者出 PR #478 修掉，进 7.0.14 |
| [#510](https://github.com/menglimi/astrbot_plugin_private_companion/issues/510) | issue | 私聊背景四项 `authorization_denied`（`private_owner_without_grant` 缺失） | ⏳ **等回复** | 询问"有意收紧还是漏了标记" |

**净收益**：两个 PR 全部合入 7.0.14；一个 issue 促成作者自己修；一个 issue 待回。

---

## 二、逐条详情

### PR #443 —— 「开始使用」重置入口

**问题**：`receive_binding()` 在窗口已确认时拒绝改绑，并回复
「请先在『开始使用』重置首次设置」；但该页面**没有重置入口**，也没有对应 HTTP 路由
⇒ 操作员被锁死（命令让去页面，页面没那个按钮）。

**根因（关键认知）**：**删掉 `binding.json` 不等于重置**。
`binding_state()` 在文件缺失时会从持久身份账本恢复窗口，并照样返回 `confirmed: True`，
而 `_core_target()` 会因为过期的 `persona_id` 继续判不匹配。
⇒ 必须把 `confirmed` 翻回 `False`。

**改动**：新增 `POST .../setup/binding/reset`（body `{"confirm": true}`）+
两个 controller 各一个 `reset_binding()` + 「开始使用」第 1 步补「解除确认并重新绑定」按钮
（走现有 `CompanionConfirm` 二次确认）。

**作者反馈**：「方向认可：这就是 `receive_binding` 里一直提示、却没人实现的 reset 入口。」
并指出分支上没有 CI —— **原因是 fork 的 Actions 默认关闭**，在 fork 里启用后 CI 全绿。

**状态**：✅ 已合并，进 7.0.14。

---

### PR #476 —— 保存后误报「还有未保存的改动」

**问题**：在「自我」保存角色档案**成功之后**，切任何分区都弹「还有未保存的改动」。

**根因**：`isDirty()` 比的是 `fieldValues` 与 `draft.fields`，而草稿一旦生成
就一直留在实例里、**从不随外部保存更新**。主编辑面（工作台）保存时只更新自己的
`state.base`，不碰草稿那份旧基线 ⇒ 两值恒不相等 ⇒ 恒脏。

**改动**（两处必须同时有）：
1. `workbench-page.js` 保存成功后按分区派发 `companion:*-profile-updated`
2. `role-profile-draft.js` 监听并在**自己是脏源时**丢弃过期草稿
   ⚠️ 必须带 `target` 判断，否则会连另一种草稿一起清掉（等于用户真丢编辑）

**状态**：✅ 已合并，进 7.0.14。

---

### issue #467 —— 人设改名后窗口永久失配

**问题**：AstrBot 人设在上游改名后，`binding.json` 里还是旧名 ⇒
`_core_target()` 精确比较失败 ⇒ `setup_core_window_not_configured`，永久失配。

**为什么 7.0.13 自愈救不到（本 issue 最有价值的部分）**：
作者加了 `_reviewed_identity()` / `_repair_parked_binding()`，
但执行顺序是 `target = _core_target(parked)` → `if target is None: raise`，
**后面的自愈永远不可达**。它只修 `role_id` 漂移，不修 `persona_id` 漂移。

**作者回复（2026-11-02）**：
> 分析准确，时间线也对：`_core_target()` 的精确名比较会先返回 `None`，
> 导致 `_repair_parked_binding()` 永远走不到。
>
> 1. #443 已合并（先解死锁）
> 2. **#478 已合并**：按你建议的方向做了 `_core_target()` 的下沉容错 ——
>    名字对不上时，若该 Bot 下**只有一个人格映射**，直接认领并交给后续
>    `_repair_parked_binding()` 写回新名字；同一 Bot 下有多个人格时仍拒绝，不猜。

**状态**：✅ 已修（作者出 PR #478），进 7.0.14。
**我们的验证**：7.0.14 里 `multi_controller.py:255-267` 已按上述逻辑实现。

---

### issue #510 —— 私聊背景四项 `authorization_denied`（待回）

**问题**：私聊里每条回复都打 4 条降级警告，真实码是 `authorization_denied`：

```
code='authorization_denied' detail=optional background is unavailable
```

**根因**：`object_access.py` 里有一组「私聊主人免授权」的豁免
（`role.self_review.read` / `role.expression.read` / `calendar.read` / `calendar.write`
都设了 `private_owner_without_grant=True`），但**同样读主人自己私聊数据**的四项没设：

| action | 免授权 | 对应 section |
|---|---|---|
| `relationship.memory.read` | ❌ | relationship |
| `role.life.read` | ❌ | life |
| `role.growth.read` | ❌ | growth |
| `memo.list` / `memo.read` | ❌ | open_items |

调用方**确实传了** `allow_private_owner=True`（`execution/service.py:227`），
是定义里没打标记 ⇒ 走不到豁免 ⇒ 必须显式 grant。
而 grant 按 `settings.capabilities` 签发（`multi_authority.py:322`），
默认模板里没这四项 ⇒ 一个都不签发。

**我们的临时解法**（已在生产配置里）：手动往 `multi_host.capabilities` 加这五项
⇒ grant 8 → 12 条 ⇒ 四个 section 全部恢复。

**影响评估（决定"先不改"的依据）**：不阻断聊天（`optional()` 失败只 `return`），
只是少了四块背景（知心感 / 临场气息 / 成长感 / 未完事项），
外加 `memo.*` 工具会返回权限错误（按设计如实报错、不崩）。
⇒ **1-2 天内影响不大**，所以先提 issue、不改代码、不提 PR。

**issue 里明确问的**：这是有意收紧还是漏了标记？
如果是有意的（角色生活/成长属于敏感数据），我们改为在文档里提示用户自行配置。

**状态**：⏳ 等回复（2026-11-03 提）。

---

## 三、操作要点（下次提 PR/issue 直接照抄）

### 提 PR 的技术路径

1. `POST /repos/<upstream>/forks` 建 fork（我们无 push 权限）
2. `POST /repos/<mine>/git/blobs` 逐文件上传（base64）
3. `POST /repos/<mine>/git/trees` 带 `base_tree` 只覆盖改动文件
4. `POST /repos/<mine>/git/commits` 带 `parents: [upstream_main_sha]`
5. `POST /repos/<mine>/git/refs` 建分支
6. `POST /repos/<upstream>/pulls` 开 PR（head 写 `<user>:<branch>`）

⚠️ **git 协议在本机代理下必断**（`fetch-pack: unexpected disconnect while reading
sideband packet`），但 **codeload tarball 下载正常** ⇒ 必须走上面的 API 路径。

### 🔴 三个踩过的坑

1. **fork 的 Actions 默认关闭** ⇒ PR 分支上 `push` 事件触发不了 CI。
   在 fork 里 `PUT /repos/<mine>/actions/permissions {"enabled": true}` 后才跑。
   作者看到"没有检查"会以为我们漏了，其实这是 fork 机制。

2. **开 PR 前必须重新读一次上游 head sha**
   （`GET /repos/<upstream>/git/ref/heads/main`），不能用昨天下的 tarball。
   实测：用 `f1e4c17e`（7.0.13）准备时上游已到 `d3ee38b1`（7.0.14）⇒ PR 变 dirty。

3. **官方 release zip 不能用 Linux 的 `unzip`**
   —— 6 个中文页面目录会按 CP437 解码成乱码，插件把同一套页面发现两遍
   （拓展页 4 → 5），测试因此变红。必须用 Python `zipfile` / `tarfile`。

### 提 issue 的判断

- **复现点在陪伴最新版 + 原生 AstrBot 同样会犯** ⇒ 提
- **宿主兼容缺口**（Starlette/Quart 形状差异、OneBot 消息段解析）⇒ 不提，我们的活
- **会改变既有契约的改法** ⇒ 写进 issue 当建议，**不直接塞进 PR**

---

## 四、我们自己的兼容层改动（不是上游的活，登记在这里防混淆）

这些**不提 PR**，但和上面的问题同批发现，容易混：

| 问题 | 归属 | 修在哪 |
|---|---|---|
| 引用消息识别不了（`Reply` 少 7 个字段 + 不认 `reply` 类型） | 我们的兼容层 | `host/.../components.py`、`emotion_app/web/default_llm_reply.py` |
| 图片没回应（没实现"落成真实字节"） | 我们的兼容层 | `host/.../adapters/onebot.py`（`get_image` + 容器路径映射） |
| 登录会话只存内存（重启掉线） | 我们的后端 | `emotion_app/web/auth.py` |

---

## 五、待办

- [ ] #510 等作者回复；若确认是漏标记 ⇒ 提 PR；若是有意 ⇒ 补文档提示用户自治
- [ ] 7.0.14 已装（我们的平台 + 本机 AstrBot），下次上游出新版时按
      「拉取 → 查看改动 → 分析问题是否修复 → 安装」流程走
- [ ] `multi_host.capabilities` 那五项**重装/重置后会丢**，每次升级后检查一次
