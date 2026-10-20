"""调度器用例 —— 覆盖工程师清单第 1 条的全部考点。

每个用例都对应一个真实事故场景, 不是形式化断言:
    休眠补发 / 改时间 / 漂移 / 漏触发 / 重复触发 / 延迟提醒不新建任务
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from reminder_assistant.clock import FakeClock
from reminder_assistant.models import Reminder, ReminderPlan
from reminder_assistant.scheduler import Scheduler
from reminder_assistant.store import Store

TZ = timezone(timedelta(hours=8))
BASE = datetime(2026, 10, 8, 10, 0, tzinfo=TZ)      # 例: 早上 10:00


@pytest.fixture()
def env(tmp_path):
    store = Store(tmp_path / "s.db")
    clock = FakeClock(BASE)
    fired: list = []
    sched = Scheduler(store, clock, on_fire=fired.append, max_sleep=0.01,
                      sweep_interval=1e9)
    yield store, clock, sched, fired
    sched.stop()
    store.close()


def add(sched, clock, minutes=60, level="日常", plan=None, title="开会"):
    rem = Reminder(title=title, event_at=clock.now() + timedelta(minutes=minutes),
                   created_at=clock.now(), level=level, plan=plan or ReminderPlan.preset(level))
    return sched.add(rem)


# ---------------------------------------------------------------- 基本
def test_fires_exactly_at_due_time(env):
    store, clock, sched, fired = env
    add(sched, clock, minutes=60)                     # 11:00
    assert sched.poll(clock.now() + timedelta(minutes=30)) == []
    clock.advance(minutes=60)
    out = sched.poll(clock.now())
    assert len(out) == 1
    assert out[0].fire_at == datetime(2026, 10, 8, 11, 0, tzinfo=TZ)
    assert out[0].late_by == 0.0 and not out[0].is_late
    assert store.occurrence(out[0].reminder.id, 0).state == "fired"


def test_does_not_fire_one_second_early(env):
    store, clock, sched, fired = env
    add(sched, clock, minutes=60)
    assert sched.poll(clock.now() + timedelta(minutes=60) - timedelta(seconds=1)) == []
    assert fired == []


def test_multiple_occurrences_fire_in_order(env):
    store, clock, sched, fired = env
    plan = ReminderPlan.custom(repeat=3, first_offset=-timedelta(minutes=60),
                               interval=timedelta(minutes=20))
    add(sched, clock, minutes=60, plan=plan)          # 事件 11:00 -> 10:00/10:20/10:40
    assert len(sched.poll(clock.now())) == 1          # 10:00
    clock.advance(minutes=20)
    assert len(sched.poll(clock.now())) == 1          # 10:20
    clock.advance(minutes=19, seconds=59)
    assert sched.poll(clock.now()) == []
    clock.advance(seconds=1)
    assert len(sched.poll(clock.now())) == 1          # 10:40
    assert [f.fire_at.minute for f in fired] == [0, 20, 40]


def test_deadline_reminder_plan_from_prd(env):
    """PRD 3.2: 事件 15:50, 第一次提前 1 小时, 后续间隔 20 分钟 -> 14:50/15:10/15:30。"""
    store, clock, sched, fired = env
    clock.set_wall(datetime(2026, 10, 8, 12, 0, tzinfo=TZ), mono_delta=0)
    plan = ReminderPlan.custom(repeat=3, first_offset=-timedelta(hours=1),
                               interval=timedelta(minutes=20))
    add(sched, clock, minutes=230, plan=plan)         # 事件 15:50
    times = [o.fire_at for o in store.pending_occurrences()]
    assert times == [datetime(2026, 10, 8, h, m, tzinfo=TZ)
                     for h, m in ((14, 50), (15, 10), (15, 30))]


# ---------------------------------------------------------------- 延迟提醒
def test_snooze_updates_original_task_not_a_new_one(env):
    store, clock, sched, fired = env
    rem = add(sched, clock, minutes=60)               # 11:00
    clock.advance(minutes=59)
    new_time = sched.snooze(rem.id, 0, delta=timedelta(minutes=10))
    assert new_time == datetime(2026, 10, 8, 11, 9, tzinfo=TZ)

    clock.advance(minutes=1)                          # 11:00 不该响
    assert sched.poll(clock.now()) == []
    clock.advance(minutes=9)                          # 11:09 响
    out = sched.poll(clock.now())
    assert len(out) == 1 and out[0].fire_at == new_time

    # 库里仍只有 1 个触发点, 且它是"同一行"被改了期
    rows = store.occurrences_of(rem.id)
    assert len(rows) == 1 and rows[0].gen == 1
    assert sched.poll(clock.now() + timedelta(minutes=5)) == []    # 不重复


def test_snooze_after_it_rang_rearms_the_same_occurrence(env):
    store, clock, sched, fired = env
    rem = add(sched, clock, minutes=60)
    clock.advance(minutes=60)
    assert len(sched.poll(clock.now())) == 1          # 先响一次
    sched.snooze(rem.id, 0, delta=timedelta(minutes=5))
    clock.advance(minutes=5)
    out = sched.poll(clock.now())
    assert len(out) == 1, "响过之后点稍后, 必须能再次提醒"
    assert out[0].fire_at == datetime(2026, 10, 8, 11, 5, tzinfo=TZ)
    assert sched.stats["skipped_dup"] == 0
    assert len(store.occurrences_of(rem.id)) == 1


def test_snooze_unknown_occurrence_raises(env):
    store, clock, sched, fired = env
    with pytest.raises(KeyError):
        sched.snooze(999, 0)


# ---------------------------------------------------------------- 休眠 / 改时间
def test_suspend_backfills_missed_reminder(env):
    store, clock, sched, fired = env
    add(sched, clock, minutes=60)                     # 11:00
    sched._tick()                                     # 建立基线
    # 合盖 3 小时: 挂钟前进 10800 秒, 单调钟只前进 0.5 秒
    clock.advance(hours=3, mono=0.5)
    sched._tick()
    assert sched.stats["suspends"] == 1
    assert len(fired) == 1 and fired[0].is_late
    assert 7100 < fired[0].late_by < 7300             # 迟到约 2 小时
    assert store.occurrence(fired[0].reminder.id, 0).late_by > 0


def test_too_late_reminder_is_silently_missed(env):
    store, clock, sched, fired = env
    sched.late_grace = timedelta(hours=1)
    rem = add(sched, clock, minutes=60)
    sched._tick()
    clock.advance(hours=5, mono=1.0)                  # 关机很久后回来
    sched._tick()
    assert fired == []
    assert sched.stats["missed"] == 1
    occ = store.occurrences_of(rem.id)
    assert len(occ) == 1 and occ[0].state == "missed"


def test_clock_moved_backward_does_not_fire_early(env):
    store, clock, sched, fired = env
    add(sched, clock, minutes=60)                     # 11:00
    changes: list[tuple[float, float]] = []
    sched.on_clock_change = lambda w, m: changes.append((w, m))
    sched._tick()
    clock.advance(minutes=10)                         # 正常走到 10:10
    sched._tick()
    # 用户把系统时间改回 10:00
    clock.set_wall(BASE, mono_delta=0.5)
    sched._tick()
    assert sched.stats["clock_changes"] == 1 and changes
    assert sched.poll(clock.now()) == []              # 不能因为时间回拨就提前响
    clock.advance(hours=1, mono=3600)                 # 再走到 11:00
    assert len(sched.poll(clock.now())) == 1


def test_clock_jump_forward_fires_immediately(env):
    store, clock, sched, fired = env
    add(sched, clock, minutes=60)
    sched._tick()
    clock.advance(hours=2, mono=7200)                 # 时间被 NTP 往前跳 2 小时
    sched._tick()
    assert len(fired) == 1 and fired[0].fire_at.hour == 11


# ---------------------------------------------------------------- 重复 / 崩溃
def test_replayed_heap_entry_is_deduplicated(env):
    store, clock, sched, fired = env
    rem = add(sched, clock, minutes=60)
    clock.advance(minutes=60)
    assert len(sched.poll(clock.now())) == 1
    # 模拟一次"崩溃重启后堆被重建, 但 fired_log 还在"的重放
    sched._push(rem.id, 0, datetime(2026, 10, 8, 11, 0, tzinfo=TZ), gen=0)
    clock.advance(minutes=1)
    assert sched.poll(clock.now()) == []
    assert sched.stats["skipped_dup"] == 1


def test_reload_after_restart_keeps_only_pending(env, tmp_path):
    store, clock, sched, fired = env
    add(sched, clock, minutes=60, title="A")
    add(sched, clock, minutes=120, title="B")
    clock.advance(minutes=60)
    sched.poll(clock.now())                           # A 触发
    sched2 = Scheduler(store, clock)
    assert sched2.load() == 1                         # 只有 B 还在
    assert sched2.next_due() == datetime(2026, 10, 8, 12, 0, tzinfo=TZ)


def test_complete_cancels_pending(env):
    store, clock, sched, fired = env
    rem = add(sched, clock, minutes=60)
    sched.complete(rem.id)
    clock.advance(minutes=120)
    assert sched.poll(clock.now()) == []
    assert store.get_reminder(rem.id).status == "completed"


# ---------------------------------------------------------------- 规模
def test_scale_2000_reminders_stay_ordered(env):
    store, clock, sched, fired = env
    for i in range(2000):
        add(sched, clock, minutes=1 + (i % 600), title=f"R{i}")
    assert sched.size == 2000
    clock.advance(minutes=700)
    t0 = time.perf_counter()
    out = sched.poll(clock.now(), limit=5000)
    cost = time.perf_counter() - t0
    assert len(out) == 2000
    assert cost < 5.0, f"2000 条触发耗时 {cost:.2f}s, 调度复杂度失控"
    assert [f.fire_at for f in out] == sorted(f.fire_at for f in out)


# ---------------------------------------------------------------- 线程
def test_run_forever_thread_actually_fires(env):
    store, clock, sched, fired = env
    add(sched, clock, minutes=60)
    sched.start()
    clock.advance(minutes=61)
    deadline = time.time() + 3
    while time.time() < deadline and not fired:
        time.sleep(0.02)
    sched.stop()
    assert len(fired) == 1, "后台线程没有把到期提醒推出来"


def test_diagnose_exposes_state(env):
    store, clock, sched, fired = env
    add(sched, clock, minutes=60)
    d = sched.diagnose()
    assert d["heap"] == 1 and d["active"] == 1 and d["pending"] == 1
    assert d["next_due"] == "2026-10-08T11:00:00+08:00"
