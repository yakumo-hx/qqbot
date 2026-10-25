# -*- coding: utf-8 -*-
"""给 LLBot OneBot 配置增加第三个反向 WS（观察通道 18082），用于 A5/A1 原始字段取证。

同时备份修改前的配置到 patches 目录。仅修改测试安装内配置。
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))
LLBOT = r"E:\QQbot-trials\llbot\bin\llbot"
CFG = os.path.join(LLBOT, "data", "config_2668851638.json")
DEFAULT = os.path.join(LLBOT, "default_config.json")
BK = r"E:\QQbot\qq_native_re\patches\llbot_trial_20261024-194032\llbot_config"
os.makedirs(BK, exist_ok=True)

entry = {
    "type": "ws-reverse",
    "enable": True,
    "url": "ws://127.0.0.1:18082",
    "heartInterval": 30000,
    "token": "",
    "reportSelfMessage": False,
    "reportOfflineMessage": False,
    "messageFormat": "array",
    "debug": False,
}

for path in (CFG, DEFAULT):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    shutil.copy2(path, os.path.join(BK, os.path.basename(path) + ".before_ch3"))
    conns = cfg["ob11"]["connect"]
    conns[:] = [c for c in conns if c.get("url") != "ws://127.0.0.1:18082"]
    conns.append(entry)
    cfg["ob11"]["connect"] = conns
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("updated", path, "connections=", [(c.get("url"), c.get("enable")) for c in conns])
print("backup dir", BK)
