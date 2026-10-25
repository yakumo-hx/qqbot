# -*- coding: utf-8 -*-
"""Legacy 普通回复链路（仅在 reply.pipeline="legacy" 时使用）。

这是重构前的行为，保留一个版本用于回退：
- 图片识图在 on_message 里 await；
- 防抖任务 + 单值 burst/原始文本；
- 整段回复发送完再写一条 assistant turn；
- 没有 turn_id、逐气泡回执、版本检查和 attempt ledger。

新 runtime / structured 路径不得 import 本模块。
"""

from __future__ import annotations

import asyncio
import random
from typing import Optional

from . import corrections as corrections_mod
from . import image_vision
from . import persona as persona_mod
from . import search as search_mod
from . import sender as sender_mod
from . import style as style_mod


class LegacyPipeline:
    def __init__(self, chat):
        self.chat = chat
        self.cfg = chat.cfg
        self.store = chat.store
        self.logger = chat.logger
        self._pending: dict[str, asyncio.Task] = {}
        self._uid_locks: dict[str, asyncio.Lock] = {}
        self._burst_n: dict[str, int] = {}
        self._turn_original_text: dict[str, str] = {}

    async def on_normal(self, ws, data: dict, text: str, images: list[dict],
                        original_text: str, msg_id) -> None:
        """legacy 的普通消息处理：识图 → 落库 → 风格/活动 → 防抖回复。"""
        raw_message = str(data.get("raw_message") or "")
        if images or image_vision.should_attempt(text, images, raw_message):
            try:
                result = await image_vision.describe_turn(
                    str(data.get("user_id") or ""), text, images, raw_message,
                    self.cfg, self.logger,
                )
            except Exception as exc:  # noqa: BLE001
                result = None
                self.logger.info(f"[图片] 识图入口异常（已忽略）: {type(exc).__name__}: {exc}")
            if result:
                text = image_vision.enrich_text(
                    text, result.get("desc", ""), kind=result.get("kind", "image")
                )
            elif images:
                image_cfg = self.cfg.get("image") or {}
                kind = image_vision.kind_of(images)
                recognize = bool(image_cfg.get("recognize_stickers", True))
                if image_cfg.get("enabled", True) and image_cfg.get("vision", True):
                    if kind != "sticker" or recognize:
                        text = image_vision.mark_unavailable(text, images)
        self.store.add_turn(str(data.get("user_id") or ""), "user", text, msg_id=msg_id)
        self.chat.log_received(uid=str(data.get("user_id") or ""), text=original_text, images=images)
        uid = str(data.get("user_id") or "")
        self._turn_original_text[uid] = original_text
        self._burst_n[uid] = self.chat.track_user_message(uid, original_text)
        self.chat.schedule_activity_extract(uid, original_text)
        self.schedule_reply(uid)

    def schedule_reply(self, uid: str) -> None:
        old = self._pending.pop(uid, None)
        if old and not old.done():
            old.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        delay = float(self.chat.debounce_delay())
        task = loop.create_task(self._reply_later(uid, delay))
        self._pending[uid] = task

    async def _reply_later(self, uid: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self.reply_now(uid)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            self.logger.info(f"[私聊] legacy 回复任务异常 uid={uid}: {type(exc).__name__}: {exc}")

    def _lock(self, uid: str) -> asyncio.Lock:
        if uid not in self._uid_locks:
            self._uid_locks[uid] = asyncio.Lock()
        return self._uid_locks[uid]

    async def reply_now(self, uid: str) -> None:
        async with self._lock(uid):
            ws = self.chat._ws
            if ws is None:
                return
            turns = self.store.recent_turns(uid, 1)
            if not turns or turns[-1].get("role") != "user":
                return
            query = str(turns[-1].get("text") or "")
            burst_n = self._burst_n.pop(uid, 1)
            style_text = self._turn_original_text.pop(uid, query)
            plan = style_mod.plan(self.cfg, self.store, uid, burst_n, style_text)
            try:
                search_payload = await search_mod.maybe_search(query, uid, self.cfg, self.logger)
            except Exception as exc:  # noqa: BLE001
                search_payload = None
                self.logger.info(f"[搜索] 入口异常（已忽略）: {type(exc).__name__}: {exc}")
            try:
                activity_text = self.chat.activity.format_prompt(uid)
            except Exception:  # noqa: BLE001
                activity_text = ""
            messages = persona_mod.build_messages(
                self.cfg, self.store, uid, query, plan=plan,
                search=search_payload, activity=activity_text,
            )
            reply = ""
            try:
                reply = await self.chat._llm_fn(messages, self.cfg)
            except Exception as exc:  # noqa: BLE001
                self.logger.info(f"[私聊] 模型不可用（{type(exc).__name__}），走降级")
                if (self.cfg.get("reply") or {}).get("fallback_on_error", True):
                    reply = self.chat.fallback_reply(uid)
            reply = str(reply or "").strip()
            if not reply:
                if not (self.cfg.get("reply") or {}).get("fallback_on_error", True):
                    return
                reply = self.chat.fallback_reply(uid)
            fixed = corrections_mod.apply(reply, persona_mod.load_corrections(self.cfg))
            if fixed != reply:
                self.logger.info("[私聊] 回复已按纠偏层修正")
                reply = fixed or self.chat.fallback_reply(uid)
            try:
                sent = await sender_mod.send_humanlike(
                    ws, uid, reply, self.cfg, self.logger, plan=plan
                )
                self.logger.info(f"[私聊] legacy 回复 {uid}：{sent} 段（{len(reply)} 字）")
            except Exception as exc:  # noqa: BLE001
                self.logger.info(f"[私聊] legacy 发送失败 uid={uid}: {exc}")
                return
            self.store.add_turn(uid, "assistant", reply)
            self.store.reset_unanswered(uid)
            self.chat.maybe_extract(uid)

    async def stop(self) -> None:
        tasks = [task for task in self._pending.values() if task is not None]
        self._pending.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
