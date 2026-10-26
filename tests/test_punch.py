"""跨网络打洞用例: STUN 解析 / 票据 / 打洞 / 打洞后的可靠传输 / 失败路径。

全部在本机回环上跑, 不需要真的连公网 —— 但走的是与线上同一条代码路径
(真 UDP 套接字、真重发、真确认)。
"""

from __future__ import annotations

import socket
import struct
import threading
from datetime import datetime, timedelta, timezone

import pytest

from reminder_assistant.models import Reminder, ReminderPlan
from reminder_assistant.store import Store
from reminder_assistant.sync_link import run_sync, serve_punch
from reminder_assistant.sync_punch import (PunchError, PunchLink, decode_ticket,
                                           encode_ticket, parse_stun, stun_endpoint)

TZ = timezone(timedelta(hours=8))
BASE = datetime(2026, 10, 26, 10, 0, tzinfo=TZ)
COOKIE = 0x2112A442


def mk(title: str = "开会") -> Reminder:
    return Reminder(title=title, event_at=BASE + timedelta(minutes=60), tz="Asia/Shanghai",
                    level="日常", created_at=BASE, plan=ReminderPlan.preset("日常"))


def stun_response(ip: str = "203.0.113.7", port: int = 45123) -> bytes:
    raw_ip = struct.unpack(">I", socket.inet_aton(ip))[0] ^ COOKIE
    value = struct.pack(">BBH", 0, 0x01, port ^ (COOKIE >> 16)) + struct.pack(">I", raw_ip)
    attr = struct.pack(">HH", 0x0020, len(value)) + value
    return struct.pack(">HHI", 0x0101, len(attr), COOKIE) + b"\x00" * 12 + attr


def test_parse_stun_reads_xor_mapped_address():
    assert parse_stun(stun_response()) == ("203.0.113.7", 45123)


@pytest.mark.parametrize("payload", [
    b"", b"\x00" * 19, stun_response()[:8],
    struct.pack(">HHI", 0x0001, 0, COOKIE) + b"\x00" * 12,     # 请求类型, 不是响应
    struct.pack(">HHI", 0x0101, 0, 0xDEADBEEF) + b"\x00" * 12,  # cookie 不对
])
def test_parse_stun_rejects_garbage(payload):
    assert parse_stun(payload) is None


def test_ticket_round_trip():
    token = encode_ticket("dev-1", ("198.51.100.9", 51234), name="笔记本")
    assert token.startswith("HR1|198.51.100.9|51234|")
    got = decode_ticket(token)
    assert got == {"device_id": "dev-1", "name": "笔记本", "ip": "198.51.100.9", "port": 51234}


def test_ticket_tolerates_pasted_whitespace():
    token = encode_ticket("dev-9", ("203.0.113.5", 7000), name="A 电脑")
    assert decode_ticket("  " + token + "\r\n")["port"] == 7000


@pytest.mark.parametrize("bad", ["", "   ", "!!!!", "HR2|1.2.3.4|5|id|n", "HR1|1.2.3.4|abc|id|n",
                                 "HR1|1.2.3.4|99999|id|n", "HR1||5|id|n"])
def test_ticket_rejects_garbage(bad):
    with pytest.raises(PunchError):
        decode_ticket(bad)


def test_punch_succeeds_on_loopback():
    a, b = PunchLink(), PunchLink()
    try:
        ok = []
        th = threading.Thread(target=lambda: ok.append(a.punch(("127.0.0.1", b.local_port), 3.0)))
        th.start()
        assert b.punch(("127.0.0.1", a.local_port), 3.0) is True
        th.join(5)
        assert ok == [True]
        assert a.peer_addr is not None and b.peer_addr is not None
    finally:
        a.close()
        b.close()


def test_punch_fails_without_peer_and_keeps_data(tmp_path):
    store = Store(tmp_path / "A" / "t.db")
    store.add_reminder(mk("打洞失败也不该丢"))
    dead = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()                                    # 端口空着, 没人应答
    link = PunchLink()
    try:
        assert link.punch(("127.0.0.1", dead_port), 0.8) is False
        assert store.outbox_count("pending") == 1   # 数据原样留着
        assert len(store.list_active()) == 1
    finally:
        link.close()
        store.close()


def test_send_frame_without_punch_raises():
    link = PunchLink()
    try:
        with pytest.raises(PunchError):
            link.send_frame({"type": "hello"})
    finally:
        link.close()


def test_sync_over_punched_link(tmp_path):
    a_store = Store(tmp_path / "A" / "t.db")
    b_store = Store(tmp_path / "B" / "t.db")
    a_store.add_reminder(mk("打洞之后同步过来的"))
    a_link, b_link = PunchLink(), PunchLink()
    errors = []

    def server():
        try:
            if not b_link.punch(("127.0.0.1", a_link.local_port), 5.0):
                errors.append("B 没打通")
                return
            serve_punch(b_store, b_link, name="B")
        except Exception as exc:                      # noqa: BLE001
            errors.append(repr(exc))

    th = threading.Thread(target=server, daemon=True)
    th.start()
    try:
        assert a_link.punch(("127.0.0.1", b_link.local_port), 5.0) is True
        result = run_sync(a_store, send=a_link.send_frame, recv=lambda: a_link.recv_frame(), name="A")
        assert result["pushed"] == 1
        assert [r.title for r in b_store.list_active()] == ["打洞之后同步过来的"]
        assert not errors
    finally:
        th.join(5)
        a_link.close()
        b_link.close()
        a_store.close()
        b_store.close()


def test_stun_endpoint_returns_none_when_unreachable():
    # 指向一个必然没有 STUN 服务的地址: 只要不抛异常、返回 None 就算对
    assert stun_endpoint(servers=("127.0.0.1:9",), timeout=0.3) is None
