package app.handyreminder

import android.content.Context
import android.net.wifi.WifiManager
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.SocketTimeoutException

/**
 * 局域网发现, 与桌面端 sync_discovery.py 同一个约定:
 * UDP 8768 广播信标 {"magic":"HandyReminder/1","protocol":1,"device_id","name","sync_port"}。
 *
 * 手机端只做"找", 不做"被找" —— 安卓上长期占着 8768 收广播要保活, 收益不值当;
 * 找到电脑就主动连过去, 连接很快, 需要的权限也最少。
 */
class Discovery(private val context: Context) {

    data class Peer(val deviceId: String, val name: String, val host: String, val syncPort: Int)

    companion object {
        const val PORT = 8768
        const val MAGIC = "HandyReminder/1"
    }

    fun scan(seconds: Int = 4): List<Peer> {
        val found = LinkedHashMap<String, Peer>()
        val lock = context.getSystemService(Context.WIFI_SERVICE)
            ?.let { (it as WifiManager).createMulticastLock("handyreminder") }
        lock?.acquire()
        DatagramSocket().use { socket ->
            socket.broadcast = true
            socket.soTimeout = 400
            val probe = JSONObject()
                .put("magic", MAGIC)
                .put("protocol", 1)
                .put("device_id", "")          // 空设备号 = 只问不答的探测包, 桌面端会忽略
                .put("name", "phone-scan")
                .put("sync_port", 0)
                .toString().toByteArray()
            val target = InetAddress.getByName("255.255.255.255")
            val end = System.currentTimeMillis() + seconds * 1000L
            while (System.currentTimeMillis() < end) {
                runCatching { socket.send(DatagramPacket(probe, probe.size, target, PORT)) }
                val buffer = ByteArray(2048)
                val packet = DatagramPacket(buffer, buffer.size)
                try {
                    socket.receive(packet)
                    val text = String(packet.data, 0, packet.length)
                    val obj = runCatching { JSONObject(text) }.getOrNull() ?: continue
                    if (obj.optString("magic") != MAGIC) continue
                    val port = obj.optInt("sync_port", 0)
                    val id = obj.optString("device_id")
                    if (port <= 0 || id.isEmpty()) continue
                    found[id] = Peer(id, obj.optString("name", "?"), packet.address.hostAddress ?: "", port)
                } catch (_: SocketTimeoutException) {
                    // 继续广播, 直到时间用完
                }
            }
        }
        lock?.release()
        return found.values.toList()
    }
}
