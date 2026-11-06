"""Offline regression probes for d32b2ab. No QQ or external API traffic."""
import asyncio
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ['DECISION_TRACE_DISABLED'] = '1'
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import reverse_bot as bot
from scripts import decision_engine as de, image_cache, image_utils, vision
from scripts import llm_pool, user_profile as profiles, web_search
from scripts.decision_receipt import DecisionSendReceipt
from scripts.commands import _state

logging.disable(logging.CRITICAL)
results = {'commit': 'd32b2ab60f2dc7eb66fa3ba1b47f24e42f804f25'}


def engine():
    obj = de.DecisionEngine()
    obj.config['reply_cooldown_seconds'] = 0
    obj._get_density = lambda gid: 12.0
    obj._pick_sticker = AsyncMock(return_value=de._NoPick())
    obj._classify_needs_reply = AsyncMock(return_value=('需要回复', 0.99))
    obj._generate_reply_async = AsyncMock(return_value='这是离线验证的回答')
    return obj


async def ask(obj, group='audit', at=False, user='audit-user'):
    return await obj.should_reply('足够长的离线测试消息', user, group, is_at_bot=at)


async def timer_lag(work):
    loop = asyncio.get_running_loop()
    started = loop.time()
    fired = loop.create_future()
    handle = loop.call_later(0.01, lambda: fired.set_result(loop.time() - started))
    try:
        await work()
        return round((await fired) * 1000, 1)
    finally:
        handle.cancel()


