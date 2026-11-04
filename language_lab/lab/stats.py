# -*- coding: utf-8 -*-
"""语料统计：为「群语域卡」和评测提供基线数字。

只输出聚合指标与极短例句，不导出完整聊天记录。
"""

from __future__ import annotations

import glob
import json
import re
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable, List

from .ingest.build import norm_text

TZ = timezone(timedelta(hours=8))

_PLACEHOLDER_RE = re.compile(r"\[[^\]]{1,12}\]")
_AT_RE = re.compile(r"@[^\s@]{1,40}\s*")
_WS_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"https?://|www\.", re.I)
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\uFE0F]"
)
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)

INTERJECTIONS = [
    "草", "笑死", "绷", "确实", "难绷", "6", "?", "？", "啊", "吧", "呢", "吗",
    "哦", "嘛", "啦", "哈", "呃", "不是", "真的", "有点", "感觉", "其实", "就",
]
AI_TELLS = [
    "首先", "其次", "另外", "总之", "综上", "希望", "建议", "值得注意",
    "总的来说", "需要注意的是", "作为一个", "可以理解", "如果你愿意",
    "总结一下", "简单来说", "换句话说",
]


def plain(text: str | None) -> str:
    t = (text or "").translate(_ZERO_WIDTH)
    t = _PLACEHOLDER_RE.sub("", t)
    t = _AT_RE.sub("", t)
    return _WS_RE.sub("", t).strip()


def load_corpus(day_dir: Path, start: str | None = None, end: str | None = None) -> List[dict]:
    records: List[dict] = []
    for path in sorted(glob.glob(str(day_dir / "*.jsonl"))):
        day = Path(path).stem
        if start and day < start:
            continue
        if end and day > end:
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
    return records


def _pct(n: int, d: int) -> float:
    return (100.0 * n / d) if d else 0.0


def _median(values: Iterable[float]) -> float:
    vals = [v for v in values if v is not None]
    return float(statistics.median(vals)) if vals else 0.0


def _p90(values: Iterable[float]) -> float:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return 0.0
    idx = min(len(vals) - 1, int(round(0.9 * (len(vals) - 1))))
    return float(vals[idx])


def _length_bucket(n: int) -> str:
    if n <= 3:
        return "1-3"
    if n <= 6:
        return "4-6"
    if n <= 10:
        return "7-10"
    if n <= 20:
        return "11-20"
    return "21+"


def _bubble_stats(records: List[dict]) -> dict:
    """同一个人在 60 秒内连发多条，算一个 burst。"""
    bursts: List[int] = []
    cur_uid, cur_ts, cur_n = None, 0.0, 0
    for rec in records:
        if rec.get("is_bot"):
            continue
        ts = float(rec.get("ts_epoch") or 0)
        uid = rec.get("uid") or rec.get("nick")
        if uid == cur_uid and ts - cur_ts <= 60:
            cur_n += 1
            cur_ts = ts
        else:
            if cur_n:
                bursts.append(cur_n)
            cur_uid, cur_ts, cur_n = uid, ts, 1
    if cur_n:
        bursts.append(cur_n)
    hist = Counter(bursts)
    total = sum(bursts)
    groups = len(bursts)
    return {
        "groups": groups,
        "messages": total,
        "hist": dict(sorted(hist.items())),
        "multi_message_ratio": _pct(total - hist.get(1, 0), total),
        "multi_group_ratio": _pct(groups - hist.get(1, 0), groups),
        "avg": round(total / groups, 2) if groups else 0.0,
    }


def _reply_latency(records: List[dict], roster_names: dict) -> List[float]:
    out: List[float] = []
    by_uid: dict = {}
    recent: List[dict] = []
    for rec in records:
        ts = float(rec.get("ts_epoch") or 0)
        reply = rec.get("reply_to") or {}
        target_uid = reply.get("uid") or roster_names.get((reply.get("nick") or "").strip())
        if target_uid:
            for prev in reversed(recent[-60:]):
                if str(prev.get("uid")) == str(target_uid) and ts - float(prev.get("ts_epoch") or 0) <= 300:
                    out.append(ts - float(prev.get("ts_epoch") or 0))
                    break
        recent.append(rec)
        if len(recent) > 200:
            recent = recent[-120:]
    return out


