# 智能手账式悬浮提醒助手（参考实现）

[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey)](#)
[![android](https://img.shields.io/badge/android-8.0%2B%20(API%2026)-3ddc84)](#)
[![tests](https://img.shields.io/badge/tests-342%20passed%20%2F%2031%20skipped-brightgreen)](#测试)
[![sync](https://img.shields.io/badge/sync-LAN%20%2B%20P2P%20punch-orange)](#跨端同步)

把《智能手账式悬浮提醒助手》PRD 落成**可运行、可测试**的桌面端参考实现。当前为**开源基础版**：仅保留核心代码、测试与打包脚本，不包含内部文档、演示截图、客户资料与敏感测试数据。

## 60 秒上手

```bash
python -m pip install -r requirements.txt        # 必需: tzdata, PySide6
python -m pytest tests -q                        # 19 个测试文件，见下
python -m reminder_assistant --selftest          # 端到端时间线自检
python -m reminder_assistant --serve             # 常驻: 便签悬浮窗 + Alt+Q 圈选
python -m reminder_assistant --autostart on      # 开机自启
python -m reminder_assistant --autostart status  # 查看自启状态

# 设备间同步 (阶段二: 同一局域网, 手工指定对端; 无服务器, 数据不中转)
python -m reminder_assistant --sync-listen 8767  # 本机监听 (顺带广播自己)
python -m reminder_assistant --sync-scan         # 找找局域网里谁在等同步
python -m reminder_assistant --peer auto         # 自动发现后同步一次
python -m reminder_assistant --peer 192.168.1.7:8767   # 手工指定对端

# 跨网络打洞 (尽力而为: 家用宽带多半能通, 手机 4G/5G 常打不通)
python -m reminder_assistant --punch-host 9011         # 打印票据, 等对方来同步
python -m reminder_assistant --punch-join "HR1|ip|port|设备号|名字"   # 贴对方票据, 同步一次
```

**便签怎么用**：纸头右上角四个小按钮 —— ＋ 添加（在下面的输入行写一句
「明天下午三点开会」回车就记上）、⚙ 设置、－ 贴边收起、× 收进托盘；拖到屏幕边缘会自动
缩成一条，点一下展开；右下角可拖动调整大小。

**改提醒频率/免打扰**：程序里从「托盘 → 设置…」改等级配方与免打扰时段；
从便签上某条卡片的「高级」按钮改这一条的频率。CLI 没有对应开关（这是交互功能，
用命令行改单条提醒的频率没有使用场景）。


## 目录

```
reminder_assistant/
  timeparse.py   中文自然语言时间解析（零依赖、歧义显式化、时区安全）
  scheduler.py   最小堆调度器（休眠补发 / 改时间纠正 / 幂等去重 / 改期不新建）
  store.py       SQLite WAL 持久化（崩溃恢复 / 多实例锁 / 通知去重 / 待发箱 outbox 与墓碑）
  lifecycle.py   完成 → 立即销毁；过期 → 静默清理；历史归档
  notify.py      通知投递（分级 / 免打扰合并 / 三级降级链）
  ocr.py         圈选截图 + 多引擎 OCR 融合 + 准确率量化
  hotkey.py      全局热键（Win32 RegisterHotKey，支持不重启换键）+ DPI 感知 + 全屏勿扰探测
  ui/notepad.py  便签悬浮窗（纸张质感 / 差量刷新 / 翻页 + 掉帧自动降级）
  ui/overlay.py  圈选冻结层（多屏逐屏 DPI 换算）
  ui/theme.py    手写字体加载 + 纸张底图缓存
  app.py         组装 / CLI / 自检
  autostart.py   开机自启（注册表 Run 键 + 写完读回校验）
  settings.py    用户设置（等级配方/自定义/免打扰/热键，落 meta 表）
  ui/settings_dialog.py  设置面板 + 单条提醒的「高级事件」频率面板（含实时预览、组合键格式实时校验）
  sync_link.py   设备间同步线缆层（TCP + 长度前缀帧; 先拉后推; 冲突判定在 store）
  sync_discovery.py  局域网发现（UDP 广播信标; 探测包即时回包; 15 秒无心跳算离开）
  sync_punch.py      跨网络打洞（最小 STUN 客户端; 明文票据; UDP 上的序号/确认/重发）
  resources.py   打包后资源定位（_MEIPASS）
  winconsole.py  窗口化构建下附着父控制台 + 标准输出统一 UTF-8，一个 exe 兼当 CLI
packaging/       PyInstaller spec（含瘦身过滤）/ 图标与版本信息生成器 / 构建与校验脚本
tests/           19 个自动化测试文件（见下文测试章节）
```

## 测试

```bash
python -m pytest tests -q
```

当前仓库包含 **19 个测试文件**（332 通过 / 31 跳过；UI/环境相关用例会自动跳过）：

| 测试文件 | 说明 |
|---|---|
| `test_timeparse.py` | 时间解析 |
| `test_scheduler.py` | 调度 |
| `test_store.py` | 存储 |
| `test_subtasks.py` | 子任务 |
| `test_sync_demo.py` | 同步演示 |
| `test_autostart.py` | 开机自启 |
| `test_qr.py` | 二维码 |
| `test_settings_ui.py` | 设置面板 |
| `test_account.py` | 账户 |
| `test_integration.py` | 集成测试 |
| `test_ui_smoke.py` | UI 冒烟 |
| `test_migration.py` | 数据迁移 |
| `test_outbox.py` | 跨端待发箱（每次操作恰一条 / 墓碑 / 重启不丢） |
| `test_sync_link.py` | 设备间同步（真 TCP 双实例 / 冲突 / 删除优先） |
| `test_discovery.py` | 局域网发现（UDP 信标 / 探测包 / 发现后直接同步） |
| `test_relay.py` | 三端转发（A→B→C / origin 不丢 / 多轮不重复） |
| `test_sync_badge.py` | 便签上的「待同步 N 条」提示（单机不显示 / 确认后消失） |
| `test_punch.py` | 打洞（STUN 解析 / 票据 / 回环打洞 / 打洞后同步 / 失败不丢数据） |
| `test_android_protocol.py` | 安卓客户端帧格式 × 桌面端服务端（跨语言契约） |

## 安卓端

`android/` 是一个独立的 Gradle 工程（Kotlin，minSdk 26，targetSdk 34）：局域网发现 → 同步 → 新建/划掉 → 到点本地通知。
交付前的人工验收步骤见 `docs/人工验收清单.md`。
编译、装机、与桌面端的协议契约见 `android/README.md`。产物 `android/app/build/outputs/apk/debug/app-debug.apk`。

## 打包

```bash
python packaging/build.py --check-only   # 结构校验（不生成产物）
```

打包前请确认以下事项：

1. **代码签名证书**。Windows 11 的 Smart App Control 处于强制状态时，未签名的 exe 可能被系统拦截（`WinError 4551`）。是否拦截取决于文件信誉与策略生效状态，换一台机器可能不同。签名是分发的前提而不是优化项。
2. ~~**内置手写字体**~~ 已内置：`reminder_assistant/assets/fonts/LXGWWenKai-Medium.ttf`
   （霞鹜文楷 v1.530，SIL OFL 1.1，20,992 基本区汉字，对常用字库 100% 覆盖；许可全文见同目录
   `OFL-LXGWWenKai.txt`）。上游 v1.530 没有 Bold，用 Medium + Qt 合成加粗。
   验证过程见 `docs/外部输入-vetting报告.md`。

## 说明

- 本仓库为开源基础版，仅保留可运行、可测试的代码与测试。
- 内部文档（交接说明、同步方案、阶段计划、答疑等）与客户资料保留在本地，**不入库**；仓库里只有可运行、可测试的代码。
- 测试数据、报告截图、客户需求、测试反馈等已移出仓库，请从原工作目录获取。

## 许可

代码以 [MIT](LICENSE) 许可发布。内置字体 **霞鹜文楷 LXGW WenKai** 使用 SIL Open Font License 1.1，
许可全文见 `reminder_assistant/assets/fonts/OFL-LXGWWenKai.txt`。
