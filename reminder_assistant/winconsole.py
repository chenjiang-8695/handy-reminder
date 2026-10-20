"""Windows 窗口化程序的控制台附着。

打包成 --noconsole 之后 sys.stdout 是 None, print() 变成静默空操作。
但用户（和我们自己）仍然需要在终端里跑 `HandyReminder.exe --demo "明天三点开会"`
看结果、看 `--autostart status` 的输出。

做法: 启动时尝试 AttachConsole(ATTACH_PARENT_PROCESS)。
  * 从 cmd/PowerShell/bash 启动 -> 附着成功, 输出正常打印到那个终端
  * 从资源管理器双击启动   -> 没有父控制台, 附着失败, 安静跳过 (不弹黑框)
这样一个 exe 同时当 GUI 程序和 CLI 工具用, 不用出两个构建产物。
"""

from __future__ import annotations

import ctypes
import sys

ATTACH_PARENT_PROCESS = 0xFFFFFFFF


def configure_stdio() -> None:
    """把标准输出统一成 UTF-8 (管道/重定向), 并把真控制台的代码页切到 65001。

    为什么必须做 (打包版实测): 中文 Windows 上 stdout 走的是 GBK, `--selftest`
    打印的 "✓" 编不出来 -> UnicodeEncodeError 把自检打断; 用户看到的是一段 traceback,
    而不是"自检没通过"。管道下更糟: 读端 (build.py 按 UTF-8 解码) 会得到一堆乱码。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            if _is_console():
                ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                 # noqa: BLE001 - 输出流异常不能拖垮启动
            pass


def _is_console() -> bool:
    """当前进程是否附着在一个真控制台上 (而不是管道/文件)。"""
    if sys.platform != "win32":
        return False
    try:
        mode = ctypes.c_uint()
        return bool(ctypes.windll.kernel32.GetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), ctypes.byref(mode)))
    except Exception:
        return False


def attach_parent_console() -> bool:
    """返回是否成功附着到父进程的控制台。"""
    if sys.platform != "win32":
        return False
    if not getattr(sys, "frozen", False):
        return False                      # 源码运行本来就有控制台
    if sys.stdout is not None and sys.stdout.fileno() >= 0:
        return True
    try:
        if not ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            return False
        # 附着后重新打开标准流; 用 CONOUT$ 拿到那块控制台
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1, errors="replace")
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1, errors="replace")
        sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
        return True
    except Exception:
        return False
