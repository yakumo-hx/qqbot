# -*- coding: utf-8 -*-
"""语料健康检查：给定时任务用，每次产出 latest.json / latest.md / history.jsonl。

检查项：
1. 今日规范语料条数 vs LLBot 今日实收条数（缺失/重复）
2. JSON 解析错误
3. 最新消息时间（活跃时段内太久没新数据 = 异常）
4. 实时落盘文件新鲜度（capture / recorder）
5. recorder 是否生效
6. uid 缺失比例、重复率
7. LLBot / 机器人日志新鲜度（协议层是否在线）

退出码：0 = ok/warn，1 = fail（方便计划任务/监控判断）。
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
ACTIVE_START, ACTIVE_END = 8, 24          # 活跃时段（小时）
MSG_STALE_MIN = 180                       # 活跃时段 3 小时无新消息 → warn
LLBOT_STALE_MIN = 30                      # LLBot 日志 30 分钟不更新 → fail
LIVE_STALE_MIN = 60                       # 实时落盘 60 分钟不更新 → warn
DUP_WARN_RATIO = 0.05
UID_MISS_WARN_RATIO = 0.05


def _now() -> datetime:
    return datetime.now(TZ)


def _age_min(mtime: float) -> float:
    return (time.time() - mtime) / 60.0


def _load_jsonl(path: Path) -> tuple[list[dict], int]:
    rows, bad = [], 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    bad += 1
    except OSError:
        pass
    return rows, bad


def _latest_row(corpus_dir: Path) -> tuple[dict | None, str]:
    files = sorted(glob.glob(str(corpus_dir / "*.jsonl")))
    for path in reversed(files):
        rows, _ = _load_jsonl(Path(path))
        if rows:
            return rows[-1], Path(path).stem
    return None, ""


def _count_llbot_today(pattern: str, gid: str, today: str) -> int:
    from language_lab.lab.ingest import llbot
    total = 0
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "[收-群]" not in line or f"({gid})" not in line:
                        continue
                    rec = llbot.parse_line(line.rstrip("\n"))
                    if rec and rec["gid"] == gid and rec["day"] == today:
                        total += 1
        except OSError:
            continue
    return total


def _latest_llbot(pattern: str, gid: str, today: str) -> str | None:
    from language_lab.lab.ingest import llbot
    latest: str | None = None
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "[收-群]" not in line or f"({gid})" not in line:
                        continue
                    rec = llbot.parse_line(line.rstrip("\n"))
                    if rec and rec["gid"] == gid and rec["day"] == today:
                        if latest is None or rec["ts"] > latest:
                            latest = rec["ts"]
        except OSError:
            continue
    return latest


def _dup_count(rows: list[dict]) -> int:
    seen: dict = {}
    dup = 0
    for rec in rows:
        text = (rec.get("text") or "").strip()
        # 纯占位符（[图片]/[表情]…）不算重复：连续发两张图是正常行为
        if not re.sub(r"\[[^\]]{1,12}\]", "", text).strip():
            continue
        if len(text) < 2:
            continue
        key = (str(rec.get("uid") or ""), text)
        ts = float(rec.get("ts_epoch") or 0)
        prev = seen.get(key)
        if prev is not None and abs(prev - ts) <= 5:
            dup += 1
        else:
            seen[key] = ts
    return dup


def run() -> int:
    base = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(base))
    from language_lab.lab import settings

    cfg = settings.load_config()
    gid = str(cfg["gid"])
    corpus_dir = base / (cfg.get("corpus") or {}).get("dir", "language_lab/data/group_history") / gid
    live_dir = base / "language_lab/data/group_history/_live" / gid
    reports = base / "language_lab/reports/health"
    reports.mkdir(parents=True, exist_ok=True)

    now = _now()
    today = now.strftime("%Y-%m-%d")
    active = ACTIVE_START <= now.hour < ACTIVE_END
    checks: list[dict] = []
    fail = warn = False

    def add(name: str, status: str, detail: str) -> None:
        nonlocal fail, warn
        checks.append({"name": name, "status": status, "detail": detail})
        if status == "fail":
            fail = True
        elif status == "warn":
            warn = True

    # 先把 logs + _live 合并进规范库，避免“实时已落盘但规范库还没刷新”被误判
    try:
        from language_lab.lab.ingest import build as _build
        _summary = _build.build(cfg, base, verbose=False)
        add("规范库刷新", "ok", f"合并后 {_summary.get('canonical')} 条（live {_summary.get('live_records')} 条）")
    except Exception as exc:
        add("规范库刷新", "fail", f"重建失败：{type(exc).__name__}: {exc}")

    # 今日语料
    day_path = corpus_dir / f"{today}.jsonl"
    rows, bad = _load_jsonl(day_path)
    llbot_pattern = str(settings.resolve((cfg.get("sources") or {}).get("llbot_logs") or ""))
    llbot_today = _count_llbot_today(llbot_pattern, gid, today)
    llbot_latest = _latest_llbot(llbot_pattern, gid, today)
    llbot_latest_age = (
        (now - datetime.fromisoformat(llbot_latest)).total_seconds() / 60.0
        if llbot_latest else 99999
    )
    add("今日语料条数", "ok", f"{len(rows)} 条（LLBot 实收 {llbot_today} 条）")
    if llbot_today and len(rows) < llbot_today:
        add("数据完整性", "fail", f"语料 {len(rows)} < LLBot 实收 {llbot_today}，缺 {llbot_today - len(rows)} 条")
    elif llbot_today and len(rows) > llbot_today * 1.10:
        add("数据完整性", "warn", f"语料 {len(rows)} 明显多于 LLBot {llbot_today}，疑似重复")
    else:
        add("数据完整性", "ok", "与 LLBot 实收一致")

    # JSON 解析
    if bad:
        add("JSON 解析", "fail", f"今日文件有 {bad} 行无法解析")
    else:
        add("JSON 解析", "ok", "无坏行")

    # 最新消息
    latest, latest_day = _latest_row(corpus_dir)
    if latest:
        age = (now - datetime.fromisoformat(latest["ts"])).total_seconds() / 60.0
        if age > MSG_STALE_MIN and active:
            add("最新消息", "warn", f"已 {age:.0f} 分钟无新消息（{latest.get('nick') or latest.get('uid')} @ {latest['ts'][11:19]}）")
        else:
            add("最新消息", "ok", f"{age:.0f} 分钟前（{latest['ts'][11:19]}）")
        if latest["ts"][:10] < (now - timedelta(days=1)).strftime("%Y-%m-%d"):
            add("语料日期", "warn", f"最新数据停在 {latest['ts'][:10]}")
    else:
        age = 99999
        add("最新消息", "fail", "语料为空")

    # 实时落盘
    live_files = glob.glob(str(live_dir / "*.jsonl"))
    live_rows, live_bad = [], 0
    for path in live_files:
        part, b = _load_jsonl(Path(path))
        live_rows.extend(part)
        live_bad += b
    if live_files:
        live_age = min(_age_min(os.path.getmtime(p)) for p in live_files)
        if live_age > LIVE_STALE_MIN and active:
            if llbot_latest_age > LIVE_STALE_MIN:
                add("实时落盘", "info", f"群内 {llbot_latest_age:.0f} 分钟无新消息，实时文件未更新属正常")
            else:
                add("实时落盘", "warn", f"实时文件落后（LLBot 最新消息 {llbot_latest_age:.0f} 分钟前）")
        else:
            add("实时落盘", "ok", f"最近更新 {live_age:.0f} 分钟前，累计 {len(live_rows)} 行")
    else:
        add("实时落盘", "warn", "今日没有 _live 文件（capture/recorder 均未落盘）")
    if live_bad:
        add("实时JSON", "fail", f"_live 有 {live_bad} 行无法解析")

    # LLBot 新鲜度（心跳是 7x24 的，不能用「活跃时段」豁免）
    llbot_dir = Path(r"E:\QQbot-trials\llbot\bin\llbot\data\logs")
    llbot_logs = sorted(glob.glob(str(llbot_dir / "llbot-*.log")))
    llbot_age = None
    if llbot_logs:
        llbot_age = _age_min(max(os.path.getmtime(p) for p in llbot_logs))
        if llbot_age > LLBOT_STALE_MIN:
            add("LLBot 在线", "fail", f"LLBot 日志 {llbot_age:.0f} 分钟未更新，进程可能已退出/掉线")
        else:
            add("LLBot 在线", "ok", f"日志 {llbot_age:.0f} 分钟前更新")
    else:
        add("LLBot 在线", "warn", "找不到 LLBot 日志")
    llbot_offline = llbot_age is None or llbot_age > LLBOT_STALE_MIN

    # recorder
    recorder_today = sum(1 for r in live_rows if r.get("src") == "recorder" and str(r.get("ts", "")).startswith(today))
    bot_logs = sorted(glob.glob(str(base / "logs/bot.2026-*.log")))
    bot_log_fresh = _age_min(os.path.getmtime(bot_logs[-1])) if bot_logs else 99999
    if recorder_today:
        add("recorder", "ok", f"今日已记录 {recorder_today} 条")
    elif llbot_offline:
        add("recorder", "info", "LLBot 离线，今日没有消息可记录")
    elif bot_log_fresh < 180:
        add("recorder", "warn", "LLBot 在线且机器人日志活跃，但今日无 recorder 记录（可能未重启到新代码）")
    else:
        add("recorder", "info", "今日无 recorder 记录（机器人可能未运行）")

    # 重复 / uid
    if rows:
        dup = _dup_count(rows)
        ratio = dup / max(1, len(rows))
        add("重复率", "warn" if ratio > DUP_WARN_RATIO else "ok", f"{dup} 条（{ratio * 100:.1f}%）")
        missing = sum(1 for r in rows if not r.get("uid"))
        miss_ratio = missing / max(1, len(rows))
        add("uid 缺失", "warn" if miss_ratio > UID_MISS_WARN_RATIO else "ok", f"{missing} 条（{miss_ratio * 100:.1f}%）")

    # 汇总
    status = "fail" if fail else ("warn" if warn else "ok")
    summary = {
        "checked_at": now.isoformat(timespec="seconds"),
        "status": status,
        "gid": gid,
        "today": today,
        "canonical_today": len(rows),
        "llbot_today": llbot_today,
        "live_rows": len(live_rows),
        "recorder_today": recorder_today,
        "latest_ts": latest.get("ts") if latest else None,
        "checks": checks,
    }
    (reports / "latest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(reports / "history.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(summary, ensure_ascii=False) + "\n")

    icon = {"ok": "✅", "warn": "⚠️", "fail": "❌", "info": "ℹ️"}
    lines = [f"# 语料健康检查 · {now.strftime('%Y-%m-%d %H:%M')}", "", f"总体：{icon[status]} **{status}**", ""]
    for c in checks:
        lines.append(f"- {icon.get(c['status'], '·')} {c['name']}：{c['detail']}")
    (reports / "latest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(run())
