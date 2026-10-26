package app.handyreminder

import org.json.JSONObject
import java.io.DataInputStream
import java.io.DataOutputStream
import java.net.InetSocketAddress
import java.net.Socket

/**
 * 同步客户端: 主动连电脑, 走与桌面端一模一样的帧 (4 字节长度前缀 + JSON)。
 *
 * 顺序必须是 先拉后推 (与桌面端 run_sync 一致): 先并进电脑的新操作, 再推自己的,
 * 否则自己的改动会基于旧版本号, 两边更容易打架。
 */
class SyncClient(
    private val store: LocalStore,
    private val deviceName: String,
) {
    data class Result(val peerName: String, val pulled: Int, val pushed: Int, val applied: Int)

    fun sync(host: String, port: Int, timeoutMs: Int = 8000): Result {
        Socket().use { socket ->
            socket.connect(InetSocketAddress(host, port), timeoutMs)
            socket.soTimeout = timeoutMs
            val input = DataInputStream(socket.getInputStream())
            val output = DataOutputStream(socket.getOutputStream())

            send(output, SyncProtocol.hello(store.deviceId(), deviceName))
            val hello = receive(input) ?: error("对端没有回应握手")
            if (hello.optString("type") != "hello") error("握手帧不对: $hello")
            val peer = hello.optString("device_id")
            val peerCursor = hello.optInt("cursor", 0)

            send(output, SyncProtocol.pull(store.cursorOf(peer)))
            val pulled = receive(input) ?: JSONObject().put("ops", emptyList<Any>())
            val ops = SyncProtocol.parseOps(pulled.optJSONArray("ops"))
            var applied = 0
            for (op in ops) {
                if (store.applyRemoteOp(op, peer) == "applied") applied++
            }
            store.relayOps(ops)
            val last = pulled.optInt("last_id", store.cursorOf(peer))
            if (last > 0) store.setCursor(peer, last)
            send(output, SyncProtocol.ack(last))

            val (mine, myLast) = store.outboxAfter(peerCursor)
            val pushable = mine.filter { it.origin != peer }
            send(output, SyncProtocol.push(pushable, myLast))
            receive(input)
            runCatching { send(output, SyncProtocol.bye()) }

            return Result(hello.optString("name", "?"), ops.size, pushable.size, applied)
        }
    }

    private fun send(out: DataOutputStream, frame: JSONObject) {
        val body = frame.toString().toByteArray(Charsets.UTF_8)
        require(body.size <= SyncProtocol.MAX_FRAME) { "帧过大: ${body.size}" }
        out.writeInt(body.size)
        out.write(body)
        out.flush()
    }

    private fun receive(input: DataInputStream): JSONObject? {
        val size = runCatching { input.readInt() }.getOrNull() ?: return null
        if (size <= 0 || size > SyncProtocol.MAX_FRAME) return null
        val buffer = ByteArray(size)
        input.readFully(buffer)
        return runCatching { JSONObject(String(buffer, Charsets.UTF_8)) }.getOrNull()
    }
}
