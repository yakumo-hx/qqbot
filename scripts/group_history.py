# -*- coding: utf-8 -*-
"""群聊语料记录器 —— 被动把每条群消息追加成一行 JSONL。

定位：离线语言学习模块（language_lab）的**原始语料**。只落盘，不发送、不决策、不调模型。
纪律：任何异常都不得抛给调用方（语料是旁路）；入队非阻塞，队列满则丢弃并计数；
单 daemon 线程串行写，按「群 / 日期」分文件并逐行 flush；不存图片字节 / base64 / 直链。
`seq` 为进程内自增序号，保留收到顺序；规范语料由离线侧重新编号。

落盘结构：``<project_root>/<dir>/<gid>/<YYYY-MM-DD>.jsonl``
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

try:
    from scripts import message_index
except ImportError:  # pragma: no cover - 独立运行
    import message_index  # type: ignore

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
CONFIG_PATH = os.path.join(_PROJECT_ROOT, "config", "group_history.json")

ROW_VERSION = 1
DEFAULTS: Dict[str, Any] = {
    "enabled": True, "groups": [], "dir": "language_lab/data/group_history/_live",
    "max_queue": 2000, "bot_uids": [], "max_text_chars": 4000,
}
# 严格 CQ 码；兜底的未闭合 CQ 码（上游截断时会出现，不补刀会漏原文）
_CQ_RE = re.compile(r"\[CQ:(?P<type>[a-zA-Z_]+)(?P<params>(?:,[^\],]*)*)\]")
_CQ_LOOSE_RE = re.compile(r"\[CQ:[^\]]{0,2000}(?:\]|$)")
# CQ 段类型 → 媒体类别（at / reply 另处理，未知段整段丢弃）
_CQ_TYPE_KIND = {"image": "image", "mface": "face", "face": "face", "record": "voice",
                 "video": "video", "file": "file", "forward": "forward", "json": "link"}
_KIND_PLACEHOLDER = {"image": "[图片]", "face": "[表情]", "voice": "[语音]", "video": "[视频]",
                     "file": "[文件]", "forward": "[合并转发]", "link": "[链接]"}
# CQ 实体转义；`&amp;` 必须最后还原，否则会把 `&#91;` 拆坏
_ESCAPES = (("&#91;", "["), ("&#93;", "]"), ("&#44;", ","), ("&amp;", "&"))

_SENTINEL = object()


# ---------- 配置 ----------

_cfg_lock = threading.Lock()
_cfg: Optional[dict] = None
_test_cfg: Optional[dict] = None      # 单测覆盖配置
_test_dir: Optional[str] = None       # 单测落盘根目录


def load_config() -> dict:
    """读 config/group_history.json（缓存一次）；读不到 → 全默认，绝不抛。"""
    global _cfg
    if _test_cfg is not None:
        return {**DEFAULTS, **_test_cfg}
    with _cfg_lock:
        if _cfg is None:
            cfg = dict(DEFAULTS)
            try:
                with open(CONFIG_PATH, encoding="utf-8") as fh:
                    user = json.load(fh)
                cfg.update({k: user[k] for k in DEFAULTS if user.get(k) is not None})
            except Exception:
                pass
            _cfg = cfg
        return dict(_cfg)


def configure() -> dict:
    """启动时调一次：重读配置并确定落盘根目录；写入线程等首条消息才启动。"""
    global _cfg, _base_dir
    try:
        _cfg = None
        cfg = load_config()
        rel = str(cfg.get("dir") or DEFAULTS["dir"])
        _base_dir = _test_dir or (rel if os.path.isabs(rel)
                                  else os.path.normpath(os.path.join(_PROJECT_ROOT, rel)))
        return cfg
    except Exception:
        return dict(DEFAULTS)


def is_enabled_for(gid: Any) -> bool:
    """该群是否记录：groups 空 = 全记录，非空 = 白名单；异常返回 False。"""
    try:
        cfg = load_config()
        groups = cfg.get("groups")
        if not cfg.get("enabled", True):
            return False
        if groups is None or groups == []:
            return True
        return isinstance(groups, (list, tuple)) and str(gid) in {str(g) for g in groups}
    except Exception:
        return False


# ---------- CQ 解析（纯函数） ----------

def _unescape(text: str) -> str:
    """还原 CQ 实体转义（`&amp;` 最后还原，避免拆坏 `&#91;`）。"""
    for src, dst in _ESCAPES:
        text = text.replace(src, dst)
    return text


def _parse_params(raw_params: Any) -> Dict[str, str]:
    """`key=value,key=value` → dict；值里的 `&amp;` 还原。"""
    out: Dict[str, str] = {}
    for chunk in str(raw_params or "").split(","):
        key, sep, value = chunk.partition("=")
        if sep and key.strip():
            out[key.strip()] = value.strip().replace("&amp;", "&")
    return out


def parse_message(raw: Any) -> dict:
    """CQ 码消息 → {text, plain, media, at, reply_id}。纯函数，绝不抛。

    - `text`：at/reply/未知段剥掉，媒体段按顺序换成占位符（`[图片]` 等）
    - `plain`：整段 CQ 都剥掉的结果——只用它判断 kind（纯图片消息 → kind=image）
    """
    out: Dict[str, Any] = {"text": "", "plain": "", "media": [], "at": [], "reply_id": ""}

    def repl(m: "re.Match[str]") -> str:
        ctype = (m.group("type") or "").lower()
        params = _parse_params(m.group("params"))
        if ctype == "at":
            qq = params.get("qq", "").strip()
            if qq and qq not in out["at"]:
                out["at"].append(qq)
        elif ctype == "reply":
            out["reply_id"] = out["reply_id"] or params.get("id", "").strip()
        elif ctype in _CQ_TYPE_KIND:
            out["media"].append(_CQ_TYPE_KIND[ctype])
            return _KIND_PLACEHOLDER[_CQ_TYPE_KIND[ctype]]
        return ""

    try:
        s = "" if raw is None else str(raw)
        out["text"] = _unescape(_CQ_LOOSE_RE.sub("", _CQ_RE.sub(repl, s))).strip()
        out["plain"] = _unescape(_CQ_LOOSE_RE.sub("", _CQ_RE.sub("", s))).strip()
    except Exception:
        pass
    return out


# ---------- 行构造 ----------

_seq_lock = threading.Lock()
_seq = 0


def _reply_to(gid: str, reply_id: str) -> Optional[dict]:
    """引用快照：只给 id；命中 message_index 时补 uid/nick/text（text 截 200 字）。"""
    if not reply_id:
        return None
    hit: Any = None
    try:
        hit = message_index.get(gid, reply_id)
    except Exception:
        hit = None
    if not isinstance(hit, dict):
        return {"id": reply_id}
    return {"id": reply_id, "uid": str(hit.get("uid") or ""),
            "nick": str(hit.get("card") or hit.get("nickname") or ""),
            "text": str(hit.get("text") or "")[:200]}


def _build_row(gid: str, data: dict, ref_record: Any) -> dict:
    """事件 → 一行记录（不写盘；任何失败由 record() 兜）。"""
    global _seq
    cfg = load_config()
    parsed = parse_message(data.get("raw_message"))
    sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
    uid = str(data.get("user_id") or sender.get("user_id") or "")
    with _seq_lock:
        _seq += 1
        seq = _seq
    try:
        limit = int(cfg.get("max_text_chars") or 4000)
    except (TypeError, ValueError):
        limit = 4000
    try:
        ts_epoch = float((ref_record or {}).get("ts"))
        if not (math.isfinite(ts_epoch) and ts_epoch > 0):
            ts_epoch = time.time()
    except Exception:
        ts_epoch = time.time()
    media, plain = parsed["media"], parsed["plain"]
    kind = "text" if plain else (str(media[0]) if media else
                                 ("at_only" if parsed["at"] else "unknown"))
    return {
        "v": ROW_VERSION,
        "gid": gid,
        "seq": seq,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ts_epoch": ts_epoch,
        "uid": uid,
        "nick": str(sender.get("nickname") or ""),
        "card": str(sender.get("card") or ""),
        "kind": kind,
        "text": parsed["text"][:limit] if limit > 0 else parsed["text"],
        "media": list(media),
        "reply_to": _reply_to(gid, str(parsed["reply_id"])),
        "at": list(parsed["at"]),
        "is_bot": uid in {str(b) for b in (cfg.get("bot_uids") or []) if str(b)},
        "mid": data.get("message_id"),
        "src": "recorder",
    }


# ---------- 写入线程（模块级单例） ----------

_life_lock = threading.Lock()
_stats_lock = threading.Lock()
_queue: Optional[queue.Queue] = None
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_base_dir: Optional[str] = None
_handle = None
_handle_path = ""
_stats = {"written": 0, "dropped": 0, "errors": 0}


def _bump(key: str) -> None:
    with _stats_lock:
        _stats[key] += 1


def _close_handle() -> None:
    global _handle, _handle_path
    handle, _handle, _handle_path = _handle, None, ""
    if handle is not None:
        try:
            handle.close()
        except Exception:
            pass


def _ensure_started() -> queue.Queue:
    """惰性创建队列与 daemon 写入线程（幂等）。"""
    global _queue, _thread, _stop, _base_dir
    with _life_lock:
        if _queue is not None and _thread is not None and _thread.is_alive():
            return _queue
        if not _base_dir:
            rel = str(load_config().get("dir") or DEFAULTS["dir"])
            _base_dir = _test_dir or (rel if os.path.isabs(rel)
                                      else os.path.normpath(os.path.join(_PROJECT_ROOT, rel)))
        try:
            size = int(load_config().get("max_queue") or 2000)
        except (TypeError, ValueError):
            size = 2000
        _queue = queue.Queue(maxsize=size if size > 0 else 2000)
        _stop = threading.Event()
        _thread = threading.Thread(target=_writer_loop, args=(_queue, _stop),
                                   name="group-history-writer", daemon=True)
        _thread.start()
        return _queue


def _writer_loop(q: queue.Queue, stop: threading.Event) -> None:
    """串行落盘；任何写入异常只计 errors，循环绝不退出。"""
    global _handle, _handle_path
    while True:
        try:
            item = q.get(timeout=0.2)
        except queue.Empty:
            if stop.is_set():
                break
            continue
        try:
            if item is _SENTINEL:
                break
            gid, day, row = item
            path = os.path.join(_base_dir, gid, day + ".jsonl")
            if path != _handle_path:
                _close_handle()
                os.makedirs(os.path.dirname(path), exist_ok=True)
                _handle = open(path, "a", encoding="utf-8", newline="\n")
                _handle_path = path
            _handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            _handle.flush()
            _bump("written")
        except Exception:
            _bump("errors")
    _close_handle()


# ---------- 对外入口 ----------

def record(gid: Any, data: Any, ref_record: Optional[dict] = None) -> None:
    """把一条群消息入队（非阻塞）。任何异常都在这里吞掉——语料只是旁路。"""
    try:
        if not isinstance(data, dict):
            return
        gid_s = str(gid or "").strip()
        if not gid_s or not is_enabled_for(gid_s):
            return
        row = _build_row(gid_s, data, ref_record)
        try:
            _ensure_started().put_nowait((gid_s, str(row["ts"])[:10], row))
        except queue.Full:
            _bump("dropped")
    except Exception:
        pass


def stats() -> dict:
    """{"written","dropped","errors","queue"}；任何异常都给零值。"""
    try:
        with _stats_lock:
            return {**_stats, "queue": _queue.qsize() if _queue is not None else 0}
    except Exception:
        return {"written": 0, "dropped": 0, "errors": 0, "queue": 0}


def shutdown() -> None:
    """唤醒写入线程、排空队列并等待退出（单测用来断言落盘结果）。"""
    global _queue, _thread
    try:
        with _life_lock:
            if _queue is None or _thread is None:
                return
            _stop.set()
            try:
                _queue.put_nowait(_SENTINEL)
            except Exception:
                pass
            thread = _thread
        thread.join(timeout=5.0)
        with _life_lock:
            _thread = _queue = None
    except Exception:
        pass


def _configure_for_tests(base_dir: str, **overrides: Any) -> dict:
    """仅供离线单测：指定落盘根目录、覆盖配置并重置计数，不碰生产配置。"""
    global _test_cfg, _test_dir, _base_dir
    shutdown()
    _test_dir = os.path.abspath(str(base_dir))
    _test_cfg = {**DEFAULTS, **overrides}
    with _stats_lock:
        for key in _stats:
            _stats[key] = 0
    _base_dir = _test_dir
    return load_config()
