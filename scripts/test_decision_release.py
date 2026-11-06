# -*- coding: utf-8 -*-
"""决策架构上线前离线测试（真实 unittest；**零真实网络 / 零 QQ action / 零模型调用**）。

覆盖范围（对应任务书 F 节 1~18）：
  A 决策台账：schema、trace_id（message_id 优先 / 缺失用 UUID）、单记录 + stages 数组、
    并发不交错、隐私（无消息正文 / 无 base64 / 无本地图片路径）、幂等收尾、兜底 incomplete。
  B 普通沉默与普通 policy_intent；@bot 正常回复；@bot 独立限流（60 秒窗口第 7 次阻断、
    5 秒最小间隔、假时钟越过窗口恢复、两群两用户互不影响、可被候选配置覆盖）；
    @bot 仍绕过 proactive_max_per_hour。
  C 重复 message_id 不产生第二次决策 / 发送（引擎层 + 入口层）。
  D 生成异常 / 空文本 / 安全过滤 / 精确去重 / 相似度去重 五种结局可区分；
    分类器异常记为 classifier_error（且不混入 classifier_low_confidence）。
  E 发送回执三态 ok/failed/unknown；unknown 不自动重发；echo 回执与群消息正确分流；
    图片发送不把 base64 / 本地路径写进台账。
  F 重启 / 新 DecisionEngine 实例后内存状态（上下文、冷却、限流、去重）重置；
    台账 trace_id 能串联同一次决策，policy_intent / generation / send 不混用。
  G 已被 command_handler 消费的指令不受 @ 限流影响。
  H 全程无真实网络 / 子进程；生产 config/ 哈希不变；生产 logs/decision_trace.jsonl 未被创建。

所有外部依赖都打桩：分类器、生成、表情检索、密度、时钟（de.time）、WebSocket。
台账重定向到临时目录（``set_trace_recorder(TraceRecorder(..., ignore_env=True))``）。
本文件不发送任何真实 QQ 消息、不连接任何真实 OneBot 端点、不启动任何服务。

跑法：``python -m unittest scripts.test_decision_release -v``
"""

import asyncio
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import shutil
import types
import unittest
import uuid
from unittest import mock

