# -*- coding: utf-8 -*-
"""W2 对拍：context_render 与生产 scripts.reference 必须 100% 相同。

允许在**测试**里 import scripts.reference 当 oracle；运行期代码不 import。
"""

from __future__ import annotations

import json
import random
import unittest
from pathlib import Path

from language_lab.lab.ingest import context_render

BASE = Path(__file__).resolve().parents[2]
CORPUS = BASE / "language_lab" / "data" / "group_history" / "284517232"


def _load_records(limit: int = 500) -> list[dict]:
    rows = []
    for p in sorted(CORPUS.glob("*.jsonl")):
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("is_bot") or rec.get("src") not in ("llbot", "recorder", "capture"):
                    continue
                rows.append(rec)
    # 确定性抽样：优先包含带引用/at 的行，再补普通行
    rows.sort(key=lambda r: str(r.get("ts") or ""))
    rng = random.Random(20261103)
    with_ref = [r for r in rows if r.get("reply_to")]
    with_at = [r for r in rows if r.get("at")]
    rest = [r for r in rows if not r.get("reply_to") and not r.get("at")]
    picked = with_ref[:180] + with_at[:80] + rng.sample(rest, min(240, len(rest)))
    rng.shuffle(picked)
    return picked[:limit]


def _item(rec: dict) -> dict:
    reply = rec.get("reply_to") or None
    return {
        "ts": rec.get("ts"),
        "uid": str(rec.get("uid") or ""),
        "nick": rec.get("nick") or "",
        "card": rec.get("card") or "",
        "text": rec.get("text") or "",
        "at": [str(x) for x in (rec.get("at") or [])],
        "reply_to": ({"uid": str(reply.get("uid") or ""),
                      "nick": reply.get("nick") or "",
                      "text": (reply.get("text") or "")[:200]} if reply else None),
    }


def _oracle_record(rec: dict) -> dict:
    reply = rec.get("reply_to") or None
    return {
        "uid": str(rec.get("uid") or ""),
        "card": rec.get("card") or "",
        "nickname": rec.get("nick") or "",
        "text": rec.get("text") or "",
        "at": [str(x) for x in (rec.get("at") or [])],
        "reply_id": "r" if reply else "",
    }


def _oracle_quote(rec: dict) -> dict | None:
    reply = rec.get("reply_to") or None
    if not reply:
        return None
    return {"uid": str(reply.get("uid") or ""),
            "nickname": reply.get("nick") or "",
            "card": "",
            "text": (reply.get("text") or "")[:200]}


class TestContextRender(unittest.TestCase):
    def test_500_real_records_match_production(self):
        from scripts import reference as prod
        recs = _load_records(500)
        self.assertEqual(len(recs), 500)
        name_of = lambda uid: {"u1": "阿强"}.get(uid, "")  # noqa: E731 - oracle 与实现传同一个函数
        same = 0
        diffs = []
        for rec in recs:
            got = context_render.render_line(_item(rec), name_of=name_of)
            want = prod.render(_oracle_record(rec), _oracle_quote(rec),
                               quote_max_chars=40, name_of=name_of)
            if got == want:
                same += 1
            elif len(diffs) < 5:
                diffs.append((rec.get("ts"), got, want))
        self.assertEqual(same, len(recs), f"mismatch {len(recs) - same}/500; first={diffs}")

    def test_rules(self):
        base = {"uid": "u1", "nick": "UC", "card": "", "at": [], "text": "正文"}
        self.assertEqual(context_render.render_line(base), "[UC] 正文")
        self.assertEqual(
            context_render.render_line({**base, "reply_to": {"uid": "u2", "nick": "阿强",
                                                             "text": "我明天可能来不了"}}),
            "[UC 回复阿强「我明天可能来不了」] 正文")
        self.assertEqual(
            context_render.render_line({**base, "reply_to": {"uid": "u2", "nick": "阿强", "text": ""}}),
            "[UC 回复阿强] 正文")
        self.assertEqual(
            context_render.render_line({"uid": "u1", "nick": "UC", "card": "", "text": "",
                                        "at": ["u2"]}, name_of=lambda u: "某人"),
            "[UC@某人]（只 @ 了人，没有文字）")
        self.assertEqual(context_render.render_line({**base, "text": ""}), "")
        long_quote = "字" * 50
        line = context_render.render_line({**base, "reply_to": {"uid": "u2", "nick": "阿强",
                                                                "text": long_quote}})
        self.assertIn("「" + "字" * 40 + "…」", line)

    def test_render_context_marks_last(self):
        items = [{"uid": "a", "nick": "A", "text": "一"},
                 {"uid": "b", "nick": "B", "text": "二"}]
        lines = context_render.render_context(items)
        self.assertEqual(lines, ["[A] 一", "[B] 二  ← 最后一条"])
        self.assertEqual(context_render.render_context(items, mark_last=False),
                         ["[A] 一", "[B] 二"])


if __name__ == "__main__":
    unittest.main()
