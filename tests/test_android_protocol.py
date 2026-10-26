"""安卓客户端帧格式 × 桌面端服务端的兼容性用例。

为什么要有这一条: 安卓端跑不了模拟器就验不了"连上去到底通不通", 但协议字段名写错
在真机上的表现是"连上了、什么都没同步"——最难查的一类。所以这里用 Python 扮演
安卓客户端, 按 `android/app/src/main/java/app/handdyreminder/SyncProtocol.kt` 里
`Op.toJson()` / `hello()` / `pull()` / `push()` 的**同一套字段**, 打给真的桌面端服务端。

字段名一旦被改坏 (payload 发成对象而不是字符串、origin 丢了、op_type 拼错),
这条用例会立刻红。
"""

from __future__ import annotations

import json
import socket
import struct
from datetime import datetime, timedelta, timezone

import pytest

from reminder_assistant.store import Store
from reminder_assistant.sync_link import SyncServer

TZ = timezone(timedelta(hours=8))
BASE = datetime(2026, 10, 26, 10, 0, tzinfo=TZ)


def _send(sock: socket.socket, frame: dict) -> None:
    body = json.dumps(frame, ensure_ascii=False).encode("utf-8")
    sock.sendall(struct.pack(">I", len(body)) + body)


def _recv(sock: socket.socket) -> dict:
    head = b""
    while len(head) < 4:
        chunk = sock.recv(4 - len(head))
        assert chunk, "连接被对端关掉了"
        head += chunk
    (size,) = struct.unpack(">I", head)
    body = b""
    while len(body) < size:
        chunk = sock.recv(size - len(body))
        assert chunk
        body += chunk
    return json.loads(body.decode("utf-8"))


def android_op(uuid: str, title: str, version: int = 1) -> dict:
    """与 Kotlin 的 SyncProtocol.Op.toJson() 逐字段一致: payload 是**字符串**。"""
    payload = {
        "uuid": uuid, "title": title,
        "event_at": "2026-10-27T09:00:00+08:00", "tz": "Asia/Shanghai",
        "level": "日常", "plan": "[0.0]", "status": "active",
        "created_at": "2026-10-26T18:00:00+08:00",
        "version": version, "parent_uuid": "", "depth": 0, "sort_key": 0.0,
        "batch_id": "", "duration_min": 0,
    }
    return {
        "op_id": "phone-op-0001",
        "uuid": uuid,
        "op_type": "create",
        "payload": json.dumps(payload, ensure_ascii=False),
        "base_version": 0,
        "created_at": "2026-10-26T18:00:00+08:00",
        "origin": "phone-device-id",
    }


@pytest.fixture()
def desktop(tmp_path):
    store = Store(tmp_path / "PC" / "t.db")
    server = SyncServer(store, port=0, name="办公室电脑")
    server.start()
    yield store, server
    server.stop()
    store.close()


def test_android_frames_are_accepted_by_desktop_server(desktop):
    store, server = desktop
    phone_uuid = "0f0e0d0c-phone-4uuid-9000-000000000001"
    op = android_op(phone_uuid, "手机上建的这条")

    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
        sock.settimeout(5)
        # 1) hello —— 字段名与 Kotlin 一致, 不带 cursor (客户端不发游标)
        _send(sock, {"type": "hello", "device_id": "phone-device-id",
                     "name": "手机", "protocol": 1})
        hello = _recv(sock)
        assert hello["type"] == "hello" and hello["device_id"] == store.device_id()

        # 2) pull —— 桌面端回 push
        _send(sock, {"type": "pull", "cursor": 0})
        pulled = _recv(sock)
        assert pulled["type"] == "push" and pulled["ops"] == []

        # 3) ack + 推自己的操作
        _send(sock, {"type": "ack", "last_id": 0})
        _send(sock, {"type": "push", "ops": [op], "last_id": 1})
        ack = _recv(sock)
        assert ack["type"] == "ack"
        assert ack["stats"]["applied"] == 1

        _send(sock, {"type": "bye"})

    # 桌面端真的落库了, 而且是"干净"状态 (不是本地待发)
    saved = store.list_active()
    assert [r.title for r in saved] == ["手机上建的这条"]
    row = saved[0]
    assert row.uuid == phone_uuid
    assert row.version == 1 and row.dirty == 0 and row.sync_state == "clean"
    assert len(store.occurrences_of(row.id)) == 1      # 触发点也建出来了
    assert store.applied("phone-op-0001")


def test_android_ticket_and_desktop_ticket_agree(desktop):
    """两端的票据解析必须一致, 否则打洞阶段互相认不出。"""
    store, _ = desktop
    ticket = f"HR1|192.168.1.7|8767|{store.device_id()}|办公室电脑"
    parts = ticket.split("|")
    assert parts[0] == "HR1" and parts[1] == "192.168.1.7" and parts[2] == "8767"
    assert parts[3] == store.device_id()
