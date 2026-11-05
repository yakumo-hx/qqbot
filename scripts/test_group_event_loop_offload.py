# -*- coding: utf-8 -*-
"""群聊事件循环同步阻塞消除的离线回归测试。

覆盖（第一批任务书）：
  1. 分类器构造/推理 / 话题 encode_batch 不在事件循环线程执行；
  2. 分类阻塞期间，事件循环里的其他任务仍能运行（threading.Event 同步，不靠毫秒阈值）；
  3. 分类结果与异常 fail-closed 语义不变（含懒加载失败）；
  4. 多群并发：分类器只初始化一次，共享模型不并发推理；
  5. 群聊图片下载/转码不在事件循环线程执行，失败仍返回原有空降级结果。

约束：零真实网络、零 QQ、零真实分类模型；分类器用假模块/假对象，
图片下载与 Pillow 转码全部打桩。跑法：

    python -m unittest scripts.test_group_event_loop_offload -v
"""

import asyncio
import contextlib
import logging
import os
import sys
import threading
import types
import unittest
import uuid
from unittest import mock

# 与 test_decision_release 相同：导入 reverse_bot 前关掉生产台账写入。
os.environ["DECISION_TRACE_DISABLED"] = "1"
os.environ.pop("DECISION_TRACE_PATH", None)

import numpy as np  # noqa: E402

import scripts.decision_engine as de  # noqa: E402
import reverse_bot as rb  # noqa: E402
import scripts.sticker as sticker_mod  # noqa: E402
import scripts.vision as vision_mod  # noqa: E402
from scripts import image_cache, image_utils  # noqa: E402
from scripts.commands import _state  # noqa: E402

MAIN_THREAD_ID = threading.get_ident()


def _silence_loggers():
    """测试进程不要把日志写进生产 logs/。"""
    for name in ("qqbot", "decision_engine", "scripts.decision_engine", "user_profile"):
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


class _FakeClassifier:
    """同步假分类器：记录调用线程，可注入阻塞/异常行为。"""

    model_name = "fake-classifier"

    def __init__(self, classify=None, encode=None):
        self._classify = classify or (lambda text, labels: ("需要回复", 0.9))
        self._encode = encode or (lambda texts: np.ones((len(texts), 4)))
        self.classify_calls = []
        self.encode_calls = []

    def classify(self, text, labels):
        self.classify_calls.append((text, list(labels), threading.get_ident()))
        return self._classify(text, labels)

    def encode_batch(self, texts):
        self.encode_calls.append((list(texts), threading.get_ident()))
        return self._encode(list(texts))


