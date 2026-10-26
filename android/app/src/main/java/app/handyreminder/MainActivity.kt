package app.handyreminder

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.app.DatePickerDialog
import android.app.TimePickerDialog
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.concurrent.Executors

/**
 * 手机端界面: **建一条**、**划掉一条**、和电脑同步。
 *
 * 时间输入用系统日期/时间选择器, 不在这里重写中文时间解析 ——
 * "明天下午三点"的解析只有桌面端那一份 (timeparse.py), 手机端再写一套两边必然对不上;
 * 手指选时间反而更快。
 */
class MainActivity : Activity() {

    private lateinit var status: TextView
    private lateinit var list: ListView
    private lateinit var adapter: ArrayAdapter<String>
    private val worker = Executors.newSingleThreadExecutor()
    private lateinit var store: LocalStore
    private var items: List<LocalStore.Item> = emptyList()

    companion object {
        /** 与桌面端 models.LEVELS 的预设完全一致; 单位秒, 负数=提前 */
        val LEVEL_PLANS = linkedMapOf(
            "日常" to listOf(0L),
            "重要" to listOf(-1800L, -1200L, -600L),
            "紧急" to listOf(-3600L, -3300L, -3000L, -2700L, -2400L),
        )
        private val SHOW = DateTimeFormatter.ofPattern("MM-dd HH:mm")
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = LocalStore(this)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(32, 48, 32, 24)
        }
        val title = TextView(this).apply {
            text = "智能手账 · 手机端"
            textSize = 20f
        }
        status = TextView(this).apply {
            textSize = 13f
            text = "设备: ${store.deviceName()}"
        }

        val buttons = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        fun actionButton(text: String, action: () -> Unit) = Button(this).apply {
            this.text = text
            setOnClickListener { action() }
        }
        val weight = { LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f) }
        buttons.addView(actionButton("＋ 新建") { showCreateDialog() }, weight())
        buttons.addView(actionButton("局域网同步") { startSync() }, weight())
        buttons.addView(actionButton("刷新") { reload("已刷新") }, weight())

        adapter = ArrayAdapter(this, android.R.layout.simple_list_item_1, ArrayList())
        list = ListView(this)
        list.setOnItemClickListener { _, _, position, _ ->
            items.getOrNull(position)?.let { askComplete(it) }
        }

        root.addView(title)
        root.addView(status)
        root.addView(buttons)
        root.addView(list, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        setContentView(root)

        askNotificationPermission()
        reload("打开手账")
    }

    // ------------------------------------------------------------ 列表
    private fun reload(note: String) {
        items = store.activeItems()
        val fmt = SHOW.withZone(ZoneId.systemDefault())
        adapter.clear()
        for (item in items) {
            val whenText = item.eventAt?.let { fmt.format(it) } ?: "无时间"
            adapter.add("$whenText   ${item.title}")
        }
        adapter.notifyDataSetChanged()
        if (items.isEmpty()) {
            list.emptyView = TextView(this).apply {
                text = "还没有提醒。点上面「＋ 新建」，或先在电脑上建一条再同步过来。"
                gravity = Gravity.CENTER
            }
        }
        status.text = "$note · ${items.size} 条 · 待同步 ${store.pendingCount()} 条"
        AlarmScheduler(this).rescheduleAll()
    }

    // ------------------------------------------------------------ 新建
    private fun showCreateDialog() {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 24, 48, 0)
        }
        val input = EditText(this).apply {
            hint = "写一句，例如：交周报"
            inputType = InputType.TYPE_CLASS_TEXT
        }
        val spinner = Spinner(this).apply {
            adapter = ArrayAdapter(
                this@MainActivity, android.R.layout.simple_spinner_dropdown_item,
                LEVEL_PLANS.keys.toList()
            )
        }
        var picked: LocalDateTime = LocalDateTime.now().plusHours(1).withSecond(0).withNano(0)
        val whenButton = Button(this).apply {
            text = picked.format(SHOW)
            setOnClickListener {
                DatePickerDialog(this@MainActivity, { _, y, m, d ->
                    picked = picked.withYear(y).withMonth(m + 1).withDayOfMonth(d)
                    TimePickerDialog(this@MainActivity, { _, hh, mm ->
                        picked = picked.withHour(hh).withMinute(mm)
                        text = picked.format(SHOW)
                    }, picked.hour, picked.minute, true).show()
                }, picked.year, picked.monthValue - 1, picked.dayOfMonth).show()
            }
        }
        box.addView(input)
        box.addView(whenButton)
        box.addView(spinner)

        AlertDialog.Builder(this)
            .setTitle("新建提醒")
            .setView(box)
            .setPositiveButton("记上") { _, _ ->
                val text = input.text.toString().trim()
                if (text.isEmpty()) {
                    Toast.makeText(this, "先写一句内容", Toast.LENGTH_SHORT).show()
                } else {
                    val level = spinner.selectedItem?.toString() ?: "日常"
                    val plan = LEVEL_PLANS[level] ?: listOf(0L)
                    val eventAt = picked.atZone(ZoneId.systemDefault()).toInstant()
                    store.addReminder(text, eventAt, level, plan)
                    reload("已记上（待同步）")
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    // ------------------------------------------------------------ 划掉
    private fun askComplete(item: LocalStore.Item) {
        AlertDialog.Builder(this)
            .setTitle(item.title)
            .setMessage("把这条划掉？（状态会同步到电脑）")
            .setPositiveButton("划掉") { _, _ ->
                store.complete(item.uuid)
                reload("已划掉（待同步）")
            }
            .setNegativeButton("取消", null)
            .show()
    }

    // ------------------------------------------------------------ 同步
    private fun startSync() {
        status.text = "正在找电脑 (最多 5 秒)…"
        worker.execute {
            val peers = runCatching { Discovery(this).scan(5) }.getOrElse { emptyList() }
            if (peers.isEmpty()) {
                runOnUiThread {
                    status.text = "没找到电脑：确认两边在同一个 WiFi，且电脑上用 --sync-listen 开着"
                    reload("未找到")
                }
                return@execute
            }
            val peer = peers.first()
            val result = runCatching {
                SyncClient(store, store.deviceName()).sync(peer.host, peer.syncPort)
            }
            runOnUiThread {
                result.onSuccess {
                    status.text = "和 ${it.peerName} 同步完成：拉 ${it.pulled} / 推 ${it.pushed} / 应用 ${it.applied}"
                    reload("已同步")
                }.onFailure {
                    status.text = "同步失败：${it.message}（改动仍在本地，待同步 ${store.pendingCount()} 条）"
                    reload("同步失败")
                }
            }
        }
    }

    private fun askNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
    }

    override fun onDestroy() {
        worker.shutdownNow()
        store.close()
        super.onDestroy()
    }
}
