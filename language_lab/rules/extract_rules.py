# -*- coding: utf-8 -*-
"""从群语料提炼「像人说话」规则 v1。

纪律：
- 只用**干净子集**（LLBot / recorder / capture 来源，10-24 起；有 uid、有媒体占位）。
  机器人日志早期 4,166 条没有媒体字段、没有 uid，混进来会得出错误规则。
- 每条规则带：统计值、样本量、适用子集、跨天稳定性（10-24~27 训练 / 10-28~30 回测）。
- 只出「可测量」的规则；内容/幽默类不写成规则（后续用检索示例）。

输出（本目录）：
    rules_v1.json          机器可读规则
    rules_v1.md            人读报告（含回测）
    rules_v1_prompt.txt    给生成器用的紧凑提示词
    validation.json        跨天稳定性明细
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]          # E:\QQbot
sys.path.insert(0, str(BASE))

from language_lab.lab import settings, stats  # noqa: E402
from language_lab.lab.ingest import roster  # noqa: E402

TZ = timezone(timedelta(hours=8))
CLEAN_SRC = {"llbot", "recorder", "capture"}
CLEAN_MIN_DAY = "2026-10-24"
TRAIN_END = "2026-10-27"
OUT_DIR = Path(__file__).resolve().parent


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _median(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 1) if vals else 0.0


def _p90(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return 0.0
    return round(vals[min(len(vals) - 1, int(round(0.9 * (len(vals) - 1))))], 1)


def load_clean(cfg: dict) -> list[dict]:
    gid = str(cfg["gid"])
    corpus_dir = settings.GROUP_HISTORY_DIR / gid
    rows: list[dict] = []
    for path in sorted(corpus_dir.glob("*.jsonl")):
        for line in path.open("r", encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("is_bot"):
                continue
            if rec.get("src") not in CLEAN_SRC:
                continue
            if (rec.get("day") or "") < CLEAN_MIN_DAY:
                continue
            rows.append(rec)
    rows.sort(key=lambda r: float(r.get("ts_epoch") or 0))
    return rows


def form_metrics(rows: list[dict]) -> dict:
    texts = [stats.plain(r.get("text")) for r in rows]
    nonempty = [t for t in texts if t]
    lens = [len(t) for t in nonempty]
    end = Counter(t[-1] for t in nonempty)
    n = len(rows)
    n_text = len(nonempty)
    media = Counter(k for r in rows for k in (r.get("media") or []))
    return {
        "n": n,
        "n_text": n_text,
        "median_len": _median(lens),
        "mean_len": round(sum(lens) / len(lens), 1) if lens else 0.0,
        "p90_len": _p90(lens),
        "len_buckets": dict(Counter(
            "1-3" if x <= 3 else "4-6" if x <= 6 else "7-10" if x <= 10
            else "11-20" if x <= 20 else "21+" for x in lens)),
        "no_end_punct": _pct(sum(1 for t in nonempty if t[-1] not in "。！？!?…~～"), n_text),
        "period": _pct(end.get("。", 0), n_text),
        "question": _pct(sum(1 for t in nonempty if "？" in t or "?" in t or t.endswith(("吗", "呢"))), n_text),
        "exclaim": _pct(sum(1 for t in nonempty if "！" in t or "!" in t), n_text),
        "ellipsis": _pct(sum(1 for t in nonempty if "…" in t or "..." in t), n_text),
        "tilde": _pct(sum(1 for t in nonempty if "~" in t or "～" in t), n_text),
        "emoji": _pct(sum(1 for t in nonempty if stats._EMOJI_RE.search(t)), n_text),
        "media": _pct(sum(1 for r in rows if r.get("media")), n),
        "media_kinds": dict(media),
        "reply": _pct(sum(1 for r in rows if r.get("reply_to")), n),
        "at": _pct(sum(1 for r in rows if r.get("at") or "@" in (r.get("text") or "")), n),
        "url": _pct(sum(1 for t in nonempty if stats._URL_RE.search(t)), n_text),
        "ai_tell": _pct(sum(1 for t in nonempty if any(w in t for w in stats.AI_TELLS)), n_text),
    }


def rhythm_metrics(rows: list[dict], name_map: dict) -> dict:
    bursts: list[int] = []
    cur_uid, cur_ts, cur_n = None, 0.0, 0
    sessions = 1
    prev_ts = None
    for rec in rows:
        ts = float(rec.get("ts_epoch") or 0)
        uid = str(rec.get("uid") or rec.get("nick") or "")
        if prev_ts is not None and ts - prev_ts > 600:
            sessions += 1
        prev_ts = ts
        if uid == cur_uid and ts - cur_ts <= 60:
            cur_n += 1
            cur_ts = ts
        else:
            if cur_n:
                bursts.append(cur_n)
            cur_uid, cur_ts, cur_n = uid, ts, 1
    if cur_n:
        bursts.append(cur_n)
    total = sum(bursts)
    hist = Counter(bursts)

    latencies: list[float] = []
    recent: list[dict] = []
    for rec in rows:
        reply = rec.get("reply_to") or {}
        target = reply.get("uid") or name_map.get((reply.get("nick") or "").strip())
        if target:
            ts = float(rec.get("ts_epoch") or 0)
            for prev in reversed(recent[-80:]):
                if str(prev.get("uid")) == str(target) and 0 < ts - float(prev.get("ts_epoch") or 0) <= 300:
                    latencies.append(ts - float(prev.get("ts_epoch") or 0))
                    break
        recent.append(rec)

    hours = Counter(datetime.fromtimestamp(float(r["ts_epoch"]), TZ).hour for r in rows if r.get("ts_epoch"))
    continuation = total - len(bursts)
    return {
        "sessions": sessions,
        "burst_messages": total,
        "burst_message_share": _pct(total - hist.get(1, 0), total),
        "continuation_share": _pct(continuation, total),
        "single_share": _pct(hist.get(1, 0), total),
        "burst_avg": round(total / len(bursts), 2) if bursts else 0.0,
        "burst_hist": dict(sorted(hist.items())),
        "reply_latency_median": _median(latencies),
        "reply_latency_p90": _p90(latencies),
        "reply_latency_n": len(latencies),
        "hour_dist": {str(k): v for k, v in sorted(hours.items())},
    }


def bot_contrast(cfg: dict, human: list[dict]) -> dict:
    """机器人账号（负样本）对照：同口径。"""
    gid = str(cfg["gid"])
    bots: list[dict] = []
    for path in sorted((settings.GROUP_HISTORY_DIR / gid).glob("*.jsonl")):
        for line in path.open("r", encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("is_bot") and (rec.get("day") or "") >= CLEAN_MIN_DAY:
                bots.append(rec)
    bots.sort(key=lambda r: float(r.get("ts_epoch") or 0))
    hm = form_metrics(human)
    bm = form_metrics(bots)

    def per_k(rows: list[dict], word: str) -> float:
        texts = [stats.plain(r.get("text")) for r in rows]
        total = sum(len(t) for t in texts) or 1
        hits = sum(t.count(word) for t in texts)
        return round(1000.0 * hits / total, 2)

    candidates = [
        "。", "，", "！", "？", "…", "~", "～", ">_<", "φ",
        "啦", "呀", "哦", "呢", "嘛", "吧", "啊", "不过", "其实",
        "确实", "建议", "希望", "首先", "其次", "总之", "另外",
        "总的来说", "可以", "需要", "应该", "可能", "然后", "所以",
    ]
    avoid = []
    for word in candidates:
        h_rate = per_k(human, word)
        b_rate = per_k(bots, word)
        if b_rate >= 0.2 and b_rate > h_rate * 1.3:
            avoid.append({"word": word, "human_per_1k": h_rate, "bot_per_1k": b_rate})
    avoid.sort(key=lambda x: -(x["bot_per_1k"] - x["human_per_1k"]))
    return {"n_bot": bm["n"], "metrics": bm, "avoid_words": avoid[:8]}


def stability(early: list[dict], late: list[dict], name_map: dict) -> dict:
    keys = ("median_len", "no_end_punct", "emoji", "media", "reply", "at", "question")
    em = form_metrics(early)
    lm = form_metrics(late)
    out = {}
    for key in keys:
        a, b = em.get(key), lm.get(key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            drift = round(abs(b - a) / a, 3) if a else None
            out[key] = {"train": a, "test": b, "relative_drift": drift,
                        "stable": drift is not None and drift <= 0.2}
    er = rhythm_metrics(early, name_map)
    lr = rhythm_metrics(late, name_map)
    for key in ("burst_message_share", "reply_latency_median"):
        a, b = er.get(key), lr.get(key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            drift = round(abs(b - a) / a, 3) if a else None
            out[key] = {"train": a, "test": b, "relative_drift": drift,
                        "stable": drift is not None and drift <= 0.2}
    return out


def build_rules(cfg: dict) -> dict:
    all_rows = load_clean(cfg)
    early = [r for r in all_rows if (r.get("day") or "") <= TRAIN_END]
    late = [r for r in all_rows if (r.get("day") or "") > TRAIN_END]
    ros = roster.load(settings.resolve((cfg.get("sources") or {}).get("roster") or "data/roster.json"),
                      str(cfg["gid"]))
    name_map = ros.unique_map()
    form = form_metrics(all_rows)
    rhythm = rhythm_metrics(all_rows, name_map)
    contrast = bot_contrast(cfg, all_rows)
    stab = stability(early, late, name_map)
    buckets = form["len_buckets"]
    n_text = form["n_text"]
    short_share = _pct(buckets.get("1-3", 0) + buckets.get("4-6", 0), n_text)
    bm = contrast["metrics"]

    def rule(rid, layer, statement, value, confidence="high"):
        return {"id": rid, "layer": layer, "statement": statement, "value": value,
                "confidence": confidence}

    rules = [
        rule("A1", "form", f"短句为主：中位 {form['median_len']} 字，P90 {form['p90_len']} 字；≤6 字的占 {short_share}%",
             {"median_len": form["median_len"], "p90_len": form["p90_len"], "short_share": short_share,
              "len_buckets": buckets}),
        rule("A2", "form", f"句尾基本不加标点（{form['no_end_punct']}%），句号仅 {form['period']}%，问句 {form['question']}%",
             {"no_end_punct": form["no_end_punct"], "period": form["period"],
              "question": form["question"], "exclaim": form["exclaim"],
              "ellipsis": form["ellipsis"], "tilde": form["tilde"]}),
        rule("A3", "form", f"形式克制：emoji {form['emoji']}%，媒体消息（图片/贴图/转发等）{form['media']}%，引用回复 {form['reply']}%，@ {form['at']}%",
             {"emoji": form["emoji"], "media": form["media"], "media_kinds": form["media_kinds"],
              "reply": form["reply"], "at": form["at"], "url": form["url"]}),
        rule("A4", "rhythm",
             f"连发常见：{rhythm['burst_message_share']}% 的消息属于 2 条以上连发组（单条 {rhythm['single_share']}%），"
             f"平均 {rhythm['burst_avg']} 条/组，{rhythm['continuation_share']}% 是连发追加",
             {"burst_message_share": rhythm["burst_message_share"],
              "continuation_share": rhythm["continuation_share"],
              "single_share": rhythm["single_share"], "burst_avg": rhythm["burst_avg"],
              "burst_hist": rhythm["burst_hist"]}),
        rule("A5", "rhythm", f"回复节奏：引用回复延迟中位 {rhythm['reply_latency_median']}s，P90 {rhythm['reply_latency_p90']}s",
             {"median_s": rhythm["reply_latency_median"], "p90_s": rhythm["reply_latency_p90"],
              "n": rhythm["reply_latency_n"]}),
        rule("D1", "avoid",
             f"负样本对照：机器人账号中位 {bm['median_len']} 字、句号 {bm['period']}%、emoji {bm['emoji']}%、"
             f"问句 {bm['question']}%；真人为 {form['median_len']} 字 / {form['period']}% / {form['emoji']}% / {form['question']}%",
             {"bot": {"median_len": bm["median_len"], "period": bm["period"],
                      "emoji": bm["emoji"], "question": bm["question"]},
              "human": {"median_len": form["median_len"], "period": form["period"],
                        "emoji": form["emoji"], "question": form["question"]}}),
        rule("D2", "avoid",
             "回避机器人高频形式（标点/颜文字/语气词）：" + "、".join(x["word"] for x in contrast["avoid_words"]),
             {"words": contrast["avoid_words"]}),
    ]
    stale_keys = {
        "A1": ["median_len"], "A2": ["no_end_punct"], "A3": ["emoji", "media", "at"],
        "A4": ["burst_message_share"], "A5": ["reply_latency_median"],
    }
    for r in rules:
        bad = [k for k in stale_keys.get(r["id"], []) if k in stab and not stab[k]["stable"]]
        if bad:
            r["confidence"] = "medium"
            r["note"] = "跨天漂移偏大：" + "、".join(
                f"{k}={stab[k]['relative_drift'] * 100:.1f}%" for k in bad)

    return {
        "version": 1,
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "gid": str(cfg["gid"]),
        "subset": {
            "sources": sorted(CLEAN_SRC), "min_day": CLEAN_MIN_DAY,
            "n_messages": form["n"], "n_text": n_text,
            "n_train": len(early), "n_test": len(late),
        },
        "rules": rules,
        "contrast": contrast,
        "stability": stab,
        "rhythm": rhythm,
    }


def render_markdown(payload: dict) -> str:
    s = payload["subset"]
    lines = [
        f"# 群「像人说话」规则 v1 · {payload['gid']}",
        "",
        f"- 生成：{payload['generated_at']}",
        f"- 子集：{s['sources']}，≥{s['min_day']}，共 {s['n_messages']} 条（有文本 {s['n_text']}）",
        f"- 回测：训练 10-24~27（{s['n_train']} 条）/ 测试 10-28~30（{s['n_test']} 条）",
        "",
        "## 规则",
        "",
        "| ID | 层 | 规则 | 置信度 |",
        "|---|---|---|---|",
    ]
    for r in payload["rules"]:
        lines.append(f"| {r['id']} | {r['layer']} | {r['statement']} | {r['confidence']} |")
    c = payload["contrast"]
    bm = c["metrics"]
    lines += [
        "",
        "## 机器人账号对照（负样本）",
        "",
        f"- 样本 {c['n_bot']} 条；中位 {bm['median_len']} 字、句号 {bm['period']}%、emoji {bm['emoji']}%、问句 {bm['question']}%",
        f"- 真人对应：中位 {payload['rules'][0]['value']['median_len']} 字、句号 {payload['rules'][1]['value']['period']}%、emoji {payload['rules'][2]['value']['emoji']}%",
    ]
    if c["avoid_words"]:
        lines += ["", "### 建议回避的词（机器人高频 / 真人低频）", ""]
        for x in c["avoid_words"]:
            lines.append(f"- {x['word']}：真人 {x['human_per_1k']}/千字，机器人 {x['bot_per_1k']}/千字")
    lines += ["", "## 跨天稳定性", "", "| 指标 | 训练 | 测试 | 相对漂移 | 稳定 |", "|---|---|---|---|---|"]
    for k, v in payload["stability"].items():
        drift = "-" if v["relative_drift"] is None else f"{v['relative_drift'] * 100:.1f}%"
        lines.append(f"| {k} | {v['train']} | {v['test']} | {drift} | {'✅' if v['stable'] else '❌'} |")
    lines += [
        "",
        "## 使用方式",
        "",
        "- 作为**生成约束**：长度、标点、emoji、媒体、气泡数；不写进人设长文。",
        "- 内容/幽默类不在此表内，用检索示例解决。",
        "- 每条规则都可回测；若新数据漂移 >20%，降级或重算。",
    ]
    return "\n".join(lines) + "\n"


def render_prompt(payload: dict) -> str:
    r = {x["id"]: x["value"] for x in payload["rules"]}
    return (
        "【本群真人说话方式·统计约束】\n"
        f"- 长度：中位 {r['A1']['median_len']} 字、P90 {r['A1']['p90_len']} 字；≤6 字占 {r['A1']['short_share']}%\n"
        f"- 标点：{r['A2']['no_end_punct']}% 句尾不加标点，句号仅 {r['A2']['period']}%，问句 {r['A2']['question']}%\n"
        f"- 形式：emoji {r['A3']['emoji']}%，媒体消息（图/贴图/转发）{r['A3']['media']}%，引用 {r['A3']['reply']}%，@ {r['A3']['at']}%\n"
        f"- 节奏：平均 {r['A4']['burst_avg']} 条/组，单条占 {r['A4']['single_share']}%；回复延迟中位 {r['A5']['median_s']}s\n"
        "- 不要：长句（>20 字）、句尾句号、首先/其次/总之/希望/建议、客服腔、每条都带 emoji\n"
        "- 少用：密集逗号/句号/感叹号/省略号、颜文字（>_< 之类）\n"
    )


def main() -> int:
    cfg = settings.load_config()
    payload = build_rules(cfg)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "rules_v1.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "rules_v1.md").write_text(render_markdown(payload), encoding="utf-8")
    (OUT_DIR / "rules_v1_prompt.txt").write_text(render_prompt(payload), encoding="utf-8")
    (OUT_DIR / "validation.json").write_text(json.dumps(payload["stability"], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"n={payload['subset']['n_messages']} text={payload['subset']['n_text']}")
    for r in payload["rules"]:
        print(f"[{r['id']}] {r['statement']}  ({r['confidence']})")
    print(f"-> {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
