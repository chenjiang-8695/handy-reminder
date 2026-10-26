"""三端转发用例: A 与 C 从不直连, 全靠 B 中转。

拓扑:
    A  ──推/拉──▶  B (监听)  ◀──推/拉──  C
A、C 之间没有任何连接。B 自己不产生提醒, 只把 A 的操作转述给 C。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reminder_assistant.models import Reminder, ReminderPlan
from reminder_assistant.store import Store
from reminder_assistant.sync_link import SyncServer, sync_once

TZ = timezone(timedelta(hours=8))
BASE = datetime(2026, 10, 26, 10, 0, tzinfo=TZ)


def mk(title: str = "开会") -> Reminder:
    return Reminder(title=title, event_at=BASE + timedelta(minutes=60), tz="Asia/Shanghai",
                    level="日常", created_at=BASE, plan=ReminderPlan.preset("日常"))


@pytest.fixture()
def mesh(tmp_path):
    a = Store(tmp_path / "A" / "t.db")
    b = Store(tmp_path / "B" / "t.db")
    c = Store(tmp_path / "C" / "t.db")
    server = SyncServer(b, port=0, name="B-中转")
    server.start()
    yield a, b, c, server
    server.stop()
    for s in (a, b, c):
        s.close()


def titles(store: Store) -> list[str]:
    return sorted(r.title for r in store.list_active())


def sync(store: Store, server: SyncServer) -> dict:
    return sync_once(store, "127.0.0.1", server.port)


def test_a_to_c_through_b(mesh):
    a, b, c, server = mesh
    a.add_reminder(mk("A 建的，C 也该看到"))

    sync(a, server)                                    # A → B
    assert titles(b) == ["A 建的，C 也该看到"]

    sync(c, server)                                    # C 从 B 拉
    assert titles(c) == ["A 建的，C 也该看到"]
    # C 没有把这条转成自己的"待发"操作 (否则两侧会互相回声)
    assert c.outbox_count("pending") == 0


def test_relay_keeps_original_origin(mesh):
    a, b, c, server = mesh
    a.add_reminder(mk("看 origin"))
    sync(a, server)
    sync(c, server)

    rel = [row for row in b.outbox_all() if row["origin"] == a.device_id()]
    assert rel, "B 的待发箱里应该有 A 那条的转述"
    assert all(row["state"] == "acked" for row in rel), "转述不该进本端重试队列"


def test_no_duplicates_after_full_round(mesh):
    a, b, c, server = mesh
    a.add_reminder(mk("只该有一条"))

    for _ in range(3):                                 # 来回多跑几轮
        sync(a, server)
        sync(c, server)
    assert titles(a) == ["只该有一条"]
    assert titles(b) == ["只该有一条"]
    assert titles(c) == ["只该有一条"]
    assert b.conn.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"] <= 2


def test_update_relayed(mesh):
    a, b, c, server = mesh
    rem = a.add_reminder(mk("原名"))
    sync(a, server)
    sync(c, server)

    a.update_title(rem.id, "改名了")
    sync(a, server)
    sync(c, server)
    assert titles(c) == ["改名了"]


def test_delete_relayed(mesh):
    a, b, c, server = mesh
    rem = a.add_reminder(mk("要删的"))
    sync(a, server)
    sync(c, server)
    assert titles(c) == ["要删的"]

    a.delete_reminder(rem.id)
    sync(a, server)
    sync(c, server)
    assert titles(c) == []
    assert titles(b) == []


def test_complete_from_c_relays_back_to_a(mesh):
    a, b, c, server = mesh
    rem = a.add_reminder(mk("C 来划掉"))
    sync(a, server)
    sync(c, server)

    c.complete_reminder(c.list_active()[0].id, BASE + timedelta(minutes=5))
    sync(c, server)                                    # C → B
    sync(a, server)                                    # A ← B
    assert a.get_reminder(rem.id).status == "completed"


def test_relay_ops_skips_own_and_bad(mesh):
    a, b, c, server = mesh
    rem = a.add_reminder(mk("x"))
    own = [row for row in a.outbox_all() if row["origin"] == a.device_id()]
    assert own
    op = {"op_id": own[0]["op_id"], "uuid": rem.uuid, "op_type": "create",
          "origin": a.device_id(), "payload": own[0]["payload"]}
    assert a.relay_ops([op]) == 0                       # 自己的操作不会再转述一遍
    assert b.relay_ops([{"op_id": "", "uuid": "", "op_type": ""}]) == 0
