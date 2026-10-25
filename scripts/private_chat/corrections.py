# -*- coding: utf-8 -*-
"""纠偏层（Correction）——移植自 love-companion 的纠偏语义。

来源：MoYan1638/love-companion（MIT），
`scripts/persona/library.py` 的 `correct()` / `apply_corrections()`：
- 一条纠偏 = {wrong, right, created_at}
- right 为空 = 直接禁用 wrong
- 最近 50 条为上限
- 编译摘要时追加「不要说…」；生成后处理时替换/删除

本文件只做存储与文本应用；不包含任何角色设定内容。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from . import config as cfgmod

MAX_ITEMS = 50


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load(path: str) -> list[dict]:
    data = cfgmod.read_json(path, {"version": 1, "items": []})
    if isinstance(data, dict):
        items = data.get("items")
    elif isinstance(data, list):
        items = data
    else:
        items = []
    out: list[dict] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            wrong = str(item.get("wrong") or "").strip()
            if not wrong:
                continue
            out.append({
                "wrong": wrong,
                "right": str(item.get("right") or "").strip(),
                "created_at": str(item.get("created_at") or _now()),
            })
    return out[-MAX_ITEMS:]


def save(path: str, items: list[dict]) -> bool:
    return cfgmod.atomic_write_json(path, {"version": 1, "items": items[-MAX_ITEMS:]})


def add(path: str, wrong: str, right: str = "") -> list[dict]:
    wrong = str(wrong or "").strip()
    right = str(right or "").strip()
    if not wrong:
        return load(path)
    items = load(path)
    # 同一句纠偏只保留最新版本
    items = [x for x in items if x.get("wrong") != wrong]
    items.append({"wrong": wrong, "right": right, "created_at": _now()})
    items = items[-MAX_ITEMS:]
    save(path, items)
    return items


def apply(text: str, items: list[dict]) -> str:
    """把纠偏规则应用到一段文本（生成后处理）。"""
    out = str(text or "")
    for item in items or []:
        wrong = str(item.get("wrong") or "")
        right = str(item.get("right") or "")
        if not wrong:
            continue
        out = out.replace(wrong, right)
    return re.sub(r"\s{2,}", " ", out).strip()


def rules_text(items: list[dict], limit: int = 3) -> str:
    """编译成 prompt 里的纠偏约束（取最近 limit 条）。"""
    picked = [x for x in (items or []) if x.get("wrong")][-max(1, int(limit)):]
    if not picked:
        return ""
    parts = []
    for item in picked:
        wrong = item.get("wrong", "")
        right = item.get("right", "")
        if right:
            parts.append(f"不要说「{wrong}」，要说「{right}」")
        else:
            parts.append(f"不要说「{wrong}」")
    return "；".join(parts)


def parse_command(text: str) -> Optional[tuple[str, str]]:
    """解析 `/纠偏 wrong => right` / `/纠偏 wrong`。

    返回 (wrong, right)，不是纠偏命令则返回 None。
    """
    value = str(text or "").strip()
    if not value.startswith("/纠偏"):
        return None
    rest = value[len("/纠偏"):].strip()
    if not rest:
        return None
    for sep in ("=>", "->", "→", "＝>", "："):
        if sep in rest:
            wrong, right = rest.split(sep, 1)
            wrong = wrong.strip(" 「」\"'")
            right = right.strip(" 「」\"'")
            if wrong:
                return wrong, right
            return None
    return rest.strip(" 「」\"'"), ""
