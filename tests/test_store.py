"""持久化层用例: 幂等 / 崩溃恢复 / 多实例 / 生命周期。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reminder_assistant.models import LEVELS, Reminder, ReminderPlan
from reminder_assistant.store import Store, StoreLocked

TZ = timezone(timedelta(hours=8))
BASE = datetime(2026, 10, 8, 10, 0, tzinfo=TZ)


def mk(delta_minutes: int = 60, level: str = "日常", **kw) -> Reminder:
    return Reminder(title=kw.pop("title", "开会"), event_at=BASE + timedelta(minutes=delta_minutes),
                    tz="Asia/Shanghai", level=level, created_at=BASE,
                    plan=ReminderPlan.preset(level), **kw)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def test_add_and_read_back(store):
    rem = store.add_reminder(mk())
    assert rem.id == 1
    got = store.get_reminder(rem.id)
    assert got.title == "开会"
    assert got.level == "日常"
    assert len(got.plan.offsets) == 1
    assert got.status == "active"
    # 落库即展开触发点
    assert len(store.pending_occurrences()) == 1


def test_plan_expansion_for_levels(store):
    store.add_reminder(mk(level="紧急"))
    occs = store.pending_occurrences()
    assert len(occs) == LEVELS["紧急"]["repeat"] == 5
    # 紧急: 提前 1 小时, 间隔 5 分钟 -> 09:00 09:05 09:10 09:15 09:20
    times = sorted(o.fire_at for o in occs)
    assert times[0] == BASE
    assert times[-1] == BASE + timedelta(minutes=20)


def test_persistence_across_reopen(tmp_path):
    path = tmp_path / "p.db"
    s1 = Store(path)
    rid = s1.add_reminder(mk()).id
    s1.close()

    s2 = Store(path)
    assert s2.get_reminder(rid).title == "开会"
    assert len(s2.pending_occurrences()) == 1
    s2.close()


def test_reschedule_updates_same_occurrence_and_bumps_gen(store):
    rem = store.add_reminder(mk())
    before = store.occurrence(rem.id, 0)
    new_time = before.fire_at + timedelta(minutes=10)
    gen = store.reschedule_occurrence(rem.id, 0, new_time)
    after = store.occurrence(rem.id, 0)
    assert after.gen == before.gen + 1 == gen
    assert after.fire_at == new_time
    # 行数不变: 是改期不是新建
    assert len(store.pending_occurrences()) == 1


def test_reschedule_rejects_finished_occurrence(store):
    rem = store.add_reminder(mk())
    store.cancel_pending(rem.id)
    with pytest.raises(Exception):
        store.reschedule_occurrence(rem.id, 0, BASE + timedelta(hours=5))


def test_fire_log_is_idempotent(store):
    assert store.try_record_fire("1#0", BASE) is True
    assert store.try_record_fire("1#0", BASE) is False
    assert store.has_fired("1#0") is True


def test_notification_dedup(store):
    assert store.try_record_notification("n:1#0", BASE) is True
    assert store.try_record_notification("n:1#0", BASE) is False


def test_complete_cancels_future_and_archives(store):
    rem = store.add_reminder(mk())
    n = store.complete_reminder(rem.id, BASE)
    assert n == 1
    assert store.get_reminder(rem.id).status == "completed"
    assert store.pending_occurrences() == []
    assert [r.id for r in store.list_history()] == [rem.id]
    assert store.list_active() == []


def test_expire_due_is_silent_cleanup(store):
    rem = store.add_reminder(mk(delta_minutes=60))     # expire_at = event + 24h
    assert store.expire_due(BASE) == []                # 还没到点
    ids = store.expire_due(BASE + timedelta(hours=30))
    assert ids == [rem.id]
    assert store.get_reminder(rem.id).status == "expired"
    assert store.pending_occurrences() == []
    assert store.expire_due(BASE + timedelta(hours=31)) == []   # 幂等


def test_prune_keeps_recent_history(store):
    rem = store.add_reminder(mk())
    store.complete_reminder(rem.id, BASE)
    assert store.prune(timedelta(days=30), BASE + timedelta(days=1)) == 0
    assert store.prune(timedelta(days=30), BASE + timedelta(days=40)) == 1
    assert store.get_reminder(rem.id) is None


def test_crash_recovery_resets_inflight(store):
    rem = store.add_reminder(mk())
    with store.conn:                                     # 伪造"出队后进程崩了"
        store.conn.execute("UPDATE occurrences SET state='firing' WHERE reminder_id=?", (rem.id,))
    assert store.recover_inflight() == 1
    assert store.occurrence(rem.id, 0).state == "pending"


def test_pending_occurrences_skip_completed_reminders(store):
    rem = store.add_reminder(mk())
    store.complete_reminder(rem.id, BASE)
    assert store.occurrence(rem.id, 0).state == "cancelled"
    assert store.pending_occurrences(only_active=False) == []
    assert store.pending_occurrences() == []


def test_second_instance_is_rejected(tmp_path):
    s1 = Store(tmp_path / "x.db")
    try:
        with pytest.raises(StoreLocked):
            Store(tmp_path / "x.db")
    finally:
        s1.close()


def test_lock_released_after_close(tmp_path):
    s1 = Store(tmp_path / "y.db")
    s1.close()
    s2 = Store(tmp_path / "y.db")     # 不应抛异常
    s2.close()


def test_meta_roundtrip(store):
    store.set_meta("last_scan", "2026-10-08")
    store.set_meta("last_scan", "2026-10-09")
    assert store.get_meta("last_scan") == "2026-10-09"
    assert store.get_meta("missing", "d") == "d"


def test_stats(store):
    store.add_reminder(mk())
    assert store.stats() == {"active": 1, "pending": 1}
