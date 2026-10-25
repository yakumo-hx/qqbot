# -*- coding: utf-8 -*-
"""DialogueState：可从 session 重建的派生对话状态。

它不是聊天真相源；`sessions/*.jsonl` 仍是唯一历史。这里只保存
“当前话题、未接住消息、待答问题、是否在等用户接话、最近气泡行为”
这类短期提示，供 BehaviorPolicy 和 prompt 使用。

阶段 2 只做确定性规则，不引入情绪/亲密度/模型推理。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from . import config as cfgmod

SCHEMA_VERSION = 1
_MAX_PENDING = 64
_MAX_RECENT_ACTS = 6

_QUESTION_RE = re.compile(
    r"([?？]|吗|呢|什么|怎么|为什么|为啥|多少|哪|谁|如何|"
    r"是不是|有没有|能不能|可不可以|要不要|好不好)"
)


def is_question_text(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    return bool(_QUESTION_RE.search(value))


def topic_hint(recent_user_texts: Iterable[str], limit: int = 80) -> str:
    """最近三条用户消息的轻量话题提示，不做模型总结。"""
    texts = [re.sub(r"\s+", " ", str(x or "")).strip() for x in recent_user_texts]
    texts = [x for x in texts if x][-3:]
    if not texts:
        return ""
    return " / ".join(x[:18] for x in texts)[:limit]


def _as_id(value: Any) -> str:
    return "" if value is None else str(value)


@dataclass
class DialogueState:
    schema_version: int = SCHEMA_VERSION
    current_topic: str = ""
    current_topic_msg_id: str = ""
    pending_user_msg_ids: list[str] = field(default_factory=list)
    pending_question: str = ""
    awaiting_user: bool = False
    last_user_msg_id: str = ""
    last_assistant_turn_id: str = ""
    recent_bubble_acts: list[str] = field(default_factory=list)
    last_behavior: dict = field(default_factory=dict)
    updated_at: str = ""

    # ---------- 序列化 ----------
    def to_dict(self) -> dict:
        return {
            "schema_version": int(self.schema_version),
            "current_topic": self.current_topic,
            "current_topic_msg_id": self.current_topic_msg_id,
            "pending_user_msg_ids": list(self.pending_user_msg_ids),
            "pending_question": self.pending_question,
            "awaiting_user": bool(self.awaiting_user),
            "last_user_msg_id": self.last_user_msg_id,
            "last_assistant_turn_id": self.last_assistant_turn_id,
            "recent_bubble_acts": list(self.recent_bubble_acts),
            "last_behavior": dict(self.last_behavior),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "DialogueState":
        if not isinstance(data, dict):
            return cls()
        state = cls()
        state.schema_version = int(data.get("schema_version") or SCHEMA_VERSION)
        state.current_topic = str(data.get("current_topic") or "")[:160]
        state.current_topic_msg_id = _as_id(data.get("current_topic_msg_id"))
        pending = data.get("pending_user_msg_ids")
        if isinstance(pending, list):
            state.pending_user_msg_ids = [str(x) for x in pending if x not in (None, "")][-_MAX_PENDING:]
        state.pending_question = str(data.get("pending_question") or "")[:160]
        state.awaiting_user = bool(data.get("awaiting_user"))
        state.last_user_msg_id = _as_id(data.get("last_user_msg_id"))
        state.last_assistant_turn_id = _as_id(data.get("last_assistant_turn_id"))
        acts = data.get("recent_bubble_acts")
        if isinstance(acts, list):
            state.recent_bubble_acts = [str(x) for x in acts if str(x).strip()][-_MAX_RECENT_ACTS:]
        behavior = data.get("last_behavior")
        if isinstance(behavior, dict):
            state.last_behavior = dict(behavior)
        state.updated_at = str(data.get("updated_at") or "")
        return state

    # ---------- 迁移 ----------
    def accept_user(self, msg_id: Any, text: str, ts: str = "",
                    recent_user_texts: Optional[Iterable[str]] = None,
                    is_question: Optional[bool] = None) -> None:
        mid = _as_id(msg_id)
        if mid and mid not in self.pending_user_msg_ids:
            self.pending_user_msg_ids.append(mid)
            self.pending_user_msg_ids = self.pending_user_msg_ids[-_MAX_PENDING:]
        self.last_user_msg_id = mid or self.last_user_msg_id
        # 注意：awaiting_user 表示“上一条已发送回复是否以追问结束”，
        # 用户回话后仍然为 True，BehaviorPolicy 用它判断 follow_up；
        # 下一次 assistant 气泡发送时才会被 record_bubble 改写。
        texts = list(recent_user_texts) if recent_user_texts is not None else [text]
        self.current_topic = topic_hint(texts)
        if mid:
            self.current_topic_msg_id = mid
        question = is_question_text(text) if is_question is None else bool(is_question)
        if question:
            self.pending_question = str(text or "").strip()[:160]
        self.updated_at = cfgmod.now_iso()

    def mark_caught(self, ids: Iterable[Any]) -> None:
        wanted = {_as_id(x) for x in ids if _as_id(x)}
        if wanted:
            self.pending_user_msg_ids = [
                x for x in self.pending_user_msg_ids if x not in wanted
            ]
        self.updated_at = cfgmod.now_iso()

    def record_behavior(self, behavior: Any, turn_id: str = "") -> None:
        if hasattr(behavior, "to_dict"):
            payload = behavior.to_dict()
        elif isinstance(behavior, dict):
            payload = dict(behavior)
        else:
            payload = {"repr": repr(behavior)}
        payload = {k: v for k, v in payload.items() if k != "target_msg_ids"}
        self.last_behavior = payload
        self.last_assistant_turn_id = str(turn_id or self.last_assistant_turn_id)
        self.updated_at = cfgmod.now_iso()

    def record_bubble(self, act: str, turn_id: str = "") -> None:
        value = str(act or "chat")
        self.recent_bubble_acts.append(value)
        self.recent_bubble_acts = self.recent_bubble_acts[-_MAX_RECENT_ACTS:]
        self.awaiting_user = value == "ask"
        if turn_id:
            self.last_assistant_turn_id = str(turn_id)
        self.updated_at = cfgmod.now_iso()

    # ---------- 重建 ----------
    @classmethod
    def rebuild_from_turns(cls, turns: Iterable[dict], max_turns: int = 300) -> "DialogueState":
        """从 session 记录重建派生状态。

        重启后 pending_user_msg_ids 一律为空：不自动补答重启前的消息。
        """
        state = cls()
        rows = [x for x in turns if isinstance(x, dict)][-max(0, int(max_turns)):]
        user_texts: list[str] = []
        last_user_question: str = ""
        answered_after_question = True
        last_assistant: Optional[dict] = None
        for turn in rows:
            role = str(turn.get("role") or "")
            text = str(turn.get("text") or "")
            if role == "user":
                state.last_user_msg_id = _as_id(turn.get("msg_id"))
                user_texts.append(text)
                if is_question_text(text):
                    last_user_question = text[:160]
                    answered_after_question = False
            elif role == "assistant":
                last_assistant = turn
                state.last_assistant_turn_id = _as_id(turn.get("turn_id")) or state.last_assistant_turn_id
                act = str(turn.get("act") or "")
                if act:
                    state.recent_bubble_acts.append(act)
                    state.recent_bubble_acts = state.recent_bubble_acts[-_MAX_RECENT_ACTS:]
                if last_user_question:
                    answered_after_question = True
        state.current_topic = topic_hint(user_texts)
        if user_texts:
            state.current_topic_msg_id = state.last_user_msg_id
        if last_user_question and not answered_after_question:
            state.pending_question = last_user_question
        if last_assistant is not None:
            act = str(last_assistant.get("act") or "")
            if act:
                state.awaiting_user = act == "ask"
            else:
                state.awaiting_user = str(last_assistant.get("text") or "").rstrip().endswith(("?", "？"))
        state.pending_user_msg_ids = []
        state.last_behavior = {}
        state.updated_at = cfgmod.now_iso()
        return state


__all__ = [
    "DialogueState",
    "SCHEMA_VERSION",
    "is_question_text",
    "topic_hint",
]
