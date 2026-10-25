# -*- coding: utf-8 -*-
"""检查已下载的 LLBot 官方 zip 结构（只读，不解压）。"""
import json
import zipfile
from pathlib import Path

DL = Path(r"E:\QQbot-trials\download")
for z in ["LuckyLillia-Desktop-win-x64.zip", "LuckyLillia-CLI-win-x64.zip"]:
    p = DL / z
    zf = zipfile.ZipFile(p)
    names = zf.namelist()
    top = sorted({n.split("/")[0] + ("/" if "/" in n else "") for n in names})
    print("=" * 20, z)
    print("top-level:", top)
    print("exe:", [n for n in names if n.lower().endswith(".exe")])
    print("pmhq:", [n for n in names if "pmhq" in n.lower()][:40])
    for cfg in ("bin/llbot/default_config.json", "bin/llbot/package.json"):
        if cfg in names:
            print("---", cfg, "---")
            print(zf.read(cfg).decode("utf-8", "replace")[:4000])
    zf.close()
