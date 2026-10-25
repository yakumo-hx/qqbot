# -*- coding: utf-8 -*-
"""私聊陪伴版的数据层：关系档案、永久记忆、自我状态、会话流水。

存储全部是 JSON / JSONL，单用户规模下不需要数据库：
- profile.json        关系与统计
- memories.json       永久记忆（0 表示不按时间删除）
- self_state.json     它自己的状态（mood/energy/today/...）
- summaries.json      日/段摘要（LLM 整理产物）
- state.json          主动消息计数、抽取游标等运行时状态
- sessions/YYYY-MM-DD.jsonl  原始对话（可永久保留）
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from . import config as cfgmod

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[A-Za-z0-9_]{2,}")
_TYPE_PRIOR = {
    "promise": 1.0,
    "relationship": 0.9,
    "event": 0.85,
    "user_preference": 0.8,
    "user_fact": 0.75,
    "self_fact": 0.6,
    "self_feeling": 0.6,
}
_ALLOWED_TYPES = set(_TYPE_PRIOR)
_MAX_MEMORIES = 50000  # 单用户上限；远高于实际使用量，等价于「永久保留」

# add_turn / update_turn 只接受这些元数据字段，避免调用方覆盖 role/text/ts/msg_id。
_TURN_META_FIELDS = {
    "turn_id",
    "bubble_index",
    "bubble_count",
    "reply_to_msg_ids",
    "source",
    "act",
    "revision",
    "delivery_status",
    "original_text",
    "vision_status",
    "vision_error",
}
_USER_META_FIELDS = {"original_text", "vision_status", "vision_error"}
_MAX_ATTEMPTS = 1000


def _turn_ts_key(record: dict) -> tuple:
    """按原始 ts 排序；解析失败的记录排到最前，保持稳定。"""
    raw = str(record.get("ts") or "")
    try:
        return (0, datetime.fromisoformat(raw).timestamp(), raw)
    except (TypeError, ValueError):
        return (1, 0.0, raw)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _history_key(role: str, text: str, ts: str) -> tuple[str, str, str]:
    """无 msg_id 历史记录的稳定去重键：角色 + 原文 + 原始时间戳。"""
    return (str(role), str(text or "").strip(), str(ts or ""))


def _tokens(text: str) -> set[str]:
    text = (text or "").lower()
    out: set[str] = set()
    for run in _CJK.findall(text):
        if len(run) == 1:
            out.add(run)
        else:
            out.update(run[i:i + 2] for i in range(len(run) - 1))
    out.update(_WORD.findall(text))
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def text_overlap(left: str, right: str) -> float:
    """公开的轻量文本相似度（主动消息去重用）。"""
    return _jaccard(_tokens(str(left or "")), _tokens(str(right or "")))


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _days_since(value: Any) -> float:
    dt = _parse_iso(value)
    if dt is None:
        return 30.0
    now = datetime.now().astimezone()
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def _safe_uid(uid: Any) -> str:
    text = str(uid or "")
    return re.sub(r"[^0-9A-Za-z_.-]", "_", text) or "unknown"


class PrivateStore:
    """单用户私聊数据仓库（每个 uid 一个目录）。"""

    def __init__(self, cfg: dict, logger=None):
        self.cfg = cfg
        self.logger = logger
        self.data_dir = cfgmod.resolve_data_dir(cfg)
        self._turns: dict[str, deque] = {}
        self._turns_loaded: set[str] = set()
        self._mem_cache: dict[str, dict] = {}
        self._profile_cache: dict[str, dict] = {}
        self._state_cache: dict[str, dict] = {}
        self._self_cache: dict[str, dict] = {}

    # ---------- 路径 ----------
    def base_dir(self, uid: Any) -> str:
        return cfgmod.ensure_dir(f"{self.data_dir}/{_safe_uid(uid)}")

    def _path(self, uid: Any, name: str) -> str:
        return f"{self.base_dir(uid)}/{name}"

    # ---------- profile ----------
    def _default_profile(self, uid: str) -> dict:
        now = cfgmod.now_iso()
        return {
            "uid": uid,
            "display_name": "",
            "first_seen": now,
            "relationship": {"stage": "familiar", "closeness": 0.35, "last_updated": now},
            "timezone": "Asia/Shanghai",
            "quiet_hours": list((self.cfg.get("proactive") or {}).get("quiet_hours") or [23, 8]),
            "active_hours": [],
            "proactive": {"enabled": True, "level": "normal", "paused_until": None},
            "last_user_at": None,
            "last_bot_at": None,
            "stats": {"user_msgs": 0, "bot_msgs": 0, "proactive_sent": 0, "days": 0},
        }

    def profile(self, uid: Any) -> dict:
        key = _safe_uid(uid)
        if key not in self._profile_cache:
            data = cfgmod.read_json(self._path(key, "profile.json"), self._default_profile(key))
            if not isinstance(data, dict):
                data = self._default_profile(key)
            data.setdefault("relationship", {"stage": "familiar", "closeness": 0.35})
            if str((data.get("relationship") or {}).get("stage") or "") not in ("stranger", "familiar", "close", "intimate"):
                data["relationship"]["stage"] = "familiar"
            data.setdefault("proactive", {"enabled": True, "level": "normal", "paused_until": None})
            data.setdefault("stats", {})
            for field in ("user_msgs", "bot_msgs", "proactive_sent", "days"):
                data["stats"].setdefault(field, 0)
            self._profile_cache[key] = data
        return self._profile_cache[key]

    def save_profile(self, uid: Any) -> bool:
        key = _safe_uid(uid)
        return cfgmod.atomic_write_json(self._path(key, "profile.json"), self.profile(key))

    def update_profile(self, uid: Any, **fields) -> dict:
        prof = self.profile(uid)
        for key, value in fields.items():
            if key in ("relationship", "proactive", "stats") and isinstance(value, dict):
                current = prof.get(key) if isinstance(prof.get(key), dict) else {}
                current.update(value)
                prof[key] = current
            else:
                prof[key] = value
        self.save_profile(uid)
        return prof

    # ---------- state ----------
    def _default_state(self) -> dict:
        today = cfgmod.today_str()
        return {
            "last_proactive_at": 0.0,
            "proactive": {
                "date": today,
                "count": 0,
                "week_start": datetime.now().strftime("%Y-%m-%d"),
                "week_count": 0,
                "unanswered": 0,
                "last_text": "",
                "recent_texts": [],
            },
            "turns_since_extract": 0,
            "total_turns": 0,
            "last_reflection_at": 0.0,
            "last_extract_at": 0.0,
        }

    def state(self, uid: Any) -> dict:
        key = _safe_uid(uid)
        if key not in self._state_cache:
            data = cfgmod.read_json(self._path(key, "state.json"), self._default_state())
            if not isinstance(data, dict):
                data = self._default_state()
            data.setdefault("proactive", {})
            self._state_cache[key] = data
        return self._state_cache[key]

    def save_state(self, uid: Any) -> bool:
        key = _safe_uid(uid)
        return cfgmod.atomic_write_json(self._path(key, "state.json"), self.state(key))

    # ---------- sessions ----------
    def _session_path(self, uid: Any, day: Optional[str] = None) -> str:
        day = day or cfgmod.today_str()
        return f"{self.base_dir(uid)}/sessions/{day}.jsonl"

    def add_turn(self, uid: Any, role: str, text: str, *, msg_id: Any = None,
                 proactive: bool = False, meta: Optional[dict] = None) -> dict:
        """追加一条会话记录并返回它。

        - 新链路每个 assistant 气泡调用一次，因此 bot_msgs 按气泡计数。
        - meta 只接受 _TURN_META_FIELDS，调用方不能覆盖 role/text/ts/msg_id。
        """
        key = _safe_uid(uid)
        record = {
            "role": role,
            "text": str(text or ""),
            "ts": cfgmod.now_iso(),
            "msg_id": msg_id,
            "proactive": bool(proactive),
        }
        if role == "assistant":
            record.setdefault("source", "proactive" if proactive else "normal")
        if isinstance(meta, dict):
            for field, value in meta.items():
                if field in _TURN_META_FIELDS and value is not None:
                    record[field] = value
        # 首次追加前先装载旧 session，避免之后 recent_turns() 初始化时
        # 用文件尾部覆盖本次进程中已经积累的内存记录。
        self._load_turns_from_disk(key)
        cfgmod.append_jsonl(self._session_path(key), record)
        q = self._turns.setdefault(key, deque(maxlen=300))
        q.append(record)
        st = self.state(key)
        st["total_turns"] = int(st.get("total_turns", 0)) + 1
        if msg_id is not None:
            # 跨重启保留本机已写入的平台 msg_id，供历史补拉去重。
            local_ids = st.get("local_msg_ids") if isinstance(st.get("local_msg_ids"), list) else []
            local_ids.append(str(msg_id))
            st["local_msg_ids"] = local_ids[-500:]
        if role == "user":
            st["turns_since_extract"] = int(st.get("turns_since_extract", 0)) + 1
            prof = self.profile(key)
            prof["last_user_at"] = record["ts"]
            prof["stats"]["user_msgs"] = int(prof["stats"].get("user_msgs", 0)) + 1
            self.save_profile(key)
        elif role == "assistant":
            prof = self.profile(key)
            prof["last_bot_at"] = record["ts"]
            prof["stats"]["bot_msgs"] = int(prof["stats"].get("bot_msgs", 0)) + 1
            self.save_profile(key)
        self.save_state(key)
        return record

    def update_turn(self, uid: Any, msg_id: Any, *, role: str = "user",
                    text: Optional[str] = None, meta: Optional[dict] = None) -> int:
        """原地更新一条用户记录（图片 enrichment 用），不改变位置和统计。

        - 按 msg_id + role 匹配，旧数据里同一 msg_id 有多条重复时全部更新；
        - 记录自身 ts 决定重写哪个 session 日期文件，避免跨午夜写错文件；
        - 找不到返回 0，并记一行日志。
        """
        key = _safe_uid(uid)
        target = str(msg_id)
        fields: dict[str, Any] = {}
        if isinstance(meta, dict):
            for field, value in meta.items():
                if field in _USER_META_FIELDS and value is not None:
                    fields[field] = value
        self._load_turns_from_disk(key)
        q = self._turns.get(key)
        if q is not None:
            for record in q:
                if str(record.get("msg_id")) == target and record.get("role") == role:
                    if text is not None:
                        record["text"] = str(text)
                    record.update(fields)
        # 文件层面按每个匹配记录自己的 ts 找到日期文件；q 只有最近 300 条，
        # 所以文件重写也负责更早的记录。
        updated = self._rewrite_session_days(key, target, role, text, fields)
        if not updated and self.logger:
            self.logger.info(f"[私聊] update_turn 未找到记录 uid={key} msg_id={target}")
        return updated

    def _rewrite_session_days(self, uid: str, target: str, role: str,
                              text: Optional[str], fields: dict) -> int:
        session_dir = f"{self.base_dir(uid)}/sessions"
        try:
            names = sorted(n for n in os.listdir(session_dir) if n.endswith(".jsonl"))
        except OSError:
            return 0
        updated = 0
        for name in names:
            path = os.path.join(session_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except OSError:
                continue
            changed = False
            out: list[str] = []
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    out.append(stripped)
                    continue
                if (isinstance(record, dict)
                        and str(record.get("msg_id")) == target
                        and record.get("role") == role):
                    if text is not None:
                        record["text"] = str(text)
                    record.update(fields)
                    changed = True
                    updated += 1
                out.append(json.dumps(record, ensure_ascii=False))
            if changed:
                cfgmod.atomic_write_text(path, "\n".join(out) + ("\n" if out else ""))
        return updated

    def import_history(self, uid: Any, records: list[dict]) -> int:
        """导入连接前的历史消息：按 msg_id 去重，保留原始 ts，不改变统计/抽取游标。

        无 msg_id 的记录使用「角色 + 原文 + 原始时间戳」稳定键去重；导入完成后
        按 ts 重新排序内存队列，避免把补拉的旧消息追加成“最新消息”。
        """
        key = _safe_uid(uid)
        self._load_turns_from_disk(key)
        q = self._turns.setdefault(key, deque(maxlen=300))
        existing = list(q)
        imported: list[dict] = []
        st = self.state(key)
        history_ids = [str(x) for x in (st.get("history_ids") or [])]
        seen_ids = set(history_ids)
        seen_ids.update(str(x) for x in (st.get("local_msg_ids") or []))
        persisted_keys = {
            tuple(x) for x in (st.get("history_keys") or [])
            if isinstance(x, (list, tuple)) and len(x) == 3
        }
        seen_keys: set[tuple[str, str, str]] = set(persisted_keys)
        for turn in q:
            mid = turn.get("msg_id")
            if mid is not None:
                seen_ids.add(str(mid))
            else:
                seen_keys.add(_history_key(
                    str(turn.get("role") or ""),
                    str(turn.get("text") or ""),
                    str(turn.get("ts") or ""),
                ))

        added = 0
        for rec in sorted(records, key=lambda x: str(x.get("ts") or "")):
            if not isinstance(rec, dict):
                continue
            text = str(rec.get("text") or "").strip()
            if not text:
                continue
            mid = rec.get("msg_id")
            ts = str(rec.get("ts") or cfgmod.now_iso())
            role = "assistant" if rec.get("role") == "assistant" else "user"
            if mid is not None:
                if str(mid) in seen_ids:
                    continue
            else:
                hkey = _history_key(role, text, ts)
                if hkey in seen_keys:
                    continue
            record = {
                "role": role,
                "text": text,
                "ts": ts,
                "msg_id": mid,
                "proactive": False,
                "history": True,
            }
            cfgmod.append_jsonl(self._session_path(key, ts[:10]), record)
            imported.append(record)
            if mid is not None:
                seen_ids.add(str(mid))
                history_ids.append(str(mid))
            else:
                seen_keys.add(_history_key(role, text, ts))
            added += 1
        if added:
            # 先合并再按时间截取，避免有界 deque 在排序前挤掉较新的本地消息。
            self._turns[key] = deque(existing + imported)
            self._sort_turns(key)
            st["history_ids"] = history_ids[-200:]
            st["history_keys"] = [list(x) for x in list(seen_keys)[-500:]]
            st["history_imported_at"] = cfgmod.now_iso()
            self.save_state(key)
        return added

    def _sort_turns(self, uid: Any) -> None:
        key = _safe_uid(uid)
        q = self._turns.get(key)
        if q is None:
            return
        ordered = sorted(list(q), key=_turn_ts_key)
        self._turns[key] = deque(ordered, maxlen=300)

    def _load_turns_from_disk(self, uid: Any) -> None:
        key = _safe_uid(uid)
        if key in self._turns_loaded:
            return
        q: deque = deque(maxlen=300)
        session_dir = f"{self.base_dir(key)}/sessions"
        try:
            names = sorted(n for n in os.listdir(session_dir) if n.endswith(".jsonl"))[-3:]
        except OSError:
            names = []
        for name in names:
            q.extend(cfgmod.read_jsonl_tail(f"{session_dir}/{name}", 120))
        self._turns[key] = deque(sorted(list(q), key=_turn_ts_key), maxlen=300)
        self._turns_loaded.add(key)

    def recent_turns(self, uid: Any, n: int = 20) -> list[dict]:
        key = _safe_uid(uid)
        self._load_turns_from_disk(key)
        return list(self._turns.get(key, ()))[-max(1, int(n)):]

    # ---------- self state ----------
    def _default_self_state(self) -> dict:
        return {
            "mood": "平静",
            "energy": 0.6,
            "today": "",
            "looking_forward_to": "",
            "recent_thoughts": [],
            "summary": "",
            "last_reflection_at": 0.0,
        }

    def self_state(self, uid: Any) -> dict:
        key = _safe_uid(uid)
        if key not in self._self_cache:
            data = cfgmod.read_json(self._path(key, "self_state.json"), self._default_self_state())
            if not isinstance(data, dict):
                data = self._default_self_state()
            self._self_cache[key] = data
        return self._self_cache[key]

    def save_self_state(self, uid: Any, data: Optional[dict] = None) -> bool:
        key = _safe_uid(uid)
        if data is not None:
            self._self_cache[key] = data
        return cfgmod.atomic_write_json(self._path(key, "self_state.json"), self.self_state(key))

    # ---------- memories ----------
    def _empty_memories(self) -> dict:
        return {"version": 1, "items": []}

    def memories(self, uid: Any) -> dict:
        key = _safe_uid(uid)
        if key not in self._mem_cache:
            data = cfgmod.read_json(self._path(key, "memories.json"), self._empty_memories())
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                data = self._empty_memories()
            self._mem_cache[key] = data
        return self._mem_cache[key]

    def save_memories(self, uid: Any) -> bool:
        key = _safe_uid(uid)
        data = self.memories(key)
        data["items"] = data["items"][-_MAX_MEMORIES:]
        return cfgmod.atomic_write_json(self._path(key, "memories.json"), data)

    def memory_items(self, uid: Any) -> list[dict]:
        return list(self.memories(uid).get("items") or [])

    def add_memories(self, uid: Any, candidates: Iterable[dict]) -> list[dict]:
        """写入候选记忆：去重/合并，返回真正新增的条目。"""
        key = _safe_uid(uid)
        items = self.memory_items(key)
        added: list[dict] = []
        changed = False
        now = cfgmod.now_iso()
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            text = str(cand.get("text") or "").strip()
            mtype = str(cand.get("type") or "user_fact")
            if not text or mtype not in _ALLOWED_TYPES:
                continue
            text = text[: int((self.cfg.get("memory") or {}).get("max_memory_chars", 120))]
            tokens = _tokens(text)
            duplicate = None
            for old in items:
                if _jaccard(tokens, _tokens(str(old.get("text") or ""))) >= 0.86:
                    duplicate = old
                    break
            if duplicate is not None:
                changed = True
                duplicate["importance"] = max(int(duplicate.get("importance", 3)), int(cand.get("importance", 3) or 3))
                duplicate["updated_at"] = now
                src = duplicate.get("source_ids") if isinstance(duplicate.get("source_ids"), list) else []
                for sid in cand.get("source_ids") or []:
                    if sid not in src:
                        src.append(sid)
                duplicate["source_ids"] = src[-20:]
                continue
            item = {
                "id": f"m_{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:6]}",
                "type": mtype,
                "text": text,
                "importance": max(1, min(5, int(cand.get("importance", 3) or 3))),
                "confidence": max(0.0, min(1.0, float(cand.get("confidence", 0.7) or 0.7))),
                "source_ids": [str(x) for x in (cand.get("source_ids") or [])][-20:],
                "created_at": now,
                "updated_at": now,
                "last_used_at": None,
                "supersedes": cand.get("supersedes"),
                "tags": [str(x) for x in (cand.get("tags") or [])][:8],
            }
            items.append(item)
            added.append(item)
            changed = True
        if changed:
            self.memories(key)["items"] = items
            self.save_memories(key)
        return added

    def mark_used(self, uid: Any, ids: Iterable[str]) -> None:
        wanted = {str(x) for x in ids}
        if not wanted:
            return
        changed = False
        now = cfgmod.now_iso()
        for item in self.memory_items(uid):
            if str(item.get("id")) in wanted:
                item["last_used_at"] = now
                changed = True
        if changed:
            self.save_memories(uid)

    def forget(self, uid: Any, keyword: str) -> int:
        query = _tokens(keyword)
        items = self.memory_items(uid)
        if not query:
            return 0
        kept = []
        removed = 0
        for item in items:
            if _jaccard(query, _tokens(str(item.get("text") or ""))) >= 0.34 or keyword in str(item.get("text") or ""):
                removed += 1
            else:
                kept.append(item)
        if removed:
            self.memories(uid)["items"] = kept
            self.save_memories(uid)
        return removed

    def retrieve(self, uid: Any, query: str, k: Optional[int] = None) -> list[dict]:
        """关键词 + 重要度 + 时间衰减的混合检索（单用户规模足够）。"""
        k = int(k or (self.cfg.get("memory") or {}).get("max_memories_in_prompt", 10))
        q = _tokens(query)
        scored: list[tuple[float, dict]] = []
        for item in self.memory_items(uid):
            text = str(item.get("text") or "")
            overlap = _jaccard(q, _tokens(text)) if q else 0.0
            importance = max(1, min(5, int(item.get("importance", 3) or 3))) / 5.0
            recency = 1.0 / (1.0 + _days_since(item.get("updated_at") or item.get("created_at")) / 30.0)
            prior = _TYPE_PRIOR.get(str(item.get("type")), 0.5)
            score = 0.50 * overlap + 0.25 * importance + 0.15 * recency + 0.10 * prior
            if score > 0.02:
                scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored[:max(1, k)]]

    # ---------- summaries ----------
    def latest_summary(self, uid: Any) -> str:
        data = cfgmod.read_json(self._path(uid, "summaries.json"), {"items": []})
        items = data.get("items") if isinstance(data, dict) else []
        if isinstance(items, list) and items:
            return str(items[-1].get("text") or "")
        return ""

    def add_summary(self, uid: Any, text: str) -> None:
        text = str(text or "").strip()
        if not text:
            return
        path = self._path(uid, "summaries.json")
        data = cfgmod.read_json(path, {"items": []})
        if not isinstance(data, dict):
            data = {"items": []}
        items = data.get("items") if isinstance(data.get("items"), list) else []
        items.append({"ts": cfgmod.now_iso(), "text": text[:1200]})
        data["items"] = items[-60:]
        cfgmod.atomic_write_json(path, data)

    # ---------- 派生对话状态 ----------
    def load_dialogue_state(self, uid: Any) -> dict:
        path = self._path(uid, "dialogue_state.json")
        data = cfgmod.read_json(path, {})
        return data if isinstance(data, dict) else {}

    def save_dialogue_state(self, uid: Any, data: dict) -> bool:
        return cfgmod.atomic_write_json(self._path(uid, "dialogue_state.json"), data or {})

    # ---------- 发送 attempt ledger ----------
    def _attempts_path(self, uid: Any) -> str:
        return self._path(uid, "attempts.jsonl")

    def add_attempt(self, uid: Any, record: dict) -> bool:
        if not isinstance(record, dict) or not record.get("attempt_id"):
            return False
        item = dict(record)
        item.setdefault("status", "pending")
        item.setdefault("attempted_at", cfgmod.now_iso())
        return cfgmod.append_jsonl(self._attempts_path(uid), item)

    def finish_attempt(self, uid: Any, attempt_id: str, *, status: str,
                       message_id: Any = None, error: str = "", echo: str = "",
                       reconciled_at: str = "") -> bool:
        """把 attempt 的最终结果回写到 ledger（同 attempt_id 覆盖）。"""
        path = self._attempts_path(uid)
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
        except OSError:
            return False
        found = False
        out: list[str] = []
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and str(item.get("attempt_id")) == str(attempt_id):
                item["status"] = str(status)
                if message_id is not None:
                    item["msg_id"] = str(message_id)
                if echo:
                    item["echo"] = str(echo)
                if error:
                    item["error"] = str(error)[:300]
                if reconciled_at:
                    item["reconciled_at"] = str(reconciled_at)
                found = True
                out.append(json.dumps(item, ensure_ascii=False))
            else:
                out.append(line)
        if not found:
            out.append(json.dumps({
                "attempt_id": str(attempt_id),
                "status": str(status),
                "msg_id": str(message_id) if message_id is not None else None,
                "error": str(error)[:300],
                "attempted_at": cfgmod.now_iso(),
            }, ensure_ascii=False))
        out = out[-_MAX_ATTEMPTS:]
        return cfgmod.atomic_write_text(path, "\n".join(out) + ("\n" if out else ""))

    def attempts(self, uid: Any, limit: int = 100) -> list[dict]:
        lines = cfgmod.read_jsonl_tail(self._attempts_path(uid), max(1, int(limit)))
        return [x for x in lines if isinstance(x, dict)]

    def unresolved_attempts(self, uid: Any, limit: int = 100) -> list[dict]:
        # unknown 也是“结果未确认”，阶段 2 的精确核对需要看到它。
        return [x for x in self.attempts(uid, limit)
                if x.get("status") in ("pending", "unknown")]

    # ---------- 主动消息计数 ----------
    def proactive_state(self, uid: Any) -> dict:
        st = self.state(uid)
        p = st.setdefault("proactive", {})
        today = cfgmod.today_str()
        if p.get("date") != today:
            p["date"] = today
            p["count"] = 0
            p["unanswered"] = 0
        return p

    def record_proactive(self, uid: Any, text: str, *, dry_run: bool = False) -> None:
        st = self.state(uid)
        p = self.proactive_state(uid)
        if not dry_run:
            p["count"] = int(p.get("count", 0)) + 1
            p["week_count"] = int(p.get("week_count", 0)) + 1
            p["unanswered"] = int(p.get("unanswered", 0)) + 1
            p["last_text"] = text
            recent = p.get("recent_texts") if isinstance(p.get("recent_texts"), list) else []
            recent.append(text[:120])
            p["recent_texts"] = recent[-10:]
            st["last_proactive_at"] = time.time()
            prof = self.profile(uid)
            prof["stats"]["proactive_sent"] = int(prof["stats"].get("proactive_sent", 0)) + 1
            self.save_profile(uid)
        self.save_state(uid)

    def reset_unanswered(self, uid: Any) -> None:
        p = self.proactive_state(uid)
        if int(p.get("unanswered", 0) or 0):
            p["unanswered"] = 0
            self.save_state(uid)
