# -*- coding: utf-8 -*-
"""消息指代（reference）离线单测。

跑法：
    python scripts\test_reference.py
    python -m unittest scripts.test_reference

全部离线：不连 NapCat、不调模型、不发消息。名册写到临时文件，跑完删除。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import message_index, reference as ref, roster as R  # noqa: E402

GID = "284517232"


def evt(mid, uid, nick, card, raw, ts=1000, gid=GID):
    return {
        "post_type": "message", "message_type": "group", "group_id": int(gid),
        "message_id": mid, "time": ts,
        "sender": {"user_id": int(uid), "nickname": nick, "card": card, "role": "member"},
        "raw_message": raw,
    }


class TestCqParsing(unittest.TestCase):
    RAW = "[CQ:at,qq=111] [CQ:reply,id=999] hello [CQ:image,file=x.jpg]"

    def test_extract_at(self):
        self.assertEqual(ref.extract_at(self.RAW), ["111"])

    def test_extract_at_dedup(self):
        self.assertEqual(ref.extract_at("[CQ:at,qq=1][CQ:at,qq=1][CQ:at,qq=2]"), ["1", "2"])

    def test_extract_reply_id(self):
        self.assertEqual(ref.extract_reply_id(self.RAW), "999")

    def test_strip_cq(self):
        self.assertEqual(ref.strip_cq(self.RAW), "hello")

    def test_no_cq(self):
        self.assertEqual(ref.extract_at("hello"), [])
        self.assertEqual(ref.extract_reply_id("hello"), "")
        self.assertEqual(ref.strip_cq("hello"), "hello")

    def test_strips_unterminated_cq(self):
        """实测用例：NapCat 的 CQ:face 带长 raw 参数，日志截断后没有闭合的 ]。

        若只剥闭合的 CQ 码，整段 CQ 原文会漏进模型上下文（浪费 token + 污染理解）。
        """
        broken = '[CQ:face,id=474,raw={"faceIndex":474&#44;"faceText":"hi"&#44;"packId":n'
        self.assertEqual(ref.strip_cq(broken), "")
        # 混合：完好的 + 未闭合的，两段都要清掉
        mixed = "前言 [CQ:at,qq=111] 中段 [CQ:face,id=1,raw={unclosed"
        self.assertEqual(ref.strip_cq(mixed), "前言  中段")
        # 完好的照常剥（回归）
        self.assertEqual(ref.strip_cq("[CQ:at,qq=1] hi"), "hi")


class TestAnnotate(unittest.TestCase):
    def test_basic(self):
        rec = ref.annotate(evt(1, "1305489180", "UC", "", "[CQ:at,qq=111] 别说了"))
        self.assertIsNotNone(rec)
        self.assertEqual(rec["uid"], "1305489180")
        self.assertEqual(rec["at"], ["111"])
        self.assertEqual(rec["text"], "别说了")
        self.assertEqual(rec["card"], "")
        self.assertEqual(rec["role"], "member")

    def test_card_kept(self):
        rec = ref.annotate(evt(1, "111", "往事随风", "阿强", "hi"))
        self.assertEqual(rec["card"], "阿强")
        self.assertEqual(rec["nickname"], "往事随风")

    def test_rejects_non_group_message(self):
        self.assertIsNone(ref.annotate({"post_type": "notice"}))
        self.assertIsNone(ref.annotate({"post_type": "message", "message_type": "private"}))
        self.assertIsNone(ref.annotate(None))
        self.assertIsNone(ref.annotate({}))

    def test_rejects_missing_ids(self):
        bad = evt(1, "111", "n", "", "hi")
        bad.pop("message_id")
        self.assertIsNone(ref.annotate(bad))

    def test_never_raises_on_garbage(self):
        for junk in ({"post_type": "message", "message_type": "group", "group_id": "x",
                      "message_id": "y", "sender": "not-a-dict", "raw_message": None},):
            ref.annotate(junk)  # 不抛即通过


class TestDisplayAndRender(unittest.TestCase):
    def test_display_fallback_chain(self):
        self.assertEqual(ref.display_from({"card": "阿强", "nickname": "x", "uid": "1"}), "阿强")
        self.assertEqual(ref.display_from({"card": "", "nickname": "往事随风", "uid": "1"}), "往事随风")
        self.assertEqual(ref.display_from({"card": "", "nickname": "", "uid": "123"}), "123")

    def test_plain(self):
        rec = {"uid": "1", "card": "UC", "nickname": "", "text": "今天天气不错", "at": [], "reply_id": ""}
        self.assertEqual(ref.render(rec), "[UC] 今天天气不错")

    def test_at(self):
        rec = {"uid": "1", "card": "UC", "nickname": "", "text": "别说了", "at": ["111"], "reply_id": ""}
        line = ref.render(rec, None, name_of=lambda u: "阿强" if u == "111" else u)
        self.assertEqual(line, "[UC@阿强] 别说了")

    def test_at_unknown_name_falls_back_to_uid(self):
        rec = {"uid": "1", "card": "UC", "nickname": "", "text": "hi", "at": ["999"], "reply_id": ""}
        self.assertEqual(ref.render(rec, None, name_of=lambda u: ""), "[UC@999] hi")

    def test_reply(self):
        rec = {"uid": "1", "card": "UC", "nickname": "", "text": "你说得对", "at": [], "reply_id": "101"}
        quote = {"uid": "111", "card": "阿强", "text": "我明天可能来不了"}
        self.assertEqual(ref.render(rec, quote),
                         "[UC 回复阿强「我明天可能来不了」] 你说得对")

    def test_reply_out_of_window_degrades(self):
        """超窗时必须降级，绝不编造原文。"""
        rec = {"uid": "1", "card": "UC", "nickname": "", "text": "你说得对", "at": [], "reply_id": "101"}
        self.assertEqual(ref.render(rec, None), "[UC 回复某条消息] 你说得对")

    def test_reply_quote_truncated(self):
        rec = {"uid": "1", "card": "UC", "nickname": "", "text": "x", "at": [], "reply_id": "1"}
        quote = {"uid": "2", "card": "A", "text": "y" * 200}
        self.assertIn("…", ref.render(rec, quote, quote_max_chars=10))

    def test_reply_author_not_double_at(self):
        """引用的作者已经在 @ 列表里时，不再重复 @ 一遍。"""
        rec = {"uid": "1", "card": "UC", "nickname": "", "text": "x", "at": ["111"], "reply_id": "101"}
        quote = {"uid": "111", "card": "阿强", "text": "y"}
        line = ref.render(rec, quote, name_of=lambda u: "阿强")
        self.assertEqual(line, "[UC 回复阿强「y」] x")

    def test_at_without_text(self):
        rec = {"uid": "1", "card": "UC", "nickname": "", "text": "", "at": ["111"], "reply_id": ""}
        self.assertEqual(ref.render(rec, None, name_of=lambda u: "阿强"),
                         "[UC@阿强]（只 @ 了人，没有文字）")

    def test_empty_returns_empty(self):
        rec = {"uid": "1", "card": "UC", "nickname": "", "text": "", "at": [], "reply_id": ""}
        self.assertEqual(ref.render(rec), "")

    def test_garbage_never_raises(self):
        ref.render(None)
        ref.render({})
        ref.render({"at": "not-a-list"})


class TestOutputStrip(unittest.TestCase):
    def test_strips_legacy_prefix(self):
        self.assertEqual(ref.strip_reference_prefix("[UC]: 你好"), "你好")
        self.assertEqual(ref.strip_reference_prefix("[UC]：你好"), "你好")

    def test_strips_new_prefix(self):
        self.assertEqual(ref.strip_reference_prefix("[UC@阿强] 你好"), "你好")
        self.assertEqual(ref.strip_reference_prefix("[UC 回复阿强「x」] 你好"), "你好")

    def test_does_not_touch_normal_output(self):
        """模型正常输出的 [笑] 好吧 不能被误伤。"""
        self.assertEqual(ref.strip_reference_prefix("[笑] 好吧"), "[笑] 好吧")
        self.assertEqual(ref.strip_reference_prefix("你好"), "你好")
        self.assertEqual(ref.strip_reference_prefix("[重要] 提醒一下"), "[重要] 提醒一下")


class TestMessageIndex(unittest.TestCase):
    def setUp(self):
        message_index.configure(200)

    def test_remember_and_get(self):
        rec = ref.annotate(evt(555, "1", "n", "", "hi"))
        message_index.remember(GID, rec)
        self.assertIsNotNone(message_index.get(GID, "555"))
        self.assertIsNone(message_index.get(GID, "nope"))
        self.assertIsNone(message_index.get("other-group", "555"))

    def test_ring_bound(self):
        message_index.configure(3)
        for i in range(10):
            message_index.remember(GID, ref.annotate(evt(i, "1", "n", "", "m%d" % i)))
        self.assertEqual(len(message_index.recent(GID, 100)), 3)
        message_index.configure(200)

    def test_never_raises(self):
        message_index.remember(GID, None)
        message_index.remember(GID, "not-a-dict")
        message_index.remember("", ref.annotate(evt(1, "1", "n", "", "x")))
        message_index.remember(GID, {"message_id": ""})


class TestRosterUidIsTruth(unittest.TestCase):
    """名册的三条硬规则（DESIGN.md §1）。"""

    def setUp(self):
        self.tmp = os.path.join(tempfile.gettempdir(), "roster_unittest.json")
        if os.path.exists(self.tmp):
            os.remove(self.tmp)
        self.ro = R.configure(self.tmp, retention_days=180, card_history_max=5, flush_seconds=0)

    def tearDown(self):
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def test_observe_and_display(self):
        self.assertTrue(self.ro.observe(GID, "111", card="阿强", nickname="往事随风", ts=1000))
        self.assertEqual(self.ro.display_of(GID, "111"), "阿强")

    def test_rename_keeps_uid_and_old_name(self):
        """规则 1：改名不产生新身份，旧名字仍然可查。"""
        self.ro.observe(GID, "111", card="阿强", ts=1000)
        self.ro.observe(GID, "111", card="阿强[加班]", ts=2000)
        self.assertEqual(self.ro.display_of(GID, "111"), "阿强[加班]")   # 渲染用最新的
        self.assertEqual(self.ro.resolve(GID, "阿强"), ["111"])          # 旧名仍可解析
        self.assertEqual(self.ro.resolve(GID, "阿强[加班]"), ["111"])
        self.assertEqual(self.ro.stats()["users"], 1)                    # 身份没变多

    def test_empty_card_does_not_overwrite(self):
        """实测 card 常为空串——不能把已知名片冲掉。"""
        self.ro.observe(GID, "111", card="阿强", ts=1000)
        self.ro.observe(GID, "111", card="", ts=2000)
        self.assertEqual(self.ro.display_of(GID, "111"), "阿强")

    def test_nickname_is_global_card_is_per_group(self):
        self.ro.observe("g1", "111", card="阿强", nickname="往事随风", ts=1000)
        self.ro.observe("g2", "111", card="小强", ts=1001)
        self.assertEqual(self.ro.display_of("g1", "111"), "阿强")
        self.assertEqual(self.ro.display_of("g2", "111"), "小强")
        self.assertEqual(self.ro.stats()["users"], 1)

    def test_unknown_uid_falls_back_to_uid(self):
        self.assertEqual(self.ro.display_of(GID, "999"), "999")
        self.assertEqual(self.ro.display_of(GID, ""), "")

    def test_resolve_is_ambiguous_by_design(self):
        """规则 2 的前提：反查本来就可能是多义的。"""
        self.ro.observe(GID, "111", card="阿强", ts=1000)
        self.ro.observe(GID, "222", card="阿强", ts=1001)   # 撞名
        self.assertEqual(sorted(self.ro.resolve(GID, "阿强")), ["111", "222"])
        self.assertEqual(self.ro.resolve(GID, "查无此人"), [])
        self.assertEqual(self.ro.resolve(GID, ""), [])

    def test_resolve_never_writes_back(self):
        """规则 2：反查是只读的——它绝不能改变名册。"""
        self.ro.observe(GID, "111", card="阿强", ts=1000)
        snapshot = self.ro.stats()["users"]
        for name in ("阿强", "乱猜的", "", "往事随风"):
            self.ro.resolve(GID, name)
        self.assertEqual(self.ro.stats()["users"], snapshot)
        self.assertEqual(self.ro.display_of(GID, "111"), "阿强")

    def test_observe_alone_persists(self):
        """回归：observe() 必须自己触发落盘。

        曾经的缺陷：只写内存、没有任何地方调 save()，导致名册重启即丢
        （现象是 data/roster.json 一直不生成）。
        """
        self.assertFalse(os.path.exists(self.tmp))
        self.ro.observe(GID, "111", card="阿强", ts=1000)
        self.assertTrue(os.path.exists(self.tmp), "observe 之后名册文件必须已生成")

    def test_first_observe_persists_even_with_throttle(self):
        """节流不能挡掉第一次落盘：文件不存在时 _last_flush=0，应立即写。"""
        tmp2 = os.path.join(tempfile.gettempdir(), "roster_throttle.json")
        if os.path.exists(tmp2):
            os.remove(tmp2)
        ro = R.configure(tmp2, flush_seconds=300)
        ro.observe(GID, "111", card="阿强", ts=time.time())
        self.assertTrue(os.path.exists(tmp2), "首次 observe 必须立刻落盘")
        if os.path.exists(tmp2):
            os.remove(tmp2)

    def test_pending_save_lands_when_throttle_expires(self):
        """节流挡下的待写数据，必须在节流到期后的下一次 observe 补上。

        否则场景：新面孔出现在节流窗口内（未落盘）→ 此后群里再没新人 →
        脏数据永远留在内存，进程被硬杀就丢了。
        """
        tmp3 = os.path.join(tempfile.gettempdir(), "roster_throttle2.json")
        if os.path.exists(tmp3):
            os.remove(tmp3)
        fake = [1000.0]
        ro = R.Roster(tmp3, flush_seconds=300, clock=lambda: fake[0])
        ro.load()
        ro.observe(GID, "111", card="阿强", ts=1000)      # 首次 → 立即落盘
        self.assertTrue(os.path.exists(tmp3), "首次 observe 应立即落盘")
        ro.observe(GID, "222", card="小强", ts=1100)      # 新面孔，但落在节流窗口内
        fake[0] = 1400.0                                  # 节流到期
        ro.observe(GID, "111", card="阿强", ts=1400)      # 本次无新信息，但应补写
        ro2 = R.Roster(tmp3, clock=lambda: fake[0])
        ro2.load()
        self.assertEqual(ro2.stats()["users"], 2, "节流到期后，之前被挡下的新面孔必须已落盘")
        if os.path.exists(tmp3):
            os.remove(tmp3)

    def test_persist_and_reload(self):
        self.ro.observe(GID, "111", card="阿强", nickname="往事随风", ts=1000)
        self.ro.observe(GID, "111", card="阿强[加班]", ts=2000)
        # observe 现在会自己落盘（见 test_observe_alone_persists）。
        # 此处再 force 一次只是确保最终状态在盘上——此时通常已无脏数据，
        # 所以**不对返回值断言**（返回 False 是正常的）。
        self.assertTrue(os.path.exists(self.tmp), "observe 之后应已自动落盘")
        self.ro.save(force=True)
        ro2 = R.configure(self.tmp, flush_seconds=0)
        self.assertEqual(ro2.display_of(GID, "111"), "阿强[加班]")
        self.assertEqual(ro2.resolve(GID, "阿强"), ["111"])

    def test_prune_removes_stale(self):
        self.ro.observe(GID, "111", card="阿强", ts=1000)
        self.ro.observe(GID, "222", card="小强", ts=10 ** 10)
        removed = self.ro.prune(now=10 ** 10)
        self.assertEqual(removed, 1)
        self.assertEqual(self.ro.display_of(GID, "111"), "111")   # 已被清理 → 回退 uid

    def test_corrupt_file_does_not_raise(self):
        with open(self.tmp, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        ro = R.configure(self.tmp)
        self.assertEqual(ro.stats()["users"], 0)


class TestGroupGate(unittest.TestCase):
    """按群启用：试运行期把影响面收在一个群里（config/reference.json 的 groups）。"""

    def _run(self, cfg, gid):
        orig = ref.load_config
        ref.load_config = lambda: cfg
        try:
            return ref.is_enabled_for(gid)
        finally:
            ref.load_config = orig

    def test_global_switch_off_wins(self):
        self.assertFalse(self._run({"enabled": False, "groups": ["1"]}, "1"))

    def test_empty_list_means_all_groups(self):
        self.assertTrue(self._run({"enabled": True, "groups": []}, "999"))

    def test_missing_key_means_all_groups(self):
        self.assertTrue(self._run({"enabled": True}, "999"))

    def test_allowlist_limits_to_listed_groups(self):
        cfg = {"enabled": True, "groups": ["284517232"]}
        self.assertTrue(self._run(cfg, "284517232"))
        self.assertFalse(self._run(cfg, "1055881417"))
        self.assertTrue(self._run(cfg, 284517232))   # 数字形态也要匹配

    def test_malformed_groups_fails_closed(self):
        """配置写坏时必须保守关闭 —— 不能把「写错了」变成「全群开启」。"""
        self.assertFalse(self._run({"enabled": True, "groups": "284517232"}, "999"))
        self.assertFalse(self._run({"enabled": True, "groups": 123}, "999"))


class TestEndToEnd(unittest.TestCase):
    """从 NapCat 事件到上下文行的完整链路。"""

    def setUp(self):
        ref.configure()
        self.tmp = os.path.join(tempfile.gettempdir(), "roster_e2e.json")
        if os.path.exists(self.tmp):
            os.remove(self.tmp)
        R.configure(self.tmp, flush_seconds=0)
        message_index.configure(200)

    def tearDown(self):
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def _build(self):
        from scripts.decision_engine import DecisionEngine
        engine = DecisionEngine(api_key="x", api_base_url="http://127.0.0.1:8090/v1",
                                api_model="deepseek-flash", fallback_api_key="")
        events = [
            (evt(101, "111", "往事随风", "阿强", "我明天可能来不了", 1000), "我明天可能来不了"),
            (evt(102, "1305489180", "UC", "", "[CQ:at,qq=111] 别说了", 1001), "别说了"),
            (evt(103, "1305489180", "UC", "", "[CQ:reply,id=101] 你说得对", 1002), "你说得对"),
        ]
        for e, text in events:
            rec = ref.annotate(e)
            message_index.remember(GID, rec)
            R.observe(GID, rec["uid"], rec["card"], rec["nickname"], rec["role"], rec["ts"])
            engine.add_message(GID, rec["uid"], rec["nickname"], text, ref=rec)
        return engine

    def test_full_chain_renders_reference(self):
        engine = self._build()
        out = asyncio.new_event_loop().run_until_complete(engine._format_context(GID))
        self.assertIn("[阿强] 我明天可能来不了", out)
        self.assertIn("[UC@阿强] 别说了", out)
        self.assertIn("[UC 回复阿强「我明天可能来不了」] 你说得对", out)
        # 上下文里不应出现裸 uid
        self.assertNotIn("1305489180", out)
        self.assertNotIn("[111]", out)

    def test_falls_back_when_no_ref(self):
        """无注解时，行为与改动前完全一致。"""
        from scripts.decision_engine import DecisionEngine
        engine = DecisionEngine(api_key="x", api_base_url="http://127.0.0.1:8090/v1",
                                api_model="deepseek-flash", fallback_api_key="")
        engine.add_message(GID, "111", "往事随风", "我明天可能来不了")
        out = asyncio.new_event_loop().run_until_complete(engine._format_context(GID))
        self.assertEqual(out, "[往事随风]: 我明天可能来不了")

    def test_reply_resolved_from_real_events(self):
        """引用解析必须靠别人发的消息——即使那条没被机器人回应。"""
        self._build()
        rec3 = message_index.get(GID, "103")
        quote = ref.resolve_quote(GID, rec3)
        self.assertIsNotNone(quote)
        self.assertEqual(quote["uid"], "111")
        self.assertEqual(quote["text"], "我明天可能来不了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
