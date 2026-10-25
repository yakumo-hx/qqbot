#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``scripts/offline_watchdog.py`` 的离线单测。

设计约束（务必保持）：
  * **不杀任何真进程**：killer / launcher / lister / notifier 全部注入假实现；
  * **不起真弹窗**：notifier 是假的，弹窗相关只做**静态**断言（脚本内容 + base64 解码）；
  * **不依赖 pytest**：本仓库没有测试框架，自带 runner（``python scripts/test_offline_watchdog.py``）。

覆盖：90s 触发 / 120s 重复 / 同时只一个弹窗 / restart 关闭时零进程动作 /
限流两条 / 启动未满 60s 不抢跑 / 路径过滤 / dry_run 不动作 / 停机结算 /
配置 mtime 热加载与非法回退 / 冷启动 armed / 空闲零唤醒 / 静默弹窗静态证据。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.offline_watchdog import (          # noqa: E402
    DEFAULT_CONFIG,
    ConfigStore,
    OfflineWatchdog,
    ProcInfo,
    build_popup_argv,
    build_popup_ps1,
    is_napcat_mirror_path,
    load_config,
    parse_config,
)

# ------------------------------------------------------------------ 测试脚手架

_TESTS = []
NAPCAT_EXE = r"E:\QQbot\NapCat\NapCat.44498.Shell\QQ.exe"
PERSONAL_EXE = r"E:\Program Files\Tencent\QQNT\QQ.exe"
PERSONAL_EXE_C = r"C:\Program Files\Tencent\QQNT\QQ.exe"
EVIL_EXE = r"E:\QQbot\NapCatEvil\QQ.exe"


def test(fn):
    _TESTS.append(fn)
    return fn


def eq(actual, expected, what=""):
    if actual != expected:
        raise AssertionError(f"{what}: 期望 {expected!r}，实际 {actual!r}")


def ok(cond, what=""):
    if not cond:
        raise AssertionError(f"断言失败: {what}")


