# -*- coding: utf-8 -*-
"""把 E:\\QQbot-trials\\qqbot_test 配置成隔离业务测试副本（最小适配）。

改动（全部在测试副本内，不碰生产）：
1. reverse_bot.py: HOST 127.0.0.1 / PORT 18080（原 0.0.0.0:8080）
2. config/private_chat.json: port 18081、host 127.0.0.1、data_dir=data/private_test、
   proactive.enabled=false、search.enabled=false
3. config/group_features.json: 只保留 284517232，且只开 decision_engine/commands/vision
4. config/auto_restart.json: alert/restart 全关（避免测试副本弹窗/重启）

产出：copy_adaptation.json（记录每处改动的 before/after 摘要）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))
ROOT = r"E:\QQbot-trials\qqbot_test"
OUT = os.path.join(r"E:\QQbot\qq_native_re\findings\llbot_trial\logs", "copy_adaptation.json")
changes = []


def load(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return json.load(f)


def save(rel, data):
    with open(os.path.join(ROOT, rel), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


# 1) reverse_bot.py 端口
p = os.path.join(ROOT, "reverse_bot.py")
src = open(p, encoding="utf-8").read()
before = [l for l in src.splitlines() if l.strip().startswith(("HOST =", "PORT ="))]
src2 = src.replace('HOST = "0.0.0.0"', 'HOST = "127.0.0.1"').replace("PORT = 8080", "PORT = 18080")
after = [l for l in src2.splitlines() if l.strip().startswith(("HOST =", "PORT ="))]
open(p, "w", encoding="utf-8").write(src2)
changes.append({"file": "reverse_bot.py", "before": before, "after": after})

# 2) private_chat.json
cfg = load("config/private_chat.json")
old = {k: cfg.get(k) for k in ("host", "port", "data_dir", "proactive", "search")}
cfg["host"] = "127.0.0.1"
cfg["port"] = 18081
cfg["data_dir"] = "data/private_test"
cfg["proactive"]["enabled"] = False
cfg["search"]["enabled"] = False
save("config/private_chat.json", cfg)
changes.append({"file": "config/private_chat.json", "before": {
    "host": old["host"], "port": old["port"], "data_dir": old["data_dir"],
    "proactive.enabled": old["proactive"].get("enabled"), "search.enabled": old["search"].get("enabled")},
    "after": {"host": cfg["host"], "port": cfg["port"], "data_dir": cfg["data_dir"],
              "proactive.enabled": False, "search.enabled": False}})

# 3) group_features.json
gf = load("config/group_features.json")
entry = gf.get("284517232", {})
before_feat = dict(entry.get("features", {}))
entry["features"] = {"decision_engine": True, "commands": True, "vision": True,
                     "plus_one": False, "newcomer_welcome": False,
                     "profile_record": False, "search": False, "sticker": False,
                     "poke_menu": False, "reaction_menu": False}
save("config/group_features.json", {"284517232": entry})
changes.append({"file": "config/group_features.json",
                "before": {"groups": list(gf.keys())[:5], "features": before_feat},
                "after": {"groups": ["284517232"], "features": entry["features"]}})

# 4) auto_restart.json
ar = load("config/auto_restart.json")
old_ar = dict(ar)
ar["alert"]["enabled"] = False
ar["restart"]["enabled"] = False
ar["dry_run"] = True
save("config/auto_restart.json", ar)
changes.append({"file": "config/auto_restart.json", "before": old_ar, "after": ar})

# 5) data_dir 目录
os.makedirs(os.path.join(ROOT, "data", "private_test"), exist_ok=True)
os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)

json.dump({"generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
           "root": ROOT, "changes": changes},
          open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(json.dumps(changes, ensure_ascii=False, indent=2))
print("saved", OUT)
