# 决策日志 016 — 掉线告警改为 5 秒立即弹窗

> 日期：2026-10-20　｜　执行：Lead
> 结论：**告警延迟 90s → 5s；同时拆出「冷启动宽限」避免重启误报。35/35 单测通过。**

## 一、触发这次改动的事实

用户指出：**NapCat 侧从来没有自动重连，历史上所有的"恢复"都是用户碰巧发现后手动连上的。**

这意味着此前对故障恢复的认知是错的：不是「看门狗 + NapCat 自愈兜底」，而是「**只有人在看的时候才能恢复**」。
今天 7 次掉线的真实结局：停机 23s / 5.8min / 4.4min / 26s / 19s / **23.4min** / **157.5min** ——
后两次不是自动恢复的，是用户发现的。

⇒ 弹窗是**唯一**的恢复信号，90 秒的延迟纯属浪费。

## 二、改了哪些

| 文件 | 改动 |
|---|---|
| `config/auto_restart.json` | `alert.after_seconds` 90 → **5**；新增 `alert.startup_grace_seconds: 60` |
| `scripts/offline_watchdog.py` | 见下表 6 处 |
| `scripts/test_offline_watchdog.py` | 同步更新 12 个用例 + 新增 `na()` 助手 |
| `qq_native_re/findings/rounds/offline_alert.md` | 文档时序同步 |

### 代码的 6 处

1. `DEFAULT_CONFIG` / `parse_config`：新增 `startup_grace_seconds`（默认 60，minimum 0）
2. `__init__`：新增 `self._startup_pending`
3. `on_start`：置 `_startup_pending=True`，日志改用宽限值
4. `on_connected`：清 `_startup_pending=False`
5. **`_step_alert`**：首弹阈值 = 冷启动时用 `startup_grace_seconds`，真掉线用 `after_seconds`
6. **`next_deadline`**：同上（否则定时唤醒时刻算错，弹窗时机仍会错）
7. `_alert_text`：`< 60s` 按**秒**显示（否则会说"已离线 1 分钟"而实际才 4 秒）

## 三、为什么必须拆出「冷启动宽限」

`alert.after_seconds` 原本被**两处复用**：

- 真掉线 → 弹窗
- `on_start`：bot 启动后 N 秒内 NapCat 没连上 → 弹窗

实测 NapCat 重连需要 **14~16 秒**。若不拆，`after_seconds=5` 会让**每次重启机器人都误弹一次**。
拆开后：真掉线 5 秒弹，冷启动给 60 秒宽限。

## 四、验证

| 项 | 结果 |
|---|---|
| `import reverse_bot` | OK 2.5.0 |
| 配置解析 | `after_seconds=5.0` / `startup_grace=60.0` / `restart.enabled=False` |
| 弹窗文案 | 4 秒→"已离线 4 秒"；90 秒→"2 分钟"；900 秒→"15 分钟" |
| **离线单测** | **35/35 通过** |

### 单测更新中遇到的问题（留档）

- 12 个用例失败，归因：6 个是「写死 90 秒」，5 个是「精确列表相等，多了一个 alert 动作」，
  1 个是「冷启动现在走 60s 宽限」。**无一是实现 bug。**
- 引入 `na()` 助手把「重启语义」与「告警语义」解耦；注意它**只滤恰好等于 `"alert"`**，
  不能按前缀滤——否则会误伤 `alert_dry_run`。

## 五、生效方式

| 部分 | 生效条件 |
|---|---|
| 告警 5 秒（配置） | **已生效**，`auto_restart.json` 由 `ConfigStore` 按 mtime 热加载 |
| 冷启动宽限（代码） | **需重启 bot**；重启后即加载新代码 |

## 六、回退

`config/auto_restart.json` 改回 `"after_seconds": 90` 即可（代码兼容旧配置：`.get` 回退）。

## 七、仍待决策的关键问题

**没有自动重连，就意味着没人在电脑前时掉线会一直掉着。** 看门狗的 L1 自动重启
（`restart.enabled`）一直是 `false`，而它正是为这个场景造的、且已有单测覆盖：

- 只杀 `E:\QQbot\NapCat\` 镜像内的进程（路径双重校验 + 单测覆盖个人 QQNT 不受影响）
- 限流：最小间隔 300s、每小时最多 3 次
- `dry_run` 可先只记日志不动手

建议：先 `dry_run: true` 观察一轮，再放开。**这才是"无人值守"的真正解法，弹窗只是叫人。**
---

## 八、L1 自动重启已开启（2026-10-20 追加）

用户决定开启 `restart.enabled`。开启前逐项验证了安全前提：

| 检查 | 结果 |
|---|---|
| 启动脚本存在性 | `tools/start_napcat_verbose.bat` ✓ 8646 B |
| 启动脚本做什么 | 免扫码快登，**只认机器人账号 2668851638**（脚本内有硬编码 precheck，填错直接 abort） |
| 路径过滤（单测） | 镜像 → True；个人 QQNT → False；前缀相似目录 → False |
| **路径过滤（实机对照）** | 5 个 `E:\Program Files\Tencent\QQNT\QQ.exe`（个人，判定 False）<br>1 个 `E:\QQbot\NapCat\...\QQ.exe`（PID 69684，判定 True） |
| 双重限流 | 最小间隔 300s、每小时最多 3 次（单测断言在位） |
| 不抢跑 | 启动未满 60s 的 NapCat 进程会等待 |
| 第二道保险 | `default_killer` 对非镜像路径抛 `SafetyError` |

最终配置：

```jsonc
{
  "alert":   { "enabled": true, "after_seconds": 5, "repeat_seconds": 120, "startup_grace_seconds": 60 },
  "restart": { "enabled": true, "after_seconds": 60, "min_interval_seconds": 300, "max_per_hour": 3 },
  "dry_run": false
}
```

### 时序

```
断连 ──5s──> 置顶弹窗告警（叫人）
     ──60s─> L1 自动重启（杀镜像 NapCat → 跑 bat → 免扫码重登）
     ──120s> 告警未关则不重复弹；关掉后每 120s 再弹
```

### 配置变更的安全断言也一起改了

原来的 `test_restart_default_config_is_disabled` 断言「仓库配置必须重启关」。
现改名为 `test_repo_config_matches_deployed_settings`，断言「已开启 **且限流参数仍在**」。

> **安全默认没有丢**：`DEFAULT_CONFIG`（配置缺失/非法时的兜底）仍然是「重启关」，
> 由 `test_config_mtime_hot_reload_and_invalid_fallback` 单独守护。两者不可混淆。

### 生效方式

`config/auto_restart.json` 由 `ConfigStore` 按 mtime **热加载** ⇒ **已立即生效，无需重启**。

### 如果觉得太激进

`config/auto_restart.json` 里 `"dry_run": true` ⇒ 只记 `[dry-run] 本应结束…` 日志、不动进程。