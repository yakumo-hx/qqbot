#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""表情包索引构建器。

分工（v1.1 起，与 data/stickers/IMAGE_SPEC.md 一致）：

- **语义标签由外部多模态 agent 产出**：读图 → 情绪/场景/意图/图上文字/检索关键词。
  产物是一个 tags JSON 文件，格式见 `data/stickers/SCHEMA.md` §6。
- **本工具只做机械活**：扫描、哈希、尺寸探测、词表校验、算向量、增量合并、原子落盘。
  它**不调用任何模型**（向量是本地 sentence-transformers 算的）。

这样切分的好处：agent 侧不需要碰哈希/路径/向量这些易错字段，
工具侧也不需要碰网络与网关（本机网关长年 ReadTimeout，实测单次 66~126s）。

子命令：
  scan     扫描图库，输出清单与体检结果（不写任何文件）
  ingest   把外部 agent 的 tags JSON 合并进索引（核心）
  stats    打印索引健康度
  review   查看/处理隔离区（quarantine）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

SUPPORTED_EXT = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MAX_BYTES = 8 * 1024 * 1024
SCHEMA_VERSION = 1
SENSITIVE_MARK = "（图片内容不便描述）"

CONFIG_PATH = os.path.join(_PROJECT_ROOT, "config", "stickers.json")
VOCAB_PATH = os.path.join(_PROJECT_ROOT, "config", "sticker_tags.json")

# 关键词与描述的长度护栏（防止 agent 产出超长字段撑爆索引）
MAX_KEYWORDS = 12
MAX_KEYWORD_LEN = 24
MAX_CAPTION_LEN = 120
MAX_TEXT_LEN = 60


# ---------- 基础 IO ----------

def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json_atomic(path: str, data: Any) -> None:
    """先写同目录临时文件再 os.replace，保证中断时索引不损坏。"""
    folder = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".index-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def norm_rel(path: str) -> str:
    """统一成相对路径 + 正斜杠（索引里 file 字段的规范形态）。"""
    return path.replace("\\", "/").lstrip("./")


# ---------- 配置 / 词表 ----------

def load_cfg() -> Dict[str, Any]:
    return load_json(CONFIG_PATH, {}) or {}


def library_root(cfg: Optional[Dict[str, Any]] = None) -> str:
    cfg = cfg if cfg is not None else load_cfg()
    rel = str(cfg.get("library_root") or "data/stickers/library")
    return rel if os.path.isabs(rel) else os.path.join(_PROJECT_ROOT, rel)


def index_path(cfg: Optional[Dict[str, Any]] = None) -> str:
    cfg = cfg if cfg is not None else load_cfg()
    rel = str(cfg.get("index_path") or "data/stickers/index.json")
    return rel if os.path.isabs(rel) else os.path.join(_PROJECT_ROOT, rel)


def load_vocab() -> Dict[str, set]:
    raw = load_json(VOCAB_PATH, {}) or {}
    out: Dict[str, set] = {}
    for key in ("emotion", "scene", "intent"):
        vals = raw.get(key) or []
        out[key] = {str(v.get("tag")).strip() for v in vals
                    if isinstance(v, dict) and str(v.get("tag") or "").strip()}
    return out


# ---------- 扫描 ----------

def probe_image(abs_path: str) -> Optional[Tuple[int, int, bool]]:
    """返回 (w, h, animated)；打不开返回 None。"""
    try:
        from PIL import Image
        with Image.open(abs_path) as im:
            w, h = im.size
            frames = getattr(im, "n_frames", 1) or 1
            return int(w), int(h), bool(frames > 1)
    except Exception:
        return None


