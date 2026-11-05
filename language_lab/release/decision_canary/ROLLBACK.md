# 全白名单群灰度回滚手册

- 适用灰度群：`1102194840`、`284517232`、`1055881417`。
- 重点观察群：`1102194840`。
- 状态：操作手册，当前未执行任何回滚。
- 配置备份：`rollback_backups/group_features.pre_all_group_canary_20261104_212703.json`。

## 1. 三条原则

1. 先止损、后定位：配置回滚优先于代码回滚。
2. 先保存现场、再修改：保留配置、台账、日志和哈希，不删除原证据。
3. 不用真实 QQ 发送验证回滚：只通过自然消息、群内可见行为、日志和台账确认。

## 2. 回滚范围选择

| 情形 | 回滚范围 |
|---|---|
| 仅某群体验异常、接错话、刷屏或群友投诉 | 只关闭该群 `decision_engine` |
| 多群同时异常、持续 unknown、跨群发送、机器人循环、发送失控 | 三群全部关闭 |
| 无法判断局部还是全局 | 先关闭异常群；风险仍扩散时立即三群关闭 |
| 怀疑台账、回执或新代码本身有缺陷 | 先三群配置止血，再评估代码回滚 |

关闭 `decision_engine` 会停止该群普通决策回复和 @ 决策回复；指令、`+1`、新人欢迎、画像等其他功能位不变。

## 3. 回滚前保存现场

在 `E:\QQbot\language_lab\release\decision_canary\rollback_backups\` 下新建带时间戳的 `scene_<时间戳>` 目录，复制：

- 当前 `config/group_features.json`；
- 当前 `logs/decision_trace.jsonl`（存在时）；
- 对应时段 `logs/bot.*.log`；
- 若准备代码回滚，再复制当前 `reverse_bot.py`、`scripts/decision_engine.py` 和两个新增模块。

记录回滚时间、原因、操作人、涉及群、配置与代码 SHA-256。不要清空、移动或删除原台账和日志。

## 4. 配置回滚（首选）

### 4.1 单群局部回滚

编辑 `E:\QQbot\config\group_features.json`，只把异常群的：

```text
features.decision_engine: true → false
```

例如重点观察群异常时，只修改 `1102194840` 这一项。其他两群和其他功能位保持不变。

### 4.2 三群全局回滚

出现全局停止条件时，将以下三群的 `features.decision_engine` 都设为 `false`：

- `1102194840`；
- `284517232`；
- `1055881417`。

不要新增群、删除群或修改其他功能位，不要用候选模板整文件覆盖生产配置。

### 4.3 生效与校验

- `group_features.json` 由 `reverse_bot.py` 按 mtime 重新读取，保存后的下一条消息生效，无需重启。
- 校验 JSON 可解析，并对比回滚前后文件：只允许预期群的 `decision_engine` 布尔值变化。
- 已在决策或发送中的一轮可能仍完成一次发送，这是既有在途行为，不是回滚失败。
- 目标群之后的自然消息不再新增决策台账；其他仍开启的群可继续产生记录。
- 不要用人工发送确认开关是否生效。

## 5. `unknown` 和现场纪律

- `unknown` 可能已经到达平台，绝不自动或人工补发。
- `failed` 本次也不自动重试。
- 已发送内容不会因回滚撤回。
- 配置回滚不会清空内存中的冷却、proactive 计数、@ 限流、上下文和去重状态。
- 重新开启前必须查明原因并获得用户确认；不要仅为清空状态反复重启。

## 6. 代码回滚（仅在配置止血不足时）

代码回滚影响所有群，需要重启 `reverse_bot.py`，并由用户在具备仓库权限的控制台执行。

基线提交：`6ad6b325623ffdc7f2863e293007e8b223bf6407`。

1. 先完成三群或受影响群的配置回滚。
2. 保存当前代码、配置、台账和日志。
3. 只还原：
   - `reverse_bot.py`；
   - `scripts/decision_engine.py`。
4. 只移除本次新增代码：
   - `scripts/decision_trace.py`；
   - `scripts/decision_receipt.py`；
   - `scripts/test_decision_release.py`。
5. 不使用 `git reset --hard`、`git clean -fd` 或宽范围 checkout。
6. 还原后执行导入检查，再由用户决定是否重启。

也可使用实验快照中的只读基线副本作为来源，但不得修改实验目录：

- `language_lab/experiments/decision_replay_20261103/snapshot/code/reverse_bot.py`
- `language_lab/experiments/decision_replay_20261103/snapshot/code/scripts/decision_engine.py`

行尾说明：发布清单 baseline 使用 CRLF 工作树字节，git restore 可能写出 LF。哈希不同但代码内容等价时，同时用 `git diff` 和导入检查判断，不要仅因 CRLF/LF 差异重复还原。

## 7. 恢复灰度的条件

只有满足以下条件才可重新打开一个或多个群：

- 停止原因已经定位并修复；
- 现场和回滚记录完整；
- 没有遗留 unknown 补发动作；
- 代码、配置、模型和角色重新与批准的发布基线一致；
- 用户明确同意恢复；
- 恢复后仍从自然消息和首条真实回执开始观察，不人为测试发送。

## 8. 当前准备状态

- 当前未执行单群或三群回滚。
- 生产配置仍保持三群 `decision_engine=true`。
- 配置备份已创建，生产配置未修改。
- 新代码等待用户重启后生效。
