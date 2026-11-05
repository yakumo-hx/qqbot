# 决策架构全白名单群灰度就绪报告（READINESS_REPORT）

- 任务：群聊决策架构上线前准备（工程准备 + 离线测试 + 灰度方案）
- 日期：2026-11-04（Asia/Shanghai）
- 代码基线：git HEAD `6ad6b325623ffdc7f2863e293007e8b223bf6407`（`2026-11-04T13:19:30+08:00`）
- 基线哈希清单：[manifest.json](manifest.json)
- 灰度方案：[CANARY_PLAN.md](CANARY_PLAN.md)
- 回滚手册：[ROLLBACK.md](ROLLBACK.md)
- 候选配置模板：[candidate_group_features.json](candidate_group_features.json)

> **结论先行：本报告只覆盖"上线前准备与离线测试"。尚未进行真实上线验证，未发送任何真实 QQ 消息，未连接生产 OneBot WS，未调用真实生成模型，未启动/停止/重启任何机器人进程，未修改 `config/` 下任何生产配置。**
>
> **是否可进入全白名单群灰度：** 工程准备与离线测试已完成。用户已确认 `1102194840`、`284517232`、`1055881417` 三群共同灰度，其中 `1102194840` 为重点观察群；生产三群开关保持 `decision_engine=true`，等待用户自行重启 `reverse_bot.py` 后通过自然消息开始验证。

---

## 1. 基线核查（A 节）

| 项 | 结果 |
|---|---|
| git HEAD | `6ad6b325623ffdc7f2863e293007e8b223bf6407`（分支 `main`） |
| 任务开始时工作树 | clean（`git status --porcelain` 无输出） |
| 任务结束时工作树 | 仅本次交付：`M reverse_bot.py`、`M scripts/decision_engine.py`、`?? language_lab/release/`、`?? scripts/decision_receipt.py`、`?? scripts/decision_trace.py`、`?? scripts/test_decision_release.py`（无 commit/push） |
| 生产代码/配置与离线实验 snapshot 是否一致 | **一致**（`reverse_bot.py`=433b…、`decision_engine.py`=8331…、`decision_rules.json`=aa93…、`group_features.json`=a832…，与 `decision_replay_20261103/snapshot/manifest.json` 逐字节相同） |
| 实验 snapshot 校验 | 317 个 code/config/raw 条目全部重算 SHA-256：**0 改动、0 缺失** |
| 实验 `data/out/report/scripts/snapshot` 是否被改写 | **否**；各子目录最后修改时间均 ≤ `2026-11-04T08:32:48+08:00`，早于本任务开始（13:38） |
| 生产 `config/` 是否被修改 | **否**：`decision_rules.json` / `group_features.json` / `active_model.json` / `model_pool.json` / `active_character.json` / `group_characters.json` / `stickers.json` / `labels.json` 哈希与基线一致，mtime 未变 |
| 生产决策台账 | `logs/decision_trace.jsonl` **不存在**（本任务未产生任何生产决策记录） |
| 哈希口径 | manifest 的 baseline/current 是**未提交工作树字节**；修改文件为 CRLF、新增文件为 LF（`.gitattributes` 声明 `eol=lf`）。git 提交/restore 后行尾会变化、哈希随之变化（行为不变），核对口径见 §3 与 CANARY_PLAN §3 |
| 当前生成模型 | `config/active_model.json → deepseek-flash`（sha256 `267cb5f4…`）；模型池 `model_pool.json`（sha256 `f8cb7537…`），优先级链首档 `deepseek-flash/deepseek-flash-2` |
| 本地分类器模型标识 | 默认 `paraphrase-multilingual-MiniLM-L12-v2`，降级 `paraphrase-MiniLM-L3-v2`；本地缓存存在（`models_cache/models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2`）。模型文件本身未冻结哈希（沿用离线实验报告的同一条限制） |
| 离线实验结论引用 | 672 / 23,256 = **2.8896%** 的「生成前政策发言意图」。**不是实际发送率，也不是长期在线发言率**；本任务未据此调整任何阈值，也未追求更高发言率 |

`manifest.json` 中逐文件记录：任务前基线 SHA-256（含来源）、任务后 SHA-256、是否被本任务修改、未改生产配置清单、实验引用、模型标识、测试结果与安全声明。

---

## 2. 已实现事项（B/C/D/E 节）

### B. 结构化决策台账（新增 `scripts/decision_trace.py`）

