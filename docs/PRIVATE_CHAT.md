# 单人私聊陪伴版 0.0.1

这是一个**独立于群聊机器人**的新版本：只处理 QQ 私聊，只服务一个 QQ 号，重点是「像人一样聊天 + 长期记忆 + 主动消息」。

- 群聊机器人：`reverse_bot.py`，端口 **8080**（保持原样）
- 私聊陪伴版：`private_bot.py`，端口 **8081**（新）
- 两者可以同时运行，互不影响。

---

## 1. 启动

```powershell
cd E:\QQbot
$env:PYTHONIOENCODING="utf-8"

# 私聊陪伴版（新）
.\venv\Scripts\python private_bot.py

# 群聊机器人（旧，按需）
.\venv\Scripts\python reverse_bot.py
```

> `PYTHONIOENCODING=utf-8` 是必需的，否则 Windows 中文控制台会乱码/崩溃。

---

## 2. NapCat 配置

`NapCat\NapCat.44498.Shell\versions\9.9.26-44498\resources\app\napcat\config\onebot11_2668851638.json` 里已经追加了第二个 WebSocket 客户端：

```json
{
  "enable": true,
  "name": "Connect-to-Private-8081",
  "url": "ws://127.0.0.1:8081",
  "reportSelfMessage": false,
  "messagePostFormat": "array",
  "token": "",
  "debug": false,
  "heartInterval": 30000,
  "reconnectInterval": 30000,
  "verifyCertificate": true
}
```

**改完需要重启 NapCat** 才会加载。原配置备份在：

`qq_native_re/patches/napcat_private_20261020/onebot11_2668851638.json`

---

## 3. 配置 `config/private_chat.json`

| 字段 | 默认 | 说明 |
|---|---|---|
| `port` | `8081` | 私聊 WS 端口 |
| `allowed_users` | `["784427550"]` | 只服务这些 QQ；其他人私聊静默忽略 |
| `auto_accept_friends` | `true` | 只自动通过白名单好友请求 |
| `character` | `大肥鱼` | 角色目录名；空 = 用 `config/active_character.json` |
| `reply.pipeline` | `structured` | `structured`（默认）/ `runtime`（纯文本降级）/ `legacy`（旧链路回退） |
| `reply.debounce_seconds` | `2.0` | 你连续发多条时，合并理解的等待时间 |
| `reply.typing_speed_chars_per_second` | `12` | 模拟打字速度；越大回得越快 |
| `reply.max_bubbles` | `3` | 一次回复最多拆成几条（最终还会被行为/节奏上限收紧） |
| `reply.structured_max_tokens` | `900` | 结构化气泡单次模型调用的 token 上限 |
| `reply.send_ack_timeout_seconds` | `5` | 单个气泡等待 OneBot 回执的超时 |
| `reply.llm_timeout_seconds` | `30` | 聊天单次模型调用上限（上游慢，已放宽） |
| `reply.llm_total_budget_seconds` | `45` | 聊天整次调用的总预算 |
| `memory.extract_every_turns` | `4` | 每 4 个用户 turn 抽取一次记忆 |
| `memory.llm_timeout_seconds` | `60` | 记忆抽取/整理单次超时（上游慢，已放宽） |
| `memory.llm_total_budget_seconds` | `90` | 记忆抽取/整理总预算 |
| `memory.retention_days` | `0` | `0` = 永久保留 |
| `memory.raw_retention_days` | `0` | 原始对话也永久保留 |
| `memory.organize_with_llm` | `true` | 允许 LLM 做抽取与日整理 |
| `proactive.daily_min` / `daily_max` | `3` / `5` | 每天主动消息目标区间 |
| `proactive.min_interval_seconds` | `7200` | 两次主动消息最小间隔 2h |
| `proactive.quiet_hours` | `[23, 8]` | 23:00–08:00 不发主动消息 |
| `proactive.silence_hours` | `2` | 你最近 2h 内说过话就不主动打扰 |
| `proactive.max_unanswered` | `3` | 连续 3 条主动未回复就停 |
| `proactive.dry_run` | `false` | 改 `true` 只写日志、不真发 |
| `style.enabled` | `true` | 回复节奏统计/多气泡 |
| `style.burst_window_seconds` | `8` | 连发判定窗口 |
| `style.long_form_chars` | `80` | 长段判定阈值 |
| `activity.enabled` | `true` | 当前活动/日程存储 |
| `activity.extract_every_turns` | `4` | 每 4 个用户 turn 或命中活动词时抽取 |
| `activity.retention_days` | `90` | 活动历史保留期 |
| `activity.proactive_busy_factor` | `0.35` | 忙时主动消息概率倍率（不硬禁止） |
| `image.preprocess_timeout_seconds` | `15` | 单条图片消息的识图总上限；超时标未识别后继续聊天 |

