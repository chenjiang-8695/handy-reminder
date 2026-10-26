# 智能手账 · 安卓端 (0.1.0)

桌面端（`../reminder_assistant`）的安卓伴生端：**同一个局域网里找到电脑、同步提醒、到点弹本地通知**。
没有服务器、没有推送服务、没有账号；数据只在你的电脑和手机之间走。

## 现在能做什么

- 局域网发现：UDP 8768 广播，和桌面端同一套信标格式（`HandyReminder/1`）
- 同步：TCP + 与桌面端完全相同的帧（`hello / pull / push / ack / bye`，4 字节长度前缀 + JSON）
- 数据模型：SQLite 表结构与桌面端 `store.py` 同构（`reminders / outbox / applied_ops / meta`），
  冲突规则一致（版本大者胜、版本相同比设备号、删除无条件优先）
- 提醒：`AlarmManager` 精确闹钟 + 通知渠道，**不依赖任何推送服务**，App 被杀也照常响
- 界面：一张清单 + 「局域网同步」按钮 + 一行状态（刻意做成最小壳，先保证能同步、能响）

## 还不能做什么（下一版）

- 手机上新建/编辑完整的提醒 UI（数据层已经支持 `addReminder` / `complete`，还没接到界面）
- 便签手写观感（桌面端那套纸张质感还没搬过来）
- 跨网络打洞（桌面端有，安卓端先只做同 WiFi）
- 后台常驻保活优化（现在靠系统闹钟，不靠前台服务）

## 怎么编

这台机器上已经装好了工具链（一次性）：

| 组件 | 位置 |
|---|---|
| JDK 21 | `C:\Program Files\JetBrains\IntelliJ IDEA 2024.3.3\jbr` |
| Android SDK | `C:\Users\26756\AndroidSdk`（platform-tools / platforms;android-34 / build-tools;34.0.0） |
| Gradle 8.7 | `C:\Users\26756\opt\gradle-8.7`（官方站点被重置，走腾讯镜像装的） |

```bash
cd android
export JAVA_HOME="C:/Program Files/JetBrains/IntelliJ IDEA 2024.3.3/jbr"
export ANDROID_HOME="C:/Users/26756/AndroidSdk"
"C:/Users/26756/opt/gradle-8.7/bin/gradle" --no-daemon :app:assembleDebug
# 产物: app/build/outputs/apk/debug/app-debug.apk  (约 3.2 MB)

# 协议层单元测试（纯 JVM，不用模拟器）
"C:/Users/26756/opt/gradle-8.7/bin/gradle" --no-daemon :app:testDebugUnitTest
```

装到手机（手机开 USB 调试，数据线连电脑）：

```bash
"C:/Users/26756/AndroidSdk/platform-tools/adb.exe" install -r app/build/outputs/apk/debug/app-debug.apk
```

## 怎么用

1. 电脑端起监听：`HandyReminder.exe --sync-listen 8767`（会顺带广播自己）
2. 手机和电脑连**同一个 WiFi**
3. 手机打开 App → 点「局域网同步」→ 电脑上的提醒就下来了；手机上加的（数据层已支持）也会推上去
4. 到点由系统闹钟弹通知，不需要 App 常驻

失败时的表现：状态行会写「没找到电脑」或「同步失败：…（改动仍在本地，待同步 N 条）」，
改动一条都不会丢，等两台设备在同一 WiFi 再点一次即可。

## 和桌面端的协议契约（改之前先看）

`app/src/main/java/app/handyreminder/SyncProtocol.kt` 里每个字段都能在桌面端找到对应的读取方：

| 帧/字段 | 桌面端读取位置 |
|---|---|
| `hello{device_id,name,protocol}` | `sync_link.serve_exchange` |
| `pull{cursor}` / `push{ops,last_id}` | `serve_exchange` 的 pull / push 分支 |
| `ops[].payload` **必须是字符串** | `store.apply_remote_op` 里 `json.loads(payload)` |
| `ops[].origin` | 冲突打平 + 转发时判断"别把对方自己的操作回显给它" |
| `op_type` 取值 | `create/update/complete/delete/replan/expire` |

桌面端有一条专门的用例钉这个契约：`tests/test_android_protocol.py`（用 Python 扮演安卓客户端，
打给真的桌面端服务端）。改字段名必须先改它。