async def main():
    obj = engine()
    del obj._classify_needs_reply
    def slow_classify(*args):
        time.sleep(0.15)
        return '需要回复', 0.99
    obj._classifier = SimpleNamespace(classify=slow_classify)
    results['classifier_event_loop'] = {
        'injected_sync_work_ms': 150, 'timer_due_ms': 10,
        'timer_fired_ms': await timer_lag(lambda: obj._classify_needs_reply('audit')),
    }

    def slow_fetch(meta):
        time.sleep(0.15)
        return b'fake-image'
    with patch.object(_state, 'vision_rate_ok', return_value=(True, 1)), \
         patch.object(image_cache, 'select_candidates', return_value=([{'file': 'audit'}], '本消息')), \
         patch.object(image_cache, 'seen_hash', return_value=False), \
         patch.object(image_utils, 'fetch_image', side_effect=slow_fetch), \
         patch.object(image_utils, 'to_data_url', return_value='data:image/jpeg;base64,AA=='), \
         patch.object(vision, 'describe', new=AsyncMock(return_value=(True, '测试图片'))):
        results['image_download_event_loop'] = {
            'injected_sync_work_ms': 150, 'timer_due_ms': 10,
            'timer_fired_ms': await timer_lag(
                lambda: bot._describe_triggered_image('audit', [], '', 'audit-user', '测试')),
        }

    obj = engine()
    obj._classify_needs_reply = AsyncMock(return_value=('需要回复', 0.20))
    answer = await ask(obj)
    results['classifier_density_fallback'] = {
        'density': 12, 'score': 0.20, 'replied': answer[0],
        'model_calls': obj._generate_reply_async.await_count,
        'decision': dict(obj.get_debug_info()),
    }

    obj = engine()
    obj._generate_reply_async = AsyncMock(side_effect=[f'离线艾特回答{i}' for i in range(5)])
    answers = [await ask(obj, at=True, user=f'user-{i}') for i in range(5)]
    reply = await ask(obj, at=False)
    results['at_replies_consume_proactive_quota'] = {
        'successful_at_generations': sum(bool(a[0]) for a in answers),
        'quota_used': obj._proactive_count_within('audit', 3600),
        'next_proactive_replied': reply[0], 'decision': dict(obj.get_debug_info()),
    }

    # Pause generation for A. Twenty more A events remain waiting on the group
    # lock; a B event completes. The per-user @ limiter cannot bound this queue.
    obj = engine()
    entered, release = asyncio.Event(), asyncio.Event()
    generated = []
    async def gated_generate(gid, target=None, **kwargs):
        generated.append(gid)
        if gid == 'A':
            entered.set()
            await release.wait()
        return '离线并发回答' + gid
    obj._generate_reply_async = gated_generate
    first = asyncio.create_task(ask(obj, 'A', True, 'user-first'))
    await entered.wait()
    queued = [asyncio.create_task(ask(obj, 'A', True, f'user-{i}')) for i in range(20)]
    other = await asyncio.wait_for(ask(obj, 'B', True), 1)
    await asyncio.sleep(0.05)
    results['per_group_head_of_line'] = {
        'same_group_pending_count': sum(not t.done() for t in [first, *queued]),
        'same_group_started_generations': generated.count('A'),
        'other_group_completed': other[0],
    }
    for task in [first, *queued]:
        task.cancel()
    release.set()
    await asyncio.gather(first, *queued, return_exceptions=True)

    # The subprocess deadline starts after context formatting. Slow preparation
    # remains outside it even though the child cleanup itself has been repaired.
    obj = engine()
    entered, release = asyncio.Event(), asyncio.Event()
    async def gated_context(gid):
        entered.set()
        await release.wait()
        return 'offline context'
    obj._format_context = gated_context
    create = AsyncMock()
    with patch.object(de, '_MODEL_SCRIPT_TIMEOUT', 0.01), \
         patch.object(de.asyncio, 'create_subprocess_exec', new=create):
        task = asyncio.create_task(obj._call_model_script('unused.py', 'audit'))
        await entered.wait()
        await asyncio.sleep(0.06)
        results['context_outside_child_deadline'] = {
            'child_deadline_ms': 10, 'observed_wait_ms': 60,
            'still_pending': not task.done(), 'child_created': create.called,
        }
        task.cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)

    # Exercise the real entry point and the real echo receipt resolver with an
    # explicit failed platform acknowledgment; all I/O stays in this fake socket.
    obj = engine()
    sender = DecisionSendReceipt(timeout=0.1)
    written_traces = []
    obj.trace_recorder.write = lambda record: written_traces.append(dict(record))
    class FailedSocket:
        def __init__(self):
            self.sent = []
        async def send(self, payload):
            data = json.loads(payload)
            self.sent.append(data)
            sender.resolve(self, {'echo': data['echo'], 'status': 'failed', 'retcode': 100})
    ws = FailedSocket()
    with patch.object(bot, 'decision_engine', obj), \
         patch.object(bot, 'decision_sender', sender), \
         patch.object(bot, '_feature_on', side_effect=lambda gid, name: name == 'decision_engine'), \
         patch.object(bot, 'PROFILE_ENABLED', False), \
         patch.object(bot.group_history, 'record'), \
         patch.object(bot.reference_mod, 'is_enabled_for', return_value=False):
        for i in range(2):
            await bot.handle_message(ws, {
                'group_id': 990001, 'user_id': 990010 + i, 'message_id': 990100 + i,
                'self_id': bot.BOT_QQ, 'sender': {'nickname': '离线审查'},
                'raw_message': f'[CQ:at,qq={bot.BOT_QQ}] 足够长的离线测试消息',
            })
    results['failed_receipt_keeps_reply_state'] = {
        'platform_ack': 'failed', 'received_new_messages': 2,
        'generated_reply_count': obj._generate_reply_async.await_count,
        'send_attempt_count': len(ws.sent),
        'payload_has_echo': bool(ws.sent and 'echo' in ws.sent[0]),
        'quota_used': obj._proactive_count_within('990001', 3600),
        'last_reply_retained': obj._last_reply.get('990001'),
        'cooldown_time_retained': bool(obj._last_reply_time.get('990001')),
        'second_decision': dict(obj.get_debug_info()),
        'recorded_send_status': [t['send_status'] for t in written_traces],
    }

    class SlowWriteSocket:
        async def send(self, payload):
            await asyncio.sleep(0.08)
    sender = DecisionSendReceipt(timeout=0.1)
    start = time.monotonic()
    outcome = await sender.send_text(SlowWriteSocket(), 990001, 'offline')
    results['receipt_timeout_resets_after_write'] = {
        'configured_timeout_ms': 100, 'injected_write_delay_ms': 80,
        'elapsed_ms': round((time.monotonic() - start) * 1000, 1),
        'status': outcome.status, 'error_kind': outcome.error_kind,
    }

    # A loopback bridge drips a valid response. The first search has only an
    # httpx read timeout; frequent small chunks keep it alive beyond that value.
    async def local_handler(reader, writer):
        try:
            header = await reader.readuntil(b'\r\n\r\n')
            length = 0
            for line in header.decode().split('\r\n'):
                if line.lower().startswith('content-length:'):
                    length = int(line.split(':', 1)[1])
            if length:
                await reader.readexactly(length)
            body = json.dumps({'ok': True, 'value': {'provider': 'offline', 'sources': [
                {'url': 'https://example.invalid/audit', 'title': 'offline audit', 'snippet': 'local response'}
            ]}}).encode()
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
                         + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n')
            await writer.drain()
            for i in range(0, len(body), 10):
                await asyncio.sleep(0.05)
                writer.write(body[i:i+10])
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
    server = await asyncio.start_server(local_handler, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with patch.object(web_search, '_base_url', return_value=f'http://127.0.0.1:{port}'), \
             patch.object(web_search, '_log'):
            start = time.monotonic()
            response = await asyncio.wait_for(web_search._raw_search('audit', 1, '', timeout=0.15), 5)
            results['first_search_has_no_wall_clock_deadline'] = {
                'configured_io_timeout_ms': 150,
                'elapsed_ms': round((time.monotonic() - start) * 1000, 1),
                'returned_success': response['ok'],
            }
    finally:
        server.close()
        await server.wait_closed()

    with patch.object(profiles, '_speak_buffer', {}), \
         patch.object(profiles, '_pending_retry', {}), \
         patch.object(profiles, '_profiles', {}), \
         patch.object(profiles, '_napcat_connected', True), \
         patch.object(profiles, '_is_school_only_hours', return_value=True), \
         patch.object(profiles, 'save_profiles'), \
         patch.object(profiles, '_call_profile_api', new=AsyncMock(side_effect=[None, '离线画像成功结果'])):
        for i in range(5):
            profiles.record_message('audit', 'u', f'离线发言{i}')
        with contextlib.redirect_stdout(io.StringIO()):
            await profiles.update_profiles()
            await profiles.retry_pending_updates()
        results['profile_duplicate_after_retry'] = {
            'retry_succeeded': bool(profiles._profiles.get('audit', {}).get('u')),
            'old_messages_still_in_next_batch': len(profiles._speak_buffer.get('audit', {}).get('u', [])),
        }

    with patch.object(profiles, '_pending_retry', {'audit': {'u': ['old'] * 5}}), \
         patch.object(profiles, '_profiles', {}), \
         patch.object(profiles, '_is_school_only_hours', return_value=True), \
         patch.object(profiles, 'save_profiles'):
        entered, release = asyncio.Event(), asyncio.Event()
        async def pending_api(*args):
            entered.set()
            await release.wait()
            return '离线画像成功结果'
        with patch.object(profiles, '_call_profile_api', new=pending_api):
            task = asyncio.create_task(profiles.retry_pending_updates())
            await entered.wait()
            profiles._pending_retry.setdefault('audit', {})['new-user'] = ['new'] * 5
            release.set()
            await task
        results['profile_concurrent_retry_loss'] = {
            'new_batch_inserted_while_waiting': True,
            'new_batch_present_after_retry': 'new-user' in profiles._pending_retry.get('audit', {}),
        }

    llm_pool.clear_cooldowns()
    results['model_migration'] = {
        'decision_default': de.DecisionEngine.DEFAULT_MODEL,
        'configured_active': de.DecisionEngine.get_active_model()['model'],
        'current_vision_preference': llm_pool._build_attempt_order('qwen3.8-flash', vision=True),
        'old_vision_preference': llm_pool._build_attempt_order('qwen3.8-chat', vision=True),
    }
    output = Path(__file__).with_name('probe-results.json')
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(results, ensure_ascii=False, indent=2))


asyncio.run(main())
