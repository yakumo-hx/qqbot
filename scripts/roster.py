# -*- coding: utf-8 -*-
"""群成员名册 —— 以 QQ 号为唯一键，名字只是带时间戳的观测值。

设计依据：`qq_native_re/findings/reference/DESIGN.md` §1、§4.2

三条硬规则（改动本文件前请先读）：

1. **uid 是唯一键；card / nickname 是挂在 uid 上的观测，不是标识。**
   群名片与 QQ 昵称随时可改，所以**禁止**任何「名字 → uid」的静态映射表。
   同一 uid 可以有多条历史名字——它们**都是事实**（在不同时间成立）。

2. **`resolve()` 的输出永不回写名册。**
   「名字 → uid」是模糊推断（可能 0 个、1 个、多个），一旦写回就变成"事实"，
   后续会自我强化、越错越离谱。名册的写入只允许两个来源：
   ① 消息事件里的 sender 字段（权威）  ② 人工显式登记的别名（source=manual）

3. 渲染历史消息用**当时的快照**（调用方从 message_index 取），
   不要用名册的 current 去覆盖它。名册的 current 只在需要问
   「他**现在**叫什么」时用（如【群友印象】注入）。

持久化：`data/roster.json`（含真实 QQ 号，已在 .gitignore 排除）
写入节流：只有「看到新名字 / 新面孔」才标脏，且落盘间隔 >= flush_seconds
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

DEFAULT_RETENTION_DAYS = 180
DEFAULT_CARD_HISTORY_MAX = 5
DEFAULT_FLUSH_SECONDS = 300

_ALIAS_MANUAL = "manual"
_ALIAS_INFERRED = "inferred"


def _s(value: Any, limit: int = 0) -> str:
    text = "" if value is None else str(value).strip()
    if limit and len(text) > limit:
        text = text[:limit]
    return text


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class Roster:
    """群成员名册。所有公开方法都不抛异常。"""

    def __init__(
        self,
        path: str,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        card_history_max: int = DEFAULT_CARD_HISTORY_MAX,
        flush_seconds: int = DEFAULT_FLUSH_SECONDS,
        clock=time.time,
    ):
        self._path = path
        self._retention = max(1, int(retention_days or DEFAULT_RETENTION_DAYS)) * 86400
        self._card_max = max(1, int(card_history_max or DEFAULT_CARD_HISTORY_MAX))
        self._flush_seconds = max(0, int(flush_seconds if flush_seconds is not None else DEFAULT_FLUSH_SECONDS))
        self._clock = clock
        self._lock = threading.Lock()
        self._users: Dict[str, dict] = {}
        self._dirty = False
        self._last_flush = 0.0
        self._loaded = False

    # ---------- 持久化 ----------

    def load(self) -> bool:
        """读盘。文件不存在/损坏 → 空名册（不抛）。"""
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            users = raw.get("users") if isinstance(raw, dict) else None
            with self._lock:
                self._users = users if isinstance(users, dict) else {}
                self._dirty = False
                self._loaded = True
                self._last_flush = _f(self._clock())
            return True
        except Exception:
            with self._lock:
                self._users = {}
                self._loaded = True
            return False

    def save(self, force: bool = False) -> bool:
        """原子落盘。非 force 时受 flush_seconds 节流。

        失败时**不清 `_dirty`**——否则一次写盘失败会让脏数据永远丢掉；
        但仍然推进 `_last_flush`，避免每条消息都重试一次写盘。
        """
        with self._lock:
            if not self._dirty:
                return False
            now = _f(self._clock())
            if not force and self._flush_seconds > 0 and (now - self._last_flush) < self._flush_seconds:
                return False
            payload = {
                "version": 1,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "users": self._users,
            }
        ok = False
        try:
            folder = os.path.dirname(os.path.abspath(self._path)) or "."
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".roster-", suffix=".tmp", dir=folder)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=1)
                os.replace(tmp, self._path)
                ok = True
            except Exception:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
        except Exception:
            ok = False
        with self._lock:
            self._last_flush = now
            if ok:
                self._dirty = False
        return ok

    def flush_if_due(self) -> bool:
        return self.save(force=False)

    # ---------- 写（唯一合法入口） ----------

    def observe(self, gid: Any, uid: Any, card: Any = "", nickname: Any = "",
                role: Any = "", ts: Any = None) -> bool:
        """记录一次「观测到某人以某名字出现」。返回 True 表示发现了新信息。

        只接受来自消息事件 sender 字段的数据（权威来源）。
        **不接受**任何「从文本里猜出来的名字」。"""
        try:
            gid_s, uid_s = _s(gid), _s(uid)
            if not gid_s or not uid_s:
                return False
            now = _f(ts) or _f(self._clock())
            card_s = _s(card, 64)
            nick_s = _s(nickname, 64)
            role_s = _s(role, 16)
            new_info = False
            with self._lock:
                user = self._users.get(uid_s)
                if not isinstance(user, dict):
                    user = {"nickname": {"value": "", "seen_at": 0.0}, "groups": {}}
                    self._users[uid_s] = user
                    new_info = True
                groups = user.setdefault("groups", {})
                if not isinstance(groups, dict):
                    groups = {}
                    user["groups"] = groups
                grp = groups.get(gid_s)
                if not isinstance(grp, dict):
                    grp = {"cards": [], "current_card": "", "aliases": [],
                           "first_seen": now, "last_seen": now, "msg_count": 0}
                    groups[gid_s] = grp
                    new_info = True

                # QQ 昵称（全局属性）
                nk = user.get("nickname")
                if not isinstance(nk, dict):
                    nk = {"value": "", "seen_at": 0.0}
                    user["nickname"] = nk
                if nick_s and nk.get("value") != nick_s:
                    nk["value"] = nick_s
                    nk["seen_at"] = now
                    new_info = True

                # 群名片（群级属性，保留历史）
                cards = grp.get("cards")
                if not isinstance(cards, list):
                    cards = []
                    grp["cards"] = cards
                if card_s and grp.get("current_card") != card_s:
                    grp["current_card"] = card_s
                    if not any(isinstance(c, dict) and c.get("value") == card_s for c in cards):
                        cards.append({"value": card_s, "seen_at": now})
                        if len(cards) > self._card_max:
                            del cards[: len(cards) - self._card_max]
                        new_info = True
                if role_s:
                    grp["role"] = role_s
                grp["last_seen"] = now
                try:
                    grp["msg_count"] = int(grp.get("msg_count", 0)) + 1
                except (TypeError, ValueError):
                    grp["msg_count"] = 1
                if new_info:
                    self._dirty = True
            # 锁外落盘：save() 自己会再取锁，持锁调用会死锁。
            # **每次 observe 都试一次**，而不是只在 new_info 时试：
            # 否则「新面孔出现在节流窗口内、此后不再有新面孔」时，
            # 那份脏数据会一直留在内存里，硬杀即丢（save 内部会自己判脏与节流，代价可忽略）。
            self.save(force=False)
            return new_info
        except Exception:
            return False

    # ---------- 读（确定性） ----------

    def display_of(self, gid: Any, uid: Any) -> str:
        """uid → 展示名。**确定性**，回退链 card → nickname → uid，永不返回空串。"""
        uid_s = _s(uid)
        if not uid_s:
            return ""
        try:
            with self._lock:
                user = self._users.get(uid_s)
                if not isinstance(user, dict):
                    return uid_s
                grp = (user.get("groups") or {}).get(_s(gid))
                if isinstance(grp, dict):
                    card = _s(grp.get("current_card"))
                    if card:
                        return card
                nk = user.get("nickname")
                if isinstance(nk, dict):
                    name = _s(nk.get("value"))
                    if name:
                        return name
        except Exception:
            pass
        return uid_s

    def known_names(self, gid: Any, uid: Any) -> List[str]:
        """某人已知的全部名字（当前名片 + 历史名片 + 昵称 + 别名），去重。"""
        out: List[str] = []
        try:
            with self._lock:
                user = self._users.get(_s(uid))
                if not isinstance(user, dict):
                    return out
                grp = (user.get("groups") or {}).get(_s(gid))
                if isinstance(grp, dict):
                    for c in (grp.get("cards") or []):
                        v = _s(c.get("value")) if isinstance(c, dict) else ""
                        if v and v not in out:
                            out.append(v)
                    for a in (grp.get("aliases") or []):
                        if isinstance(a, dict) and a.get("source") == _ALIAS_MANUAL:
                            v = _s(a.get("value"))
                            if v and v not in out:
                                out.append(v)
                nk = user.get("nickname")
                if isinstance(nk, dict):
                    v = _s(nk.get("value"))
                    if v and v not in out:
                        out.append(v)
        except Exception:
            pass
        return out

    # ---------- 读（模糊，只读，绝不回写） ----------

    def resolve(self, gid: Any, name: Any) -> List[str]:
        """名字 → uid 候选列表。**模糊**：可能 0 个、1 个、多个。

        ⚠️ 硬规则（DESIGN.md §1.3）：本方法的输出**只能用于当前这一轮**，
        **绝不允许回写名册**。调用方不得把它当作「事实」持久化。
        """
        target = _s(name)
        if not target:
            return []
        out: List[str] = []
        try:
            with self._lock:
                for uid_s, user in self._users.items():
                    if not isinstance(user, dict):
                        continue
                    hit = False
                    grp = (user.get("groups") or {}).get(_s(gid))
                    if isinstance(grp, dict):
                        if _s(grp.get("current_card")) == target:
                            hit = True
                        if not hit:
                            for c in (grp.get("cards") or []):
                                if isinstance(c, dict) and _s(c.get("value")) == target:
                                    hit = True
                                    break
                        if not hit:
                            for a in (grp.get("aliases") or []):
                                if (isinstance(a, dict) and a.get("source") == _ALIAS_MANUAL
                                        and _s(a.get("value")) == target):
                                    hit = True
                                    break
                    if not hit:
                        nk = user.get("nickname")
                        if isinstance(nk, dict) and _s(nk.get("value")) == target:
                            hit = True
                    if hit:
                        out.append(uid_s)
        except Exception:
            return []
        return out

    # ---------- 维护 ----------

    def prune(self, now: Any = None) -> int:
        """清理超过保留期没出现的人。返回清理数量。"""
        try:
            cutoff = (_f(now) or _f(self._clock())) - self._retention
            removed = 0
            with self._lock:
                for uid_s in list(self._users.keys()):
                    user = self._users.get(uid_s)
                    if not isinstance(user, dict):
                        del self._users[uid_s]
                        removed += 1
                        continue
                    groups = user.get("groups") or {}
                    latest = 0.0
                    for grp in groups.values():
                        if isinstance(grp, dict):
                            latest = max(latest, _f(grp.get("last_seen")))
                    if latest and latest < cutoff:
                        del self._users[uid_s]
                        removed += 1
                if removed:
                    self._dirty = True
            return removed
        except Exception:
            return 0

    def stats(self) -> dict:
        try:
            with self._lock:
                groups = set()
                total_msgs = 0
                multi_name = 0
                for user in self._users.values():
                    if not isinstance(user, dict):
                        continue
                    for gid_s, grp in (user.get("groups") or {}).items():
                        groups.add(gid_s)
                        if isinstance(grp, dict):
                            total_msgs += int(grp.get("msg_count", 0) or 0)
                            if len(grp.get("cards") or []) > 1:
                                multi_name += 1
                return {"users": len(self._users), "groups": len(groups),
                        "messages": total_msgs, "renamed": multi_name,
                        "dirty": self._dirty, "path": self._path}
        except Exception:
            return {"users": 0, "groups": 0, "messages": 0, "renamed": 0,
                    "dirty": False, "path": self._path}


# ---------- 模块级单例 ----------

_ROSTER: Optional[Roster] = None


def configure(path: str, **kwargs) -> Roster:
    """由 reference.configure() 调用；重复调用会重建实例。"""
    global _ROSTER
    _ROSTER = Roster(path, **kwargs)
    _ROSTER.load()
    return _ROSTER


def get_roster() -> Optional[Roster]:
    return _ROSTER


def observe(gid, uid, card="", nickname="", role="", ts=None) -> bool:
    return _ROSTER.observe(gid, uid, card, nickname, role, ts) if _ROSTER else False


def display_of(gid, uid) -> str:
    return _ROSTER.display_of(gid, uid) if _ROSTER else _s(uid)


def resolve(gid, name) -> List[str]:
    return _ROSTER.resolve(gid, name) if _ROSTER else []


def flush(force: bool = False) -> bool:
    return _ROSTER.save(force=force) if _ROSTER else False


def stats() -> dict:
    return _ROSTER.stats() if _ROSTER else {}