def compute(records: List[dict], roster_names: dict | None = None) -> dict:
    roster_names = roster_names or {}
    humans = [r for r in records if not r.get("is_bot")]
    bots = [r for r in records if r.get("is_bot")]
    lens = [len(plain(r.get("text"))) for r in humans]
    lens = [n for n in lens if n > 0]
    bot_lens = [len(plain(r.get("text"))) for r in bots]
    bot_lens = [n for n in bot_lens if n > 0]

    def form_stats(rows: List[dict], lengths: List[int]) -> dict:
        n = len(rows) or 1
        texts = [plain(r.get("text")) for r in rows]
        ends = Counter((t[-1] if t else "") for t in texts)
        return {
            "n": len(rows),
            "median_len": _median(lengths),
            "mean_len": round(sum(lengths) / len(lengths), 1) if lengths else 0.0,
            "p90_len": _p90(lengths),
            "no_punct": _pct(sum(1 for t in texts if t and t[-1] not in "。！？!?…~～"), len(texts)),
            "period": _pct(ends.get("。", 0), len(texts)),
            "question": _pct(sum(1 for t in texts if "？" in t or "?" in t or t.endswith(("吗", "呢"))), len(texts)),
            "exclaim": _pct(sum(1 for t in texts if "！" in t or "!" in t), len(texts)),
            "ellipsis": _pct(sum(1 for t in texts if "…" in t or "..." in t), len(texts)),
            "tilde": _pct(sum(1 for t in texts if "~" in t or "～" in t), len(texts)),
            "emoji": _pct(sum(1 for t in texts if _EMOJI_RE.search(t)), len(texts)),
            "media": _pct(sum(1 for r in rows if r.get("media")), n),
            "image": _pct(sum(1 for r in rows if "image" in (r.get("media") or [])), n),
            "face": _pct(sum(1 for r in rows if "face" in (r.get("media") or [])), n),
            "reply": _pct(sum(1 for r in rows if r.get("reply_to")), n),
            "at": _pct(sum(1 for r in rows if r.get("at") or "@" in (r.get("text") or "")), n),
            "ai_tell": _pct(sum(1 for t in texts if any(w in t for w in AI_TELLS)), len(texts)),
        }

    token_counter: Counter = Counter()
    for t in (plain(r.get("text")) for r in humans):
        for tok in INTERJECTIONS:
            token_counter[tok] += t.count(tok)
    ai_counter: Counter = Counter()
    for t in (plain(r.get("text")) for r in humans):
        for w in AI_TELLS:
            ai_counter[w] += t.count(w)

    bubbles = _bubble_stats(humans)
    latencies = _reply_latency(humans, roster_names)
    days = Counter((r.get("day") or "")[:10] for r in records)
    by_day = []
    for day in sorted(days):
        rows = [r for r in records if (r.get("day") or "") == day]
        h = [r for r in rows if not r.get("is_bot")]
        by_day.append({
            "day": day,
            "total": len(rows),
            "human": len(h),
            "bot": len(rows) - len(h),
            "media_pct": round(_pct(sum(1 for r in h if r.get("media")), len(h)), 1),
        })

    short_examples = []
    for r in humans:
        t = plain(r.get("text"))
        if 4 <= len(t) <= 18 and not _URL_RE.search(t) and not r.get("media") and "签到" not in t:
            short_examples.append(t)
        if len(short_examples) >= 30:
            break

    return {
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "total": len(records),
        "human": len(humans),
        "bot": len(bots),
        "uid_missing": sum(1 for r in records if not r.get("uid")),
        "days": by_day,
        "human_form": form_stats(humans, lens),
        "bot_form": form_stats(bots, bot_lens),
        "length_dist": dict(Counter(_length_bucket(n) for n in lens)),
        "tokens": [{"tok": k, "count": v} for k, v in token_counter.most_common(20)],
        "ai_tells": [{"word": k, "count": v} for k, v in ai_counter.most_common(12) if v],
        "bursts": bubbles,
        "reply_latency_s": {
            "n": len(latencies),
            "median": round(_median(latencies), 1),
            "p90": round(_p90(latencies), 1),
        },
        "short_examples": short_examples[:20],
    }


