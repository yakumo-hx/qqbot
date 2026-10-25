# LLBot 试验 — 回滚手册（ROLLBACK）

> 本文件在**任何真实账号测试之前**写定。若试验中止或结束，按本文件恢复 NapCat 基线。
> 备份根目录：`E:\QQbot\qq_native_re\patches\llbot_trial_20261024-194032\`
> 清单：`E:\QQbot\qq_native_re\findings\llbot_trial\manifest.json`（含每个文件的 SHA256）
> 纪律：**只用 PID 精确停止进程**；禁止 `taskkill /IM QQ.exe`、`Stop-Process -Name QQ` 等按名字全局操作。

## 0. 一眼看清：本轮到底改了哪些生产文件

- 预检阶段：**0 个**。只复制（备份）与只读哈希，未写入任何生产文件、未改 NapCat 配置、未动 `data/private`。
- 测试阶段（预计）：生产侧仍为 **0 个**；LLBot 配置只存在于 `E:\QQbot-trials\llbot\`；业务验证只用测试副本。
- 若出现意外改动，按下文第 3 节按文件恢复，并先记录“为什么会被改”。

## 1. 停止 LLBot（候选协议端）

### 1.1 识别（先确认身份，再停止）

LLBot 启动时由试验脚本写入 PID 记录：
`E:\QQbot-trials\state\llbot.pid.json`（PID、可执行文件路径、启动时间、监听端口）。

人工复核（PowerShell）：

```powershell
$pidRec = Get-Content 'E:\QQbot-trials\state\llbot.pid.json' -Raw | ConvertFrom-Json
Get-CimInstance Win32_Process -Filter "ProcessId=$($pidRec.pid)" |
  Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine
# 必须满足：ExecutablePath 以 E:\QQbot-trials\ 开头，否则不要停
```

### 1.2 停止顺序

1. 先停 LLBot 主进程：`Stop-Process -Id <确认过的 pid> -Force`
2. 等 5 秒，确认其子进程（LLBot 拉起的 QQ）是否仍在：
   ```powershell
   Get-CimInstance Win32_Process |
     Where-Object { $_.ParentProcessId -eq <llbot_pid> } |
     Select-Object ProcessId,Name,ExecutablePath
   ```
3. 只对**路径属于试验目录**（`E:\QQbot-trials\...`）且父进程为 LLBot 的 QQ 子进程执行 `Stop-Process -Id <pid> -Force`。
   **绝不要**停止 `E:\Program Files\Tencent\QQNT\QQ.exe`（个人 QQ）。
4. 验证：LLBot 端口关闭；`E:\QQbot-trials\state\llbot.pid.json` 中的 PID 已不存在。
5. 确认机器人账号不再被 LLBot 持有：再次检查 `netstat -ano | findstr :18080 :18081` 无 ESTABLISHED；LLBot 日志最后一行是退出/关闭。

### 1.3 若无法确认进程身份

立即停止试验（不 kill），保留现场，报告“无法安全停止候选协议端”。这不是失败，是纪律。

## 2. 恢复文件（仅当对应文件真的被改过）

```powershell
$bk = 'E:\QQbot\qq_native_re\patches\llbot_trial_20261024-194032'
$m  = Get-Content 'E:\QQbot\qq_native_re\findings\llbot_trial\manifest.json' -Raw | ConvertFrom-Json

# 1) 先检查差异（不做恢复）
$m.backup_entries | ForEach-Object {
  if (Test-Path $_.original_path) {
    $now = (Get-FileHash $_.original_path -Algorithm SHA256).Hash
    if ($now -ne $_.sha256) { "CHANGED: $($_.original_path)" }
  } else { "MISSING: $($_.original_path)" }
}

# 2) 只恢复被改动的具体文件（示例：config/private_chat.json）
Copy-Item -LiteralPath "$bk\config\private_chat.json" `
          -Destination 'E:\QQbot\config\private_chat.json' -Force
# 3) 恢复后复核哈希
(Get-FileHash 'E:\QQbot\config\private_chat.json' -Algorithm SHA256).Hash
```

