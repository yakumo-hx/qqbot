# LLBot 试验 — 测试矩阵（TEST_MATRIX）

- 版本/环境见 `../REPORT.md`；证据根：`E:\QQbot\qq_native_re\findings\llbot_trial\`
- 判定口径：**PASS**=有实际证据；**FAIL**=实测失败；**BLOCKED**=外部条件挡住；**NOT_RUN**=未执行；**UNKNOWN**=无法判定；**NOT_CLEARED**=未获准做可见操作
- 只向已批准目标发消息：私聊 784427550（用户本人）、群 284517232（消息带【LLBot测试】标识）

## 0. 前置与隔离

| ID | 项目 | 结果 | 证据 | 说明 |
|---|---|---|---|---|
| P1 | 备份 + manifest + ROLLBACK 先于账号测试 | PASS | `../manifest.json`、`../ROLLBACK.md`、`patches/llbot_trial_20261024-194032/` | 44 个文件备份 + 37 项完整性快照 |
| P2 | 生产配置零改动 | PASS | `../manifest.json` → `backup_entries` 逐项复核 0 变化 | 回滚后复核 |
| P3 | LLBot 独立目录安装 | PASS | `E:\QQbot-trials\llbot\` | CLI 包，含 PMHQ 与自包含 node/ffmpeg |
| P4 | OneBot 只连隔离端口 18080/18081/18082 | PASS | `logs/stageA/probe/events.jsonl`、`logs/stageB/probe/events.jsonl` | 生产 8080/8081 全程未接 LLBot |
| P5 | Milky/Satori 关闭、WebUI 仅 127.0.0.1:3080 | PASS | `patches/.../llbot_config/default_config.json`、LLBot stdout | 配置显式 enable=false |
| P6 | 无生产 data/private 写入（试验阶段） | PASS | 试验前后快照对比；`baseline.json` 与回滚复核 | 试验期间生产 bot 已暂停；**回滚后**生产 private_bot 正常回填了 QQ 历史（见 R6） |
| P7 | 同账号单登录 | PASS（按日志） | `state/trial_timeline.json` | 有头/无头不并行；切换时先停旧进程再登新 |

## A1. 协议与连接（有头 PMHQ）

| ID | 项目 | 结果 | 证据 | 说明 |
|---|---|---|---|---|
| A1-1 | OneBot 11 反向 WS 连接 | PASS | `logs/stageA/probe/events.jsonl`（ws_connected，ua=LLOneBot/8.3.2，x-self-id=2668851638） | 3s 内连上 |
| A1-2 | heartbeat | PASS | 同上 meta_event/heartbeat interval=30000 status.online=true | 有头/无头均正常 |
| A1-3 | lifecycle connect | PASS | 同上 meta_event/lifecycle sub_type=connect | 重连会重发 |
| A1-4 | get_login_info | PASS | `logs/stageA/results.jsonl` | user_id/nickname 正确 |
| A1-5 | get_status | PASS | 同上 | online/good/stat 字段齐 |
| A1-6 | get_version_info | PASS | 同上 | app_name=LLOneBot, protocol_version=v11, app_version=8.3.2 |
| A1-7 | 群消息事件字段 | PASS | `logs/stageA/probe/events.jsonl`（sender_keys/card/role/title 等） | 见下“字段对照” |
| A1-8 | 私聊消息事件字段 | PASS | `logs/stageB/probe/events.jsonl`（private，sender_keys=[nickname,user_id]） | 有头私聊事件同类 |
| A1-9 | messageFormat / raw_message CQ 兼容 | PASS（有坑） | 群事件 raw_message=`[CQ:at,qq=2668851638,name=Deepseek] …` | 生产解析器能解析 image/reply，但 **@bot 字面量判断会漏判**（见 A5-1） |
| A1-10 | 两个独立 WS 通道同时工作 | PASS | 同一 message_id 在两通道各出现 1 次，时间差 1–3ms | ch0 开了 debug，帧体积大 3–8 倍；ch1 无 debug 帧更小 |
| A1-11 | 两通道互不覆盖 | PASS | `logs/stageA/results.jsonl`（分别从 ch0/ch1 发 action 均 ok） | 回执按连接+echo 路由 |
| A1-12 | echo / retcode / message_id | PASS | 同上 | retcode=0；message_id 返回且与 get_msg/history 一致 |

**字段对照（LLBot 实测 vs 生产期望）**

| 字段 | 生产需要 | LLBot 实测 | 结论 |
|---|---|---|---|
| `group_id`/`user_id`/`self_id` | 是 | 有（数值） | OK |
| `message_id` | 是（去重/落盘） | 有；**可为负数**，同一消息在发送回执与历史中一致 | 需按字符串处理，生产已按 str 处理 |
| `raw_message` | CQ 字符串 | 有（`[CQ:reply]`/`[CQ:at,qq=…,name=…]`/`[CQ:image,…&amp;…]`） | OK，但 `name=` 参数影响字面量匹配 |
| `message` | array | 有；image 元素含 `file/url/file_size/subType` | OK（生产使用 message 数组调试能力也兼容） |
| `sender` | nickname | 群：card/level/nickname/role/title/user_id；私聊：nickname/user_id | OK |
| `time`/`font` | 部分 | 有 | OK |
| `post_type=message_sent` | 生产不发自消息 | 历史/get_msg 会带 message_sent | 生产 `reportSelfMessage=false`，正常 |

## A2. 核心 action（有头）

| ID | action | 结果 | 证据 | 说明 |
|---|---|---|---|---|
| A2-1 | send_private_msg | PASS | `evidence_actions.json` | retcode=0，message_id 可落盘 |
| A2-2 | send_group_msg | PASS | 同上 | 群 284517232 实测可见（用户确认） |
| A2-3 | get_friend_msg_history | PASS | 同上 | 返回 messages[]，含 raw/raw_pb，顺序 earliest→latest |
| A2-4 | get_group_msg_history | PASS | 同上 | 5 条样本，message_seq 连续 |
| A2-5 | get_msg | PASS | 同上 | 文本/图片/回复@ 均能取回 |
| A2-6 | 图片接收 + 下载 | PASS | `logs/stageA/probe/images.jsonl` | HTTP 200，image/jpeg 92228B，sha256 记录；URL 含 rkey |
| A2-7 | 图片发送（base64 数组） | PASS | `evidence_actions.json` | 私聊/群均可；8.1MB 大图 5.3s 成功 |
| A2-8 | 回复 + @ 消息 | PASS | `evidence_actions.json`（get_msg msg_types=[reply,at,text]） | 生产用 CQ 字符串发送，LLBot 正确解析 |
| A2-9 | get_group_member_list | PASS | 同上 | 180 人 |
| A2-10 | get_group_info / get_friend_list | PASS | 同上 | group_name/member_count 正常；好友 1 人 |
| A2-11 | get_group_list | PASS | 同上 | 4 个群 |
| A2-12 | 其他项目实际使用 action | PASS | 生产仅依赖以上 + set_msg_emoji_like（扩展） | — |

### 扩展项（不因失败误判核心）

| ID | action | 结果 | 证据 | 说明 |
|---|---|---|---|---|
| X1 | set_msg_emoji_like | NOT_CLEARED | — | 会产生群内可见表情回应；未获准 |
| X2 | group poke | NOT_CLEARED | — | 可见操作；未获准 |
| X3 | get_group_notice（群公告） | **FAIL** | `evidence_actions.json` retcode=1404 | LLBot 返回失败；不影响核心 |
| X4 | 文件相关接口 | NOT_RUN | — | 可见/副作用，未测 |

## A3. 顺序与回执

| ID | 项目 | 结果 | 证据 | 说明 |
|---|---|---|---|---|
| A3-1 | 连续文本不乱序 | PASS | `evidence_actions.json` order_history_tail：seq 446→453 与发送顺序一致 | 6 条 + 图 + 文 |
| A3-2 | 图片后紧跟文字不乱序 | PASS | 同上（img seq 452，txt seq 453） | — |
| A3-3 | WS 断开后重复补报 | PASS（无重复） | `evidence_faults.json` duplicate_report：29 个 message_id、58 行、0 重复 | reportOfflineMessage=false |
| A3-4 | 重连后历史补拉重复 | PASS（去重生效） | 私聊测试副本日志：首次 20 新增 20；重连 20 去重 20 | 生产同逻辑 |
| A3-5 | action 超时区分 failed/unknown | PASS | `evidence_faults.json` timeout_call status=unknown；history 中该消息实际已送达 | unknown 不自动重发（生产 rpc.py 语义正确） |
| A3-6 | 回执 message_id 可用于落盘 | PASS | 测试副本 attempts.jsonl：status=sent + msg_id；sessions 带 msg_id/delivery_status | 2 气泡时逐气泡记录 |
| A3-7 | 两条 WS 交叉回应/互相清理 | PASS（未观察到） | 两通道各自收发，无串扰；关闭 ch0 不影响 ch1 | — |

## A4. 故障注入（有头）

| ID | 变量 | 结果 | 证据 | 说明 |
|---|---|---|---|---|
| A4-1 | 停止测试 WS 服务端（close ch0） | PASS | `events.jsonl` ws_disconnected 20:48:24.284 → ws_connected 20:48:27.293 | **重连 3.0s** |
| A4-2 | 重启 Python 测试接收端（进程整体重启） | PASS | 两通道均在 ~2s 内重连 | 无重复补报 |
| A4-3 | 通过 LLBot 正常方式重启协议端 | PASS | `state/trial_timeline.json` restart_1 | 登录 22s，快速登录无二维码 |
| A4-4 | 模拟 action 超时 | PASS | `evidence_faults.json` | 0.05s 超时 → unknown；实际已送达 |
| A4-5 | 大图片/大 payload | PASS | `evidence_faults.json` big_image：8,101,680B，5.3s，retcode=0 | 未发生截断 |
| A4-6 | 断网恢复 | NOT_RUN | — | 会影响整机/用户网络，超出安全边界 |
| A4-7 | 协议端/WS/QQ 三态区分 | PASS | get_status.online 与 WS 连接状态分开记录；headful 重启期间 WS 断但账号随后恢复 | 不把 WS 在线等同 QQ 在线 |
| A4-8 | QQ 错误弹窗（wrapper.node） | 已记录（UNKNOWN 归因） | `logs/stageA/qq_crash_dialog_wrapper_node.png`、`qq_crash_4213eee1.txt/.dmp` | 崩溃时间 21:27:10 与“强制结束试验 QQ 进程树”重合；无法排除系测试操作诱发，需长期观察 |

## A5. 现有业务验证（测试副本）

测试副本：`E:\QQbot-trials\qqbot_test`（`copy_adaptation.json` 记录 5 处最小适配）

| ID | 项目 | 结果 | 证据 | 说明 |
|---|---|---|---|---|
| A5-1 | 群聊被动回复（@bot） | **PASS（需最小补丁）** | 打补丁前：`testcopy_reverse.out` 无回复；补丁后：`[决策] 群 284517232 回复`，LLBot 收到 `send_group_msg` | 根因：LLBot CQ 码 `[CQ:at,qq=…,name=Deepseek]`，生产用字面量 `[CQ:at,qq=…]` 判断 → 漏判。补丁见 `patches/.../test_copy_at_patch/` |
| A5-2 | 私聊结构化多气泡 | PASS | 测试副本 `attempts.jsonl`：bubble_count=2，bubble_index 0/1，逐气泡 sent + msg_id | 3 条连发合并，reply_to_msg_ids 3 个 |
| A5-3 | revision 打断 | PASS（间接） | 会话 turn_id 递增（revision 1→7）；新消息进入后旧回复未落盘 | 未做严格“生成中打断”计时 |
| A5-4 | 主动任务竞争 | NOT_RUN | — | 为安全在测试副本关闭 proactive；生产逻辑未改 |
| A5-5 | 私聊重连历史补拉 | PASS | `testcopy_private3.out`：重连拉 20，新增 0，去重 20 | — |
| A5-6 | 每气泡回执与落盘 | PASS | `attempts.jsonl` + `sessions/*.jsonl` 的 msg_id/delivery_status | — |
| A5-7 | 图片识图链路 | PASS | 测试副本日志：`[图片] 识图成功 … 下载=571ms 编码=74ms 识图=2931ms 描述=75字`，随后正常回复 | 私聊图片入站→识图→回复全通 |
| A5-8 | 模型池无需因通道变化修改 | PASS | 测试副本用同一 `llm_pool`/网关，未改模型配置 | 仅注入 .env 环境变量，不复制密钥文件 |

## B. 阶段 B（headless 纯协议）

| ID | 项目 | 结果 | 证据 | 说明 |
|---|---|---|---|---|
| B-1 | 首次登录 | PASS（人工扫码 1 次） | `state/trial_timeline.json` | 二维码文件 + 用户扫码；手机端选“下次登录无需手机确认” |
| B-2 | 客户端形态 | PASS | LLBot stdout | QQ 侧显示 linux 版本；**无 QQ 客户端进程**（试验进程 0 个） |
| B-3 | 核心 action 复测 | PASS | `logs/stageB/evidence_actions_B.json` | 登录/状态/版本/私聊/群/历史/get_msg/成员/好友/图片/顺序/超时 全部 ok |
| B-4 | 顺序 | PASS | 同上（seq 475→478 与发送顺序一致） | — |
| B-5 | 入站字段（私聊 + 群 @） | PASS（同样有 @ name 坑） | `logs/stageB/probe/events.jsonl` | CQ at 仍带 `name=`；同一补丁适用 |
| B-6 | 重启快速登录 | PASS | `trial_timeline.json` restart_1：**4s 登录，无二维码** | 依赖 `qq-session-2668851638.json` |
| B-7 | WS 断开/接收端重启恢复 | PASS | 关闭 ch0 → 2s 重连；接收端重启 → 两通道立即重连 | — |
| B-8 | 登录失效/KickedOffLine/静默离线 | UNKNOWN | — | 观察窗仅数十分钟，未出现；不能外推 |
| B-9 | 设备验证 | 有（手机确认） | 用户选择“下次登录无需手机确认” | 记录为人工干预 1 次的一部分 |
| B-10 | 无头稳定性 | SOAK_PENDING | — | 官方警告“无头模式有掉线风险，号体质不佳的慎用”；短时无故障≠稳定 |

## C. 稳定性观察

| 模式 | 观察窗 | 掉线 | 人工干预 | 结论 |
|---|---|---|---|---|
| 有头 PMHQ | 20:38–21:26（约 48 分钟） | 0 次 KickedOffLine（含一次主动重启） | 扫码 1 次 + 快速登录自动恢复 1 次 | SOAK_PENDING |
| 无头直连 | 21:30–21:37（约 7 分钟） | 0 次 | 扫码 1 次 + 快速登录 1 次 | SOAK_PENDING |

> 两个窗口都远小于 24–72h 的建议观察期；**不得**把“短时无掉线”写成稳定性通过。
