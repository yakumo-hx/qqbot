# -*- coding: utf-8 -*-
"""解析器与合并逻辑的离线单测。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from language_lab.lab.ingest import botlog, build, llbot, roster


LL = ("2026-10-28 16:18:00 [I] core [收-群] 碧油鸡乐土（转正版）（鱼塘承包）(284517232) "
      "游云不喜欢哦(10001)：@爱尔奎特的十七分之一 和魔力差不多哦")


class TestLLBot(unittest.TestCase):
    def test_basic(self):
        rec = llbot.parse_line(LL)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["gid"], "284517232")
        self.assertEqual(rec["uid"], "10001")
        self.assertEqual(rec["nick"], "游云不喜欢哦")
        self.assertEqual(rec["day"], "2026-10-28")
        self.assertEqual(rec["ts"], "2026-10-28T16:18:00+08:00")
        self.assertIn("魔力", rec["text"])

    def test_image(self):
        rec = llbot.parse_line(
            "2026-10-28 12:59:45 [I] core [收-群] 群(284517232) 某人(123)：[图片]AB.jpg")
        self.assertEqual(rec["kind"], "image")
        self.assertEqual(rec["media"], ["image"])
        self.assertEqual(rec["text"], "[图片]")

    def test_other_group_filtered(self):
        recs, _ = llbot.load([], "284517232")
        self.assertEqual(recs, [])
        rec = llbot.parse_line(
            "2026-10-28 12:59:45 [I] core [收-群] 别的群(1102194840) 某人(123)：hi")
        self.assertEqual(rec["gid"], "1102194840")


class TestBotLog(unittest.TestCase):
    def test_head_plain(self):
        p = botlog.parse_head("游云不喜欢哦")
        self.assertEqual(p["nick"], "游云不喜欢哦")
        self.assertEqual(p["at"], [])

    def test_head_reply(self):
        p = botlog.parse_head("爱莉不知道哦 回复游云不喜欢哦「“我怎么办？”」")
        self.assertEqual(p["nick"], "爱莉不知道哦")
        self.assertEqual(p["reply_nick"], "游云不喜欢哦")
        self.assertIn("我怎么办", p["reply_text"])

    def test_head_at_and_quote(self):
        p = botlog.parse_head("爱莉不知道哦@.,爱莉不知道哦：女装了 回复爱莉不知道哦")
        self.assertEqual(p["nick"], "爱莉不知道哦")
        self.assertIn(".", p["at"])
        self.assertEqual(p["reply_nick"], "爱莉不知道哦")

    def test_iter_records_with_continuation(self):
        lines = [
            "[2026-10-28 15:42:19] [指代] [游云不喜欢哦] 第一行",
            "第二行",
            "[2026-10-28 15:42:20] [指代] [某人 回复游云不喜欢哦「第一行」] 收到",
            "[2026-10-28 15:42:21] [指代] 注解层已启用=True 索引窗口=200条/群",
        ]
        recs = botlog.iter_records(lines)
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["text"], "第一行\n第二行")
        self.assertEqual(recs[1]["reply_to"]["nick"], "游云不喜欢哦")
        self.assertEqual(recs[1]["ts"], "2026-10-28T15:42:20+08:00")

    def test_at_only(self):
        lines = ["[2026-10-28 15:42:19] [指代] [某人@别人]（只 @ 了人，没有文字）"]
        recs = botlog.iter_records(lines)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["kind"], "at_only")
        self.assertEqual(recs[0]["text"], "")
        self.assertEqual(recs[0]["at"], ["别人"])


class TestRoster(unittest.TestCase):
    def _write(self, data):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(data, tmp, ensure_ascii=False)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return tmp.name

    def test_unique_and_ambiguous(self):
        data = {"users": {
            "1": {"nickname": {"value": "阿强"}, "groups": {"284517232": {"current_card": "强哥"}}},
            "2": {"nickname": {"value": "小明"}, "groups": {"284517232": {"current_card": "阿强"}}},
        }}
        ros = roster.load(self._write(data), "284517232")
        self.assertEqual(ros.resolve("小明"), "2")
        self.assertIsNone(ros.resolve("阿强"))  # 1 的昵称与 2 的群名片重名
        self.assertEqual(ros.size(), 2)
        m = ros.unique_map()
        self.assertEqual(m["小明"], "2")
        self.assertNotIn("阿强", m)


class TestMerge(unittest.TestCase):
    def _rec(self, src, ts, uid, nick, text, **kw):
        rec = {"v": 1, "gid": "284517232", "ts": "2026-10-26 00:00:00", "ts_epoch": ts,
               "day": "2026-10-26",
               "uid": uid, "nick": nick, "card": "", "kind": "text", "media": [],
               "text": text, "reply_to": None, "at": [], "is_bot": False, "src": src, "mid": None}
        rec.update(kw)
        return rec

    def test_llbot_wins_and_botlog_enriches(self):
        ll = [self._rec("llbot", 1000.0, "1", "甲", "哦对了")]
        bl = [self._rec("botlog", 1000.0, "1", "甲", "哦对了",
                        reply_to={"uid": None, "nick": "乙", "text": "前一句"})]
        out = build.merge("284517232", ll, bl, [], 5)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["src"], "llbot")
        self.assertEqual(out[0]["reply_to"]["nick"], "乙")

    def test_botlog_unmatched_inside_coverage_dropped(self):
        ll = [self._rec("llbot", 1000.0, "1", "甲", "随便一句比较长的消息")]
        bl = [self._rec("botlog", 1002.0, "2", "乙", "完全不同的另一句话")]
        out = build.merge("284517232", ll, bl, [], 5)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["uid"], "1")

    def test_botlog_before_coverage_kept(self):
        ll = [self._rec("llbot", 2000.0, "1", "甲", "随便一句比较长的消息")]
        bl = [self._rec("botlog", 1000.0, "2", "乙", "更早的一条消息")]
        out = build.merge("284517232", ll, bl, [], 5)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["src"], "botlog")
        self.assertEqual(out[0]["uid_src"], "roster")


class TestTextMatch(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(build.norm_text("@某人 你好 [图片]"), "你好")
        self.assertTrue(build.text_match("哦对了", "哦对了"))
        self.assertTrue(build.text_match("哦对了", "哦对了！！"))
        self.assertFalse(build.text_match("你好", "再见"))


if __name__ == "__main__":
    unittest.main()
