# -*- coding: utf-8 -*-
"""机器人日志 `[指代]` 行解析。

这一层从 `logs/bot.YYYY-MM-DD.log` 抽取「谁、在几点、说了什么、回复了谁」。
它覆盖 10-20 起的 9 天文本（LLBot 只从 10-24 开始记），代价是没有 uid、且媒体消息
几乎不落日志，所以它只作为**文本与回复关系的补充源**。

真实格式（消息正文可跨多行）：
    [2026-10-28 15:44:30] [指代] [爱莉不知道哦 回复游云不喜欢哦「“我怎么办？”」] 答案是三○主义青年团
    [2026-10-21 20:40:04] [指代] [爱莉不知道哦@.,爱莉不知道哦：女装了［图片］［图片］［ 回复爱莉不知道哦]（只 @ 了人，没有文字）
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from typing import Iterable, List

TZ = timezone(timedelta(hours=8))

_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\] \[([^\]]+)\] ?(.*)$")
_HEAD_RE = re.compile(r"^\[(.+?)\](.*)$")
_AT_ONLY = "（只 @ 了人，没有文字）"

_MEDIA_MAP = (
    ("［合并转发］", "forward"),
    ("［图片］", "image"),
    ("［表情］", "face"),
    ("［贴图］", "face"),
    ("［语音］", "voice"),
    ("［视频］", "video"),
    ("［文件］", "file"),
    ("［链接］", "link"),
    ("［红包］", "link"),
    ("[图片]", "image"),
    ("[表情]", "face"),
    ("[语音]", "voice"),
    ("[视频]", "video"),
    ("[文件]", "file"),
    ("[合并转发]", "forward"),
    ("[链接]", "link"),
)

_PLACEHOLDER = {
    "image": "[图片]", "face": "[表情]", "voice": "[语音]", "video": "[视频]",
    "file": "[文件]", "forward": "[合并转发]", "link": "[链接]",
}

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)
_NICK_BAD_TAIL = "，,、;；:： "


def _ts(date: str, clock: str) -> datetime | None:
    try:
        return datetime.strptime(f"{date} {clock}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    except ValueError:
        return None


def parse_head(head: str) -> dict:
    """拆 `[昵称@某人 回复谁「引用」]`。返回 nick / at / reply_nick / reply_text。"""
    out = {"nick": "", "at": [], "reply_nick": "", "reply_text": ""}
    left = head.strip()
    idx = left.rfind(" 回复")
    if idx >= 0:
        right = left[idx + 3:].strip()
        left = left[:idx].strip()
        m = re.match(r"^(.+?)(?:「(.*)」)?$", right)
        if m:
            out["reply_nick"] = (m.group(1) or "").strip()
            out["reply_text"] = (m.group(2) or "").strip().rstrip("…").strip()
        else:
            out["reply_nick"] = right
    at: list[str] = []
    if "@" in left:
        nick, rest = left.split("@", 1)
        at_part = rest.split("：")[0]
        at = [x.strip(_NICK_BAD_TAIL) for x in at_part.split(",") if x.strip(_NICK_BAD_TAIL)]
    else:
        nick = left
    if "：" in nick:
        nick = nick.split("：")[0]
    out["nick"] = nick.strip().rstrip(_NICK_BAD_TAIL)[:40]
    out["at"] = at[:10]
    return out


def _body(text: str) -> tuple[str, list[str], bool]:
    """正文占位符归一化。返回 (text, media, at_only)。"""
    at_only = _AT_ONLY in text
    text = text.replace(_AT_ONLY, "").strip()
    media: list[str] = []
    for token, kind in _MEDIA_MAP:
        if token in text:
            media.append(kind)
            text = text.replace(token, _PLACEHOLDER[kind])
    text = text.translate(_ZERO_WIDTH).strip()
    # 去重保序
    seen: list[str] = []
    for k in media:
        if k not in seen:
            seen.append(k)
    return text, seen, at_only


def iter_records(lines: Iterable[str]) -> List[dict]:
    """按时间戳切块 → 解析 [指代] → 补时间。"""
    records: List[dict] = []
    cur: dict | None = None
    for raw in lines:
        line = raw.rstrip("\n")
        m = _TS_RE.match(line)
        if m:
            if cur is not None:
                rec = _finish(cur)
                if rec:
                    records.append(rec)
            cur = {
                "dt": _ts(m.group(1), m.group(2)),
                "day": m.group(1),
                "tag": m.group(3),
                "rest": m.group(4),
                "cont": [],
            }
        elif cur is not None and line.strip():
            cur["cont"].append(line)
    if cur is not None:
        rec = _finish(cur)
        if rec:
            records.append(rec)
    return records


def _finish(cur: dict) -> dict | None:
    if cur["tag"] != "指代" or cur["dt"] is None:
        return None
    m = _HEAD_RE.match(cur["rest"])
    if not m:
        return None  # 初始化调试行 `[指代] 注解层已启用=True ...`
    parsed = parse_head(m.group(1))
    if not parsed["nick"]:
        return None
    text, media, at_only = _body(m.group(2) + ("\n" + "\n".join(cur["cont"]) if cur["cont"] else ""))
    kind = "at_only" if (at_only and not text) else ("text" if text else "unknown")
    if media and not text:
        kind = media[0]
    reply_to = ({"uid": None, "nick": parsed["reply_nick"], "text": parsed["reply_text"]}
                if parsed["reply_nick"] else None)
    return {
        "v": 1,
        "gid": None,
        "ts": cur["dt"].isoformat(timespec="seconds"),
        "ts_epoch": cur["dt"].timestamp(),
        "day": cur["day"],
        "uid": None,
        "nick": parsed["nick"],
        "card": "",
        "kind": kind,
        "media": media,
        "text": text[:4000],
        "reply_to": reply_to,
        "at": parsed["at"],
        "is_bot": False,
        "src": "botlog",
        "mid": None,
    }


def load(paths: Iterable[str]) -> List[dict]:
    records: List[dict] = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                records.extend(iter_records(fh))
        except OSError:
            continue
    return records
