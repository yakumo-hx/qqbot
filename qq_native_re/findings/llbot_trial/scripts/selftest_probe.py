# -*- coding: utf-8 -*-
"""ws_probe.py 的离线自测：用假 OneBot 客户端验证探针本身可用。

验证点：
  1. 探针在 18080/18081 同时监听；
  2. 假客户端连入后，探针记录 ws_connected；
  3. 假客户端上报 meta_event 心跳 + 群消息，探针正确分类与脱敏；
  4. 控制端口发 action → 假客户端按 echo 回 ok，探针判定 ok 且拿到 message_id；
  5. 假客户端不回 → 探针判定 unknown（超时）；
  6. close 命令断开后，探针记录 ws_closed；
  7. 结束后自动停止探针（控制端 stop，父进程 wait；不按进程名杀）。

运行：python selftest_probe.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = os.path.join(HERE, "ws_probe.py")
PY = sys.executable
CTRL = 19080


def ctl(req: dict, timeout: float = 40.0) -> dict:
    with socket.create_connection(("127.0.0.1", CTRL), timeout=timeout) as s:
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode())


async def _fake_client(stop_evt: threading.Event) -> None:
    async with websockets.connect("ws://127.0.0.1:18080") as ws:
        await ws.send(json.dumps({
            "post_type": "meta_event", "meta_event_type": "heartbeat",
            "status": {"online": True, "good": True}, "interval": 30000,
            "time": int(time.time()), "self_id": 2668851638,
        }))
        await ws.send(json.dumps({
            "post_type": "message", "message_type": "group", "sub_type": "normal",
            "time": int(time.time()), "self_id": 2668851638, "group_id": 1055881417,
            "user_id": 10001, "message_id": 900001,
            "raw_message": "测试 [CQ:at,qq=2668851638]",
            "font": 0, "message_format": "array",
            "sender": {"user_id": 10001, "nickname": "测试员", "card": "", "role": "member"},
            "message": [
                {"type": "at", "data": {"qq": "2668851638"}},
                {"type": "text", "data": {"text": "测试"}},
                {"type": "image", "data": {"file": "abc.image", "url": "https://example.invalid/x.png",
                                           "file_size": 12345, "summary": "[图片]"}},
            ],
        }, ensure_ascii=False))
        while not stop_evt.is_set():
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except websockets.exceptions.ConnectionClosed:
                return
            data = json.loads(msg)
            if data.get("action") == "slow_action":
                continue
            await ws.send(json.dumps({
                "status": "ok", "retcode": 0, "echo": data.get("echo"),
                "data": {"message_id": 123456789, "user_id": 2668851638,
                         "nickname": "bot", "online": True, "good": True},
            }))


def fake_client_thread(stop_evt: threading.Event) -> None:
    import asyncio
    try:
        asyncio.run(_fake_client(stop_evt))
    except Exception:
        pass


def main() -> int:
    out = os.path.join(tempfile.gettempdir(), f"probe_selftest_{int(time.time())}")
    os.makedirs(out, exist_ok=True)
    proc = subprocess.Popen([PY, PROBE, "--out-dir", out, "--ports", "18080", "18081",
                             "--control", str(CTRL)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    ready = False
    for _ in range(50):
        time.sleep(0.2)
        try:
            ctl({"cmd": "status"}, timeout=2)
            ready = True
            break
        except Exception:
            if proc.poll() is not None:
                print(proc.stdout.read())
                return 1
    if not ready:
        print("probe did not start:", proc.stdout.read())
        proc.kill()
        return 1

    failures: list[str] = []
    stop_evt = threading.Event()
    t = threading.Thread(target=fake_client_thread, args=(stop_evt,), daemon=True)
    t.start()
    time.sleep(1.0)

    st = ctl({"cmd": "status"})
    if 0 not in st.get("connected", []):
        failures.append(f"channel0 not connected: {st}")
    ev = ctl({"cmd": "action", "channel": 0, "action": "get_login_info",
              "params": {}, "timeout": 5})
    o = ev["outcome"]
    if o["status"] != "ok" or o["message_id"] != 123456789:
        failures.append(f"action ok path failed: {o}")
    to = ctl({"cmd": "action", "channel": 0, "action": "slow_action",
              "params": {}, "timeout": 1})
    if to["outcome"]["status"] != "unknown":
        failures.append(f"timeout path failed: {to}")
    ctl({"cmd": "close", "channel": 0})
    time.sleep(0.5)
    st2 = ctl({"cmd": "status"})
    if 0 in st2.get("connected", []):
        failures.append(f"close did not disconnect: {st2}")

    events = [json.loads(line) for line in
              open(os.path.join(out, "events.jsonl"), encoding="utf-8")]
    kinds = [e.get("kind") for e in events]
    if "ws_connected" not in kinds or "ws_disconnected" not in kinds:
        failures.append(f"connect/disconnect not logged: {kinds}")
    msg_ev = [e for e in events if e.get("post_type") == "message"]
    if len(msg_ev) != 1:
        failures.append(f"message events={len(msg_ev)}")
    else:
        m = msg_ev[0]
        if m.get("group_id") != 1055881417 or m.get("message_types") != ["at", "text", "image"]:
            failures.append(f"event fields wrong: {m}")
        if str(m.get("raw_message")).startswith("测试"):
            failures.append("raw_message not masked")
        if m.get("raw_message_len") != len("测试 [CQ:at,qq=2668851638]"):
            failures.append(f"raw_message_len wrong: {m.get('raw_message_len')}")
    hb = [e for e in events if e.get("post_type") == "meta_event"]
    if len(hb) != 1:
        failures.append(f"heartbeat events={len(hb)}")

    try:
        ctl({"cmd": "stop"})
    except Exception:
        pass
    stop_evt.set()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    result = {"out_dir": out, "failures": failures}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    import asyncio  # noqa: F401  (供线程内使用)
    raise SystemExit(main())
