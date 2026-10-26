"""跨网络打洞 (阶段三, 尽力而为): STUN 拿公网地址 → 交换"票据" → UDP 直连。

诚实的能力边界:

* 打洞**不是**总能成功。家用路由器(锥形 NAT)一般能打通; 手机 4G/5G 走运营商级 NAT
  (对称型)时基本打不通。打不通时本模块不装作成功, 而是抛 / 返回失败, 让上层把
  "待同步 N 条" 原样留着, 等两端回到同一网络再补。
* 没有 TURN, 没有中转: 数据只在两台设备之间走。
* 信令靠人: 票据是一串可以直接发微信的短字符串, 两端各贴一次。没有服务器记这件事。

为什么不复用 TCP: 打洞开出的是 UDP 通路, 所以这里自带一层最小可靠传输
(序号 + 确认 + 重发), 只够传送我们这种一问一答的小帧; 帧超过 MTU 就明确报错,
而不是悄悄分片成一堆半截包。
"""

from __future__ import annotations

import json
import os
import socket
import struct
import time

STUN_SERVERS = ("stun.l.google.com:19302", "stun.miwifi.com:3478")
MAGIC_UDP = b"HRPUNCH1"
TICKET_VERSION = 1
MAX_DATAGRAM = 1200          # 留出余量, 避免被路径 MTU 切碎
_MAGIC_COOKIE = 0x2112A442


class PunchError(RuntimeError):
    pass


# ---------------------------------------------------------------- STUN
def parse_stun(data: bytes) -> tuple[str, int] | None:
    """从 Binding 成功响应里取公网 (ip, port)。坏包返回 None, 绝不抛。"""
    if len(data) < 20:
        return None
    msg_type, length, cookie = struct.unpack(">HHI", data[:8])
    if msg_type != 0x0101 or cookie != _MAGIC_COOKIE:      # 只要 Binding Success
        return None
    end = min(len(data), 20 + length)
    off = 20
    mapped: tuple[str, int] | None = None
    while off + 4 <= end:
        attr, alen = struct.unpack(">HH", data[off:off + 4])
        value = data[off + 4: off + 4 + alen]
        off += 4 + alen + ((4 - alen % 4) % 4)
        if attr == 0x0020 and len(value) >= 8 and value[1] == 0x01:      # XOR-MAPPED-ADDRESS
            port = struct.unpack(">H", value[2:4])[0] ^ (_MAGIC_COOKIE >> 16)
            raw = struct.unpack(">I", value[4:8])[0] ^ _MAGIC_COOKIE
            ip = socket.inet_ntoa(struct.pack(">I", raw))
            return ip, port
        if attr == 0x0001 and len(value) >= 8 and value[1] == 0x01:      # MAPPED-ADDRESS (老式)
            port = struct.unpack(">H", value[2:4])[0]
            ip = socket.inet_ntoa(value[4:8])
            mapped = (ip, port)
    return mapped


def stun_endpoint(servers: tuple[str, ...] = STUN_SERVERS, timeout: float = 2.0,
                  sock: socket.socket | None = None) -> tuple[str, int] | None:
    """问 STUN 服务器要自己的公网地址。全部失败返回 None (上层据此提示"打洞不可用")。"""
    own = sock is None
    s = sock or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for item in servers:
            host, _, port = item.partition(":")
            request = struct.pack(">HHI", 0x0001, 0, _MAGIC_COOKIE) + os.urandom(12)
            try:
                s.settimeout(timeout)
                s.sendto(request, (host, int(port or 3478)))
                data, _ = s.recvfrom(2048)
            except (OSError, socket.timeout):
                continue
            got = parse_stun(data)
            if got:
                return got
        return None
    finally:
        if own:
            s.close()


# ---------------------------------------------------------------- 票据 (信令靠人传)
def encode_ticket(device_id: str, endpoint: tuple[str, int], name: str = "") -> str:
    """票据用**明文**拼, 不用 base64。

    理由很实际: 一长串 base64 在聊天软件里会折行、复制容易漏尾巴, 而且看着像密钥;
    票据里本来就没有秘密 (设备号 + 公网地址 + 名字), 明文一眼能看懂、坏了也能看出坏在哪。
    """
    safe_name = (name or "").replace("|", "/").replace("\n", " ").strip()
    return f"HR1|{endpoint[0]}|{int(endpoint[1])}|{device_id}|{safe_name}"


