# eval — 「像人」测试环境

把「测试 → 比较 → 修改规范」跑成可重复的回合制流程。三个角色：

| 角色 | 文件 | 职责 |
|---|---|---|
| **出题人** | `examiner.py` | 从群里（历史语料或实时 `_live`）取「上下文 → 真人下一句」，固化成题目 |
| **生成器** | `generator.py` | 按当前规范 + 上下文生成 1–3 条气泡（调 `llm_pool`，带缓存） |
| **评分器** | `scorer.py` | 对比 AI 与真人的形态/行为/分布，产出报告与人工盲评包 |
| **回合** | `runner.py` | 抽题 → 生成 → 评分 → `reports/` |

## 数据流

```
群消息（recorder / 语料）
   │  examiner
   ▼
eval/data/scenarios/<gid>/<day>.jsonl   题目（context/target/reference）
   │  runner + generator（rules_v1_prompt）
   ▼
eval/data/cache/<spec>/<scenario>.json  缓存（可复算）
   │  scorer
   ▼
eval/reports/roundN_*.md/json           回合报告
eval/reports/blind_roundN.md            人工盲评包（A/B 匿名）
```

## 题目结构

```json
{"id":"sc-...","gid":"284517232","day":"2026-10-30","type":"reply|mention|media_context|initiation|question|continue",
 "split":"train|test",
 "context":[{"ts","uid","nick","text","kind","media"}...],
 "target":{"nick","text"} | null,
 "reference":{"bubbles":[...],"n_bubbles":1},
 "meta":{"gap_s":...,"has_media":...,"context_has_media":...,"is_question":...}}
```

- `media_context`：上文 2 条内出现过图片/表情，参考是真人下一句（文字或媒体），用来测「接文字还是接媒体」。
  旧的 `media`（参考本身是媒体）已废弃，`media_post` 只留档不参与评测。
- `split`：`day >= 2026-10-29` 为 `test`，更早为 `train`；规范只能在 train 上调，**test 只用于验收**。
- `reference.bubbles`：把同一人 60 秒内的连发算作一组，最多 3 条。

## 人工盲测（命令行交互）

```powershell
cd E:\QQbot
$env:PYTHONIOENCODING="utf-8"

# 12 组（round6 + round7 各 6 组），带上下文，自动保存进度
& ".\venv\Scripts\python" -m language_lab blind

# 只做 6 组
& ".\venv\Scripts\python" -m language_lab blind --limit 6

# 清空重来 / 只猜不评分
& ".\venv\Scripts\python" -m language_lab blind --reset
& ".\venv\Scripts\python" -m language_lab blind --quick
```

每组输入 `A 4`（猜 A 是 AI，接受度 4/5）、`? 3`（看不出）、`s` 跳过、`q` 保存退出。
答完自动输出 `reports/blind_result_<时间>.md/.json`：AI 被识破率、平均接受度、分题型弱点。

上下文与媒体说明：
- 上下文长度由**会话边界（默认 15 分钟）+ 话题连续性（字符 bigram）**决定，默认 10~30 条不等，
  可用 `--min-context/--max-context/--gap` 调整；
- 图片/表情/转发只保存占位符；能匹配到机器人识图描述的会显示 `[图片：描述]`，
  匹配不到就只有 `[图片]`；**媒体作为参考答案且没有描述时该题会被跳过**，避免无法判断；
- 进度存在 `data/blind/answers_v5.json`，下次运行自动续答；答案在 `data/blind/pack_v5.json`，
  **开始前不要打开**。

## 自动盲测（LLM 判官）

用一个「老群友」LLM 角色代替人做 A/B 盲测：

```powershell
& ".\venv\Scripts\python" -m language_lab blind-auto
& ".\venv\Scripts\python" -m language_lab blind-auto --limit 6 --no-swap
& ".\venv\Scripts\python" -m language_lab blind-auto --force   # 忽略缓存重问
```

- 角色定义：[judges/lao_qunyou.md](judges/lao_qunyou.md)
- 每题问「哪条是真人发的 / 更愿意看哪条」，并做 A/B 交换检查位置偏见；
- 结果缓存到 `data/blind_auto/cache/`，失败的缓存会自动重试；
- 输出 `reports/blind_auto_<时间>.md/.json`：AI 被识破率、位置一致性、偏好、分题型，
  若存在人工盲测结果还会做对照。

## 命令

```powershell
cd E:\QQbot
$env:PYTHONIOENCODING="utf-8"

# 1) 历史回填题目（一次性）
& ".\venv\Scripts\python" -m language_lab examiner --backfill

# 2) 实时出题（尾随 _live，90 秒后固化；首次默认只读新消息）
& ".\venv\Scripts\python" -m language_lab examiner --watch --interval 5
#    如需把已有 _live 也补成题目：加 --from-start

# 3) 跑一个评测回合
& ".\venv\Scripts\python" -m language_lab eval --n 12
#    只看缓存：--cache-only；指定题型：--types reply,mention

# 4) 更换规范
& ".\venv\Scripts\python" -m language_lab eval --spec language_lab\rules\rules_v2_prompt.txt
```

## 评测维度

| 维度 | 指标 |
|---|---|
| 形态 | 中位/平均长度、句尾无标点率、emoji、媒体、问句 |
| 分布 | 长度桶、气泡数、emoji、媒体的 JS 散度（越小越像） |
| 行为 | 媒体选择一致率、气泡数桶一致率、长度桶一致率 |
| 人工 | `blind_roundN.md`：A/B 匿名，判断哪条是 AI、是否愿意发到群里 |

## 首轮验收建议

- 12~36 题（覆盖 reply/mention/media/initiation/question/continue）；
- 生成失败率 <20%；
- 形态偏差：中位长度差 ≤3 字、无标点率差 ≤10pp、媒体率差 ≤10pp；
- 盲评：AI 被识破率 ≤65%；
- 只改规范中偏差最大的 1–3 条，重跑同一 test 集看是否改善且不退化。

## 防过拟合纪律

1. 规范只允许用 `train` 题目调；`test` 每轮固定抽样（可换 seed，但不能挑结果）。
2. 每轮报告保留 `roundN.json`，记录规范版本与指标，便于回溯。
3. 新规范上线前，必须同时看「旧题回归」和「新题抽样」，避免只对旧题变好。
4. 内容/幽默不写进规范，用检索例句；规范只留可测量的形态/行为约束。

## 已知局限

- 题目来自单群 7 天语料 + 实时增量；话题偏军事/游戏/日常；
- 真人回复是「一种可能」而非唯一答案，逐字相似没有意义，所以只看分布与人工判断；
- 媒体只有占位符，无法测「发什么图」，只能测「发不发媒体」；
- 生成依赖本地模型网关，模型抖动时失败率会升高（有缓存可复算）。
