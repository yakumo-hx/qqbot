# 决策日志 013 — PR#1（戳一戳 + 表情互动菜单）验收通过

> 日期：2026-10-13 17:55
> 决策者：PM（用户先报“已重启，功能测试没问题”；PM 随后从 **NapCat 一手 fileLog** 独立复核）
> 结论：**功能上线并通过实机验收**；当前仅在群 **284517232** 启用

## 一、代码落盘与静态验收（16:22–17:11）

- 采纳方式 A：4 个新增文件原样引入 + 5 处接线按 `REVIEW.md §5` 改编（含必须修 **M1/M2/M3** 与本地补丁 **S1**）
- 9 文件 SHA256 与 `_pr1_stage` 副本一致；PM 亲跑：`unittest scripts.test_interaction_menu` → **Ran 25 tests OK**；`check_wiring` `WIRING_FAILURES=0`；`check_s1` `S1_FAILURES=0`；`check_facade` 11/11；`VERSION` 2.5.0；`_FEATURE_KEYS` 不含菜单两键
- 末界（当时）：未重启（旧 PID 45892 未变）、`scripts/command_handler.py` 与 `config/group_features.json` 哈希未变

## 二、实机验收（17:45 重启后；用户操作 + PM 读 NapCat 一手日志）

| 时刻 | 证据（NapCat fileLog 原文节选） | 判定 |
|---|---|---|
| 17:46:27 | 3× `set_msg_emoji_like`（emoji_id `128077`/`127836`/`10068`，message_id 222528090） | 菜单已发出 + 3 个预置表情回应设置成功 |
| 17:48:11 | `发送 -> 群聊[…(284517232)] 【互动菜单】全群可用，操作记在点击者本人名下。` | 菜单文案正确 |
| 17:48:12 | 3× `set_msg_emoji_like`（message_id 2016739404） | 第二次开菜单同样成功 |
| 17:48:15 | `send_group_msg … [CQ:at,qq=10001] 签到成功！你是今日第2位签到者，累计签到1天，连续签到1天，全群排名第2位。` | **点击者的表情回应以「点击者本人」身份执行了原签到指令**（核心设计意图达成） |
| 17:49+ | `欢迎新人`、`[定向回复] 目标: 幽幽子(3889001246)`、`[防复读] 相似度=0.10`、`搜索资料=0条` | 既有功能**零回归** |

新 bot 进程：PID **46328**（2026-10-13T17:45:38 启动），`:8080` LISTENING 且 NapCat 已 ESTABLISHED。

> 说明：bot 侧日志中 `[互动菜单]` 命中 0 行属**正常**——菜单模块仅在异常时打该前缀日志；成功路径的“动作级”证据在 NapCat 侧。

## 三、开关现状与回滚

- **284517232**：`poke_menu: true` + `reaction_menu: true`（本次启用）；**1055881417**：保持关闭
- 软回滚：把两键改回 `false`（mtime 热加载，立即停用）
- 硬回滚：`qq_native_re/patches/pr1_land_backup_20261013-165506/` 恢复 5 个接线文件 + 删 4 个新增文件；⚠️ **勿整文件覆盖 `reverse_bot.py`**（会连带回退并行 search 工作流的改动），建议反向摘 hunk

## 四、已分析、无需处理的一项

`_FEATURE_KEYS`（`reverse_bot.py:67`）未含菜单两键，该元组在 `:336` 作群级短路门。PM 读码核实：`interaction_menu.enabled()`（`:224-227`）**要求 `commands` 为真**，而 `commands` 就在 `_FEATURE_KEYS` 内 ⇒ 能开菜单的群必然通过该门 ⇒ **伪命题，无需改动**（与审查 S4 一致）。