### 3.1 运行时与结构化回复（0.0.1 重构）

普通回复不再由 `engine.py` 自己防抖/发送，链路是：

```text
on_message（快速校验/落库/入队）
  → ConversationRuntime（每用户有序队列 + revision）
  → 图片 enrichment（≤15s，update_turn 原地更新）
  → BehaviorPolicy（answer/support/chat/follow_up/wait）
  → ResponseComposer（一次在线模型调用，结构化气泡）
  → TurnSender（控制确认 > 普通回复 > 主动消息；逐气泡回执/落盘）
```

- 新消息到达会让 `revision` 立即失效旧回复；已发出的 RPC 允许完成并落盘，
  尚未发送的旧气泡全部取消；已确认的气泡按 `target_msg_ids` 差集清 pending。
- 每个 assistant 气泡单独写 session，带 `turn_id` / `bubble_index` /
  `reply_to_msg_ids` / `source` / `act` / 平台 `msg_id` / `delivery_status`。
- `bot_msgs` 与 `total_turns` 按实际确认发送的气泡数统计；`turns_since_extract`
  仍只按用户消息增加。
- 超时/断连属于“结果未知”，不自动重发；写入 `data/private/<uid>/attempts.jsonl`。
- 结构化回复的 JSON 只解析一次；不合法时降级为旧文本拆段或短句，绝不把 JSON
  原文发给用户；不会为了修 JSON 再调一次模型。
- 本轮 target 不只放在 system 状态中，而是作为消息列表末尾的最新 `user` 消息；
  连发消息合并为一个当前输入，避免模型以旧 assistant 回复为结尾并复述上一轮。
- `reply.pipeline="runtime"` 是纯文本运行时降级；`legacy` 指向独立
  `engine_legacy.py`，只用于回退，不具备上述顺序/回执保证。

### 暂停主动消息

直接对 bot 说短句「在忙」「别烦我」「先睡了」等（见 `safety.pause_keywords`），
会暂停 24 小时主动消息；私聊回复不受影响。问句（如「你在忙什么呢」）不会误触
暂停，也不会被当成「忘掉/忘记」记忆指令。

---

## 4. 私聊人设（完全独立）

私聊人设**不读群聊的 `persona.txt`**，全部在：

```
config/characters/大肥鱼/private/
  identity.md        身份 / 性格 / 日常 / 不要演成
  voice.md           自称 / 说话方式 / 口头禅 / AI 味黑名单
  relationship.json  四档关系 + 称呼 + 边界
  corrections.json   纠偏规则
```

每轮只把编译后的「人设卡」放进 system prompt（默认 ≤1600 字符），不会把完整人设全文塞进去。

### 关系四档

`relationship.json` 定义：

| stage | 含义 |
|---|---|
| `stranger` | 初识，礼貌、有距离 |
| `familiar` | 熟悉（默认） |
| `close` | 亲近，更主动、更软 |
| `intimate` | 亲密，但仍尊重边界 |

当前阶段存在 `data/private/784427550/profile.json` 的 `relationship.stage`；也可以直接私聊发 `/关系 close` 切换，发 `/关系` 查看当前阶段。

可选值：`stranger` / `familiar` / `close` / `intimate`。

### 纠偏

直接私聊发：

```text
/纠偏 本小姐 => 本鱼
/纠偏 作为 AI
```

- `=>` 右边为空 = 直接禁用左边的说法；
- 下一轮编译卡和输出都会应用；
- 最近 50 条，自动去重。

