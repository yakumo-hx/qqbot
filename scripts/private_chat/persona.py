# -*- coding: utf-8 -*-
"""私聊人设：完全独立的人设文件 + 编译卡。

私聊不再读取群聊的 `persona.txt`。人设真相源在：

    config/characters/<角色>/private/
      identity.md         身份 / 性格 / 日常
      voice.md            自称 / 说话方式 / 口头禅 / 黑名单
      relationship.json   四档关系、称呼、边界
      corrections.json    纠偏规则（移植自 love-companion 的纠偏语义）

每轮只把「编译卡」放进 system prompt，不注入完整人设文件。
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Optional

from . import config as cfgmod
from . import corrections as corrections_mod
from . import search as search_mod
from .store import PrivateStore

_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
_STAGES = ("stranger", "familiar", "close", "intimate")

_TYPE_LABEL = {
    "user_fact": "关于对方",
    "user_preference": "对方的喜好",
    "relationship": "你们之间",
    "event": "共同经历",
    "promise": "约定",
    "self_fact": "关于你自己",
    "self_feeling": "你的感受",
}


def private_dir(cfg: dict) -> str:
    character = str(cfg.get("character") or "").strip() or cfgmod.get_active_character()
    return os.path.join(cfgmod.CHARACTERS_DIR, character, "private")


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _section(markdown: str, heading: str) -> str:
    """取 `## heading` 到下一个 `##` 之间的正文。"""
    match = re.search(rf"^##\s+{re.escape(heading)}\s*$", markdown, flags=re.MULTILINE)
    if not match:
        return ""
    rest = markdown[match.end():]
    nxt = re.search(r"^##\s+", rest, flags=re.MULTILINE)
    body = rest[:nxt.start()] if nxt else rest
    return body.strip()


def _clean(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", str(text or "")).strip()
    text = re.sub(r"[ \t]+", " ", text)
    return text


def _truncate(text: str, limit: int) -> str:
    text = _clean(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)].rstrip() + "…"


def _raw_persona_text(cfg: dict) -> str:
    """raw 模式：直接读取原始人设长文；不解析任何 ## 小节。

    raw_file 默认相对于角色目录（<character>/_source/raw.md）。
    """
    data = cfg.get("persona")
    if not isinstance(data, dict):
        return ""
    if str(data.get("mode") or "card").strip().lower() != "raw":
        return ""
    name = str(data.get("raw_file") or "_source/raw.md").strip() or "_source/raw.md"
    if os.path.isabs(name):
        path = name
    else:
        path = os.path.join(os.path.dirname(private_dir(cfg)), name)
    return _clean(_read(path))


def load_identity(cfg: dict) -> dict:
    md = _read(os.path.join(private_dir(cfg), "identity.md"))
    return {
        "core": _clean(_section(md, "核心身份")),
        "traits": _clean(_section(md, "性格")),
        "daily": _clean(_section(md, "日常")),
        "avoid": _clean(_section(md, "不要演成")),
    }


def load_voice(cfg: dict) -> dict:
    md = _read(os.path.join(private_dir(cfg), "voice.md"))
    return {
        "self": _clean(_section(md, "自称")),
        "speaking": _clean(_section(md, "说话方式")),
        "phrases": _clean(_section(md, "常用语料")),
        "blacklist": _clean(_section(md, "AI 味黑名单（出现即失败）")),
        "address": _clean(_section(md, "私聊称呼")),
    }


def load_relationship(cfg: dict) -> dict:
    data = cfgmod.read_json(os.path.join(private_dir(cfg), "relationship.json"), {})
    if not isinstance(data, dict):
        data = {}
    stages = data.get("stages") if isinstance(data.get("stages"), dict) else {}
    default = str(data.get("default_stage") or "familiar")
    if default not in _STAGES:
        default = "familiar"
    return {
        "default_stage": default,
        "address": str(data.get("address") or "你"),
        "stages": stages,
        "boundaries": [str(x) for x in (data.get("boundaries") or []) if str(x).strip()],
    }


def load_corrections(cfg: dict) -> list[dict]:
    return corrections_mod.load(os.path.join(private_dir(cfg), "corrections.json"))


def stage_of(store: PrivateStore, uid: str, rel: dict) -> tuple[str, dict]:
    profile = store.profile(uid)
    raw = profile.get("relationship") if isinstance(profile.get("relationship"), dict) else {}
    stage = str(raw.get("stage") or rel.get("default_stage") or "familiar")
    if stage not in _STAGES:
        stage = rel.get("default_stage") or "familiar"
    info = (rel.get("stages") or {}).get(stage)
    if not isinstance(info, dict):
        info = {}
    return stage, info


def _address_of(rel: dict, voice: dict, prof: dict) -> str:
    """称呼优先级：relationship.address > voice.address（短文本）> QQ 昵称。"""
    for source in (rel.get("address"), voice.get("address")):
        text = str(source or "").strip()
        if 0 < len(text) <= 20:
            return text
    return cfgmod.normalize_display_name(prof.get("display_name"))


def _persona_int(cfg: dict, key: str, default: int) -> int:
    data = cfg.get("persona")
    raw = data.get(key) if isinstance(data, dict) else None
    try:
        return max(1, int(raw if raw is not None else default))
    except (TypeError, ValueError):
        return default


def _cap(cfg: dict, key: str, default: int) -> int:
    data = cfg.get("persona")
    caps = data.get("caps") if isinstance(data, dict) else None
    raw = caps.get(key) if isinstance(caps, dict) else None
    try:
        return max(1, int(raw if raw is not None else default))
    except (TypeError, ValueError):
        return default


def _time_line() -> str:
    now = datetime.now()
    return f"{now.strftime('%Y-%m-%d %H:%M')}（{_WEEKDAYS[now.weekday()]}）"


def _relationship_line(prof: dict) -> str:
    rel = prof.get("relationship") if isinstance(prof.get("relationship"), dict) else {}
    stage = rel.get("stage") or "familiar"
    closeness = rel.get("closeness")
    first = prof.get("first_seen") or ""
    days = ""
    try:
        dt = datetime.fromisoformat(str(first))
        days = f"，认识第 {max(1, (datetime.now().astimezone() - dt.astimezone()).days + 1)} 天"
    except (TypeError, ValueError):
        pass
    closeness_text = f"，亲近度 {closeness}" if isinstance(closeness, (int, float)) else ""
    return f"关系阶段：{stage}{closeness_text}{days}"


def _memory_block(items: list[dict]) -> str:
    if not items:
        return "（暂时还没有值得长期记住的事）"
    lines = []
    for item in items:
        label = _TYPE_LABEL.get(str(item.get("type")), "记忆")
        text = str(item.get("text") or "").strip()
        if text:
            lines.append(f"- [{label}] {text}")
    return "\n".join(lines) if lines else "（暂时还没有值得长期记住的事）"


def _self_state_block(state: dict) -> str:
    parts = []
    mood = str(state.get("mood") or "").strip()
    energy = state.get("energy")
    if mood:
        parts.append(f"心情：{mood}")
    if isinstance(energy, (int, float)):
        parts.append(f"精力：{float(energy):.2f}")
    today = str(state.get("today") or "").strip()
    if today:
        parts.append(f"你今天：{today}")
    looking = str(state.get("looking_forward_to") or "").strip()
    if looking:
        parts.append(f"你在惦记：{looking}")
    thoughts = state.get("recent_thoughts")
    if isinstance(thoughts, list) and thoughts:
        parts.append("最近的想法：" + "；".join(str(x) for x in thoughts[:3] if str(x).strip()))
    return "\n".join(parts) if parts else "（没什么特别的状态，就是平常的一天）"


def build_persona_card(cfg: dict, store: PrivateStore, uid: str,
                       max_chars: Optional[int] = None) -> str:
    """把私聊人设编译成一张卡。

    默认仍走四文件结构化编译；`persona.mode="raw"` 时直接读取
    `persona.raw_file`（默认 `<character>/_source/raw.md`）作为整张卡，
    不再解析 `##` 小节。上限取自 card_max_chars / raw_max_chars。
    """
    if max_chars is None:
        max_chars = _persona_int(cfg, "card_max_chars", 1600)
    raw = _raw_persona_text(cfg)
    if raw:
        limit = max(1, min(max_chars, _persona_int(cfg, "raw_max_chars", max_chars)))
        return _truncate("【人设原文】以下是你的人物设定，请按此扮演：\n\n" + raw, limit)
    ident = load_identity(cfg)
    voice = load_voice(cfg)
    rel = load_relationship(cfg)
    stage, info = stage_of(store, uid, rel)
    prof = store.profile(uid)
    address = _address_of(rel, voice, prof)

    core = _truncate(ident.get("core"), _cap(cfg, "core", 420))
    if not core:
        core = "你是大肥鱼，一个正在和对方一对一私聊的蓝色鲸鱼娘；短句、口语、傲娇但心软。"

    stage_label = str(info.get("label") or stage)
    tone = _truncate(str(info.get("tone") or ""), 160)
    must_parts = [
        "【身份】" + core,
        f"【关系】当前阶段：{stage_label}（{stage}）；{tone} 称呼对方：{address}。",
        "【边界】" + ("；".join(rel.get("boundaries") or []) or "不编造、不越界。"),
    ]
    corr = corrections_mod.rules_text(load_corrections(cfg), limit=3)
    if corr:
        must_parts.append("【纠偏】" + corr)
    must = "\n".join(must_parts)

    nice_parts = []
    if ident.get("traits"):
        nice_parts.append("【性格】" + _truncate(ident["traits"], _cap(cfg, "traits", 220)))
    if voice.get("self") or voice.get("speaking"):
        nice_parts.append("【说话】" + _truncate(" ".join(x for x in (voice.get("self"), voice.get("speaking")) if x), _cap(cfg, "speaking", 320)))
    if voice.get("blacklist"):
        nice_parts.append("【禁区】" + _truncate(voice["blacklist"], _cap(cfg, "blacklist", 240)))
    if ident.get("daily"):
        nice_parts.append("【日常】" + _truncate(ident["daily"], _cap(cfg, "daily", 160)))
    if voice.get("phrases"):
        nice_parts.append("【常用语】" + _truncate(voice["phrases"], _cap(cfg, "phrases", 180)))
    if ident.get("avoid"):
        nice_parts.append("【不要演成】" + _truncate(ident["avoid"], _cap(cfg, "avoid", 120)))
    nice = "\n".join(nice_parts)

    room = max_chars - len(must) - 1
    if room <= 0:
        return _truncate(must, max_chars)
    if nice and len(nice) > room:
        nice = _truncate(nice, room)
    card = (must + "\n" + nice).strip() if nice else must
    return card


def _limit_of(cfg: dict, behavior=None, rhythm=None) -> int:
    reply_cfg = cfg.get("reply") or {}
    hard = max(1, int(reply_cfg.get("max_bubbles", 3) or 3))
    behavior_cap = int(getattr(behavior, "bubble_cap", hard) or hard)
    rhythm_cap = int(getattr(rhythm, "max_bubbles", hard) or hard)
    return max(1, min(hard, behavior_cap, rhythm_cap))


def _behavior_block(behavior, rhythm, cfg: dict) -> str:
    if behavior is None:
        return ""
    intent = str(getattr(behavior, "intent", "chat") or "chat")
    allow_q = "是" if getattr(behavior, "allow_question", True) else "否"
    allow_share = "是" if getattr(behavior, "allow_self_share", True) else "否"
    limit = _limit_of(cfg, behavior, rhythm)
    return (
        f"意图={intent}；允许追问={allow_q}；允许加入自己的经历={allow_share}；"
        f"本轮气泡上限={limit}。"
        "意图只是行为提示，不要求每一种行为都出现。"
    )


def _structured_contract(cfg: dict, behavior=None, rhythm=None) -> str:
    limit = _limit_of(cfg, behavior, rhythm)
    max_chars = int((cfg.get("reply") or {}).get("max_reply_chars", 600) or 600)
    return (
        "【输出契约】\n"
        "只输出一个 JSON 对象，不要解释、不要 Markdown 代码块、不要前后缀：\n"
        "{\"bubbles\":[{\"act\":\"react|answer|support|ask|share|continue|chat\","
        "\"text\":\"完整可发送的一句\"}]}\n"
        f"- 气泡数量 1~{limit} 条；每条必须能独立发送；\n"
        f"- 单条不超过 160 字，整轮不超过 {max_chars} 字；\n"
        "- 气泡内允许换行；不要输出 JSON 以外的任何字符；\n"
        "- 不要为了凑格式强行追问；最后一条不要机械追加问题。"
    )


def _dialogue_block(dialogue, target_messages: list[dict]) -> str:
    if dialogue is None and not target_messages:
        return ""
    lines: list[str] = []
    topic = str(getattr(dialogue, "current_topic", "") or "").strip()
    if topic:
        lines.append(f"当前话题：{topic}")
    question = str(getattr(dialogue, "pending_question", "") or "").strip()
    if question:
        lines.append(f"对方还没被回答的问题：{question}")
    if bool(getattr(dialogue, "awaiting_user", False)):
        lines.append("你上一轮以追问结束，对方正在接你的话。")
    return "\n".join(lines)


def build_system_prompt(cfg: dict, store: PrivateStore, uid: str, query: str = "",
                        max_card_chars: Optional[int] = None, plan: Optional[dict] = None,
                        search: Optional[dict] = None,
                        activity: Optional[str] = None,
                        dialogue=None, behavior=None, rhythm=None,
                        target_messages: Optional[list[dict]] = None,
                        structured: bool = False) -> str:
    prof = store.profile(uid)
    if max_card_chars is None:
        max_card_chars = _persona_int(cfg, "card_max_chars", 1600)
    address = _address_of(load_relationship(cfg), load_voice(cfg), prof)
    card = build_persona_card(cfg, store, uid, max_chars=max_card_chars)
    self_state = store.self_state(uid)
    summary = store.latest_summary(uid)
    # 有本轮用户消息时按相关度检索；没有则按重要度取前 N 条。
    memories = store.retrieve(uid, query, None) if query else store.memory_items(uid)[-10:]
    memories = memories[: int((cfg.get("memory") or {}).get("max_memories_in_prompt", 10) or 10)]
    target_messages = [x for x in (target_messages or []) if isinstance(x, dict)]

    blocks = [
        card,
        f"【场景】现在 {_time_line()}，你在 QQ 上和「{address}」一对一私聊。",
        "【你记得的事（长期记忆，只是背景）】\n" + _memory_block(memories),
        "【你最近的状态】\n" + _self_state_block(self_state),
    ]
    if summary:
        blocks.append("【你们最近聊过什么】\n" + summary)
    if activity:
        blocks.append("【对方的状态】\n" + str(activity).strip())
    if dialogue is not None or target_messages:
        block = _dialogue_block(dialogue, target_messages)
        if block:
            blocks.append("【当前对话状态】\n" + block)
    if behavior is not None:
        blocks.append("【本轮行为】\n" + _behavior_block(behavior, rhythm, cfg))
    if search:
        max_chars = int((cfg.get("search") or {}).get("max_block_chars", 6000) or 6000)
        search_block = search_mod.format_search_block(search, max_chars=max_chars)
        if search_block:
            blocks.append("【本轮搜索资料】\n" + search_block)
    blocks.append(
        "【对话要求】\n"
        "- 像熟人聊天：短句、口语，一次说一两件事，不列清单。\n"
        "- 不用「作为 AI」「有什么可以帮你」「希望对你有帮助」这类客服腔。\n"
        "- 记忆只在自然相关时提，不要机械复述，也不要说「根据我的记忆」。\n"
        "- 不知道就说不记得/不确定，不编造共同经历。\n"
        "- 最后一条 user 消息是本轮必须回应的新消息；优先回应当前内容，"
        "不要复述、续写或稍作改写后重发更早的 assistant 回复。\n"
        "- 如果上下文只有 [图片] 占位符而没有描述，说明你没看到图片内容；"
        "不要假装看到，直接说没看清/让对方描述。\n"
        "- [图片: 描述] 是照片/截图；[表情: 描述] 是对方发的表情包/梗图，"
        "像聊天一样接，不要写成图片分析报告。\n"
        "- 看到 [表情包: 未识别] 表示这次没看成；不要编造画面，可以随口接一句。\n"
        "- 不要暴露其他群聊或私聊的内容。"
    )
    if cfg.get("safety", {}).get("disclose_ai_when_asked", True):
        blocks.append(
            "【身份边界】\n"
            "平时保持角色，不主动提自己是 AI；但如果对方直接问「你是不是 AI / 机器人 / 真人」，"
            "如实承认自己是 AI，不要编造现实身份。"
        )
    if structured:
        blocks.append(_structured_contract(cfg, behavior, rhythm))
    return "\n\n".join(block for block in blocks if block and block.strip())


def build_messages(cfg: dict, store: PrivateStore, uid: str, query: str = "",
                   plan: Optional[dict] = None, search: Optional[dict] = None,
                   activity: Optional[str] = None, *,
                   turns: Optional[list[dict]] = None,
                   dialogue=None, behavior=None, rhythm=None,
                   target_messages: Optional[list[dict]] = None,
                   structured: bool = False) -> list[dict]:
    reply_cfg = cfg.get("reply") or {}
    max_turns = int(reply_cfg.get("max_context_turns", 24) or 24)
    target_messages = [x for x in (target_messages or []) if isinstance(x, dict)]
    target_ids = {str(x.get("msg_id")) for x in target_messages if x.get("msg_id") is not None}
    messages: list[dict] = [
        {"role": "system", "content": build_system_prompt(
            cfg, store, uid, query, plan=plan, search=search, activity=activity,
            dialogue=dialogue, behavior=behavior, rhythm=rhythm,
            target_messages=target_messages, structured=structured)}
    ]
    pool = turns if turns is not None else store.recent_turns(uid, max_turns)
    for turn in list(pool)[-max_turns:]:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        # 本轮 target 在最后作为最新 user 消息出现，历史中不重复。
        if turn.get("role") == "user" and str(turn.get("msg_id")) in target_ids:
            continue
        messages.append({"role": role, "content": text})
    targets = [str(x.get("text") or "").strip() for x in target_messages]
    targets = [x for x in targets if x]
    if len(targets) == 1:
        messages.append({"role": "user", "content": targets[0]})
    elif targets:
        content = "这轮连续发来的消息（请一起理解并回应）：\n" + "\n".join(
            f"{index}. {text}" for index, text in enumerate(targets, start=1)
        )
        messages.append({"role": "user", "content": content})
    return messages


def build_proactive_prompt(cfg: dict, store: PrivateStore, uid: str, hook: str,
                           activity: Optional[str] = None) -> list[dict]:
    prof = store.profile(uid)
    address = _address_of(load_relationship(cfg), load_voice(cfg), prof)
    self_state = store.self_state(uid)
    max_chars = int((cfg.get("proactive") or {}).get("content_max_chars", 80) or 80)
    card_max_chars = _persona_int(cfg, "proactive_card_max_chars", 1200)
    card = build_persona_card(cfg, store, uid, max_chars=card_max_chars)
    system = (
        card
        + "\n\n【场景】\n"
        + f"你在 QQ 上和「{address}」私聊，现在 {_time_line()}。你想主动发一条消息，不是回复对方。\n"
        + (f"【对方的状态】\n{activity}\n" if activity else "")
        + "【你最近的状态】\n" + _self_state_block(self_state) + "\n"
        + "【这次想主动说话的依据】\n" + (hook or "只是突然想到对方") + "\n"
        + "【要求】\n"
        + f"- 只写一条消息，{max(10, max_chars // 3)}~{max_chars} 个中文字符；\n"
        + "- 像真人随口发的，不要「在吗」「忙不忙」「今天过得怎么样」这种模板；\n"
        + "- 不要解释你为什么发这条，不要提记忆系统；\n"
        + "- 不要连续追问，不要制造压力；\n"
        + "- 直接输出消息正文，不要引号、不要前缀。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": "（现在）"}]


__all__ = [
    "build_persona_card",
    "build_system_prompt",
    "build_messages",
    "build_proactive_prompt",
    "load_corrections",
    "load_identity",
    "load_relationship",
    "load_voice",
    "private_dir",
    "stage_of",
]
