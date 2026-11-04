# -*- coding: utf-8 -*-
"""交互式盲测程序（命令行，无 GUI）。

用法：
    python -m language_lab blind
    python -m language_lab blind --limit 6
    python -m language_lab blind --reset
    python -m language_lab blind --quick        # 只猜 A/B/?, 不评接受度

每组显示最近 3 条上下文 + A/B 两条回复；输入 `A 4` 表示“猜 A 是 AI，接受度 4/5”。
输入 `? 3` 表示看不出；`s` 跳过；`q` 保存退出。
结果写入 eval/reports/blind_result_*.md / .json。
"""

from __future__ import annotations

import argparse
import glob
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from language_lab.eval import context_builder as cb  # noqa: E402

EVAL = Path(__file__).resolve().parent
REP = EVAL / "reports"
SC = EVAL / "data" / "scenarios"
DATA = EVAL / "data" / "blind"
PACK_PATH = DATA / "pack_v5.json"
ANS_PATH = DATA / "answers_v5.json"

_INPUT_RE = re.compile(r"^([AB?absq])\s*([1-5])?$", re.I)


def _load_scenarios() -> dict:
    out = {}
    for pattern in (str(SC / "*" / "*.jsonl"),):
        for f in glob.glob(pattern):
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


def build_pack(rounds: list[int], per_round: int, cache_dir: str, seed: int = 30,
               min_ctx: int = 10, max_ctx: int = 30, gap_s: int = 900) -> list[dict]:
    from language_lab.lab import settings
    cfg = settings.load_config()
    gid = str(cfg["gid"])
    rows = cb.load_corpus(gid)
    media_index = cb.build_media_index(gid)
    smap = _load_scenarios()
    cache = EVAL / "data" / "cache" / cache_dir
    picked: list[tuple[dict, dict]] = []
    for n in rounds:
        data = _round(n)
        if not data:
            continue
        items = []
        for case in data.get("cases", []):
            sid = case["id"]
            gp = cache / f"{sid}.json"
            if not gp.exists():
                continue
            gen = json.loads(gp.read_text(encoding="utf-8"))
            if not gen.get("ok") or not gen.get("bubbles"):
                continue
            sc = smap.get(sid)
            if sc:
                items.append((sc, gen))
        random.Random(n).shuffle(items)
        # 媒体参考若没有识图描述，用户无法判断内容，跳过，避免不公平
        valid = []
        for sc, gen in items:
            ref = (sc.get("reference") or {}).get("bubbles") or []
            has_text = any((b.get("text") or "").strip() for b in ref)
            has_desc = False
            for b in ref:
                row = {"uid": b.get("uid"), "media": b.get("media") or [], "ts_epoch": 0}
                try:
                    row["ts_epoch"] = datetime.fromisoformat(b["ts"]).timestamp()
                except Exception:
                    pass
                key = (str(row["uid"] or ""), int(float(row["ts_epoch"] or 0)))
                if any(x.get("desc") for x in (media_index.get(key) or [])):
                    has_desc = True
                    break
            if has_text or has_desc:
                valid.append((sc, gen))
        picked.extend(valid[:per_round])
    rng = random.Random(seed)
    rng.shuffle(picked)

    def _bubble_row(b: dict) -> dict:
        row = {"uid": b.get("uid"), "text": b.get("text") or "",
               "media": b.get("media") or [], "ts_epoch": 0}
        try:
            row["ts_epoch"] = datetime.fromisoformat(b["ts"]).timestamp()
        except Exception:
            pass
        return row

    pack = []
    for sc, gen in picked:
        ref = sc["reference"]["bubbles"]
        idx = cb.find_ref_index(rows, ref)
        ctx = []
        if idx is not None:
            for r in cb.semantic_context(rows, idx, min_n=min_ctx, max_n=max_ctx, gap_s=gap_s):
                text = cb.media_text(r, media_index)
                if len(text) > 120:
                    text = text[:120] + "…"
                ctx.append({"nick": r.get("nick") or str(r.get("uid")),
                            "text": text, "is_bot": bool(r.get("is_bot"))})
        if not ctx:  # 定位不到参考消息时退回题目自带的 8 条
            for c in sc.get("context") or []:
                text = (c.get("text") or "").strip()
                if not text and c.get("media"):
                    text = f"[{c['media'][0]}]"
                ctx.append({"nick": c.get("nick") or c.get("uid"), "text": text})

        human_parts = [cb.media_text(_bubble_row(b), media_index) for b in ref]
        human = "\n".join(t for t in human_parts if t) or "（发表情/图片）"
        ai_parts = list(gen.get("bubbles") or [])
        if gen.get("use_media"):
            ai_parts.append("（AI 附加：表情/图片）" if ai_parts else "（AI 选择发表情/图片）")
        ai_text = "\n".join(ai_parts)
        order = rng.choice(["ai_a", "ai_b"])
        a, b = (ai_text, human) if order == "ai_a" else (human, ai_text)
        pack.append({
            "id": sc["id"], "type": sc["type"], "context": ctx,
            "A": a, "B": b, "ai_side": "A" if order == "ai_a" else "B",
        })
    return pack


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _ask(item: dict, idx: int, total: int, quick: bool) -> dict | None:
    print("\n" + "=" * 64)
    print(f"第 {idx}/{total} 组 · 题型 {item['type']} · 上下文 {len(item['context'])} 条")
    print("-" * 64)
    for c in item["context"]:
        suffix = "（bot）" if c.get("is_bot") else ""
        print(f"  {c['nick']}{suffix}：{c['text']}")
    print("-" * 64)
    print(f"A：{item['A']}")
    print(f"B：{item['B']}")
    print("-" * 64)
    tip = "输入 A/B/?（可带 1-5 分，如 `A 4`）；s 跳过，q 保存退出"
    while True:
        raw = input(tip + "\n> ").strip()
        if raw.lower() == "q":
            return None
        m = _INPUT_RE.match(raw)
        if not m:
            print("格式不对：A/B/? + 可选 1-5，例如 `A 4` 或 `? 3`")
            continue
        choice = m.group(1).upper()
        if choice == "S":
            return {"skip": True}
        if choice == "Q":
            return None
        score = m.group(2)
        if quick or score is not None:
            return {"guess": choice, "score": int(score) if score else None, "ts": time.time()}
        s = input("接受度 1-5（愿意让这条 AI 回复发到群里吗）> ").strip()
        if s.isdigit() and 1 <= int(s) <= 5:
            return {"guess": choice, "score": int(s), "ts": time.time()}