- 每次进入 `DecisionEngine._should_reply_locked` 的消息生成 `trace_id`：**优先 `message_id`，缺失时随机 UUID**（不含消息正文），记录 `schema_version=decision_trace.v1`。
- **单记录 + `stages` 数组**：决策收尾时一次性追加一行 JSONL（模块级锁 + 单次写入 + flush），并发决策不会交错成半行。
- 生产路径 `logs/decision_trace.jsonl`，由 `reverse_bot.py` 显式传入路径开启；`DecisionEngine` 默认（未显式给路径）**不写任何文件**；测试一律重定向到工作区临时目录。可选环境变量：`DECISION_TRACE_PATH`（演练重定向）、`DECISION_TRACE_DISABLED=1`（强制关闭，灰度观察期不得设置）。
- 字段：`trace_id/timestamp/ts_epoch/group_id/user_id/message_id/rules_version/config_sha256/active_model/classifier_model/is_at_bot/message_length/at_reply_policy/stages[]/classifier_label/classifier_score/dynamic_threshold/classifier_error/classifier_error_total/policy_intent/reached_generation/generation_ok/generation_empty/generation_error/content_filtered/deduplicated/dedupe_kind/content_kind/sticker_only/send_status/send_attempts[]/decision_latency_ms/latency_ms/instrumentation_gap/final_outcome`。
- **三种语义严格分开**：`policy_intent`（规则希望发言）≠ `generation_ok`（得到可用文字内容）≠ `send_status=ok`（OneBot 明确成功）。`websocket.write` 成功**不**记为 ok。
- 隐私：不记录消息正文；`send_attempts[]` 只保留 `kind/status/retcode/message_id/error_kind`，**不含 base64、完整 payload、本地图片路径**。
- 写入失败（目录不可写/磁盘满）**不会影响决策链**，但台账记录会丢失：recorder 记录 `write_failures` / `last_error_type`，首次失败向 stderr 打一行 `[决策台账] 写入失败…` 警告，进程退出（`atexit`）时关闭文件句柄。
- 兜底：调用方漏收尾（异常/未来重构）时，下次决策会把旧 trace 标为 `final_outcome=incomplete / send_status=unknown / instrumentation_gap=true`，不补发任何消息；跟踪键为 `(group_id, trace_id)`，避免 message_id 跨群复用时互相覆盖。
- `final_outcome` 取值：`silent / classifier_error / generation_empty / generation_error / content_filtered / deduplicated / reply_sent / reply_failed / reply_unknown / incomplete`。

### C. @bot 独立防滥用限流（`DecisionEngine` @ 路径）

- 计数键 **`(group_id, user_id)`**，纯内存、随引擎实例存活（重启/新实例清空）。
- 代码默认值（**未写入生产 `decision_rules.json`**）：`at_reply_min_interval_seconds=5`、`at_reply_max_per_user_per_window=6`、`at_reply_window_seconds=60`。
- 0/负值语义：`min_interval=0` 或 `max_per_user=0` 表示**显式关闭该维度**（只建议测试/应急；灰度观察期不得关闭）；`window_seconds` 必须为正，非法/非正值回落 60 秒。
- 可被未来候选灰度配置覆盖：解析顺序 同名扁平键 → `at_reply` 块 → `at_reply.groups[<gid>]`（非法值回落默认；本任务不写生产配置）。
- 超限在**表情检索与模型生成之前**阻断：台账 `stage=at_rate_limited`、`final_outcome=silent`、`policy_intent=false`、`send_status=not_attempted`；不发消息、不占 proactive 名额。
- **与 proactive 上限互不影响**：@bot 仍绕过 `proactive_max_per_hour`；`scripts/test_proactive_limit.py` 未修改（哈希不变）且继续通过。
- 只限制 `DecisionEngine` 的回复；已被 `command_handler` 消费的指令在决策链之前返回，不受新的 @ 限流影响。
- **限制（C.6）**：不根据昵称/文本/显示名猜测 bot 账号；限流键不含 bot 身份，因此不依赖 bot 账号标记。副作用：同一用户在同一群 @ 不同 bot 会共用计数；别名命中与真正 @ 共用该路径与计数；跨群不互相限制。

### D. 决策回复发送回执（新增 `scripts/decision_receipt.py`）

