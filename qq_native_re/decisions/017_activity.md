# 决策日志 017 — 当前活动 / 日程存储（LLM 从聊天记录抽取）

> 日期：2026-10-21
> 决策者：用户（明确：只服务 784427550；要求 LLM 从聊天记录提取；支持每周重复；主动消息不硬禁止、只降频；地点只进 notes；历史 90 天）
> 状态：**代码完成，42 项离线单测通过；待真实聊天验收**

## 一、做了什么

新增 `scripts/private_chat/activity.py` 与：

```text
data/private/784427550/activities.json
```

结构：

- `current`：现在正在做什么；
- `schedule`：今天/明天/每周重复安排；
- `history`：过期/结束活动（90 天）。

时间精度分三档：

- `exact`：有 `start_at/end_at`；
- `period`：只知道上午/下午/晚上；
- `unknown`：只知道今天有这件事。

## 二、关键决策

1. **只服务 784427550**：活动文件按 uid 隔离，只有白名单用户会触发抽取。
2. **LLM 从聊天记录抽取**：`engine._extract_activity` 取最近 12 个 turn，交给 `llm.chat_json`，输出严格 JSON `{"operations":[...]}`；不靠关键词硬编码。
3. **支持重复日程**：`recurrence={"type":"weekly","weekdays":[1..7],"until":...}`；按天展开 occurrence。
4. **主动消息不硬禁止**：用户忙时只降概率，`busy ×0.35`、15 分钟内要忙 `×0.6`；不改变原 quiet hours/频率上限。
5. **地点只进 notes**：不建独立位置字段。
6. **90 天历史**：`history` 超期自动清理，`history_max=500`。
7. **失败静默**：抽取/读写/时区异常都不影响聊天；时区缺失 tzdata 时固定 +08:00 兜底。

## 三、抽取操作

```json
{"operations": [
  {"op": "set_current", "content": "上课", "category": "class",
   "start_at": "2026-10-21T08:00:00+08:00",
   "end_at": "2026-10-21T12:10:00+08:00",
   "precision": "exact", "confidence": 0.95},
  {"op": "add_schedule", "content": "数论", "category": "class",
   "date": "2026-10-21", "period": "afternoon",
   "precision": "period", "confidence": 0.8},
  {"op": "add_schedule", "content": "早课", "category": "class",
   "start_at": "2026-10-19T08:00:00+08:00",
   "end_at": "2026-10-19T10:00:00+08:00",
   "recurrence": {"type": "weekly", "weekdays": [1], "until": "2026-12-31"}},
  {"op": "close_current", "reason": "下课了"},
  {"op": "cancel_schedule", "content": "合唱团"}
]}
```

- 置信度 <0.5 丢弃；
- content ≤40 字；
- 没有明确钟点不允许编造 `start_at/end_at`；
- 只抽取用户明确说过的内容。

## 四、运行时

- 用户消息命中活动提示词，或每 4 个用户 turn → 后台 task 抽取（30s 冷却），不阻塞回复；
- 每次回复前 `activity.format_prompt()` 注入 ≤220 字：
  ```text
  【对方现在】上课（08:00–12:10，还剩 25 分钟；忙碌）
  【接下来】今天下午 数论；今晚 合唱团小排
  ```
- 主动消息生成时也带当前状态；
- 主动消息 tick 按 `proactive_factor()` 降频，不拦截。

## 五、验证

| 项 | 结果 |
|---|---|
| `import private_bot` | OK |
| `python -m unittest scripts.test_private_chat -v` | **42/42 OK** |
| 当前活动渲染 | 通过 |
| 时段日程（下午/晚上） | 通过 |
| 每周重复 | 通过 |
| 过期/归档 | 通过 |
| 主动消息降频因子 | 通过 |
| prompt 注入 | 通过 |
| LLM 抽取接线（fake JSON） | 通过 |

未验证：
- 真实 LLM 抽取质量（尤其「到十二点十分」「每周一」这类表达）；
- 真实 `activities.json` 在长时间聊天中的漂移；
- 用户对「对方现在」注入频率的体感。

## 六、配置

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

## 七、回退

- 功能级：`config/private_chat.json` 设 `"activity": {"enabled": false}`，重启；
- 数据级：删除 `data/private/784427550/activities.json`；
- 不影响群聊、不影响长期记忆。

## 八、遗留

1. 真实聊天观察 3 天，重点看 LLM 是否把「今天上午一直上课到十二点十分」抽成正确 current；
2. `/活动` 已实现（查看当前/接下来、`/活动 结束`、`/活动 清空`）；手动“设置某条活动”仍以 LLM 抽取为主；
3. 重复日程的 `until` 与跨周展开需要真实数据校验；
4. 活动与长期记忆的边界：重要承诺仍归 memory，活动只做短期状态。
