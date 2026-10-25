# -*- coding: utf-8 -*-
"""ws_probe.py 的本机控制客户端。

用法：
  python probe_client.py status
  python probe_client.py channels
  python probe_client.py action 0 get_login_info {}
  python probe_client.py action 0 get_status {} 5
  python probe_client.py close 0
  python probe_client.py download_image 123456
  python probe_client.py stop
  python probe_client.py raw 0 '{"action":"send_private_msg","params":{...},"echo":"x"}'
"""

from __future__ import annotations

import json
import socket
import sys


def send(req: dict, host: str = "127.0.0.1", port: int = 19080, timeout: float = 30.0) -> dict:
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode("utf-8"))


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "action":
        if len(argv) < 4:
            print("usage: action <channel> <action> <params_json> [timeout]")
            return 2
        req = {"cmd": "action", "channel": int(argv[1]), "action": argv[2],
               "params": json.loads(argv[3])}
        if len(argv) > 4:
            req["timeout"] = float(argv[4])
    elif cmd in ("status", "channels", "stop"):
        req = {"cmd": cmd}
    elif cmd == "close":
        req = {"cmd": "close", "channel": int(argv[1])}
    elif cmd == "download_image":
        req = {"cmd": "download_image", "message_id": argv[1]}
    elif cmd == "raw":
        req = {"cmd": "raw", "channel": int(argv[1]), "payload": json.loads(argv[2])}
    else:
        print(f"unknown command: {cmd}")
        return 2
    resp = send(req)
    print(json.dumps(resp, ensure_ascii=False, indent=2))
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
