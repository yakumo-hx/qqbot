# 群聊决策架构全白名单群灰度方案

- 状态：已准备，等待用户重启 `reverse_bot.py` 后执行。
- 更新日期：2026-11-04（Asia/Shanghai）。
- 发布负责人及重启执行人：用户。
- 重点观察群：`1102194840`。
- 代码基线与哈希：[manifest.json](manifest.json)。
- 工程验收：[READINESS_REPORT.md](READINESS_REPORT.md)。
- 回滚手册：[ROLLBACK.md](ROLLBACK.md)。

> 本文件只完成发布准备。当前尚未重启、尚未部署新代码、尚未进行真实 QQ 验证。灰度开始后只观察自然消息，不人为发送测试消息，不通过调参凑指标。

## 1. 已确认的灰度范围

本次不是单群代码隔离，而是三个已配置白名单群共同灰度：

| 群号 | 当前 `decision_engine` | 观察定位 |
|---|---:|---|
| `1102194840` | `true` | 重点观察群 |
| `284517232` | `true` | 共同灰度群 |
| `1055881417` | `true` | 共同灰度群 |

- 三个群都使用同一份新代码，均会启用结构化台账、@ 回复独立限流和决策回复 echo 回执。
- 生产 `config/group_features.json` 不需要为本次灰度修改；准备阶段保持原样。
- 未配置的群仍按白名单语义全部关闭，不在灰度范围内。
- 统计必须同时提供逐群结果和三群汇总，不能只报告重点群或只报告总计。

## 2. 本次验证的行为边界

### 2.1 三层结果不得混用

| 层次 | 字段 | 含义 |
|---|---|---|
| 政策意图 | `policy_intent=true` | 规则希望发言，不代表生成或发送成功 |
| 生成结果 | `reached_generation` / `generation_ok` | 是否进入生成、是否得到可用文字 |
| 平台回执 | `send_status=ok` | OneBot 明确返回 `status=ok` 且 `retcode=0` |

- `send_status` 取值为 `ok / failed / unknown / not_attempted`。
- `unknown` 表示超时、断连、写入异常或结果无法确定，不等于失败，也不等于成功。
- `unknown` 和 `failed` 均不自动重发。
- 记录级状态按 `unknown > failed > ok` 保守聚合；逐次文字/图片结果看 `send_attempts[]`。
- 表情 strong 只发图可以 `reached_generation=false`，但仍可能产生图片发送，属于合法组合。

### 2.2 所有灰度群共同生效的新安全行为

- @bot/别名回复：同一 `(group_id, user_id)` 最小准入间隔 5 秒，滚动 60 秒最多准入 6 次。
- @ 限流在表情检索和模型生成之前执行；超限记 `at_rate_limited`，不发送。
- @bot 仍绕过 `proactive_max_per_hour`，但不再绕过自己的独立安全限流。
- 指令在决策引擎之前消费，不受该 @ 回复限流影响。
- 决策回复文字和图片等待真实 echo，默认每次最多 10 秒；文字和图片串行时最坏等待 20 秒。
- 指令、`+1`、新人欢迎、神秘数字等 legacy 发送路径没有新增回执，不纳入决策台账发送统计。

### 2.3 台账

- 路径：`E:\QQbot\logs\decision_trace.jsonl`。
- 格式：每个决策一行 JSON，`schema_version=decision_trace.v1`。
- `trace_id` 优先使用 `message_id`，缺失时使用随机 UUID；跨群关联必须同时看 `(group_id, trace_id)`。
- 不记录消息正文、base64、完整发送 payload 或本地图片路径。
- 重复 `message_id` 在台账创建前抑制，因此重复投递不会形成第二条决策记录。
- 台账写失败只影响可观测性，不改变决策行为；若出现 `[决策台账] 写入失败`，先修台账再解释结果。

## 3. 用户重启前的最终核对

1. 代码文件与 `manifest.json.current_after_task` 一致；生产配置与备份的 JSON 内容一致（换行字节允许不同）。
2. 已保存配置备份：
   `rollback_backups/group_features.pre_all_group_canary_20261104_212703.json`。
3. 三个灰度群的 `decision_engine` 均为 `true`，其他功能位不变。
4. 离线验收结果仍为：
   - `scripts.test_proactive_limit + scripts.test_decision_release`：58/58 通过；
   - `scripts.test_reference`：49/49 通过；
   - `reverse_bot`、`private_bot` 导入检查通过。
5. 未设置 `DECISION_TRACE_DISABLED=1`；生产默认台账路径未被改到实验目录。
6. 已阅读 [ROLLBACK.md](ROLLBACK.md)，能在不重启的情况下按群关闭 `decision_engine`。

任何代码或配置哈希不符，都先停止发布并查明差异，不带病重启。

## 4. 重启与首轮观察

