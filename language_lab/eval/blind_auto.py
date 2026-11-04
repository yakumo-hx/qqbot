# -*- coding: utf-8 -*-
"""自动盲测：让一个「老群友」LLM 角色代替人做 A/B 盲测。

每个题目：
- 给上下文 + A/B，问「哪条是真人发的」+「你更愿意在群里看到哪条」；
- 默认再做一次 A/B 交换，检查位置偏见；两次不一致记「不确定」；
- 结果缓存，失败重试；最后与真人（你的 answers_v5.json）对照。

用法：
    python -m language_lab blind-auto
    python -m language_lab blind-auto --limit 6 --no-swap
    python -m language_lab blind-auto --pack language_lab\\eval\\data\\blind\\pack_v5.json
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

EVAL = Path(__file__).resolve().parent
REP = EVAL / "reports"
DATA = EVAL / "data" / "blind_auto"
CACHE = DATA / "cache"
PACK_DEFAULT = EVAL / "data" / "blind" / "pack_v5.json"
ANS_DEFAULT = EVAL / "data" / "blind" / "answers_v5.json"

PERSONA = (
    "你是这个 QQ 群的一名老群友，待了很久，平时就爱看大家聊天。"
    "你说话直，凭直觉判断，不会因为哪条更长、更礼貌就觉得它更好。"
)

PROMPT = """下面是一段群聊上下文，以及两条候选回复 A / B。
其中**只有一条**是本群群友真实发的，另一条是 AI 写的。

【上下文】
{context}

A：{a}
B：{b}

请回答：
1) 哪条更像真人发的（A / B / ?）；
2) 如果只能留一条在群里，你更愿意看到哪条（A / B）；
3) 用一句短话说明原因（不超过 30 字）。

