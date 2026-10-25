# -*- coding: utf-8 -*-
"""阶段 B（headless）核心复测：动作/回执/顺序/图片/超时。只发到已批准目标。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import time
import zlib
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))
CTRL = 19080
STAGE = r"E:\QQbot\qq_native_re\findings\llbot_trial\logs\stageB"
UID = "784427550"
GID = "284517232"
OUT = os.path.join(STAGE, "evidence_actions_B.json")


def ctl(req: dict, timeout: float = 90.0) -> dict:
    with socket.create_connection(("127.0.0.1", CTRL), timeout=timeout) as s:
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            c = s.recv(1 << 20)
            if not c:
                break
            buf += c
    return json.loads(buf.decode())


def call(ch: int, action: str, params: dict, timeout: float = 20.0, tag: str = "") -> dict:
    r = ctl({"cmd": "action", "channel": ch, "action": action, "params": params,
             "timeout": timeout, "tag": tag})
    return r.get("outcome") or {"status": "error", "error": r}


def brief(o: dict) -> dict:
    d = o.get("data")
    b = {k: o.get(k) for k in ("status", "retcode", "latency_ms", "message_id", "error")}
    if isinstance(d, dict):
        b["data_keys"] = sorted(d.keys())
        if "messages" in d and isinstance(d["messages"], list):
            b["history_count"] = len(d["messages"])
            b["history_ids"] = [m.get("message_id") for m in d["messages"]][-5:]
            b["history_seq"] = [m.get("message_seq") for m in d["messages"]][-5:]
        if "message" in d and isinstance(d["message"], list):
            b["msg_types"] = [x.get("type") for x in d["message"] if isinstance(x, dict)]
        if "group_name" in d:
            b["group_name"] = d["group_name"]
        if "stat" in d:
            b["stat"] = d["stat"]
    elif isinstance(d, list):
        b["data_len"] = len(d)
    return b


def make_png(path: str, w: int = 96, h: int = 96) -> str:
    raw = b"".join(b"\x00" + bytes((30, 120, 200)) * w for _ in range(h))

    def chunk(tag: bytes, p: bytes) -> bytes:
        return struct.pack(">I", len(p)) + tag + p + struct.pack(">I", zlib.crc32(tag + p) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
           chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    open(path, "wb").write(png)
    return hashlib.sha256(png).hexdigest()


def main() -> int:
    out = {"started_at": datetime.now(TZ).isoformat(timespec="seconds"), "steps": {}}
    S = out["steps"]
    S["get_login_info"] = brief(call(0, "get_login_info", {}, tag="B1-login"))
    S["get_status"] = brief(call(0, "get_status", {}, tag="B1-status"))
    S["get_version_info"] = brief(call(0, "get_version_info", {}, tag="B1-version"))
    S["get_login_info_ch1"] = brief(call(1, "get_login_info", {}, tag="B1-login-ch1"))

    S["send_private_msg"] = brief(call(0, "send_private_msg", {
        "user_id": UID, "message": "【LLBot测试】B2 无头模式 send_private_msg；2026-10-24 21:32 请忽略"},
        15, tag="B2-send-private"))
    S["send_group_msg"] = brief(call(0, "send_group_msg", {
        "group_id": GID, "message": "【LLBot测试】无头模式兼容性测试，请忽略（2026-10-24 21:32）"},
        20, tag="B2-send-group"))
    if S["send_private_msg"].get("message_id"):
        S["get_msg_private"] = brief(call(0, "get_msg", {"message_id": S["send_private_msg"]["message_id"]}, 15, tag="B2-getmsg-private"))
    if S["send_group_msg"].get("message_id"):
        S["get_msg_group"] = brief(call(0, "get_msg", {"message_id": S["send_group_msg"]["message_id"]}, 15, tag="B2-getmsg-group"))

    S["get_friend_msg_history"] = brief(call(0, "get_friend_msg_history",
        {"user_id": UID, "count": 10, "message_seq": "0", "reverse_order": False,
         "disable_get_url": True}, 20, tag="B2-friend-history"))
    S["get_group_msg_history"] = brief(call(0, "get_group_msg_history",
        {"group_id": GID, "count": 5, "message_seq": "0", "reverse_order": False}, 20, tag="B2-group-history"))
    S["get_group_member_list"] = brief(call(0, "get_group_member_list", {"group_id": GID}, 20, tag="B2-members"))
    S["get_group_info"] = brief(call(0, "get_group_info", {"group_id": GID}, 15, tag="B2-group-info"))
    S["get_friend_list"] = brief(call(0, "get_friend_list", {}, 15, tag="B2-friends"))

    img = r"E:\QQbot-trials\data\llbot_test_stageB.png"
    sha = make_png(img)
    S["send_private_image"] = brief(ctl({"cmd": "send_image_file", "channel": 0, "path": img,
        "target": {"type": "private", "user_id": UID, "timeout": 60}, "tag": "B2-image"})["outcome"])
    S["send_private_image"]["png_sha256"] = sha

    order = []
    for i in range(1, 5):
        o = call(0, "send_private_msg", {"user_id": UID,
                 "message": f"【LLBot测试】B3-order-{i}（乱序检查，请忽略）"}, 15, tag=f"B3-order-{i}")
        order.append({"i": i, "status": o.get("status"), "message_id": o.get("message_id")})
        time.sleep(0.12)
    S["order_send"] = order
    time.sleep(1.0)
    h = call(0, "get_friend_msg_history", {"user_id": UID, "count": 8, "message_seq": "0",
             "reverse_order": False, "disable_get_url": True}, 20, tag="B3-history")
    S["order_history"] = brief(h)
    S["order_history_tail"] = [{"message_id": m.get("message_id"), "message_seq": m.get("message_seq"),
                                "raw_len": len(str(m.get("raw_message") or "")),
                                "types": [x.get("type") for x in (m.get("message") or []) if isinstance(x, dict)]}
                               for m in ((h.get("data") or {}).get("messages") or [])[-8:]]

    t = call(1, "send_private_msg", {"user_id": UID,
             "message": "【LLBot测试】B4 超时 unknown 验证（请忽略）"}, timeout=0.05, tag="B4-timeout")
    S["timeout_call"] = {k: t.get(k) for k in ("status", "retcode", "latency_ms", "message_id", "error")}

    out["finished_at"] = datetime.now(TZ).isoformat(timespec="seconds")
    os.makedirs(STAGE, exist_ok=True)
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps({k: (v.get("status") if isinstance(v, dict) else v) for k, v in S.items()},
                     ensure_ascii=False, indent=2)[:3000])
    print("saved", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
