# 决策日志 018 — 私聊运行时重构（阶段 1～2）

> 日期：2026-10-24
> 决策者：用户（明确要求：有序队列/会话版本/逐气泡回执/结构化气泡；structured 默认、runtime 与 legacy 可回退；legacy 保留一个版本）
> 状态：**代码完成；90 项离线单测通过；待真实 QQ 私聊验收**

## 一、做了什么

把普通回复从 `engine.py` 拆成一条可测试的运行时链路：

```text
private_bot.handler → PrivateChat.on_message（校验/去重/原始落库/入队）
  → ConversationRuntime（每用户有序队列 + revision + 防抖 + 取消 + 主动仲裁）
  → prepare_inbound（图片 enrichment ≤15s，update_turn 原地更新）
  → BehaviorPolicy（answer/support/chat/follow_up/wait）
  → ResponseComposer（一次 chat_raw 调用，结构化气泡）
  → TurnSender（控制 0 > 普通 1 > 主动 2；逐气泡 RPC/回执/落盘）
```

新增/重写文件：

| 文件 | 作用 |
|---|---|
| `scripts/private_chat/runtime.py` | `ConversationRuntime`、`NormalEvent`、`ControlEvent` |
| `scripts/private_chat/dialogue_state.py` | 可从 session 重建的派生对话状态 |
| `scripts/private_chat/behavior.py` | 纯规则 `BehaviorPolicy` |
| `scripts/private_chat/response.py` | 结构化气泡解析/校验/降级 |
| `scripts/private_chat/sender.py` | 旧低层发送 + `TurnSender` 优先级/回执引擎 |
| `scripts/private_chat/engine_legacy.py` | 旧回复链，仅 `reply.pipeline="legacy"` 使用 |
| `scripts/private_chat/store.py` | `add_turn` 元数据、`update_turn`、attempt ledger、历史去重排序 |
| `scripts/private_chat/rpc.py` | `ActionOutcome`：ok / failed / unknown |
| `scripts/private_chat/proactive.py` | 改为提交 runtime 发送；hook/文本最小去重、unknown 冷却 |
| `scripts/private_chat/llm.py` | 新增 `chat_raw()`；聊天/JSON 熔断保持独立 |
| `scripts/private_chat/persona.py` | 对话状态/行为/节奏块 + 结构化输出契约 |
| `scripts/private_chat/style.py` | `RhythmPlan` dataclass；移除语义 hint |

## 二、关键语义

1. **版本**：普通用户消息和 pause 请求 `revision += 1`；其他控制/历史/主动不增。新消息取消旧未发送气泡；已发出的 RPC 允许完成并落盘；发送前在 TurnSender 再查版本。
2. **顺序**：用户消息先落库；图片只写 `[图片]` 占位，识图后 `update_turn` 原地更新，不追加第二条用户消息；单条图片预处理 15s 上限，超时标未识别继续。
3. **逐气泡**：每个气泡单独 RPC、单独回执、单独写 session；assistant 记录平台 `msg_id`、`turn_id`、`bubble_index`、`reply_to_msg_ids`、`source`、`act`、`revision`、`delivery_status`。
4. **unknown**：不写“已发送”assistant turn、不自动重发、不增加主动计数；写 `attempts.jsonl`，unknown 主动消息冷却 2h；历史补拉按 `msg_id` / 稳定键只导入一次。
5. **pending**：第一个气泡成功即按 `target_msg_ids` 差集清除；旧版本迟到确认只清自己的 target，不覆盖新话题/问题/行为。
6. **主动消息**：普通回复活跃或输入队列非空时跳过本 tick；生成中被用户消息取消；cancel/failed/unknown 不计 `proactive_sent`/`unanswered`；最近 72h 用过的 hook 和相似文本不重复。
7. **结构化**：`reply.pipeline="structured"` 默认；一次 `chat_raw`（900 tokens，不传 response_format）；有效 JSON 不再按标点重拆；无效 JSON 不把原文发用户；不二次修 JSON；纠偏逐气泡；单气泡 ≤160 字、整轮 ≤`max_reply_chars`。
8. **回退**：`runtime` 为纯文本运行时降级；`legacy` 指向 `engine_legacy.py`，不具备新链路的顺序/回执/逐气泡保证；本轮不删除 legacy。

## 三、数据兼容

- 旧 session 记录缺新字段时默认空；不批量重写。
- `data/private/<uid>/dialogue_state.json` 是派生缓存，缺失/损坏可重建；启动清空 pending，不自动补答。
- `data/private/<uid>/attempts.jsonl` 是发送 attempt ledger。
- 统计口径：`bot_msgs`/`total_turns` 按实际确认发送的气泡数；`turns_since_extract` 仍只按用户消息；旧统计不重算。
- 活动抽取兼容 `op`/`type`/`action` 三种字段，并支持中文 period；暂停词只匹配短句，避免“你在忙什么”误触。

## 四、验证

| 项 | 结果 |
|---|---|
| `import private_bot` / `import reverse_bot` | OK |
| `python -m unittest scripts.test_private_chat scripts.test_private_dialogue scripts.test_private_runtime` | **90/90 OK** |
| 图片先于文本落库、识图后原地更新 | 单测覆盖 |
| 气泡间隔插话取消旧第二气泡 | 单测覆盖 |
| 第二个 RPC 已发出后插话允许完成 | 单测覆盖 |
| 模型生成中插话丢弃旧结果 | 单测覆盖 |
| 主动生成被用户消息取消 / RPC 在途完成一次 | 单测覆盖 |
| 部分成功、unknown 不落盘、控制优先 | 单测覆盖 |
| 结构化有效/纯文本降级/残缺 JSON 不发送 | 单测覆盖 |
| 历史 msg_id 去重、无 ID 稳定键、时间排序 | 单测覆盖 |

未验证：真实 QQ 私聊下的新链路端到端（需重启 `private_bot.py`）；真实模型结构化输出长期质量；attack ledger 的精确 reconciliation（阶段 2 可选）。

## 五、回退

1. `config/private_chat.json` → `"reply.pipeline": "runtime"` 或 `"legacy"`，重启 `private_bot.py`；
2. 不需要改群聊；
3. `data/private/` 可保留；`dialogue_state.json`/`attempts.jsonl` 可删除后自动重建。
