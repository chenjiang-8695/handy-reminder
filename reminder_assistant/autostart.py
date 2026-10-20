"""开机自启。

Windows 用注册表 `HKCU\\...\\CurrentVersion\\Run`（不需要管理员权限，用户级），
这是最标准、最容易被安全软件接受、也最容易排查的方式。

为什么不用启动文件夹放快捷方式: 生成 .lnk 要调 COM (pywin32/powershell),
多一个依赖、多一个可能失败的环境; 注册表值是一行字符串, 出问题肉眼就能看。

命令行构造有两个必须处理的现实:
  1. **打包后** (PyInstaller) —— 直接指向 exe, 简单可靠。
  2. **源码运行** —— 不能直接写 `pythonw -m reminder_assistant`, 因为
     Run 键不支持设置工作目录, `-m` 会因找不到包而失败; 而且 python.exe 会
     闪一个黑窗口, 违反"无感运行"。
     所以生成一个 launcher 脚本 (把项目根目录塞进 sys.path) 并用 pythonw 启动。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Sequence

log = logging.getLogger("reminder.autostart")

APP_NAME = "HandyReminder"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LAUNCHER_NAME = "autostart_launcher.py"
DEFAULT_ARGS = ("--serve",)


class AutostartError(RuntimeError):
    pass


# ---------------------------------------------------------------- 纯函数 (可单测)

def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def python_launcher() -> str:
    """优先 pythonw.exe: 不带控制台窗口, 否则开机时会闪一个黑框。"""
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        cand = exe.with_name("pythonw.exe")
        if cand.exists():
            return str(cand)
    return str(exe)


def launcher_source(root: str | Path, args: Sequence[str] = DEFAULT_ARGS) -> str:
    """生成开机自启入口脚本: 不依赖工作目录, 不依赖 PYTHONPATH。"""
    arglist = ", ".join(repr(str(a)) for a in args)
    return (
        '"""由「智能手账提醒」自动生成 —— 开机自启入口, 手改会被覆盖。"""\n'
        "import sys\n\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        "from reminder_assistant.app import main\n\n"
        'if __name__ == "__main__":\n'
        f"    raise SystemExit(main([{arglist}]))\n"
    )


def build_command(*, frozen: bool = False, executable: str | None = None,
                  root: str | Path | None = None, launcher: str | Path | None = None,
                  args: Sequence[str] = DEFAULT_ARGS) -> str:
    """构造要写进注册表的命令行（含引号，防路径带空格）。

    打包后: 直接把启动参数写在命令行里。
    源码运行: 参数写进 launcher 脚本, 命令行只负责调 pythonw 启动它 ——
              Run 键无法设置工作目录, 把 `-m` 直接写进去会因找不到包而静默失败。
    """
    if frozen:
        exe = executable or sys.executable
        return " ".join([f'"{exe}"', *args])
    return f'"{executable or python_launcher()}" "{launcher}"'


def desktop_entry(*, root: str | Path, launcher: str | Path, name: str = APP_NAME,
                  args: Sequence[str] = DEFAULT_ARGS) -> str:
    """Linux 的 autostart .desktop 内容（与 Windows 路径无关，便于单测）。"""
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={name}\n"
        f"Comment=悬浮便签提醒\n"
        f"Exec={sys.executable} {launcher}\n"
        "X-GNOME-Autostart-enabled=true\n"
        "Terminal=false\n"
    )


# ---------------------------------------------------------------- 注册表读写

def _reg_set(name: str, value: str) -> None:
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def _reg_get(name: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _reg_delete(name: str) -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
            return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AutostartError(f"删除自启项失败: {exc}") from exc


# ---------------------------------------------------------------- 管理器

class Autostart:
    """开机自启开关。name 可换成测试专用名, 避免污染真实启动项。"""

    def __init__(self, name: str = APP_NAME, *, data_dir: Path | str | None = None,
                 args: Sequence[str] = DEFAULT_ARGS, root: Path | str | None = None):
        self.name = name
        self.args = tuple(args)
        self.root = Path(root) if root else project_root()
        if data_dir is None:
            from .store import default_data_dir

            data_dir = default_data_dir()
        self.data_dir = Path(data_dir)
        self.launcher = self.data_dir / LAUNCHER_NAME

    # ---- 查询
    @property
    def frozen(self) -> bool:
        return bool(getattr(sys, "frozen", False))

    def command(self) -> str:
        """按当前环境应当写入的命令行。"""
        return build_command(frozen=self.frozen, root=self.root, launcher=self.launcher,
                             args=self.args)

    def current(self) -> str | None:
        if os.name != "nt":
            path = Path.home() / ".config" / "autostart" / f"{self.name}.desktop"
            return path.read_text(encoding="utf-8") if path.exists() else None
        return _reg_get(self.name)

    def is_enabled(self) -> bool:
        return self.current() is not None

    def is_stale(self) -> bool:
        """已开启, 但登记的命令和现在应该用的不一致 (比如换了 Python 解释器路径)。"""
        cur = self.current()
        if cur is None:
            return False
        return cur.strip() != self.command().strip()

    def status(self) -> dict:
        cur = self.current()
        return {
            "enabled": cur is not None,
            "stale": self.is_stale(),
            "registered": cur,
            "expected": self.command(),
            "scope": "HKCU Run" if os.name == "nt" else "~/.config/autostart",
        }

    # ---- 开关
    def enable(self, *, refresh: bool = False) -> str:
        """开启。返回实际写入的命令行。refresh=True 时即使已开启也重写（修复失效路径）。"""
        if self.is_enabled() and not refresh and not self.is_stale():
            return self.command()

        if not self.frozen:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.launcher.write_text(launcher_source(self.root, self.args), encoding="utf-8")

        cmd = self.command()
        if os.name == "nt":
            try:
                _reg_set(self.name, cmd)
            except OSError as exc:
                raise AutostartError(f"写入开机自启失败 (可能需要放行安全软件): {exc}") from exc
        else:
            path = Path.home() / ".config" / "autostart" / f"{self.name}.desktop"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(desktop_entry(root=self.root, launcher=self.launcher,
                                          name=self.name, args=self.args), encoding="utf-8")

        # 写完必须立刻读回校验。实测在启用"注册表防护"的安全软件机器上,
        # 自启项写入后几十毫秒内会被静默摘掉 —— 不校验的话用户以为开了,
        # 开机却什么都没启动, 而且不会有任何报错。
        if not self.is_enabled():
            raise AutostartError(
                "自启项写入后立即被系统移除, 通常是被安全软件的注册表防护或组策略拦截。"
                "请在该软件里放行本程序; 另外从临时目录运行的副本会被当作可疑项清理, "
                "建议先安装到 Program Files 或用户目录再开启。")
        log.info("已开启开机自启: %s", cmd)
        return cmd

    def disable(self) -> bool:
        log.info("关闭开机自启: %s", self.name)
        if os.name != "nt":
            path = Path.home() / ".config" / "autostart" / f"{self.name}.desktop"
            if path.exists():
                path.unlink()
                return True
            return False
        return _reg_delete(self.name)

    def toggle(self) -> bool:
        """返回切换后的状态。"""
        if self.is_enabled():
            self.disable()
            return False
        self.enable()
        return True
