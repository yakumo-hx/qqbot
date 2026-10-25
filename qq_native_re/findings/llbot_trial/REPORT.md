# LLBot（LuckyLilliaBot）通道可行性与兼容性试验报告

- 试验日期：2026-10-24 19:20 ~ 21:50（Asia/Shanghai）
- 执行：Lead（DSH 会话），按用户受控试验要求执行
- 结论一句话：**CONDITIONAL_GO（有条件通过）** — 现有 OneBot 11 业务可在 LLBot 上跑通，但需要修一处 `@` 识别不兼容，且**稳定性只到 SOAK_PENDING**，不能称为“完全兼容”。

> 证据根目录：`E:\QQbot\qq_native_re\findings\llbot_trial\`
> 备份根目录：`E:\QQbot\qq_native_re\patches\llbot_trial_20261024-194032\`
> 测试矩阵：`TEST_MATRIX.md`；回滚手册/记录：`ROLLBACK.md`

---

## 1. 测试目标与范围

**目标（按用户定义）**
1. 阶段 A：验证现有 OneBot 11 业务能否跑在 LLBot 的 PMHQ 有头模式上；
2. 阶段 B：在 A 核心通过后，仅切换 headless，验证不依赖 QQ 客户端时的登录/恢复/业务；
3. 全程受控、留证据、可回滚；不做生产迁移、不重构。

**实际执行**
- 只读预检、备份/manifest/回滚手册；
- 官方 LLBot v8.3.2 下载、哈希校验、隔离安装；
- 自建隔离 OneBot 反向 WS 探针（18080/18081/18082）+ 自测；
- 阶段 A1–A5（有头 PMHQ）实测；
- 阶段 B（headless 直连）核心复测；
- 回滚到 NapCat + 生产 Python，并做收发验收。

**未执行 / 未完成**
- 24–72 小时稳定性观察（SOAK_PENDING）；
- 断网/断电类故障注入（影响整机与用户网络，超安全边界）；
- 表情回应、戳一戳、文件发送等**可见扩展操作**（未获准，NOT_CLEARED）；
- 主动消息竞争（测试副本按要求关闭 proactive，NOT_RUN）；
- Lagrange V2 / Milky 等其他路线（按约束不扩大范围）。

---

## 2. 版本与下载来源

| 组件 | 版本 | 说明 |
|---|---|---|
| LLBot（LuckyLilliaBot） | **8.3.2**（2026-10-12 release） | CLI 包，含 `llbot.js 8.3.2` + `pmhq 9.0.4` |
| 官方 NTQQ（有头用） | **9.9.36-53644** | `E:\Program Files\Tencent\QQNT\QQ.exe`，未修改 |
| NapCat（基线） | 引擎 4.18.38 / 运行时 9.9.26-44498 | 生产现状，试验期间未改 |
| Python | 3.14.3（venv） | 生产/测试副本共用 |
| 群 bot | 2.5.0 | `reverse_bot.py` |
| 私聊 bot | 0.0.1 | `private_bot.py`（structured pipeline） |

**下载（仅官方 GitHub Releases）**
- Release：https://github.com/LLOneBot/LuckyLilliaBot/releases/tag/v8.3.2
- 资产与本地 SHA256（与 GitHub API `digest` **逐一吻合**）：
  - `LuckyLillia-Desktop-win-x64.zip` 65,767,668 B
    `38fde3cc61d76fc9bfcfabcc7b0b362a5c938016d8b57a1665d4173492d3981f`
  - `LuckyLillia-CLI-win-x64.zip` 95,713,510 B
    `3b29f2bdaafbbb1e0e494e42d81da7566aeb16114e9eb42a5d168c59dbcadcb7` ← **实际安装**
- 下载记录：`E:\QQbot-trials\download\download_hashes.json`、`release_v8.3.2.json`
- 网络说明：直连 GitHub Release 资产不稳定；经用户提供的本机代理 127.0.0.1:15715 完成下载，未使用任何第三方镜像。

**官方文档依据**
- https://luckylillia.com/guide/introduction 、`/guide/choice_install`、`/guide/config`、`/guide/faq`
- PMHQ 配置：https://github.com/linyuchen/PMHQ/blob/main/doc/config.md（官方文档内链）
- 无头官方警告原文：**“无头模式有掉线风险，号体质不佳的慎用”**

---

## 3. 隔离、账号与生产安全

- 机器人账号 **2668851638** 全程只由 LLBot 登录；有头/无头**不并行**：切换前先停旧进程并确认无残留。
- 个人 QQ **784427550** 未被操作；其 8 个官方 QQNT 进程全程存活（回滚后复核）。
- 生产 NapCat 在阶段 A 前已处于离线（19:16 后自然停），试验期间**未启动**，回滚时才恢复。
- 生产 Python bot 在试验期间按用户批准**暂停**；8080/8081 无监听，直到回滚恢复。
- LLBot 配置：仅 OneBot 11 反向 WS；Milky/Satori 显式关闭；WebUI 仅 `127.0.0.1:3080`；AccessToken/Token 均空（仅本机回环）。
- 测试副本 `E:\QQbot-trials\qqbot_test` 使用独立 `data/private_test`，未读写生产 `data/private`。
- **生产文件改动：0**（`manifest.json` 的 44 个备份项回滚后逐项哈希复核，全部一致）。

---

## 4. 有头 / 无头配置差异

| 项 | 阶段 A（有头 PMHQ） | 阶段 B（headless） |
|---|---|---|
| 启动命令 | `LuckyLillia.exe --qq=2668851638 --work-dir=E:\QQbot-trials\qq_workdir` | `LuckyLillia.exe --headless --qq=2668851638` |
| QQ 客户端 | 官方 NTQQ 9.9.36 由 PMHQ 拉起（debugger 模式，0 DLL 注入） | **无 QQ 客户端进程**（LLBot 直连协议） |
| 登录协议 | NTQQ 本机协议 | `--protocol=linux`（默认；QQ 侧显示 linux 版本） |
| 首次登录 | 人工扫码 1 次 | 人工扫码 1 次 + 手机确认（用户选“下次登录无需手机确认”） |
| 重启登录 | 快速登录 **22s**，无扫码 | 会话文件快速登录 **4s**，无扫码 |
| 配置文件 | `bin/llbot/data/config_2668851638.json`（继承 `default_config.json`） | 同一份配置 |
| OneBot 连接 | 同一 3 条 ws-reverse（18080/18081/18082） | 同左 |

**LLBot 侧配置要点**（完整备份：`patches/.../llbot_config/`）
- `ob11.enable=true`；`connect` 全为 `ws-reverse`，只指向 `127.0.0.1` 隔离端口；
- `messageFormat=array`；`reportSelfMessage=false`；`reportOfflineMessage=false`；
- `webui` 127.0.0.1:3080；`milky/satori` false；`autoDeleteFile=false`。
- 观察通道（18082）是为了取证追加的第 3 条反向 WS，属于试验配置，不影响生产语义。

---

## 5. 核心 API 兼容性（有头实测，无头复测一致）

| 能力 | 结果 | 关键证据 |
|---|---|---|
| 反向 WS / 心跳 / lifecycle | PASS | `logs/stageA/probe/events.jsonl` |
| get_login_info / get_status / get_version_info | PASS | `logs/stageA/results.jsonl` |
| send_private_msg / send_group_msg | PASS | `logs/stageA/evidence_actions.json` |
| get_msg / get_friend_msg_history / get_group_msg_history | PASS | 同上 |
| get_group_member_list / get_group_info / get_group_list / get_friend_list | PASS | 同上 |
| 图片接收 + 下载 | PASS | `logs/stageA/probe/images.jsonl` |
| 图片发送（base64） | PASS（含 8.1MB 大图） | `logs/stageA/evidence_faults.json` |
| 回复 + @（CQ 字符串发送） | PASS | `evidence_actions.json` → get_msg msg_types=[reply,at,text] |
| 两个独立 WS 通道 | PASS（互不覆盖） | 同 message_id 两通道各 1 次，时间差 1–3ms |
| action 超时 unknown | PASS（unknown 不重发，但消息可能已送达） | `evidence_faults.json` |
| 群公告 `get_group_notice`（扩展） | **FAIL retcode=1404** | `evidence_actions.json` |
| 表情回应 / 戳一戳 / 文件（扩展） | NOT_CLEARED / NOT_RUN | 未产生可见操作 |

**Phase B 复测**：`logs/stageB/evidence_actions_B.json` —— 上述核心 action 全部 ok；顺序无乱序；图片发送 ok；超时 unknown ok。

---

## 6. 两通道、历史、图片、回执、乱序、重复

- **双通道**：同一事件在两通道 payload 等价（消息字段一致），时间差 1–3ms；ch0 因 `debug=true` 帧体积大 3–8 倍（生产配置里 8080 也是 debug=true，建议迁 LLBot 时关掉以减少带宽）。
- **历史**：`get_friend_msg_history` / `get_group_msg_history` 返回 `messages[]`，字段含 `raw` 与 `raw_pb`（体积大）；私聊测试副本首次拉 20 条全部新增，重连后 20 条全部去重。
- **图片**：入站图片 URL 直链 HTTP 200（92228 B，image/jpeg），可下载；发送 base64 图片成功，8.1MB 大图 5.3s。
- **回执**：`retcode=0` + `message_id`；私聊 attempts ledger 记录 `sent + msg_id`，多气泡逐气泡记录；超时记为 `unknown`，**未自动重发**。
- **乱序**：连续 6 条文本 + 图 + 文，历史 `message_seq` 446→453 与发送顺序完全一致；headless 复测 475→478 同样一致。
- **重复**：29 个 message_id、58 行（双通道）**0 重复**；WS 断开/接收端重启后无补报重复。

---

## 7. 有头 vs 无头：登录与恢复对比

| 指标 | 有头 PMHQ | 无头 headless |
|---|---|---|
| 首次登录 | 人工扫码 1 次 | 人工扫码 1 次 + 手机确认 |
| 重启后登录 | 快速登录 22s，无扫码 | 会话快速登录 4s，无扫码 |
| QQ 客户端 | 需要（官方 NTQQ） | 不需要（0 个 QQ 进程） |
| 启动资源/复杂度 | 高（QQ + PMHQ + LLBot） | 低（仅 LLBot） |
| 异常显示 | QQ 弹出“错误报告”窗口 1 次（wrapper.node，见 §9） | 未见 |
| 短时掉线 | 0 次（48 分钟窗口） | 0 次（7 分钟窗口） |
| 长期稳定性 | **SOAK_PENDING** | **SOAK_PENDING** |

> 两次“快速登录”都是账号已有会话/缓存的场景；**不能**据此推断长期掉线后仍能免扫码。官方已警告无头掉线风险。

---

## 8. 与 NapCat 基线的公平比较

基线：`baseline/BASELINE.md`（最近 7 天，2026-10-18 ~ 10-24）
- NapCat：KickedOffLine **32 次**（按日 7/5/8/5/4/2/1），「登录已失效」32 行，1006514 发送错误 20 行；
- WS 抖动样本 63 次：**<60s 34 次（中位 24s）**、60–600s 14 次、>600s 15 次（最大 11 小时，含整机休眠，无法与掉线拆分）；
- 私聊 `retcode=1200` 明确失败 7 次（failed，不重发）。

**公平性说明**
- LLBot 两个观察窗合计不到 1 小时，而 NapCat 是 7 天；**样本量不可比**，只能做“功能/行为”对照，不能做“稳定性优劣”结论。
- 试验期间生产账号被主动停止约 1 小时，已从 NapCat 基线中剔除。
- 结论：**无证据表明 LLBot 更稳定或不稳定**；需要同账号顺序、24–72h 的 soak 才有比较意义。

---

## 9. 失败、阻塞、不确定项（全部保留）

1. **@ 识别不兼容（重要，已最小适配验证）**
   - LLBot 群消息 CQ：`[CQ:at,qq=2668851638,name=Deepseek]`；
   - 生产 `reverse_bot.py:369` 用字面量 `[CQ:at,qq=2668851638]` 子串判断 → 漏判 @bot，群被动回复完全失效；
   - 测试副本改为正则 `\[CQ:at,qq=…(?:,[^\]]*)?\]` 后，群 @ 回复恢复正常；
   - 补丁与回滚：`patches/llbot_trial_20261024-194032/test_copy_at_patch/`（**生产未改**）。
2. **QQ 错误弹窗 / 崩溃（UNKNOWN 归因）**
   - 错误：`QQ 遇到错误…`，错误号 `4213eee1-5f47-462e-a1b0-ee74db36a74f`，模块 `wrapper.node`；
   - Crashpad 时间 **21:27:10** 与“强制结束试验 QQ 进程树”的时间重合，**无法排除是测试强制终止诱发**；
   - 证据：`logs/stageA/qq_crash_dialog_wrapper_node.png`、`qq_crash_4213eee1.txt`、`.dmp`（5.2MB，敏感，仅本机）。
3. **群公告接口** `get_group_notice` retcode=1404（扩展项，不影响核心）。
4. **稳定性 SOAK_PENDING**：两模式都没有 24–72h 数据；无头官方风险警告未解除。
5. **人工扫码次数**：NapCat 基线中 UNKNOWN（“二维码/快速登录”日志行无法可靠区分自动/人工）；LLBot 侧为有头 1 次、无头 1 次（用户确认）。
6. **主动消息竞争 / 可见扩展操作**：NOT_RUN / NOT_CLEARED（安全边界）。
7. **生产私聊历史被正常回填**：回滚后生产 `private_bot` 启动时按既有逻辑 `get_friend_msg_history` 拉取 20 条，其中包含试验期间产生的测试消息 → `data/private/784427550/sessions/2026-10-24.jsonl` 等文件变化**可解释**（不是试验直接写入，而是恢复生产后的正常历史回填）；未删除/覆盖既有数据。

---

## 10. 回滚验证结果

- 生产文件：**0 改动**（44 个备份项复核全部一致；见 `manifest.json`）。
- 生产服务恢复：
  - 8080/8081：LISTENING + 来自 NapCat 的 ESTABLISHED（21:43:33）；
  - 群 bot 日志：`⚡ 客户端已连接`；私聊 bot：`NapCat 已连接` + `[历史] 拉取 20 条`；
  - 端到端：私聊 `【回滚验证】在吗` → 21:48:40 回复成功（attempts `sent`，msg_id `2010684556`）；群 `【回滚验证】@bot` → 21:49:09 群回复成功（LLBot/NapCat 正常）；
  - NapCat：引擎 4.18.38，WebUI 6099 监听，账号在线（历史拉取成功即证明）。
- 进程清理：LLBot/PMHQ/试验 QQ/测试副本/探针均已停止；个人 QQ 的 8 个子进程全程未受影响。
- **回滚结论：PASS（已恢复）**，但有一个可解释的数据回填（见 §9.7）。

---

## 11. 最终判定

**CONDITIONAL_GO（有条件通过，SOAK_PENDING）**

- 支持理由：
  - 核心 OneBot 11 能力在**有头与无头**均可工作（登录、状态、私聊/群发送、历史、get_msg、成员、图片收发、回复@、双通道、超时 unknown、重连、顺序、去重）；
  - 现有私聊结构化业务（多气泡、逐气泡回执、修订/合并、历史补拉、图片识图）在 LLBot 上实测通过；
  - 无头模式可完全脱离 QQ 客户端运行，重启快速登录 4s（有会话时）。
- 不能给 GO 的原因：
  1. 必须修 `@` 识别（LLBot CQ 带 `name=`），否则群被动回复失效；
  2. 未完成 24–72h 稳定性观察（官方明确警告无头掉线风险）；
  3. 有头模式出现过一次 QQ 崩溃弹窗（归因 UNKNOWN，需观察）；
  4. 扩展接口（群公告）有失败，可见扩展操作未验证。
- 若在 soak 中出现：KickedOffLine 频繁、静默离线、重复发送、账号安全警告 ⇒ 立即停止并回滚。

**后续是否值得进入 Lagrange V2 / Milky？**
- 按用户约束，**本轮不自动扩大范围**。建议顺序：先完成 LLBot 24–72h soak + 修 @ 兼容；若 LLBot 的无头掉线率仍明显高于 NapCat，再评估 Lagrange V2 / Milky 作为协议端候选（需另开试验与独立回滚点）。

---

## 12. 关键证据索引

| 证据 | 路径（相对 `findings/llbot_trial/`） |
|---|---|
| 测试矩阵 | `TEST_MATRIX.md` |
| 有头动作/顺序/图片证据 | `logs/stageA/evidence_actions.json` |
| 有头故障注入证据 | `logs/stageA/evidence_faults.json` |
| 有头原始事件/回执（双通道） | `logs/stageA/probe/events.jsonl`、`results.jsonl` |
| 第三通道观察（@ 原始 CQ） | `logs/stageA/observer/events.jsonl` |
| 无头动作/顺序证据 | `logs/stageB/evidence_actions_B.json` |
| 无头原始事件 | `logs/stageB/probe/events.jsonl` |
| 时间线与登录/恢复记录 | `state/trial_timeline.json` |
| NapCat 基线 | `baseline/BASELINE.md`、`baseline.json` |
| 预检状态 | `state/PREFLIGHT_STATE.md` |
| 备份清单（含哈希） | `manifest.json`（备份本体在 `patches/llbot_trial_20261024-194032/`） |
| 测试副本最小适配 | `logs/copy_adaptation.json`、`patches/.../test_copy_at_patch/` |
| QQ 崩溃证据 | `logs/stageA/qq_crash_dialog_wrapper_node.png`、`qq_crash_4213eee1.txt/.dmp` |
| 测试脚本 | `scripts/`（探针、驱动、自测、基线、补丁） |

**脱敏声明**：本报告及 manifest 不含密码、AccessToken、QQ 登录凭据、Cookie、AppSecret；不含完整私聊内容；Crashpad dmp/txt 与备份中的 NapCat token 文件仅存本机、未外发。
