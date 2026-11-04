# -*- coding: utf-8 -*-
"""风格卡：把语料蒸馏成「群语域卡 + Top N 个人卡」。

- 输入只给**形式样本**（短句、去媒体、去链接、去长文），不给观点与隐私长文；
- 输出 JSON 卡，供生成器拼 prompt；离线运行、失败重试、失败则退回纯统计卡；
- 禁止逐字复用：卡里的例句只做“风格锚点”，生成阶段另有泄漏检查。
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List

from .ingest.build import norm_text
from .stats import _URL_RE, load_corpus, plain

_SYSTEM = (
    "你是群聊语言风格分析器。只分析『怎么说』（句长、标点、语气词、emoji、连发、接话方式），"
    "不评论内容、不评价人品、不输出原文长句。输出必须是合法 JSON，不要 markdown。"
)

_CARD_PROMPT = """下面是本群成员「{nick}」的真实聊天样本（每行一条，已去媒体/链接）。

请总结他的说话方式，输出 JSON：
{{
  "summary": "80-120 字，描述句长、标点、语气词、常用梗、连发/接话习惯；不要引用整句原文",
  "length": {{"median": 数字, "p90": 数字}},
  "rhythm": {{"bubbles": "1-2", "gap_s": [数字, 数字]}},
  "tokens": ["3-8 个高频词或口头语"],
  "emoji": ["0-4 个常用 emoji"],
  "openers": ["2-4 种常见起手方式"],
  "avoid": ["2-4 条这个成员不会说的话术（如总结句、客服腔）"],
  "confidence": 0.0
}}

样本：
{samples}
"""

_GROUP_PROMPT = """下面是群「{gid}」的真实聊天样本（多成员混合，已去媒体/链接）。
已知统计：中位 {median} 字，P90 {p90} 字，{no_punct}% 句尾不加标点，emoji {emoji}%，
引用回复 {reply}%，连发平均 {avg} 条/组。

请总结“这个群整体怎么说话”，输出 JSON：
{{
  "summary": "80-120 字，描述群的语域：长度、标点、语气、梗密度、接话风格",
  "length": {{"median": 数字, "p90": 数字}},
  "tokens": ["4-8 个群高频词"],
  "avoid": ["2-4 条与本群语域冲突的话术"],
  "confidence": 0.0
}}

