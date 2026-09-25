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
- **人设级参考图未接**：插件有 `photo_persona_reference_image_path`（角色固定参考图），
  我们的 `image_tool` 只接受模型每次传的 `reference_image_path`。
  想让"她每次都长得一样"，应把人设的参考图作为默认值接进来。
- **vibe 模式未实现**：账号没额度，实现了也用不了。
