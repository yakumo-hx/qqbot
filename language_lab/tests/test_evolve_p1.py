# -*- coding: utf-8 -*-
"""P1 决策点数据集断言（审计 4.1 的 5 条 + 补充 schema 检查）。

运行：
    python -m unittest language_lab.tests.test_evolve_p1 -v
"""

from __future__ import annotations

import json
import unittest
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
DP_DIR = BASE / "language_lab" / "evolve" / "data" / "decision_points" / "284517232"
CONFIG_HISTORY = BASE / "config" / "group_history.json"


def load_decision_points() -> list[dict]:
    rows: list[dict] = []
    for path in sorted(DP_DIR.glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


class TestDecisionPoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = load_decision_points()
        cls.bot_uids = {str(x) for x in json.loads(
            CONFIG_HISTORY.read_text(encoding="utf-8")).get("bot_uids") or []}

    def test_00_nonempty(self):
        self.assertGreaterEqual(len(self.rows), 8000,
                                msg=f"决策点只有 {len(self.rows)} 条，达不到 ≥8,000")

    # ---- 审计要求的 5 条 ----
    def test_01_t0_strictly_after_context(self):
        for rec in self.rows:
            ctx = rec["context"]
            self.assertTrue(ctx, msg=rec["id"])
            self.assertGreater(epoch(rec["t0"]), epoch(ctx[-1]["ts"]),
                               msg=f"{rec['id']} t0 不严格大于 context[-1].ts")

    def test_02_latency_matches_t0_minus_context(self):
        for rec in self.rows:
            if not rec["speak"]:
                self.assertIsNone(rec["latency_s"], msg=rec["id"])
                continue
            expect = epoch(rec["t0"]) - epoch(rec["context"][-1]["ts"])
            self.assertAlmostEqual(rec["latency_s"], expect, delta=0.01,
                                   msg=f"{rec['id']} latency {rec['latency_s']} != {expect}")

    def test_03_positive_message_not_in_context(self):
        for rec in self.rows:
            if not rec["speak"]:
                continue
            own = rec["bubbles"][0]
            for c in rec["context"]:
                same_ts = str(c.get("ts")) == str(own.get("ts"))
                same_uid = str(c.get("uid")) == str(rec["actor"]["uid"])
                self.assertFalse(same_ts and same_uid,
                                 msg=f"{rec['id']} 正样本消息泄漏进 context（ts={own.get('ts')}）")

    def test_04_actor_never_bot(self):
        for rec in self.rows:
            self.assertNotIn(str(rec["actor"]["uid"]), self.bot_uids,
                             msg=f"{rec['id']} actor 是 bot uid")

    def test_05_ratio_and_silent_share(self):
        pos = sum(1 for r in self.rows if r["speak"])
        neg = len(self.rows) - pos
        self.assertGreater(pos, 0)
        self.assertGreaterEqual(neg / pos, 1.0, msg="正负比 <1:1")
        self.assertLessEqual(neg / pos, 3.0, msg="正负比 >1:3")
        silent = sum(1 for r in self.rows
                     if not r["speak"]
                     and (r["meta"] or {}).get("cold")
                     and not (r["meta"] or {}).get("after_self"))
        # 静默行用"cold 且 不是任何 turn 的跟随"识别不可靠；改用每 t0 组内"无正样本"判定
        groups: dict[str, list[dict]] = defaultdict(list)
        for rec in self.rows:
            groups[rec["t0"]].append(rec)
        silent_groups = [g for g in groups.values() if not any(x["speak"] for x in g)]
        silent_rows = sum(len(g) for g in silent_groups)
        self.assertLessEqual(silent_rows / len(self.rows), 0.30,
                             msg=f"静默行占比 {silent_rows / len(self.rows):.3f} > 30%")
        # 每个"有正样本"的 turn：正好 1 个正样本 + 1~3 个负样本
        for t0, g in groups.items():
            n_pos = sum(1 for x in g if x["speak"])
            if n_pos == 0:
                continue
            self.assertEqual(n_pos, 1, msg=f"turn {t0} 正样本数 {n_pos}")
            self.assertGreaterEqual(len(g) - n_pos, 1, msg=f"turn {t0} 没有负样本")
            self.assertLessEqual(len(g) - n_pos, 3, msg=f"turn {t0} 负样本 >3")

    # ---- 补充 schema 检查 ----
    def test_06_schema(self):
        required = {"id", "gid", "day", "t0", "context", "actor", "speak", "latency_s",
                    "bubbles", "reply_to", "at", "split", "oos", "meta"}
        flags = {"night", "after_self", "cold", "ignored", "last_is_question",
                 "topic_switch", "actor_msgs_30m", "live_roster_n"}
        ids = set()
        for rec in self.rows:
            self.assertTrue(required <= set(rec), msg=rec.get("id"))
            self.assertTrue(flags <= set(rec["meta"]), msg=rec.get("id"))
            self.assertIn(rec["split"], ("train", "test"))
            self.assertIsInstance(rec["oos"], bool)
            self.assertTrue(8 <= len(rec["context"]) <= 30, msg=rec["id"])
            self.assertNotIn(rec["id"], ids)
            ids.add(rec["id"])
            for c in rec["context"]:
                self.assertTrue({"ts", "uid", "nick", "kind", "text", "media"} <= set(c),
                                msg=rec["id"])
            if rec["speak"]:
                self.assertTrue(rec["bubbles"], msg=rec["id"])
                self.assertIsNotNone(rec["latency_s"])
            else:
                self.assertEqual(rec["bubbles"], [])
                self.assertEqual(rec["at"], [])
                self.assertIsNone(rec["reply_to"])

    # ---- replay 验尺子（不调模型） ----
    def test_07_identity_bcs_is_one(self):
        from language_lab.evolve.metrics import replay
        from language_lab.evolve.search.candidate import load_candidate
        points = [p for p in self.rows if p["split"] == "test"][:200]
        cand = load_candidate(BASE / "language_lab" / "evolve" / "candidates" / "c000")
        result = replay.run(points, cand, k=1, identity=True, out_dir=None,
                            bootstrap=0, verbose=False, label="selftest")
        bcs = result["bcs"]
        self.assertIsNotNone(bcs.get("bcs"))
        self.assertGreaterEqual(bcs["bcs"], 0.99, msg=f"identity BCS={bcs['bcs']}")
        self.assertTrue(bcs["gates"]["passed"], msg=str(bcs["gates"]["violations"]))
        for name, dim in bcs["dims"].items():
            if dim["score"] is not None:      # 小样本下 B2/B5 可能因样本不足从加权剔除
                self.assertAlmostEqual(dim["score"], 1.0, places=9, msg=name)


if __name__ == "__main__":
    unittest.main()
