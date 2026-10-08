# 手机 App 实施计划（S7）

> **状态**：📋 **计划已定稿，待情绪拍板决策 1/2**
> **日期**：2026-10-02（第十三轮）
> **作者**：良芙（调研由 5 个子代理并行完成，关键断言已由我独立复核）
> ⚠️ **本文所有结论都标了证据等级**：`[实测]` = 我真跑过 · `[源码]` = 读代码/反编译 ·
> `[文档]` = 既有文档 · `[未查证]` = 明确没验过，**不许当事实用**。

---

## 0. 一句话结论

**不用重写 App、不用重打包、不用装模拟器** ——
爱语原版 APK 改 3 处配置就能直连我们的后端，**实测已打通**。
🔴 但它**推翻了一条既有权威结论**：套壳方案（原文档推荐）的代价**比记录的贵得多**（见 §2）。

---

## 1. 🎯 推荐路线：方案 C —— 改配置，零改包

### 1.1 怎么操作（用户视角，3 步）

1. 爱语 App → 设置 → 供应商 → 选「**自定义**」
2. 自定义 URL 填 **`http://<电脑内网IP>:6191/v1`**（尾部 `/` 可省，App 会自动补）
3. API Key 填 **后端访问令牌**（`EMOTION_AUTH_ACCESS_TOKEN`），模型名填**后端配置的那个模型**

### 1.2 为什么可行（源码证据 + 实测）

| 环节 | 证据 |
|---|---|
| 自定义 URL 是**用户输入、零校验** | `MainActivity.java:9445` `KEY_CUSTOM_URL` → `:9450` `currentBaseUrl = string`；`:9462-9463` 只补尾部 `/` `[源码]` |
| 供应商三选项，默认第 1 个 | `MainActivity.java:9440-9445`（0=deepseek / 1=siliconflow / 2=自定义）`[源码]` |
| 明文 HTTP **放行** | `AndroidManifest.decoded.txt:91` `usesCleartextTraffic=true`（targetSdk 35 下**显式置 true**，无 `networkSecurityConfig`）`[源码]` |
| 无证书固定 / 无 host 白名单 | `CertificatePinner` 全库 **0 命中**、`X509TrustManager` 业务包 0、拦截器列表初始为空 `[源码]` |

### 1.3 🔴 实测记录（**这是整份计划可信的基础**）

我用 `curl` 模拟爱语的真实请求形状打后端（内网 `10.52.116.33:6191`）：

| 测试 | 结果 |
|---|---|
| `GET /v1/models`（Bearer 令牌当 key） | **200**，返回后端配置的模型 `[实测]` |
| `POST /v1/chat/completions` 流式，模型名填对 | **200**，拿到真实回复「在呢。」`[实测]` |
| 同一请求连打两次 | **两次都 200**，内容一致 `[实测]` |

⚠️ **中途踩到一个坑（已查清，写进计划避免重蹈）**：
我一开始把 `model` 填成 `/v1/models` 返回的名字，结果 **503**。
- **真因**：爱语发的 `model` 是**用户在设置页手填的 `currentModel`**（`dr.java:175`/`:361`），
  **不是** `/v1/models` 的回显；填错 = 后端把它原样转发给上游 = 上游没有这模型。
- **⇒ 计划里的填法必须是「填后端当前生效的模型名」**，`/v1/models` 只是用来查这个名字的工具。

⚠️ **另有一次 500 是上游瞬时抖动**（`api.syuan.org`），**不是链路问题** ——
判据：同一时刻绕过我们后端直打上游也是 200/500 交替，重试即恢复 `[实测]`。

### 1.4 前后端契约逐条核对（**全通**）

| 项 | 爱语 | 我们后端 | 判据 |
|---|---|---|---|
| 路径 | `<baseUrl>chat/completions` | `/v1/chat/completions` | `ApiService.java:15` ↔ `app.py:491` ✅ |
| 鉴权 | `Authorization: Bearer <key>` | 接受"访问令牌当 Bearer" | `dr.java:278` ↔ `auth.py` ✅ |
| 流式 | 只认 `data: ` 前缀，`[DONE]` 结束，**非 `data:` 行跳过** | 同形状 | ✅（**爱语的契约比想的更宽容**，我们**无需为它改任何代码**）`[源码]` |
| 非流式 | `choices[0].message.content` + `usage` | 同形 | ✅ |
| `/v1/models` | 需 `id`/`object`/`owned_by` | 提供了 | ✅ |
| 超时 | 默认 **600 秒** | — | 够用 |

---

