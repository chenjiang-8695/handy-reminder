"""局域网设备发现 (阶段二第二切片): UDP 信标 + 同机回环。

每台设备每隔两秒发一份信标, 收到就记成"看得见的对端"。

* 信标里只有身份信息 (设备号 / 名称 / 同步端口), **不含任何提醒内容** ——
  发现阶段就能被同一网络里的人看到设备名, 所以名称默认是主机名, 用户可改。
* 同时发 255.255.255.255 和 127.0.0.1: 广播在多网卡 / 开了 VPN / 部分企业网络里
  不一定回环, 单独再发一份到本机回环, 才能保证"同一台机器上的两个实例"也找得到彼此。
* 只用标准库。没有 mDNS 依赖, 也没有要用户装的 Bonjour。

发现的语义是"尽力而为": 找不到对端不算错误, 调用方应该退回手工填 IP。
"""

from __future__ import annotations

import json
import socket
import threading
import time

from .store import Store

DISCOVERY_PORT = 8768
MAGIC = "HandyReminder/1"
BEACON_INTERVAL = 2.0
PEER_TTL = 15.0


def encode_beacon(device_id: str, name: str, sync_port: int) -> bytes:
    return json.dumps({"magic": MAGIC, "protocol": 1, "device_id": device_id,
                       "name": name, "sync_port": int(sync_port)}, ensure_ascii=False).encode("utf-8")


def decode_beacon(data: bytes) -> dict | None:
    """坏包一律返回 None —— 局域网里什么都能广播过来, 不能因为一个怪包把线程弄崩。"""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict) or obj.get("magic") != MAGIC:
        return None
    if not obj.get("device_id"):
        return None
    try:
        obj["sync_port"] = int(obj.get("sync_port") or 0)
    except (TypeError, ValueError):
        return None
    if obj["sync_port"] < 0:
        return None
    return obj


class Discovery:
    """广播 + 收包。start() 起两个线程, stop() 收干净 (测试里也走这两个方法)。"""

    def __init__(self, store: Store, sync_port: int = 0, name: str = "",
                 port: int = DISCOVERY_PORT, targets: list[tuple[str, int]] | None = None):
        self.store = store
        self.device_id = store.device_id()
        self.name = name or socket.gethostname()
        self.sync_port = int(sync_port)
        self._peers: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._sock.bind(("", int(port)))
        self._sock.settimeout(0.5)
        self.port = self._sock.getsockname()[1]
        self.targets = targets if targets is not None else [
            ("255.255.255.255", self.port), ("127.0.0.1", self.port)]

    # ------------------------------------------------------------ 生命周期
    def start(self) -> "Discovery":
        for fn in (self._listen, self._beat):
            th = threading.Thread(target=fn, daemon=True)
            th.start()
            self._threads.append(th)
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ------------------------------------------------------------ 收发
    def announce(self) -> None:
        """发一份信标。sync_port=0 表示"我在找对端、但我没开监听"(探测包)。"""
        data = encode_beacon(self.device_id, self.name, self.sync_port)
        for host, port in self.targets:
            try:
                self._sock.sendto(data, (host, port))
            except OSError:
                continue                            # 某个网卡发不出去不影响其它目标

    def poll(self, timeout: float = 0.5) -> bool:
        """收一份信标。返回是否记下了一个对端 (自己发的不算)。"""
        self._sock.settimeout(timeout)
        try:
            data, addr = self._sock.recvfrom(4096)
        except (OSError, socket.timeout):
            return False
        return self._record(data, addr)

    def _record(self, data: bytes, addr) -> bool:
        beacon = decode_beacon(data)
        if not beacon or beacon["device_id"] == self.device_id:
            return False                            # 自己的广播, 忽略
        with self._lock:
            self._peers[beacon["device_id"]] = {
                "device_id": beacon["device_id"], "name": beacon.get("name") or "?",
                "host": addr[0], "sync_port": beacon["sync_port"],
                "last_seen": time.time()}
        if beacon["sync_port"] == 0 and self.sync_port > 0:
            # 对方只是来问"有人吗": 直接回一份, 不用等下一次心跳 (发现从 2 秒降到毫秒级)
            try:
                self._sock.sendto(encode_beacon(self.device_id, self.name, self.sync_port), addr)
            except OSError:
                pass
        return True

    def peers(self, ttl: float = PEER_TTL) -> list[dict]:
        """能连的对端 (没开监听的探测方不列出来, 否则会连一个空端口)。"""
        now = time.time()
        with self._lock:
            return [dict(p, age=round(now - p["last_seen"], 1))
                    for p in self._peers.values()
                    if now - p["last_seen"] <= ttl and p["sync_port"] > 0]

    def scan(self, seconds: float = 5.0) -> list[dict]:
        """一边广播一边收, 到点返回发现的对端 (按名称排序, 结果稳定)。"""
        end = time.time() + max(0.2, seconds)
        while time.time() < end:
            self.announce()
            self.poll(min(0.5, max(0.05, end - time.time())))
        return sorted(self.peers(), key=lambda p: (p["name"], p["device_id"]))

    # ------------------------------------------------------------ 线程
    def _listen(self) -> None:
        while not self._stop.is_set():
            if not self.poll(0.5):
                continue

    def _beat(self) -> None:
        while not self._stop.is_set():
            self.announce()
            if self._stop.wait(BEACON_INTERVAL):
                return
