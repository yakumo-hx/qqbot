# -*- coding: utf-8 -*-
"""主动发言限流 + 阈值曲线的离线单测（假时钟，不碰模型/网络）。

对应方案：2026-10-20 降低搭话频率
  - 每群每小时最多 5 次「主动」发言（滚动窗口）
  - @bot / 别名走硬规则，不受限流影响
  - 阈值曲线：density>=10 -> default-swing，density<=3 -> default+swing

跑法：python -m unittest scripts.test_proactive_limit -v
"""

import asyncio
import types
import unittest

import scripts.decision_engine as de


class FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ProactiveLimitTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self._orig_time = de.time
        de.time = types.SimpleNamespace(time=self.clock.time)   # 只替换模块内的 time

        self.engine = de.DecisionEngine()
        # 配置覆盖：确保测试不受真实 decision_rules.json 影响
        self.engine.config["proactive_max_per_hour"] = 5
        self.engine.config["proactive_window_seconds"] = 3600
        self.engine.config["reply_cooldown_seconds"] = 0
        self.engine.config["reply_threshold_default"] = 0.68
        self.engine.config["threshold_swing"] = 0.08
        # 全部外部依赖打桩
        async def _cls(text):
            return ("需要回复", 0.99)
        async def _gen(*args, **kw):
            # 必须每次返回「差异足够大」的内容：否则会被引擎的
            #   · 精确去重（完全相同）
            #   · 相似度去重（difflib > 0.75）
            # 拦下——那与本测试目标（限流）无关。用随机串保证低相似度。
            import uuid
            return "回答" + uuid.uuid4().hex
        self.engine._classify_needs_reply = _cls
        self.engine._generate_reply_async = _gen
        self.engine._pick_sticker = lambda *a, **k: de._NoPick()
        self.engine._get_density = lambda gid: 5.0     # 中间密度，避开密度跳过
        self.engine._pre_filter = lambda msg: None

    def tearDown(self):
        de.time = self._orig_time

    def _ask(self, gid, msg="这是一句足够长的测试消息", at=False):
        return asyncio.run(self.engine.should_reply(
            message=msg, user_id="u1", group_id=gid,
            is_at_bot=at, sender_nickname="测试", sticker_on=False))

    # ---------- 限流 ----------
    def test_五次要放行_第六次被拦(self):
        for i in range(5):
            ok, _ = self._ask("g1", f"测试消息第{i}条")
            self.assertTrue(ok, f"第 {i+1} 次应当放行")
        ok, _ = self._ask("g1", "测试消息第六条")
        self.assertFalse(ok, "第 6 次应当被限流拦下")

    def test_窗口滑过后恢复(self):
        for i in range(5):
            self._ask("g1", f"刷满第{i}条")
        self.assertFalse(self._ask("g1", "仍应被拦")[0])
        self.clock.advance(3601)                 # 整个窗口滑过
        self.assertTrue(self._ask("g1", "窗口过后应放行")[0])

    def test_两群计数互不影响(self):
        for i in range(5):
            self._ask("gA", f"A群第{i}条")
        self.assertFalse(self._ask("gA", "A群第6条")[0], "A 群应被限流")
        self.assertTrue(self._ask("gB", "B群第一条")[0], "B 群不应受影响")

    def test_艾特机器人不受限流影响(self):
        for i in range(5):
            self._ask("g1", f"占满名额第{i}条")
        self.assertFalse(self._ask("g1")[0], "主动路径应被拦")
        ok, res = self._ask("g1", "@bot 在吗", at=True)
        self.assertTrue(ok, "@bot 必须绕过限流")
        self.assertTrue(getattr(res, "text", ""), "@bot 回复内容不应为空")

    def test_cap为0视为不限制(self):
        self.engine.config["proactive_max_per_hour"] = 0
        for i in range(12):
            self.assertTrue(self._ask("g1", f"第{i}条不限流")[0], "cap=0 应当完全不限制")

    # ---------- 阈值曲线 ----------
    def test_阈值曲线(self):
        f = self.engine._get_dynamic_threshold
        self.assertAlmostEqual(f(10.0), 0.60, places=6)   # 热闹：0.68-0.08
        self.assertAlmostEqual(f(20.0), 0.60, places=6)
        self.assertAlmostEqual(f(3.0), 0.76, places=6)    # 冷清：0.68+0.08
        self.assertAlmostEqual(f(1.0), 0.76, places=6)
        mid = f(6.5)                                      # 中间线性
        self.assertTrue(0.60 < mid < 0.76, f"中间值应介于两者之间，实得 {mid}")

    def test_低分不再靠热闹放行(self):
        """分数 0.55：改造前在热闹群会被放行（阈值0.45），现在应当被拦（阈值0.60）。"""
        async def _low(text):
            return ("需要回复", 0.55)
        self.engine._classify_needs_reply = _low
        self.engine._get_density = lambda gid: 12.0        # 热闹
        ok, _ = self._ask("g1", "一句分数只有0.55的话")
        self.assertFalse(ok, "0.55 < 0.60，热闹时也应当拒绝")


if __name__ == "__main__":
    unittest.main(verbosity=2)