def _summary(pack: list[dict], answers: dict) -> dict:
    rows = []
    for i, item in enumerate(pack, 1):
        ans = answers.get(str(i))
        if not ans or ans.get("skip"):
            continue
        guess = ans.get("guess")
        correct = guess in ("A", "B") and guess == item["ai_side"]
        rows.append({"i": i, "type": item["type"], "guess": guess,
                     "ai_side": item["ai_side"], "correct": correct,
                     "score": ans.get("score")})
    guessed = [r for r in rows if r["guess"] in ("A", "B")]
    correct = sum(1 for r in guessed if r["correct"])
    acc = round(100 * correct / len(guessed), 1) if guessed else 0.0
    scores = [r["score"] for r in rows if r.get("score")]
    by_type: dict[str, dict] = {}
    for r in rows:
        d = by_type.setdefault(r["type"], {"n": 0, "correct": 0, "guessed": 0, "scores": []})
        d["n"] += 1
        if r["guess"] in ("A", "B"):
            d["guessed"] += 1
            d["correct"] += 1 if r["correct"] else 0
        if r.get("score"):
            d["scores"].append(r["score"])
    for d in by_type.values():
        d["accuracy"] = round(100 * d["correct"] / d["guessed"], 1) if d["guessed"] else None
        d["avg_score"] = round(sum(d["scores"]) / len(d["scores"]), 2) if d["scores"] else None
    return {
        "answered": len(rows),
        "unidentified": sum(1 for r in rows if r["guess"] == "?"),
        "ai_detected": correct,
        "accuracy": acc,
        "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
        "by_type": by_type,
        "rows": rows,
    }


