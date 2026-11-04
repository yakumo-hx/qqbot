# -*- coding: utf-8 -*-
"""命令行入口：python -m language_lab [ingest|stats|select|all]"""

from __future__ import annotations

import argparse
import sys

from . import select, settings, stats, style
from .ingest import build, roster


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "capture":
        from . import capture_cli
        return capture_cli.main(argv[1:])
    if argv and argv[0] == "examiner":
        from language_lab.eval.examiner import main as examiner_main
        return examiner_main(argv[1:])
    if argv and argv[0] == "eval":
        from language_lab.eval.runner import main as eval_main
        return eval_main(argv[1:])
    if argv and argv[0] == "blind":
        from language_lab.eval.blind_cli import main as blind_main
        return blind_main(argv[1:])
    if argv and argv[0] == "blind-auto":
        from language_lab.eval.blind_auto import main as blind_auto_main
        return blind_auto_main(argv[1:])

    parser = argparse.ArgumentParser(prog="language_lab", description="群聊语言学习实验模块")
    parser.add_argument("task", nargs="?", default="all",
                        choices=["ingest", "stats", "select", "cards", "all"],
                        help="ingest=重建语料，stats=统计，select=成员候选，cards=风格卡，all=除 cards 外全流程；"
                             "capture=实时抓取（python -m language_lab capture --help）")
    parser.add_argument("--gid", help="覆盖配置里的群号")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    cfg = settings.load_config()
    if args.gid:
        cfg["gid"] = str(args.gid)
    gid = str(cfg["gid"])
    base = settings.PROJECT_ROOT
    ros = roster.load(settings.resolve((cfg.get("sources") or {}).get("roster") or "data/roster.json"), gid)

    if args.task in ("ingest", "all"):
        build.build(cfg, base, verbose=not args.quiet)
    if args.task in ("stats", "all"):
        stats.run(cfg, base, ros.unique_map(), verbose=not args.quiet)
    if args.task in ("select", "all"):
        select.run(cfg, base, ros)
    if args.task == "cards":
        style.run(cfg, base, verbose=not args.quiet)
    return 0
