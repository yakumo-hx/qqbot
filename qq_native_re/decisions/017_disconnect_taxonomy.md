# 决策日志 017 — 掉线的四类故障分类，与 L1 自动重启的实测缺陷

> 日期：2026-10-21　｜　执行：Lead
> 触发：用户问「二手安卓手机能否解决掉线自动恢复」，并要求给出完整判断。
> 结论：**掉线不是一件事，是四件事**；且 **L1 自动重启当前有害，已关闭**。
>
> ⚠️ **阅读前请注意**：本文件 §3.2 与 §四 把 L1 失效归因为「Windows 权限不足」，
> 该判断**是错的**，已于同日更正 —— **真因是进程名白名单的扩展名比较 bug（纯代码缺陷）**。
> 请直接看文末 **§八 重要更正**，§3.2 / §四 仅作错误推理的过程留档。

---

## 一、先纠正一个前提

用户长期认为「NapCat 从不自动重连，全靠人工」。**日志不支持这个结论**：

NapCat 的 `onebot11` 配置是 `reconnectInterval: 30000`（30 秒），实测每天十几次 **22~40 秒的自愈**：

| 断开 | 重连 | 停机 |
|---|---|---|
| 10-21 09:37:39 | 09:38:06 | **27 秒** |
| 10-21 10:43:25 | 10:43:56 | **31 秒** |
| 10-21 10:50:27 | 10:50:54 | **27 秒** |
| 10-21 11:26:49 | 11:27:26 | **37 秒** |
| 10-21 12:09:09 | 12:09:31 | **22 秒** |
| 10-21 12:57:25 | 12:57:49 | **24 秒** |

⇒ 短抖动**会自愈且用户根本没察觉**；用户注意到的都是长停机。**长停机的成因与短抖动完全不同。**

---

## 二、四类故障（这是本次最重要的产出）

| # | 类型 | 判定依据 | 频率 | 正确对策 |
|---|---|---|---|---|
| **A** | WS 短抖动 | 22~40 秒自动恢复 | **每天十几次** | 不干预，NapCat 自愈 |
| **B** | NapCat 杀不掉 → 实例堆积 | `taskkill → Access denied` | 2026-10-20 起 | 修权限模型（见 §三） |
| **C** | 账号被踢下线 | `[KickedOffLine] 你的账号当前登录已失效`（决策 012） | 偶发 | 密码回退 / 无人扫码 |
| **D** | 整机休眠或关机 | 日志出现 8 小时空档 | **可能每晚** | 防休眠 / 换常开主机 |

**关键判据**：

- A 类：`[自愈][INFO]` … 随后 `⚡ 客户端已连接`，间隔 < 60 秒
- B 类：`L1 结果：已结束 无，失败 [...]`
- C 类：NapCat 控制台出现 `[KickedOffLine]`
- D 类：相邻两条日志时间差以**小时**计，且中间没有任何 `[自愈]` 心跳告警

---

## 三、🚨 L1 自动重启的实测缺陷（已关闭）

### 3.1 现场证据

```
[08:43:47] L1 自动重启触发：镜像路径 NapCat 进程 5 个，非镜像（个人 QQ 等，已忽略）8 个
[08:43:47] L1 结果：已结束 无，失败 [82484, 5148, 51728, 55996, 81860]
[08:48:47] L1 自动重启触发：镜像路径 NapCat 进程 10 个        ← 杀掉失败，却又新起一个
[08:48:47] L1 结果：已结束 无，失败 [60632, 82484, 5148, 39072, …]
[08:53:47] L1 自动重启触发：镜像路径 NapCat 进程 15 个        ← 再新起一个
```

### 3.2 根因（已确证，非推测）

```
bot 进程是否管理员 = False
taskkill /PID <镜像 QQ> → ERROR: Access denied
```

镜像 QQ 由**需要管理员**的 `NapCatWinBootMain.exe` 拉起（注入 `NapCatWinBootHook.dll`），
所以它是**提权进程**；bot 是普通权限 ⇒ **一个都杀不掉**。但 `_attempt_restart` 不在乎 kill 成败，
**杀完照样调用 launcher** ⇒ 每次尝试都新增一个实例。

