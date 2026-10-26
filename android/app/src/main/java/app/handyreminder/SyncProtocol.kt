package app.handyreminder

import org.json.JSONArray
import org.json.JSONObject

/**
 * 与桌面端 (reminder_assistant/sync_link.py) 完全对齐的同步协议。
 *
 * 这里刻意不带任何 Android 依赖: 纯 Kotlin + org.json, 所以能在 JVM 单元测试里直接跑,
 * 不用开模拟器就能验证"帧长什么样、冲突怎么判"。协议字段名一旦写错, 桌面端会直接
 * 认不出来 —— 那是最难查的一类 bug, 所以先用测试钉住。
 */
object SyncProtocol {
    const val VERSION = 1
    const val MAX_FRAME = 1 shl 20          // 与桌面端一致: 单帧上限 1 MB

    /** 一条待发操作。payload 是桌面端 outbox 里那一列 JSON 字符串。 */
    data class Op(
        val opId: String,
        val uuid: String,
        val type: String,
        val payloadJson: String,
        val baseVersion: Int,
        val createdAt: String,
        val origin: String,
    ) {
        val payload: JSONObject get() = JSONObject(payloadJson.ifBlank { "{}" })

        /** 操作里带的权威版本号; 没有就退回 base_version。 */
        val version: Int
            get() = payload.optInt("version", baseVersion)

        fun toJson(): JSONObject = JSONObject()
            .put("op_id", opId)
            .put("uuid", uuid)
            .put("op_type", type)
            .put("payload", payloadJson)
            .put("base_version", baseVersion)
            .put("created_at", createdAt)
            .put("origin", origin)
    }

    fun parseOps(array: JSONArray?): List<Op> {
        if (array == null) return emptyList()
        val out = ArrayList<Op>(array.length())
        for (i in 0 until array.length()) {
            val o = array.optJSONObject(i) ?: continue
            val opId = o.optString("op_id")
            val uuid = o.optString("uuid")
            val type = o.optString("op_type")
            if (opId.isEmpty() || uuid.isEmpty() || type.isEmpty()) continue
            val rawPayload = o.opt("payload")
            val payloadJson = when (rawPayload) {
                is String -> rawPayload
                is JSONObject -> rawPayload.toString()
                else -> "{}"
            }
            out.add(
                Op(
                    opId = opId,
                    uuid = uuid,
                    type = type,
                    payloadJson = payloadJson,
                    baseVersion = o.optInt("base_version", 0),
                    createdAt = o.optString("created_at"),
                    origin = o.optString("origin"),
                )
            )
        }
        return out
    }

    /**
     * 冲突判定, 与桌面端 store.apply_remote_op 同一条规则:
     *  - 删除无条件优先 (不允许被旧 update 复活)
     *  - 否则版本大者胜; 版本相同用 origin 设备号定胜负 (两端算出同一个结果)
     */
    fun shouldAccept(op: Op, localVersion: Int, myDeviceId: String): Boolean {
        if (op.type == "delete") return true
        val v = op.version
        return v > localVersion || (v == localVersion && op.origin > myDeviceId)
    }

    // ---------------------------------------------------------------- 帧
    fun hello(deviceId: String, name: String, cursor: Int? = null): JSONObject {
        val f = JSONObject()
            .put("type", "hello")
            .put("device_id", deviceId)
            .put("name", name)
            .put("protocol", VERSION)
        if (cursor != null) f.put("cursor", cursor)
        return f
    }

    fun pull(cursor: Int): JSONObject = JSONObject().put("type", "pull").put("cursor", cursor)

    fun push(ops: List<Op>, lastId: Int): JSONObject {
        val arr = JSONArray()
        ops.forEach { arr.put(it.toJson()) }
        return JSONObject().put("type", "push").put("ops", arr).put("last_id", lastId)
    }

    fun ack(lastId: Int): JSONObject = JSONObject().put("type", "ack").put("last_id", lastId)

    fun bye(): JSONObject = JSONObject().put("type", "bye")

    // ---------------------------------------------------------------- 票据 (打洞用, 与桌面同格式)
    data class Ticket(val ip: String, val port: Int, val deviceId: String, val name: String)

    fun parseTicket(text: String): Ticket? {
        val raw = text.trim().replace("\r", "").replace("\n", "").replace(" ", "")
        val parts = raw.split("|")
        if (parts.size < 4 || parts[0] != "HR1") return null
        val port = parts[2].toIntOrNull() ?: return null
        if (port !in 1..65535 || parts[1].isEmpty() || parts[3].isEmpty()) return null
        return Ticket(parts[1], port, parts[3], parts.getOrElse(4) { "" })
    }
}
