# 规范版本记录

## v4（2026-10-30）

依据 round 4（v3 过度收敛：气泡 1.19、问句 0%、无标点 100%）：

1. 气泡从「默认 1 条」改为「多数 1 条，约 1/3 可分 2 条」；
2. 恢复「约 10% 追问」；
3. 标点改为「句号很少用，不刻意补」；
4. 媒体场景给 `use_media` 示例（生成器里 one-shot）；
5. media_context 题库按「参考文字 / 参考媒体」各 60/天限额，并用 `-m` 独立 id 与其它题型共存。

文件：`rules_v4_prompt.txt`。

## v3（2026-10-30）

依据 eval round 3（v2：长度/气泡已对齐，媒体 0%、短句标点过度）：

1. 标点改为「短句不加，长句可正常用」——修正 v2 下 94% 无标点、真人长文有标点的偏差；
2. 长度按场景分档（接话 4~12、问句/起头 6~20、媒体 0~8）；
3. emoji 明确「有情绪/吐槽/回应表情包时可用」；
4. 媒体：上文有图时可直接回媒体（配合新 `media_context` 题型）。

文件：`rules_v3_prompt.txt`。评测：
```powershell
python -m language_lab eval --reuse 3 --spec language_lab\rules\rules_v3_prompt.txt
```

## v2（2026-10-30）

依据 eval round 2 的偏差（AI 从不发媒体、emoji 0%、气泡 1.83 vs 1.44、长度 12 vs 7）：

1. 媒体从「少用」改为「有条件使用」：媒体场景优先 `use_media=true`；
2. emoji 改为「表达情绪时用，约 3%」，不再暗示完全不用；
3. 气泡默认 1 条，确有补充才 2 条；
4. 加长度硬约束：多数 4~12 字、总长 ≤30 字；
5. 加接话约束：不复述对方、不总结、不客服腔。

文件：`rules_v2_prompt.txt`。评测命令：
```powershell
python -m language_lab eval --reuse 2 --spec language_lab\rules\rules_v2_prompt.txt
```

## v1（2026-10-30）

从 10-24 起 5,798 条干净语料提炼 A（形式）/B（节奏）/D（反面）三层规则，含跨天回测。
文件：`rules_v1.json` / `rules_v1.md` / `rules_v1_prompt.txt` / `validation.json`。
