package app.handyreminder

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 载荷字段用例: 这些键名桌面端 `store.apply_remote_op` 会直接读,
 * 少一个 / 拼错一个, 表现是"同步成功但电脑上那张卡是空的"。
 */
class PayloadsTest {

    private val create = Payloads.create(
        uuid = "uuid-1",
        title = "手机上建的",
        eventAtIso = "2026-10-27T09:00:00+08:00",
        tz = "Asia/Shanghai",
        level = "重要",
        planSeconds = listOf(-1800L, -1200L, -600L),
        createdAtIso = "2026-10-26T18:00:00+08:00",
    )

    @Test
    fun `create 载荷带齐桌面端要读的字段`() {
        val required = listOf(
            "uuid", "title", "event_at", "tz", "level", "plan", "status",
            "created_at", "version", "parent_uuid", "depth", "sort_key", "batch_id", "duration_min",
        )
        for (key in required) {
            assertTrue("缺少字段 $key", create.has(key))
        }
        assertEquals("手机上建的", create.getString("title"))
        assertEquals("active", create.getString("status"))
        assertEquals(1, create.getInt("version"))
        assertEquals(0, create.getInt("depth"))
    }

    @Test
    fun `plan 必须是字符串形式的数组`() {
        // 桌面端 store 里 plan 是 TEXT 列, 用 json.loads 解析
        val plan = create.get("plan")
        assertTrue("plan 应该是字符串, 实际是 ${plan::class.java}", plan is String)
        assertEquals("[-1800,-1200,-600]", plan as String)
    }

    @Test
    fun `事件时间带时区偏移`() {
        // 桌面端用 datetime.fromisoformat 解析, 没有偏移量会退化成"本地时间", 跨时区就错
        val eventAt = create.getString("event_at")
        assertTrue(eventAt.endsWith("+08:00") || eventAt.endsWith("Z"))
    }

    @Test
    fun `complete 载荷带完成时间与版本`() {
        val payload: JSONObject = Payloads.complete("2026-10-26T19:00:00+08:00", 2)
        assertEquals(2, payload.getInt("version"))
        assertEquals("2026-10-26T19:00:00+08:00", payload.getString("completed_at"))
    }

    @Test
    fun `replan 载荷里 plan 也是字符串`() {
        val payload = Payloads.replan(listOf(0L), 3)
        assertTrue(payload.get("plan") is String)
        assertEquals(3, payload.getInt("version"))
    }
}
