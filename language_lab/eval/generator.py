# -*- coding: utf-8 -*-
"""生成器：给定「上下文 + 规范」，让模型写出群成员式的回复。

- 规范来自 `language_lab/rules/rules_v1_prompt.txt`（可换 spec）；
- 输出 1-3 条气泡 + 是否使用媒体；
- 所有结果缓存到 `eval/data/cache/<spec>/<scenario>.json`，便于复算与重跑。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

SPEC_PATH = BASE / "language_lab" / "rules" / "rules_v1_prompt.txt"
CACHE_DIR = Path(__file__).resolve().parent / "data" / "cache"

TYPE_HINT = {
    "reply": "对方刚引用了你说的话，接他的话。",
    "mention": "有人 @ 了你，回应他。",
    "media_context": "刚才有人发了图片/表情。你可以用文字接，也可以回一个表情/图；按群的常态自然选择。",
    "media": "群里刚出现图片/表情，你可以决定用文字还是也用媒体回应。",
    "initiation": "群里安静了一会儿，你可以主动起个话头。",
    "question": "有人在问问题，回答他。",
    "continue": "群聊进行中，自然地接一句。",
}

_SYSTEM = ("你是这个 QQ 群里的普通成员，不是助手、客服或 AI。"
           "只输出接下来会发的消息，不要解释，不要 Markdown，不要自称 AI。")


def load_spec(path: str | Path | None = None) -> tuple[str, str]:
    p = Path(path) if path else SPEC_PATH
    text = p.read_text(encoding="utf-8").strip()
    return p.stem, text


def _ctx_text(sc: dict) -> str:
    lines = []
    for c in sc.get("context") or []:
        text = (c.get("text") or "").strip()
        if not text and c.get("media"):
            text = f"[{c['media'][0]}]"
        lines.append(f"[{c.get('nick') or c.get('uid')}] {text}")
    return "\n".join(lines)


def build_prompt(sc: dict, spec_text: str) -> list[dict]:
    target = sc.get("target") or {}
    target_line = ""
    if target:
        target_line = f"\n【重点对象】{target.get('nick') or target.get('uid')}：{target.get('text') or '[媒体]'}"
    media_hint = ""
    if sc.get("type") in ("media", "media_context"):
        media_hint = ("\n【可选】如果这轮更像发个表情/图片，就设 use_media=true，bubbles 留空。"
                      '示例：{"skip": false, "use_media": true, "bubbles": []}')
    user = (
        f"{spec_text}\n\n"
        f"【本回合】{TYPE_HINT.get(sc.get('type'), '自然接话')}\n"
        f"【最近聊天】\n{_ctx_text(sc)}{target_line}{media_hint}\n\n"
        '输出 JSON：{"skip": false, "use_media": false, "bubbles": ["..."]}\n'
        "要求：bubbles 1-3 条，每条尽量短（多数 ≤10 字），可以只回半句/一个字；"
        "不要 Markdown、不要列表、不要总结、不要自称 AI。"
    )
    return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]


def _extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def _clean_bubbles(data: dict) -> dict:
    bubbles = data.get("bubbles")
    if isinstance(bubbles, str):
        bubbles = [bubbles]
    if not isinstance(bubbles, list):
        bubbles = []
    cleaned = []
    for b in bubbles[:3]:
        t = str(b or "").strip()
        t = re.sub(r"^\[[^\]]{1,20}\]\s*", "", t)     # 去 [昵称] 前缀
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            cleaned.append(t[:80])
    return {
        "skip": bool(data.get("skip")),
        "use_media": bool(data.get("use_media")),
        "bubbles": cleaned,
    }


def generate(sc: dict, spec_id: str, spec_text: str, force: bool = False,
             retries: int = 3, preferred: str | None = None) -> dict:
    cache_path = CACHE_DIR / spec_id / (preferred or "") / f"{sc['id']}.json"
    if cache_path.exists() and not force:
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    result = {"scenario_id": sc["id"], "spec": spec_id, "ok": False, "error": None,
              "model": None, "bubbles": [], "skip": False, "use_media": False,
              "raw": None, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        from scripts import llm_pool
    except ImportError as exc:
        result["error"] = f"llm_pool 不可用: {exc}"
        return result
    messages = build_prompt(sc, spec_text)
    for attempt in range(retries):
        try:
            text, model = llm_pool.chat_sync(messages, label="lab_eval", max_tokens=320,
                                             temperature=0.8, timeout=60, preferred=preferred)
            data = _extract_json(text)
            if data is None:
                lines = [ln.strip(" -\"“”") for ln in (text or "").splitlines() if ln.strip()]
                data = {"bubbles": lines[:3]}
            parsed = _clean_bubbles(data)
            result.update(parsed)
            result.update({"ok": bool(parsed["bubbles"]) or parsed["skip"] or parsed["use_media"],
                           "model": model, "raw": text})
            break
        except Exception as exc:  # 模型抖动：重试
            result["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            time.sleep(2 + attempt * 3)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
