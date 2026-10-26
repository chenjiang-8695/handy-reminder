"""待发箱 (outbox) 用例: 每次业务改动恰好一条待发行 / 同事务 / 重启不丢 / 幂等键唯一。

阶段一的验收口径 (docs/同步方案-B版.md §7):
  拔了网线功能一个都不少; 重启之后, 期间做过的每一次操作都在 outbox 里, 且只有一份。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from reminder_assistant.models import Reminder, ReminderPlan
from reminder_assistant.store import Store, StoreError

TZ = timezone(timedelta(hours=8))
BASE = datetime(2026, 10, 26, 10, 0, tzinfo=TZ)


def mk(minutes: int = 60, **kw) -> Reminder:
    return Reminder(title=kw.pop("title", "开会"), event_at=BASE + timedelta(minutes=minutes),
                    tz="Asia/Shanghai", level=kw.pop("level", "日常"), created_at=BASE,
                    plan=ReminderPlan.preset("日常"), **kw)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def test_create_appends_one_op_with_version(store):
    rem = store.add_reminder(mk())
    ops = store.outbox_all()
    assert len(ops) == 1
    op = ops[0]
    assert op["op_type"] == "create"
    assert op["uuid"] == rem.uuid and rem.uuid
    assert op["state"] == "pending"
    assert json.loads(op["payload"])["title"] == "开会"
    got = store.get_reminder(rem.id)
    assert got.version == 1 and got.dirty == 1 and got.sync_state == "dirty"


def test_update_records_base_version_and_fields(store):
    rem = store.add_reminder(mk())
    assert store.update_attributes(rem.id, title="改个标题") is True
    ops = store.outbox_all()
    assert [o["op_type"] for o in ops] == ["create", "update"]
    payload = json.loads(ops[1]["payload"])
    assert payload["fields"] == {"title": "改个标题"}
    assert payload["version"] == 2
    # base_version 是"改之前"的版本号: 乐观锁要靠它判断对端有没有插队
    assert ops[1]["base_version"] == 1
    assert store.get_reminder(rem.id).version == 2


def test_update_title_also_records(store):
    rem = store.add_reminder(mk())
    store.update_title(rem.id, "新标题")
    assert store.outbox_count() == 2
    assert json.loads(store.outbox_all()[1]["payload"])["fields"] == {"title": "新标题"}


def test_complete_records_and_tree_completes_children(store):
    parent = store.add_reminder(mk())
    child = store.add_reminder(mk(title="子任务", parent_uuid=parent.uuid, depth=1, sort_key=1.0))
    store.complete_tree(parent.id)
    ops = store.outbox_all()
    types = [o["op_type"] for o in ops]
    # create, create, complete(父), complete(子)
    assert types.count("complete") == 2
    uuids = {o["uuid"] for o in ops if o["op_type"] == "complete"}
    assert uuids == {parent.uuid, child.uuid}
    assert store.get_reminder(parent.id).status == "completed"


def test_delete_keeps_tombstone_snapshot(store):
    rem = store.add_reminder(mk(title="要走的那条"))
    store.delete_reminder(rem.id)
    ops = store.outbox_all()
    delete = [o for o in ops if o["op_type"] == "delete"]
    assert len(delete) == 1
    payload = json.loads(delete[0]["payload"])
    assert payload["uuid"] == rem.uuid and payload["title"] == "要走的那条"
    assert store.get_reminder(rem.id) is None


def test_replan_and_expire_recorded(store):
    rem = store.add_reminder(mk())
    custom = ReminderPlan(offsets=(timedelta(minutes=60), timedelta(minutes=40)))
    store.replace_plan(rem.id, custom)
    ops = store.outbox_all()
    assert ops[-1]["op_type"] == "replan"
    assert json.loads(ops[-1]["payload"])["plan"] == custom.to_json()

    ids = store.expire_due(BASE + timedelta(days=3))
    assert ids == [rem.id]
    assert store.outbox_all()[-1]["op_type"] == "expire"


def test_failed_update_leaves_no_op(store):
    rem = store.add_reminder(mk())
    before = store.outbox_count()
    with pytest.raises(StoreError):
        store.update_attributes(rem.id, not_a_field=1)
    assert store.outbox_count() == before


def test_outbox_survives_restart_and_inflight_retries(tmp_path):
    path = tmp_path / "t.db"
    s = Store(path)
    rem = s.add_reminder(mk())
    first = s.outbox_pending()[0]["op_id"]
    s.outbox_mark(first, "inflight")
    assert s.outbox_count("inflight") == 1
    s.close()

    s2 = Store(path)
    try:
        ops = s2.outbox_pending()
        # 断电时"已发出未确认"的那条必须退回 pending 重投, 否则这条改动永远发不出去
        assert [o["op_id"] for o in ops] == [first]
        assert ops[0]["attempts"] == 1
        assert s2.get_reminder(rem.id).uuid == rem.uuid
    finally:
        s2.close()


def test_outbox_mark_and_unknown_state(store):
    store.add_reminder(mk())
    op_id = store.outbox_pending()[0]["op_id"]
    assert store.outbox_mark(op_id, "acked") is True
    assert store.outbox_pending() == []
    assert store.outbox_count("acked") == 1
    assert store.outbox_mark("不存在", "acked") is False
    with pytest.raises(StoreError):
        store.outbox_mark(op_id, "乱七八糟")


def test_op_ids_are_unique_and_ordered(store):
    for i in range(5):
        store.add_reminder(mk(minutes=i, title=f"第{i}条"))
    ops = store.outbox_all()
    assert len({o["op_id"] for o in ops}) == 5
    # 顺序就是因果顺序 (对端按这个顺序重放)
    assert [json.loads(o["payload"])["title"] for o in ops] == [f"第{i}条" for i in range(5)]


def test_prune_only_cleans_acked_ops(store):
    rem = store.add_reminder(mk())
    acked = store.outbox_pending()[0]["op_id"]
    store.outbox_mark(acked, "acked")
    store.update_title(rem.id, "还在队列里的那条")
    assert store.prune(older_than=timedelta(days=-1), now=BASE) == 0
    assert store.outbox_count("acked") == 0      # 已确认的可以清
    assert store.outbox_count("pending") == 1     # 未发的绝不能清
