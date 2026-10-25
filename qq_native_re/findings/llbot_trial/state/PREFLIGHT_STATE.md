# LLBot 试验 — 只读预检状态快照

- 采集时间：2026-10-24 19:35 ~ 19:45（Asia/Shanghai）
- 采集人：Lead（本机只读检查；备份见 `patches/llbot_trial_20261024-194032/`）
- 用途：进入真实账号测试前的现状基线；任何结论以本文件 + `manifest.json` + 日志证据为准。

## 1. 账号与环境

| 项 | 值 | 证据 |
|---|---|---|
| 机器人账号 | 2668851638（`BOT_QQ`） | `scripts/commands/_state.py` |
| 私聊白名单 | 个人号 1 个（完整号见 `config/private_chat.json`，报告中不回写） | `config/private_chat.json` |
| 群 bot 版本 | 2.5.0 | `scripts/commands/_state.py` |
| 私聊 bot 版本 | 0.0.1 | `scripts/private_chat/config.py` |
| venv Python | 3.14.3 | `venv\Scripts\python.exe -V` |
| 官方 NTQQ（个人号） | `E:\Program Files\Tencent\QQNT\QQ.exe` 9.9.36.53644 | 文件版本信息 |
| NapCat 运行时 | 9.9.26-44498（目录名）；`napcat.mjs` SHA256 `F80E4BE3…`（3,130,951 B，2026-10-12 修改，含既有自愈补丁） | 目录 + `Get-FileHash` |
| NapCat 主程序版本 | 4.18.38（决策 011 记录；本次日志未重新打印版本行） | `decisions/011_r3_success.md` |
| 系统 | Windows，普通用户 `shiga`（非管理员） | `whoami` / `IsInRole(Administrator)=False` |

## 2. 当前 Python 服务与端口（采集时）

| 端口 | 用途 | PID（父→子） | 启动时间 | 命令行 |
|---|---|---|---|---|
| 8080 | 群 bot 反向 WS 服务端 | 15808 → 11088 | 2026-10-22 07:23:59 | `"E:\QQbot\venv\Scripts\python.exe" reverse_bot.py` |
| 8081 | 私聊 bot 反向 WS 服务端 | 74780 → 80964 | 2026-10-24 13:11:20 | `"E:\QQbot\venv\Scripts\python.exe" private_bot.py` |
| 8090 | 本地模型网关（保留，不属本轮范围） | 57332 | 2026-10-16 17:40:30 | `D:\python\python.exe server.py` |

- `netstat -ano`：8080/8081 仅 `LISTENING`，**没有** ESTABLISHED 连接 ⇒ 当前无 OneBot 客户端接入。
- 另有 `harmonyos_dev_mcp` 等无关 Python 进程；不属本机 QQbot，不处理。

## 3. NapCat / QQ 当前状态（关键）

| 判定 | 证据 |
|---|---|
| NapCat 协议端**当前未运行** | 无 `NapCatWinBootMain.exe` 进程；无 `E:\QQbot\NapCat\...` 路径的 QQ 进程；NapCat WebUI 端口 6099 无监听 |
| 机器人账号**当前离线** | 私聊 bot 日志 `19:16:21 NapCat 连接断开`；群 bot `[自愈]` 19:18 起报“账号已离线 N 分钟”；NapCat 最新日志 19:11:05 后无写入 |
| 离线前已有异常 | NapCat 日志 18:35 起多次 `网络连接异常`（result 1006514），私聊 `send_private_msg` 回执 `retcode=1200`（failed，非 unknown） |
| 个人 QQ | 8 个 `QQ.exe` 进程全部位于 `E:\Program Files\Tencent\QQNT\`，父进程 58860；未发现与 NapCat 混用。**本轮不触碰**。 |

> 结论（预检）：阶段 A 的前置条件“NapCat 不再登录机器人账号”在采集时已满足；但仍需用户确认不是自己正在准备登录。

## 4. 真实配置（不要只信旧文档）

### 4.1 双适配器状态（历史变量，本轮**不清理**）

`NapCat\...\resources\app\napcat\config\`：

- `onebot11_2668851638.json`：两个反向 WS 客户端都 `enable=true`
  - `Connect-to-NoneBot2` → `ws://127.0.0.1:8080`，token 存在（16 字符，值不记录），`messagePostFormat=array`，`debug=true`，重连 30s
  - `Connect-to-Private-8081` → `ws://127.0.0.1:8081`，token 为空，`messagePostFormat=array`，重连 30s
