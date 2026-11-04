# -*- coding: utf-8 -*-
"""私聊陪伴版 0.0.1 的配置、路径与通用小工具。

设计原则：
- 独立进程、独立端口、独立数据目录；群聊链路完全不参与；
- 配置缺失/损坏时 fail-closed（enabled=False），不会误放行；
- 数据落在 data/private/，原子写 JSON，进程被杀不留下半截文件。
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import time
from datetime import datetime
from typing import Any, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "private_chat.json")
ACTIVE_CHARACTER_PATH = os.path.join(PROJECT_ROOT, "config", "active_character.json")
CHARACTERS_DIR = os.path.join(PROJECT_ROOT, "config", "characters")
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")

VERSION = "0.0.1"

DEFAULT_CONFIG: dict = {
    "enabled": True,
    "host": "0.0.0.0",
    "port": 8081,
    "allowed_users": ["784427550"],
    "auto_accept_friends": True,
    "data_dir": "data/private",
    "character": "大肥鱼",
    "persona": {
        "mode": "card",
        "raw_file": "_source/raw.md",
        "raw_max_chars": 6000,
        "card_max_chars": 6000,
        "proactive_card_max_chars": 6000,
        "caps": {
            "core": 420,
            "traits": 260,
            "speaking": 700,
            "blacklist": 240,
            "daily": 160,
            "phrases": 400,
            "avoid": 120,
        },
    },
    "reply": {
        "pipeline": "structured",
        "debounce_seconds": 2.0,
        "debounce_jitter_seconds": 1.5,
        "max_bubbles": 3,
        "typing_speed_chars_per_second": 12.0,
        "max_typing_seconds": 8.0,
        "max_reply_chars": 600,
        "llm_timeout_seconds": 30.0,
        "llm_total_budget_seconds": 45.0,
        "max_tokens": 600,
        "structured_max_tokens": 900,
        "send_ack_timeout_seconds": 5.0,
        "fallback_on_error": True,
        "max_context_turns": 24,
    },
    "memory": {
        "enabled": True,
        "extract_every_turns": 4,
        "extract_min_new_msgs": 6,
        "max_memories_in_prompt": 10,
        "max_memory_chars": 120,
        "min_confidence": 0.5,
        "retention_days": 0,
        "raw_retention_days": 0,
        "organize_with_llm": True,
        "reflection_interval_seconds": 86400,
        "llm_timeout_seconds": 60.0,
        "llm_total_budget_seconds": 90.0,
    },
    "image": {
        "enabled": True,
        "vision": True,
        "vision_model": "qwen3.8-chat",
        "recognize_stickers": True,
        "sticker_max_per_hour": 8,
        "sticker_vision_timeout_seconds": 30.0,
        "sticker_max_edge": 512,
        "max_per_hour": 10,
        "recent_window_seconds": 120,
        "seen_window_seconds": 60,
        "pick_max": 3,
        "max_images_per_turn": 1,
        "fetch_timeout_seconds": 10.0,
        "preprocess_timeout_seconds": 15.0,
        "vision_timeout_seconds": 60.0,
        "max_bytes": 8 * 1024 * 1024,
        "max_edge": 1024,
        "cache_prefix": "private",
        "send_enabled": False,
    },
    "proactive": {
        "enabled": True,
        "dry_run": False,
        "daily_min": 3,
        "daily_max": 5,
        "weekly_max": 30,
        "min_interval_seconds": 7200,
        "silence_hours": 2,
        "quiet_hours": [23, 8],
        "startup_grace_seconds": 600,
        "max_unanswered": 3,
        "content_max_chars": 80,
        "tick_seconds": 60,
    },
    "history": {
        "enabled": True,
        "on_connect": True,
        "count": 20,
        "min_interval_seconds": 60,
        "disable_get_url": True,
    },
    "activity": {
        "enabled": True,
        "timezone": "Asia/Shanghai",
        "extract_from_chat": True,
        "extract_every_turns": 4,
        "extract_cooldown_seconds": 30.0,
        "default_duration_minutes": 120,
        "retention_days": 90,
        "history_max": 500,
        "prompt_max_chars": 220,
        "proactive_busy_factor": 0.35,
        "proactive_upcoming_factor": 0.6,
        "upcoming_skip_minutes": 15,
        "busy_categories": ["class", "exam", "meeting", "sleep", "work"],
        "periods": {
            "morning": [6, 12],
            "afternoon": [12, 18],
            "evening": [18, 23],
            "night": [23, 6],
        },
    },
    "search": {
        "enabled": True,
        "router_enabled": True,
        "prefilter": True,
        "max_results": 5,
        "timeout_seconds": 15.0,
        "with_pages": False,
        "cache_ttl_seconds": 600,
        "max_query_chars": 200,
        "max_block_chars": 6000,
        "sensitive_skip": True,
    },
    "style": {
        "enabled": True,
        "burst_window_seconds": 8,
        "long_form_chars": 80,
        "ewma_alpha": 0.2,
    },
    "safety": {
        "disclose_ai_when_asked": True,
        "log_message_bodies": False,
        "pause_keywords": ["别烦我", "在忙", "勿扰", "安静点", "晚点说", "先睡了", "睡觉了"],
        "pause_hours": 24,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    merged = copy.deepcopy(base)
    if not isinstance(override, dict):
        return merged
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_config(path: Optional[str] = None) -> dict:
    """读取配置；缺失/损坏时返回 enabled=False 的安全默认。"""
    path = path or CONFIG_PATH
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("config root is not an object")
        cfg = _deep_merge(DEFAULT_CONFIG, raw)
    except FileNotFoundError:
        cfg["enabled"] = False
        cfg["_load_error"] = f"配置文件不存在: {path}"
    except Exception as exc:  # noqa: BLE001
        cfg["enabled"] = False
        cfg["_load_error"] = f"配置读取失败: {type(exc).__name__}: {exc}"
    return cfg


def project_path(*parts: str) -> str:
    return os.path.join(PROJECT_ROOT, *parts)


def resolve_data_dir(cfg: dict) -> str:
    raw = str(cfg.get("data_dir") or "data/private")
    return raw if os.path.isabs(raw) else os.path.join(PROJECT_ROOT, raw)


def resolve_log_path(name: str = "private_bot") -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    return os.path.join(LOG_DIR, f"{name}.{datetime.now().strftime('%Y-%m-%d')}.log")


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return copy.deepcopy(default)
    except (OSError, json.JSONDecodeError):
        return copy.deepcopy(default)


def atomic_write_json(path: str, data: Any) -> bool:
    directory = os.path.dirname(path) or "."
    try:
        ensure_dir(directory)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return True
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
    except OSError:
        return False


def atomic_write_text(path: str, text: str) -> bool:
    """原子重写文本文件（JSONL 的 update_turn 用）。"""
    directory = os.path.dirname(path) or "."
    try:
        ensure_dir(directory)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".txt", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(str(text))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return True
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
    except OSError:
        return False


def append_jsonl(path: str, record: dict) -> bool:
    try:
        ensure_dir(os.path.dirname(path) or ".")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def read_jsonl_tail(path: str, max_lines: int = 200) -> list[dict]:
    if max_lines <= 0:
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-max_lines:]
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def get_active_character() -> str:
    data = read_json(ACTIVE_CHARACTER_PATH, {})
    if isinstance(data, dict):
        name = str(data.get("character") or "").strip()
        if name:
            return name
    return ""


def load_private_text(cfg: dict) -> str:
    """只读私聊自己的 identity.md / voice.md；不读群聊 persona.txt。"""
    character = str(cfg.get("character") or "").strip() or get_active_character()
    if not character:
        return ""
    base = os.path.join(CHARACTERS_DIR, character, "private")
    parts: list[str] = []
    for name in ("identity.md", "voice.md"):
        try:
            with open(os.path.join(base, name), "r", encoding="utf-8") as f:
                text = f.read().strip()
            if text:
                parts.append(text)
        except OSError:
            continue
    return "\n\n".join(parts)


def monotonic() -> float:
    return time.monotonic()


_INVISIBLE = re.compile(r"[\s\u200b-\u200f\u202a-\u202e\u2060\ufeff\u3164]+")


def normalize_display_name(value) -> str:
    """昵称/群名片 -> 可读显示名；只有空白/隐形字符时回退为「你」。"""
    cleaned = _INVISIBLE.sub("", str(value or "")).strip()
    return cleaned or "你"
