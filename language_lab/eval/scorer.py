# -*- coding: utf-8 -*-
"""评分器：比较「生成回复」与「真人回复」的形态、行为与分布。

只做可复现的客观指标；「接不接得住」留给人工盲评（见 blind 包）。
"""

from __future__ import annotations

import json
import math
import random
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from language_lab.lab import stats  # noqa: E402

PUNCT = "。！？!?…~～"
LEN_BUCKETS = ("1-3", "4-6", "7-10", "11-20", "21+")
BUBBLE_BUCKETS = ("1", "2", "3+")

def _plain(t: str | None) -> str:
    return stats.plain(t)


def _len_bucket(n: int) -> str:
    if n <= 3:
        return "1-3"
    if n <= 6:
        return "4-6"
    if n <= 10:
        return "7-10"
    if n <= 20:
        return "11-20"
    return "21+"


def _bubble_bucket(n: int) -> str:
    return "1" if n <= 1 else ("2" if n == 2 else "3+")


def _no_punct(text: str) -> bool:
    t = _plain(text)
    return bool(t) and t[-1] not in PUNCT


def _has_emoji(text: str) -> bool:
    return bool(stats._EMOJI_RE.search(text or ""))


def _is_question(text: str) -> bool:
    t = _plain(text)
    return "？" in t or "?" in t or t.endswith(("吗", "呢"))


def _js(p: Counter, q: Counter, keys) -> float:
    total_p = sum(p.get(k, 0) for k in keys) or 1
    total_q = sum(q.get(k, 0) for k in keys) or 1
    js = 0.0
    for k in keys:
        pi = p.get(k, 0) / total_p
        qi = q.get(k, 0) / total_q
        mi = (pi + qi) / 2
        if pi > 0:
            js += 0.5 * pi * math.log2(pi / mi)
        if qi > 0:
            js += 0.5 * qi * math.log2(qi / mi)
    return round(js, 4)