- NapCat 配置同理：备份在 `$bk\napcat_config\`，**必须先在 NapCat 停止状态**下恢复，再启动 NapCat。
- `sensitive=true` 的备份（webui.json / onebot11 / passkey 等）含本地 token，只能本机使用，禁止贴进报告或外发。

## 3. 绝不能“直接覆盖回滚”的文件（会丢真实数据）

以下文件在试验期间会因正常使用而增长/变化，覆盖旧快照 = 丢数据：

- `data/private/**`（私聊永久会话；本轮明确不读写）
- `logs/**`、NapCat `logs/**`、`native/napi2native_bypass.log`（历史日志与取证）
- `user_profiles.json`、`menu_data.json`、`data/roster.json`、`scripts/sign_in_data.json`（运行时画像/菜单/名册）
- `.env`（含密钥；本轮不备份也不修改）
- NapCat `cache/`、`config/plugins/` 下的运行态

处理原则：这些文件**只验证“未被本轮写入”**（对照 `manifest.integrity_snapshot` 的 SHA256），不要恢复。

## 4. 恢复 NapCat 与生产 bot

前置：LLBot 已停止（第 1 节），机器人账号无其他登录端。

1. 用**既有安全启动方式**启动 NapCat（双击，不要用命令行另造一套）：
   `E:\QQbot\tools\start_napcat_verbose.bat`
2. 等待 NapCat 快速登录；如需扫码，由用户人工完成（不要尝试绕过）。
3. 生产 bot 若在试验期间被暂停，按记录 PID 重新拉起（不要重复启动）：
   ```powershell
   cd E:\QQbot
   $env:PYTHONIOENCODING='utf-8'
   Start-Process -FilePath '.\venv\Scripts\python.exe' -ArgumentList 'reverse_bot.py' -WorkingDirectory 'E:\QQbot'
   Start-Process -FilePath '.\venv\Scripts\python.exe' -ArgumentList 'private_bot.py'  -WorkingDirectory 'E:\QQbot'
   ```
4. 验证（缺一不可）：
   - `netstat -ano | findstr ":8080 :8081"` → 两个 LISTENING + 来自 NapCat 的 ESTABLISHED
   - `logs\bot.YYYY-MM-DD.log`（或当前进程日志）出现 `⚡ 客户端已连接`
   - `logs\private_bot.YYYY-MM-DD.log` 出现 `NapCat 已连接` 与 `[历史] 拉取 ...`
   - `get_login_info` 等价证据：OneBot 事件流中出现 `self_id=2668851638` 的 meta_event（可用试验探针在**独立端口**验证，不得抢占 8080/8081）
   - 测试群/私聊白名单收发：需用户批准的测试目标，只发 1 条最小消息并核对回执
   - RPC 回执：私聊发送出现 `sent`（`attempts.jsonl` 无新增 unknown）
5. 记录恢复后的账号状态、WS 状态、时间戳；**回滚失败必须醒目标记 FAILED，不得写“已恢复”**。

## 5. 生产数据完整性校验（每次回滚必做）

```powershell
$m = Get-Content 'E:\QQbot\qq_native_re\findings\llbot_trial\manifest.json' -Raw | ConvertFrom-Json
$m.integrity_snapshot | ForEach-Object {
  if (Test-Path $_.path) {
    $now = (Get-FileHash $_.path -Algorithm SHA256).Hash
    if ($now -ne $_.sha256) { "CHANGED_SINCE_PREFLIGHT: $($_.path)" }
  } else { "MISSING: $($_.path)" }
}
```

- 预期：`data/private/**` 在试验期间**不应**变化（生产私聊进程未被 LLBot 接入）。
- `logs/**`、`user_profiles.json` 等允许变化，但必须能解释（例如生产 bot 仍在运行时的正常日志）。
- 出现无法解释的变化 ⇒ 立即停止并升级为事故记录。

## 6. 快速判定表

| 场景 | 动作 |
|---|---|
| 试验正常结束，生产文件 0 改动 | 只停 LLBot、按第 4 节恢复 NapCat，无需恢复文件 |
| LLBot 配置写到了生产 8080/8081 | 立即停 LLBot；检查生产日志是否被触发；记录事件；必要时按第 3 节恢复配置 |
| 个人 QQ 被退出/误杀 | 立即停止一切试验；报告事故；不自行重新登录个人号 |
| 同账号多端互踢且无法确认来源 | 立即停止；保持离线；记录时间线；交由用户决定 |
| 机器人账号被冻结/安全警告 | 立即停止；不回滚配置、不加新变量；保留所有日志 |
| 无法确认 LLBot 进程身份 | 不 kill；按第 1.3 节处理 |
| 备份/回滚路径失效 | 停止试验，先修复备份（重新备份当前状态并记录差异）再继续 |

---

## 7. 本次实际回滚记录（2026-10-24 21:37 ~ 21:50）

> 本节为**执行记录**（预检时写的前 6 节仍有效）。结论：**PASS（已恢复）**。

### 7.1 停止候选协议端
- 有头 LLBot：停 launcher PID 95084；残留 PMHQ/node 随之退出；试验 QQ 树（父 86124）按 PID 精确停止。
- 无头 LLBot：停 launcher PID 85512；无 QQ 客户端进程（headless 特性）。
- 停止后复核：`E:\QQbot-trials\*` 无任何进程；`tasklist` 中个人 QQ（父 58860）8 个子进程完好。

### 7.2 恢复生产
1. 启动生产 bot（**用 WMI `Win32_Process.Create` 分离启动**，避免被当前工具会话的 job 回收）：
   - `reverse_bot.py` → 父 103392 / 子 84588（监听 8080）
   - `private_bot.py` → 父 96888 / 子 94940（监听 8081）
2. 用既有安全脚本启动 NapCat：
   `E:\QQbot\tools\start_napcat_verbose.bat`（捕获输出运行，stdin=NUL）
   - NapCatWinBootMain 83620 → QQ 91064；WebUI 6099 由 67980 监听
3. 连接：`8080/8081` LISTENING + NapCat→bot ESTABLISHED（21:43:33）。

### 7.3 回滚验收（逐项）
| 检查 | 结果 |
|---|---|
| 8080/8081 监听 | PASS |
| NapCat→8080/8081 ESTABLISHED | PASS（127.0.0.1:61917 / 61918） |
| 群 bot 日志 `⚡ 客户端已连接` | PASS |
| 私聊 bot `NapCat 已连接` + `[历史] 拉取 20 条` | PASS（账号在线、get_friend_msg_history 成功） |
| 私聊白名单收发 | PASS：`【回滚验证】在吗` → 21:48:40 回复，attempts `sent` msg_id 2010684556 |
| 测试群收发 | PASS：群 284517232 `@bot 【回滚验证】` → 21:49:09 群回复（带 @） |
| RPC 回执 | PASS（send 回执 retcode ok；attempts 落盘） |
| 生产配置未变 | PASS（44 个备份项哈希复核全部一致） |
| 个人 QQ 未受影响 | PASS（8 个 QQNT 子进程全程存活） |

### 7.4 已知可解释的数据变化
- `data/private/784427550/sessions/2026-10-24.jsonl`、`attempts.jsonl`、`profile/state/style/dialogue_state` 有变化：
  原因是生产 `private_bot` 恢复后按既有逻辑拉取 20 条 QQ 历史（其中含试验期间产生的测试消息），并正常回复了回滚验收消息。
  **不是**试验直接写入，也**没有**删除或覆盖既有数据；其余日期 session 哈希未变。
- `logs/**` 有变化：生产 bot 恢复后正常写日志。

### 7.5 回滚失败项
- 无。唯一操作瑕疵：第一次用 `Start-Process` 启动生产 bot 时，进程被工具会话的 job 回收（已用 WMI 分离方式重启并验证）；NapCat 第一次启动因 shell 等待未真正拉起，已用捕获输出的 `cmd /c` 方式重跑成功。
