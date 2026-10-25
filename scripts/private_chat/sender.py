# -*- coding: utf-8 -*-
"""私聊发送：OneBot 11 send_private_msg + 拟人化分段/延迟。

本文件分两层：

- 旧的低层函数（send_private_text / send_humanlike / split_bubbles）保留给
  legacy 回退链路和旧文本兼容测试；
- 新链路使用 TurnSender：每用户一个发送 worker，按优先级仲裁
  （控制确认 0 > 普通回复 1 > 主动消息 2），每个气泡单独走 RPC 回执，
  成功后才 commit（写 session / 更新对话状态）。

“结果未知”（超时/断连）绝不自动重发，只记录 attempt ledger。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import random
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import config as cfgmod

_SPLIT = re.compile(r"(?<=[。！？!?；;…\n])")
_COMMA = re.compile(r"(?<=[，,、])")

_PRIORITY_CONTROL = 0
_PRIORITY_REPLY = 1
_PRIORITY_PROACTIVE = 2


class SenderError(RuntimeError):
    pass


@dataclass
class SendOutcome:
    status: str                     # sent / failed / unknown / stale
    message_id: Optional[str] = None
    error: str = ""
    retcode: Optional[int] = None
    echo: str = ""
    attempt_id: str = ""

    @property
    def sent(self) -> bool:
        return self.status == "sent"


def _plan_field(plan: Any, name: str, default: Any) -> Any:
    """兼容 dict 与 RhythmPlan dataclass。"""
    if plan is None:
        return default
    if isinstance(plan, dict):
        return plan.get(name, default)
    return getattr(plan, name, default)


# ---------- 旧的低层发送（legacy / 回退用） ----------

async def _action(ws, action: str, params: dict) -> bool:
    if ws is None:
        raise SenderError("WebSocket 未连接")
    payload = {"action": action, "params": params}
    try:
        await ws.send(json.dumps(payload, ensure_ascii=False))
        return True
    except Exception as exc:  # noqa: BLE001 - 连接错误交给上层降级
        raise SenderError(f"发送失败: {type(exc).__name__}: {exc}") from exc


async def send_private_text(ws, uid: str, text: str) -> bool:
    if not str(text or "").strip():
        return False
    return await _action(ws, "send_private_msg", {
        "user_id": int(uid),
        "message": [{"type": "text", "data": {"text": str(text)}}],
    })


async def approve_friend(ws, flag: str, remark: str = "") -> bool:
    return await _action(ws, "set_friend_add_request", {
        "flag": str(flag),
        "approve": True,
        "remark": str(remark or ""),
    })


# ---------- 分段 ----------

def _split_longest(parts: list[str], target: int) -> list[str]:
    """在最长的一段里找一个逗号/中点，继续拆，直到 target 或拆不动。"""
    while len(parts) < target:
        idx = max(range(len(parts)), key=lambda i: len(parts[i]))
        text = parts[idx]
        if len(text) < 12:
            break
        candidates = [m.end() for m in _COMMA.finditer(text)]
        cut = None
        for pos in candidates:
            if 5 <= pos <= len(text) - 5:
                cut = pos
                break
        if cut is None:
            cut = len(text) // 2
            if cut < 5 or len(text) - cut < 5:
                break
        parts[idx:idx + 1] = [text[:cut].strip(), text[cut:].strip()]
    return parts


def _merge_to_target(parts: list[str], target: int) -> list[str]:
    """把过多的小段合并成 target 组，保持顺序。"""
    if len(parts) <= target:
        return parts
    total = sum(len(p) for p in parts)
    goal = max(1, total // target)
    groups: list[str] = []
    current = ""
    for part in parts:
        if not current:
            current = part
        elif len(groups) < target - 1 and len(current) >= goal:
            groups.append(current)
            current = part
        else:
            current += part
    if current:
        groups.append(current)
    # 极端情况下仍然过多：把尾部并入最后一条
    if len(groups) > target:
        groups = groups[:target - 1] + ["".join(groups[target - 1:])]
    return groups


def split_bubbles(text: str, max_bubbles: int = 3, target_bubbles: int = 1,
                  min_chars: int = 5) -> list[str]:
    """按 target_bubbles 切成 1~max_bubbles 条。"""
    value = str(text or "").strip()
    if not value:
        return []
    max_bubbles = max(1, int(max_bubbles or 1))
    target = max(1, min(max_bubbles, int(target_bubbles or 1)))
    if target <= 1 or len(value) < max(8, target * min_chars):
        return [value]

    parts = [p.strip() for p in _SPLIT.split(value) if p.strip()]
    if len(parts) < target:
        comma_parts = [p.strip() for p in _COMMA.split(value) if p.strip()]
        if len(comma_parts) > len(parts):
            parts = comma_parts
    if len(parts) < target:
        parts = _split_longest(parts, target)
    parts = _merge_to_target(parts, target)
    bubbles = [p for p in parts if p]

    # 过短的段合并给相邻段，避免发出「嗯。」这种独立气泡
    i = 0
    while i < len(bubbles):
        if len(bubbles[i]) >= min_chars or len(bubbles) <= 1:
            i += 1
            continue
        if i > 0:
            bubbles[i - 1] = bubbles[i - 1] + bubbles[i]
            bubbles.pop(i)
        else:
            bubbles[i + 1] = bubbles[i] + bubbles[i + 1]
            bubbles.pop(i)
    return [b.strip() for b in bubbles if b.strip()][:max_bubbles] or [value]


def _typing_delay(text: str, cfg: dict) -> float:
    reply_cfg = cfg.get("reply") or {}
    speed = float(reply_cfg.get("typing_speed_chars_per_second", 8.0) or 8.0)
    cap = float(reply_cfg.get("max_typing_seconds", 8.0) or 8.0)
    base = min(cap, max(0.8, len(text) / max(1.0, speed)))
    return base + random.uniform(0.2, 0.9)


def _plan_delay(plan: Optional[dict], index: int, bubble: str, cfg: dict) -> float:
    if not plan:
        delay = _typing_delay(bubble, cfg)
        return max(0.6, delay * 0.7) if index == 0 else delay
    if index == 0:
        base = float(_plan_field(plan, "first_delay_ms", 600) or 0) / 1000.0
    else:
        base = float(_plan_field(plan, "gap_ms", 1100) or 0) / 1000.0
    return max(0.0, base + random.uniform(-0.1, 0.25))


async def send_humanlike(ws, uid: str, text: str, cfg: dict, logger=None,
                         plan: Optional[dict] = None) -> int:
    """legacy：按 reply_plan 分段发送，返回实际发出的气泡数。"""
    reply_cfg = cfg.get("reply") or {}
    max_bubbles = int(reply_cfg.get("max_bubbles", 3) or 3)
    target = int(_plan_field(plan, "target_bubbles", 1) or 1)
    bubbles = split_bubbles(text, max_bubbles, target_bubbles=target)
    sent = 0
    for index, bubble in enumerate(bubbles):
        delay = _plan_delay(plan, index, bubble, cfg)
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await send_private_text(ws, uid, bubble)
            sent += 1
        except SenderError:
            raise
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.info(f"[私聊] 分段发送失败（位置 {index + 1}）: {exc}")
            raise SenderError(str(exc)) from exc
    return sent


async def send_proactive(ws, uid: str, text: str, cfg: dict) -> bool:
    """legacy：主动消息不模拟长打字，只留一点自然延迟。"""
    await asyncio.sleep(random.uniform(0.3, 1.2))
    return await send_private_text(ws, uid, text)


# ---------- 新链路：TurnSender ----------

@dataclass
class _SendJob:
    uid: str
    text: str
    priority: int
    source: str
    turn_id: str
    bubble_index: int
    bubble_count: int
    revision: int
    target_ids: list[str]
    check_version: Optional[Callable[[], bool]]
    on_success: Optional[Callable[[SendOutcome], None]]
    future: asyncio.Future
    canceled: bool = False


class TurnSender:
    """每用户一个优先级发送 worker，保证消息顺序与成功落盘。

    - 控制确认 priority 0；普通回复 1；主动消息 2；
    - 同一用户同一时刻只有一条 action 在途；
    - 气泡间隔由调用方 sleep，控制确认可以插到尚未发送的普通气泡之前；
    - 已发出的 RPC 不会被取消：外层任务取消只是“后续气泡不再发”，
      当前 action 的结果仍会回调 on_success 落盘；
    - 超时/断连 → unknown，不重发，仅留 attempt ledger。
    """

    def __init__(self, store, rpc, cfg: dict, logger=None, ws_getter=None):
        self.store = store
        self.rpc = rpc
        self.cfg = cfg
        self.logger = logger
        self._ws_getter = ws_getter or (lambda: None)
        self._queues: dict[str, asyncio.PriorityQueue] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._seq = itertools.count()
        self._closed = False

    # ---- worker ----
    def _queue(self, uid: str) -> asyncio.PriorityQueue:
        key = str(uid)
        if key not in self._queues:
            self._queues[key] = asyncio.PriorityQueue()
            self._workers[key] = asyncio.get_running_loop().create_task(
                self._worker(key, self._queues[key])
            )
        return self._queues[key]

    def _resolve(self, job: _SendJob, outcome: SendOutcome) -> None:
        if not job.future.done():
            job.future.set_result(outcome)

    async def _worker(self, uid: str, queue: asyncio.PriorityQueue) -> None:
        while True:
            _priority, _seq, job = await queue.get()
            if not isinstance(job, _SendJob):
                continue
            if job.canceled:
                self._resolve(job, SendOutcome("stale", error="任务在发送前被取消"))
                continue
            if job.check_version is not None:
                try:
                    fresh = bool(job.check_version())
                except Exception:  # noqa: BLE001
                    fresh = False
                if not fresh:
                    self._resolve(job, SendOutcome("stale", error="会话版本已更新"))
                    continue
            try:
                outcome = await self._execute(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - worker 不能因单条异常退出
                if self.logger:
                    self.logger.info(
                        f"[私聊] 发送异常 uid={uid}: {type(exc).__name__}: {exc}"
                    )
                outcome = SendOutcome("unknown", error=f"{type(exc).__name__}: {exc}")
            if outcome.sent and job.on_success is not None:
                try:
                    job.on_success(outcome)
                except Exception as exc:  # noqa: BLE001 - 落盘失败不能影响后续发送
                    if self.logger:
                        self.logger.info(
                            f"[私聊] 发送成功但落盘失败 uid={uid}: "
                            f"{type(exc).__name__}: {exc}"
                        )
            self._resolve(job, outcome)

    async def _execute(self, job: _SendJob) -> SendOutcome:
        if self._closed:
            return SendOutcome("unknown", error="发送器已关闭")
        ws = self._ws_getter()
        if ws is None:
            return SendOutcome("unknown", error="WebSocket 未连接")
        text = str(job.text or "").strip()
        if not text:
            return SendOutcome("failed", error="空消息")
        attempt_id = "a_" + uuid.uuid4().hex
        self.store.add_attempt(job.uid, {
            "attempt_id": attempt_id,
            "echo": "",
            "turn_id": job.turn_id,
            "bubble_index": job.bubble_index,
            "bubble_count": job.bubble_count,
            "source": job.source,
            "text": text,
            "attempted_at": cfgmod.now_iso(),
            "status": "pending",
            "msg_id": None,
            "revision": job.revision,
        })
        timeout = float(
            (self.cfg.get("reply") or {}).get("send_ack_timeout_seconds", 5.0) or 5.0
        )
        if self.rpc is not None:
            raw = await self.rpc.call_outcome(
                ws,
                "send_private_msg",
                {"user_id": int(job.uid),
                 "message": [{"type": "text", "data": {"text": text}}]},
                timeout=timeout,
            )
            status = {"ok": "sent", "failed": "failed", "unknown": "unknown"}.get(
                raw.status, "unknown"
            )
            outcome = SendOutcome(
                status,
                message_id=raw.message_id,
                error=raw.error,
                retcode=raw.retcode,
                echo=raw.echo,
                attempt_id=attempt_id,
            )
        else:
            # 测试/离线路径：没有 RPC 时退回 fire-and-forget，但仍走统一 commit。
            try:
                await send_private_text(ws, job.uid, text)
                outcome = SendOutcome("sent", attempt_id=attempt_id)
            except Exception as exc:  # noqa: BLE001
                outcome = SendOutcome(
                    "failed", error=f"{type(exc).__name__}: {exc}", attempt_id=attempt_id
                )
        self.store.finish_attempt(
            job.uid,
            attempt_id,
            status=("sent" if outcome.sent else outcome.status),
            message_id=outcome.message_id,
            error=outcome.error,
            echo=outcome.echo,
        )
        return outcome

    async def send_bubble(self, uid: str, text: str, *, priority: int, source: str,
                          turn_id: str, bubble_index: int, bubble_count: int,
                          revision: int = 0, target_ids: Optional[list[str]] = None,
                          check_version: Optional[Callable[[], bool]] = None,
                          on_success: Optional[Callable[[SendOutcome], None]] = None
                          ) -> SendOutcome:
        if self._closed:
            return SendOutcome("unknown", error="发送器已关闭")
        queue = self._queue(uid)
        job = _SendJob(
            uid=str(uid),
            text=str(text),
            priority=int(priority),
            source=str(source),
            turn_id=str(turn_id),
            bubble_index=int(bubble_index),
            bubble_count=int(bubble_count),
            revision=int(revision),
            target_ids=[str(x) for x in (target_ids or [])],
            check_version=check_version,
            on_success=on_success,
            future=asyncio.get_running_loop().create_future(),
        )
        await queue.put((job.priority, next(self._seq), job))
        try:
            return await job.future
        except asyncio.CancelledError:
            job.canceled = True
            raise

    async def send_reply(self, uid: str, bubbles: list[str], *, source: str,
                         turn_id: str, revision: int, target_ids: list[str],
                         delays: list[float], check_version: Callable[[], bool],
                         on_success: Callable[[SendOutcome], None],
                         ) -> list[SendOutcome]:
        """依次发送普通回复气泡；失败/过期即停，返回已尝试的结果。"""
        outcomes: list[SendOutcome] = []
        count = len(bubbles)
        for index, bubble in enumerate(bubbles):
            delay = float(delays[index]) if index < len(delays) else 0.0
            if delay > 0:
                await asyncio.sleep(max(0.0, delay))
            if not check_version():
                break
            outcome = await self.send_bubble(
                uid,
                bubble,
                priority=_PRIORITY_REPLY,
                source=source,
                turn_id=turn_id,
                bubble_index=index,
                bubble_count=count,
                revision=revision,
                target_ids=target_ids,
                check_version=check_version,
                on_success=on_success,
            )
            outcomes.append(outcome)
            if not outcome.sent:
                break
        return outcomes

    async def send_control(self, uid: str, text: str, *, turn_id: str,
                           command_msg_id: Optional[str] = None,
                           on_success: Callable[[SendOutcome], None],
                           ) -> SendOutcome:
        target = [str(command_msg_id)] if command_msg_id not in (None, "") else []
        return await self.send_bubble(
            uid,
            text,
            priority=_PRIORITY_CONTROL,
            source="control",
            turn_id=turn_id,
            bubble_index=0,
            bubble_count=1,
            revision=0,
            target_ids=target,
            on_success=on_success,
        )

    async def send_proactive(self, uid: str, text: str, *, turn_id: str,
                             on_success: Callable[[SendOutcome], None],
                             check_version: Optional[Callable[[], bool]] = None,
                             ) -> SendOutcome:
        return await self.send_bubble(
            uid,
            text,
            priority=_PRIORITY_PROACTIVE,
            source="proactive",
            turn_id=turn_id,
            bubble_index=0,
            bubble_count=1,
            revision=0,
            target_ids=[],
            check_version=check_version,
            on_success=on_success,
        )

    async def stop(self) -> None:
        self._closed = True
        tasks = list(self._workers.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