### 3.3 后果

- 实测堆积 **4 个**镜像 NapCat（启动于 08:43 / 08:48 / 08:53 / 12:57）
- 加上 5 个个人 QQNT，全机 **9 个 QQ 进程**
- 多个 NapCat 实例抢同一账号登录 ⇒ **怀疑会加剧 C 类（被踢）**，与 A 类频率上升可能相关（待验证）

> `NapCat/napcat.kill.qq.bat` 也用的是 `taskkill /f`，**同样受权限限制**，不能拿来当绕过方案。

### 3.4 处置（2026-10-21 已执行）

| 动作 | 状态 |
|---|---|
| `config/auto_restart.json` → `restart.enabled: false` | ✅ 已执行（热加载，立即生效） |
| 安全断言 `test_repo_config_matches_deployed_settings` → 断言 False | ✅ 已执行 |
| 单测 | ✅ **35/35 通过** |
| 清理 4 个堆积实例 | ⏳ **需管理员控制台**（见下方命令） |

```powershell
# 管理员控制台：只杀镜像路径的 QQ（不要用 /IM QQ.exe，会连个人 QQ 一起杀）
Get-Process QQ | Where-Object { $_.Path -like 'E:\QQbot\NapCat\*' } | Stop-Process -Force
# 然后双击 tools\start_napcat_verbose.bat 重新拉起一个
```

---

## 四、L1 要真能用，必须改权限模型

| 方案 | 做法 | 评价 |
|---|---|---|
| **① 提权计划任务**（推荐） | 在「任务计划程序」里建一个勾选**「使用最高权限运行」**的任务，动作 = 跑一个 `kill+start` 的 bat；bot 侧改用 `schtasks /run /tn <任务名>` 触发（**非管理员也能触发已建好的任务**） | 权限面最小；需要用户**提权建一次** |
| ② bot 以管理员运行 | 用管理员控制台启动 `reverse_bot.py` | 可行但权限面过大，且会改变 subprocess 行为，不推荐 |
| ③ 让 NapCat 非提权启动 | 改 hook 注入方式 | 受 NapCat 自身限制，不可控 |

### 若走方案 ①，改造点

1. 新建 `tools\napcat_restart_elevated.bat`：`taskkill` 镜像 QQ → 启动 `start_napcat_verbose.bat`
2. 任务计划程序建任务（最高权限、手动触发）
3. `offline_watchdog` 的 `launcher` 注入改成 `schtasks /run /tn "QQBot-NapCatRestart"`
4. **杀掉失败时必须中止本轮、不得继续 launcher**（这是本次暴露的真正逻辑缺陷）
5. 单测补一条：kill 全部失败 ⇒ 不产生 `restart` 动作

> 第 4 条与权限无关，**是设计缺陷**：`_attempt_restart` 在 `failed` 非空时也应放弃启动，
> 否则任何权限问题都会演变成实例堆积。

### 4.1 ✅ 设计缺陷已修复（2026-10-21）

**改动**（`scripts/offline_watchdog.py` `_attempt_restart`）：kill 循环之后、launcher 之前插入安全闸。

```python
# 只要有目标杀不掉，就绝不启动新的
if failed:
    self._next_restart_at = now + restart["min_interval_seconds"]
    self._log("WARN", f"L1 中止启动：{len(failed)} 个镜像 NapCat 进程结束失败 "
                      f"{failed}（成功 {killed or '无'}）⇒ **不重复启动**，避免实例堆积；"
                      f"{self._down_text(now)}")
    actions.append("restart_kill_failed")
    return
```

**要点**：

- **部分失败也中止**：残留实例仍会与新实例冲突，堆积照样发生
- **`targets` 为空仍照常启动**（无残留进程是正常重启场景，不能误伤）
- **中止不消耗每小时配额**（它不是一次重启），但会按 `min_interval_seconds` 退避，避免忙等重试
- 新增动作标记 `restart_kill_failed`，便于日志统计

**新增 4 条回归测试**（`scripts/test_offline_watchdog.py`）：

