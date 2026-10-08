# 生图参考图（selfie / edit）

> 2026-09-25 修。此前**参考图只被记录、不发给上游** ——
> `backend.generate()` 里一行「后端不支持参考图，已忽略」就把它丢了。
> 这正是本项目最贵的那类 bug（**静默丢弃**：无报错、用户只能凭感觉报
> 「好像没用上参考图」，aitag 那边有过同类事故）。

---

## 1. 修好之后的行为

| `kind` | 走哪条路 | 端点 | 上游实测 |
|---|---|---|---|
| `selfie` | **精准参考**（`director_reference_*` 五数组） | `POST /v1/images/generations` | **200 出图** ✅ |
| `edit` | **真 img2img** | `POST /v1/images/edits` | **200 出图** ✅ |
| 其它 | 纯文生图（不带参考图） | `POST /v1/images/generations` | 200 ✅ |

参考图统一转成 **data URI**（`reference_data_uri()`）再提交。

---

## 2. 🔴 两条**实测出来的上游硬约束**（照抄插件会踩）

### ① 精准参考**只支持 NAI 4.5 系**

```
nai-diffusion-5-full   + director_reference_*  → 500
    novelai adaptor: precise reference is only supported by NAI 4.5 models
nai-diffusion-5-curated + director_reference_* → 同样 500
nai-diffusion-4-5-full + director_reference_*  → 200 出图 ✅
```

⚠️ 陪伴插件 `astrbot_plugin_nai_image` 的 README 写「v4.5f / v5f 实测均可用」——
**那是另一套网关的结论，不能照搬到 `api.syuan.org`**。

→ 我们的做法：模型不在 4.5 系时**自动切到 `nai-diffusion-4-5-full`**，
**并打 WARNING**（切换会改变画风，静默切换 = 用户不知道为什么画风变了）。

### ② vibe 模式需要「氛围迁移编码额度」

```
reference_image_multiple → 400
    处理氛围转移图片失败: 第 1 张氛围迁移自动编码失败: 当前没有可用的氛围迁移编码额度
```

→ **当前账号没有该额度**，所以本项目**不用 vibe**，走精准参考。

### ③ 参数形状（实测确认，不是猜）

| 项 | 结论 |
|---|---|
| `parameters` 嵌套 | ✅ 被读取（非法值会 400） |
| 顶层扁平字段（`steps`/`scale`） | ✅ **也**被读取（我们现有的 `EMOTION_IMAGE_EXTRA_PARAMS` 写法没问题） |
| 字段名 | `director_reference_images` / `_strength_values` / `_secondary_strength_values` / `_information_extracted` / `_descriptions`，**五个数组严格等长** |
| `director_reference_information_extracted` | **必须是 `1.0`**（其他取值被上游参数校验拒绝） |
| `action` 字段 | **不是必需的** |
| 参考图格式 | **data URI**（非法 base64 会被点名：`failed to decode base64 string`） |
| 张数上限 | 8 张（超出截断并打日志） |

---

## 3. 代码位置

| 文件 | 改了什么 |
|---|---|
| `emotion_app/domain/image_gen.py` | 新增 `reference_data_uri()`、`NAI_DIRECTOR_MODELS`、`_apply_director_reference()`、`ImageClient.edit()`；`generate()` 支持 `reference_images`；抽出 `_base_body()` / `_post()` |
| `emotion_app/domain/backends.py` | `OpenAIImageBackend.generate()`：**真的把参考图发出去**（原来只记日志） |
| `emotion_app/domain/image_tool.py` | `_failure_result` 新增 `reference_unreadable` 分支（如实告诉模型"没拿到参考图"） |

**失败语义**：参考图读不到 → `ImageError(code="reference_unreadable")` →
工具返回 `status="reference_unreadable"` + `must_not_claim_sent=True`，
**绝不降级成"悄悄画一张无关的图"**。

---

## 4. 判据与验证