- 最小侵入的 OneBot action echo RPC，echo 前缀 `decision:`；**只用于 `DecisionEngine` 的文字/图片回复**。
- `reverse_bot.handler` 在 `post_type` 路由**之前**调用 `decision_sender.resolve()`，回执不会被当作群消息；断连时 `disconnect()` 把未完成发送标为 `unknown`。
- 三态：`ok` = `status=ok` 且 `retcode=0`；`failed` = 明确失败（非零 retcode / status 非 ok / 非法回执 / 本地读图失败）；`unknown` = 超时 / 断连 / 写入异常 / 无法判定。
- **`unknown` 不自动重发；`failed` 本任务也不重试。**
- 回执超时默认 **10 秒**（`config/decision_rules.json` 可选键 `send_receipt_timeout_seconds` 优先，其次环境变量 `DECISION_RECEIPT_TIMEOUT_SECONDS`；本任务未写生产配置；测试用 0.01~0.2 秒假超时）。**写入与回执都受同一超时约束**：WS 背压导致 `send` 挂起时按 `unknown/write_timeout` 收敛，不会无限拖住调用方。
- `reverse_bot` 中决策回复改为走回执层；文字与图片分别记录一次 `send_attempt`，记录级 `send_status` 按 **unknown > failed > ok** 保守聚合。
- **未重写其他发送链路**：指令 `send_message/send_group_image/send_group_file`、`+1`、新人欢迎、神秘数字队列仍是 legacy（无回执），见 §6。

### E. 分类器异常可观测性

- 保留 **fail-closed**（异常 → 不回复），用户可见行为与改动前一致。
- 台账单独记 `classifier_error={type, phase}`、`stage=classifier_error`、`final_outcome=classifier_error`；**不得**归入 `classifier_low_confidence`。
- 进程内计数 `DecisionEngine.classifier_error_count`，并写入每条台账 `classifier_error_total`。
- try 范围覆盖"分类器懒加载 + 单条推理"：模型加载失败同样按分类器异常 fail-closed 记录（否则会冒泡成决策引擎整体报错）。
- 只记录异常类型与阶段，**不记录异常文本**（异常文本可能夹带消息正文）。

### 其他（为满足验收而做的必要改动）

- **重复 `message_id` 抑制**：同一 `(group_id, message_id)` 进程内只决策/发送一次（最多 1024 键，重启清空）；抑制发生在台账创建之前，因此**不产生第二条记录**（只写 bot 日志）。入口层 `reverse_bot` 原有的 `message_id` 去重保持不变（第一道防线）。
- **生成异常收口**：`_generate_reply_async` 抛异常时由引擎内 catch（`CancelledError` 除外），台账记 `generation_error={type, phase}`、fail-closed 不回复；不再向 `reverse_bot` 外层抛出（用户可见行为不变，异常类型进台账与 bot 日志）。
- `ReplyResult` 增加 `trace` 槽位（新增文件/配置之外的唯一共享数据形状变化，向后兼容：默认 `None`）。

---

## 3. 修改与新增文件

| 文件 | 类型 | 基线 SHA-256（任务前） | 当前 SHA-256 | 字节数 | 说明 |
|---|---|---|---|---|---|
| `reverse_bot.py` | 修改 | `433b2004911139c7566f54a518e168c2a1f82349d3ed4fe98efcfa37c6ae1d66` | `2012663a18b64555420af0f1a3e96075120f3d9978fc7e7b890eafdf1eecd446` | 37647 | 台账路径开启、决策回复回执发送与收尾、handler 回执路由、message_id 透传、回执超时配置读取 |
| `scripts/decision_engine.py` | 修改 | `8331cd4d6ffccf42b51ce021bc62f142f6670abccf30b13a23959d36e59acfde` | `561ab312725ac620b81e70c0fd03645e7a58ee3c2ff532aaed8ae2aed534be6a` | 95218 | 台账/stages、@bot 独立限流、分类器异常标记与计数、message_id 去重、生成异常收口、`ReplyResult.trace`、台账跟踪键 `(gid, trace_id)` |
| `scripts/decision_trace.py` | 新增 | — | `2dcdc9a72d8ea742bc1ade1ff79f77d58a9c29497d91c2d96eada0175a717541` | 28795 | JSONL 台账 schema v1 + `AtReplyLimiter` + 策略解析 + 写失败可观测/atexit 关闭 |
| `scripts/decision_receipt.py` | 新增 | — | `4a02a3ab302c1fa0a2436112010ec81953f5e0ae8651808976bad5fce95a96e1` | 10350 | echo 回执层（ok/failed/unknown，写入与回执均受超时约束，unknown 不重发） |
| `scripts/test_decision_release.py` | 新增 | — | `fb40977ead390a544754582a44837ec97258a0dc826d2a1d6db0997dd54ee3da` | 66381 | 51 条离线 unittest（真实 unittest，无跳过/无伪造） |

