# -*- coding: utf-8 -*-
"""hello-world 插件 —— 验证宿主兼容层的最小闭环。

🔴 它**故意只用真 AstrBot 的 API 写法**（不碰我们的任何内部东西），
   这样"它跑通了"就等于"兼容层对真插件写法是通的"。
覆盖：
  * `from astrbot.api.star import Context, Star, StarTools`
  * `from astrbot.api.event import AstrMessageEvent, MessageChain, filter`
  * 5 类装饰器：`llm_tool` / `command` / `event_message_type` /
    `on_llm_request` / `on_plugin_loaded`
  * `StarTools.get_data_dir()` 取数据目录
  * `context.get_config()` 读配置
"""
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools


class HelloPlugin(Star):
    """最小插件（继承 `Star` 即可被宿主发现）。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.greeted = 0
        self.data_dir = StarTools.get_data_dir("hello_plugin")

    @filter.on_plugin_loaded()
    async def on_loaded(self, *args, **kwargs):
        """插件加载完成钩子。"""
        self.logger.info("hello_plugin 已加载，数据目录=%s", self.data_dir)

    @filter.command("你好", alias={"hello"})
    async def hello_cmd(self, event: AstrMessageEvent, name: str = "世界"):
        """打个招呼。

        Args:
            name(string): 要打招呼的对象
        """
        self.greeted += 1
        yield event.plain_result(f"你好，{name}！（第 {self.greeted} 次）")

    @filter.llm_tool(name="get_greeting_count")
    async def greeting_count(self, event: AstrMessageEvent):
        """查询已经打过多少次招呼。

        Args:
            无
        """
        return f"已经打过 {self.greeted} 次招呼。"

    @filter.event_message_type()
    async def on_any_message(self, event: AstrMessageEvent):
        """任何消息都会到这里（用于观察事件流）。"""
        self.logger.debug("收到消息：%s", event.message_str)

    @filter.on_llm_request()
    async def before_llm(self, event: AstrMessageEvent, req=None):
        """LLM 请求前钩子（可注入提示词）。"""
        return None

    async def initialize(self) -> None:
        self.logger.info("hello_plugin initialize()")

    async def terminate(self) -> None:
        self.logger.info("hello_plugin terminate()")
