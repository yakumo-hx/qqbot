# -*- coding: utf-8 -*-
"""决策回复的 OneBot echo 回执发送层（最小侵入）。

背景：``reverse_bot.send_message`` 过去只是 ``await websocket.send(...)`` 后返回
``True``——那只代表"写进了 WebSocket"，不代表 QQ/OneBot 侧明确收到。本模块给
**DecisionEngine 的文字/图片回复**补一个 echo 回执层：

- ``ok``      OneBot 明确返回 ``status=ok`` 且 ``retcode=0``（回执里带 message_id 时记录）；
- ``failed``  OneBot 明确失败（非零 retcode / status 非 ok / 回执不是对象），或本地
              读取图片失败（明确没发出去）；
- ``unknown`` 超时、断连、写入异常、回执无法判定——请求**可能已经到达平台**，
              **绝不自动重发**。

写入与回执都受同一个 ``timeout`` 约束（默认 10 秒）：WS 背压导致 ``send``
长时间不返回时按 ``unknown/write_timeout`` 收敛，不会无限挂起调用方。

约束：
- 只覆盖 DecisionEngine 的回复；指令/``+1``/新人欢迎/神秘数字等其它发送路径仍是
  legacy（会用本模块的 :func:`legacy_note` 说明，不做统一重写）。
- 不引入私聊运行时：设计参考 ``scripts/private_chat/rpc.py``，但实现独立（纯 stdlib，
  无任何私聊 import）。
- 所有回执测试必须用模拟 WebSocket；本模块自身不发真实网络请求（发送动作由调用方
  传入的 websocket 执行）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
from dataclasses import dataclass
from typing import Any, Optional

ECHO_PREFIX = "decision:"
DEFAULT_TIMEOUT_SECONDS = 10.0

# 回执无法判定/断连时用这个异常标记 Future，调用方统一收敛成 unknown
class ReceiptUnknownError(RuntimeError):
    """结果未知（断连/关闭），调用方不得当作成功或失败重试。"""


@dataclass
class SendOutcome:
    """一条发送的结果。``status`` 只取 ok/failed/unknown 三态。"""

    status: str
    kind: str = ""                  # text / sticker
    data: Any = None                # OneBot 回执 data（不含本地图片内容）
    message_id: Optional[str] = None
    error: str = ""
    error_kind: str = ""            # onebot / local / timeout / disconnected / write
    retcode: Optional[int] = None
    echo: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_attempt(self) -> dict:
        """台账用的最小摘要（不含 payload/base64/本地路径）。"""
        return {
            "kind": self.kind,
            "status": self.status,
            "retcode": self.retcode,
            "message_id": self.message_id,
            "error_kind": self.error_kind,
        }


def _as_retcode(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class DecisionSendReceipt:
    """给决策回复用的 echo RPC：``call`` 等回执，``resolve`` 由入口路由回执。

    ``timeout`` 默认 10 秒（可由调用方按配置覆盖；测试用很短的假超时）。
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 prefix: str = ECHO_PREFIX):
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_SECONDS
        self.timeout = timeout if timeout > 0 else DEFAULT_TIMEOUT_SECONDS
        self.prefix = str(prefix or ECHO_PREFIX)
        self.pending: dict = {}

    # ---------- 发送 ----------

    async def send_text(self, websocket, group_id, text: str,
                        at_user: Optional[str] = None) -> SendOutcome:
        """发文字（与 legacy ``send_message`` 的 payload 形状一致）。"""
        message = f"[CQ:at,qq={at_user}] {text}" if at_user else text
        return await self._call(
            websocket, "send_group_msg",
            {"group_id": group_id, "message": message},
            kind="text",
        )

    async def send_image(self, websocket, group_id, image_path: str,
                         sub_type: int = 0) -> SendOutcome:
        """发图片（base64 内联，与 legacy ``send_group_image`` 形状一致）。

        图片内容只出现在发给 OneBot 的 payload 里；返回值/台账永不含 base64 或路径。
        """
        try:
            with open(image_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        except OSError as exc:
            # 明确没发出去：算 failed（不是 unknown），且错误里不带本地路径
            return SendOutcome(
                status="failed", kind="sticker", error_kind="local",
                error=f"本地读取图片失败: {type(exc).__name__}",
            )
        data = {"file": f"base64://{b64}"}
        if int(sub_type or 0) == 1:
            data["sub_type"] = 1
        return await self._call(
            websocket, "send_group_msg",
            {"group_id": group_id, "message": [{"type": "image", "data": data}]},
            kind="sticker",
        )

    async def _call(self, websocket, action: str, params: dict, kind: str = "") -> SendOutcome:
        """发一个 action 并等 echo；任何不确定都收敛成 unknown，且只发一次。"""
        if websocket is None:
            return SendOutcome("unknown", kind=kind, error="WebSocket 未连接",
                               error_kind="disconnected")
        echo = self.prefix + uuid.uuid4().hex
        key = (websocket, echo)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending[key] = future
        try:
            try:
                # 写入也加超时：WS 背压/半死连接时 send 可能长时间挂起，
                # 那样会在调用方（同一条决策）里无限等待；超时按 unknown 处理。
                await asyncio.wait_for(websocket.send(json.dumps({
                    "action": action, "params": params, "echo": echo,
                }, ensure_ascii=False)), self.timeout)
            except asyncio.TimeoutError:
                return SendOutcome(
                    "unknown", kind=kind, echo=echo, error_kind="write_timeout",
                    error=f"{action} 写入超时（{self.timeout}s）",
                )
            except Exception as exc:  # noqa: BLE001 - 写入异常 => 结果未知
                return SendOutcome(
                    "unknown", kind=kind, echo=echo, error_kind="write",
                    error=f"写入异常: {type(exc).__name__}",
                )
            try:
                response = await asyncio.wait_for(future, self.timeout)
            except asyncio.TimeoutError:
                return SendOutcome(
                    "unknown", kind=kind, echo=echo, error_kind="timeout",
                    error=f"{action} 回执超时（{self.timeout}s）",
                )
            except ReceiptUnknownError as exc:
                return SendOutcome(
                    "unknown", kind=kind, echo=echo, error_kind="disconnected",
                    error=str(exc),
                )
            except asyncio.CancelledError:
                raise
        finally:
            self.pending.pop(key, None)
            if not future.done():
                future.cancel()

        return self._interpret(response, action=action, kind=kind, echo=echo)

    @staticmethod
    def _interpret(response: Any, *, action: str, kind: str, echo: str) -> SendOutcome:
        if not isinstance(response, dict):
            return SendOutcome("failed", kind=kind, echo=echo, error_kind="onebot",
                               error=f"{action} 回执不是对象")
        retcode = _as_retcode(response.get("retcode"))
        data = response.get("data")
        if response.get("status") != "ok" or retcode != 0:
            return SendOutcome(
                "failed", kind=kind, echo=echo, data=data, retcode=retcode,
                error_kind="onebot",
                error=(f"{action} 失败：status={response.get('status')} "
                       f"retcode={response.get('retcode')}"),
            )
        message_id = None
        if isinstance(data, dict) and data.get("message_id") is not None:
            message_id = str(data.get("message_id"))
        return SendOutcome("ok", kind=kind, echo=echo, data=data,
                           message_id=message_id, retcode=retcode)

    # ---------- 回执路由 / 断连 ----------

    def resolve(self, websocket, data) -> bool:
        """处理带 ``decision:`` echo 的回执；返回 True 表示这条不是普通事件。"""
        if not isinstance(data, dict) or "post_type" in data:
            return False
        echo = data.get("echo")
        if not isinstance(echo, str) or not echo.startswith(self.prefix):
            return False
        future = self.pending.get((websocket, echo))
        if future is not None and not future.done():
            future.set_result(data)
        return True  # 迟到/重复/未知回执也不进入消息路由

    def disconnect(self, websocket) -> None:
        """断连：把该连接上未完成的发送标成"结果未知"（不要用 cancel 抹掉语义）。"""
        for key in list(self.pending):
            if key[0] is websocket:
                future = self.pending.pop(key)
                if not future.done():
                    future.set_exception(ReceiptUnknownError("连接断开，结果未知"))

    def close(self) -> None:
        for key, future in list(self.pending.items()):
            self.pending.pop(key, None)
            if not future.done():
                future.set_exception(ReceiptUnknownError("回执层关闭，结果未知"))


def legacy_note() -> str:
    """说明还有哪些发送路径没有回执（写进报告用，避免误以为全链路已统一）。"""
    return (
        "legacy（无 echo 回执）：指令回复 send_message/send_group_image/send_group_file、"
        "+1 复读、新人欢迎、神秘数字队列。仅 DecisionEngine 的文字/图片回复有回执。"
    )


__all__ = [
    "ECHO_PREFIX", "DEFAULT_TIMEOUT_SECONDS", "ReceiptUnknownError",
    "SendOutcome", "DecisionSendReceipt", "legacy_note",
]
