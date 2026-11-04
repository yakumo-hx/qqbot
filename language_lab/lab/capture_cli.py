# -*- coding: utf-8 -*-
"""实时抓取服务：尾随 LLBot 日志 → 结构化 JSONL → 周期刷新规范语料。"""

from __future__ import annotations

import argparse
import json

from . import capture, settings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="language_lab capture", description="群聊数据实时抓取")
    parser.add_argument("--gid", help="覆盖配置里的群号")
    parser.add_argument("--once", action="store_true", help="抓一轮就退出（会刷新一次规范语料）")
    parser.add_argument("--init-eof", action="store_true", help="把已有日志的读取位置初始化到末尾，不回填")
    parser.add_argument("--interval", type=float, default=5.0, help="轮询间隔秒数")
    parser.add_argument("--ingest-every", type=float, default=300.0, help="每隔多少秒刷新规范语料")
    args = parser.parse_args(argv)

    cfg = settings.load_config()
    if args.gid:
        cfg["gid"] = str(args.gid)
    base = settings.PROJECT_ROOT
    if args.once:
        return capture.run(cfg, base, once=True, init_eof=args.init_eof,
                           interval=args.interval, ingest_every=0)
    print(json.dumps({"capture": "start", "gid": cfg["gid"], "interval": args.interval,
                      "ingest_every": args.ingest_every}, ensure_ascii=False), flush=True)
    return capture.run(cfg, base, once=False, init_eof=args.init_eof,
                       interval=args.interval, ingest_every=args.ingest_every)


if __name__ == "__main__":
    raise SystemExit(main())
