# -*- coding: utf-8 -*-
"""生产树落盘后证明（PR#1 retry landing）。

目的：
  1) 用**模块文件绝对路径**证明校验确实跑在 E:\\QQbot 生产树上，而不是 _pr1_stage/verify/ 隔离副本；
  2) 逐条复述 PR#1 关键不变量（含并行 search 工作流带来的 _FEATURE_KEYS 变化）。
用法（cwd = 项目根）：python qq_native_re/findings/pr1/check_prod_proof.py
"""
import json
import os
import sys

ROOT = r"E:\QQbot"
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

failures = []


def check(ok, label):
    print("  [%s] %s" % ("PASS" if ok else "FAIL", label))
    if not ok:
        failures.append(label)


def main():
    import reverse_bot as rb
    from scripts import interaction_menu as im
    from scripts.commands import _state, _registry, interaction as icmd
    from scripts import command_handler as ch

    print("=== 1. 模块来源（必须是 E:\\QQbot 生产树） ===")
    for label, mod in (("reverse_bot", rb), ("scripts.interaction_menu", im),
                       ("scripts.commands._state", _state),
                       ("scripts.commands.interaction", icmd),
                       ("scripts.command_handler", ch)):
        p = os.path.abspath(mod.__file__)
        in_prod = p.lower().startswith(ROOT.lower()) and "_pr1_stage" not in p
        print("  %-32s %s" % (label, p))
        check(in_prod, "模块 %s 来自生产树（非 _pr1_stage/verify）" % label)

    print()
    keys = tuple(rb._FEATURE_KEYS)
    print("  _FEATURE_KEYS = %r" % (keys,))
    check("search" in keys, "并行 search 工作流的 'search' 键仍在（未被本波回退）")
    check("poke_menu" not in keys and "reaction_menu" not in keys,
          "_FEATURE_KEYS 不含 poke_menu / reaction_menu（末界）")
    # 2026-10-15：加入 sticker 后由 7 项变 8 项。
    # 判据与 vision / search 同类 —— 三者都只在「消息路径」被消费（decision_engine 段），
    # 放进这道群级短路门是一致的；poke_menu / reaction_menu 不进（它们走 notice 路径，
    # 且 interaction_menu.enabled() 本来就要求 commands，见 REVIEW.md S4）。
    check(len(keys) == 8, "_FEATURE_KEYS == 8 项（6 旧 + search + sticker）")
    for name in ("handle_command", "_mystery_queue", "_dedup_lock",
                 "BOT_QQ", "VERSION", "init_handlers", "processed_msg_ids", "PROCESSED_MSG_MAX",
                 "known_groups", "find_valid_number"):
        check(hasattr(ch, name), "门面再导出 %s" % name)

    print()
    print("=== 3. 默认全关（未配置群） ===")
    menus = rb.interaction_menus
    check(menus.enabled("999999999") is False, "未配置群 enabled() == False")
    # sticker 也必须默认关闭：新增群不能自动拿到这个功能位（FEATURE_DEFAULTS 里排除）
    try:
        from scripts import group_manager as gm
        check(gm.FEATURE_DEFAULTS.get("sticker") is False,
              "FEATURE_DEFAULTS 里 sticker 默认值为 False（新增群默认关闭）")
    except Exception as exc:
        check(False, "无法读取 group_manager.FEATURE_DEFAULTS: %s" % exc)

    print()
    print("=== 4. group_features.json 末界（内容级） ===")
    with open(os.path.join(ROOT, "config", "group_features.json"), encoding="utf-8") as f:
        gf = json.load(f)
    for gid, entry in gf.items():
        feats = entry.get("features", {})
        print("  %s %s -> %s" % (gid, entry.get("group_name", ""), sorted(feats)))
        # 2026-10-17：用户要求 1102194840 的所有功能与 284517232 对齐，两群菜单功能位均已开启。
        if gid in ("284517232", "1102194840"):
            # 决策 013：PR#1 在 284517232 验收通过后由用户开启；1102194840 于 2026-10-17 对齐
            check(feats.get("poke_menu") is True and feats.get("reaction_menu") is True,
                  "群 %s 的 poke_menu / reaction_menu 已开启" % gid)
        else:
            check("poke_menu" not in feats and "reaction_menu" not in feats,
                  "群 %s 未启用 poke_menu / reaction_menu（保持关闭）" % gid)
        # 2026-10-15（sticker 波次）：白名单语义下缺失即为关闭；这里守住「不会自动上线」
        # 2026-10-15（sticker 波次）：用户决定在 284517232 与 1102194840 上线，其余群保持关闭。
        # 白名单语义下缺失即为关闭，所以「没开」是默认态、也是安全态。
        if gid in ("284517232", "1102194840"):
            check(feats.get("sticker") is True,
                  "群 %s 的 sticker 已启用（用户 2026-10-15 决定上线）" % gid)
        else:
            check(feats.get("sticker") is not True,
                  "群 %s 未启用 sticker（保持关闭）" % gid)

    print()
    print("=== 5. config/interaction_menu.json 语法 ===")
    with open(os.path.join(ROOT, "config", "interaction_menu.json"), encoding="utf-8") as f:
        imcfg = json.load(f)
    check("defaults" in imcfg and "groups" in imcfg, "interaction_menu.json 结构齐备")
    check(imcfg.get("groups") == {}, "groups 为空 ⇒ 无任何群被启用")

    print()
    print("PROD_PROOF_FAILURES=%d %s" % (len(failures), failures or ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
