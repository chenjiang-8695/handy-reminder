"""局域网发现用例: 信标编解码 + 对端记账 + 发现后直接同步。

不依赖广播回环的稳定性 —— 用例里把目标显式指到 127.0.0.1 的某个端口 (单播),
所以既能测到真实收发路径, 又不会因为机器上开了 VPN / 多网卡而飘。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reminder_assistant.models import Reminder, ReminderPlan
from reminder_assistant.store import Store
from reminder_assistant.sync_discovery import Discovery, decode_beacon, encode_beacon
from reminder_assistant.sync_link import SyncServer, sync_once

TZ = timezone(timedelta(hours=8))
BASE = datetime(2026, 10, 26, 10, 0, tzinfo=TZ)


def mk(title: str = "开会") -> Reminder:
    return Reminder(title=title, event_at=BASE + timedelta(minutes=60), tz="Asia/Shanghai",
                    level="日常", created_at=BASE, plan=ReminderPlan.preset("日常"))


@pytest.fixture()
def stores(tmp_path):
    a = Store(tmp_path / "A" / "t.db")
    b = Store(tmp_path / "B" / "t.db")
    yield a, b
    a.close()
    b.close()


def test_beacon_round_trip():
    data = encode_beacon("dev-1", "办公室电脑", 8767)
    got = decode_beacon(data)
    assert got["device_id"] == "dev-1" and got["name"] == "办公室电脑" and got["sync_port"] == 8767


@pytest.mark.parametrize("payload", [
    b"", b"not json", b'{"magic":"Other/1","device_id":"x","sync_port":1}',
    b'{"magic":"HandyReminder/1","sync_port":8767}',
    b'{"magic":"HandyReminder/1","device_id":"x","sync_port":"abc"}',
])
def test_decode_rejects_bad_beacons(payload):
    assert decode_beacon(payload) is None


def test_probe_beacon_is_accepted_with_zero_port():
    # sync_port=0 表示"我在找对端但没开监听", 是合法探测包
    assert decode_beacon(encode_beacon("dev-2", "找人的", 0))["sync_port"] == 0


def test_peer_recorded_with_host_and_name(stores):
    a, b = stores
    target = Discovery(b, sync_port=9001, name="B 主机", port=0)
    source = Discovery(a, sync_port=9002, name="A 主机", port=0,
                       targets=[("127.0.0.1", target.port)])
    try:
        source.announce()
        assert target.poll(1.0) is True
        peers = target.peers()
        assert len(peers) == 1
        assert peers[0]["name"] == "A 主机" and peers[0]["sync_port"] == 9002
        assert peers[0]["host"] == "127.0.0.1"
    finally:
        source.stop()
        target.stop()


def test_own_beacon_is_ignored(stores):
    a, _ = stores
    disc = Discovery(a, sync_port=9003, name="自己", port=0)
    disc.targets = [("127.0.0.1", disc.port)]
    try:
        disc.announce()
        disc.poll(1.0)
        assert disc.peers() == []
    finally:
        disc.stop()


def test_peers_expire(stores):
    a, b = stores
    target = Discovery(b, sync_port=9004, name="B", port=0)
    source = Discovery(a, sync_port=9005, name="A", port=0,
                       targets=[("127.0.0.1", target.port)])
    try:
        source.announce()
        assert target.poll(1.0) is True
        assert target.peers(ttl=0.0) == []      # 过期了就当作没看见
    finally:
        source.stop()
        target.stop()


def test_scan_finds_peer_within_timeout(stores):
    a, b = stores
    server = SyncServer(b, port=0, name="办公室电脑")
    server.start()
    target = Discovery(b, sync_port=server.port, name="办公室电脑").start()
    scanner = Discovery(a, sync_port=0, name="家里电脑", port=0,
                        targets=[("127.0.0.1", target.port)])
    try:
        found = scanner.scan(1.5)
        assert [p["name"] for p in found] == ["办公室电脑"]
        assert found[0]["sync_port"] == server.port
    finally:
        scanner.stop()
        target.stop()
        server.stop()


def test_discovered_peer_can_sync(stores):
    """发现 → 直连 → 同步, 一路走通 (这就是 --peer auto 的机器版)。"""
    a, b = stores
    a.add_reminder(mk("同步后该出现在 B 上"))

    server = SyncServer(b, port=0, name="B")
    server.start()
    target = Discovery(b, sync_port=server.port, name="B").start()
    scanner = Discovery(a, sync_port=0, name="A", port=0,
                        targets=[("127.0.0.1", target.port)])
    try:
        found = scanner.scan(1.5)
        assert found, "没发现对端"
        result = sync_once(a, found[0]["host"], found[0]["sync_port"], name="A")
        assert result["pushed"] == 1
        assert [r.title for r in b.list_active()] == ["同步后该出现在 B 上"]
    finally:
        scanner.stop()
        target.stop()
        server.stop()
