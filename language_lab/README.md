# language_lab — 群聊语言学习实验模块

**独立模块**：不 import、不修改机器人现有代码（唯一的例外是 2026-10-28 新增的旁路 recorder，
见下）。目标：先把「像本群真人一样发消息」的语料/统计/风格卡/生成/评测闭环跑通，
暂不接回机器人、不自动发 QQ 消息。

## 当前进度（2026-10-30）

- **语料 v1**：10,959 条（10-20 ~ 10-30），LLBot 主源 + recorder 实时落盘；
  机器人日志补 10-24 之前的文本（无 uid/媒体）。
- **规则 v1**：`rules/`（独立子目录）——用 10-24 起 5,798 条干净人类消息提炼 A（形式）/B（节奏）/D（反面）
  三层规则，含跨天回测与给生成器的紧凑提示词。
- **统计基线**：见 `reports/corpus_stats.md`。
- **风格卡**：`reports/group_register.*` + `reports/style_cards/`（模型故障时退回统计卡）。
- **Top 6 个人卡**：`reports/candidates.md` + `reports/style_cards/<uid>.json`。
- **recorder**：`scripts/group_history.py`，被动落盘到 `data/group_history/_live/<gid>/`。

## 隐私与授权

- 语料仅用于本群（284517232）内部表达风格参考，已获得群成员许可（由模块所有者确认）。
- 原始语料仅本机保存，默认保留 90 天；`data/`、`reports/` 已在 `.gitignore` 中排除，**不要提交**。
- 报告只展示聚合指标与经过过滤的短例句，不导出完整聊天记录。
- 风格卡生成后禁止逐字复用原句（卡内带泄漏检查，生成器带 n-gram 泄漏检查）。

## 数据来源

| 来源 | 路径 | 特点 |
|---|---|---|
| LLBot 日志 | `E:\QQbot-trials\llbot\bin\llbot\data\logs\llbot-*.log` | 主源：秒级时间、uid、媒体占位；10-24 起 |
| 机器人日志 | `logs/bot.2026-*.log` | 10-20 起；纯文本 + 回复引用；无 uid，昵称可能张冠李戴 |
| 名册 | `data/roster.json` | 昵称/群名片 → uid，仅唯一命中才采用 |
| recorder | `data/group_history/_live/` | 重启后实时落盘，字段最全 |

## 目录

```
language_lab/
├─ config/lab.json          # 群号、bot 名单、窗口、筛选阈值
├─ rules/                   # 「像人说话」规则 v1（提取脚本 + 规则 + 回测）
├─ eval/                    # 出题人 + 生成器 + 评分器（测试-比较-修改循环）
├─ lab/
│  ├─ ingest/               # llbot.py botlog.py roster.py build.py
│  ├─ stats.py              # 语料统计 + 群语域卡
│  ├─ select.py             # 成员筛选（Top N）
│  ├─ style.py              # 风格卡（模型蒸馏，失败退回统计卡）
│  └─ cli.py
├─ data/group_history/      # 规范语料（gitignored）
├─ data/group_history/_live # recorder 原始落盘（gitignored）
├─ reports/                 # 统计/候选/风格卡（gitignored）
└─ tests/                   # 离线 unittest
```

## 用法

```powershell
cd E:\QQbot
$env:PYTHONIOENCODING="utf-8"
& ".\venv\Scripts\python" -m language_lab ingest     # 解析日志 → 规范语料
& ".\venv\Scripts\python" -m language_lab stats      # 统计 + 群语域卡
& ".\venv\Scripts\python" -m language_lab select     # 成员候选 Top 6
& ".\venv\Scripts\python" -m language_lab cards      # 风格卡（离线，调模型，可重试）
& ".\venv\Scripts\python" -m language_lab all        # ingest + stats + select
& ".\venv\Scripts\python" -m language_lab capture    # 实时抓取（尾随 LLBot 日志，不需要重启机器人）
& ".\venv\Scripts\python" -m unittest language_lab.tests.test_parsers -v
```

实时抓取：

```powershell
# 常驻：每 5s 抓新消息、每 300s 刷新规范语料
& ".\venv\Scripts\python" -m language_lab capture --interval 5 --ingest-every 300
# 或普通控制台双击脚本
.\tools\start_language_capture.ps1
# 抓一轮就退出（并刷新规范语料）
& ".\venv\Scripts\python" -m language_lab capture --once
```

抓取服务只读 LLBot 日志、只写 `data/group_history/_live/` 与规范语料；不发送、不调模型、
不改机器人。机器人侧 recorder 是同一份 schema 的更全版本（需要重启后生效），两路数据
在 `ingest` 时按 uid+时间+文本去重，recorder 优先。

## 数据质量纪律（踩过的坑）

1. **昵称会漂移**：同一昵称在不同时期可能属于不同 uid（实测"爱莉不知道哦"曾同时是
   408754232 和 2475663424）。因此 LLBot 覆盖期外只用名册反查并标 `uid_src="roster"`；
   成员筛选窗口固定为 10-24 起（uid 干净）。
2. **机器人日志不记媒体**：`[指代]` 几乎不落图片/贴图，LLBot 才是媒体占位与全量来源。
3. **日志不按天切分**：没有 `bot.2026-10-23.log`，当天的消息在 10-22 的文件里，
   必须扫全量日志并按行内日期过滤。
4. **秒级不足以保序**：群里同秒多条很常见，规范语料用 `seq` 保序。

## 阶段

- **D1（已完成）**：语料归并 + 统计 + 群语域 + Top 6 候选 + recorder
- **D2（进行中）**：风格卡 + 接话检索库 + 生成器 v0 + 30 条样例
- **D3**：判别式重排（n-gram 指纹）+ 多候选 + 行为采样 + shadow 评测
- **D4**：可区分性测试 + 集成设计（仍不改机器人回复链路）
