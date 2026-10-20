# 智能手账式悬浮提醒助手（参考实现）

把《智能手账式悬浮提醒助手》PRD V2.1 落成**可运行、可测试**的桌面端参考实现。
完整技术方案见 [`docs/总体方案.md`](docs/总体方案.md)。

## 60 秒上手

```bash
python -m pip install -r requirements.txt        # 必需: tzdata, PySide6
python -m pytest tests -q                        # 214 passed
python -m reminder_assistant --selftest          # 端到端时间线自检
python -m reminder_assistant --serve             # 常驻: 便签悬浮窗 + Alt+Q 圈选
python -m reminder_assistant --autostart on      # 开机自启 (托盘菜单里也能勾)
python -m reminder_assistant --autostart status  # 查看, 含"指向已失效"的提醒
python packaging/build.py --gui-check             # 打包 (66MB / zip 29MB) + 结构/执行/GUI 三层校验
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
  store.py       SQLite WAL 持久化（崩溃恢复 / 多实例锁 / 通知去重）
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
  resources.py   打包后资源定位（_MEIPASS）
  winconsole.py  窗口化构建下附着父控制台 + 标准输出统一 UTF-8，一个 exe 兼当 CLI
packaging/       PyInstaller spec（含瘦身过滤）/ 图标与版本信息生成器 / 构建与校验脚本
                 smoke_gui.py 打包产物 GUI 冒烟 / Inno Setup 安装脚本
tests/           214 项自动化用例（含 Qt 无头冒烟、Win32 热键真实注册与热切换），语句覆盖率 83%
```

## ⚠ 交付前必须做的两件事

1. **代码签名证书**。本机实测：Windows 11 的 Smart App Control 处于强制状态时，未签名的 exe
   会被系统直接拒绝执行（`WinError 4551 应用程序控制策略已阻止此文件`）；但同一台机器上后来又
   实测到产物能正常跑 —— 说明这取决于文件信誉与策略的实际生效状态，**换一台机器就可能被拦**。
   PyInstaller 产物又是安全软件误报重灾区，所以签名是分发的前提而不是优化项。
2. **内置手写字体**。把 `LXGWWenKai-Bold.ttf` 放进 `reminder_assistant/assets/fonts/`
   （该目录有说明）。不放会退到系统楷体，PRD 4.1 的"100% 清晰可辨认"无从保证。

打包校验分两层，`python packaging/build.py`（或 `--check-only`）一次跑完：
结构 10 项（tzdata / Qt 平台插件 / 版本资源 / 资源目录…）+ **执行 5 项**（`--version` / `--demo` /
无时间表达退出码 / `--autostart status` / `--selftest`，真的把 exe 跑起来）。本机实测两层全过；
若目标机被应用控制策略拦住，脚本会明确说明并跳过执行层，而不是抛 traceback。

## 与工程师需求清单的对应

| 工程师考点 | 落地位置 | 验证 |
|---|---|---|
| 自然语言（相对/绝对/重复/时区夏令时） | `timeparse.py` | `test_timeparse.py` 47 项 |
| 休眠、待机、时间被修改后补发/纠正 | `scheduler._detect_clock_events`、`_fire` | `test_scheduler.py` 休眠/改时间用例 |
| 定时器漂移、重复触发、漏触发 | 绝对时刻入堆 + `fired_log(rid#idx@gen)` | 漂移/重放去重用例 |
| 延迟提醒更新原任务而非新建 | `store.reschedule_occurrence` | `test_snooze_*` |
| 置顶/穿透/多屏/DPI/全屏勿扰 | `ui/notepad.py`、`hotkey.py` | 坐标换算用例 + 热键真实注册/热切换用例 + 人工验收 |
| 关闭/托盘/开机自启 | `ui/notepad.py`、`autostart.py` | 关闭收托盘用例 + 注册表真实往返回例 |
| 等级/提醒频率/免打扰可自定义 | `settings.py`、`ui/settings_dialog.py` | `test_settings_ui.py` 34 项（含"预览==实际调度"、换键失败回滚） |
| 打扰控制（动画/声音/时段） | `notify.QuietHours` + 摘要合并 | `test_quiet_hours_*` |
| 性能与资源（轮询/重绘/泄漏） | 事件驱动 + 纸张缓存 + 行控件差量 | `test_refresh_is_diff_based`、2000 条规模用例 |
| 持久化/崩溃恢复/多实例/去重 | `store.py` | `test_store.py` 16 项 |
| 安全与隐私（本地存储、最小权限、注入） | 全本地 SQLite、无网络、通知文本转义 | 代码评审 |

## 交付前必做

1. 把 `LXGWWenKai-Bold.ttf` 放进 `reminder_assistant/assets/fonts/`（见该目录 README）。
2. 按 `docs/总体方案.md` 第 3.4 节的"未验证清单"在真实桌面/多屏机器上人工验收。
