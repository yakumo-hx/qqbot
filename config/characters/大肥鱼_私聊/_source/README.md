# 人设源文件说明

当前 `config/private_chat.json` 使用：

```json
"persona": {
  "mode": "raw",
  "raw_file": "_source/raw.md",
  "raw_max_chars": 6000
}
```

这意味着：

- `_source/raw.md` 会被 private_bot **直接读取**，作为整张人设卡；每轮现读，改完下一轮生效；
- 不再解析 `identity.md` / `voice.md` 的 `##` 小节；
- `identity.md` / `voice.md` / `relationship.json` 仍保留，作为切回结构化模式的备份；
- **切回结构化**：把 `persona.mode` 改成 `"card"`，重启 private_bot；
- raw 文件缺失或为空时，会自动回退到 `card` 模式；
- raw 超过 `raw_max_chars` 会截断。

其他说明：

- `COMPRESS_NOTES.md` 是之前压缩版的取舍记录，只在 `card` 模式下有参考价值；
- `_source/` 里其他草稿文件不会被读取；
- 涉及未成年人、现实伤害或违法内容的部分不要放进运行时人设。
