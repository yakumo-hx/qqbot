# -*- coding: utf-8 -*-
"""LLBot 试验专用 OneBot 11 反向 WebSocket 探针（隔离端口，不碰生产 8080/8081）。

角色：本探针是 **WS 服务端**，LLBot 作为客户端连入（与生产架构一致）。
能力：
  - 同时监听多个端口（默认 18080 主 / 18081 次），验证两条独立 WS 是否互不覆盖；
  - 逐帧记录事件 / 心跳 / 回执，带到达时间与序号（用于乱序、重复、补报分析）；
  - 通过本机控制端口（默认 19080）按 echo 发送 action，区分 ok / failed / unknown；
  - 记录事件字段结构（sender / message_id / raw_message / message / message_format）；
  - 可选下载消息中的图片 URL（验证图片接收链路）；
  - 默认对文本与昵称做脱敏，只对显式允许的测试目标保留原文。

用法：
  python ws_probe.py --out-dir <dir> [--ports 18080 18081] [--control 19080]
                     [--allow-uid <测试私聊QQ>] [--allow-gid <测试群>]

控制协议：TCP 127.0.0.1:19080，每行一个 JSON 请求，回一行 JSON。
  {"cmd":"status"}
  {"cmd":"channels"}
  {"cmd":"action","channel":0,"action":"get_login_info","params":{},"timeout":10}
  {"cmd":"raw","channel":0,"payload":{...},"timeout":10}
  {"cmd":"close","channel":0}
  {"cmd":"download_image","message_id":"..."}
  {"cmd":"stop"}
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import sys
import time
import traceback
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import websockets

TZ = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="milliseconds")


def sha16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def mask_mid(value: Any) -> str:
    s = str(value)
    return s if len(s) <= 24 else s[:24] + "…"


@dataclass
class ProbeConfig:
    out_dir: str
    ports: list[int] = field(default_factory=lambda: [18080, 18081])
    control_port: int = 19080
    host: str = "127.0.0.1"
    allow_uids: set[str] = field(default_factory=set)
    allow_gids: set[str] = field(default_factory=set)
    keep_text: bool = False
    token: str = ""


class Probe:
    def __init__(self, cfg: ProbeConfig) -> None:
        self.cfg = cfg
        self.conns: dict[int, Any] = {}
        self.conn_meta: dict[int, dict] = {}
        self.pending: dict[tuple[int, str], asyncio.Future] = {}
        self.events_path = os.path.join(cfg.out_dir, "events.jsonl")
        self.results_path = os.path.join(cfg.out_dir, "results.jsonl")
        self.images_path = os.path.join(cfg.out_dir, "images.jsonl")
        self.events_f = open(self.events_path, "a", encoding="utf-8")
        self.results_f = open(self.results_path, "a", encoding="utf-8")
        self.images_f = open(self.images_path, "a", encoding="utf-8")
        self.seq = 0
        self.started_at = now_iso()
        self.message_store: dict[str, dict] = {}
        self.stats = {
            "events": 0, "heartbeats": 0, "responses": 0, "other": 0,
            "actions_ok": 0, "actions_failed": 0, "actions_unknown": 0,
            "disconnects": 0, "image_downloads": 0,
        }
        self._order: list[dict] = []

    # ---------------- logging ----------------
    def _jsonl(self, fh, obj: dict) -> None:
        fh.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
        fh.flush()

    def log_event(self, channel: int, data: dict, raw_len: int) -> None:
        self.seq += 1
        ev = {
            "seq": self.seq,
            "ts": now_iso(),
            "epoch": time.time(),
            "channel": channel,
            "raw_bytes": raw_len,
            "post_type": data.get("post_type"),
            "message_type": data.get("message_type"),
            "sub_type": data.get("sub_type"),
            "notice_type": data.get("notice_type"),
            "meta_event_type": data.get("meta_event_type"),
            "self_id": data.get("self_id"),
            "time": data.get("time"),
        }
        if ev["post_type"] == "meta_event":
            self.stats["heartbeats"] += 1
            ev["status"] = data.get("status")
            ev["interval"] = data.get("interval")
        elif ev["post_type"] == "message":
            self.stats["events"] += 1
            ev["group_id"] = data.get("group_id")
            ev["user_id"] = self._mask_uid(data.get("user_id"))
            ev["message_id"] = mask_mid(data.get("message_id"))
            ev["message_format"] = "array" if isinstance(data.get("message"), list) else "string"
            ev["raw_message_len"] = len(str(data.get("raw_message") or ""))
            ev["raw_message"] = self._text_for(data, data.get("raw_message"))
            ev["message_types"] = self._message_types(data.get("message"))
            ev["sender_keys"] = sorted((data.get("sender") or {}).keys())
            ev["sender_nick"] = self._nick_for(data)
            ev["font"] = data.get("font")
            if isinstance(data.get("message"), list):
                ev["message_summary"] = self._message_summary(data.get("message"))
            self._store_message(data)
            self._order.append({
                "seq": self.seq, "ts": ev["ts"], "channel": channel,
                "message_id": ev["message_id"], "message_type": ev["message_type"],
                "user_id": ev["user_id"], "group_id": ev["group_id"],
            })
        else:
            self.stats["other"] += 1
        self._jsonl(self.events_f, ev)

    def _allowed(self, data: dict) -> bool:
        uid = str(data.get("user_id") or "")
        gid = str(data.get("group_id") or "")
        return uid in self.cfg.allow_uids or gid in self.cfg.allow_gids

    def _text_for(self, data: dict, text: Any) -> Optional[str]:
        if text is None:
            return None
        s = str(text)
        if self.cfg.keep_text or self._allowed(data):
            return s
        if len(s) <= 12:
            return f"<masked len={len(s)}>"
        return f"<masked len={len(s)} head={s[:4]!r}>"

    def _nick_for(self, data: dict) -> str:
        nick = str((data.get("sender") or {}).get("nickname") or "")
        if self.cfg.keep_text or self._allowed(data):
            return nick
        return f"<nick len={len(nick)}>" if nick else ""

    def _mask_uid(self, uid: Any) -> str:
        s = str(uid or "")
        if s in self.cfg.allow_uids:
            return s
        if not s:
            return ""
        return "uid_" + hashlib.sha256(s.encode()).hexdigest()[:8]

    def _message_types(self, message: Any) -> list:
        if isinstance(message, list):
            return [str(m.get("type")) for m in message if isinstance(m, dict)]
        return ["string"]

    def _message_summary(self, message: list) -> list:
        out = []
        for m in message:
            if not isinstance(m, dict):
                continue
            d = m.get("data") or {}
            item = {"type": m.get("type"), "data_keys": sorted(d.keys())}
            if m.get("type") == "text":
                item["text_len"] = len(str(d.get("text") or ""))
            if m.get("type") == "image":
                item["file"] = str(d.get("file") or "")[:16] + "…" if d.get("file") else ""
                item["url_present"] = bool(d.get("url"))
                item["file_size"] = d.get("file_size")
                item["summary"] = d.get("summary")
            if m.get("type") == "at":
                item["qq"] = self._mask_uid(d.get("qq"))
            if m.get("type") == "reply":
                item["id"] = mask_mid(d.get("id"))
            out.append(item)
        return out

    def _store_message(self, data: dict) -> None:
        mid = str(data.get("message_id") or "")
        if not mid:
            return
        if len(self.message_store) > 500:
            self.message_store.pop(next(iter(self.message_store)))
        self.message_store[mid] = data

    def log_result(self, tag: str, outcome: dict) -> None:
        rec = {"ts": now_iso(), "tag": tag, **outcome}
        self._jsonl(self.results_f, rec)
        self.stats[f"actions_{outcome.get('status', 'unknown')}"] = \
            self.stats.get(f"actions_{outcome.get('status', 'unknown')}", 0) + 1

    # ---------------- ws server ----------------
    async def handler(self, channel: int, ws, *args):
        remote = getattr(ws, "remote_address", None)
        headers = {}
        try:
            req = getattr(ws, "request", None)
            if req is not None and getattr(req, "headers", None) is not None:
                headers = {k: v for k, v in req.headers.items() if k.lower() in
                           ("user-agent", "authorization", "x-self-id", "x-client-role", "sec-websocket-protocol")}
        except Exception:
            pass
        self.conns[channel] = ws
        self.conn_meta[channel] = {
            "remote": str(remote), "connected_at": now_iso(),
            "headers": headers, "sec_websocket_protocol": getattr(ws, "subprotocol", None),
        }
        self._jsonl(self.events_f, {"ts": now_iso(), "kind": "ws_connected",
                                   "channel": channel, **self.conn_meta[channel]})
        try:
            async for message in ws:
                raw_len = len(message) if isinstance(message, (bytes, str)) else 0
                try:
                    data = json.loads(message)
                except Exception:
                    self._jsonl(self.events_f, {"ts": now_iso(), "kind": "non_json",
                                               "channel": channel, "bytes": raw_len})
                    continue
                if not isinstance(data, dict):
                    continue
                echo = data.get("echo")
                if isinstance(echo, str) and ("status" in data or "retcode" in data):
                    self._resolve(channel, echo, data)
                    continue
                if data.get("post_type"):
                    self.log_event(channel, data, raw_len)
                else:
                    self.stats["other"] += 1
                    self._jsonl(self.events_f, {"ts": now_iso(), "kind": "unknown_frame",
                                               "channel": channel, "keys": sorted(data.keys()),
                                               "bytes": raw_len})
        except websockets.exceptions.ConnectionClosed as exc:
            self._jsonl(self.events_f, {"ts": now_iso(), "kind": "ws_closed",
                                       "channel": channel, "code": getattr(exc, "code", None),
                                       "reason": str(getattr(exc, "reason", ""))[:120]})
        except Exception as exc:  # noqa: BLE001
            self._jsonl(self.events_f, {"ts": now_iso(), "kind": "handler_error",
                                       "channel": channel, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            self.stats["disconnects"] += 1
            if self.conns.get(channel) is ws:
                self.conns.pop(channel, None)
                self._jsonl(self.events_f, {"ts": now_iso(), "kind": "ws_disconnected",
                                           "channel": channel})
            # 断连时把所有挂起调用标为 unknown（与生产 rpc.py 语义一致）
            for key in list(self.pending):
                if key[0] == channel:
                    fut = self.pending.pop(key)
                    if not fut.done():
                        fut.set_exception(RuntimeError("连接断开，结果未知"))

    async def serve_channel(self, channel: int) -> None:
        port = self.cfg.ports[channel]
        async def _h(ws, *args):
            await self.handler(channel, ws, *args)
        # max_size=None：大图片/base64 事件可能超过默认 1MB 帧上限
        async with websockets.serve(_h, self.cfg.host, port, ping_interval=30, ping_timeout=10,
                                    max_size=None):
            self._jsonl(self.events_f, {"ts": now_iso(), "kind": "probe_listening",
                                       "channel": channel, "port": port})
            await asyncio.Future()

    def _resolve(self, channel: int, echo: str, data: dict) -> None:
        fut = self.pending.get((channel, echo))
        if fut is not None and not fut.done():
            fut.set_result(data)
        self.stats["responses"] += 1
        self._jsonl(self.events_f, {
            "ts": now_iso(), "kind": "response", "channel": channel,
            "echo": echo, "status": data.get("status"), "retcode": data.get("retcode"),
            "has_data": isinstance(data.get("data"), (dict, list)),
        })

    # ---------------- actions ----------------
    async def call(self, channel: int, action: str, params: dict,
                   timeout: float = 10.0, tag: str = "") -> dict:
        ws = self.conns.get(channel)
        if ws is None:
            out = {"status": "unknown", "action": action, "tag": tag,
                   "error": "WS 未连接", "channel": channel, "echo": ""}
            self.log_result(tag, out)
            return out
        echo = f"probe:{int(time.time() * 1000)}:{os.urandom(3).hex()}"
        fut = asyncio.get_running_loop().create_future()
        self.pending[(channel, echo)] = fut
        t0 = time.time()
        payload = {"action": action, "params": params, "echo": echo}
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            self.pending.pop((channel, echo), None)
            out = {"status": "unknown", "action": action, "tag": tag, "channel": channel,
                   "echo": echo, "error": f"发送异常: {type(exc).__name__}: {exc}",
                   "latency_ms": 0}
            self.log_result(tag, out)
            return out
        try:
            resp = await asyncio.wait_for(fut, float(timeout))
        except asyncio.TimeoutError:
            self.pending.pop((channel, echo), None)
            out = {"status": "unknown", "action": action, "tag": tag, "channel": channel,
                   "echo": echo, "error": f"超时（{timeout}s）",
                   "latency_ms": int((time.time() - t0) * 1000)}
            self.log_result(tag, out)
            return out
        except Exception as exc:  # noqa: BLE001
            self.pending.pop((channel, echo), None)
            out = {"status": "unknown", "action": action, "tag": tag, "channel": channel,
                   "echo": echo, "error": str(exc),
                   "latency_ms": int((time.time() - t0) * 1000)}
            self.log_result(tag, out)
            return out
        finally:
            self.pending.pop((channel, echo), None)
        latency = int((time.time() - t0) * 1000)
        retcode = resp.get("retcode")
        status = "ok" if (resp.get("status") == "ok" and retcode == 0) else "failed"
        data = resp.get("data")
        out = {
            "status": status, "action": action, "tag": tag, "channel": channel,
            "echo": echo, "retcode": retcode, "latency_ms": latency, "data": data,
            "message_id": (data or {}).get("message_id") if isinstance(data, dict) else None,
        }
        self.log_result(tag, out)
        return out

    # ---------------- images ----------------
    def download_image(self, message_id: str) -> dict:
        data = self.message_store.get(str(message_id))
        if not data:
            return {"status": "error", "error": "message_id 不在内存缓存"}
        urls = []
        for m in data.get("message") or []:
            if isinstance(m, dict) and m.get("type") == "image":
                d = m.get("data") or {}
                for key in ("url", "file"):
                    if isinstance(d.get(key), str) and d[key].startswith("http"):
                        urls.append(d[key])
        if not urls:
            return {"status": "error", "error": "消息中没有 http 图片 URL"}
        url = urls[0]
        host = url.split("/")[2] if "//" in url else "?"
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "llbot-probe/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read()
                ctype = r.headers.get("Content-Type", "")
                code = r.status
        except Exception as exc:  # noqa: BLE001
            rec = {"ts": now_iso(), "status": "error", "message_id": str(message_id),
                   "url_host": host, "error": f"{type(exc).__name__}: {exc}"}
            self._jsonl(self.images_f, rec)
            return rec
        rec = {
            "ts": now_iso(), "status": "ok", "message_id": str(message_id),
            "url_host": host, "http": code, "content_type": ctype,
            "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
            "magic": body[:8].hex(), "elapsed_ms": int((time.time() - t0) * 1000),
            "is_png": body[:8] == b"\x89PNG\r\n\x1a\n",
            "is_jpeg": body[:2] == b"\xff\xd8",
            "is_gif": body[:6] in (b"GIF87a", b"GIF89a"),
        }
        self.stats["image_downloads"] += 1
        self._jsonl(self.images_f, rec)
        return rec

    # ---------------- control ----------------
    async def send_image_file(self, channel: int, path: str, target: dict, tag: str = "") -> dict:
        """从本地文件发送图片（base64 内联），避免控制协议传超大 JSON 行。"""
        try:
            body = open(path, "rb").read()
        except OSError as exc:
            return {"status": "error", "error": f"读取失败: {exc}"}
        b64 = base64.b64encode(body).decode("ascii")
        params = {"message": [{"type": "image", "data": {"file": f"base64://{b64}"}}]}
        if target.get("type") == "group":
            action = "send_group_msg"
            params["group_id"] = target["group_id"]
        else:
            action = "send_private_msg"
            params["user_id"] = target["user_id"]
        out = await self.call(channel, action, params,
                             timeout=float(target.get("timeout", 90)), tag=tag)
        out["image_bytes"] = len(body)
        out["image_sha256"] = hashlib.sha256(body).hexdigest()
        return out

    def status(self) -> dict:
        return {
            "started_at": self.started_at, "seq": self.seq, "stats": self.stats,
            "channels": {str(k): v for k, v in self.conn_meta.items()},
            "connected": sorted(self.conns.keys()),
            "paths": {"events": self.events_path, "results": self.results_path,
                      "images": self.images_path},
        }

    async def control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    req = json.loads(line.decode("utf-8").strip())
                except Exception as exc:  # noqa: BLE001
                    writer.write((json.dumps({"ok": False, "error": f"bad json: {exc}"}) + "\n").encode())
                    await writer.drain()
                    continue
                cmd = req.get("cmd")
                try:
                    if cmd == "status":
                        resp = {"ok": True, **self.status()}
                    elif cmd == "channels":
                        resp = {"ok": True, "connected": sorted(self.conns.keys()),
                                "meta": {str(k): v for k, v in self.conn_meta.items()}}
                    elif cmd == "action":
                        out = await self.call(int(req.get("channel", 0)), req["action"],
                                              req.get("params") or {}, float(req.get("timeout", 10)),
                                              tag=str(req.get("tag") or ""))
                        resp = {"ok": True, "outcome": out}
                    elif cmd == "raw":
                        ws = self.conns.get(int(req.get("channel", 0)))
                        if ws is None:
                            resp = {"ok": False, "error": "WS 未连接"}
                        else:
                            await ws.send(json.dumps(req["payload"], ensure_ascii=False))
                            resp = {"ok": True, "sent": True}
                    elif cmd == "close":
                        ch = int(req.get("channel", 0))
                        ws = self.conns.get(ch)
                        if ws is not None:
                            await ws.close(code=1001, reason="probe requested")
                            resp = {"ok": True, "closed": ch}
                        else:
                            resp = {"ok": False, "error": "channel 未连接"}
                    elif cmd == "download_image":
                        resp = {"ok": True, "result": self.download_image(req["message_id"])}
                    elif cmd == "send_image_file":
                        resp = {"ok": True, "outcome": await self.send_image_file(
                            int(req.get("channel", 0)), req["path"],
                            req.get("target") or {}, tag=str(req.get("tag") or ""))}
                    elif cmd == "stop":
                        resp = {"ok": True, "stopping": True}
                        writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
                        await writer.drain()
                        os._exit(0)
                    else:
                        resp = {"ok": False, "error": f"unknown cmd {cmd}"}
                except Exception as exc:  # noqa: BLE001
                    resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                            "trace": traceback.format_exc()[-800:]}
                writer.write((json.dumps(resp, ensure_ascii=False, default=str) + "\n").encode())
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def run(self) -> None:
        os.makedirs(self.cfg.out_dir, exist_ok=True)
        tasks = [asyncio.create_task(self.serve_channel(i)) for i in range(len(self.cfg.ports))]
        # limit 放大：控制命令可能携带 base64 大图（默认 64KB 行上限会直接拒绝）
        ctrl = await asyncio.start_server(self.control, self.cfg.host, self.cfg.control_port,
                                          limit=64 * 1024 * 1024)
        self._jsonl(self.events_f, {"ts": now_iso(), "kind": "probe_start",
                                   "ports": self.cfg.ports, "control": self.cfg.control_port})
        print(f"[probe] listening ws={self.cfg.ports} control={self.cfg.control_port} out={self.cfg.out_dir}",
              flush=True)
        async with ctrl:
            await ctrl.serve_forever()


def parse_args() -> ProbeConfig:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--ports", type=int, nargs="+", default=[18080, 18081])
    ap.add_argument("--control", type=int, default=19080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--allow-uid", action="append", default=[])
    ap.add_argument("--allow-gid", action="append", default=[])
    ap.add_argument("--keep-text", action="store_true")
    ap.add_argument("--token", default="")
    a = ap.parse_args()
    return ProbeConfig(
        out_dir=a.out_dir, ports=a.ports, control_port=a.control,
        host=a.host, allow_uids=set(map(str, a.allow_uid)),
        allow_gids=set(map(str, a.allow_gid)), keep_text=a.keep_text, token=a.token,
    )


if __name__ == "__main__":
    cfg = parse_args()
    os.makedirs(cfg.out_dir, exist_ok=True)
    asyncio.run(Probe(cfg).run())