**未修改**（可核对哈希）：`config/` 下全部生产配置、`scripts/test_proactive_limit.py`（`ff9b73639e717b851df4de320b738c4742485a464e83c833a997ae2b0afd3656`）、`scripts/zero_shot_classifier.py`、`scripts/interaction_menu.py`、`scripts/private_chat/rpc.py`、`scripts/command_handler.py`、`scripts/commands/_state.py`、`config/characters/**`、`language_lab/experiments/decision_replay_20261103/**`。

> 工程备注：`reverse_bot.py` / `decision_engine.py` 沿用仓库检出的既有 CRLF 行尾；新增文件为 LF（`.gitattributes` 声明 `eol=lf`，git 提交时会统一规范化）。`git diff --check` 通过，不影响运行。

---

## 4. 测试命令与真实结果

所有命令在 `E:\QQbot` 下、使用项目 venv、UTF-8 执行；**未运行任何真实网络/QQ/模型调用**。

### 4.1 验收命令

```powershell
$env:PYTHONIOENCODING="utf-8"
& ".\venv\Scripts\python" -m unittest scripts.test_proactive_limit scripts.test_decision_release -v
& ".\venv\Scripts\python" -c "import reverse_bot; print('reverse_bot OK')"
& ".\venv\Scripts\python" -c "import private_bot; print('private_bot OK')"
```

实际结果：

| 命令 | 真实输出（节选） | 结果 |
|---|---|---|
| unittest 两模块 | `Ran 58 tests in 0.415s` / `OK`（`scripts.test_proactive_limit` 7 条 + `scripts.test_decision_release` 51 条） | **通过** |
| `import reverse_bot` | `reverse_bot OK` | **通过** |
| `import private_bot` | `private_bot OK` | **通过** |
| `git diff --check` | 无输出，exit 0 | **通过** |

补充：以最终版本最后一次运行为准，`scripts.test_decision_release` 51 条全部通过（独立审计员另行复跑亦为 `Ran 55 tests / OK`；本报告按最终代码复跑为 `Ran 58 tests / OK`）；删除了生成清单用的临时核查脚本后再复跑验收命令，仍全绿。开发过程中曾因本机 TEMP 不可写出现过测试临时目录问题，已在测试里改为工作区临时目录并在 teardown 清理（最终版本干净通过，无跳过/无 xfail）。

受影响的既有离线测试（共享模块 `decision_engine` 被修改）：

```powershell
New-Item -ItemType Directory -Force .tmp_existing_tests | Out-Null
$env:TEMP="E:\QQbot\.tmp_existing_tests"; $env:TMP="E:\QQbot\.tmp_existing_tests"
& ".\venv\Scripts\python" -m unittest scripts.test_reference -v
```

实际结果：`Ran 49 tests in 10.297s` / `OK`（独立审计员另一次运行 9.711s 亦 OK）。**说明**：`scripts/test_reference.py` 用 `tempfile.gettempdir()` 写名册临时文件，本机沙箱默认 TEMP 不可写，因此运行时把 `TEMP/TMP` 指向工作区临时目录（**未修改测试文件**；该环境限制不是本次改动引入的）。

### 4.2 新增测试与任务书 F 节 1~18 的对应

