package app.handyreminder

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import java.time.Instant

/**
 * 本地提醒: 精确闹钟 + 通知。全程不联网, 这是"手机端做本地通知"这条决策的落地。
 *
 * 每次都由触发点反推下一次时间, 不做常驻服务 —— 手机被系统杀后台也不会漏响,
 * 因为闹钟是系统持有的 (这也是为什么不用前台服务硬撑)。
 */
class AlarmScheduler(private val context: Context) {

    private val alarmManager: AlarmManager =
        context.getSystemService(Context.ALARM_SERVICE) as AlarmManager

    fun rescheduleAll() {
        val store = LocalStore(context)
        val now = Instant.now()
        var scheduled = 0
        for (item in store.activeItems()) {
            val base = item.eventAt ?: continue
            item.plan.forEachIndexed { index, offsetSeconds ->
                val fireAt = base.plusSeconds(offsetSeconds)
                if (fireAt.isAfter(now) && scheduled < MAX_ALARMS) {
                    schedule(item.uuid, index, item.title, fireAt)
                    scheduled++
                }
            }
        }
        store.close()
    }

    private fun schedule(uuid: String, index: Int, title: String, fireAt: Instant) {
        val intent = Intent(context, NotifyReceiver::class.java).apply {
            putExtra("title", title)
            putExtra("key", "$uuid#$index")
        }
        val pending = PendingIntent.getBroadcast(
            context,
            (uuid.hashCode() * 31 + index),
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val triggerAt = fireAt.toEpochMilli()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && !alarmManager.canScheduleExactAlarms()) {
            // 没给"精确闹钟"权限时用不精确的, 宁可晚几分钟也别不响
            alarmManager.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, pending)
        } else {
            alarmManager.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, pending)
        }
    }

    companion object {
        private const val MAX_ALARMS = 40      // 系统对每应用闹钟数量有上限, 留余量
    }
}
