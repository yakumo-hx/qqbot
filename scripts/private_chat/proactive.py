# -*- coding: utf-8 -*-
"""主动消息引擎。

默认每天 3~5 条：低于 daily_min 时只要满足硬闸门就发；达到 daily_min 后
按概率放慢；达到 daily_max 停止。所有内容必须有依据（记忆或自我状态），
不允许「在吗」式空消息。

阶段 2 起主动消息不再自己发送/落盘：它只负责闸门、hook 选择、生成和校验，
真正发送、版本检查、回执与计数由 ConversationRuntime / TurnSender 完成。
"""

from __future__ import annotations

import random
import time
from datetime import date, datetime, timedelta
from typing import Optional

from . import config as cfgmod
from . import corrections as corrections_mod
from . import persona as persona_mod
from .store import text_overlap

_HOOK_REPEAT_TTL = 72 * 3600
_UNKNOWN_COOLDOWN = 2 * 3600
_ERROR_COOLDOWN = 300


def _parse_epoch(value) -> float:
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _in_quiet_hours(hour: int, quiet) -> bool:
    try:
        start, end = int(quiet[0]), int(quiet[1])
    except (TypeError, ValueError, IndexError):
        return False
    if start == end:
        return False
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


class ProactiveEngine:
    def __init__(self, chat, logger):
        self.chat = chat
        self.logger = logger
        self.cfg = chat.cfg
        self.store = chat.store
        self._last_dry_log: dict[str, float] = {}

    async def tick(self) -> None:
        cfg = self.cfg.get("proactive") or {}
        if not cfg.get("enabled", True):
            return
        ws = self.chat._ws
        if ws is None:
            return
        for uid in self.chat._known_uids():
            try:
                await self._tick_uid(uid, time.time())
            except Exception as exc:  # noqa: BLE001
                self.logger.info(f"[私聊] 主动消息 uid={uid} 异常: {type(exc).__name__}: {exc}")

    def _cooldown_active(self, p: dict, now: float) -> bool:
        last_attempt = float(p.get("last_attempt_at", 0) or 0)
        status = str(p.get("last_attempt_status") or "")
        if last_attempt:
            if status == "unknown" and now - last_attempt < _UNKNOWN_COOLDOWN:
                return True
            if status == "failed" and now - last_attempt < _ERROR_COOLDOWN:
                return True
        return False

    async def _tick_uid(self, uid: str, now: float) -> None:
        cfg = self.cfg.get("proactive") or {}
        prof = self.store.profile(uid)
        p = self.store.proactive_state(uid)

        # 暂停
        paused_until = _parse_epoch((prof.get("proactive") or {}).get("paused_until"))
        if paused_until and now < paused_until:
            return

        # 启动缓冲
        created = float(getattr(self.chat, "_created_at", now))
        if now - created < float(cfg.get("startup_grace_seconds", 600) or 600):
            return

        # 静默时段
        if _in_quiet_hours(datetime.now().hour, prof.get("quiet_hours") or cfg.get("quiet_hours")):
            return

        # 当日/本周计数
        today = cfgmod.today_str()
        if p.get("date") != today:
            p["date"] = today
            p["count"] = 0
            p["unanswered"] = 0
            self.store.save_state(uid)
        week_monday = (date.today() - timedelta(days=date.today().weekday())).isoformat()
        if p.get("week_start") != week_monday:
            p["week_start"] = week_monday
            p["week_count"] = 0
            self.store.save_state(uid)

        count = int(p.get("count", 0) or 0)
        week_count = int(p.get("week_count", 0) or 0)
        daily_max = int(cfg.get("daily_max", 5) or 5)
        weekly_max = int(cfg.get("weekly_max", 30) or 30)
        if count >= daily_max or week_count >= weekly_max:
            return

        # 最小间隔
        last_at = float(p.get("_last_epoch", 0) or 0)
        if not last_at:
            last_at = _parse_epoch(p.get("last_at"))
        if not last_at:
            # 兼容重构前只写 state.last_proactive_at 的持久化数据。
            last_at = float(self.store.state(uid).get("last_proactive_at", 0) or 0)
        min_interval = float(cfg.get("min_interval_seconds", 7200) or 7200)
        if last_at and now - last_at < min_interval:
            return

        # unknown / 明确失败的冷却
        if self._cooldown_active(p, now):
            return

        # 未回复上限
        if int(p.get("unanswered", 0) or 0) >= int(cfg.get("max_unanswered", 3) or 3):
            return

        # 用户最近是否还在说话
        last_user = _parse_epoch(prof.get("last_user_at"))
        silence_hours = float(cfg.get("silence_hours", 2) or 2)
        if last_user and now - last_user < silence_hours * 3600:
            return

        # 用户忙时只降低概率，不硬禁止
        try:
            factor = self.chat.activity.proactive_factor(uid, datetime.now().astimezone())
        except Exception:
            factor = 1.0
        if factor < 1.0 and random.random() > factor:
            if self.logger:
                self.logger.info(f"[活动] 当前较忙，主动消息概率 x{factor:.2f}，本次跳过")
            return

        # 低于 daily_min 时更积极；达到后按概率放慢
        daily_min = int(cfg.get("daily_min", 3) or 3)
        if count >= daily_min and random.random() > 0.35:
            return

        hook = self._pick_hook(uid)
        if not hook:
            return
        hook_text, hook_id = hook

        if cfg.get("dry_run"):
            last_dry = self._last_dry_log.get(uid, 0.0)
            if now - last_dry < 3600:
                return
            self._last_dry_log[uid] = now
            self.logger.info(f"[私聊][dry-run] 本应主动发消息 uid={uid}：依据={hook_text[:80]}")
            return

        runtime = self.chat.get_runtime(uid)
        outcome = await runtime.request_proactive(
            generate=lambda: self._generate(uid, hook_text),
            validate=lambda text: self._valid(text, uid),
            on_sent=lambda text: self._on_sent(uid, text, hook_id),
            on_failed=lambda text, status: self._on_failed(uid, status),
        )
        if outcome is None:
            return
        if outcome.sent:
            self.logger.info(f"[私聊] 主动消息已发 uid={uid}：{outcome.status}")
        else:
            self.logger.info(f"[私聊] 主动消息未确认 uid={uid}：{outcome.status} {outcome.error[:80]}")

    def _on_sent(self, uid: str, text: str, hook_id: str) -> None:
        st = self.store.state(uid)
        p = self.store.proactive_state(uid)
        p["_last_epoch"] = time.time()
        p["last_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        p["last_attempt_at"] = time.time()
        p["last_attempt_status"] = "sent"
        hooks = p.get("recent_hooks") if isinstance(p.get("recent_hooks"), list) else []
        hooks.append({"id": str(hook_id), "ts": time.time()})
        p["recent_hooks"] = hooks[-20:]
        self.store.save_state(uid)
        self.store.record_proactive(uid, text)

    def _on_failed(self, uid: str, status: str) -> None:
        p = self.store.proactive_state(uid)
        p["last_attempt_at"] = time.time()
        p["last_attempt_status"] = "unknown" if status == "unknown" else "failed"
        self.store.save_state(uid)

    def _pick_hook(self, uid: str) -> Optional[tuple[str, str]]:
        items = self.store.memory_items(uid)
        p = self.store.proactive_state(uid)
        recent_texts = [str(x) for x in (p.get("recent_texts") or [])]
        now = time.time()
        recent_hooks = [
            x for x in (p.get("recent_hooks") or [])
            if isinstance(x, dict) and now - float(x.get("ts", 0) or 0) < _HOOK_REPEAT_TTL
        ]
        used_ids = {str(x.get("id")) for x in recent_hooks}
        candidates: list[tuple[int, str, str]] = []
        for item in items:
            if item.get("type") not in ("promise", "event", "relationship"):
                continue
            text = str(item.get("text") or "").strip()
            item_id = str(item.get("id") or "")
            if not text or (item_id and item_id in used_ids):
                continue
            if any(text_overlap(text, old) >= 0.55 for old in recent_texts[-10:]):
                continue
            candidates.append((int(item.get("importance", 3) or 3), text, item_id or text[:24]))
        if candidates:
            candidates.sort(reverse=True)
            top = candidates[:3]
            _imp, text, item_id = random.choice(top)
            return "你记得：" + text, item_id
        self_state = self.store.self_state(uid)
        for key in ("looking_forward_to", "today"):
            value = str(self_state.get(key) or "").strip()
            if not value:
                continue
            item_id = "self:" + key
            if item_id in used_ids:
                continue
            if any(text_overlap(value, old) >= 0.55 for old in recent_texts[-10:]):
                continue
            return value, item_id
        thoughts = self_state.get("recent_thoughts")
        if isinstance(thoughts, list):
            for thought in thoughts:
                value = str(thought or "").strip()
                item_id = "self:thought"
                if item_id in used_ids:
                    continue
                if value and not any(text_overlap(value, old) >= 0.55 for old in recent_texts[-10:]):
                    return value, item_id
        return None

    async def _generate(self, uid: str, hook: str) -> str:
        messages = persona_mod.build_proactive_prompt(
            self.cfg, self.store, uid, hook,
            activity=self.chat.activity.format_prompt(uid),
        )
        text = await self.chat._llm_fn(messages, self.cfg)
        text = corrections_mod.apply(str(text or ""), persona_mod.load_corrections(self.cfg))
        return str(text or "").strip()

    def _valid(self, reply: str, uid: str) -> bool:
        value = reply.strip()
        if not value:
            return False
        max_chars = int((self.cfg.get("proactive") or {}).get("content_max_chars", 80) or 80)
        if len(value) < 4 or len(value) > max_chars:
            return False
        if any(x in value for x in ("在吗", "忙不忙", "作为AI", "作为 AI", "有什么可以帮你", "希望对你有帮助")):
            return False
        recent = self.store.proactive_state(uid).get("recent_texts") or []
        if value in recent:
            return False
        if any(text_overlap(value, str(old)) >= 0.75 for old in recent[-10:]):
            return False
        if value.count("？") + value.count("?") > 2:
            return False
        return True
