package app.handyreminder

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneId
import java.util.UUID

/**
 * 本地库, 表结构与桌面端 store.py 对齐 (reminders / outbox / applied_ops / meta)。
 *
 * 为什么照抄桌面端的结构而不是"手机简化版": 两端交换的是**操作流**, 不是整表快照。
 * 字段名字一旦不一致 (version / origin / op_id / uuid), 同步就只能在真机上靠猜。
 */
class LocalStore(context: Context) : SQLiteOpenHelper(context, DB_NAME, null, DB_VERSION) {

    companion object {
        const val DB_NAME = "handyreminder.db"
        const val DB_VERSION = 1
    }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            """
            CREATE TABLE reminders(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL DEFAULT '',
              event_at TEXT,
              tz TEXT NOT NULL DEFAULT '',
              level TEXT NOT NULL DEFAULT '日常',
              plan TEXT NOT NULL DEFAULT '[]',
              status TEXT NOT NULL DEFAULT 'active',
              created_at TEXT NOT NULL,
              completed_at TEXT,
              expire_at TEXT,
              note TEXT NOT NULL DEFAULT '',
              uuid TEXT NOT NULL DEFAULT '',
              parent_uuid TEXT NOT NULL DEFAULT '',
              depth INTEGER NOT NULL DEFAULT 0,
              sort_key REAL NOT NULL DEFAULT 0,
              batch_id TEXT NOT NULL DEFAULT '',
              version INTEGER NOT NULL DEFAULT 0,
              dirty INTEGER NOT NULL DEFAULT 0,
              sync_state TEXT NOT NULL DEFAULT 'clean',
              duration_min INTEGER NOT NULL DEFAULT 0
            )
            """.trimIndent()
        )
        db.execSQL("CREATE UNIQUE INDEX ix_rem_uuid ON reminders(uuid) WHERE uuid <> ''")
        db.execSQL(
            """
            CREATE TABLE outbox(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              op_id TEXT NOT NULL UNIQUE,
              uuid TEXT NOT NULL,
              op_type TEXT NOT NULL,
              payload TEXT NOT NULL DEFAULT '{}',
              base_version INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'pending',
              attempts INTEGER NOT NULL DEFAULT 0,
              last_error TEXT,
              origin TEXT NOT NULL DEFAULT ''
            )
            """.trimIndent()
        )
        db.execSQL("CREATE INDEX ix_outbox_state ON outbox(state, id)")
        db.execSQL("CREATE TABLE applied_ops(op_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL, peer TEXT NOT NULL DEFAULT '')")
        db.execSQL("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        // 0.1.0 还没有需要迁移的旧库; 真有了就在这里按 oldVersion 逐档升
    }

    // ------------------------------------------------------------ 身份
    fun deviceId(): String = meta("sync.device_id") ?: UUID.randomUUID().toString().also {
        setMeta("sync.device_id", it)
    }

    fun deviceName(): String = meta("sync.device_name") ?: android.os.Build.MODEL.orEmpty()

    // ------------------------------------------------------------ meta
    fun meta(key: String): String? =
        readableDb.query("meta", arrayOf("v"), "k=?", arrayOf(key), null, null, null).use {
            if (it.moveToFirst()) it.getString(0) else null
        }

    fun setMeta(key: String, value: String) {
        writableDb.execSQL(
            "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            arrayOf(key, value)
        )
    }

    fun cursorOf(peer: String): Int = meta("sync.cursor.$peer")?.toIntOrNull() ?: 0

    fun setCursor(peer: String, value: Int) = setMeta("sync.cursor.$peer", value.toString())

    fun hasPeer(): Boolean =
        readableDb.rawQuery("SELECT COUNT(*) FROM meta WHERE k LIKE 'sync.cursor.%'", null).use {
            it.moveToFirst() && it.getInt(0) > 0
        }

    // ------------------------------------------------------------ 读出给界面
    data class Item(
        val uuid: String,
        val title: String,
        val eventAt: Instant?,
        val level: String,
        val plan: List<Long>,
        val status: String,
    )

    fun activeItems(): List<Item> {
        val out = ArrayList<Item>()
        readableDb.query(
            "reminders", arrayOf("uuid", "title", "event_at", "level", "plan", "status"),
            "status='active'", null, null, null, "COALESCE(event_at,'')"
        ).use { c ->
            while (c.moveToNext()) {
                out.add(
                    Item(
                        uuid = c.getString(0),
                        title = c.getString(1),
                        eventAt = parseInstant(c.getString(2)),
                        level = c.getString(3),
                        plan = parsePlan(c.getString(4)),
                        status = c.getString(5),
                    )
                )
            }
        }
        return out
    }

    fun pendingCount(): Int =
        readableDb.rawQuery("SELECT COUNT(*) FROM outbox WHERE state='pending'", null).use {
            if (it.moveToFirst()) it.getInt(0) else 0
        }

    // ------------------------------------------------------------ 本地写入 (手机自己改)
    fun addReminder(
        title: String,
        eventAt: Instant,
        level: String = "日常",
        plan: List<Long> = listOf(0L),
    ): String {
        val uuid = UUID.randomUUID().toString()
        val now = Instant.now().toString()
        val tz = ZoneId.systemDefault().id
        // 载荷交给 Payloads 构造 (纯 JVM 有测试钉字段名); 这里只负责落库 + 记待发
        val payload = Payloads.create(uuid, title, eventAt.toString(), tz, level, plan, now)
        val db = writableDb
        db.beginTransaction()
        try {
            val values = ContentValues().apply {
                put("title", title); put("event_at", eventAt.toString())
                put("tz", ZoneId.systemDefault().id); put("level", level)
                put("plan", JSONArray(plan).toString()); put("status", "active")
                put("created_at", now); put("uuid", uuid); put("version", 1)
                put("dirty", 1); put("sync_state", "dirty")
            }
            db.insertOrThrow("reminders", null, values)
            recordOp(db, uuid, "create", payload, 0)
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
        return uuid
    }

    fun complete(uuid: String) {
        val db = writableDb
        db.beginTransaction()
        try {
            val version = db.rawQuery("SELECT version FROM reminders WHERE uuid=?", arrayOf(uuid)).use {
                if (it.moveToFirst()) it.getInt(0) else 0
            }
            val now = Instant.now().toString()
            db.execSQL(
                "UPDATE reminders SET status='completed', completed_at=?, version=version+1, dirty=1, sync_state='dirty' WHERE uuid=?",
                arrayOf(now, uuid)
            )
            recordOp(db, uuid, "complete", Payloads.complete(now, version + 1), version)
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    private fun recordOp(db: SQLiteDatabase, uuid: String, type: String, payload: JSONObject, baseVersion: Int) {
        db.execSQL(
            "INSERT INTO outbox(op_id,uuid,op_type,payload,base_version,created_at,origin) VALUES(?,?,?,?,?,?,?)",
            arrayOf(UUID.randomUUID().toString(), uuid, type, payload.toString(), baseVersion,
                Instant.now().toString(), deviceId())
        )
    }

    // ------------------------------------------------------------ 应用对端操作
    /** 与桌面端 apply_remote_op 一一对应: 返回 applied / duplicate / orphan / rejected。 */
    fun applyRemoteOp(op: SyncProtocol.Op, peer: String): String {
        val db = writableDb
        val myId = deviceId()
        db.beginTransaction()
        try {
            readableDb.rawQuery("SELECT 1 FROM applied_ops WHERE op_id=?", arrayOf(op.opId)).use {
                if (it.moveToFirst()) return "duplicate"
            }
            val local = db.rawQuery(
                "SELECT id, version FROM reminders WHERE uuid=?", arrayOf(op.uuid)
            ).use { if (it.moveToFirst()) it.getInt(0) to it.getInt(1) else null }

            if (op.type == "create") {
                if (local == null) {
                    insertFromSnapshot(db, op)
                    markApplied(db, op.opId, peer)
                    db.setTransactionSuccessful()
                    return "applied"
                }
            } else if (local == null) {
                markApplied(db, op.opId, peer)
                db.setTransactionSuccessful()
                return "orphan"
            }

            // 走到这里 local 一定非空 (上面两个分支已经把所有 null 情况返回了)
            val localVersion = local.second
            if (!SyncProtocol.shouldAccept(op, localVersion, myId)) {
                markApplied(db, op.opId, peer)
                db.setTransactionSuccessful()
                return "rejected"
            }
            val rowId = local.first
            when (op.type) {
                "create", "update" -> applyFields(db, op, rowId)
                "complete" -> db.execSQL(
                    "UPDATE reminders SET status='completed', completed_at=?, version=?, dirty=0, sync_state='clean' WHERE uuid=?",
                    arrayOf(op.payload.optString("completed_at", Instant.now().toString()), op.version, op.uuid)
                )
                "delete" -> db.execSQL("DELETE FROM reminders WHERE uuid=?", arrayOf(op.uuid))
                "replan" -> db.execSQL(
                    "UPDATE reminders SET plan=?, version=?, dirty=0, sync_state='clean' WHERE uuid=?",
                    arrayOf(op.payload.optString("plan", "[]"), op.version, op.uuid)
                )
                "expire" -> db.execSQL(
                    "UPDATE reminders SET status='expired', version=?, dirty=0, sync_state='clean' WHERE uuid=?",
                    arrayOf(op.version, op.uuid)
                )
            }
            markApplied(db, op.opId, peer)
            db.setTransactionSuccessful()
            return "applied"
        } finally {
            db.endTransaction()
        }
    }

    private fun applyFields(db: SQLiteDatabase, op: SyncProtocol.Op, rowId: Int) {
        val fields = op.payload.optJSONObject("fields") ?: op.payload
        val allowed = setOf("title", "event_at", "level", "note", "expire_at", "duration_min")
        val values = ContentValues()
        for (key in allowed) {
            if (fields.has(key)) values.put(key, fields.optString(key))
        }
        if (values.size() == 0) return
        values.put("version", op.version)
        values.put("dirty", 0)
        values.put("sync_state", "clean")
        db.update("reminders", values, "id=?", arrayOf(rowId.toString()))
    }

    private fun insertFromSnapshot(db: SQLiteDatabase, op: SyncProtocol.Op) {
        val p = op.payload
        val values = ContentValues().apply {
            put("title", p.optString("title"))
            put("event_at", p.optString("event_at"))
            put("tz", p.optString("tz"))
            put("level", p.optString("level", "日常"))
            put("plan", p.optString("plan", "[]"))
            put("status", p.optString("status", "active"))
            put("created_at", p.optString("created_at", Instant.now().toString()))
            put("completed_at", p.optString("completed_at"))
            put("expire_at", p.optString("expire_at"))
            put("note", p.optString("note"))
            put("uuid", op.uuid)
            put("parent_uuid", p.optString("parent_uuid"))
            put("depth", p.optInt("depth", 0))
            put("sort_key", p.optDouble("sort_key", 0.0))
            put("batch_id", p.optString("batch_id"))
            put("duration_min", p.optInt("duration_min", 0))
            put("version", op.version)
            put("dirty", 0)
            put("sync_state", "clean")
        }
        db.insertWithOnConflict("reminders", null, values, SQLiteDatabase.CONFLICT_IGNORE)
    }

    private fun markApplied(db: SQLiteDatabase, opId: String, peer: String) {
        db.execSQL(
            "INSERT OR IGNORE INTO applied_ops(op_id,applied_at,peer) VALUES(?,?,?)",
            arrayOf(opId, Instant.now().toString(), peer)
        )
    }

    // ------------------------------------------------------------ 待发 (手机改动推给电脑)
    fun outboxAfter(cursor: Int, limit: Int = 200): Pair<List<SyncProtocol.Op>, Int> {
        val ops = ArrayList<SyncProtocol.Op>()
        var last = cursor
        readableDb.query(
            "outbox", null, "id > ?", arrayOf(cursor.toString()), null, null, "id LIMIT $limit"
        ).use { c ->
            while (c.moveToNext()) {
                last = c.getInt(c.getColumnIndexOrThrow("id"))
                ops.add(
                    SyncProtocol.Op(
                        opId = c.getString(c.getColumnIndexOrThrow("op_id")),
                        uuid = c.getString(c.getColumnIndexOrThrow("uuid")),
                        type = c.getString(c.getColumnIndexOrThrow("op_type")),
                        payloadJson = c.getString(c.getColumnIndexOrThrow("payload")),
                        baseVersion = c.getInt(c.getColumnIndexOrThrow("base_version")),
                        createdAt = c.getString(c.getColumnIndexOrThrow("created_at")),
                        origin = c.getString(c.getColumnIndexOrThrow("origin")),
                    )
                )
            }
        }
        return ops to last
    }

    fun relayOps(ops: List<SyncProtocol.Op>) {
        val mine = deviceId()
        val db = writableDb
        db.beginTransaction()
        try {
            for (op in ops) {
                if (op.origin == mine) continue
                db.execSQL(
                    "INSERT OR IGNORE INTO outbox(op_id,uuid,op_type,payload,base_version,created_at,state,origin) " +
                        "VALUES(?,?,?,?,?,?,'acked',?)",
                    arrayOf(op.opId, op.uuid, op.type, op.payloadJson, op.baseVersion, op.createdAt, op.origin)
                )
            }
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    // ------------------------------------------------------------ 解析小工具
    private val readableDb: SQLiteDatabase get() = readableDatabase
    private val writableDb: SQLiteDatabase get() = writableDatabase

    fun parseInstant(text: String?): Instant? {
        if (text.isNullOrBlank()) return null
        return runCatching { OffsetDateTime.parse(text).toInstant() }
            .recoverCatching { Instant.parse(text) }
            .getOrNull()
    }

    fun parsePlan(text: String?): List<Long> {
        if (text.isNullOrBlank()) return emptyList()
        return runCatching {
            val arr = JSONArray(text)
            (0 until arr.length()).map { arr.optDouble(it, 0.0).toLong() }
        }.getOrDefault(emptyList())
    }
}
