# -*- coding: utf-8 -*-
"""BehaviorPolicy：同步、无模型、无 I/O 的行为决策器。

输入是 DialogueState 快照 + 本轮目标消息 + 运行时状态；输出本轮应该
respond/wait、意图、是否允许追问/自我分享、气泡上限和可测试的 reason。

它不生成文本、不拆气泡、不算延迟，也不读写 session。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from . import style as style_mod
from .dialogue_state import is_question_text

_VALID_INTENTS = {"answer", "support", "chat", "follow_up"}


@dataclass
class BehaviorDecision:
    action: str                     # respond / wait
    intent: str                     # answer / support / chat / follow_up
    allow_question: bool
    allow_self_share: bool
    bubble_cap: int
    reason: str
    target_msg_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "intent": self.intent,
            "allow_question": bool(self.allow_question),
            "allow_self_share": bool(self.allow_self_share),
            "bubble_cap": int(self.bubble_cap),
            "reason": self.reason,
            "target_msg_ids": list(self.target_msg_ids),
        }


def _last_text(messages: Iterable[dict]) -> str:
    texts = [str(x.get("text") or "") for x in messages if isinstance(x, dict)]
    return texts[-1] if texts else ""


def _combined_text(messages: Iterable[dict]) -> str:
    return "\n".join(str(x.get("text") or "") for x in messages if isinstance(x, dict))


def _all_ids(messages: Iterable[dict]) -> list[str]:
    return [str(x.get("msg_id")) for x in messages
            if isinstance(x, dict) and x.get("msg_id") is not None]


def _bubble_cap(cfg: dict, intent: str) -> int:
    reply_cfg = cfg.get("reply") or {}
    hard_cap = max(1, int(reply_cfg.get("max_bubbles", 3) or 3))
    caps = {"support": 1, "answer": 2, "chat": 2, "follow_up": 2}
    return max(1, min(hard_cap, caps.get(intent, 2)))


def decide(cfg: dict, state, target_messages: list[dict], *,
           queue_busy: bool = False, image_pending: bool = False,
           burst_n: int = 1, text: Optional[str] = None,
           relationship_stage: str = "") -> BehaviorDecision:
    """确定本轮行为。规则优先级：wait > distress > question > burst > follow-up > chat。"""
    messages = [x for x in (target_messages or []) if isinstance(x, dict)]
    ids = _all_ids(messages)
    if queue_busy or image_pending:
        return BehaviorDecision(
            action="wait",
            intent="chat",
            allow_question=False,
            allow_self_share=False,
            bubble_cap=0,
            reason="还有排队输入或图片预处理未完成",
            target_msg_ids=ids,
        )

    value = str(text if text is not None else _last_text(messages))
    combined = _combined_text(messages) or value
    recent_acts = [str(x) for x in (getattr(state, "recent_bubble_acts", []) or [])]
    # 最近两个已发送气泡都在追问：本轮不再机械追问。
    no_repeat_ask = sum(1 for x in recent_acts[-2:] if x == "ask") >= 2
    if style_mod.is_distress(combined):
        return BehaviorDecision(
            action="respond",
            intent="support",
            allow_question=False,
            allow_self_share=False,
            bubble_cap=_bubble_cap(cfg, "support"),
            reason="distress",
            target_msg_ids=ids,
        )

    pending_question = str(getattr(state, "pending_question", "") or "")
    question_text = pending_question if pending_question else (
        value if is_question_text(value) else ""
    )
    if question_text:
        return BehaviorDecision(
            action="respond",
            intent="answer",
            allow_question=not no_repeat_ask,
            allow_self_share=False,
            bubble_cap=_bubble_cap(cfg, "answer"),
            reason="question" + (":no_repeat_ask" if no_repeat_ask else ""),
            target_msg_ids=ids,
        )

    if int(burst_n or 1) >= 2:
        return BehaviorDecision(
            action="respond",
            intent="chat",
            allow_question=not no_repeat_ask,
            allow_self_share=True,
            bubble_cap=_bubble_cap(cfg, "chat"),
            reason="burst" + (":no_repeat_ask" if no_repeat_ask else ""),
            target_msg_ids=ids,
        )

    if bool(getattr(state, "awaiting_user", False)):
        return BehaviorDecision(
            action="respond",
            intent="follow_up",
            allow_question=not no_repeat_ask,
            allow_self_share=True,
            bubble_cap=_bubble_cap(cfg, "follow_up"),
            reason="awaiting_user" + (":no_repeat_ask" if no_repeat_ask else ""),
            target_msg_ids=ids,
        )

    intent = "chat"
    if relationship_stage in ("close", "intimate"):
        # 关系阶段只作为 reason 记录，阶段 2 不改变行为矩阵。
        reason = f"default:{relationship_stage}"
    else:
        reason = "default"
    return BehaviorDecision(
        action="respond",
        intent=intent,
        allow_question=not no_repeat_ask,
        allow_self_share=True,
        bubble_cap=_bubble_cap(cfg, intent),
        reason=reason + (":no_repeat_ask" if no_repeat_ask else ""),
        target_msg_ids=ids,
    )


__all__ = ["BehaviorDecision", "decide"]
