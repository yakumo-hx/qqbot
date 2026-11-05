# -*- coding: utf-8 -*-
"""决策台账（结构化 JSONL trace）+ @bot 回复防滥用限流。

设计要点
========
1. **单记录 + stages 数组**：一次决策只在结束时写 **一行** JSON（追加写、单次
   ``write`` 调用、模块级锁），因此多个群并发决策时不会出现日志行交错。字段见
   :class:`DecisionTrace`（schema ``decision_trace.v1``）。
2. **三种语义严格分开**（见 ``README/release`` 文档）：
   - ``policy_intent``  规则希望发言（生成前的意图）；
   - ``generation_ok``  真的得到可用内容；
   - ``send_status=ok`` OneBot **明确**回执成功。
   ``websocket.send`` 写成功只是"写进了 WebSocket"，绝不算 ok（对应 ``unknown``）。
3. **隐私**：记录 ``message_length``，不记录消息正文；``send_attempts`` 只含
   kind/status/retcode/message_id/error_kind，**不含** base64、完整 payload 或
   本地图片路径。
4. **默认关闭**：只有显式传入 ``path``（生产由 reverse_bot.py 显式开启，
   ``logs/decision_trace.jsonl``）才会写文件；测试必须重定向到临时目录。环境变量
   ``DECISION_TRACE_DISABLED=1`` 可强制关闭，``DECISION_TRACE_PATH`` 可改路径。
5. **@bot 限流**（:class:`AtReplyLimiter`）：每 ``(group_id, user_id)`` 独立，
   默认 5 秒最小间隔 / 60 秒窗口内最多 6 次；**独立于** proactive 每小时上限，
   @bot 仍可绕过后者。限额是"准入"计数（通过限流即占额，即使随后生成失败），
   防止用必然失败的生成绕过成本闸门。

本模块是纯内存/文件逻辑：不联网、不读模型、不发任何 QQ 消息。
"""

from __future__ import annotations

import atexit
import json
import os
import sys
import threading
import time
import uuid
from collections import OrderedDict, deque
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

# ---------- schema / 枚举常量 ----------

SCHEMA_VERSION = "decision_trace.v1"

SEND_OK = "ok"
SEND_FAILED = "failed"
SEND_UNKNOWN = "unknown"
SEND_NOT_ATTEMPTED = "not_attempted"
SEND_STATUSES = (SEND_OK, SEND_FAILED, SEND_UNKNOWN, SEND_NOT_ATTEMPTED)

OUTCOME_SILENT = "silent"
OUTCOME_CLASSIFIER_ERROR = "classifier_error"
OUTCOME_GENERATION_EMPTY = "generation_empty"
OUTCOME_GENERATION_ERROR = "generation_error"
OUTCOME_CONTENT_FILTERED = "content_filtered"
OUTCOME_DEDUPLICATED = "deduplicated"
OUTCOME_REPLY_SENT = "reply_sent"
OUTCOME_REPLY_FAILED = "reply_failed"
OUTCOME_REPLY_UNKNOWN = "reply_unknown"
OUTCOME_INCOMPLETE = "incomplete"

FINAL_OUTCOMES = (
    OUTCOME_SILENT,
    OUTCOME_CLASSIFIER_ERROR,
    OUTCOME_GENERATION_EMPTY,
    OUTCOME_GENERATION_ERROR,
    OUTCOME_CONTENT_FILTERED,
    OUTCOME_DEDUPLICATED,
    OUTCOME_REPLY_SENT,
    OUTCOME_REPLY_FAILED,
    OUTCOME_REPLY_UNKNOWN,
    OUTCOME_INCOMPLETE,
)

# 生产建议路径（相对项目根）；是否启用由调用方显式决定
DEFAULT_TRACE_RELPATH = os.path.join("logs", "decision_trace.jsonl")
TRACE_PATH_ENV = "DECISION_TRACE_PATH"
TRACE_DISABLED_ENV = "DECISION_TRACE_DISABLED"

