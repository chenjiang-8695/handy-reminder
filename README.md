# 智能手账式悬浮提醒助手（参考实现）

把《智能手账式悬浮提醒助手》PRD 落成**可运行、可测试**的桌面端参考实现。当前为**开源基础版**：仅保留核心代码、测试与打包脚本，不包含内部文档、演示截图、客户资料与敏感测试数据。

## 60 秒上手

```bash
python -m pip install -r requirements.txt        # 必需: tzdata, PySide6
python -m pytest tests -q                        # 16 个测试文件，见下
python -m reminder_assistant --selftest          # 端到端时间线自检
python -m reminder_assistant --serve             # 常驻: 便签悬浮窗 + Alt+Q 圈选
python -m reminder_assistant --autostart on      # 开机自启
python -m reminder_assistant --autostart status  # 查看自启状态

# 设备间同步 (阶段二: 同一局域网, 手工指定对端; 无服务器, 数据不中转)
python -m reminder_assistant --sync-listen 8767  # 本机监听 (顺带广播自己)
python -m reminder_assistant --sync-scan         # 找找局域网里谁在等同步
python -m reminder_assistant --peer auto         # 自动发现后同步一次
python -m reminder_assistant --peer 192.168.1.7:8767   # 手工指定对端
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
  resources.py   打包后资源定位（_MEIPASS）
  winconsole.py  窗口化构建下附着父控制台 + 标准输出统一 UTF-8，一个 exe 兼当 CLI
packaging/       PyInstaller spec（含瘦身过滤）/ 图标与版本信息生成器 / 构建与校验脚本
tests/           16 个自动化测试文件（见下文测试章节）
```

## 测试

```bash
python -m pytest tests -q
```

当前仓库包含 **16 个测试文件**（306 通过 / 31 跳过；UI/环境相关用例会自动跳过）：

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

## 打包

```bash
python packaging/build.py --check-only   # 结构校验（不生成产物）
```

打包前请确认以下事项：

1. **代码签名证书**。Windows 11 的 Smart App Control 处于强制状态时，未签名的 exe 可能被系统拦截（`WinError 4551`）。是否拦截取决于文件信誉与策略生效状态，换一台机器可能不同。签名是分发的前提而不是优化项。
2. **内置手写字体**。把 `LXGWWenKai-Bold.ttf` 放进 `reminder_assistant/assets/fonts/`（见该目录 `README.txt`）。不放会退到系统楷体，PRD 4.1 的「100% 清晰可辨认」无从保证。

## 说明

- 本仓库为开源基础版，仅保留可运行、可测试的代码与测试。
- `docs/` 里有：交接说明、同步方案-B版（本方案）、云端同步与数据模型设计（旧版，云端事实源）、阶段一实施计划、总体方案、技术介绍、演示脚本、答疑两篇、识别抗干扰方案、免安装版使用说明。
- 测试数据、报告截图、客户需求、测试反馈等已移出仓库，请从原工作目录获取。