| 任务书要求 | 覆盖测试（`scripts/test_decision_release.py`） |
|---|---|
| F1 普通沉默 / 普通 `policy_intent` | `test_10`（另见 `test_01`、`test_09`） |
| F2 @bot 正常回复 | `test_11`（另见 `test_40`） |
| F3 同 gid+uid 60s 窗口第 7 次被阻断 | `test_12`（引擎层）、`test_42`（入口层） |
| F4 5 秒最小间隔 | `test_13` |
| F5 假时钟越过窗口恢复 | `test_14` |
| F6 两群、两用户计数互不影响 | `test_15` |
| F7 @bot 仍不受 `proactive_max_per_hour` 阻断 | `test_16`（既有 `test_艾特机器人不受限流影响` 继续通过） |
| F8 指令处理不受新限流影响 | `test_38` |
| F9 重复 `message_id` 不产生第二次决策/发送 | `test_19`（引擎）、`test_19b`（同一 mid 跨群互不影响）、`test_39`（入口；先清掉入口旧去重以强制进入引擎去重） |
| F10 模型异常/空文本/安全过滤/精确去重可区分 | `test_20`、`test_21`、`test_22`、`test_23`、`test_24`、`test_25` |
| F11 分类器异常记为 `classifier_error` | `test_26`（推理异常 + 懒加载失败两种） |
| F12 发送 ok/failed/unknown 三态 | `test_27`、`test_28`、`test_29`（另 `test_30`/`test_30b`/`test_31`/`test_32`、`test_35`；`test_30b` 覆盖写入超时 → unknown/write_timeout） |
| F13 unknown 不自动重发 | `test_29`、`test_30`、`test_31`、`test_41`（入口端到端） |
| F14 echo 响应与群消息正确分流 | `test_33`（回执层）、`test_43`（入口 `handler`） |
| F15 图片发送不泄漏 base64 | `test_05`（引擎台账）、`test_34`（回执 payload 有 base64、台账无） |
| F16 重启/新实例按内存语义重置 | `test_18` |
| F17 台账串联同一 `trace_id`，三语义不混用 | `test_09`、`test_06`、`test_07`（另 `test_40`） |
| F18 全程无真实网络/QQ action | `test_44`（入口流程内 patch `socket.socket`/`asyncio.create_subprocess_exec`/`websockets.connect` 为抛错仍通过）、`test_46`/`test_47`/`test_48`（生产台账未创建、环境安全网、接线检查） |

新增测试的 51 条中还包含：台账 schema 与隐私（`test_01`/`test_05`/`test_34`）、并发不交错（`test_04`）、trace_id 来源（`test_02`）、收尾幂等（`test_06`）、孤儿兜底（`test_07`）、默认关闭（`test_08`）、**台账写失败不静默且不影响决策**（`test_08b`：`write_failures`/stderr 警告/仍返回沉默）、限流策略默认值与候选配置覆盖（`test_17`）、聚合优先级（`test_35`）、legacy 路径声明（`test_36`）、回执模块不引入私聊运行时（`test_37`）、生产配置哈希未变（`test_45`）。其中 `test_18`（新实例重置冷却与限流）、`test_25`（五种结局由真实决策产出）、`test_39`（入口进入引擎去重）在独立审计后被加强，不是同义反复。

**没有降低断言、没有跳过测试、没有使用真实发送来"凑通过"**：`send ok` 只由模拟 WebSocket 回执驱动；引擎层台账收尾测试用显式标注的"模拟回执"对象（不产生任何网络/QQ 行为）；真实三态由 `test_27`~`test_34` 的回执层与入口端到端测试覆盖。

---

## 5. 生产安全核查（本任务全程）

| 约束 | 核查结果 |
|---|---|
| 不发送任何真实 QQ 消息 | ✅ 未调用任何真实 `websocket.send`；所有发送都是模拟连接 |
| 不启动/停止/重启 `reverse_bot.py`、`private_bot.py`、NapCat | ✅ 未执行；仅做 `import` 自检与离线测试 |
| 不连接生产 OneBot WebSocket | ✅ 未建立任何 WS 连接（`test_44` 还会强制 `socket`/`websockets.connect` 抛错以证明） |
| 不调用真实生成模型/云端 API | ✅ 本任务的命令（离线测试/导入自检）未触发任何模型调用：生成/分类全部打桩、`test_44` 强制 `asyncio.create_subprocess_exec` 抛错仍通过。**观察项**：`logs/llm_pool.jsonl` 在本任务窗口内有真实模型调用（label=`profile`/生成），时间线与**一直在运行的生产 bot**（PID 14284，11-03 启动）一致，且测试窗口内无新增；不是本任务命令发起的（见 §8.4） |
| 不修改生产 `config/` | ✅ 哈希与基线一致（见 §1、§3、`test_45`） |
| 不修改/覆盖离线实验 snapshot/raw/data/out/报告 | ✅ 317 个快照条目重算一致、0 改动；data/out/report mtime 早于任务开始 |
| 不修改私聊业务逻辑 | ✅ `scripts/private_chat/**` 未改；`rpc.py` 仅作设计参考（`test_37` 检查回执模块无 `private_chat` import） |
| 不做无关重构 / 不改指令/角色/模型池/阈值 | ✅ 指令链路、角色、模型池、`decision_rules.json` 均未改 |
| 不执行破坏性 git 操作 | ✅ 未 `commit/push/reset`；`git diff --check` 通过 |
| 测试只用临时目录/内存/假时钟/模拟 WS/生成桩 | ✅ 台账与临时文件均在工作区临时目录并在 teardown 清理；测试进程强制 `DECISION_TRACE_DISABLED=1` 作为安全网 |
| 生产决策台账未被创建 | ✅ `logs/decision_trace.jsonl` 不存在。`logs/bot.2026-11-04.log` 在本任务验收命令跑完后为 **0 字节**；独立审计脚本于 18:08:29 误写 2 行/209 B（假 gid 100880001 的测试日志），已按"只读、不删生产日志"的纪律**保留现场**，见 §8.4 |