## 2. 🔴 推翻的既有结论：套壳（方案 B）代价被严重低估

### 2.1 旧结论 vs 实测

| 出处 | 旧说法 | 本轮实测 |
|---|---|---|
| `ARCHITECTURE_THIN_CLIENT.md:150-151` | 「业务代码里 **11 个**类用到 WebView」 | ❌ **假数字** —— 真 **6 个**（`gi`/`kr`/`nx`/`ModuleWebViewActivity`/`ox`/`px`），**且漏了真正的载体 `gi`** `[实测]` |
| 同上 `:156` | 「套壳是量变不是质变」 | ❌ **缺源码支撑** —— 4 个 WebView 加载点**全部是 `file://` 本地路径或内存字符串，无一处远程 URL** `[源码]` |

**独立复核命令**（我亲自跑过）：
```bash
grep -rl "Landroid/webkit/WebView;" dumped/smali/ | wc -l   # → 6
```

### 2.2 真正的代价

主聊天页 **100% 原生**：`MainActivity` **17,798 行** + `jv`(MessageAdapter) **3,772** + `ev`(ViewHolder) **1,847**。

⇒ **套壳不是"扩大已验证机制"，而是"从零重写 82,744 行原生 UI"。**
旧文档 §4.2 的代价表**只算了"省掉 30 个 Activity 的复刻"，没计入"要重写 8 万行 UI"**。

### 2.3 套壳还有一条**硬边界**（已用 MDN 独立核实）

**WebView 里拿不到 Web Push** —— MDN `PushManager` 的 `WebView Android` = **No support**。
⇒ 「伴侣主动找我」在 WebView 形态下**没有可靠通道**，必须另开**原生 Service + FCM/本地通知**，
**这是一块不能省的原生工作量**。

---

## 3. 必须留在手机的部分（后端替代不了）

| 能力 | 证据 |
|---|---|
| 原生 UI 渲染栈 | `MainActivity.java:15069`/`:15503`、`jv.java:3507` `[源码]` |
| 🔴 **Live2D** | `MainActivity.java:15260-15280` `new GLSurfaceView(this)` + `JniBridgeJava.SetModelPath` ⇒ **GLES+JNI，WebView 做不了（硬伤）** `[源码]` |
| 🔴 **触摸手势 → `{system:…}`** | `MainActivity.java:1742-1769`（`y3<height/3`→forehead、`<height*2/3`→belly…）`[源码]` |
| 悬浮窗/桌宠 | `Settings.canDrawOverlays`：`MainActivity.java:1459`、`wv.java:140` `[源码]` |
| 无障碍读屏 | `DesktopAccessibilityService.java:3-15` `extends AccessibilityService` `[源码]` |
| 通知/前台服务/保活 | `ProactiveMessageService.java:858-860` `startForeground(...)` `[源码]` |
| 麦克风 / 本地 ASR | `libsherpa-onnx-*.so` + `assets/encoder-….onnx`（**67,963,182 B**）`[实测]` |
| 模块双桥（仅 2 处） | `addJavascriptInterface` 全库**只有 2 处**：`ModuleWebViewActivity.smali:368`→`ox`、`ox.smali:274`→`mx` `[源码]` |

> 新增两条**硬证据**：**Live2D** 与 **触摸手势**（旧文档未列，两者都无法放进 WebView）。

---

## 4. ⚠️ 三个功能缺口（**不阻塞**，是"某些功能用不了"）

| 缺口 | 现象 | 证据 |
|---|---|---|
| **生图机制不同** | 爱语靠**文本标签** `<generate_image:描述>`（`da.java:75`）；我们靠 **function calling** ⇒ 图走 SSE `images` 扩展字段，而**爱语主聊天不读它**（`oe.smali` 里 `"images"` **0 命中**） | **图到不了 UI** `[源码]` |
| **无 `/sticker` 路由** | 爱语表情图 URL 取自 `da.java:2249` 的 `/sticker` | 自定义表情包不显示 `[源码]` |
| **无 TTS/ASR 入站端点** | 我们只有配置层 | 语音不可用（**可选**） `[文档]` |

---

## 5. 🔴 安全前置（**上手机之前必须做**）

| # | 问题 | 现状 | 处置 |
|---|---|---|---|
| 1 | 服务绑 `0.0.0.0` + 明文 http + **单令牌** | `.env` 实测 `EMOTION_SERVER_HOST=0.0.0.0`、`HTTPS_ONLY=false` | **对外暴露前必须挂 TLS 反代** + `EMOTION_SERVER_HTTPS_ONLY=true` |
| 2 | 内网使用（`http://10.52.116.33:6191`） | 明文但**仅内网** | **可接受**（爱语已放行明文）；`[实测]` 当前走的就是这条 |
| 3 | 令牌进 localStorage | 现有面板已守这条 | App 侧同样**不许写 localStorage** |

