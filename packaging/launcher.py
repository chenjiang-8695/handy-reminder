"""无窗口启动器。

为什么需要它: 直接双击 .py / 用 `python -m reminder_assistant` 启动会留一个黑色的命令行窗口
(客户反馈原话: "还是有 python 的窗口提示使用快捷键启动ocr识别")。这个脚本负责两件事:

1. **挑一个真的能跑的解释器** —— 机器上可能有好几个 python, 而只有装了 PySide6 的那个能开窗口。
   挑错了的典型症状是"双击一下, 什么都没发生"或者弹一个 ImportError 的黑框。
2. 用 **pythonw.exe**(无控制台) 启动主程序, 并且自己立刻退出 —— 全程没有任何黑框。

用法(给用户): 双击 `启动手账.vbs`, 它用任意一个 python 跑这个脚本。
排障: `python packaging/launcher.py --dry-run` 会只打印选中了哪个解释器, 不真的启动。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 项目根目录 (reminder_assistant 的上一级)

# 窗口化的解释器: 优先 pythonw.exe (不带控制台), 找不到才退回 python.exe
_CANDIDATE_NAMES = ("pythonw.exe", "python.exe")


def _pythonw(path: Path) -> Path | None:
    """把解释器路径换成同目录下的无控制台版本。"""
    for name in _CANDIDATE_NAMES:
        cand = path.with_name(name)
        if cand.exists():
            return cand
    return None


def candidates() -> list[Path]:
    """候选解释器, 按优先级排。

    顺序的理由: 项目自己的虚拟环境 > 环境变量指定 > 正在运行这个脚本的解释器 >
    用户级的 pythoncore 安装 (开发商机器上常见的那个) > PATH 上的 python。
    """
    out: list[Path] = []
    override = os.environ.get("HANDYREMINDER_PYTHON")
    if override:
        out.append(Path(override))
    for venv in (ROOT / ".venv" / "Scripts", ROOT / "venv" / "Scripts"):
        for name in _CANDIDATE_NAMES:
            if (venv / name).exists():
                out.append(venv / name)
    out.append(Path(sys.executable))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        base = Path(local) / "Python"
        if base.is_dir():
            for python_dir in sorted(base.glob("pythoncore-*")):
                out.append(python_dir / "python.exe")
    deduped: list[Path] = []
    for path in out:
        resolved = _pythonw(path) or (path if path.exists() else None)
        if resolved and resolved not in deduped:
            deduped.append(resolved)
    return deduped


def can_import_pyside(exe: Path, timeout: float = 20.0) -> bool:
    """这个解释器能不能 import PySide6 (能不能开窗口就看这一条)。"""
    try:
        r = subprocess.run([str(exe), "-c", "import PySide6"],
                           capture_output=True, timeout=timeout,
                           creationflags=_no_window_flag())
        return r.returncode == 0
    except Exception:                                   # noqa: BLE001 - 起不来就当不行, 换下一个
        return False


def pick_interpreter(check=can_import_pyside) -> Path | None:
    for exe in candidates():
        if check(exe):
            return exe
    return None


def _no_window_flag() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def launch(exe: Path, extra_args: list[str] | None = None) -> int:
    """用无控制台的方式把主程序拉起来, 然后立刻返回 (不等待)。"""
    env = dict(os.environ)
    # 让子进程自己也不再弹控制台: 打包版/源码版都可能再往下起东西 (通知走 powershell)
    env.setdefault("PYTHONWARNINGS", "ignore")
    subprocess.Popen([str(exe), "-m", "reminder_assistant", "--serve", *(extra_args or [])],
                     cwd=str(ROOT), env=env,
                     creationflags=_no_window_flag(), close_fds=True)
    return 0


def _explain(msg: str) -> int:
    """没有可用解释器时说人话: 无窗口进程没有 stdout, 所以用系统消息框。"""
    text = (msg + "\n\n修法: 在这台机器上装好依赖 (需要 PySide6).\n"
            "例如:\n    \"<你的python.exe>\" -m pip install PySide6 tzdata mss\n"
            "然后再双击本快捷方式。也可以先跑一遍:\n"
            "    <python.exe> packaging\\launcher.py --dry-run")
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, text, "智能手账 · 启动失败", 0x30)
            return 3
        except Exception:                               # noqa: BLE001
            pass
    print(text)
    return 3


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    dry = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]
    exe = pick_interpreter()
    if exe is None:
        return _explain("没有找到装了 PySide6 的 Python 解释器。")
    if dry:
        print(f"选中的解释器: {exe}")
        return 0
    return launch(exe, args)


if __name__ == "__main__":
    raise SystemExit(main())
