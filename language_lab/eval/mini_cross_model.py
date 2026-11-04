# -*- coding: utf-8 -*-
"""最小跨模型测试：出题 / 回答 / 判题 用三个不同模型，全部走脚本的 llm_pool 配置。

流程：
1) 出题模型：看 4 个真实群聊片段（只给上下文），选出 2 个最适合测「像真人接话」的；
2) 回答模型：按当前规范 rules_v4 生成回复；
3) 判题模型：把「新 AI 回复」和「真人当时的回复」匿名混排，判断哪条是真人。

所有调用都是 llm_pool.chat_sync(preferred=...)，与机器人聊天共用：
E:\\QQbot\\config\\model_pool.json、.env 里的 LLM_POOL_BASE_URL / LLM_POOL_API_KEY。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

EVAL = Path(__file__).resolve().parent
REP = EVAL / "reports"
PACK = EVAL / "data" / "blind" / "pack_v5.json"
SPEC = BASE / "language_lab" / "rules" / "rules_v4_prompt.txt"

EXAM_MODEL = "qwen3.8-flash-next"
ANSWER_MODEL = "deepseek-flash"
JUDGE_MODEL = "deepseek-flash-2"


def _extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def _ask_exam(candidates: list[dict], model: str) -> dict:
    from scripts import llm_pool
    lines = []
    for i, item in enumerate(candidates, 1):
        ctx = "\n".join(f"  {c.get('nick')}：{c.get('text')}" for c in (item.get("context") or [])[-3:])
        lines.append(f"片段 {i}（{item['type']}）：\n{ctx}")
    prompt = (
        "你是评测出题人。下面是 4 个真实的群聊片段（只给上下文，不含答案）。\n"
        "请选出 2 个最适合测试「像本群真人一样接话」的片段，并说明一句理由。\n\n"
        + "\n\n".join(lines)
        + '\n\n只输出 JSON：{"picks":[1,3],"reason":"..."}'
    )
    last_error = ""
    for attempt in range(3):
        try:
            text, used = llm_pool.chat_sync(
                [{"role": "system", "content": "你是严格的语料评测出题人，只输出 JSON。"},
                 {"role": "user", "content": prompt}],
                label="lab_exam", max_tokens=400, temperature=0.4, timeout=60, preferred=model)
            if used != model:
                last_error = f"fallback:{used}"
                continue
            data = _extract_json(text) or {}
            picks = [int(x) for x in (data.get("picks") or []) if str(x).isdigit() and 1 <= int(x) <= len(candidates)]
            picks = list(dict.fromkeys(picks))[:2] or [1, 2]
            return {"ok": True, "model": used, "picks": picks,
                    "reason": str(data.get("reason") or "")[:120], "raw": text, "attempts": attempt + 1}
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
    return {"ok": False, "model": None, "picks": [1, 2], "reason": "",
            "error": last_error, "attempts": 3}


def _ask_judge(item: dict, model: str) -> dict:
    from language_lab.eval import blind_auto
    last = None
    for _ in range(3):
        last = blind_auto._ask(item, "ab", force=True, preferred=model)
        if last.get("model") == model:
            return last
    return last or {"ok": False, "model": None, "human": "?", "prefer": "?", "reason": ""}


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="language_lab mini-cross", description="最小跨模型测试")
    parser.add_argument("--exam-model", default=EXAM_MODEL)
    parser.add_argument("--answer-model", default=ANSWER_MODEL)
    parser.add_argument("--judge-model", default=JUDGE_MODEL)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)

    from language_lab.lab import settings
    from language_lab.eval import generator

    pack = json.loads(PACK.read_text(encoding="utf-8"))
    usable = [x for x in pack if x["type"] in ("question", "initiation", "continue", "reply")
              and "（AI" not in x["A"] and "（AI" not in x["B"]]
    random.Random(args.seed).shuffle(usable)
    candidates = usable[:4]
    print(f"[exam] {len(candidates)} candidates, model={args.exam_model}")
    exam = _ask_exam(candidates, args.exam_model)
    print(f"[exam] used={exam.get('model')} picks={exam['picks']} reason={exam.get('reason')}")

    spec_id, spec_text = generator.load_spec(SPEC)
    rows = []
    for n, pick in enumerate(exam["picks"], 1):
        item = candidates[pick - 1]
        sc = None
        from language_lab.eval import blind_cli
        # pack 里没有 scenario 对象；用 scenario id 从题库找
        for p in (EVAL / "data" / "scenarios").glob("*/*.jsonl"):
            for line in p.open(encoding="utf-8"):
                try:
                    cand = json.loads(line)
                except ValueError:
                    continue
                if cand.get("id") == item["id"]:
                    sc = cand
                    break
            if sc:
                break
        if sc is None:
            print(f"[skip] scenario not found {item['id']}")
            continue
        gen = None
        for _ in range(3):
            gen = generator.generate(sc, spec_id, spec_text, force=True, preferred=args.answer_model)
            if gen.get("model") == args.answer_model:
                break
        human = item["B"] if item["ai_side"] == "A" else item["A"]
        ai_text = "\n".join(gen.get("bubbles") or []) or "（AI 选择发表情/图片）"
        judge_item = {"id": f"mini-{n}", "type": item["type"], "context": item["context"][-4:],
                      "A": ai_text, "B": human, "ai_side": "A"}
        # 随机交换，避免位置偏见
        if random.Random(args.seed + n).random() < 0.5:
            judge_item["A"], judge_item["B"] = judge_item["B"], judge_item["A"]
            judge_item["ai_side"] = "B"
        judge = _ask_judge(judge_item, args.judge_model)
        human_side = judge.get("human", "?")
        correct = human_side != judge_item["ai_side"] and human_side in ("A", "B")
        rows.append({"n": n, "type": item["type"], "context": item["context"][-4:],
                     "scenario": sc["id"], "gen": gen, "ai_text": ai_text, "human_text": human,
                     "judge": judge, "judge_item": judge_item, "correct": correct})
        print(f"[round {n}] answer_model={gen.get('model')} judge_model={judge.get('model')} "
              f"judge_human={human_side} correct={correct}")

    lines = [
        "# 最小跨模型测试",
        "",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 配置：`scripts/llm_pool.py` + `config/model_pool.json`（与机器人聊天同一条出口）",
        f"- 出题模型：{args.exam_model}（实际 {exam.get('model')}，{exam.get('attempts')} 次）",
        f"- 回答模型：{args.answer_model}",
        f"- 判题模型：{args.judge_model}",
        "",
        "## 出题",
        "",
        f"- 4 选 2：{exam['picks']}",
        f"- 理由：{exam.get('reason') or '—'}",
        "",
        "## 结果",
        "",
        "| # | 题型 | 回答模型（请求/实际） | 判题模型（请求/实际） | 判官认为真人 | 判对 |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['n']} | {r['type']} | {args.answer_model} / {r['gen'].get('model')} | "
                     f"{args.judge_model} / {r['judge'].get('model')} | "
                     f"{r['judge'].get('human')} | {'✅' if r['correct'] else '❌'} |")
    for r in rows:
        lines += ["", f"### 案例 {r['n']}（{r['type']}）", "", "上下文："]
        lines += [f"- {c['nick']}：{c['text']}" for c in r["context"]]
        lines += ["", f"- AI 回复：{r['ai_text']}", f"- 真人回复：{r['human_text']}",
                  f"- 判官理由：{r['judge'].get('reason') or '—'}"]
    acc = round(100 * sum(1 for r in rows if r["correct"]) / len(rows), 1) if rows else 0
    lines += ["", f"**判题正确率：{acc}%**（{sum(1 for r in rows if r['correct'])}/{len(rows)}）"]
    text = "\n".join(lines) + "\n"
    REP.mkdir(parents=True, exist_ok=True)
    path = REP / f"mini_cross_model_{time.strftime('%Y%m%d_%H%M')}.md"
    path.write_text(text, encoding="utf-8")
    (path.with_suffix(".json")).write_text(json.dumps(
        {"exam": exam, "rows": [{k: v for k, v in r.items() if k != "judge_item"} for r in rows],
         "accuracy": acc}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(text)
    print(f"-> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