# ---------------------------------------------------------------------------
# 安全网：先关掉本进程的"生产决策台账写入"，再 import reverse_bot。
# 这样即使某个测试疏忽了重定向，也不会在 logs/decision_trace.jsonl 留下记录；
# 需要写台账的测试用 ignore_env=True 显式重定向到临时目录。
# ---------------------------------------------------------------------------
os.environ["DECISION_TRACE_DISABLED"] = "1"
os.environ.pop("DECISION_TRACE_PATH", None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROD_TRACE_PATH = os.path.join(ROOT, "logs", "decision_trace.jsonl")
PROD_TRACE_ABSENT_AT_START = not os.path.exists(PROD_TRACE_PATH)

# 本机 TEMP 由沙箱托管且不可写（E:\QQbot\logs 也不可写），测试临时文件统一放工作区
# 根目录下的专用目录，tearDownModule 负责删除。
TEMP_ROOT = os.path.join(ROOT, ".tmp_decision_release_tests")
_TEMP_ROOT_CREATED = False


def _tempdir(prefix: str):
    return _TempDir(prefix)


class _TempDir:
    """工作区内的临时目录（等价 TemporaryDirectory，但不用 mkdtemp）。

    本机沙箱下 ``tempfile.TemporaryDirectory`` 会 ``chmod 0700``，随后连创建者自己
    都无法枚举/删除该目录；测试全程改用普通 ``os.makedirs`` 目录 + 宽松清理。
    """

    def __init__(self, prefix: str = ""):
        self.name = os.path.join(TEMP_ROOT, "%s%s" % (prefix, uuid.uuid4().hex))
        os.makedirs(self.name, exist_ok=True)

    def cleanup(self):
        shutil.rmtree(self.name, ignore_errors=True)

# 本任务开始前的生产配置基线（发布清单 / 实验 snapshot 同一哈希）
BASELINE_CONFIG_SHA256 = {
    "config/decision_rules.json":
        "aa9354a50565a1a88ae0ea410620839fc19299e9b94aca6476eb4013d4b74d90",
    "config/group_features.json":
        "a832e006afe85a78b6f65bc1a180c6d472e622d8183c1a1bbd0051ca5bfdb27b",
}

import scripts.decision_engine as de  # noqa: E402
import scripts.decision_receipt as receipt_mod  # noqa: E402
import scripts.decision_trace as trace_mod  # noqa: E402
import reverse_bot as rb  # noqa: E402

from scripts.command_handler import processed_msg_ids, known_groups  # noqa: E402
from scripts.decision_receipt import (  # noqa: E402
    DecisionSendReceipt,
    ReceiptUnknownError,
    SendOutcome,
    legacy_note,
)
from scripts.decision_trace import (  # noqa: E402
    AtReplyLimiter,
    DecisionTrace,
    TraceRecorder,
    aggregate_send_status,
    resolve_at_reply_policy,
    SCHEMA_VERSION,
    SEND_FAILED,
    SEND_NOT_ATTEMPTED,
    SEND_OK,
    SEND_UNKNOWN,
    OUTCOME_CLASSIFIER_ERROR,
    OUTCOME_CONTENT_FILTERED,
    OUTCOME_DEDUPLICATED,
    OUTCOME_GENERATION_EMPTY,
    OUTCOME_GENERATION_ERROR,
    OUTCOME_INCOMPLETE,
    OUTCOME_REPLY_SENT,
    OUTCOME_SILENT,
)

BOT_QQ = rb.BOT_QQ


def _silence_loggers():
    """测试进程不要把日志写进生产 logs/（本仓 logs/ 通常也不可写，双保险）。"""
    for name in ("qqbot", "decision_engine", "user_profile"):
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


# ---------------------------------------------------------------------------
# 通用测试替身
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = float(start)

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class GenStub:
    """生成桩：按顺序返回给定值（最后一个值重复使用）；值可以是异常实例。"""

    def __init__(self, *values):
        self._values = list(values)
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        if not self._values:
            return "回答" + uuid.uuid4().hex
        value = self._values[0]
        if len(self._values) > 1:
            self._values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class UniqueGen:
    """每次返回不同文本，避开精确/相似度去重的干扰。"""

    def __init__(self, prefix: str = "回答"):
        self.prefix = prefix
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        return self.prefix + uuid.uuid4().hex


class FakeWebSocket:
    """模拟 OneBot 连接：记录发出的 action，可按需把 echo 回执喂回去。

    - ``echo`` + ``sender``：send() 时同步调用 ``sender.resolve()``（用于发送层单测）；
    - ``echo_via_frames``：send() 时把回执塞进 ``frames``，由入口 handler 的
      ``async for`` 循环读出（用于验证"回执在入口被正确路由、不当群消息"）；
    - 不发任何真实网络包。
    """

    def __init__(self, sender=None, echo=None, raise_on_send=False,
                 echo_via_frames=False, send_delay=0.0):
        self.sender = sender
        self.echo = echo
        self.raise_on_send = raise_on_send
        self.echo_via_frames = echo_via_frames
        self.send_delay = send_delay
        self.sent = []
        self.frames = []
        self.remote_address = ("127.0.0.1", 0)
        self._stop = False

    async def send(self, payload):
        data = json.loads(payload)
        self.sent.append(data)
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        if self.raise_on_send:
            raise RuntimeError("模拟写入异常")
        if self.sender is None or not data.get("echo"):
            return None
        if self.echo_via_frames:
            # frames 模拟真实 WS 收到的原始文本帧（handler 会 json.loads）
            self.frames.append(json.dumps({
                "status": "ok", "retcode": 0,
                "data": {"message_id": "srv-frame"}, "echo": data["echo"],
            }, ensure_ascii=False))
        elif self.echo is not None:
            response = dict(self.echo)
            response["echo"] = data["echo"]
            self.sender.resolve(self, response)
        return None

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        while not self._stop:
            if self.frames:
                yield self.frames.pop(0)
            else:
                await asyncio.sleep(0)

    def stop(self):
        self._stop = True


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


# ---------------------------------------------------------------------------
# 引擎级测试基类
# ---------------------------------------------------------------------------

class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = _tempdir("engine_")
        self.addCleanup(self._tmp.cleanup)
        self.trace_path = os.path.join(self._tmp.name, "decision_trace.jsonl")
        self.clock = FakeClock()
        self._orig_time = de.time
        de.time = types.SimpleNamespace(time=self.clock.time)
        self.addCleanup(self._restore_time)

    def _restore_time(self):
        de.time = self._orig_time

    # ---------- 引擎构造 ----------

    def _make_engine(self, *, trace=True, label="需要回复", score=0.99,
                     generation=None, density=5.0, sticker=None,
                     real_classifier=False, **config_overrides):
        engine = de.DecisionEngine()
        engine.config.update({
            "reply_cooldown_seconds": 0,
            "proactive_max_per_hour": 0,
            "proactive_window_seconds": 3600,
            "reply_threshold_default": 0.60,
            "threshold_swing": 0.0,
            "density_high_threshold": 10,
            "density_low_threshold": 3,
            "max_density_skip": 15,
            "classifier_fallback_threshold": 0.45,
        })
        engine.config.update(config_overrides)
        if trace:
            recorder = TraceRecorder(path=self.trace_path, enabled=True,
                                     ignore_env=True)
            engine.set_trace_recorder(recorder)
            self.addCleanup(recorder.close)
        engine.get_all_character_aliases = lambda: {}
        engine._get_density = lambda gid: float(density)
        engine._pre_filter = lambda message: None

        async def _pick_sticker_stub(*args, **kwargs):
            return sticker if sticker is not None else de._NoPick()

        engine._pick_sticker = _pick_sticker_stub
        if not real_classifier:
            async def _classify(text):
                return (label, score)
            engine._classify_needs_reply = _classify
        engine._generate_reply_async = generation if generation is not None else UniqueGen()
        return engine

    # ---------- 调用 / 断言辅助 ----------

    def _ask(self, engine, text="这是一句足够长的测试消息", *, gid="100880001",
             uid="2001", at=False, mid=None):
        return asyncio.run(engine.should_reply(
            message=text, user_id=uid, group_id=gid, is_at_bot=at,
            sender_nickname="测试用户", sticker_on=False, message_id=mid))

    def _records(self):
        return _read_jsonl(self.trace_path)

    def _raw(self):
        if not os.path.exists(self.trace_path):
            return ""
        with open(self.trace_path, "r", encoding="utf-8") as f:
            return f.read()

    def _last(self):
        records = self._records()
        self.assertTrue(records, "台账为空")
        return records[-1]

    def _finalize(self, engine, reply, *, status="ok", kind="text", retcode=0):
        """用**模拟回执**给 reply 收尾（不是真实发送；真实三态由 ReceiptTest 覆盖）。"""
        outcome = SendOutcome(
            status=status, kind=kind, retcode=retcode,
            message_id=("srv-1" if status == "ok" else None),
            error_kind=("" if status == "ok" else "onebot"),
        )
        engine.finish_reply(reply.trace.record["group_id"], reply, [(kind, outcome)],
                            is_at_bot=reply.trace.record["is_at_bot"])

    @staticmethod
    def _stages(record):
        return [entry["stage"] for entry in record["stages"]]

    def _stage(self, record, name):
        for entry in record["stages"]:
            if entry["stage"] == name:
                return entry
        self.fail("台账缺少 stage=%s（实际 %s）" % (name, self._stages(record)))


# ---------------------------------------------------------------------------
# A. 台账 schema / trace_id / 隐私 / 并发
# ---------------------------------------------------------------------------

class TraceLedgerTest(EngineTestBase):
    REQUIRED_FIELDS = (
        "schema_version", "trace_id", "timestamp", "ts_epoch",
        "group_id", "user_id", "message_id", "is_at_bot", "message_length",
        "rules_version", "config_sha256", "active_model", "classifier_model",
        "at_reply_policy", "stages", "classifier_label", "classifier_score",
        "dynamic_threshold", "classifier_error", "classifier_error_total",
        "policy_intent", "reached_generation", "generation_ok", "generation_empty",
        "generation_error", "content_filtered", "deduplicated", "dedupe_kind",
        "content_kind", "sticker_only", "send_status", "send_attempts",
        "decision_latency_ms", "latency_ms", "instrumentation_gap", "final_outcome",
    )

    def test_01_记录字段齐全且不含消息正文(self):
        secret = "机密正文ZZZ-不应出现在台账里-42"
        engine = self._make_engine(label="不需要回复", score=0.99)
        ok, reply = self._ask(engine, secret, mid=None)
        self.assertFalse(ok)
        self.assertIsNone(reply)

        record = self._last()
        for field in self.REQUIRED_FIELDS:
            self.assertIn(field, record, "台账缺字段 %s" % field)
        self.assertEqual(record["schema_version"], SCHEMA_VERSION)
        self.assertEqual(record["final_outcome"], OUTCOME_SILENT)
        self.assertFalse(record["policy_intent"])
        self.assertFalse(record["generation_ok"])
        self.assertEqual(record["send_status"], SEND_NOT_ATTEMPTED)
        self.assertIsNone(record["classifier_error"])
        self.assertEqual(record["message_length"], len(secret))
        self.assertIsNone(record["message_id"])
        self.assertRegex(record["trace_id"], r"^[0-9a-f]{32}$")
        self.assertIn("threshold", self._stages(record))
        self.assertEqual(record["at_reply_policy"]["at_reply_min_interval_seconds"], 5.0)
        self.assertEqual(record["at_reply_policy"]["at_reply_max_per_user_per_window"], 6)
        self.assertEqual(record["at_reply_policy"]["at_reply_window_seconds"], 60.0)
        self.assertTrue(record["rules_version"].startswith("decision_rules.json@sha256:"))
        self.assertEqual(len(record["config_sha256"]), 64)

        raw = self._raw()
        self.assertNotIn(secret, raw, "台账泄漏了消息正文")
        for forbidden_key in ("message_text", "raw_message", "reply_text", "content"):
            self.assertNotIn(forbidden_key, record)

    def test_02_trace_id优先使用message_id(self):
        engine = self._make_engine(label="不需要回复", score=0.99)
        mid = "1234567890"
        self._ask(engine, mid=mid)
        record = self._last()
        self.assertEqual(record["trace_id"], mid)
        self.assertEqual(record["message_id"], mid)

    def test_03_每次决策恰好一行(self):
        engine = self._make_engine(label="不需要回复", score=0.99)
        for index in range(3):
            self._ask(engine, mid="line-%d" % index)
        records = self._records()
        self.assertEqual(len(records), 3)
        self.assertEqual(len({r["trace_id"] for r in records}), 3)
        self.assertEqual(self._raw().count("\n"), 3)

    def test_04_并发决策不会交错成半行(self):
        engine = self._make_engine(label="需要回复", score=0.99)
        engine._generate_reply_async = UniqueGen("并发回答")

        async def scenario():
            tasks = [
                engine.should_reply(
                    message="并发测试消息 %d" % index,
                    user_id="u%d" % (index % 3),
                    group_id="g%d" % (index % 2),
                    message_id="concurrent-%d" % index,
                )
                for index in range(20)
            ]
            return await asyncio.gather(*tasks)

        results = asyncio.run(scenario())
        self.assertEqual(len(results), 20)
        for ok, reply in results:
            self.assertTrue(ok)
            self._finalize(engine, reply)
        records = self._records()
        self.assertEqual(len(records), 20)          # 每行都是完整 JSON
        self.assertEqual(len({r["trace_id"] for r in records}), 20)
        self.assertEqual({r["group_id"] for r in records}, {"g0", "g1"})

    def test_05_表情台账不含base64与本地路径(self):
        png_path = os.path.join(self._tmp.name, "private_alpha",
                                "secret_sticker_abc123.png")
        os.makedirs(os.path.dirname(png_path), exist_ok=True)
        with open(png_path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nfake-image-bytes")
        pick = types.SimpleNamespace(level="strong", path=png_path)
        engine = self._make_engine(label="需要回复", sticker=pick)
        ok, reply = self._ask(engine, at=True, mid="sticker-mid-1")
        self.assertTrue(ok)
        self.assertEqual(reply.text, "")
        self.assertEqual(reply.sticker, png_path)
        self._finalize(engine, reply, kind="sticker")

        record = self._last()
        self.assertEqual(record["content_kind"], "sticker")
        self.assertTrue(record["sticker_only"])
        self.assertFalse(record["reached_generation"])
        self.assertFalse(record["generation_ok"])
        self.assertTrue(record["policy_intent"])
        self.assertEqual(record["send_status"], SEND_OK)
        self.assertEqual(record["send_attempts"][0]["kind"], "sticker")

        raw = self._raw()
        self.assertNotIn("base64", raw)
        self.assertNotIn(png_path, raw)
        self.assertNotIn("secret_sticker_abc123", raw)
        self.assertNotIn("fake-image-bytes", raw)
        loaded = json.loads(raw.strip())
        self.assertNotIn("data", loaded["send_attempts"][0])

    def test_06_台账收尾幂等(self):
        engine = self._make_engine(label="需要回复", score=0.99)
        ok, reply = self._ask(engine, mid="idempotent-1")
        self.assertTrue(ok)
        self.assertEqual(self._records(), [])
        self._finalize(engine, reply)
        self.assertEqual(len(self._records()), 1)
        self.assertFalse(reply.trace.finish(final_outcome="silent",
                                            send_status="failed"))
        self.assertEqual(len(self._records()), 1)

    def test_07_漏收尾的台账被兜底标记incomplete(self):
        engine = self._make_engine(label="需要回复", score=0.99)
        engine._trace_orphan_seconds = 10
        ok, reply = self._ask(engine, mid="orphan-1")
        self.assertTrue(ok)
        self.assertEqual(self._records(), [])

        self.clock.advance(11)

        async def _silent(text):
            return ("不需要回复", 0.9)
        engine._classify_needs_reply = _silent
        self._ask(engine, mid="after-orphan")

        records = self._records()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["trace_id"], "orphan-1")
        self.assertEqual(records[0]["final_outcome"], OUTCOME_INCOMPLETE)
        self.assertEqual(records[0]["send_status"], SEND_UNKNOWN)
        self.assertTrue(records[0]["instrumentation_gap"])
        self.assertEqual(records[1]["final_outcome"], OUTCOME_SILENT)

    def test_08_台账默认关闭且不写生产路径(self):
        disabled_path = os.path.join(self._tmp.name, "disabled.jsonl")
        engine = de.DecisionEngine()
        engine.set_trace_recorder(TraceRecorder(path=disabled_path,
                                                enabled=False, ignore_env=True))

        async def _silent(text):
            return ("不需要回复", 0.9)
        engine._classify_needs_reply = _silent
        engine._get_density = lambda gid: 5.0
        engine._pre_filter = lambda message: None

        async def _pick_sticker_none(*args, **kwargs):
            return de._NoPick()

        engine._pick_sticker = _pick_sticker_none
        engine._generate_reply_async = UniqueGen()
        engine.config["reply_cooldown_seconds"] = 0
        ok, _ = self._ask(engine)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(disabled_path))

        # 构造时只给了 path 才启用；不放 path = 不写任何东西
        self.assertFalse(TraceRecorder().enabled)
        self.assertFalse(os.path.exists(PROD_TRACE_PATH))

    def test_08b_台账写失败不静默且不影响决策(self):
        blocked_parent = os.path.join(self._tmp.name, "blocked_parent")
        with open(blocked_parent, "w", encoding="utf-8") as f:
            f.write("我是一个文件，不是目录")
        blocked_path = os.path.join(blocked_parent, "decision_trace.jsonl")
        recorder = TraceRecorder(path=blocked_path, enabled=True, ignore_env=True)
        engine = self._make_engine(label="不需要回复", score=0.9, trace=False)
        engine.set_trace_recorder(recorder)

        stderr_buffer = io.StringIO()
        with contextlib.redirect_stderr(stderr_buffer):
            ok, _ = self._ask(engine, mid="write-fail-1")
        self.assertFalse(ok, "台账写失败不得影响决策结果")
        self.assertFalse(recorder.enabled)
        self.assertEqual(recorder.write_failures, 1)
        self.assertEqual(recorder.last_error_type, "FileExistsError")
        self.assertFalse(os.path.exists(blocked_path))
        self.assertIn("决策台账", stderr_buffer.getvalue())
        self.assertIn("写入失败", stderr_buffer.getvalue())

    def test_09_会话级trace_id串联且三语义不混用(self):
        engine = self._make_engine(label="不需要回复", score=0.95)
        self._ask(engine, mid="session-silent")

        async def _high(text):
            return ("需要回复", 0.99)
        engine._classify_needs_reply = _high
        ok, reply = self._ask(engine, mid="session-reply")
        self.assertTrue(ok)
        self._finalize(engine, reply)

        records = self._records()
        self.assertEqual(len(records), 2)
        self.assertEqual([r["trace_id"] for r in records],
                         ["session-silent", "session-reply"])
        silent, replied = records
        self.assertFalse(silent["policy_intent"])
        self.assertFalse(silent["generation_ok"])
        self.assertEqual(silent["send_status"], SEND_NOT_ATTEMPTED)
        self.assertTrue(replied["policy_intent"])
        self.assertTrue(replied["generation_ok"])
        self.assertEqual(replied["send_status"], SEND_OK)
        self.assertEqual(replied["final_outcome"], OUTCOME_REPLY_SENT)
        self.assertEqual(replied["send_attempts"][0]["kind"], "text")
        self.assertIn("send", self._stages(replied))
        self.assertIn("generation", self._stages(replied))


