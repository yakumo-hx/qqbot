# -*- coding: utf-8 -*-
"""把 LLBot 日志、机器人日志、recorder 实时数据合并成规范语料。

输出：`language_lab/data/group_history/<gid>/YYYY-MM-DD.jsonl`（一行一条消息）。

合并纪律（踩过的坑）：
- LLBot 是权威源：有 uid、有媒体占位符、秒级时间，且覆盖期内全量。
- 机器人日志覆盖 10-20 起，但没有 uid，且**昵称可能张冠李戴**（同一昵称在不同时期
  属于不同 uid）。因此：
    * LLBot 已覆盖的时段，只用机器人日志**补回复关系**，匹配不上就丢弃（多半是媒体
      消息，机器人日志本来就不记）；
    * LLBot 未覆盖的时段（10-20 ~ 10-24 20:38），机器人日志才是唯一来源，uid 由名册
      反查，标记 `uid_src="roster"`。
- recorder（`_live/`）优先级最高，字段最全。
"""

from __future__ import annotations

import glob
import json
import os
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, Iterable, List

from . import botlog, llbot, roster

_PLACEHOLDER_RE = re.compile(r"\[[^\]]{1,12}\]")
_AT_RE = re.compile(r"@[^\s@]{1,40}\s*")
_WS_RE = re.compile(r"\s+")
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)

_SRC_PRIORITY = {"recorder": 3, "llbot": 2, "botlog": 1}

_EDGE_PUNCT = "。！？!?…~～,，、;；:：\"'“”‘’()（）[]【】 \t"


class IngestGidMismatch(RuntimeError):
    """botlog 行的实际群号与 --gid 不一致；必须中止 ingest，绝不盖章。"""


def stamp_botlog(gid: str, records: List[dict], declared_gid: str | None) -> tuple[List[dict], int]:
    """1.5 护栏：给 botlog 行盖群号前先验证归属。

    规则（用户 2026-11-03）：
    - 行里**有**实际群号且 != gid → 立刻抛 IngestGidMismatch（报错退出，不写任何文件）；
    - 行里**没有**群号：只有在本语料显式声明 `corpus.botlog_gid == gid` 时才允许盖章；
      否则拒绝盖章（跳过并计数）——这正是 1055881417 把 A 群 12,900 行 botlog 误盖成 B 的成因。
    """
    kept: List[dict] = []
    skipped = 0
    declared = str(declared_gid or "").strip()
    for rec in records:
        actual = rec.get("gid")
        if actual is not None:
            if str(actual) != str(gid):
                raise IngestGidMismatch(
                    f"ingest aborted: botlog 行群号 {actual!r} 与目标 --gid {gid!r} 不符；"
                    f"拒绝写入（行 ts={rec.get('ts')!r}）")
            rec["gid"] = gid
            kept.append(rec)
            continue
        if declared and declared == str(gid):
            rec["gid"] = gid
            kept.append(rec)
        else:
            skipped += 1
    return kept, skipped


def norm_text(text: str | None) -> str:
    t = (text or "").translate(_ZERO_WIDTH)
    t = _PLACEHOLDER_RE.sub("", t)
    t = _AT_RE.sub("", t)
    return _WS_RE.sub("", t).strip(_EDGE_PUNCT)


def text_match(a: str | None, b: str | None) -> bool:
    na, nb = norm_text(a), norm_text(b)
    if not na and not nb:
        return True
    if not na or not nb:
        return False
    if na == nb:
        return True
    if min(len(na), len(nb)) >= 6 and (na.startswith(nb) or nb.startswith(na)):
        return True
    return SequenceMatcher(None, na[:40], nb[:40]).ratio() >= 0.8


def _match_key(rec: dict) -> str:
    return str(rec.get("uid") or ("nick:" + (rec.get("nick") or "")))


class _KeyIndex:
    """按 (uid 或昵称) + 时间排序，用于 recorder ↔ LLBot 的精确对齐。"""

    def __init__(self, records: Iterable[dict]):
        self.by_key: Dict[str, List[dict]] = defaultdict(list)
        for rec in records:
            self.by_key[_match_key(rec)].append(rec)
        for items in self.by_key.values():
            items.sort(key=lambda r: r.get("ts_epoch") or 0)

    def take(self, rec: dict, sec: float) -> dict | None:
        items = self.by_key.get(_match_key(rec))
        if not items:
            return None
        ts = float(rec.get("ts_epoch") or 0)
        best, best_dt = None, sec + 1
        for item in items:
            dt = abs(float(item.get("ts_epoch") or 0) - ts)
            if dt > sec:
                continue
            if text_match(rec.get("text"), item.get("text")) and dt < best_dt:
                best, best_dt = item, dt
        if best is not None:
            items.remove(best)
        return best