| 层 | 判据 | 结果 |
|---|---|---|
| 单元（`tests/test_s6_image_reference.py`，**23 条**） | data URI 读取/拒绝、五数组等长、`information_extracted==1.0`、模型自动切换+日志、截断、img2img 端点、读不到必须抛、工具入口接线 | **23 passed** |
| 注入验证（`tools/inject_ref_image_check.py`，**10 组**） | 含「参考图又变回静默丢弃」「切模型不打日志」「读不到返回空串」 | **10/10 全被抓住**，逐字节还原 |
| 真环境（`tools/ref_image_live_check.py`） | 真后端 + 真上游 + 真参考图 | **3/3**：selfie 出图、edit 出图、读不到如实拒绝 |

### 怎么复核

```bash
cd /root/Ai-works/情绪app
.venv/bin/python -m pytest tests/test_s6_image_reference.py -q      # 23 passed
.venv/bin/python tools/inject_ref_image_check.py                   # 10 组全抓住
.venv/bin/python tools/ref_image_live_check.py                     # 真上游，会产生费用
```

---

## 5. 还没做的（诚实边界）

- **`edit` 只取一张源图**：插件支持多张主输入图，我们目前单张。
- **vibe 模式未实现**：账号没额度，实现了也用不了。
- ~~人设级参考图未接~~ → ✅ **已接**（§6 与 §7）。

### ✅ 官方文档背书（2026-09-25 补，来自作者发的 wiki）

「**改图原图缺失时按设计失败关闭，不应悄悄生成一张新图**」——
这句话在官方 wiki 里**独立出现三处**：
`guide/troubleshooting.md`、`guide/bug-fixing.md`、`guide/media-integrations.md`。
与我们的 `reference_unreadable` **硬失败**、`edit` 缺路径**照旧拒绝**完全一致。

⚠️ **wiki 对 NAI 的参考图协议零覆盖**：`director` / `vibe` / `精准参考` /
`reference_strength` / `information_extracted` 在全部 wiki 里 **0 命中**
（两个子代理独立 grep 验证）。因为 wiki 把「NAI 参考图走哪个模型」
**划给了 `astrbot_plugin_nai_image` 扩展**去适配。
→ 本文 §2 那两条（NAI 5 不支持精准参考、vibe 无额度）是**我们这台网关**
（`api.syuan.org`）的性质，**不是陪伴体系的承诺**；引用时要说清这一点。

---

## 6. 🆕 人设固定参考图（2026-09-25 补）

**为什么还要这个**：光让参考图"能发出去"不够 —— 每次还得**模型自己传路径**，
同一个角色画出来每次都不一样。把角色图设成默认值，`selfie` 才稳定。

### 用法

```bash
# .env
EMOTION_IMAGE_PERSONA_REFERENCE='/path/to/角色图.png'
```

配好之后：`kind="selfie"` 且模型**没**指定 `reference_image_path` → 自动带上它。

### 🔴 三条边界（都有判据盯着）

| 边界 | 为什么 |
|---|---|
| **只给 `selfie`** | `edit` 的语义是"改**用户给的那张图**"，拿角色图兜底会把语义**静默换掉**（画出来完全不是用户要改的图）→ `edit` 缺路径时**照旧拒绝** |
| **`text2img` 也不给** | 风景/物件画面注入角色参考图 = 画错东西 |
| **读不到就响亮失败** | 不静默降级成纯文生图（那样用户只会觉得"怎么画得还是不像"，无从排查） |

另外两条实现约束：

* **显式路径优先** —— 模型真给了 `reference_image_path` 就用人家的。
* **必须留痕** —— 走人设兜底时打一条 `selfie 未指定参考图，使用人设固定参考图`
  （参考图"从哪来的"是排查时唯一的现场证据；静默替换是本项目最贵的一课）。

### 代码位置

| 文件 | 改了什么 |
|---|---|
| `emotion_app/infra/config.py` | `ImageConfig.persona_reference` + env 绑定 `EMOTION_IMAGE_PERSONA_REFERENCE`；`public_view` 只暴露 `persona_reference_configured` 布尔（**不回路径全文**，少一条泄漏目录结构的通道） |
| `emotion_app/domain/image_tool.py` | `ImageToolService(persona_reference=...)` + `set_persona_reference()`（切人设用）；`__call__` 里给 `selfie` 兜底 |
| `emotion_app/__main__.py` | 把 `config.image.persona_reference` 接进服务 |

### 判据

