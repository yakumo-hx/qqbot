# -*- coding: utf-8 -*-
"""模型池总预算硬截止的离线 unittest。

使用本机 127.0.0.1 的慢速分块 HTTP 服务，不访问真实模型/网关：
  - 单块间隔低于 httpx read timeout，但总传输超过 budget → 必须在预算附近失败；
  - budget 内的正常响应仍成功；
  - 多模型故障转移时，第一段耗尽预算后不再发起第二个请求；
  - 单次 attempt 超时但总预算仍充足时，继续按原顺序降档；
  - 私聊自定义 budget 生效；
  - 预算耗尽仍抛 LLMPoolExhausted（或其子类），不泄漏裸 asyncio.TimeoutError；
  - 审计日志重定向到临时目录，不写生产 logs/。

跑法：python -m unittest scripts.test_llm_pool_budget -v
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import scripts.llm_pool as lp

_ROOT = Path(__file__).resolve().parent.parent
_TEST_ROOT = _ROOT / ".tmp_llm_pool_budget_tests"


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _start_server(state: dict) -> tuple[_HTTPServer, threading.Thread]:
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args, **kwargs):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                payload = {}
            model = str(payload.get("model") or "")
            with state["lock"]:
                state["models"].append(model)

            slow = model in state["slow_models"]
            chunks = int(state["slow_chunks"] if slow else 1)
            delay = float(state["slow_delay"] if slow else 0.0)
            body = json.dumps(
                {
                    "choices": [
                        {"message": {"content": "本地桩回复"},
                         "finish_reason": "stop"}
                    ],
                    "usage": {"total_tokens": 3},
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            piece = max(1, (len(body) + chunks - 1) // chunks)
            try:
                for start in range(0, len(body), piece):
                    self.wfile.write(body[start:start + piece])
                    self.wfile.flush()
                    if delay:
                        time.sleep(delay)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                # 客户端在预算截止时取消连接，属于预期行为。
                pass

    server = _HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class LLMPoolBudgetTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        os.makedirs(_TEST_ROOT, exist_ok=True)
        self.tmpdir = _TEST_ROOT / uuid.uuid4().hex
        self.tmpdir.mkdir(parents=True, exist_ok=True)
        self.audit_path = self.tmpdir / "llm_pool.jsonl"
        self.config_path = self.tmpdir / "model_pool.json"

        self._old_env = {
            "LLM_POOL_LOG_PATH": os.environ.get("LLM_POOL_LOG_PATH"),
            "LLM_POOL_DISABLE": os.environ.get("LLM_POOL_DISABLE"),
            "LLM_POOL_BASE_URL": os.environ.get("LLM_POOL_BASE_URL"),
            "LLM_POOL_API_KEY": os.environ.get("LLM_POOL_API_KEY"),
        }
        os.environ["LLM_POOL_LOG_PATH"] = str(self.audit_path)
        os.environ.pop("LLM_POOL_DISABLE", None)
        lp._LOG_RESOLVED = None
        lp.clear_cooldowns()

        self._config_patch = mock.patch.object(lp, "CONFIG_PATH", self.config_path)
        self._config_patch.start()
        self._reset_config_cache()

        self.state = {
            "lock": threading.Lock(),
            "models": [],
            "slow_models": set(),
            "slow_chunks": 40,
            "slow_delay": 0.03,
        }
        self.server = None
        self.server_thread = None

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join(2.0)
        self._config_patch.stop()
        self._reset_config_cache()
        lp._LOG_RESOLVED = None
        lp.clear_cooldowns()
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _reset_config_cache(self):
        lp._CONFIG_CACHE = None
        lp._CONFIG_MTIME = None

    def _start(self):
        self.server, self.server_thread = _start_server(self.state)
        return self.server.server_address[1]

    def _write_config(self, tiers, *, attempt_timeout=5.0, total_budget=10.0):
        port = self._start()
        cfg = {
            "base_url": "http://127.0.0.1:%d" % port,
            "api_key": "test-key",
            "tiers": tiers,
            "attempt_timeout_seconds": attempt_timeout,
            "total_budget_seconds": total_budget,
            "cooldown_seconds": 1.0,
            "hard_cooldown_seconds": 1.0,
            "max_tokens_default": 16,
            "min_max_tokens": 1,
            "model_overrides": {},
            "no_response_format": [],
            "vision_models": [],
        }
        self.config_path.write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self._reset_config_cache()

    def _read_audit(self, label):
        if not self.audit_path.exists():
            return []
        out = []
        for line in self.audit_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("label") == label:
                out.append(rec)
        return out

    async def test_01_慢速分块超过budget必须失败(self):
        self._write_config([["slow-model"]], attempt_timeout=5.0,
                           total_budget=10.0)
        self.state["slow_models"] = {"slow-model"}
        started = time.monotonic()
        with self.assertRaises(lp.LLMPoolExhausted) as ctx:
            await lp.chat(
                [{"role": "user", "content": "测试"}],
                label="budget-slow", budget=0.25,
            )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.2, "慢速传输没有被总预算硬截止")
        self.assertIsInstance(ctx.exception, lp.LLMPoolExhausted)
        self.assertNotIsInstance(ctx.exception, asyncio.TimeoutError)
        self.assertEqual(self.state["models"], ["slow-model"])
        records = self._read_audit("budget-slow")
        self.assertTrue(records, "预算超时也必须留下审计记录")
        self.assertFalse(any(r.get("ok") for r in records))
        self.assertTrue(all("latency_ms" in r for r in records))
        self.assertTrue(any("budget" in str(r.get("err") or "")
                            or "预算" in str(r.get("err") or "")
                            for r in records))
        self.assertNotIn("test-key", self.audit_path.read_text(encoding="utf-8"))

    async def test_02_budget内正常响应仍成功(self):
        self._write_config([["fast-model"]], attempt_timeout=5.0,
                           total_budget=10.0)
        started = time.monotonic()
        text, model = await lp.chat(
            [{"role": "user", "content": "测试"}],
            label="budget-fast", budget=2.0,
        )
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(text, "本地桩回复")
        self.assertEqual(model, "fast-model")
        records = self._read_audit("budget-fast")
        self.assertTrue(any(r.get("ok") for r in records))
        self.assertEqual(self.state["models"], ["fast-model"])

    async def test_03_预算耗尽后不再启动第二个模型(self):
        self._write_config([["slow-model"], ["second-model"]],
                           attempt_timeout=5.0, total_budget=10.0)
        self.state["slow_models"] = {"slow-model"}
        with self.assertRaises(lp.LLMPoolExhausted):
            await lp.chat(
                [{"role": "user", "content": "测试"}],
                label="budget-failover", budget=0.25,
            )
        self.assertEqual(self.state["models"], ["slow-model"],
                         "预算耗尽后不应再发起第二个请求")

    async def test_04_单次attempt超时且预算充足时继续降档(self):
        self._write_config([["slow-model"], ["fast-model"]],
                           attempt_timeout=0.15, total_budget=3.0)
        self.state["slow_models"] = {"slow-model"}
        started = time.monotonic()
        text, model = await lp.chat(
            [{"role": "user", "content": "测试"}],
            label="attempt-timeout", budget=2.0,
        )
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(text, "本地桩回复")
        self.assertEqual(model, "fast-model")
        self.assertEqual(self.state["models"], ["slow-model", "fast-model"])
        records = self._read_audit("attempt-timeout")
        self.assertTrue(any(r.get("model") == "slow-model" and not r.get("ok")
                            for r in records))
        self.assertTrue(any(r.get("model") == "fast-model" and r.get("ok")
                            for r in records))

    async def test_05_私聊自定义budget覆盖配置总预算(self):
        # 配置总预算 10s；私聊只给 0.2s，必须按 0.2s 硬截止。
        self._write_config([["slow-model"]], attempt_timeout=5.0,
                           total_budget=10.0)
        self.state["slow_models"] = {"slow-model"}
        started = time.monotonic()
        with self.assertRaises(lp.LLMPoolExhausted):
            await lp.chat(
                [{"role": "user", "content": "测试"}],
                label="private-budget", budget=0.2,
            )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.2)
        self.assertTrue(any("总预算" in str(r.get("err") or "")
                            for r in self._read_audit("private-budget")))

    async def test_06_审计重定向到临时目录(self):
        self._write_config([["fast-model"]])
        await lp.chat([{"role": "user", "content": "测试"}],
                      label="audit-redirect", budget=2.0)
        self.assertEqual(lp.audit_log_path(), self.audit_path)
        self.assertTrue(self.audit_path.exists())
        self.assertNotEqual(
            lp.audit_log_path(), lp.LOG_PATH,
            "测试不得写生产 logs/llm_pool.jsonl")


if __name__ == "__main__":
    unittest.main(verbosity=2)
