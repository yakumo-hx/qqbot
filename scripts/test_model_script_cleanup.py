# -*- coding: utf-8 -*-
"""模型脚本子进程超时/取消回收的离线 unittest（不调外部服务、不跑真实脚本）。

覆盖：
  - 60s 外框超时：terminate → wait/reap，读取并关闭残余管道；
  - terminate 后不退出：升级 kill，且有界完成；
  - asyncio.CancelledError：同样回收后重新抛出；
  - 正常退出：不 terminate / 不 kill / 不重复 wait；
  - 清理本身读取残余 stdout/stderr 并关闭 transport。

跑法：python -m unittest scripts.test_model_script_cleanup -v
"""

import asyncio
import json
import logging
import unittest
from unittest import mock

import scripts.decision_engine as de


def _silence_loggers():
    for name in ("decision_engine", "scripts.decision_engine"):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            try:
                handler.close()
            except Exception:
                pass
            logger.removeHandler(handler)
        logger.addHandler(logging.NullHandler())
        logger.propagate = False


_silence_loggers()


class _FakeTransport:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class _FakeReader:
    def __init__(self, data=b""):
        self.data = data
        self.read_calls = 0

    async def read(self):
        self.read_calls += 1
        return self.data


class _FakeStdin:
    def __init__(self):
        self.closed = False
        self.close_calls = 0

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True
        self.close_calls += 1


class _FakeProc:
    """最小的 asyncio.subprocess.Process 替身。"""

    def __init__(self, *, terminate_exits=True, communicate=None):
        self.returncode = None
        self.stdout = _FakeReader(b"")
        self.stderr = _FakeReader(b"")
        self.stdin = _FakeStdin()
        self._transport = _FakeTransport()
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self.communicate_calls = 0
        self._terminate_exits = terminate_exits
        self._exit_event = asyncio.Event()
        self._communicate = communicate
        self.started = asyncio.Event()

    async def communicate(self, input=None):
        self.communicate_calls += 1
        self.started.set()
        if self._communicate is not None:
            return await self._communicate(input)
        await asyncio.sleep(3600)  # 由 wait_for 超时 / 任务取消打断

    def terminate(self):
        self.terminate_calls += 1
        if self._terminate_exits:
            self.returncode = 0
            self._exit_event.set()

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9
        self._exit_event.set()

    async def wait(self):
        self.wait_calls += 1
        if self.returncode is not None:
            return self.returncode
        await self._exit_event.wait()
        return self.returncode


def _make_engine():
    engine = de.DecisionEngine()

    async def _context(gid):
        return "测试上下文"

    engine._format_context = _context
    engine._load_character = lambda gid: "测试人设"
    engine.get_active_model = lambda: {
        "model": "fake-model",
        "api_base_url": "http://127.0.0.1:0/v1",
        "api_key": "test-key",
    }
    return engine


class ModelScriptCleanupTest(unittest.IsolatedAsyncioTestCase):
    async def _run_with_proc(self, proc, *, timeout=0.01, terminate_grace=0.01,
                             kill_grace=0.2, drain_grace=0.01):
        engine = _make_engine()
        created = mock.AsyncMock(return_value=proc)
        with mock.patch.object(de.asyncio, "create_subprocess_exec", created), \
             mock.patch.object(de, "_MODEL_SCRIPT_TIMEOUT", timeout), \
             mock.patch.object(de, "_MODEL_SCRIPT_TERMINATE_GRACE", terminate_grace), \
             mock.patch.object(de, "_MODEL_SCRIPT_KILL_GRACE", kill_grace), \
             mock.patch.object(de, "_MODEL_SCRIPT_DRAIN_GRACE", drain_grace):
            result = await engine._call_model_script("model_primary.py", "g1")
        return result

    async def test_01_超时会terminate并wait回收(self):
        proc = _FakeProc(terminate_exits=True)
        result = await self._run_with_proc(proc)
        self.assertEqual(result, "")
        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 0)
        self.assertGreaterEqual(proc.wait_calls, 1)
        self.assertTrue(proc.stdin.closed)
        self.assertGreaterEqual(proc.stdout.read_calls, 1)
        self.assertGreaterEqual(proc.stderr.read_calls, 1)
        self.assertEqual(proc._transport.closed, 1)

    async def test_02_terminate后不退出会升级kill且有界(self):
        proc = _FakeProc(terminate_exits=False)
        result = await self._run_with_proc(
            proc, terminate_grace=0.01, kill_grace=0.2)
        self.assertEqual(result, "")
        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 1)
        self.assertGreaterEqual(proc.wait_calls, 2)
        self.assertEqual(proc._transport.closed, 1)

    async def test_03_取消时先回收再重新抛出CancelledError(self):
        proc = _FakeProc(terminate_exits=True)
        engine = _make_engine()
        created = mock.AsyncMock(return_value=proc)
        with mock.patch.object(de.asyncio, "create_subprocess_exec", created), \
             mock.patch.object(de, "_MODEL_SCRIPT_TIMEOUT", 60.0), \
             mock.patch.object(de, "_MODEL_SCRIPT_TERMINATE_GRACE", 0.01), \
             mock.patch.object(de, "_MODEL_SCRIPT_KILL_GRACE", 0.01), \
             mock.patch.object(de, "_MODEL_SCRIPT_DRAIN_GRACE", 0.01):
            task = asyncio.create_task(
                engine._call_model_script("model_primary.py", "g1"))
            await asyncio.wait_for(proc.started.wait(), 1.0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(proc.terminate_calls, 1)
        self.assertGreaterEqual(proc.wait_calls, 1)
        self.assertTrue(proc.stdin.closed)
        self.assertEqual(proc._transport.closed, 1)

    async def test_04_正常退出不terminate也不kill(self):
        async def _ok_communicate(input_data):
            return json.dumps({"reply": "正常回答"}).encode("utf-8"), b""

        proc = _FakeProc(terminate_exits=False, communicate=_ok_communicate)
        proc.returncode = 0
        result = await self._run_with_proc(proc)
        self.assertEqual(result, "正常回答")
        self.assertEqual(proc.communicate_calls, 1)
        self.assertEqual(proc.terminate_calls, 0)
        self.assertEqual(proc.kill_calls, 0)
        self.assertEqual(proc.wait_calls, 0)

    async def test_05_已退出进程清理时不得重复kill(self):
        proc = _FakeProc(terminate_exits=False)
        proc.returncode = 0
        engine = _make_engine()
        await engine._cleanup_model_script_proc(proc, "model_primary.py")
        self.assertEqual(proc.terminate_calls, 0)
        self.assertEqual(proc.kill_calls, 0)
        self.assertEqual(proc.wait_calls, 0)
        self.assertGreaterEqual(proc.stdout.read_calls, 1)
        self.assertGreaterEqual(proc.stderr.read_calls, 1)
        self.assertEqual(proc._transport.closed, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