| 组 | 条数 | 覆盖 |
|---|---|---|
| `tests/test_s6_image_reference.py` §6 | **8** | 兜底生效/显式优先/不漏进 edit/不漏进 text2img/未配时不崩/读不到响亮失败/留痕/切人设 |
| `tests/test_assembly.py` | **2** | ⭐ **接线**：入口把配置传进服务了；env 键名真的绑上了 |

**注入验证**：`tools/inject_persona_ref_check.py` **10 组全被抓住**。
⚠️ 第一轮有 **3 组没抓住**（⑤ 不判空 / ⑧ 装配漏传 / ⑩ env 绑定去掉）——
说明判据只测了**函数**没测**接线**。已补 `tests/test_assembly.py` 两条并收紧 ⑤，
第二轮 10/10。**又一次印证「函数对 ≠ 接线对」。**

### 还没做的

* **真正的角色图要你自己放** —— 仓库里/本机没有现成的人设参考图，
  代码只负责把它接上（`photo_persona_reference_image_path` 在插件里也是空的）。
  → 现在**在面板上上传**（§7），不需要再改 `.env`。

---

## 7. 🆕 per-persona 参考图：**面板上传**（2026-09-25 下午）

> 情绪 2026-09-25：「**你判断一下嘛，用户能填的就让用户改**」

### 为什么从 `.env` 改成"面板上传"

§6 那版只有 `EMOTION_IMAGE_PERSONA_REFERENCE` **一个**全局入口。三个问题：

1. **手机 App 壳上没有文件系统路径可填** —— 那是**后端机器**的路径；
2. 让用户改 `.env` = 要求他**登服务器改配置再重启** —— 那不叫"用户能改"；
3. 人设是**一对多**的（一张卡一个角色）→ 全局一个值会让**所有角色共用一张脸**。

### 三级优先（**唯一判据**在 `domain/persona_ref.py`）

```
① 模型显式给的 reference_image_path   （最高，在 image_tool.py）
② 人设自己的 reference_image_path     ← 面板上传的，per-persona
③ 全局 EMOTION_IMAGE_PERSONA_REFERENCE（兜底：只有一个角色时最省事）
```

⚠️ **每一级都打日志点名 `source`**（`explicit`/`persona`/`global`）——
   用户报"画得不像"时，必须能一眼看出是"人设图没上传"还是"全局兜底在顶"。

### 面板操作

`人设` 页 → 选中一个角色 → **角色参考图** → 选图片 → 上传。支持 png/jpg/jpeg/webp，≤12 MB。
图上会显示缩略预览；删除按钮清字段与文件。

### 存储

| 项 | 值 |
|---|---|
| 落盘目录 | `data/persona_refs/`（与人设数据同源 → 备份 `data/` 一起带走） |
| 字段 | `Persona.reference_image_path`，存**纯文件名**（防路径穿越；同 `avatar_path` 的理由） |
| 命名 | `<人设id><扩展名>` —— 一个人设**只有一张**，重传覆盖（用 id 不用名字：名字可改可重） |
| 换扩展名 | 旧文件**自动清掉**（否则磁盘留垃圾、且与字段不一致） |

### 代码位置

| 文件 | 改了什么 |
|---|---|
| `emotion_app/domain/persona_ref.py` | 🆕 落盘目录 + `resolve_persona_reference()`（**唯一**判据）+ `is_safe_name()` |
| `emotion_app/domain/tool_context.py` | 🆕 请求级"当前人设"（`ContextVar`，按 asyncio 任务隔离） |
| `emotion_app/domain/persona.py` | 新字段 `reference_image_path` + 进 `to_dict`/`from_dict`（否则重启即丢） |
| `emotion_app/domain/persona_repo.py` | `update()` 白名单放行该字段 |
| `emotion_app/domain/chat.py` | 工具执行前后用 `tool_context.tool_scope()` 包住（**非流式 + 流式两条路**） |
| `emotion_app/web/api_admin.py` | 🆕 `POST/GET/DELETE /personas/<id>/reference`（**三条都要求鉴权**） |
| `emotion_app/web/static/{index.html,app.js,app.css}` | 面板控件（缩略图 + 上传 + 删除） |

### 🛠 为什么用 `ContextVar` 而不是成员变量

工具 handler 的签名是 `Callable[[dict], Awaitable[ToolResult]]` —— **只有参数**，
没有"这一轮是哪个会话/哪个人设"。三条路：

