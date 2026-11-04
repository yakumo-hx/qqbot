# -*- coding: utf-8 -*-
"""SQLite 语料库。"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    gid         TEXT NOT NULL,
    uid         TEXT,
    nick        TEXT,
    ts          TEXT NOT NULL,
    ts_epoch    INTEGER NOT NULL,
    day         TEXT NOT NULL,
    text_raw    TEXT,
    text_norm   TEXT,
    msg_type    TEXT,
    flags       TEXT,
    reply_to    TEXT,
    reply_quote TEXT,
    src         TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_gid_day ON messages(gid, day);
CREATE INDEX IF NOT EXISTS idx_messages_gid_uid_day ON messages(gid, uid, day);
CREATE INDEX IF NOT EXISTS idx_messages_norm ON messages(text_norm);

CREATE TABLE IF NOT EXISTS nick_evidence (
    nick  TEXT NOT NULL,
    uid   TEXT NOT NULL,
    src   TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (nick, uid, src)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS run_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    task   TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT
);
"""


def message_id(row: dict) -> str:
    raw = "|".join(
        [
            str(row.get("gid", "")),
            str(row.get("src", "")),
            str(row.get("ts", "")),
            str(row.get("uid") or row.get("nick") or ""),
            str(row.get("text_norm", "")),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def insert_messages(con: sqlite3.Connection, rows: Iterable[dict]) -> int:
    payload = []
    for r in rows:
        payload.append(
            (
                r.get("id") or message_id(r),
                r["gid"],
                r.get("uid"),
                r.get("nick"),
                r["ts"],
                int(r["ts_epoch"]),
                r["day"],
                r.get("text_raw"),
                r.get("text_norm"),
                r.get("msg_type"),
                r.get("flags"),
                r.get("reply_to"),
                r.get("reply_quote"),
                r.get("src"),
            )
        )
    before = con.total_changes
    con.executemany(
        """INSERT OR IGNORE INTO messages
           (id, gid, uid, nick, ts, ts_epoch, day, text_raw, text_norm,
            msg_type, flags, reply_to, reply_quote, src)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        payload,
    )
    con.commit()
    return con.total_changes - before


def save_nick_evidence(con: sqlite3.Connection, evidence: dict[tuple[str, str, str], int]) -> None:
    con.executemany(
        """INSERT INTO nick_evidence(nick, uid, src, count) VALUES (?,?,?,?)
           ON CONFLICT(nick, uid, src) DO UPDATE SET count=count+excluded.count""",
        [(n, u, s, c) for (n, u, s), c in evidence.items()],
    )
    con.commit()


def get_meta(con: sqlite3.Connection, key: str) -> str | None:
    cur = con.execute("SELECT value FROM meta WHERE key=?", (key,))
    row = cur.fetchone()
    return row[0] if row else None


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    con.commit()


def log_run(con: sqlite3.Connection, ts: str, task: str, status: str, detail: str = "") -> None:
    con.execute(
        "INSERT INTO run_log(ts, task, status, detail) VALUES (?,?,?,?)",
        (ts, task, status, detail),
    )
    con.commit()