样本：
{samples}
"""


def _sample_messages(records: List[dict], limit: int = 90) -> List[str]:
    picked: List[str] = []
    seen = set()
    for rec in records:
        if rec.get("is_bot") or rec.get("media"):
            continue
        t = plain(rec.get("text"))
        if not (3 <= len(t) <= 40) or _URL_RE.search(t):
            continue
        key = norm_text(t)
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        picked.append(t)
        if len(picked) >= limit:
            break
    return picked


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


def _leak_check(card: dict, samples: List[str]) -> int:
    """卡里出现与样本连续重合 ≥10 字 → 计一次泄漏。"""
    blob = json.dumps(card, ensure_ascii=False)
    hits = 0
    for s in samples:
        s = plain(s)
        if len(s) >= 10 and s[:10] in blob:
            hits += 1
    return hits


def _call(prompt: str, retries: int = 3) -> dict | None:
    try:
        from scripts import llm_pool
    except ImportError:
        return None
    for attempt in range(retries):
        try:
            text, model = llm_pool.chat_sync(
                [{"role": "system", "content": _SYSTEM},
                 {"role": "user", "content": prompt}],
                label="lab_style", max_tokens=700, temperature=0.4, timeout=90,
            )
            card = _extract_json(text)
            if card and card.get("summary"):
                card["_model"] = model
                card["_attempts"] = attempt + 1
                return card
        except Exception:
            time.sleep(2 + attempt * 3)
    return None


def _stat_card(records: List[dict], metrics: dict | None, nickname: str) -> dict:
    lens = [len(plain(r.get("text"))) for r in records if plain(r.get("text"))]
    lens.sort()
    median = lens[len(lens) // 2] if lens else 0
    p90 = lens[min(len(lens) - 1, int(0.9 * (len(lens) - 1)))] if lens else 0
    tokens = Counter()
    for r in records:
        t = plain(r.get("text"))
        for tok in ("草", "笑死", "确实", "绷", "6", "？", "啊", "吧", "呢", "吗", "哈"):
            tokens[tok] += t.count(tok)
    return {
        "uid": (metrics or {}).get("uid"),
        "nick": nickname,
        "summary": f"{nickname}：中位 {median} 字、P90 {p90} 字，句尾多不加标点，常用"
                   + "、".join(k for k, _ in tokens.most_common(4)) + "；统计卡（模型不可用时的兜底）。",
        "length": {"median": median, "p90": p90},
        "rhythm": {"bubbles": "1-2", "gap_s": [1, 4]},
        "tokens": [k for k, _ in tokens.most_common(6)],
        "emoji": [],
        "openers": [],
        "avoid": ["首先/其次/总之", "希望/建议", "作为一个AI"],
        "confidence": 0.35,
        "_model": "statistical",
        "_attempts": 0,
    }


def run(cfg: dict, base_dir: Path, verbose: bool = True) -> dict:
    gid = str(cfg["gid"])
    corpus_dir = base_dir / (cfg.get("corpus") or {}).get("dir", "language_lab/data/group_history") / gid
    reports = base_dir / "language_lab" / "reports"
    cards_dir = reports / "style_cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    records = load_corpus(corpus_dir)

    cand_path = reports / "candidates.json"
    top: List[dict] = []
    if cand_path.exists():
        try:
            top = json.loads(cand_path.read_text(encoding="utf-8")).get("top") or []
        except ValueError:
            top = []

    register = {}
    reg_path = reports / "group_register.json"
    if reg_path.exists():
        try:
            register = json.loads(reg_path.read_text(encoding="utf-8"))
        except ValueError:
            register = {}

    # 群语域卡
    group_samples = _sample_messages(records, 80)
    group_prompt = _GROUP_PROMPT.format(
        gid=gid,
        median=register.get("median_len", 0),
        p90=register.get("p90_len", 0),
        no_punct=register.get("no_punct_pct", 0),
        emoji=register.get("emoji_pct", 0),
        reply=register.get("reply_pct", 0),
        avg=(register.get("burst") or {}).get("avg", 0),
        samples="\n".join(f"- {t}" for t in group_samples),
    )
    group_card = _call(group_prompt) or {
        "summary": "统计兜底：群内以短句为主，句尾多不加标点，少用书面连接词。",
        "length": {"median": register.get("median_len", 0), "p90": register.get("p90_len", 0)},
        "tokens": [x["tok"] for x in register.get("tokens", [])[:6]],
        "avoid": ["首先/其次/总之", "希望/建议"],
        "confidence": 0.35,
        "_model": "statistical",
    }
    group_card.update({"gid": gid, "samples": group_samples[:20],
                       "leak": _leak_check(group_card, group_samples)})
    (cards_dir / "group.json").write_text(
        json.dumps(group_card, ensure_ascii=False, indent=2), encoding="utf-8")

    # 个人卡
    results = []
    for m in top:
        uid = str(m.get("uid"))
        recs = [r for r in records if str(r.get("uid")) == uid and not r.get("is_bot")]
        samples = _sample_messages(recs, 80)
        prompt = _CARD_PROMPT.format(nick=m.get("nick") or uid,
                                     samples="\n".join(f"- {t}" for t in samples))
        card = _call(prompt) or _stat_card(recs, m, m.get("nick") or uid)
        card.update({"uid": uid, "nick": m.get("nick") or uid,
                     "n": len(recs), "samples": samples[:12],
                     "leak": _leak_check(card, samples)})
        (cards_dir / f"{uid}.json").write_text(
            json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
        results.append(card)
        if verbose:
            print(f"card {uid} {card.get('nick')} model={card.get('_model')} leak={card['leak']}")

    lines = [f"# 风格卡 · 群 {gid}", "",
             f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"- 模型：{group_card.get('_model')}；个人卡 "
             + "、".join(f"{c['nick']}={c.get('_model')}" for c in results), ""]
    lines += ["## 群语域卡", "", group_card.get("summary", ""), "",
              f"- 长度：中位 {group_card['length']['median']} / P90 {group_card['length']['p90']}",
              f"- 高频词：{'、'.join(group_card.get('tokens') or [])}",
              f"- 避免：{'；'.join(group_card.get('avoid') or [])}", ""]
    lines += ["## 个人卡", ""]
    for c in results:
        lines += [f"### {c['nick']}（{c['uid']}）", "",
                  c.get("summary", ""), "",
                  f"- 长度：中位 {c['length']['median']} / P90 {c['length']['p90']}",
                  f"- 节奏：{c.get('rhythm')}",
                  f"- 口头语：{'、'.join(c.get('tokens') or [])}",
                  f"- 起手：{'；'.join(c.get('openers') or []) or '—'}",
                  f"- 避免：{'；'.join(c.get('avoid') or []) or '—'}",
                  f"- 泄漏检查：{c.get('leak', 0)}", ""]
    (reports / "style_cards.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if verbose:
        print(f"cards -> {reports / 'style_cards.md'}")
    return {"group": group_card, "members": results}
