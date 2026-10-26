package app.handyreminder

import org.json.JSONArray
import org.json.JSONObject

/**
 * 操作载荷的唯一构造处。
 *
 * 抽出来的理由: 桌面端 `store.apply_remote_op` 读的就是这些字段名
 * (title / event_at / tz / level / plan / status / created_at / version / ...),
 * 写错一个就是"同步成功但手机建的提醒在电脑上变成空白卡片"。
 * 这里不碰 Android, 所以能在纯 JVM 用例里钉住。
 */
object Payloads {

    fun create(
        uuid: String,
        title: String,
        eventAtIso: String,
        tz: String,
        level: String,
        planSeconds: List<Long>,
        createdAtIso: String,
    ): JSONObject = JSONObject()
        .put("uuid", uuid)
        .put("title", title)
        .put("event_at", eventAtIso)
        .put("tz", tz)
        .put("level", level)
        .put("plan", JSONArray(planSeconds).toString())
        .put("status", "active")
        .put("created_at", createdAtIso)
        .put("version", 1)
        .put("parent_uuid", "")
        .put("depth", 0)
        .put("sort_key", 0.0)
        .put("batch_id", "")
        .put("duration_min", 0)

    fun complete(completedAtIso: String, version: Int): JSONObject = JSONObject()
        .put("completed_at", completedAtIso)
        .put("version", version)

    fun replan(planSeconds: List<Long>, version: Int): JSONObject = JSONObject()
        .put("plan", JSONArray(planSeconds).toString())
        .put("version", version)
}
