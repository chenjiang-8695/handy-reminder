package app.handyreminder

import android.Manifest
import android.app.Activity
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.Gravity
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.TextView
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.concurrent.Executors

/**
 * 0.1.0 的界面: 一张清单 + 一个"局域网同步"按钮 + 一行状态。
 *
 * 刻意不做便签手写风——这是给"能不能同步、能不能到点响"用的最小壳,
 * 观感对齐桌面端放到下一版, 否则一上来就把时间花在贴图上, 同步反而验不了。
 */
class MainActivity : Activity() {

    private lateinit var status: TextView
    private lateinit var list: ListView
    private lateinit var adapter: ArrayAdapter<String>
    private val worker = Executors.newSingleThreadExecutor()
    private lateinit var store: LocalStore

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
        val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        val refresh = Button(this).apply {
            text = "刷新"
            setOnClickListener { reload("已刷新") }
        }
        val syncButton = Button(this).apply {
            text = "局域网同步"
            setOnClickListener { startSync() }
        }
        row.addView(refresh, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        row.addView(syncButton, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))

        adapter = ArrayAdapter(this, android.R.layout.simple_list_item_1, ArrayList())
        list = ListView(this)

        root.addView(title)
        root.addView(status)
        root.addView(row)
        root.addView(list, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        setContentView(root)

        askNotificationPermission()
        reload("打开手账")
    }

    private fun askNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
    }

    private fun reload(note: String) {
        val items = store.activeItems()
        val fmt = DateTimeFormatter.ofPattern("MM-dd HH:mm").withZone(ZoneId.systemDefault())
        adapter.clear()
        for (item in items) {
            val whenText = item.eventAt?.let { fmt.format(it) } ?: "无时间"
            adapter.add("$whenText   ${item.title}")
        }
        adapter.notifyDataSetChanged()
        if (items.isEmpty()) {
            list.emptyView = TextView(this).apply {
                text = "还没有提醒。先在电脑上建一条，再点上面「局域网同步」。"
                gravity = Gravity.CENTER
            }
        }
        status.text = "$note · ${items.size} 条 · 待同步 ${store.pendingCount()} 条"
        AlarmScheduler(this).rescheduleAll()
    }

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

    override fun onDestroy() {
        worker.shutdownNow()
        store.close()
        super.onDestroy()
    }
}
