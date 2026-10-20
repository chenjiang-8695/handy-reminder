"""端到端用例: 严格按 PRD 第三章的用户操作路径走一遍。

  按 Alt+Q 圈选 -> OCR 出文字 -> 解析时间 -> 生成卡片 -> 设频率
  -> 到点提醒 -> 点"划掉"销毁 (或过期静默清理) -> 翻页看历史
这里用 LogNotifier 代替真实弹窗, 其余链路全部是生产代码。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reminder_assistant.clock import FakeClock
from reminder_assistant.lifecycle import Lifecycle
from reminder_assistant.models import Reminder, ReminderPlan
from reminder_assistant.notify import LogNotifier, NotificationCenter, QuietHours
from reminder_assistant.scheduler import Scheduler
from reminder_assistant.store import Store
from reminder_assistant.timeparse import parse_time

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 8, 10, 0, tzinfo=TZ)


@pytest.fixture()
def app(tmp_path):
    store = Store(tmp_path / "app.db")
    clock = FakeClock(NOW)
    notifier = LogNotifier()
    fired = []
    sched = Scheduler(store, clock, on_fire=fired.append, max_sleep=0.01, sweep_interval=1e9)
    center = NotificationCenter(store, notifier)
    life = Lifecycle(store, sched)
    yield store, clock, sched, life, center, notifier, fired
    sched.stop()
    store.close()


def test_prd_user_journey(tmp_path):
    """PRD 三章全流程。"""
    store, clock, sched, life, center, notifier, fired = _fresh(tmp_path)

    # 1) Alt+Q 圈选 -> OCR 文本 -> 时间语义
    ocr_text = "明天下午三点开会"
    r = parse_time(ocr_text, now=clock.now())
    assert r.when == datetime(2026, 10, 9, 15, 0, tzinfo=TZ)

    # 2) 生成卡片 + 设置频率: 总 3 次, 第一次 14:50, 后续间隔 5 分钟
    rem = Reminder(title=ocr_text, source_text=ocr_text, event_at=r.when,
                   created_at=clock.now(), level="重要",
                   plan=ReminderPlan.custom(repeat=3, first_offset=-timedelta(minutes=10),
                                            interval=timedelta(minutes=5)))
    rem = life.create(rem)
    assert [o.fire_at for o in store.occurrences_of(rem.id)] == [
        datetime(2026, 10, 9, 14, 50, tzinfo=TZ),
        datetime(2026, 10, 9, 14, 55, tzinfo=TZ),
        datetime(2026, 10, 9, 15, 0, tzinfo=TZ)]

    # 3) 到点送达三次
    clock.set_wall(datetime(2026, 10, 9, 14, 49, tzinfo=TZ), mono_delta=0)
    assert sched.poll(clock.now()) == []
    for t in (14 * 60 + 50, 14 * 60 + 55, 15 * 60):
        clock.set_wall(datetime(2026, 10, 9, t // 60, t % 60, tzinfo=TZ))
        for f in sched.poll(clock.now()):
            assert center.deliver(f) == "delivered"
    assert len(notifier.sent) == 3
    assert all("明天下午三点开会" in body for _, body, _ in notifier.sent)

    # 4) 用户点"划掉" -> 事件立即销毁并归档
    life.complete(rem.id)
    assert store.get_reminder(rem.id).status == "completed"
    assert len(life.history()) == 1
    assert store.list_active() == []


def _fresh(tmp_path):
    store = Store(tmp_path / "journey.db")
    clock = FakeClock(NOW)
    notifier = LogNotifier()
    fired = []
    sched = Scheduler(store, clock, on_fire=fired.append, max_sleep=0.01, sweep_interval=1e9)
    return store, clock, sched, Lifecycle(store, sched), NotificationCenter(store, notifier), notifier, fired


def test_expired_event_is_silently_cleaned(tmp_path):
    """PRD 三章: 用户没处理, 到截止时间系统自动清除, 界面保持干净。"""
    store, clock, sched, life, center, notifier, fired = _fresh(tmp_path)
    r = parse_time("明天上午10点体检", now=clock.now())
    rem = life.create(Reminder(title="体检", event_at=r.when, created_at=clock.now(),
                               level="日常", plan=ReminderPlan.preset("日常")))
    assert rem.expire_at == r.when + timedelta(hours=24)

    clock.set_wall(r.when + timedelta(hours=1), mono_delta=3600)
    assert life.sweep(clock.now()) == []            # 还没到兜底时间
    clock.set_wall(r.when + timedelta(hours=25), mono_delta=3600 * 24)
    assert life.sweep(clock.now()) == [rem.id]      # 静默清理
    assert store.list_active() == []
    assert life.board(clock.now())["active"] == []
    assert life.history()[0].status == "expired"


def test_hit_complete_or_snooze_from_the_card(tmp_path):
    store, clock, sched, life, center, notifier, fired = _fresh(tmp_path)
    rem = life.create(Reminder(title="交周报", event_at=clock.now() + timedelta(minutes=30),
                               created_at=clock.now(), plan=ReminderPlan.preset("日常")))
    clock.set_wall(clock.now() + timedelta(minutes=30))
    assert len(sched.poll(clock.now())) == 1
    for f in fired:
        center.deliver(f)
    assert len(notifier.sent) == 1

    life.snooze(rem.id, 0, minutes=5)               # 点"5分钟后"
    clock.set_wall(clock.now() + timedelta(minutes=5))
    out = sched.poll(clock.now())
    assert len(out) == 1
    for f in out:
        assert center.deliver(f) == "delivered"
    assert len(notifier.sent) == 2                   # 一共只响两次, 没有重复


def test_quiet_hours_defers_then_sends_one_digest(tmp_path):
    store, clock, sched, life, center, notifier, fired = _fresh(tmp_path)
    center.quiet = QuietHours(enabled=True, start=__import__("datetime").time(22, 30),
                              end=__import__("datetime").time(7, 30))
    night = datetime(2026, 10, 8, 23, 0, tzinfo=TZ)
    for i in range(3):
        life.create(Reminder(title=f"夜间任务{i}", event_at=night + timedelta(minutes=i),
                             created_at=clock.now(), plan=ReminderPlan.preset("日常")))
    clock.set_wall(night + timedelta(minutes=3))     # 三条都到点
    for f in sched.poll(clock.now()):
        assert center.deliver(f) == "deferred"
    assert notifier.sent == []                       # 夜里一条都不弹

    morning = datetime(2026, 10, 9, 8, 0, tzinfo=TZ)
    assert center.flush_deferred(morning) == 3
    assert len(notifier.sent) == 1                   # 合并成一条摘要
    assert "夜间任务0" in notifier.sent[0][1]


def test_same_reminder_never_notified_twice(tmp_path):
    store, clock, sched, life, center, notifier, fired = _fresh(tmp_path)
    life.create(Reminder(title="只响一次", event_at=clock.now() + timedelta(minutes=1),
                         created_at=clock.now(), plan=ReminderPlan.preset("日常")))
    clock.set_wall(clock.now() + timedelta(minutes=1))
    out = sched.poll(clock.now())
    assert center.deliver(out[0]) == "delivered"
    assert center.deliver(out[0]) == "duplicate"     # 重放/重复点击
    assert len(notifier.sent) == 1


def test_board_snapshot_for_the_note_window(tmp_path):
    store, clock, sched, life, center, notifier, fired = _fresh(tmp_path)
    life.create(Reminder(title="紧急事", event_at=clock.now() + timedelta(hours=3),
                         created_at=clock.now(), level="紧急", plan=ReminderPlan.preset("紧急")))
    life.create(Reminder(title="日常事", event_at=clock.now() + timedelta(hours=1),
                         created_at=clock.now(), level="日常", plan=ReminderPlan.preset("日常")))
    board = life.board(clock.now())
    # 紧急事首次提醒在 event-1h = 2 小时后; 日常事就在 1 小时后 -> 日常事排前面
    assert [x["title"] for x in board["active"]] == ["日常事", "紧急事"]
    assert board["active"][1]["total"] == 5 and board["active"][1]["fired"] == 0
    assert board["now"] == clock.now()
