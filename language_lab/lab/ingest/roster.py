# -*- coding: utf-8 -*-
"""名册（roster）解析：昵称/群名片 → uid。

机器人日志里没有 uid，只能靠 `data/roster.json` 反查。名册结构（实测）：
    users[uid] = {
      "nickname": {"value": ..., "seen_at": ...},
      "groups": {"284517232": {"cards": [{"value": ...}, ...],
                               "current_card": ..., "aliases": [...], ...}},
      ...
    }

解析原则：只有**唯一命中**才给出 uid；同名多人返回 None（宁可缺，不可错）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, Optional, Set


class Roster:
    def __init__(self, gid: str):
        self.gid = gid
        self._index: Dict[str, Set[str]] = {}
        self._uid_nick: Dict[str, str] = {}
        self.users: dict = {}

    @classmethod
    def load(cls, path: str | Path, gid: str) -> "Roster":
        inst = cls(gid)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return inst
        users = data.get("users") if isinstance(data, dict) else None
        if not isinstance(users, dict):
            return inst
        inst.users = users
        for uid, info in users.items():
            if not isinstance(info, dict):
                continue
            names: Set[str] = set()
            nick = (info.get("nickname") or {}).get("value") if isinstance(info.get("nickname"), dict) else None
            if nick:
                names.add(str(nick))
            grp = (info.get("groups") or {}).get(gid) or {}
            if isinstance(grp, dict):
                if grp.get("current_card"):
                    names.add(str(grp["current_card"]))
                for card in grp.get("cards") or []:
                    if isinstance(card, dict) and card.get("value"):
                        names.add(str(card["value"]))
                for alias in grp.get("aliases") or []:
                    if alias:
                        names.add(str(alias))
            names.discard("")
            for name in names:
                inst._index.setdefault(name, set()).add(str(uid))
            if nick:
                inst._uid_nick.setdefault(str(uid), str(nick))
        return inst

    def resolve(self, name: str) -> Optional[str]:
        if not name:
            return None
        hits = self._index.get(name.strip())
        if not hits:
            return None
        return next(iter(hits)) if len(hits) == 1 else None

    def is_ambiguous(self, name: str) -> bool:
        hits = self._index.get((name or "").strip())
        return bool(hits) and len(hits) > 1

    def nick_of(self, uid: str) -> str:
        return self._uid_nick.get(str(uid), "")

    def size(self) -> int:
        return len(self.users)

    def names_for(self, uid: str) -> list[str]:
        return [n for n, hits in self._index.items() if str(uid) in hits]

    def unique_map(self) -> Dict[str, str]:
        """昵称 → uid，只保留唯一映射。"""
        return {name: next(iter(hits)) for name, hits in self._index.items() if len(hits) == 1}


def load(path: str | Path, gid: str) -> Roster:
    return Roster.load(path, gid)