def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return 0.0
    return round(vals[len(vals) // 2], 1)


def case_features(sc: dict, gen: dict) -> dict | None:
    bubbles = gen.get("bubbles") or []
    if not bubbles and not gen.get("skip") and not gen.get("use_media"):
        return None
    ref = sc.get("reference") or {}
    ref_bubbles = ref.get("bubbles") or []
    ref_text = "\n".join((b.get("text") or "") for b in ref_bubbles)
    ai_text = "\n".join(bubbles)
    ref_n = int(ref.get("n_bubbles") or len(ref_bubbles) or 1)
    ai_n = len(bubbles) if bubbles else (1 if gen.get("use_media") else 0)
    return {
        "type": sc.get("type"),
        "ai_len": sum(len(_plain(b)) for b in bubbles),
        "ref_len": sum(len(_plain(b.get("text"))) for b in ref_bubbles),
        "ai_n": ai_n,
        "ref_n": ref_n,
        "ai_no_punct": _no_punct(bubbles[-1]) if bubbles else False,
        "ref_no_punct": _no_punct(ref_bubbles[-1].get("text")) if ref_bubbles else False,
        "ai_emoji": any(_has_emoji(b) for b in bubbles),
        "ref_emoji": any(_has_emoji(b.get("text")) for b in ref_bubbles),
        "ai_q": any(_is_question(b) for b in bubbles),
        "ref_q": any(_is_question(b.get("text")) for b in ref_bubbles),
        "ai_media": bool(gen.get("use_media")),
        "ref_media": bool((sc.get("meta") or {}).get("has_media")) or any(
            b.get("media") and not _plain(b.get("text")) for b in ref_bubbles),
        "ai_len_bucket": _len_bucket(sum(len(_plain(b)) for b in bubbles)),
        "ref_len_bucket": _len_bucket(sum(len(_plain(b.get("text"))) for b in ref_bubbles)),
        "ai_bubble_bucket": _bubble_bucket(ai_n),
        "ref_bubble_bucket": _bubble_bucket(ref_n),
    }


def _summary(rows: list[dict], prefix: str) -> dict:
    lens = sorted(r[f"{prefix}_len"] for r in rows)
    bubbles = [r[f"{prefix}_n"] for r in rows]
    p90 = lens[min(len(lens) - 1, int(round(0.9 * (len(lens) - 1))))] if lens else 0
    return {
        "median_len": _median(lens),
        "mean_len": round(sum(lens) / len(lens), 1) if lens else 0.0,
        "p90_len": p90,
        "avg_bubbles": round(sum(bubbles) / len(bubbles), 2) if bubbles else 0.0,
        "no_punct": round(100 * sum(1 for r in rows if r[f"{prefix}_no_punct"]) / len(rows), 1) if rows else 0.0,
        "emoji": round(100 * sum(1 for r in rows if r[f"{prefix}_emoji"]) / len(rows), 1) if rows else 0.0,
        "media": round(100 * sum(1 for r in rows if r[f"{prefix}_media"]) / len(rows), 1) if rows else 0.0,
        "question": round(100 * sum(1 for r in rows if r[f"{prefix}_q"]) / len(rows), 1) if rows else 0.0,
    }


def score(results: list[dict]) -> dict:
    cases = []
    failures = []
    for item in results:
        sc, gen = item["scenario"], item["gen"]
        if not gen.get("ok"):
            failures.append({"id": sc.get("id"), "type": sc.get("type"), "error": gen.get("error")})
            continue
        feat = case_features(sc, gen)
        if feat:
            feat["id"] = sc.get("id")
            cases.append(feat)
    if not cases:
        return {"n": 0, "failures": failures, "cases": []}

    ai = _summary(cases, "ai")
    human = _summary(cases, "ref")
    js = {
        "length": _js(Counter(r["ai_len_bucket"] for r in cases),
                      Counter(r["ref_len_bucket"] for r in cases), LEN_BUCKETS),
        "bubbles": _js(Counter(r["ai_bubble_bucket"] for r in cases),
                       Counter(r["ref_bubble_bucket"] for r in cases), BUBBLE_BUCKETS),
        "emoji": _js(Counter("yes" if r["ai_emoji"] else "no" for r in cases),
                     Counter("yes" if r["ref_emoji"] else "no" for r in cases), ("yes", "no")),
        "media": _js(Counter("yes" if r["ai_media"] else "no" for r in cases),
                     Counter("yes" if r["ref_media"] else "no" for r in cases), ("yes", "no")),
    }
    behavior = {
        "media_match": round(100 * sum(1 for r in cases if r["ai_media"] == r["ref_media"]) / len(cases), 1),
        "bubble_match": round(100 * sum(1 for r in cases if r["ai_bubble_bucket"] == r["ref_bubble_bucket"]) / len(cases), 1),
        "length_bucket_match": round(100 * sum(1 for r in cases if r["ai_len_bucket"] == r["ref_len_bucket"]) / len(cases), 1),
    }
    per_type = {}
    for t in sorted({r["type"] for r in cases}):
        sub = [r for r in cases if r["type"] == t]
        per_type[t] = {"n": len(sub), "ai": _summary(sub, "ai"), "human": _summary(sub, "ref")}
    return {"n": len(cases), "failures": failures, "ai": ai, "human": human,
            "js": js, "behavior": behavior, "per_type": per_type, "cases": cases}


def render_markdown(score_data: dict, meta: dict) -> str:
    a, h = score_data["ai"], score_data["human"]
    lines = [
        f"# 评测回合 {meta.get('round')} · 规范 {meta.get('spec')}",
        "",
        f"- 时间：{meta.get('generated_at')}",
        f"- 题目：{score_data['n']} 条（失败 {len(score_data['failures'])}）",
        f"- 题源：{meta.get('types')}；模型：{meta.get('models')}",
        "",
        "## 总体：AI vs 真人",
        "",
        "| 指标 | AI | 真人 |",
        "|---|---|---|",
        f"| 中位长度 | {a['median_len']} | {h['median_len']} |",
        f"| P90 长度 | {a['p90_len']} | {h['p90_len']} |",
        f"| 平均长度 | {a['mean_len']} | {h['mean_len']} |",
        f"| 气泡数/组 | {a['avg_bubbles']} | {h['avg_bubbles']} |",
        f"| 句尾不加标点 | {a['no_punct']}% | {h['no_punct']}% |",
        f"| emoji | {a['emoji']}% | {h['emoji']}% |",
        f"| 媒体 | {a['media']}% | {h['media']}% |",
        f"| 问句 | {a['question']}% | {h['question']}% |",
        "",
        "## 分布距离（JS，越小越像）",
        "",
        "| 维度 | JS |",
        "|---|---|",
    ]
    for k, v in score_data["js"].items():
        lines.append(f"| {k} | {v} |")
    lines += ["", "## 行为一致率", "", "| 维度 | 一致率 |", "|---|---|"]
    for k, v in score_data["behavior"].items():
        lines.append(f"| {k} | {v}% |")
    if score_data["per_type"]:
        lines += ["", "## 分题型", "", "| 题型 | n | AI 中位长 | 真人中位长 | AI 媒体 | 真人媒体 |", "|---|---|---|---|---|---|"]
        for t, d in score_data["per_type"].items():
            lines.append(f"| {t} | {d['n']} | {d['ai']['median_len']} | {d['human']['median_len']} | "
                         f"{d['ai']['media']}% | {d['human']['media']}% |")
    if score_data["failures"]:
        lines += ["", "## 失败样本", ""]
        for f in score_data["failures"][:10]:
            lines.append(f"- {f['id']} ({f['type']}): {f['error']}")
    lines += ["", "## 下一步建议（自动生成，供改规范参考）", ""]
    if a["median_len"] > h["median_len"] + 2:
        lines.append(f"- AI 偏长：中位 {a['median_len']} vs 真人 {h['median_len']}，优先压长度")
    if a["no_punct"] < h["no_punct"] - 10:
        lines.append("- AI 句尾标点偏多：规范里加强「句尾不加标点」")
    if abs(a["media"] - h["media"]) > 10:
        lines.append(f"- 媒体选择偏差：AI {a['media']}% vs 真人 {h['media']}%")
    if abs(a["emoji"] - h["emoji"]) > 5:
        lines.append(f"- emoji 偏差：AI {a['emoji']}% vs 真人 {h['emoji']}%")
    if not lines[-1].startswith("-"):
        lines.append("- 暂无明显形态偏差，重点看人工盲评的「接不接得住」")
    return "\n".join(lines) + "\n"


def blind_pack(results: list[dict], n: int = 10, seed: int = 7) -> tuple[str, dict]:
    rng = random.Random(seed)
    picked = [x for x in results if x["gen"].get("ok") and x["gen"].get("bubbles")]
    rng.shuffle(picked)
    picked = picked[:n]
    lines = ["# 人工盲评包", "", "下面每组 A/B 中，一条是 AI 写的、一条是真人当时发的。", "看完请回「序号 + A 或 B」即可。", ""]
    key = {}
    for i, item in enumerate(picked, 1):
        sc, gen = item["scenario"], item["gen"]
        human_text = "\n".join((b.get("text") or "") for b in (sc.get("reference") or {}).get("bubbles") or [])
        ai_text = "\n".join(gen["bubbles"])
        order = rng.choice(["ai_a", "ai_b"])
        a, b = (ai_text, human_text) if order == "ai_a" else (human_text, ai_text)
        key[str(i)] = {"ai": "A" if order == "ai_a" else "B", "type": sc.get("type"),
                       "scenario_id": sc.get("id")}
        lines += [f"### {i}", f"A：{a}", f"B：{b}", ""]
    return "\n".join(lines) + "\n", key
