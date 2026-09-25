# -*- coding: utf-8 -*-
"""`PersonaManager` 兼容实现 —— 把**我们的人设库**接到插件的 `context.persona_manager`。

⭐⭐ **为什么必须有这个模块**（情绪 2026-09-21 报障，实测复现）：

插件面板的「快速从已有人格导入」点「生成导入草稿」永远报
`还没有读取到可用的主回复人格文本，请先让 Bot 触发一次对话或检查人格配置`。

根因**不是**"没触发过对话"，而是我们的 compat `Context` **完全没有
`persona_manager` 这个属性**（全仓 0 命中）。插件那条链路是：

    page_api._roleplay_persona_prompt_for_id   (page_api.py:14799)
      └─ plugin._refresh_default_persona_prompt(umo)      (daily_state.py:11073)
           └─ manager = getattr(context, "persona_manager", None)   → None
                └─ getter = get_default_persona_v3 if manager else None → None
                     └─ return cached or _get_default_persona_prompt(umo)
                          └─ DEFAULT_PERSONA_PROMPT_FALLBACK
                               ("未读取到 AstrBot 默认人格。…")          (daily_state.py:163)
      └─ if prompt.startswith("未读取到 AstrBot 默认人格")  → 报错 (page_api.py:14849)

🔴 **那句报错文案是误诊**：它让用户"先让 Bot 触发一次对话"，但
   `_refresh_default_persona_prompt` 的**两个缓存写入口都在 `manager` 分支之后**
   （`daily_state.py:11113`/`:11153`）—— `manager is None` 时**永远不会写缓存**，
   所以"触发了也修不好"，而且**全程一行日志都没有**（静默降级）。
   → 这正是本项目最贵的一类 bug；补上这个桥才是真修。

## 🔴🔴 数据安全：本桥**必须**如实回答"未初始化"

插件有一处**会删文件**的对账（`main.py:3430 _reconcile_deleted_personas_async`，
由 `page_api.py:14426` 与 `main.py:8751` 触发）：它拿我们的全量人格 ID 当作
**权威集合**，把"插件自己档案里有、但 AstrBot 人格列表里没有"的人格判为
**已删除** → 从配置里剔除并 `_retire_deleted_persona_store_sync()` **删掉档案**
（`.db` / `.json` + WAL/SHM，只在删前留一份备份）。

它的"能不能信这个列表"判据是（`main.py:3341-3347`）：

    personas 为空(list)  且  selected_default_persona is None
                        且  selected_default_persona_v3 is None
    → 判定 "persona_manager_not_initialized" → **放弃对账**（安全）

⚠️ 所以**空列表 + 两个 `selected_default_*` 为 `None`** 这个组合是**安全闸门**，
   必须保持一致：只要给了非空 `personas`，插件就会认为"这是权威全集"并**真去删**。
   本模块用 `_initialized` 一个来源同时驱动这三者，避免三者漂移。
   ✅ 另有一道保险：`_reconcile_deleted_personas_async` 只对**非主人格**动手
   （`main.py:3443` 过滤 `pid == primary`），而 `default.db` 就是主人格档案。

## 插件的实际接口面（实测穷尽，全仓只有 12 处访问）

| 成员 | 形态 | 谁在用 |
|---|---|---|
| `personas_v3` | `list[dict]`（真实现是 `Personality` TypedDict） | `main.py:3316,3348` |
| `personas` | `list`（真实现是 SQLModel 对象；插件 dict/属性**两种都读**） | `main.py:2692,3316` |
| `resolve_selected_persona` | **async**、全 kw-only、返 **4 元组** | `main.py:3516`、`meme_manager:825` |
| `get_default_persona_v3` | **async**、`(umo=None)`、**永不返 None** | `daily_state.py:11134`、`together_companion:4092` |
| `get_persona_v3_by_id` | **同步**、`(persona_id)` → dict 或 None | `main.py:3310` |
| `get_persona` | **async**、查不到**抛 ValueError** | `daily_state.py:11104`、`together_companion:4087` |
| `selected_default_persona` / `_v3` | 属性，可为 None | **仅**"未初始化"判据 `main.py:3344` |

⚠️ **`resolve_selected_persona` 必须返回 4 元组**：`meme_manager` 按 4 元解包
   （`(persona_id, _, _, _)`），返回 3 元会让它 `ValueError`（被它的
   `except Exception` 吞成 warning）。
⚠️ **`get_default_persona_v3` 永不返 None**：真实现兜底 `DEFAULT_PERSONALITY`，
   而 `daily_state._extract_default_persona_prompt(None)` 会走到
   `DEFAULT_PERSONA_PROMPT_FALLBACK` → 又报那句误诊文案。
⚠️ **`get_config` 的 `umo` 必须是位置参数**（本模块不涉及，但同批修）：
   插件有 12 处按位置传 umo，改成 keyword-only 会静默打断它们。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("astrbot")

#: 真实现 `persona_mgr.py:9-19` 的兜底人格（逐字照抄 prompt 与 name）。
#: ⚠️ 我们**不**把它塞进 `personas_v3` —— 它只是"一个都读不到时"的最后兜底，
#:    塞进去会让插件以为"用户真的建了一个叫 default 的人格"。
DEFAULT_PERSONA_PROMPT = "You are a helpful and friendly assistant."
DEFAULT_PERSONA_NAME = "default"

#: 真实现里 `"[%None]"` 是**显式表示"这条会话不要人格"** 的哨兵值
#: （`persona_mgr.py:107-110` 用 `pass` 保留它）。照抄语义。
NO_PERSONA_SENTINEL = "[%None]"

#: 平台类型名 —— 真实现 `persona_mgr.py:118` 用它判"要不要用 webchat 专用默认人格"。
#: ⚠️ 我们的平台类型名是 `webchat`（`umo.PLATFORM_NAME`），而 umo 第一段是
#:    平台**实例** id（`"11"`）—— 两者不是一回事，真实现收的是**类型名**。
WEBCHAT_PLATFORM_NAME = "webchat"


class HostPersonaManager:
    """把我们的 `PersonaRepository` 适配成 AstrBot 的 `PersonaManager` 接口面。

    ⚠️ **依赖倒置**：本类**不 import `emotion_app`** —— 它只认一个鸭子类型的
       `repo`（有 `async list()/get()`）。这样 `host/` 保持"可独立交付"，
       而 `emotion_app` 是"把 repo 注进来"的那一方（与 `HostPorts` 同款做法）。

    ⚠️ **repo 为 None = 如实"人设能力不可用"**：`personas` 保持空、
       `selected_default_*` 保持 None → 插件走它自己的降级路径，
       且"未初始化"判据**不会**误触发删除对账（见模块头的数据安全说明）。
       绝不凭空造一个人格（那是伪造能力，作者判据③明确不许）。
    """

    def __init__(self, repo: Any = None, default_persona_id: str = "") -> None:
        self._repo = repo
        #: 宿主配置里"默认采用的人格"id（`config_profile.session.default_persona_id`）。
        #: ⚠️ 这个字段以前**没有任何消费方**（死配置）；现在它是"哪个人格算默认"
        #:    的唯一来源 —— 插件 `get_default_persona_v3()` 拿到的就是它。
        self._default_persona_id = str(default_persona_id or "").strip()
        #: 与 `personas_v3` 并列（真实现里两者同源，只是一个对象一个 dict）
        self.personas: list[dict[str, Any]] = []
        self.personas_v3: list[dict[str, Any]] = []
        #: 真实现 `persona_mgr.py:29,32`：构造时都是 `None`，`initialize()` 后才填。
        #: 🔴 这两个属性是插件的**"未初始化"判据**（`main.py:3344-3345`），
        #:    它们在数据没读出来之前**必须**是 None（见模块头"数据安全"）。
        self.selected_default_persona: dict[str, Any] | None = None
        self.selected_default_persona_v3: dict[str, Any] | None = None
        #: 真实现同名字段（`main.py:27` 读它算 selected_default）
        self.default_persona: str = DEFAULT_PERSONA_NAME
        #: `provider_manager.persona_configs` 在真实现里返回这个（无插件直接用它）
        self.persona_v3_config: list[dict[str, Any]] = []
        #: 数据是否**成功**读出来过（驱动上面那组"未初始化"判据）
        self._initialized = False

    # ---- 装配 ----

    async def refresh(self) -> None:
        """从 repo 重新载入人设（**唯一**的数据入口）。

        🔴 三条"必须"：
          ① **失败要响亮**（`logger.error` + `exc_info`）——这是插件的核心能力，
             静默失败正是本次事故的形态；
          ② **失败时保持"未初始化"**（`_initialized=False` + 空列表 + 双 None）→
             插件会跳过删除对账（不会误删档案），只是人格读不到；
          ③ **读成功但一条都没有**也算"已初始化"（空集是**可信的**答案）——
             ⚠️ 但这会让对账认为"一条人格都不存在"→ 会删非主人格档案。
             实测当前插件只有主人格档案（`default.db`）且对账显式跳过主人格，
             但**这条边界必须知道**：见下方 `_initialized` 赋值处的注释。
        """
        if self._repo is None:
            logger.warning(
                "没有人设仓库 → `context.persona_manager` 保持未初始化"
                "（插件读不到人格，但不会误删档案）"
            )
            return
        try:
            items = list(await self._repo.list() or [])
        except Exception:  # noqa: BLE001
            logger.error(
                "读取人设失败 → persona_manager 保持未初始化"
                "（插件将读不到人格；为避免误删档案，**不**把空列表当权威答案）",
                exc_info=True,
            )
            return
        self._apply(items)

    def refresh_sync_from(self, items: Any) -> None:
        """用**已经拿到**的人设对象列表刷新（同步；给装配层/测试用）。

        ⚠️ 与 `refresh()` 分离是刻意的：装配层往往**已经**因为别的原因读过一次
           人设，再 await 一次 repo 既慢又可能拿到不一致的两份快照。
        """
        if items is None:
            return
        self._apply(list(items))

    def _apply(self, items: list[Any]) -> None:
        """把领域层的 `Persona` 对象转成插件要的 dict 形态。

        🔴 **只放插件真的会读的键**，外加 `Personality` 的必需字段：
           插件的两条路径分别是"当下标用"（`item["name"]`，真实现 `persona_mgr.py:59`）
           和"当字典/属性用"（`main.py:3354-3362` 先 `isinstance(item, dict)`）。
           所以给 **dict** 最保险 —— 两种读法都能过。

        🔴🔴 **必须带上内置的 `default` 人格**（下面 `_ensure_builtin_default`）——
           这**不是**凑数，它同时是"忠实于真实现"和"防误删用户数据"两件事，理由见该函数。
        """
        v3: list[dict[str, Any]] = []
        for p in items:
            d = _persona_to_v3(p)
            if d is not None:
                v3.append(d)
        v3 = self._ensure_builtin_default(v3)
        self.personas_v3 = v3
        #: `personas` 与 `personas_v3` **同源同形**（真实现里一个是 ORM 对象、
        #: 一个是 dict；插件两条路都只是取 id/name/prompt，形状一致即可）。
        self.personas = list(v3)
        self.persona_v3_config = list(v3)
        self.selected_default_persona_v3 = self._pick_default(v3)
        self.selected_default_persona = self.selected_default_persona_v3
        # 🔴 `_initialized=True` 的含义是"上面这组值**可信**"（而不是"读成功了"）。
        #    插件用「空 list + 双 None」区分"未初始化"与"真的没人格"；
        #    我们只有在**确实读到了**（哪怕结果为空）时才翻转它。
        self._initialized = True
        logger.info(
            "context.persona_manager 已装载：%d 个人格（默认=%s）",
            len(v3),
            (self.selected_default_persona_v3 or {}).get("name") or "无",
        )

    def _ensure_builtin_default(self, v3: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """确保列表里**总有**一个名叫 `default` 的人格。

        🔴🔴 **为什么这一条是安全关键的（实测推演，不是理论）**

        插件有一处会**删文件**的对账（`main.py:3430`，启动时
        `main.py:8751` 与 `page_api.py:14426` 都会触发）：

            official_ids = 我们的全量人格 id 集合          # 它当作**权威**
            candidates   = 插件 persona_profiles/ 下的档案  # 实测 = {"default"}
            stale = [p for p in candidates
                     if p and p != primary and p not in official_ids]
            → 逐个 _retire_deleted_persona_store_sync()    # **删 .db + WAL/SHM**

        实测本机状态（只读推演）：

            primary      = ""             ← plugin_specific_persona_id 为空，**保护不了任何人**
            candidates   = {"default"}    ← data/plugins/…/persona_profiles/default.db
            official_ids = {"糯米茨"}      ← 桥报出去的人设名

            → stale = ["default"] → **会删掉 default.db**

        而那个 `default.db` **装着用户真实数据**（实测 150 段：`persona_settings`
        / 衣柜 `WARDROBE_*` / `unified_person` / `_req041_private_memory` /
        `primary_store_ownership` …，163 KB）。

        ⚠️ **在我加这个桥之前这条路是走不到的**：`persona_manager` 为 None →
        `_astrbot_persona_ids_snapshot()` 返 `(None, "persona_manager_unavailable")`
        → 对账**直接跳过**。也就是说**我的修复会新解锁一条破坏性路径** ——
        所以必须在**同一处**把它关上，不能等出了事再补。

        ✅ **关法就是"照真实现"**：真 AstrBot 的 `personas_v3` **永远含一个
           `default`** —— `get_v3_persona_data()` 在"没有任何人格"时会
           `personas_v3.append(DEFAULT_PERSONALITY)`（`persona_mgr.py:416-418`），
           而 `default_persona` 出厂值也是 `"default"`（`persona_mgr.py:27`）。
           → 真 AstrBot 上 `official_ids` 必然含 `"default"`，
             所以 `default.db` **在真 AstrBot 上也不会被判 stale**。
           我们补上它，行为就与真实现一致，同时天然堵住这个误删。

        ⚠️ 命名冲突时要小心：如果我们的人设里**已经**有人叫 `default`，
           就不再补（避免两个同名条目让 `["name"]` 匹配出现歧义）。
        """
        if any(str(item.get("name") or "") == DEFAULT_PERSONA_NAME for item in v3):
            return v3
        return [*v3, {**_default_personality(), "_synthetic_default": True}]

    def _pick_default(self, v3: list[dict[str, Any]]) -> dict[str, Any] | None:
        """挑"默认人格"。

        ⚠️ 顺序照真实现 `get_v3_persona_data()`（`persona_mgr.py:406,412-414`），
           但**多一条我们特有的规则**（因为我们会补一个合成 `default`，见下）：

           ① **宿主配置指定的那个**（`config_profile.session.default_persona_id`）
              —— 也就是面板上「默认采用的人格」，这个字段以前是死配置；
           ② **第一个"真"人设**（我们自己库里的人设）；
           ③ 名字等于 `self.default_persona` 的那个（真实现的默认名 `default`）；
           ④ 实在没有 → `v3[0]`（可能就是我们合成的那个 `default`）。

        🔴 **为什么 ② 必须排在 ③ 前面**（实测踩到，有判据守着）：
           `_ensure_builtin_default()` 会**补**一个合成的 `default` 进列表。
           若照真实现先按名字找 `default`，那个**合成的空壳**就会永远胜出
           → 用户在面板上建的人设（糯米茨）永远当不上默认人格，
           插件拿到的是一句 `You are a helpful and friendly assistant.`
           —— 功能上"通了"，但用的是**假人格**，比报错更糟（静默错）。
        """
        if not v3:
            return None
        # ① 面板上「默认采用的人格」
        if self._default_persona_id:
            for item in v3:
                if self._default_persona_id in {
                    str(item.get("name") or ""), str(item.get("id") or "")
                }:
                    return item
        # ② 第一个"真"人设（排除我们合成的 default 空壳）
        for item in v3:
            if item.get("_synthetic_default") is not True:
                return item
        # ③ 真实现的默认名
        for item in v3:
            if str(item.get("name") or "") == self.default_persona:
                return item
        # ④ 兜底
        return v3[0]

    @property
    def initialized(self) -> bool:
        """数据是否可信（诊断用；**不是**真实现的接口面）。"""
        return self._initialized

    # ---- 插件真的会调的 5 个方法 ----

    def get_persona_v3_by_id(self, persona_id: str | None) -> dict[str, Any] | None:
        """按 id 取人格（**同步** —— 真实现 `persona_mgr.py:47` 也是同步）。

        ⚠️ 真实现按 `["name"]` 匹配（不是 `persona_id`），我们照抄；
           额外**也**接受我们自己的人设 id —— 因为插件的
           `plugin_specific_persona_id` 里存的可能是我们面板上那个人设 id
           （用户从我们的 panel 拷过来的），两种写法都该能查到。
        """
        pid = str(persona_id or "").strip()
        if not pid:
            return None
        if pid == DEFAULT_PERSONA_NAME and not self.personas_v3:
            return _default_personality()
        for item in self.personas_v3:
            if pid in {str(item.get("name") or ""), str(item.get("id") or "")}:
                return item
        return None

    async def get_default_persona_v3(self, umo: Any = None) -> dict[str, Any]:
        """取默认人格（**async**；真实现 `persona_mgr.py:63`）。

        🔴 **永不返回 None**（真实现的兜底是 `DEFAULT_PERSONALITY`）：
           返回 None 会让 `daily_state._extract_default_persona_prompt(None)`
           拿到空串 → 又走回那句"未读取到 AstrBot 默认人格"的误诊文案。

        ⚠️ `umo` 收下但**不做区分**（我们单用户）——**如实**，不假装按会话隔离。
           真实现会按 umo 查 `provider_settings.default_personality`；
           我们没接那份配置（那是 `get_config` 的活），所以用我们的"默认人格"。
        """
        picked = self.selected_default_persona_v3
        if picked is not None:
            return picked
        if self.personas_v3:
            return self.personas_v3[0]
        return _default_personality()

    async def get_persona(self, persona_id: str) -> Any:
        """按 id 取人格；**查不到抛 `ValueError`**（真实现 `persona_mgr.py:40`）。

        🔴 抛错是**预期路径**：`daily_state.py:11104-11133` 专门为它写了一串
           `except`（含 `CancelledError`/`sqlite3`/`TimeoutError`），抛了它
           会**记 warning 并继续**去试 `get_default_persona_v3()`。
           静默返回 None 反而会绕过那条降级链。
        """
        pid = str(persona_id or "").strip()
        found = self.get_persona_v3_by_id(pid)
        if found is None:
            raise ValueError(f"Persona with ID {persona_id} does not exist.")
        return found

    async def resolve_selected_persona(
        self,
        *,
        umo: Any,
        conversation_persona_id: str | None,
        platform_name: str = "",
        provider_settings: dict | None = None,
    ) -> tuple[str | None, dict[str, Any] | None, str | None, bool]:
        """解析"这条会话最终用哪个人格"（真实现 `persona_mgr.py:75`）。

        🔴 **必须返回 4 元组** —— 两个调用方的解包方式不同：
           * `main.py:3526-3528` 按**下标**读 `[0]/[1]/[2]`（≥3 即可）；
           * `meme_manager/mixins/event_handlers.py:825` 按**4 元解包**
             `(persona_id, _, _, _)` → 少一个就 `ValueError`。

        优先级**照真实现逐条对照**（`persona_mgr.py:102-120`）：

          ① `session_service_config.persona_id`（会话级强制规则）
          ② `conversation_persona_id`（对话自身带的人格）
          ③ `provider_settings["default_personality"]`（全局默认）
          ④ 真实现还有一条 **webchat 专用默认**：解析不到且
             `platform_name == "webchat"` 时改用 `_chatui_default_` 并置第 4 位 True。

        ⚠️ **①我们没有 `sp`（会话级偏好存储）** → 如实跳过该层（不是"静默失败"：
           我们本来就没有这个机制，`force_applied` 永远是 `None`）。
        ⚠️ **④照抄但语义要看清**：我们平台类型名就是 `webchat`
           （`umo.PLATFORM_NAME`），所以真实现那条"webchat 专用默认"会命中。
           我们**只在实在没人格时**才走它（见 ③′）。

        🔴 **③′ 是我们的【有意偏离】**（照抄真实现会让用户拿不到人设）：
           真实现在"`conversation_persona_id` 为空 **且**
           `provider_settings.default_personality` 也为空"时，
           `persona` 就是 `None`（`persona_mgr.py:105-115`）→ 插件拿到 None
           → 记忆/日程那边判 `astrbot_persona_unresolved` 而早退。
           而**我们就是这个平台**，我们**确实有**一个默认人设
           （面板「默认采用的人格」，没有就取第一个人设）—— 所以这里回落到它。

           ⚠️ 偏离的正当性：用户的原话就是「**陪伴那边获取不到平台的默认人设**」。
              照抄真实现等于把这个诉求原样退回去。真 AstrBot 之所以能返回 None
              还不难受，是因为它自己那份 `cmd_config.json` 里
              `default_personality` **永远有值**（出厂 `"default"`）——
              也就是"它靠配置兜住了，我们靠代码兜住"，效果等价而更稳。
        """
        provider_settings = provider_settings if isinstance(provider_settings, dict) else {}

        # ---- ① 会话级强制规则：我们没有 sp，如实跳过 ----
        force_applied_persona_id: str | None = None
        persona_id: str | None = None

        # ---- ② 对话自带的人格 ----
        if not persona_id:
            persona_id = conversation_persona_id
            if persona_id == NO_PERSONA_SENTINEL:
                pass                      # 显式"不要人格"，保留哨兵（真实现同款）
            elif persona_id is None:
                # ---- ③ 全局默认（`provider_settings.default_personality`）----
                persona_id = provider_settings.get("default_personality")

        persona = self.get_persona_v3_by_id(persona_id)

        # ---- ③′ 我们自己的默认人设（有意偏离，理由见 docstring）----
        if persona is None and persona_id != NO_PERSONA_SENTINEL:
            fallback = self.selected_default_persona_v3
            if fallback is not None:
                persona = fallback
                persona_id = str(fallback.get("name") or "") or persona_id

        # ---- ④ webchat 专用默认：**只在真的一个人格都没有时** ----
        use_webchat_special_default = False
        if (
            persona is None
            and str(platform_name or "") == WEBCHAT_PLATFORM_NAME
            and persona_id != NO_PERSONA_SENTINEL
        ):
            persona_id = "_chatui_default_"
            use_webchat_special_default = True

        return (
            persona_id,
            persona,
            force_applied_persona_id,
            use_webchat_special_default,
        )

    # ---- 真实现还有、但我们**如实不提供**的方法 ----
    #
    # 🔴 `get_all_personas` / `get_personas` / `list_personas` / `get_persona_list`
    #    / `get_all` / `list` —— **故意不给**。
    #
    #    理由：`page_api.py:14596` 会**按这个顺序盲试**这些名字，命中就用。
    #    真实现里 `get_all_personas()` 是 async 且返回 ORM 对象列表，
    #    插件要用 `model_dump()` 才能序列化 —— 而我们给的是**纯 dict**，
    #    没有 `model_dump`，虽然插件有 `.dict`/属性兜底（`object_entry`），
    #    但那条路会多绕一层、且 `source` 标注会变。
    #    `personas`/`personas_v3` 两个属性已经覆盖了**全部真实需求**，
    #    少给一个方法就少一条"行为与真实现不一致"的分叉。

    def describe(self) -> dict[str, Any]:
        """脱敏诊断（给 `/api/v1/status` 与日志用）。"""
        return {
            "initialized": self._initialized,
            "count": len(self.personas_v3),
            "default": (self.selected_default_persona_v3 or {}).get("name") or "",
            "names": [str(p.get("name") or "") for p in self.personas_v3],
        }


# ---------------------------------------------------------------------------
# 转换助手
# ---------------------------------------------------------------------------


def _persona_to_v3(persona: Any) -> dict[str, Any] | None:
    """领域层 `Persona` → 插件的 v3 人格 dict（形状照 `po.py:581-600` 的 `Personality`）。

    🔴 **`prompt` 是拼接出来的**，不是某个单独字段：
       我们的 `Persona` 是**6 字段 + 片段**模型（`domain/persona.py`），
       而 AstrBot 的 `Personality.prompt` 是**一坨 system 文本**。
       → 必须走**同一个组装器**（`domain.prompt.assemble`），
         否则"面板里看着是人设 A、插件实际拿到的是 A 的一部分"
         —— 那是同一语义两处实现，本项目的老坑。

    ⚠️ 组装器**不 import**（用回调/鸭子类型）会很难看，所以这里**允许**
       import `emotion_app`：但只在**调用时**（函数内）import ——
       这样 `host/` 单独被 import 时不依赖 `emotion_app`
       （`host/tests` 就是这么跑的）。
    """
    get = (lambda k, d="": getattr(persona, k, d)) if not isinstance(persona, dict) else (
        lambda k, d="": persona.get(k, d)
    )
    name = str(get("name") or "").strip()
    if not name:
        # 没名字的人格不能进列表：`_sanitize_persona_id` 会把它变成空串，
        # 而空 id 会让 `main.py:3354-3366` 判 `item_invalid` → 整个对账
        # 变成"不可验证"（等于白接）。**响亮记一条**，不静默丢。
        logger.warning("跳过没有名字的人设（插件无法为它生成人格 id）")
        return None

    prompt = _persona_prompt_text(persona)
    pid = str(get("id") or "").strip()
    return {
        # ⚠️ `name` 是插件的**主键**（真实现按 `["name"]` 匹配，
        #    插件 `_sanitize_persona_id` 也拿它当 persona_id）。
        "name": name,
        "prompt": prompt,
        # `id` 是我们自己的 id：让"插件配置里填我们的人设 id"也能查到
        # （`get_persona_v3_by_id` 两种都认）。
        "id": pid or name,
        # 照 `Personality` 补齐必需字段（缺了真实现 `Personality(**cfg)` 会 KeyError；
        # 插件只读 `name`/`prompt`，但形状对齐能少一类分叉）。
        "begin_dialogs": [],
        "mood_imitation_dialogs": [],
        "tools": None,
        "skills": None,
        "custom_error_message": None,
        "_begin_dialogs_processed": [],
        "_mood_imitation_dialogs_processed": "",
    }


def _persona_prompt_text(persona: Any) -> str:
    """把一个人设组装成 system 提示词文本。

    ⚠️ 组装失败**不静默**：记 ERROR 并退回"6 字段简单拼接"，
       绝不让插件拿到空串（空串 = 又变成那句误诊文案）。
    """
    try:
        from emotion_app.domain import prompt as prompt_mod
    except Exception:  # noqa: BLE001
        # `host/` 独立运行（不装 emotion_app）时走这里 —— 用手搓兜底，且**不报错**
        return _fallback_prompt_text(persona)
    try:
        assembled = prompt_mod.assemble(persona, include_time=False)
        text = assembled.text.strip()
        if text:
            return text
        logger.warning("人设组装结果为空，退回简单拼接")
    except Exception:  # noqa: BLE001
        logger.error("人设组装失败，退回简单拼接", exc_info=True)
    return _fallback_prompt_text(persona)


def _fallback_prompt_text(persona: Any) -> str:
    """不依赖 `emotion_app` 的兜底拼接（照 `persona_core_sections` 的分节风格）。"""
    if isinstance(persona, dict):
        get = lambda k: str(persona.get(k) or "").strip()  # noqa: E731
    else:
        get = lambda k: str(getattr(persona, k, "") or "").strip()  # noqa: E731

    parts: list[str] = []
    for title, key in (
        ("角色设定", "description"),
        ("性格", "personality"),
        ("场景", "scenario"),
        ("创作者备注", "creator_notes"),
    ):
        body = get(key)
        if body:
            parts.append(f"【{title}】\n{body}")
    return "\n\n".join(parts)


def _default_personality() -> dict[str, Any]:
    """真实现 `persona_mgr.py:9-19` 的 `DEFAULT_PERSONALITY`（逐字照抄 prompt/name）。

    ⚠️ 每次返回**新 dict** —— 插件会往人格条目里写东西
       （`meme_manager/main.py` 就有 `persona["prompt"] = ...` 这种写法），
       共享一份会被跨插件改坏。
    """
    return {
        "name": DEFAULT_PERSONA_NAME,
        "prompt": DEFAULT_PERSONA_PROMPT,
        "id": DEFAULT_PERSONA_NAME,
        "begin_dialogs": [],
        "mood_imitation_dialogs": [],
        "tools": None,
        "skills": None,
        "custom_error_message": None,
        "_begin_dialogs_processed": [],
        "_mood_imitation_dialogs_processed": "",
    }


__all__ = [
    "DEFAULT_PERSONA_NAME",
    "DEFAULT_PERSONA_PROMPT",
    "HostPersonaManager",
    "NO_PERSONA_SENTINEL",
    "WEBCHAT_PLATFORM_NAME",
]
