# -*- coding: utf-8 -*-
"""群聊语料记录器（group_history）离线单测。

跑法：
    python scripts\test_group_history.py
    python -m unittest scripts.test_group_history -v

全部离线：不连 NapCat、不调模型、不读写生产语料目录；落盘写到临时目录，跑完删除。
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import group_history, message_index  # noqa: E402

GID = "284517232"                     # 与 config/group_history.json 白名单一致
OTHER_GID = "123456789"
BOT_UID = "2668851638"                # config/group_history.json 的 bot_uids[0]


def _new_tmp_dir() -> str:
    """建一个可写的临时目录。

    刻意不用 ``tempfile.mkdtemp()``：它按 0o700 建目录，在 Windows 受限令牌
    （低完整性 / 沙箱）下新建的目录可能连自己都写不进去。``os.makedirs`` 用默认
    权限，任何环境都能写。
    """
    path = os.path.join(tempfile.gettempdir(), "group_history_test_" + uuid.uuid4().hex)
    os.makedirs(path)
    return path


def _event(**overrides) -> dict:
    """最小可用的群消息事件（字段形状同 NapCat 推送）。"""
    data = {
        "post_type": "message",
        "message_type": "group",
        "group_id": int(GID),
        "user_id": 12345,
        "message_id": "mid-1",
        "raw_message": "你好",
        "sender": {"user_id": 12345, "nickname": "小明", "card": ""},
    }
    data.update(overrides)
    return data


class GroupHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = _new_tmp_dir()
        group_history._configure_for_tests(
            self.tmp, enabled=True, groups=[GID], bot_uids=[BOT_UID],
            max_queue=2000, max_text_chars=4000,
        )
        message_index.clear()

    def tearDown(self):
        group_history.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- 工具 ----------

    def _rows(self) -> list:
        """停写线程后读出所有落盘行（按文件、行序）。"""
        group_history.shutdown()
        rows = []
        for path in sorted(glob.glob(os.path.join(self.tmp, "**", "*.jsonl"), recursive=True)):
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        rows.append(json.loads(line))
        return rows

    def _one_row(self, **overrides) -> dict:
        group_history.record(GID, _event(**overrides))
        rows = self._rows()
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    # ---------- CQ 解析 ----------

    def test_cq_media_placeholders(self):
        """每种媒体 CQ 段 → 对应占位符 / media 类别。"""
        cases = (
            ("image", "[CQ:image,file=x.jpg]", "image", "[图片]"),
            ("mface", "[CQ:mface,summary=偷笑]", "face", "[表情]"),
            ("face", "[CQ:face,id=1]", "face", "[表情]"),
            ("record", "[CQ:record,file=a.silk]", "voice", "[语音]"),
            ("video", "[CQ:video,file=v.mp4]", "video", "[视频]"),
            ("file", "[CQ:file,file=f.txt,size=1]", "file", "[文件]"),
            ("forward", "[CQ:forward,id=fw1]", "forward", "[合并转发]"),
            ("json", "[CQ:json,data=abc]", "link", "[链接]"),
        )
        for name, raw, kind, placeholder in cases:
            with self.subTest(cq=name):
                parsed = group_history.parse_message(raw)
                self.assertEqual(parsed["text"], placeholder)
                self.assertEqual(parsed["plain"], "")
                self.assertEqual(parsed["media"], [kind])

    def test_cq_mixed_with_text_reply_at_and_escapes(self):
        """reply/at 从正文剥离，媒体按顺序占位，CQ 实体转义还原。"""
        raw = (
            "[CQ:reply,id=123][CQ:at,qq=999] 你好&#91;笑&#93;&#44;世界&amp; "
            "[CQ:image,file=x.jpg][CQ:mface,summary=hi][CQ:face,id=1]"
            "[CQ:record,file=a.silk][CQ:video,file=v.mp4][CQ:file,file=f.txt]"
            "[CQ:forward,id=fw1][CQ:json,data=x] 结束"
        )
        parsed = group_history.parse_message(raw)
        self.assertEqual(
            parsed["text"],
            "你好[笑],世界& [图片][表情][表情][语音][视频][文件][合并转发][链接] 结束",
        )
        self.assertEqual(parsed["media"],
                         ["image", "face", "face", "voice", "video", "file", "forward", "link"])
        self.assertEqual(parsed["at"], ["999"])
        self.assertEqual(parsed["reply_id"], "123")
        # plain 不含占位符，只留实义文字
        self.assertIn("你好[笑],世界&", parsed["plain"])
        self.assertTrue(parsed["plain"].endswith("结束"))
        self.assertNotIn("[图片]", parsed["plain"])

    def test_cq_at_dedup_and_reply_first_wins(self):
        parsed = group_history.parse_message(
            "[CQ:at,qq=1][CQ:at,qq=1][CQ:reply,id=7][CQ:reply,id=8] hi")
        self.assertEqual(parsed["at"], ["1"])
        self.assertEqual(parsed["reply_id"], "7")
        self.assertEqual(parsed["text"], "hi")

    def test_unterminated_cq_is_stripped(self):
        """上游截断（没有右方括号）的 CQ 尾巴不能漏进语料。"""
        parsed = group_history.parse_message("hi [CQ:face,id=1")
        self.assertEqual(parsed["text"], "hi")
        self.assertEqual(parsed["plain"], "hi")
        self.assertEqual(parsed["media"], [])

    # ---------- 行结构 ----------

    def test_row_has_required_fields(self):
        row = self._one_row(
            message_id="mid-7",
            raw_message="[CQ:reply,id=123][CQ:at,qq=999] 看这个[CQ:image,file=x.jpg] 好玩",
            sender={"user_id": 12345, "nickname": "小明", "card": "小明明"},
        )
        for key in ("v", "gid", "seq", "ts", "ts_epoch", "uid", "nick", "card", "kind",
                    "text", "media", "reply_to", "at", "is_bot", "mid", "src"):
            self.assertIn(key, row)
        self.assertEqual(row["v"], 1)
        self.assertEqual(row["gid"], GID)
        self.assertIsInstance(row["seq"], int)
        self.assertGreaterEqual(row["seq"], 1)
        self.assertIsInstance(row["ts_epoch"], float)
        self.assertEqual(row["uid"], "12345")
        self.assertEqual(row["nick"], "小明")
        self.assertEqual(row["card"], "小明明")
        self.assertEqual(row["kind"], "text")
        self.assertEqual(row["text"], "看这个[图片] 好玩")
        self.assertEqual(row["media"], ["image"])
        self.assertEqual(row["reply_to"], {"id": "123"})
        self.assertEqual(row["at"], ["999"])
        self.assertFalse(row["is_bot"])
        self.assertEqual(row["mid"], "mid-7")
        self.assertEqual(row["src"], "recorder")

    def test_timestamp_iso_seconds_and_local_offset(self):
        row = self._one_row()
        ts = row["ts"]
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$")
        self.assertNotIn(".", ts)                     # 秒精度，无毫秒
        self.assertEqual(datetime.fromisoformat(ts).microsecond, 0)
        self.assertTrue(ts.endswith("+08:00"), ts)    # 部署时区 Asia/Shanghai

    def test_ts_epoch_can_be_injected_via_ref_record(self):
        """ref_record["ts"]（事件时间）优先于墙钟，便于离线复算。"""
        group_history.record(GID, _event(message_id="mid-ts", raw_message="hi"), {"ts": 1234.0})
        self.assertEqual(self._rows()[0]["ts_epoch"], 1234.0)

    def test_image_only_message(self):
        row = self._one_row(raw_message="[CQ:image,file=x.jpg,url=https://example.com/a.jpg]")
        self.assertEqual(row["kind"], "image")
        self.assertEqual(row["text"], "[图片]")
        self.assertEqual(row["media"], ["image"])

    def test_bot_uid_flagged(self):
        row = self._one_row(
            user_id=int(BOT_UID),
            raw_message="喵",
            sender={"user_id": int(BOT_UID), "nickname": "机器人", "card": ""},
        )
        self.assertEqual(row["uid"], BOT_UID)
        self.assertTrue(row["is_bot"])

    def test_at_only_kind(self):
        row = self._one_row(raw_message="[CQ:at,qq=999]")
        self.assertEqual(row["kind"], "at_only")
        self.assertEqual(row["text"], "")
        self.assertEqual(row["at"], ["999"])

    def test_reply_to_enriched_from_message_index(self):
        message_index.remember(GID, {
            "message_id": "r1", "uid": "555", "card": "强哥",
            "nickname": "阿强", "text": "明天来不了", "ts": 1.0,
        })
        row = self._one_row(raw_message="[CQ:reply,id=r1] 好的")
        self.assertEqual(row["reply_to"],
                         {"id": "r1", "uid": "555", "nick": "强哥", "text": "明天来不了"})

        message_index.remember(GID, {
            "message_id": "r2", "uid": "556", "nickname": "阿强", "text": "长" * 300,
        })
        group_history.record(GID, _event(message_id="mid-2", raw_message="[CQ:reply,id=r2] 嗯"))
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[1]["reply_to"]["text"]), 200)

    # ---------- 过滤与健壮性 ----------

    def test_group_not_in_config_writes_nothing(self):
        group_history.record(OTHER_GID, _event(group_id=int(OTHER_GID)))
        self.assertEqual(self._rows(), [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, OTHER_GID)))

    def test_malformed_input_never_raises(self):
        for gid, data in (
            (GID, None),
            (GID, "not-a-dict"),
            (GID, 123),
            (None, {}),
            ("", {}),
            (GID, {}),
            (GID, {"user_id": None, "sender": "bad", "raw_message": None}),
            (GID, {"sender": None, "message_id": None}),
        ):
            with self.subTest(data=repr(data)[:40]):
                group_history.record(gid, data)      # 不抛即通过
        for row in self._rows():                     # 能落盘的行必须是合法 JSON 对象
            self.assertIsInstance(row, dict)

    # ---------- 落盘 ----------

    def test_jsonl_one_object_per_line_utf8_and_daily_file(self):
        row = self._one_row(raw_message="中文表情😂[CQ:image,file=x.jpg]")
        paths = glob.glob(os.path.join(self.tmp, GID, "*.jsonl"))
        self.assertEqual(len(paths), 1)
        self.assertEqual(os.path.basename(paths[0]), row["ts"][:10] + ".jsonl")
        with open(paths[0], "rb") as fh:
            raw_bytes = fh.read()
        lines = [ln for ln in raw_bytes.decode("utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)              # 一行一个 JSON 对象
        obj = json.loads(lines[0])
        self.assertEqual(obj["text"], "中文表情😂[图片]")
        self.assertTrue(raw_bytes.endswith(b"\n"))   # 行尾换行（可追加）

    def test_stats_counts(self):
        group_history.record(GID, _event(message_id="mid-a"))
        group_history.record(GID, _event(message_id="mid-b"))
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertGreater(rows[1]["seq"], rows[0]["seq"])   # 进程内序号递增
        stats = group_history.stats()
        self.assertEqual(stats["written"], 2)
        self.assertEqual(stats["dropped"], 0)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["queue"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