class FakeClock:
    def __init__(self, t=0.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += float(dt)
        return self.t


class Recorder:
    """记录调用；可选固定返回值或每次新建（``factory``）。"""

    def __init__(self, ret=None, factory=None):
        self.calls = []
        self.ret = ret
        self.factory = factory

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.factory is not None:
            return self.factory(*args, **kwargs)
        return self.ret

    @property
    def count(self):
        return len(self.calls)


class FakePopup:
    """假弹窗进程：``poll()`` 返回 None 表示还开着。"""

    def __init__(self):
        self.alive = True
        self.terminated = 0

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive = False
        self.terminated += 1

    def close(self):
        self.alive = False


class FakeNotifier:
    def __init__(self):
        self.texts = []
        self.procs = []

    def __call__(self, text):
        self.texts.append(text)
        popup = FakePopup()
        self.procs.append(popup)
        return popup


class StaticStore:
    """固定配置源（替代 mtime 热加载），可选地在第 N 次 get 后改变配置。"""

    def __init__(self, cfg=None, switch_after=None, new_cfg=None):
        self.cfg = parse_config(cfg if cfg is not None else {})
        self.switch_after = switch_after
        self.new_cfg = parse_config(new_cfg) if new_cfg is not None else None
        self.calls = 0

    def get(self):
        self.calls += 1
        if self.switch_after is not None and self.calls > self.switch_after \
                and self.new_cfg is not None:
            return self.new_cfg
        return self.cfg


class LogCapture:
    def __init__(self):
        self.lines = []

    def info(self, msg):
        self.lines.append(msg)

    def warning(self, msg):
        self.lines.append(msg)

    def text(self):
        return "\n".join(self.lines)


def make_proc(pid, path, age=500.0, name="QQ.exe"):
    return ProcInfo(pid, name, path, age)


class Harness:
    """一套假依赖 + 假时钟的看门狗。"""

    def __init__(self, cfg=None, procs=None, store=None, popup=None):
        self.clock = FakeClock()
        self.log = LogCapture()
        self.procs = list(procs or [])
        self.lister = Recorder(factory=lambda: list(self.procs))
        self.killer = Recorder(ret=True)
        self.launcher = Recorder(ret=("popen", object()))
        self.notifier = popup or FakeNotifier()
        self.store = store or StaticStore(cfg)
        self.wd = OfflineWatchdog(
            logger=self.log, clock=self.clock, config_store=self.store,
            notifier=self.notifier, process_lister=self.lister,
            killer=self.killer, launcher=self.launcher,
        )

    # 便捷操作
    def connect(self, t=None):
        if t is not None:
            self.clock.t = float(t)
        self.wd.on_connected()
        return self.wd

    def disconnect(self, t=None):
        if t is not None:
            self.clock.t = float(t)
        self.wd.on_disconnected()
        return self.wd

    def tick(self, t):
        self.clock.t = float(t)
        return self.wd.tick()

    def sweep(self, end, step=10.0):
        """从当前时刻扫到 end，并断言"下一个唤醒点永远在未来"（无忙等）。"""
        actions = []
        while self.clock.t <= end:
            actions.extend(self.wd.tick())
            deadline = self.wd.next_deadline()
            if deadline is not None:
                ok(deadline > self.clock.t,
                   f"next_deadline 必须是未来时刻（t={self.clock.t} deadline={deadline}）")
            self.clock.advance(step)
        return actions


def na(actions):
    """过滤掉告警动作：重启类用例只关心进程动作（告警另有专门用例覆盖）。

    2026-10-20 告警阈值改为 5 秒后，每个 restart 用例的 tick 里都会**同时**
    多出一个 alert 动作；本助手把「重启语义」与「告警语义」解耦，避免断言脆弱。
    """
    # 只滤掉「纯告警」动作；alert_dry_run / alert_failed 等仍保留给各自的用例断言
    return [a for a in actions if a != "alert"]


def base_cfg(**overrides):
    raw = json.loads(json.dumps(DEFAULT_CONFIG))
    for key, value in overrides.items():
        if isinstance(value, dict):
            raw.setdefault(key, {}).update(value)
        else:
            raw[key] = value
    return raw


# ------------------------------------------------------------------ L2 告警

@test
def test_alert_fires_at_5s_not_before():
    """2026-10-20：阈值 90s → 5s（用户要求"一掉线就弹窗"）。"""
    h = Harness()
    h.connect(0)
    h.disconnect(0)
    eq(h.tick(4.9), [], "4.9s 时不应有任何动作")
    eq(len(h.notifier.texts), 0, "4.9s 不应弹窗")
    eq(h.tick(5), ["alert"], "5s 恰好应触发弹窗")
    eq(len(h.notifier.texts), 1, "弹窗次数")
    ok("账号已离线 5 秒" in h.notifier.texts[0], f"文案应含停机秒数: {h.notifier.texts[0]}")
    ok("扫码" in h.notifier.texts[0], "文案含行动指引")
    ok("【掉线告警】" in h.notifier.texts[0], "文案含前缀")


@test
def test_alert_repeats_every_120s():
    h = Harness()
    h.connect(0)
    h.disconnect(0)
    h.tick(90)
    eq(len(h.notifier.texts), 1, "首次弹窗")
    h.notifier.procs[-1].close()             # 用户关掉了弹窗
    eq(h.tick(209), [], "距上次 119s，未到重复点")
    eq(len(h.notifier.texts), 1, "不应重复弹窗")
    eq(h.tick(210), ["alert"], "距上次 120s，应重复弹窗")
    eq(len(h.notifier.texts), 2, "重复弹窗次数")
    ok("4 分钟" in h.notifier.texts[1], f"重复弹窗文案应带最新停机时长: {h.notifier.texts[1]}")


@test
def test_only_one_popup_at_a_time():
    h = Harness()
    h.connect(0)
    h.disconnect(0)
    eq(h.tick(90), ["alert"], "首次弹窗")
    # 弹窗一直开着（用户没关）⇒ 后续到期只能跳过，绝不堆叠
    eq(h.tick(210), ["alert_skipped_open"], "重复到期但弹窗未关 ⇒ 跳过")
    eq(h.tick(330), ["alert_skipped_open"], "持续未关 ⇒ 继续跳过")
    eq(len(h.notifier.texts), 1, "全程只允许一个弹窗")
    eq(len(h.notifier.procs), 1, "只创建过一个弹窗进程")
    # 用户关掉后，从"上次到期点"起算 120s 再弹
    h.notifier.procs[-1].close()
    eq(h.tick(449), [], "未到 120s")
    eq(h.tick(450), ["alert"], "关掉后满 120s 应重新弹窗")
    eq(len(h.notifier.texts), 2, "第二次弹窗")


@test
def test_reconnect_settles_downtime_and_closes_popup():
    h = Harness()
    h.connect(0)
    h.disconnect(0)
    h.tick(90)
    eq(len(h.notifier.procs), 1, "弹窗已开")
    ok(h.wd.state()["alert_open"], "state 应报告弹窗开启")
    h.connect(600)                            # 10 分钟后重连
    ok("本轮停机 10.0 分钟" in h.log.text(), f"应结算停机时长:\n{h.log.text()}")
    ok(h.notifier.procs[0].terminated == 1, "重连应关闭残留弹窗（保证不堆叠）")
    ok(not h.wd.state()["alert_open"], "重连后弹窗应为关闭态")
    eq(h.wd.state()["down_seconds"], None, "重连后停止计时")
    eq(h.tick(1000), [], "重连后 tick 应无动作")


@test
def test_short_disconnect_does_not_alert():
    h = Harness()
    h.connect(0)
    h.disconnect(0)
    h.tick(3)
    h.connect(3)
    eq(len(h.notifier.texts), 0, "3 秒自愈不应告警（低于 5s 阈值）")
    ok("本轮停机" in h.log.text(), f"应有停机结算日志:\n{h.log.text()}")


@test
def test_cold_start_is_armed():
    """bot 启动后 NapCat 一直没连上，也要告警。"""
    h = Harness()
    h.wd.on_start()
    eq(h.wd.state()["connected"], False, "冷启动视为未连接")
    eq(h.tick(59), [], "未到冷启动宽限 60s（与真掉线的 5s 分开）")
    eq(h.tick(60), ["alert"], "启动后 60s 仍无连接 ⇒ 告警")
    eq(len(h.notifier.texts), 1, "冷启动告警次数")


@test
def test_disconnect_is_idempotent():
    h = Harness()
    h.connect(0)
    h.disconnect(0)
    h.disconnect(500)                         # 重复通知不得重置计时
    eq(h.tick(90), ["alert"], "90s 仍应触发（计时未被重置）")


@test
def test_alert_disabled_is_silent():
    h = Harness(cfg=base_cfg(alert={"enabled": False}))
    h.connect(0)
    h.disconnect(0)
    h.sweep(1000, 10)
    eq(len(h.notifier.texts), 0, "alert.enabled=false ⇒ 不弹窗")


# ------------------------------------------------------------------ L1 重启 + 安全红线

@test
def test_restart_disabled_means_zero_process_actions():
    """**核心安全不变量**：restart.enabled=false ⇒ 零进程动作。"""
    h = Harness(cfg=base_cfg(restart={"enabled": False}),
                procs=[make_proc(43232, NAPCAT_EXE)])
    h.connect(0)
    h.disconnect(0)
    actions = h.sweep(2000, 10)
    eq(h.lister.count, 0, "关闭时连枚举进程都不允许调用")
    eq(h.killer.count, 0, "关闭时绝不允许杀进程")
    eq(h.launcher.count, 0, "关闭时绝不允许起进程")
    ok("restart" not in " ".join(actions), f"不应出现任何 restart 动作: {set(actions)}")
    eq(len(h.notifier.texts), 1, "告警仍必须工作（只叫人不动手）")
    eq(h.wd.state()["restart_enabled"], False, "state 应报告重启关闭")


@test
def test_repo_config_matches_deployed_settings():
    """仓库实际部署的 config/auto_restart.json。

    2026-10-21：**L1 已重新关闭**。2026-10-20 曾开启并实机验证，发现：
      bot 非管理员运行，而镜像 QQ 是提权进程 ⇒ taskkill 全部 Access denied；
      杀不掉却每次仍新起一个 ⇒ 镜像 NapCat 实例堆积（实测 4 个）。
    在"提权计划任务"方案落地前必须保持关闭，否则有害无益。
    详见 qq_native_re/decisions/017_disconnect_taxonomy.md
    """
    cfg = load_config(os.path.join(PROJECT_ROOT, "config", "auto_restart.json"))
    eq(cfg["restart"]["enabled"], True, "仓库配置：L1 已开启（待观察后转正式）")
    # ★ 关键安全姿态：修好名字比较后先跑 dry-run。dry_run 在 kill/launch 之前就 return，
    #   因此即使运行中的进程还是旧代码，也不会产生任何进程动作。
    eq(cfg["dry_run"], True, "必须先 dry_run 观察一轮，确认选中的 PID 正确")
    eq(cfg["alert"]["after_seconds"], 5.0, "告警仍为 5 秒")
    eq(cfg["restart"]["min_interval_seconds"], 300.0, "限流仍在：最小间隔 300s")
    eq(cfg["restart"]["max_per_hour"], 3, "限流仍在：每小时最多 3 次")
    eq(cfg["alert"]["enabled"], True, "告警默认开")
    eq(cfg["alert"]["after_seconds"], 5.0, "告警阈值 5s（2026-10-20 由 90s 改）")
    eq(cfg["alert"]["startup_grace_seconds"], 60.0, "冷启动宽限 60s")
    eq(cfg["alert"]["repeat_seconds"], 120.0, "重复间隔 120s")
    eq(cfg["restart"]["after_seconds"], 60.0, "重启阈值 60s")
    eq(cfg["restart"]["min_interval_seconds"], 300.0, "重启最小间隔 300s")
    eq(cfg["restart"]["max_per_hour"], 3, "每小时上限 3 次")
    eq(cfg["dry_run"], True, "dry_run 处于观察态（只记日志不动进程）")


@test
def test_path_filter_only_mirror_processes_are_killed():
    """假进程列表：只允许选中 NapCat 镜像目录下的那个。"""
    h = Harness(
        cfg=base_cfg(restart={"enabled": True, "after_seconds": 60,
                              "min_interval_seconds": 300, "max_per_hour": 3}),
        procs=[
            make_proc(43232, NAPCAT_EXE, 600),          # ✔ 镜像路径
            make_proc(6708, PERSONAL_EXE, 600),         # ✘ 个人 QQNT（实测存在 5 个）
            make_proc(23636, PERSONAL_EXE_C, 600),      # ✘ 个人 QQNT（C 盘）
            make_proc(9999, EVIL_EXE, 600),             # ✘ 前缀相似但不同目录
            make_proc(8888, "", 600),                   # ✘ 路径未知
        ],
    )
    h.connect(0)
    h.disconnect(0)
    eq(na(h.tick(60)), ["restart"], "60s 应触发重启阶梯")
    eq([call[0][0].pid for call in h.killer.calls], [43232],
       "只允许结束镜像路径下的 PID 43232")
    eq(h.launcher.count, 1, "应启动一次 NapCat 启动脚本")


@test
def test_path_predicate_edges():
    ok(is_napcat_mirror_path(NAPCAT_EXE), "镜像路径应判真")
    ok(is_napcat_mirror_path(r"E:\QQbot\NapCat\NapCatWinBootMain.exe"), "镜像根下应判真")
    ok(is_napcat_mirror_path("e:/qqbot/napcat/napcat.44498.shell/qq.exe"), "大小写/斜杠不敏感")
    ok(not is_napcat_mirror_path(PERSONAL_EXE), "个人 QQNT 应判假")
    ok(not is_napcat_mirror_path(EVIL_EXE), "前缀相似目录应判假")
    ok(not is_napcat_mirror_path(r"E:\QQbot\NapCat"), "镜像根自身不是文件路径")
    ok(not is_napcat_mirror_path(""), "空路径应判假")
    ok(not is_napcat_mirror_path(None), "None 应判假")


@test
def test_killer_name_whitelist_normalizes_extension():
    """★ 2026-10-21 真因回归：L1 从未杀掉任何进程。

    default_process_lister 用 PowerShell 的 $p.ProcessName，返回**不带扩展名**的
    基名 'QQ'；而 NAPCAT_PROCESS_NAMES 写的是 'QQ.exe'。
    旧比较 'qq' in {'qq.exe', ...} 恒为 False ⇒ 每个目标都被判"白名单外进程"，
    SafetyError 全拒 ⇒ 杀不掉；旧逻辑仍会启动新实例 ⇒ 实例堆积 5→10→15。
    """
    from scripts.offline_watchdog import _is_allowed_proc_name

    ok(_is_allowed_proc_name("QQ"), "'QQ'（PowerShell 风格，无扩展名）必须放行")
    ok(_is_allowed_proc_name("QQ.exe"), "'QQ.exe' 也必须放行")
    ok(_is_allowed_proc_name("NapCatWinBootMain"), "启动器基名必须放行")
    ok(_is_allowed_proc_name("napcatwinbootmain.exe"), "大小写不敏感")
    ok(not _is_allowed_proc_name("python"), "白名单外必须拒绝")
    ok(not _is_allowed_proc_name("QQNT"), "相似名必须拒绝（不得前缀匹配）")
    ok(not _is_allowed_proc_name("QQ.exe.bak"), "扩展名不符必须拒绝")
    ok(not _is_allowed_proc_name(""), "空名必须拒绝")
    ok(not _is_allowed_proc_name(None), "None 必须拒绝")


@test
def test_lister_name_and_killer_whitelist_are_compatible():
    """枚举器产出的名字必须能通过 killer 的白名单 —— 这是两者间的契约。

    历史上这两处各自独立演化，枚举器给 'QQ'、白名单收 'QQ.exe'，
    中间没有任何测试守护，于是 L1 静默失效了整整一个版本。
    """
    from scripts.offline_watchdog import _is_allowed_proc_name

    # 枚举脚本里 Get-Process -Name 的取值就是不带扩展名的基名
    for enumerated in ("QQ", "NapCatWinBootMain", "NapCatWinBootHook"):
        ok(_is_allowed_proc_name(enumerated),
           f"枚举器产出的 {enumerated!r} 必须能被 killer 接受")


@test
def test_default_killer_refuses_non_mirror_path():
    """默认 killer 的第二道保险：非镜像路径直接 SafetyError。"""
    from scripts.offline_watchdog import SafetyError, default_killer
    for proc in (make_proc(6708, PERSONAL_EXE),
                 make_proc(1, NAPCAT_EXE, name="python.exe")):
        try:
            default_killer(proc)
        except SafetyError:
            continue
        raise AssertionError(f"应拒绝结束 {proc!r}")


@test
def test_throttle_min_interval_behavior():
    """行为层面：每 5 分钟最多 1 次。"""
    h = Harness(cfg=base_cfg(restart={"enabled": True, "after_seconds": 60,
                                      "min_interval_seconds": 300, "max_per_hour": 3}),
                procs=[make_proc(43232, NAPCAT_EXE, 600)])
    h.connect(0)
    h.disconnect(0)
    h.sweep(600, 10)
    eq(h.killer.count, 2, f"600s 内最多 2 次重启（60s / 360s），实际 {h.killer.count}")
    eq([call[0][0].pid for call in h.killer.calls], [43232, 43232], "两次目标一致")


@test
def test_throttle_min_interval_guard_directly():
    """直接驱动内部闸门：即便截止时间已过，间隔未满也必须拒动。"""
    cfg = base_cfg(restart={"enabled": True, "after_seconds": 60,
                            "min_interval_seconds": 300, "max_per_hour": 3})
    h = Harness(cfg=cfg, procs=[make_proc(43232, NAPCAT_EXE, 600)])
    h.connect(0)
    h.disconnect(0)
    h.wd._last_restart_at = 100.0             # 假装 100s 时刚重启过
    actions = []
    h.wd._attempt_restart(150.0, 150.0, h.wd._store.get(), actions)   # 只过了 50s
    eq(actions, ["restart_throttled_interval"], "应被最小间隔限流")
    eq(h.killer.count, 0, "限流期间不得杀进程")
    eq(h.launcher.count, 0, "限流期间不得起进程")


@test
def test_throttle_max_per_hour_behavior():
    """行为层面：每小时最多 3 次。"""
    h = Harness(cfg=base_cfg(restart={"enabled": True, "after_seconds": 60,
                                      "min_interval_seconds": 1, "max_per_hour": 3}),
                procs=[make_proc(43232, NAPCAT_EXE, 600)])
    h.connect(0)
    h.disconnect(0)
    actions = h.sweep(3600, 30)
    eq(h.killer.count, 3, f"一小时最多 3 次，实际 {h.killer.count}")
    ok("restart_throttled_hourly" in actions, f"应出现小时限流动作: {set(actions)}")


@test
def test_fresh_process_is_not_preempted():
    """存在启动未满 60 秒的 NapCat 进程 ⇒ 不抢跑。"""
    young = make_proc(43232, NAPCAT_EXE, 30)
    h = Harness(cfg=base_cfg(restart={"enabled": True, "after_seconds": 60,
                                      "min_interval_seconds": 300, "max_per_hour": 3}),
                procs=[young])
    h.connect(0)
    h.disconnect(0)
    eq(na(h.tick(60)), ["restart_wait_fresh"], "启动未满 60s ⇒ 等待")
    eq(h.killer.count, 0, "不得杀刚起来的进程")
    eq(h.launcher.count, 0, "不得重复起进程")
    h.procs[:] = [make_proc(43232, NAPCAT_EXE, 95)]     # 进程长到 95 秒
    eq(h.tick(120), ["restart"], "满 60s 后重启动手（告警已在 5s 时先单独发过）")
    eq(h.killer.count, 1, "此时才允许动手")


@test
def test_restart_runs_even_with_no_leftover_process():
    """没有残留进程时也应重新拉起（launcher 必须被调用）。"""
    h = Harness(cfg=base_cfg(restart={"enabled": True, "after_seconds": 60}),
                procs=[])
    h.connect(0)
    h.disconnect(0)
    eq(na(h.tick(60)), ["restart"], "应触发")
    eq(h.killer.count, 0, "无目标 ⇒ 不杀")
    eq(h.launcher.count, 1, "仍应启动")


@test
def test_kill_failure_never_launches_new_instance():
    """★ 2026-10-21 实证缺陷回归：杀不掉却照启 ⇒ 镜像实例堆积（5→10→15）。

    权限实测：镜像 QQ 是提权进程，bot 非管理员 ⇒ taskkill 恒返回 Access denied。
    修复后：只要有目标结束失败，就**不得**调用 launcher。
    """
    h = Harness(cfg=base_cfg(restart={"enabled": True, "after_seconds": 60}),
                procs=[make_proc(43232, NAPCAT_EXE, 600)])
    h.killer.ret = False                      # 模拟 taskkill 返回非 0
    h.connect(0)
    h.disconnect(0)
    eq(na(h.tick(60)), ["restart_kill_failed"], "杀不掉 ⇒ 中止启动")
    eq(h.killer.count, 1, "尝试过一次结束")
    eq(h.launcher.count, 0, "★ 绝不允许在结束失败后启动新实例")
    ok("不重复启动" in h.log.text(), f"应留下明确日志:\n{h.log.text()}")


@test
def test_partial_kill_failure_also_aborts():
    """部分成功也不得启动：残留实例仍会与新实例冲突，堆积照样发生。"""
    h = Harness(cfg=base_cfg(restart={"enabled": True, "after_seconds": 60}),
                procs=[make_proc(111, NAPCAT_EXE, 600),
                       make_proc(222, NAPCAT_EXE, 600)])
    seq = {"n": 0}

    def kill(proc):
        seq["n"] += 1
        return seq["n"] != 1                  # 第一个失败，第二个成功

    h.killer.factory = kill
    h.connect(0)
    h.disconnect(0)
    eq(na(h.tick(60)), ["restart_kill_failed"], "部分失败 ⇒ 同样中止")
    eq(h.killer.count, 2, "两个目标都尝试过")
    eq(h.launcher.count, 0, "★ 只要有残留就不得启动")


@test
def test_kill_safety_error_also_aborts():
    """安全拦截（SafetyError）同样视为失败 ⇒ 中止启动。"""
    from scripts.offline_watchdog import SafetyError

    h = Harness(cfg=base_cfg(restart={"enabled": True, "after_seconds": 60}),
                procs=[make_proc(43232, NAPCAT_EXE, 600)])

    def kill(proc):
        raise SafetyError("路径不在镜像目录")

    h.killer.factory = kill
    h.connect(0)
    h.disconnect(0)
    eq(na(h.tick(60)), ["restart_kill_failed"], "被安全拦截 ⇒ 中止启动")
    eq(h.launcher.count, 0, "不得启动")


@test
def test_kill_failure_backs_off_before_retry():
    """中止后不得忙等重试：下一次尝试至少隔 min_interval_seconds。"""
    h = Harness(cfg=base_cfg(restart={"enabled": True, "after_seconds": 60,
                                      "min_interval_seconds": 300}),
                procs=[make_proc(43232, NAPCAT_EXE, 600)])
    h.killer.ret = False
    h.connect(0)
    h.disconnect(0)
    eq(na(h.tick(60)), ["restart_kill_failed"], "首次中止")
    # 只用「restart 族动作」断言：告警族（alert / alert_skipped_open）不在本用例关注范围
    eq([a for a in h.tick(200) if a.startswith("restart")], [],
       "未到 300s 退避 ⇒ 不应再次尝试重启")
    eq(h.killer.count, 1, "不得重复杀")
    eq([a for a in h.tick(400) if a.startswith("restart")], ["restart_kill_failed"],
       "退避后才允许再试")


@test
def test_restart_lister_failure_is_contained():
    h = Harness(cfg=base_cfg(restart={"enabled": True, "after_seconds": 60}))
    h.lister = Recorder(factory=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    h.wd._lister = h.lister
    h.connect(0)
    h.disconnect(0)
    eq(na(h.tick(60)), ["restart_list_failed"], "枚举失败应被隔离并记录")
    eq(h.killer.count, 0, "枚举失败不得盲杀")
    ok("枚举进程失败" in h.log.text(), "应有失败日志")


@test
def test_restart_never_touches_other_pids_even_when_mixed():
    """混合列表下 killer 的输入永远只有镜像路径进程。"""
    h = Harness(cfg=base_cfg(restart={"enabled": True, "after_seconds": 60}),
                procs=[make_proc(1, PERSONAL_EXE, 900), make_proc(2, PERSONAL_EXE_C, 900),
                       make_proc(3, NAPCAT_EXE, 900)])
    h.connect(0)
    h.disconnect(0)
    h.tick(60)
    for call in h.killer.calls:
        ok(is_napcat_mirror_path(call[0][0].path), "killer 只应收到镜像路径进程")


# ------------------------------------------------------------------ dry_run

@test
def test_dry_run_takes_no_action():
    h = Harness(cfg=base_cfg(dry_run=True,
                             restart={"enabled": True, "after_seconds": 60}),
                procs=[make_proc(43232, NAPCAT_EXE, 600)])
    h.connect(0)
    h.disconnect(0)
    eq(na(h.tick(60)), ["restart_dry_run", "alert_dry_run"],
       "dry-run 只记日志：重启与告警都只出 dry_run 动作")
    eq(h.killer.count, 0, "dry-run 不得杀进程")
    eq(h.launcher.count, 0, "dry-run 不得起进程")
    eq(h.tick(90), [],
       "90s 无新动作（告警已在 60s 那次 dry-run 过，下次重复在 180s）")
    eq(len(h.notifier.texts), 0, "dry-run 不得弹窗")
    ok("[dry-run]" in h.log.text(), "应有 dry-run 日志")


# ------------------------------------------------------------------ 配置

@test
def test_config_invalid_falls_back_to_safe_defaults():
    for bad in ({"alert": {"after_seconds": "abc"}},
                {"alert": {"enabled": "yes"}},
                {"restart": {"enabled": True, "max_per_hour": 0}},
                {"restart": {"enabled": True, "min_interval_seconds": -5}},
                {"dry_run": "false"},
                {"alert": []},
                [1, 2, 3]):
        try:
            parse_config(bad)
        except ValueError:
            continue
        raise AssertionError(f"非法配置应被拒绝: {bad!r}")
    cfg = parse_config({})
    eq(cfg["alert"]["enabled"], True, "缺省告警开")
    eq(cfg["restart"]["enabled"], False, "缺省重启关")


@test
def test_config_mtime_hot_reload_and_invalid_fallback():
    tmp = os.path.join(PROJECT_ROOT, ".tmp_offline_watchdog")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    path = os.path.join(tmp, "auto_restart.json")
    log = LogCapture()
    try:
        # 1) 文件缺失 ⇒ 安全默认
        store = ConfigStore(path, log)
        eq(store.get()["alert"]["after_seconds"], 5.0, "缺失时安全默认")
        eq(store.get()["restart"]["enabled"], False, "缺失时重启关")

        # 2) 正常配置
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(base_cfg(alert={"after_seconds": 5}), handle)
        os.utime(path, (1_700_000_000, 1_700_000_000))
        eq(store.get()["alert"]["after_seconds"], 5.0, "应热加载新值")

        # 3) 改成非法 JSON ⇒ 回退安全默认
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        os.utime(path, (1_700_000_100, 1_700_000_100))
        eq(store.get()["alert"]["after_seconds"], 5.0, "非法 JSON ⇒ 安全默认")
        eq(store.get()["restart"]["enabled"], False, "非法 JSON ⇒ 重启关")
        ok("JSON 非法" in log.text(), f"应有一行回退日志:\n{log.text()}")

        # 4) 合法 JSON 但字段非法 ⇒ 回退安全默认
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"alert": {"enabled": True, "after_seconds": -1}}, handle)
        os.utime(path, (1_700_000_200, 1_700_000_200))
        eq(store.get()["alert"]["after_seconds"], 5.0, "字段非法 ⇒ 安全默认")
        ok("配置字段非法" in log.text(), "应有字段非法日志")

        # 5) 未变更的文件戳不得重复读盘（日志行数不增长）
        before = len(log.lines)
        store.get()
        store.get()
        eq(len(log.lines), before, "文件戳未变不应再打日志")

        # 6) 热加载改到"重启开"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(base_cfg(alert={"after_seconds": 5},
                               restart={"enabled": True, "after_seconds": 3}), handle)
        os.utime(path, (1_700_000_300, 1_700_000_300))
        cfg = store.get()
        eq(cfg["restart"]["enabled"], True, "应热加载到重启开")
        eq(cfg["restart"]["after_seconds"], 3.0, "应热加载到 3 秒")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def test_config_hot_reload_switches_restart_on_mid_outage():
    """断连中途把 restart.enabled 打开，下一轮 tick 就应生效。"""
    h = Harness(cfg=base_cfg(restart={"enabled": False}),
                procs=[make_proc(43232, NAPCAT_EXE, 600)],
                store=StaticStore(base_cfg(restart={"enabled": False}),
                                  switch_after=2,
                                  new_cfg=base_cfg(restart={"enabled": True,
                                                            "after_seconds": 60,
                                                            "min_interval_seconds": 300,
                                                            "max_per_hour": 3})))
    h.connect(0)
    h.disconnect(0)
    h.tick(30)                                # 此时还是关闭态
    eq(h.lister.count, 0, "关闭态不得枚举进程")
    h.tick(61)                                # 已切到开启态（第 3 次读配置）
    eq(h.killer.count, 1, "热加载开启后应生效")


# ------------------------------------------------------------------ 弹窗静默静态证据

@test
def test_popup_script_is_silent_by_construction():
    text = "【掉线告警】账号已离线 3 分钟。请在 NapCat 窗口扫码重新登录。"
    ps1 = build_popup_ps1(text)
    variants = [ps1, base64.b64decode(
        build_popup_argv(text)[-1]).decode("utf-16-le")]
    for script in variants:
        low = script.lower()
        for banned in ("beep", "soundplayer", "systemsounds", "media.sound",
                       "speechsynthesizer", "mb_icon", "messagebox", "0x00000040",
                       "0x00000030", "0x00000020", "0x00000010"):
            ok(banned not in low, f"弹窗脚本不得出现 {banned!r}")
        # 自绘 Form：不经过任何 MessageBox 提示音路径，也不受 SPI_MESSAGEDURATION 影响
        ok("System.Windows.Forms.Form" in script, "必须是自绘 WinForms 窗体")
        ok("$f.TopMost=$true" in script, "必须 TopMost（WS_EX_TOPMOST）")
        ok("ShowDialog" in script, "必须 ShowDialog 保持模态直到用户处理")
        ok("System.Windows.Forms" in script, "必须显式加载 WinForms 程序集")
    ok(text in variants[1], "base64 解码后必须还原完整中文文案（无编码/转义损失）")


@test
def test_popup_avoids_messagebox_entirely():
    """回归护栏 —— 两条都是**实机测出来**的硬约束，改回 MessageBox 会直接失效：
    ① ``MessageBoxOptions`` 枚举里没有 TopMost（实测成员只有 DefaultDesktopOnly /
       RightAlign / RtlReading / ServiceNotification），托管 MessageBox 无法置顶；
    ② 本机 ``SPI_GETMESSAGEDURATION = 5``：任何 MessageBox 出现约 5 秒后会被系统
       自动关掉（实测窗口从 0.21s 存活到 3.84s 就消失、进程 returncode=0）。
    """
    script = build_popup_ps1("x")
    ok("MessageBox" not in script, "弹窗不得使用 MessageBox（5 秒自动消失 + 无法置顶）")
    ok("ShowDialog" in script, "必须用 ShowDialog 保持模态")
    ok("TopMost=$true" in script, "必须置顶")


@test
def test_popup_failure_retries_in_30s_not_120s():
    h = Harness()
    h.notifier = lambda text: None               # 弹窗永远起不来
    h.wd._notifier = h.notifier
    h.connect(0)
    h.disconnect(0)
    eq(h.tick(90), ["alert_failed"], "弹窗失败要单独标记")
    ok("弹窗未成功拉起" in h.log.text(), "失败要有 WARN 日志")
    eq(h.tick(119), [], "未到退避点")
    eq(h.tick(120), ["alert_failed"], "30 秒退避后应重试（而不是等满 120 秒）")
    eq(h.wd.next_deadline(), 150.0, "下一次唤醒点应紧跟退避节奏")


@test
def test_popup_argv_is_robust_and_hidden():
    argv = build_popup_argv("【掉线告警】含'单引号'与\"双引号\"的文案")
    eq(argv[0], "powershell", "用 powershell 承载弹窗")
    ok("-EncodedCommand" in argv, "用 -EncodedCommand 规避中文代码页/引号转义问题")
    ok("-STA" in argv, "STA 线程模型（WinForms 弹窗要求）")
    ok("-NoProfile" in argv, "不加载用户 profile")
    ok("-WindowStyle" in argv, "隐藏宿主控制台窗口")
    script = base64.b64decode(argv[-1]).decode("utf-16-le")
    ok("''单引号''" in script, f"单引号必须被 PowerShell 转义: {script}")
    ok('\\"双引号\\"' not in script and '"双引号"' in script, "双引号不应被破坏")


@test
def test_harness_injects_all_side_effects():
    """自检：每个测试用到的 Harness 都注入了假依赖（保证没有真弹窗/真杀进程）。"""
    h = Harness()
    for name in ("_notifier", "_lister", "_killer", "_launcher"):
        fn = getattr(h.wd, name)
        ok(getattr(fn, "__self__", None) is h or fn in (h.notifier, h.lister,
                                                        h.killer, h.launcher),
           f"{name} 必须是注入的假实现，不能落到默认实现")


# ------------------------------------------------------------------ 排程/观测

@test
def test_idle_means_no_wakeups():
    h = Harness()
    h.connect(1000)
    eq(h.wd.next_deadline(), None, "在线时不应有任何唤醒点（零轮询）")
    eq(h.wd.tick(), [], "在线 tick 无动作")
    eq(h.wd.state()["next_wake_in"], None, "在线时 next_wake_in 应为 None")


@test
def test_deadline_schedule_is_forward_looking():
    h = Harness(cfg=base_cfg(restart={"enabled": True}))
    h.connect(0)
    h.disconnect(0)
    eq(h.wd.next_deadline(), 5.0, "告警 5s 比重启 60s 更早 ⇒ 先醒在 5s")
    h.clock.t = 60.0
    h.wd.tick()
    nd = h.wd.next_deadline()
    ok(nd is not None and nd > 60.0, f"下一次唤醒应是未来时刻（aligned 到下一告警/重启），实得 {nd}")


@test
def test_run_forever_drives_alerts_without_blocking():
    """真实事件循环下的端到端：单个 task 驱动 tick，空闲时阻塞在 Event 上。"""
    import asyncio
    import time as _time

    async def scenario():
        log = LogCapture()
        notifier = FakeNotifier()
        cfg = StaticStore(parse_config(base_cfg(alert={"after_seconds": 1,
                                                      "repeat_seconds": 1,
                                                      "startup_grace_seconds": 1})))
        wd = OfflineWatchdog(logger=log, config_store=cfg, notifier=notifier,
                             clock=_time.monotonic)
        wd.on_start()
        loop = asyncio.get_running_loop()
        task = wd.start_task(loop)
        eq(wd.start_task(loop), task, "start_task 必须幂等（全进程只允许一个驱动 task）")
        await asyncio.sleep(1.25)
        ok(len(notifier.texts) >= 1, f"1 秒阈值后应由 task 驱动出弹窗:\n{log.text()}")
        fired = len(notifier.texts)
        wd.on_connected()
        eq(wd.next_deadline(), None, "重连后不应再有任何唤醒点")
        await asyncio.sleep(0.25)
        eq(len(notifier.texts), fired, "重连后不得再弹窗")
        eq(task.done(), False, "重连后 task 应仍在（阻塞在 Event 上，零轮询）")
        wd.stop_task()
        await asyncio.sleep(0.05)
        ok(task.cancelled() or task.done(), "stop_task 应能干净收尾")

    asyncio.run(scenario())


@test
def test_state_snapshot_fields():
    h = Harness(cfg=base_cfg(restart={"enabled": True}),
                procs=[make_proc(43232, NAPCAT_EXE, 600)])
    h.connect(0)
    h.disconnect(0)
    state = h.wd.state(300)
    eq(state["connected"], False, "connected")
    eq(state["down_minutes"], 5.0, "down_minutes")
    eq(state["alert_enabled"], True, "alert_enabled")
    eq(state["restart_enabled"], True, "restart_enabled")
    eq(state["dry_run"], False, "dry_run")
    eq(state["restarts_last_hour"], 0, "restarts_last_hour")
    h.tick(60)
    eq(h.wd.state(60)["last_restart_at"], 60.0, "last_restart_at")


@test
def test_all_logs_are_prefixed_and_carry_no_secrets():
    h = Harness(cfg=base_cfg(restart={"enabled": True}),
                procs=[make_proc(43232, NAPCAT_EXE, 600)])
    h.connect(0)
    h.disconnect(0)
    h.tick(60)
    h.tick(90)
    h.connect(300)
    ok(h.log.lines, "应有日志产出")
    for line in h.log.lines:
        ok(line.startswith("[自愈]"), f"日志必须以 [自愈] 开头: {line!r}")
    low = h.log.text().lower()
    for banned in ("password", "api_key", "apikey", "token", "secret", "sk-"):
        ok(banned not in low, f"日志不得出现 {banned!r}")
    ok("已停机" in h.log.text() or "停机" in h.log.text(), "日志应带停机时长")


@test
def test_logger_failure_never_breaks_watchdog():
    class BadLogger:
        def info(self, msg):
            raise RuntimeError("logger down")

        def warning(self, msg):
            raise RuntimeError("logger down")

    h = Harness()
    h.wd._log = __import__("scripts.offline_watchdog", fromlist=["x"])._make_logger(BadLogger())
    h.connect(0)
    h.disconnect(0)
    eq(h.tick(90), ["alert"], "日志实现炸了也不得影响告警")


# ------------------------------------------------------------------ runner

def main() -> int:
    print(f"offline_watchdog 单测：{len(_TESTS)} 项（离线，不杀真进程 / 不起真弹窗）")
    failures = []
    for fn in _TESTS:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as exc:                      # noqa: BLE001
            failures.append(fn.__name__)
            print(f"  FAIL  {fn.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n结果：{len(_TESTS) - len(failures)}/{len(_TESTS)} 通过"
          + (f"，失败：{failures}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