| 做法 | 为什么不选 |
|---|---|
| `ImageToolService._current_persona` 成员变量 | 🔴 **并发下会串**：两个会话同时生图，后一个改掉前一个的人设 → **画错脸**；且单测串行跑**看不出来** |
| 改 `ToolHandler` 签名加 `persona=` | 要动 `ToolRegistry` / 所有注册点 / 所有测试，收益一样 |
| ✅ `ContextVar` | 天生按 asyncio 任务隔离 → 无竞态；不改协议；测试可直接设 |

⚠️ **只包 `execute` 那一句**，不要把 `yield` 也包进去：async generator 没有自己的
context 副本（PEP 567 只给 Task 发），`yield` 期间挂着会让"当前人设"暴露到**调用方**的上下文里。

### 判据

| 层 | 文件 | 条数 | 覆盖 |
|---|---|---|---|
| 函数 | `tests/test_s6_image_reference.py` §6 | 8 | 兜底/显式优先/不漏进 edit 与 text2img/未配不崩/读不到响亮失败/**日志点名 source**/切人设 |
| **真 HTTP** | `tests/test_persona_ref_http.py` | **15** | 三条路由**都鉴权**（反向：未登录 401）/ 上传→字段+文件+可服务 / 换扩展名清旧文件 / 删除清两处 / 文件已丢仍能清字段 / 拒非法扩展名·空文件·超限·缺字段·人设不存在 / **路径穿越** / 名字安全契约 |
| **端到端** | `tests/test_persona_ref_e2e.py` | **5** | ⭐ **换人设→发出去的图跟着换** / 无人设回落全局 / 人设优先于全局 / 事后不残留上下文 / edit 不吃人设图 |
| 接线 | `tests/test_assembly.py` | 6 | 路由都在 / 白名单放行 / 序列化往返 / chat 两条路都设上下文 / `complete()` 真传 `persona=` |
| 真浏览器 | `tools/persona_ref_panel_check.py` | **17** | 控件在 / 真 UI 选图→按钮解禁 / 上传后后端真有 / **预览图 naturalWidth>0**（不是破图）/ 删除回到未设置 / 零 JS 报错 |

**注入验证**：`tools/inject_persona_ref_check.py` **23 组**（第一代 10 + 第二代 13）。

### 🔴 这个功能让我踩到的两个坑（值得记）

1. **真浏览器判据抓出一个真缺陷**：我原来在上传成功后往状态行塞"已上传"，
   而那个元素（`#p-ref-hint`）的**唯一含义**是"当前配置是什么" ——
   瞬时文案把它盖掉，用户传完反而**看不到现在用的是哪张图**。
   → 改成重新渲染持久状态，反馈由**预览图出现**给出（比一行字更直接）。
   17 条判据里 ⑦c/⑨b 两条就是抓这个的。

2. **注入脚本自己有致命缺陷，害我丢了一行 `require_auth()`**：
   第一版把"注入前"的哈希**只放内存**，然后我被 10 分钟墙钟杀掉 ——
   杀掉时源码正停在**注入态** → 下次运行时"注入前"读到的就是**已改坏的文件**
   → 结尾"还原校验 ✅ 逐字节一致"是**假绿**（比对的是它自己的注入产物）。
   直到全量测试报 `POST 未鉴权也能上传：500` 才暴露。
   ✅ **已修**：基线**落盘**到 `tools/.inject_persona_ref_baseline.json` +
   启动时**自检残留**（不一致就拒绝运行）+ 注册 SIGTERM/SIGINT 抢救还原。
   **通则：任何"改了再还原"的工具，基线必须落盘到工具之外** ——
   否则"还原成功"这句话本身就无法被验证。

### 复核命令

```bash
cd /root/Ai-works/情绪app
.venv/bin/python -m pytest tests/test_persona_ref_http.py tests/test_persona_ref_e2e.py -q   # 20 passed
.venv/bin/python -m pytest tests/test_assembly.py -q                                          # 19 passed
.venv/bin/python tools/persona_ref_panel_check.py                                             # 17/17（真浏览器）
.venv/bin/python tools/inject_persona_ref_check.py                                            # 23 组全抓住
```
