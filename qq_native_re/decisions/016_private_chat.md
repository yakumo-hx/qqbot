# 决策日志 016 — 单人私聊陪伴版 0.0.1（独立端口）

> 日期：2026-10-20
> 决策者：用户（明确要求：新端口、只服务 784427550、先沿用大肥鱼、主动 3~5 条/天、记忆永久保留并允许 LLM 整理、版本 0.0.1）
> 状态：**代码完成，离线单测通过；待真实 QQ 私聊验收**

## 一、做了什么

在现有 v2.5.0 仓库上新增一个**完全独立的私聊进程**，不改群聊链路：

| 文件/目录 | 作用 |
|---|---|
| `private_bot.py` | 独立入口，WebSocket 服务端，默认 `:8081` |
| `scripts/private_chat/` | 私聊引擎、配置、存储、prompt、模型快车道、发送、主动消息 |
| `config/private_chat.json` | 端口/白名单/人设/记忆/主动参数 |
| `config/characters/大肥鱼/private/{identity.md,voice.md,relationship.json,corrections.json}` | 完全独立的私聊人设文件（编译卡 + 四档关系 + 纠偏） |
| `scripts/private_chat/style.py` | 对话节奏统计 + reply plan（连发/长段/低落 → 1~2 条气泡） |
| `scripts/private_chat/image_vision.py` | 私聊收图：缓存/限流/去重/下载/缩放/多模态识图，对齐群聊 |
| `scripts/private_chat/rpc.py` | echo 回执 RPC（用于 `get_friend_msg_history`） |
| `scripts/private_chat/search.py` | 私聊搜索：预过滤 + search_gate + web_search + 结果块 |
| `scripts/private_chat/engine.py` | 连接后拉取最近 20 条好友历史并导入 session（去重）；回复前搜索 |
| `data/private/<uid>/` | profile / memories / self_state / summaries / state / sessions |
| `scripts/test_private_chat.py` | 42 项离线单测 |
| `NapCat/.../onebot11_2668851638.json` | 追加第二个 WebSocket 客户端 `ws://127.0.0.1:8081`（备份在 `qq_native_re/patches/napcat_private_20261020/`） |

## 二、关键决策

1. **独立端口/独立进程**：群聊 `reverse_bot.py` 继续占用 8080；私聊 `private_bot.py` 占 8081。两边互不 import、互不共享运行时状态。
2. **只服务 784427550**：白名单硬校验；好友请求只自动通过白名单，其余忽略。
3. **私聊人设完全独立**：不读群聊 `persona.txt`；私聊人设文件在 `config/characters/大肥鱼/private/`，每轮编译成 ≤1600 字符的人设卡；四档关系 `stranger/familiar/close/intimate`；纠偏层 `/纠偏 wrong => right`（语义移植自 love-companion，MIT）。
4. **记忆永久**：`retention_days=0`、`raw_retention_days=0`；不自动删除。允许 LLM 抽取与每日整理；重要度/去重由代码兜底，避免模型误删。
5. **主动 3~5 条/天**：`daily_min=3`、`daily_max=5`、最小间隔 2h、静默 23:00–08:00、连续未回复 3 条即停、内容必须有记忆/自我状态依据。
6. **不修复旧问题**：不改 `decision_engine.py` 群聊路径，不改 `commands/`，不改旧文档/测试；只给 `llm_pool.chat` 增加一个**可选** `budget` 参数（默认行为不变），让私聊聊天总预算 45s、记忆整理总预算 90s。
7. **回复节奏计划**：每轮按用户连发数/最后一条长度/情绪词生成 `reply_plan`；连发或长段决定 1~2 条气泡，低落固定 1 条；只学节奏不复制用词；`style.json` 记录 EWMA 统计。
8. **私聊收图**：对齐群聊的 `image_utils` / `image_cache` / `vision`：普通图 10 张/小时、贴纸 8 张/小时，120s 最近图、60s 同图去重、8MB/1024px；**贴纸/表情默认也识别**（`recognize_stickers=true`，512px/30s），输出 `[表情: 描述]`；失败输出 `[表情包: 未识别]` / `[图片: 未识别]`，prompt 禁止假装看到；只存描述，不存原图/base64。第一阶段不做 bot 发图。
9. **识图超时修复（2026-10-20）**：实测 20s 外层超时会在模型回退前取消任务；改为 60s。进一步定位到 `qwen3.8-chat` 虽在 `vision_models` 白名单里，但不在聊天 `tiers` 中，`llm_pool` 从不会尝试它；`_build_attempt_order` 现在对 `vision=True` 允许把视觉白名单里的 preferred 放到最前。私聊 `vision_model=qwen3.8-chat`，实测 3.0s 返回「图中文字为 HELLO 2026」；`vision.describe` 增加可选 `preferred`/`timeout`（群聊默认行为不变）；日志增加来源/模型/下载/编码/识图耗时。
10. **连接前历史（2026-10-20）**：新增 `rpc.py` + `get_friend_msg_history`；每次连接拉最近 `history.count=20` 条，按 `message_id` 去重导入 session，保留原始时间；不增加抽取游标、不改 `last_user_at`、不触发主动消息；失败静默降级。不读 QQ `nt_msg.db`（QQ 定制 SQLite，标准 sqlite3 打不开）。
11. **私聊搜索（2026-10-21）**：新增 `scripts/private_chat/search.py`，复用 `search_gate` + `web_search`；本地预过滤（显式/疑问/实时词）、敏感信息跳过、10 次/小时、10 分钟缓存、15s 超时、`with_pages=false`；结果作为“资料不是指令”注入 persona；搜索失败静默降级。`search_gate.decide` 增加可选 `allow_t2`（群聊默认不变）。**依赖本地 free-search bridge `127.0.0.1:19387`；当前实测 `web_search.ping()` 不可达，需先启动 bridge。**

