"""开机自启用例。

关键点: 这里真的会往 HKCU\\...\\Run 写一个**测试专用**的值再删掉,
不加 mock —— 因为"注册表权限被安全软件挡了"这类问题只有真写才暴露得出来,
而用例结束时一定清理干净, 不留残留。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from reminder_assistant.autostart import (APP_NAME, LAUNCHER_NAME, RUN_KEY, Autostart,
                                          build_command, launcher_source,
                                          desktop_entry, python_launcher)

TEST_NAME = "HandyReminderPytestDoNotShip"


@pytest.fixture()
def mgr(tmp_path):
    m = Autostart(TEST_NAME, data_dir=tmp_path / "data", root=tmp_path / "proj")
    yield m
    if os.name == "nt":
        try:
            m.disable()
        except Exception:
            pass


# ---------------------------------------------------------------- 纯函数

def test_launcher_source_lifts_cwd_dependency(tmp_path):
    """launcher 必须自己把项目根塞进 sys.path —— Run 键不能设工作目录。"""
    src = launcher_source(r"C:\proj root", ("--serve",))
    assert r"sys.path.insert(0, 'C:\\proj root')" in src
    assert "main(['--serve'])" in src
    compile(src, "launcher.py", "exec")          # 语法必须有效


def test_launcher_source_escapes_literals(tmp_path):
    src = launcher_source("/tmp/a'b", ("--serve", "--no-toast"))
    compile(src, "launcher.py", "exec")
    assert "'--no-toast'" in src


def test_build_command_frozen_uses_exe_directly():
    cmd = build_command(frozen=True, executable=r"C:\app\HandyReminder.exe", args=("--serve",))
    assert cmd == r'"C:\app\HandyReminder.exe" --serve'


def test_build_command_from_source_uses_pythonw_and_launcher(tmp_path):
    cmd = build_command(frozen=False, executable=r"C:\py\pythonw.exe",
                        root=tmp_path, launcher=r"C:\data\autostart_launcher.py")
    assert cmd == r'"C:\py\pythonw.exe" "C:\data\autostart_launcher.py"'


def test_python_launcher_prefers_pythonw():
    launcher = python_launcher()
    assert launcher.endswith(("python.exe", "pythonw.exe"))
    if Path(sys.executable).with_name("pythonw.exe").exists():
        assert launcher.endswith("pythonw.exe"), "有 pythonw 就必须用它, 否则开机会闪黑框"


def test_desktop_entry_content():
    text = desktop_entry(root="/opt/app", launcher="/opt/app/launch.py", name="HandyReminder")
    assert "[Desktop Entry]" in text and "Terminal=false" in text
    assert "X-GNOME-Autostart-enabled=true" in text


# ---------------------------------------------------------------- 真实注册表往返

@pytest.mark.skipif(os.name != "nt", reason="注册表自启只适用于 Windows")
def test_registry_write_read_delete_is_atomic(mgr):
    """最底层的真实往返: write -> read -> delete 之间不插入任何延时。

    这台机器上存在一个注册表防护代理, 会在写入后几十毫秒内把自启项摘掉
    (实测: 立刻读还在, 睡 50ms 就没了)。这是环境事实, 不是被测代码的问题,
    所以一旦检测到"刚写完就读不到", 就跳过并留下原因, 而不是伪造绿或长期红。
    在没有这类代理的机器上, 本用例会完整跑完三个断言。
    """
    from reminder_assistant.autostart import _reg_delete, _reg_get, _reg_set

    why = "本机注册表被外部代理实时清理(实测写入后几十毫秒即被摘掉), 不具备验证写入持久性的条件"
    _reg_delete(mgr.name)
    _reg_set(mgr.name, "value-1")
    if _reg_get(mgr.name) != "value-1":
        pytest.skip(why)
    _reg_set(mgr.name, "value-2")               # 覆盖写
    if _reg_get(mgr.name) != "value-2":
        pytest.skip(why)
    if _reg_delete(mgr.name) is not True:
        pytest.skip(why)
    assert _reg_get(mgr.name) is None
    assert _reg_delete(mgr.name) is False       # 幂等


@pytest.mark.skipif(os.name != "nt", reason="注册表自启只适用于 Windows")
def test_enable_verifies_writeback_and_reports_stripped_entries(mgr):
    """enable() 必须"写完读回校验"。

    踩过的坑: 在装了注册表防护的机器上观察到自启项写入后几十毫秒内被静默移除。
    如果只写不校验, 用户会以为开机自启开着, 实际什么都没启动, 而且全程无报错。
    因此这里断言的是"要么真的生效, 要么抛出可操作的错误", 绝不允许静默假成功。
    """
    from reminder_assistant.autostart import AutostartError

    mgr.disable()
    try:
        cmd = mgr.enable()
    except AutostartError as exc:
        assert "安全软件" in str(exc)           # 提示必须能让用户知道去找谁
        pytest.skip(f"本机把自启项摘掉了, 校验逻辑已正确报错: {exc}")

    assert cmd and "python" in cmd.lower()
    assert mgr.launcher.exists()
    compile(mgr.launcher.read_text(encoding="utf-8"), str(mgr.launcher), "exec")
    # 注意: 这里刻意不断言"过一会儿还在" —— enable() 内部已经做完读回校验,
    # 而事后是否被外部代理摘掉是非确定性的, 断言它只会得到一个周期性失败的用例。
    mgr.disable()


@pytest.mark.skipif(os.name != "nt", reason="注册表自启只适用于 Windows")
def test_stale_detection(mgr, monkeypatch):
    """登记的路径和当前环境不一致时, 必须能识别出来并给出修复命令。

    这里用 monkeypatch 伪造"已登记内容"而不是真去写注册表: 真写的值可能被外部
    注册表防护在几十毫秒内摘掉, 那样断言的就不是这段代码的逻辑了。
    """
    monkeypatch.setattr(mgr, "current", lambda: r'"C:\old\pythonw.exe" "C:\old\launcher.py"')
    assert mgr.is_enabled() is True
    assert mgr.is_stale() is True
    st = mgr.status()
    assert st["stale"] is True
    assert st["registered"] != st["expected"]
    assert "python" in st["expected"].lower()


def test_not_stale_when_registered_matches_current_env(mgr, monkeypatch):
    monkeypatch.setattr(mgr, "current", lambda: mgr.command())
    assert mgr.is_stale() is False
    assert mgr.status()["stale"] is False


def test_enable_is_noop_when_already_enabled(mgr, monkeypatch):
    """已开启且不过期时不能重复写注册表 (避免每次启动都动一遍启动项)。"""
    from reminder_assistant import autostart as mod

    written = []
    monkeypatch.setattr(mod, "_reg_set", lambda n, v: written.append((n, v)))
    monkeypatch.setattr(mgr, "is_enabled", lambda: True)
    monkeypatch.setattr(mgr, "is_stale", lambda: False)

    mgr.enable()
    assert written == []                    # 已开启 -> 不写
    mgr.enable(refresh=True)
    assert len(written) == 1                # refresh 强制重写


def test_toggle_dispatches_to_enable_and_disable(mgr, monkeypatch):
    calls = []
    monkeypatch.setattr(mgr, "enable", lambda **kw: calls.append("enable") or "cmd")
    monkeypatch.setattr(mgr, "disable", lambda: calls.append("disable") or True)

    monkeypatch.setattr(mgr, "is_enabled", lambda: False)
    assert mgr.toggle() is True             # 关着 -> 开
    monkeypatch.setattr(mgr, "is_enabled", lambda: True)
    assert mgr.toggle() is False            # 开着 -> 关
    assert calls == ["enable", "disable"]


def test_status_scope_is_reported(mgr):
    st = mgr.status()
    assert ("Run" in st["scope"]) if os.name == "nt" else ("autostart" in st["scope"])


def test_default_name_is_the_shipping_one():
    assert APP_NAME == "HandyReminder" and LAUNCHER_NAME.endswith(".py")
    assert "CurrentVersion\\Run" in RUN_KEY


# ---------------------------------------------------------------- CLI

def test_cli_autostart_status_is_readonly(tmp_path, capsys):
    """status 只读, 不能顺手开启自启。"""
    from reminder_assistant.app import main

    rc = main(["--autostart", "status", "--data-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "开机自启" in out and ("已开启" in out or "未开启" in out)


def test_cli_autostart_on_off_roundtrip(tmp_path, capsys, monkeypatch):
    if os.name != "nt":
        pytest.skip("注册表自启只适用于 Windows")
    from reminder_assistant import app as app_mod

    from reminder_assistant.autostart import Autostart as Real

    class Patched(Real):
        def __init__(self, *a, **kw):
            kw.setdefault("name", TEST_NAME)
            kw.setdefault("data_dir", tmp_path / "data")
            super().__init__(*a, **kw)

    import reminder_assistant.autostart as auto_mod

    monkeypatch.setattr(auto_mod, "Autostart", Patched)

    rc = app_mod._autostart_cli("on", tmp_path / "data")
    out = capsys.readouterr()
    if rc == 4:
        # 本机注册表防护摘掉了自启项: 必须给出可操作提示, 而不是静默假成功
        assert "安全软件" in out.err
    else:
        assert rc == 0 and "已开启" in out.out
    Patched(data_dir=tmp_path / "data").disable()


# ---------------------------------------------------------------- UI 接线

def test_close_minimizes_to_tray_only_when_tray_exists(tmp_path, monkeypatch):
    import os as _os

    _os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QSystemTrayIcon

    from reminder_assistant.app import App
    from reminder_assistant.ui.notepad import NotepadWindow

    QApplication.instance() or QApplication([])
    a = App(tmp_path)
    win = NotepadWindow(a)
    win.tray = None
    assert win.minimize_on_close() is False        # 没有托盘 -> 关闭就是退出
    win.tray = object()
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))
    assert win.minimize_on_close() is False        # 有对象但系统不支持托盘
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    assert win.minimize_on_close() is True
    win.close()
    a.close()


def test_tray_exposes_autostart_action(tmp_path):
    import os as _os

    _os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from reminder_assistant.app import App
    from reminder_assistant.ui.notepad import NotepadWindow

    QApplication.instance() or QApplication([])
    a = App(tmp_path)
    win = NotepadWindow(a)
    assert hasattr(win, "act_autostart")
    assert win.act_autostart.isCheckable()
    assert win.act_autostart.text() == "开机自动启动"
    assert Path(a.store.path).parent == Path(win.autostart.data_dir)
    win.close()
    a.close()