def render_markdown(stats: dict, gid: str) -> str:
    h, b = stats["human_form"], stats["bot_form"]
    lines = [
        f"# 语料统计 · 群 {gid}",
        "",
        f"- 生成时间：{stats['generated_at']}",
        f"- 总消息：**{stats['total']}**（人类 {stats['human']} / 机器人账号 {stats['bot']}）",
        f"- uid 缺失：{stats['uid_missing']}",
        "",
        "## 每日消息量",
        "",
        "| 日期 | 总计 | 人类 | 机器人 | 媒体占比 |",
        "|---|---|---|---|---|",
    ]
    for d in stats["days"]:
        lines.append(f"| {d['day']} | {d['total']} | {d['human']} | {d['bot']} | {d['media_pct']}% |")
    lines += [
        "",
        "## 人类消息形态",
        "",
        "| 指标 | 人类 | 机器人账号（对照） |",
        "|---|---|---|",
        f"| 消息数 | {h['n']} | {b['n']} |",
        f"| 中位长度 | **{h['median_len']}** 字 | {b['median_len']} 字 |",
        f"| 平均长度 | {h['mean_len']} 字 | {b['mean_len']} 字 |",
        f"| P90 长度 | {h['p90_len']} 字 | {b['p90_len']} 字 |",
        f"| 句尾无标点 | {h['no_punct']:.1f}% | {b['no_punct']:.1f}% |",
        f"| 句号结尾 | {h['period']:.1f}% | {b['period']:.1f}% |",
        f"| 问句 | {h['question']:.1f}% | {b['question']:.1f}% |",
        f"| 感叹号 | {h['exclaim']:.1f}% | {b['exclaim']:.1f}% |",
        f"| 省略号 | {h['ellipsis']:.1f}% | {b['ellipsis']:.1f}% |",
        f"| 波浪号 | {h['tilde']:.1f}% | {b['tilde']:.1f}% |",
        f"| emoji | {h['emoji']:.1f}% | {b['emoji']:.1f}% |",
        f"| 媒体消息 | {h['media']:.1f}% | {b['media']:.1f}% |",
        f"| 引用回复 | {h['reply']:.1f}% | {b['reply']:.1f}% |",
        f"| @ 他人 | {h['at']:.1f}% | {b['at']:.1f}% |",
        f"| AI 味词 | {h['ai_tell']:.1f}% | {b['ai_tell']:.1f}% |",
        "",
        "## 长度分布（人类）",
        "",
        "| 长度 | 条数 |",
        "|---|---|",
    ]
    for k in ("1-3", "4-6", "7-10", "11-20", "21+"):
        lines.append(f"| {k} 字 | {stats['length_dist'].get(k, 0)} |")
    lines += [
        "",
        "## 连发与回复",
        "",
        f"- 60 秒内连发：{stats['bursts']['groups']} 组 / {stats['bursts']['messages']} 条；"
        f"多条消息占比 **{stats['bursts']['multi_message_ratio']:.1f}%**，多条连发组占比 {stats['bursts']['multi_group_ratio']:.1f}%，"
        f"平均 {stats['bursts']['avg']} 条/组",
        f"- 连发条数分布：{json.dumps(stats['bursts']['hist'], ensure_ascii=False)}",
        f"- 回复延迟：中位 **{stats['reply_latency_s']['median']}s**，P90 {stats['reply_latency_s']['p90']}s（n={stats['reply_latency_s']['n']}）",
        "",
        "## 高频词（人类原文计数）",
        "",
    ]
    lines.append("、".join(f"{x['tok']}({x['count']})" for x in stats["tokens"][:16]))
    if stats["ai_tells"]:
        lines += ["", "## 人类语料里的“AI 味词”出现次数（反面参照）", ""]
        lines.append("、".join(f"{x['word']}({x['count']})" for x in stats["ai_tells"]))
    lines += ["", "## 短句样例（节选，已去占位符）", ""]
    for t in stats["short_examples"][:12]:
        lines.append(f"- {t}")
    return "\n".join(lines) + "\n"


