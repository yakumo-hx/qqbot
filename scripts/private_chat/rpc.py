# -*- coding: utf-8 -*-
"""最小 OneBot action RPC：给需要回执的调用（历史消息、私聊发送）用。

私聊主循环原本只发 action、不读回执；这里用 echo 关联 Future，
由 private_bot.handler 在解析消息前先交给 resolve()。

发送语义分三种（见 ActionOutcome）：
- ok       OneBot 明确返回 status=ok / retcode=0；
- failed   OneBot 明确返回非零 retcode；
- unknown  超时、断连、写入异常 —— 请求可能已经到达平台，绝不能自动重发。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


class RPCError(RuntimeError):
    pass


class RPCUnknownError(RPCError):
    """结果未知（超时/断连/写入失败），调用方不得当作成功或失败重试。"""


@dataclass
class ActionOutcome:
    status: str                     # ok / failed / unknown
    data: Any = None
    message_id: Optional[str] = None
    error: str = ""
    retcode: Optional[int] = None
    echo: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class OneBotRPC:
    def __init__(self, prefix: str = "private:"):
        self.prefix = str(prefix)
        self.pending: dict[tuple, asyncio.Future] = {}

    async def call_outcome(self, websocket, action: str, params: dict,
                           timeout: float = 10.0) -> ActionOutcome:
        if websocket is None:
            return ActionOutcome("unknown", error="WebSocket 未连接")
        echo = self.prefix + uuid.uuid4().hex
        key = (websocket, echo)
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            try:
                await websocket.send(json.dumps({
                    "action": action, "params": params, "echo": echo,
                }, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001 - 发送失败视为结果未知
                return ActionOutcome(
                    "unknown", error=f"发送异常: {type(exc).__name__}: {exc}", echo=echo,
                )
            try:
                response = await asyncio.wait_for(future, float(timeout))
            except asyncio.TimeoutError:
                return ActionOutcome("unknown", error=f"{action} 超时（{timeout}s）", echo=echo)
            except RPCUnknownError as exc:
                return ActionOutcome("unknown", error=str(exc), echo=echo)
            except asyncio.CancelledError:
                # 外部任务被取消：结果未知，交给调用方按 unknown 处理。
                raise
        finally:
            self.pending.pop(key, None)
            if not future.done():
                future.cancel()

        if not isinstance(response, dict):
            return ActionOutcome("failed", error=f"{action} 回执不是对象", echo=echo)
        data = response.get("data")
        retcode = response.get("retcode")
        try:
            retcode_int = int(retcode) if retcode is not None else None
        except (TypeError, ValueError):
            retcode_int = None
        if response.get("status") != "ok" or retcode_int != 0:
            return ActionOutcome(
                "failed",
                data=data,
                error=(
                    f"{action} 失败：status={response.get('status')} "
                    f"retcode={response.get('retcode')}"
                ),
                retcode=retcode_int,
                echo=echo,
            )
        message_id = None
        if isinstance(data, dict):
            raw_mid = data.get("message_id")
            if raw_mid is not None:
                message_id = str(raw_mid)
        return ActionOutcome(
            "ok", data=data, message_id=message_id, retcode=retcode_int, echo=echo,
        )

    async def call(self, websocket, action: str, params: dict, timeout: float = 10.0):
        """兼容旧调用：成功返回 data，其余抛 RPCError/RPCUnknownError。"""
        outcome = await self.call_outcome(websocket, action, params, timeout=timeout)
        if outcome.ok:
            return outcome.data
        if outcome.status == "unknown":
            raise RPCUnknownError(outcome.error)
        raise RPCError(outcome.error)

    def resolve(self, websocket, data) -> bool:
        """处理带 echo 的回执；返回 True 表示这条不是普通事件。"""
        if not isinstance(data, dict) or "post_type" in data:
            return False
        echo = data.get("echo")
        if not isinstance(echo, str) or not echo.startswith(self.prefix):
            return False
        future = self.pending.get((websocket, echo))
        if future is not None and not future.done():
            future.set_result(data)
        return True  # 迟到/重复回执也不进入消息路由

    def disconnect(self, websocket) -> None:
        """断连时把未完成调用标为“结果未知”，不要用 cancel 抹掉语义。"""
        for key in list(self.pending):
            if key[0] is websocket:
                future = self.pending.pop(key)
                if not future.done():
                    future.set_exception(RPCUnknownError("连接断开，结果未知"))

    def close(self) -> None:
        for key, future in list(self.pending.items()):
            self.pending.pop(key, None)
            if not future.done():
                future.set_exception(RPCUnknownError("RPC 关闭，结果未知"))