def scan_library(root: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """扫描一层子目录；返回 (条目列表, 问题清单)。条目不含语义字段。"""
    items: List[Dict[str, Any]] = []
    problems: List[str] = []
    if not os.path.isdir(root):
        return items, ["图库目录不存在: " + root]

    def consider(abs_path: str, rel: str, depth: int) -> None:
        ext = os.path.splitext(abs_path)[1].lower()
        if ext not in SUPPORTED_EXT:
            problems.append("扩展名不支持，跳过: " + rel)
            return
        try:
            size = os.path.getsize(abs_path)
        except OSError as exc:
            problems.append("读不到文件: %s (%s)" % (rel, exc))
            return
        if size > MAX_BYTES:
            problems.append("超过 8MiB，跳过: %s (%d B)" % (rel, size))
            return
        probed = probe_image(abs_path)
        if probed is None:
            problems.append("Pillow 打不开，跳过: " + rel)
            return
        w, h, animated = probed
        items.append({
            "id": "", "file": norm_rel(rel), "sha256": "", "bytes": int(size),
            "w": w, "h": h, "animated": animated, "_abs": abs_path,
        })

    for entry in sorted(os.listdir(root)):
        full = os.path.join(root, entry)
        if os.path.isdir(full):
            for sub in sorted(os.listdir(full)):
                subfull = os.path.join(full, sub)
                if os.path.isdir(subfull):
                    problems.append("层级过深，跳过: " + norm_rel(os.path.join(entry, sub)))
                    continue
                if sub.startswith("."):
                    continue
                consider(subfull, os.path.join(entry, sub), 2)
        else:
            if entry.startswith("."):
                continue
            consider(full, entry, 1)

    # 哈希去重（同一内容只保留第一条）
    seen: Dict[str, str] = {}
    uniq: List[Dict[str, Any]] = []
    for it in items:
        try:
            digest = sha256_file(it["_abs"])
        except OSError as exc:
            problems.append("哈希失败: %s (%s)" % (it["file"], exc))
            continue
        if digest in seen:
            problems.append("内容重复，跳过: %s（与 %s 同哈希）" % (it["file"], seen[digest]))
            continue
        seen[digest] = it["file"]
        it["sha256"] = digest
        it["id"] = digest[:12]
        uniq.append(it)
    return uniq, problems


# ---------- 向量 ----------

def embed_texts(texts: List[str]) -> List[List[float]]:
    """本地算向量。编码器不可用时返回空列表（调用点据此降级）。"""
    if not texts:
        return []
    try:
        from scripts.zero_shot_classifier import get_classifier
        clf = get_classifier()
        matrix = clf.encode_batch(texts)
        if matrix is None or len(matrix) == 0:
            return []
        return [[float(x) for x in row] for row in matrix]
    except Exception as exc:
        print("[向量] 编码器不可用，本次不写 vec：%s" % exc, file=sys.stderr)
        return []


def build_embed_text(item: Dict[str, Any]) -> str:
    """把一条索引变成用于编码的自然语言（运行时用同一编码器编码用户消息）。"""
    parts: List[str] = []
    cap = str(item.get("caption") or "").strip()
    if cap:
        parts.append(cap)
    tie = str(item.get("text_in_image") or "").strip()
    if tie:
        parts.append(tie)
    parts.extend(str(k) for k in (item.get("keywords") or []))
    parts.extend(item.get("emotion") or [])
    parts.extend(item.get("scene") or [])
    parts.extend(item.get("intent") or [])
    return "；".join(p for p in parts if p)


# ---------- 索引 ----------

def load_index(path: str) -> Dict[str, Any]:
    raw = load_json(path, None)
    if not isinstance(raw, dict):
        return {"version": SCHEMA_VERSION, "embedding_model": "", "vec_dim": 0,
                "generated_at": _now_iso(), "items": []}
    items = raw.get("items")
    if not isinstance(items, list):
        raw["items"] = []
    raw.setdefault("version", SCHEMA_VERSION)
    return raw


def _clean_str_list(value: Any, allowed: Optional[set], limit: int = 3) -> Tuple[List[str], List[str]]:
    """返回 (合法项, 被丢弃项)。allowed=None 表示不校验词表。"""
    kept: List[str] = []
    dropped: List[str] = []
    if not isinstance(value, (list, tuple)):
        return kept, dropped
    for raw in value:
        tag = str(raw).strip()
        if not tag:
            continue
        if allowed is not None and tag not in allowed:
            dropped.append(tag)
            continue
        if tag not in kept:
            kept.append(tag)
        if len(kept) >= limit:
            break
    return kept, dropped


def normalize_tag_item(raw: Dict[str, Any], vocab: Dict[str, set],
                       file_meta: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """把 agent 产出的语义字段与工具扫出的机械字段合成一条索引项。"""
    notes: List[str] = []
    emo, d1 = _clean_str_list(raw.get("emotion"), vocab.get("emotion"))
    scn, d2 = _clean_str_list(raw.get("scene"), vocab.get("scene"))
    itt, d3 = _clean_str_list(raw.get("intent"), vocab.get("intent"))
    for label, dropped in (("emotion", d1), ("scene", d2), ("intent", d3)):
        if dropped:
            notes.append("%s 词表外标签已丢弃: %s" % (label, ",".join(dropped[:5])))

    tie = str(raw.get("text_in_image") or "").strip()[:MAX_TEXT_LEN]

    kws: List[str] = []
    for k in (raw.get("keywords") or []):
        kk = str(k).strip()[:MAX_KEYWORD_LEN]
        if kk and kk not in kws:
            kws.append(kk)
        if len(kws) >= MAX_KEYWORDS:
            break
    if tie and tie not in kws:
        kws.insert(0, tie)

    caption = str(raw.get("caption") or "").strip()[:MAX_CAPTION_LEN]
    try:
        intensity = int(raw.get("intensity", 0))
    except (TypeError, ValueError):
        intensity = 0
    intensity = max(0, min(3, intensity))

    safe = raw.get("safe", True)
    unsafe = (safe is False) or (SENSITIVE_MARK in caption)
    if unsafe:
        notes.append("标记为 quarantine（safe=false 或敏感内容）")

    if not caption and not kws:
        return None, notes + ["caption 与 keywords 都为空，无可检索内容"]

    return {
        "id": file_meta["id"],
        "file": file_meta["file"],
        "sha256": file_meta["sha256"],
        "bytes": file_meta["bytes"],
        "w": file_meta["w"],
        "h": file_meta["h"],
        "animated": file_meta["animated"],
        "emotion": emo,
        "scene": scn,
        "intent": itt,
        "text_in_image": tie,
        "keywords": kws,
        "caption": caption,
        "intensity": intensity,
        "quarantine": bool(unsafe),
        "tagged_at": _now_iso(),
        "model": str(raw.get("_model") or raw.get("model") or "external-agent"),
    }, notes


# ---------- 子命令 ----------

def cmd_scan(args: argparse.Namespace) -> int:
    cfg = load_cfg()
    root = library_root(cfg)
    print("[scan] 图库 = %s" % root)
    items, problems = scan_library(root)
    animated = sum(1 for i in items if i["animated"])
    print("[scan] 合格文件 = %d（其中动图 %d）" % (len(items), animated))
    if problems:
        print("[scan] 问题 %d 条：" % len(problems))
        for p in problems:
            print("       - " + p)
    if args.json:
        slim = [{k: v for k, v in i.items() if k != "_abs"} for i in items]
        print(json.dumps(slim, ensure_ascii=False, indent=1))
    else:
        for i in items[: args.limit]:
            print("  %-58s %sx%s %s" % (
                i["file"][:56], i["w"], i["h"], "动图" if i["animated"] else "静态"))
        if len(items) > args.limit:
            print("  ...（其余 %d 条省略，--limit 调整）" % (len(items) - args.limit))
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    cfg = load_cfg()
    root = library_root(cfg)
    ipath = index_path(cfg)
    vocab = load_vocab()

    tags = load_json(args.tags, None)
    if not isinstance(tags, dict) or not isinstance(tags.get("items"), list):
        print("[ingest] tags 文件格式不对（需要 {version, items:[...]}）: %s" % args.tags)
        return 2

    print("[ingest] tags = %s（%d 条）" % (args.tags, len(tags["items"])))
    print("[ingest] 图库 = %s" % root)

    scanned, problems = scan_library(root)
    by_file = {i["file"]: i for i in scanned}
    for p in problems:
        print("       ! " + p)

    index = load_index(ipath)
    known = {str(it.get("file")) for it in index["items"] if isinstance(it, dict)}
    print("[ingest] 现有索引 %d 条，图库合格 %d 张" % (len(index["items"]), len(scanned)))

    merged: List[Dict[str, Any]] = []
    skipped_missing: List[str] = []
    skipped_empty: List[str] = []
    all_notes: List[str] = []

    for raw in tags["items"]:
        if not isinstance(raw, dict):
            continue
        rel = norm_rel(str(raw.get("file") or ""))
        if not rel:
            continue
        meta = by_file.get(rel)
        if meta is None:
            skipped_missing.append(rel)
            continue
        entry, notes = normalize_tag_item(raw, vocab, meta)
        if entry is None:
            skipped_empty.append(rel)
            all_notes.extend("%s: %s" % (rel, n) for n in notes)
            continue
        merged.append(entry)
        all_notes.extend("%s: %s" % (rel, n) for n in notes)

    print("[ingest] 合成 %d 条" % len(merged))
    if skipped_missing:
        print("[ingest] 跳过 %d 条（图库里找不到对应文件）：" % len(skipped_missing))
        for f in skipped_missing[:10]:
            print("       - " + f)
    if skipped_empty:
        print("[ingest] 跳过 %d 条（语义字段为空）" % len(skipped_empty))
    if all_notes:
        print("[ingest] 提示 %d 条：" % len(all_notes))
        for n in all_notes[:15]:
            print("       - " + n)

    # 增量：同 file 覆盖，新 file 追加；未出现在本次 tags 里的旧条目保留
    by_existing = {str(it.get("file")): it for it in index["items"] if isinstance(it, dict)}
    for entry in merged:
        by_existing[entry["file"]] = entry
    final_items = sorted(by_existing.values(), key=lambda x: str(x.get("file")))

    # 向量：只给「没有 vec」或「语义字段变了」的条目重算
    # 需要算向量的条目 = 「本次刚合并进来的」+「任何还没有向量的」。
    # 前者是关键：重打标会改 emotion/keywords/caption，而 vec 正是由它们算出来的，
    # 只补「没有 vec 的」会让重打过的条目挂着旧向量 —— 标签变了、匹配却还是旧的。
    merged_files = {e["file"] for e in merged}
    need: List[Dict[str, Any]] = []
    if cfg.get("embedding_enabled", True):
        for it in final_items:
            stale = str(it.get("file")) in merged_files
            blank = not isinstance(it.get("vec"), list) or not it["vec"]
            if stale or blank:
                need.append(it)
    if need:
        print("[ingest] 需要计算向量 %d 条 ..." % len(need))
        vectors = embed_texts([build_embed_text(i) for i in need])
        if vectors and len(vectors) == len(need):
            dim = len(vectors[0])
            for it, vec in zip(need, vectors):
                it["vec"] = vec
            index["vec_dim"] = dim
            try:
                from scripts.zero_shot_classifier import _DEFAULT_MODEL
                index["embedding_model"] = str(_DEFAULT_MODEL)
            except Exception:
                index["embedding_model"] = "unknown"
            print("[ingest] 向量维度 = %d" % dim)
        else:
            print("[ingest] 向量计算不可用，本次不写 vec（运行时将退化为纯字面匹配）")
    elif not cfg.get("embedding_enabled", True):
        print("[ingest] 配置 embedding_enabled=false，跳过向量计算")

    index["version"] = SCHEMA_VERSION
    index["items"] = final_items
    index["generated_at"] = _now_iso()

    quarantined = sum(1 for i in final_items if i.get("quarantine"))
    with_vec = sum(1 for i in final_items if i.get("vec"))
    print("[ingest] 结果：共 %d 条（隔离 %d，含向量 %d）" % (len(final_items), quarantined, with_vec))

    if args.dry_run:
        print("[ingest] --dry-run，未写盘。目标 = %s" % ipath)
        return 0
    save_json_atomic(ipath, index)
    print("[ingest] 已原子写入 %s" % ipath)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = load_cfg()
    ipath = index_path(cfg)
    root = library_root(cfg)
    index = load_index(ipath)
    items = [i for i in index["items"] if isinstance(i, dict)]
    print("[stats] 索引 = %s" % ipath)
    print("[stats] 条目 = %d    版本 = %s    vec_dim = %s" % (
        len(items), index.get("version"), index.get("vec_dim")))
    if not items:
        return 0

    from collections import Counter
    emo: Counter = Counter()
    scn: Counter = Counter()
    for it in items:
        emo.update(it.get("emotion") or [])
        scn.update(it.get("scene") or [])
    print("[stats] 隔离 = %d    含向量 = %d    有图上文字 = %d    动图 = %d" % (
        sum(1 for i in items if i.get("quarantine")),
        sum(1 for i in items if i.get("vec")),
        sum(1 for i in items if str(i.get("text_in_image") or "").strip()),
        sum(1 for i in items if i.get("animated")),
    ))
    missing = [i["file"] for i in items
               if not os.path.isfile(os.path.join(root, str(i.get("file") or "")))]
    if missing:
        print("[stats] 磁盘上找不到 %d 个文件（运行时会被跳过）：" % len(missing))
        for f in missing[:10]:
            print("        - " + f)
    print("[stats] 情绪分布 top8 = %s" % (emo.most_common(8) or "无"))
    print("[stats] 场景分布 top8 = %s" % (scn.most_common(8) or "无"))
    uncovered = [i["file"] for i in items
                 if not (i.get("emotion") or i.get("scene") or i.get("intent"))]
    if uncovered:
        print("[stats] 没有任何语义标签的条目 = %d（这些几乎匹配不上任何场景）" % len(uncovered))
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    cfg = load_cfg()
    ipath = index_path(cfg)
    index = load_index(ipath)
    items = [i for i in index["items"] if isinstance(i, dict)]
    q = [i for i in items if i.get("quarantine")]

    if args.release or args.drop:
        target = args.release or args.drop
        hit = next((i for i in items if i.get("id") == target or i.get("file") == target), None)
        if hit is None:
            print("[review] 找不到条目: %s" % target)
            return 2
        if args.release:
            hit["quarantine"] = False
            print("[review] 已解除隔离: %s" % hit.get("file"))
        else:
            index["items"] = [i for i in items if i is not hit]
            print("[review] 已从索引移除: %s" % hit.get("file"))
        index["generated_at"] = _now_iso()
        save_json_atomic(ipath, index)
        return 0

    print("[review] 隔离区 = %d / %d" % (len(q), len(items)))
    for i in q:
        print("  id=%s  %s" % (i.get("id"), i.get("file")))
        print("        caption = %s" % (i.get("caption") or "(空)"))
    if q:
        print("")
        print("  解除隔离：python scripts\\sticker_manager.py review --release <id>")
        print("  永久移除：python scripts\\sticker_manager.py review --drop <id>")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="表情包索引构建器（不调用任何模型）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="扫描图库并体检（不写文件）")
    p.add_argument("--json", action="store_true", help="输出 JSON 清单")
    p.add_argument("--limit", type=int, default=30, help="表格模式最多列几条")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("ingest", help="把多模态 agent 的 tags JSON 合并进索引")
    p.add_argument("tags", help="tags JSON 路径")
    p.add_argument("--dry-run", action="store_true", help="只演算不写盘")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("stats", help="索引健康度")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("review", help="查看/处理隔离区")
    p.add_argument("--release", metavar="ID", help="解除某条隔离")
    p.add_argument("--drop", metavar="ID", help="从索引移除某条")
    p.set_defaults(func=cmd_review)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