---

## 6. 仍未覆盖的发送路径（明确 legacy）

以下路径**没有 echo 回执**，不在 `logs/decision_trace.jsonl` 的发送统计内，也不能用台账的 `send_status=ok` 推断它们的发送量：

- 指令回复：`send_message` / `send_group_image` / `send_group_file`（`command_handler` 及各指令模块）；
- `+1` 复读（`reverse_bot.handle_message` 内）；
- 新人欢迎（`handle_group_increase`）；
- 神秘数字队列（`mystery_number_task`）；
- 互动菜单开销类 action（沿用 `interaction_menu.OneBotRPC`，既有实现，本次未动）。

本次**没有**为"统一架构"重写上述链路；`scripts/decision_receipt.py::legacy_note()` 与文档中均已声明该边界。

---

## 7. 剩余风险与限制

1. **回执未做真实 OneBot 验证**：`ok` 语义依赖 NapCat 对 `send_group_msg` 返回 `status/retcode/echo`。若某部署不回 echo，决策回复会等满超时（默认 10s）后记 `unknown`——消息仍会发出、不会自动重发，但**延迟与统计都会受影响**。灰度第一步（P3）必须观察首条决策记录的 `send_status` 与延迟；这也是 CANARY_PLAN §7 的停止线之一。
2. **发送/回执等待不在群锁内**：`should_reply` 的群锁只覆盖决策本身；拿到 `ReplyResult` 后，发送与回执等待发生在 `reverse_bot` 里、**群锁之外**，所以同群后续消息的决策不会被等待回执阻塞（多轮发送可能并发；观测延迟要按单条发送看；文字+图片最坏 2×超时）。
3. **台账写失败会降级（但不再静默）**：`logs/` 不可写/磁盘满时 recorder 关闭自身，记录会丢失；`write_failures`/`last_error_type` 与首次 stderr 警告用于发现该情况（只影响可观测性，不影响决策）。灰度前必须确认生产进程可写 `logs/decision_trace.jsonl`（P3）；`instrumentation_gap` 非 0 要先修台账再谈结论。
4. **@ 限流是"准入"计数**：通过限流即占额（即使随后生成失败/空文本），用于堵住"必然失败的生成绕过成本闸门"；副作用是连续生成失败时用户更快触顶。若未来要改语义需单独评审。
5. **无法自动识别其它 bot 账号**：入口仅过滤 `self_id`；机器人循环只能靠 CANARY_PLAN §7 的人工/统计停止线发现，代码层没有账号判定（C.6 限制）。
6. **`message_id` 唯一性假设**：引擎级去重键 `(gid, message_id)`，依赖 OneBot message_id 全局唯一；若复用会误抑制。入口层原有去重仍在，二者都只在进程内有效。
7. **分类器异常只记类型**：为隐私不记录异常文本，排查需结合时间线；`classifier_error_total` 是进程内累计，重启归零，不能跨进程相加。
8. **台账无轮转**：所有 `decision_engine=true` 群都会写；按历史消息量估计约 1~2 MB/日量级，需运维关注磁盘（本任务未加轮转）。
9. **代码尚未发布**：按约束未 commit/push，工作树即交付物；生产进程仍在跑旧代码。发布/重启与灰度执行不在本任务范围。
10. **行尾**：修改文件保留仓库既有 CRLF、新文件 LF；提交时由 `.gitattributes` 统一，`git diff --check` 通过（工程备注，不影响行为）。
11. **分类器异常/生成异常的行为**：异常仍然 fail-closed（与改动前用户可见行为一致）；但分类器异常路径现在提前 return（不再走"低置信降级"分支后仍沉默）——结果同为沉默，台账区分更清晰。
12. **实验口径**：2.8896% 是生成前政策发言意图，本任务没有把它当发送率，也没有据此调阈值或追求更高发言率。
13. **生产日志有 2 行审计残留**：`logs/bot.2026-11-04.log` 被独立审计脚本误写 2 行/209 B（假 gid 测试日志）；保留现场未清理，详见 §8.4。
14. **真实模型调用来自运行中的生产 bot**：本任务命令未调用模型，但生产 bot 一直在跑并调用模型。进程拓扑已复核：PID 30636 是 venv 启动器父进程，PID 14284 是其子进程且唯一监听 :8080，不是两个独立机器人。详见 §8.4。
15. **决策回复 payload 的 JSON 字节形状**：回执层用 `ensure_ascii=False`（legacy `send_message` 用默认 `True`）；语义等价、OneBot 可解析，但如果灰度期用字节级比对做取证需注意差异。
16. **哈希口径**：manifest 的 baseline 是"未提交 CRLF 工作树"字节；`.gitattributes` 的 `eol=lf` 会在 checkout/restore 后改变哈希（行为不变）。核对方法见 CANARY_PLAN §3 与 ROLLBACK §6。

