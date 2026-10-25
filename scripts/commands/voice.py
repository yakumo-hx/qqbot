# -*- coding: utf-8 -*-
r"""【已停用 2026-10-15】语音转写模块 —— 本文件是墓碑，不导出任何符号。

原实现：把 [CQ:record] 语音消息 POST 到 http://127.0.0.1:6099/fetch_ptt_text 转成文字。

**实测该端点从未可用**：NapCat 的 onebot11 配置里 httpServers: []（没有 HTTP 动作服务），
6099 是 WebUI，上面所有 OneBot 动作一律 404，于是 resp.json() 抛
"Expecting value: line 1 column 1 (char 0)" —— 语音消息从来没有成功转写过。

用户于 2026-10-15 决定去掉该功能。以下位置已全部摘除：
  - scripts/commands/__init__.py 的 "from . import voice"
  - scripts/command_handler.py 的 process_voice_message 再导出
  - reverse_bot.py 的 [CQ:record 调用块与导入
  - scripts/commands/_state.py 的 NAPCAT_HTTP_URL / VOICE_TRANSCRIBE_TIMEOUT

**本文件不应被任何模块导入。** 之所以还留在磁盘上，是因为 scripts/ 目录属于
Medium 完整性级别，而沙箱里的 pwsh 子进程是 Low 完整性 —— 删除会被 Windows 拒绝
（UnauthorizedAccessException: Access to the path ... is denied）。
要彻底清掉，在**普通控制台**执行：Remove-Item 该文件 -Force
（完整路径见同目录 README 或决策日志 015）。

若将来要恢复语音转写，正确做法是**走反向 WebSocket**
（参考 scripts/interaction_menu.py 的 OneBotRPC），而不是 6099。

备份留档：qq_native_re/patches/voice_removal_backup_20261015-092516/
"""
