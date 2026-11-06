# QQbot 决策链复查：2026-11-06

审查基线：`d32b2ab60f2dc7eb66fa3ba1b47f24e42f804f25`，对照旧版 `6ad6b325623ffdc7f2863e293007e8b223bf6407`。原始测试输出、定向复现脚本和结果保留为比较基线；本 PR 的修复结果如下。

## 修复结果

9 个 unittest 模块共 **209 项全部通过**，耗时 59.857 秒；包含新增的 27 项回归。完整输出见 [fix-unittest-output.txt](fix-unittest-output.txt)。`reverse_bot`、`private_bot` 导入检查及 `git diff --check` 通过。

| 问题 | 修复后的行为 |
| --- | --- |
| 同群积压与请求耗时 | 一个连接内每群最多 20 条在处理消息，满额时记录群号和消息 ID；同群锁等待上限 60 秒。消息接收时启动 180 秒总预算，覆盖识图、排队、搜索、分类、主备生成和发送，耗尽时取消并进入现有回收流程。断线取消该连接关联的消息任务，回执路由和其他群继续独立处理。 |
| 搜索与上下文预算 | 首次搜索的整个 HTTP 往返受硬超时约束；模型脚本的 60 秒预算从上下文准备开始，继续覆盖启动和通信。 |
| 明确发送失败污染状态 | 同群锁覆盖发送收尾；`finish_reply` 根据回执更新冷却、主动配额、去重与防复读上下文。`failed` 释放本轮状态，`unknown` 按可能已送达保留状态且不自动重发。图文分别判断，文字失败、图片成功时只记录图片回复。 |
| 密度兜底失效 | 低置信度且密度超过兜底门槛时进入生成；正常分类仍走动态阈值，分类器异常仍保持 fail-closed。 |
| @ 回复挤占主动配额 | 回复来源在收尾时决定是否增加主动计数；@/别名保持独立限流，纯图回复同样遵守来源区分。 |
| 画像重复与任务丢失 | 长、短周期串行认领批次，按消息身份清理另一队列的镜像；保留相同文本的新发言和处理期间新增任务。失败归还对应队列，取消时归还当前批次，未处理用户留在原队列。 |
| 模型迁移遗漏 | 默认模型、内置池配置与当前配置一致；视觉测试分别覆盖视觉专用候选和当前私聊首选模型。 |
| 发送耗时与台账 | 写入和等回执共享 10 秒总预算；台账从入口处理开始计时，包含识图和群锁等待。 |

主备模型脚本、群白名单、私聊独立运行、搜索、表情和双周期画像更新沿用现有架构。发送流程通过 `on_reply` 回调在已有群锁内收尾，画像批次调整集中于 `user_profile.py`。

回归命令：

```powershell
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONDONTWRITEBYTECODE='1'
$env:DECISION_TRACE_DISABLED='1'
python -B -m unittest scripts.test_decision_regressions scripts.test_decision_release scripts.test_group_event_loop_offload scripts.test_llm_pool_budget scripts.test_model_script_cleanup scripts.test_private_chat scripts.test_private_dialogue scripts.test_private_runtime scripts.test_proactive_limit -v
python -B -c "import reverse_bot; import private_bot; print('reverse_bot / private_bot imports OK')"
```

## 基线验证结果

Windows、Python 3.10.11，使用项目虚拟环境，UTF-8 输出。8 个 unittest 模块共运行 182 项，耗时 56.894 秒：180 项通过，2 项断言失败。完整输出见 [unittest-output.txt](unittest-output.txt)，定向复现见 [probe-results.json](probe-results.json)。耗时数值是此次运行的观测值。

```powershell
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONDONTWRITEBYTECODE='1'
$env:DECISION_TRACE_DISABLED='1'
# 从仓库根目录，使用已安装 requirements 的项目虚拟环境执行
python -B -m unittest scripts.test_decision_release scripts.test_group_event_loop_offload scripts.test_llm_pool_budget scripts.test_model_script_cleanup scripts.test_private_chat scripts.test_private_dialogue scripts.test_private_runtime scripts.test_proactive_limit -v
python -B docs/audits/2026-11-06/probes.py
```

定向脚本使用模拟模型、模拟 WebSocket 和 loopback HTTP 服务，输出覆盖同目录 `probe-results.json`。原始 unittest 输出保留执行机器路径，便于追溯 traceback。

## 已验证的修复

1. 分类器加载与推理、话题编码、表情选择、图片下载与转换移到工作线程。注入 150ms 同步工作后，10ms 计时器实测约 16ms 触发。事件循环相关测试通过。
2. 模型池请求采用统一截止时间与外层硬超时。慢速分块、预算耗尽停止后续尝试、自定义私聊预算等 6 项测试通过。
3. 模型子进程超时和取消时执行 terminate、等待、必要时 kill，并清理管道。相关 5 项测试通过。
4. 决策回复已接入 echo 回执，台账区分 ok、failed、unknown。模拟平台明确失败时，台账正确记录 failed。