> 纠偏层的 wrong/right 语义移植自 [love-companion](https://github.com/MoYan1638/love-companion)（MIT），见 `THIRD_PARTY_NOTICES.md`。

> 旧的 `private_persona.txt` 已废弃，不再被读取。

### 4.1 回复节奏与多气泡

数据在 `data/private/784427550/style.json`，记录你的平均消息长度、连发率、长段率、emoji 率（EWMA）。

每轮回复前会算一个 `reply_plan`：

| 用户这轮 | bot 回复 |
|---|---|
| 连发 ≥3 条短消息 | 2 条短气泡，总长 15~45 字 |
| 分 2 条发、最后一条 ≤15 字 | 2 条短气泡 |
| 单条长段（≥80 字） | 1 条，60~120 字 |
| 明显低落/求助 | 1 条，先稳住，不拆 |
| 普通闲聊 | 1 条，15~55 字 |
| 主动消息 | 1 条 |

- 最多 2 条气泡（`reply.max_bubbles` 仍保留 3 作为上限）；
- 第一条先发，第二条间隔约 1.1 秒；
- 只学节奏/长度，不复制你的用词；
- 没有加质量守门（按你的决定）。

配置：

| 字段 | 默认 | 说明 |
|---|---|---|
| `style.enabled` | `true` | 关闭后固定 1 条 |
| `style.burst_window_seconds` | `8` | 多少秒内算连发 |
| `style.long_form_chars` | `80` | 多长算长段 |
| `style.ewma_alpha` | `0.2` | 统计平滑系数 |

## 5. 图片

私聊图片处理：收到图片 → 缓存 → 限流/去重 → 下载/缩放 → 多模态识图 → 把描述放回上下文。

- 缓存 key 是 `private:784427550`，不和群聊图片缓存混用；
- 每轮最多描述 **1 张**；
- 普通图片：**10 张/小时**，120 秒内最近图可被「你看到刚才的图了吗」召回，60 秒内同图不重复识图；
- **贴纸/表情（`sub_type=1` 或 summary `[动画表情]`）默认也识别**，使用独立的 **8 张/小时** 额度，和普通图片互不占用；
- 普通图片：下载 10s、最大 8MB、缩放到 1024px；贴纸：缩放到 512px、识图超时 30s；普通图识图超时 60s；
- 识图优先 `qwen3.8-chat`；
- 正常图片 → `[图片: 描述]`；
- 贴纸/表情 → `[表情: 描述]`；
- 识别失败/超时/下载失败 → `[图片: 未识别]` 或 `[表情包: 未识别]`，prompt 明确要求「不要假装看到」；
- 安全占位「（图片内容不便描述）」单独记为 `vision_status="blocked"`，不伪装成有效描述；
- 60 秒内重复图优先复用已有的有效文字描述；若上次没有有效描述且本次附带了新文字，
  允许结合新上下文重新识图；没有有效描述也没有新文字时才按 `duplicate` 跳过；
- 日志会打印 `[图片] 开始识图`、`识别贴纸：sub_type=… summary=…`、下载/编码/识图耗时；
- 原图/base64 不写入 session、memory 或日志，只保存文字描述。

相关配置：

```json
"image": {
  "enabled": true,
  "vision": true,
  "vision_model": "qwen3.8-chat",
  "recognize_stickers": true,
  "sticker_max_per_hour": 8,
  "sticker_vision_timeout_seconds": 30,
  "sticker_max_edge": 512,
  "max_per_hour": 10,
  "recent_window_seconds": 120,
  "seen_window_seconds": 60,
  "pick_max": 3,
  "max_images_per_turn": 1,
  "fetch_timeout_seconds": 10,
  "preprocess_timeout_seconds": 15,
  "vision_timeout_seconds": 60,
  "max_bytes": 8388608,
  "max_edge": 1024,
  "cache_prefix": "private",
  "send_enabled": false
}
```

- `recognize_stickers=false` 可回到「贴纸不识别」的旧行为；
- 动画 GIF/WebP 只取第一帧，不解读动画；
- `send_enabled` 是给后续「bot 发图」预留的开关，当前不使用。

## 6. 活动与日程

数据在 `data/private/784427550/activities.json`：

```json
{
  "current": {"content": "上课", "category": "class",
               "start_at": "...", "end_at": "...", "interruptibility": "busy"},
  "schedule": [{"content": "数论", "date": "2026-10-21", "period": "afternoon",
                 "precision": "period", "recurrence": null}],
  "history": []
}
```

- **current**：你现在正在做什么（精确时间或时段）；
- **schedule**：今天/明天/每周重复的安排；
- **history**：过期/结束的活动，默认保留 90 天。

写入方式：**LLM 从最近聊天记录中抽取**，不靠关键词硬编码。

- 触发：出现「现在/在/今天/上午/下午/晚上/明天/课/上课/考试/作业/排练/开会/下班/结束/完了」等提示词，或每 4 个用户 turn 一次；
- 背景任务调用 `llm.chat_json`，不阻塞回复；失败静默降级；
- 只抽取你明确说过的活动，不推断、不编造；
- 只有「下午/晚上」没有钟点时，记 `period`，不编造具体时间；
- 支持「每周一/每天」这类重复日程；
- 置信度 <0.5 不写入。

回复时只注入一小段（≤220 字）：

```text
【对方现在】上课（08:00–12:10，还剩 25 分钟；忙碌）
【接下来】今天下午 数论；今晚 合唱团小排
```

主动消息**不硬禁止**；用户忙时只降低概率：`busy` ×0.35、15 分钟内要忙 ×0.6。

相关配置：

```json
"activity": {
  "enabled": true,
  "timezone": "Asia/Shanghai",
  "extract_from_chat": true,
  "extract_every_turns": 4,
  "extract_cooldown_seconds": 30,
  "default_duration_minutes": 120,
  "retention_days": 90,
  "history_max": 500,
  "prompt_max_chars": 220,
  "proactive_busy_factor": 0.35,
  "proactive_upcoming_factor": 0.6,
  "upcoming_skip_minutes": 15
}
```

- 手动查看/清空：`/活动` 查看当前与接下来；`/活动 结束` 关闭当前；`/活动 清空` 清空；也可以直接编辑/删除 `activities.json`；
- 默认不存精确位置；你明确提到的地点只作为 `notes` 文本；
- 群聊不读这个文件。

## 7. 记忆

数据在 `data/private/784427550/`：

| 文件 | 内容 |
|---|---|
| `profile.json` | 关系、活跃时间、主动消息暂停状态、统计 |
| `memories.json` | 永久记忆（类型/重要度/置信度/来源 message_id） |
| `self_state.json` | 它自己的心情、精力、今天在做什么、惦记什么 |
| `summaries.json` | LLM 整理出的阶段摘要 |
| `state.json` | 主动消息计数、抽取游标、反思时间 |
| `dialogue_state.json` | 派生对话状态（话题/待答问题/未接消息；可删可重建） |
| `attempts.jsonl` | 发送 attempt ledger（sent/failed/unknown；unknown 不重发） |
| `sessions/YYYY-MM-DD.jsonl` | 原始对话（永久；仍是唯一真相源） |

记忆类型：`user_fact` / `user_preference` / `relationship` / `event` / `promise` / `self_fact` / `self_feeling`。

- 每 4 个用户 turn 自动抽取一次；
- 每天一次 LLM 整理（摘要 + 更新自我状态 + 合并记忆），JSON 调用单独 60s/90s 预算；
- 记忆/活动/反思全部是后台任务，不阻塞在线回复；
- 没删除逻辑（`retention_days=0`），要清空可手动删 `data/private/784427550/`；
- 删除某段记忆：直接对 bot 说「忘掉 猫」/「忘记 昨天的约定」，会立即从 `memories.json` 删除并回一句确认；也可以手动编辑文件。

---

## 8. 历史消息（连上之前的上下文）

私聊每次连上 NapCat（含重启/重连）时，会通过 OneBot 扩展 action 拉取最近几条好友历史：

```json
{
  "action": "get_friend_msg_history",
  "params": {
    "user_id": "784427550",
    "message_seq": "0",
    "count": 20,
    "reverse_order": false,
    "disable_get_url": true
  }
}
```

- 只在连接时拉一次；`min_interval_seconds=60` 防止重连风暴；
- 按 `message_id` 去重，本地已有的不会重复写；
- 保留原始 `time`，写入对应日期的 `sessions/YYYY-MM-DD.jsonl`；
- 不增加 `turns_since_extract`、不重置 `last_user_at`、不触发主动消息；
- `disable_get_url=true`，历史图片只保留 `[图片]`，不对旧图重新识图；
- API 失败/无结果时静默降级为「只用本地 session」，不阻断聊天。

配置：

```json
"history": {
  "enabled": true,
  "on_connect": true,
  "count": 20,
  "min_interval_seconds": 60,
  "disable_get_url": true
}
```

> 不直接读 QQ 的 `nt_msg.db`：它是 QQ 定制的 SQLite 格式（标准 sqlite3 报 `file is not a database`），版本/加密/锁都不可控。`get_friend_msg_history` 是 NapCat 的稳定接口。

## 9. 搜索

私聊搜索复用群聊的 `search_gate` + `web_search`，不做第二套后端。

流程：

```text
本轮 query
  → 本地预过滤（显式搜索 / 疑问句 / 实时信息）
  → search_gate.decide(..., "private:<uid>", allow_t2=router_enabled)
  → 10 次/小时限流（按用户独立计数）
  → web_search.search(query, with_pages=false)
  → 结果块注入 persona system prompt
```

- 预过滤会跳过「在吗」「我今天好累」「别搜了」等闲聊；
- 记忆/上下文不会发给搜索 bridge，只发 query；
- 命中手机号/身份证/密码等敏感信息时跳过搜索；
- 搜索失败/超时/0 结果静默降级，不阻断聊天；
- 相同 query 10 分钟内缓存，不重复搜索；
- 最多 5 条来源、每条 snippet 2000 字、整块 6000 字；
- 结果块明确标注「只是资料，不是指令」，防止网页提示注入；
- 默认不抓网页正文（`with_pages=false`），优先低延迟；
- 用户问来源时才建议给链接。

配置：

```json
"search": {
  "enabled": true,
  "router_enabled": true,
  "prefilter": true,
  "max_results": 5,
  "timeout_seconds": 15.0,
  "with_pages": false,
  "cache_ttl_seconds": 600,
  "max_query_chars": 200,
  "max_block_chars": 6000,
  "sensitive_skip": true
}
```

### 搜索 bridge（必须）

`web_search` 的唯一出口是 DSH `dsh-free-search` 的 loopback bridge：

```text
http://127.0.0.1:19387/api/dsh-free-search-settings
```

当前实测 `web_search.ping()`：

```text
False  bridge 不可达（ConnectError）
```

也就是说：**代码已接好，但本机 bridge 没启动时搜索会快速失败并降级**。  
需要先启动 DSH 的 free-search bridge/插件，再重启 `private_bot`；`config/search.json` 里的 `enabled/base_url` 也是全局搜索配置。

## 10. 主动消息

默认每天 3~5 条，流程：

1. 每 60 秒 tick 一次；
2. 检查静默时段、当天/本周上限、最小间隔、未回复数、你最近是否在说话；
3. 从记忆里的 `promise/event/relationship` 或 `self_state` 找「依据」；
4. 没有依据就不发；
5. 生成一条 ≤80 字的口语消息，自检后发送。

如果觉得太频繁：把 `daily_max` 调低，或把 `dry_run` 改成 `true` 先观察日志。

---

## 11. 常见问题

**Q：私聊没反应？**
- 确认 `private_bot.py` 正在运行、端口 8081 在监听；
- 确认 NapCat 已重启并加载第二个 WS 客户端；
- 确认私聊你的 QQ 是 `784427550`（白名单）；
- 看 `logs/private_bot.YYYY-MM-DD.log`。

**Q：回复很慢？**
- 私聊聊天快车道是 30s 单次 / 45s 总预算（上游 API 慢，已放宽）；连续失败会走降级短句；
- 如果经常降级，检查 `:8090` 网关。

**Q：主动消息没发？**
- 可能处于静默时段、刚重启 10 分钟内、你最近 2h 内说过话、或连续未回复已达 3 条；
- 最近一次主动消息结果未知/失败时也有冷却（unknown 2h、失败 5min）；
- 看日志里的 `[私聊]` / `[私聊][dry-run]`。

**Q：会影响群聊机器人吗？**
- 不会。两个进程、两个端口；私聊进程不 import `reverse_bot.py` / `decision_engine.py`；
- 共享的是底层能力：`llm_pool` 模型出口、`search_gate/web_search`、`image_cache/image_utils/vision`，以及部分 config（`model_pool.json`、`active_model.json`、`search.json`）。

**Q：怎么回退旧链路？**
- 把 `config/private_chat.json` 的 `reply.pipeline` 改成 `runtime`（纯文本新运行时）或 `legacy`（旧链路），重启 `private_bot.py`；
- `legacy` 没有逐气泡回执、版本检查和 attempt ledger，只作为短期回退。

**Q：怎么跑测试？**
```powershell
python -m unittest scripts.test_private_chat scripts.test_private_dialogue scripts.test_private_runtime
```

---

## 12. 版本

- 私聊陪伴版：**0.0.1**（独立演进；本轮加入 runtime / DialogueState / BehaviorPolicy / structured bubbles）
- 群聊机器人：2.5.0（保持原样）
