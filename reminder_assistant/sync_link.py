"""设备间直连同步的线缆层 (阶段二第一切片: T1 局域网 / 同机双实例)。

分工:
  * 冲突判定与落库全在 store.apply_remote_op() —— 与本地写入分开, 且**不产生待发行**
  * 本文件只做三件事: 帧的编解码、握手/拉取/推送的次序、把一条连接上的异常关掉

线缆格式: 4 字节大端长度前缀 + UTF-8 JSON。一帧一问一答, 不做并发 ——
连接很短 (握手 → 拉 → 推 → 确认 → 关), 断了下次重连, 游标接上继续。

次序是刻意的: **先拉后推**。反过来的话, 本端先推过去的操作会基于一个旧版本号,
对端在拉取时又拿回自己的版本, 两条改动互相打架的概率明显变高。
"""

from __future__ import annotations

import json
import socket
import struct
import threading

from .store import Store

PROTOCOL = 1
MAX_FRAME = 1 << 20          # 单帧上限 1 MB (当前数据量远达不到, 超了直接报错而不是悄悄截断)
_HEADER = struct.Struct(">I")


class SyncError(RuntimeError):
    pass


# ---------------------------------------------------------------- 帧
def _send(sock: socket.socket, frame: dict) -> None:
    body = json.dumps(frame, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise SyncError(f"帧过大: {len(body)} 字节")
    sock.sendall(_HEADER.pack(len(body)) + body)


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _recv(sock: socket.socket) -> dict | None:
    head = _recv_exact(sock, _HEADER.size)
    if head is None:
        return None
    (size,) = _HEADER.unpack(head)
    if size <= 0 or size > MAX_FRAME:
        raise SyncError(f"帧长度非法: {size}")
    body = _recv_exact(sock, size)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------- 公共动作
def apply_ops(store: Store, ops: list[dict], peer: str) -> dict:
    """逐条应用对端操作。一条坏操作不该毁掉整批 —— 记进 errors 继续往下走。"""
    stats: dict[str, int] = {"applied": 0, "duplicate": 0, "rejected": 0, "orphan": 0}
    errors: list[str] = []
    for op in ops:
        try:
            result = store.apply_remote_op(op, peer)
        except Exception as exc:                      # noqa: BLE001 —— 单条失败不影响其余
            errors.append(f"{op.get('op_id')}: {exc}")
            continue
        stats[result] = stats.get(result, 0) + 1
    if errors:
        stats["errors"] = len(errors)
        stats["last_error"] = errors[-1]
    return stats


def _handle_connection(store: Store, conn: socket.socket, name: str) -> None:
    hello = _recv(conn)
    if not hello or hello.get("type") != "hello":
        return
    peer = str(hello.get("device_id") or "")
    if not peer:
        return
    if int(hello.get("protocol") or 0) != PROTOCOL:
        _send(conn, {"type": "error", "reason": f"协议版本不支持: {hello.get('protocol')}"})
        return
    _send(conn, {"type": "hello", "device_id": store.device_id(), "name": name,
                 "protocol": PROTOCOL, "cursor": store.cursor_of(peer)})
    while True:
        frame = _recv(conn)
        if not frame:
            return
        kind = frame.get("type")
        if kind == "pull":
            ops = store.outbox_after(int(frame.get("cursor") or 0))
            last = int(ops[-1]["id"]) if ops else int(frame.get("cursor") or 0)
            _send(conn, {"type": "push", "ops": ops, "last_id": last})
        elif kind == "push":
            stats = apply_ops(store, frame.get("ops") or [], peer)
            last = int(frame.get("last_id") or 0)
            if last:
                store.set_cursor(peer, last)
            _send(conn, {"type": "ack", "last_id": last, "stats": stats})
        elif kind in ("ack", "ping"):
            continue                                  # 单方面确认, 无需回应
        else:
            return                                    # bye / 未知帧: 收工


class SyncServer(threading.Thread):
    """监听对端连接。用线程而不是 asyncio: 打包体积与调试成本都更低。"""

    def __init__(self, store: Store, host: str = "127.0.0.1", port: int = 0, name: str = ""):
        super().__init__(daemon=True)
        self.store = store
        self.name = name or socket.gethostname()
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, int(port)))
        self._sock.listen(4)
        self.host, self.port = self._sock.getsockname()[0], self._sock.getsockname()[1]

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return                                 # stop() 关掉了监听口
            with conn:
                conn.settimeout(10.0)
                try:
                    _handle_connection(self.store, conn, self.name)
                except (OSError, ValueError, SyncError):
                    continue                           # 一条坏连接不该拖垮监听循环

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


def sync_once(store: Store, host: str = "127.0.0.1", port: int = 0,
              timeout: float = 10.0, name: str = "") -> dict:
    """主动连一次对端: 握手 → 拉 → 应用 → 确认 → 推 → 收确认。返回本次战果。"""
    with socket.create_connection((host, int(port)), timeout=timeout) as sock:
        sock.settimeout(timeout)
        _send(sock, {"type": "hello", "device_id": store.device_id(),
                     "name": name or socket.gethostname(), "protocol": PROTOCOL})
        hello = _recv(sock)
        if not hello or hello.get("type") != "hello":
            raise SyncError(f"对端没有正常握手: {hello}")
        peer = str(hello["device_id"])
        peer_cursor = int(hello.get("cursor") or 0)

        cursor = store.cursor_of(peer)
        _send(sock, {"type": "pull", "cursor": cursor})
        pulled = _recv(sock) or {}
        ops = pulled.get("ops") or []
        stats = apply_ops(store, ops, peer)
        last = int(pulled.get("last_id") or cursor)
        if last:
            store.set_cursor(peer, last)               # 先记水位再确认: 宁可重放也不重复
        _send(sock, {"type": "ack", "last_id": last, "stats": stats})

        mine = store.outbox_after(peer_cursor)
        my_last = int(mine[-1]["id"]) if mine else peer_cursor
        _send(sock, {"type": "push", "ops": mine, "last_id": my_last})
        ack = _recv(sock) or {}
        _send(sock, {"type": "bye"})

        return {"peer": peer, "peer_name": hello.get("name"), "pulled": len(ops),
                "pushed": len(mine), "apply": stats,
                "peer_apply": ack.get("stats") or {}}
