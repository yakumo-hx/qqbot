# 决策日志 015 — 去掉语音转写功能

> 日期：2026-10-15　｜　执行：Lead
> 结论：**功能已从代码中彻底摘除；文件本身因 OS 权限限制未能删除，改为惰性墓碑**

## 一、为什么去掉

不是「不用了」，而是**它从来就没工作过**。

`scripts/commands/voice.py` 的实现是把 `[CQ:record]` 语音消息 POST 到
`http://127.0.0.1:6099/fetch_ptt_text` 取转写文本。但：

| 事实 | 证据 |
|---|---|
| NapCat 的 `onebot11` **没有 HTTP 动作服务** | 配置里 `httpServers: []` |
| 6099 是 **WebUI**，不是 OneBot | 探测：`get_login_info` / `get_group_list` / `fetch_ptt_text` **全部 404** |
| 所以 `resp.json()` 拿到的不是 JSON | 生产日志：`[语音转写] API 调用失败: Expecting value: line 1 column 1 (char 0)` |
| bot 侧真实的动作通道是**反向 WebSocket** | `interaction_menu.py` 的 `OneBotRPC` 就是这么发动作的 |

**结论：语音消息从来没有成功转写过**，每次都是失败后保留原 CQ 码。

## 二、改了什么（8 处）

| # | 位置 | 改动 |
|---|---|---|
| 1 | `scripts/commands/__init__.py` | 去掉 `from . import voice` |
| 2 | `scripts/command_handler.py` | 去掉 `process_voice_message` 再导出 |
| 3 | `reverse_bot.py` | 去掉 `process_voice_message` 导入 |
| 4 | `reverse_bot.py` | 去掉 `[CQ:record` 调用块 |
| 5 | `scripts/commands/_state.py` | 去掉 `NAPCAT_HTTP_URL` / `VOICE_TRANSCRIBE_TIMEOUT` |
| 6 | `scripts/commands/voice.py` | 内容替换为惰性墓碑（**文件未能删除**，见 §四） |
| 7 | `AGENTS.md` | 模块计数 14→13；再导出契约去掉 `process_voice_message`；说明 voice 已停用 |
| 8 | `README.md` | 入口职责、模块计数同步 |

另：`qq_native_re/findings/pr1/check_prod_proof.py` 的再导出断言同步去掉该符号。

## 三、验证

| 项 | 结果 |
|---|---|
| `import reverse_bot` | **OK**，VERSION 2.5.0 |
| 门面 `hasattr(ch, 'process_voice_message')` | **False**（符号已移除） |
| 墓碑模块可导入且无导出 | True |
| 注册指令数 | **33**（`voice.py` 本就没有 `@register`，不受影响） |
| 四个关键文件残留引用 | **0** |
| `check_prod_proof.py` | **PROD_PROOF_FAILURES=0** |

## 四、一个未能完成的动作：文件没删掉

`Remove-Item scripts\commands\voice.py` 被 Windows 拒绝：

```
UnauthorizedAccessException: Access to the path 'E:\QQbot\scripts\commands\voice.py' is denied.
```

对照实验定位到根因是**路径**而非命令：

| 位置 | 沙箱 pwsh 能否建/删 |
|---|---|
| `E:\QQbot\`（仓库根） | **能** |
| `E:\QQbot\scripts\` | **不能** |

即 `scripts/` 属 **Medium 完整性**，沙箱 pwsh 子进程是 **Low 完整性**，无写权限。
（DSH 的 write/edit 工具能写该目录，所以我改得动文件、却删不掉文件。）

一次提权重试因等待批准超时作罢。**因此改为墓碑**：不导出任何符号、写清来龙去脉、
并在文件头给出人工删除命令。这样即便无人处理也不会被误导入。

要彻底清掉，在**普通控制台**执行：

```powershell
Remove-Item E:\QQbot\scripts\commands\voice.py -Force
Remove-Item E:\QQbot\scripts\commands\__pycache__\voice.cpython-*.pyc -Force
```

## 五、回退

备份点：`qq_native_re/patches/voice_removal_backup_20261015-092516/`
（含 8 个改动前文件 + `MANIFEST.tsv` 记录的 SHA256）

注意：即使还原代码，**它依然不工作**——除非先给 NapCat 配上 HTTP 动作服务，
或把实现改成走反向 WebSocket。

## 六、遗留待办

1. **删除墓碑文件**（上面两条命令，需普通控制台）；
2. **恢复语音转写的正确姿势**：走反向 WebSocket（`interaction_menu.py` 的 `OneBotRPC` 范式），
   而不是 6099；若要做，需先确认 NapCat 侧 `fetch_ptt_text` 动作确实可用；
3. **`_state.py` 里可能还有别的「指向 6099 的僵尸常量」**，本次一并清了两个；
   后续若新增 NapCat 动作调用，务必先确认通道（WS 而非 6099）。