class _TimeIndex:
    """按秒分桶的时间索引，用于机器人日志 ↔ 已合并语料的跨昵称对齐。"""

    def __init__(self, records: Iterable[dict]):
        self.buckets: Dict[int, List[dict]] = defaultdict(list)
        for rec in records:
            self.add(rec)

    def add(self, rec: dict) -> None:
        try:
            bucket = int(float(rec.get("ts_epoch") or 0))
        except (TypeError, ValueError):
            return
        self.buckets[bucket].append(rec)

    def take(self, rec: dict, sec: float) -> dict | None:
        try:
            ts = int(float(rec.get("ts_epoch") or 0))
        except (TypeError, ValueError):
            return None
        nt = norm_text(rec.get("text"))
        best, best_score = None, -1
        for bucket in range(ts - int(sec), ts + int(sec) + 1):
            for item in list(self.buckets.get(bucket, ())):
                ni = norm_text(item.get("text"))
                if not text_match(rec.get("text"), item.get("text")):
                    continue
                if not nt and not ni:
                    if not (rec.get("uid") and rec.get("uid") == item.get("uid")):
                        continue
                elif min(len(nt), len(ni)) < 4:
                    if not ((rec.get("uid") and rec.get("uid") == item.get("uid"))
                            or (rec.get("nick") and rec.get("nick") == item.get("nick"))):
                        continue
                score = 2 if nt == ni else 1
                if score > best_score:
                    best, best_score = item, score
        if best is not None:
            self.buckets[int(float(best["ts_epoch"]))].remove(best)
        return best


def _dedupe_live(records: List[dict]) -> List[dict]:
    """recorder / capture 两路实时数据可能重复：同 uid、±5s、文本相同视为同一条，
    保留字段更全的一路（recorder > capture）。"""
    out: List[dict] = []
    for rec in sorted(records, key=lambda r: float(r.get("ts_epoch") or 0)):
        ts = float(rec.get("ts_epoch") or 0)
        dup = False
        for i in range(len(out) - 1, max(-1, len(out) - 80), -1):
            prev = out[i]
            if abs(float(prev.get("ts_epoch") or 0) - ts) > 5:
                continue
            if str(prev.get("uid") or prev.get("nick")) != str(rec.get("uid") or rec.get("nick")):
                continue
            if not text_match(prev.get("text"), rec.get("text")):
                continue
            if _SRC_PRIORITY.get(str(rec.get("src") or ""), 0) > _SRC_PRIORITY.get(str(prev.get("src") or ""), 0):
                out[i] = rec
            dup = True
            break
        if not dup:
            out.append(rec)
    return out


def _load_live(gid: str, live_dir: Path) -> List[dict]:
    records: List[dict] = []
    for path in sorted(glob.glob(str(live_dir / gid / "*.jsonl"))):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("gid") != gid or rec.get("ts_epoch") is None:
                        continue
                    rec.setdefault("src", "recorder")
                    rec.setdefault("uid_src", "bot")
                    rec["is_bot"] = bool(rec.get("is_bot"))
                    records.append(rec)
        except OSError:
            continue
    return records


def _enrich(dst: dict, src: dict) -> None:
    if not dst.get("reply_to") and src.get("reply_to"):
        dst["reply_to"] = src["reply_to"]
    if not dst.get("at") and src.get("at"):
        dst["at"] = src["at"]
    if not dst.get("uid") and src.get("uid"):
        dst["uid"] = src["uid"]
    if (dst.get("kind") in (None, "unknown")) and src.get("text"):
        dst["kind"] = "text"
        dst["text"] = src["text"]


