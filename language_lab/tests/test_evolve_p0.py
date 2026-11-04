# -*- coding: utf-8 -*-
"""evolve/P0 的离线单测：harness 等价性、指标可复算、冻结资产完整性、防作弊扫描。

运行：
    python -m unittest language_lab.tests.test_evolve_p0 -v
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from language_lab.evolve.audit import vault_guard
from language_lab.evolve.data import freeze
from language_lab.evolve.metrics import bcs, form, stats_tests
from language_lab.evolve.search import evaluator, parse, prompts
from language_lab.evolve.search.candidate import load_candidate

BASE = Path(__file__).resolve().parents[2]
EVOLVE = BASE / "language_lab" / "evolve"
CAND = EVOLVE / "candidates" / "c000"


class TestCandidate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cand = load_candidate(CAND)

    def test_load(self):
        self.assertEqual(self.cand.id, "c000")
        self.assertEqual(self.cand.preferred, "deepseek-flash")
        self.assertEqual(self.cand.temperature, 0.8)
        self.assertEqual(self.cand.max_tokens, 320)
        self.assertEqual(len(self.cand.sha), 64)

    def test_prompt_matches_old_generator_everywhere(self):
        """c000 的 prompt 必须与 eval/generator.build_prompt 对全部 5,860 题逐字节一致。"""
        from language_lab.eval import generator as old
        scenarios = evaluator.load_scenarios()
        self.assertGreater(len(scenarios), 5000)
        for sc in scenarios:
            self.assertEqual(prompts.build_messages(sc, self.cand),
                             old.build_prompt(sc, self.cand.rules_text.strip()),
                             msg=f"prompt 不一致: {sc['id']}")

    def test_parser_matches_old_generator(self):
        from language_lab.eval import generator as old
        fixtures = [
            '{"skip": false, "use_media": false, "bubbles": ["草", "这也行"]}',
            '```json\n{"use_media": true, "bubbles": []}\n```',
            '```\n{"bubbles": "[昵称] 半句"}\n```',
            '不是 JSON\n第二行\n第三行\n第四行',
            '{"bubbles": ["  a   b  ", "[某某] c"], "skip": true}',
            '{}',
            '吾辈觉得不行',
        ]
        for raw in fixtures:
            data = old._extract_json(raw)
            if data is None:
                lines = [ln.strip(" -\"“”") for ln in raw.splitlines() if ln.strip()]
                data = {"bubbles": lines[:3]}
            expected = old._clean_bubbles(data)
            got = parse.parse_output(raw)
            got.pop("raw", None)
            self.assertEqual(got, expected, msg=f"解析不一致: {raw!r}")

    def test_post_is_identity(self):
        self.assertEqual(self.cand.postprocess_bubbles(["a", "b"]), ["a", "b"])


class TestFormMetrics(unittest.TestCase):
    def test_js_bounds(self):
        self.assertEqual(form.js({"a": 3}, {"a": 3}, ("a",)), 0.0)
        self.assertAlmostEqual(form.js({"a": 5}, {"b": 5}, ("a", "b")), 1.0, places=6)

    def test_case_features_types(self):
        sc = {"type": "reply", "meta": {"has_media": True},
              "reference": {"bubbles": [{"text": "好的", "media": []}], "n_bubbles": 1}}
        gen = {"ok": True, "bubbles": ["行"], "use_media": False, "skip": False}
        feat = form.case_features(sc, gen)
        self.assertEqual(feat["ai_len"], 1)
        self.assertEqual(feat["ref_len"], 2)
        self.assertTrue(feat["ref_media"])
        self.assertFalse(feat["ai_media"])
        self.assertEqual(feat["ai_len_bucket"], "1-3")

    def test_round7_cache_recomputes_old_scorer_exactly(self):
        """round7 的 12 条案例：新 form.score 与旧 scorer.score 逐字段相同。"""
        from language_lab.eval import scorer as old_scorer
        ref = json.loads(evaluator._latest_round_report(7).read_text(encoding="utf-8"))
        smap = evaluator.scenario_map()
        results = []
        cache_dir = BASE / "language_lab" / "eval" / "data" / "cache" / "rules_v4_prompt"
        for case in ref["cases"]:
            sc = smap[case["id"]]
            gen = json.loads((cache_dir / f"{sc['id']}.json").read_text(encoding="utf-8"))
            results.append({"scenario": sc, "gen": gen})
        old = old_scorer.score(results)
        mine = form.score(results)
        for part in ("ai", "human", "js", "behavior", "n"):
            self.assertEqual(mine[part], old[part], msg=part)
        self.assertEqual(len(mine["cases"]), len(old["cases"]))


class TestBCS(unittest.TestCase):
    def test_selfcheck_script_passes(self):
        self.assertEqual(bcs.selfcheck(verbose=False), 0)

    def test_weights_sum(self):
        self.assertAlmostEqual(sum(bcs.DEFAULT_WEIGHTS.values()), 1.0, places=9)

    def test_identity_is_one(self):
        records = bcs._synthetic_records(400, seed=3)
        out = bcs.compute_bcs(records)
        self.assertIsNotNone(out["bcs"])
        self.assertAlmostEqual(out["bcs"], 1.0, places=9)
        for name, dim in out["dims"].items():
            self.assertIsNotNone(dim["score"], msg=name)

    def test_bootstrap_includes_point(self):
        records = bcs._synthetic_records(60, seed=4)
        out = bcs.bootstrap_bcs(records, n_boot=40, seed=5)
        self.assertLessEqual(out["lo"], out["point"] + 1e-9)
        self.assertGreaterEqual(out["hi"], out["point"] - 1e-9)

    def test_gate_repeater(self):
        records = bcs._synthetic_records(200, seed=6)
        for rec in records:
            if rec["cand"]["speak"]:
                rec["cand"]["bubbles"] = ["确实确实确实确实"]
        out = bcs.compute_bcs(records)
        gates = [g["gate"] for g in out["gates"]["violations"]]
        self.assertIn("self_repeat", gates)
        self.assertEqual(out["bcs_final"], 0.0)


class TestStats(unittest.TestCase):
    def test_all(self):
        for row in stats_tests.selfcheck(verbose=False):
            self.assertTrue(row["ok"], msg=f"{row['check']}: {row['detail']}")


class TestFrozenAssets(unittest.TestCase):
    def test_freeze_check(self):
        self.assertEqual(freeze.check(), 0, msg="holdout/vault 被改动或缺失")

    def test_holdout_shape(self):
        data = json.loads((EVOLVE / "data" / "holdout.json").read_text(encoding="utf-8"))
        self.assertEqual(data["n"], 500)
        self.assertEqual(len(data["ids"]), 500)
        self.assertEqual(data["sha256"], freeze._sha_ids(data["ids"]))
        vault = json.loads((EVOLVE / "vault" / "items.json").read_text(encoding="utf-8"))
        self.assertEqual(len(vault["ids"]), 200)
        self.assertFalse(set(data["ids"]) & set(vault["ids"]))

    def test_holdout_ids_are_test_split(self):
        smap = evaluator.scenario_map()
        data = json.loads((EVOLVE / "data" / "holdout.json").read_text(encoding="utf-8"))
        for sid in data["ids"]:
            self.assertIn(sid, smap)
            self.assertEqual(smap[sid]["split"], "test")
            self.assertNotEqual(smap[sid].get("type"), "media_post")


class TestAudit(unittest.TestCase):
    def test_vault_guard_clean(self):
        report = vault_guard.scan(verbose=False)
        self.assertTrue(report["ok"], msg=json.dumps(report["violations"], ensure_ascii=False))
        self.assertGreater(report["scanned"], 0)

    def test_vault_guard_self_test(self):
        self.assertEqual(vault_guard.self_test(verbose=False), 0)


if __name__ == "__main__":
    unittest.main()
