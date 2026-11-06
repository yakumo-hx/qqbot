# -*- coding: utf-8 -*-
"""私聊陪伴版离线单测（不连 QQ、不调模型、不依赖网络）。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scripts.private_chat import activity as activity_mod
from scripts.private_chat import config as cfgmod
from scripts.private_chat import corrections as corrections_mod
from scripts.private_chat import image_vision
from scripts.private_chat import persona as persona_mod
from scripts.private_chat import search as search_mod
from scripts.private_chat import style as style_mod
from scripts.private_chat.engine import PrivateChat, extract_text, parse_message
from scripts.private_chat.proactive import ProactiveEngine, _in_quiet_hours
from scripts.private_chat.rpc import OneBotRPC
from scripts.private_chat.sender import split_bubbles
from scripts.private_chat.store import PrivateStore

UID = "784427550"
ROOT = cfgmod.PROJECT_ROOT


def make_test_dir(prefix: str) -> str:
    """优先临时目录；沙箱拒绝时回退 data/private/_test。"""
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


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


def make_cfg(data_dir: str) -> dict:
    cfg = cfgmod.load_config()
    cfg["data_dir"] = data_dir
    cfg["allowed_users"] = [UID]
    cfg.setdefault("persona", {})["mode"] = "card"
    cfg["reply"].update({
        "debounce_seconds": 0.01,
        "debounce_jitter_seconds": 0.0,
        "typing_speed_chars_per_second": 1000.0,
        "max_typing_seconds": 0.01,
        "max_bubbles": 2,
        "fallback_on_error": True,
    })
    cfg["memory"].update({"extract_every_turns": 100, "organize_with_llm": False})
    cfg["proactive"].update({
        "startup_grace_seconds": 0,
        "quiet_hours": [0, 0],
        "silence_hours": 0,
        "daily_min": 3,
        "daily_max": 5,
        "min_interval_seconds": 0,
        "tick_seconds": 3600,
    })
    return cfg


class FakeLLM:
    def __init__(self, reply="收到。"):
        self.reply = reply
        self.calls = 0

    async def __call__(self, messages, cfg):
        self.calls += 1
        return self.reply


class FakeJSON:
    def __init__(self, data=None):
        self.data = data or {"memories": []}

    async def __call__(self, messages, cfg):
        return self.data


class FakeRPC:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    async def call(self, websocket, action, params, timeout=10.0):
        self.calls.append((action, params))
        return {"messages": list(self.messages)}


class PrivateChatTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = make_test_dir("case")
        self.cfg = make_cfg(self.tmp)
        self.chat = None

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _stop(self):
        if self.chat is not None:
            await self.chat.stop()
            self.chat = None

    def _msg(self, text="你好", uid=UID, mid=1001):
        return {
            "post_type": "message", "message_type": "private", "sub_type": "friend",
            "self_id": 2668851638, "user_id": int(uid), "message_id": mid,
            "raw_message": text, "message": [{"type": "text", "data": {"text": text}}],
            "sender": {"nickname": "测试", "card": ""},
        }

    def _image_msg(self, text="看看", uid=UID, mid=2001, sub_type=0, summary=""):
        return {
            "post_type": "message", "message_type": "private", "sub_type": "friend",
            "self_id": 2668851638, "user_id": int(uid), "message_id": mid,
            "raw_message": f"[CQ:image,file=cat.jpg,sub_type={sub_type},summary={summary}]" + text,
            "message": [
                {"type": "image", "data": {
                    "file": "cat.jpg", "url": "https://example.com/cat.jpg",
                    "sub_type": sub_type, "summary": summary, "file_size": 12345,
                }},
                {"type": "text", "data": {"text": text}},
            ],
            "sender": {"nickname": "测试", "card": ""},
        }

    async def test_vision_describe_preferred(self):
        from scripts import vision as vision_mod

        captured = {}

        async def fake_chat(messages, **kwargs):
            captured.update(kwargs)
            return "一只猫", "fake-model"

        with patch("scripts.vision.chat", new=fake_chat):
            ok, desc = await vision_mod.describe(
                "data:image/jpeg;base64,eA==", "看看",
                preferred="glm-5.3-flash", timeout=60,
            )
        self.assertTrue(ok)
        self.assertEqual(desc, "一只猫")
        self.assertEqual(captured.get("preferred"), "glm-5.3-flash")
        self.assertEqual(captured.get("timeout"), 60)

    async def test_image_rate_limit(self):
        image_vision.reset_rate()
        cfg = make_cfg(self.tmp)
        cfg["image"]["max_per_hour"] = 1
        meta = {"file": "cat.jpg", "url": "https://example.com/cat.jpg",
                "sub_type": 0, "file_size": 123}

        async def fake_desc(url, hint):
            return True, "一只猫"

        with patch("scripts.private_chat.image_vision.image_cache.select_candidates",
                   return_value=([meta], "当前消息")), \
             patch("scripts.private_chat.image_vision.image_cache.seen_hash", return_value=False):
            first = await image_vision.describe_turn(
                "rate-test-user", "看看", [meta], "看看", cfg, FakeLogger(),
                fetch=lambda m: b"x", encode=lambda b: "data:image/jpeg;base64,eA==",
                describe=fake_desc,
            )
            second = await image_vision.describe_turn(
                "rate-test-user", "再看看", [meta], "再看看", cfg, FakeLogger(),
                fetch=lambda m: b"x", encode=lambda b: "data:image/jpeg;base64,eA==",
                describe=fake_desc,
            )
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        image_vision.reset_rate()

    async def test_image_vision_module(self):
        image_vision.reset_rate()
        cfg = make_cfg(self.tmp)
        meta = {"file": "cat.jpg", "url": "https://example.com/cat.jpg",
                "sub_type": 0, "file_size": 123}

        async def fake_desc(url, hint):
            return True, "一只猫"

        with patch("scripts.private_chat.image_vision.image_cache.select_candidates",
                   return_value=([meta], "当前消息")), \
             patch("scripts.private_chat.image_vision.image_cache.seen_hash", return_value=False):
            result = await image_vision.describe_turn(
                "image-test-user", "看看", [meta], "看看", cfg, FakeLogger(),
                fetch=lambda m: b"fake-image-bytes",
                encode=lambda b: "data:image/jpeg;base64,eA==",
                describe=fake_desc,
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["desc"], "一只猫")
        self.assertEqual(result["source"], "当前消息")

    async def test_sensitive_placeholder_is_not_valid_description(self):
        from scripts import vision as vision_mod

        image_vision.reset_rate()
        cfg = make_cfg(self.tmp)
        meta = {"file": "person.jpg", "url": "https://example.com/person.jpg",
                "sub_type": 0, "file_size": 123}

        async def fake_desc(url, hint):
            return True, vision_mod.SENSITIVE_REPLY

        with patch("scripts.private_chat.image_vision.image_cache.select_candidates",
                   return_value=([meta], "本消息")), \
             patch("scripts.private_chat.image_vision.image_cache.seen_hash", return_value=False):
            result = await image_vision.describe_turn(
                "sensitive-test-user", "[图片]", [meta], "[CQ:image,file=person.jpg]",
                cfg, FakeLogger(), fetch=lambda m: b"x",
                encode=lambda b: "data:image/jpeg;base64,eA==", describe=fake_desc,
            )
        self.assertEqual(result["desc"], "")
        self.assertEqual(result["skipped"], "blocked")

    async def test_duplicate_image_reuses_valid_description(self):
        image_vision.reset_rate()
        cfg = make_cfg(self.tmp)
        meta = {"file": "teacher.jpg", "url": "https://example.com/teacher.jpg",
                "sub_type": 0, "file_size": 123}
        calls = {"n": 0}

        async def fake_desc(url, hint):
            calls["n"] += 1
            return True, "一位戴圆框眼镜的老师"

        with patch("scripts.private_chat.image_vision.image_cache.select_candidates",
                   side_effect=[([meta], "本消息"), ([meta], "引用")]), \
             patch("scripts.private_chat.image_vision.image_cache.seen_hash",
                   side_effect=[False, True]):
            first = await image_vision.describe_turn(
                "cache-test-user", "[图片]", [meta], "[CQ:image,file=teacher.jpg]",
                cfg, FakeLogger(), fetch=lambda m: b"x",
                encode=lambda b: "data:image/jpeg;base64,eA==", describe=fake_desc,
            )
            second = await image_vision.describe_turn(
                "cache-test-user", "[引用]这张怎么样", [], "[CQ:reply,id=1]这张怎么样",
                cfg, FakeLogger(), fetch=lambda m: b"x",
                encode=lambda b: "data:image/jpeg;base64,eA==", describe=fake_desc,
            )
        self.assertEqual(first["desc"], "一位戴圆框眼镜的老师")
        self.assertEqual(second["desc"], first["desc"])
        self.assertEqual(calls["n"], 1)

    async def test_duplicate_image_retries_with_new_text_after_blocked(self):
        from scripts import vision as vision_mod

        image_vision.reset_rate()
        cfg = make_cfg(self.tmp)
        meta = {"file": "teacher.jpg", "url": "https://example.com/teacher.jpg",
                "sub_type": 0, "file_size": 123}
        answers = iter([vision_mod.SENSITIVE_REPLY, "一位戴圆框眼镜的老师"])
        calls = {"n": 0}

        async def fake_desc(url, hint):
            calls["n"] += 1
            return True, next(answers)

        with patch("scripts.private_chat.image_vision.image_cache.select_candidates",
                   side_effect=[([meta], "本消息"), ([meta], "引用")]), \
             patch("scripts.private_chat.image_vision.image_cache.seen_hash",
                   side_effect=[False, True]):
            first = await image_vision.describe_turn(
                "retry-test-user", "[图片]", [meta], "[CQ:image,file=teacher.jpg]",
                cfg, FakeLogger(), fetch=lambda m: b"x",
                encode=lambda b: "data:image/jpeg;base64,eA==", describe=fake_desc,
            )
            second = await image_vision.describe_turn(
                "retry-test-user", "[引用]数论老师有点像憨豆先生", [],
                "[CQ:reply,id=1]数论老师有点像憨豆先生",
                cfg, FakeLogger(), fetch=lambda m: b"x",
                encode=lambda b: "data:image/jpeg;base64,eA==", describe=fake_desc,
            )
        self.assertEqual(first["skipped"], "blocked")
        self.assertEqual(second["desc"], "一位戴圆框眼镜的老师")
        self.assertEqual(calls["n"], 2)

    async def test_sticker_recognized(self):
        image_vision.reset_rate()
        cfg = make_cfg(self.tmp)
        cfg["image"]["recognize_stickers"] = True
        cfg["image"]["sticker_max_per_hour"] = 1
        meta = {"file": "sticker.jpg", "url": "https://example.com/s.jpg",
                "sub_type": 1, "summary": "[动画表情]", "file_size": 123}
        calls = {"n": 0}

        async def fake_desc(url, hint):
            calls["n"] += 1
            return True, "猫猫震惊"

        with patch("scripts.private_chat.image_vision.image_cache.select_candidates",
                   return_value=([meta], "当前消息")), \
             patch("scripts.private_chat.image_vision.image_cache.seen_hash", return_value=False):
            first = await image_vision.describe_turn(
                "sticker-test-user", "看看", [meta], "看看", cfg, FakeLogger(),
                fetch=lambda m: b"x", encode=lambda b: "data:image/jpeg;base64,eA==",
                describe=fake_desc,
            )
            second = await image_vision.describe_turn(
                "sticker-test-user", "再看看", [meta], "再看看", cfg, FakeLogger(),
                fetch=lambda m: b"x", encode=lambda b: "data:image/jpeg;base64,eA==",
                describe=fake_desc,
            )
        self.assertIsNotNone(first)
        self.assertEqual(first["kind"], "sticker")
        self.assertEqual(first["desc"], "猫猫震惊")
        self.assertIsNone(second)  # 贴纸桶每小时 1 张
        self.assertEqual(calls["n"], 1)
        image_vision.reset_rate()

    async def test_sticker_disabled(self):
        image_vision.reset_rate()
        cfg = make_cfg(self.tmp)
        cfg["image"]["recognize_stickers"] = False
        meta = {"file": "sticker.jpg", "url": "https://example.com/s.jpg",
                "sub_type": 1, "summary": "[动画表情]", "file_size": 123}
        called = {"n": 0}

        async def fake_desc(url, hint):
            called["n"] += 1
            return True, "猫猫震惊"

        with patch("scripts.private_chat.image_vision.image_cache.select_candidates",
                   return_value=([meta], "当前消息")), \
             patch("scripts.private_chat.image_vision.image_cache.seen_hash", return_value=False):
            result = await image_vision.describe_turn(
                "sticker-off-user", "看看", [meta], "看看", cfg, FakeLogger(),
                fetch=lambda m: b"x", encode=lambda b: "data:image/jpeg;base64,eA==",
                describe=fake_desc,
            )
        self.assertIsNone(result)
        self.assertEqual(called["n"], 0)
        image_vision.reset_rate()

    async def test_rpc_call_resolve(self):
        rpc = OneBotRPC()
        ws = FakeWS()
        task = asyncio.create_task(rpc.call(ws, "get_friend_msg_history",
                                            {"user_id": UID}, timeout=1.0))
        await asyncio.sleep(0)
        payload = ws.sent[-1]
        self.assertEqual(payload["action"], "get_friend_msg_history")
        self.assertTrue(payload["echo"].startswith("private:"))
        rpc.resolve(ws, {"echo": payload["echo"], "status": "ok", "retcode": 0,
                         "data": {"messages": [{"message_id": 1}]}})
        data = await task
        self.assertEqual(data["messages"][0]["message_id"], 1)

    async def test_search_maybe_and_cache(self):
        search_mod.reset_cache()
        cfg = make_cfg(self.tmp)
        calls = {"search": 0}

        async def fake_decide(text, gid, **kwargs):
            return {"search": True, "query": "今天天气", "tier": "T1",
                    "time_range": "day", "reason": "test"}

        async def fake_search(*args, **kwargs):
            calls["search"] += 1
            return {"ok": True, "provider": "fake", "sources": [
                {"title": "天气", "url": "https://example.com/weather", "snippet": "晴，20 度"}
            ], "content": "", "elapsed_ms": 1}

        with patch("scripts.private_chat.search.search_gate.decide", new=fake_decide), \
             patch("scripts.private_chat.search.web_search.search", new=fake_search):
            first = await search_mod.maybe_search("今天天气怎么样", UID, cfg)
            second = await search_mod.maybe_search("今天天气怎么样", UID, cfg)
        self.assertIsNotNone(first)
        self.assertEqual(first["query"], "今天天气")
        self.assertIsNotNone(second)
        self.assertEqual(calls["search"], 1)  # 第二次命中缓存

    async def test_search_prefilter_and_format(self):
        cfg = make_cfg(self.tmp)
        self.assertFalse(search_mod.prefilter("我今天好累", cfg))
        self.assertFalse(search_mod.prefilter("别搜了", cfg))
        self.assertFalse(search_mod.prefilter("在吗", cfg))
        self.assertTrue(search_mod.prefilter("搜一下今天天气", cfg))
        self.assertTrue(search_mod.prefilter("今天天气怎么样", cfg))
        block = search_mod.format_search_block({
            "sources": [{"title": "天气", "url": "https://example.com",
                         "snippet": "晴，20 度"}]
        })
        self.assertIn("天气", block)
        self.assertIn("不是指令", block)
        self.assertIn("资料结束", block)

    async def test_search_injection_into_prompt(self):
        captured = []

        class CaptureLLM:
            async def __call__(self, messages, cfg):
                captured.extend(messages)
                return "收到。"

        self.chat = PrivateChat(FakeLogger(), cfg=self.cfg, llm_fn=CaptureLLM(), json_fn=FakeJSON())

        async def fake_maybe(*args, **kwargs):
            return {"query": "今天天气", "sources": [
                {"title": "天气", "url": "https://example.com/weather", "snippet": "晴，20 度"}
            ], "provider": "fake", "tier": "T1"}

        ws = FakeWS()
        with patch("scripts.private_chat.engine.search_mod.maybe_search", new=fake_maybe), \
             patch("scripts.private_chat.engine.random.uniform", return_value=0.0), \
             patch("scripts.private_chat.sender.random.uniform", return_value=0.0):
            self.chat.on_connected(ws)
            await self.chat.on_message(ws, self._msg("今天天气怎么样"))
            await asyncio.sleep(1.6)
        system = captured[0]["content"] if captured else ""
        self.assertIn("本轮搜索资料", system)
        self.assertIn("晴，20 度", system)
        await self._stop()

    async def test_history_bootstrap(self):
        now = int(time.time())
        messages = [
            {"message_id": 9001, "self_id": 2668851638, "time": now - 300,
             "sender": {"user_id": int(UID), "nickname": "测试"},
             "message": [{"type": "text", "data": {"text": "连上之前说的话"}}],
             "raw_message": "连上之前说的话"},
            {"message_id": 9002, "self_id": 2668851638, "time": now - 200,
             "sender": {"user_id": 2668851638, "nickname": "bot"},
             "message": [{"type": "text", "data": {"text": "连上之前的回复"}}],
             "raw_message": "连上之前的回复"},
        ]
        rpc = FakeRPC(messages)
        self.cfg["history"].update({"enabled": True, "on_connect": True,
                                    "count": 20, "min_interval_seconds": 0})
        self.chat = PrivateChat(FakeLogger(), cfg=self.cfg, llm_fn=FakeLLM(), json_fn=FakeJSON(), rpc=rpc)
        ws = FakeWS()
        self.chat.on_connected(ws)
        await asyncio.sleep(0.2)
        turns = self.chat.store.recent_turns(UID, 10)
        texts = [str(t.get("text") or "") for t in turns]
        self.assertIn("连上之前说的话", texts)
        self.assertIn("连上之前的回复", texts)
        self.assertTrue(any(t.get("role") == "assistant" for t in turns))
        self.assertEqual(rpc.calls[0][0], "get_friend_msg_history")
        self.assertEqual(rpc.calls[0][1]["user_id"], UID)
        # 再拉一次应全部去重
        await self.chat._bootstrap_history(ws)
        texts2 = [str(t.get("text") or "") for t in self.chat.store.recent_turns(UID, 20)]
        self.assertEqual(texts2.count("连上之前说的话"), 1)
        await self._stop()

    async def test_image_message_real_pipeline(self):
        image_vision.reset_rate()
        self.chat = PrivateChat(FakeLogger(), cfg=self.cfg, llm_fn=FakeLLM("看到了。"), json_fn=FakeJSON())
        ws = FakeWS()

        async def fake_vision(data_url, hint="", **kwargs):
            return True, "一只猫趴在键盘上"

        with patch("scripts.private_chat.image_vision.image_utils.fetch_image",
                   return_value=b"fake-image-bytes"), \
             patch("scripts.private_chat.image_vision.image_utils.to_data_url",
                   return_value="data:image/jpeg;base64,eA=="), \
             patch("scripts.private_chat.image_vision.vision.describe", new=fake_vision), \
             patch("scripts.private_chat.engine.random.uniform", return_value=0.0), \
             patch("scripts.private_chat.sender.random.uniform", return_value=0.0):
            self.chat.on_connected(ws)
            await self.chat.on_message(ws, self._image_msg())
            await asyncio.sleep(1.6)
        turns = self.chat.store.recent_turns(UID, 5)
        self.assertTrue(any("一只猫趴在键盘上" in str(t.get("text")) for t in turns))
        await self._stop()

    async def test_image_message_vision(self):
        self.chat = PrivateChat(FakeLogger(), cfg=self.cfg, llm_fn=FakeLLM("看到了。"), json_fn=FakeJSON())
        ws = FakeWS()

        async def fake_describe(*args, **kwargs):
            return {"desc": "一只猫在键盘上", "source": "当前消息", "signature": "cat.jpg"}

        with patch("scripts.private_chat.engine.image_vision.describe_turn", new=fake_describe), \
             patch("scripts.private_chat.engine.random.uniform", return_value=0.0), \
             patch("scripts.private_chat.sender.random.uniform", return_value=0.0):
            self.chat.on_connected(ws)
            await self.chat.on_message(ws, self._image_msg())
            await asyncio.sleep(1.6)
        turns = self.chat.store.recent_turns(UID, 5)
        self.assertTrue(any("一只猫在键盘上" in str(t.get("text")) for t in turns))
        await self._stop()

    async def test_image_message_sticker(self):
        self.chat = PrivateChat(FakeLogger(), cfg=self.cfg, llm_fn=FakeLLM("看到了。"), json_fn=FakeJSON())
        ws = FakeWS()

        async def fake_describe(*args, **kwargs):
            return {"desc": "猫猫震惊", "source": "当前消息",
                    "signature": "sticker", "kind": "sticker"}

        with patch("scripts.private_chat.engine.image_vision.describe_turn", new=fake_describe), \
             patch("scripts.private_chat.engine.random.uniform", return_value=0.0), \
             patch("scripts.private_chat.sender.random.uniform", return_value=0.0):
            self.chat.on_connected(ws)
            await self.chat.on_message(ws, self._image_msg(sub_type=1, summary="[动画表情]"))
            await asyncio.sleep(1.6)
        turns = self.chat.store.recent_turns(UID, 5)
        self.assertTrue(any("[表情: 猫猫震惊]" in str(t.get("text")) for t in turns))
        await self._stop()

    async def test_activity_extract_from_chat(self):
        now = datetime.now().astimezone()
        start = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        end = (now + timedelta(hours=1)).isoformat(timespec="seconds")

        async def activity_json(messages, cfg):
            return {"operations": [{
                "op": "set_current", "content": "上课", "category": "class",
                "start_at": start, "end_at": end, "confidence": 0.95,
            }]}

        self.chat = PrivateChat(FakeLogger(), cfg=self.cfg, llm_fn=FakeLLM("嗯。"),
                                json_fn=activity_json)
        ws = FakeWS()
        with patch("scripts.private_chat.engine.random.uniform", return_value=0.0), \
             patch("scripts.private_chat.sender.random.uniform", return_value=0.0):
            self.chat.on_connected(ws)
            await self.chat.on_message(ws, self._msg("我现在在上课"))
            await asyncio.sleep(0.4)
        current = self.chat.activity.current(UID, now)
        self.assertIsNotNone(current)
        self.assertEqual(current["content"], "上课")
        await self._stop()

    async def test_activity_command(self):
        self.chat = PrivateChat(FakeLogger(), cfg=self.cfg, llm_fn=FakeLLM("嗯。"), json_fn=FakeJSON())
        now = datetime.now().astimezone()
        self.chat.activity.set_current(
            UID, "上课", category="class",
            start_at=(now - timedelta(minutes=10)).isoformat(timespec="seconds"),
            end_at=(now + timedelta(minutes=50)).isoformat(timespec="seconds"),
            now=now,
        )
        ws = FakeWS()
        with patch("scripts.private_chat.engine.random.uniform", return_value=0.0), \
             patch("scripts.private_chat.sender.random.uniform", return_value=0.0):
            self.chat.on_connected(ws)
            await self.chat.on_message(ws, self._msg("/活动"))
            await asyncio.sleep(0.1)
        actions = [x for x in ws.sent if x.get("action") == "send_private_msg"]
        self.assertTrue(actions)
        self.assertIn("上课", json.dumps(actions[0]["params"], ensure_ascii=False))
        await self._stop()

    async def test_private_reply(self):
        fake = FakeLLM("嗯，我在。")
        self.chat = PrivateChat(FakeLogger(), cfg=self.cfg, llm_fn=fake, json_fn=FakeJSON())
        ws = FakeWS()
        with patch("scripts.private_chat.engine.random.uniform", return_value=0.0), \
             patch("scripts.private_chat.sender.random.uniform", return_value=0.0):
            self.chat.on_connected(ws)
            await self.chat.on_message(ws, self._msg("在吗"))
            await asyncio.sleep(1.6)
        actions = [x for x in ws.sent if x.get("action") == "send_private_msg"]
        self.assertTrue(actions, "应该发出一条私聊消息")
        self.assertEqual(actions[0]["params"]["user_id"], int(UID))
        self.assertTrue(actions[0]["params"]["message"][0]["data"]["text"])
        self.assertGreaterEqual(fake.calls, 1)
        await self._stop()

    async def test_whitelist(self):
        fake = FakeLLM()
        self.chat = PrivateChat(FakeLogger(), cfg=self.cfg, llm_fn=fake, json_fn=FakeJSON())
        ws = FakeWS()
        self.chat.on_connected(ws)
        await self.chat.on_message(ws, self._msg("你好", uid="123456"))
        await asyncio.sleep(0.15)
        self.assertEqual([x for x in ws.sent if x.get("action") == "send_private_msg"], [])
        self.assertEqual(fake.calls, 0)
        await self._stop()

    async def test_friend_request_whitelist(self):
        self.chat = PrivateChat(FakeLogger(), cfg=self.cfg, llm_fn=FakeLLM(), json_fn=FakeJSON())
        ws = FakeWS()
        data = {"post_type": "request", "request_type": "friend", "user_id": int(UID), "flag": "abc"}
        await self.chat.on_friend_request(ws, data)
        self.assertEqual(ws.sent[0]["action"], "set_friend_add_request")
        self.assertTrue(ws.sent[0]["params"]["approve"])

    async def test_pause_keyword(self):
        self.chat = PrivateChat(FakeLogger(), cfg=self.cfg, llm_fn=FakeLLM(), json_fn=FakeJSON())
        ws = FakeWS()
        with patch("scripts.private_chat.engine.random.uniform", return_value=0.0), \
             patch("scripts.private_chat.sender.random.uniform", return_value=0.0):
            self.chat.on_connected(ws)
            await self.chat.on_message(ws, self._msg("在忙，别烦我"))
            await asyncio.sleep(0.05)
        prof = self.chat.store.profile(UID)
        self.assertTrue(prof["proactive"]["paused_until"])
        actions = [x for x in ws.sent if x.get("action") == "send_private_msg"]
        self.assertEqual(len(actions), 1)
        await self._stop()

    async def test_memory_extract(self):
        self.chat = PrivateChat(FakeLogger(), cfg=self.cfg, llm_fn=FakeLLM(), json_fn=FakeJSON({
            "memories": [{"type": "user_fact", "text": "他喜欢测试。", "importance": 3,
                          "confidence": 0.9, "source_ids": [1]}]
        }))
        self.chat.cfg["memory"]["extract_every_turns"] = 1
        self.chat.store.add_turn(UID, "user", "之前聊过一点。")
        self.chat.store.add_turn(UID, "assistant", "嗯，记得。")
        ws = FakeWS()
        with patch("scripts.private_chat.engine.random.uniform", return_value=0.0), \
             patch("scripts.private_chat.sender.random.uniform", return_value=0.0):
            self.chat.on_connected(ws)
            await self.chat.on_message(ws, self._msg("记住，我喜欢测试"))
            await asyncio.sleep(2.0)
        items = self.chat.store.memory_items(UID)
        self.assertTrue(any("测试" in x["text"] for x in items))
        await self._stop()

    async def test_burst_reply_two_bubbles(self):
        fake = FakeLLM("第一句话说完了。第二句话也说完了。")
        self.chat = PrivateChat(FakeLogger(), cfg=self.cfg, llm_fn=fake, json_fn=FakeJSON())
        ws = FakeWS()
        with patch("scripts.private_chat.engine.random.uniform", return_value=0.0), \
             patch("scripts.private_chat.sender.random.uniform", return_value=0.0):
            self.chat.on_connected(ws)
            for mid in (2001, 2002, 2003, 2004):
                await self.chat.on_message(ws, self._msg("短句", mid=mid))
            await asyncio.sleep(3.0)
        actions = [x for x in ws.sent if x.get("action") == "send_private_msg"]
        self.assertGreaterEqual(len(actions), 2)
        await self._stop()

    async def test_relationship_command(self):
        self.chat = PrivateChat(FakeLogger(), cfg=self.cfg, llm_fn=FakeLLM(), json_fn=FakeJSON())
        ws = FakeWS()
        with patch("scripts.private_chat.engine.random.uniform", return_value=0.0), \
             patch("scripts.private_chat.sender.random.uniform", return_value=0.0):
            self.chat.on_connected(ws)
            await self.chat.on_message(ws, self._msg("/关系 close"))
            await asyncio.sleep(0.05)
        self.assertEqual(self.chat.store.profile(UID)["relationship"]["stage"], "close")
        self.assertTrue(any("亲近" in str(x.get("params")) for x in ws.sent if x.get("action") == "send_private_msg"))
        await self._stop()

    async def test_correction_command(self):
        private_dir = os.path.join(self.tmp, "private")
        os.makedirs(private_dir, exist_ok=True)
        self.chat = PrivateChat(FakeLogger(), cfg=self.cfg, llm_fn=FakeLLM(), json_fn=FakeJSON())
        ws = FakeWS()
        with patch("scripts.private_chat.persona.private_dir", return_value=private_dir), \
             patch("scripts.private_chat.engine.random.uniform", return_value=0.0), \
             patch("scripts.private_chat.sender.random.uniform", return_value=0.0):
            self.chat.on_connected(ws)
            await self.chat.on_message(ws, self._msg("/纠偏 本小姐 => 本鱼"))
            await asyncio.sleep(0.1)
        path = os.path.join(private_dir, "corrections.json")
        self.assertTrue(os.path.exists(path))
        items = corrections_mod.load(path)
        self.assertEqual(items[0]["wrong"], "本小姐")
        self.assertEqual(items[0]["right"], "本鱼")
        await self._stop()

    async def test_proactive_send(self):
        fake = FakeLLM("刚想到你，今天还顺利吗")
        self.chat = PrivateChat(FakeLogger(), cfg=self.cfg, llm_fn=fake, json_fn=FakeJSON())
        ws = FakeWS()
        self.chat._ws = ws
        self.chat._created_at = time.time() - 3600
        st = self.chat.store
        st.update_profile(UID, last_user_at="2026-01-01T00:00:00+08:00",
                          proactive={"paused_until": None})
        st.add_memories(UID, [{"type": "promise", "text": "他说周末要一起试新游戏。",
                               "importance": 4, "confidence": 0.9}])
        st.self_state(UID)["today"] = "今天在整理东西。"
        st.save_self_state(UID)
        engine = ProactiveEngine(self.chat, FakeLogger())
        with patch("scripts.private_chat.sender.random.uniform", return_value=0.0), \
             patch("scripts.private_chat.proactive.random.random", return_value=0.0):
            await engine.tick()
        actions = [x for x in ws.sent if x.get("action") == "send_private_msg"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(st.proactive_state(UID)["count"], 1)


class FakeLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class ActivityTest(unittest.TestCase):
    def setUp(self):
        self.base = make_test_dir("act")
        self.cfg = make_cfg(self.base)
        self.store = PrivateStore(self.cfg)
        self.act = activity_mod.ActivityStore(self.store, self.cfg)
        self.now = datetime(2026, 10, 21, 10, 0, tzinfo=timezone(timedelta(hours=8)))

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_current_and_prompt(self):
        self.act.set_current(
            UID, "上课", category="class",
            start_at="2026-10-21T08:00:00+08:00",
            end_at="2026-10-21T12:10:00+08:00",
            now=self.now,
        )
        text = self.act.format_prompt(UID, self.now)
        self.assertIn("【对方现在】上课", text)
        self.assertIn("还剩", text)
        self.assertIn("忙碌", text)
        self.assertTrue(self.act.current(UID, self.now))
        self.assertIsNone(self.act.current(UID, datetime(2026, 10, 21, 12, 30, tzinfo=timezone(timedelta(hours=8)))))

    def test_period_schedule(self):
        self.act.add_schedule(
            UID, "数论", category="class", day="2026-10-21",
            period="afternoon", precision="period", now=self.now,
        )
        text = self.act.format_prompt(UID, self.now)
        self.assertIn("今天下午 数论", text)
        cur = self.act.current(UID, datetime(2026, 10, 21, 13, 0, tzinfo=timezone(timedelta(hours=8))))
        self.assertIsNotNone(cur)
        self.assertEqual(cur["content"], "数论")

    def test_recurring_schedule(self):
        self.act.add_schedule(
            UID, "早课", category="class", day="2026-10-19",
            start_at="2026-10-19T08:00:00+08:00",
            end_at="2026-10-19T10:00:00+08:00",
            recurrence={"type": "weekly", "weekdays": [1], "until": "2026-12-31"},
            now=self.now,
        )
        monday = datetime(2026, 10, 26, 9, 0, tzinfo=timezone(timedelta(hours=8)))
        cur = self.act.current(UID, monday)
        self.assertIsNotNone(cur)
        self.assertEqual(cur["content"], "早课")

    def test_apply_operations_and_close(self):
        applied = self.act.apply_operations(UID, [
            {"op": "set_current", "content": "写作业", "category": "study",
             "start_at": "2026-10-21T09:00:00+08:00",
             "end_at": "2026-10-21T11:00:00+08:00", "confidence": 0.9},
            {"op": "add_schedule", "content": "合唱团", "category": "social",
             "date": "2026-10-21", "period": "evening", "confidence": 0.8},
        ], now=self.now)
        self.assertEqual(applied, 2)
        self.assertIn("写作业", self.act.format_prompt(UID, self.now))
        self.assertTrue(self.act.close_current(UID, "写完了", now=self.now))
        data = self.act.load(UID)
        self.assertIsNone(data["current"])
        self.assertEqual(data["history"][-1]["content"], "写作业")

    def test_proactive_factor(self):
        self.act.set_current(
            UID, "考试", category="exam",
            start_at="2026-10-21T09:00:00+08:00",
            end_at="2026-10-21T11:00:00+08:00", now=self.now,
        )
        self.assertLess(self.act.proactive_factor(UID, self.now), 1.0)
        self.act.close_current(UID, "考完", now=self.now)
        self.assertEqual(self.act.proactive_factor(UID, self.now), 1.0)

    def test_persona_activity_block(self):
        text = persona_mod.build_system_prompt(
            self.cfg, self.store, UID,
            activity="【对方现在】上课（08:00–12:10；忙碌）",
        )
        self.assertIn("【对方的状态】", text)
        self.assertIn("上课", text)


class PureTest(unittest.TestCase):
    def test_extract_text(self):
        data = {"message": [
            {"type": "text", "data": {"text": "你好"}},
            {"type": "image", "data": {}},
            {"type": "face", "data": {"id": "1"}},
        ]}
        text = extract_text(data)
        self.assertIn("你好", text)
        self.assertIn("[图片]", text)
        self.assertIn("[表情]", text)

    def test_parse_message_images_and_enrich(self):
        data = {"message": [
            {"type": "image", "data": {"file": "cat.jpg", "url": "https://example.com/cat.jpg",
                                       "sub_type": 0, "file_size": 123}},
            {"type": "text", "data": {"text": "看看"}},
        ], "raw_message": "[CQ:image,file=cat.jpg,url=https://example.com/cat.jpg]看看"}
        text, images = parse_message(data)
        self.assertIn("[图片]", text)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["url"], "https://example.com/cat.jpg")
        self.assertEqual(image_vision.enrich_text(text, "一只猫"), "[图片: 一只猫]看看")
        self.assertEqual(image_vision.enrich_text("你看到图了吗", "一只猫"),
                         "你看到图了吗\n[图片: 一只猫]")
        self.assertEqual(image_vision.enrich_text(text, "猫猫震惊", kind="sticker"),
                         "[表情: 猫猫震惊]看看")

    def test_sticker_kind_and_fallback(self):
        sticker = {"file": "s.jpg", "url": "https://example.com/s.jpg",
                   "sub_type": 1, "summary": "[动画表情]", "file_size": 1}
        normal = {"file": "p.jpg", "url": "https://example.com/p.jpg",
                  "sub_type": 0, "summary": "", "file_size": 1}
        self.assertEqual(image_vision.kind_of([sticker]), "sticker")
        self.assertEqual(image_vision.kind_of([normal]), "image")
        self.assertEqual(image_vision.mark_unavailable("[图片] 看", [sticker]),
                         "[表情包: 未识别] 看")
        self.assertEqual(image_vision.mark_unavailable("[图片] 看", [normal]),
                         "[图片: 未识别] 看")

    def test_split_bubbles(self):
        self.assertEqual(split_bubbles("短句。", 3), ["短句。"])
        parts = split_bubbles("第一句。第二句！第三句？第四句。", 3)
        self.assertLessEqual(len(parts), 3)
        self.assertEqual("".join(parts), "第一句。第二句！第三句？第四句。")

    def test_split_target_two(self):
        parts = split_bubbles("哦豁，复变啊。哪块卡了，解析函数还是留数？", 3, target_bubbles=2)
        self.assertGreaterEqual(len(parts), 2)
        self.assertEqual("".join(parts).replace(" ", ""), "哦豁，复变啊。哪块卡了，解析函数还是留数？".replace(" ", ""))
        self.assertEqual(len(split_bubbles("短", 3, target_bubbles=2)), 1)

    def test_reply_plan(self):
        base = make_test_dir("plan")
        try:
            cfg = make_cfg(base)
            store = PrivateStore(cfg)
            burst = style_mod.plan(cfg, store, UID, 4, "复变好难")
            self.assertEqual(burst.target_bubbles, 2)
            long_form = style_mod.plan(cfg, store, UID, 1, "x" * 120)
            self.assertEqual(long_form.target_bubbles, 1)
            self.assertGreaterEqual(long_form.target_chars, 80)
            distress = style_mod.plan(cfg, store, UID, 1, "今天好难受")
            self.assertEqual(distress.target_bubbles, 1)
            proactive = style_mod.plan(cfg, store, UID, 1, "", proactive=True)
            self.assertEqual(proactive.target_bubbles, 1)
            stats = style_mod.update(store, UID, "短句", 4, cfg)
            self.assertEqual(stats["samples"], 1)
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_quiet_hours(self):
        self.assertTrue(_in_quiet_hours(23, [23, 8]))
        self.assertTrue(_in_quiet_hours(3, [23, 8]))
        self.assertFalse(_in_quiet_hours(12, [23, 8]))

    def test_normalize_display_name(self):
        self.assertEqual(cfgmod.normalize_display_name("ㅤ"), "你")
        self.assertEqual(cfgmod.normalize_display_name("   "), "你")
        self.assertEqual(cfgmod.normalize_display_name("阿强"), "阿强")

    def test_vision_order_prefers_visual_only_model(self):
        from scripts import llm_pool
        from unittest.mock import patch
        llm_pool.clear_cooldowns()
        cfg = {"tiers": [["chat-model"]], "vision_models": ["visual-only"]}
        with patch.object(llm_pool, "_load_config", return_value=cfg):
            order = llm_pool._build_attempt_order("visual-only", vision=True)
        self.assertTrue(order)
        self.assertEqual(order[0][1], "visual-only")

    def test_corrections(self):
        base = make_test_dir("corr")
        path = os.path.join(base, "corrections.json")
        try:
            corrections_mod.add(path, "本小姐", "本鱼")
            corrections_mod.add(path, "作为 AI", "")
            items = corrections_mod.load(path)
            self.assertEqual(len(items), 2)
            self.assertEqual(corrections_mod.apply("本小姐才不呢", items), "本鱼才不呢")
            self.assertEqual(corrections_mod.apply("作为 AI 我不能", items), "我不能")
            rules = corrections_mod.rules_text(items)
            self.assertIn("不要说「本小姐」", rules)
            # 同一 wrong 只保留最新
            corrections_mod.add(path, "本小姐", "大肥鱼")
            self.assertEqual(len(corrections_mod.load(path)), 2)
            self.assertIn("大肥鱼", corrections_mod.load(path)[-1]["right"])
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_persona_card_and_stage(self):
        base = make_test_dir("card")
        try:
            cfg = make_cfg(base)
            store = PrivateStore(cfg)
            store.update_profile(UID, display_name="ㅤ", relationship={"stage": "close"})
            card = persona_mod.build_persona_card(cfg, store, UID)
            self.assertIn("大肥鱼", card)
            self.assertIn("亲近", card)
            limit = int((cfg.get("persona") or {}).get("card_max_chars", 1600) or 1600)
            self.assertLessEqual(len(card), limit)
            store.update_profile(UID, relationship={"stage": "intimate"})
            self.assertIn("亲密", persona_mod.build_persona_card(cfg, store, UID))
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_raw_persona_mode(self):
        base = make_test_dir("rawpersona")
        try:
            char_root = os.path.join(base, "characters")
            char_dir = os.path.join(char_root, "rawtest")
            os.makedirs(os.path.join(char_dir, "private"), exist_ok=True)
            os.makedirs(os.path.join(char_dir, "_source"), exist_ok=True)
            with open(os.path.join(char_dir, "_source", "raw.md"), "w", encoding="utf-8") as f:
                f.write("固定形象：这是一段原始人设。" * 20)
            cfg = make_cfg(base)
            cfg["character"] = "rawtest"
            cfg["persona"]["mode"] = "raw"
            with patch.object(cfgmod, "CHARACTERS_DIR", char_root):
                store = PrivateStore(cfg)
                card = persona_mod.build_persona_card(cfg, store, UID)
                self.assertIn("原始人设", card)
                self.assertIn("【人设原文】", card)
                system = persona_mod.build_system_prompt(cfg, store, UID, "你好")
                self.assertIn("原始人设", system)
                cfg["persona"]["mode"] = "card"
                self.assertNotIn("原始人设", persona_mod.build_persona_card(cfg, store, UID))
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_store_dedupe_and_forget(self):
        base = make_test_dir("store")
        try:
            cfg = make_cfg(base)
            store = PrivateStore(cfg)
            store.add_memories(UID, [{"type": "user_fact", "text": "他喜欢猫。", "importance": 3}])
            store.add_memories(UID, [{"type": "user_fact", "text": "他喜欢猫。", "importance": 5}])
            items = store.memory_items(UID)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["importance"], 5)
            self.assertEqual(store.forget(UID, "猫"), 1)
            self.assertEqual(store.memory_items(UID), [])
        finally:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
