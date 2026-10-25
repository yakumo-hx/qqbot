# -*- coding: utf-8 -*-
"""私聊专用模型调用：快车道 + 熔断 + 输出清洗。

群聊路径的模型池超时是 55s 总预算；私聊不能等那么久，但上游网关可能很慢，
所以聊天给 30s 单次 / 45s 总预算，记忆整理给 60s / 90s。
聊天、结构化回复和记忆整理各有独立熔断：连续失败 3 次 → 对应通道 120 秒内
直接走降级。

- chat()      老文本链路：返回 clean_reply 后的文本；
- chat_raw()  结构化链路：只做最外层空白清理，不截断/不去围栏；
- chat_json() 记忆/活动链路：返回解析后的 JSON 对象（失败 None）。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Optional

from scripts import llm_pool

from . import config as cfgmod

_PREFIX = re.compile(r"^\s*(?:\[[^\]\n]{1,80}\][:：]\s*|\[[^\]\n]{1,80}(?:@|回复)[^\]\n]{0,80}\]\s*)")
_ROLE_PREFIX = re.compile(r"^\s*(?:assistant|ai|bot|机器人|助手)\s*[:：]\s*", re.I)
_FENCE = re.compile(r"^```(?:json|text)?\s*|\s*```$", re.I)


class LLMUnavailable(RuntimeError):
    """私聊快车道不可用（熔断中或池耗尽）。"""


# 聊天和记忆整理分开熔断：慢的 JSON 整理不能把正常聊天也熔断掉。
# structured 与普通 chat 共用 chat 熔断，避免结构化失败把聊天也冻住。
_CIRCUITS = {
    "chat": {"fail": 0, "open_until": 0.0},
    "json": {"fail": 0, "open_until": 0.0},
}
_lock = asyncio.Lock()


def _cfg_reply(cfg: dict) -> dict:
    return cfg.get("reply") or {}


def clean_reply(text: Any, cfg: dict) -> str:
    """去掉模型可能带出的前缀/代码围栏；不做内容改写。"""
    value = str(text or "").strip()
    value = _FENCE.sub("", value).strip()
    value = _PREFIX.sub("", value)
    value = _ROLE_PREFIX.sub("", value)
    value = value.strip().strip('"').strip("“”").strip()
    max_chars = int(_cfg_reply(cfg).get("max_reply_chars", 600) or 600)
    if len(value) > max_chars:
        value = value[:max_chars].rstrip() + "…"
    return value


async def _call(
    messages: list[dict],
    cfg: dict,
    *,
    max_tokens: Optional[int],
    timeout: Optional[float],
    budget: Optional[float],
    label: str,
    circuit: str,
    clean: bool,
) -> str:
    state = _CIRCUITS.setdefault(circuit, {"fail": 0, "open_until": 0.0})
    now = time.monotonic()
    if now < float(state.get("open_until", 0.0)):
        raise LLMUnavailable("模型快车道熔断中")

    reply_cfg = _cfg_reply(cfg)
    timeout = float(timeout or reply_cfg.get("llm_timeout_seconds", 30.0) or 30.0)
    budget = float(budget or reply_cfg.get("llm_total_budget_seconds", 45.0) or 45.0)
    max_tokens = int(max_tokens or reply_cfg.get("max_tokens", 600) or 600)

    try:
        text, _model = await llm_pool.chat(
            messages,
            label=label,
            max_tokens=max_tokens,
            timeout=timeout,
            budget=budget,
        )
    except Exception:
        async with _lock:
            state["fail"] = int(state.get("fail", 0)) + 1
            if state["fail"] >= 3:
                state["open_until"] = time.monotonic() + 120.0
        raise
    else:
        async with _lock:
            state["fail"] = 0
        raw = str(text or "").strip()
        return clean_reply(raw, cfg) if clean else raw


async def chat(
    messages: list[dict],
    cfg: dict,
    *,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
    budget: Optional[float] = None,
    label: str = "private_chat",
) -> str:
    """老文本链路：返回经过 clean_reply 的文本。"""
    return await _call(
        messages, cfg,
        max_tokens=max_tokens, timeout=timeout, budget=budget,
        label=label, circuit="chat", clean=True,
    )


async def chat_raw(
    messages: list[dict],
    cfg: dict,
    *,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
    budget: Optional[float] = None,
    label: str = "private_chat_structured",
) -> str:
    """结构化链路：不截断、不去围栏，只 strip 最外层空白。"""
    return await _call(
        messages, cfg,
        max_tokens=max_tokens, timeout=timeout, budget=budget,
        label=label, circuit="chat", clean=False,
    )


async def chat_json(
    messages: list[dict],
    cfg: dict,
    *,
    max_tokens: int = 700,
    timeout: Optional[float] = None,
    budget: Optional[float] = None,
) -> Any:
    """JSON 抽取/整理专用：比聊天快车道更宽的超时（默认 60s / 90s）。"""
    mem_cfg = cfg.get("memory") or {}
    timeout = float(timeout if timeout is not None else mem_cfg.get("llm_timeout_seconds", 60.0) or 60.0)
    budget = float(budget if budget is not None else mem_cfg.get("llm_total_budget_seconds", 90.0) or 90.0)
    text = await _call(
        messages, cfg,
        max_tokens=max_tokens, timeout=timeout, budget=budget,
        label="private_chat_json", circuit="json", clean=False,
    )
    return extract_json(text)


def extract_json(text: Any) -> Any:
    """从模型输出里尽力提取 JSON；失败返回 None。"""
    raw = str(text or "").strip()
    raw = _FENCE.sub("", raw).strip()
    for candidate in (raw, _slice_between(raw, "{", "}"), _slice_between(raw, "[", "]")):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _slice_between(text: str, left: str, right: str) -> str:
    start = text.find(left)
    end = text.rfind(right)
    if start >= 0 and end > start:
        return text[start:end + 1]
    return ""


def reset_circuit() -> None:
    """测试/手动恢复用。"""
    for state in _CIRCUITS.values():
        state["fail"] = 0
        state["open_until"] = 0.0


def circuit_snapshot() -> dict:
    now = time.monotonic()
    return {
        key: {
            "fail_streak": state.get("fail", 0),
            "open_until": state.get("open_until", 0.0),
            "open": now < float(state.get("open_until", 0.0)),
        }
        for key, state in _CIRCUITS.items()
    }