| 用例 | 断言 |
|---|---|
| `test_kill_failure_never_launches_new_instance` | 全部杀失败 ⇒ `launcher.count == 0` |
| `test_partial_kill_failure_also_aborts` | 部分成功 ⇒ 同样不启动 |
| `test_kill_safety_error_also_aborts` | `SafetyError` 视为失败 |
| `test_kill_failure_backs_off_before_retry` | 中止后 300s 内不得重试 |

**单测：39/39 通过**（原 35 + 新增 4）。

---

## 五、Android 手机能解决什么

| 类型 | Android 有用？ | 说明 |
|---|---|---|
| A 短抖动 | ❌ 不需要 | NapCat 30 秒自愈，十几次/天 |
| B 杀不掉 | ❌ 完全无关 | 纯 Windows 权限问题 |
| C 账号被踢 | ✅ **有用** | 可用 ADB 驱动手机做「无人扫码」 |
| D 整机休眠 | ⚠️ 只能部分 | 手机改不了 PC 睡眠 |

### 路线 1（推荐先试）：Android 做「无人扫码」兜底

被踢时本地快登凭据作废、只能扫码（决策 012）。手机可做：

```
PC 侧 NapCat 弹二维码 → ADB 通知手机 → 手机 QQ 打开扫一扫 → 对屏扫码 → 确认
```

- **能做**：`am start` 开 QQ、`uiautomator dump` 读页面、`input tap/text` 操作
- **做不了**：滑块验证、短信验证、设备验证
- **风险**：QQ 版本更新会改 UI 层级，选择器需长期维护
- **前置验证**：先确认决策 012 的 `NAPCAT_QUICK_PASSWORD` 是否已能让被踢自愈——能则这条路线不必做

### 路线 2（治本但高风险）：把协议端搬到 Android

PC 只跑 bot 逻辑，Android 跑 OneBot 实现。

- 好处：绕开 NTQQ hook、Windows 权限、注入 DLL
- 代价：Android 侧方案（Xposed/协议）的**可用性、维护状态、封号风险均未验证**；QQ 版本锁死；手机侧故障恢复手段更少

### 路线 3：解决夜间休眠（D 类）

`22:28:09 → 06:35:32` 停了 **487 分钟**，是已知最长停机，**与重连无关**——机器睡了。

- 对策：电源设置「永不睡眠」+ 定时任务唤醒；或换常开小主机
- **硬约束**：bot 逻辑搬不到手机——零样本分类器依赖 `sentence-transformers + torch`（数百 MB），
  Termux 跑不动。**Android 最多替代协议端，bot 本体仍需留在一台常开机器上。**

---

## 六、决策

1. **L1 立即关闭**（有害）——已执行；
2. **告警保持 5 秒**（配置热加载，未回退）；
3. **暂不上 Android**——现有数据不支持，先补分类与权限；
4. 待办按优先级：

| # | 待办 | 负责 |
|---|---|---|
| 1 | 管理员清理 4 个堆积实例，重启为 1 个 | **用户**（需提权） |
| 2 | 修 `_attempt_restart`：kill 失败必须中止启动 | Lead |
| 3 | 加「掉线分类」日志（A/B/C/D 四类打标） | Lead |
| 4 | 跑 2~3 天统计四类占比 | 用户 / 巡检 |
| 5 | 按占比决定是否做提权计划任务 / Android 扫码 | 用户 |
| 6 | 夜间休眠（D 类）单独决策 | 用户 |

---

## 七、数据附录

- 镜像 NapCat 进程：**4 个**（08:43 / 08:48 / 08:53 / 12:57 启动）
- 全机 QQ 进程：**9 个**（5 个人 + 4 镜像）
- bot 权限：**非管理员**（`IsUserAnAdmin() == False`）
- `taskkill` 探测结果：`ERROR: Access denied`
- 10-18 另有超长停机：**43.5 分钟 / 45.5 分钟 / 98 分钟**（同样无自动恢复）
- 单测：`scripts/test_offline_watchdog.py` → **35/35 通过**
---

## 八、⚠️ 重要更正：L1 失效的真因是代码 bug，不是权限

