# 距离最终目标还差什么（2026-09-22 第三轮收尾实测）

> 🎯 **最终目标**（情绪 2026-09-18 定，未变）：
> **自用的「虚拟聊天伙伴」** —— 类似爱语原版的 App，**后端部署在自己电脑/服务器，
> 手机只是 WebView 套壳**（瘦客户端/BFF）。总范围：**记忆 · 聊天 · 生图 · App端改配置**。
> 第一版范围：**保证「人设」和「能聊天」正确**。
>
> 本文件**只回答"还差什么"**；每条都带证据（实测数字 / 代码位置），
> 不确定就写**未知**，不写"应该差不多"。

---

## 0. 一句话结论

**后端主干已经通透，卡口全在「手机那一端」。**

- ✅ 后端：四条链路全通、插件跑起来了、工具能执行了、配置能读了 —— 基线
  **主 666 + host 260 = 926 passed**，9 套真浏览器判据全绿。
- 🔴 **手机 App 套壳：零代码、零任务拆解。** 这一步不做，"自用的 App" 就还没成型
  （现在只能在浏览器里用面板）。
- ⏸️ 另有 **9 项要情绪拍板**的决定，其中 3 项不决定就没法往下做。

---

## 1. ✅ 已经达成的（后端主干，都有判据）

| 层 | 内容 | 证据 |
|---|---|---|
| **能聊天** | S1 领域模型 + 提示词组装 + 上游客户端 + `/v1/*`（含 SSE 流式） | `README.md` 状态表 |
| **人设** | S2 人设 CRUD + 面板预览 + 爱语备份格式导入 | `docs/PERSONA_PROMPT_SYSTEM.md`（爱语侧） |
| **多轮记忆** | S3 会话存储 + 三种上下文模式 | `README.md` |
| **宿主合体** | 11 个插件装+跑+挂（266 个 Web API），面板能开能存（**747** 个配置项） | `host/README.md` |
| **四条链路** | 命令派发 / LLM 钩子（记忆注入）/ 模型端口 / 投递端口 | `docs/NEXT_STEPS.md` §0 |
| **🆕 工具执行链** | P-N6：多轮 `step_until_done` + 工具执行器 + 结果回填上游 | `host/tests/test_tool_loop.py`（22 条） |
| **🆕 全局配置** | P-N5：`get_config(umo)` 真读 `cmd_config.json` | `host/tests/test_global_config.py`（9 条） |
| **生图** | function calling + NAI 实测 + 面板渲染 | `爱语/docs/FEATURE_STATUS.md` §5 |
| **平台可扩展** | 加 onebot 不用改代码（注册期硬校验） | `host/astrbot_host/platforms.py` |

**验证强度**：全量 **949 passed** · 真浏览器 **9 套全绿** · 注入验证 **16 + 9 + 10 组全被抓住**
（且逐字节还原）· 数据完整性按**内容**判（`store_sections` 段数，**不是文件哈希** —— 见 §7）。

> 🆕 **2026-09-25 补**：**生图参考图已修好**（原 §3 缺口 2）—— `selfie` 走精准参考、
> `edit` 走真 img2img，两条都经真上游实测出图。详见 `docs/REF_IMAGE.md`。
> ⚠️ 同时实测出两条**上游硬约束**：① 精准参考**只支持 NAI 4.5 系**（该网关 NAI 5 通道会 500）；
> ② vibe 模式需要"氛围迁移编码额度"，**当前账号没有**。

---

## 2. 🔴 最大的缺口：手机 App 套壳（S7）

**状态：零代码。**

| 项 | 实测 |
|---|---|
| App 壳工程 | **不存在**。已搜：`*.gradle` / `*.kt` / `*.java` / `AndroidManifest.xml` / `*.apk` / `capacitor.config*` / `cordova*` / PWA `manifest.json` / service-worker —— 命中的 `.java` **全在 `爱语/delivery-*/jadx`**（那是原版 APK 的逆向产物，不是我们的 App） |
| 任务拆解 | **没有**。`S5/S6/S7` 在 `docs/NEXT_STEPS.md`、`docs/P0_P1_TASKS.md` 里 **0 命中** —— 只存在于 `README.md` 状态表的一行 |
| 打包/分发/部署自动化 | **没有**。只有 `run.sh`（注入 `.env`）；无 Dockerfile / systemd unit / installer |
| 形态 | **未定**：纯 WebView vs WebView+原生桥（`爱语/docs/ARCHITECTURE_THIN_CLIENT.md` §5.3 **推荐后者**） |
| 离线推送 | **刻意没做** —— 投递只写本机会话，手机离线收不到主动消息 |