# @bot 回复防滥用默认策略（代码内生效；生产 decision_rules.json 本任务不修改）
AT_REPLY_MIN_INTERVAL_SECONDS = 5.0
AT_REPLY_MAX_PER_USER_PER_WINDOW = 6
AT_REPLY_WINDOW_SECONDS = 60.0

DEFAULT_AT_REPLY_POLICY = {
    "at_reply_min_interval_seconds": AT_REPLY_MIN_INTERVAL_SECONDS,
    "at_reply_max_per_user_per_window": AT_REPLY_MAX_PER_USER_PER_WINDOW,
    "at_reply_window_seconds": AT_REPLY_WINDOW_SECONDS,
}

# DecisionTrace 允许写入的字段（防止拼写错误静默产出脏字段）
_FIELDS = frozenset({
    "schema_version", "trace_id", "timestamp", "ts_epoch",
    "group_id", "user_id", "message_id", "is_at_bot", "message_length",
    "rules_version", "config_sha256", "active_model", "classifier_model",
    "at_reply_policy", "stages",
    "classifier_label", "classifier_score", "dynamic_threshold",
    "classifier_error", "classifier_error_total",
    "policy_intent", "reached_generation", "generation_ok", "generation_empty",
    "generation_error", "content_filtered", "deduplicated", "dedupe_kind",
    "content_kind", "sticker_only",
    "send_status", "send_attempts",
    "decision_latency_ms", "latency_ms", "instrumentation_gap", "final_outcome",
})


# ---------- 工具函数 ----------

def iso_timestamp(ts: float) -> str:
    """把 epoch 秒格式化为带本地时区偏移的 ISO 时间（台账可读字段）。"""
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec="milliseconds")
    except (OverflowError, OSError, ValueError):
        return ""


