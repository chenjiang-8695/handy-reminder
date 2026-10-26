"""「待同步 N 条」界面提示用例 (offscreen, 不需要桌面会话)。

要求:
  * 没配过对端的机器上永远不显示 (单机用户不该看到"待同步 12 条")
  * 配过对端、且有没发出去的改动时显示, 数字跟着变
  * 全部确认之后自动消失
  * 便签收起时不显示 (收起态只有一条纸边)
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="未安装 PySide6, 跳过 UI 用例")

from PySide6.QtWidgets import QApplication                    # noqa: E402

from reminder_assistant.app import App                       # noqa: E402
from reminder_assistant.clock import FakeClock               # noqa: E402
from reminder_assistant.models import Reminder, ReminderPlan  # noqa: E402
from reminder_assistant.ui.notepad import NotepadWindow       # noqa: E402

TZ = datetime(2026, 10, 26, 10, 0).astimezone().tzinfo
NOW = datetime(2026, 10, 26, 10, 0, tzinfo=TZ)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def note(tmp_path, qapp):
    a = App(tmp_path)
    a.clock = FakeClock(NOW)
    a.scheduler.clock = a.clock
    a.life.clock = a.clock
    win = NotepadWindow(a)
    yield a, win
    win.close()
    a.close()


def add(a, title="开会"):
    return a.store.add_reminder(Reminder(
        title=title, event_at=NOW + timedelta(hours=2), tz="Asia/Shanghai",
        level="日常", created_at=NOW, plan=ReminderPlan.preset("日常")))


def test_badge_hidden_for_single_machine(note):
    a, win = note
    add(a)
    win.refresh(force=True)
    assert win._sync_badge == ""
    assert win.sync_label.isHidden() is True


def test_badge_appears_after_peer_and_pending(note):
    a, win = note
    add(a)
    a.store.set_cursor("另一个设备", 0)          # 发生过一次同步
    win.refresh(force=True)
    assert win._sync_badge == "待同步 1 条"
    assert win.sync_label.isHidden() is False
    assert "1 条改动没发到别的设备" in win.sync_label.toolTip()


def test_badge_counts_and_disappears_when_acked(note):
    a, win = note
    add(a, "第一条")
    add(a, "第二条")
    a.store.set_cursor("另一个设备", 0)
    win.refresh(force=True)
    assert win._sync_badge == "待同步 2 条"

    for op in a.store.outbox_pending():
        a.store.outbox_mark(op["op_id"], "acked")   # 对端确认了
    win.refresh(force=True)
    assert win._sync_badge == ""
    assert win.sync_label.isHidden() is True


def test_badge_hidden_when_collapsed(note):
    a, win = note
    add(a)
    a.store.set_cursor("另一个设备", 0)
    win.refresh(force=True)
    win._collapsed = True
    win._refresh_sync_badge()
    win._layout_parts()
    assert win.sync_label.isHidden() is True
