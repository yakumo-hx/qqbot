# -*- coding: utf-8 -*-
"""QQbot 单人私聊陪伴版 0.0.1。

这是一个独立进程/独立端口的新版本：
- 只处理私聊，不处理群聊；
- 只服务 config/private_chat.json 里的 allowed_users；
- 数据全部落在 data/private/；
- 群聊入口 reverse_bot.py 完全不受影响。
"""

from .config import VERSION
from .engine import PrivateChat

__all__ = ["VERSION", "PrivateChat"]
