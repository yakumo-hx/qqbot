"""PR #8 回归：消息预算、发送提交、决策分支、画像批次和模型迁移。"""
import asyncio
import contextlib
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import reverse_bot as bot
from scripts import decision_engine as de, llm_pool, user_profile as profiles, web_search
from scripts.decision_receipt import DecisionSendReceipt, SendOutcome


def make_engine():
    engine = de.DecisionEngine()
    engine.config.update(reply_cooldown_seconds=0, proactive_max_per_hour=5,
                         at_reply_min_interval_seconds=0)
    engine._get_density = lambda gid: 12.0
    engine._classify_needs_reply = AsyncMock(return_value=("需要回复", 0.99))
    engine._pick_sticker = AsyncMock(return_value=de._NoPick())
    engine._generate_reply_async = AsyncMock(return_value="这是一条完整的测试回答")
    return engine


async def ask(engine, *, gid="g", uid="u", at=False, status="ok", **kwargs):
    async def deliver(reply):
        kind = "text" if reply.text else "sticker"
        engine.finish_reply(gid, reply, [(kind, SendOutcome(status, kind=kind))],
                            is_at_bot=at)
    return await engine.should_reply("足够长的测试消息", uid, gid, is_at_bot=at,
                                     on_reply=deliver, **kwargs)


class DecisionRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def test_density_fallback_reaches_generation(self):
        engine = make_engine()
        engine._classify_needs_reply.return_value = ("不需要回复", 0.2)
        self.assertTrue((await ask(engine))[0])
        self.assertEqual(engine._generate_reply_async.await_count, 1)
        engine._get_density = lambda gid: 9.0
        self.assertFalse((await ask(engine, gid="quiet"))[0])

    async def test_at_replies_leave_proactive_quota_available(self):
        engine = make_engine()
        for index in range(5):
            engine._generate_reply_async.return_value = f"定向回答 {index}"
            self.assertTrue((await ask(engine, at=True, uid=str(index)))[0])
        self.assertEqual(engine._proactive_count_within("g", 3600), 0)
        engine._generate_reply_async.return_value = "接下来谈一下完全不同的话题内容"
        self.assertTrue((await ask(engine))[0])
        self.assertEqual(engine._proactive_count_within("g", 3600), 1)

    async def test_failed_send_leaves_all_reply_state_empty(self):
        engine = make_engine()
        engine.config["reply_cooldown_seconds"] = 60
        self.assertTrue((await ask(engine, status="failed", message_id="one"))[0])
        self.assertEqual(engine._last_reply_time, {})
        self.assertEqual(engine._last_reply, {})
        self.assertEqual(engine._proactive_count_within("g", 3600), 0)
        self.assertEqual(list(engine._recent_bot_replies["g"]), [])
        self.assertTrue((await ask(engine, message_id="two"))[0])
        self.assertEqual(engine._proactive_count_within("g", 3600), 1)

    async def test_unknown_keeps_state_without_resend(self):
        engine = make_engine()
        self.assertTrue((await ask(engine, status="unknown"))[0])
        self.assertEqual(engine._proactive_count_within("g", 3600), 1)
        self.assertFalse((await ask(engine))[0])

    async def test_partial_image_success_does_not_commit_failed_text(self):
        engine = make_engine()
        ok, reply = await engine.should_reply("测试消息", "u", "g")
        self.assertTrue(ok)
        engine.finish_reply("g", reply, [("text", SendOutcome("failed")),
                                         ("sticker", SendOutcome("ok"))], is_at_bot=False)
        self.assertEqual(engine._last_reply, {})
        self.assertIn("g", engine._last_reply_time)
        self.assertEqual(engine._proactive_count_within("g", 3600), 1)
        engine.finish_reply("g", reply, [("sticker", SendOutcome("ok"))], is_at_bot=False)
        self.assertEqual(engine._proactive_count_within("g", 3600), 1)

    async def test_sticker_at_does_not_consume_proactive_quota(self):
        engine = make_engine()
        engine._pick_sticker.return_value = SimpleNamespace(level="strong", path="test.png")
        self.assertTrue((await ask(engine, at=True))[0])
        self.assertEqual(engine._proactive_count_within("g", 3600), 0)
        self.assertIn("g", engine._last_reply_time)
        engine._generate_reply_async.assert_not_awaited()

    async def test_group_waits_for_receipt_and_other_group_continues(self):
        engine = make_engine()
        entered, release = asyncio.Event(), asyncio.Event()
        async def deliver(reply):
            entered.set()
            await release.wait()
            engine.finish_reply("g", reply, [("text", SendOutcome("ok"))], is_at_bot=False)
        first = asyncio.create_task(engine.should_reply("测试消息", "u", "g", on_reply=deliver))
        await entered.wait()
        second = asyncio.create_task(ask(engine, uid="another"))
        self.assertTrue((await ask(engine, gid="other"))[0])
        self.assertFalse(second.done())
        self.assertEqual(engine._proactive_count_within("g", 3600), 0)
        release.set()
        await first
        self.assertFalse((await second)[0])  # 第一条确认后，重复内容不再发送。

    async def test_queue_wait_expires_without_generation(self):
        engine = make_engine()
        records = []
        engine.trace_recorder.write = lambda record: records.append(dict(record))
        lock = await engine._get_group_lock("g")
        await lock.acquire()
        try:
            with patch.object(de, "_GROUP_QUEUE_WAIT_SECONDS", 0.02):
                self.assertFalse((await ask(engine))[0])
        finally:
            lock.release()
        engine._generate_reply_async.assert_not_awaited()
        self.assertEqual(records[0]["stages"][0]["stage"], "queue_timeout")
        self.assertEqual(records[0]["send_status"], "not_attempted")

    async def test_trace_includes_time_before_decision(self):
        engine = make_engine()
        _, reply = await ask(engine, received_at=time.time() - 10)
        self.assertGreaterEqual(reply.trace.record["latency_ms"], 10000)

    async def test_context_preparation_uses_child_budget(self):
        engine = make_engine()
        cancelled = asyncio.Event()
        async def blocked(gid):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        engine._format_context = blocked
        with patch.object(de, "_MODEL_SCRIPT_TIMEOUT", 0.02), \
             patch.object(de.asyncio, "create_subprocess_exec", new=AsyncMock()) as create:
            self.assertEqual(await engine._call_model_script("unused.py", "g"), "")
        create.assert_not_awaited()
        self.assertTrue(cancelled.is_set())


