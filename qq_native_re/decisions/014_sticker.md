# 决策日志 014 — 机器人自主发表情包（sticker）

> 日期：2026-10-14 ~ 10-15　｜　执行：Lead（子 agent 在 `sticker-logic` / `sticker-tagger` 两个任务上失败，改由 Lead 接手）
> 结论：**代码完成、离线验收全绿；已于 2026-10-15 08:42 实机验收通过并在两个群上线**
> （离线部分为 10-14 完成；实机验收与上线为 10-15，见 §七）

## 一、交付物

| 文件 | 状态 | 说明 |
|---|---|---|
| `scripts/sticker.py` | 新建 | 表情判断库：本地向量检索 + 闸门。纯逻辑、零 I/O、依赖全注入 |
| `scripts/sticker_manager.py` | 新建 | 索引构建器：`scan` / `ingest` / `stats` / `review`。**不调用任何模型** |
| `scripts/decision_engine.py` | 修改 | 新增 `ReplyResult`；回复链插入表情步 |
| `reverse_bot.py` | 修改 | 发送分支；`send_group_image` 加 `sub_type`；`_FEATURE_KEYS` 加 `sticker` |
| `scripts/group_manager.py` | 修改 | `FEATURES` 加第 8 项「表情包」，默认关闭 |
| `config/sticker_tags.json` | 新建 | 受控词表 15 情绪 / 16 场景 / 10 意图 |
| `config/stickers.json` | 新建 | 运行时配置（阈值 / 限流 / 发送形态） |
| `data/stickers/SCHEMA.md` | 新建 | 索引契约 + 打标交付契约 |
| `data/stickers/IMAGE_SPEC.md` | 新建 | 图片处理规范 v1.1 |
| `data/stickers/index.json` | 生成 | 65 条成品索引（384 维向量） |
| `data/stickers/library/` | 用户提供 | 65 张（54 WebP + 11 GIF），已 gitignore |
| `qq_native_re/patches/sticker_backup_20261014-153415/` | 新建 | 改动前快照 + `rollback.ps1` + `MANIFEST.tsv`(SHA256) |

## 二、关键决策（含被否掉的方案）

### 1. 单链，不是两条链
用户明确否决了「表情链 + 文字链 + 总协调层」。最终形态：**表情是回复链内部的一个步骤**，
判断逻辑单独放在 `scripts/sticker.py`（是被调用的库，不是并列的链）。

### 2. 表情步放在文字生成**之前**
这是唯一能真正「抑制文字链」的位置——`strong` 档直接返回纯图，
**完全跳过 `_generate_reply_async`**。在当前网关（实测单次 66~126s）下这是实打实的收益。
实测：假引擎路径秒回，证明全程未调模型。

### 3. 运行时零 API 调用
索引在打标阶段就把向量算好，运行时只编码当前上下文一次 + 点积。
否掉了：① 改回复 prompt 搭车（会污染已验收的回复质量链）；② 独立小模型 router（当前网关下要等超时）。

### 4. 打标交给外部多模态 agent，工具只做机械活
用户提出后采纳。好处是**工具侧完全不碰网络与网关**——而本机网关长年 ReadTimeout。
agent 只填语义字段；`id`/`sha256`/`w`/`h`/`animated`/`vec`/`tagged_at` 全部由工具补。

### 5. 词表受控 + 「keywords 长短都写、符号也写」
实测教训两条，都已写进 `SCHEMA.md` §6.2：
- 「吃瓜」不含「瓜」→ 用户说「什么瓜」时字面命中 0，差 0.007 没发出去；
- 「问号」不是「？」的子串 → 用户打「？？？」时字面命中 0。

### 6. `weak_score` 0.45 → 0.40
实测：**无字面命中的**合理语义场景分数集中在 0.39~0.45，0.45 会一刀切掉。
0.40 切得很干净：放进 0.409/0.440，仍拦住 0.392。
记录在 `config/stickers.json` 的 `_notes` 里，并写明纪律：**能用 keywords 解决的不要用阈值解决**。

## 三、实测证据