1. 用户按现有运维方式只重启群机器人 `reverse_bot.py`。
2. 不重启私聊，不切换 NapCat/LLBot，不修改任何模型或角色配置。
3. 确认新进程正常监听 `:8080`，NapCat 已连接，启动日志无导入错误。
4. 等待群内自然消息进入决策；不人为发消息“试一下”。
5. 首条自然决策后检查台账：
   - 文件存在且可继续追加；
   - JSON 可解析，`schema_version=decision_trace.v1`；
   - `group_id` 属于三个灰度群之一；
   - `rules_version/config_sha256` 与发布基线一致；
   - `policy_intent`、生成字段和发送字段语义合理；
   - `instrumentation_gap=false`。
6. 首次真实发送后重点检查 `send_status` 和 `send_attempts[]`：
   - `ok`：真实 echo 链已打通；
   - `failed`：保存 retcode/error_kind，按停止条件处理；
   - `unknown`：不补发，检查是否等待满 10 秒以及 NapCat 是否返回 echo。
7. 重点群 `1102194840` 出现首条记录后单独核对一次，但其他两群异常同样纳入停止判断。

## 5. 灰度期间冻结

观察期内不得修改：

- `config/decision_rules.json`，包括发言阈值、冷却、proactive 上限、@ 限额和回执超时；
- `config/group_features.json`（紧急回滚除外）；
- `config/active_model.json`、`config/model_pool.json`；
- 当前角色、人设、记忆和表情包参数；
- `reverse_bot.py`、`scripts/*.py`；
- 进程数量和协议端拓扑。

不得为了提高到 100 条而调低阈值、增加主动发言、反复重启刷新状态或人为触发回复。离线实验的 2.8896% 只是历史观察上的生成前政策意图，不是本次线上发送率目标。

## 6. 统计与质量复核

### 6.1 统计维度

先按 `group_id` 分群，再计算三群汇总；每群内部再按 `is_at_bot=false/true` 分开：

- 决策记录数、`policy_intent` 数；
- `reached_generation`、`generation_ok`、只发图数；
- `ok/failed/unknown/not_attempted`；
- 文字和图片 `send_attempts` 成功数；
- 首个阻断 stage、`at_rate_limited`、分类器异常；
- 内容过滤、精确/相似去重；
- `decision_latency_ms` 和 `latency_ms` 的中位数、p95；
- `instrumentation_gap` 数。

重点群 `1102194840` 必须单列。其他两群样本不足时标为“样本不足”，不能用重点群结果替代。

### 6.2 首个质量复核节点

- 三群合计约 100 条记录级 `send_status=ok` 的决策回复时，进行首次质量复核。
- 同时报告决策级 ok 数、文字 ok 条目数、图片 ok 条目数，不能混为一个数。
- 每个有实际发送的群都要抽取样本；重点群单独抽样。
- 人工检查：上下文相关性、回复时机、重复、角色一致性、安全性、@ 限流体验和群内负面反馈。
- 报告只引用 `trace_id`、时间和群内可见现象，不把聊天正文导出到仓库。

## 7. 立即停止条件

| 情形 | 处置 |
|---|---|
| 跨群发送、错误对象发送 | 立即关闭受影响群；无法定位时三群全部关闭 |
| 机器人自我循环或与其他 bot 循环 | 立即关闭受影响群；持续时三群全部关闭 |
| 严重隐私、密钥、路径、base64 或内容安全问题 | 立即三群停止并保存证据 |
| 连续 3 条有发送尝试的决策均为 `unknown`，或 unknown 占有发送尝试决策比例 ≥5% | 停止受影响群；全局 echo 异常时三群停止；不得补发 |
| 普通接话突破既有 proactive 上限，或 @ 回复突破 5 秒/6 次每 60 秒限制 | 立即三群停止并检查代码路径 |
| 台账持续不写、`instrumentation_gap` 非 0 或统计无法解释 | 暂停灰度结论；无法快速恢复时停止灰度 |
| 模型调用、发送频率或进程行为失控 | 立即三群关闭，必要时评估代码回滚 |
| 任何未批准的代码、配置、模型或角色漂移 | 停止灰度，保存现场并恢复基线 |

局部体验异常只回滚对应群；全局性代码、回执或台账异常按三群共同回滚处理。具体步骤见 [ROLLBACK.md](ROLLBACK.md)。

## 8. 只读观察入口

- 主台账：`E:\QQbot\logs\decision_trace.jsonl`。
- 辅助日志：`E:\QQbot\logs\bot.YYYY-MM-DD.log`。
- 只按 `schema_version/group_id/ts_epoch` 过滤和统计，不修改原文件。
- 回滚或复核前复制现场到 `rollback_backups/scene_<时间戳>/`，不删除、不清空原日志。
- 不用真实 QQ 发送验证灰度、回滚或恢复；只使用自然消息、群内可见行为、日志和台账。

## 9. 当前待执行状态

- 发布材料：已完成。
- 生产配置备份：已完成。
- 生产群开关：保持三群 `decision_engine=true`。
- 新代码部署：工作树已准备，等待用户重启生效。
- 真实 NapCat echo：尚未验证，是重启后的首要技术观察项。
- 真实 QQ 测试：尚未开始，由用户重启后通过自然消息展开。
