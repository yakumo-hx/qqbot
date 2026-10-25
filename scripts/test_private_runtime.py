# -*- coding: utf-8 -*-
"""阶段 1/2 运行时测试：图片顺序、发送插话、主动竞争、回执落盘、结构化输出。

对应设计文档 8.1～8.5。全部离线；用可控 Event/假 RPC 控制回执时机，
不用长时间 sleep 猜时序。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts.private_chat import config as cfgmod
from scripts.private_chat.engine import PrivateChat
from scripts.private_chat.rpc import ActionOutcome

UID = "784427550"
ROOT = cfgmod.PROJECT_ROOT


def make_test_dir(prefix: str) -> str:
    """优先临时目录；沙箱拒绝时回退 data/private/_test（测试结束即删）。"""
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


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


class GatedRPC:
    """每次 send 都先等测试释放；可指定结果序列。"""

    def __init__(self, results=None, auto=False):
        self.results = list(results or [])
        self.auto = auto
        self.requests = []
        self.n = 0

    async def call_outcome(self, websocket, action, params, timeout=10.0):
        self.n += 1
        gate = asyncio.Event()
        req = {"params": params, "gate": gate, "n": self.n}
        self.requests.append(req)
        if self.auto:
            gate.set()
        await gate.wait()
        spec = self.results.pop(0) if self.results else {"status": "ok"}
        status = spec.get("status", "ok")
        mid = spec.get("message_id", str(9000 + self.n))
        return ActionOutcome(
            status,
            data={"message_id": mid} if status == "ok" else None,
            message_id=mid if status == "ok" else None,
            error=spec.get("error", ""),
            retcode=spec.get("retcode"),
            echo="private:test",
        )

    async def call(self, websocket, action, params, timeout=10.0):
        return {"messages": []}


class HoldFirstLLM:
    def __init__(self, output):
        self.output = output
        self.calls = 0
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def __call__(self, messages, cfg):
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            await self.gate.wait()
        return self.output


class InstantLLM:
    def __init__(self, output):
        self.output = output
        self.calls = 0

    async def __call__(self, messages, cfg):
        self.calls += 1
        return self.output


TWO_BUBBLES = json.dumps({
    "bubbles": [
        {"act": "answer", "text": "第一句"},
        {"act": "chat", "text": "第二句"},
    ]
}, ensure_ascii=False)
ONE_BUBBLE = json.dumps({"bubbles": [{"act": "answer", "text": "收到"}]}, ensure_ascii=False)


def make_cfg(base: str) -> dict:
    cfg = cfgmod.load_config()
    cfg["data_dir"] = base
    cfg["allowed_users"] = [UID]
    cfg["history"]["enabled"] = False
    cfg["reply"].update({
        "debounce_seconds": 0.01,
        "debounce_jitter_seconds": 0.0,
        "typing_speed_chars_per_second": 1000.0,
        "max_typing_seconds": 0.01,
    })
    cfg["memory"].update({"extract_every_turns": 100, "organize_with_llm": False})
    cfg["proactive"]["enabled"] = False
    return cfg


def text_msg(text="你好", mid=1001):
    return {
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "self_id": 2668851638, "user_id": int(UID), "message_id": mid,
        "raw_message": text, "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"nickname": "测试", "card": ""},
    }


def image_msg(mid=2001, text=""):
    return {
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "self_id": 2668851638, "user_id": int(UID), "message_id": mid,
        "raw_message": "[CQ:image,file=cat.jpg,url=https://example.com/cat.jpg]" + text,
        "message": [{"type": "image", "data": {
            "file": "cat.jpg", "url": "https://example.com/cat.jpg",
            "sub_type": 0, "file_size": 123,
        }}],
        "sender": {"nickname": "测试", "card": ""},
    }


class RuntimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.base = make_test_dir("rt")
        self.cfg = make_cfg(self.base)
        self.chat = None
        self.ws = FakeWS()

    async def asyncTearDown(self):
        if self.chat is not None:
            await self.chat.stop()
        shutil.rmtree(self.base, ignore_errors=True)

    async def _wait(self, predicate, timeout=4.0):
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if predicate():
                return True
            await asyncio.sleep(0.01)
        return False

    def _start(self, llm, rpc):
        self.chat = PrivateChat(FakeLogger(), cfg=self.cfg, llm_fn=llm, rpc=rpc)
        self.chat.on_connected(self.ws)
        return self.chat

    def _turns(self):
        return self.chat.store.recent_turns(UID, 30)

    def _assistants(self):
        return [t for t in self._turns() if t.get("role") == "assistant"]

    # ---------- 8.1 图片/文本顺序 ----------
    async def test_image_before_text_and_in_place_update(self):
        gate = asyncio.Event()
        started = asyncio.Event()

        async def fake_describe(*args, **kwargs):
            started.set()
            await gate.wait()
            return {"desc": "一只猫", "source": "本消息", "signature": "cat", "kind": "image"}

        self._start(InstantLLM(ONE_BUBBLE), GatedRPC(auto=True))
        with patch("scripts.private_chat.engine.image_vision.describe_turn", new=fake_describe):
            await self.chat.on_message(self.ws, image_msg(1))
            self.assertTrue(await self._wait(started.is_set))
            await self.chat.on_message(self.ws, text_msg("第二句", 2))
            turns = self.store_turns()
            self.assertEqual([t["text"] for t in turns[:2]], ["[图片]", "第二句"])
            gate.set()
            self.assertTrue(await self._wait(lambda: bool(self._assistants()), timeout=5))
        turns = self._turns()
        users = [t for t in turns if t.get("role") == "user"]
        self.assertEqual([t["text"] for t in users], ["[图片: 一只猫]", "第二句"])
        self.assertEqual([str(t.get("msg_id")) for t in users], ["1", "2"])
        assistants = self._assistants()
        self.assertTrue(assistants)
        self.assertEqual(assistants[-1].get("reply_to_msg_ids"), ["1", "2"])

    def store_turns(self):
        return self.chat.store.recent_turns(UID, 30)

    async def test_image_failure_then_text_keeps_order(self):
        gate = asyncio.Event()

        async def failed_describe(*args, **kwargs):
            await gate.wait()
            return None

        self._start(InstantLLM(ONE_BUBBLE), GatedRPC(auto=True))
        with patch("scripts.private_chat.engine.image_vision.describe_turn", new=failed_describe):
            await self.chat.on_message(self.ws, image_msg(1))
            await asyncio.sleep(0.02)
            await self.chat.on_message(self.ws, text_msg("第二句", 2))
            gate.set()
            self.assertTrue(await self._wait(lambda: bool(self._assistants()), timeout=5))
        users = [t for t in self._turns() if t.get("role") == "user"]
        self.assertEqual([t["text"] for t in users], ["[图片: 未识别]", "第二句"])
        self.assertEqual([str(t.get("msg_id")) for t in users], ["1", "2"])

    async def test_image_blocked_keeps_placeholder_and_marks_status(self):
        async def blocked_describe(*args, **kwargs):
            return {"desc": "", "skipped": "blocked", "kind": "image"}

        self._start(InstantLLM(ONE_BUBBLE), GatedRPC(auto=True))
        with patch("scripts.private_chat.engine.image_vision.describe_turn", new=blocked_describe):
            await self.chat.on_message(self.ws, image_msg(1))
            self.assertTrue(await self._wait(lambda: bool(self._assistants()), timeout=5))
        users = [t for t in self._turns() if t.get("role") == "user"]
        self.assertEqual(users[0]["text"], "[图片]")
        self.assertEqual(users[0].get("vision_status"), "blocked")

    async def test_stop_cancels_runtime_workers(self):
        self._start(InstantLLM(ONE_BUBBLE), GatedRPC(auto=True))
        runtime = self.chat.get_runtime(UID)
        await self.chat.stop()
        self.assertTrue(runtime._input_task.done())
        self.assertTrue(runtime._control_task.done())
        self.chat = None

    async def test_stop_propagates_cancellation_from_proactive_wait(self):
        self._start(InstantLLM(ONE_BUBBLE), GatedRPC(auto=True))
        runtime = self.chat.get_runtime(UID)
        started = asyncio.Event()

        async def generate():
            started.set()
            await asyncio.Event().wait()

        async def service():
            while True:
                await runtime.request_proactive(
                    generate=generate,
                    validate=lambda text: True,
                    on_sent=lambda text: None,
                )

        service_task = self.chat.spawn(service())
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(self.chat.stop(), timeout=1)
        self.chat = None
        self.assertTrue(service_task.done())
        self.assertTrue(service_task.cancelled())

    async def test_image_timeout_marks_unavailable(self):
        self.cfg["image"]["preprocess_timeout_seconds"] = 0.05

        async def slow_describe(*args, **kwargs):
            await asyncio.sleep(10)
            return {"desc": "不会到达", "kind": "image"}

        self._start(InstantLLM(ONE_BUBBLE), GatedRPC(auto=True))
        with patch("scripts.private_chat.engine.image_vision.describe_turn", new=slow_describe):
            await self.chat.on_message(self.ws, image_msg(1))
            self.assertTrue(await self._wait(lambda: bool(self._assistants()), timeout=5))
        users = [t for t in self._turns() if t.get("role") == "user"]
        self.assertEqual(users[0]["text"], "[图片: 未识别]")
        self.assertEqual(users[0].get("vision_status"), "timeout")

    # ---------- 8.2 发送中插话 ----------
    async def test_interrupt_during_gap_cancels_old_second_bubble(self):
        rpc = GatedRPC(auto=False)
        self._start(InstantLLM(TWO_BUBBLES), rpc)
        await self.chat.on_message(self.ws, text_msg("你好", 1))
        self.assertTrue(await self._wait(lambda: len(rpc.requests) >= 1))
        rpc.requests[0]["gate"].set()
        self.assertTrue(await self._wait(lambda: len(self._assistants()) == 1))
        await self.chat.on_message(self.ws, text_msg("插话", 2))
        await asyncio.sleep(0.05)
        self.assertEqual(len(rpc.requests), 1, "新消息到达后旧第二气泡不应再发")
        for _ in range(150):
            for req in rpc.requests:
                req["gate"].set()
            if len(self._assistants()) >= 3:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(len(self._assistants()) >= 3)
        old = [t for t in self._assistants() if t.get("reply_to_msg_ids") == ["1"]]
        new = [t for t in self._assistants() if t.get("reply_to_msg_ids") == ["2"]]
        self.assertEqual([t["text"] for t in old], ["第一句"])
        self.assertEqual([t["text"] for t in new], ["第一句", "第二句"])

    async def test_second_bubble_inflight_is_allowed_to_finish(self):
        rpc = GatedRPC(auto=False)
        self._start(InstantLLM(TWO_BUBBLES), rpc)
        await self.chat.on_message(self.ws, text_msg("你好", 1))
        self.assertTrue(await self._wait(lambda: len(rpc.requests) >= 1))
        rpc.requests[0]["gate"].set()
        self.assertTrue(await self._wait(lambda: len(rpc.requests) >= 2, timeout=4))
        # 第二个 RPC 已经发出后才来新消息：允许完成并落盘，但不再发后续旧气泡
        await self.chat.on_message(self.ws, text_msg("插话", 2))
        rpc.requests[1]["gate"].set()
        for _ in range(150):
            for req in rpc.requests:
                req["gate"].set()
            if len(self._assistants()) >= 4:
                break
            await asyncio.sleep(0.02)
        old = [t for t in self._assistants() if t.get("reply_to_msg_ids") == ["1"]]
        new = [t for t in self._assistants() if t.get("reply_to_msg_ids") == ["2"]]
        self.assertEqual([t["text"] for t in old], ["第一句", "第二句"])
        self.assertEqual([t["text"] for t in new], ["第一句", "第二句"])

    async def test_revision_rules_and_bubble_stats(self):
        self._start(InstantLLM(TWO_BUBBLES), GatedRPC(auto=True))
        runtime = self.chat.get_runtime(UID)
        await self.chat.on_message(self.ws, text_msg("你好", 1))
        self.assertTrue(await self._wait(lambda: len(self._assistants()) >= 2))
        self.assertEqual(runtime.revision, 1)
        stats = self.chat.store.profile(UID)["stats"]
        state = self.chat.store.state(UID)
        self.assertEqual(stats["bot_msgs"], 2)          # 按气泡
        self.assertEqual(state["total_turns"], 3)       # 1 user + 2 bubbles
        self.assertEqual(state["turns_since_extract"], 1)  # 只按用户消息
        # 控制指令不增 revision
        await self.chat.on_message(self.ws, text_msg("/关系 close", 2))
        await asyncio.sleep(0.05)
        self.assertEqual(runtime.revision, 1)
        # pause 增 revision，并取消旧回复
        await self.chat.on_message(self.ws, text_msg("在忙", 3))
        await asyncio.sleep(0.05)
        self.assertEqual(runtime.revision, 2)

    async def test_old_bubble_commit_does_not_overwrite_new_state(self):
        rpc = GatedRPC(auto=False)
        self._start(InstantLLM(TWO_BUBBLES), rpc)
        await self.chat.on_message(self.ws, text_msg("旧消息", 1))
        self.assertTrue(await self._wait(lambda: len(rpc.requests) >= 1))
        rpc.requests[0]["gate"].set()
        self.assertTrue(await self._wait(lambda: len(rpc.requests) >= 2, timeout=4))
        # 让新回复停在防抖窗口，先观察旧气泡迟到确认对状态的影响
        self.cfg["reply"]["debounce_seconds"] = 5.0
        self.cfg["reply"]["debounce_jitter_seconds"] = 0.0
        await self.chat.on_message(self.ws, text_msg("新问题？", 2))
        rpc.requests[1]["gate"].set()
        self.assertTrue(await self._wait(
            lambda: any(t.get("reply_to_msg_ids") == ["1"] and t.get("bubble_index") == 1
                        for t in self._assistants()), timeout=3
        ))
        state = self.chat.store.load_dialogue_state(UID)
        self.assertEqual(state.get("pending_user_msg_ids"), ["2"])
        self.assertIn("新问题", state.get("current_topic", ""))
        self.assertEqual(state.get("pending_question"), "新问题？")

    async def test_sent_reply_msg_id_dedupes_history(self):
        rpc = GatedRPC(auto=True)
        self._start(InstantLLM(ONE_BUBBLE), rpc)
        await self.chat.on_message(self.ws, text_msg("你好", 1))
        self.assertTrue(await self._wait(lambda: bool(self._assistants())))
        assistant = self._assistants()[0]
        added = self.chat.store.import_history(UID, [{
            "role": "assistant", "text": assistant["text"],
            "msg_id": assistant["msg_id"], "ts": cfgmod.now_iso(),
        }])
        self.assertEqual(added, 0)
        self.assertEqual(len(self._assistants()), 1)

    async def test_interrupt_model_generation_discards_old(self):
        llm = HoldFirstLLM(TWO_BUBBLES)
        rpc = GatedRPC(auto=True)
        self._start(llm, rpc)
        await self.chat.on_message(self.ws, text_msg("旧问题", 1))
        self.assertTrue(await self._wait(llm.started.is_set))
        await self.chat.on_message(self.ws, text_msg("新问题", 2))
        llm.gate.set()
        self.assertTrue(await self._wait(lambda: len(self._assistants()) >= 1, timeout=5))
        await asyncio.sleep(0.05)
        assistants = self._assistants()
        # 旧回复从未发出，旧 target 仍在 pending，新回复应把 1、2 一起理解；
        # 但绝不能出现只针对旧 target 的过期回复。
        self.assertEqual(assistants[-1].get("reply_to_msg_ids"), ["1", "2"])
        self.assertFalse(any(t.get("reply_to_msg_ids") == ["1"] for t in assistants),
                         "旧模型结果必须丢弃")

    # ---------- 8.3 主动消息竞争 ----------
    async def test_proactive_cancelled_by_user_message(self):
        self._start(InstantLLM(ONE_BUBBLE), GatedRPC(auto=True))
        runtime = self.chat.get_runtime(UID)
        started = asyncio.Event()
        gate = asyncio.Event()
        sent = []

        async def generate():
            started.set()
            await gate.wait()
            return "主动消息"

        task = asyncio.create_task(runtime.request_proactive(
            generate=generate,
            validate=lambda text: True,
            on_sent=lambda text: sent.append(text),
        ))
        self.assertTrue(await self._wait(started.is_set))
        await self.chat.on_message(self.ws, text_msg("普通消息", 1))
        gate.set()
        await task
        await asyncio.sleep(0.1)
        self.assertEqual(sent, [])
        self.assertFalse(any(t.get("source") == "proactive" for t in self._assistants()))
        self.assertTrue(await self._wait(
            lambda: any(t.get("source") == "normal" for t in self._assistants()), timeout=3
        ))

    async def test_proactive_busy_when_pending_exists(self):
        self._start(InstantLLM(ONE_BUBBLE), GatedRPC(auto=True))
        runtime = self.chat.get_runtime(UID)
        runtime.dialogue.pending_user_msg_ids = ["1"]
        self.assertTrue(runtime.proactive_busy())
        runtime.dialogue.pending_user_msg_ids = []
        self.assertFalse(runtime.proactive_busy())

    async def test_proactive_skipped_while_normal_reply_active(self):
        llm = HoldFirstLLM(ONE_BUBBLE)
        self._start(llm, GatedRPC(auto=True))
        runtime = self.chat.get_runtime(UID)
        await self.chat.on_message(self.ws, text_msg("你好", 1))
        self.assertTrue(await self._wait(llm.started.is_set))
        outcome = await runtime.request_proactive(
            generate=lambda: asyncio.sleep(0, "主动"),
            validate=lambda text: True,
            on_sent=lambda text: None,
        )
        self.assertIsNone(outcome)
        llm.gate.set()
        self.assertTrue(await self._wait(lambda: bool(self._assistants())))

    async def test_proactive_rpc_inflight_completes_once(self):
        rpc = GatedRPC(auto=False)
        self._start(InstantLLM(ONE_BUBBLE), rpc)
        runtime = self.chat.get_runtime(UID)
        sent = []
        task = asyncio.create_task(runtime.request_proactive(
            generate=lambda: asyncio.sleep(0, "主动消息"),
            validate=lambda text: True,
            on_sent=lambda text: sent.append(text),
        ))
        self.assertTrue(await self._wait(lambda: len(rpc.requests) >= 1))
        await self.chat.on_message(self.ws, text_msg("用户来了", 1))
        rpc.requests[0]["gate"].set()
        await task
        for req in rpc.requests:
            req["gate"].set()
        self.assertTrue(await self._wait(lambda: len(self._assistants()) >= 1))
        proactive = [t for t in self._assistants() if t.get("source") == "proactive"]
        self.assertEqual(len(proactive), 1)
        self.assertEqual(len(sent), 1)

    # ---------- 8.4 回执与落盘 ----------
    async def test_partial_send_only_first_committed(self):
        rpc = GatedRPC(results=[{"status": "ok"}, {"status": "failed", "error": "retcode"}], auto=True)
        self._start(InstantLLM(TWO_BUBBLES), rpc)
        await self.chat.on_message(self.ws, text_msg("你好", 1))
        self.assertTrue(await self._wait(
            lambda: len(self.chat.store.attempts(UID)) >= 2, timeout=5
        ))
        await asyncio.sleep(0.1)
        assistants = self._assistants()
        self.assertEqual([t["text"] for t in assistants], ["第一句"])
        attempts = self.chat.store.attempts(UID)
        self.assertEqual([a.get("status") for a in attempts], ["sent", "failed"])

    async def test_unknown_does_not_commit_and_history_dedupes(self):
        rpc = GatedRPC(results=[{"status": "unknown", "error": "timeout"}], auto=True)
        self._start(InstantLLM(ONE_BUBBLE), rpc)
        await self.chat.on_message(self.ws, text_msg("你好", 1))
        self.assertTrue(await self._wait(lambda: bool(self.chat.store.attempts(UID))))
        await asyncio.sleep(0.1)
        self.assertFalse(self._assistants())
        self.assertEqual(self.chat.store.attempts(UID)[0]["status"], "unknown")
        # 平台最终补拉到实际消息：只导入一次
        added = self.chat.store.import_history(UID, [{
            "role": "assistant", "text": "收到", "msg_id": "777",
            "ts": cfgmod.now_iso(),
        }])
        self.assertEqual(added, 1)
        added2 = self.chat.store.import_history(UID, [{
            "role": "assistant", "text": "收到", "msg_id": "777",
            "ts": cfgmod.now_iso(),
        }])
        self.assertEqual(added2, 0)

    async def test_control_confirmation_preempts_normal_reply(self):
        llm = HoldFirstLLM(ONE_BUBBLE)
        rpc = GatedRPC(auto=True)
        self._start(llm, rpc)
        await self.chat.on_message(self.ws, text_msg("你好", 1))
        self.assertTrue(await self._wait(llm.started.is_set))
        await self.chat._send_control_reply(self.ws, UID, "控制确认", 55)
        self.assertTrue(await self._wait(
            lambda: any(t.get("source") == "control" for t in self._assistants())
        ))
        llm.gate.set()
        self.assertTrue(await self._wait(
            lambda: any(t.get("source") == "normal" for t in self._assistants())
        ))
        sources = [t.get("source") for t in self._assistants()]
        self.assertLess(sources.index("control"), sources.index("normal"))

    # ---------- 8.5 structured 集成 ----------
    async def test_legacy_pipeline_still_works(self):
        self.cfg["reply"]["pipeline"] = "legacy"
        self._start(InstantLLM("旧链路回复。"), GatedRPC(auto=True))
        await self.chat.on_message(self.ws, text_msg("你好", 1))
        self.assertTrue(await self._wait(
            lambda: any(t.get("role") == "assistant" for t in self._turns()), timeout=4
        ))
        assistants = self._assistants()
        self.assertTrue(assistants)
        self.assertIn("旧链路回复", assistants[0]["text"])

    async def test_structured_bubbles_keep_semantics(self):
        self._start(InstantLLM(TWO_BUBBLES), GatedRPC(auto=True))
        await self.chat.on_message(self.ws, text_msg("给我讲讲", 1))
        self.assertTrue(await self._wait(lambda: len(self._assistants()) >= 2, timeout=5))
        assistants = self._assistants()
        self.assertEqual([t["text"] for t in assistants], ["第一句", "第二句"])
        self.assertEqual([t.get("bubble_index") for t in assistants], [0, 1])
        self.assertTrue(all(t.get("turn_id") for t in assistants))

    async def test_malformed_json_not_sent_raw(self):
        self._start(InstantLLM('{"bubbles":[{"act":"answer","text":'), GatedRPC(auto=True))
        await self.chat.on_message(self.ws, text_msg("你好", 1))
        self.assertTrue(await self._wait(lambda: bool(self._assistants())))
        text = self._assistants()[0]["text"]
        self.assertNotIn("{", text)
        self.assertNotIn("bubbles", text)


class RPCOutcomeTest(unittest.IsolatedAsyncioTestCase):
    async def test_ok_failed_unknown_disconnect(self):
        from scripts.private_chat.rpc import OneBotRPC
        rpc = OneBotRPC()
        ws = FakeWS()
        # ok
        task = asyncio.create_task(rpc.call_outcome(ws, "send_private_msg", {}, timeout=0.5))
        await asyncio.sleep(0)
        echo = ws.sent[-1]["echo"]
        rpc.resolve(ws, {"echo": echo, "status": "ok", "retcode": 0,
                         "data": {"message_id": 123}})
        outcome = await task
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.message_id, "123")
        # failed
        task = asyncio.create_task(rpc.call_outcome(ws, "send_private_msg", {}, timeout=0.5))
        await asyncio.sleep(0)
        echo = ws.sent[-1]["echo"]
        rpc.resolve(ws, {"echo": echo, "status": "failed", "retcode": 102, "data": None})
        outcome = await task
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.retcode, 102)
        # timeout
        outcome = await rpc.call_outcome(ws, "send_private_msg", {}, timeout=0.02)
        self.assertEqual(outcome.status, "unknown")
        # disconnect while waiting
        task = asyncio.create_task(rpc.call_outcome(ws, "send_private_msg", {}, timeout=1.0))
        await asyncio.sleep(0)
        rpc.disconnect(ws)
        outcome = await task
        self.assertEqual(outcome.status, "unknown")


if __name__ == "__main__":
    unittest.main()
