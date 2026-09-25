# -*- coding: utf-8 -*-
"""差分：我们的路由匹配器 vs 真 AstrBot 的（同一批路由/输入，比输出）。"""
import sys
sys.path.insert(0, "/root/Ai-works/情绪app/host")

from astrbot.dashboard.api.plugins import (          # 真实现
    _plugin_api_route_pattern as real_pattern,
    _match_registered_web_api as real_match,
)
from astrbot_host.web_router import (                 # 我们
    plugin_api_route_pattern as our_pattern,
    match_registered_web_api as our_match,
)

async def _h(**kw):
    return {}

# 覆盖：无参数 / 单段参数 / 贪婪参数 / 多参数 / 特殊字符 / 末尾斜杠
ROUTES = [
    "/astrbot_plugin_x/page/overview",
    "/astrbot_plugin_x/page/persona/<persona_id>",
    "/astrbot_plugin_x/page/files/<path:file_path>",
    "/astrbot_plugin_x/page/a/<x>/b/<y>",
    "/astrbot_plugin_x/page/dot.name/under_score/dash-name",
    "/astrbot_plugin_x/page/nested/<path:p>/tail",
    "no-slash-route",
    "/trailing/",
]
PATHS = [
    "astrbot_plugin_x/page/overview",
    "astrbot_plugin_x/page/persona/abc123",
    "astrbot_plugin_x/page/persona/a/b",           # 单段参数**不该**匹配
    "astrbot_plugin_x/page/files/a/b/c.png",       # 贪婪**该**匹配
    "astrbot_plugin_x/page/files/",
    "astrbot_plugin_x/page/a/1/b/2",
    "astrbot_plugin_x/page/dot.name/under_score/dash-name",
    "astrbot_plugin_x/page/nested/deep/er/tail",
    "no-slash-route",
    "trailing",
    "trailing/",
    "unknown/path",
]
METHODS = ["GET", "get", "POST", "DELETE"]

fails = 0
checks = 0

# ① 正则本身逐字对比
for r in ROUTES:
    checks += 1
    a, b = real_pattern(r), our_pattern(r)
    if a != b:
        fails += 1
        print(f"❌ pattern 不同 route={r}\n   真={a!r}\n   我={b!r}")
print(f"① 正则逐字对比：{checks - fails}/{checks}")

# ② 匹配结果对比（含 path_params）
reg = [(r, _h, ["GET", "post", "DELETE"], "d") for r in ROUTES]
m_fails = m_checks = 0
for p in PATHS:
    for m in METHODS:
        m_checks += 1
        ra = real_match(reg, p, m)
        rb = our_match(reg, p, m)
        ka = None if ra is None else (ra[0] is _h, ra[1])
        kb = None if rb is None else (rb[0] is _h, rb[1])
        if ka != kb:
            m_fails += 1
            print(f"❌ 匹配不同 path={p} method={m}\n   真={ka}\n   我={kb}")
print(f"② 匹配结果对比：{m_checks - m_fails}/{m_checks}")

# ③ 打印几条真实样例，便于人眼确认
print("\n样例（必须看得出 <path:> 贪婪、<x> 单段）：")
for p in ["astrbot_plugin_x/page/persona/a/b", "astrbot_plugin_x/page/files/a/b/c.png",
          "astrbot_plugin_x/page/nested/deep/er/tail"]:
    print(f"  {p} -> {our_match(reg, p, 'GET')}")

print(f"\n结果：{checks - fails + m_checks - m_fails}/{checks + m_checks} 项通过")
sys.exit(1 if (fails or m_fails) else 0)
