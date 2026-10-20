"""打包产物的 GUI 冒烟: 真的把 exe 跑起来, 确认便签悬浮窗出现在桌面上了。

为什么必须单独做这一步: `--selftest` 用不到 Qt, 结构校验只看文件在不在 ——
"Qt 平台插件能不能加载、被排除掉的 Qt 模块有没有被别的模块依赖、内置字体能不能读",
只有在真正建出窗口时才走得到。而这几件事的故障表现恰好都是"双击没反应"。

做法: 起 `--serve`, 用 EnumWindows 找该进程的可见顶层窗口 (标题以「智能手账」开头),
找到就立刻退出进程 —— 全程不碰鼠标键盘, 也不会真的弹通知 (加 `--no-toast`)。

用法:
    python packaging/smoke_gui.py                 # 测 dist/HandyReminder/HandyReminder.exe
    python packaging/smoke_gui.py --timeout 30 --keep-log
返回码: 0 = 窗口起来了; 1 = 没起来 (打印原因与日志尾部)
"""

from __future__ import annotations

import argparse
import ctypes
import subprocess
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXE = ROOT / "dist" / "HandyReminder" / "HandyReminder.exe"
WINDOW_TITLE_PREFIX = "智能手账"
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def visible_windows_of(pid: int) -> list[tuple[int, str]]:
    """该进程名下可见的顶层窗口 (标题, hwnd)。"""
    u = ctypes.windll.user32
    found: list[tuple[int, str]] = []

    def cb(hwnd, _lparam):
        owner = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and u.IsWindowVisible(hwnd):
            length = u.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            u.GetWindowTextW(hwnd, buf, length + 1)
            found.append((hwnd, buf.value))
        return True

    u.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def _log_tail(path: Path, lines: int = 15) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return "(没有日志文件)"
    return "\n".join(text[-lines:])


def smoke(exe: Path = DEFAULT_EXE, timeout: float = 25.0,
          data_dir: Path | None = None, keep_log: bool = False) -> tuple[bool, str]:
    """返回 (是否成功, 说明)。"""
    if not exe.exists():
        return False, f"找不到 {exe}"
    tmp = tempfile.TemporaryDirectory(prefix="handy-smoke-")
    ddir = Path(data_dir or tmp.name)
    # 故意**不加 `--serve`**: 无子命令 = 用户双击 exe 时的路径, 也就是"打开不了"那个缺陷的位置。
    cmd = [str(exe), "--no-toast", "--data-dir", str(ddir)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    deadline = time.time() + timeout
    window: tuple[int, str] | None = None
    try:
        while time.time() < deadline:
            if proc.poll() is not None:                       # 进程自己退了 = 起不来
                out = (proc.stdout.read() or "").strip()
                return False, (f"进程提前退出 (退出码 {proc.returncode})。输出:\n{out}\n"
                               f"日志:\n{_log_tail(ddir / 'logs' / 'app.log')}")
            hits = [w for w in visible_windows_of(proc.pid)
                    if w[1].startswith(WINDOW_TITLE_PREFIX)]
            if hits:
                window = hits[0]
                break
            time.sleep(0.25)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:                  # pragma: no cover
                proc.kill()
        if not keep_log:
            tmp.cleanup()
    if window is None:
        return False, (f"{timeout:.0f} 秒内没有出现标题以「{WINDOW_TITLE_PREFIX}」开头的可见窗口。"
                       f"日志:\n{_log_tail(ddir / 'logs' / 'app.log')}")
    log = _log_tail(ddir / "logs" / "app.log", 8)
    bad = [ln for ln in log.splitlines() if "ERROR" in ln or "Traceback" in ln]
    if bad:
        return False, "窗口起来了, 但日志里有错误:\n" + "\n".join(bad)
    return True, f"窗口已出现 (hwnd={window[0]}, 标题「{window[1]}」), 进程退出码干净"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="打包产物 GUI 冒烟")
    p.add_argument("--exe", default=str(DEFAULT_EXE))
    p.add_argument("--timeout", type=float, default=25.0)
    p.add_argument("--keep-log", action="store_true", help="保留临时数据目录以便排查")
    args = p.parse_args(argv)

    ok, detail = smoke(Path(args.exe), timeout=args.timeout, keep_log=args.keep_log)
    print(("✓ " if ok else "✗ ") + "GUI 冒烟: " + detail)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
