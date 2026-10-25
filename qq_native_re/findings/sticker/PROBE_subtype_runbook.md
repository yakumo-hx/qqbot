# 贴图发送形态 —— 实机验证手册（待执行）

> 状态：**未执行**。用户明确要求本轮不发任何测试消息，因此 `send_sub_type=1` 目前只是
> `napcat.mjs` 的**源码取证结论**，没有实机渲染验证。本文件把那次验证固化成可复现步骤，
> 等用户方便时单独跑一轮。

## 为什么必须验证

普通 `[CQ:image]` 在 QQ 里渲染成**图片**（带图片边框、可点开大图），
而收藏表情渲染成**贴图**（小、无边框、更像"表情"）。用户明确在意这个观感差异。

源码证据链（四步闭合，全部来自 `napcat.mjs`）：

1. 发送侧段→元素映射 `[ze.image]` → `createValidSendPicElement(n, path, summary, e.data.sub_type, isLocal)`
   ⇒ **`sub_type` 被透传**，不是被忽略。
2. `createValidSendPicElement()` 内部：`picElement: { …, picSubType: i, … }`
   ⇒ **`sub_type` 落进 QQ 原生字段 `picElement.picSubType`**。
3. 接收侧解码：`sub_type: e.picSubType` ⇒ **收发是同一个字段**。
4. 本项目自己的判据 `scripts/image_utils.py::is_sticker()`：`sub_type == 1` 即"QQ 的贴图/动画表情"。

结论：image 段带 `sub_type=1` 应当就是贴图形态。但**渲染效果只有 QQ 客户端说了算**。

## 怎么验（三条对照消息，发到群 284517232 或任意指定群）

| # | 发送内容 | 目的 |
|---|---|---|
| 1 | `{"type":"image","data":{"file":"base64://…"}}` | 基线：现有 `send_group_image` 的普通图片形态 |
| 2 | `{"type":"image","data":{"file":"base64://…","sub_type":1}}` | 同一张 **GIF**，看是否变贴图 + 动画是否保留 |
| 3 | `{"type":"image","data":{"file":"file:///E:/QQbot/data/stickers/library/xxx.gif","sub_type":1}}` | 同一张图改走**本地路径**，看哪种投递方式对贴图生效 |

## 要看什么（逐条记录，不要只写"正常"）

1. 消息 2 是否**无图片边框**、是否可直接当表情点开
2. 消息 2 的 **GIF 动画是否自动播放**（贴图形态可能只显示首帧）
3. 大尺寸图（>500px）是否被拉伸变形
4. 消息 3 与消息 2 是否表现一致 ⇒ 决定实现用 `base64://` 还是 `file://`
5. 三条的**尺寸差异**（贴图通常渲染更小）

## 判定与分支

| 观察结果 | 处置 |
|---|---|
| 消息 2/3 呈现贴图、动画保留 | **保持 `send_sub_type: 1`**，实现按 `base64://` 投递即可 |
| 呈现贴图但动画丢失 | `send_sub_type: 1` 但**只入库静态图**，动态图退回 `sub_type: 0` |
| 与消息 1 无差别 | **改 `config/stickers.json` 的 `send_sub_type` 为 `0`**（配置级回退，无需改代码）；若仍要贴图观感，再评估 `add_custom_face` 收藏表情路线 |
| 消息 2 与 3 表现不一致 | 实现侧以表现更好的那种投递方式为准，并记进报告 |

## 证据要求

- 三条 `send_group_msg` 的 **NapCat 一手 fileLog 原文**（项目惯例：以 NapCat 侧日志为准，不以 bot 侧日志为准）
- 用户对渲染形态的**肉眼结论**（一句话即可，但要说清 5 个观察点）
- 截图可选；若截图，注意不要把群号/昵称以外的隐私内容带入仓库

## 回退（三层，任选其一即可，都不需要改代码）

1. **功能级**：`config/group_features.json` 把 `sticker` 改回 `false`（mtime 热加载，立即停用）
2. **渲染级**：`config/stickers.json` 的 `send_sub_type` 改成 `0`（退回普通图片形态）
3. **数据级**：删掉 `data/stickers/index.json`（判断库静默失效，机器人行为与改动前完全一致）

## 执行前检查

- [ ] 群 284517232 的 `sticker` 功能位已置 `true`（默认是 `false`，不会自动上线）
- [ ] `data/stickers/index.json` 已生成且至少有一张非 quarantine 的图
- [ ] 征得群内同意再发测试消息（三条会被真人看到）