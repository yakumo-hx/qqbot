# -*- coding: utf-8 -*-
"""为盲测提供「可变长度、带媒体信息」的上下文。

- 上下文长度由会话边界（时间间隔）+ 话题连续性（字符 bigram 相似度）共同决定，6~30 条不定长；
- 媒体消息尽量补上机器人识图描述（来自 logs 的 `[识图] 成功：文件名 → 描述`），
  没有描述就退回 `[图片]` / `[表情]` 占位。
"""

from __future__ import annotations

import glob
import json
import re
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0, str(BASE))

from language_lab.lab import settings, stats  # noqa: E402

_LLBOT_LINE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<clock>\d{2}:\d{2}:\d{2}).*?\[收-群\] "
    r".*?\((?P<gid>\d+)\) .*?\((?P<uid>\d+)\)：(?P<body>.*)$"
)
_MEDIA = re.compile(r"\[(图片|表情|动画表情|合并转发|视频|文件|语音|链接|红包)\]([^\s\[]*)")
_DESC = re.compile(r"\[识图\] 成功：(.+?) → (.+)$")
_KIND_ZH = {"image": "图片", "face": "表情", "sticker": "贴图", "forward": "合并转发",
            "video": "视频", "file": "文件", "voice": "语音", "link": "链接",
            "redpacket": "红包", "unknown": "媒体"}


def load_corpus(gid: str) -> list[dict]:
    rows: list[dict] = []
    for path in sorted((settings.GROUP_HISTORY_DIR / str(gid)).glob("*.jsonl")):
        for line in path.open("r", encoding="utf-8", errors="replace"):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("ts_epoch") is not None:
                rows.append(rec)
    rows.sort(key=lambda r: float(r.get("ts_epoch") or 0))
    return rows


def build_media_index(gid: str) -> dict[tuple[str, int], list[dict]]:
    """(uid, ts_epoch) -> [{"kind": "图片", "file": "...", "desc": "..."}]"""
    descs: dict[str, str] = {}
    for f in glob.glob(str(BASE / "logs" / "bot.2026-*.log")):
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = _DESC.search(line)
                    if m:
                        descs[m.group(1).strip()] = m.group(2).strip()
        except OSError:
            continue

    index: dict[tuple[str, int], list[dict]] = {}
    llbot_dir = Path(r"E:\QQbot-trials\llbot\bin\llbot\data\logs")
    for f in sorted(glob.glob(str(llbot_dir / "llbot-*.log"))):
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "[收-群]" not in line:
                        continue
                    m = _LLBOT_LINE.match(line.rstrip("\n"))
                    if not m or m.group("gid") != str(gid):
                        continue
                    try:
                        epoch = int(datetime.strptime(
                            f"{m.group('date')} {m.group('clock')}", "%Y-%m-%d %H:%M:%S").timestamp())
                    except ValueError:
                        continue
                    body = m.group("body")
                    items = []
                    for kind, fname in _MEDIA.findall(body):
                        kind = "表情" if kind == "动画表情" else kind
                        items.append({"kind": kind, "file": fname,
                                      "desc": descs.get(fname, "") if fname else ""})
                    if items:
                        index[(m.group("uid"), epoch)] = items
        except OSError:
            continue
    return index


def find_ref_index(rows: list[dict], ref_bubbles: list[dict]) -> int | None:
    if not ref_bubbles:
        return None
    first = ref_bubbles[0]
    ts = first.get("ts")
    uid = str(first.get("uid") or "")
    text = stats.plain(first.get("text"))
    if not ts:
        return None
    try:
        epoch = datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None
    best = None
    for i, r in enumerate(rows):
        if uid and str(r.get("uid")) != uid:
            continue
        if abs(float(r.get("ts_epoch") or 0) - epoch) <= 2:
            if not text or stats.plain(r.get("text")) == text:
                best = i
                break
            best = best if best is not None else i
    return best


def _bigrams(text: str) -> set[str]:
    t = stats.plain(text)
    return {t[i:i + 2] for i in range(max(0, len(t) - 1))}


def _sim(a: str, b: str) -> float:
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def semantic_context(rows: list[dict], idx: int, min_n: int = 10, max_n: int = 30,
                     gap_s: int = 900, sim_threshold: float = 0.005,
                     low_need: int = 8) -> list[dict]:
    """从 idx 往前取上下文，直到会话边界或话题断裂；长度 6~30 条不固定。

    - 会话边界：相邻消息间隔超过 gap_s；
    - 话题断裂：与后一条的字符 bigram 相似度连续 low_need 次低于阈值；
    - 至少 min_n 条，最多 max_n 条。
    """
    picked: list[dict] = []
    prev_ts = float(rows[idx].get("ts_epoch") or 0)
    prev_text = rows[idx].get("text") or ""
    low_streak = 0
    for j in range(idx - 1, -1, -1):
        r = rows[j]
        ts = float(r.get("ts_epoch") or 0)
        gap = prev_ts - ts
        if gap > gap_s and len(picked) >= min_n:
            break
        picked.append(r)
        if len(picked) >= min_n:
            sim = _sim(r.get("text") or "", prev_text)
            if sim < sim_threshold:
                low_streak += 1
                if low_streak >= low_need:
                    break
            else:
                low_streak = 0
        prev_ts, prev_text = ts, r.get("text") or ""
        if len(picked) >= max_n:
            break
    picked.reverse()
    return picked


def media_text(row: dict, media_index: dict, max_desc: int = 50) -> str:
    """把一条消息渲染成「文字 + 媒体描述」。"""
    raw = (row.get("text") or "").strip()
    text = re.sub(r"\[[^\]]{1,12}\]", "", raw).strip()
    media = row.get("media") or []
    if not media:
        return text or raw
    key = (str(row.get("uid") or ""), int(float(row.get("ts_epoch") or 0)))
    items = media_index.get(key) or []
    parts = []
    for i, kind in enumerate(media):
        zh = _KIND_ZH.get(str(kind), str(kind))
        desc = ""
        if i < len(items):
            desc = items[i].get("desc") or ""
        if desc:
            if len(desc) > max_desc:
                desc = desc[:max_desc] + "…"
            parts.append(f"[{zh}：{desc}]")
        else:
            parts.append(f"[{zh}]")
    return (text + " " + " ".join(parts)).strip() if text else " ".join(parts)
