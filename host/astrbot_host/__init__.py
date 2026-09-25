# -*- coding: utf-8 -*-
"""AstrBot 兼容宿主 —— 让陪伴插件跑在我们的后端上。

⭐ 定位（见 `爱语/docs/PLATFORM_POSITIONING.md`）：
   作者的设计里 **AstrBot 只是"首个宿主适配器"**，`embedded_app` 是一等部署模式。
   所以这个包实现的是**宿主端口**，不是"另一套业务实现"。

三层：
   * `compat/`  —— `astrbot` 影子包（行为等价）
   * `registry.py` —— 事件总线（EventType / handler 注册表 / LLM 工具表）
   * `loader/`   —— 插件加载器（扫描/导入/实例化/生命周期）

🔴 **硬约束**：`情绪app/emotion_app/domain/` **绝不能** import 本包
   （照作者 §7「便携层不能依赖宿主」判据）。
"""
__version__ = "0.1.0"