**阻塞项：无**（工程准备层面）。用户已确认三群共同灰度及重点观察群，发布材料与配置备份已更新；真实部署仍需要用户重启 `reverse_bot.py`，随后观察自然消息的首条台账与真实回执，**这些均未在执行准备阶段完成**。

---

## 8. 独立审计与修正记录

收尾阶段由独立审计员（只读）复核了代码/测试/文档/生产安全。审计结论：**P0 阻断项：无**；代码实现（B/C/D/E）真实落地、测试覆盖 F1~18、生产配置/实验/日志无污染、无真实发送，均核查通过；审计员独立复跑得到 `Ran 55 tests / OK`（当时版本）与 `scripts.test_reference` 49/49 OK，并按 G 节逐项读过三份灰度文档。以下 P1/P2 已处理或已明确记录。

### 8.1 P1（已修）

- **旧版 CANARY_PLAN 的灰度前核对与哈希口径**：原文档要求用 `Get-FileHash` 把代码文件对照 manifest 的 *baseline*——但发布已改代码，照做会必然失败。现改为"**配置对照 baseline、代码对照 current_after_task**"，并写明 CRLF/LF 行尾两种口径。
- **旧版 ROLLBACK 的回滚期望哈希**：`git restore` 按 `.gitattributes` 写出 LF，哈希与 CRLF baseline 不同，原表格会误判回滚失败。manifest 现同时保留 LF（`ddb17e43…` / `8e8d2b92…`）与 CRLF 快照副本（`433b…` / `8331…`）两组期望值，并说明行尾差异不影响行为。
- **manifest baseline 冗余哈希不一致**：baseline 条目里的 `current_sha256` 已统一刷新为最终值（与 `current_after_task` 一致），并新增 `hash_convention` 说明。

### 8.2 P2（已修 / 已记录）

- READINESS 风险 2 更正为"发送/回执等待发生在群锁之外"（原表述错误）。
- 旧版 CANARY_PLAN 停止检查改为按"有发送尝试"的普通接话统计 proactive 占额（proactive 名额在决策回复产生时即占用，与发送 ok/failed/unknown 无关）；现行方案保留同一语义。
- CANARY_PLAN §7 跨群异常信号改为 `(group_id, trace_id)` 定位，并说明 `trace_id` 跨群重复本身不是异常（它优先取 message_id）。
- candidate 模板收敛为**只含 `decision_engine`** 一个字段，其余功能位必须照抄目标群现状（避免误开功能位）。
- 台账写失败改为可观测：`write_failures` / `last_error_type` / 首次 stderr 警告；文件句柄 `atexit` 关闭。
- `_open_traces` 跟踪键改为 `(group_id, trace_id)`，避免 message_id 跨群复用时覆盖未收尾跟踪项。
- 回执层 `websocket.send` 增加超时（`unknown/write_timeout`），避免 WS 背压时无限挂起。
- 明确 0/负值限流语义（0 = 显式关闭该维度；window 非法回落 60 秒）。
- 加强测试：`test_18`（新实例重置冷却+限流）、`test_25`（五种结局由真实决策产出）、`test_39`（清掉入口旧去重以强制进入引擎去重）；新增 `test_08b`（写失败不静默）、`test_19b`（同一 message_id 跨群互不影响）、`test_30b`（写入超时 unknown）。测试由 48 → **51**。
- **未修（记录为已知限制）**：决策回复 payload 用 `ensure_ascii=False`（legacy 默认 True，语义等价）；`_active_model_name()` 每条决策读一次 `active_model.json`（廉价文件 IO，未加缓存）；台账无轮转（见 §7-8）。

