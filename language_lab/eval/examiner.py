# -*- coding: utf-8 -*-
"""出题人：从群聊里实时取「上下文 → 真人下一句」作为测试题。

角色：
- 回填模式：从规范语料批量生成历史题目（用于首轮基线）；
- 实时模式：尾随 `_live` 新消息，等 90 秒把「答案」连同前文一起固化成题目，
  这样连发气泡数、媒体选择都能如实记录。

题目结构（eval/data/scenarios/<gid>/<day>.jsonl）：
    id / gid / day / type / split / context[] / target / reference{bubbles,n_bubbles} / meta
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]      # E:\QQbot
import sys
sys.path.insert(0, str(BASE))

from language_lab.lab import settings, stats  # noqa: E402

TZ = timezone(timedelta(hours=8))
CLEAN_SRC = {"llbot", "recorder", "capture"}
DATA_DIR = Path(__file__).resolve().parent / "data"
SCEN_DIR = DATA_DIR / "scenarios"
STATE_PATH = DATA_DIR / "examiner_state.json"

# 每种题型每天最多留多少题，避免刷屏日淹没题库
CAPS = {"reply": 120, "mention": 80, "media_context": 120, "media": 20,
        "media_post": 20, "initiation": 40, "question": 60, "continue": 80}
MEDIA_CTX_CAP = 60          # media_context 的「参考媒体 / 参考文字」各留多少
CONTEXT_N = 8
WATCH_DELAY_S = 90
TEST_FROM_DAY = "2026-10-29"


def _now() -> datetime:
    return datetime.now(TZ)


def _plain(text: str | None) -> str:
    return stats.plain(text)


def _is_human(rec: dict) -> bool:
    return not rec.get("is_bot") and rec.get("src") in CLEAN_SRC and bool(rec.get("uid"))


def _scenario_id(rec: dict) -> str:
    raw = "|".join([str(rec.get("gid", "")), str(rec.get("ts", "")), str(rec.get("uid", "")),
                    str(rec.get("text", ""))])
    return "sc-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _is_question(text: str) -> bool:
    t = text.strip()
    return "？" in t or "?" in t or t.endswith(("吗", "呢"))


def _context_has_media(context: list[dict], window: int = 2) -> bool:
    return any(x.get("media") for x in context[-window:])


def _classify(rec: dict, prev: dict | None, reply_target: dict | None,
              context: list[dict] | None = None) -> str:
    # 媒体上下文优先：只要上文出现过图片/表情，先归到 media_context，
    # 这样「文字接图」和「媒体接图」都能进入题库，而不是被 reply/mention 吃掉。
    if context and _context_has_media(context):
        return "media_context"
    if rec.get("reply_to") and reply_target:
        return "reply"
    if rec.get("at") or "@" in (rec.get("text") or ""):
        return "mention"
    if rec.get("media") and not _plain(rec.get("text")):
        return "media_post"             # 参考本身就是媒体（不用于「媒体选择」评测）
    if prev is None or float(rec.get("ts_epoch") or 0) - float(prev.get("ts_epoch") or 0) > 300:
        return "initiation"
    if _is_question(rec.get("text") or ""):
        return "question"
    return "continue"


def _ctx_item(rec: dict) -> dict:
    return {"ts": rec.get("ts"), "uid": str(rec.get("uid") or ""), "nick": rec.get("nick") or "",
            "kind": rec.get("kind") or "text", "text": rec.get("text") or "",
            "media": list(rec.get("media") or [])}


def _reference(rows: list[dict], idx: int) -> dict:
    """把同一个人 60 秒内的连续消息算作一组气泡。"""
    first = rows[idx]
    bubbles = [_ctx_item(first)]
    uid = str(first.get("uid"))
    for nxt in rows[idx + 1: idx + 4]:
        if str(nxt.get("uid")) != uid:
            break
        if float(nxt.get("ts_epoch") or 0) - float(first.get("ts_epoch") or 0) > 60:
            break
        bubbles.append(_ctx_item(nxt))
    return {"bubbles": bubbles, "n_bubbles": len(bubbles)}


def make_scenario(rows: list[dict], idx: int, context: list[dict], ref: dict | None = None,
                  id_suffix: str = "") -> dict:
    rec = rows[idx]
    day = str(rec.get("day") or (rec.get("ts") or "")[:10] or "")
    prev = context[-1] if context else None
    target = None
    reply = rec.get("reply_to") or {}
    if reply:
        want_uid = str(reply.get("uid") or "")
        want_nick = reply.get("nick") or ""
        for item in reversed(context):
            if want_uid and str(item.get("uid")) == want_uid:
                target = _ctx_item(item)
                break
            if want_nick and item.get("nick") == want_nick:
                target = _ctx_item(item)
                break
    stype = _classify(rec, prev, target, context)
    ref = ref or _reference(rows, idx)
    return {
        "v": 1,
        "id": _scenario_id(rec) + id_suffix,
        "gid": str(rec.get("gid")),
        "day": day,
        "created_at": _now().isoformat(timespec="seconds"),
        "type": stype,
        "split": "test" if day >= TEST_FROM_DAY else "train",
        "context": [_ctx_item(x) for x in context[-CONTEXT_N:]],
        "target": target,
        "reference": ref,
        "meta": {
            "gap_s": round(float(rec.get("ts_epoch") or 0) - float(prev.get("ts_epoch") or 0), 1) if prev else None,
            "has_media": bool(rec.get("media")),
            "context_has_media": _context_has_media(context),
            "is_question": _is_question(rec.get("text") or ""),
        },
    }


def _scenario_path(day: str, gid: str) -> Path:
    return SCEN_DIR / str(gid) / f"{day}.jsonl"


def _day_state(day: str, gid: str) -> Counter:
    path = _scenario_path(day, gid)
    counts: Counter = Counter()
    if not path.exists():
        return counts
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                counts[json.loads(line)["type"]] += 1
            except (ValueError, KeyError):
                continue
    return counts


def _ref_is_media(sc: dict) -> bool:
    """只看参考消息本身（第一气泡），不看同人后续连发。"""
    bubbles = (sc.get("reference") or {}).get("bubbles") or []
    if not bubbles:
        return False
    first = bubbles[0]
    return bool(first.get("media")) and not _plain(first.get("text"))


def _media_ctx_counts(day: str, gid: str) -> tuple[int, int]:
    """(参考媒体数, 参考文字数)，用于 media_context 两类的限额。"""
    media = text = 0
    path = _scenario_path(day, gid)
    if not path.exists():
        return 0, 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                sc = json.loads(line)
            except ValueError:
                continue
            if sc.get("type") != "media_context":
                continue
            if _ref_is_media(sc):
                media += 1
            else:
                text += 1
    return media, text


def _append_scenario(sc: dict, seen: set[str], respect_caps: bool = True) -> bool:
    if sc["id"] in seen:
        return False
    if respect_caps:
        counts = _day_state(sc["day"], sc["gid"])
        if counts[sc["type"]] >= CAPS.get(sc["type"], 80):
            return False
        if sc["type"] == "media_context":
            media_n, text_n = _media_ctx_counts(sc["day"], sc["gid"])
            if (_ref_is_media(sc) and media_n >= MEDIA_CTX_CAP) or \
               (not _ref_is_media(sc) and text_n >= MEDIA_CTX_CAP):
                return False
    path = _scenario_path(sc["day"], sc["gid"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(sc, ensure_ascii=False) + "\n")
    seen.add(sc["id"])
    return True


def _existing_ids() -> set[str]:
    ids: set[str] = set()
    for path in SCEN_DIR.glob("*/"):
        for f in path.glob("*.jsonl"):
            with f.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        ids.add(json.loads(line)["id"])
                    except (ValueError, KeyError):
                        continue
    return ids


def backfill(cfg: dict, verbose: bool = True) -> dict:
    gid = str(cfg["gid"])
    seen = _existing_ids()
    rows: list[dict] = []
    for path in sorted((settings.GROUP_HISTORY_DIR / gid).glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if _is_human(rec) and (rec.get("day") or "") >= "2026-10-24":
                    rows.append(rec)
    rows.sort(key=lambda r: float(r.get("ts_epoch") or 0))
    added = Counter()
    for i, rec in enumerate(rows):
        context = [x for x in rows[max(0, i - CONTEXT_N):i] if not x.get("is_bot")]
        sc = make_scenario(rows, i, context)
        if sc["type"] == "media_context":
            sc["id"] += "-m"      # 与同一条消息的其它题型共存
        if _append_scenario(sc, seen):
            added[sc["type"]] += 1
    summary = {"rows": len(rows), "added": dict(added), "total": sum(added.values())}
    if verbose:
        print(json.dumps(summary, ensure_ascii=False))
    return summary


def _load_buffer(gid: str, limit: int = 50) -> list[dict]:
    rows: list[dict] = []
    files = sorted((settings.GROUP_HISTORY_DIR / gid).glob("*.jsonl"))
    for path in files[-2:]:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if _is_human(rec):
                    rows.append(rec)
    return rows[-limit:]


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"files": {}}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _read_new(path: Path, offset: int) -> tuple[list[str], int]:
    try:
        size = path.stat().st_size
    except OSError:
        return [], offset
    if size < offset:
        offset = 0
    if size == offset:
        return [], offset
    with path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read()
    parts = data.split(b"\n")
    lines = [p.decode("utf-8", "replace") for p in parts[:-1]]
    return lines, offset + len(data) - len(parts[-1])


def watch(cfg: dict, interval: float = 5.0, verbose: bool = True, from_start: bool = False) -> int:
    gid = str(cfg["gid"])
    live_dir = settings.LIVE_DIR / gid
    fresh = not STATE_PATH.exists()
    state = _load_state()
    if fresh and not from_start:
        # 首次启动只从「当前末尾」开始，避免把历史消息重复出一遍题（历史用 --backfill）
        for path in sorted(live_dir.glob("*.jsonl")):
            try:
                state.setdefault("files", {})[str(path)] = {"offset": path.stat().st_size}
            except OSError:
                pass
        _save_state(state)
    if from_start:
        state = {"files": {}}
    seen = _existing_ids()
    buffer = [] if from_start else _load_buffer(gid)
    pending: list[dict] = []
    while True:
        try:
            files = sorted(live_dir.glob("*.jsonl"))
            new_rows: list[dict] = []
            for path in files:
                entry = state.setdefault("files", {}).setdefault(str(path), {"offset": 0})
                lines, new_offset = _read_new(path, int(entry.get("offset") or 0))
                entry["offset"] = new_offset
                for line in lines:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("gid") != gid or not _is_human(rec):
                        continue
                    if buffer and str(buffer[-1].get("uid")) == str(rec.get("uid")) \
                            and buffer[-1].get("text") == rec.get("text") \
                            and abs(float(buffer[-1].get("ts_epoch") or 0) - float(rec.get("ts_epoch") or 0)) <= 2:
                        continue
                    ctx = list(buffer[-CONTEXT_N:])
                    buffer.append(rec)
                    pending.append({"rec": rec, "context": ctx, "at": float(rec.get("ts_epoch") or 0)})
            # 90 秒后固化：这时同一人的连发已进 buffer
            now = time.time()
            keep = []
            for item in pending:
                if now - item["at"] < WATCH_DELAY_S:
                    keep.append(item)
                    continue
                rec = item["rec"]
                ref_bubbles = [_ctx_item(rec)]
                for nxt in buffer:
                    if float(nxt.get("ts_epoch") or 0) <= float(rec.get("ts_epoch") or 0):
                        continue
                    if str(nxt.get("uid")) != str(rec.get("uid")):
                        break
                    if float(nxt.get("ts_epoch") or 0) - float(rec.get("ts_epoch") or 0) > 60:
                        break
                    ref_bubbles.append(_ctx_item(nxt))
                    if len(ref_bubbles) >= 3:
                        break
                sc = make_scenario([rec], 0, item["context"], {"bubbles": ref_bubbles, "n_bubbles": len(ref_bubbles)})
                if sc["type"] == "media_context":
                    sc["id"] += "-m"
                if _append_scenario(sc, seen, respect_caps=False) and verbose:
                    print(f"[examiner] +{sc['type']} {rec.get('nick')} {str(rec.get('text'))[:30]}", flush=True)
            pending = keep
            state["updated"] = _now().isoformat(timespec="seconds")
            _save_state(state)
            time.sleep(max(1.0, interval))
        except KeyboardInterrupt:
            print("[examiner] stopped", flush=True)
            return 0
        except Exception as exc:
            print(f"[examiner] error: {exc}", flush=True)
            time.sleep(max(2.0, interval))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="language_lab examiner", description="出题人：群聊 → 测试题")
    parser.add_argument("--backfill", action="store_true", help="从规范语料批量生成历史题目")
    parser.add_argument("--watch", action="store_true", help="实时尾随 _live 生成题目")
    parser.add_argument("--from-start", action="store_true", help="实时模式也从文件开头读（默认只读新消息）")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--gid")
    args = parser.parse_args(argv)
    cfg = settings.load_config()
    if args.gid:
        cfg["gid"] = str(args.gid)
    if args.backfill:
        backfill(cfg)
    if args.watch:
        print(json.dumps({"examiner": "watch", "gid": cfg["gid"]}, ensure_ascii=False), flush=True)
        return watch(cfg, args.interval, from_start=args.from_start)
    if not args.backfill and not args.watch:
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
