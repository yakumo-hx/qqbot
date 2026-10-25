# -*- coding: utf-8 -*-
"""对话节奏模型：学「怎么发消息」，不是学「说什么」。

每个用户一份 style.json，记录平均消息长度（EWMA）、连发率、长段率、emoji 率。
回复前根据本轮状态生成 RhythmPlan：回几条、目标长度、首条延迟和气泡间隔。

职责边界（阶段 2 起）：
- 只提供节奏/长度偏好，不描述「先安慰/不要追问」这类语义行为；
- 语义行为由 behavior.BehaviorPolicy 负责；
- 最终气泡数取 BehaviorPolicy.bubble_cap、RhythmPlan.max_bubbles 与
  config reply.max_bubbles 的最小值，实际可以少于目标。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional

from . import config as cfgmod
from .store import PrivateStore

_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U0001F000-\U0001F2FF"
    "\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]"
)

_DISTRESS = (
    "难受", "难过", "伤心", "累", "焦虑", "压力", "想哭", "崩溃", "不开心",
    "失眠", "孤独", "害怕", "抑郁", "烦", "撑不住", "喘不过气", "委屈",
)

_DEFAULT_STYLE = {
    "samples": 0,
    "msg_len_ewma": 0.0,
    "burst_rate": 0.0,
    "long_form_rate": 0.0,
    "emoji_rate": 0.0,
    "updated_at": "",
}


@dataclass
class RhythmPlan:
    target_bubbles: int
    max_bubbles: int
    target_chars: int
    first_delay_ms: int
    gap_ms: int
    reason: str

    def to_dict(self) -> dict:
        return {
            "target_bubbles": self.target_bubbles,
            "max_bubbles": self.max_bubbles,
            "target_chars": self.target_chars,
            "first_delay_ms": self.first_delay_ms,
            "gap_ms": self.gap_ms,
            "reason": self.reason,
        }


def _style_path(store: PrivateStore, uid: str) -> str:
    return f"{store.base_dir(uid)}/style.json"


def load(store: PrivateStore, uid: str) -> dict:
    data = cfgmod.read_json(_style_path(store, uid), dict(_DEFAULT_STYLE))
    if not isinstance(data, dict):
        return dict(_DEFAULT_STYLE)
    out = dict(_DEFAULT_STYLE)
    out.update({k: data.get(k, v) for k, v in _DEFAULT_STYLE.items()})
    return out


def save(store: PrivateStore, uid: str, data: dict) -> bool:
    return cfgmod.atomic_write_json(_style_path(store, uid), data)


def update(store: PrivateStore, uid: str, text: str, burst_n: int, cfg: dict) -> dict:
    """每收到一条用户消息更新一次（EWMA）。"""
    style_cfg = cfg.get("style") or {}
    alpha = float(style_cfg.get("ewma_alpha", 0.2) or 0.2)
    alpha = max(0.05, min(0.6, alpha))
    data = load(store, uid)
    samples = int(data.get("samples", 0) or 0)
    length = len(str(text or ""))
    emoji = len(_EMOJI.findall(str(text or "")))
    long_form = 1.0 if length >= int(style_cfg.get("long_form_chars", 80) or 80) else 0.0
    burst = 1.0 if int(burst_n or 1) >= 3 else 0.0
    if samples <= 0:
        data["msg_len_ewma"] = float(length)
        data["burst_rate"] = burst
        data["long_form_rate"] = long_form
        data["emoji_rate"] = float(emoji)
    else:
        data["msg_len_ewma"] = round((1 - alpha) * float(data.get("msg_len_ewma", 0)) + alpha * length, 2)
        data["burst_rate"] = round((1 - alpha) * float(data.get("burst_rate", 0)) + alpha * burst, 3)
        data["long_form_rate"] = round((1 - alpha) * float(data.get("long_form_rate", 0)) + alpha * long_form, 3)
        data["emoji_rate"] = round((1 - alpha) * float(data.get("emoji_rate", 0)) + alpha * emoji, 3)
    data["samples"] = samples + 1
    data["updated_at"] = cfgmod.now_iso()
    save(store, uid, data)
    return data


def is_distress(text: str) -> bool:
    value = str(text or "")
    return any(word in value for word in _DISTRESS)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def plan(cfg: dict, store: PrivateStore, uid: str, burst_n: int, text: str,
         *, proactive: bool = False) -> RhythmPlan:
    """计算本轮节奏计划；只影响长度/气泡上限/延迟，不影响语义行为。"""
    style_cfg = cfg.get("style") or {}
    reply_cfg = cfg.get("reply") or {}
    hard_cap = max(1, int(reply_cfg.get("max_bubbles", 3) or 3))
    length = len(str(text or ""))
    long_form_chars = int(style_cfg.get("long_form_chars", 80) or 80)
    long_form = length >= long_form_chars
    distress = is_distress(text)
    burst_n = max(1, int(burst_n or 1))

    if proactive:
        return RhythmPlan(
            target_bubbles=1,
            max_bubbles=1,
            target_chars=60,
            first_delay_ms=300,
            gap_ms=0,
            reason="proactive",
        )
    if distress:
        return RhythmPlan(
            target_bubbles=1,
            max_bubbles=1,
            target_chars=90,
            first_delay_ms=700,
            gap_ms=0,
            reason="distress",
        )
    if burst_n >= 3:
        return RhythmPlan(
            target_bubbles=2,
            max_bubbles=min(2, hard_cap),
            target_chars=45,
            first_delay_ms=400,
            gap_ms=1100,
            reason="burst",
        )
    if burst_n == 2 and length <= 15:
        return RhythmPlan(
            target_bubbles=2,
            max_bubbles=min(2, hard_cap),
            target_chars=45,
            first_delay_ms=400,
            gap_ms=1100,
            reason="burst2",
        )
    if long_form:
        return RhythmPlan(
            target_bubbles=1,
            max_bubbles=1,
            target_chars=_clamp(80 + min(40, (length - long_form_chars) // 4), 80, 140),
            first_delay_ms=900,
            gap_ms=0,
            reason="long_form",
        )
    return RhythmPlan(
        target_bubbles=1,
        max_bubbles=min(2, hard_cap),
        target_chars=_clamp(
            20 + int(float(load(store, uid).get("msg_len_ewma", 0) or 0)) * 2,
            25, 70
        ),
        first_delay_ms=600,
        gap_ms=900,
        reason="default",
    )


__all__ = ["RhythmPlan", "is_distress", "load", "plan", "save", "update"]
