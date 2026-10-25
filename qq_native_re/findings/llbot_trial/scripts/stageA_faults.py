# -*- coding: utf-8 -*-
"""A4 故障注入：重复补报分析 / action 超时 unknown / 大图片 / 双通道独立发送。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import socket
import struct
import time
import zlib
from collections import Counter
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))
CTRL = 19080
STAGE = r"E:\QQbot\qq_native_re\findings\llbot_trial\logs\stageA"
UID = "784427550"
OUT = os.path.join(STAGE, "evidence_faults.json")


def ctl(req: dict, timeout: float = 60.0) -> dict:
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


def png_noise(path: str, w: int, h: int) -> str:
    rnd = random.Random(20261024)
    rows = []
    for _ in range(h):
        row = bytes(rnd.randrange(256) for _ in range(w * 3))
        rows.append(b"\x00" + row)
    raw = b"".join(rows)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload +
                struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n" +
           chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
           chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))
    open(path, "wb").write(png)
    return hashlib.sha256(png).hexdigest()


def main() -> int:
    out: dict = {"started_at": datetime.now(TZ).isoformat(timespec="seconds")}

    # 1) 重复补报分析：同一 message_id 在每通道出现次数
    events = [json.loads(x) for x in open(os.path.join(STAGE, "probe", "events.jsonl"), encoding="utf-8") if x.strip()]
    cnt = Counter((str(e.get("message_id")), e.get("channel")) for e in events if e.get("post_type") == "message")
    dup = {f"{mid}|ch{ch}": n for (mid, ch), n in cnt.items() if n > 1}
    msg_ids = {str(e.get("message_id")) for e in events if e.get("post_type") == "message"}
    out["duplicate_report"] = {
        "distinct_message_ids": len(msg_ids),
        "message_id_channel_rows": len(cnt),
        "duplicates": dup,
        "duplicate_count": len(dup),
    }

    # 2) action 超时 → unknown（用通道 1，验证双通道都可发；极短超时制造 unknown）
    tag = f"A4-timeout-{int(time.time())}"
    t = call(1, "send_private_msg", {
        "user_id": UID,
        "message": f"【LLBot测试】A4 超时 unknown 验证（{tag}，请忽略）",
    }, timeout=0.05, tag="A4-timeout-unknown")
    out["timeout_call"] = {k: t.get(k) for k in ("status", "retcode", "latency_ms", "message_id", "error", "echo")}
    # 等待后查历史：确认“unknown 但可能已送达”
    time.sleep(2.5)
    hist = call(0, "get_friend_msg_history",
                {"user_id": UID, "count": 10, "message_seq": "0", "reverse_order": False,
                 "disable_get_url": True}, 20, tag="A4-timeout-history")
    out["timeout_history"] = {
        "status": hist.get("status"),
        "found_delivered": any(tag in str(m.get("raw_message") or "")
                               for m in ((hist.get("data") or {}).get("messages") or [])),
        "ids": [m.get("message_id") for m in ((hist.get("data") or {}).get("messages") or [])][-5:],
    }

    # 3) 大图片（约 1600x1600 噪声 PNG，仅供参考的体积测试）
    big = r"E:\QQbot-trials\data\llbot_big_test.png"
    sha = png_noise(big, 1600, 1600)
    size = os.path.getsize(big)
    b64 = base64.b64encode(open(big, "rb").read()).decode("ascii")
    t0 = time.time()
    big_resp = ctl({"cmd": "send_image_file", "channel": 0, "path": big,
                    "target": {"type": "private", "user_id": UID, "timeout": 90},
                    "tag": "A4-big-image"}, timeout=120)
    big_out = big_resp.get("outcome") or {"status": "error", "error": big_resp}
    out["big_image"] = {
        "png_bytes": size, "png_sha256": sha,
        "status": big_out.get("status"), "retcode": big_out.get("retcode"),
        "message_id": big_out.get("message_id"), "latency_ms": big_out.get("latency_ms"),
        "wall_ms": int((time.time() - t0) * 1000),
    }
    if big_out.get("message_id"):
        gm = call(0, "get_msg", {"message_id": big_out["message_id"]}, 20, tag="A4-big-image-getmsg")
        data = gm.get("data") or {}
        types = [x.get("type") for x in (data.get("message") or []) if isinstance(x, dict)]
        out["big_image"]["get_msg_status"] = gm.get("status")
        out["big_image"]["get_msg_types"] = types
        out["big_image"]["raw_message_len"] = len(str(data.get("raw_message") or ""))

    out["finished_at"] = datetime.now(TZ).isoformat(timespec="seconds")
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2)[:3500])
    print("saved", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
