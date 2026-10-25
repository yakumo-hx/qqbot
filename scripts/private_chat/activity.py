# -*- coding: utf-8 -*-
"""用户活动/日程存储（single-user companion）。

解决的问题：bot 需要知道「对方现在在做什么、接下来要做什么」，
而不是把每句话都当成长期记忆。

数据：data/private/<uid>/activities.json
- current  当前活动（精确时间或时段）
- schedule 一次性/每周重复日程
- history  过期/结束的活动

时间精度：
- exact   有 start_at/end_at
- period  morning/afternoon/evening/night，具体时间未知
- unknown 只知道「今天有这件事」

写入以 LLM 从聊天记录抽取为主（engine._extract_activity），
本模块只做数据校验、解析、渲染，不调用模型。
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime, time as dtime, timedelta, timezone, tzinfo
from typing import Any, Optional
from zoneinfo import ZoneInfo

from . import config as cfgmod

CATEGORIES = {
    "class", "study", "work", "meal", "rest", "sleep", "commute",
    "exercise", "social", "entertainment", "exam", "meeting", "other",
}
PERIODS = ("morning", "afternoon", "evening", "night")
PERIOD_LABEL = {"morning": "上午", "afternoon": "下午", "evening": "晚上", "night": "深夜"}
_PERIOD_ALIASES = {
    "上午": "morning", "早上": "morning", "早晨": "morning",
    "下午": "afternoon", "中午": "afternoon",
    "晚上": "evening", "傍晚": "evening",
    "深夜": "night", "半夜": "night", "凌晨": "night",
    "morning": "morning", "afternoon": "afternoon",
    "evening": "evening", "night": "night",
}


def _norm_period(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in PERIODS:
        return text
    return _PERIOD_ALIASES.get(str(value or "").strip(), "")
PRECISIONS = {"exact", "period", "unknown"}
_ACTIVITY_CUE = re.compile(
    r"现在|正在|在上课|在吃饭|在写|在复习|在考试|在开会|在下班|在睡|在忙|"
    r"今天|上午|下午|晚上|今晚|明天|后天|周[一二三四五六日天]|每[天周]|"
    r"上课|下课|考试|作业|复习|排练|开会|下班|食堂|宿舍|回家|到[点十百]|"
    r"结束|完了|有空|没空|安排|日程"
)


def _tz(cfg: dict) -> tzinfo:
    name = str((cfg.get("activity") or {}).get("timezone") or "Asia/Shanghai")
    try:
        return ZoneInfo(name)
    except Exception:
        # Windows Python 可能没装 tzdata；固定 +08:00 兜底，避免整个活动模块不可用。
        return timezone(timedelta(hours=8), name="Asia/Shanghai")


def _now(cfg: dict, now: Optional[datetime] = None) -> datetime:
    tz = _tz(cfg)
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def _parse_dt(value: Any, cfg: dict) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz(cfg))
    return dt.astimezone(_tz(cfg))


def _parse_date(value: Any, cfg: dict) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


def _hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _period_window(cfg: dict, period: str) -> tuple[int, int]:
    defaults = {
        "morning": [6, 12], "afternoon": [12, 18],
        "evening": [18, 23], "night": [23, 6],
    }
    raw = ((cfg.get("activity") or {}).get("periods") or {}).get(period)
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        try:
            return int(raw[0]), int(raw[1])
        except (TypeError, ValueError):
            pass
    return defaults.get(period, (0, 24))


def _period_at(cfg: dict, period: str, day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    start_h, end_h = _period_window(cfg, period)
    start = datetime.combine(day, dtime(start_h % 24), tzinfo=tz)
    end_day = day if end_h > start_h else day + timedelta(days=1)
    end = datetime.combine(end_day, dtime(end_h % 24), tzinfo=tz)
    return start, end


def _default_duration(cfg: dict) -> timedelta:
    minutes = int((cfg.get("activity") or {}).get("default_duration_minutes", 120) or 120)
    return timedelta(minutes=max(10, minutes))


def _busy_categories(cfg: dict) -> set[str]:
    raw = (cfg.get("activity") or {}).get("busy_categories") or []
    return {str(x) for x in raw} or {"class", "exam", "meeting", "sleep", "work"}


def _norm_category(value: Any) -> str:
    value = str(value or "other").strip().lower()
    return value if value in CATEGORIES else "other"


def _norm_interruptibility(cfg: dict, category: str, value: Any = None) -> str:
    value = str(value or "").strip().lower()
    if value in ("busy", "normal", "free"):
        return value
    return "busy" if category in _busy_categories(cfg) else "normal"


def _text(value: Any, limit: int = 40) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


class ActivityStore:
    def __init__(self, store, cfg: dict, logger=None):
        self.store = store
        self.cfg = cfg
        self.logger = logger
        self._locks: dict[str, threading.RLock] = {}

    def path(self, uid: str) -> str:
        return f"{self.store.base_dir(uid)}/activities.json"

    def _lock(self, uid: str) -> threading.RLock:
        key = str(uid)
        if key not in self._locks:
            self._locks[key] = threading.RLock()
        return self._locks[key]

    def _empty(self) -> dict:
        return {"version": 1, "timezone": str((self.cfg.get("activity") or {}).get("timezone") or "Asia/Shanghai"),
                "current": None, "schedule": [], "history": []}

    def load(self, uid: str) -> dict:
        data = cfgmod.read_json(self.path(uid), self._empty())
        if not isinstance(data, dict):
            data = self._empty()
        for key, default in self._empty().items():
            data.setdefault(key, default)
        if not isinstance(data.get("schedule"), list):
            data["schedule"] = []
        if not isinstance(data.get("history"), list):
            data["history"] = []
        return data

    def save(self, uid: str, data: dict) -> bool:
        return cfgmod.atomic_write_json(self.path(uid), data)

    # ---------- 时间解析 ----------
    def _active_current(self, item: Any, now: datetime) -> bool:
        if not isinstance(item, dict):
            return False
        start = _parse_dt(item.get("start_at"), self.cfg) or _parse_dt(item.get("created_at"), self.cfg)
        end = _parse_dt(item.get("end_at"), self.cfg)
        if start and end:
            return start <= now < end
        if start:
            return start <= now < start + _default_duration(self.cfg)
        return False

    def _occurrence(self, item: dict, day: date, now: datetime) -> Optional[dict]:
        if not isinstance(item, dict) or item.get("status") in ("cancelled", "done"):
            return None
        tz = _tz(self.cfg)
        item_date = _parse_date(item.get("date"), self.cfg)
        rec = item.get("recurrence") if isinstance(item.get("recurrence"), dict) else None
        if rec:
            if item_date and day < item_date:
                return None
            until = _parse_date(rec.get("until"), self.cfg) if rec.get("until") else None
            if until and day > until:
                return None
            weekdays = rec.get("weekdays")
            if rec.get("type") == "weekly" and isinstance(weekdays, list):
                if (day.weekday() + 1) not in {int(x) for x in weekdays if str(x).isdigit()}:
                    return None
        elif item_date and item_date != day:
            return None

        start_at = _parse_dt(item.get("start_at"), self.cfg)
        end_at = _parse_dt(item.get("end_at"), self.cfg)
        period = str(item.get("period") or "")
        if start_at:
            start = datetime.combine(day, start_at.timetz().replace(tzinfo=None), tzinfo=tz)
            if end_at:
                end = datetime.combine(day, end_at.timetz().replace(tzinfo=None), tzinfo=tz)
                if end <= start:
                    end += timedelta(days=1)
            else:
                end = start + _default_duration(self.cfg)
        elif period in PERIODS:
            start, end = _period_at(self.cfg, period, day, tz)
        else:
            return None
        if end <= now and day < now.date():
            return None
        out = dict(item)
        out["_start"] = start
        out["_end"] = end
        out["_day"] = day.isoformat()
        return out

    def current(self, uid: str, now: Optional[datetime] = None) -> Optional[dict]:
        now = _now(self.cfg, now)
        with self._lock(uid):
            data = self.load(uid)
            cur = data.get("current")
            if self._active_current(cur, now):
                out = dict(cur)
                out["_start"] = _parse_dt(cur.get("start_at"), self.cfg) or _parse_dt(cur.get("created_at"), self.cfg)
                out["_end"] = _parse_dt(cur.get("end_at"), self.cfg)
                return out
            if cur:
                data["current"] = None
                data["history"].append({**cur, "status": "done", "ended_at": _iso(now)})
                self.save(uid, data)
            # 日程正在窗口内（精确时间）也算当前。
            for item in data.get("schedule") or []:
                occ = self._occurrence(item, now.date(), now)
                if occ and occ["_start"] <= now < occ["_end"]:
                    return occ
        return None

    def upcoming(self, uid: str, now: Optional[datetime] = None, limit: int = 3) -> list[dict]:
        now = _now(self.cfg, now)
        out: list[dict] = []
        with self._lock(uid):
            data = self.load(uid)
            for offset in (0, 1, 2):
                day = now.date() + timedelta(days=offset)
                for item in data.get("schedule") or []:
                    occ = self._occurrence(item, day, now)
                    if occ and occ["_end"] > now:
                        out.append(occ)
        out.sort(key=lambda x: x.get("_start") or now)
        return out[:max(1, int(limit))]

    def format_prompt(self, uid: str, now: Optional[datetime] = None,
                      max_chars: Optional[int] = None) -> str:
        now = _now(self.cfg, now)
        max_chars = int(max_chars or (self.cfg.get("activity") or {}).get("prompt_max_chars", 220) or 220)
        cur = self.current(uid, now)
        parts: list[str] = []
        if cur:
            start, end = cur.get("_start"), cur.get("_end")
            span = ""
            if start and end:
                span = f"{_hhmm(start)}–{_hhmm(end)}"
                mins = int((end - now).total_seconds() // 60)
                if 0 < mins <= 180:
                    span += f"，还剩 {mins} 分钟"
            elif start:
                span = f"从 {_hhmm(start)} 开始"
            elif cur.get("period") in PERIOD_LABEL:
                span = PERIOD_LABEL[cur["period"]]
            level = cur.get("interruptibility") or "normal"
            level_text = {"busy": "忙碌", "normal": "一般", "free": "空闲"}.get(level, level)
            parts.append(f"【对方现在】{cur.get('content', '')}（{span}；{level_text}）")
        upcoming = self.upcoming(uid, now, limit=3)
        if upcoming:
            items = []
            for item in upcoming:
                if cur and item.get("id") == cur.get("id"):
                    continue
                start = item.get("_start")
                if item.get("precision") == "period" and item.get("period") in PERIOD_LABEL:
                    span = PERIOD_LABEL[item["period"]]
                else:
                    span = _hhmm(start) if start else PERIOD_LABEL.get(str(item.get("period")), "")
                day_text = "今天" if (item.get("_day") == now.date().isoformat()) else "明天"
                items.append(f"{day_text}{span} {item.get('content', '')}".strip())
                if len(items) >= 2:
                    break
            if items:
                parts.append("【接下来】" + "；".join(items))
        text = "\n".join(parts)
        return text[:max_chars].rstrip()

    def proactive_factor(self, uid: str, now: Optional[datetime] = None) -> float:
        """用户忙时降低（但不禁止）主动消息频率。"""
        cfg = self.cfg.get("activity") or {}
        if not cfg.get("enabled", True):
            return 1.0
        now = _now(self.cfg, now)
        cur = self.current(uid, now)
        if cur and cur.get("interruptibility") == "busy":
            return float(cfg.get("proactive_busy_factor", 0.35) or 0.35)
        skip_min = int(cfg.get("upcoming_skip_minutes", 15) or 15)
        for item in self.upcoming(uid, now, limit=5):
            start = item.get("_start")
            if start and item.get("interruptibility") == "busy":
                delta = (start - now).total_seconds() / 60
                if 0 <= delta <= skip_min:
                    return float(cfg.get("proactive_upcoming_factor", 0.6) or 0.6)
        return 1.0

    # ---------- 写入 ----------
    def _archive_current(self, data: dict, now: datetime, reason: str = "replaced") -> None:
        cur = data.get("current")
        if isinstance(cur, dict):
            data["history"].append({**cur, "status": "done", "ended_at": _iso(now), "reason": reason})
        data["current"] = None

    def set_current(self, uid: str, content: str, *, category: str = "other",
                    start_at: Any = None, end_at: Any = None, period: str = "",
                    precision: str = "exact", confidence: float = 0.9,
                    interruptibility: Any = None, notes: str = "",
                    source: str = "user", now: Optional[datetime] = None) -> Optional[dict]:
        content = _text(content)
        if not content:
            return None
        now = _now(self.cfg, now)
        start = _parse_dt(start_at, self.cfg) or now
        end = _parse_dt(end_at, self.cfg)
        if end is None:
            end = start + _default_duration(self.cfg)
        if end <= start:
            end = start + _default_duration(self.cfg)
        category = _norm_category(category)
        period = _norm_period(period)
        item = {
            "id": f"act_{int(now.timestamp())}_{int(time.time_ns() % 100000)}",
            "content": content,
            "category": category,
            "start_at": _iso(start),
            "end_at": _iso(end),
            "period": period,
            "precision": precision if precision in PRECISIONS else "exact",
            "source": source,
            "confidence": max(0.0, min(1.0, float(confidence or 0.9))),
            "interruptibility": _norm_interruptibility(self.cfg, category, interruptibility),
            "notes": _text(notes, 120),
            "created_at": _iso(now),
        }
        with self._lock(uid):
            data = self.load(uid)
            self._archive_current(data, now, "replaced")
            data["current"] = item
            self.save(uid, data)
        return item

    def add_schedule(self, uid: str, content: str, *, category: str = "other",
                     day: Any = None, period: str = "", start_at: Any = None,
                     end_at: Any = None, precision: str = "period",
                     recurrence: Any = None, confidence: float = 0.8,
                     interruptibility: Any = None, notes: str = "",
                     source: str = "user", now: Optional[datetime] = None) -> Optional[dict]:
        content = _text(content)
        if not content:
            return None
        now = _now(self.cfg, now)
        category = _norm_category(category)
        rec = recurrence if isinstance(recurrence, dict) else None
        date_text = ""
        if day:
            d = _parse_date(day, self.cfg)
            date_text = d.isoformat() if d else ""
        start = _parse_dt(start_at, self.cfg)
        end = _parse_dt(end_at, self.cfg)
        if start:
            date_text = start.date().isoformat()
        if not date_text and not rec:
            date_text = now.date().isoformat()
        period = _norm_period(period)
        if not start and not period and not rec:
            return None
        item = {
            "id": f"sch_{int(now.timestamp())}_{int(time.time_ns() % 100000)}",
            "content": content,
            "category": category,
            "date": date_text or None,
            "period": period,
            "start_at": _iso(start) if start else None,
            "end_at": _iso(end) if end else None,
            "precision": precision if precision in PRECISIONS else "period",
            "recurrence": rec,
            "status": "planned",
            "source": source,
            "confidence": max(0.0, min(1.0, float(confidence or 0.8))),
            "interruptibility": _norm_interruptibility(self.cfg, category, interruptibility),
            "notes": _text(notes, 120),
            "created_at": _iso(now),
        }
        with self._lock(uid):
            data = self.load(uid)
            data["schedule"].append(item)
            self.save(uid, data)
        return item

    def close_current(self, uid: str, reason: str = "closed", now: Optional[datetime] = None) -> bool:
        now = _now(self.cfg, now)
        with self._lock(uid):
            data = self.load(uid)
            if not data.get("current"):
                return False
            self._archive_current(data, now, reason)
            self.save(uid, data)
            return True

    def cancel_schedule(self, uid: str, *, item_id: str = "", content: str = "") -> int:
        removed = 0
        with self._lock(uid):
            data = self.load(uid)
            kept = []
            for item in data.get("schedule") or []:
                if item.get("status") in ("done", "cancelled"):
                    continue
                if (item_id and item.get("id") == item_id) or (content and content in str(item.get("content") or "")):
                    removed += 1
                    continue
                kept.append(item)
            data["schedule"] = kept
            self.save(uid, data)
        return removed

    def clear(self, uid: str) -> None:
        with self._lock(uid):
            data = self.load(uid)
            data["current"] = None
            data["schedule"] = []
            data["history"] = list(data.get("history") or [])
            self.save(uid, data)

    def apply_operations(self, uid: str, operations: Any, now: Optional[datetime] = None) -> int:
        """应用 LLM 抽取出的操作；返回成功操作数。"""
        if not isinstance(operations, list):
            return 0
        now = _now(self.cfg, now)
        applied = 0
        for op in operations:
            if not isinstance(op, dict):
                continue
            # 模型可能写 op / type / action；兼容三种，避免整批静默丢弃。
            kind = str(op.get("op") or op.get("type") or op.get("action") or "").strip()
            confidence = float(op.get("confidence", 0.8) or 0)
            if confidence < 0.5:
                continue
            try:
                if kind == "set_current":
                    if self.set_current(
                        uid, op.get("content", ""), category=op.get("category", "other"),
                        start_at=op.get("start_at"), end_at=op.get("end_at"),
                        period=op.get("period", ""), precision=op.get("precision", "exact"),
                        confidence=confidence, interruptibility=op.get("interruptibility"),
                        notes=op.get("notes", ""), source="llm", now=now,
                    ):
                        applied += 1
                elif kind == "add_schedule":
                    if self.add_schedule(
                        uid, op.get("content", ""), category=op.get("category", "other"),
                        day=op.get("date"), period=op.get("period", ""),
                        start_at=op.get("start_at"), end_at=op.get("end_at"),
                        precision=op.get("precision", "period"),
                        recurrence=op.get("recurrence"), confidence=confidence,
                        interruptibility=op.get("interruptibility"),
                        notes=op.get("notes", ""), source="llm", now=now,
                    ):
                        applied += 1
                elif kind == "close_current":
                    if self.close_current(uid, str(op.get("reason") or "closed"), now=now):
                        applied += 1
                elif kind == "cancel_schedule":
                    applied += self.cancel_schedule(
                        uid, item_id=str(op.get("id") or ""),
                        content=str(op.get("content") or ""),
                    )
            except Exception as exc:  # noqa: BLE001
                if self.logger:
                    self.logger.info(f"[活动] 操作失败（已忽略）: {type(exc).__name__}: {exc}")
        if applied:
            self._prune(uid, now)
        return applied

    def _prune(self, uid: str, now: datetime) -> None:
        cfg = self.cfg.get("activity") or {}
        days = int(cfg.get("retention_days", 90) or 90)
        max_history = int(cfg.get("history_max", 500) or 500)
        with self._lock(uid):
            data = self.load(uid)
            cutoff = now - timedelta(days=max(1, days))
            kept = []
            for item in data.get("history") or []:
                end = _parse_dt(item.get("ended_at"), self.cfg) or _parse_dt(item.get("created_at"), self.cfg)
                if end is None or end >= cutoff:
                    kept.append(item)
            data["history"] = kept[-max(1, max_history):]
            schedule = []
            for item in data.get("schedule") or []:
                if item.get("status") in ("cancelled", "done"):
                    continue
                day = _parse_date(item.get("date"), self.cfg)
                if day and day < now.date() - timedelta(days=2):
                    data["history"].append({**item, "status": "expired", "ended_at": _iso(now)})
                    continue
                schedule.append(item)
            data["schedule"] = schedule
            self.save(uid, data)

    # ---------- LLM 抽取辅助 ----------
    def should_extract(self, text: str) -> bool:
        return bool(_ACTIVITY_CUE.search(str(text or "")))

    def format_for_llm(self, uid: str, now: Optional[datetime] = None) -> str:
        now = _now(self.cfg, now)
        data = self.load(uid)
        payload = {
            "now": _iso(now),
            "current": data.get("current"),
            "schedule": data.get("schedule") or [],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)[:5000]