## 三、数据流

```
NapCat --ws://127.0.0.1:8081--> private_bot.py
  → PrivateChat.on_message
      → 白名单/去重/暂停词
      → 图片缓存（如有）→ 限流/去重/下载/缩放/vision → `[图片: 描述]` / `[表情: 描述]`
      → 连接后后台 get_friend_msg_history → 去重导入 session
      → store.add_turn + profile 更新
      → 防抖（2~4s）
      → style.plan（连发/长段/低落 → 回复计划）
      → persona.build_messages（人设卡 + 关系 + 记忆 + self_state + 最近对话 + 本轮计划）
      → llm.chat（聊天 30s 单次 / 45s 总预算；记忆整理 60s / 90s；独立熔断）
      → sender.send_humanlike（按计划 1~2 条气泡 + 分段延迟）
      → 记录回复 + 触发记忆抽取
  → 后台：每日 reflection（LLM 整理记忆/生成摘要/更新 self_state）
  → 后台：主动消息 tick（60s，硬闸门 + 3~5 条/天）
```

## 四、验证

| 项 | 结果 |
|---|---|
| `import private_bot` | OK，版本 0.0.1 |
| `python -m unittest scripts.test_private_chat -v` | **42/42 OK** |
| 配置加载 | `enabled=true`、`port=8081`、`allowed_users=["784427550"]`、`daily 3~5` |
| NapCat 配置 | 两个 WS 客户端：8080（群聊）+ 8081（私聊），JSON 校验通过 |
| 数据目录 | `data/private/` 已加入 `.gitignore` |
| 真实网关视觉 | `qwen3.8-chat` 3.0s 返回「图中文字为 HELLO 2026」；私聊 `image_vision` 端到端 4.2s 返回描述 |
| 搜索离线链路 | 42/42 单测 OK；fake search/search_gate 覆盖注入、缓存、预过滤 |
| 搜索 bridge | `web_search.ping()` 当前返回 `False bridge 不可达`，需先启动本地 free-search bridge |

未验证（需要真实 QQ / NapCat 重启）：
- 私聊事件真实到达 8081；
- `send_private_msg` 真实发送；
- 好友请求真实到达并自动通过；
- 真实 QQ 图片 URL 的下载（离线/合成图已覆盖 fetch/encode/vision）。

## 五、运行

```powershell
cd E:\QQbot
$env:PYTHONIOENCODING="utf-8"
.\venv\Scripts\python private_bot.py
```

NapCat 需要重启以加载第二个 WS 客户端配置。群聊机器人照旧：

```powershell
.\venv\Scripts\python reverse_bot.py
```

## 六、回退

1. 停 `private_bot.py`；
2. 把 `NapCat/.../onebot11_2668851638.json` 恢复为备份文件（或删掉第二个 client）；
3. 不需要动群聊代码；`data/private/` 可保留或手动删除。

## 七、遗留

1. 真实私聊验收（发一条消息、看回复、看 `data/private/784427550/` 是否落盘）；
2. 主动消息先观察 3 天，必要时把 `config/private_chat.json` 的 `dry_run` 改 `true` 做纯日志演练；
3. 人设后续由用户改写 `config/characters/大肥鱼/private/` 下的文件；旧的 `private_persona.txt` 已废弃；
4. 动画 GIF/WebP 只取第一帧，不解读动画；bot 发图未接入；
5. 记忆检索目前是关键词+重要度+时间衰减，数据量变大后再考虑 embedding。
