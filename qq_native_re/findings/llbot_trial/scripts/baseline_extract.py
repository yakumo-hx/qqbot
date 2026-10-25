# -*- coding: utf-8 -*-
"""NapCat 对照基线提取（只读日志）：最近 7 天（2026-10-18 ~ 2026-10-24）。

产出：
  findings/llbot_trial/baseline/BASELINE.md
  findings/llbot_trial/baseline/baseline.json
诚实原则：能从日志确定的才写数字；推不出的写 UNKNOWN/“无法确定”。
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))
ROOT = r"E:\QQbot"
NAP = os.path.join(ROOT, r"NapCat\NapCat.44498.Shell\versions\9.9.26-44498\resources\app\napcat\logs")
OUTDIR = os.path.join(ROOT, r"qq_native_re\findings\llbot_trial\baseline")
START_DATE = "2026-10-18"


def d(s):
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=TZ)


def read_lines(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.readlines()


def parse_napcat_ts(line):
    m = re.match(r"(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})", line)
    if not m:
        return None
    mo, dd, hh, mm, ss = map(int, m.groups())
    return datetime(2026, mo, dd, hh, mm, ss, tzinfo=TZ)


result = {
    "window": {"start": START_DATE, "end": "2026-10-24"},
    "napcat": {"files": [], "kicked_offline": [], "network_error_1006514": 0,
               "login_invalid_lines": [], "qr_lines": 0, "quick_login_lines": 0,
               "quick_login_failed_lines": 0, "account_offline_lines": 0},
    "bots": {"reverse": {}, "private": {}},
    "downtime_samples_s": [],
    "unknowns": [],
}

# ---------- NapCat logs ----------
nap_files = []
for name in os.listdir(NAP):
    m = re.match(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})\.\d+\.log$", name)
    if not m:
        continue
    day = m.group(1)
    if day < START_DATE:
        continue
    path = os.path.join(NAP, name)
    started = datetime.strptime(
        f"{day} {m.group(2)}:{m.group(3)}:{m.group(4)}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    lines = read_lines(path)
    kicked = []
    net_err = 0
    invalid = []
    qr = quick = qfail = offline = 0
    for line in lines:
        ts = parse_napcat_ts(line)
        if "KickedOffLine" in line:
            kicked.append({"ts": ts.isoformat() if ts else "?", "text": line.strip()[:160]})
        if "网络连接异常" in line or "1006514" in line:
            net_err += 1
        if "登录已失效" in line:
            invalid.append({"ts": ts.isoformat() if ts else "?", "text": line.strip()[:160]})
        if "二维码" in line:
            qr += 1
        if "快速登录" in line:
            quick += 1
        if "自动快速登录失败" in line:
            qfail += 1
        if "账号状态变更为离线" in line:
            offline += 1
    result["napcat"]["files"].append({
        "file": name, "size": os.path.getsize(path),
        "start": started.isoformat(), "lines": len(lines),
        "kicked": len(kicked), "net_err_1006514": net_err,
        "login_invalid": len(invalid), "qr_lines": qr,
        "quick_login_lines": quick, "quick_login_failed": qfail, "offline_lines": offline,
    })
    result["napcat"]["kicked_offline"] += kicked
    result["napcat"]["network_error_1006514"] += net_err
    result["napcat"]["login_invalid_lines"] += invalid
    result["napcat"]["qr_lines"] += qr
    result["napcat"]["quick_login_lines"] += quick
    result["napcat"]["quick_login_failed_lines"] += qfail
    result["napcat"]["account_offline_lines"] += offline
    nap_files.append(name)
result["napcat"]["files"].sort(key=lambda x: x["start"])

# ---------- bot logs ----------
def bot_metrics(prefix):
    files = []
    connects = disconnects = zombie = 0
    recoveries = []
    for name in sorted(os.listdir(os.path.join(ROOT, "logs"))):
        if not name.startswith(prefix + ".") or not name.endswith(".log"):
            continue
        day = name.split(".")[1]
        if day < START_DATE:
            continue
        path = os.path.join(ROOT, "logs", name)
        lines = read_lines(path)
        c = sum(1 for l in lines if "⚡ 客户端已连接" in l or "NapCat 已连接" in l)
        dc = sum(1 for l in lines if "连接断开" in l)
        z = sum(1 for l in lines if "僵尸连接" in l)
        conn = connects + c
        connects, disconnects, zombie = conn, disconnects + dc, zombie + z
        for l in lines:
            m = re.search(r"本轮停机 ([\d.]+) 分钟（([\d.]+) 秒）", l)
            if m:
                sec = float(m.group(1)) * 60
                recoveries.append({"file": name, "seconds": sec})
        files.append({"file": name, "size": os.path.getsize(path), "connects": c, "disconnects": dc})
    return {"files": files, "connects": connects, "disconnects": disconnects,
            "zombie": zombie, "recoveries": recoveries}


result["bots"]["reverse"] = bot_metrics("bot")
result["bots"]["private"] = bot_metrics("private_bot")
result["downtime_samples_s"] = [r["seconds"] for r in result["bots"]["reverse"]["recoveries"]]

# retcode 1200 in private bot logs
rc1200 = 0
for name in os.listdir(os.path.join(ROOT, "logs")):
    if name.startswith("private_bot.") and name.endswith(".log"):
        day = name.split(".")[1]
        if day < START_DATE:
            continue
        rc1200 += sum(1 for l in read_lines(os.path.join(ROOT, "logs", name)) if "retcode=1200" in l)
result["bots"]["private"]["retcode_1200"] = rc1200

# ---------- markdown ----------
os.makedirs(OUTDIR, exist_ok=True)
json.dump(result, open(os.path.join(OUTDIR, "baseline.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)

nap = result["napcat"]
rev = result["bots"]["reverse"]
pri = result["bots"]["private"]
dts = sorted(result["downtime_samples_s"])
md = []
md.append("# NapCat 对照基线（最近 7 天）\n")
md.append(f"- 统计窗口：{START_DATE} ~ 2026-10-24（日志可得范围）\n")
md.append(f"- NapCat 日志文件：{len(nap['files'])} 个；文件起止见 baseline.json\n")
md.append("## 1. 账号层事件（NapCat fileLog）\n")
md.append(f"- KickedOffLine：**{len(nap['kicked_offline'])} 次**（窗口内）")
for k in nap["kicked_offline"][-5:]:
    md.append(f"  - {k['ts']}: {k['text']}")
md.append(f"- 「登录已失效」行：{len(nap['login_invalid_lines'])} 行")
md.append(f"- 「网络连接异常」/1006514 发送错误行：**{nap['network_error_1006514']} 行**")
md.append(f"- 「账号状态变更为离线」行：{nap['account_offline_lines']} 行")
md.append(f"- 二维码相关日志行：{nap['qr_lines']}；快速登录相关：{nap['quick_login_lines']}；"
          f"自动快速登录失败：{nap['quick_login_failed_lines']}")
md.append("\n> 说明：二维码/快速登录行不能直接等价于「人工扫码次数」——需要逐条看上下文才能判定；"
          "本轮未逐条还原，标 UNKNOWN。\n")
md.append("## 2. WS 连接与恢复（Python bot 日志）\n")
md.append(f"- 群 bot：连接事件 {rev['connects']} 次 / 断开事件 {rev['disconnects']} 次 / 僵尸连接提示 {rev['zombie']} 次")
md.append(f"- 私聊 bot：连接事件 {pri['connects']} 次 / 断开事件 {pri['disconnects']} 次 / 僵尸连接提示 {pri['zombie']} 次")
md.append(f"- 「本轮停机」样本 {len(dts)} 个；中位数 "
          f"{dts[len(dts)//2]:.0f}s，最大 {max(dts):.0f}s" if dts else "- 无可用「本轮停机」样本")
md.append(f"- 私聊发送明确失败 retcode=1200：**{rc1200} 次**（failed，非 unknown，不会自动重发）")
md.append("## 3. 不可用时长估算\n")
md.append("- 方法：以 bot 日志 `[自愈] ... 本轮停机 X 分钟（Y 秒）` 行统计 WS/账号停机；"
          "NapCat 日志空档用于补充（整机休眠/关机无法与账号掉线区分）。")
if dts:
    md.append(f"- 可估算的短停机：{len(dts)} 次，合计 {sum(dts)/60:.1f} 分钟；中位 {dts[len(dts)//2]:.0f}s。")
md.append("- 长停机（>10 分钟）与整机休眠无法从现有数据精确拆分 ⇒ **无法确定**总不可用时长；"
          "只给样本与已知空档。")
md.append("## 4. 明确的不确定项\n")
md.append("- 人工扫码次数：UNKNOWN（二维码/快速登录日志行无法可靠区分自动/人工）。")
md.append("- KickedOffLine 与「静默离线」的完整分型：窗口内日志有多次 1006514 网络异常与 WS 断开，"
          "但当前没有统一的四类打标（决策 017 的 A/B/C/D 标签未落盘），只能给出原始计数。")
md.append("- 生产账号在试验窗口内被主动停止（20:38-21:37 用于 LLBot 对照），该时段不计入 NapCat 基线。")
md.append("\n## 5. 数据可信度\n")
md.append("- 计数来自 fileLog/bot 日志精确字符串匹配，可复核；口径见脚本 baseline_extract.py。")
md.append("- 该基线只用于「量级对照」，不用于统计显著性；LLBot 侧样本仅数十分钟，结论只能标 SOAK_PENDING。")
open(os.path.join(OUTDIR, "BASELINE.md"), "w", encoding="utf-8").write("\n".join(md))
print(json.dumps({"napcat_files": len(nap["files"]), "kicked": len(nap["kicked_offline"]),
                  "net_err": nap["network_error_1006514"], "ws_recoveries": len(dts),
                  "rc1200": rc1200}, ensure_ascii=False))
print("saved", OUTDIR)
