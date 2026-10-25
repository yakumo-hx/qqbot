# -*- coding: utf-8 -*-
"""ResponseComposer：普通回复的唯一一次在线模型调用 + 结构化气泡解析。

输出契约（structured）：

    {"bubbles": [{"act": "react", "text": "第一句"},
                 {"act": "answer", "text": "第二句"}]}

- 只调用一次模型；绝不为了修 JSON 再调一次；
- 有效 JSON → 保留语义气泡，不再按标点重拆；
- 普通旧式文本 → 用 split_bubbles() 兼容降级；
- 残缺 JSON → 不允许把 JSON 原文发给用户，使用短句 fallback；
- 纠偏逐气泡执行；空气泡删除；全部为空时 fallback。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import corrections as corrections_mod
from . import llm as llm_mod
from .sender import split_bubbles

_VALID_ACTS = {"react", "answer", "support", "ask", "share", "continue", "chat"}
_TEXT_LIMIT = 160
_FENCE = re.compile(r"^```(?:json|text)?\s*|\s*```$", re.I)
_JSONISH = re.compile(r"^\s*[\[{]")
# 模型可能输出“好的，这是回复：{...}”；只要出现 bubbles 键，就不能当纯文本发出去。
_JSON_LEAK = re.compile(r'"bubbles"\s*:', re.I)


@dataclass
class ComposedReply:
    bubbles: list[dict]
    structured: bool
    degraded: str = ""          # "" / "plain_text" / "malformed_json" / "model_error"
    raw_chars: int = 0

    @property
    def texts(self) -> list[str]:
        return [str(x.get("text") or "") for x in self.bubbles]


def _get(obj: Any, name: str, default: Any) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    text = _FENCE.sub("", text).strip()
    return text


def _validate_bubbles(payload: Any, max_items: int = 20) -> list[dict]:
    """收集所有合法气泡；超上限的合并交给 _limit_bubbles，不在这里丢弃内容。"""
    if not isinstance(payload, dict):
        return []
    items = payload.get("bubbles")
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = _clean_text(item.get("text"))
        if not text:
            continue
        act = str(item.get("act") or "chat").strip().lower()
        if act not in _VALID_ACTS:
            act = "chat"
        out.append({"act": act, "text": text})
        if len(out) >= max(1, int(max_items)):
            break
    return out


def _apply_corrections(bubbles: list[dict], corrections: list[dict]) -> list[dict]:
    out: list[dict] = []
    for bubble in bubbles:
        text = corrections_mod.apply(str(bubble.get("text") or ""), corrections)
        text = text.strip()
        if text:
            out.append({"act": bubble.get("act", "chat"), "text": text})
    return out


def _limit_bubbles(bubbles: list[dict], limit: int, max_chars: int) -> list[dict]:
    """气泡过多时合并尾部；长度超限时截断/继续合并。"""
    if not bubbles:
        return []
    limit = max(1, int(limit))
    if len(bubbles) > limit:
        head = bubbles[:limit - 1]
        tail = bubbles[limit - 1:]
        merged = "".join(str(x.get("text") or "") for x in tail)
        head.append({"act": tail[0].get("act", "chat"), "text": merged})
        bubbles = head
    # 单气泡长度上限
    for bubble in bubbles:
        text = str(bubble.get("text") or "")
        if len(text) > _TEXT_LIMIT:
            bubble["text"] = text[:_TEXT_LIMIT - 1].rstrip() + "…"
    # 整轮长度上限：优先合并尾部
    total = sum(len(str(x.get("text") or "")) for x in bubbles)
    while total > max_chars and len(bubbles) > 1:
        last = bubbles.pop()
        bubbles[-1]["text"] = str(bubbles[-1].get("text") or "") + str(last.get("text") or "")
        total = sum(len(str(x.get("text") or "")) for x in bubbles)
    if total > max_chars and bubbles:
        text = str(bubbles[-1].get("text") or "")
        bubbles[-1]["text"] = text[:max_chars].rstrip() + "…"
    return bubbles


class ResponseComposer:
    def __init__(self, cfg: dict, logger=None,
                 llm_fn: Optional[Callable[[list[dict], dict], Any]] = None,
                 raw_fn: Optional[Callable[[list[dict], dict], Any]] = None):
        self.cfg = cfg
        self.logger = logger
        self._llm_fn = llm_fn
        self._raw_fn = raw_fn

    def _max_bubbles(self, decision: Any, rhythm: Any) -> int:
        reply_cfg = self.cfg.get("reply") or {}
        hard = max(1, int(reply_cfg.get("max_bubbles", 3) or 3))
        behavior_cap = int(_get(decision, "bubble_cap", hard) or hard)
        rhythm_cap = int(_get(rhythm, "max_bubbles", hard) or hard)
        return max(1, min(hard, behavior_cap, rhythm_cap))

    def _fallback(self, fallback_text: str, decision: Any) -> list[dict]:
        text = str(fallback_text or "").strip() or "……刚刚走神了，你再说一遍？"
        act = "answer" if str(_get(decision, "intent", "")) == "answer" else "chat"
        return [{"act": act, "text": text}]

    async def compose(self, messages: list[dict], *, structured: bool,
                      decision: Any, rhythm: Any, corrections: Optional[list[dict]] = None,
                      fallback_text: str = "") -> ComposedReply:
        limit = self._max_bubbles(decision, rhythm)
        max_chars = int((self.cfg.get("reply") or {}).get("max_reply_chars", 600) or 600)
        target = int(_get(rhythm, "target_bubbles", 1) or 1)
        corrections = list(corrections or [])

        if not structured:
            try:
                if self._llm_fn is not None:
                    raw_text = await self._llm_fn(messages, self.cfg)
                else:
                    raw_text = await llm_mod.chat(messages, self.cfg)
            except Exception as exc:  # noqa: BLE001
                if self.logger:
                    self.logger.info(f"[私聊] 模型不可用（{type(exc).__name__}），走降级")
                return ComposedReply(self._fallback(fallback_text, decision), False, "model_error")
            text = _clean_text(raw_text)
            if not text:
                return ComposedReply(self._fallback(fallback_text, decision), False, "model_error")
            bubbles = [
                {"act": "answer" if str(_get(decision, "intent", "")) == "answer" else "chat",
                 "text": part}
                for part in split_bubbles(text, limit, target_bubbles=target)
            ]
            bubbles = _apply_corrections(bubbles, corrections)
            bubbles = _limit_bubbles(bubbles, limit, max_chars)
            if not bubbles:
                return ComposedReply(self._fallback(fallback_text, decision), False, "model_error")
            return ComposedReply(bubbles, False, "", len(text))

        max_tokens = int(
            (self.cfg.get("reply") or {}).get("structured_max_tokens", 900) or 900
        )
        try:
            if self._raw_fn is not None:
                raw_text = await self._raw_fn(messages, self.cfg)
            else:
                raw_text = await llm_mod.chat_raw(
                    messages, self.cfg, max_tokens=max_tokens, label="private_chat_structured",
                )
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.info(f"[私聊] 结构化模型不可用（{type(exc).__name__}），走降级")
            return ComposedReply(self._fallback(fallback_text, decision), True, "model_error")

        text = _clean_text(raw_text)
        if not text:
            return ComposedReply(self._fallback(fallback_text, decision), True, "model_error")

        payload = llm_mod.extract_json(text)
        bubbles = _validate_bubbles(payload) if payload is not None else []
        if bubbles:
            bubbles = _apply_corrections(bubbles, corrections)
            bubbles = _limit_bubbles(bubbles, limit, max_chars)
            if not bubbles:
                return ComposedReply(self._fallback(fallback_text, decision), True, "malformed_json")
            return ComposedReply(bubbles, True, "", len(text))

        # JSON 解析失败：普通旧式文本才走 split_bubbles；JSON 残片/带前导文本的
        # JSON 残片都不给用户看。
        if _JSONISH.match(text) or _JSON_LEAK.search(text):
            return ComposedReply(self._fallback(fallback_text, decision), True, "malformed_json")
        bubbles = [
            {"act": "answer" if str(_get(decision, "intent", "")) == "answer" else "chat",
             "text": part}
            for part in split_bubbles(text, limit, target_bubbles=target)
        ]
        bubbles = _apply_corrections(bubbles, corrections)
        bubbles = _limit_bubbles(bubbles, limit, max_chars)
        if not bubbles:
            return ComposedReply(self._fallback(fallback_text, decision), True, "model_error")
        return ComposedReply(bubbles, True, "plain_text", len(text))


__all__ = ["ComposedReply", "ResponseComposer"]
