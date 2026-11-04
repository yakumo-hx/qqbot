# -*- coding: utf-8 -*-
"""对比两个评测回合（同一批题目）：python -m language_lab.eval.compare 2 3"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

REPORT_DIR = Path(__file__).resolve().parent / "reports"


def _load(round_no: int) -> dict:
    files = sorted(REPORT_DIR.glob(f"round{round_no}_*.json"))
    if not files:
        raise SystemExit(f"找不到 round{round_no} 报告")
    return json.loads(files[-1].read_text(encoding="utf-8"))


def _agg(cases: list[dict], key: str) -> float:
    vals = [float(c.get(key) or 0) for c in cases]
    return round(sum(vals) / len(vals), 2) if vals else 0.0


def _rate(cases: list[dict], key: str) -> float:
    return round(100 * sum(1 for c in cases if c.get(key)) / len(cases), 1) if cases else 0.0


def compare(a: int, b: int) -> str:
    da, db = _load(a), _load(b)
    ca = {c["id"]: c for c in (da.get("cases") or [])}
    cb = {c["id"]: c for c in (db.get("cases") or [])}
    ids = [i for i in ca if i in cb]
    if not ids:
        raise SystemExit("两轮没有共同题目")
    xa = [ca[i] for i in ids]
    xb = [cb[i] for i in ids]
    metrics = [
        ("平均长度", _agg(xa, "ai_len"), _agg(xb, "ai_len"), "真人 " + str(_agg(xa, "ref_len"))),
        ("平均气泡数", _agg(xa, "ai_n"), _agg(xb, "ai_n"), "真人 " + str(_agg(xa, "ref_n"))),
        ("媒体率%", _rate(xa, "ai_media"), _rate(xb, "ai_media"), "真人 " + str(_rate(xa, "ref_media"))),
        ("emoji率%", _rate(xa, "ai_emoji"), _rate(xb, "ai_emoji"), "真人 " + str(_rate(xa, "ref_emoji"))),
        ("句尾不加标点%", _rate(xa, "ai_no_punct"), _rate(xb, "ai_no_punct"), "真人 " + str(_rate(xa, "ref_no_punct"))),
        ("问句率%", _rate(xa, "ai_q"), _rate(xb, "ai_q"), "真人 " + str(_rate(xa, "ref_q"))),
    ]
    lines = [
        f"# 回合对比：round{a} → round{b}",
        "",
        f"- 共同题目：{len(ids)} 条（同 id，可直接归因）",
        f"- 规范：{da['meta'].get('spec')} → {db['meta'].get('spec')}",
        "",
        "| 指标 | round%d | round%d | 变化 | 真人参考 |" % (a, b),
        "|---|---|---|---|---|",
    ]
    for name, va, vb, ref in metrics:
        lines.append(f"| {name} | {va} | {vb} | {round(vb - va, 2):+} | {ref} |")
    # 分题型媒体
    lines += ["", "### 分题型（媒体率）", "", "| 题型 | n | round%d | round%d | 真人 |" % (a, b), "|---|---|---|---|---|"]
    types = sorted({c.get("type") for c in xa})
    for t in types:
        sa = [c for c in xa if c.get("type") == t]
        sb = [c for c in xb if c.get("type") == t]
        if not sa:
            continue
        lines.append(f"| {t} | {len(sa)} | {_rate(sa, 'ai_media')}% | {_rate(sb, 'ai_media')}% | {_rate(sa, 'ref_media')}% |")
    text = "\n".join(lines) + "\n"
    out = REPORT_DIR / f"compare_round{a}_round{b}.md"
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"-> {out}")
    return text


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("用法：python -m language_lab.eval.compare <roundA> <roundB>")
    compare(int(sys.argv[1]), int(sys.argv[2]))