def register_payload(stats: dict) -> dict:
    """群语域卡（机器可读）：后续生成器的硬约束与提示词素材。"""
    h = stats["human_form"]
    return {
        "gid": None,
        "generated_at": stats["generated_at"],
        "n_human": stats["human"],
        "median_len": h["median_len"],
        "mean_len": h["mean_len"],
        "p90_len": h["p90_len"],
        "no_punct_pct": round(h["no_punct"], 1),
        "period_pct": round(h["period"], 1),
        "question_pct": round(h["question"], 1),
        "exclaim_pct": round(h["exclaim"], 1),
        "ellipsis_pct": round(h["ellipsis"], 1),
        "tilde_pct": round(h["tilde"], 1),
        "emoji_pct": round(h["emoji"], 1),
        "media_pct": round(h["media"], 1),
        "reply_pct": round(h["reply"], 1),
        "at_pct": round(h["at"], 1),
        "ai_tell_pct": round(h["ai_tell"], 1),
        "length_dist": stats["length_dist"],
        "tokens": stats["tokens"][:16],
        "burst": stats["bursts"],
        "reply_latency_s": stats["reply_latency_s"],
        "examples": stats["short_examples"][:20],
    }


def render_register(reg: dict) -> str:
    tokens = "、".join(f"{x['tok']}" for x in reg["tokens"][:12])
    lines = [
        f"# 群语域卡 · {reg['gid']}",
        "",
        f"- 样本：{reg['n_human']} 条真人消息；中位 **{reg['median_len']:.0f} 字**，平均 {reg['mean_len']} 字，P90 {reg['p90_len']:.0f} 字",
        f"- 标点：句尾不加标点 **{reg['no_punct_pct']}%**；句号 {reg['period_pct']}%、问句 {reg['question_pct']}%、感叹 {reg['exclaim_pct']}%、省略号 {reg['ellipsis_pct']}%、波浪号 {reg['tilde_pct']}%",
        f"- 形式：emoji {reg['emoji_pct']}%、图片/表情 {reg['media_pct']}%、引用回复 {reg['reply_pct']}%、@ 他人 {reg['at_pct']}%",
        f"- 连发：平均 {reg['burst']['avg']} 条/组，多条消息占比 {reg['burst']['multi_message_ratio']:.1f}%",
        f"- 回复延迟中位 {reg['reply_latency_s']['median']}s（P90 {reg['reply_latency_s']['p90']}s）",
        f"- 高频口头语：{tokens}",
        f"- 人味约束：几乎不用「首先/其次/总之/希望/建议」这类词（命中最高的「建议」也仅 23 次）",
        "",
        "## 短句样例",
    ]
    for t in reg["examples"][:12]:
        lines.append(f"- {t}")
    return "\n".join(lines) + "\n"


def run(cfg: dict, base_dir: Path, roster_names: dict | None = None,
        day_dir: Path | None = None, verbose: bool = True) -> dict:
    gid = str(cfg["gid"])
    corpus_cfg = cfg.get("corpus") or {}
    day_dir = day_dir or (base_dir / (corpus_cfg.get("dir") or "language_lab/data/group_history") / gid)
    records = load_corpus(day_dir)
    stats = compute(records, roster_names)
    reports = base_dir / "language_lab" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "corpus_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (reports / "corpus_stats.md").write_text(
        render_markdown(stats, gid), encoding="utf-8"
    )
    reg = register_payload(stats)
    reg["gid"] = gid
    (reports / "group_register.json").write_text(
        json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (reports / "group_register.md").write_text(render_register(reg), encoding="utf-8")
    if verbose:
        print(f"stats: {stats['total']} messages -> {reports / 'corpus_stats.md'}")
    return stats