只输出 JSON：
{{"human":"A|B|?","prefer":"A|B","reason":"..."}}"""


def _pack(path: Path) -> list[dict]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    from language_lab.eval import blind_cli
    pack = blind_cli.build_pack([6, 7], 6, "rules_v4_prompt", 30)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
    return pack


def _ctx(item: dict) -> str:
    return "\n".join(f"- {c.get('nick')}：{c.get('text')}" for c in item.get("context") or [])


def _extract(text: str) -> dict | None:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    human = str(data.get("human") or "?").strip().upper()[:1]
    prefer = str(data.get("prefer") or "").strip().upper()[:1]
    if human not in ("A", "B"):
        human = "?"
    if prefer not in ("A", "B"):
        prefer = "?"
    return {"human": human, "prefer": prefer, "reason": str(data.get("reason") or "")[:60]}


def _ask(item: dict, order: str, force: bool = False, preferred: str | None = None) -> dict:
    """order: 'ab' 原序，'ba' 交换。返回已映射回原始 A/B 的结果。"""
    cache_dir = CACHE / (preferred or "pool")
    cache_path = cache_dir / f"{item['id']}_{order}.json"
    if cache_path.exists() and not force:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("ok"):
                return cached          # 失败缓存不复用：下次自动重试
        except ValueError:
            pass
    a, b = (item["A"], item["B"]) if order == "ab" else (item["B"], item["A"])
    prompt = PROMPT.format(context=_ctx(item), a=a, b=b)
    messages = [{"role": "system", "content": PERSONA}, {"role": "user", "content": prompt}]
    result = {"id": item["id"], "order": order, "ok": False, "error": None,
              "model": None, "human": "?", "prefer": "?", "reason": "", "raw": None}
    try:
        from scripts import llm_pool
    except ImportError as exc:
        result["error"] = f"llm_pool: {exc}"
        return result
    for attempt in range(5):
        try:
            text, model = llm_pool.chat_sync(messages, label="lab_judge", max_tokens=260,
                                             temperature=0.3, timeout=60, preferred=preferred)
            parsed = _extract(text)
            if parsed:
                # 映射回原始顺序
                def _map(choice: str) -> str:
                    if choice == "A":
                        return "A" if order == "ab" else "B"
                    if choice == "B":
                        return "B" if order == "ab" else "A"
                    return "?"
                result.update({"ok": True, "human": _map(parsed["human"]),
                               "prefer": _map(parsed["prefer"]),
                               "reason": parsed["reason"], "model": model, "raw": text})
                break
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            time.sleep(2 + attempt * 3)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _load_human(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _summary(pack: list[dict], calls: dict, human_ans: dict) -> dict:
    rows = []
    for idx, item in enumerate(pack, 1):
        sid = item["id"]
        ab = calls.get(sid, {}).get("ab")
        ba = calls.get(sid, {}).get("ba")
        if not ab or not ab.get("ok"):
            continue
        human_pred = ab["human"]  # 判官认为的真人侧
        ai_pred = "B" if human_pred == "A" else ("A" if human_pred == "B" else "?")
        correct = ai_pred == item["ai_side"]
        consistent = None
        if ba and ba.get("ok"):
            consistent = (ba["human"] == human_pred)
        h = human_ans.get(str(idx)) or {}
        rows.append({
            "id": sid, "type": item["type"], "ai_side": item["ai_side"],
            "judge_human": human_pred, "judge_ai": ai_pred, "correct": correct,
            "prefer_human": ab["prefer"] == human_pred if ab["prefer"] != "?" else None,
            "consistent": consistent, "reason": ab.get("reason") or "",
            "human_guess": (h or {}).get("guess"), "human_score": (h or {}).get("score"),
        })
    answered = [r for r in rows if r["judge_ai"] != "?"]
    correct = sum(1 for r in answered if r["correct"])
    acc = round(100 * correct / len(answered), 1) if answered else 0.0
    by_type = defaultdict(lambda: {"n": 0, "correct": 0, "guessed": 0})
    for r in rows:
        d = by_type[r["type"]]
        d["n"] += 1
        if r["judge_ai"] != "?":
            d["guessed"] += 1
            d["correct"] += 1 if r["correct"] else 0
    for d in by_type.values():
        d["accuracy"] = round(100 * d["correct"] / d["guessed"], 1) if d["guessed"] else None
    return {
        "n": len(rows), "answered": len(answered), "accuracy": acc,
        "uncertain": sum(1 for r in rows if r["judge_ai"] == "?"),
        "position_consistent": sum(1 for r in rows if r["consistent"] is True),
        "position_checked": sum(1 for r in rows if r["consistent"] is not None),
        "prefer_human": sum(1 for r in rows if r["prefer_human"] is True),
        "prefer_checked": sum(1 for r in rows if r["prefer_human"] is not None),
        "by_type": dict(by_type), "rows": rows,
    }


def _report(summary: dict, human_ans: dict) -> str:
    lines = [
        "# 自动盲测结果（LLM 老群友）",
        "",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 题目：{summary['n']} 组；不确定 {summary['uncertain']} 组",
        f"- **AI 被识破率：{summary['accuracy']}%**（50% 最好）",
        f"- 位置一致性：{summary['position_consistent']}/{summary['position_checked']}",
        f"- 更愿意看真人那条：{summary['prefer_human']}/{summary['prefer_checked']}",
        "",
        "| 组 | 题型 | AI 实际 | 判官认为真人 | 判官认为 AI | 对错 | 交换后一致 | 真人猜 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(summary["rows"], 1):
        ok = "✅" if r["correct"] else ("—" if r["judge_ai"] == "?" else "❌")
        cons = "—" if r["consistent"] is None else ("✅" if r["consistent"] else "❌")
        lines.append(f"| {i} | {r['type']} | {r['ai_side']} | {r['judge_human']} | {r['judge_ai']} | "
                     f"{ok} | {cons} | {r.get('human_guess') or '—'} |")
    lines += ["", "## 分题型", "", "| 题型 | 组数 | 识破率 |", "|---|---|---|"]
    for t, d in summary["by_type"].items():
        lines.append(f"| {t} | {d['n']} | {d['accuracy'] if d['accuracy'] is not None else '—'}% |")
    if human_ans:
        human_guessed = [r for r in summary["rows"] if r.get("human_guess") in ("A", "B")]
        if human_guessed:
            agree = sum(1 for r in human_guessed if r["human_guess"] == r["judge_ai"])
            lines += ["", "## 与真人盲测对照", "",
                      f"- 你的作答：{len(human_guessed)} 组；判官与你的判断一致：{agree}/{len(human_guessed)}"]
    lines += ["", "## 判官理由（前 8 条）", ""]
    for r in summary["rows"][:8]:
        lines.append(f"- [{r['type']}] 判官认为真人={r['judge_human']}：{r['reason']}")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="language_lab blind-auto", description="LLM 自动盲测")
    parser.add_argument("--pack", default=str(PACK_DEFAULT))
    parser.add_argument("--answers", default=str(ANS_DEFAULT))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-swap", action="store_true", help="不做 A/B 交换")
    parser.add_argument("--force", action="store_true", help="忽略缓存重新问")
    parser.add_argument("--preferred", default=None,
                        help="指定判官模型（默认走模型池；建议用与生成器不同的模型，避免自偏好）")
    args = parser.parse_args(argv)

    pack = _pack(Path(args.pack))
    if args.limit:
        pack = pack[: args.limit]
    calls = defaultdict(dict)
    ok = 0
    for i, item in enumerate(pack, 1):
        ab = _ask(item, "ab", force=args.force, preferred=args.preferred)
        calls[item["id"]]["ab"] = ab
        if ab.get("ok"):
            ok += 1
        if not args.no_swap:
            calls[item["id"]]["ba"] = _ask(item, "ba", force=args.force, preferred=args.preferred)
        print(f"[judge] {i}/{len(pack)} ok={ok} type={item['type']}", flush=True)

    summary = _summary(pack, calls, _load_human(Path(args.answers)))
    REP.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M")
    md = REP / f"blind_auto_{stamp}.md"
    md.write_text(_report(summary, _load_human(Path(args.answers))), encoding="utf-8")
    (REP / f"blind_auto_{stamp}.json").write_text(
        json.dumps({k: v for k, v in summary.items() if k != "rows"}, ensure_ascii=False, indent=2)
        + "\n", encoding="utf-8")
    print(f"识破率 {summary['accuracy']}%  位置一致 {summary['position_consistent']}/{summary['position_checked']}")
    print(f"-> {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
