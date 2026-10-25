# -*- coding: utf-8 -*-
"""在 LLBot/PMHQ 官方二进制中检索关键配置字符串（只读分析，不执行登录）。"""
import re
from pathlib import Path

ROOT = Path(r"E:\QQbot-trials\llbot")
KEYS = ["auth_token", "headless", "pmhq_config", "quick_login_qq", "qq_path",
        "pmhq_api_token", "Luckylillia", "luckylillia", "auth.luckylillia",
        "data/config_", "config_", "instance", "qq_args"]

for rel in ["bin/llbot/llbot.js", "bin/pmhq/pmhq.dll"]:
    p = ROOT / rel
    if not p.exists():
        print("MISSING", p)
        continue
    data = p.read_bytes()
    print("=" * 30, rel, len(data), "bytes")
    text = data.decode("utf-8", "ignore")
    for key in KEYS:
        idxs = [m.start() for m in re.finditer(re.escape(key), text)]
        print(f"  {key}: {len(idxs)} hits")
        for i in idxs[:3]:
            snippet = text[max(0, i - 120):i + 160].replace("\n", " ")
            print("     ...", snippet[:280])

# PMHQ exe: 只读字符串
p = ROOT / "bin/pmhq/pmhq.exe"
if p.exists():
    data = p.read_bytes()
    print("=" * 30, "bin/pmhq/pmhq.exe", len(data), "bytes")
    # .NET/Go? 先看可打印 ASCII 串
    asc = re.findall(rb"[\x20-\x7e]{6,}", data)
    joined = b"\n".join(asc).decode("ascii", "ignore")
    for key in ["auth_token", "headless", "pmhq_config", "quick_login_qq", "qq_path",
                "auth.luckylillia", "--config", "manager-server"]:
        hits = [m.start() for m in re.finditer(re.escape(key), joined)]
        print(f"  {key}: {len(hits)} hits")
        for i in hits[:2]:
            print("     ...", joined[max(0, i - 100):i + 160].replace("\n", " | ")[:260])