- `napcat_protocol_2668851638.json`：文件里写 `"enabled": true`，但 NapCat 运行时按 `enable` 字段解析（日志显示加载为 `enable:false`）⇒ **该协议适配器实际处于关闭状态**。这是已知历史变量，本轮原样保留、不修改。

### 4.2 私聊配置

`config/private_chat.json`：`port=8081`，`allowed_users` 仅 1 人，`data_dir=data/private`，`reply.pipeline=structured`，`proactive.enabled=true`（`dry_run=false`），`history.on_connect=true`。

### 4.3 已启用群（`config/group_features.json` 白名单语义）

| 群号 | 群名 | 备注 |
|---|---|---|
| 284517232 | 碧油鸡乐土（转正版） | 全功能（含 sticker/poke/reaction） |
| 1055881417 | 碧油鸡乐土(二阶英灵殿） | sticker=false |
| 1102194840 | （空） | 全功能 |

> 本轮未获用户指定测试群前，不向任何群发可见消息。

### 4.4 NapCat 启动路径（既有安全启动方式）

`tools\start_napcat_verbose.bat`（双击运行）：设置 `NAPCAT_ENABLE_VERBOSE_LOG=1`、`NAPCAT_QUICK_ACCOUNT=2668851638`，以账号参数调 `NapCatWinBootMain.exe`；只探测密码回退变量是否存在、从不回显。
- 当前 `HKCU\Environment`：`NAPCAT_QUICK_ACCOUNT` 存在、`NAPCAT_QUICK_PASSWORD` **存在**（只记录存在性，不读取/不记录值）、`NAPCAT_QUICK_PASSWORD_MD5` 不存在。
- 无管理员权限要求；个人号 784427550 不得出现在该脚本或本试验中。

## 5. 其他环境事实

- 工作区 `E:\QQbot` 的 `.git` 为只读（Administrators only），本轮不执行任何 git 写操作；测试前 `git status --short` 已有大量既存改动/未跟踪文件，属正常基线，不作为本轮产出。
- 网络：本机无 7890/15715 代理监听；系统 schannel 直连 GitHub 失败（`SEC_E_NO_CREDENTIALS`），但 **venv Python + OpenSSL 可直连** GitHub API（已验证 `HTTP 200`，LLBot latest `v8.3.2`）。
- 沙箱：本轮会话为 `danger-full-access`；`E:\QQbot-trials\` 已创建（`llbot/`、`data/`、`runtime/`）。
- 进程枚举：权限放开后 `tasklist` 可用（371 进程）；仍只用 **PID 精确停止**，禁止 `taskkill /IM`、`Stop-Process -Name QQ`。

## 6. 未确认项（需用户确认）

1. 机器人账号 2668851638 当前是否在**任何**客户端（含手机/官方 QQNT）保持登录——决定 LLBot 首次登录是否会互踢。
2. 允许本轮使用的**测试群**（群号）——验证 send_group_msg / 群聊被动回复必须有明确目标。
3. 是否允许向私聊白名单用户（即用户本人）发送测试消息（A2 的 send_private_msg、A5 私聊业务验证）。
4. 是否允许在试验期间**暂停生产 Python bot**（8080/8081），以防止 LLBot 误配时事件流入生产逻辑；回滚时按 PID 恢复。
5. 人工扫码窗口：LLBot 有头模式首次登录预计需要用户扫码（bot 账号手机 QQ）。

## 7. 本轮边界（复述）

- 不改生产 `config/`、不改 NapCat 配置、不动 `data/private`（仅哈希快照）。
- 不运行 NapCat / 不启动第二个客户端登录同一账号。
- LLBot 只使用官方 Release / 官方文档；安装到 `E:\QQbot-trials\llbot\`。
- 证据与报告写 `qq_native_re/findings/llbot_trial/`；备份写 `qq_native_re/patches/llbot_trial_20261024-194032/`。