### 🔴 上手机之前必须先解决的安全前置

1. 服务**现在绑 `0.0.0.0`**，只有一道令牌；**明文 http 下 cookie 可被嗅探**
   → 必须先挂 **TLS 反代** + 打开 `EMOTION_SERVER_HTTPS_ONLY=true`
2. App 里**不能把令牌写进 localStorage**（现有面板已守这条，App 要照做）
3. ⚠️ `爱语/docs/ARCHITECTURE_THIN_CLIENT.md` 的硬边界：**模块系统/悬浮窗/无障碍/通知/麦克风
   必须留手机**；切分原则「需要手机硬件权限的留手机，纯数据与算力的搬后端」

> **这一条是"自用 App"和"自用网页"的分界线。** 只要情绪说做，我可以先出**任务拆解**
> （选型 → 壳工程 → 登录/配置页 → 聊天页 → 打包签名），但那需要先定形态（见 §4 决策 8）。

---

## 3. 功能缺口（按优先级，均已实测）

| # | 缺口 | 现状 | 影响 |
|---|---|---|---|
| 1 | **预设"能执行"比例低** | 对话 **31 → 19** 可执行、语音 **11 → 0**、嵌入 **5 → 0**（`docs/NEXT_STEPS.md` §2） | 面板上配置得出来、真调会失败 —— 正是本项目最忌的"看着能用其实不能用"。**N4 可补到 25+ 且不用写新适配器**（照 `google_gemini` → `google_gemini_openai` 先例） |
| 2 | **生图参考图只记录不生效** | selfie/edit 的参考图**不发给上游** | 「拍张照」类玩法用不了 |
| 3 | **STT 只有配置层** | 适配器 `executable_by_us=false` | 语音转文字不能用（⚠️ 但主陪伴插件**不用 STT**，实测 0 命中；用它的是一起陪伴） |
| 4 | **主动消息的落点/推送未定** | 插件循环在转，但**唯一真实用户 `default` 的 `proactive_private_enabled=False`**（自动建档默认不主动触达） | 想让"它主动找你"得先开开关 + 定策略（见 §4） |
| 5 | **onebot 没接** | 地基铺好（`platforms.py`），缺 ①真 OneBot 连接 ②会话绑平台字段 ③按 umo 分发 | 现在只有 Web 端，零外部耦合（**这是刻意的**） |
| 6 | **插件面板的供应商 key 与 `.env` 不同步且已失效** | `docs/P0_P1_TASKS.md` §5 | 插件调模型会 503 |
| 7 | 情绪状态 / 长期记忆 / 日程 | 平台**没做** | ⚠️ **按路线刻意不做** —— 插件自己会做，重复=白干。**动手前必须先查插件是否已覆盖** |
| 8 | 4 个"插件自身 bug"（33 处调用） | **刻意不补** | 补了=伪造能力（作者判据明确禁止） |

---

## 4. ⏸️ 等情绪拍板的决定（9 项）

### 不做就没法往下走的（3 项）

| # | 决定 | 为什么要你定 |
|---|---|---|
| **8** | **手机端形态**：纯 WebView 套壳 vs WebView+原生桥；后端跑在**哪台机器**；要不要保留"直连上游"回退 | **S7 的第一步**。不定就只能停在网页面板 |
| **3** | **STT 要不要真转写**：只做配置层（小活）vs 真能转写（要音频上传链路 + 录音控件，且主陪伴插件不用它） | 工作量差一个量级 |
| **6** | **N4 那批厂商 key 有没有**：没 key 就不许加预设（宁可少一条，也不要没验过的） | 决定缺口 1 能不能补 |

### 影响体验但不阻塞的（6 项）

| # | 决定 | 现状 |
|---|---|---|
| 1 | 主动消息**落哪个会话** | 现状=按 `user_id` 找用户自己的会话（**确定性**，重启不漂移）—— 推荐保持 |
| 2 | 要不要接**外部推送**（离线也能收到） | 刻意没做；要接就得显式授权 + 安全设计，是**最需谨慎**的动作 |
| 4 | 要不要**真接 onebot** | 现在只有 Web 端 |
| 5 | **Fish Audio TTS 本机到不了**（DNS 投毒 + TLS 被 RST） | 凭据**已验证有效**（hk3 上实测 200 / 40KB 真 MP3），卡在本机可达性 |
| 7 | N3-2/3/4（情绪状态/长期记忆/日程）**插件是否已覆盖** | 我可以先查（grep 插件源码），但你也可以直接说"别做" |
| 9 | 面板供应商 key 是否换成 `.env` 那把 + 换模型 | 换完插件调模型才不报 503 |

