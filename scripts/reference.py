# -*- coding: utf-8 -*-
"""消息指代注解层 —— 把 NapCat 事件变成「带明确指代」的结构化记录与上下文行。

设计依据：`qq_native_re/findings/reference/DESIGN.md`

职责边界：
- **本模块是纯函数层**（除配置读取外无副作用），便于离线回放与单测
- 它**不发送消息**、**不改决策**、**不调模型**
- 它只做两件事：① 从事件里把指代信息抽出来 ② 把它渲染成模型能读懂的一行

渲染格式（DESIGN.md §5）：
  `[UC] 今天天气不错`
  `[UC@阿强] 别说了`
  `[UC 回复阿强「我明天可能来不了」] 你说得对`
  `[UC 回复某条消息] 你说得对`          ← 超出缓存窗口，降级不编造
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

try:
    from scripts import message_index, roster as roster_mod
except ImportError:  # pragma: no cover - 独立运行
    import message_index  # type: ignore
    import roster as roster_mod  # type: ignore

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
CONFIG_PATH = os.path.join(_PROJECT_ROOT, "config", "reference.json")

# CQ 码：`[CQ:image,key=value,key=value]`（值里不含逗号与右括号）——**用于抽取**，严格
_CQ_RE = re.compile(r"\[CQ:(?P<type>[a-zA-Z_]+)(?P<params>(?:,[^\],]*)*)\]")

# 兜底：**未闭合**的 CQ 码（上游异常 / 日志截断时会见到）。
# 实测：NapCat 的 `CQ:face` 带 `raw={...}` 长参数，日志里会被截断成没有 `]` 的样子；
# 严格正则匹配不到 → 整段 CQ 原文会漏进模型上下文。这条负责把这种尾巴也切掉。
_CQ_LOOSE_RE = re.compile(r"\[CQ:[^\]]{0,2000}(?:\]|$)")

# 输出侧剥离：防止 LLM 模仿上下文格式。
# 旧形态 `[昵称]: 内容`（原有能力，必须保留）；新形态 `[昵称@某人] …` / `[昵称 回复某人「…」] …`。
# 只在括号内容**确实含 @ 或 回复**时才剥——否则会误伤模型正常输出的 `[笑] 好吧`。
_REF_PREFIX_RE = re.compile(
    r"^\s*\[[^\]\n]{1,60}\][:：]\s*"
    r"|^\s*\[[^\]\n]{1,80}(?:@|回复)[^\]\n]{0,80}\]\s*"
)

DEFAULTS = {
    "enabled": True,
    "groups": [],
    "index_max_per_group": 200,
    "quote_max_chars": 40,
    "roster_path": "data/roster.json",
    "roster_retention_days": 180,
    "roster_card_history_max": 5,
    "roster_flush_seconds": 300,
    "debug_log": False,
}

_cfg_lock = threading.Lock()
_cfg_cache: Dict[str, Any] = {"data": None, "mtime": 0.0}


# ---------- 配置 ----------

def load_config() -> dict:
    """读 config/reference.json，带 mtime 缓存。读不到 → 全默认（不抛）。"""
    cfg = dict(DEFAULTS)
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return cfg
    with _cfg_lock:
        if _cfg_cache["data"] is None or mtime != _cfg_cache["mtime"]:
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                _cfg_cache["data"] = raw if isinstance(raw, dict) else {}
                _cfg_cache["mtime"] = mtime
            except Exception:
                _cfg_cache["data"] = {}
                _cfg_cache["mtime"] = mtime
        user = _cfg_cache["data"] or {}
    for key in DEFAULTS:
        if key in user and user[key] is not None:
            cfg[key] = user[key]
    return cfg


def configure() -> dict:
    """按配置初始化消息索引与名册。由 reverse_bot 启动时调用一次。"""
    cfg = load_config()
    try:
        message_index.configure(int(cfg.get("index_max_per_group") or 200))
    except Exception:
        pass
    try:
        rel = str(cfg.get("roster_path") or "data/roster.json")
        path = rel if os.path.isabs(rel) else os.path.join(_PROJECT_ROOT, rel)
        roster_mod.configure(
            path,
            retention_days=int(cfg.get("roster_retention_days") or 180),
            card_history_max=int(cfg.get("roster_card_history_max") or 5),
            flush_seconds=int(cfg.get("roster_flush_seconds") or 300),
        )
    except Exception:
        pass
    return cfg


def is_enabled() -> bool:
    """总开关（不含按群过滤）。"""
    return bool(load_config().get("enabled", True))


def is_enabled_for(gid: Any) -> bool:
    """该群是否启用指代。

    `groups` 为空数组 = 所有群启用；非空 = 仅列出的群启用（试运行期用来收窄影响面）。
    任何异常都返回 False：宁可注解层不工作，也不要意外改动某个群的行为。
    """
    try:
        cfg = load_config()
        if not cfg.get("enabled", True):
            return False
        groups = cfg.get("groups")
        if groups is None:
            return True                                   # 未配置 → 不限制
        if not isinstance(groups, (list, tuple)):
            return False                                  # 配置写坏了 → 保守关闭
        if len(groups) == 0:
            return True                                   # 空列表 → 不限制
        return str(gid) in {str(g) for g in groups}
    except Exception:
        return False


def debug_on() -> bool:
    """是否把每条渲染结果打一行日志（试运行期观察用）。"""
    try:
        return bool(load_config().get("debug_log", False))
    except Exception:
        return False


def render_for(gid: Any, record: Dict[str, Any]) -> str:
    """便捷入口：按 gid 解析引用并渲染成一行。

    上下文渲染与调试日志共用这一条路径——保证「日志里看到的」就是「模型看到的」。
    """
    try:
        cfg = load_config()
        quote = resolve_quote(gid, record)
        return render(
            record, quote,
            quote_max_chars=int(cfg.get("quote_max_chars") or 40),
            name_of=lambda u: roster_mod.display_of(gid, u),
        )
    except Exception:
        return ""


# ---------- CQ 解析（纯函数） ----------

def _parse_params(raw_params: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for chunk in str(raw_params or "").split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        key = key.strip()
        if key:
            out[key] = value.strip().replace("&amp;", "&")
    return out


def extract_at(raw_message: Any) -> List[str]:
    """抽取 `[CQ:at,qq=…]` 的目标 uid 列表（去重、保序）。"""
    out: List[str] = []
    try:
        for m in _CQ_RE.finditer(str(raw_message or "")):
            if m.group("type").lower() != "at":
                continue
            qq = _parse_params(m.group("params")).get("qq", "").strip()
            if qq and qq not in out:
                out.append(qq)
    except Exception:
        return []
    return out


def extract_reply_id(raw_message: Any) -> str:
    """抽取 `[CQ:reply,id=…]` 的被引用 message_id；没有则空串。

    注意：NapCat 的 reply 段**只给 id**，不给原文也不给作者 uid
    （napcat.mjs 源码取证）→ 必须靠 message_index 解引用。
    """
    try:
        for m in _CQ_RE.finditer(str(raw_message or "")):
            if m.group("type").lower() != "reply":
                continue
            rid = _parse_params(m.group("params")).get("id", "").strip()
            if rid:
                return rid
    except Exception:
        pass
    return ""


def strip_cq(raw_message: Any) -> str:
    """剥掉全部 CQ 码，只留文本。

    两遍：① 剥**闭合完好**的（严格正则，不会误伤）；
    ② 再补一刀剥**未闭合**的（上游异常时会出现，不补的话 CQ 原文会整段漏给模型）。
    """
    try:
        s = _CQ_RE.sub("", str(raw_message or ""))
        s = _CQ_LOOSE_RE.sub("", s)
        return s.strip()
    except Exception:
        return ""


# ---------- 事件 → 结构化记录 ----------

def _sender_of(data: Dict[str, Any]) -> Dict[str, str]:
    sender = data.get("sender")
    if not isinstance(sender, dict):
        sender = {}
    return {
        "uid": str(sender.get("user_id") or data.get("user_id") or ""),
        "card": str(sender.get("card") or "").strip(),
        "nickname": str(sender.get("nickname") or "").strip(),
        "role": str(sender.get("role") or "").strip(),
    }


def display_from(record: Dict[str, Any]) -> str:
    """用**消息自带快照**算展示名：card → nickname → uid。

    刻意**不查名册**——DESIGN.md §1.2：历史消息要用「当时的名字」，
    用名册的 current 覆盖会让历史上下文失真。
    """
    card = str(record.get("card") or "").strip()
    if card:
        return card
    nick = str(record.get("nickname") or "").strip()
    if nick:
        return nick
    return str(record.get("uid") or "").strip()


def annotate(data: Dict[str, Any]) -> Optional[dict]:
    """NapCat 群消息事件 → 结构化记录。不是群消息 / 缺关键字段 → None。"""
    try:
        if not isinstance(data, dict):
            return None
        if data.get("post_type") != "message" or data.get("message_type") != "group":
            return None
        gid = str(data.get("group_id") or "")
        mid = str(data.get("message_id") or "")
        if not gid or not mid:
            return None
        raw = str(data.get("raw_message") or "")
        sender = _sender_of(data)
        if not sender["uid"]:
            return None
        record = {
            "message_id": mid,
            "uid": sender["uid"],
            "card": sender["card"],
            "nickname": sender["nickname"],
            "role": sender["role"],
            "text": strip_cq(raw),
            "at": extract_at(raw),
            "reply_id": extract_reply_id(raw),
            "ts": float(data.get("time") or 0) or None,
        }
        return record
    except Exception:
        return None


# ---------- 渲染 ----------

def _truncate(text: Any, limit: int) -> str:
    s = str(text or "").strip().replace("\n", " ")
    if limit > 0 and len(s) > limit:
        return s[:limit] + "…"
    return s


def build_bracket(record: Dict[str, Any], quote: Optional[dict], *,
                  quote_max_chars: int = 40, name_of=None) -> str:
    """拼出方括号里的部分（不含 `[]` 与正文）。纯字符串操作。"""
    speaker = display_from(record)
    at_uids = list(record.get("at") or [])
    reply_id = str(record.get("reply_id") or "")

    quote_uid = str((quote or {}).get("uid") or "") if quote else ""
    # 若引用作者已经在 @ 列表里，就不再重复 @ 一遍
    if quote_uid and quote_uid in at_uids:
        at_uids = [u for u in at_uids if u != quote_uid]

    parts = speaker
    if at_uids:
        names: List[str] = []
        for u in at_uids:
            nm = ""
            if callable(name_of):
                try:
                    nm = str(name_of(u) or "").strip()
                except Exception:
                    nm = ""
            if not nm:
                nm = u
            if nm and nm not in names:
                names.append(nm)
        if names:
            parts += "@" + ",".join(names)

    if reply_id:
        if quote:
            who = display_from(quote)
            body = _truncate(quote.get("text"), quote_max_chars)
            if body:
                parts += " 回复" + who + "「" + body + "」"
            else:
                parts += " 回复" + who
        else:
            # 缓存窗口外：降级，**不编造原文**
            parts += " 回复某条消息"
    return parts


def render(record: Dict[str, Any], quote: Optional[dict] = None, *,
           quote_max_chars: int = 40, name_of=None) -> str:
    """结构化记录 → 一行上下文。无正文且无 @ → 返回空串（调用方跳过）。"""
    try:
        text = str(record.get("text") or "").strip()
        bracket = build_bracket(record, quote, quote_max_chars=quote_max_chars, name_of=name_of)
        if not text:
            if record.get("at"):
                return "[" + bracket + "]（只 @ 了人，没有文字）"
            return ""
        return "[" + bracket + "] " + text
    except Exception:
        return ""


def resolve_quote(gid: Any, record: Dict[str, Any]) -> Optional[dict]:
    """按 reply_id 从消息索引里取被引用的那条；取不到返回 None（不编造）。"""
    try:
        rid = str((record or {}).get("reply_id") or "")
        if not rid or not gid:
            return None
        return message_index.get(gid, rid)
    except Exception:
        return None


# ---------- 输出侧剥离 ----------

def strip_reference_prefix(text: Any) -> str:
    """剥掉模型可能模仿出来的指代前缀。

    覆盖两种形态：
      `[昵称]: 内容`                     ← 原有能力
      `[昵称@某人] 内容` / `[昵称 回复某人「…」] 内容`  ← 新增格式
    刻意**不**匹配 `[笑] 好吧` 这类正常输出（括号里没有 @ / 回复，也没有冒号）。
    """
    try:
        return _REF_PREFIX_RE.sub("", str(text or ""), count=1).strip()
    except Exception:
        return str(text or "")


def stats() -> dict:
    out: Dict[str, Any] = {}
    try:
        out["message_index"] = message_index.stats()
    except Exception:
        pass
    try:
        out["roster"] = roster_mod.stats()
    except Exception:
        pass
    return out