def decode_ticket(text: str) -> dict:
    """解析对端票据。格式不对就抛 PunchError, 不返回半个结果。"""
    raw = (text or "").strip().replace("\r", "").replace("\n", "")
    if not raw:
        raise PunchError("票据是空的")
    parts = raw.split("|")
    if len(parts) < 4 or parts[0] != "HR1":
        raise PunchError("票据格式不对(应该以 HR1| 开头, 直接从聊天里整段复制)")
    ip, port_text, device_id = parts[1], parts[2], parts[3]
    name = parts[4] if len(parts) > 4 else ""
    if not ip or not device_id:
        raise PunchError("票据内容不完整")
    try:
        port = int(port_text)
    except ValueError:
        raise PunchError("票据里的端口不是数字") from None
    if not (0 < port < 65536):
        raise PunchError("票据里的端口超出范围")
    return {"device_id": device_id, "name": name or "?", "ip": ip, "port": port}


# ---------------------------------------------------------------- UDP 通路
class PunchLink:
    """打洞 + 最小可靠传输 (序号/确认/重发)。

    两端都先往后发小包 (打洞), 收到对方的小包就说明通路开了;
    之后 send_frame / recv_frame 在这条通路上传同步帧。
    """

    def __init__(self, bind_port: int = 0, device_id: str = ""):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("", int(bind_port)))
        self.sock.settimeout(0.2)
        self.local_port = self.sock.getsockname()[1]
        self.device_id = device_id
        self.peer_addr: tuple[str, int] | None = None
        self._stash: list[dict] = []
        self._seq = 0

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------ 打洞
    def _beacon(self) -> bytes:
        return MAGIC_UDP + json.dumps({"id": self.device_id}).encode("utf-8")

    def poke(self, addr: tuple[str, int]) -> None:
        try:
            self.sock.sendto(self._beacon(), addr)
        except OSError:
            pass

    def punch(self, peer: tuple[str, int], seconds: float = 20.0,
              interval: float = 0.25) -> bool:
        """朝对端连续发包, 收到对方任何一包就算通。成功返回 True 并记下实际来源地址。"""
        end = time.time() + max(0.5, seconds)
        while time.time() < end:
            self.poke(peer)
            try:
                data, addr = self.sock.recvfrom(4096)
            except (OSError, socket.timeout):
                continue
            if not data.startswith(MAGIC_UDP):
                continue
            # 用**实际来源地址**: 存在 NAT 时它和对端自报的公网端口可能不同
            self.peer_addr = (addr[0], addr[1])
            return True
        return False

    # ------------------------------------------------ 帧
    def _send_raw(self, obj: dict) -> None:
        if not self.peer_addr:
            raise PunchError("还没有打通, 不能发数据")
        body = MAGIC_UDP + json.dumps(obj, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_DATAGRAM:
            raise PunchError(f"这一批改动太大({len(body)} 字节), 请在同一个 WiFi 下同步一次")
        self.sock.sendto(body, self.peer_addr)

    def _read_one(self, timeout: float) -> dict | None:
        self.sock.settimeout(max(0.05, timeout))
        try:
            data, addr = self.sock.recvfrom(65535)
        except (OSError, socket.timeout):
            return None
        if not data.startswith(MAGIC_UDP):
            return None
        try:
            obj = json.loads(data[len(MAGIC_UDP):].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(obj, dict):
            return None
        if self.peer_addr is None:
            self.peer_addr = (addr[0], addr[1])
        return obj

    def send_frame(self, frame: dict, retries: int = 4, wait: float = 0.6) -> None:
        self._seq += 1
        seq = self._seq
        for _ in range(retries):
            self._send_raw({"t": "data", "seq": seq, "frame": frame})
            end = time.time() + wait
            while time.time() < end:
                obj = self._read_one(end - time.time())
                if obj is None:
                    break
                if obj.get("t") == "ack" and obj.get("seq") == seq:
                    return
                if obj.get("t") == "data":
                    self._stash.append(obj)          # 对端同时也在发, 先收着
                    self._ack(int(obj.get("seq") or 0))
        raise PunchError("对端没有确认(UDP 通路可能已断)")

    def _ack(self, seq: int) -> None:
        if seq:
            try:
                self._send_raw({"t": "ack", "seq": seq})
            except PunchError:
                pass

    def recv_frame(self, timeout: float = 15.0) -> dict | None:
        end = time.time() + timeout
        while True:
            if self._stash:
                obj = self._stash.pop(0)
            else:
                obj = self._read_one(max(0.05, end - time.time()))
            if obj is None:
                if time.time() >= end:
                    return None
                continue
            if obj.get("t") != "data":
                continue                              # ack 之类, 忽略
            seq = int(obj.get("seq") or 0)
            self._ack(seq)
            for dup in [o for o in self._stash if o.get("seq") == seq]:
                self._stash.remove(dup)               # 重发造成的重复帧
            frame = obj.get("frame")
            return frame if isinstance(frame, dict) else None
