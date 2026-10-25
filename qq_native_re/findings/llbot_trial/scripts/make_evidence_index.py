# -*- coding: utf-8 -*-
"""生成证据索引：文件/大小/SHA256/敏感标记（不写任何消息正文）。"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))
ROOT = r"E:\QQbot\qq_native_re\findings\llbot_trial"
SENSITIVE_HINTS = ("events.jsonl", "results.jsonl", "crash", ".dmp", "observer", "probe", "evidence_", "attempts")
rows = []


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


for dirpath, dirnames, filenames in os.walk(ROOT):
    for name in filenames:
        if name in ("EVIDENCE_INDEX.md", "EVIDENCE_INDEX.json"):
            continue
        path = os.path.join(dirpath, name)
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        sensitive = any(k in name for k in SENSITIVE_HINTS)
        rows.append({
            "path": rel, "bytes": os.path.getsize(path),
            "sha256": sha256(path), "sensitive": sensitive,
        })
rows.sort(key=lambda r: r["path"])
doc = {
    "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
    "root": ROOT,
    "notes": [
        "sensitive=true 的文件可能含群聊原文/图片直链/崩溃转储/消息 ID 等，仅存本机，禁止外发或贴进报告。",
        "REPORT.md / TEST_MATRIX.md / manifest.json 均不含密码、AccessToken、登录凭据或完整私聊内容。",
    ],
    "files": rows,
}
json.dump(doc, open(os.path.join(ROOT, "EVIDENCE_INDEX.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
lines = ["# 证据索引（EVIDENCE_INDEX）", "",
         f"- 生成时间：{doc['generated_at']}",
         f"- 文件数：{len(rows)}；敏感文件数：{sum(1 for r in rows if r['sensitive'])}",
         "- 敏感文件仅存本机，不得外发或贴入报告。", "",
         "| 文件 | 大小(B) | SHA256(前16) | 敏感 |", "|---|---:|---|---|"]
for r in rows:
    lines.append(f"| `{r['path']}` | {r['bytes']} | `{r['sha256'][:16]}…` | {'是' if r['sensitive'] else '否'} |")
open(os.path.join(ROOT, "EVIDENCE_INDEX.md"), "w", encoding="utf-8").write("\n".join(lines))
print("files", len(rows), "sensitive", sum(1 for r in rows if r["sensitive"]))

