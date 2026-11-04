# -*- coding: utf-8 -*-
"""生成带上下文的盲测包：从最近两个回合抽题，A/B 匿名，附答题模板。

用法：python -m language_lab.eval.make_blind
产出：reports/blind_test_v4.md（给人看）、blind_test_v4_key.json（答案，别看）
"""

from __future__ import annotations

import glob
import json
import random
from pathlib import Path

REP = Path(__file__).resolve().parent / "reports"
SC = Path(__file__).resolve().parent / "data" / "scenarios"
CACHE = Path(__file__).resolve().parent / "data" / "cache" / "rules_v4_prompt"


def _smap() -> dict:
    out = {}
    for f in glob.glob(str(SC / "*" / "*.jsonl")):
        for line in open(f, encoding="utf-8"):
            try:
                sc = json.loads(line)
            except ValueError:
                continue
            out[sc["id"]] = sc
    return out


def _round(n: int) -> dict | None:
    files = sorted(REP.glob(f"round{n}_*.json"))
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


def main() -> int:
    smap = _smap()
    picked = []
    for n in (6, 7):
        data = _round(n)
        if not data:
            continue
        items = []
        for case in data.get("cases", []):
            sid = case["id"]
            gen_path = CACHE / f"{sid}.json"
            if not gen_path.exists():
                continue
            gen = json.loads(gen_path.read_text(encoding="utf-8"))
            if not gen.get("ok") or not gen.get("bubbles"):
                continue
            sc = smap.get(sid)
            if sc:
                items.append((sc, gen))
        random.Random(n).shuffle(items)
        picked.extend(items[:6])
    rng = random.Random(30)
    rng.shuffle(picked)

    lines = [
        "# 人工盲测包 v4（12 组）",
        "",
        "每组 A/B 里，一条是 AI 写的，一条是真人当时发的；上下文只给最近 3 条。",
        "请按格式回复：`1 A 4`（第 1 组猜 A 是 AI，愿意让它发到群里的程度 1-5 分）；不确定写 `1 ? 3`。",
        "",
        "判断标准：像不像本群真人的说话方式、你会不会觉得“这是 AI 写的”。",
        "",
    ]
    key = {}
    for i, (sc, gen) in enumerate(picked, 1):
        ctx_lines = []
        for c in (sc.get("context") or [])[-3:]:
            text = (c.get("text") or "").strip()
            if not text and c.get("media"):
                text = f"[{c['media'][0]}]"
            if len(text) > 60:
                text = text[:60] + "…"
            ctx_lines.append(f"- {c.get('nick') or c.get('uid')}：{text}")
        ai_text = "\n".join(gen["bubbles"]) if gen.get("bubbles") else "（发表情/图片）"
        human_bubbles = sc["reference"]["bubbles"]
        human_text = "\n".join(
            (b.get("text") or "").strip() for b in human_bubbles if (b.get("text") or "").strip()
        ) or "（发表情/图片）"
        order = rng.choice(["ai_a", "ai_b"])
        a, b = (ai_text, human_text) if order == "ai_a" else (human_text, ai_text)
        key[str(i)] = {"ai_side": "A" if order == "ai_a" else "B",
                       "scenario_id": sc["id"], "type": sc["type"]}
        lines += [f"### {i}（{sc['type']}）", "上下文："] + ctx_lines + [f"A：{a}", f"B：{b}", ""]
    lines += ["---", "看完直接回我，例如：", "`1 A 4, 2 ? 3, 3 B 5 ...`"]

    out = REP / "blind_test_v4.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (REP / "blind_test_v4_key.json").write_text(
        json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"picked {len(picked)} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
