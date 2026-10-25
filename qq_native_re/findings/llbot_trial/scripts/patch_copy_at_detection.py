# -*- coding: utf-8 -*-
"""最小适配补丁（仅测试副本）：LLBot 的 CQ @ 带 name 参数导致 @ 识别失败。

不兼容点（实测证据）：
  LLBot 群消息 raw_message = ``[CQ:at,qq=2668851638,name=Deepseek] ...``
  生产 reverse_bot.py:369 用字面量 ``f"[CQ:at,qq={BOT_QQ}]" in raw_message`` 判断，
  由于 ``,name=Deepseek`` 出现在 ``]`` 之前，子串不匹配 → bot_mentioned=False → @bot 不回复。

最小适配：
  E:\\QQbot-trials\\qqbot_test\\reverse_bot.py 中把该行改为正则：
  ``re.search(rf"\\[CQ:at,qq={re.escape(str(BOT_QQ))}(?:,[^\\]]*)?\\]", raw_message)``
  只改测试副本，不碰生产。

产物：patches/llbot_trial_*/test_copy_at_patch/（原文件副本 + diff + 说明）
"""
from __future__ import annotations

import difflib
import os
import shutil
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))
TARGET = r"E:\QQbot-trials\qqbot_test\reverse_bot.py"
PATCH_DIR = r"E:\QQbot\qq_native_re\patches\llbot_trial_20261024-194032\test_copy_at_patch"
OLD = 'bot_mentioned = f"[CQ:at,qq={BOT_QQ}]" in raw_message'
NEW = ('bot_mentioned = bool(re.search(\n'
       '        rf"\\[CQ:at,qq={re.escape(str(BOT_QQ))}(?:,[^\\]]*)?\\]", raw_message))\n'
       '    # LLBot 兼容（试验副本最小适配）：CQ at 可能带 name 等参数，如\n'
       '    # [CQ:at,qq=2668851638,name=Deepseek]；原字面量子串匹配会漏判 @bot。')

os.makedirs(PATCH_DIR, exist_ok=True)
src = open(TARGET, encoding="utf-8").read()
if OLD not in src:
    raise SystemExit("原行未找到，可能已打过补丁")
shutil.copy2(TARGET, os.path.join(PATCH_DIR, "reverse_bot.py.before_at_patch"))
new_src = src.replace(OLD, NEW, 1)
open(TARGET, "w", encoding="utf-8").write(new_src)

diff = "".join(difflib.unified_diff(
    src.splitlines(True), new_src.splitlines(True),
    fromfile="a/qqbot_test/reverse_bot.py", tofile="b/qqbot_test/reverse_bot.py"))
open(os.path.join(PATCH_DIR, "at_detection.diff"), "w", encoding="utf-8").write(diff)
open(os.path.join(PATCH_DIR, "README.md"), "w", encoding="utf-8").write(
    "# 测试副本最小适配：LLBot CQ @ 带 name 参数\n\n"
    f"- 生成时间：{datetime.now(TZ).isoformat(timespec='seconds')}\n"
    "- 不兼容点：LLBot 群消息 raw_message 形如 "
    "`[CQ:at,qq=2668851638,name=Deepseek] …`；生产 `reverse_bot.py:369` 用字面量 "
    "`[CQ:at,qq=2668851638]` 子串判断 → 漏判 @bot。\n"
    "- 适配：测试副本内改为正则匹配（允许 `,name=…` 等参数）。\n"
    "- 影响面：仅 `E:\\QQbot-trials\\qqbot_test\\reverse_bot.py`，生产文件零改动。\n"
    "- 回滚：`copy reverse_bot.py.before_at_patch` 覆盖回测试副本即可。\n"
    "- 建议（不在本轮实施）：生产若要迁 LLBot，需要同样修这一行；或改为解析 message 数组的 at 元素。\n")
print("patched", TARGET)
print(diff[:1200])
print("patch dir", PATCH_DIR)