| 验证 | 结果 |
|---|---|
| 判断库冒烟 | **24 项全通过**（含 SCHEMA §4 五条容错） |
| 接线功能测试 | **20 项全通过**（含「strong 跳过模型调用」） |
| 打标器端到端 | 65/65；4 个故意错误输入全部拦下 |
| 打标结果独立审计 | 65/65、字段零缺失、SHA256 抽验相符 |
| 打标 session 声称的分数 | **7/7 逐条复现一致** |
| 实机匹配（真实索引） | 前排吃瓜 1.000 / 呜呜呜我考砸了 0.916 / 晚安各位 0.791 / 今天好闲啊 0.791 |
| `import reverse_bot` | OK，VERSION 2.5.0，`_FEATURE_KEYS` 8 项 |
| `check_prod_proof.py` | **PROD_PROOF_FAILURES=0** |

### 过程中发现并修掉的两个问题

1. **`sticker_manager.ingest` 的旧向量 bug**：原先只给「没有向量的」条目算向量，
   而重打标改的正是 `emotion/keywords/caption`——重打过的条目会挂着旧向量，
   **标签变了匹配却还是旧的**。已改为「本次合并进来的条目一律重算」。
2. **`check_prod_proof.py` 两处断言过期**：`_FEATURE_KEYS == 7`（现为 8）、
   「群 284517232 未启用 poke_menu/reaction_menu」（决策 013 后用户已开启）。
   均已按当前事实改写，并**新增不变量:`所有群的 sticker 未启用`**。

## 四、回退（三层，都不需要改代码）

| 层级 | 操作 | 效果 |
|---|---|---|
| 功能级 | `config/group_features.json` 把 `sticker` 改回 `false` | 热加载立即停用 |
| 渲染级 | `config/stickers.json` 的 `send_sub_type` 改 `0` | 退回普通图片形态 |
| 数据级 | 删掉 `data/stickers/index.json` | 判断库静默失效，行为与改动前完全一致 |

硬回退：`qq_native_re/patches/sticker_backup_20261014-153415/rollback.ps1 -Mode hard`
（⚠️ 勿整文件覆盖 `reverse_bot.py`，先 `git diff` 看清，或反向摘 hunk）。

## 五、遗留待办

1. **实机验证 `sub_type=1` 的渲染形态（最高优先）**。目前仅为 `napcat.mjs` 源码取证结论。
   三条对照消息与判定分支已固化为 `qq_native_re/findings/sticker/PROBE_subtype_runbook.md`。
   同时要确认 **WebP 在贴图形态下能否正常显示**（54/65 是 WebP）。
2. **上线开关未动**：两个已配置群的 `sticker` 均为 `false`，需人工开启才会上线。
3. 词表里 `同情` / `赞同` / `道别` / `道歉` / `打游戏` 等标签仍无图可用（**缺图，不是标错**）。
4. 12 条索引没有 `scene`，接不了「场合」类匹配。
5. 图库尺寸普遍 1024（建议 240~512）。**若将来要缩图，必须在重新打标之前决定**——
   缩图会改 SHA256，让索引全部变孤儿。
6. `data/stickers_acl_report/` 记录了一次 DSH 沙箱 ACL 修复（给 `data/stickers` 加了当前账号的
   FullControl）。评估：窄范围、可撤销、实际未提权（该账号本就有 Modify）。已加进 `.gitignore`。

## 六、未做的事（如实标注）

- **没有发任何一条测试消息**（用户明确要求）；
- **没有重启生产 bot**；
- `sticker-tagger` 与 `sticker-logic` 两个子 agent 失败/停滞，由 Lead 接手；
- `scripts/test_sticker.py`（正式 unittest 套件）未单独产出——本次用两次定向冒烟
  （24 项 + 20 项）代替，覆盖范围见 §三。
---

## 七、上线验收与实机证据（2026-10-15）

### 7.1 首次 `[只发图]`（核心验收）

用户在群 284517232 发 `@bot 前排吃瓜`，日志：

```
[2026-10-15 08:42:27] [表情] 群 284517232 发出 048_DeepSeek娘_吃瓜_sticker_issue_32_022.webp
                              （sub_type=1，1=贴图/0=普通图片）
[2026-10-15 08:42:27] [决策] 群 284517232 回复[只发图]（总耗时 137ms）
```

### 7.2 「跳过模型调用」的实证（同群同日对比）

| 时间 | 档位 | 总耗时 |
|---|---|---|
| 08:36:58 | 只发文字 | 4617 ms |
| 08:36:40 | 只发文字 | 3702 ms |
| 08:13:48 | 文字+图 | 2417 ms |
| 08:13:32 | 文字+图 | 2019 ms |
| **08:42:27** | **只发图** | **137 ms** |

**137ms vs 2000~4600ms，快 15~34 倍** —— 同一群、同一套链路、同一天，纯图档确实完全没碰模型网关。

### 7.3 贴图形态的 NapCat 侧证据（一手）

```
[OneBot] 收到正向Websocket消息 {"action":"send_group_msg","params":{"group_id":284517232,
   "message":[{"type":"image","data":{"file":"base64://R0lGODlhWAJYAocAAE5jiP38...","sub_type":1}}]}}
[info] 发送 -> 群聊 [...] [Deepseek(2668851638)] [图片]
```

QQ 回执里的 `picElement`：

```json
{ "picSubType": 1, "picType": 2000, "storeID": 1, "original": true,
  "sourcePath": "E:\\QQ文件\\Tencent Files\\2668851638\\nt_qq\\nt_data\\Emoji\\emoji-recv\\...gif" }
```

**`sourcePath` 落在 `nt_data\Emoji\` 下 —— QQ 确实把它当「表情」而非普通图片归档。**
（两次发送 08:13:33 / 08:13:48 均如此；`group_recall`=0、`delete_msg`=0、`recallTime` 非 0 = 0，未被撤回。）

### 7.4 fail-closed 的实机验证（意外收获）

用户在**未开启 sticker** 的群 1102194840 发言，机器人输出：

```
[2026-10-15 08:36:40] [决策] 群 1102194840 回复[只发文字]（总耗时 3702ms）
```

**功能位关闭 → 行为与接线前完全一致**，这条设计在真实环境里被验证了一次（不是单测里模拟的）。

### 7.5 一个「看起来像 bug 但不是」的案例

08:35:55 用户发了「前排吃瓜」（未 `@bot`、也不含别名），机器人**什么都没做、一行日志都没有**。
排查结论：**设计如此**——表情步位于回复链内部，机器人判定「这条不需要回复」时就结束了，
表情步没有机会执行。带 `@bot` 或角色别名（如「大肥鱼」）时走硬规则强制回复，表情步才会跑。

> 附带记一个**误导性日志**（既有，非本波引入）：`reverse_bot.py` 的
> `[名字匹配] '…' → 视为 @bot，群{alias_gid}` 打印的是「别名映射到哪个群」，
> 而不是「消息来自哪个群」。上述排查时它把 1102194840 的消息显示成 284517232，
> 一度误导定位。**列为遗留待办。**

### 7.6 上线范围与配置

| 项 | 值 |
|---|---|
| 群 284517232 | `sticker: true` ✅ 上线 |
| 群 1102194840 | `sticker: true` ✅ 上线 |
| 群 1055881417 | `sticker: false`（保持关闭） |
| 新增群默认 | **false**（`FEATURE_DEFAULTS` 排除，已有断言守护） |
| `probability` / 冷却 / 限流 / 同图窗口 | **0.35 / 180s / 3 次每小时 / 3600s**（已从测试值还原） |
| `send_sub_type` | 1 |

### 7.7 `check_prod_proof.py` 的两次订正

本波共订正三处过期断言（都非代码回归）：

1. `_FEATURE_KEYS == 7` → **8**（加入 sticker）；
2. 「群 284517232 未启用 poke_menu / reaction_menu」→ 按群分别断言（决策 013 后用户已开启）；
3. 「所有群 sticker 未启用」→ 改为按群断言（两群上线、其余关闭），
   **并新增不变量「`FEATURE_DEFAULTS` 里 sticker 默认 False」**。

末界：**`PROD_PROOF_FAILURES=0`**。

### 7.8 尚未闭环的一项

**`sub_type=1` 的观感是否真的变成了「贴图」**，目前只有两条间接证据
（`picSubType=1`、QQ 归档到 `Emoji\` 目录），**缺一次明确的肉眼判别**。
用户 2026-10-15 反馈「图片很正常，看起来和我发的一样」——该表述既可解释为「无异常」，
也可解释为「仍是普通图片观感」，**未能定论**。

判别方法（见 `PROBE_subtype_runbook.md`）：看有无图片边框 / 能否点开放大 / 长按能否收藏为表情。
若判定为普通图片观感 → 改 `config/stickers.json` 的 `send_sub_type` 为 `0` 退回；
若仍要贴图观感 → 评估 `add_custom_face` 收藏表情路线。