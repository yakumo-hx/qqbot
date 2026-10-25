# 表情包功能 · 验收报告（sticker）

- 日期：2026-10-15　｜　状态：**已实机验收通过，并在群 284517232 / 1102194840 上线**
- 配套：决策日志 `qq_native_re/decisions/014_sticker.md`　｜　回退点 `qq_native_re/patches/sticker_backup_20261014-153415/`

## 1. 系统边界

```
用户/agent 提供图片
   │  （规范：data/stickers/IMAGE_SPEC.md v1.1）
   ▼
data/stickers/library/            65 张（54 WebP + 11 GIF），gitignore
   │  ← 多模态 agent 读图，产出语义标签（契约：SCHEMA.md §6）
   ▼
scripts/sticker_manager.py ingest  工具补全 id/sha256/w/h/vec，原子落盘
   ▼
data/stickers/index.json          65 条，384 维向量
   │
   ▼  运行时（零 API 调用）
scripts/sticker.py                本地向量检索 + 6 道闸门 → StickerPick
   │
   ▼
scripts/decision_engine.py       回复链插入表情步 → ReplyResult(text, sticker)
   ▼
reverse_bot.py                    发送：文字 → 图（image 段带 sub_type）
```

## 2. 改动清单

| 类别 | 文件 |
|---|---|
| 新增代码 | `scripts/sticker.py`、`scripts/sticker_manager.py` |
| 修改代码 | `scripts/decision_engine.py`、`reverse_bot.py`、`scripts/group_manager.py` |
| 新增配置 | `config/sticker_tags.json`、`config/stickers.json` |
| 修改配置 | `config/group_features.json`（三群各加 `sticker`）、`.gitignore` |
| 文档/契约 | `data/stickers/SCHEMA.md`、`IMAGE_SPEC.md`、`TAGS_EXAMPLE.json`、`RETAG_PROMPT.md` |
| 档案 | `qq_native_re/decisions/014_sticker.md`、本文件、`PROBE_subtype_runbook.md` |

**未改动（刻意保持）**：`vision.py`、`llm_pool.py`、`image_utils.py`、`search_gate.py`、
`web_search.py`、`interaction_menu.py`、`offline_watchdog.py`、`decision.py`。

## 3. 闸门与默认参数

| 闸门 | 默认值 | 位置 |
|---|---|---|
| 群功能位 `sticker` | **false（全部群）** | `config/group_features.json` |
| 总开关 `enabled` | true | `config/stickers.json` |
| `strong_score` | 0.62（+0.03×intensity） | 同上 |
| `weak_score` | **0.40**（2026-10-15 由 0.45 下调） | 同上 |
| `probability` | 0.35（随机降温） | 同上 |
| `cooldown_seconds` | 180 | 同上 |
| `max_per_hour` | 3 | 同上 |
| `same_image_window_seconds` | 3600 | 同上 |
| `send_sub_type` | 1（**未实机验证**） | 同上 |

## 4. 验证结果

### 4.1 判断库（`scripts/sticker.py`）

24 项定向冒烟 **全通过**，覆盖：功能位关闭 / 配置缺失 / `enabled=false` / 索引缺失 /
版本不认识 / items 为空 / cos 三档分档 / 字面命中 / intensity 抬阈值 / quarantine 排除 /
文件缺失排除 / **vec 维度不符跳过** / 冷却 / 每小时限流 / 同图窗口 / 概率门 / 无向量降级。

过程中修正两处偏离冻结契约的实现：① 低于阈值时未回传真实 score（可解释性）；
② vec 维度不符的条目本应**跳过**，原先却以 sim=0 参与比较。

### 4.2 索引构建器（`scripts/sticker_manager.py`）

对**真实图库**跑通：`scan` 65 张合格零问题；`ingest` 的 4 个故意错误输入全部拦下
（不存在的文件 / 空语义 / 词表外标签 / `safe=false`）；向量 384 维本地算出。

### 4.3 打标结果独立审计

| 项 | 结果 |
|---|---|
| 库文件 / 索引条目 | 65 / 65，无缺图、无多余条目 |
| 必备字段 | 零缺失 |
| 向量 | 65/65 全 384 维 |
| SHA256 | 抽验相符（含重打的） |
| 词表外标签 / 隔离 | 0 / 0 |
| **名字泄漏检测** | 27 条关键词与文件名重合 → 抽查 6 张图，**全部为内容推导**，非抄袭 |

反证两条最硬：`018` 文件名写「涩涩鲸鱼」，标注判为**无害**；`001` 文件名是**无意义符号 `／／／`**，
标注依然丰富。若在抄名字，这两条都会翻车。

### 4.4 接线（`decision_engine.py` + `reverse_bot.py`）

20 项功能测试 **全通过**。最关键一条：用假引擎注入 `strong` 结果，
`should_reply(is_at_bot=True, sticker_on=True)` **秒回纯图 ReplyResult** ——
走真模型的话这里会卡满 45s 超时，因此证明「只发图」确实跳过了模型调用。

真引擎端到端：`前排吃瓜` / `呜呜呜我考砸了` / `今天好闲啊` 三句均正确出图。

### 4.5 生产不变量

`qq_native_re/findings/pr1/check_prod_proof.py` → **PROD_PROOF_FAILURES=0**，
含新增不变量「所有群的 `sticker` 未启用」。

## 5. 残留缺口（如实标注）

1. **`sub_type=1` 的「贴图观感」仍未闭环**（唯一未闭环项）。已实机发出并确认
   `picSubType=1`、QQ 归档到 `nt_data\Emoji\`、未被撤回；但**缺一次明确的肉眼判别**。
   用户反馈「很正常，看起来和我发的一样」未能定论。判别方法见 `PROBE_subtype_runbook.md`。
2. 未产出独立的 `scripts/test_sticker.py` unittest 套件，以两次定向冒烟代替（24 + 20 项）。
3. 词表 `同情`/`赞同`/`道别`/`道歉`/`打游戏` 无对应图片（缺图）。
4. 12 条索引无 `scene`；图库尺寸普遍 1024（建议 240~512，**缩图必须赶在重打标之前**）。
5. 子 agent `sticker-tagger` / `sticker-logic` 失败与停滞，相关工作由 Lead 接手完成。
6. `data/stickers_acl_report/` 记录了一次 DSH 沙箱 ACL 修复（窄范围、可撤销、未实际提权），已 gitignore。

## 6. 上线前检查清单

```
[x] 1. config/group_features.json 给目标群开 sticker: true
       284517232 ✅ / 1102194840 ✅ / 1055881417 保持关闭
[x] 2. 观察 NapCat 一手 fileLog 里的 [表情] 行与 sub_type 取值
       picSubType=1、sourcePath 落在 nt_data\Emoji\、group_recall=0（未被撤回）
[x] 3. 验证「只发图」跳过模型调用
       08:42:27 总耗时 137ms，对比同群文字回复 2000~4600ms（快 15~34 倍）
[x] 4. 验证 fail-closed
       群 1102194840 未开功能位时输出 回复[只发文字]，行为与接线前一致
[ ] 5. 肉眼判别 sub_type=1 是否真的呈现为「贴图」
       ⚠️ 仍未闭环：只有 picSubType=1 与 Emoji 归档两条间接证据；
       用户反馈「很正常，看起来和我发的一样」未能定论。
       判别：看有无图片边框 / 能否点开放大 / 长按能否收藏。
       若判定为普通图片观感 → send_sub_type 改 0（配置级，无需改代码）
```