def _report(pack: list[dict], summary: dict) -> str:
    lines = [
        "# 盲测结果",
        "",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 作答：{summary['answered']} 组（看不出 {summary['unidentified']} 组）",
        f"- **AI 被识破率：{summary['accuracy']}%**（{summary['ai_detected']}/{summary['answered'] - summary['unidentified']}，50% 最好）",
        f"- 平均接受度：{summary['avg_score'] if summary['avg_score'] is not None else '—'}",
        "",
        "| 组 | 题型 | AI 实际 | 你的判断 | 对错 | 接受度 |",
        "|---|---|---|---|---|---|",
    ]
    for r in summary["rows"]:
        lines.append(f"| {r['i']} | {r['type']} | {r['ai_side']} | {r['guess']} | "
                     f"{'✅' if r['correct'] else ('—' if r['guess'] == '?' else '❌')} | {r.get('score') or '—'} |")
    lines += ["", "## 分题型", "", "| 题型 | 组数 | 识破率 | 平均接受度 |", "|---|---|---|---|"]
    for t, d in summary["by_type"].items():
        lines.append(f"| {t} | {d['n']} | {d['accuracy'] if d['accuracy'] is not None else '—'}% | {d['avg_score'] or '—'} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="language_lab blind", description="交互式盲测")
    parser.add_argument("--rounds", default="6,7", help="从哪些回合抽题，逗号分隔")
    parser.add_argument("--per-round", type=int, default=6)
    parser.add_argument("--cache", default="rules_v4_prompt", help="生成缓存目录名")
    parser.add_argument("--limit", type=int, default=None, help="最多测几组")
    parser.add_argument("--reset", action="store_true", help="清空已答记录")
    parser.add_argument("--quick", action="store_true", help="不评接受度，只猜 A/B/?")
    parser.add_argument("--seed", type=int, default=30)
    parser.add_argument("--min-context", type=int, default=10)
    parser.add_argument("--max-context", type=int, default=30)
    parser.add_argument("--gap", type=int, default=900, help="会话边界：超过多少秒算换话题")
    args = parser.parse_args(argv)

    DATA.mkdir(parents=True, exist_ok=True)
    if args.reset and ANS_PATH.exists():
        ANS_PATH.unlink()
    pack = _load_json(PACK_PATH, None)
    if not pack or args.reset:
        rounds = [int(x) for x in args.rounds.split(",") if x.strip()]
        pack = build_pack(rounds, args.per_round, args.cache, args.seed,
                          min_ctx=args.min_context, max_ctx=args.max_context, gap_s=args.gap)
        _save_json(PACK_PATH, pack)
    if args.limit:
        pack = pack[: args.limit]
    if not pack:
        print("没有可用题目：检查 rounds/cache 是否正确。")
        return 1

    answers = _load_json(ANS_PATH, {})
    print("=" * 64)
    print("群聊「像人」盲测")
    print("上下文长度由会话边界 + 话题连续性决定（10~30 条不等）。")
    print("图片/表情只保存占位符；能识别的会带一句描述，没有描述的媒体题会被跳过。")
    print(f"共 {len(pack)} 组，已答 {len(answers)} 组。输入 q 可随时保存退出。")
    print("=" * 64)
    interrupted = False
    for i, item in enumerate(pack, 1):
        if str(i) in answers:
            continue
        ans = _ask(item, i, len(pack), args.quick)
        if ans is None:
            interrupted = True
            break
        answers[str(i)] = ans
        _save_json(ANS_PATH, answers)

    summary = _summary(pack, answers)
    if summary["answered"] == 0:
        print("还没有作答。")
        return 1
    print("\n" + "=" * 64)
    print(f"已答 {summary['answered']} 组；AI 被识破率 {summary['accuracy']}%"
          f"（50% 最好，越低越难分辨）；平均接受度 {summary['avg_score']}")

    REP.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M")
    report = _report(pack, summary)
    md = REP / f"blind_result_{stamp}.md"
    md.write_text(report, encoding="utf-8")
    _save_json(REP / f"blind_result_{stamp}.json", summary)
    print(f"报告：{md}")
    print("（答案已在上表揭晓；未作答的组下次继续。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
