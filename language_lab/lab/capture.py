# -*- coding: utf-8 -*-
"""实时抓取服务：尾随 LLBot 日志 → 结构化 JSONL → 周期刷新规范语料。

为什么需要它：机器人进程里的 recorder 需要重启才生效；而 LLBot 日志本身已经
包含「秒级时间 + 群号 + uid + 昵称 + 原文 + 媒体占位」，尾随它就能**不重启任何进程**
立刻开始结构化落盘。两路数据最终由 build 合并去重（recorder 优先）。

用法：
    python -m language_lab capture --once --init-eof   # 初始化偏移（从当前末尾开始）
    python -m language_lab capture                     # 常驻：每 5s 抓新行，每 300s 刷新规范语料
"""

from __future__ import annotations

import glob
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List

from . import settings
from .ingest import build, llbot

TZ = timezone(timedelta(hours=8))
STATE_NAME = "_capture_state.json"


def _state_path(base: Path) -> Path:
    return base / "language_lab/data/group_history/_live" / STATE_NAME


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"files": {}}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _row_of(rec: dict, cfg: dict) -> dict:
    uid = str(rec.get("uid") or "")
    bots = {str(x) for x in cfg.get("bot_uids") or []}
    return {
        "v": 1,
        "gid": rec["gid"],
        "seq": 0,
        "ts": rec["ts"],
        "ts_epoch": rec["ts_epoch"],
        "uid": uid,
        "nick": rec.get("nick") or "",
        "card": "",
        "kind": rec.get("kind") or "text",
        "text": rec.get("text") or "",
        "media": list(rec.get("media") or []),
        "reply_to": None,
        "at": list(rec.get("at") or []),
        "is_bot": uid in bots,
        "mid": None,
        "src": "capture",
        "uid_src": "log",
    }


def _append_rows(live_dir: Path, gid: str, rows: List[dict]) -> int:
    """按天追加；返回写入条数。"""
    if not rows:
        return 0
    by_day: Dict[str, List[dict]] = {}
    for row in rows:
        by_day.setdefault(str(row["ts"])[:10], []).append(row)
    written = 0
    for day, items in by_day.items():
        path = live_dir / gid / f"{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            for row in items:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                written += 1
            fh.flush()
    return written


def _read_new(path: str, offset: int) -> tuple[List[str], int]:
    """从 offset 读完整行；返回 (lines, 新 offset)。处理 UTF-8 字节偏移与半行。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset
    if size < offset:          # 文件被截断/轮转
        offset = 0
    if size == offset:
        return [], offset
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    if not data:
        return [], offset
    parts = data.split(b"\n")
    complete = parts[:-1]
    tail = parts[-1]
    lines = [p.decode("utf-8", "replace") for p in complete]
    consumed = offset + len(data) - len(tail)
    return lines, consumed


def scan_once(cfg: dict, base: Path, state: dict, init_eof: bool = False) -> int:
    """扫描一轮所有 LLBot 日志，返回新增结构化条数。"""
    gid = str(cfg["gid"])
    pattern = (cfg.get("sources") or {}).get("llbot_logs") or ""
    files = sorted(glob.glob(str(settings.resolve(pattern))), key=lambda p: (os.path.getmtime(p), p))
    live_dir = base / "language_lab/data/group_history/_live"
    seen = state.setdefault("files", {})
    total = 0
    for path in files:
        entry = seen.setdefault(path, {"offset": 0})
        if init_eof and "inited" not in entry:
            try:
                entry["offset"] = os.path.getsize(path)
            except OSError:
                entry["offset"] = 0
            entry["inited"] = True
            continue
        lines, new_offset = _read_new(path, int(entry.get("offset") or 0))
        if new_offset != entry.get("offset"):
            entry["offset"] = new_offset
        parses = (llbot.parse_line(line) for line in lines)
        rows = [_row_of(rec, cfg) for rec in parses if rec and rec["gid"] == gid]
        total += _append_rows(live_dir, gid, rows)
    return total


def refresh(cfg: dict, base: Path) -> dict:
    """重建规范语料（logs + _live 合并去重）。"""
    return build.build(cfg, base, verbose=False)


def run(cfg: dict, base: Path, once: bool = False, init_eof: bool = False,
        interval: float = 5.0, ingest_every: float = 300.0) -> int:
    state_file = _state_path(base)
    state = _load_state(state_file)
    last_ingest = 0.0
    scanned = 0
    while True:
        try:
            n = scan_once(cfg, base, state, init_eof=init_eof)
            init_eof = False
            state["updated"] = datetime.now(TZ).isoformat(timespec="seconds")
            _save_state(state_file, state)
            scanned += n
            now = time.monotonic()
            if n:
                print(f"[capture] +{n} rows (total {scanned})", flush=True)
            if once:
                summary = refresh(cfg, base)
                print(json.dumps({"captured": scanned, "canonical": summary.get("canonical")},
                                 ensure_ascii=False), flush=True)
                return 0
            if ingest_every > 0 and now - last_ingest >= ingest_every:
                last_ingest = now
                summary = refresh(cfg, base)
                print(f"[capture] refreshed canonical={summary.get('canonical')}", flush=True)
            time.sleep(max(1.0, float(interval)))
        except KeyboardInterrupt:
            print("[capture] stopped", flush=True)
            return 0
        except Exception as exc:  # 抓取服务不能死
            print(f"[capture] error: {exc}", flush=True)
            time.sleep(max(2.0, float(interval)))
