# -*- coding: utf-8 -*-
"""W2 · 上下文渲染器（与生产 `scripts/reference.py` 的 build_bracket + render 逐字一致）。

纯函数，**运行期不 import scripts.***；`scripts.reference` 只允许在测试里当 oracle。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

QUOTE_MAX_CHARS = 40
LAST_MARK = "  ← 最后一条"


def _display(item: dict) -> str:
    """card → nick → uid（与 production display_from 一致，历史消息用当时快照名）。"""
    card = str(item.get("card") or "").strip()
    if card:
        return card
    nick = str(item.get("nick") or item.get("nickname") or "").strip()
    if nick:
        return nick
    return str(item.get("uid") or "").strip()


def _truncate(text: Any, limit: int) -> str:
    s = str(text or "").strip().replace("\n", " ")
    if limit > 0 and len(s) > limit:
        return s[:limit] + "…"
    return s


def _name_of(name_of: Callable[[str], str] | None, uid: str) -> str:
    if callable(name_of):
        try:
            nm = str(name_of(uid) or "").strip()
            if nm:
                return nm
        except Exception:  # noqa: BLE001 - 与生产一致：取名失败用 uid
            pass
    return uid


def build_bracket(item: dict, *, name_of=None, quote_max_chars: int = QUOTE_MAX_CHARS) -> str:
    """拼方括号内部（不含 [] 与正文）。结构与 production build_bracket 逐字对齐。"""
    speaker = _display(item)
    at_uids = [str(x) for x in (item.get("at") or [])]
    reply = item.get("reply_to") or None
    if reply:
        reply_uid = str(reply.get("uid") or "")
        if reply_uid and reply_uid in at_uids:
            at_uids = [u for u in at_uids if u != reply_uid]
    else:
        reply_uid = ""

    parts = speaker
    if at_uids:
        names: list[str] = []
        for uid in at_uids:
            nm = _name_of(name_of, uid)
            if nm and nm not in names:
                names.append(nm)
        if names:
            parts += "@" + ",".join(names)

    if reply is not None:
        who = _display(reply)
        if who:
            body = _truncate(reply.get("text"), quote_max_chars)
            if body:
                parts += " 回复" + who + "「" + body + "」"
            else:
                parts += " 回复" + who
        else:
            # 有 reply_to 但既无 uid/nick：按"引用某条消息"降级（不编造）
            parts += " 回复某条消息"
    return parts


def render_line(item: dict, *, name_of=None, quote_max_chars: int = QUOTE_MAX_CHARS) -> str:
    """一条上下文 → 一行。无正文且无 @ → 空串。"""
    try:
        text = str(item.get("text") or "").strip()
        bracket = build_bracket(item, name_of=name_of, quote_max_chars=quote_max_chars)
        if not text:
            if item.get("at"):
                return "[" + bracket + "]（只 @ 了人，没有文字）"
            return ""
        return "[" + bracket + "] " + text
    except Exception:  # noqa: BLE001 - 与生产一致：坏行返回空串，跳过
        return ""


def render_context(items: Iterable[dict], *, mark_last: bool = True,
                   name_of=None, quote_max_chars: int = QUOTE_MAX_CHARS) -> list[str]:
    """渲染整个上下文；空行跳过；末条追加 `  ← 最后一条`。"""
    lines = []
    for item in items or []:
        line = render_line(item, name_of=name_of, quote_max_chars=quote_max_chars)
        if line:
            lines.append(line)
    if mark_last and lines:
        lines[-1] = lines[-1] + LAST_MARK
    return lines