# ---------------------------------------------------------------------------
# B. 普通沉默 / policy_intent / @bot / 独立限流
# ---------------------------------------------------------------------------

class DecisionOutcomeTest(EngineTestBase):
    def test_10_普通沉默与普通policy_intent可区分(self):
        engine = self._make_engine(label="不需要回复", score=0.99)
        ok, reply = self._ask(engine, mid="silent-plain")
        self.assertFalse(ok)
        self.assertIsNone(reply)

        async def _high(text):
            return ("需要回复", 0.99)
        engine._classify_needs_reply = _high
        ok, reply = self._ask(engine, mid="intent-plain")
        self.assertTrue(ok)
        self.assertTrue(reply.text)
        self.assertEqual(reply.trace.policy_intent, True)
        self._finalize(engine, reply)

        records = self._records()
        self.assertEqual(records[0]["final_outcome"], OUTCOME_SILENT)
        self.assertIn("threshold", self._stages(records[0]))
        self.assertEqual(records[1]["final_outcome"], OUTCOME_REPLY_SENT)
        self.assertTrue(records[1]["policy_intent"])
        self.assertTrue(records[1]["generation_ok"])
        self.assertEqual(records[1]["content_kind"], "text")

    def test_11_艾特机器人正常回复(self):
        engine = self._make_engine(label="需要回复", score=0.99)
        ok, reply = self._ask(engine, at=True, mid="at-normal")
        self.assertTrue(ok)
        self.assertTrue(reply.text)
        record = reply.trace.record
        self.assertTrue(record["is_at_bot"])
        self.assertTrue(record["policy_intent"])
        self.assertTrue(record["generation_ok"])
        self.assertIn("at_bot", self._stages(record))
        self.assertIn("at_reply_limit", self._stages(record))
        self.assertTrue(self._stage(record, "at_reply_limit")["passed"])
        self._finalize(engine, reply)
        self.assertEqual(self._last()["final_outcome"], OUTCOME_REPLY_SENT)

    def test_12_六十秒窗口内第七次艾特被阻断(self):
        engine = self._make_engine(label="需要回复")
        generation = UniqueGen("at回答")
        engine._generate_reply_async = generation
        for index in range(6):
            if index:
                self.clock.advance(5)
            ok, reply = self._ask(engine, at=True, mid="at-window-%d" % index)
            self.assertTrue(ok, "第 %d 次 @ 应放行（%s）"
                            % (index + 1, engine.get_debug_info()))
            self._finalize(engine, reply)
        self.assertEqual(generation.calls, 6)

        self.clock.advance(5)                      # 仍在 60s 窗口内
        ok, reply = self._ask(engine, at=True, mid="at-window-7")
        self.assertFalse(ok, "第 7 次 @ 应被独立限流阻断")
        self.assertIsNone(reply)
        self.assertEqual(generation.calls, 6, "被阻断的请求不得进入模型生成")

        record = self._last()
        limited = self._stage(record, "at_rate_limited")
        self.assertFalse(limited["passed"])
        self.assertEqual(limited["reason"], "window_cap")
        self.assertEqual(record["final_outcome"], OUTCOME_SILENT)
        self.assertFalse(record["policy_intent"])
        self.assertEqual(record["send_status"], SEND_NOT_ATTEMPTED)

    def test_13_五秒最小间隔生效(self):
        engine = self._make_engine(label="需要回复")
        generation = UniqueGen("interval回答")
        engine._generate_reply_async = generation

        ok, reply = self._ask(engine, at=True, mid="interval-1")
        self.assertTrue(ok)
        self._finalize(engine, reply)

        self.clock.advance(4.9)
        ok, reply = self._ask(engine, at=True, mid="interval-2")
        self.assertFalse(ok)
        self.assertEqual(generation.calls, 1)
        limited = self._stage(self._last(), "at_rate_limited")
        self.assertEqual(limited["reason"], "min_interval")

        self.clock.advance(0.1)                   # 恰好满 5 秒
        ok, reply = self._ask(engine, at=True, mid="interval-3")
        self.assertTrue(ok, "满 5 秒后应恢复")
        self._finalize(engine, reply)

    def test_14_假时钟越过窗口后恢复(self):
        engine = self._make_engine(label="需要回复")
        generation = UniqueGen("recover回答")
        engine._generate_reply_async = generation
        for index in range(6):
            if index:
                self.clock.advance(5)
            ok, reply = self._ask(engine, at=True, mid="recover-%d" % index)
            self.assertTrue(ok)
            self._finalize(engine, reply)
        self.clock.advance(5)
        self.assertFalse(self._ask(engine, at=True, mid="recover-7")[0])

        self.clock.advance(31)                    # 1000/1005..1025 都已出窗口
        ok, reply = self._ask(engine, at=True, mid="recover-8")
        self.assertTrue(ok, "窗口滑过后应恢复")
        self._finalize(engine, reply)
        self.assertEqual(generation.calls, 7)

    def test_15_两群两用户计数互不影响(self):
        engine = self._make_engine(label="需要回复")
        engine._generate_reply_async = UniqueGen("cross回答")
        for index in range(6):
            if index:
                self.clock.advance(5)
            ok, reply = self._ask(engine, at=True, gid="gA", uid="uA",
                                  mid="gA-uA-%d" % index)
            self.assertTrue(ok)
            self._finalize(engine, reply)
        self.clock.advance(5)
        self.assertFalse(self._ask(engine, at=True, gid="gA", uid="uA",
                                   mid="gA-uA-7")[0])

        ok_b, reply_b = self._ask(engine, at=True, gid="gA", uid="uB", mid="gA-uB-1")
        self.assertTrue(ok_b, "同群不同用户应独立计数")
        self._finalize(engine, reply_b)
        ok_c, reply_c = self._ask(engine, at=True, gid="gB", uid="uA", mid="gB-uA-1")
        self.assertTrue(ok_c, "同用户不同群应独立计数")
        self._finalize(engine, reply_c)

        self.assertEqual(engine._at_reply_limiter.count("gA", "uA", self.clock.now), 6)
        self.assertEqual(engine._at_reply_limiter.count("gA", "uB", self.clock.now), 1)
        self.assertEqual(engine._at_reply_limiter.count("gB", "uA", self.clock.now), 1)

    def test_16_艾特仍绕过proactive每小时上限(self):
        engine = self._make_engine(label="需要回复", proactive_max_per_hour=1)
        engine._generate_reply_async = UniqueGen("proactive回答")
        ok, reply = self._ask(engine, mid="proactive-1")
        self.assertTrue(ok)
        self._finalize(engine, reply)

        ok, reply = self._ask(engine, mid="proactive-2")
        self.assertFalse(ok, "第二次主动应被 proactive 上限拦下")
        self.assertEqual(self._stage(self._last(), "proactive_cap")["passed"], False)

        self.clock.advance(1)
        ok, reply = self._ask(engine, at=True, mid="proactive-at")
        self.assertTrue(ok, "@bot 必须绕过 proactive 上限")
        self._finalize(engine, reply)
        record = self._last()
        self.assertNotIn("proactive_cap", self._stages(record))
        self.assertTrue(self._stage(record, "at_reply_limit")["passed"])

    def test_17_限流策略默认值可由候选配置覆盖(self):
        defaults = resolve_at_reply_policy({}, "any")
        self.assertEqual(defaults["at_reply_min_interval_seconds"], 5.0)
        self.assertEqual(defaults["at_reply_max_per_user_per_window"], 6)
        self.assertEqual(defaults["at_reply_window_seconds"], 60.0)

        flat = resolve_at_reply_policy({
            "at_reply_min_interval_seconds": "非法",
            "at_reply_max_per_user_per_window": 3,
        }, "any")
        self.assertEqual(flat["at_reply_min_interval_seconds"], 5.0)
        self.assertEqual(flat["at_reply_max_per_user_per_window"], 3)

        nested = resolve_at_reply_policy({
            "at_reply": {
                "at_reply_window_seconds": 30,
                "groups": {"g2": {"at_reply_max_per_user_per_window": 1}},
            }
        }, "g2")
        self.assertEqual(nested["at_reply_window_seconds"], 30)
        self.assertEqual(nested["at_reply_max_per_user_per_window"], 1)
        self.assertEqual(resolve_at_reply_policy(
            {"at_reply": {"groups": {"g2": {"at_reply_max_per_user_per_window": 1}}}},
            "g3")["at_reply_max_per_user_per_window"], 6)

        # 引擎实际按覆盖值执行
        engine = self._make_engine(
            label="需要回复",
            at_reply={"at_reply_max_per_user_per_window": 2,
                      "at_reply_min_interval_seconds": 0})
        for index in range(2):
            ok, reply = self._ask(engine, at=True, mid="override-%d" % index)
            self.assertTrue(ok)
            self._finalize(engine, reply)
        ok, _ = self._ask(engine, at=True, mid="override-3")
        self.assertFalse(ok)
        self.assertEqual(self._stage(self._last(), "at_rate_limited")["reason"],
                         "window_cap")

    def test_18_新实例重置上下文冷却限流与去重状态(self):
        engine_a = self._make_engine(label="需要回复", reply_cooldown_seconds=60)
        engine_a._generate_reply_async = UniqueGen("reset回答")
        for index in range(6):
            if index:
                self.clock.advance(5)
            ok, reply = self._ask(engine_a, at=True, mid="reset-a-%d" % index)
            self.assertTrue(ok)
            self._finalize(engine_a, reply)
        self.clock.advance(5)
        self.assertFalse(self._ask(engine_a, at=True, mid="reset-a-7")[0])
        self.assertEqual(engine_a._at_reply_limiter.count("100880001", "2001",
                                                          self.clock.now), 6)
        # 同一实例的主动路径应被冷却挡住（@ 回复刚刷新过冷却）
        ok, _ = self._ask(engine_a, at=False, mid="reset-a-proactive")
        self.assertFalse(ok)
        self.assertEqual(self._stage(self._last(), "cooldown")["passed"], False)

        engine_b = self._make_engine(label="需要回复", reply_cooldown_seconds=60)
        self.assertEqual(engine_b._at_reply_limiter.tracked_keys(), [])
        self.assertNotIn("100880001", engine_b._contexts)
        self.assertEqual(engine_b._last_reply_time, {})
        self.assertEqual(len(engine_b._seen_message_ids), 0)
        # 新实例：冷却与 @ 限流都必须重置（同一假时钟时刻即可放行）
        ok, reply = self._ask(engine_b, at=False, mid="reset-b-proactive")
        self.assertTrue(ok, "新实例的冷却状态必须重置")
        self._finalize(engine_b, reply)
        ok, reply = self._ask(engine_b, at=True, mid="reset-b-1")
        self.assertTrue(ok, "新实例的限流状态必须重置")
        self._finalize(engine_b, reply)