def sha256_bytes(raw: bytes) -> str:
    import hashlib
    return hashlib.sha256(raw).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def config_identity(config_path: Optional[str], config: Optional[dict]) -> Tuple[str, str]:
    """返回 ``(rules_version, config_sha256)``。

    优先对配置文件原始字节取哈希（与发布清单/实验 snapshot 使用的哈希一致）；
    文件不可读时退化为"内存配置的规范化 JSON"哈希，绝不抛异常。
    ``rules_version`` 是给台账人读的短标识（``文件名@sha256:前 12 位``）。
    """
    raw = None
    if config_path:
        try:
            with open(config_path, "rb") as f:
                raw = f.read()
        except OSError:
            raw = None
    if raw is None:
        try:
            raw = json.dumps(config or {}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        except (TypeError, ValueError):
            raw = b""
    digest = sha256_bytes(raw)
    name = os.path.basename(config_path) if config_path else "decision_rules(in-memory)"
    return "%s@sha256:%s" % (name, digest[:12]), digest


def aggregate_send_status(statuses) -> str:
    """把多条发送结果合成一个 ``send_status``。

    保守优先：unknown > failed > ok；一条都没有 → not_attempted。
    "unknown 优先"意味着只要有一路结果未知，整体就不能声称成功（unknown 不得自动重发）。
    """
    vals = [str(s) for s in (statuses or ()) if s]
    if not vals:
        return SEND_NOT_ATTEMPTED
    if SEND_UNKNOWN in vals:
        return SEND_UNKNOWN
    if SEND_FAILED in vals:
        return SEND_FAILED
    if all(v == SEND_OK for v in vals):
        return SEND_OK
    return SEND_UNKNOWN


_SEND_TO_OUTCOME = {
    SEND_OK: OUTCOME_REPLY_SENT,
    SEND_FAILED: OUTCOME_REPLY_FAILED,
    SEND_UNKNOWN: OUTCOME_REPLY_UNKNOWN,
    SEND_NOT_ATTEMPTED: OUTCOME_INCOMPLETE,
}


# ---------- @bot 回复限流 ----------

def _coerce_float(value: Any, default: float, *, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number < minimum or number != number:  # NaN
        return float(default)
    return number


def _coerce_int(value: Any, default: int, *, minimum: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return int(default)
    if number < minimum:
        return int(default)
    return number


def _merge_at_reply_policy(policy: Dict[str, Any], override: Any) -> Dict[str, Any]:
    """把一层（扁平或嵌套）覆盖合并进策略；非法值保留上一层，绝不抛异常。"""
    if not isinstance(override, dict):
        return policy
    merged = dict(policy)
    if "at_reply_min_interval_seconds" in override:
        merged["at_reply_min_interval_seconds"] = _coerce_float(
            override["at_reply_min_interval_seconds"],
            merged["at_reply_min_interval_seconds"],
        )
    if "at_reply_window_seconds" in override:
        merged["at_reply_window_seconds"] = _coerce_float(
            override["at_reply_window_seconds"],
            merged["at_reply_window_seconds"],
            minimum=1e-3,   # 窗口必须为正，否则退化为"永不计数"
        )
    if "at_reply_max_per_user_per_window" in override:
        merged["at_reply_max_per_user_per_window"] = _coerce_int(
            override["at_reply_max_per_user_per_window"],
            merged["at_reply_max_per_user_per_window"],
        )
    return merged


def resolve_at_reply_policy(config: Optional[dict], group_id: Any = "") -> Dict[str, float]:
    """解析 @bot 回复限流策略（代码默认 → 扁平键 → ``at_reply`` 块 → ``at_reply.groups[gid]``）。

    这样未来的候选灰度配置可以在 ``decision_rules.json`` 里只给单个群调限额，
    而不必改代码；本任务不写任何生产配置，因此当前生效的是代码默认值。

    **0/负值语义**：``at_reply_min_interval_seconds=0`` 或
    ``at_reply_max_per_user_per_window=0`` 表示**显式关闭该维度**（只建议测试/应急，
    生产不应关闭）；``at_reply_window_seconds`` 必须为正，非法/非正值回落 60 秒。
    """
    policy = dict(DEFAULT_AT_REPLY_POLICY)
    cfg = config if isinstance(config, dict) else {}
    # 1) 扁平键（与 decision_rules.json 现有风格一致）
    policy = _merge_at_reply_policy(policy, cfg)
    # 2) 嵌套 at_reply 块
    block = cfg.get("at_reply")
    if isinstance(block, dict):
        policy = _merge_at_reply_policy(policy, block)
        # 3) 每群覆盖（灰度用）
        groups = block.get("groups")
        if isinstance(groups, dict):
            gid = str(group_id or "")
            per_group = groups.get(gid) if gid else None
            if per_group is None:
                for key, value in groups.items():
                    if str(key) == gid:
                        per_group = value
                        break
            policy = _merge_at_reply_policy(policy, per_group)
    return policy


class AtReplyLimiter:
    """每 ``(group_id, user_id)`` 的 @bot 回复准入限流（纯内存、可注入时钟）。

    - ``min_interval``：两次**准入**之间的最小间隔（秒），0 = 不限制；
    - ``window`` / ``max_per_window``：滚动窗口内最多准入次数，0 = 不限制；
    - 只有 ``acquire()`` 通过时才计数；被拦截的尝试不占额（但也不会因为反复被拦
      而"欠账"）。
    - 状态随 ``DecisionEngine`` 实例存活：重启/新实例 = 清空（与现有内存语义一致）。

    **0/负值语义**（与 :func:`resolve_at_reply_policy` 一致）：`min_interval=0` 或
    `max_per_window=0` 表示**显式关闭该维度**（只建议测试/应急时使用，生产不应关闭）；
    `window_seconds` 必须为正，非法/非正值会回落到默认 60 秒（避免"窗口为 0"导致
    计数永不过期）。
    """

    def __init__(self, max_tracked_keys: int = 4096):
        self._hits: "OrderedDict[Tuple[str, str], deque]" = OrderedDict()
        self._max_keys = max(1, int(max_tracked_keys))

    def acquire(self, group_id: Any, user_id: Any, now: float,
                policy: Optional[dict] = None) -> Tuple[bool, str, dict]:
        """尝试占一个准入名额。

        返回 ``(allowed, reason, snapshot)``；``reason`` ∈
        ``""`` / ``"min_interval"`` / ``"window_cap"``。``snapshot`` 用于台账，
        不含任何消息内容。
        """
        pol = dict(DEFAULT_AT_REPLY_POLICY)
        pol.update(policy or {})
        window = _coerce_float(pol.get("at_reply_window_seconds"),
                               AT_REPLY_WINDOW_SECONDS, minimum=1e-3)
        cap = _coerce_int(pol.get("at_reply_max_per_user_per_window"),
                          AT_REPLY_MAX_PER_USER_PER_WINDOW)
        interval = _coerce_float(pol.get("at_reply_min_interval_seconds"),
                                 AT_REPLY_MIN_INTERVAL_SECONDS)

        gid, uid = str(group_id or ""), str(user_id or "")
        key = (gid, uid)
        buf = self._hits.get(key)
        if buf is None:
            buf = deque()
            self._hits[key] = buf
        else:
            self._hits.move_to_end(key)

        try:
            now = float(now)
        except (TypeError, ValueError):
            now = time.time()
        cutoff = now - window
        while buf and buf[0] < cutoff:
            buf.popleft()

        snapshot = {
            "window_count": len(buf),
            "window_seconds": window,
            "max_per_user_per_window": cap,
            "min_interval_seconds": interval,
            "seconds_since_last": (round(now - buf[-1], 3) if buf else None),
        }

        if interval > 0 and buf and (now - buf[-1]) < interval:
            snapshot["seconds_since_last"] = round(now - buf[-1], 3)
            return False, "min_interval", snapshot
        if cap > 0 and len(buf) >= cap:
            return False, "window_cap", snapshot

        buf.append(now)
        snapshot["window_count"] = len(buf)
        if len(self._hits) > self._max_keys:
            self._hits.popitem(last=False)
        return True, "", snapshot

    def count(self, group_id: Any, user_id: Any, now: Optional[float] = None,
              window: float = AT_REPLY_WINDOW_SECONDS) -> int:
        """只读：返回窗口内已准入次数（不改变状态；供测试/观测）。"""
        buf = self._hits.get((str(group_id or ""), str(user_id or "")))
        if not buf:
            return 0
        if now is None:
            return len(buf)
        cutoff = float(now) - float(window)
        return sum(1 for ts in buf if ts >= cutoff)

    def reset(self, group_id: Any = None, user_id: Any = None) -> None:
        """清空限流状态；不传参 = 清空全部（仅供测试/人工复位）。"""
        if group_id is None and user_id is None:
            self._hits.clear()
            return
        key = (str(group_id or ""), str(user_id or ""))
        self._hits.pop(key, None)

    def tracked_keys(self) -> list:
        return list(self._hits)


# ---------- 决策台账 ----------

class DecisionTrace:
    """一次决策的台账记录（内存中累积，结束时一次性写一行 JSONL）。

    字段读写走 ``record`` 字典；对已知字段可直接属性赋值（拼错字段名会抛
    ``AttributeError``，避免脏字段悄悄写进台账）。
    """

    def __init__(self, recorder: "TraceRecorder", *, trace_id: str,
                 message_id: Optional[str] = None, group_id: Any = "",
                 user_id: Any = "", is_at_bot: bool = False,
                 message_length: int = 0, now: Optional[float] = None,
                 rules_version: str = "", config_sha256: str = "",
                 active_model: str = "", classifier_model: str = "",
                 at_reply_policy: Optional[dict] = None):
        object.__setattr__(self, "_recorder", recorder)
        object.__setattr__(self, "_finished", False)
        start = float(now if now is not None else time.time())
        self.record = {
            "schema_version": SCHEMA_VERSION,
            "trace_id": str(trace_id),
            "timestamp": iso_timestamp(start),
            "ts_epoch": start,
            "group_id": str(group_id or ""),
            "user_id": str(user_id or ""),
            "message_id": (str(message_id) if message_id not in (None, "") else None),
            "is_at_bot": bool(is_at_bot),
            "message_length": int(message_length or 0),
            "rules_version": str(rules_version or ""),
            "config_sha256": str(config_sha256 or ""),
            "active_model": str(active_model or ""),
            "classifier_model": str(classifier_model or ""),
            "at_reply_policy": dict(at_reply_policy or DEFAULT_AT_REPLY_POLICY),
            "stages": [],
            "classifier_label": "",
            "classifier_score": None,
            "dynamic_threshold": None,
            "classifier_error": None,
            "classifier_error_total": 0,
            "policy_intent": False,
            "reached_generation": False,
            "generation_ok": False,
            "generation_empty": False,
            "generation_error": None,
            "content_filtered": False,
            "deduplicated": False,
            "dedupe_kind": "",
            "content_kind": "",
            "sticker_only": False,
            "send_status": SEND_NOT_ATTEMPTED,
            "send_attempts": [],
            "decision_latency_ms": None,
            "latency_ms": None,
            "instrumentation_gap": False,
            "final_outcome": "",
        }
        object.__setattr__(self, "_started_at", start)

    # ----- 属性访问：已知字段映射到 record -----
    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        record = object.__getattribute__(self, "record")
        try:
            return record[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name: str, value):
        if name.startswith("_") or name == "record":
            object.__setattr__(self, name, value)
            return
        if name not in _FIELDS:
            raise AttributeError("DecisionTrace 未知字段: %s" % name)
        self.record[name] = value

    # ----- 只读属性 -----
    @property
    def trace_id(self) -> str:
        return self.record["trace_id"]

    @property
    def finished(self) -> bool:
        return bool(object.__getattribute__(self, "_finished"))

    @property
    def started_at(self) -> float:
        return float(object.__getattribute__(self, "_started_at"))

    # ----- 记录 API -----
    def stage(self, name: str, passed: bool, reason: str = "", **extra) -> dict:
        """追加一个闸门/环节。``stage`` 名与 ``passed`` 是机器可读的主键。"""
        entry = {"stage": str(name), "passed": bool(passed), "reason": str(reason or "")}
        for key, value in extra.items():
            if key in ("stage", "passed", "reason"):
                continue
            entry[str(key)] = value
        self.record["stages"].append(entry)
        return entry

    def blocking_stage(self) -> str:
        for entry in reversed(self.record["stages"]):
            if entry.get("passed") is False:
                return str(entry.get("stage") or "")
        return ""

    def mark_decision_done(self, now: Optional[float] = None) -> int:
        """决策阶段结束（生成/过滤/去重都已完成）时记录耗时；发送回执后才是总耗时。"""
        end = float(now if now is not None else time.time())
        ms = int(round(max(0.0, end - self.started_at) * 1000))
        self.record["decision_latency_ms"] = ms
        return ms

    def finish(self, *, final_outcome: Optional[str] = None,
               send_status: Optional[str] = None,
               send_attempts: Optional[list] = None,
               instrumentation_gap: bool = False,
               now: Optional[float] = None) -> bool:
        """完成并写出记录；幂等（重复调用只有第一次生效）。"""
        if self.finished:
            return False
        end = float(now if now is not None else time.time())
        if final_outcome:
            self.record["final_outcome"] = str(final_outcome)
        if not self.record["final_outcome"]:
            self.record["final_outcome"] = OUTCOME_INCOMPLETE
        if send_status is not None:
            status = str(send_status)
            if status not in SEND_STATUSES:
                status = SEND_UNKNOWN
            self.record["send_status"] = status
            # not_attempted 不写 send 闸门（否则"首个失败闸门"分析会被它污染）
            if status != SEND_NOT_ATTEMPTED:
                self.stage("send", status == SEND_OK, status)
        if send_attempts is not None:
            self.record["send_attempts"] = [_clean_attempt(a) for a in send_attempts]
        self.record["instrumentation_gap"] = bool(instrumentation_gap)
        self.record["latency_ms"] = int(round(max(0.0, end - self.started_at) * 1000))
        if self.record["decision_latency_ms"] is None:
            self.record["decision_latency_ms"] = self.record["latency_ms"]
        object.__setattr__(self, "_finished", True)
        self._recorder.write(self.record)
        return True

    def finish_after_send(self, attempts, now: Optional[float] = None) -> bool:
        """按发送结果收尾（决策回复专用）。

        ``attempts`` 是 ``[(kind, outcome), ...]``；``outcome`` 只需有
        ``status/retcode/message_id`` 属性（duck typing，避免依赖回执模块）。
        """
        normalized = []
        statuses = []
        for item in (attempts or ()):
            if isinstance(item, (tuple, list)) and len(item) == 2:
                kind, outcome = item
            else:
                outcome, kind = item, ""
            status = str(getattr(outcome, "status", SEND_UNKNOWN))
            if status not in SEND_STATUSES:
                status = SEND_UNKNOWN
            statuses.append(status)
            normalized.append({
                "kind": str(kind or getattr(outcome, "kind", "") or ""),
                "status": status,
                "retcode": getattr(outcome, "retcode", None),
                "message_id": getattr(outcome, "message_id", None),
                "error_kind": str(getattr(outcome, "error_kind", "") or ""),
            })
        status = aggregate_send_status(statuses)
        return self.finish(
            final_outcome=_SEND_TO_OUTCOME.get(status, OUTCOME_INCOMPLETE),
            send_status=status,
            send_attempts=normalized,
            now=now,
        )

    def finish_incomplete(self, reason: str = "调用方未收尾", now: Optional[float] = None) -> bool:
        """兜底收尾：调用方漏了收尾（异常/未来重构）时，标成不完整而不是悄悄丢失。"""
        if self.finished:
            return False
        self.stage("trace_incomplete", False, reason)
        return self.finish(
            final_outcome=OUTCOME_INCOMPLETE,
            send_status=SEND_UNKNOWN,
            instrumentation_gap=True,
            now=now,
        )


def _clean_attempt(attempt: Any) -> dict:
    """白名单化一条发送记录：绝不放 base64/payload/本地路径。"""
    if isinstance(attempt, dict):
        return {
            "kind": str(attempt.get("kind") or ""),
            "status": str(attempt.get("status") or SEND_UNKNOWN),
            "retcode": attempt.get("retcode"),
            "message_id": attempt.get("message_id"),
            "error_kind": str(attempt.get("error_kind") or ""),
        }
    return {
        "kind": str(getattr(attempt, "kind", "") or ""),
        "status": str(getattr(attempt, "status", SEND_UNKNOWN)),
        "retcode": getattr(attempt, "retcode", None),
        "message_id": getattr(attempt, "message_id", None),
        "error_kind": str(getattr(attempt, "error_kind", "") or ""),
    }


class TraceRecorder:
    """JSONL 台账写入器（线程安全、单次 write、失败降级为关闭）。

    - 只有 ``enabled=True`` 时才会创建/写入文件；默认由 ``path`` 决定是否启用
      （显式传 path 才启用），并受 ``DECISION_TRACE_DISABLED`` 环境变量强制关闭。
    - 写入失败（权限/磁盘）只降级关闭一次，**绝不影响决策主链**；但失败不是静默的：
      ``write_failures`` / ``last_error_type`` 会记录，并在首次失败时向 stderr 打一行
      警告（台账唯一记录仍会丢——写不进去就没有记录，这一点运维必须靠该警告/计数发现）。
    - 文件句柄在首次写入时打开，进程退出（``atexit``）时关闭；每次写入都 flush。
    """

    def __init__(self, path: Optional[str] = None, enabled: Optional[bool] = None,
                 ignore_env: bool = False):
        env_path = os.environ.get(TRACE_PATH_ENV) or ""
        self.path = str(path or env_path or "")
        disabled = str(os.environ.get(TRACE_DISABLED_ENV) or "").strip().lower()
        env_disabled = (not ignore_env) and disabled in ("1", "true", "yes", "on")
        if enabled is None:
            enabled = bool(self.path) and not env_disabled
        self.enabled = bool(enabled) and not env_disabled and bool(self.path)
        self.written = 0
        self.write_failures = 0
        self.last_error_type = ""
        self._write_warned = False
        self._atexit_registered = False
        self._lock = threading.Lock()
        self._fh = None

    def new_trace(self, *, message_id: Optional[str] = None,
                  group_id: Any = "", user_id: Any = "", is_at_bot: bool = False,
                  message_length: int = 0, now: Optional[float] = None,
                  rules_version: str = "", config_sha256: str = "",
                  active_model: str = "", classifier_model: str = "",
                  at_reply_policy: Optional[dict] = None) -> DecisionTrace:
        # trace_id 优先 message_id；缺失时随机 UUID（不含消息正文）
        trace_id = str(message_id) if message_id not in (None, "") else uuid.uuid4().hex
        return DecisionTrace(
            self,
            trace_id=trace_id,
            message_id=message_id,
            group_id=group_id,
            user_id=user_id,
            is_at_bot=is_at_bot,
            message_length=message_length,
            now=now,
            rules_version=rules_version,
            config_sha256=config_sha256,
            active_model=active_model,
            classifier_model=classifier_model,
            at_reply_policy=at_reply_policy,
        )

    def write(self, record: dict) -> bool:
        """整条记录一次写入（含换行）。调用方应已保证 record 已完成。"""
        if not self.enabled:
            return False
        try:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            return False
        if "\n" in line or "\r" in line:  # json.dumps 会转义，双保险
            line = line.replace("\r", "").replace("\n", "")
        with self._lock:
            try:
                if self._fh is None:
                    directory = os.path.dirname(self.path)
                    if directory:
                        os.makedirs(directory, exist_ok=True)
                    self._fh = open(self.path, "a", encoding="utf-8", newline="\n")
                    self._register_atexit()
                self._fh.write(line + "\n")
                self._fh.flush()
                self.written += 1
                return True
            except OSError as exc:
                # 日志目录不可写（本项目已知 ACL 问题）→ 关闭台账，不影响决策链。
                # 失败不静默：计数 + 首次告警（记录本身已丢，只能靠这里让运维发现）。
                self.write_failures += 1
                self.last_error_type = type(exc).__name__
                self.enabled = False
                try:
                    if self._fh is not None:
                        self._fh.close()
                except OSError:
                    pass
                self._fh = None
                if not self._write_warned:
                    self._write_warned = True
                    print(f"[决策台账] 写入失败（{type(exc).__name__}），"
                          f"已降级为不写台账；决策链不受影响，但台账记录缺失",
                          file=sys.stderr)
                return False

    def _register_atexit(self) -> None:
        """首次打开文件后登记退出关闭（幂等；生产进程不会显式调用 close）。"""
        if self._atexit_registered:
            return
        self._atexit_registered = True
        try:
            atexit.register(self.close)
        except Exception:
            pass

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None


__all__ = [
    "SCHEMA_VERSION", "DEFAULT_TRACE_RELPATH", "TRACE_PATH_ENV", "TRACE_DISABLED_ENV",
    "SEND_OK", "SEND_FAILED", "SEND_UNKNOWN", "SEND_NOT_ATTEMPTED", "SEND_STATUSES",
    "OUTCOME_SILENT", "OUTCOME_CLASSIFIER_ERROR", "OUTCOME_GENERATION_EMPTY",
    "OUTCOME_GENERATION_ERROR", "OUTCOME_CONTENT_FILTERED", "OUTCOME_DEDUPLICATED",
    "OUTCOME_REPLY_SENT", "OUTCOME_REPLY_FAILED", "OUTCOME_REPLY_UNKNOWN",
    "OUTCOME_INCOMPLETE", "FINAL_OUTCOMES",
    "DEFAULT_AT_REPLY_POLICY", "resolve_at_reply_policy", "AtReplyLimiter",
    "DecisionTrace", "TraceRecorder", "aggregate_send_status", "config_identity",
    "iso_timestamp", "sha256_bytes", "sha256_text",
]
