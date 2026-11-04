# -*- coding: utf-8 -*-
"""评测回合：抽题 → 生成 → 评分 → 报告（含人工盲评包）。

用法：
    python -m language_lab eval --n 36
    python -m language_lab eval --n 20 --types reply,mention
    python -m language_lab eval --cache-only
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from language_lab.eval import generator, scorer  # noqa: E402

TZ = timezone(timedelta(hours=8))
EVAL_DIR = Path(__file__).resolve().parent
SCEN_DIR = EVAL_DIR / "data" / "scenarios"
REPORT_DIR = EVAL_DIR / "reports"
DEFAULT_TYPES = ("reply", "mention", "media_context", "initiation", "question", "continue")


def load_scenarios(types: tuple[str, ...], split: str = "test") -> list[dict]:
    picked: list[dict] = []
    for path in sorted(SCEN_DIR.glob("*/*.jsonl")):
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    sc = json.loads(line)
                except ValueError:
                    continue
                if sc.get("split") != split:
                    continue
                if types and sc.get("type") not in types:
                    continue
                picked.append(sc)
    return picked


def scenario_map() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(SCEN_DIR.glob("*/*.jsonl")):
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    sc = json.loads(line)
                except ValueError:
                    continue
                out[sc.get("id")] = sc
    return out


def round_ids(round_no: int) -> list[str]:
    files = sorted(REPORT_DIR.glob(f"round{round_no}_*.json"))
    if not files:
        raise SystemExit(f"找不到 round{round_no} 的报告 JSON")
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    ids = [c.get("id") for c in (data.get("cases") or []) if c.get("id")]
    if not ids:
        raise SystemExit(f"round{round_no} 报告里没有题目 id")
    return ids


def sample(scenarios: list[dict], n: int, seed: int = 11) -> list[dict]:
    rng = random.Random(seed)
    by_type: dict[str, list[dict]] = {}
    for sc in scenarios:
        by_type.setdefault(sc.get("type"), []).append(sc)
    for items in by_type.values():
        rng.shuffle(items)
    types = list(by_type)
    per = max(1, n // max(1, len(types)))
    out: list[dict] = []
    for t in types:
        out.extend(by_type[t][:per])
    if len(out) < n:
        rest = [sc for t in types for sc in by_type[t][per:]]
        rng.shuffle(rest)
        out.extend(rest[: n - len(out)])
    rng.shuffle(out)
    return out[:n]


def next_round() -> int:
    nums = []
    for p in REPORT_DIR.glob("round*.json"):
        try:
            nums.append(int(p.stem.split("_")[0].replace("round", "")))
        except ValueError:
            continue
    return (max(nums) + 1) if nums else 1


def run(n: int = 36, types: tuple[str, ...] = DEFAULT_TYPES, split: str = "test",
        spec_path: str | None = None, force: bool = False, cache_only: bool = False,
        seed: int = 11, reuse: int | None = None) -> dict:
    spec_id, spec_text = generator.load_spec(spec_path)
    if reuse is not None:
        ids = round_ids(reuse)
        smap = scenario_map()
        chosen = [smap[i] for i in ids if i in smap]
        if not chosen:
            raise SystemExit(f"round{reuse} 的题目在题库里找不到了")
    else:
        scenarios = load_scenarios(types, split)
        if not scenarios:
            raise SystemExit(f"没有可用题目（split={split}, types={types}）")
        chosen = sample(scenarios, n, seed)
    results = []
    models = Counter()
    ok = 0
    for i, sc in enumerate(chosen, 1):
        if cache_only:
            gen = generator.generate(sc, spec_id, spec_text, force=False)
            if not gen.get("ok"):
                gen = {"scenario_id": sc["id"], "spec": spec_id, "ok": False,
                       "error": "无缓存", "bubbles": [], "model": None, "skip": False, "use_media": False}
        else:
            gen = generator.generate(sc, spec_id, spec_text, force=force)
        results.append({"scenario": sc, "gen": gen})
        if gen.get("ok"):
            ok += 1
            models[gen.get("model") or "?"] += 1
        if i % 5 == 0 or i == len(chosen):
            print(f"[eval] generated {i}/{len(chosen)} ok={ok}", flush=True)

    score_data = scorer.score(results)
    round_no = next_round()
    stamp = datetime.now(TZ)
    meta = {
        "round": round_no,
        "spec": spec_id,
        "generated_at": stamp.isoformat(timespec="seconds"),
        "types": list(types),
        "split": split,
        "models": dict(models),
        "n": len(chosen),
        "ok": ok,
        "reuse_round": reuse,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    base = REPORT_DIR / f"round{round_no}_{stamp.strftime('%Y%m%d_%H%M')}"
    (base.with_suffix(".md")).write_text(scorer.render_markdown(score_data, meta), encoding="utf-8")
    payload = {"meta": meta, "score": {k: v for k, v in score_data.items() if k != "cases"},
               "cases": score_data.get("cases")}
    (base.with_suffix(".json")).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    blind_md, blind_key = scorer.blind_pack(results, n=min(10, len(results)))
    (REPORT_DIR / f"blind_round{round_no}.md").write_text(blind_md, encoding="utf-8")
    (REPORT_DIR / f"blind_round{round_no}_key.json").write_text(
        json.dumps(blind_key, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[eval] round {round_no}: n={score_data['n']} fail={len(score_data['failures'])}")
    print(f"[eval] -> {base.with_suffix('.md')}")
    print(f"[eval] blind -> {REPORT_DIR / f'blind_round{round_no}.md'}")
    return {"meta": meta, "score": score_data}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="language_lab eval", description="评测回合")
    parser.add_argument("--n", type=int, default=36, help="抽多少题")
    parser.add_argument("--types", default=",".join(DEFAULT_TYPES), help="题型，逗号分隔")
    parser.add_argument("--split", default="test", choices=["test", "train"])
    parser.add_argument("--spec", default=None, help="规范 prompt 文件路径（默认 rules_v1_prompt.txt）")
    parser.add_argument("--force", action="store_true", help="忽略缓存重新生成")
    parser.add_argument("--cache-only", action="store_true", help="只用缓存，不调模型")
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--reuse", type=int, default=None,
                        help="复用某一轮评测的题目 id（用于规范对比，忽略 --n/--types）")
    args = parser.parse_args(argv)
    types = tuple(t.strip() for t in args.types.split(",") if t.strip())
    run(n=args.n, types=types, split=args.split, spec_path=args.spec,
        force=args.force, cache_only=args.cache_only, seed=args.seed, reuse=args.reuse)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