---

## 5. 建议的下一步（按"离最终目标最近"排）

| 顺序 | 做什么 | 为什么 | 阻塞 |
|---|---|---|---|
| **1** | **手机 App 套壳的任务拆解 + 形态定案** | 这是"自用 App"和"网页"的分界；**零代码是当前最大缺口** | ⛔ 决策 8 |
| 2 | **N4 补 OpenAI 兼容预设**（19 → 25+） | 低成本高收益，且**不用写新适配器** | ⛔ 决策 6（key） |
| 3 | **生图参考图真发上游** | 平台自身功能，插件不一定覆盖 | 无 |
| 4 | **开主动消息 + 定落点** | 「它主动找你」是这个项目的灵魂功能；插件代码已就绪 | ⛔ 决策 1/2 |
| 5 | onebot（若你要） | 地基已就绪，只差连接/绑定/分发 | ⛔ 决策 4 |
| 6 | N3-2/3/4 **先查插件是否已覆盖** | 覆盖了就不做（避免白干） | 查本身可立刻做 |

---

## 6. 两个必须说清的前提

1. **`docs/NEXT_STEPS.md` §4 的"建议动手顺序"已过时** —— 那是 N0/N1 还没做时写的；
   N0/N1 早已完成，**应以 §1 的 🥇🥈🥉 排序 + 本文件为准**。
2. **别把 `docs/FRAMEWORK_V1_*.md` 里的 S0–S7 当成我们的阶段编号** ——
   那是**陪伴插件作者新框架**的阶段（他写着整体仍在 `S2→S3`）。
   我们的 S0–S7 只在本项目 `README.md` 的状态表里。

---

## 7. 怎么复核这份清单

```bash
cd /root/Ai-works/情绪app
# 基线（必须主 + host 一起跑 —— 单文件跑会漏掉"重载"类 bug）
.venv/bin/python -m pytest tests host/tests -q \
  --deselect tests/test_plugin_host_e2e.py::test_router_matches_real_implementation_live
# → 926 passed, 1 deselected

# 真浏览器 9 套
TOK=$(grep '^EMOTION_AUTH_ACCESS_TOKEN=' .env | cut -d= -f2- | tr -d "'\"")
for t in login_check settings_check logs_check provider_panel_check chat_persona_check \
         plugin_persona_check panel_binding_check mobile_check session_userid_check; do
  .venv/bin/python tools/$t.py http://127.0.0.1:6191 "$TOK" | tail -2
done

# 注入验证（判据能不能失败）
.venv/bin/python tools/inject_tool_loop_check.py
.venv/bin/python tools/inject_global_config_check.py

# 🔴 数据完整性：**查"段数"而不是文件哈希**（2026-09-22 修正的判据）
.venv/bin/python - <<'PY'
import sqlite3, glob
for f in glob.glob('data/plugins/astrbot_plugin_private_companion/persona_profiles/*.db'):
    c = sqlite3.connect(f'file:{f}?mode=ro', uri=True)
    n = c.execute('select count(*) from store_sections').fetchone()[0]
    print(f, n, '段')      # default.db 应 ≈ 150 段
PY
# ⚠️ **别拿 `sha256sum` 当完整性判据** —— `default.db` 是**活库**，
#    服务在跑/插件在写时主文件字节就会变（**实测**：哈希从 `1c6bb963…` 变成
#    `dc1d58d8…`，而段数仍是 **150**、字节仍是 163,840 —— 数据一段没少）。
#    用哈希判"有没有被改"会把**正常运行**误报成异常。

# App 壳是否真的不存在（只应命中【原版 APK 的逆向产物】，不是我们的 App）
find . ../爱语 -iname "*.gradle" -o -iname "AndroidManifest.xml" -o -iname "capacitor.config*" \
  -o -iname "*.apk" 2>/dev/null | grep -v jadx
# 实测唯一命中：`../爱语/dumped/apk-res/AndroidManifest.xml`
#   —— 那是**原版爱语 APK** 解出来的清单（dumped=逆向产物），
#      **没有任何属于我们自己的 App 壳工程**。
```
