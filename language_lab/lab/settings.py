# -*- coding: utf-8 -*-
"""路径与配置。"""

from __future__ import annotations

import json
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parent.parent       # language_lab/
PROJECT_ROOT = LAB_ROOT.parent                          # E:\QQbot/
CONFIG_PATH = LAB_ROOT / "config" / "lab.json"
DATA_DIR = LAB_ROOT / "data"
REPORTS_DIR = LAB_ROOT / "reports"
DB_PATH = DATA_DIR / "lab.sqlite3"
GROUP_HISTORY_DIR = DATA_DIR / "group_history"
LIVE_DIR = GROUP_HISTORY_DIR / "_live"


def load_config(path: str | Path | None = None) -> dict:
    p = Path(path) if path else CONFIG_PATH
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def resolve(value: str | Path) -> Path:
    """把配置里的相对路径按仓库根目录解析。"""
    p = Path(value)
    return p if p.is_absolute() else PROJECT_ROOT / p


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    GROUP_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