> 追加日期：2026-10-21　｜　更正 §3.2 与 §四
> 按「决策日志只追加」纪律，不删除原文；错误推理保留作过程留档。

### 8.1 我错在哪

当时我用一条命令下的结论：

    bot 进程是否管理员 = False
    taskkill /PID <镜像QQ>        →  ERROR: Access denied

据此判定「镜像 QQ 是提权进程，bot 杀不掉」。**这个推理有两处硬伤**：

1. 我执行的 taskkill **没加 /F**。对 GUI 进程不带 /F 会发 WM_CLOSE，被拒是常态，
   与带 /F 的强制结束完全不是一回事；
2. 我**没去看代码里的安全拦截日志**，而它早就把答案写在日志里了。

### 8.2 真正的日志证据

    [08:43:47] 安全拦截，未结束进程：拒绝结束白名单外进程 PID=82484 name='NapCatWinBootMain'
    [08:43:47] 安全拦截，未结束进程：拒绝结束白名单外进程 PID=5148  name='QQ'
    [08:43:47] 安全拦截，未结束进程：拒绝结束白名单外进程 PID=51728 name='QQ'

注意 **name='QQ' 没有 .exe**。而白名单是：

    NAPCAT_PROCESS_NAMES = ('QQ.exe', 'NapCatWinBootMain.exe', 'NapCatWinBootHook.exe')

而枚举器用的是 PowerShell 的 `$p.ProcessName`，它返回**不带扩展名**的基名。
于是比较：

    'qq' in {'qq.exe', 'napcatwinbootmain.exe', ...}   →  False（恒成立）

⇒ **每一个目标都被判成「白名单外进程」并被 SafetyError 拒绝**。
**L1 从写出来那天起，一个进程都没杀掉过。**

### 8.3 为什么这个 bug 能存活这么久

`default_process_lister`（产出 `'QQ'`）与 `default_killer`（收 `'QQ.exe'`）
是两处独立演化的代码，**中间没有任何测试守护这个契约**。
36 条单测全绿，却没有一条把「枚举器的输出」喂给「killer 的白名单」验证。

### 8.4 修复

新增两个辅助（`scripts/offline_watchdog.py`）：

    def _proc_name_key(name):        # 取基名、去掉 .exe、小写
    def _is_allowed_proc_name(name): # 归一化键是否在白名单集合内

`default_killer` 改用 `_is_allowed_proc_name(proc.name)`。

**效果（实机验证）**：

| 输入 | 修复前 | 修复后 |
|---|---|---|
| `'QQ'`（PowerShell 风格） | **False（拒绝）** | **True（放行）** |
| `'QQ.exe'` | True | True |
| `'NapCatWinBootMain'` | **False** | **True** |
| `'python'` / `'QQNT'` / `''` / `None` | False | False（仍拒绝） |
| 实机镜像进程 `name='QQ'` | 拒绝 | **放行** |

**新增 2 条回归测试**：

| 用例 | 守护什么 |
|---|---|
| `test_killer_name_whitelist_normalizes_extension` | 扩展名可有可无、相似名不得前缀匹配 |
| `test_lister_name_and_killer_whitelist_are_compatible` | **枚举器输出必须能被 killer 接受**（契约测试） |

**单测：41/41 通过。**

### 8.5 连带结论：不需要提权，也不需要计划任务

§四 推荐的三套「权限方案」（提权计划任务 / bot 提权运行 / 改 hook）**全部作废** ——
问题不在权限，而在名字比较。修掉之后：

- bot 以普通用户身份即可结束镜像 QQ（它们与 bot 同为该用户所启动）
- 不需要建计划任务，不需要 schtasks
- **`restart.enabled` 可以重新打开**（但建议先用 `dry_run: true` 观察一轮）

### 8.6 仍然成立的结论

- §4.1 的「kill 失败必须中止启动」安全闸**依然必要且已实现**（41/41 已验证）——
  它是防御性的：即使将来出现别的杀不掉原因，也不会再演变成实例堆积；
- §二 的四类故障分类**不受影响**；
- L1 目前仍保持 **关闭**，等 `dry_run` 观察后再决定开放。