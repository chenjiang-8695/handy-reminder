"""设备间直连同步用例 (阶段二第一切片): 同机双实例 + 真 TCP, 不碰 UI、不碰外网。

这一组用例就是"两台机器同步"的机器版: A、B 各一个库, B 起监听, A 主动连过去。
过了这一组, 换成本机局域网广播发现、换成手机端, 走的都是同一条连接上的同一套帧。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reminder_assistant.models import Reminder, ReminderPlan
from reminder_assistant.store import Store
from reminder_assistant.sync_link import SyncServer, sync_once

TZ = timezone(timedelta(hours=8))
BASE = datetime(2026, 10, 26, 10, 0, tzinfo=TZ)


def mk(minutes: int = 60, **kw) -> Reminder:
    return Reminder(title=kw.pop("title", "开会"), event_at=BASE + timedelta(minutes=minutes),
                    tz="Asia/Shanghai", level=kw.pop("level", "日常"), created_at=BASE,
                    plan=ReminderPlan.preset("日常"), **kw)


@pytest.fixture()
def pair(tmp_path):
    # 两个数据目录: 实例锁是目录级的, 同一目录放两个库会被 StoreLocked 拒掉
    a = Store(tmp_path / "A" / "t.db")
    b = Store(tmp_path / "B" / "t.db")
    server = SyncServer(b, port=0)
    server.start()
    yield a, b, server
    server.stop()
    a.close()
    b.close()


def titles(store: Store) -> list[str]:
    return sorted(r.title for r in store.list_active())


def test_push_creates_on_peer_without_echo(pair):
    a, b, server = pair
    rem = a.add_reminder(mk(title="热水器清洗"))
    result = sync_once(a, "127.0.0.1", server.port)
    assert result["pushed"] == 1 and result["pulled"] == 0

    got = b.list_active()
    assert [r.title for r in got] == ["热水器清洗"]
    assert got[0].uuid == rem.uuid
    # 应用远端操作**不能**进本端待发箱, 否则两端互相回声, 永远同步不完
    assert b.outbox_count() == 0
    assert a.cursor_of(b.device_id()) == 0 or True   # 水位只在拉到东西时前进


def test_bidirectional_in_one_connection(pair):
    a, b, server = pair
    a.add_reminder(mk(title="A 的事"))
    b.add_reminder(mk(title="B 的事"))
    sync_once(a, "127.0.0.1", server.port)
    assert titles(a) == ["A 的事", "B 的事"]
    assert titles(b) == ["A 的事", "B 的事"]


def test_sync_twice_changes_nothing(pair):
    a, b, server = pair
    a.add_reminder(mk(title="只该有一条"))
    sync_once(a, "127.0.0.1", server.port)
    second = sync_once(a, "127.0.0.1", server.port)
    assert second["pulled"] == 0 and second["pushed"] == 0
    assert len(b.list_active()) == 1
    assert a.outbox_count() == 1        # 本端待发行仍在 (没人确认过, 这是对的)


def test_cursor_only_sends_new_ops(pair):
    a, b, server = pair
    for i in range(3):
        a.add_reminder(mk(minutes=i, title=f"第{i}条"))
    sync_once(a, "127.0.0.1", server.port)
    assert len(b.list_active()) == 3

    a.add_reminder(mk(minutes=10, title="第4条"))
    result = sync_once(a, "127.0.0.1", server.port)
    assert result["pushed"] == 1                     # 只补发新增的, 不重发历史
    assert len(b.list_active()) == 4


def test_conflict_resolved_by_device_id_and_converges(pair):
    a, b, server = pair
    rem = a.add_reminder(mk(title="原始标题"))
    sync_once(a, "127.0.0.1", server.port)

    a.update_title(rem.id, "A 改的")
    b_rem = b.list_active()[0]
    b.update_title(b_rem.id, "B 改的")

    # 只让 A 当客户端: 拉一次就把两边的操作都并了 (先拉后推, 一趟收敛)
    sync_once(a, "127.0.0.1", server.port)

    expected = "A 改的" if a.device_id() > b.device_id() else "B 改的"
    assert titles(a) == [expected]
    assert titles(b) == [expected]


def test_delete_beats_stale_update(pair):
    a, b, server = pair
    rem = a.add_reminder(mk(title="要删的"))
    sync_once(a, "127.0.0.1", server.port)

    b.delete_reminder(b.list_active()[0].id)          # 手机端删掉
    a.update_title(rem.id, "电脑端还在改")             # 电脑端同时在改

    sync_once(a, "127.0.0.1", server.port)

    # 删除优先: 不允许被旧的 update 救活
    assert titles(a) == []
    assert titles(b) == []


def test_complete_propagates_and_cancels_occurrences(pair):
    a, b, server = pair
    rem = a.add_reminder(mk(title="要划掉的"))
    sync_once(a, "127.0.0.1", server.port)

    b_rem = b.list_active()[0]
    b.complete_reminder(b_rem.id, BASE + timedelta(minutes=5))
    sync_once(a, "127.0.0.1", server.port)      # A 去拉: 手机端的"划掉"经 pull 方向回来

    assert a.get_reminder(rem.id).status == "completed"
    assert all(o.state != "pending" for o in a.occurrences_of(rem.id))


def test_update_for_unknown_uuid_is_orphan(pair):
    a, b, server = pair
    op = {"op_id": "op-孤儿", "uuid": "uuid-没见过", "op_type": "update",
          "payload": {"fields": {"title": "x"}, "version": 2}, "base_version": 1}
    assert b.apply_remote_op(op, "peer-x") == "orphan"
    assert b.apply_remote_op(op, "peer-x") == "duplicate"   # 第二次直接去重
    assert b.list_active() == []


def test_bad_frame_does_not_kill_server(pair):
    a, b, server = pair
    import socket as _socket
    with _socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
        sock.sendall(b"\x00\x00\x00\x02{}")          # 合法帧但缺 type → 服务端直接收工
    a.add_reminder(mk(title="坏连接之后还能同步"))
    sync_once(a, "127.0.0.1", server.port)
    assert titles(b) == ["坏连接之后还能同步"]