### 8.3 审计未覆盖 / 未执行

- 未执行灰度/部署/重启/任何真实发送/真实 OneBot 回执验证（任务禁止）。
- 未独立证明"任务开始时工作树 clean"（由 baseline 哈希 + HEAD 间接证明）。
- 未逐行审 `decision_engine.py` 的未改动部分，仅跑任务指定的既有测试。

### 8.4 保留现场的观察项（未清理）

- **生产日志 2 行审计残留**：`logs/bot.2026-11-04.log` 于 18:08:29 被审计员的独立引擎级验证脚本写入 2 行/209 B（假 gid 100880001 的"重复 message_id 已抑制"/"生成异常"）。本任务验收命令跑完后该文件为 **0 B**；审计按只读纪律未清理，保留现场。生产 bot（11-03 启动）写的是 `logs/bot.2026-11-03.log`，故不影响生产观测。
- **真实模型调用属于运行中的生产 bot**：`logs/llm_pool.jsonl` 在 16:31/16:33/17:14（生成）与 18:25 左右（label=profile）有真实调用；时间线与生产 bot（PID 14284，11-03 启动）一致，本任务的测试窗口内无新增，且测试全部打桩（`test_44` 证明无子进程）。**进程拓扑已复核**：30636 是 venv 启动器父进程，14284 是其子进程且唯一持有 :8080，不构成重复运行；本任务未触碰任何进程。
- **哈希口径**：manifest 是"未提交 CRLF 工作树"字节；提交后 `.gitattributes` 的 `eol=lf` 会改变哈希（行为不变）。核对时保持口径一致，见 CANARY_PLAN §3 与 ROLLBACK §6。

---

## 9. 用户确认后的执行范围与待办

- 灰度范围：`1102194840`、`284517232`、`1055881417` 三群共同灰度；`1102194840` 重点观察。
- 生产配置：三群当前均为 `decision_engine=true`，本次无需改动，其他功能位保持不变。
- 配置备份：`rollback_backups/group_features.pre_all_group_canary_20261104_212703.json`，已按当前生产配置创建。
- 重启执行人：用户。准备任务不启动、停止或重启任何机器人。
- 重启后首要检查：自然消息产生首条台账，以及首次真实发送的 NapCat echo/回执延迟。
- 首个质量复核节点：三群合计约 **100 条记录级 `send_status=ok` 的决策回复**；逐群、普通接话与 @bot 分开统计，每个有发送的群都要人工抽样。
- 冻结：观察期不调阈值、角色、生成模型、proactive 上限与 `at_reply` 限额。

---

## 10. 交付物

| 文件 | 说明 |
|---|---|
| [manifest.json](manifest.json) | 基线/当前哈希、git 状态、模型标识、实验引用、测试结果与安全声明 |
| [CANARY_PLAN.md](CANARY_PLAN.md) | 全白名单三群灰度方案；`1102194840` 重点观察；100 条成功决策回复复核；逐群及汇总统计；立即停止条件 |
| [ROLLBACK.md](ROLLBACK.md) | 单群局部/三群全局配置回滚 + 代码回滚说明、备份/校验、unknown 不补发、验证只靠日志与台账 |
| [candidate_group_features.json](candidate_group_features.json) | 三群灰度候选对照文件（非生产副本；禁止整文件覆盖） |
| `rollback_backups/group_features.pre_all_group_canary_20261104_212703.json` | 用户确认三群灰度时的生产群配置备份 |
| `scripts/test_decision_release.py` | 51 条离线 unittest（本任务的测试交付物） |

---

## 11. 最终声明

**尚未进行真实上线验证。**

本报告中的所有"通过"均来自离线测试与只读核查；`send_status=ok` 的语义只在模拟 WebSocket 回执下被测试，未对真实 NapCat/QQ 平台做过一次真实发送验证。用户已确认三群共同灰度并指定 `1102194840` 重点观察；当前只完成发布材料与配置备份，等待用户重启 `reverse_bot.py` 后按 [CANARY_PLAN.md](CANARY_PLAN.md) 通过自然消息开始验证。
