package app.handyreminder

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 协议层用例: 这里每一条都对应桌面端的一个约定, 写错一个字段名, 真机上就是"连上了但什么都没同步"。
 * 纯 JVM 跑, 不需要模拟器。
 */
class SyncProtocolTest {

    private fun op(
        type: String,
        version: Int,
        origin: String = "dev-b",
        opId: String = "op-1",
        uuid: String = "uuid-1",
        payload: String = "",
    ) = SyncProtocol.Op(
        opId = opId,
        uuid = uuid,
        type = type,
        payloadJson = payload.ifBlank { JSONObject().put("version", version).toString() },
        baseVersion = version - 1,
        createdAt = "2026-10-26T10:00:00+08:00",
        origin = origin,
    )

    @Test
    fun `hello 帧字段与桌面端一致`() {
        val hello = SyncProtocol.hello("dev-a", "手机")
        assertEquals("hello", hello.getString("type"))
        assertEquals("dev-a", hello.getString("device_id"))
        assertEquals("手机", hello.getString("name"))
        assertEquals(1, hello.getInt("protocol"))
        assertFalse("客户端 hello 不该带 cursor", hello.has("cursor"))
    }

    @Test
    fun `pull 帧带游标`() {
        val pull = SyncProtocol.pull(7)
        assertEquals("pull", pull.getString("type"))
        assertEquals(7, pull.getInt("cursor"))
    }

    @Test
    fun `push 帧里 payload 必须是字符串`() {
        val push = SyncProtocol.push(listOf(op("create", 1)), 3)
        assertEquals("push", push.getString("type"))
        assertEquals(3, push.getInt("last_id"))
        val first = push.getJSONArray("ops").getJSONObject(0)
        // 桌面端 outbox 的 payload 列是 TEXT; 发成嵌套对象它会当成 {fields:...} 解析失败
        assertTrue(first.get("payload") is String)
        assertEquals("op-1", first.getString("op_id"))
        assertEquals("dev-b", first.getString("origin"))
    }

    @Test
    fun `解析对端的操作数组`() {
        val array = JSONArray()
            .put(JSONObject().put("op_id", "a").put("uuid", "u").put("op_type", "create")
                .put("payload", JSONObject().put("title", "开会").put("version", 2)))
            .put(JSONObject().put("op_id", "").put("uuid", "u2").put("op_type", "create"))
        val ops = SyncProtocol.parseOps(array)
        assertEquals(1, ops.size)                       // 缺 op_id 的直接丢掉
        assertEquals(2, ops[0].version)
        assertEquals("开会", ops[0].payload.getString("title"))
    }

    @Test
    fun `版本大者胜`() {
        assertTrue(SyncProtocol.shouldAccept(op("update", 3), localVersion = 2, myDeviceId = "dev-a"))
        assertFalse(SyncProtocol.shouldAccept(op("update", 2), localVersion = 3, myDeviceId = "dev-a"))
    }

    @Test
    fun `版本相同按设备号定胜负`() {
        val op = op("update", 2, origin = "dev-b")
        assertTrue("dev-b > dev-a, 应该接受", SyncProtocol.shouldAccept(op, 2, "dev-a"))
        assertFalse("dev-b < dev-c, 应该拒绝", SyncProtocol.shouldAccept(op, 2, "dev-c"))
    }

    @Test
    fun `删除无条件优先`() {
        assertTrue(SyncProtocol.shouldAccept(op("delete", 1), localVersion = 9, myDeviceId = "dev-z"))
    }

    @Test
    fun `票据与桌面端同格式`() {
        val ticket = SyncProtocol.parseTicket("HR1|192.168.1.7|8767|dev-a|家里电脑")
        assertEquals("192.168.1.7", ticket?.ip)
        assertEquals(8767, ticket?.port)
        assertEquals("dev-a", ticket?.deviceId)
        assertEquals("家里电脑", ticket?.name)
    }

    @Test
    fun `坏票据一律拒绝`() {
        assertNull(SyncProtocol.parseTicket(""))
        assertNull(SyncProtocol.parseTicket("HR2|1.2.3.4|5|id|n"))
        assertNull(SyncProtocol.parseTicket("HR1|1.2.3.4|abc|id|n"))
        assertNull(SyncProtocol.parseTicket("HR1|1.2.3.4|99999|id|n"))
    }
}