def merge(gid: str, llbot_recs: List[dict], botlog_recs: List[dict],
          live_recs: List[dict], dedupe_sec: float = 5.0) -> List[dict]:
    canon: List[dict] = []

    # 1) recorder 优先：与 LLBot 同一条时用 recorder 的字段
    llbot_index = _KeyIndex(llbot_recs)
    for rec in live_recs:
        base = llbot_index.take(rec, dedupe_sec)
        if base is not None:
            _enrich(rec, dict(base))
        canon.append(rec)

    # 2) 剩余 LLBot
    for items in llbot_index.by_key.values():
        canon.extend(items)

    # 3) 机器人日志：覆盖期内只补关系；覆盖期外才新增
    coverage: Dict[str, float] = {}
    for rec in llbot_recs:
        day = rec.get("day") or ""
        ts = float(rec.get("ts_epoch") or 0)
        if day and (day not in coverage or ts < coverage[day]):
            coverage[day] = ts

    time_index = _TimeIndex(canon)
    for rec in botlog_recs:
        hit = time_index.take(rec, dedupe_sec)
        if hit is not None:
            _enrich(hit, rec)
            continue
        day = rec.get("day") or ""
        cov = coverage.get(day)
        if cov is not None and float(rec.get("ts_epoch") or 0) >= cov - 1:
            continue  # 覆盖期内匹配不上 → 视为媒体消息/错配，丢弃
        rec["uid_src"] = "roster"
        canon.append(rec)
        time_index.add(rec)

    # 4) 排序 + 序号
    canon.sort(key=lambda r: (float(r.get("ts_epoch") or 0),
                              _SRC_PRIORITY.get(r.get("src") or "", 0)))
    seq_by_day: Counter = Counter()
    for rec in canon:
        day = (rec.get("ts") or "")[:10]
        rec["day"] = day
        seq_by_day[day] += 1
        rec["seq"] = seq_by_day[day]
        rec["gid"] = gid
        rec["is_bot"] = bool(rec.get("is_bot"))
        rec["v"] = 1
        rec.setdefault("uid_src", "log")
    return canon


def _write_day(path: Path, records: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def build(cfg: dict, base_dir: Path, verbose: bool = True) -> dict:
    gid = str(cfg["gid"])
    corpus_cfg = cfg.get("corpus") or {}
    sources = cfg.get("sources") or {}
    bot_uids = {str(x) for x in cfg.get("bot_uids") or []}
    bot_names = {str(x) for x in cfg.get("bot_names") or []}
    start = corpus_cfg.get("start_day")
    end = corpus_cfg.get("end_day")
    dedupe = float(corpus_cfg.get("dedupe_seconds") or 5)

    bot_paths = sorted(glob.glob(str(base_dir / (sources.get("bot_logs") or "logs/bot.2026-*.log"))))
    llbot_paths = sorted(glob.glob(str(base_dir / sources["llbot_logs"]))) if sources.get("llbot_logs") else []
    live_dir = base_dir / (sources.get("live_dir") or "language_lab/data/group_history/_live")
    roster_path = base_dir / (sources.get("roster") or "data/roster.json")

    ros = roster.load(roster_path, gid)

    llbot_recs, llbot_lines = llbot.load(llbot_paths, gid)
    botlog_gid = str(corpus_cfg.get("botlog_gid") or "").strip()
    botlog_recs, botlog_skipped = stamp_botlog(gid, botlog.load(bot_paths), botlog_gid)
    for rec in botlog_recs:
        if rec.get("uid") is None:
            rec["uid"] = ros.resolve(rec.get("nick") or "")
    live_recs = _load_live(gid, live_dir)
    live_recs = _dedupe_live(live_recs)

    def in_window(rec: dict) -> bool:
        day = (rec.get("ts") or "")[:10]
        if start and day < start:
            return False
        if end and day > end:
            return False
        return True

    llbot_recs = [r for r in llbot_recs if in_window(r)]
    botlog_recs = [r for r in botlog_recs if in_window(r)]
    live_recs = [r for r in live_recs if in_window(r)]

    canon = merge(gid, llbot_recs, botlog_recs, live_recs, dedupe)

    for rec in canon:
        uid = str(rec.get("uid") or "")
        if uid in bot_uids or (rec.get("nick") in bot_names):
            rec["is_bot"] = True

    by_day: Dict[str, List[dict]] = defaultdict(list)
    for rec in canon:
        day = rec.get("day") or (rec.get("ts") or "")[:10]
        if day:
            by_day[day].append(rec)

    out_dir = base_dir / (corpus_cfg.get("dir") or "language_lab/data/group_history") / gid
    for day, recs in sorted(by_day.items()):
        _write_day(out_dir / f"{day}.jsonl", recs)

    summary = {
        "gid": gid,
        "llbot_lines": llbot_lines,
        "llbot_records": len(llbot_recs),
        "botlog_records": len(botlog_recs),
        "botlog_skipped_no_gid": botlog_skipped,
        "botlog_gid": botlog_gid or None,
        "live_records": len(live_recs),
        "canonical": len(canon),
        "days": {d: len(v) for d, v in sorted(by_day.items())},
        "src": dict(Counter(r.get("src") for r in canon)),
        "uid_src": dict(Counter(r.get("uid_src") for r in canon)),
        "bots": sum(1 for r in canon if r.get("is_bot")),
        "uid_missing": sum(1 for r in canon if not r.get("uid")),
        "out_dir": str(out_dir),
        "roster_size": ros.size(),
    }
    (out_dir / "_meta.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if verbose:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary
