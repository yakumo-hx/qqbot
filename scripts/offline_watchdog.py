#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""掉线看门狗（offline_watchdog）—— 纯逻辑、可单测、默认"只叫人不动手"。

职责（三级阶梯）：
  L0  断连 0~60s：什么都不做（NapCat 自带自愈先试）。
  L1  默认关闭。``restart.enabled=True`` 时，断连超过 ``restart.after_seconds``
      仍离线 ⇒ 按"镜像路径过滤"结束 NapCat 侧残留进程 ⇒ 分离启动
      ``E:\\QQbot\\tools\\start_napcat_verbose.bat``；带限流与"不抢跑"。
  L2  **本波主交付**：断连 90s 仍离线 ⇒ 置顶弹窗（**无声音**）；未恢复每 120s
      重复；同一时刻最多一个弹窗（不堆叠）。
  L3  全程 ``[自愈]`` 前缀日志；重连时结算一次"本轮停机 N 分钟"。

安全不变量（有单测断言）：
  1. 只结束 **镜像路径在 ``<项目根>\\NapCat\\`` 之下** 的进程（绝不 ``taskkill /im``）；
  2. ``restart.enabled`` 为假 ⇒ 绝不调用任何杀进程/起进程代码路径（调用次数 = 0）；
  3. ``dry_run`` 为真 ⇒ 只记日志，不杀进程、不起进程、不弹窗；
  4. 不碰 bot 自身进程、QQ 数据目录、``versions\\`` 运行时；
  5. 弹窗脚本禁用一切发声 API（见 ``build_popup_ps1``）。

时区/时钟：内部一律用 ``clock()``（默认 ``time.monotonic``）算间隔；日志里的
时刻用本地墙钟 ``datetime.now()``。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_CONFIG_PATH",
    "LOG_PREFIX",
    "NAPCAT_LAUNCH_BAT",
    "NAPCAT_PROCESS_NAMES",
    "NAPCAT_ROOT",
    "ConfigStore",
    "OfflineWatchdog",
    "ProcInfo",
    "SafetyError",
    "build_popup_ps1",
    "default_killer",
    "default_launcher",
    "default_notifier",
    "default_process_lister",
    "get_watchdog",
    "is_napcat_mirror_path",
    "load_config",
    "parse_config",
    "reset_watchdog",
]

# ---------------------------------------------------------------- 常量

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "auto_restart.json")

#: 只读引用：启动脚本真实路径（本模块不修改它）
NAPCAT_LAUNCH_BAT = os.path.join(PROJECT_ROOT, "tools", "start_napcat_verbose.bat")
#: 只读引用：NapCat 镜像安装根。**只有** 该目录之下的进程允许被结束。
NAPCAT_ROOT = os.path.join(PROJECT_ROOT, "NapCat")
#: 枚举这些名字的进程（`Get-Process -Name ...`）；是否动手还要过路径过滤。
NAPCAT_PROCESS_NAMES = ("QQ.exe", "NapCatWinBootMain.exe", "NapCatWinBootHook.exe")

#: 存在"启动未满 N 秒"的 NapCat 进程时不许抢跑
FRESH_PROCESS_GRACE_SECONDS = 60.0
#: 单次 tick 之间的兜底下限（正常路径不会触发，只用于防止任何情况下的忙等）
MIN_TICK_INTERVAL = 1.0
#: 弹窗拉起后等多久确认它没有立刻挂掉（模态弹窗活着 = 窗口开着）
POPUP_DEATH_CHECK_SECONDS = 0.5
#: 弹窗失败后的退避重试间隔（比 repeat_seconds 快，避免 120 秒内无人知晓）
ALERT_FAILURE_RETRY_SECONDS = 30.0
#: 重启限流窗口
RESTART_WINDOW_SECONDS = 3600.0

LOG_PREFIX = "[自愈]"

POPUP_TITLE = "QQ Bot 掉线告警"
ALERT_TEMPLATE = "【掉线告警】账号已离线 {minutes} 分钟。请在 NapCat 窗口扫码重新登录。"

#: 非法/缺失配置时的安全默认：**告警开、重启关**。
DEFAULT_CONFIG = {
    "alert": {
        "enabled": True,
        "after_seconds": 5.0,              # 2026-10-20：真掉线 5 秒即弹窗（原 90）
        "repeat_seconds": 120.0,
        "startup_grace_seconds": 60.0,     # 冷启动给 NapCat 的连接宽限（与掉线分开）
    },
    "restart": {
        "enabled": False,
        "after_seconds": 60.0,
        "min_interval_seconds": 300.0,
        "max_per_hour": 3,
    },
    "dry_run": False,
}


class SafetyError(RuntimeError):
    """违反安全不变量时抛出（宁可不动手，也不越界）。"""


# ---------------------------------------------------------------- 工具

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _norm_path(path) -> str:
    """规范化用于比较的路径（Windows 下大小写不敏感、分隔符统一）。"""
    if not path:
        return ""
    try:
        return os.path.normcase(os.path.normpath(str(path)))
    except (TypeError, ValueError):
        return ""


_MIRROR_PREFIX = _norm_path(NAPCAT_ROOT) + os.sep


def is_napcat_mirror_path(path) -> bool:
    """路径是否位于 ``<项目根>\\NapCat\\`` 之下（镜像目录）。

    这是唯一的"允许动手"判据：``E:\\QQbot\\NapCat\\NapCat.44498.Shell\\QQ.exe``
    ⇒ True；``E:\\Program Files\\Tencent\\QQNT\\QQ.exe`` ⇒ False。
    前缀必须带分隔符，``E:\\QQbot\\NapCatEvil\\x.exe`` 不会被误判。
    """
    normalized = _norm_path(path)
    if not normalized:
        return False
    return normalized.startswith(_MIRROR_PREFIX)


def _proc_name_key(name) -> str:
    """比较用的进程名键：取基名、去掉 .exe、转小写。

    为什么必须这样比（2026-10-21 实机确认，这是 L1 从未杀掉任何进程的真正原因）：
    default_process_lister 用 PowerShell 的 $p.ProcessName，它返回**不带扩展名**
    的基名（'QQ'）；而 NAPCAT_PROCESS_NAMES 里写的是 'QQ.exe'。
    直接比较 'qq' in {'qq.exe', ...} 恒为 False ⇒ 每个目标都被判成
    "白名单外进程"并被 SafetyError 拒绝 ⇒ 杀不掉，但旧逻辑仍会启动新实例。
    """
    base = os.path.basename(str(name or "").strip())
    stem, ext = os.path.splitext(base)
    return (stem if ext.lower() == ".exe" else base).lower()


#: 进程名白名单的归一化键集合（'QQ.exe' 与 'QQ' 归一后同键）
_NAPCAT_NAME_KEYS = frozenset(_proc_name_key(n) for n in NAPCAT_PROCESS_NAMES)


def _is_allowed_proc_name(name) -> bool:
    """进程名是否在 NapCat 侧白名单内（大小写不敏感、扩展名可有可无）。"""
    return _proc_name_key(name) in _NAPCAT_NAME_KEYS


def _make_logger(logger):
    """把 bot_logger（任意对象）适配成 ``log(level, msg)``，永不自爆。"""
    def _log(level: str, message: str) -> None:
        line = f"{LOG_PREFIX}[{level}] {_ts()} {message}"
        if logger is not None:
            name = "warning" if level == "WARN" else "info"
            fn = getattr(logger, name, None) or getattr(logger, "info", None)
            if fn is not None:
                try:
                    fn(line)
                    return
                except Exception:       # pragma: no cover - 日志失败不影响主流程
                    pass
        print(line, flush=True)

    return _log


# ---------------------------------------------------------------- 配置

def _as_bool(value, field_name: str):
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是布尔值")
    return value


def _as_number(value, field_name: str, minimum: float):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} 必须是数字")
    number = float(value)
    if number < minimum:
        raise ValueError(f"{field_name} 不能小于 {minimum}")
    return number


def _as_int(value, field_name: str, minimum: int):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} 必须是整数")
    if value < minimum:
        raise ValueError(f"{field_name} 不能小于 {minimum}")
    return value


def parse_config(data) -> dict:
    """校验并归一化配置；非法即 ``ValueError``（调用方回退安全默认）。"""
    if not isinstance(data, dict):
        raise ValueError("配置根节点必须是对象")

    alert = data.get("alert", {})
    restart = data.get("restart", {})
    # 只在"键缺失/显式 null"时补空对象；空列表等非法类型必须被拒绝
    if alert is None:
        alert = {}
    if restart is None:
        restart = {}
    if not isinstance(alert, dict):
        raise ValueError("alert 必须是对象")
    if not isinstance(restart, dict):
        raise ValueError("restart 必须是对象")

    parsed = {
        "alert": {
            "enabled": _as_bool(alert.get("enabled", True), "alert.enabled"),
            "after_seconds": _as_number(
                alert.get("after_seconds", 5), "alert.after_seconds", 1),
            "repeat_seconds": _as_number(
                alert.get("repeat_seconds", 120), "alert.repeat_seconds", 1),
            # 冷启动（bot 刚起来、NapCat 还没连过）用的宽限。
            # 必须与 after_seconds 分开：after_seconds 调到 5 秒后，
            # NapCat 正常重连要 14~16 秒，用同一个值会让每次重启都误报。
            "startup_grace_seconds": _as_number(
                alert.get("startup_grace_seconds", 60),
                "alert.startup_grace_seconds", 0),
        },
        "restart": {
            # 安全默认：缺失即"关"
            "enabled": _as_bool(restart.get("enabled", False), "restart.enabled"),
            "after_seconds": _as_number(
                restart.get("after_seconds", 60), "restart.after_seconds", 0),
            "min_interval_seconds": _as_number(
                restart.get("min_interval_seconds", 300), "restart.min_interval_seconds", 1),
            "max_per_hour": _as_int(
                restart.get("max_per_hour", 3), "restart.max_per_hour", 1),
        },
        "dry_run": _as_bool(data.get("dry_run", False), "dry_run"),
    }
    return parsed


def load_config(path=None, logger=None) -> dict:
    """一次性读取并校验配置（带缓存的热加载请用 :class:`ConfigStore`）。

    文件缺失 / JSON 损坏 / 字段非法 ⇒ 返回安全默认（告警开、重启关）并打一行日志。
    """
    log = _make_logger(logger)
    target = path or DEFAULT_CONFIG_PATH
    try:
        with open(target, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        return parse_config(raw)
    except FileNotFoundError:
        log("WARN", f"配置文件缺失（{target}）⇒ 使用安全默认：告警开 / 重启关")
    except json.JSONDecodeError as exc:
        log("WARN", f"配置文件 JSON 非法（{exc}）⇒ 使用安全默认：告警开 / 重启关")
    except ValueError as exc:
        log("WARN", f"配置字段非法（{exc}）⇒ 使用安全默认：告警开 / 重启关")
    except OSError as exc:
        log("WARN", f"配置文件不可读（{exc}）⇒ 使用安全默认：告警开 / 重启关")
    return parse_config({})


class ConfigStore:
    """按 ``(st_mtime_ns, st_size)`` 热加载 ``config/auto_restart.json``。

    与 ``config/interaction_menu.json`` 的既有热加载范式一致：只在文件戳变化时
    重新读盘；解析失败回退安全默认，并且**每次戳变化只打一行日志**。
    """

    def __init__(self, path=None, logger=None):
        self.path = path or DEFAULT_CONFIG_PATH
        self._logger = logger                      # 原样保留，交给 load_config 包装
        self._log = _make_logger(logger)
        self._stamp = None
        self._config = parse_config({})

    def get(self) -> dict:
        try:
            stat = os.stat(self.path)
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = ("missing",)
        if stamp != self._stamp:
            self._stamp = stamp
            self._config = load_config(self.path, self._logger)
        return self._config


# ---------------------------------------------------------------- 进程/弹窗副作用（可注入）

@dataclass(frozen=True)
class ProcInfo:
    """一条进程记录（``age_seconds`` 为 None 表示拿不到启动时刻）。"""

    pid: int
    name: str
    path: str
    age_seconds: float | None = None


_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0)
_CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

#: 枚举进程：只用 Get-Process（PID/名字/镜像路径/存活秒数）。
#: 注意 $p.Path 需要同用户权限，取不到就留空（留空的进程因路径过滤不会被动）。
_PROC_LIST_PS = (
    "$ErrorActionPreference='SilentlyContinue';"
    "Get-Process -Name QQ,NapCatWinBootMain,NapCatWinBootHook -ErrorAction SilentlyContinue |"
    "ForEach-Object { $p=$_; $age='';"
    "try { $age=[int]((Get-Date)-$p.StartTime).TotalSeconds } catch { $age='' };"
    "Write-Output ($p.Id.ToString() + \"`t\" + $p.ProcessName + \"`t\" + $p.Path + \"`t\" + $age) }"
)


def default_process_lister(logger=None, timeout: float = 15.0):
    """列出候选进程（**不做**路径过滤——过滤由调用方显式完成，便于单测）。"""
    log = _make_logger(logger)
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PROC_LIST_PS],
        capture_output=True, text=True, timeout=timeout,
        creationflags=_CREATE_NO_WINDOW, check=False,
    )
    items = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        pid_text, name, path, age_text = (parts[0].strip(), parts[1].strip(),
                                          parts[2].strip(), parts[3].strip())
        if not pid_text.isdigit():
            continue
        try:
            age = float(age_text) if age_text else None
        except ValueError:
            age = None
        items.append(ProcInfo(int(pid_text), name, path, age))
    if proc.returncode != 0:
        log("WARN", f"枚举进程返回码 {proc.returncode}（继续用已解析到的 {len(items)} 条）")
    return items


def default_killer(proc: ProcInfo, logger=None):
    """结束**单个** NapCat 侧进程。双重保险：路径 + 名字都要过。

    严禁 ``taskkill /im QQ.exe``（机器上实测有 5 个个人 QQNT 进程同名）。
    不加 ``/T``：只结束枚举到的那个 PID，避免树展开带来意外。
    """
    log = _make_logger(logger)
    if not is_napcat_mirror_path(proc.path):
        raise SafetyError(f"拒绝结束非镜像路径进程 PID={proc.pid} path={proc.path!r}")
    if not _is_allowed_proc_name(proc.name):
        # 用归一化比较（去 .exe、小写）。历史上写成
        # str(proc.name).lower() not in {n.lower() ...} 会恒不匹配 —— 见 _proc_name_key。
        raise SafetyError(f"拒绝结束白名单外进程 PID={proc.pid} name={proc.name!r}")
    completed = subprocess.run(
        ["taskkill", "/PID", str(proc.pid), "/F"],
        capture_output=True, text=True, timeout=15,
        creationflags=_CREATE_NO_WINDOW, check=False,
    )
    ok = completed.returncode == 0
    if not ok:
        log("WARN", f"结束进程 PID={proc.pid} 失败（返回码 {completed.returncode}）")
    return ok


