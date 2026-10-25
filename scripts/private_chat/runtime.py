# -*- coding: utf-8 -*-
"""ConversationRuntime：每个用户一条有序输入队列 + 会话版本 + 回复/主动消息仲裁。

职责：
- 接受已去重、已原始落库的事件；
- 每接受一条普通用户消息 revision += 1，并取消旧回复/主动生成；
- 顺序执行图片 enrichment、风格统计、活动抽取；
- 防抖后冻结目标消息，调用 BehaviorPolicy / ResponseComposer / TurnSender；
- 普通回复、主动消息、控制确认都通过 TurnSender 发送；
- 断连、停止时管理自己的 worker 和任务。

它不解析 OneBot 原始消息、不拼 persona prompt、不直接拆气泡、
不直接修改长期记忆；这些通过 chat 注入的回调完成。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import behavior as behavior_mod
from . import sender as sender_mod
from . import style as style_mod
from .dialogue_state import DialogueState

_MAX_TARGET_MESSAGES = 8
_MAX_TARGET_CHARS = 1200


@dataclass
class NormalEvent:
    uid: str
    msg_id: Optional[str]
    text: str
    original_text: str
    images: list[dict] = field(default_factory=list)
    raw_message: str = ""
    ts: str = ""
    revision: int = 0
    burst_n: int = 1


@dataclass
class ControlEvent:
    uid: str
    text: str
    reply_to_msg_id: Optional[str] = None
    command_msg_id: Optional[str] = None
    source: str = "control"


class ConversationRuntime:
    def __init__(self, chat, uid: str):
        self.chat = chat
        self.uid = str(uid)
        self.cfg = chat.cfg
        self.store = chat.store
        self.logger = chat.logger
        self.sender = chat.sender
        self.composer = chat.composer
        self.revision = 0
        self.burst_n = 1
        self._input_q: asyncio.Queue = asyncio.Queue()
        self._control_q: asyncio.Queue = asyncio.Queue()
        self._wake = asyncio.Event()
        self._input_task: Optional[asyncio.Task] = None
        self._control_task: Optional[asyncio.Task] = None
        self._reply_task: Optional[asyncio.Task] = None
        self._proactive_task: Optional[asyncio.Task] = None
        self._suspended = False
        self._closed = False
        self._started = False
        self.dialogue = self._load_dialogue()
        self._start_workers()

    # ---------- 状态加载 ----------
    def _load_dialogue(self) -> DialogueState:
        try:
            data = self.store.load_dialogue_state(self.uid)
        except Exception:  # noqa: BLE001
            data = {}
        if data:
            state = DialogueState.from_dict(data)
        else:
            try:
                turns = self.store.recent_turns(self.uid, 300)
            except Exception:  # noqa: BLE001
                turns = []
            state = DialogueState.rebuild_from_turns(turns, max_turns=300)
        # A12：重启不自动补答，pending 一律清空。
        state.pending_user_msg_ids = []
        return state

    def save_dialogue(self) -> None:
        try:
            self.store.save_dialogue_state(self.uid, self.dialogue.to_dict())
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.info(f"[私聊] dialogue_state 保存失败: {type(exc).__name__}: {exc}")

    # ---------- 生命周期 ----------
    def _start_workers(self) -> None:
        if self._started or self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._started = True
        self._input_task = loop.create_task(self._input_loop())
        self._control_task = loop.create_task(self._control_loop())

    def on_connected(self) -> None:
        self._suspended = False
        self._start_workers()

    def on_disconnected(self) -> None:
        """断连：取消未发送回复，清空未答 pending，不自动补答。"""
        self._suspended = True
        self._cancel_reply()
        self._cancel_proactive()
        self._drain_queue(self._input_q)
        self._drain_queue(self._control_q)
        self.dialogue.pending_user_msg_ids = []
        self.save_dialogue()

    async def stop(self) -> None:
        self._closed = True
        self._cancel_reply()
        self._cancel_proactive()
        tasks = [x for x in (self._input_task, self._control_task) if x is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.save_dialogue()

    def _drain_queue(self, queue: asyncio.Queue) -> None:
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def _cancel_reply(self) -> None:
        task = self._reply_task
        self._reply_task = None
        if task is not None and not task.done():
            task.cancel()

    def _cancel_proactive(self) -> None:
        task = self._proactive_task
        self._proactive_task = None
        if task is not None and not task.done():
            task.cancel()

    # ---------- 事件入口 ----------
    def submit_message(self, event: NormalEvent) -> None:
        if self._closed or self._suspended:
            return
        self._start_workers()
        self.revision += 1
        event.revision = self.revision
        self._cancel_reply()
        self._cancel_proactive()
        try:
            recent = self._recent_user_texts(include_id=event.msg_id)
        except Exception:  # noqa: BLE001
            recent = [event.text]
        self.dialogue.accept_user(
            event.msg_id, event.text, event.ts, recent_user_texts=recent,
        )
        self._input_q.put_nowait(event)
        self._wake.set()

    def submit_pause(self, event: ControlEvent) -> None:
        """pause：立即让旧版本失效，然后排队确认。"""
        if self._closed or self._suspended:
            return
        self.revision += 1
        self._cancel_reply()
        self._cancel_proactive()
        self.submit_control(event)

    def submit_control(self, event: ControlEvent) -> None:
        if self._closed or self._suspended:
            return
        self._start_workers()
        self._control_q.put_nowait(event)

    def _recent_user_texts(self, include_id: Optional[str] = None, limit: int = 3) -> list[str]:
        turns = self.store.recent_turns(self.uid, 12)
        texts = [str(x.get("text") or "") for x in turns if x.get("role") == "user"]
        return texts[-limit:]

    # ---------- 输入 worker ----------
    async def _input_loop(self) -> None:
        while not self._closed:
            try:
                event = await self._input_q.get()
            except asyncio.CancelledError:
                return
            if self._suspended or self._closed:
                continue
            try:
                await self._prepare(event)
                while not self._input_q.empty() and not self._suspended:
                    nxt = self._input_q.get_nowait()
                    await self._prepare(nxt)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                if self.logger:
                    self.logger.info(
                        f"[私聊] 输入预处理异常 uid={self.uid}: {type(exc).__name__}: {exc}"
                    )
            if self._suspended or self._closed:
                continue
            # 防抖：期间来新消息则重新计时；新消息会由 submit_message 唤醒。
            # clear 之后再检查一次队列，避免“事件刚好在 clear 前入队”导致
            # 跳过它的图片/风格预处理就直接回复。
            delay = float(self.chat.debounce_delay())
            self._wake.clear()
            if not self._input_q.empty():
                continue
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
                continue
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return
            if self._suspended or self._closed or not self._input_q.empty():
                continue
            self._start_reply()

    async def _prepare(self, event: NormalEvent) -> None:
        try:
            await self.chat.prepare_inbound(event)
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.info(
                    f"[私聊] 图片预处理异常 uid={self.uid}: {type(exc).__name__}: {exc}"
                )
        # burst 在 on_message 按到达时间统计，避免被 15s 图片预处理窗口拉偏。
        self.burst_n = max(self.burst_n, int(event.burst_n or 1))
        try:
            self.chat.schedule_activity_extract(self.uid, event.original_text)
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.info(f"[活动] 调度失败（已忽略）: {exc}")

    # ---------- 普通回复 ----------
    def _start_reply(self) -> None:
        if self._closed or self._suspended:
            return
        if self._reply_task is not None and not self._reply_task.done():
            return
        target = self._select_targets()
        if not target:
            return
        rev = self.revision
        turn_id = self._new_turn_id()
        self._reply_task = asyncio.get_running_loop().create_task(
            self._reply_run(rev, turn_id, target)
        )

    def _new_turn_id(self) -> str:
        return f"{self.uid}-{self.revision}-{uuid.uuid4().hex[:8]}"

    def _select_targets(self) -> list[dict]:
        pending = list(self.dialogue.pending_user_msg_ids)
        if not pending:
            return []
        turns = self.store.recent_turns(self.uid, 300)
        by_id: dict[str, dict] = {}
        for turn in turns:
            if turn.get("role") != "user" or turn.get("msg_id") is None:
                continue
            by_id[str(turn.get("msg_id"))] = turn
        ordered = [by_id[x] for x in pending if x in by_id]
        # 超过 8 条或 1200 字时只保留最新可处理批次；更早的从 pending 移除。
        while ordered and (
            len(ordered) > _MAX_TARGET_MESSAGES
            or sum(len(str(x.get("text") or "")) for x in ordered) > _MAX_TARGET_CHARS
        ):
            dropped = ordered.pop(0)
            self.dialogue.mark_caught([dropped.get("msg_id")])
        if len(ordered) != len(pending):
            # 不存在的 pending id（历史被清/重启）一并移除
            valid = {str(x.get("msg_id")) for x in ordered}
            self.dialogue.pending_user_msg_ids = [
                x for x in self.dialogue.pending_user_msg_ids if x in valid
            ]
        return ordered

    def _relationship_stage(self) -> str:
        try:
            return str((self.store.profile(self.uid).get("relationship") or {}).get("stage") or "")
        except Exception:  # noqa: BLE001
            return ""

    async def _reply_run(self, rev: int, turn_id: str, target: list[dict]) -> None:
        memory_state = {"scheduled": False}
        try:
            if rev != self.revision or self._suspended:
                return
            target_ids = [str(x.get("msg_id")) for x in target if x.get("msg_id") is not None]
            last_text = str(target[-1].get("text") or "") if target else ""
            decision = behavior_mod.decide(
                self.cfg,
                self.dialogue,
                target,
                queue_busy=not self._input_q.empty(),
                image_pending=False,
                burst_n=self.burst_n,
                text=last_text,
                relationship_stage=self._relationship_stage(),
            )
            if decision.action != "respond":
                return
            rhythm = style_mod.plan(self.cfg, self.store, self.uid, self.burst_n, last_text)
            self.burst_n = 1
            try:
                self.chat.reset_user_burst(self.uid)
            except Exception:  # noqa: BLE001
                pass
            self.dialogue.record_behavior(decision, turn_id)
            structured = str(
                (self.cfg.get("reply") or {}).get("pipeline", "structured") or "structured"
            ).lower() == "structured"
            max_turns = int((self.cfg.get("reply") or {}).get("max_context_turns", 24) or 24)
            context = {
                "uid": self.uid,
                "revision": rev,
                "turn_id": turn_id,
                "target_msg_ids": target_ids,
                "target_messages": target,
                "decision": decision,
                "rhythm": rhythm,
                "structured": structured,
                "query": last_text,
                # 快照而非引用：搜索/组装期间新消息或控制指令不会改变本轮 prompt。
                "dialogue": DialogueState.from_dict(self.dialogue.to_dict()),
                "turns": self.store.recent_turns(self.uid, max_turns),
            }
            if rev != self.revision:
                return
            messages = await self.chat.build_reply_messages(context)
            if rev != self.revision or self._suspended:
                return
            reply = await self.composer.compose(
                messages,
                structured=structured,
                decision=decision,
                rhythm=rhythm,
                corrections=self.chat.get_corrections(),
                fallback_text=self.chat.fallback_reply(self.uid),
            )
            if rev != self.revision or self._suspended:
                return
            bubbles = [x for x in reply.bubbles if str(x.get("text") or "").strip()]
            if not bubbles:
                return
            delays = [
                max(0.0, sender_mod._plan_delay(
                    {"first_delay_ms": rhythm.first_delay_ms, "gap_ms": rhythm.gap_ms},
                    index, bubble, self.cfg,
                ))
                for index, bubble in enumerate(bubbles)
            ]
            # 每次发送成功立即落盘；旧版本只能清自己的 target，不覆盖新状态。
            state = {"first": True, "committed": False}

            def _commit_bubble_for(index: int):
                def _inner(outcome) -> None:
                    if index >= len(bubbles):
                        return
                    act = str(bubbles[index].get("act") or "chat")
                    self.store.add_turn(
                        self.uid,
                        "assistant",
                        bubbles[index]["text"],
                        msg_id=outcome.message_id,
                        meta={
                            "turn_id": turn_id,
                            "bubble_index": index,
                            "bubble_count": len(bubbles),
                            "reply_to_msg_ids": target_ids,
                            "source": "normal",
                            "act": act,
                            "revision": rev,
                            "delivery_status": "sent",
                        },
                    )
                    state["committed"] = True
                    if rev == self.revision:
                        if state["first"]:
                            self.dialogue.mark_caught(target_ids)
                            self.dialogue.pending_question = ""
                            state["first"] = False
                        self.dialogue.record_bubble(act, turn_id)
                    else:
                        # 旧版本：只清自己的 target ids，不覆盖新话题/问题/行为。
                        self.dialogue.mark_caught(target_ids)
                    self.save_dialogue()
                    # 首个确认气泡就触发一次后台记忆抽取；即使外层任务随后被取消，
                    # 已落盘的气泡也不会漏掉 on_turn_finished。
                    if not memory_state["scheduled"]:
                        memory_state["scheduled"] = True
                        try:
                            self.chat.on_turn_finished(
                                self.uid,
                                [str(x.get("msg_id")) for x in target],
                            )
                        except Exception:  # noqa: BLE001
                            pass
                return _inner

            # 每个气泡用各自的 commit 回调；sender 按顺序发送，索引对应正确。
            outcomes: list = []
            for index, bubble in enumerate(bubbles):
                delay = delays[index] if index < len(delays) else 0.0
                if delay > 0:
                    await asyncio.sleep(delay)
                if rev != self.revision or self._suspended:
                    break
                outcome = await self.sender.send_bubble(
                    self.uid,
                    bubble["text"],
                    priority=sender_mod._PRIORITY_REPLY,
                    source="normal",
                    turn_id=turn_id,
                    bubble_index=index,
                    bubble_count=len(bubbles),
                    revision=rev,
                    target_ids=target_ids,
                    check_version=lambda: rev == self.revision,
                    on_success=_commit_bubble_for(index),
                )
                outcomes.append(outcome)
                if not outcome.sent:
                    break
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.info(
                    f"[私聊] 回复失败 uid={self.uid}: {type(exc).__name__}: {exc}"
                )
        finally:
            self.save_dialogue()
            if self._reply_task is asyncio.current_task():
                self._reply_task = None

    # ---------- 控制确认 ----------
    async def _control_loop(self) -> None:
        while not self._closed:
            try:
                event = await self._control_q.get()
            except asyncio.CancelledError:
                return
            if self._suspended or self._closed:
                continue
            try:
                await self._send_control(event)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                if self.logger:
                    self.logger.info(
                        f"[私聊] 控制确认失败 uid={self.uid}: {type(exc).__name__}: {exc}"
                    )

    async def _send_control(self, event: ControlEvent) -> None:
        turn_id = f"{self.uid}-ctl-{uuid.uuid4().hex[:8]}"

        def _commit(outcome) -> None:
            self.store.add_turn(
                self.uid,
                "assistant",
                event.text,
                msg_id=outcome.message_id,
                meta={
                    "turn_id": turn_id,
                    "bubble_index": 0,
                    "bubble_count": 1,
                    "reply_to_msg_ids": [str(event.command_msg_id)] if event.command_msg_id else [],
                    "source": event.source or "control",
                    "act": "chat",
                    "revision": self.revision,
                    "delivery_status": "sent",
                },
            )

        await self.sender.send_control(
            self.uid, event.text, turn_id=turn_id,
            command_msg_id=event.command_msg_id, on_success=_commit,
        )

    # ---------- 主动消息 ----------
    def proactive_busy(self) -> bool:
        if self._closed or self._suspended:
            return True
        if not self._input_q.empty():
            return True
        # 防抖/图片预处理/等待回复期间，输入队列可能已经空但消息还没被接住。
        if self.dialogue.pending_user_msg_ids:
            return True
        task = self._reply_task
        return task is not None and not task.done()

    async def request_proactive(self, *, generate: Callable[[], Any],
                                validate: Callable[[str], bool],
                                on_sent: Callable[[str], None],
                                on_failed: Optional[Callable[[str, str], None]] = None
                                ) -> Optional[Any]:
        """提交一次主动消息；返回 SendOutcome 或 None（被跳过/取消）。"""
        if self.proactive_busy():
            return None
        if self._proactive_task is not None and not self._proactive_task.done():
            return None
        rev = self.revision
        task = asyncio.get_running_loop().create_task(
            self._proactive_run(rev, generate, validate, on_sent, on_failed)
        )
        self._proactive_task = task
        try:
            # shield 让“外层 stop 被取消”和“内部主动任务被插话取消”
            # 保持可区分；直接 await task 会把两种取消传播混在一起。
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            if self._proactive_task is task:
                self._proactive_task = None

    async def _proactive_run(self, rev: int, generate, validate, on_sent, on_failed):
        try:
            text = await generate()
            if rev != self.revision or self._suspended:
                return None
            text = str(text or "").strip()
            if not text or not validate(text):
                return None
            if rev != self.revision or self._suspended:
                return None
            turn_id = f"{self.uid}-pro-{uuid.uuid4().hex[:8]}"

            def _commit(outcome) -> None:
                self.store.add_turn(
                    self.uid, "assistant", text, msg_id=outcome.message_id,
                    proactive=True,
                    meta={
                        "turn_id": turn_id,
                        "bubble_index": 0,
                        "bubble_count": 1,
                        "reply_to_msg_ids": [],
                        "source": "proactive",
                        "act": "chat",
                        "revision": rev,
                        "delivery_status": "sent",
                    },
                )
                try:
                    on_sent(text)
                except Exception:  # noqa: BLE001
                    pass

            outcome = await self.sender.send_proactive(
                self.uid, text, turn_id=turn_id, on_success=_commit,
                check_version=lambda: rev == self.revision,
            )
            if outcome.status == "stale":
                return outcome
            if not outcome.sent and on_failed is not None:
                try:
                    on_failed(text, outcome.status)
                except Exception:  # noqa: BLE001
                    pass
            return outcome
        except asyncio.CancelledError:
            return None

    # ---------- 调试快照 ----------
    def snapshot(self) -> dict:
        return {
            "uid": self.uid,
            "revision": self.revision,
            "suspended": self._suspended,
            "pending": list(self.dialogue.pending_user_msg_ids),
            "dialogue": self.dialogue.to_dict(),
        }


__all__ = ["ControlEvent", "ConversationRuntime", "NormalEvent"]
