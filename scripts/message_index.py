# -*- coding: utf-8 -*-
"""消息索引 —— message_id → 消息元数据的环形缓冲。

**存在的唯一原因**：NapCat 的 `replyElement` 解码后只给被引用消息的
`message_id`，既不给原文也不给作者 uid（napcat.mjs 源码取证）。所以要自己记一份。

项目已有先例：`scripts/image_cache.py` 为识图做同样的事（注释里写明 OneBot
`get_image` 是请求-响应式、实测不可用）——但那条路只服务图片，这里服务指代。

纪律：
- **纯内存**，不持久化（重启即空，属预期行为）
- 每群独立环形，有上限（默认 200 条）
- **绝不存图片字节 / base64 / data URL**，只存文本与 id
- 任何入参异常都**不得抛出**（指代是增强项，不能拖垮消息主流程）
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional

DEFAULT_MAX_PER_GROUP = 200

# 记录里允许出现的键（其余一律丢弃，防止把整个事件原样吞进来）
_ALLOWED_KEYS = (
    "message_id", "uid", "card", "nickname", "role",
    "text", "at", "reply_id", "ts",
)


def _s(value: Any) -> str:
    """安全转字符串（None → 空串）。"""
    return "" if value is None else str(value)


class MessageIndex:
    """按群维护的环形消息索引。查不到就返回 None，从不抛异常。"""

    def __init__(self, max_per_group: int = DEFAULT_MAX_PER_GROUP):
        try:
            n = int(max_per_group)
        except (TypeError, ValueError):
            n = DEFAULT_MAX_PER_GROUP
        self._max = n if n > 0 else DEFAULT_MAX_PER_GROUP
        self._groups: Dict[str, deque] = defaultdict(lambda: deque(maxlen=self._max))

    # ---------- 写 ----------

    def remember(self, gid: Any, record: Dict[str, Any]) -> None:
        """记一条消息。缺字段补默认值；异常一律吞掉。"""
        try:
            if not isinstance(record, dict):
                return
            gid_s = _s(gid)
            mid = _s(record.get("message_id"))
            if not gid_s or not mid:
                return
            item = {k: record.get(k) for k in _ALLOWED_KEYS}
            item["message_id"] = mid
            item["uid"] = _s(item.get("uid"))
            item["card"] = _s(item.get("card"))
            item["nickname"] = _s(item.get("nickname"))
            item["role"] = _s(item.get("role"))
            item["text"] = _s(item.get("text"))
            item["reply_id"] = _s(item.get("reply_id"))
            at = item.get("at")
            item["at"] = [_s(x) for x in at if _s(x)] if isinstance(at, (list, tuple)) else []
            try:
                item["ts"] = float(item.get("ts") or time.time())
            except (TypeError, ValueError):
                item["ts"] = time.time()
            self._groups[gid_s].append(item)
        except Exception:
            pass

    # ---------- 读 ----------

    def get(self, gid: Any, message_id: Any) -> Optional[dict]:
        """按 message_id 取一条。线性扫 <=max 条，代价可忽略。"""
        try:
            mid = _s(message_id)
            if not mid:
                return None
            for item in reversed(self._groups.get(_s(gid), ())):
                if item.get("message_id") == mid:
                    return item
        except Exception:
            pass
        return None

    def recent(self, gid: Any, n: int = 20) -> List[dict]:
        """最近 n 条（时间正序）。"""
        try:
            buf = self._groups.get(_s(gid))
            if not buf:
                return []
            try:
                k = int(n)
            except (TypeError, ValueError):
                k = 20
            if k <= 0:
                return []
            return list(buf)[-k:]
        except Exception:
            return []

    def clear(self, gid: Any = None) -> None:
        try:
            if gid is None:
                self._groups.clear()
            else:
                self._groups.pop(_s(gid), None)
        except Exception:
            pass

    def stats(self) -> dict:
        try:
            return {"groups": len(self._groups), "total": sum(len(v) for v in self._groups.values()),
                    "max_per_group": self._max}
        except Exception:
            return {"groups": 0, "total": 0, "max_per_group": self._max}


# ---------- 模块级单例（与 image_cache 同风格） ----------

_INDEX = MessageIndex()


def configure(max_per_group: int) -> None:
    global _INDEX
    _INDEX = MessageIndex(max_per_group)


def remember(gid, record) -> None:
    _INDEX.remember(gid, record)


def get(gid, message_id):
    return _INDEX.get(gid, message_id)


def recent(gid, n=20):
    return _INDEX.recent(gid, n)


def clear(gid=None) -> None:
    _INDEX.clear(gid)


def stats() -> dict:
    return _INDEX.stats()