async def _async_launch_fallback(bat_path: str, cwd: str, logger=None):
    """降级路径：``asyncio.create_subprocess_exec`` 分离启动（沙箱内可能 WinError 5）。"""
    log = _make_logger(logger)
    try:
        await asyncio.create_subprocess_exec(
            "cmd.exe", "/c", bat_path, cwd=cwd,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log("INFO", "已通过 asyncio 降级路径启动 NapCat 启动脚本")
    except Exception as exc:                      # pragma: no cover - 环境相关
        log("WARN", f"asyncio 降级启动也失败：{exc!r}")


def default_launcher(logger=None, bat_path=None, cwd=None):
    """**分离**启动 ``tools/start_napcat_verbose.bat``（窗口独立于 bot）。

    首选 ``subprocess.Popen`` + ``DETACHED_PROCESS | CREATE_NEW_CONSOLE``（已知环境
    事实：沙箱内 ``asyncio.create_subprocess_exec`` 会 WinError 5，普通控制台下
    Popen 可用）；Popen 抛 ``OSError`` 时降级到 asyncio 分离子进程并记一行日志。

    返回 ``(mode, proc_or_exc)``，mode ∈ {"popen", "async", "failed"}。
    """
    log = _make_logger(logger)
    bat = bat_path or NAPCAT_LAUNCH_BAT
    workdir = cwd or PROJECT_ROOT
    flags = _DETACHED_PROCESS | _CREATE_NEW_CONSOLE
    if not os.path.exists(bat):
        log("WARN", f"启动脚本不存在：{bat} ⇒ 放弃启动")
        return ("failed", FileNotFoundError(bat))
    try:
        child = subprocess.Popen(
            ["cmd.exe", "/c", bat], cwd=workdir,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True,
        )
        return ("popen", child)
    except OSError as exc:
        log("WARN", f"Popen 分离启动失败（{exc!r}）⇒ 降级 asyncio.create_subprocess_exec")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log("WARN", "当前无运行中的事件循环，无法降级启动")
            return ("failed", exc)
        loop.create_task(_async_launch_fallback(bat, workdir, logger))
        return ("async", None)


_POPUP_SCRIPT_TEMPLATE = """$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$f=New-Object System.Windows.Forms.Form
$f.Text='__TITLE__'
$f.TopMost=$true
$f.StartPosition='CenterScreen'
$f.FormBorderStyle='FixedDialog'
$f.MaximizeBox=$false
$f.MinimizeBox=$false
$f.ShowInTaskbar=$true
$f.ClientSize=New-Object System.Drawing.Size(560,230)
$l=New-Object System.Windows.Forms.Label
$l.Text='__TEXT__'
$l.AutoSize=$false
$l.TextAlign='MiddleCenter'
$l.SetBounds(24,20,512,130)
try{$l.Font=New-Object System.Drawing.Font('Microsoft YaHei UI',13,[System.Drawing.FontStyle]::Bold)}catch{}
$f.Controls.Add($l)
$b=New-Object System.Windows.Forms.Button
$b.Text='我知道了（请在 NapCat 窗口扫码重新登录）'
$b.DialogResult='OK'
$b.SetBounds(90,165,380,42)
$f.Controls.Add($b)
$f.AcceptButton=$b
[void]$f.ShowDialog()
$f.Dispose()"""


def build_popup_ps1(text: str, title: str = POPUP_TITLE) -> str:
    """生成**静默置顶**弹窗的 PowerShell 脚本（自绘 WinForms 窗体，不用 MessageBox）。

    为什么**不用** ``MessageBox``（两个都是实测出来的硬约束）：
      1. ``MessageBoxOptions`` 枚举里**没有 TopMost**（实测成员只有 DefaultDesktopOnly /
         RightAlign / RtlReading / ServiceNotification），而 PowerShell 会校验枚举值，
         连 ``[MessageBoxOptions]0x40000`` 塞原始位也会被拒（``UndefinedIntegerToEnum``）
         ⇒ 托管封装根本没法置顶；
      2. **更致命**：本机 ``SPI_GETMESSAGEDURATION = 5``（实测），即系统设置了
         "消息框 5 秒后自动消失"。任何 ``MessageBox`` 都会在 5 秒后自己关掉，
         用户极可能根本没看到 —— 掉线告警绝不能建在这上面。
    自绘窗体由 ``ShowDialog()`` 保持，直到用户点按钮/关窗（或看门狗在重连时结束该进程）。

    静默依据：
      * 全程不碰 ``MessageBox``（因此不经过任何 MB_ICON* 提示音路径）；
      * 脚本内没有 ``[console]::beep`` / ``System.Media.SoundPlayer`` / ``SystemSounds``；
      * WinForms 窗体与按钮的显示本身不播放任何声音。
    置顶依据：``Form.TopMost = $true``（``WS_EX_TOPMOST``，实测已确认）。
    """
    safe_text = str(text).replace("'", "''")
    safe_title = str(title).replace("'", "''")
    return (_POPUP_SCRIPT_TEMPLATE
            .replace("__TEXT__", safe_text)
            .replace("__TITLE__", safe_title))


def build_popup_argv(text: str, title: str = POPUP_TITLE):
    """``-EncodedCommand``（UTF-16LE + base64）：中文/引号零转义风险，不受代码页影响。"""
    import base64
    script = build_popup_ps1(text, title)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["powershell", "-NoProfile", "-STA", "-NonInteractive",
            "-WindowStyle", "Hidden", "-EncodedCommand", encoded]


def default_notifier(text: str, logger=None, title: str = POPUP_TITLE):
    """弹一个**无声音**的置顶告警窗体（独立进程，弹窗阻塞不影响 bot 主循环）。

    拉起后最多等 :data:`POPUP_DEATH_CHECK_SECONDS` 秒确认它没有立刻挂掉——弹窗是模态
    阻塞的，只要没挂就说明窗口正开着。挂掉则返回 None，让上层退避重试并打 WARN。
    这段时间会阻塞事件循环；但它只发生在**已经离线**的路径上（每 120 秒最多一次），
    此时 bot 本来就没有流量可处理。
    """
    log = _make_logger(logger)
    try:
        child = subprocess.Popen(
            build_popup_argv(text, title),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW, close_fds=True,
        )
    except OSError as exc:
        log("WARN", f"弹窗启动失败：{exc!r}")
        return None
    try:
        child.wait(timeout=POPUP_DEATH_CHECK_SECONDS)
    except subprocess.TimeoutExpired:
        return child                                  # 仍存活 ⇒ 弹窗正开着
    log("WARN", f"弹窗脚本 {POPUP_DEATH_CHECK_SECONDS} 秒内就退出了"
                f"（返回码 {child.returncode}）⇒ 告警可能未显示，稍后退避重试")
    return None


# ---------------------------------------------------------------- 看门狗

class OfflineWatchdog:
    """断连状态机。所有副作用（弹窗/枚举/杀进程/起进程）都可注入，便于离线单测。"""

    def __init__(self, config_path=None, logger=None, *, clock=time.monotonic,
                 notifier=None, process_lister=None, killer=None, launcher=None,
                 config_store=None, launcher_bat=None):
        self._clock = clock
        self._log = _make_logger(logger)
        self._store = config_store or ConfigStore(config_path, logger)
        self._notifier = notifier or (lambda text: default_notifier(text, logger))
        self._lister = process_lister or (lambda: default_process_lister(logger))
        self._killer = killer or (lambda proc: default_killer(proc, logger))
        self._launcher = launcher or (
            lambda: default_launcher(logger, bat_path=launcher_bat))

        self._lock = threading.RLock()
        self._connected = None            # None=未知（冷启动）；True/False=已知
        self._started = False
        # True = 因 bot 冷启动尚未连过 NapCat（首弹用 startup_grace_seconds）
        self._startup_pending = False
        self._down_since = None
        self._last_alert_at = None
        self._alert_proc = None
        self._alert_visible = False
        self._restart_marks = deque(maxlen=64)
        self._last_restart_at = None
        self._next_restart_at = None
        self._task = None
        self._wake = None

    # -------------------------------------------------- 时间/文案

    def _minutes(self, now: float) -> float:
        if self._down_since is None:
            return 0.0
        return max(0.0, (now - self._down_since) / 60.0)

    def _down_text(self, now: float) -> str:
        return f"已停机 {self._minutes(now):.1f} 分钟"

    @staticmethod
    def _alert_text(elapsed_seconds: float) -> str:
        # 立即弹窗后，1~59 秒内按秒显示才准确（否则会显示"已离线 1 分钟"而实际才 3 秒）
        if elapsed_seconds < 60:
            seconds = max(1, int(elapsed_seconds + 0.5))
            return (f"【掉线告警】账号已离线 {seconds} 秒。"
                    f"请在 NapCat 窗口扫码重新登录。")
        # 四舍五入（half-up，不用 Python 的银行家舍入，避免 3.5 → 4 / 2.5 → 2 的怪值）
        minutes = max(1, int(elapsed_seconds / 60.0 + 0.5))
        return ALERT_TEMPLATE.format(minutes=minutes)

    # -------------------------------------------------- 生命周期钩子

    def on_start(self, now=None) -> None:
        """bot 启动即调用：在"NapCat 从未连上"的情况下也把时钟armed 起来。

        冷启动时 bot 本就无法工作 ⇒ 启动后 ``alert.after_seconds`` 内仍无连接
        即视为离线并告警（这是唯一能覆盖"bot 重启后 NapCat 没连回来"的窗口）。
        """
        now = self._clock() if now is None else now
        with self._lock:
            self._started = True
            if self._connected is None:
                self._connected = False
                if self._down_since is None:
                    self._down_since = now
                cfg = self._store.get()
                self._startup_pending = True
                grace = cfg["alert"].get("startup_grace_seconds",
                                         cfg["alert"]["after_seconds"])
                self._log("INFO",
                          f"启动守望：等待 NapCat 连接，{grace:.0f} 秒"
                          f"内未连上即告警（restart.enabled="
                          f"{str(cfg['restart']['enabled']).lower()}）")
                self._bump()

    def on_connected(self, now=None) -> None:
        """连上：结算本轮停机 + 关闭残留弹窗（保证"同一时刻最多一个弹窗"永真）。"""
        now = self._clock() if now is None else now
        with self._lock:
            self._startup_pending = False      # 已连上 ⇒ 之后按真掉线阈值
            was_down = self._down_since is not None
            downtime = None if not was_down else max(0.0, now - self._down_since)
            if was_down and downtime >= 1.0:
                self._log("OK", f"NapCat 已重连 —— 本轮停机 {downtime / 60.0:.1f} 分钟"
                                f"（{downtime:.0f} 秒），告警计数已复位")
            elif was_down:
                self._log("INFO", f"NapCat 已重连（停机 {downtime:.1f} 秒，未达告警阈值）")
            elif self._connected is not True:
                self._log("INFO", "NapCat 已连接（此前未处于停机状态）")
            # 本来就在线且没有停机 ⇒ 静默，重复连接不刷日志
            self._connected = True
            self._down_since = None
            self._last_alert_at = None
            self._next_restart_at = None
            self._close_alert_popup("已重连")
            self._bump()

    def on_disconnected(self, now=None) -> None:
        """断开：开一轮停机计时（重复调用幂等，不重置计时）。"""
        now = self._clock() if now is None else now
        with self._lock:
            self._connected = False
            if self._down_since is None:
                self._down_since = now
                cfg = self._store.get()
                self._log("WARN", f"检测到与 NapCat 的 WS 断连 —— L0 静默期 "
                                  f"{max(cfg['restart']['after_seconds'], 0):.0f} 秒"
                                  f"（自愈先试），{cfg['alert']['after_seconds']:.0f} 秒"
                                  f"无恢复即弹窗告警")
                self._next_restart_at = None
            self._bump()

    # -------------------------------------------------- 主逻辑（同步、可单测）

    def tick(self, now=None) -> list:
        """推进状态机；返回本轮动作标签列表（便于单测断言）。

        由 :meth:`run_forever` 的**单个** task 驱动：在线时该 task 阻塞在
        ``Event.wait()`` 上，**零轮询唤醒**；只在有明确截止时间时才 ``sleep``。
        """
        now = self._clock() if now is None else now
        actions = []
        with self._lock:
            if self._down_since is None:
                return actions                    # 在线/未armed：什么都不做
            elapsed = max(0.0, now - self._down_since)
            cfg = self._store.get()
            self._step_restart(now, elapsed, cfg, actions)
            self._step_alert(now, elapsed, cfg, actions)
            return actions

    # ---- L1：可选自动重启（默认关闭）

    def _step_restart(self, now, elapsed, cfg, actions) -> None:
        restart = cfg["restart"]
        if not restart["enabled"]:
            # 安全不变量：开关为假 ⇒ 连"枚举进程"都不做（单测断言调用次数 = 0）
            self._next_restart_at = None
            return
        due_at = self._next_restart_at
        if due_at is None:
            due_at = self._down_since + restart["after_seconds"]
        if now + 1e-9 < due_at:
            self._next_restart_at = due_at
            return
        self._attempt_restart(now, elapsed, cfg, actions)

    def _attempt_restart(self, now, elapsed, cfg, actions) -> None:
        restart, dry = cfg["restart"], bool(cfg["dry_run"])
        if not restart["enabled"]:                # 第二道闸
            return

        # 限流 1：每 min_interval 最多一次
        if self._last_restart_at is not None:
            wait_left = restart["min_interval_seconds"] - (now - self._last_restart_at)
            if wait_left > 0:
                self._next_restart_at = now + wait_left
                self._log("INFO", f"重启被限流（距上次不足 "
                                  f"{restart['min_interval_seconds']:.0f} 秒，还需 "
                                  f"{wait_left:.0f} 秒），{self._down_text(now)}")
                actions.append("restart_throttled_interval")
                return

        # 限流 2：每小时最多 max_per_hour 次
        recent = [t for t in self._restart_marks if now - t < RESTART_WINDOW_SECONDS]
        if len(recent) >= restart["max_per_hour"]:
            oldest = min(recent)
            self._next_restart_at = oldest + RESTART_WINDOW_SECONDS + 1.0
            self._log("INFO", f"重启被限流（近 1 小时已 {len(recent)} 次 ≥ "
                              f"{restart['max_per_hour']} 次），{self._down_text(now)}")
            actions.append("restart_throttled_hourly")
            return

        # 枚举 + **路径过滤**
        try:
            procs = list(self._lister() or [])
        except Exception as exc:
            self._next_restart_at = now + FRESH_PROCESS_GRACE_SECONDS
            self._log("WARN", f"枚举进程失败（{exc!r}）⇒ 本轮不动作，"
                              f"{FRESH_PROCESS_GRACE_SECONDS:.0f} 秒后重试")
            actions.append("restart_list_failed")
            return
        targets = [p for p in procs if is_napcat_mirror_path(p.path)]
        others = [p for p in procs if not is_napcat_mirror_path(p.path)]

        # 不抢跑：存在启动未满 60 秒的 NapCat 进程 ⇒ 等它
        fresh = [p for p in targets
                 if p.age_seconds is not None and p.age_seconds < FRESH_PROCESS_GRACE_SECONDS]
        if fresh:
            self._next_restart_at = now + FRESH_PROCESS_GRACE_SECONDS
            detail = ", ".join(f"PID {p.pid}({p.age_seconds:.0f}s)" for p in fresh)
            self._log("INFO", f"检测到启动未满 {FRESH_PROCESS_GRACE_SECONDS:.0f} 秒的 NapCat "
                              f"进程 {detail} ⇒ 不抢跑，等它自行恢复，{self._down_text(now)}")
            actions.append("restart_wait_fresh")
            return

        if dry:
            names = ", ".join(f"PID {p.pid}" for p in targets) or "（无残留进程）"
            self._log("INFO", f"[dry-run] 本应结束 NapCat 侧进程：{names}，并分离启动 "
                              f"{NAPCAT_LAUNCH_BAT}；{self._down_text(now)}")
            self._mark_restart(now, restart["min_interval_seconds"])
            actions.append("restart_dry_run")
            return

        self._log("WARN", f"L1 自动重启触发：镜像路径 NapCat 进程 {len(targets)} 个，"
                          f"非镜像（个人 QQ 等，已忽略）{len(others)} 个，"
                          f"{self._down_text(now)}")
        killed, failed = [], []
        for proc in targets:
            try:
                (killed if self._killer(proc) else failed).append(proc.pid)
            except SafetyError as exc:
                failed.append(proc.pid)
                self._log("WARN", f"安全拦截，未结束进程：{exc}")
            except Exception as exc:
                failed.append(proc.pid)
                self._log("WARN", f"结束进程 PID={proc.pid} 异常：{exc!r}")
        # ===== 安全闸：只要有目标杀不掉，就绝不启动新的 =====
        # 2026-10-21 实证（decisions/017）：bot 非管理员、镜像 QQ 是提权进程
        # ⇒ taskkill 全部 Access denied。旧逻辑"杀不掉也照启"，
        # 导致镜像 NapCat 实例从 5 个堆到 15 个，越重启越糟。
        # 注意 targets 为空（无残留进程）是正常情况，仍需启动。
        if failed:
            self._next_restart_at = now + restart["min_interval_seconds"]
            self._log("WARN",
                      f"L1 中止启动：{len(failed)} 个镜像 NapCat 进程结束失败 "
                      f"{failed}（成功 {killed or '无'}）⇒ **不重复启动**，避免实例堆积；"
                      f"{self._down_text(now)}")
            actions.append("restart_kill_failed")
            return

        mode, _ = self._launcher()
        self._log("WARN", f"L1 结果：已结束 {killed or '无'}，失败 无，"
                          f"启动方式 {mode} ⇒ {NAPCAT_LAUNCH_BAT}；等待 WS 恢复")
        self._mark_restart(now, restart["min_interval_seconds"])
        actions.append("restart")

    def _mark_restart(self, now, min_interval) -> None:
        self._restart_marks.append(now)
        self._last_restart_at = now
        self._next_restart_at = now + min_interval

    # ---- L2：置顶弹窗（无声音）

    def _step_alert(self, now, elapsed, cfg, actions) -> None:
        alert = cfg["alert"]
        if not alert["enabled"]:
            return
        if self._last_alert_at is None:
            # 冷启动宽限 / 真掉线阈值，两者分开
            # .get 回退：兼容尚未写入 startup_grace_seconds 的旧配置/测试配置
            grace = alert.get("startup_grace_seconds", alert["after_seconds"])
            first_after = grace if self._startup_pending else alert["after_seconds"]
            if elapsed + 1e-9 < first_after:
                return
        elif now - self._last_alert_at + 1e-9 < alert["repeat_seconds"]:
            return
        self._fire_alert(now, elapsed, cfg, actions)

    def _fire_alert(self, now, elapsed, cfg, actions) -> None:
        if self._alert_open():
            # 不堆叠：上一个弹窗还没关，推迟到下一个 repeat 周期再看
            self._last_alert_at = now
            self._log("INFO", f"告警重复到期，但上一个弹窗仍开着 ⇒ 不堆叠，"
                              f"{cfg['alert']['repeat_seconds']:.0f} 秒后再看，"
                              f"{self._down_text(now)}")
            actions.append("alert_skipped_open")
            return
        text = self._alert_text(elapsed)
        if cfg["dry_run"]:
            self._last_alert_at = now
            self._log("INFO", f"[dry-run] 本应弹窗（无声音/置顶）：{text}")
            actions.append("alert_dry_run")
            return
        proc = self._notifier(text)
        self._alert_proc = proc
        self._alert_visible = proc is not None
        if proc is None:
            # 弹窗没起来（脚本报错/无桌面权限）⇒ 快退避重试，别让 120 秒里无人知晓
            self._last_alert_at = (now - cfg["alert"]["repeat_seconds"]
                                   + ALERT_FAILURE_RETRY_SECONDS)
            self._log("WARN", f"弹窗未成功拉起 ⇒ {ALERT_FAILURE_RETRY_SECONDS:.0f} 秒后重试；"
                              f"{self._down_text(now)}")
            actions.append("alert_failed")
            return
        self._last_alert_at = now
        self._log("WARN", f"已弹窗告警（无声音/置顶）：{text}；{self._down_text(now)}")
        actions.append("alert")

    def _alert_open(self) -> bool:
        proc = self._alert_proc
        if proc is None:
            return False
        poll = getattr(proc, "poll", None)
        if poll is None:
            alive = bool(self._alert_visible)
        else:
            try:
                alive = poll() is None
            except Exception:
                alive = False
        if not alive:
            self._alert_proc = None
            self._alert_visible = False
        return alive

    def _close_alert_popup(self, reason: str) -> None:
        if not self._alert_open():
            return
        proc = self._alert_proc
        term = getattr(proc, "terminate", None)
        try:
            if term is not None:
                term()
            self._log("INFO", f"关闭残留弹窗（{reason}）")
        except Exception as exc:                  # pragma: no cover
            self._log("WARN", f"关闭残留弹窗失败：{exc!r}")
        finally:
            self._alert_proc = None
            self._alert_visible = False

    # -------------------------------------------------- 排程

    def next_deadline(self, now=None):
        """下一个需要醒来的绝对时刻；None = 无需唤醒（在线/未armed）。"""
        now = self._clock() if now is None else now
        with self._lock:
            if self._down_since is None:
                return None
            cfg = self._store.get()
            candidates = []
            if cfg["alert"]["enabled"]:
                if self._last_alert_at is None:
                    grace = cfg["alert"].get("startup_grace_seconds",
                                             cfg["alert"]["after_seconds"])
                    first_after = grace if self._startup_pending else cfg["alert"]["after_seconds"]
                    candidates.append(self._down_since + first_after)
                else:
                    candidates.append(self._last_alert_at + cfg["alert"]["repeat_seconds"])
            if cfg["restart"]["enabled"]:
                if self._next_restart_at is not None:
                    candidates.append(self._next_restart_at)
                else:
                    candidates.append(self._down_since + cfg["restart"]["after_seconds"])
            if not candidates:
                return None
            return min(candidates)

    async def run_forever(self) -> None:
        """**单个** task 驱动 tick：在线时空闲阻塞（零轮询唤醒）。"""
        if self._wake is None:
            self._wake = asyncio.Event()
        while True:
            now = self._clock()
            try:
                self.tick(now)
            except Exception as exc:              # pragma: no cover - 兜底隔离
                self._log("WARN", f"tick 异常（已隔离，不影响 bot）：{exc!r}")
            deadline = self.next_deadline(now)
            if deadline is None:
                self._wake.clear()
                if self.next_deadline(self._clock()) is None:
                    await self._wake.wait()
                    continue
                continue
            timeout = max(MIN_TICK_INTERVAL, deadline - self._clock())
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def start_task(self, loop=None):
        """创建唯一驱动 task（幂等）；返回该 task。"""
        if self._task is not None and not self._task.done():
            return self._task
        if self._wake is None:
            self._wake = asyncio.Event()
        target_loop = loop or asyncio.get_event_loop()
        self._task = target_loop.create_task(self.run_forever())
        return self._task

    def stop_task(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()

    def _bump(self) -> None:
        """唤醒驱动 task（在线时它阻塞在 Event 上，只有这里会叫醒它）。"""
        wake = self._wake
        if wake is None:
            return
        try:
            wake.set()
        except Exception:                         # pragma: no cover
            pass

    # -------------------------------------------------- 观测

    def state(self, now=None) -> dict:
        now = self._clock() if now is None else now
        with self._lock:
            cfg = self._store.get()
            down = None if self._down_since is None else max(0.0, now - self._down_since)
            deadline = self.next_deadline(now)
            return {
                "connected": self._connected,
                "started": self._started,
                "down_seconds": down,
                "down_minutes": None if down is None else round(down / 60.0, 1),
                "alert_enabled": cfg["alert"]["enabled"],
                "alert_open": self._alert_open(),
                "last_alert_at": self._last_alert_at,
                "restart_enabled": cfg["restart"]["enabled"],
                "dry_run": cfg["dry_run"],
                "last_restart_at": self._last_restart_at,
                "restarts_last_hour": len(
                    [t for t in self._restart_marks if now - t < RESTART_WINDOW_SECONDS]),
                "next_wake_in": None if deadline is None else round(max(0.0, deadline - now), 1),
            }


# ---------------------------------------------------------------- 单例（供 reverse_bot 使用）

_singleton = None


def get_watchdog(logger=None, config_path=None, **kwargs) -> OfflineWatchdog:
    """进程内单例；测试请直接构造 :class:`OfflineWatchdog`。"""
    global _singleton
    if _singleton is None:
        _singleton = OfflineWatchdog(config_path, logger, **kwargs)
    return _singleton


def reset_watchdog() -> None:
    global _singleton
    wd, _singleton = _singleton, None
    if wd is not None:
        wd.stop_task()
