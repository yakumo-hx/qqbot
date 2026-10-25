# Third-Party Notices

本仓库的私聊陪伴版包含一小段从第三方 MIT 项目移植的纠偏逻辑。

## love-companion

- 项目：https://github.com/MoYan1638/love-companion
- 使用位置：`scripts/private_chat/corrections.py`
- 移植内容：`scripts/persona/library.py` 中 `correct()` / `apply_corrections()` 的语义
  （wrong/right、right 为空即禁用、最近 50 条、编译时追加纠偏约束、生成后替换）。

```
MIT License

Copyright (c) 2026 MoYan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