class _TrackingRLock:
    """可重入锁包装：记录第二次外层进入正在等锁，用来确定性地证明串行。

    只在测试里替换引擎/分类器自身的锁使用；若被测代码没有在共享锁上串行，
    第二个调用不会进入本包装，``second_waiting`` 永远不会被置位。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._guard = threading.Lock()
        self._count = 0
        self.second_waiting = threading.Event()

    def __enter__(self):
        with self._guard:
            self._count += 1
            index = self._count
        if index == 2:
            self.second_waiting.set()
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()
        return False


def _make_engine(classifier=None, stub_sticker=True):
    engine = de.DecisionEngine()
    engine.config.update({
        "reply_cooldown_seconds": 0,
        "proactive_max_per_hour": 0,
        "proactive_window_seconds": 3600,
        "reply_threshold_default": 0.50,
        "threshold_swing": 0.0,
        "density_high_threshold": 10,
        "density_low_threshold": 3,
        "max_density_skip": 15,
        "classifier_fallback_threshold": 0.45,
    })
    engine.get_all_character_aliases = lambda: {}
    engine._get_density = lambda gid: 5.0
    engine._pre_filter = lambda message: None

    async def _no_pick(*args, **kwargs):
        return de._NoPick()

    if stub_sticker:
        engine._pick_sticker = _no_pick

    async def _gen(*args, **kwargs):
        return "回答" + uuid.uuid4().hex

    engine._generate_reply_async = _gen
    if classifier is not None:
        engine._classifier = classifier
    return engine


def _run(coro, timeout=15):
    return asyncio.run(asyncio.wait_for(coro, timeout=timeout))


class ClassifierEventLoopTest(unittest.TestCase):
    def test_01_分类推理在线程池且阻塞时事件循环可跑(self):
        engine = _make_engine()
        started = threading.Event()
        release = threading.Event()

        def _classify(text, labels):
            started.set()
            self.assertTrue(release.wait(5.0), "测试未能释放分类线程")
            return ("需要回复", 0.88)

        engine._classifier = _FakeClassifier(classify=_classify)

        async def scenario():
            task = asyncio.create_task(engine._classify_needs_reply("测试消息"))
            for _ in range(5000):
                if started.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(started.is_set(), "分类线程未启动")

            # 分类线程被 release 阻塞时，事件循环必须还能跑其他任务。
            ticks = 0
            for _ in range(25):
                await asyncio.sleep(0)
                ticks += 1
            self.assertEqual(ticks, 25)
            self.assertFalse(task.done(), "分类阻塞期间任务不应完成")

            release.set()
            return await task

        try:
            label, score = _run(scenario())
        finally:
            release.set()  # 断言失败时也解开线程，避免测试进程吊死

        self.assertEqual((label, score), ("需要回复", 0.88))
        self.assertNotEqual(engine._classifier.classify_calls[0][2], MAIN_THREAD_ID,
                            "classify 不应在事件循环线程执行")

    def test_02_分类结果与异常fail_closed语义不变(self):
        engine = _make_engine()
        engine._classifier = _FakeClassifier(
            lambda text, labels: ("需要回复", 0.73))
        label, score = _run(engine._classify_needs_reply("你好"))
        self.assertEqual((label, score), ("需要回复", 0.73))

        def _boom(text, labels):
            raise ValueError("分类器炸了：%s" % text)

        engine2 = _make_engine()
        engine2._classifier = _FakeClassifier(_boom)

        async def scenario():
            result = await engine2._classify_needs_reply("敏感正文ABC")
            return result, de._classifier_error_ctx.get()

        (label2, score2), err = _run(scenario())
        self.assertEqual((label2, score2), ("不需要回复", 0.0))
        self.assertEqual(engine2.classifier_error_count, 1)
        self.assertEqual(err, {"type": "ValueError", "phase": "inference"})

    def test_03_懒加载构造只一次且在线程池(self):
        class _CountingModel:
            instances = 0
            init_threads = []

            def __init__(self):
                type(self).instances += 1
                type(self).init_threads.append(threading.get_ident())
                self.model_name = "counting-fake"

            def classify(self, text, labels):
                return ("需要回复", 0.5)

            def encode_batch(self, texts):
                return np.zeros((len(texts), 2))

        fake_mod = types.ModuleType("scripts.zero_shot_classifier")
        fake_mod.ZeroShotClassifier = _CountingModel
        engine = _make_engine()

        async def scenario():
            return await asyncio.gather(
                *[engine._get_classifier() for _ in range(16)])

        with mock.patch.dict(sys.modules, {"scripts.zero_shot_classifier": fake_mod}):
            results = _run(scenario())

        self.assertEqual(_CountingModel.instances, 1, "分类器不得重复初始化")
        self.assertTrue(all(item is results[0] for item in results))
        self.assertEqual(len(_CountingModel.init_threads), 1)
        self.assertNotEqual(_CountingModel.init_threads[0], MAIN_THREAD_ID,
                            "分类器导入/构造不应在事件循环线程执行")

    def test_04_懒加载构造失败仍fail_closed(self):
        class _BoomModel:
            def __init__(self):
                raise RuntimeError("分类器模型加载失败")

        fake_mod = types.ModuleType("scripts.zero_shot_classifier")
        fake_mod.ZeroShotClassifier = _BoomModel
        engine = _make_engine()

        with mock.patch.dict(sys.modules, {"scripts.zero_shot_classifier": fake_mod}):
            label, score = _run(engine._classify_needs_reply("另一条消息"))

        self.assertEqual((label, score), ("不需要回复", 0.0))
        self.assertEqual(engine.classifier_error_count, 1)

    def test_05_多群并发时共享模型不并发推理(self):
        engine = _make_engine()
        state = {"active": 0, "max_active": 0, "calls": 0}
        state_lock = threading.Lock()
        first_started = threading.Event()
        release_first = threading.Event()

        def _classify(text, labels):
            with state_lock:
                state["calls"] += 1
                index = state["calls"]
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            try:
                if index == 1:
                    first_started.set()
                    self.assertTrue(release_first.wait(5.0),
                                    "测试未能释放第一次分类")
                return ("需要回复", 0.99)
            finally:
                with state_lock:
                    state["active"] -= 1

        engine._classifier = _FakeClassifier(classify=_classify)

        # 用可观测锁替换引擎锁：第二次进入在 acquire 前置位事件，确定性地
        # 证明第二个群正在等锁，而不是靠毫秒窗口猜测。
        tracker = _TrackingRLock()
        original_lock = engine._classifier_run_lock
        engine._classifier_run_lock = tracker

        async def scenario():
            first = asyncio.create_task(engine.should_reply(
                message="A群消息", user_id="u1", group_id="gA",
                is_at_bot=False, sender_nickname="甲", sticker_on=False))
            for _ in range(5000):
                if first_started.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(first_started.is_set(), "第一次分类未启动")

            second = asyncio.create_task(engine.should_reply(
                message="B群消息", user_id="u2", group_id="gB",
                is_at_bot=False, sender_nickname="乙", sticker_on=False))
            for _ in range(5000):
                if tracker.second_waiting.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(tracker.second_waiting.is_set(),
                            "第二个群未在引擎共享锁上等待")
            self.assertEqual(state["calls"], 1, "共享模型被并发进入")
            self.assertEqual(state["max_active"], 1)

            release_first.set()
            return await asyncio.gather(first, second)

        try:
            results = _run(scenario())
        finally:
            release_first.set()
            engine._classifier_run_lock = original_lock

        self.assertEqual(state["calls"], 2)
        self.assertEqual(state["max_active"], 1, "同一模型实例出现并发推理")
        self.assertEqual([ok for ok, _ in results], [True, True])
        for _, _, thread_id in engine._classifier.classify_calls:
            self.assertNotEqual(thread_id, MAIN_THREAD_ID)

    def test_06_话题encode_batch在线程池且失败返回0(self):
        engine = _make_engine()
        gid = "g_topic"
        for index in range(20):
            engine.add_message(gid, "u", "群友", "消息%d" % index)
        engine.config.update({
            "topic_recent_window": 3,
            "topic_scan_window": 2,
            "topic_similarity_threshold": 0.35,
        })

        threads = []

        def _encode(texts):
            threads.append(threading.get_ident())
            return np.ones((len(texts), 4))

        engine._classifier = _FakeClassifier(encode=_encode)
        self.assertEqual(_run(engine._detect_topic_switch(gid)), 0)
        self.assertTrue(threads)
        for thread_id in threads:
            self.assertNotEqual(thread_id, MAIN_THREAD_ID,
                                "encode_batch 不应在事件循环线程执行")

        # 与改动前一致：encode 异常时话题检测返回 0（使用全部上下文），不抛。
        def _boom(texts):
            raise RuntimeError("编码失败")

        engine2 = _make_engine()
        for index in range(20):
            engine2.add_message(gid, "u", "群友", "消息%d" % index)
        engine2.config.update({
            "topic_recent_window": 3,
            "topic_scan_window": 2,
        })
        engine2._classifier = _FakeClassifier(encode=_boom)
        self.assertEqual(_run(engine2._detect_topic_switch(gid)), 0)

    def test_10_类级共享模型跨实例串行(self):
        """模拟决策线程与表情包模块各自持有分类器实例，但共享同一个模型。"""
        fake_st = types.ModuleType("sentence_transformers")

        class _NoRealModel:
            def __init__(self, *args, **kwargs):
                raise AssertionError("测试不得加载真实模型")

        fake_st.SentenceTransformer = _NoRealModel
        with mock.patch.dict(sys.modules, {"sentence_transformers": fake_st}):
            sys.modules.pop("scripts.zero_shot_classifier", None)
            import scripts.zero_shot_classifier as zsc

        state = {"active": 0, "max_active": 0, "calls": 0}
        state_lock = threading.Lock()
        first_started = threading.Event()
        release_first = threading.Event()

        class _FakeModel:
            def encode(self, texts, convert_to_numpy=True):
                with state_lock:
                    state["calls"] += 1
                    index = state["calls"]
                    state["active"] += 1
                    state["max_active"] = max(state["max_active"], state["active"])
                try:
                    if index == 1:
                        first_started.set()
                        if not release_first.wait(5.0):
                            raise AssertionError("测试未能释放第一次编码")
                    return np.ones((len(texts), 2))
                finally:
                    with state_lock:
                        state["active"] -= 1

        cls = zsc.ZeroShotClassifier
        original_lock = cls._shared_model_lock
        tracker = _TrackingRLock()
        cls._shared_model_lock = tracker
        model = _FakeModel()
        first = cls()
        second = cls()
        for clf in (first, second):
            clf._model = model
            clf._load_model = lambda: None  # 避免内层 RLock 干扰进入计数

        errors = []

        def _worker(clf):
            try:
                clf.encode_batch(["文本"])
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=_worker, args=(first,))
        t2 = threading.Thread(target=_worker, args=(second,))
        try:
            t1.start()
            self.assertTrue(first_started.wait(5.0), "第一次编码未启动")
            t2.start()
            self.assertTrue(tracker.second_waiting.wait(5.0),
                            "第二个实例未在类级锁上等待（共享模型串行失效）")
            self.assertEqual(state["calls"], 1)
            self.assertEqual(state["max_active"], 1, "共享模型出现并发推理")
            release_first.set()
            t1.join(5.0)
            t2.join(5.0)
        finally:
            release_first.set()
            cls._shared_model_lock = original_lock
            sys.modules.pop("scripts.zero_shot_classifier", None)

        self.assertFalse(t1.is_alive() or t2.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(state["calls"], 2)
        self.assertEqual(state["max_active"], 1, "同一模型实例出现并发推理")

    def test_11_表情路径整体offload且等待锁时不阻塞事件循环(self):
        tracker = _TrackingRLock()
        load_threads = []
        encode_threads = []
        load_count = {"value": 0}

        class _LazyClassifier:
            """模拟共享模型：首次 encode_batch 才加载，加载/编码都在锁内。"""

            _shared_model_lock = tracker

            def __init__(self):
                self._model = None
                self.model_name = "lazy-fake"
                self._load_failed = False

            def _load_model(self):
                if self._model is None:
                    load_count["value"] += 1
                    load_threads.append(threading.get_ident())
                    self._model = object()

            def classify(self, text, labels):
                return ("需要回复", 0.9)

            def encode_batch(self, texts):
                with self._shared_model_lock:
                    self._load_model()
                    encode_threads.append(threading.get_ident())
                    return np.ones((len(texts), 2))

        classifier = _LazyClassifier()
        fake_mod = types.ModuleType("scripts.zero_shot_classifier")
        fake_mod.ZeroShotClassifier = _LazyClassifier
        fake_mod.get_classifier = lambda: classifier

        class _FakeStickerEngine:
            def pick(self, gid, message, sticker_on):
                # 与真实 sticker.py 的编码器路径一致：get_classifier().encode_batch()
                fake_mod.get_classifier().encode_batch([message])
                return de._NoPick()

        fake_engine = _FakeStickerEngine()
        build_threads = []

        def _build_engine():
            build_threads.append(threading.get_ident())
            return fake_engine

        engine = _make_engine(stub_sticker=False)
        old_engine, old_tried = de._STICKER_ENGINE, de._STICKER_TRIED
        de._STICKER_ENGINE = None
        de._STICKER_TRIED = False

        lock_held = threading.Event()
        release_lock = threading.Event()

        def _hold_shared_lock():
            with tracker:
                lock_held.set()
                if not release_lock.wait(5.0):
                    raise AssertionError("测试未能释放共享模型锁")

        holder = threading.Thread(target=_hold_shared_lock, daemon=True)
        holder.start()
        self.assertTrue(lock_held.wait(5.0))
        self.assertEqual(load_count["value"], 0)
        self.assertIsNone(classifier._model, "测试前提：模型尚未加载")

        async def scenario():
            task = asyncio.create_task(engine.should_reply(
                message="看这个表情", user_id="u1", group_id="g_sticker",
                is_at_bot=True, sender_nickname="甲", sticker_on=True))
            for _ in range(5000):
                if tracker.second_waiting.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(tracker.second_waiting.is_set(),
                            "表情路径未等待共享模型锁")

            # 第一路仍持锁、表情 worker 在等锁时，事件循环必须能继续跑任务。
            ticks = 0
            for _ in range(20):
                await asyncio.sleep(0)
                ticks += 1
            self.assertEqual(ticks, 20)
            self.assertFalse(task.done(), "等待模型锁期间任务不应完成")

            release_lock.set()
            return await task

        try:
            with mock.patch.dict(
                    sys.modules, {"scripts.zero_shot_classifier": fake_mod}), \
                 mock.patch.object(sticker_mod, "build_default_engine",
                                   _build_engine):
                should, reply = _run(scenario())
        finally:
            release_lock.set()
            holder.join(5.0)
            de._STICKER_ENGINE = old_engine
            de._STICKER_TRIED = old_tried

        self.assertTrue(should)
        self.assertTrue(reply.text)
        self.assertEqual(load_count["value"], 1, "模型应由表情路径首次加载一次")
        self.assertTrue(load_threads)
        self.assertNotEqual(load_threads[0], MAIN_THREAD_ID,
                            "模型加载不应在事件循环线程执行")
        self.assertTrue(encode_threads)
        self.assertNotEqual(encode_threads[0], MAIN_THREAD_ID,
                            "encode_batch 不应在事件循环线程执行")
        self.assertTrue(build_threads)
        self.assertNotEqual(build_threads[0], MAIN_THREAD_ID,
                            "_get_sticker_engine 不应在事件循环线程执行")

    def test_12_表情快速返回与异常降级(self):
        engine = _make_engine(stub_sticker=False)
        called = threading.Event()

        def _sync(*args, **kwargs):
            called.set()
            return de._NoPick()

        engine._pick_sticker_sync = _sync
        pick = _run(engine._pick_sticker("g", "消息", False))
        self.assertEqual(pick.level, de._LEVEL_NONE)
        self.assertFalse(called.is_set(), "sticker_on=False 不应进入 worker 线程")

        def _boom(*args, **kwargs):
            raise RuntimeError("表情检索异常")

        engine._pick_sticker_sync = _boom
        pick = _run(engine._pick_sticker("g", "消息", True))
        self.assertIsInstance(pick, de._NoPick)
        self.assertEqual(pick.level, de._LEVEL_NONE)

        # 真实 _get_sticker_engine 构造失败也必须静默降级为 _NoPick。
        engine2 = _make_engine(stub_sticker=False)
        old_engine, old_tried = de._STICKER_ENGINE, de._STICKER_TRIED
        de._STICKER_ENGINE = None
        de._STICKER_TRIED = False
        try:
            with mock.patch.object(sticker_mod, "build_default_engine",
                                   side_effect=RuntimeError("引擎构造异常")):
                pick = _run(engine2._pick_sticker("g", "消息", True))
        finally:
            de._STICKER_ENGINE = old_engine
            de._STICKER_TRIED = old_tried
        self.assertIsInstance(pick, de._NoPick)
        self.assertEqual(pick.level, de._LEVEL_NONE)


class GroupImageOffloadTest(unittest.TestCase):
    """验证 _describe_triggered_image 的下载/转码在线程池，失败静默降级。"""

    CANDIDATE = {
        "file": "unit-test.jpg",
        "url": "https://example.invalid/unit-test.jpg",
        "sub_type": 0,
        "file_size": 10,
    }

    def _describe_coro(self):
        return rb._describe_triggered_image(
            "g_img", [dict(self.CANDIDATE)],
            "[CQ:image,file=unit-test.jpg]", "u1", "")

    def _patches(self, fetch, encode, describe=None):
        async def _default_describe(data_url, hint=""):
            return True, "一只猫"

        return [
            mock.patch.object(_state, "vision_rate_ok",
                              lambda *a, **k: (True, 0)),
            mock.patch.object(image_cache, "select_candidates",
                              lambda *a, **k: ([dict(self.CANDIDATE)], "本消息")),
            mock.patch.object(image_cache, "image_hash", lambda meta: "sig"),
            mock.patch.object(image_cache, "seen_hash", lambda *a, **k: False),
            mock.patch.object(image_utils, "is_sticker", lambda meta: False),
            mock.patch.object(image_utils, "fetch_image", fetch),
            mock.patch.object(image_utils, "to_data_url", encode),
            mock.patch.object(vision_mod, "describe", describe or _default_describe),
        ]

    def _run_with(self, fetch, encode, describe=None):
        with contextlib.ExitStack() as stack:
            for patch in self._patches(fetch, encode, describe):
                stack.enter_context(patch)
            return _run(self._describe_coro())

    def test_07_图片下载与转码不在事件循环线程且不阻塞循环(self):
        fetch_started = threading.Event()
        fetch_release = threading.Event()
        encode_started = threading.Event()
        encode_release = threading.Event()
        threads = {}

        def _fetch(meta):
            threads["fetch"] = threading.get_ident()
            fetch_started.set()
            self.assertTrue(fetch_release.wait(5.0), "测试未能释放下载线程")
            return b"fake-image-bytes"

        def _encode(data):
            threads["encode"] = threading.get_ident()
            encode_started.set()
            self.assertTrue(encode_release.wait(5.0), "测试未能释放转码线程")
            return "data:image/jpeg;base64,AAAA"

        async def scenario():
            task = asyncio.create_task(self._describe_coro())
            for _ in range(5000):
                if fetch_started.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(fetch_started.is_set(), "下载线程未启动")
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertFalse(task.done(), "下载阻塞期间任务不应完成")
            fetch_release.set()

            for _ in range(5000):
                if encode_started.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(encode_started.is_set(), "转码线程未启动")
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertFalse(task.done(), "转码阻塞期间任务不应完成")
            encode_release.set()
            return await task

        try:
            with contextlib.ExitStack() as stack:
                for patch in self._patches(_fetch, _encode):
                    stack.enter_context(patch)
                result = _run(scenario())
        finally:
            fetch_release.set()
            encode_release.set()

        self.assertEqual(result, ["[图片: 一只猫]"])
        self.assertNotEqual(threads["fetch"], MAIN_THREAD_ID,
                            "fetch_image 不应在事件循环线程执行")
        self.assertNotEqual(threads["encode"], MAIN_THREAD_ID,
                            "to_data_url 不应在事件循环线程执行")

    def test_08_下载失败仍返回空列表且不转码不识图(self):
        called = {"encode": 0, "describe": 0}

        def _fetch(meta):
            return None

        def _encode(data):
            called["encode"] += 1
            return "data:image/jpeg;base64,AAAA"

        async def _describe(data_url, hint=""):
            called["describe"] += 1
            return True, "不应被调用"

        self.assertEqual(self._run_with(_fetch, _encode, _describe), [])
        self.assertEqual(called, {"encode": 0, "describe": 0})

    def test_09_转码失败仍返回空列表且不识图(self):
        called = {"describe": 0}

        def _fetch(meta):
            return b"fake-image-bytes"

        def _encode(data):
            return None

        async def _describe(data_url, hint=""):
            called["describe"] += 1
            return True, "不应被调用"

        self.assertEqual(self._run_with(_fetch, _encode, _describe), [])
        self.assertEqual(called["describe"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