# ---------------------------------------------------------------------------
# C. 重复 message_id
# ---------------------------------------------------------------------------

class DuplicateMessageTest(EngineTestBase):
    def test_19_重复message_id不产生第二次决策(self):
        engine = self._make_engine(label="需要回复")
        generation = UniqueGen("dup回答")
        engine._generate_reply_async = generation

        ok, reply = self._ask(engine, mid="dup-mid-1")
        self.assertTrue(ok)
        self._finalize(engine, reply)

        ok, reply = self._ask(engine, mid="dup-mid-1")
        self.assertFalse(ok)
        self.assertIsNone(reply)
        self.assertEqual(generation.calls, 1, "重复 message_id 不得再次生成")
        records = self._records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["trace_id"], "dup-mid-1")

    def test_19b_同一message_id跨群互不影响(self):
        engine = self._make_engine(label="需要回复")
        generation = UniqueGen("跨群回答")
        engine._generate_reply_async = generation
        ok_a, reply_a = self._ask(engine, gid="gA", mid="shared-mid")
        ok_b, reply_b = self._ask(engine, gid="gB", mid="shared-mid")
        self.assertTrue(ok_a, "同一 message_id 在 A 群应正常决策")
        self.assertTrue(ok_b, "同一 message_id 在 B 群不得被 A 群去重误伤")
        self.assertEqual(generation.calls, 2)
        self._finalize(engine, reply_a)
        self._finalize(engine, reply_b)
        records = self._records()
        self.assertEqual(len(records), 2)
        self.assertEqual({r["trace_id"] for r in records}, {"shared-mid"})
        self.assertEqual({r["group_id"] for r in records}, {"gA", "gB"})
        self.assertEqual({r["final_outcome"] for r in records}, {OUTCOME_REPLY_SENT})


