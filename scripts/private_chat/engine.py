# -*- coding: utf-8 -*-
"""私聊陪伴版 facade / 后台服务协调器。

重构后的分工：
- on_message：快速校验、去重、控制指令分类、原始用户消息落库、入队；
- ConversationRuntime：每用户有序输入、revision、防抖、取消、图片 enrichment、
  普通回复与主动消息仲裁；
- ResponseComposer / TurnSender：一次模型调用、逐气泡回执与落盘；
- engine_legacy：仅在 reply.pipeline="legacy" 时使用的旧回复链。

群聊入口 reverse_bot.py 完全不受影响。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from . import activity as activity_mod
from . import config as cfgmod
from . import corrections as corrections_mod
from . import image_vision
from . import llm as llm_mod
from . import persona as persona_mod
from . import search as search_mod
from . import sender as sender_mod
from . import style as style_mod
from .engine_legacy import LegacyPipeline
from .response import ResponseComposer
from .runtime import ControlEvent, ConversationRuntime, NormalEvent
from .sender import TurnSender
from .store import PrivateStore

_FALLBACK_LINES = [
    "……刚刚走神了，你再说一遍？",
    "嗯，我在听。",
    "等一下，我回个神。",
    "你突然这么说，我有点没接住。",
]


def parse_message(data: dict) -> tuple[str, list[dict]]:
    """把 OneBot 私聊消息转成 (文本, 图片元数据列表)。"""
    images = image_vision.parse_images(data)
    message = data.get("message")
    if isinstance(message, list):
        parts: list[str] = []
        for seg in message:
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type")
            seg_data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            if seg_type == "text":
                parts.append(str(seg_data.get("text") or ""))
            elif seg_type == "face":
                parts.append("[表情]")
            elif seg_type == "image":
                parts.append("[图片]")
            elif seg_type == "at":
                qq = seg_data.get("qq")
                parts.append(f"@{qq}" if qq else "@某人")
            elif seg_type == "reply":
                parts.append("[引用]")
        text = "".join(parts).strip()
        if text:
            return text, images
    raw = str(data.get("raw_message") or "")
    raw = re.sub(r"\[CQ:[^\]]*\]", "", raw).strip()
    return raw, images


def extract_text(data: dict) -> str:
    """兼容旧调用：只要文本；图片仍保留 [图片] 占位。"""
    return parse_message(data)[0]


def _is_allowed(uid: str, cfg: dict) -> bool:
    allowed = cfg.get("allowed_users") or []
    if not isinstance(allowed, (list, tuple)):
        return False
    return str(uid) in {str(x) for x in allowed}


class PrivateChat:
    def __init__(self, logger, cfg_path: Optional[str] = None, cfg: Optional[dict] = None,
                 store: Optional[PrivateStore] = None,
                 llm_fn: Optional[Callable[[list[dict], dict], Awaitable[str]]] = None,
                 json_fn: Optional[Callable[[list[dict], dict], Awaitable[Any]]] = None,
                 rpc=None):
        self.logger = logger
        self.cfg = cfg if isinstance(cfg, dict) else cfgmod.load_config(cfg_path)
        self.store = store or PrivateStore(self.cfg, logger)
        self.activity = activity_mod.ActivityStore(self.store, self.cfg, logger)
        self._llm_fn = llm_fn or self._default_llm
        self._json_fn = json_fn or self._default_json
        self._rpc = rpc
        self._ws = None
        self._seen: set[str] = set()
        self._seen_order: list[str] = []
        self._tasks: list[asyncio.Task] = []
        self._extract_locks: dict[str, asyncio.Lock] = {}
        self._user_times: dict[str, Any] = {}
        self._history_last: dict[str, float] = {}
        self._activity_turns: dict[str, int] = {}
        self._activity_last: dict[str, float] = {}
        self._activity_locks: dict[str, asyncio.Lock] = {}
        self._runtimes: dict[str, ConversationRuntime] = {}
        self._started = False
        self._stopping = False
        self._created_at = time.time()
        self._last_extract_error = ""
        self._last_reflection_error = ""
        # 发送层：production 一定有 rpc；测试/离线没传 rpc 时 TurnSender 退回
        # fire-and-forget（仍走统一 commit，但没有平台 msg_id）。
        self.sender = TurnSender(
            self.store, rpc, self.cfg, logger, ws_getter=lambda: self._ws,
        )
        # 测试注入 llm_fn 时，structured 路径也用它；生产用 chat_raw。
        self.composer = ResponseComposer(
            self.cfg, logger, llm_fn=llm_fn, raw_fn=llm_fn,
        )
        self._legacy = LegacyPipeline(self)

    # ---------- 模型 ----------
    async def _default_llm(self, messages: list[dict], cfg: dict) -> str:
        return await llm_mod.chat(messages, cfg)

    async def _default_json(self, messages: list[dict], cfg: dict) -> Any:
        return await llm_mod.chat_json(messages, cfg)

    # ---------- 连接生命周期 ----------
    def on_connected(self, ws) -> None:
        self._ws = ws
        self.logger.info(
            f"[私聊] NapCat 已连接，端口 {self.cfg.get('port')}；"
            f"服务对象={self.cfg.get('allowed_users')}；"
            f"角色={self.cfg.get('character') or cfgmod.get_active_character()}"
        )
        self.start_tasks()
        for uid in self._known_uids():
            self.get_runtime(uid).on_connected()
        hcfg = self.cfg.get("history") or {}
        if hcfg.get("enabled", True) and hcfg.get("on_connect", True) and self._rpc is not None:
            try:
                self.spawn(self._bootstrap_history(ws))
            except RuntimeError:
                pass

    async def _bootstrap_history(self, ws) -> None:
        """连接后拉取最近几条好友历史，补上连上之前/重启期间的上下文。"""
        hcfg = self.cfg.get("history") or {}
        if not hcfg.get("enabled", True) or not hcfg.get("on_connect", True):
            return
        if self._rpc is None:
            return
        try:
            count = max(1, min(50, int(hcfg.get("count", 20) or 20)))
        except (TypeError, ValueError):
            count = 20
        interval_raw = hcfg.get("min_interval_seconds", 60)
        try:
            interval = float(60 if interval_raw is None else interval_raw)
        except (TypeError, ValueError):
            interval = 60.0
        for uid in self._known_uids():
            now = time.monotonic()
            if now - self._history_last.get(uid, 0.0) < interval:
                continue
            self._history_last[uid] = now
            if self._ws is not ws:
                return
            try:
                data = await self._rpc.call(
                    ws,
                    "get_friend_msg_history",
                    {
                        "user_id": uid,
                        "message_seq": "0",
                        "count": count,
                        "reverse_order": False,
                        "disable_get_url": bool(hcfg.get("disable_get_url", True)),
                        "parse_mult_msg": False,
                        "quick_reply": True,
                    },
                    timeout=10.0,
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.info(f"[历史] 拉取失败（已忽略）: {type(exc).__name__}: {exc}")
                continue
            messages = data.get("messages") if isinstance(data, dict) else data
            if not isinstance(messages, list) or not messages:
                self.logger.info(f"[历史] 无历史消息（uid={uid}）")
                continue
            records = self._history_records(uid, messages)
            added = self.store.import_history(uid, records)
            self.logger.info(
                f"[历史] 拉取 {len(messages)} 条，新增 {added} 条"
                f"（去重 {len(messages) - added} 条）"
            )

    def _history_records(self, uid: str, messages: list[dict]) -> list[dict]:
        records: list[dict] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
            sender_uid = str(sender.get("user_id") or item.get("user_id") or "")
            self_id = str(item.get("self_id") or "")
            if self_id:
                role = "assistant" if sender_uid == self_id else "user"
            else:
                role = "user" if sender_uid == uid else "assistant"
            text, images = parse_message(item)
            if not text:
                text = "[图片]" if images else ""
            if not text:
                continue
            raw_time = item.get("time")
            try:
                ts = datetime.fromtimestamp(int(raw_time)).astimezone().isoformat(timespec="seconds")
            except (TypeError, ValueError, OSError):
                ts = cfgmod.now_iso()
            records.append({
                "role": role,
                "text": text,
                "ts": ts,
                "msg_id": item.get("message_id"),
            })
        return records

    def on_disconnected(self, ws) -> None:
        if self._ws is ws:
            self._ws = None
            for runtime in list(self._runtimes.values()):
                runtime.on_disconnected()
            self.logger.info("[私聊] 连接断开，未发送回复已取消，主动消息暂停")

    def start_tasks(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            self.spawn(self._proactive_loop())
            self.spawn(self._reflection_loop())
        except RuntimeError:
            return
        self.logger.info("[私聊] 后台任务已启动（主动消息 / 记忆整理）")

    def spawn(self, coro) -> asyncio.Task:
        if self._stopping:
            if hasattr(coro, "close"):
                coro.close()
            raise RuntimeError("正在停止，拒绝新任务")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            if hasattr(coro, "close"):
                coro.close()
            raise RuntimeError("没有运行中的事件循环")
        task = loop.create_task(coro)
        self._tasks.append(task)
        self._tasks = [x for x in self._tasks if not x.done()]
        return task

    async def stop(self) -> None:
        self._stopping = True
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for runtime in list(self._runtimes.values()):
            try:
                await runtime.stop()
            except Exception:  # noqa: BLE001
                pass
        self._runtimes.clear()
        try:
            await self._legacy.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.sender.stop()
        except Exception:  # noqa: BLE001
            pass
        if self._rpc is not None:
            try:
                self._rpc.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            for uid in list(self.store._state_cache):
                self.store.save_state(uid)
            for uid in list(self.store._profile_cache):
                self.store.save_profile(uid)
        except Exception:
            pass
        self.logger.info("[私聊] 已停止")

    # ---------- runtime ----------
    def get_runtime(self, uid: str) -> ConversationRuntime:
        key = str(uid)
        runtime = self._runtimes.get(key)
        if runtime is None:
            runtime = ConversationRuntime(self, key)
            self._runtimes[key] = runtime
        return runtime

    def _known_uids(self) -> list[str]:
        allowed = self.cfg.get("allowed_users") or []
        return [str(x) for x in allowed] if isinstance(allowed, (list, tuple)) else []

    # ---------- 事件入口 ----------
    async def on_message(self, ws, data: dict) -> None:
        if not self.cfg.get("enabled"):
            return
        if data.get("message_type") != "private":
            return
        uid = str(data.get("user_id") or "")
        if not uid or str(data.get("self_id")) == uid:
            return
        if not _is_allowed(uid, self.cfg):
            self.logger.info(f"[私聊] 忽略非白名单私聊 uid={uid}")
            return

        msg_id = data.get("message_id")
        dedup_key = f"{uid}:{msg_id}" if msg_id is not None else ""
        if dedup_key:
            if dedup_key in self._seen:
                self.logger.info(f"[私聊] 重复消息 mid={msg_id}，忽略")
                return
            self._seen.add(dedup_key)
            self._seen_order.append(dedup_key)
            if len(self._seen_order) > 500:
                old = self._seen_order.pop(0)
                self._seen.discard(old)

        text, images = parse_message(data)
        if not text:
            text = "[图片]"
        original_text = text
        raw_message = str(data.get("raw_message") or "")
        sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        raw_nickname = str(sender.get("card") or sender.get("nickname") or "").strip()
        prof = self.store.profile(uid)
        if raw_nickname:
            nickname = cfgmod.normalize_display_name(raw_nickname)
            if nickname != prof.get("display_name"):
                self.store.update_profile(uid, display_name=nickname)
        self.store.update_profile(uid, last_user_at=cfgmod.now_iso())
        self.store.reset_unanswered(uid)

        if self._is_pause_request(text):
            self._pause_proactive(uid, text, msg_id)
            await self._send_quiet_ack(ws, uid, msg_id)
            return

        # 图片先入缓存（当前/引用/最近图都靠它），控制指令不做识图。
        if images:
            image_vision.remember(uid, data, images, self.cfg, self.logger)

        if self._is_control_command(text):
            self.store.add_turn(
                uid, "user", text, msg_id=msg_id,
                meta={"source": "control", "original_text": original_text},
            )
            self.log_received(uid, original_text, images)
            if await self._maybe_relationship_command(ws, uid, text, msg_id):
                return
            if await self._maybe_activity_command(ws, uid, text, msg_id):
                return
            if await self._maybe_correction_command(ws, uid, text, msg_id):
                return
            if await self._maybe_memory_command(ws, uid, text, msg_id):
                return
            return

        pipeline = str((self.cfg.get("reply") or {}).get("pipeline", "structured") or "structured")
        if pipeline.lower() == "legacy":
            # legacy 可能 await 识图，不能阻塞 WS 接收循环（echo 回执还靠它）。
            self.spawn(self._legacy.on_normal(ws, data, text, images, original_text, msg_id))
            return

        # 新链路：先把原始用户消息落库（图片只写占位，识图后 update_turn）。
        meta = {"source": "user", "original_text": original_text}
        if images:
            meta["vision_status"] = "pending"
        self.store.add_turn(uid, "user", text, msg_id=msg_id, meta=meta)
        self.log_received(uid, original_text, images)
        burst_n = self.track_user_message(uid, original_text)
        event = NormalEvent(
            uid=uid,
            msg_id=str(msg_id) if msg_id is not None else None,
            text=text,
            original_text=original_text,
            images=images,
            raw_message=raw_message,
            ts=cfgmod.now_iso(),
            burst_n=burst_n,
        )
        self.get_runtime(uid).submit_message(event)

    # ---------- runtime 回调 ----------
    def log_received(self, uid: str, text: str, images: list[dict]) -> None:
        pics = f"，图片{len(images)}张" if images else ""
        if (self.cfg.get("safety") or {}).get("log_message_bodies"):
            self.logger.info(f"[私聊] 收到 {uid}: {text[:80]}{pics}")
        else:
            self.logger.info(f"[私聊] 收到 {uid} 的消息（{len(text)} 字{pics}）")

    def debounce_delay(self) -> float:
        reply_cfg = self.cfg.get("reply") or {}
        base = reply_cfg.get("debounce_seconds", 3.0)
        base = float(3.0 if base is None else base)
        jitter = reply_cfg.get("debounce_jitter_seconds", 1.5)
        jitter = float(1.5 if jitter is None else jitter)
        return max(0.5, base + random.uniform(0, max(0.1, jitter)))

    def track_user_message(self, uid: str, text: str) -> int:
        """记录用户连发节奏，更新 style.json；返回本轮 burst_n。"""
        style_cfg = self.cfg.get("style") or {}
        if not style_cfg.get("enabled", True):
            return 1
        window = float(style_cfg.get("burst_window_seconds", 8) or 8)
        now = time.time()
        times = self._user_times.get(uid)
        if times is None:
            from collections import deque
            times = deque()
            self._user_times[uid] = times
        while times and now - times[0] > window:
            times.popleft()
        times.append(now)
        burst_n = max(1, len(times))
        try:
            style_mod.update(self.store, uid, text, burst_n, self.cfg)
        except Exception as exc:  # noqa: BLE001
            self.logger.info(f"[私聊] 风格统计失败（已忽略）: {type(exc).__name__}: {exc}")
        return burst_n

    # 兼容旧调用名
    _track_user_message = track_user_message

    def reset_user_burst(self, uid: str) -> None:
        """回复计划已取到，清空连发窗口；回复期间新来的消息重新累计。"""
        self._user_times.pop(str(uid), None)

    def schedule_activity_extract(self, uid: str, text: str) -> None:
        self._maybe_activity_extract(uid, text)

    async def prepare_inbound(self, event: NormalEvent) -> None:
        """图片/引用图 enrichment：15 秒总上限，完成后原地 update_turn。"""
        image_cfg = self.cfg.get("image") or {}
        if not image_cfg.get("enabled", True):
            if event.msg_id is not None and event.images:
                self.store.update_turn(
                    event.uid, event.msg_id,
                    meta={"vision_status": "disabled", "original_text": event.original_text},
                )
            return
        images = list(event.images or [])
        text = str(event.text or "")
        should = bool(images) or image_vision.should_attempt(
            text, images, event.raw_message
        )
        if not should:
            return
        timeout = float(image_cfg.get("preprocess_timeout_seconds", 15.0) or 15.0)
        result = None
        timed_out = False
        try:
            result = await asyncio.wait_for(
                image_vision.describe_turn(
                    event.uid, text, images, event.raw_message, self.cfg, self.logger,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            timed_out = True
            self.logger.info(f"[图片] 预处理超时（>{timeout:.0f}s，标记未识别后继续）")
        except Exception as exc:  # noqa: BLE001
            self.logger.info(f"[图片] 识图入口异常（已忽略）: {type(exc).__name__}: {exc}")

        new_text = text
        status = ""
        if result:
            desc = str(result.get("desc") or "").strip()
            if desc:
                new_text = image_vision.enrich_text(
                    text, desc, kind=result.get("kind", "image")
                )
                status = "described"
            else:
                status = str(result.get("skipped") or "duplicate")
        elif timed_out:
            status = "timeout"
            if images and image_cfg.get("vision", True):
                kind = image_vision.kind_of(images)
                recognize = bool(image_cfg.get("recognize_stickers", True))
                if kind != "sticker" or recognize:
                    new_text = image_vision.mark_unavailable(text, images)
        elif images:
            status = "failed"
            if image_cfg.get("vision", True):
                kind = image_vision.kind_of(images)
                recognize = bool(image_cfg.get("recognize_stickers", True))
                if kind != "sticker" or recognize:
                    new_text = image_vision.mark_unavailable(text, images)

        if event.msg_id is None:
            return
        meta = {"original_text": event.original_text}
        if status:
            meta["vision_status"] = status
        if new_text != text or status:
            self.store.update_turn(event.uid, event.msg_id, text=new_text, meta=meta)
        event.text = new_text

    async def build_reply_messages(self, context: dict) -> list[dict]:
        uid = str(context.get("uid") or "")
        target = context.get("target_messages") or []
        query = str(context.get("query") or "")
        decision = context.get("decision")
        rhythm = context.get("rhythm")
        structured = bool(context.get("structured"))
        try:
            search_payload = await search_mod.maybe_search(query, uid, self.cfg, self.logger)
        except Exception as exc:  # noqa: BLE001
            search_payload = None
            self.logger.info(f"[搜索] 入口异常（已忽略）: {type(exc).__name__}: {exc}")
        try:
            activity_text = self.activity.format_prompt(uid)
        except Exception as exc:  # noqa: BLE001
            activity_text = ""
            self.logger.info(f"[活动] 读取/渲染失败（已忽略）: {type(exc).__name__}: {exc}")
        max_turns = int((self.cfg.get("reply") or {}).get("max_context_turns", 24) or 24)
        turns = context.get("turns")
        if not isinstance(turns, list):
            turns = self.store.recent_turns(uid, max_turns)
        return persona_mod.build_messages(
            self.cfg, self.store, uid, query,
            search=search_payload, activity=activity_text,
            turns=turns,
            dialogue=context.get("dialogue"),
            behavior=decision,
            rhythm=rhythm,
            target_messages=target,
            structured=structured,
        )

    def get_corrections(self) -> list[dict]:
        try:
            return persona_mod.load_corrections(self.cfg)
        except Exception:  # noqa: BLE001
            return []

    def fallback_reply(self, uid: str) -> str:
        recent = [str(t.get("text") or "") for t in self.store.recent_turns(uid, 8)]
        options = [x for x in _FALLBACK_LINES if x not in recent] or _FALLBACK_LINES
        return random.choice(options)

    _fallback_reply = fallback_reply

    def on_turn_finished(self, uid: str, target_ids: list[str]) -> None:
        self.maybe_extract(uid)

    # ---------- 控制指令 ----------
    def _is_control_command(self, text: str) -> bool:
        value = str(text or "").strip()
        if value.startswith(("/关系", "/纠偏", "/活动")):
            return True
        for prefix in ("忘掉", "忘记", "别记着", "别记"):
            if not value.startswith(prefix):
                continue
            rest = value[len(prefix):].strip(" ：:，,。.！!？?")
            if not rest:
                continue
            if prefix in ("忘掉", "忘记"):
                if len(value) > 30 or rest.startswith("错"):
                    continue
                if rest.endswith(("了", "吗", "呢", "吧", "啊")):
                    continue
                return True
            if rest.startswith("错"):
                continue
            return True
        return False

    async def _send_control_reply(self, ws, uid: str, text: str, msg_id,
                                  source: str = "control") -> None:
        pipeline = str((self.cfg.get("reply") or {}).get("pipeline", "structured") or "structured")
        if pipeline.lower() == "legacy":
            try:
                await sender_mod.send_private_text(ws, uid, text)
                self.store.add_turn(uid, "assistant", text, meta={"source": source})
            except Exception as exc:  # noqa: BLE001
                self.logger.info(f"[私聊] 控制确认发送失败: {exc}")
            return
        runtime = self.get_runtime(uid)
        runtime.submit_control(ControlEvent(
            uid=uid,
            text=str(text),
            reply_to_msg_id=str(msg_id) if msg_id is not None else None,
            command_msg_id=str(msg_id) if msg_id is not None else None,
            source=source,
        ))

    async def _maybe_activity_command(self, ws, uid: str, text: str, msg_id=None) -> bool:
        """`/活动` 查看；`/活动 结束` 关闭当前；`/活动 清空` 清空当前+日程。"""
        value = str(text or "").strip()
        if not value.startswith("/活动"):
            return False
        arg = value[len("/活动"):].strip().lower()
        if arg in ("清空", "清除", "clear"):
            self.activity.clear(uid)
            line = "活动/日程记录清空了。"
        elif arg in ("结束", "关闭", "done"):
            ok = self.activity.close_current(uid, "用户手动结束")
            line = "好，当前活动结束了。" if ok else "现在没有正在进行的活动。"
        else:
            snapshot = self.activity.format_prompt(uid)
            line = snapshot or "目前没记录到你正在做什么，也没有接下来的安排。"
        await self._send_control_reply(ws, uid, line, msg_id)
        return True

    async def _maybe_relationship_command(self, ws, uid: str, text: str, msg_id=None) -> bool:
        """`/关系` 查看；`/关系 close` 切换四档关系。"""
        value = str(text or "").strip()
        if not value.startswith("/关系"):
            return False
        arg = value[len("/关系"):].strip().lower()
        rel = persona_mod.load_relationship(self.cfg)
        stages = rel.get("stages") or {}
        valid = [s for s in ("stranger", "familiar", "close", "intimate") if s in stages] or [
            "stranger", "familiar", "close", "intimate"
        ]
        profile = self.store.profile(uid)
        current = str(
            (profile.get("relationship") or {}).get("stage") or rel.get("default_stage") or "familiar"
        )
        if arg not in valid:
            labels = " / ".join(
                f"{s}={((stages.get(s) or {}).get('label') or s)}" for s in valid
            )
            line = f"现在是「{current}」。可选关系阶段：{labels}；发送 /关系 close 切换。"
        else:
            self.store.update_profile(uid, relationship={"stage": arg})
            label = (stages.get(arg) or {}).get("label") or arg
            line = f"好，关系阶段切到「{label}」了。"
        await self._send_control_reply(ws, uid, line, msg_id)
        return True

    async def _maybe_correction_command(self, ws, uid: str, text: str, msg_id=None) -> bool:
        """`/纠偏 wrong => right`：写入即生效，下一轮编译卡与输出都会应用。"""
        parsed = corrections_mod.parse_command(text)
        if not parsed:
            return False
        wrong, right = parsed
        path = os.path.join(persona_mod.private_dir(self.cfg), "corrections.json")
        corrections_mod.add(path, wrong, right)
        if right:
            line = f"好，记下了：不说「{wrong}」，说「{right}」。"
        else:
            line = f"好，以后不说「{wrong}」。"
        await self._send_control_reply(ws, uid, line, msg_id)
        self.logger.info(f"[私聊] 纠偏已写入：{wrong!r} -> {right!r}")
        return True

    async def _maybe_memory_command(self, ws, uid: str, text: str, msg_id=None) -> bool:
        """支持「忘掉 X」这种自然语言删记忆；不触达模型。"""
        value = str(text or "").strip()
        for prefix in ("忘掉", "忘记", "别记着", "别记"):
            if not value.startswith(prefix):
                continue
            keyword = value[len(prefix):].strip(" ：:，,。.！!？?")
            if not keyword:
                continue
            removed = self.store.forget(uid, keyword)
            if removed:
                line = f"好，关于「{keyword}」的事我不记着了。"
            else:
                line = f"我本来就没记着「{keyword}」这件事。"
            await self._send_control_reply(ws, uid, line, msg_id)
            return True
        return False

    async def on_friend_request(self, ws, data: dict) -> None:
        if not self.cfg.get("enabled") or not self.cfg.get("auto_accept_friends", True):
            return
        if data.get("request_type") != "friend":
            return
        uid = str(data.get("user_id") or "")
        flag = data.get("flag")
        if not flag or not _is_allowed(uid, self.cfg):
            self.logger.info(f"[私聊] 忽略非白名单好友请求 uid={uid}")
            return
        try:
            await sender_mod.approve_friend(
                ws, str(flag), remark=str(self.cfg.get("character") or "")
            )
            self.logger.info(f"[私聊] 已通过白名单好友请求 uid={uid}")
        except Exception as exc:  # noqa: BLE001
            self.logger.info(f"[私聊] 通过好友请求失败 uid={uid}: {exc}")

    # ---------- 暂停 ----------
    def _is_pause_request(self, text: str) -> bool:
        value = str(text or "").strip()
        if not value or len(value) > 12:
            return False
        if "?" in value or "？" in value:
            return False
        if value.endswith(("吗", "呢", "什么", "怎么", "哪")):
            return False
        candidates = [value]
        for prefix in ("我那就", "那就", "我", "那"):
            if value.startswith(prefix):
                candidates.append(value[len(prefix):].lstrip("，, "))
        keywords = [str(k or "").strip() for k in (
            (self.cfg.get("safety") or {}).get("pause_keywords") or []
        )]
        keywords = [k for k in keywords if k]
        question_marks = ("?", "？", "吗", "呢", "什么", "怎么", "啥", "干嘛", "哪", "谁")
        particles = set("，,。.！!～~ 了啦哦呀吧")
        for candidate in candidates:
            for key in keywords:
                if candidate == key:
                    return True
                if not candidate.startswith(key):
                    continue
                rest = candidate[len(key):].strip()
                if not rest:
                    return True
                if any(mark in rest for mark in question_marks):
                    continue
                # 关键词后只能接语气词，或接另一个暂停关键词（“在忙，别烦我”）。
                if any(other in rest for other in keywords):
                    return True
                if all(ch in particles for ch in rest):
                    return True
        return False

    def _pause_proactive(self, uid: str, text: str, msg_id=None) -> None:
        hours = float((self.cfg.get("safety") or {}).get("pause_hours", 24) or 24)
        until = (datetime.now().astimezone() + timedelta(hours=hours)).isoformat(timespec="seconds")
        self.store.update_profile(uid, proactive={"paused_until": until})
        self.store.add_turn(uid, "user", text, msg_id=msg_id, meta={"source": "pause"})
        self.logger.info(f"[私聊] 用户要求暂停主动消息直到 {until}")

    async def _send_quiet_ack(self, ws, uid: str, msg_id=None) -> None:
        line = random.choice(["好，那我不吵你。", "行，你先忙。", "嗯，我在。"])
        pipeline = str((self.cfg.get("reply") or {}).get("pipeline", "structured") or "structured")
        if pipeline.lower() == "legacy":
            await self._send_control_reply(ws, uid, line, msg_id, source="pause_ack")
            return
        runtime = self.get_runtime(uid)
        runtime.submit_pause(ControlEvent(
            uid=uid,
            text=line,
            reply_to_msg_id=str(msg_id) if msg_id is not None else None,
            command_msg_id=str(msg_id) if msg_id is not None else None,
            source="pause_ack",
        ))

    # ---------- 活动抽取 ----------
    def _maybe_activity_extract(self, uid: str, text: str) -> None:
        cfg = self.cfg.get("activity") or {}
        if not cfg.get("enabled", True) or not cfg.get("extract_from_chat", True):
            return
        turns = int(self._activity_turns.get(uid, 0)) + 1
        self._activity_turns[uid] = turns
        every = int(cfg.get("extract_every_turns", 4) or 4)
        cue = self.activity.should_extract(text)
        if not cue and turns < every:
            return
        now = time.time()
        cooldown = float(cfg.get("extract_cooldown_seconds", 30.0) or 30.0)
        if now - float(self._activity_last.get(uid, 0) or 0) < cooldown:
            return
        self._activity_last[uid] = now
        self._activity_turns[uid] = 0
        try:
            self.spawn(self._extract_activity(uid))
        except RuntimeError:
            pass

    async def _extract_activity(self, uid: str) -> None:
        lock = self._activity_locks.setdefault(uid, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            turns = self.store.recent_turns(uid, 12)
            if not turns:
                return
            lines = []
            for turn in turns:
                role = "对方" if turn.get("role") == "user" else "你"
                lines.append(f"[{turn.get('ts', '')}] {role}: {turn.get('text', '')}")
            now = datetime.now().astimezone()
            try:
                existing = self.activity.format_for_llm(uid, now)
            except Exception:
                existing = "{}"
            prompt = [
                {"role": "system", "content": (
                    "你从聊天记录中抽取用户的活动与日程。只输出 JSON，不要解释。\n"
                    "当前时间：" + now.isoformat(timespec="seconds") + "\n"
                    "格式：{\"operations\":[...]}；没有可抽取内容输出 {\"operations\":[]}。\n"
                    "每个操作必须带 \"op\" 字段，取值只能是：set_current / add_schedule / "
                    "close_current / cancel_schedule。\n"
                    "示例：{\"operations\":[{\"op\":\"set_current\",\"content\":\"上课\","
                    "\"category\":\"class\",\"start_at\":\"2026-10-21T08:00:00+08:00\","
                    "\"end_at\":\"2026-10-21T12:10:00+08:00\",\"precision\":\"exact\","
                    "\"confidence\":0.95}]}。\n"
                    "1) set_current：用户明确说现在/正在/在上课/在吃饭时使用；字段 content, category, "
                    "start_at, end_at, period, precision, confidence, interruptibility, notes。\n"
                    "2) add_schedule：未来的/今天的安排；字段 content, category, date, period, "
                    "start_at, end_at, precision, recurrence, confidence, interruptibility。\n"
                    "3) close_current：用户说下课了/结束了/吃完了；字段 reason。\n"
                    "4) cancel_schedule：用户取消某项安排；字段 id 或 content。\n"
                    "规则：\n"
                    "- 只抽取用户明确说出的活动，不推断、不编造；content ≤40 字。\n"
                    "- category 只能是 class/study/work/meal/rest/sleep/commute/exercise/social/"
                    "entertainment/exam/meeting/other。\n"
                    "- 有明确钟点才用 start_at/end_at（ISO 8601，带 +08:00）；只知道上午/下午/晚上时用 "
                    "period + precision=period，start_at/end_at 置 null。\n"
                    "- “到十二点十分”用今天日期作为 end_at；“每周一/每天”用 "
                    "recurrence={\"type\":\"weekly\",\"weekdays\":[1..7],\"until\":null}。\n"
                    "- 置信度低于 0.5 不要输出。\n"
                    "已有活动：\n" + existing
                )},
                {"role": "user", "content": "最近对话：\n" + "\n".join(lines)[-6000:]},
            ]
            try:
                data = await self._json_fn(prompt, self.cfg)
            except Exception as exc:  # noqa: BLE001
                self.logger.info(f"[活动] 抽取失败: {type(exc).__name__}: {exc}")
                return
            operations = None
            if isinstance(data, dict):
                operations = data.get("operations")
                # 兼容模型漏掉外层 operations、直接输出单个 op 对象的情况。
                if operations is None and any(
                    key in data for key in ("op", "type", "action")
                ):
                    operations = [data]
            elif isinstance(data, list):
                operations = data
            applied = self.activity.apply_operations(uid, operations, now=now)
            self.logger.info(
                f"[活动] 抽取：候选 {len(operations) if isinstance(operations, list) else 0}，"
                f"应用 {applied}"
            )

    # ---------- 记忆抽取 ----------
    def maybe_extract(self, uid: str) -> None:
        mem_cfg = self.cfg.get("memory") or {}
        if not mem_cfg.get("enabled", True):
            return
        st = self.store.state(uid)
        if int(st.get("turns_since_extract", 0)) < int(mem_cfg.get("extract_every_turns", 8) or 8):
            return
        try:
            self.spawn(self._extract_memories(uid))
        except RuntimeError:
            pass

    _maybe_extract = maybe_extract

    async def _extract_memories(self, uid: str) -> None:
        lock = self._extract_locks.setdefault(uid, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            turns = self.store.recent_turns(uid, 24)
            if len(turns) < 4:
                return
            lines = []
            for turn in turns:
                role = "对方" if turn.get("role") == "user" else "你"
                mid = turn.get("msg_id")
                tag = f"[{mid}] " if mid is not None else ""
                lines.append(f"{tag}{role}: {turn.get('text')}")
            prompt = [
                {"role": "system", "content": (
                    "你是长期记忆抽取器。从对话中抽取值得长期记住的事实，只输出 JSON。\n"
                    "格式：{\"memories\":[{\"type\":\"user_fact|user_preference|relationship|event|promise|self_fact|self_feeling\","
                    "\"text\":\"不超过120字\",\"importance\":1-5,\"confidence\":0-1,\"source_ids\":[\"消息id\"]}]}\n"
                    "规则：只记持久信息；不记寒暄和一次性的话；不编造；source_ids 必须来自给定消息 id；"
                    "没有值得记的就输出 {\"memories\":[]}。只输出 JSON。"
                )},
                {"role": "user", "content": "对话记录：\n" + "\n".join(lines)},
            ]
            try:
                data = await self._json_fn(prompt, self.cfg)
            except Exception as exc:  # noqa: BLE001
                self._last_extract_error = f"{type(exc).__name__}: {exc}"
                self.logger.info(f"[私聊] 记忆抽取失败: {self._last_extract_error}")
                return
            candidates = []
            if isinstance(data, dict):
                raw_items = data.get("memories")
                if isinstance(raw_items, list):
                    for item in raw_items:
                        if isinstance(item, dict):
                            candidates.append(item)
            min_conf = float((self.cfg.get("memory") or {}).get("min_confidence", 0.5) or 0.5)
            candidates = [c for c in candidates if float(c.get("confidence", 0.7) or 0) >= min_conf]
            added = self.store.add_memories(uid, candidates)
            st = self.store.state(uid)
            st["turns_since_extract"] = 0
            st["last_extract_at"] = time.time()
            self.store.save_state(uid)
            self.logger.info(f"[私聊] 记忆抽取：候选 {len(candidates)}，新增/合并 {len(added)}")

    # ---------- 反思 / 记忆整理 ----------
    async def _reflection_loop(self) -> None:
        first = True
        while True:
            try:
                await asyncio.sleep(300 if first else 1800)
                first = False
                now = time.time()
                for uid in self._known_uids():
                    await self._maybe_reflect(uid, now)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                self.logger.info(f"[私聊] 反思循环异常: {type(exc).__name__}: {exc}")

    async def _maybe_reflect(self, uid: str, now: float) -> None:
        mem_cfg = self.cfg.get("memory") or {}
        if not mem_cfg.get("enabled", True) or not mem_cfg.get("organize_with_llm", True):
            return
        st = self.store.state(uid)
        last = float(st.get("last_reflection_at", 0) or 0)
        if last and now - last < float(mem_cfg.get("reflection_interval_seconds", 86400) or 86400):
            return
        if int(st.get("total_turns", 0)) < 6:
            return
        await self._reflect(uid)

    async def _reflect(self, uid: str) -> None:
        turns = self.store.recent_turns(uid, 20)
        if not turns:
            return
        memories = self.store.memory_items(uid)[-20:]
        self_state = self.store.self_state(uid)
        lines = [f"{'对方' if t.get('role') == 'user' else '你'}: {t.get('text')}" for t in turns]
        prompt = [
            {"role": "system", "content": (
                "你在整理一段一对一私聊的长期记忆。只输出 JSON。\n"
                "格式：{\"summary\":\"最近聊过什么（<=200字）\","
                "\"self_state\":{\"mood\":\"\",\"energy\":0.0-1.0,\"today\":\"\","
                "\"looking_forward_to\":\"\",\"recent_thoughts\":[\"<=3条\"]},"
                "\"memories\":[{\"type\":\"user_fact|user_preference|relationship|event|promise|self_fact|self_feeling\","
                "\"text\":\"<=120字\",\"importance\":1-5,\"confidence\":0-1,\"source_ids\":[]}]}\n"
                "整理要求：合并重复、保留长期有效的事实、不要删掉重要信息、不要编造。"
            )},
            {"role": "user", "content": (
                "已有记忆：\n" + json.dumps(memories, ensure_ascii=False)[:4000]
                + "\n\n最近对话：\n" + "\n".join(lines)[-5000:]
                + "\n\n当前自我状态：\n"
                + json.dumps(self_state, ensure_ascii=False)[:1200]
            )},
        ]
        try:
            data = await self._json_fn(prompt, self.cfg)
        except Exception as exc:  # noqa: BLE001
            self._last_reflection_error = f"{type(exc).__name__}: {exc}"
            self.logger.info(f"[私聊] 记忆整理失败: {self._last_reflection_error}")
            return
        if not isinstance(data, dict):
            return
        if isinstance(data.get("summary"), str) and data["summary"].strip():
            self.store.add_summary(uid, data["summary"])
            self.store.self_state(uid)["summary"] = data["summary"].strip()[:1200]
        new_state = data.get("self_state")
        if isinstance(new_state, dict):
            current = self.store.self_state(uid)
            for key in ("mood", "energy", "today", "looking_forward_to"):
                if key in new_state:
                    current[key] = new_state[key]
            thoughts = new_state.get("recent_thoughts")
            if isinstance(thoughts, list):
                current["recent_thoughts"] = [str(x)[:120] for x in thoughts[:3]]
            current["last_reflection_at"] = time.time()
            self.store.save_self_state(uid)
        if isinstance(data.get("memories"), list):
            self.store.add_memories(uid, [x for x in data["memories"] if isinstance(x, dict)])
        st = self.store.state(uid)
        st["last_reflection_at"] = time.time()
        self.store.save_state(uid)
        self.logger.info(f"[私聊] 记忆整理完成 uid={uid}")

    # ---------- 主动消息 ----------
    async def _proactive_loop(self) -> None:
        from .proactive import ProactiveEngine
        engine = ProactiveEngine(self, self.logger)
        while True:
            try:
                await asyncio.sleep(float(
                    (self.cfg.get("proactive") or {}).get("tick_seconds", 60) or 60
                ))
                await engine.tick()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                self.logger.info(f"[私聊] 主动消息循环异常: {type(exc).__name__}: {exc}")
