# -*- coding: utf-8 -*-
"""阶段 2 离线测试：DialogueState / BehaviorPolicy / ResponseComposer /
活动 op 兼容 / 历史去重排序 / update_turn / 控制指令误判 / 主动去重。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from scripts.private_chat import activity as activity_mod
from scripts.private_chat import behavior as behavior_mod
from scripts.private_chat import config as cfgmod
from scripts.private_chat import corrections as corrections_mod
from scripts.private_chat import style as style_mod
from scripts.private_chat.dialogue_state import DialogueState, is_question_text
from scripts.private_chat.engine import PrivateChat
from scripts.private_chat.proactive import ProactiveEngine
from scripts.private_chat.response import ResponseComposer
from scripts.private_chat.store import PrivateStore

UID = "784427550"
ROOT = cfgmod.PROJECT_ROOT


def make_test_dir(prefix: str) -> str:
    """优先使用平台临时目录；沙箱拒绝临时目录写入时回退到 data/private/_test。"""
    try:
        base = tempfile.mkdtemp(prefix="qqbot_" + prefix + "_")
        probe = os.path.join(base, ".probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("x")
        os.remove(probe)
        return base
    except OSError:
        if "base" in locals():
            shutil.rmtree(base, ignore_errors=True)
        base = os.path.join(ROOT, "data", "private", "_test", prefix + os.urandom(4).hex())
        os.makedirs(base, exist_ok=True)
        return base


class FakeLogger:
    def info(self, *args, **kwargs):
        pass


def make_cfg(data_dir: str) -> dict:
    cfg = cfgmod.load_config()
    cfg["data_dir"] = data_dir
    cfg["allowed_users"] = [UID]
    cfg["history"]["enabled"] = False
    cfg["reply"].update({
        "debounce_seconds": 0.01,
        "debounce_jitter_seconds": 0.0,
        "typing_speed_chars_per_second": 1000.0,
        "max_typing_seconds": 0.01,
    })
    cfg["memory"].update({"extract_every_turns": 100, "organize_with_llm": False})
    return cfg


def msg(text="你好", mid=1001):
    return {
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "self_id": 2668851638, "user_id": int(UID), "message_id": mid,
        "raw_message": text, "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"nickname": "测试", "card": ""},
    }


class BaseTest(unittest.TestCase):
    def setUp(self):
        self.base = make_test_dir("dlg")
        self.cfg = make_cfg(self.base)
        self.store = PrivateStore(self.cfg)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)


class DialogueStateTest(BaseTest):
    def test_rebuild_clears_pending_and_restores_hints(self):
        now = datetime.now().astimezone()
        self.store.add_turn(UID, "user", "今天在上课", msg_id="1")
        self.store.add_turn(UID, "assistant", "听得怎么样？", msg_id="2",
                            meta={"turn_id": "t1", "act": "ask"})
        state = DialogueState.rebuild_from_turns(self.store.recent_turns(UID, 20))
        self.assertEqual(state.pending_user_msg_ids, [])
        self.assertTrue(state.awaiting_user)
        self.assertEqual(state.last_user_msg_id, "1")
        self.assertEqual(state.current_topic, "今天在上课")
        self.assertEqual(state.recent_bubble_acts, ["ask"])

    def test_question_and_catch(self):
        state = DialogueState()
        state.accept_user("1", "你吃了吗", "t")
        self.assertEqual(state.pending_user_msg_ids, ["1"])
        self.assertEqual(state.pending_question, "你吃了吗")
        state.mark_caught(["1"])
        self.assertEqual(state.pending_user_msg_ids, [])
        # awaiting_user 不应被用户回话清掉
        state.record_bubble("ask", "turn")
        state.accept_user("2", "吃了", "t2")
        self.assertTrue(state.awaiting_user)

    def test_is_question(self):
        self.assertTrue(is_question_text("你在干嘛？"))
        self.assertTrue(is_question_text("为什么"))
        self.assertFalse(is_question_text("今天好累"))


class BehaviorTest(BaseTest):
    def test_wait_when_queue_busy(self):
        d = behavior_mod.decide(self.cfg, DialogueState(), [{"msg_id": "1", "text": "你好"}],
                               queue_busy=True)
        self.assertEqual(d.action, "wait")

    def test_support_single_bubble(self):
        d = behavior_mod.decide(self.cfg, DialogueState(), [{"msg_id": "1", "text": "今天好难受"}])
        self.assertEqual(d.intent, "support")
        self.assertEqual(d.bubble_cap, 1)
        self.assertFalse(d.allow_question)

    def test_question_answer(self):
        state = DialogueState()
        state.accept_user("1", "这道题怎么做？", "t")
        d = behavior_mod.decide(self.cfg, state, [{"msg_id": "1", "text": "这道题怎么做？"}])
        self.assertEqual(d.intent, "answer")

    def test_follow_up(self):
        state = DialogueState()
        state.record_bubble("ask", "turn")
        d = behavior_mod.decide(self.cfg, state, [{"msg_id": "1", "text": "吃过了"}])
        self.assertEqual(d.intent, "follow_up")

    def test_no_repeat_question_after_two_asks(self):
        state = DialogueState()
        state.recent_bubble_acts = ["ask", "ask"]
        d = behavior_mod.decide(self.cfg, state, [{"msg_id": "1", "text": "嗯"}])
        self.assertFalse(d.allow_question)
        self.assertIn("no_repeat_ask", d.reason)

    def test_ai_act_valid_set(self):
        from scripts.private_chat.response import _VALID_ACTS
        self.assertIn("chat", _VALID_ACTS)
        self.assertIn("answer", _VALID_ACTS)
        self.assertNotIn("dance", _VALID_ACTS)


class ComposerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.base = make_test_dir("cmp")
        self.cfg = make_cfg(self.base)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    async def _compose(self, output, structured=True, corrections=None, intent="chat"):
        class L:
            async def __call__(self, messages, cfg):
                return output
        composer = ResponseComposer(self.cfg, FakeLogger(), llm_fn=L(), raw_fn=L())
        decision = behavior_mod.BehaviorDecision(
            action="respond", intent=intent, allow_question=True,
            allow_self_share=True, bubble_cap=2, reason="test", target_msg_ids=["1"],
        )
        rhythm = style_mod.RhythmPlan(2, 2, 60, 0, 0, "test")
        return await composer.compose(
            [{"role": "system", "content": "x"}], structured=structured,
            decision=decision, rhythm=rhythm, corrections=corrections or [],
            fallback_text="fallback",
        )

    async def test_valid_json_not_resplit(self):
        out = await self._compose('{"bubbles":[{"act":"answer","text":"第一句。"},{"act":"chat","text":"第二句。"}]}')
        self.assertEqual(len(out.bubbles), 2)
        self.assertEqual(out.bubbles[0]["text"], "第一句。")
        self.assertEqual(out.bubbles[0]["act"], "answer")

    async def test_plain_text_compat(self):
        out = await self._compose("第一句话说完了。第二句话也说完了。")
        self.assertEqual(len(out.bubbles), 2)
        self.assertEqual(out.degraded, "plain_text")

    async def test_malformed_json_not_sent(self):
        out = await self._compose('{"bubbles":[{"act":"answer","text":')
        self.assertEqual(out.bubbles[0]["text"], "fallback")
        self.assertEqual(out.degraded, "malformed_json")

    async def test_json_fragment_with_preamble_not_sent(self):
        out = await self._compose('好的，这是回复：{"bubbles":[{"act":"chat","text":"坏 JSON"')
        self.assertEqual(out.bubbles[0]["text"], "fallback")
        self.assertEqual(out.degraded, "malformed_json")

    async def test_unknown_act_normalized(self):
        out = await self._compose('{"bubbles":[{"act":"dance","text":"嗯。"}]}')
        self.assertEqual(out.bubbles[0]["act"], "chat")

    async def test_corrections_per_bubble_and_empty_removed(self):
        out = await self._compose(
            '{"bubbles":[{"act":"chat","text":"作为 AI 我不能"},{"act":"chat","text":"本小姐在"}]}',
            corrections=[{"wrong": "作为 AI", "right": ""}, {"wrong": "本小姐", "right": "本鱼"}],
        )
        texts = [x["text"] for x in out.bubbles]
        self.assertNotIn("作为 AI", " ".join(texts))
        self.assertTrue(any("本鱼" in t for t in texts))

    async def test_bubble_limit_and_length(self):
        out = await self._compose(
            '{"bubbles":[{"act":"chat","text":"一"},{"act":"chat","text":"二"},'
            '{"act":"chat","text":"三"}]}'
        )
        self.assertLessEqual(len(out.bubbles), 2)
        for bubble in out.bubbles:
            self.assertLessEqual(len(bubble["text"]), 160)


class StoreHistoryTest(BaseTest):
    def test_history_dedupe_and_sort(self):
        now = datetime.now().astimezone()
        self.store.add_turn(UID, "assistant", "本地回复", msg_id="9001",
                            meta={"source": "normal"})
        self.store.add_turn(UID, "user", "本地用户", msg_id="9002")
        old_ts = (now - timedelta(days=2)).isoformat(timespec="seconds")
        added = self.store.import_history(UID, [
            {"role": "assistant", "text": "本地回复", "ts": now.isoformat(timespec="seconds"), "msg_id": "9001"},
            {"role": "user", "text": "很久以前", "ts": old_ts, "msg_id": "7001"},
            {"role": "user", "text": "无ID", "ts": (now - timedelta(days=2, minutes=1)).isoformat(timespec="seconds"), "msg_id": None},
        ])
        self.assertEqual(added, 2)
        texts = [t["text"] for t in self.store.recent_turns(UID, 10)]
        self.assertEqual(texts[:2], ["无ID", "很久以前"])
        self.assertEqual(texts.count("本地回复"), 1)
        # 无 ID 记录再次导入应被稳定键去重
        added2 = self.store.import_history(UID, [
            {"role": "user", "text": "无ID", "ts": (now - timedelta(days=2, minutes=1)).isoformat(timespec="seconds"), "msg_id": None}
        ])
        self.assertEqual(added2, 0)

    def test_history_dedupe_survives_restart(self):
        self.store.add_turn(UID, "assistant", "本机回复", msg_id="9001")
        self.store.import_history(UID, [{
            "role": "user", "text": "无ID历史", "msg_id": None,
            "ts": "2026-10-20T10:00:00+08:00",
        }])
        # 新 store（模拟重启）仍应记住本机 msg_id 与无 ID 稳定键
        store2 = PrivateStore(self.cfg)
        added = store2.import_history(UID, [
            {"role": "assistant", "text": "本机回复", "msg_id": "9001",
             "ts": "2026-10-20T10:00:00+08:00"},
            {"role": "user", "text": "无ID历史", "msg_id": None,
             "ts": "2026-10-20T10:00:00+08:00"},
        ])
        self.assertEqual(added, 0)

    def test_history_import_keeps_latest_records_at_cache_limit(self):
        for index in range(300):
            self.store.add_turn(UID, "user", f"本地消息{index}", msg_id=str(index))
        old_ts = (datetime.now().astimezone() - timedelta(days=1)).isoformat(timespec="seconds")
        added = self.store.import_history(UID, [{
            "role": "user", "text": "更早的历史", "msg_id": "old", "ts": old_ts,
        }])
        self.assertEqual(added, 1)
        turns = self.store.recent_turns(UID, 300)
        self.assertEqual(len(turns), 300)
        self.assertEqual(turns[0]["msg_id"], "0")
        self.assertNotIn("old", {str(turn.get("msg_id")) for turn in turns})

    def test_update_turn_in_place(self):
        self.store.add_turn(UID, "user", "[图片]", msg_id="1",
                            meta={"vision_status": "pending", "original_text": "[图片]"})
        n = self.store.update_turn(UID, "1", text="[图片: 一只猫]",
                                   meta={"vision_status": "described"})
        self.assertEqual(n, 1)
        turn = [t for t in self.store.recent_turns(UID, 10) if str(t.get("msg_id")) == "1"][0]
        self.assertEqual(turn["text"], "[图片: 一只猫]")
        self.assertEqual(turn["vision_status"], "described")


class MemoryPersistenceTest(BaseTest):
    def test_duplicate_merge_is_saved_without_new_item(self):
        candidate = {"type": "user_fact", "text": "用户喜欢蓝莓", "importance": 2}
        self.store.add_memories(UID, [candidate])
        merged = dict(candidate, importance=5, source_ids=["msg-1"])
        added = self.store.add_memories(UID, [merged])
        self.assertEqual(added, [])
        reloaded = PrivateStore(self.cfg).memory_items(UID)
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0]["importance"], 5)
        self.assertEqual(reloaded[0]["source_ids"], ["msg-1"])


class ActivityCompatTest(BaseTest):
    def test_type_alias_and_chinese_period(self):
        now = datetime(2026, 10, 21, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        act = activity_mod.ActivityStore(self.store, self.cfg)
        applied = act.apply_operations(UID, [
            {"type": "add_schedule", "content": "数论", "category": "class",
             "date": "2026-10-21", "period": "下午", "confidence": 0.9},
        ], now=now)
        self.assertEqual(applied, 1)
        text = act.format_prompt(UID, now)
        self.assertIn("数论", text)
        data = act.load(UID)
        self.assertEqual(data["schedule"][0]["period"], "afternoon")

    def test_confidence_filter(self):
        now = datetime(2026, 10, 21, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        act = activity_mod.ActivityStore(self.store, self.cfg)
        applied = act.apply_operations(UID, [
            {"op": "add_schedule", "content": "低置信", "date": "2026-10-21",
             "period": "afternoon", "confidence": 0.3},
        ], now=now)
        self.assertEqual(applied, 0)


class ControlDetectTest(BaseTest):
    def test_pause_false_positive(self):
        chat = PrivateChat(FakeLogger(), cfg=self.cfg)
        self.assertTrue(chat._is_pause_request("在忙"))
        self.assertTrue(chat._is_pause_request("我先睡了"))
        self.assertTrue(chat._is_pause_request("在忙，别烦我"))
        self.assertFalse(chat._is_pause_request("你在忙什么呢"))
        self.assertFalse(chat._is_pause_request("今天上课在忙吗"))
        self.assertFalse(chat._is_pause_request("我在忙，怎么了"))
        self.assertFalse(chat._is_pause_request("在忙，啥事"))
        self.assertFalse(chat._is_pause_request("在忙，干嘛"))

    def test_control_false_positive(self):
        chat = PrivateChat(FakeLogger(), cfg=self.cfg)
        self.assertFalse(chat._is_control_command("忘记带钥匙了"))
        self.assertFalse(chat._is_control_command("别记错我的名字"))
        self.assertTrue(chat._is_control_command("忘掉猫"))
        self.assertTrue(chat._is_control_command("/关系 close"))
        self.assertTrue(chat._is_control_command("别记着昨天的事"))


class LegacyDataTest(BaseTest):
    def test_build_structured_prompt_from_old_session(self):
        from scripts.private_chat import persona as persona_mod
        self.store.add_turn(UID, "user", "旧消息", msg_id="1")
        self.store.add_turn(UID, "assistant", "旧回复", msg_id="2")
        turns = self.store.recent_turns(UID, 20)
        messages = persona_mod.build_messages(
            self.cfg, self.store, UID, "旧消息", turns=turns, structured=True,
            target_messages=[{"role": "user", "text": "旧消息", "msg_id": "1"}],
            dialogue=DialogueState(awaiting_user=True),
            behavior=behavior_mod.BehaviorDecision(
                action="respond", intent="chat", allow_question=True,
                allow_self_share=True, bubble_cap=2, reason="old", target_msg_ids=["1"],
            ),
            rhythm=style_mod.RhythmPlan(1, 2, 60, 0, 0, "old"),
        )
        system = messages[0]["content"]
        self.assertIn("输出契约", system)
        self.assertNotIn("旧消息", system)
        self.assertEqual(messages[-2:], [
            {"role": "assistant", "content": "旧回复"},
            {"role": "user", "content": "旧消息"},
        ])

    def test_multiple_targets_end_as_one_current_user_message(self):
        from scripts.private_chat import persona as persona_mod
        self.store.add_turn(UID, "assistant", "旧回复", msg_id="0")
        self.store.add_turn(UID, "user", "[图片: 一位老师]", msg_id="1")
        self.store.add_turn(UID, "user", "数论老师有点像憨豆先生", msg_id="2")
        targets = [t for t in self.store.recent_turns(UID, 20) if t.get("role") == "user"]
        messages = persona_mod.build_messages(
            self.cfg, self.store, UID, targets[-1]["text"],
            turns=self.store.recent_turns(UID, 20), structured=True,
            target_messages=targets, dialogue=DialogueState(),
        )
        self.assertEqual(messages[-1]["role"], "user")
        self.assertIn("[图片: 一位老师]", messages[-1]["content"])
        self.assertIn("数论老师有点像憨豆先生", messages[-1]["content"])
        self.assertEqual(messages[-2], {"role": "assistant", "content": "旧回复"})
        history_text = "".join(m["content"] for m in messages[1:-1])
        self.assertNotIn("数论老师有点像憨豆先生", history_text)


class ProactiveRuntimeTest(BaseTest):
    def _fake_chat(self, logger):
        chat = type("C", (), {})()
        chat.cfg = self.cfg
        chat.store = self.store
        chat.logger = logger
        chat.activity = activity_mod.ActivityStore(self.store, self.cfg, logger)
        return chat

    def test_unknown_cooldown_blocks_next_tick(self):
        engine = ProactiveEngine(self._fake_chat(FakeLogger()), FakeLogger())
        engine._on_failed(UID, "unknown")
        p = self.store.proactive_state(UID)
        self.assertTrue(engine._cooldown_active(p, __import__("time").time()))
        self.assertFalse(engine._cooldown_active(p, __import__("time").time() + 3 * 3600))

    def test_record_proactive_updates_stats(self):
        self.store.add_memories(UID, [{"type": "promise", "text": "约定", "importance": 3}])
        self.store.record_proactive(UID, "主动消息")
        p = self.store.proactive_state(UID)
        self.assertEqual(p["count"], 1)
        self.assertEqual(self.store.profile(UID)["stats"]["proactive_sent"], 1)
        self.assertIn("主动消息", p["recent_texts"])


class ProactiveDedupTest(BaseTest):
    def test_legacy_last_proactive_timestamp_still_enforces_interval(self):
        logger = FakeLogger()
        chat = type("C", (), {})()
        chat.cfg = self.cfg
        chat.store = self.store
        chat.logger = logger
        chat.activity = activity_mod.ActivityStore(self.store, self.cfg, logger)
        chat._created_at = 0

        def unexpected_runtime(uid):
            raise AssertionError("最小间隔内不应进入主动消息运行时")

        chat.get_runtime = unexpected_runtime
        self.store.profile(UID)["quiet_hours"] = [0, 0]
        self.store.state(UID)["last_proactive_at"] = __import__("time").time()
        self.store.add_memories(UID, [{
            "type": "event", "text": "周末有安排", "importance": 3, "confidence": 0.9,
        }])
        engine = ProactiveEngine(chat, logger)
        asyncio.run(engine._tick_uid(UID, __import__("time").time()))

    def test_hook_skips_recent_id_and_similar_text(self):
        logger = FakeLogger()
        chat = type("C", (), {})()
        chat.cfg = self.cfg
        chat.store = self.store
        chat.logger = logger
        chat.activity = activity_mod.ActivityStore(self.store, self.cfg, logger)
        engine = ProactiveEngine(chat, logger)
        self.store.add_memories(UID, [
            {"type": "promise", "text": "他说周末要一起试新游戏。", "importance": 4, "confidence": 0.9},
            {"type": "event", "text": "他今天在复习复变函数。", "importance": 3, "confidence": 0.9},
        ])
        first = engine._pick_hook(UID)
        self.assertIsNotNone(first)
        # 模拟已发送第一条 hook
        p = self.store.proactive_state(UID)
        p["recent_hooks"] = [{"id": first[1], "ts": __import__("time").time()}]
        p["recent_texts"] = [first[0].replace("你记得：", "")]
        self.store.save_state(UID)
        second = engine._pick_hook(UID)
        self.assertIsNotNone(second)
        self.assertNotEqual(first[1], second[1])

    def test_self_thought_hook_respects_recent_usage(self):
        logger = FakeLogger()
        chat = type("C", (), {})()
        chat.cfg = self.cfg
        chat.store = self.store
        chat.logger = logger
        chat.activity = activity_mod.ActivityStore(self.store, self.cfg, logger)
        engine = ProactiveEngine(chat, logger)
        state = self.store.self_state(UID)
        state["recent_thoughts"] = ["想聊起最近的一部电影"]
        proactive = self.store.proactive_state(UID)
        proactive["recent_hooks"] = [{"id": "self:thought", "ts": __import__("time").time()}]
        self.assertIsNone(engine._pick_hook(UID))


if __name__ == "__main__":
    unittest.main()