## 基线问题与触发条件

### P1：同群等待缺少完整请求预算与队列约束

位置：`scripts/decision_engine.py:1851`、`:1208`、`:1394`；`reverse_bot.py:739`、`:762`；`scripts/web_search.py:656`、`:730`（行号均对应审查基线）。

同群串行符合注释。当前锁覆盖搜索、上下文准备和模型生成；入口逐条 create_task，断线清理回执状态后，既有消息任务继续执行。暂停群 A 首条生成，再提交 20 条 A 消息，21 个任务均等待，群 B 能完成。分类与上下文准备位于子进程超时之前；将子进程超时设为 10ms、上下文暂停 60ms，任务仍等待，子进程尚未创建。

首次搜索只使用 httpx 网络阶段超时。loopback 服务持续返回小块数据时，150ms 读超时下，调用在约 1110ms 后成功返回。模型池预算因而不能代表整条回复链的耗时上限。

建议：从消息接收开始传递统一截止时间，约束排队数量与消息年龄，连接断开时处理关联任务；搜索和上下文准备共享剩余预算。验收场景：慢搜索、慢分类、突发同群消息、断线期间的待处理消息。

### P1：明确发送失败后，回复状态仍被提交

位置：`scripts/decision_engine.py:1984`、`:2148`、`:2161`；`reverse_bot.py:565`、`:590`。

生成完成即更新冷却、主动配额与最近回复，之后才发送并等待回执。模拟第一次发送返回 failed，再发送另一位用户的新消息并生成相同回答：生成次数为 2，实际发送尝试只有 1 次，第二次被精确去重；第一次失败仍占 1 个配额并保留冷却时间。

建议：区分生成结果、在途发送和已确认发送；明确失败时释放相关状态，成功时提交。unknown 按注释保持结果未知且不自动重发，并为其单独定义配额和去重策略。

### P2：低置信度密度兜底被后续阈值覆盖

位置：`scripts/decision_engine.py:2050`、`:2066`。

注释与分支说明低置信度时按密度决策。密度 12、score=0.2 时，调试结果先写“密度12.0>10，回复”，随后被 0.6 阈值拒绝，模型调用次数为 0。建议让密度兜底的肯定决策进入生成路径；分类器异常继续按独立的 fail-closed 策略处理。

### P2：@ 回复消耗主动发言配额

位置：`scripts/decision_engine.py:387`、`:2008`、`:2169`。

注释要求 @ 限流和 proactive 上限独立。以 5 个不同用户分别 @，成功生成 5 次后主动配额为 5，下一次普通消息被 5/5 上限拦截。复现将冷却设为 0，以单独观测配额影响。建议记录回复来源，只对主动接话增加主动计数。

### P2：画像重试成功后保留旧批次，结束时覆盖新增任务

位置：`scripts/user_profile.py:235`、`:270`、`:305`。

双队列兜底符合注释。复现显示，短周期重试成功后，同一批 5 条消息仍在长周期缓冲；另一个复现中，等待 API 期间加入的新用户任务在重试结束后消失。建议以批次标识清理已完成消息，并合并失败快照与重试期间新增任务。

## 基线模型迁移的两项失败断言

1. `ProductionSafetyTest.test_48_入口接线使用回执层且默认关闭生产台账`：默认常量 `deepseek-flash` 与配置 `deepseek-v4-flash` 不一致。`llm_pool._FALLBACK_CONFIG` 也仍保留旧模型列表，应与新配置一起更新。
2. `PureTest.test_vision_order_prefers_visual_only_model`：测试期待旧的 `qwen3.8-chat`；当前配置视觉模型为 `qwen3.8-flash`。定向复现确认新名称能优先进入视觉尝试序列。建议测试使用明确的候选配置，分别覆盖视觉专用模型和当前生产配置。

## 基线耗时与观测补充

- 回执层对写入、等待回执分别使用 timeout。100ms 参数加 80ms 写入延迟，实测约 203ms 返回 unknown。默认 10 秒参数对应两个独立阶段，文档宜明确这一口径。
- 决策台账从获取群锁之后开始计时，因此排队和前置识图耗时需要结合入口总耗时查看。
- 主备模型脚本按注释运行于独立进程，各自持有模型池冷却状态；整体耗时评估应计入两轮尝试。

## 部署配置

`.env.example` 已列出当前模型出口 `LLM_POOL_BASE_URL` 和 `LLM_POOL_API_KEY`，旧密钥标注为回滚配置。`set_env.py` 默认使用项目下的 `models_cache`，部署者显式设置的 `HF_HOME`、`TRANSFORMERS_CACHE` 和 `HUGGINGFACE_HUB_CACHE` 优先。

本目录同时保留基线证据与修复后的测试输出。
