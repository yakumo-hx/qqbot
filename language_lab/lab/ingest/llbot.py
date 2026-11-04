# -*- coding: utf-8 -*-
"""LLBot 日志解析。

LLBot 的 `[收-群]` 行是我们最干净的数据源：秒级时间 + 群号 + 昵称(uid) + 原文，
并且媒体消息自带占位符（`[图片]xxx.jpg`、`[表情]`、`[合并转发]`……）。

格式（实测）：
    2026-10-28 16:18:00 [I] core [收-群] 碧油鸡乐土（转正版）（鱼塘承包）(284517232) 游云不喜欢哦(10001)：内容
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from typing import Iterable, List

TZ = timezone(timedelta(hours=8))

# 群名里允许出现中文括号；昵称里极少出现 ASCII 括号+纯数字+右括号+全角冒号。
_LINE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2}:\d{2}) "
    r"\[[A-Z]\] \S+ \[收-群\] (?P<gname>.+?)\((?P<gid>\d+)\) "
    r"(?P<nick>.+?)\((?P<uid>\d+)\)：(?P<body>.*)$"
)

# 顺序重要：先匹配长 token。
_MEDIA_TOKENS = (
    ("[合并转发]", "forward"),
    ("[动画表情]", "face"),
    ("[图片]", "image"),
    ("[表情]", "face"),
    ("[语音]", "voice"),
    ("[视频]", "video"),
    ("[文件]", "file"),
    ("[链接]", "link"),
    ("[红包]", "link"),
)

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)


def _media_of(body: str) -> tuple[str, list[str]]:
    """返回 (text, media)。

    LLBot 的媒体行形如 `[图片]ABC.jpg`（占位符 + 文件名，没有正文）。
    我们把占位符后面的 payload 去掉，只留 `[图片]`。
    """
    media: list[str] = []
    text = body
    for token, kind in _MEDIA_TOKENS:
        if token not in text:
            continue
        media.append(kind)
        if text.startswith(token):
            rest = text[len(token):]
            # 去掉紧跟的文件名/哈希，保留真正的后续文本（少见）
            m = re.match(r"^[^\s\u4e00-\u9fff]{1,120}\s*", rest)
            if m:
                rest = rest[m.end():]
            text = token + rest
        else:
            text = text.replace(token, token)
    text = text.translate(_ZERO_WIDTH).strip()
    return text, media


def iter_lines(paths: Iterable[str]):
    """逐文件逐行产出 (path, line)。"""
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    yield path, line.rstrip("\n")
        except OSError:
            continue


def parse_line(line: str) -> dict | None:
    m = _LINE_RE.match(line)
    if not m:
        return None
    date, clock = m.group("date"), m.group("time")
    try:
        dt = datetime.strptime(f"{date} {clock}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    except ValueError:
        return None
    body = m.group("body")
    text, media = _media_of(body)
    empty = not re.sub(r"\[[^\]]+\]", "", text).strip()
    kind = "unknown"
    if not empty:
        kind = "text"
    elif media:
        kind = media[0]
    return {
        "v": 1,
        "gid": m.group("gid"),
        "ts": dt.isoformat(timespec="seconds"),
        "ts_epoch": dt.timestamp(),
        "day": date,
        "uid": m.group("uid"),
        "nick": m.group("nick").strip(),
        "card": "",
        "kind": kind,
        "media": media,
        "text": text[:4000],
        "reply_to": None,
        "at": [],
        "is_bot": False,
        "src": "llbot",
        "mid": None,
    }


def load(paths: Iterable[str], gid: str) -> tuple[List[dict], int]:
    """解析全部给定日志，只保留 gid 群。返回 (records, 行数)。"""
    records: List[dict] = []
    total = 0
    for _path, line in iter_lines(paths):
        total += 1
        rec = parse_line(line)
        if rec and rec["gid"] == gid:
            records.append(rec)
    return records, total
