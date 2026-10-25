# -*- coding: utf-8 -*-
"""阶段 A 动作/顺序/图片测试驱动（通过 ws_probe 控制端口），输出脱敏证据。

只向已批准目标发消息：
  - 私聊 784427550（用户本人，已批准）
  - 群 284517232（已批准，消息带【LLBot测试】标识）

产出：logs/stageA/evidence_actions.json（脱敏：不含消息正文/图片 URL token）
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys
import time
import zlib
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))
CTRL = 19080
OUT = r"E:\QQbot\qq_native_re\findings\llbot_trial\logs\stageA\evidence_actions.json"
IMG = r"E:\QQbot-trials\data\llbot_test_pixel.png"
UID = "784427550"
GID = "284517232"
BOT = "2668851638"
TEST_GROUP_MSG_ID = 2127657647  # 前面的测试群消息

results: dict = {"started_at": datetime.now(TZ).isoformat(timespec="seconds"), "steps": {}}


def ctl(req: dict, timeout: float = 40.0) -> dict:
    with socket.create_connection(("127.0.0.1", CTRL), timeout=timeout) as s:
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode("utf-8"))


def call(channel: int, action: str, params: dict, timeout: float = 15.0, tag: str = "") -> dict:
    t0 = time.time()
    resp = ctl({"cmd": "action", "channel": channel, "action": action, "params": params,
                "timeout": timeout, "tag": tag})
    out = resp.get("outcome") or {"status": "error", "error": resp}
    out["elapsed_ms"] = int((time.time() - t0) * 1000)
    return out


def brief(o: dict) -> dict:
    """把 outcome 压成脱敏摘要。"""
    data = o.get("data")
    b = {"status": o.get("status"), "retcode": o.get("retcode"),
         "latency_ms": o.get("latency_ms"), "echo": o.get("echo"),
         "message_id": o.get("message_id"), "error": o.get("error", "")}
    if isinstance(data, dict):
        b["data_keys"] = sorted(data.keys())
        if "message_id" in data:
            b["data_message_id"] = data["message_id"]
        if "messages" in data and isinstance(data["messages"], list):
            msgs = data["messages"]
            b["history_count"] = len(msgs)
            b["history"] = [{
                "message_id": m.get("message_id"),
                "message_seq": m.get("message_seq"),
                "message_type": m.get("message_type"),
                "time": m.get("time"),
                "post_type": m.get("post_type"),
                "sender_id": (m.get("sender") or {}).get("user_id"),
                "message_types": [x.get("type") for x in (m.get("message") or []) if isinstance(x, dict)],
                "raw_message_len": len(str(m.get("raw_message") or "")),
                "has_raw": "raw" in m, "has_raw_pb": "raw_pb" in m,
            } for m in msgs]
        if "message" in data and isinstance(data["message"], list):
            b["msg_types"] = [x.get("type") for x in data["message"] if isinstance(x, dict)]
        if "raw_message" in data:
            b["raw_message_len"] = len(str(data["raw_message"]))
        for k in ("online", "good", "user_id", "nickname", "group_name", "member_count"):
            if k in data:
                b[k] = data[k]
        if "stat" in data:
            b["stat"] = data["stat"]
        if isinstance(data.get("members"), list):
            b["member_count"] = len(data["members"])
            roles = {}
            for m in data["members"]:
                roles[str(m.get("role"))] = roles.get(str(m.get("role")), 0) + 1
            b["roles"] = roles
        if isinstance(data.get("friends"), list):
            b["friend_count"] = len(data["friends"])
        if "notices" in data:
            b["notice_count"] = len(data["notices"]) if isinstance(data["notices"], list) else None
    elif isinstance(data, list):
        b["data_len"] = len(data)
    return b


def make_png(path: str, w: int = 64, h: int = 64, rgb=(200, 60, 60)) -> str:
    """生成一个纯色 PNG（避免依赖第三方库）。"""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload +
                struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n" +
           chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
           chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)
    import hashlib
    return hashlib.sha256(png).hexdigest()


def main() -> int:
    # 0) 基础
    results["steps"]["get_login_info_ch1"] = brief(call(1, "get_login_info", {}, tag="A1-login-ch1"))
    results["steps"]["get_status_ch1"] = brief(call(1, "get_status", {}, tag="A1-status-ch1"))

    # 1) 群历史 / 群信息 / 群成员 / 好友列表
    results["steps"]["get_group_msg_history"] = brief(call(
        0, "get_group_msg_history",
        {"group_id": GID, "count": 5, "message_seq": "0", "reverse_order": False}, 15,
        tag="A2-group-history"))
    results["steps"]["get_group_info"] = brief(call(0, "get_group_info", {"group_id": GID}, 15, tag="A2-group-info"))
    results["steps"]["get_group_member_list"] = brief(call(
        0, "get_group_member_list", {"group_id": GID}, 20, tag="A2-group-members"))
    results["steps"]["get_friend_list"] = brief(call(0, "get_friend_list", {}, 15, tag="A2-friend-list"))

    # 2) 图片接收：下载前面群图片事件
    dl = ctl({"cmd": "download_image", "message_id": "-477010617"}, timeout=40)
    results["steps"]["download_group_image"] = dl.get("result") or dl

    # 3) 图片发送（私聊，base64 数组；与生产 send_group_image 的接线一致）
    sha = make_png(IMG)
    b64 = base64.b64encode(open(IMG, "rb").read()).decode("ascii")
    img_out = call(0, "send_private_msg", {
        "user_id": UID,
        "message": [{"type": "image", "data": {"file": f"base64://{b64}"}}],
    }, 30, tag="A2-send-private-image")
    results["steps"]["send_private_image"] = brief(img_out)
    results["steps"]["send_private_image"]["png_sha256"] = sha
    if img_out.get("message_id"):
        results["steps"]["get_msg_sent_image"] = brief(call(
            0, "get_msg", {"message_id": img_out["message_id"]}, 15, tag="A2-get-msg-image"))

    # 4) 回复 + @（生产用 CQ 字符串形态）—— 回复自己刚才的测试群消息，@bot 自己，避免打扰群友
    cq = f"[CQ:reply,id={TEST_GROUP_MSG_ID}][CQ:at,qq={BOT}] 【LLBot测试】reply+@ 兼容性验证，请忽略（2026-10-24 20:46）"
    rep = call(0, "send_group_msg", {"group_id": GID, "message": cq}, 20, tag="A2-group-cq-reply-at")
    results["steps"]["send_group_cq_reply_at"] = brief(rep)
    if rep.get("message_id"):
        results["steps"]["get_msg_reply_at"] = brief(call(
            0, "get_msg", {"message_id": rep["message_id"]}, 15, tag="A2-get-msg-reply-at"))

    # 5) 顺序：连续 6 条私聊文本 + 图片后紧跟文字
    order = []
    for i in range(1, 7):
        o = call(0, "send_private_msg", {
            "user_id": UID,
            "message": f"【LLBot测试】A3-order-{i}（乱序检查，请忽略）",
        }, 15, tag=f"A3-order-{i}")
        order.append({"i": i, "status": o.get("status"), "message_id": o.get("message_id"),
                      "latency_ms": o.get("latency_ms")})
        time.sleep(0.15)
    o_img = call(0, "send_private_msg", {
        "user_id": UID,
        "message": [{"type": "image", "data": {"file": f"base64://{b64}"}}],
    }, 30, tag="A3-image-then-text-img")
    o_txt = call(0, "send_private_msg", {
        "user_id": UID,
        "message": "【LLBot测试】A3-image-then-text-文字（请忽略）",
    }, 15, tag="A3-image-then-text-text")
    order.append({"i": "img", "status": o_img.get("status"), "message_id": o_img.get("message_id"),
                  "latency_ms": o_img.get("latency_ms")})
    order.append({"i": "txt", "status": o_txt.get("status"), "message_id": o_txt.get("message_id"),
                  "latency_ms": o_txt.get("latency_ms")})
    results["steps"]["order_send"] = order
    time.sleep(1.0)
    hist = call(0, "get_friend_msg_history",
                {"user_id": UID, "count": 12, "message_seq": "0", "reverse_order": False,
                 "disable_get_url": True}, 20, tag="A3-order-history")
    results["steps"]["order_history"] = brief(hist)
    # 只保留与 A3 相关的最后 10 条（脱敏）
    if isinstance(hist.get("data"), dict):
        msgs = hist["data"].get("messages") or []
        results["steps"]["order_history_tail"] = [{
            "message_id": m.get("message_id"), "message_seq": m.get("message_seq"),
            "sender_id": (m.get("sender") or {}).get("user_id"),
            "types": [x.get("type") for x in (m.get("message") or []) if isinstance(x, dict)],
            "raw_len": len(str(m.get("raw_message") or "")),
            "text_has_A3_tag": "A3-order" in str(m.get("raw_message") or ""),
        } for m in msgs[-10:]]

    # 6) 扩展只读接口
    results["steps"]["get_group_notice"] = brief(call(0, "get_group_notice", {"group_id": GID}, 15, tag="A2-ext-notice"))
    results["steps"]["get_group_list"] = brief(call(0, "get_group_list", {}, 15, tag="A2-group-list"))
    results["steps"]["get_forward_msg_probe"] = {"skipped": "未构造合并转发，见 TEST_MATRIX"}

    results["finished_at"] = datetime.now(TZ).isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: (v.get("status") if isinstance(v, dict) else v)
                      for k, v in results["steps"].items()}, ensure_ascii=False, indent=2)[:4000])
    print("saved", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
