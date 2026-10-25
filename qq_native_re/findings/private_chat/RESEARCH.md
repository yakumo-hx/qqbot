# 单人私聊陪伴版 · 调研记录（0.0.1）

- 日期：2026-10-20
- 目的：为「QQ 私聊 + 像人一样聊天 + 长期记忆 + 主动消息」寻找成熟参考，确定 0.0.1 的取舍。
- 结论：**没有可以直接替换本项目的成熟方案；但 MaiBot / Mem0 / Generative Agents 的三套设计值得借鉴，且都不需要引入重依赖。**

## 1. 参考项目与文档

| 项目 | 规模/状态 | 与本需求相关的能力 | 链接 |
|---|---|---|---|
| MaiBot（麦麦） | GitHub 6.3k★，活跃 | 数字生命体定位；长期记忆；人物画像；聊天摘要；Episode；记忆衰减；记忆包；图片记忆；私聊/群聊记忆隔离 | [GitHub](https://github.com/Mai-with-u/MaiBot) · [记忆管理](https://docs.mai-mai.org/manual/webui/memory-management) · [A_Memorix 配置](https://docs.mai-mai.org/manual/configuration/amemorix-config) · [Bot 配置](https://docs.mai-mai.org/manual/configuration/bot-config) |
| AstrBot | GitHub 42k★，成熟 | 多平台 IM、插件、人格设置、知识库、上下文压缩、Agent/MCP | [GitHub](https://github.com/AstrBotDevs/AstrBot) · [文档](https://docs.astrbot.app/) |
| AI-BOT-Life | 新项目（0★） | 本地陪伴框架：QQ + GAL 双端、生活模拟心跳、主动发起、日历约定、情绪/关系状态、日复盘与事实提取 | [GitHub](https://github.com/xgx042375/AI-BOT-Life) |
| Character-AI-QQ-Bot | alpha | 隐私优先、长期记忆、关系事件、离线游标、默认关闭主动/联网 | [GitHub](https://github.com/ray153860/Character-AI-QQ-Bot) |
| Mem0 | 成熟记忆框架 | 抽取（add）与检索（search）两阶段；存事实而非回放全文；去重/实体关联 | [How Mem0 works](https://docs.mem0.ai/core-concepts/how-it-works) |
| Generative Agents | 论文（2023） | 记忆流 → 反思 → 规划；按 recency/importance/relevance 检索；主动行为 | [arXiv:2304.03442](https://arxiv.org/abs/2304.03442) |
| MaiMBot-LPMM | 49★ | HippoRAG 2 图记忆、遗忘/再激活机制（多数待实现） | [GitHub](https://github.com/Mai-with-u/MaiMBot-LPMM) |
| OneBot 11 | 标准 | `send_private_msg`、`set_friend_add_request`、`get_friend_list`，私聊事件 `message_type=private` | [公开 API](https://github.com/botuniverse/onebot-11/blob/master/api/public.md) |

## 2. 关键结论

### 2.1 记忆：按聊天流隔离是底线

MaiBot 的 A_Memorix 默认「每个群、每个私聊只能检索自己产生的记忆」，跨聊天流共享需要显式打开；群→私、私→群的启发式召回默认也是关闭的。  
⇒ 本版把私聊记忆放在 `data/private/<uid>/`，**绝不注入群聊记忆**，也不把私聊内容写进群聊 prompt。

### 2.2 记忆：存事实，不存全文

Mem0 的流程是「对话 → 抽取事实 → 去重/关联 → 检索 top-k → 放进 prompt」，而不是把历史记录全部重放。  
⇒ 本版每 4 个用户 turn 抽一次候选记忆，code 侧校验 `type/text/source_ids/confidence`，检索时只取 top-k（默认 10 条）。

### 2.3 像人：记忆 + 反思 + 主动，而不是 prompt 花活

Generative Agents 的 observation → memory stream → reflection → planning 结构，以及 MaiBot「more lifelike, not merely better」的定位，说明「像人」主要来自：
- 有连续的记忆（能提起过去）；
- 有自我状态（会累、会想起事情）；
- 会主动开口（不是永远等输入）；
- 有节奏（不秒回、会分段、会短）。

⇒ 本版实现 `self_state.json`（mood/energy/today/looking_forward_to/recent_thoughts）、每日 LLM 整理、主动消息引擎。

### 2.4 主动消息：先限制，再拟人

AI-BOT-Life 的生活心跳/主动发起，以及 Character-AI-QQ-Bot 默认关闭主动，说明主动消息必须有硬闸门。  
⇒ 本版默认 3~5 条/天、静默时段 23:00–08:00、最小间隔 2h、连续未回复 2 条即停、内容必须有记忆或自我状态依据。

### 2.5 不需要的东西

- 不需要向量数据库：单用户、几千条记忆以内，关键词 + 重要度 + 时间衰减足够；
- 不需要 HippoRAG 图检索：先跑出真实使用数据，再考虑升级；
- 不需要 WebUI：0.0.1 用 JSON 文件 + 日志，先验证「像不像人」。

## 3. 本版取舍

| 参考 | 采纳 | 暂不采纳 |
|---|---|---|
| MaiBot | 聊天流隔离、人物事实抽取、聊天摘要、LLM 整理、静默/主动开关 | 100+ 配置、向量库、图记忆、图片记忆 |
| Mem0 | 事实抽取 + 检索 top-k、置信度/来源 | 外部服务、实体链接 |
| Generative Agents | 记忆流、反思、主动规划、recency/importance/relevance | 完整生活模拟、多人社会 |
| AI-BOT-Life | 生活心跳、主动发起、日历约定、日复盘 | GAL 前端、内容包体系 |
| Character-AI-QQ-Bot | 隐私默认、白名单、可暂停 | 其框架实现 |
| AstrBot | 人格配置、上下文压缩思路 | 插件/多平台架构 |

## 4. 0.0.1 的实现映射

- 私聊消息 → `scripts/private_chat/engine.py`；
- 记忆 → `scripts/private_chat/store.py`（JSON/JSONL，永久保留）；
- 抽取/整理 → `engine._extract_memories` / `_reflect`（调用 `llm_pool`）；
- 主动 → `scripts/private_chat/proactive.py`（3~5 条/天）；
- 拟人化 → `scripts/private_chat/sender.py` + `persona.py`；
- 收图识图 → `scripts/private_chat/image_vision.py`（复用群聊 `image_utils` / `image_cache` / `vision`）；
- 当前活动/日程 → `scripts/private_chat/activity.py`（LLM 从聊天记录抽取 current/schedule；90 天历史；忙时只降主动消息概率）；
- 独立入口 → `private_bot.py`（端口 8081）；
- 单测 → `scripts/test_private_chat.py`（42 项）。

## 5. 参考边界说明

以上资料通过 GitHub API / 项目文档 / 论文摘要获取；它们是**设计参考**，不是本项目的运行依赖。本版没有复制任何第三方代码。

## 6. 人设处理补充调研（2026-10-20）

用户要求重点看两套人设方案，结论已落到 0.0.1：

- [aemeath-skill](https://github.com/NoMeaning13/aemeath-skill)
  - 借鉴：SKILL.md 路由、references 按职责拆分、按需加载、runtime profile、运行时变量、关系阶段枚举、评测/反例/测试集。
  - 未采用：强硬「永不出戏/拒绝身份提问」；无 license，不复制文本。
- [love-companion](https://github.com/MoYan1638/love-companion)
  - 借鉴：人设单一真相源、结构化字段、`lover_card()` 的 must/nice 两级编译、纠偏层（wrong/right、right 为空即禁用、最近 50 条）、离线/在线分层。
  - 未采用：500 Token 硬上限、8 套恋爱预设、趋势/多模态/依恋引擎。
- 本版落地：
  - 私聊人设**完全独立**：`config/characters/大肥鱼/private/{identity.md,voice.md,relationship.json,corrections.json}`，不读群聊 `persona.txt`；
  - 每轮编译 ≤1600 字符人设卡；
  - 四档关系 `stranger/familiar/close/intimate`；
  - `/纠偏 wrong => right`，语义移植自 love-companion（MIT），见 `THIRD_PARTY_NOTICES.md`。

