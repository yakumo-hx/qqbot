# -*- coding: utf-8 -*-
"""1.5 ingest 护栏单测：botlog 无群号拒绝盖章 / 群号不符报错退出。"""

from __future__ import annotations

import unittest

from language_lab.lab.ingest.build import IngestGidMismatch, stamp_botlog


class TestIngestGidGuard(unittest.TestCase):
    def test_declared_owner_stamps(self):
        recs = [{"ts": "2026-10-20T07:00:00+08:00", "gid": None, "text": "x"}]
        kept, skipped = stamp_botlog("284517232", recs, "284517232")
        self.assertEqual(len(kept), 1)
        self.assertEqual(skipped, 0)
        self.assertEqual(kept[0]["gid"], "284517232")

    def test_no_declared_refuses_stamp(self):
        recs = [{"ts": "2026-10-20T07:00:00+08:00", "gid": None, "text": "x"}]
        kept, skipped = stamp_botlog("1055881417", recs, "284517232")
        self.assertEqual(kept, [])
        self.assertEqual(skipped, 1)

    def test_no_declared_at_all_refuses(self):
        recs = [{"gid": None, "text": "x"}]
        kept, skipped = stamp_botlog("1055881417", recs, "")
        self.assertEqual(kept, [])
        self.assertEqual(skipped, 1)

    def test_explicit_mismatch_raises(self):
        recs = [{"gid": "284517232", "text": "x"}]
        with self.assertRaises(IngestGidMismatch):
            stamp_botlog("1055881417", recs, "1055881417")

    def test_explicit_match_kept(self):
        recs = [{"gid": "1055881417", "text": "x"}]
        kept, skipped = stamp_botlog("1055881417", recs, "")
        self.assertEqual(len(kept), 1)
        self.assertEqual(skipped, 0)


if __name__ == "__main__":
    unittest.main()
