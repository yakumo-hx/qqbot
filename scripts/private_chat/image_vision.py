# -*- coding: utf-8 -*-
"""私聊图片理解（对齐群聊的 vision 链路）。

群聊链路在 reverse_bot._describe_triggered_image：
  parse CQ image → image_cache.remember → rate limit → select_candidates
  → 跳过贴纸/重复 → fetch_image → to_data_url → vision.describe
  → 把 "[图片: 描述]" 塞回文本

私聊复用同样的底层能力，但：
- 缓存 key 用 private:<uid>，不碰群聊缓存；
- 不依赖 scripts.commands._state（导入它会触发所有指令注册），限流器自带；
- 只有当前消息有图、引用图、或用户明确在说「图/照片/截图」时才触发；
- 下载/Pillow 放线程，vision 调用加超时，失败保持 "[图片]" 不假装看到。
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import Any, Callable, Optional

from scripts import image_cache, image_utils, vision

# 用户明确在说图/照片/截图时，才去最近缓存里找图，避免每句话都识图。
_REF_RE = re.compile(
    r"(图片|图|照片|截图|看图|看这个|这张|那张|刚才|看到|发的图|发图)"
)

_usage: dict[str, list[float]] = {}
# 只缓存有效文字描述，不保存图片字节或 data URL；重复引用可直接复用。
_descriptions: dict[str, tuple[float, str, str]] = {}
_rate_lock = threading.Lock()
_DESCRIPTION_CACHE_MAX = 512


def reset_rate() -> None:
    with _rate_lock:
        _usage.clear()
        _descriptions.clear()


def _description_key(key: str, signature: str) -> str:
    return f"{key}:{signature}"


def _cached_description(key: str, signature: str, within: float) -> tuple[str, str]:
    if not signature:
        return "", "image"
    cache_key = _description_key(key, signature)
    now = time.monotonic()
    with _rate_lock:
        item = _descriptions.get(cache_key)
        if not item or now - item[0] > within:
            _descriptions.pop(cache_key, None)
            return "", "image"
        return item[1], item[2]


def _cache_description(key: str, signature: str, desc: str, kind: str) -> None:
    if not signature or not desc:
        return
    with _rate_lock:
        cache_key = _description_key(key, signature)
        _descriptions.pop(cache_key, None)
        _descriptions[cache_key] = (time.monotonic(), desc, kind)
        while len(_descriptions) > _DESCRIPTION_CACHE_MAX:
            oldest = next(iter(_descriptions))
            _descriptions.pop(oldest, None)


def _has_new_question(text: str) -> bool:
    """去掉消息占位后仍有文字，才允许为无有效描述的重复图重新识图。"""
    value = str(text or "").replace("[引用]", "").replace("[图片]", "")
    return bool(value.strip())


def rate_ok(key: str, limit: int, window: float) -> tuple[bool, int]:
    """本地限流，语义对齐 scripts.commands._state.vision_rate_ok。"""
    key = str(key or "")
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 0
    now = time.time()
    with _rate_lock:
        buf = _usage.setdefault(key, [])
        cutoff = now - float(window)
        while buf and buf[0] < cutoff:
            buf.pop(0)
        used = len(buf)
        if limit <= 0 or used >= limit:
            return False, used
        buf.append(now)
        return True, used + 1


def cache_key(cfg: dict, uid: str) -> str:
    prefix = str((cfg.get("image") or {}).get("cache_prefix") or "private")
    return f"{prefix}:{uid}"


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_images(data: dict) -> list[dict]:
    """从 OneBot 私聊 message 数组收集图片；用 raw CQ 补全 url/file。"""
    images: list[dict] = []
    message = data.get("message")
    if isinstance(message, list):
        for seg in message:
            if not isinstance(seg, dict) or seg.get("type") != "image":
                continue
            seg_data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            meta = dict(seg_data)
            meta["sub_type"] = _to_int(meta.get("sub_type"), 0)
            meta["file_size"] = _to_int(meta.get("file_size"), 0)
            images.append(meta)

    raw = str(data.get("raw_message") or "")
    cq_images = image_utils.parse_cq_images(raw) if raw else []
    if not images:
        images = cq_images
    else:
        # 数组里的 image 段有时只有 file，没有 url；按顺序用 CQ 元数据补全。
        for index, meta in enumerate(images):
            if index >= len(cq_images):
                break
            cq = cq_images[index]
            for field in ("url", "file", "summary", "key"):
                if not str(meta.get(field) or "").strip() and cq.get(field):
                    meta[field] = cq[field]
            if not meta.get("sub_type"):
                meta["sub_type"] = cq.get("sub_type", 0)
            if not meta.get("file_size"):
                meta["file_size"] = cq.get("file_size", 0)
    return [m for m in images if isinstance(m, dict)]


def remember(uid: str, data: dict, images: list[dict], cfg: dict, logger=None) -> None:
    if not images:
        return
    sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
    nickname = str(sender.get("card") or sender.get("nickname") or "")
    image_cache.remember(
        cache_key(cfg, uid),
        data.get("message_id"),
        uid,
        nickname,
        images=images,
        raw_message=str(data.get("raw_message") or ""),
    )
    if logger:
        logger.info(f"[图片] 缓存 {len(images)} 张（key={cache_key(cfg, uid)}）")


def kind_of(images: list[dict]) -> str:
    """本轮图片的类型：sticker / image（按第一张判断）。"""
    return "sticker" if images and image_utils.is_sticker(images[0]) else "image"


def should_attempt(text: str, images: list[dict], raw_message: str = "") -> bool:
    if images:
        return True
    raw = str(raw_message or "")
    if "[CQ:reply" in raw or "[引用]" in str(text or ""):
        return True
    return bool(_REF_RE.search(str(text or "")))


def enrich_text(text: str, desc: str, kind: str = "image") -> str:
    """把第一条 [图片] 换成 [图片: 描述] / [表情: 描述]；没有占位就追加。"""
    value = str(text or "").strip()
    desc = str(desc or "").strip()
    if not desc:
        return value
    tag = f"[表情: {desc}]" if str(kind) == "sticker" else f"[图片: {desc}]"
    if "[图片]" in value:
        return value.replace("[图片]", tag, 1)
    if value:
        return value + "\n" + tag
    return tag


def mark_unavailable(text: str, images: list[dict]) -> str:
    """识图失败/跳过时给出明确占位，避免模型误以为「图没加载」。"""
    value = str(text or "").strip()
    sticker = bool(images) and image_utils.is_sticker(images[0])
    tag = "[表情包: 未识别]" if sticker else "[图片: 未识别]"
    if "[图片]" in value:
        return value.replace("[图片]", tag, 1)
    if value:
        return value + "\n" + tag
    return tag


async def describe_turn(
    uid: str,
    text: str,
    images: list[dict],
    raw_message: str,
    cfg: dict,
    logger=None,
    *,
    fetch: Optional[Callable[[dict], Optional[bytes]]] = None,
    encode: Optional[Callable[[bytes], Optional[str]]] = None,
    describe: Optional[Callable[[str, str], Any]] = None,
) -> Optional[dict]:
    """识图；返回 {desc, source, signature} 或 None。绝不抛异常给聊天主链路。"""
    image_cfg = cfg.get("image") or {}
    if not image_cfg.get("enabled", True) or not image_cfg.get("vision", True):
        return None
    if not should_attempt(text, images, raw_message):
        return None

    key = cache_key(cfg, uid)
    recognize_stickers = bool(image_cfg.get("recognize_stickers", True))
    sticker_limit = int(image_cfg.get("sticker_max_per_hour", 8) or 8)
    normal_limit = int(image_cfg.get("max_per_hour", 10) or 10)

    try:
        candidates, source = image_cache.select_candidates(
            raw_message,
            key,
            uid,
            message_images=images,
            within=float(image_cfg.get("recent_window_seconds", 120) or 120),
            limit=int(image_cfg.get("pick_max", 3) or 3),
        )
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.info(f"[图片] 候选选择失败（已忽略）: {type(exc).__name__}: {exc}")
        return None

    if not candidates:
        if logger:
            logger.info("[图片] 无可用候选（当前/引用/最近图都为空）")
        return None

    fetch_timeout = float(image_cfg.get("fetch_timeout_seconds", 10.0) or 10.0)
    max_bytes = int(image_cfg.get("max_bytes", 8 * 1024 * 1024) or 8 * 1024 * 1024)
    max_edge = int(image_cfg.get("max_edge", 1024) or 1024)
    vision_timeout = float(image_cfg.get("vision_timeout_seconds", 60.0) or 60.0)
    sticker_edge = int(image_cfg.get("sticker_max_edge", 512) or 512)
    sticker_timeout = float(image_cfg.get("sticker_vision_timeout_seconds", 30.0) or 30.0)
    vision_model = str(image_cfg.get("vision_model") or "").strip()
    if logger:
        logger.info(
            f"[图片] 开始识图：来源={source} 模型={vision_model or '默认'} "
            f"图片超时={vision_timeout:.0f}s 贴纸超时={sticker_timeout:.0f}s"
        )

    def _fetch(meta: dict):
        if fetch is not None:
            return fetch(meta)
        return image_utils.fetch_image(meta, timeout=fetch_timeout, max_bytes=max_bytes)

    def _encode(data: bytes, edge: int):
        if encode is not None:
            return encode(data)
        return image_utils.to_data_url(data, max_edge=edge)

    async def _describe(data_url: str, timeout: float):
        if describe is not None:
            result = describe(data_url, text)
            if asyncio.iscoroutine(result):
                return await result
            return result
        return await vision.describe(
            data_url, hint=text,
            preferred=vision_model or None,
            timeout=timeout,
        )

    seen_window = float(image_cfg.get("seen_window_seconds", 60) or 60)
    # 只返回第一张成功的描述（max_images_per_turn 目前固定 1，与群聊一致）；
    # 贴纸/重复/失败会继续看下一个候选。
    for meta in candidates:
        if not isinstance(meta, dict):
            continue
        is_sticker = image_utils.is_sticker(meta)
        kind = "sticker" if is_sticker else "image"
        # 默认识别贴纸；只有显式关闭 recognize_stickers 时才回到旧行为。
        if is_sticker and not recognize_stickers and source != "引用":
            if logger:
                logger.info("[图片] 跳过：贴纸/表情（recognize_stickers=false）")
            continue
        signature = image_cache.image_hash(meta)
        if signature and image_cache.seen_hash(key, signature, seen_window):
            cached_desc, cached_kind = _cached_description(key, signature, seen_window)
            if cached_desc:
                if logger:
                    logger.info("[图片] 复用：重复图已有的有效描述")
                return {
                    "desc": cached_desc,
                    "source": source,
                    "signature": signature,
                    "kind": cached_kind,
                }
            if _has_new_question(text):
                # 上次可能失败或只返回安全占位；新附言是重新理解的重要上下文。
                if logger:
                    logger.info("[图片] 重试：重复图无有效描述，且本次附带了新文字")
            else:
                if logger:
                    logger.info(f"[图片] 跳过：{seen_window:.0f}s 内重复图")
                return {
                    "desc": "",
                    "source": source,
                    "signature": signature,
                    "kind": kind,
                    "skipped": "duplicate",
                }

        # 贴纸/普通图分开限流，互不占用额度。
        rate_key = f"{key}:sticker" if is_sticker else key
        allowed, used = rate_ok(rate_key, sticker_limit if is_sticker else normal_limit, 3600.0)
        if not allowed:
            if logger:
                logger.info(
                    f"[图片] 限流：{'贴纸' if is_sticker else '图片'}本小时已用 {used} 张，跳过"
                )
            continue
        if is_sticker and logger:
            logger.info(
                f"[图片] 识别贴纸：sub_type={image_utils._to_int(meta.get('sub_type'), 0)} "
                f"summary={str(meta.get('summary') or '')[:40]!r}"
            )
        edge = sticker_edge if is_sticker else max_edge
        timeout = sticker_timeout if is_sticker else vision_timeout

        try:
            t_fetch = time.monotonic()
            img_bytes = await asyncio.to_thread(_fetch, meta)
            fetch_ms = int((time.monotonic() - t_fetch) * 1000)
            if not img_bytes:
                if logger:
                    logger.info(f"[图片] 下载失败/无 url（{fetch_ms}ms，保持占位符）")
                continue
            t_encode = time.monotonic()
            data_url = await asyncio.to_thread(_encode, img_bytes, edge)
            encode_ms = int((time.monotonic() - t_encode) * 1000)
            if not data_url:
                if logger:
                    logger.info(f"[图片] 图片转码失败（{encode_ms}ms，保持占位符）")
                continue
            t_vision = time.monotonic()
            ok, desc = await asyncio.wait_for(_describe(data_url, timeout), timeout=timeout)
            vision_ms = int((time.monotonic() - t_vision) * 1000)
            if ok and vision.is_sensitive_reply(desc):
                if logger:
                    logger.info(
                        f"[图片] 内容安全占位：来源={source} 模型={vision_model or '默认'} "
                        f"识图={vision_ms}ms，不视为有效描述"
                    )
                return {
                    "desc": "",
                    "source": source,
                    "signature": signature,
                    "kind": kind,
                    "skipped": "blocked",
                }
            if ok and str(desc or "").strip():
                clean_desc = str(desc).strip()
                _cache_description(key, signature, clean_desc, kind)
                if logger:
                    logger.info(
                        f"[图片] {'贴纸' if is_sticker else ''}识图成功：来源={source} "
                        f"模型={vision_model or '默认'} 下载={fetch_ms}ms 编码={encode_ms}ms "
                        f"识图={vision_ms}ms 描述={len(clean_desc)}字"
                    )
                return {
                    "desc": clean_desc,
                    "source": source,
                    "signature": signature,
                    "kind": kind,
                }
            if logger:
                logger.info(
                    f"[图片] 识图失败（下载={fetch_ms}ms 编码={encode_ms}ms "
                    f"识图={vision_ms}ms，保持占位符）"
                )
        except asyncio.TimeoutError:
            if logger:
                logger.info(
                    f"[图片] 识图超时（>{timeout:.0f}s，模型={vision_model or '默认'}，"
                    f"保持占位符）"
                )
            break
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.info(f"[图片] 识图异常（已忽略）: {type(exc).__name__}: {exc}")
            continue
    return None
