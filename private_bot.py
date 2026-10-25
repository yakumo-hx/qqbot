#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQbot 单人私聊陪伴版 0.0.1 —— 独立入口（默认端口 8081）。

运行：
    cd E:\\QQbot
    $env:PYTHONIOENCODING="utf-8"
    .\\venv\\Scripts\\python private_bot.py

NapCat 需要再添加一个 WebSocket 客户端：ws://127.0.0.1:8081
（群聊机器人仍用 8080，两者互不影响。）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(ROOT, ".env"))

import websockets  # noqa: E402

from scripts.private_chat import VERSION, PrivateChat  # noqa: E402
from scripts.private_chat import config as cfgmod  # noqa: E402
from scripts.private_chat.rpc import OneBotRPC  # noqa: E402

logger = logging.getLogger("private_bot")
rpc = OneBotRPC()


def setup_logger() -> None:
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(logging.INFO)
    logger.addHandler(console)
    try:
        path = cfgmod.resolve_log_path("private_bot")
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s"))
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)
        logger.info(f"日志文件：{path}")
    except OSError as exc:
        logger.info(f"文件日志不可用（降级为控制台）：{exc}")


async def _safe_message(chat: PrivateChat, websocket, data: dict) -> None:
    try:
        await chat.on_message(websocket, data)
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[私聊] 处理消息异常: {type(exc).__name__}: {exc}")


async def _safe_request(chat: PrivateChat, websocket, data: dict) -> None:
    try:
        await chat.on_friend_request(websocket, data)
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[私聊] 处理请求异常: {type(exc).__name__}: {exc}")


async def handler(websocket, *args):
    logger.info(f"NapCat 已连接：{getattr(websocket, 'remote_address', '?')}")
    chat = websocket_server.chat  # type: ignore[attr-defined]
    chat.on_connected(websocket)
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                if not isinstance(data, dict):
                    continue
                if rpc.resolve(websocket, data):
                    continue
                post_type = data.get("post_type")
                if post_type == "message" and data.get("message_type") == "private":
                    # 新链路 on_message 只做校验/落库/入队，必须快速返回；
                    # 图片、模型、发送都在 runtime worker 里执行。
                    await _safe_message(chat, websocket, data)
                elif post_type == "request":
                    chat.spawn(_safe_request(chat, websocket, data))
                elif post_type == "meta_event":
                    continue
            except json.JSONDecodeError:
                logger.info(f"无效 JSON：{str(message)[:80]}")
            except Exception as exc:  # noqa: BLE001
                logger.info(f"消息循环异常: {type(exc).__name__}: {exc}")
    except websockets.exceptions.ConnectionClosed:
        logger.info("NapCat 连接断开")
    finally:
        rpc.disconnect(websocket)
        chat.on_disconnected(websocket)


server = None


class _ServerHolder:
    chat: PrivateChat


websocket_server = _ServerHolder()


async def main() -> None:
    global server
    setup_logger()
    cfg = cfgmod.load_config()
    if cfg.get("_load_error"):
        logger.info(f"配置警告：{cfg['_load_error']}")
    if not cfg.get("enabled"):
        logger.info("私聊功能未启用（config/private_chat.json enabled=false 或配置损坏），退出")
        return

    chat = PrivateChat(logger, cfg=cfg, rpc=rpc)
    websocket_server.chat = chat
    chat.start_tasks()

    host = str(cfg.get("host") or "0.0.0.0")
    port = int(cfg.get("port") or 8081)

    server = await websockets.serve(
        handler, host, port, ping_interval=30, ping_timeout=10,
    )
    logger.info(f"私聊陪伴版 v{VERSION} 启动，监听 {host}:{port}")
    logger.info(f"服务对象：{cfg.get('allowed_users')}")
    logger.info(f"角色：{cfg.get('character') or cfgmod.get_active_character()}")
    logger.info("请确保 NapCat 已添加 WebSocket 客户端：ws://127.0.0.1:%d" % port)
    logger.info("按 Ctrl+C 停止")

    try:
        await asyncio.Future()
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("正在关闭...")
    finally:
        try:
            server.close()
            await server.wait_closed()
        except Exception:
            pass
        await chat.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("正在退出...")