⚠️ **公网暴露的额外风险**（已知）：外网 NAT `31818 → 6191` 是通的，
**明文 + 单令牌暴露公网 = 会话 cookie 可被嗅探**（README:141 已标）。

---

## 6. 🛠 本机工具链（**已实测齐全，不用换机器**）

| 工具 | 版本 | 位置 |
|---|---|---|
| JDK | **21.0.12.1** | `/usr/bin/java` |
| apktool | ✅ | `/usr/bin/apktool` |
| **apksigner** | **0.9** | `/opt/android-sdk/build-tools/34.0.0/` |
| zipalign / d8 / aapt2 | ✅ / **8.2.2** / **2.19** | 同上 |
| adb | **1.0.41** | `/opt/android-sdk/platform-tools/` |

⚠️ 后三样**不在默认 PATH**：用前 `export PATH=/opt/android-sdk/build-tools/34.0.0:$PATH`。

### 模拟器：**已删，但能恢复**（`[实测]`）

- 现状：`emulator` / `system-images` / AVD **全无**
- 恢复：`sdkmanager` 直连 Google 源正常（**200 / 0.13s**），镜像 **1.28 GB**，磁盘 **33G 可用**
- **建议先不装** —— 走方案 C **完全用不上**模拟器（§8 决策 2）

---

## 7. 🔴 若将来走「改包」路线：唯一的雷区

### 7.1 绝对不要改 `packageName`

`MainActivity.java:8916-8920` 校验包名（**在签名校验之前**），断言值逐层解码 = **`com.shuoshuo.aiyu`**。
失败 → `f.n(false)` → 真读点 `y0.smali:272` → **`:347 clearApplicationUserData()` = 清空全部 App 数据**。

⇒ **只改配置永不触发；即使改包，保留 `com.shuoshuo.aiyu` 也安全。** `[源码]`

⚠️ **一处易误判**：jadx 的 `ChatSettingsActivity.java` 里那 **11 处** `if (!f.j())` 是
**jadx 把内部类 `y0` 的 switch 分支错并进宿主类的假象** ——
`grep "Lcom/shuoshuo/aiyu/f;" ChatSettingsActivity.smali` **0 命中**，**真只有 1 处**。

### 7.2 签名校验是**死代码**（不挡改包）

`MainActivity.java:8950` 比对 SHA-256 = `624b0dad…dcc841`，
**我独立复核该值 = 本 APK 真实证书**：

```bash
openssl x509 -in delivery-*/signer-0.pem -outform DER | sha256sum
# → 624b0dadfbf874a66183dc5b736eb52929ffccfb9701b2507320700c64dcc841  ✅ 逐字符一致
```

**但全库 0 读**：`sget-boolean … isConfigOptimized` → **0 命中**（smali 裁决）`[实测]`。
⇒ 重签名后只是往一个**没人读的字段**写 `false`。**不挡改包。**

⚠️ **方法论教训**：`grep "GET_SIGNATURES"` **匹配不到**这段 ——
源码用**裸数字常量** `134217728`/`64`（R8 内联），两个子代理第一轮都因此漏判。

### 7.3 SecShell 壳：**未找到完整性校验证据，但有明确未查证**

⚠️ **以下 4 条明确没验过，不许当"壳是安全的"用**：
1. `assets/meta-data/*` 的读取点在 **native 加密区** ⇒ 三面静态扫描**不覆盖它**
2. `manifest.mf` 那 5 个摘要保护什么（遍历 1,115 个条目 **0 命中**）
3. `libSecShell32` 的 `file_hash`/`check_value` 语义
4. **真机端到端从未跑过**

⇒ **若走改包路线，必须先真机实测。**（方案 C 不改包，不受此影响）

---

## 8. ⏸️ 待情绪拍板的决策