class EntryBudgetTest(unittest.IsolatedAsyncioTestCase):
    async def test_full_request_timeout_cancels_slow_classifier(self):
        engine = make_engine()
        cancelled = asyncio.Event()
        async def blocked(text):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        engine._classify_needs_reply = blocked
        async def handle(ws, event):
            await ask(engine)
        with patch.object(bot, "handle_message", new=handle), \
             patch.object(bot, "_MESSAGE_TIMEOUT_SECONDS", 0.03):
            await bot._safe_handle_message(None, {"group_id": "g"})
        self.assertTrue(cancelled.is_set())
        self.assertFalse((await engine._get_group_lock("g")).locked())

    async def test_bounded_groups_keep_routing_echo_and_cancel_on_disconnect(self):
        class Socket:
            remote_address = ("offline", 0)
            def __init__(self):
                self.frames = asyncio.Queue()
            def __aiter__(self):
                return self
            async def __anext__(self):
                frame = await self.frames.get()
                if frame is None:
                    raise StopAsyncIteration
                return json.dumps(frame)
            async def send(self, payload):
                echo = json.loads(payload)["echo"]
                await self.frames.put({"echo": echo, "status": "ok", "retcode": 0})
        ws = Socket()
        started, cancelled = [], []
        ready = asyncio.Event()
        async def handle(ws, data, deadline):
            started.append(data["group_id"])
            if len(started) == 3:
                ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(data["group_id"])
        sender = DecisionSendReceipt(timeout=0.5)
        with patch.object(bot, "_safe_handle_message", new=handle), \
             patch.object(bot, "_MAX_GROUP_MESSAGE_TASKS", 2), \
             patch.object(bot, "decision_sender", sender), \
             patch.object(bot, "_watchdog", Mock()), \
             patch.object(bot, "set_napcat_connected"), \
             patch.object(bot, "has_pending_retry", return_value=False), \
             patch.object(bot.interaction_menus, "disconnect"):
            task = asyncio.create_task(bot.handler(ws))
            try:
                for index, gid in enumerate(["A", "A", "A", "B"]):
                    await ws.frames.put({"post_type": "message", "message_type": "group",
                                         "group_id": gid, "message_id": index})
                await asyncio.wait_for(ready.wait(), 1)
                self.assertEqual(started.count("A"), 2)
                self.assertIn("B", started)
                self.assertTrue((await sender.send_text(ws, 1, "测试回执")).ok)
            finally:
                await ws.frames.put(None)
                await asyncio.wait_for(task, 1)
        self.assertCountEqual(cancelled, ["A", "A", "B"])

    async def test_entry_cancellation_commits_unknown_and_releases_group(self):
        engine = make_engine()
        entered = asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        sender = SimpleNamespace(send_text=send)
        with patch.object(bot, "decision_engine", engine), patch.object(bot, "decision_sender", sender):
            task = asyncio.create_task(engine.should_reply(
                "测试消息", "u", "g", on_reply=lambda reply:
                bot._send_decision_reply(None, "g", "u", False, reply)))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(engine._proactive_count_within("g", 3600), 1)
        self.assertFalse((await engine._get_group_lock("g")).locked())

    async def test_receipt_write_and_ack_share_deadline(self):
        class Socket:
            async def send(self, payload):
                await asyncio.sleep(0.24)
        sender = DecisionSendReceipt(timeout=0.3)
        start = time.monotonic()
        outcome = await sender.send_text(Socket(), 1, "测试消息")
        self.assertEqual(outcome.status, "unknown")
        self.assertLess(time.monotonic() - start, 0.48)
        self.assertEqual(sender.pending, {})

    async def test_disconnect_during_write_cleans_receipt_future(self):
        entered = asyncio.Event()
        class Socket:
            async def send(self, payload):
                entered.set()
                await asyncio.Event().wait()
        sender, ws = DecisionSendReceipt(), Socket()
        task = asyncio.create_task(sender.send_text(ws, 1, "测试消息"))
        await entered.wait()
        future = next(iter(sender.pending.values()))
        sender.disconnect(ws)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(sender.pending, {})
        self.assertFalse(future._log_traceback)

    async def test_first_search_trickle_hits_wall_clock_timeout(self):
        finished = asyncio.Event()
        async def handle(reader, writer):
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                size = next(int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                            if line.lower().startswith(b"content-length:"))
                await reader.readexactly(size)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
                for _ in range(100):
                    writer.write(b" ")
                    await writer.drain()
                    await asyncio.sleep(0.02)
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()
                finished.set()
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        try:
            url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            with patch.object(web_search, "_base_url", return_value=url):
                result = await asyncio.wait_for(web_search._raw_search("test", 1, "", timeout=0.5), 3)
            self.assertFalse(result["ok"])
            self.assertIn("TimeoutError", result["reason"])
            await asyncio.wait_for(finished.wait(), 2)
        finally:
            server.close()
            await server.wait_closed()


class ProfileBatchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("_speak_buffer", {}), ("_pending_retry", {}), ("_profiles", {}),
                            ("_update_lock", asyncio.Lock()), ("_napcat_connected", True)):
            self.stack.enter_context(patch.object(profiles, name, value))
        self.stack.enter_context(patch.object(profiles, "_is_school_only_hours", return_value=True))
        self.stack.enter_context(patch.object(profiles, "save_profiles"))
        self.api = self.stack.enter_context(patch.object(profiles, "_call_profile_api", new=AsyncMock(return_value=None)))

    def record(self, uid="u", text="相同的新旧发言"):
        for _ in range(5):
            profiles.record_message("g", uid, text)

    async def seed_retry(self):
        self.record()
        await profiles.update_profiles()
        self.assertEqual(len(profiles._pending_retry["g"]["u"]), 5)

    async def test_retry_success_preserves_identical_new_messages(self):
        await self.seed_retry()
        old = profiles._pending_retry["g"]["u"][:]
        async def success(*args):
            self.record()
            return "画像成功"
        self.api.side_effect = success
        await profiles.retry_pending_updates()
        remaining = profiles._speak_buffer["g"]["u"]
        self.assertEqual(len(remaining), 5)
        self.assertTrue(all(all(msg is not previous for previous in old) for msg in remaining))
        self.assertEqual(profiles._pending_retry, {})

    async def test_long_cycle_success_removes_short_cycle_mirror(self):
        await self.seed_retry()
        self.api.return_value = "画像成功"
        await profiles.update_profiles()
        await profiles.retry_pending_updates()
        self.assertEqual(self.api.await_count, 2)
        self.assertEqual(profiles._speak_buffer, {})
        self.assertEqual(profiles._pending_retry, {})

    async def test_retry_does_not_overwrite_new_pending_work(self):
        await self.seed_retry()
        async def success(*args):
            profiles._pending_retry.setdefault("g", {})["new"] = ["新任务"]
            profiles._pending_retry["g"]["u"] = ["同一用户的新任务"]
            return "画像成功"
        self.api.side_effect = success
        await profiles.retry_pending_updates()
        self.assertEqual(profiles._pending_retry["g"]["new"], ["新任务"])
        self.assertEqual(profiles._pending_retry["g"]["u"], ["同一用户的新任务"])

    async def test_repeated_failures_do_not_multiply_messages(self):
        await self.seed_retry()
        for _ in range(3):
            await profiles.retry_pending_updates()
            self.assertEqual(len(profiles._speak_buffer["g"]["u"]), 5)
            await profiles.update_profiles()
            self.assertEqual(len(profiles._pending_retry["g"]["u"]), 5)
            self.assertEqual(len(profiles._speak_buffer["g"]["u"]), 5)

    async def test_overlapping_cycles_process_each_batch_once(self):
        await self.seed_retry()
        entered, release = asyncio.Event(), asyncio.Event()
        async def success(*args):
            entered.set()
            await release.wait()
            return "画像成功"
        self.api.side_effect = success
        retry = asyncio.create_task(profiles.retry_pending_updates())
        await entered.wait()
        update = asyncio.create_task(profiles.update_profiles())
        self.record(text="后到的新批次")
        await asyncio.sleep(0)
        self.assertFalse(update.done())
        release.set()
        await asyncio.gather(retry, update)
        self.assertEqual(self.api.await_count, 3)  # 初次失败、旧批重试、新批更新
        self.assertEqual(profiles._speak_buffer, {})
        self.assertEqual(profiles._pending_retry, {})

    async def test_cancelled_retry_restores_both_triggers(self):
        await self.seed_retry()
        entered = asyncio.Event()
        async def blocked(*args):
            entered.set()
            await asyncio.Event().wait()
        self.api.side_effect = blocked
        task = asyncio.create_task(profiles.retry_pending_updates())
        await entered.wait()
        self.record(text="重试期间新消息")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(profiles._pending_retry["g"]["u"]), 5)
        self.assertEqual(len(profiles._speak_buffer["g"]["u"]), 10)

    async def test_cancelled_long_cycle_keeps_unprocessed_users(self):
        self.record("u")
        self.record("later")
        entered = asyncio.Event()
        async def blocked(*args):
            entered.set()
            await asyncio.Event().wait()
        self.api.side_effect = blocked
        task = asyncio.create_task(profiles.update_profiles())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(profiles._speak_buffer["g"]["u"]), 5)
        self.assertEqual(len(profiles._speak_buffer["g"]["later"]), 5)

    async def test_disconnected_failure_stays_pending_until_reconnect(self):
        profiles._napcat_connected = False
        await self.seed_retry()
        self.assertEqual(profiles._speak_buffer, {})
        await profiles.retry_pending_updates()
        self.assertEqual(len(profiles._pending_retry["g"]["u"]), 5)
        profiles._napcat_connected = True
        self.api.return_value = "画像成功"
        self.assertEqual(await profiles.retry_pending_updates(), 1)
        self.assertEqual(profiles._pending_retry, {})


class ModelMigrationTest(unittest.TestCase):
    def test_fallback_matches_current_pool_configuration(self):
        configured = json.loads(llm_pool.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(configured, llm_pool._FALLBACK_CONFIG)
        self.assertEqual(de.DecisionEngine.DEFAULT_MODEL, configured["tiers"][0][0])

    def test_current_private_vision_model_is_preferred(self):
        cfg = json.loads((llm_pool.PROJECT_ROOT / "config/private_chat.json").read_text(encoding="utf-8"))
        preferred = cfg["image"]["vision_model"]
        with patch.object(llm_pool, "_COOLDOWN", {}):
            self.assertEqual(llm_pool._build_attempt_order(preferred, vision=True)[0][1], preferred)


if __name__ == "__main__":
    unittest.main()