# ---------------------------------------------------------------------------
# D. 生成 / 过滤 / 去重 / 分类器异常
# ---------------------------------------------------------------------------

class GenerationOutcomeTest(EngineTestBase):
    def test_20_空文本结局(self):
        engine = self._make_engine(label="需要回复", generation=GenStub(""))
        ok, reply = self._ask(engine, mid="gen-empty")
        self.assertFalse(ok)
        record = self._last()
        self.assertEqual(record["final_outcome"], OUTCOME_GENERATION_EMPTY)
        self.assertTrue(record["policy_intent"])
        self.assertTrue(record["generation_empty"])
        self.assertFalse(record["generation_ok"])
        self.assertEqual(record["send_status"], SEND_NOT_ATTEMPTED)
        self.assertEqual(self._stage(record, "generation")["reason"], "empty")

    def test_21_模型异常结局且不抛出(self):
        engine = self._make_engine(label="需要回复",
                                   generation=GenStub(RuntimeError("模型炸了")))
        ok, reply = self._ask(engine, mid="gen-error")
        self.assertFalse(ok)
        record = self._last()
        self.assertEqual(record["final_outcome"], OUTCOME_GENERATION_ERROR)
        self.assertEqual(record["generation_error"]["type"], "RuntimeError")
        self.assertEqual(record["send_status"], SEND_NOT_ATTEMPTED)
        self.assertNotIn("模型炸了", self._raw())

    def test_22_内容安全过滤结局(self):
        engine = self._make_engine(label="需要回复",
                                   generation=GenStub("我们一起杀人吧"))
        ok, reply = self._ask(engine, mid="gen-filtered")
        self.assertFalse(ok)
        record = self._last()
        self.assertEqual(record["final_outcome"], OUTCOME_CONTENT_FILTERED)
        self.assertTrue(record["content_filtered"])
        self.assertFalse(record["generation_ok"])
        self.assertEqual(self._stage(record, "content_filter")["passed"], False)

    def test_23_精确去重结局(self):
        engine = self._make_engine(label="需要回复",
                                   generation=GenStub("今天天气真不错啊"))
        ok, reply = self._ask(engine, mid="dedupe-exact-1")
        self.assertTrue(ok)
        self._finalize(engine, reply)
        ok, reply = self._ask(engine, mid="dedupe-exact-2")
        self.assertFalse(ok)
        record = self._last()
        self.assertEqual(record["final_outcome"], OUTCOME_DEDUPLICATED)
        self.assertEqual(record["dedupe_kind"], "exact")
        self.assertEqual(self._stage(record, "dedupe_exact")["passed"], False)

    def test_24_相似度去重结局(self):
        engine = self._make_engine(
            label="需要回复",
            generation=GenStub("今天天气真不错啊", "今天天气真不错呢"))
        ok, reply = self._ask(engine, mid="dedupe-similar-1")
        self.assertTrue(ok)
        self._finalize(engine, reply)
        ok, reply = self._ask(engine, mid="dedupe-similar-2")
        self.assertFalse(ok)
        record = self._last()
        self.assertEqual(record["final_outcome"], OUTCOME_DEDUPLICATED)
        self.assertEqual(record["dedupe_kind"], "similar")
        self.assertEqual(self._stage(record, "dedupe_similar")["passed"], False)

    def test_25_五种结局互不混淆(self):
        engine = self._make_engine(label="需要回复", generation=GenStub(""))
        self.assertFalse(self._ask(engine, mid="mix-empty")[0])
        empty = self._last()["final_outcome"]

        engine = self._make_engine(label="需要回复",
                                   generation=GenStub(RuntimeError("生成失败")))
        self.assertFalse(self._ask(engine, mid="mix-error")[0])
        gen_error = self._last()["final_outcome"]

        engine = self._make_engine(label="需要回复",
                                   generation=GenStub("我们一起杀人吧"))
        self.assertFalse(self._ask(engine, mid="mix-filtered")[0])
        filtered = self._last()["final_outcome"]

        engine = self._make_engine(label="需要回复",
                                   generation=GenStub("重复文本测试ABC"))
        ok, reply = self._ask(engine, mid="mix-dup-1")
        self.assertTrue(ok)
        self._finalize(engine, reply)
        self.assertFalse(self._ask(engine, mid="mix-dup-2")[0])
        exact_record = self._last()

        engine = self._make_engine(
            label="需要回复",
            generation=GenStub("今天天气真不错啊", "今天天气真不错呢"))
        ok, reply = self._ask(engine, mid="mix-sim-1")
        self.assertTrue(ok)
        self._finalize(engine, reply)
        self.assertFalse(self._ask(engine, mid="mix-sim-2")[0])
        similar_record = self._last()

        self.assertEqual(empty, OUTCOME_GENERATION_EMPTY)
        self.assertEqual(gen_error, OUTCOME_GENERATION_ERROR)
        self.assertEqual(filtered, OUTCOME_CONTENT_FILTERED)
        self.assertEqual(exact_record["final_outcome"], OUTCOME_DEDUPLICATED)
        self.assertEqual(exact_record["dedupe_kind"], "exact")
        self.assertEqual(similar_record["final_outcome"], OUTCOME_DEDUPLICATED)
        self.assertEqual(similar_record["dedupe_kind"], "similar")
        self.assertEqual(len({empty, gen_error, filtered, OUTCOME_DEDUPLICATED}), 4)
        # 分类器异常必须是独立结局，不能与"低置信沉默"共用
        self.assertNotEqual(OUTCOME_CLASSIFIER_ERROR, OUTCOME_SILENT)

    def test_26_分类器异常记录为classifier_error且fail_closed(self):
        engine = self._make_engine(label="需要回复", real_classifier=True)
        engine._get_density = lambda gid: 12.0     # 密度高也不得"降级放行"

        class BadClassifier:
            model_name = "bad-classifier"

            def classify(self, text, labels):
                raise ValueError("分类器炸了：%s" % text)

        async def _get_bad():
            return BadClassifier()
        engine._get_classifier = _get_bad

        ok, reply = self._ask(engine, "带正文的敏感测试消息ABC", mid="cls-error-1")
        self.assertFalse(ok, "分类器异常必须 fail-closed 不回复")
        self.assertIsNone(reply)
        self.assertEqual(engine.classifier_error_count, 1)

        record = self._last()
        self.assertEqual(record["final_outcome"], OUTCOME_CLASSIFIER_ERROR)
        self.assertEqual(record["classifier_error"]["type"], "ValueError")
        self.assertEqual(record["classifier_error"]["phase"], "inference")
        self.assertEqual(record["classifier_error_total"], 1)
        self.assertEqual(record["classifier_label"], "不需要回复")
        self.assertEqual(record["classifier_score"], 0.0)
        self.assertNotIn("classifier_low_confidence", self._stages(record))
        self.assertEqual(record["send_status"], SEND_NOT_ATTEMPTED)
        self.assertNotIn("带正文的敏感测试消息ABC", self._raw())
        self.assertNotIn("分类器炸了", self._raw())

        # 分类器懒加载失败同样按 classifier_error fail-closed 记录（不会冒泡成整轮报错）
        engine2 = self._make_engine(label="需要回复", real_classifier=True)

        async def _get_load_failed():
            raise RuntimeError("分类器模型加载失败")

        engine2._get_classifier = _get_load_failed
        engine2._get_density = lambda gid: 5.0
        ok, reply = self._ask(engine2, "另一条消息", mid="cls-error-load")
        self.assertFalse(ok, "分类器加载失败也必须 fail-closed")
        self.assertIsNone(reply)
        record2 = self._last()
        self.assertEqual(record2["final_outcome"], OUTCOME_CLASSIFIER_ERROR)
        self.assertEqual(record2["classifier_error"]["type"], "RuntimeError")
        self.assertEqual(engine2.classifier_error_count, 1)