| # | 决策 | 选项 | 我的建议 |
|---|---|---|---|
| **1** | **走哪条路线** | A) 方案 C 改配置（零改包）<br>B) 套壳（重写 8 万行 UI）<br>C) 从零写原生<br>D) PWA（WebView 里没推送） | **A 起步** —— 今天就能跑，白拿全套系统能力 |
| **2** | **要不要现在装模拟器**（1.28 GB） | 装 / 不装 | **不装** —— 方案 C 用不上；要改包了再装（5 分钟） |
| **3** | **后端跑在哪台机器** | 本机（当前）/ 别处 | 本机（内网 `10.52.116.33`） |
| **4** | **要不要补三个功能缺口** | 补 / 不补 | 先跑通再补；**生图缺口最值得补** |
| **5** | **要不要 HTTPS 反代** | 现在就挂 / 暂不 | 内网自用**可暂不**；**一旦公网必须挂** |

---

## 9. 建议的实施顺序

| 序 | 做什么 | 判据 | 阻塞 |
|---|---|---|---|
| **1** | **真机跑方案 C**：爱语装到你手机 → 填 `http://<电脑IP>:6191/v1` + 令牌 + 模型名 | 手机上能正常聊天、**能流式吐字** | ⛔ 决策 1/3 |
| 2 | 补「生图到 UI」缺口（SSE `images` → 爱语不读 ⇒ 需改渲染或换机制） | 手机上说"画张图"能看到图 | ⛔ 决策 4 |
| 3 | 补 `/sticker` 或让它优雅降级 | 表情包不报错 | — |
| 4 | 按需补 CORS（**仅当**用非同源前端时） | `file://` 或跨域能取数 | — |
| 5 | 再评估"UI 可改"（套壳）—— **先算清 8 万行的账** | — | ⛔ 决策 1 |

### 四种形态的共同前置

1. **传输层**：内网明文（方案 C 可用，因爱语已放行）；**公网必须 HTTPS 反代**
2. 🔴 **WebView 拿不到 Web Push** ⇒ 「伴侣主动找我」在 A/B 形态下**必须走原生通知通道**
3. **CORS**：后端**零 CORS** ⇒ 同源不受影响；**不同源（含 `file://`，origin = `null`）必须先补**

---

## 10. ⚠️ 未查证清单（如实）

1. **真机端到端从未跑过** —— §1 结论来自**静态源码 + 我用 curl 模拟**，不是真机
2. **SecShell native 加密区是否有完整性校验**（只证明"静态三面没有"）
3. `assets/meta-data/*` 读取点与 5 个摘要覆盖对象
4. 带 Magisk/su 时 `check_root` 是否杀进程
5. 无 GMS 国产机是否 **100%** 收不到 Web Push（endpoint 是 `fcm.googleapis.com`，GMS 那步无官方明文）
6. 爱语 WebView 除 `file://` 外**是否支持远程 URL**（代码没写死禁止，但无实测）
7. 重打包 SecShell 包的**具体失败模式**
8. 后端到手机的端到端**延迟**（未测）

---

## 11. 与既有文档的关系（**哪些作废**）

| 文档 | 处置 |
|---|---|
| `ARCHITECTURE_THIN_CLIENT.md:150-151`「11 个类用 WebView」 | ❌ **作废**（真 6 个） |
| 同上 `:156`「套壳是量变不是质变」 | ❌ **作废**（缺源码支撑，代价被低估） |
| 同上 `:184`「30 个 Activity」 | ✅ 正确（28 自有 + 2 三方） |
| 同上 §2.2「28 个 `external_access_*` 键」 | ✅ 正确 |
| `SECSHELL_LOADER.md:204/205/209`「RSA-512」 | ❌ **作废**（真 **RSA-1024 / SHA-1 / PKCS#1 v1.5**） |
| `REPRODUCTION.md:101` `"<KEY_ENABLE_THINKING>"` 占位符 | ❌ **作废**（真键名 `enable_thinking`） |
| `ARCHITECTURE_THIN_CLIENT.md:172-200` 收益/代价表 | ⚠️ **需修订**（漏算 8 万行 UI 重写） |

### 关于「爱语自己跑了个 OpenAI 服务器」

`ha.java`（**63 行**）= `ExternalAccessServer`（NanoHTTPD），路由 `/health`、`/v1/models`、
`/v1/chat/completions`、`/sticker/<x>`。`[源码]`

**它的价值是「反向证明我们后端该长什么样」，不是一条可用替代链路**，因为：
- **绑 `0.0.0.0`**（`da.java:2234-2249`；`n.smali:70-79` hostname==null 时走单参 `InetSocketAddress(port)` = 通配）
- 默认端口 **8866**，**默认关闭**（`ExternalAccessService.java:292` `getBoolean(KEY_SHOULD_RUN,false)`）
- ⚠️ **它的假密钥别学**：`DEFAULT_FAKE_KEY = "aiyu"`，鉴权比对的就是这个字面量 `[源码]`
