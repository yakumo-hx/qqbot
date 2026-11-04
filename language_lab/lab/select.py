# -*- coding: utf-8 -*-
"""成员筛选：从语料里挑出「话多 + 有内容」的人，产出 Top N 候选与样例。

纪律：
- 只统计窗口内、非 bot、有 uid 的消息；
- 身份优先用 LLBot/recorder 的 uid；名册反查的旧数据单独标记可信度；
- 报告只给聚合指标与极短例句。
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List

from .ingest import roster
from .ingest.build import norm_text
from .stats import _EMOJI_RE, _URL_RE, load_corpus, plain

_ID_FIELDS = ("min_messages", "min_active_days", "min_substantive_ratio",
              "median_len_min", "median_len_max", "max_duplicate_ratio",
              "max_link_ratio", "substantive_min_chars", "top_n")


def _z(values: List[float]) -> List[float]:
    if not values:
        return []
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    std = var ** 0.5
    if std == 0:
        return [0.0 for _ in values]
    return [(v - mean) / std for v in values]


def _bigrams(text: str) -> List[str]:
    t = plain(text)
    return [t[i:i + 2] for i in range(len(t) - 1)]


def _user_metrics(uid: str, recs: List[dict], name_map: Dict[str, str],
                  min_chars: int) -> dict:
    texts = [plain(r.get("text")) for r in recs]
    lengths = [len(t) for t in texts if t]
    days = {r.get("day") for r in recs if r.get("day")}
    substantive = sum(1 for t in texts if len(t) >= min_chars)
    dup_base = [norm_text(t) for t in texts if len(norm_text(t)) >= 2]
    dup = len(dup_base) - len(set(dup_base))
    links = sum(1 for t in texts if _URL_RE.search(t))
    media = sum(1 for r in recs if r.get("media"))
    questions = sum(1 for t in texts if "？" in t or "?" in t or t.endswith(("吗", "呢")))
    emoji = sum(1 for t in texts if _EMOJI_RE.search(t))
    bigrams = [b for t in texts for b in _bigrams(t)]
    vocab = (len(set(bigrams)) / len(bigrams)) if bigrams else 0.0
    nick = Counter(r.get("nick") for r in recs if r.get("nick")).most_common(1)
    roster_share = sum(1 for r in recs if r.get("uid_src") == "roster") / len(recs)
    return {
        "uid": uid,
        "nick": nick[0][0] if nick else "",
        "n": len(recs),
        "days": len(days),
        "median_len": float(statistics.median(lengths)) if lengths else 0.0,
        "mean_len": round(sum(lengths) / len(lengths), 1) if lengths else 0.0,
        "substantive_ratio": substantive / len(recs),
        "duplicate_ratio": dup / len(dup_base) if dup_base else 0.0,
        "link_ratio": links / len(recs),
        "media_ratio": media / len(recs),
        "question_ratio": questions / len(recs),
        "emoji_ratio": emoji / len(recs),
        "vocab_richness": vocab,
        "roster_share": roster_share,
    }


def _samples(recs: List[dict], limit: int = 3) -> List[str]:
    out: List[str] = []
    seen = set()
    for r in sorted(recs, key=lambda x: -len(plain(x.get("text")))):
        t = plain(r.get("text"))
        if not (4 <= len(t) <= 28) or _URL_RE.search(t) or r.get("media"):
            continue
        key = t[:12]
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def select(cfg: dict, records: List[dict], name_map: Dict[str, str]) -> dict:
    sel = cfg.get("selection") or {}
    p = {k: sel.get(k) for k in _ID_FIELDS}
    start, end = sel.get("window_start"), sel.get("window_end")
    min_chars = int(p["substantive_min_chars"] or 6)
    rows = [r for r in records
            if r.get("uid") and not r.get("is_bot")
            and (not start or (r.get("day") or "") >= start)
            and (not end or (r.get("day") or "") <= end)]
    by_uid: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        by_uid[str(r["uid"])].append(r)

    metrics = [_user_metrics(uid, recs, name_map, min_chars) for uid, recs in by_uid.items()]
    # 被回复次数
    replied: Counter = Counter()
    for r in rows:
        reply = r.get("reply_to") or {}
        target = reply.get("uid") or name_map.get((reply.get("nick") or "").strip())
        if target:
            replied[str(target)] += 1
    for m in metrics:
        m["reply_received"] = replied.get(m["uid"], 0)
        # 得分：只对样本量 >=20 的人做 z-score，避免小样本极端值
        m["_samples"] = _samples(by_uid[m["uid"]])

    base = [m for m in metrics if m["n"] >= 20]
    zs = {k: _z([float(m[k]) for m in base]) for k in
          ("n", "days", "substantive_ratio", "vocab_richness", "reply_received")}
    zmap = {}
    for i, m in enumerate(base):
        zmap[m["uid"]] = {k: zs[k][i] for k in zs}
    for m in metrics:
        z = zmap.get(m["uid"], {k: 0.0 for k in zs})
        m["score"] = round(
            0.35 * z.get("n", 0) + 0.15 * z.get("days", 0)
            + 0.20 * z.get("substantive_ratio", 0)
            + 0.15 * z.get("vocab_richness", 0)
            + 0.15 * z.get("reply_received", 0)
            - 0.30 * m["duplicate_ratio"], 3)

    def gate(m: dict) -> tuple[bool, str]:
        if m["n"] < int(p["min_messages"] or 100):
            return False, "条数不足"
        if m["days"] < int(p["min_active_days"] or 5):
            return False, "活跃天数不足"
        if m["substantive_ratio"] < float(p["min_substantive_ratio"] or 0.4):
            return False, "实质消息占比低"
        if not (float(p["median_len_min"] or 3) <= m["median_len"] <= float(p["median_len_max"] or 60)):
            return False, "长度分布异常"
        if m["duplicate_ratio"] > float(p["max_duplicate_ratio"] or 0.35):
            return False, "重复率高"
        if m["link_ratio"] > float(p["max_link_ratio"] or 0.02):
            return False, "链接/广告多"
        return True, ""

    for m in metrics:
        ok, why = gate(m)
        m["eligible"] = ok
        m["reject"] = why
        m["_samples"] = m.get("_samples") or []

    passed = sorted([m for m in metrics if m["eligible"]], key=lambda x: -x["score"])
    top = passed[: int(p["top_n"] or 6)]
    return {
        "window": {"start": start, "end": end},
        "gates": {k: p[k] for k in _ID_FIELDS},
        "total_users": len(metrics),
        "eligible": len(passed),
        "top": top,
        "runners_up": passed[len(top):len(top) + 6],
        "rejected": [m for m in metrics if not m["eligible"]][:10],
    }


def render_markdown(result: dict, gid: str) -> str:
    lines = [
        f"# 成员候选报告 · 群 {gid}",
        "",
        f"- 统计窗口：{result['window']['start']} ~ {result['window']['end']}",
        f"- 通过硬门槛：**{result['eligible']}** / {result['total_users']} 人",
        "",
        "## Top 6（建议做个人风格卡）",
        "",
        "| # | uid | 昵称 | 条数 | 活跃天 | 中位长 | 实质率 | 重复率 | 提问率 | 被回复 | 身份可信 | 得分 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, m in enumerate(result["top"], 1):
        lines.append(
            f"| {i} | {m['uid']} | {m['nick']} | {m['n']} | {m['days']} | {m['median_len']:.0f} | "
            f"{m['substantive_ratio'] * 100:.0f}% | {m['duplicate_ratio'] * 100:.0f}% | "
            f"{m['question_ratio'] * 100:.0f}% | {m['reply_received']} | "
            f"{100 - m['roster_share'] * 100:.0f}% | {m['score']} |"
        )
    lines += ["", "## 代表句（节选）", ""]
    for m in result["top"]:
        lines.append(f"### {m['nick']}（{m['uid']}）")
        for t in m["_samples"]:
            lines.append(f"- {t}")
        lines.append("")
    if result["runners_up"]:
        lines += ["## 备选（未进 Top 6）", "",
                  "| uid | 昵称 | 条数 | 得分 |", "|---|---|---|---|"]
        for m in result["runners_up"]:
            lines.append(f"| {m['uid']} | {m['nick']} | {m['n']} | {m['score']} |")
        lines.append("")
    if result["rejected"]:
        lines += ["## 未通过门槛（前 10）", "",
                  "| 昵称 | 条数 | 原因 |", "|---|---|---|"]
        for m in result["rejected"]:
            lines.append(f"| {m['nick'] or m['uid']} | {m['n']} | {m['reject']} |")
        lines.append("")
    lines += [
        "## 说明",
        "",
        "- 「身份可信」= 窗口内非名册反查（uid_src=log）的比例；10-24 之前的数据没有 uid，仅用于群语域统计。",
        "- 得分只用于排序，不是绝对质量；最终名单建议结合你的主观判断。",
        "- 代表句只保留 4-28 字、无链接、无媒体的短句，避免导出长文与隐私内容。",
    ]
    return "\n".join(lines) + "\n"


def run(cfg: dict, base_dir: Path, roster_obj: roster.Roster | None = None) -> dict:
    gid = str(cfg["gid"])
    corpus_cfg = cfg.get("corpus") or {}
    day_dir = base_dir / (corpus_cfg.get("dir") or "language_lab/data/group_history") / gid
    reports = base_dir / "language_lab" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    ros = roster_obj or roster.load(base_dir / ((cfg.get("sources") or {}).get("roster") or "data/roster.json"), gid)
    name_map = ros.unique_map()
    records = load_corpus(day_dir)
    result = select(cfg, records, name_map)
    payload = dict(result)
    for key in ("top", "runners_up", "rejected"):
        payload[key] = [{k: v for k, v in m.items() if not k.startswith("_")} for m in payload[key]]
    (reports / "candidates.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (reports / "candidates.md").write_text(render_markdown(result, gid), encoding="utf-8")
    print(f"select: {result['eligible']} eligible, top={len(result['top'])} -> {reports / 'candidates.md'}")
    return result
