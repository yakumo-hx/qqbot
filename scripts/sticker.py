# -*- coding: utf-8 -*-
"""表情判断库 —— 决定「这一轮发不发表情包、发哪张」。

契约见 `data/stickers/SCHEMA.md`。本模块是**被 decision_engine 调用的库**，
不是并列的第二条链；它绝不自己发消息、不读环境变量、不发网络请求。

设计纪律：
- **零 I/O**：配置、索引、编码器、时钟、随机数、文件存在性全部依赖注入，
  因此可以完全离线单测（假索引 + 假编码器 + 假时钟）。
- **永不抛异常**：SCHEMA §4 列出的五种情况一律静默返回 `none`——
  表情是增强项，任何故障都不允许影响文字链。
- **可解释**：每次判断都给出人话 `reason`，日志里能读出"为什么发/为什么不发"。
- **先占额**：命中即记账（冷却与限流），沿用识图那条"宁可少发不可无限发"的纪律。
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---- 取值常量（SCHEMA 冻结，不得改名）----
LEVEL_NONE = "none"
LEVEL_WEAK = "weak"
LEVEL_STRONG = "strong"

SCHEMA_VERSION = 1

# 字面命中的加分参数：命中一个词给 BASE，词越长加得越多，总上限 CAP
LEX_BASE = 0.15
LEX_PER_CHAR = 0.05
LEX_CHAR_CAP = 6
LEX_CAP = 0.60

# 强度对 strong 阈值的小幅抬升（强度越高越要求确信，避免误发强烈情绪）
INTENSITY_STRONG_STEP = 0.03


@dataclass(frozen=True)
class StickerPick:
    """一次表情判断的结果（SCHEMA §3 冻结）。"""
    level: str = LEVEL_NONE
    path: str = ""
    score: float = 0.0
    reason: str = ""


def _none(reason: str) -> StickerPick:
    return StickerPick(level=LEVEL_NONE, path="", score=0.0, reason=reason)


def _cosine(a: Any, b: Any) -> float:
    """纯 Python 余弦相似度（不引 numpy，便于离线单测且完全确定性）。"""
    if a is None or b is None:
        return 0.0
    try:
        dot = 0.0
        na = 0.0
        nb = 0.0
        for x, y in zip(a, b):
            fx = float(x)
            fy = float(y)
            dot += fx * fy
            na += fx * fx
            nb += fy * fy
    except (TypeError, ValueError):
        return 0.0
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


def _as_float(value: Any, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:  # NaN
        return default
    return out


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str_list(value: Any) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


class StickerEngine:
    """表情判断引擎。

    注入项（全部可为假实现，单测不需要任何真实资源）：

    - `config_loader()`  -> dict  运行时配置（config/stickers.json 的内容）
    - `index_loader()`   -> dict  索引（data/stickers/index.json 的内容）
    - `encoder(texts)`   -> 向量矩阵 (N, dim)；`embedding_enabled=false` 时不会被调用
    - `clock()`          -> float 单调或墙钟秒；默认 time.time
    - `rng()`            -> float [0,1)；默认 random.random
    - `file_exists(path)`-> bool  单条目文件存在性；默认 os.path.isfile
    - `resolve(file)`    -> str   把索引里的相对路径解析为绝对路径
    - `logger(msg)`      -> None  可选，仅用于诊断
    """

    def __init__(
        self,
        config_loader: Callable[[], dict],
        index_loader: Callable[[], dict],
        encoder: Optional[Callable[[List[str]], Any]] = None,
        *,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
        file_exists: Callable[[str], bool] = os.path.isfile,
        resolve: Optional[Callable[[str], str]] = None,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._config_loader = config_loader
        self._index_loader = index_loader
        self._encoder = encoder
        self._clock = clock
        self._rng = rng
        self._file_exists = file_exists
        self._resolve = resolve or (lambda p: p)
        self._logger = logger
        # 每群发送记账：(时间戳, 条目 id)
        self._sends: Dict[str, List[Tuple[float, str]]] = {}

    # ---------- 内部工具 ----------

    def _log(self, msg: str) -> None:
        if self._logger:
            try:
                self._logger(msg)
            except Exception:
                pass

    def _abs_path(self, rel: str) -> str:
        try:
            return self._resolve(rel)
        except Exception:
            return rel

    def _recent(self, gid: str, window: float, now: float) -> List[Tuple[float, str]]:
        """返回窗口内的发送记录，并顺手清掉过期的。"""
        buf = self._sends.get(gid)
        if not buf:
            return []
        cutoff = now - window
        keep = [rec for rec in buf if rec[0] >= cutoff]
        if len(keep) != len(buf):
            self._sends[gid] = keep
        return keep

    # ---------- 主入口 ----------

    def pick(self, gid: str, context_text: str, feature_on: bool) -> StickerPick:
        """判断这一轮发不发表情包。任何异常都降级为 `none`。"""
        try:
            return self._pick(gid, context_text, feature_on)
        except Exception as exc:  # pragma: no cover - 兜底，绝不影响文字链
            return _none("判断库内部异常，降级不发表情（%s）" % type(exc).__name__)

    def _pick(self, gid: str, context_text: str, feature_on: bool) -> StickerPick:
        gid = str(gid or "")
        text = str(context_text or "").strip()

        # 闸门 0：群功能位
        if not feature_on:
            return _none("本群未开启 sticker 功能")

        if not text:
            return _none("上下文为空")

        # 闸门 1：运行时配置（缺失/非法/enabled=false 一律停用）
        try:
            cfg = self._config_loader() or {}
        except Exception:
            return _none("运行时配置读取失败")
        if not isinstance(cfg, dict) or not cfg:
            return _none("运行时配置缺失或非法")
        if not cfg.get("enabled", True):
            return _none("表情功能总开关已关闭")

        # 闸门 2：索引（缺失/版本不认识/空 一律停用）
        try:
            index = self._index_loader() or {}
        except Exception:
            return _none("索引读取失败")
        if not isinstance(index, dict) or not index:
            return _none("索引缺失或非法")
        if _as_int(index.get("version"), -1) != SCHEMA_VERSION:
            return _none("索引版本不认识（期望 %d，实得 %r）"
                         % (SCHEMA_VERSION, index.get("version")))
        items = index.get("items")
        if not isinstance(items, list) or not items:
            return _none("索引为空（图库还没有打标产物）")

        now = _as_float(self._clock(), 0.0)
        cooldown = max(0.0, _as_float(cfg.get("cooldown_seconds"), 180.0))
        max_per_hour = _as_int(cfg.get("max_per_hour"), 3)
        same_window = max(0.0, _as_float(cfg.get("same_image_window_seconds"), 3600.0))
        probability = _as_float(cfg.get("probability"), 0.0)
        strong_base = _as_float(cfg.get("strong_score"), 0.62)
        weak_score = _as_float(cfg.get("weak_score"), 0.45)
        embedding_enabled = bool(cfg.get("embedding_enabled", True))

        # 闸门 3：冷却
        recent_all = self._recent(gid, max(cooldown, 3600.0), now)
        if recent_all:
            last = max(rec[0] for rec in recent_all)
            elapsed = now - last
            if cooldown > 0 and elapsed < cooldown:
                return _none("冷却中（距上次 %.0fs，需 %.0fs）" % (elapsed, cooldown))

        # 闸门 4：每小时限流
        if max_per_hour <= 0:
            return _none("每小时限流为 0（等于停用）")
        hour_records = [rec for rec in recent_all if rec[0] >= now - 3600.0]
        if len(hour_records) >= max_per_hour:
            return _none("本群 1 小时内已发 %d 次（上限 %d）" % (len(hour_records), max_per_hour))

        # 闸门 5：同图窗口内不重发
        blocked_ids = {rec[1] for rec in recent_all if rec[0] >= now - same_window}

        # 候选筛选：排除隔离区、同图、文件不存在
        context_vec = None
        if embedding_enabled and self._encoder is not None:
            try:
                matrix = self._encoder([text])
                if matrix is not None and len(matrix) > 0:
                    context_vec = list(matrix[0])
            except Exception:
                context_vec = None
        dim = len(context_vec) if context_vec is not None else 0

        text_lower = text.lower()
        best: Optional[Tuple[float, dict, str]] = None  # (score, item, why)
        skipped_quarantine = 0
        skipped_missing = 0
        skipped_vec = 0

        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("quarantine"):
                skipped_quarantine += 1
                continue
            item_id = str(item.get("id") or "")
            rel = str(item.get("file") or "")
            if not rel:
                continue
            if item_id and item_id in blocked_ids:
                continue
            abs_path = self._abs_path(rel)
            try:
                exists = bool(self._file_exists(abs_path))
            except Exception:
                exists = False
            if not exists:
                skipped_missing += 1
                continue

            # 语义分
            sim = 0.0
            if dim:
                vec = item.get("vec")
                if not isinstance(vec, (list, tuple)) or len(vec) != dim:
                    # SCHEMA §4-4：向量缺失或维度不符 → **跳过该条目**。
                    # 阈值是按"语义分为主"标定的，只让字面分参与比较会系统性低估，
                    # 与其给出一个假的低分，不如直接不用这条。
                    skipped_vec += 1
                    continue
                sim = max(0.0, _cosine(context_vec, vec))

            # 字面分：keywords + text_in_image 命中（大小写不敏感）
            lex = 0.0
            hit_words: List[str] = []
            probes = _as_str_list(item.get("keywords"))
            tie = str(item.get("text_in_image") or "").strip()
            if tie:
                probes.append(tie)
            for word in probes:
                wl = word.lower()
                if wl and wl in text_lower:
                    lex += LEX_BASE + LEX_PER_CHAR * min(len(word), LEX_CHAR_CAP)
                    hit_words.append(word)
            lex = min(lex, LEX_CAP)

            score = min(1.0, sim + lex)
            if best is None or score > best[0]:
                why = "语义=%.3f 字面=%.3f" % (sim, lex)
                if hit_words:
                    why += " 命中=%s" % "/".join(hit_words[:3])
                best = (score, item, why)

        if best is None:
            return _none("无可用候选（隔离 %d / 缺文件 %d / 向量不符 %d）"
                         % (skipped_quarantine, skipped_missing, skipped_vec))

        score, item, why = best
        intensity = _as_int(item.get("intensity"), 0)
        strong_threshold = strong_base + INTENSITY_STRONG_STEP * max(0, intensity)

        if score >= strong_threshold:
            level = LEVEL_STRONG
        elif score >= weak_score:
            level = LEVEL_WEAK
        else:
            # 低于阈值也要把真实分数回传：日志里没分数就没法调阈值
            return StickerPick(level=LEVEL_NONE, path="", score=score,
                               reason="最高分 %.3f 低于 weak 阈值 %.2f（%s）" % (score, weak_score, why))

        # 闸门 6：随机降温（probability<=0 视为关闭该闸门）
        if probability > 0.0:
            draw = _as_float(self._rng(), 1.0)
            if draw >= min(1.0, probability):
                return _none("随机降温未通过（%.2f >= %.2f）" % (draw, probability))

        # 命中即记账（先占额）
        abs_path = self._abs_path(str(item.get("file") or ""))
        item_id = str(item.get("id") or abs_path)
        self._sends.setdefault(gid, []).append((now, item_id))

        reason = "%s 分=%.3f（strong阈值 %.2f）%s" % (level, score, strong_threshold, why)
        self._log("[表情] %s" % reason)
        return StickerPick(level=level, path=abs_path, score=score, reason=reason)


# ---------- 默认 I/O 适配器（与纯逻辑类分离，便于单测绕开） ----------

def file_config_loader(path: str) -> Callable[[], dict]:
    """返回一个读 config/stickers.json 的加载器；读不到给空 dict。"""
    def _load() -> dict:
        import json
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return _load


def file_index_loader(path: str) -> Callable[[], dict]:
    """返回一个读索引的加载器；读不到给空 dict。"""
    def _load() -> dict:
        import json
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return _load


_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "stickers.json"
)


def build_default_engine(logger: Optional[Callable[[str], None]] = None) -> Optional["StickerEngine"]:
    """按项目约定路径装配一个真实引擎（配置与索引读文件，编码器用零样本分类器）。

    **任何环节不可用都返回 None**——调用方据此退回「不发表情」，
    此时机器人行为与接线前完全一致。这是本功能的总保险丝。
    """
    try:
        cfg_loader = file_config_loader(_CONFIG_PATH)
        cfg = cfg_loader()
        if not isinstance(cfg, dict) or not cfg:
            return None

        root = str(cfg.get("library_root") or "data/stickers/library")
        if not os.path.isabs(root):
            root = os.path.join(os.path.dirname(os.path.dirname(_CONFIG_PATH)), root)
        ipath = str(cfg.get("index_path") or "data/stickers/index.json")
        if not os.path.isabs(ipath):
            ipath = os.path.join(os.path.dirname(os.path.dirname(_CONFIG_PATH)), ipath)

        def _encode(texts):
            # 复用决策引擎同一个分类器单例：模型此时早已加载，不产生额外开销
            from scripts.zero_shot_classifier import get_classifier
            return get_classifier().encode_batch(texts)

        return StickerEngine(
            config_loader=cfg_loader,
            index_loader=file_index_loader(ipath),
            encoder=_encode,
            resolve=lambda rel: os.path.join(root, str(rel).replace("/", os.sep)),
            logger=logger,
        )
    except Exception:
        return None