# ---------------------------------------------------------------------------
# E. 发送回执三态（全部使用模拟 WebSocket）
# ---------------------------------------------------------------------------

class ReceiptTest(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_27_发送成功ok只认OneBot明确回执(self):
        sender = DecisionSendReceipt(timeout=0.2)
        ws = FakeWebSocket(sender=sender, echo={
            "status": "ok", "retcode": 0, "data": {"message_id": "srv-1"}})
        outcome = self._run(sender.send_text(ws, 100, "你好", at_user="u1"))
        self.assertEqual(outcome.status, SEND_OK)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.message_id, "srv-1")
        self.assertEqual(outcome.retcode, 0)
        self.assertEqual(outcome.kind, "text")
        self.assertEqual(sender.pending, {})
        self.assertEqual(len(ws.sent), 1)
        action = ws.sent[0]
        self.assertEqual(action["action"], "send_group_msg")
        self.assertEqual(action["params"]["group_id"], 100)
        self.assertTrue(action["params"]["message"].startswith("[CQ:at,qq=u1] 你好"))
        self.assertTrue(action["echo"].startswith(receipt_mod.ECHO_PREFIX))

    def test_28_明确失败failed且不重试(self):
        sender = DecisionSendReceipt(timeout=0.2)
        ws = FakeWebSocket(sender=sender, echo={
            "status": "failed", "retcode": 100, "data": None})
        outcome = self._run(sender.send_text(ws, 100, "你好"))
        self.assertEqual(outcome.status, SEND_FAILED)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.retcode, 100)
        self.assertEqual(outcome.error_kind, "onebot")
        self.assertEqual(len(ws.sent), 1, "failed 不在本任务的重试范围内")

    def test_29_超时unknown且不重发(self):
        sender = DecisionSendReceipt(timeout=0.02)
        ws = FakeWebSocket(sender=None, echo=None)
        outcome = self._run(sender.send_text(ws, 100, "你好"))
        self.assertEqual(outcome.status, SEND_UNKNOWN)
        self.assertEqual(outcome.error_kind, "timeout")
        self.assertEqual(sender.pending, {})
        self.assertEqual(len(ws.sent), 1, "unknown 绝不允许自动重发")
        self.assertIn("超时", outcome.error)

    def test_30_写入异常unknown(self):
        sender = DecisionSendReceipt(timeout=0.2)
        ws = FakeWebSocket(raise_on_send=True)
        outcome = self._run(sender.send_text(ws, 100, "你好"))
        self.assertEqual(outcome.status, SEND_UNKNOWN)
        self.assertEqual(outcome.error_kind, "write")
        self.assertEqual(sender.pending, {})
        self.assertEqual(len(ws.sent), 1)

    def test_30b_写入超时unknown(self):
        sender = DecisionSendReceipt(timeout=0.02)
        ws = FakeWebSocket(send_delay=0.2)
        outcome = self._run(sender.send_text(ws, 100, "你好"))
        self.assertEqual(outcome.status, SEND_UNKNOWN)
        self.assertEqual(outcome.error_kind, "write_timeout")
        self.assertEqual(sender.pending, {})
        self.assertEqual(len(ws.sent), 1, "写入超时也不得重发")

    def test_31_断连时未完成发送记为unknown(self):
        sender = DecisionSendReceipt(timeout=5.0)
        ws = FakeWebSocket(sender=None, echo=None)

        async def scenario():
            task = asyncio.create_task(sender.send_text(ws, 100, "你好"))
            for _ in range(50):
                if ws.sent:
                    break
                await asyncio.sleep(0)
            self.assertEqual(len(ws.sent), 1)
            sender.disconnect(ws)
            return await task

        outcome = self._run(scenario())
        self.assertEqual(outcome.status, SEND_UNKNOWN)
        self.assertEqual(outcome.error_kind, "disconnected")
        self.assertEqual(sender.pending, {})

    def test_32_非法回执算failed(self):
        sender = DecisionSendReceipt(timeout=0.2)
        ws = FakeWebSocket(sender=sender, echo={"status": "what", "retcode": None})
        outcome = self._run(sender.send_text(ws, 100, "你好"))
        self.assertEqual(outcome.status, SEND_FAILED)
        self.assertEqual(outcome.error_kind, "onebot")

    def test_33_echo与群消息正确分流(self):
        sender = DecisionSendReceipt(timeout=0.2)
        ws = FakeWebSocket(sender=None, echo=None)

        async def scenario():
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            sender.pending[(ws, "decision:abc")] = future
            handled = sender.resolve(ws, {"echo": "decision:abc", "status": "ok",
                                          "retcode": 0, "data": {}})
            group_event = {"post_type": "message", "message_type": "group",
                           "group_id": 1, "raw_message": "hi"}
            not_echo = sender.resolve(ws, group_event)
            foreign = sender.resolve(ws, {"echo": "interaction:zzz", "status": "ok"})
            late = sender.resolve(ws, {"echo": "decision:late", "status": "ok"})
            sender.pending.pop((ws, "decision:abc"), None)
            return handled, future.done(), not_echo, foreign, late

        handled, future_done, not_echo, foreign, late = self._run(scenario())
        self.assertTrue(handled)
        self.assertTrue(future_done)
        self.assertFalse(not_echo, "群消息不能被当成回执吞掉")
        self.assertFalse(foreign, "其它前缀的回执不归本层")
        self.assertTrue(late, "迟到/未知回执也要吞掉，不能进消息路由")

    def test_34_图片发送payload有base64但台账不泄漏(self):
        tmp = _tempdir("receipt_image_")
        self.addCleanup(tmp.cleanup)
        png_path = os.path.join(tmp.name, "sticker_private_999.png")
        with open(png_path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\npayload-bytes")
        sender = DecisionSendReceipt(timeout=0.2)
        ws = FakeWebSocket(sender=sender, echo={
            "status": "ok", "retcode": 0, "data": {"message_id": "img-1"}})

        outcome = self._run(sender.send_image(ws, 100, png_path, sub_type=1))
        self.assertEqual(outcome.status, SEND_OK)
        self.assertEqual(outcome.kind, "sticker")
        message = ws.sent[0]["params"]["message"][0]
        self.assertEqual(message["type"], "image")
        self.assertTrue(message["data"]["file"].startswith("base64://"))
        self.assertEqual(message["data"]["sub_type"], 1)

        trace_path = os.path.join(tmp.name, "trace.jsonl")
        recorder = TraceRecorder(path=trace_path, enabled=True, ignore_env=True)
        self.addCleanup(recorder.close)
        trace = recorder.new_trace(message_id="img-mid", group_id="100",
                                   user_id="2001", is_at_bot=True,
                                   message_length=8)
        trace.policy_intent = True
        trace.content_kind = "sticker"
        trace.sticker_only = True
        trace.finish_after_send([("sticker", outcome)])
        with open(trace_path, "r", encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("base64", raw)
        self.assertNotIn(png_path, raw)
        self.assertNotIn("payload-bytes", raw)
        record = json.loads(raw.strip())
        self.assertEqual(record["send_status"], SEND_OK)
        self.assertEqual(record["final_outcome"], OUTCOME_REPLY_SENT)

    def test_35_发送状态聚合优先级(self):
        self.assertEqual(aggregate_send_status([]), SEND_NOT_ATTEMPTED)
        self.assertEqual(aggregate_send_status([SEND_OK]), SEND_OK)
        self.assertEqual(aggregate_send_status([SEND_OK, SEND_FAILED]), SEND_FAILED)
        self.assertEqual(aggregate_send_status([SEND_FAILED, SEND_UNKNOWN]), SEND_UNKNOWN)
        self.assertEqual(aggregate_send_status([SEND_OK, SEND_UNKNOWN]), SEND_UNKNOWN)

    def test_36_legacy路径声明保留(self):
        note = legacy_note()
        self.assertIn("legacy", note)
        self.assertIn("DecisionEngine", note)
        self.assertIn("指令", note)

    def test_37_回执模块不引入私聊运行时(self):
        source_path = os.path.join(ROOT, "scripts", "decision_receipt.py")
        with open(source_path, "r", encoding="utf-8") as f:
            source = f.read()
        # 只检查真实的 import 语句；文档里"参考私聊 rpc"的说明不算依赖
        self.assertIsNone(
            re.search(r"(?m)^\s*(?:from|import)\s+[\w.]*private_chat", source))
        self.assertIsNone(
            re.search(r"(?m)^\s*(?:from|import)\s+websockets?\b", source))


# ---------------------------------------------------------------------------
# F/G. 入口层（reverse_bot.handle_message / handler）
# ---------------------------------------------------------------------------

class EntryIntegrationTest(EngineTestBase):
    def setUp(self):
        super().setUp()
        self.features = {}
        self.generation = UniqueGen("入口回答")
        self.engine = self._make_engine(label="需要回复")
        self.engine._generate_reply_async = self.generation
        self.sender = DecisionSendReceipt(timeout=0.05)
        self.legacy_sends = []
        self.command_calls = []
        self.decision_calls = []
        self._patches = []
        self._install_patches()

        # 包装 should_reply 以统计入口是否真的调用了决策引擎
        original_should_reply = self.engine.should_reply

        async def _spy(*args, **kwargs):
            self.decision_calls.append(1)
            return await original_should_reply(*args, **kwargs)
        self.engine.should_reply = _spy
        processed_msg_ids.clear()
        known_groups.clear()

    def _install_patches(self):
        def patch(name, value):
            patcher = mock.patch.object(rb, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
            self._patches.append(patcher)

        patch("decision_engine", self.engine)
        patch("decision_sender", self.sender)
        patch("_feature_on", self._feature_on)
        patch("handle_command", self._fake_handle_command)
        patch("send_message", self._legacy_send_message)
        patch("send_group_image", self._legacy_send_image)
        patch("send_group_file", self._legacy_send_file)
        patch("group_history", types.SimpleNamespace(
            record=lambda *a, **k: None, configure=lambda: {}))
        patch("reference_mod", types.SimpleNamespace(
            is_enabled_for=lambda gid: False))
        patch("PROFILE_ENABLED", False)
        patch("_sticker_send_sub_type", lambda: 0)
        patch("has_pending_retry", lambda: False)
        patch("set_napcat_connected", lambda connected: None)
        patch("_watchdog", types.SimpleNamespace(
            on_connected=lambda: None, on_disconnected=lambda: None))

    # ---------- 打桩 ----------

    def _feature_on(self, gid, key):
        return bool(self.features.get(key, False))

    async def _fake_handle_command(self, websocket, data, clean_message, raw_message):
        self.command_calls.append({"clean_message": clean_message,
                                   "raw_message": raw_message})
        return bool(self.features.get("_command_handled", False))

    async def _legacy_send_message(self, websocket, group_id, message, at_user=None):
        self.legacy_sends.append(("text", group_id, message, at_user))
        return True

    async def _legacy_send_image(self, websocket, group_id, image_path, sub_type=0):
        self.legacy_sends.append(("image", group_id, image_path, sub_type))

    async def _legacy_send_file(self, websocket, group_id, file_path):
        self.legacy_sends.append(("file", group_id, file_path))

    # ---------- 事件构造 ----------

    def _event(self, text="这是一句足够长的测试消息", *, mid=None,
               gid=100880001, uid=2001, at=True, nickname="测试用户"):
        if mid is None:
            mid = int(uuid.uuid4().int % 10 ** 9)
        raw = ("[CQ:at,qq=%s] %s" % (BOT_QQ, text)) if at else text
        return {
            "post_type": "message", "message_type": "group",
            "group_id": gid, "user_id": uid, "message_id": mid,
            "raw_message": raw, "self_id": BOT_QQ,
            "sender": {"nickname": nickname, "card": "", "user_id": uid},
        }

    def _handle(self, event, websocket):
        asyncio.run(rb.handle_message(websocket, event))

    # ---------- 用例 ----------

    def test_38_指令处理不受新限流影响(self):
        self.features.update({"commands": True, "decision_engine": True,
                              "_command_handled": True})
        gid, uid = "100880001", "2001"
        policy = resolve_at_reply_policy(self.engine.config, gid)
        policy["at_reply_min_interval_seconds"] = 0   # 只为快速占满窗口名额
        for _ in range(6):
            allowed, _, _ = self.engine._at_reply_limiter.acquire(
                gid, uid, self.clock.now, policy)
            self.assertTrue(allowed)
        self.assertEqual(self.engine._at_reply_limiter.count(gid, uid, self.clock.now), 6)

        ws = FakeWebSocket()
        self._handle(self._event(text="菜单", mid=900001), ws)

        self.assertEqual(len(self.command_calls), 1)
        self.assertEqual(self.command_calls[0]["clean_message"], "菜单")
        self.assertEqual(self.decision_calls, [], "指令命中后不得进入决策引擎")
        self.assertEqual(self.engine._at_reply_limiter.count(gid, uid, self.clock.now), 6)
        self.assertEqual(ws.sent, [])
        self.assertEqual(self.legacy_sends, [])     # 指令回复由 handle_command 自己发

    def test_39_入口重复message_id不产生第二次决策发送(self):
        self.features["decision_engine"] = True
        ws = FakeWebSocket(sender=self.sender, echo={
            "status": "ok", "retcode": 0, "data": {"message_id": "srv-1"}})
        event = self._event(mid=424242)
        self._handle(event, ws)
        # 清掉入口层的旧去重集合，强制第二次投递进入 DecisionEngine，
        # 这样断言的是**新引擎层**的 message_id 去重（而不是 legacy 入口去重）。
        processed_msg_ids.clear()
        self._handle(event, ws)

        self.assertEqual(len(self.decision_calls), 2,
                         "第二次应真正进入引擎（被引擎层去重拦下）")
        self.assertEqual(self.generation.calls, 1)
        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(len(self._records()), 1)
        self.assertEqual(self._records()[0]["trace_id"], "424242")

    def test_40_入口发送ok写入台账(self):
        self.features["decision_engine"] = True
        ws = FakeWebSocket(sender=self.sender, echo={
            "status": "ok", "retcode": 0, "data": {"message_id": "srv-ok"}})
        self._handle(self._event(mid=888888), ws)

        self.assertEqual(len(ws.sent), 1, "只应有一个 send_group_msg action")
        self.assertEqual(ws.sent[0]["action"], "send_group_msg")
        self.assertIn("echo", ws.sent[0])
        record = self._last()
        self.assertEqual(record["trace_id"], "888888")
        self.assertTrue(record["is_at_bot"])
        self.assertTrue(record["policy_intent"])
        self.assertTrue(record["generation_ok"])
        self.assertEqual(record["send_status"], SEND_OK)
        self.assertEqual(record["final_outcome"], OUTCOME_REPLY_SENT)
        self.assertEqual(record["send_attempts"][0]["status"], "ok")
        self.assertIn("send", self._stages(record))
        self.assertEqual(self.legacy_sends, [], "决策回复不得再走 legacy send_message")

    def test_41_入口unknown不自动重发(self):
        self.features["decision_engine"] = True
        self.sender.timeout = 0.01
        ws = FakeWebSocket(sender=None, echo=None)
        self._handle(self._event(mid=777777), ws)

        self.assertEqual(len(ws.sent), 1, "unknown 不得自动重发")
        record = self._last()
        self.assertEqual(record["send_status"], SEND_UNKNOWN)
        self.assertEqual(record["final_outcome"], "reply_unknown")
        self.assertFalse(record["instrumentation_gap"])
        self.assertEqual(self.generation.calls, 1)

    def test_42_入口第七次艾特在生成前被阻断(self):
        self.features["decision_engine"] = True
        ws = FakeWebSocket(sender=self.sender, echo={
            "status": "ok", "retcode": 0, "data": {"message_id": "srv"}})
        for index in range(6):
            if index:
                self.clock.advance(5)
            self._handle(self._event(mid=500000 + index), ws)
        self.assertEqual(self.generation.calls, 6)
        self.assertEqual(len(ws.sent), 6)

        self.clock.advance(5)
        self._handle(self._event(mid=500099), ws)
        self.assertEqual(self.generation.calls, 6, "第 7 次 @ 必须不进入生成")
        self.assertEqual(len(ws.sent), 6)
        records = self._records()
        self.assertEqual(len(records), 7)
        last = records[-1]
        self.assertEqual(last["final_outcome"], OUTCOME_SILENT)
        limited = self._stage(last, "at_rate_limited")
        self.assertEqual(limited["reason"], "window_cap")
        self.assertEqual(last["send_status"], SEND_NOT_ATTEMPTED)

    def test_42b_明确失败后新消息可以发送相同回答(self):
        self.features["decision_engine"] = True
        async def same_reply(*args, **kwargs):
            return "这是一条发送失败后仍应允许发送的回答"
        self.engine._generate_reply_async = same_reply
        ws = FakeWebSocket(sender=self.sender, echo={"status": "failed", "retcode": 100})
        self._handle(self._event(mid=510001, uid=2001), ws)
        self.assertEqual(self.engine._last_reply, {})
        self.assertEqual(self.engine._last_reply_time, {})
        self._handle(self._event(mid=510002, uid=2002), ws)
        self.assertEqual(len(ws.sent), 2)
        self.assertEqual([row["send_status"] for row in self._records()], ["failed", "failed"])

    def test_43_echo回执在入口被路由且不当群消息(self):
        self.sender.timeout = 0.5
        injected = []

        async def fake_safe_handle(websocket, data, deadline=None):
            injected.append(data)

        async def scenario():
            ws = FakeWebSocket(sender=self.sender, echo=None,
                              echo_via_frames=True)
            with mock.patch.object(rb, "_safe_handle_message", fake_safe_handle):
                handler_task = asyncio.create_task(rb.handler(ws))
                outcome = await self.sender.send_text(ws, 100, "hello")
                ws.frames.append(json.dumps({
                    "post_type": "message", "message_type": "group",
                    "group_id": 100, "user_id": 1, "message_id": 9,
                    "raw_message": "hi", "self_id": BOT_QQ,
                    "sender": {"nickname": "n"},
                }, ensure_ascii=False))
                await asyncio.sleep(0.05)
                ws.stop()
                await asyncio.wait_for(handler_task, 2.0)
            return outcome, ws

        outcome, ws = asyncio.run(scenario())
        self.assertEqual(outcome.status, SEND_OK)
        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(len(injected), 1, "只有群消息应进入消息处理")
        self.assertEqual(injected[0]["message_id"], 9)

    def test_44_入口流程全程无真实网络与子进程(self):
        self.features["decision_engine"] = True
        ws = FakeWebSocket(sender=self.sender, echo={
            "status": "ok", "retcode": 0, "data": {"message_id": "srv"}})

        async def scenario():
            with mock.patch("socket.socket.connect",
                            side_effect=AssertionError("不得创建真实 socket")), \
                 mock.patch("asyncio.create_subprocess_exec",
                            side_effect=AssertionError("不得启动模型子进程")), \
                 mock.patch("websockets.connect",
                            side_effect=AssertionError("不得连接真实 OneBot")):
                await rb.handle_message(ws, self._event(mid=666666))
            return ws

        ws = asyncio.run(scenario())
        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(self._last()["send_status"], SEND_OK)


# ---------------------------------------------------------------------------
# H. 生产安全
# ---------------------------------------------------------------------------

class ProductionSafetyTest(unittest.TestCase):
    def test_45_生产配置哈希未改变(self):
        for rel_path, expected in BASELINE_CONFIG_SHA256.items():
            path = os.path.join(ROOT, rel_path)
            with open(path, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            self.assertEqual(digest, expected, "%s 被修改了" % rel_path)

        engine = de.DecisionEngine()
        self.assertEqual(engine.config_sha256,
                         BASELINE_CONFIG_SHA256["config/decision_rules.json"])

    def test_46_生产决策台账未被创建(self):
        if PROD_TRACE_ABSENT_AT_START:
            self.assertFalse(os.path.exists(PROD_TRACE_PATH),
                             "测试过程中生成了生产决策台账 %s" % PROD_TRACE_PATH)
        self.assertFalse(TraceRecorder().enabled,
                         "默认构造的 TraceRecorder 必须关闭")

    def test_47_环境安全网生效(self):
        self.assertEqual(os.environ.get(trace_mod.TRACE_DISABLED_ENV), "1")
        tmp = _tempdir("trace_env_")
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "trace.jsonl")
        self.assertFalse(TraceRecorder(path=path, enabled=True).enabled,
                         "环境安全网必须能压住显式 enabled")
        self.assertTrue(TraceRecorder(path=path, enabled=True,
                                      ignore_env=True).enabled)
        self.assertFalse(os.path.exists(path))

    def test_48_入口接线使用回执层且默认关闭生产台账(self):
        self.assertIsInstance(rb.decision_sender, DecisionSendReceipt)
        self.assertIsInstance(rb.decision_engine.trace_recorder, TraceRecorder)
        self.assertFalse(rb.decision_engine.trace_recorder.enabled,
                         "本测试进程不得启用生产台账")
        self.assertEqual(rb.decision_engine.DEFAULT_MODEL,
                         rb.decision_engine.get_active_model()["model"])


def setUpModule():
    """在**工作区**内建测试临时根目录（本机 TEMP/logs 被沙箱置为不可写）。"""
    global _TEMP_ROOT_CREATED
    os.makedirs(TEMP_ROOT, exist_ok=True)
    _TEMP_ROOT_CREATED = True


def tearDownModule():
    """模块级最后一道检查：测试临时目录已清理、生产 logs/ 无决策台账。"""
    global _TEMP_ROOT_CREATED
    if _TEMP_ROOT_CREATED:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        _TEMP_ROOT_CREATED = False
    if PROD_TRACE_ABSENT_AT_START and os.path.exists(PROD_TRACE_PATH):
        raise AssertionError("测试污染了生产决策台账: %s" % PROD_TRACE_PATH)


if __name__ == "__main__":
    unittest.main(verbosity=2)
