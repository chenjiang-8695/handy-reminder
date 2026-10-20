"""组装与启动。

    python -m reminder_assistant --demo "明天下午三点开会"   解析演示 (无需 GUI/OCR)
    python -m reminder_assistant --selftest                 端到端自检 (假时钟, 打印时间线)
    python -m reminder_assistant --list                     查看当前便签上的事件
    python -m reminder_assistant --serve                    常驻运行 (悬浮窗 + 热键 + 调度)
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import sys
from datetime import datetime, timedelta

from .account import AccountBook
from .clock import Clock, SystemClock
from .lifecycle import Lifecycle
from .models import LEVELS, Reminder, ReminderPlan
from .notify import ConsoleNotifier, LogNotifier, NotificationCenter, QuietHours, WindowsToastNotifier
from .scheduler import Scheduler
from .settings import Settings
from .store import Store, StoreLocked, default_data_dir
from .tasks import GROUP_MODE_LABELS
from .timeparse import parse_time
from .winconsole import configure_stdio

log = logging.getLogger("reminder.app")


def report_startup_failure(message: str) -> None:
    """把"起不来"的原因告诉用户 —— 桌面程序没有控制台时, print 等于什么都没发生。

    业务背景: 免安装目录形态下用户是双击 exe 启动的, 那时没有控制台, sys.stderr 是 None。
    只写 stderr 的话表现就是"双击了, 什么也没发生" —— 用户没法自助排查, 只能来问"为什么打不开"。
    所以: 终端里照旧写 stderr; 双击启动时弹一个 Win32 原生消息框 (不依赖 Qt, 此时 Qt 可能还没起来)。

    单独抽成函数是为了让测试能替换掉它: 模态框在无头环境里会永久阻塞。
    """
    log.error("启动失败: %s", message)
    if sys.stderr is not None:
        print(f"启动失败: {message}", file=sys.stderr)
        return
    if sys.platform != "win32":
        return
    try:
        MB_ICONERROR, MB_TOPMOST = 0x00000010, 0x00040000
        ctypes.windll.user32.MessageBoxW(None, message, "智能手账提醒 · 启动失败",
                                        MB_ICONERROR | MB_TOPMOST)
    except Exception:                       # noqa: BLE001 - 连提示都弹不出来时只能靠日志
        log.exception("弹出失败提示时又出错了")



class App:
    def __init__(self, data_dir=None, *, clock: Clock | None = None, notifier=None,
                 exclusive: bool = True, ui: bool = False):
        self.store = Store((data_dir / "reminders.db") if data_dir else None, exclusive=exclusive)
        self.clock = clock or SystemClock()
        self.notifier = notifier or LogNotifier()
        self.fired: list = []
        self.scheduler = Scheduler(self.store, self.clock, on_fire=self._on_fire)
        self.settings = Settings.load(self.store)
        self.center = NotificationCenter(self.store, self.notifier,
                                         quiet=self.settings.quiet_hours())
        self.life = Lifecycle(self.store, self.scheduler)
        # 账户与设备绑定 (PRD 2.4): 唯一入口是 AccountBook (读-改-写都过它),
        # 设置面板与同步桥共用同一个 book, 免得两边各存一份内存副本互相覆盖。
        self.account = AccountBook(self.store)
        self.scanner = None
        # 常驻运行时持有的全局热键对象 (未运行时为 None); effective_hotkey 记的是
        # **当前真正注册在系统上的**组合键 —— CLI 的 --hotkey 会覆盖设置里的值,
        # 两者不分清的话换键判断会以为"没变"而静默不生效。
        self.hotkey = None
        self.window = None         # 主窗口 (由 _run_ui 填), 供热键回调排队用
        self.effective_hotkey: str | None = None
        # 热键注册失败时记下原因: 程序照常起来 (否则用户连设置都进不去, 没法换键),
        # 由界面把原因告诉用户。
        self.hotkey_error: str | None = None
        if ui:
            self.ensure_scanner()

    def ensure_scanner(self, *, refresh: bool = False):
        """懒建圈选器 (截图 + OCR)。

        单独抽出来是因为"打开应用"有两条入口: `--serve`, 以及**双击 exe 时的无参数启动**。
        后者是在 App 构造之后才确定要进常驻模式的, 少了这一步就会得到一个"窗口在、Alt+Q 没反应"
        的半残状态 —— 而这类缺陷在自动化里几乎看不出来 (窗口是起来的)。

        refresh=True 会**重新探测 OCR 后端**。什么时候需要: 启动早期还没有 QApplication, 那时
        不能构造 Windows 自带 OCR (winrt 先于 Qt = 段错误, 实测), 探测结论只有 Stub;
        界面起来之后必须重探一次, 否则"明明能识别"会被判成"这台机器没装 OCR"。
        """
        from .ocr import StubOcrEngine

        stale = (self.scanner is not None and self.scanner.engines
                 and isinstance(self.scanner.engines[0], StubOcrEngine))
        if self.scanner is None or (refresh and stale):
            from .ocr import Scanner

            if refresh:
                from .ocr import probe_engines

                probe_engines(force=True)
            self.scanner = Scanner(clock=self.clock)
        return self.scanner

    def warm_up_scanner(self, *, refresh: bool = False) -> bool:
        """后台把 OCR 模型加载好。

        为什么需要: OCR 引擎的构造函数要加载 ONNX 模型 (几十 MB), 第一次按 Alt+Q 会卡好几秒 ——
        这时候冻结层已经收起、屏幕上什么都没有, 用户看到的就是"识别了但没反应"(客户反馈)。
        在启动时常驻加载一次, 第一次圈选就是秒回。放在守护线程里做, 失败也不影响启动。

        refresh=True: 重新探测后端 (界面起来之后才允许构造 winrt, 见 ensure_scanner 的说明)。
        """
        import threading

        def _warm() -> None:
            import time as _time

            from .ocr import preload_heavy

            t0 = _time.perf_counter()
            preload_heavy()          # 只加载重模型 (rapidocr); Windows OCR 必须等 GUI 起来
            try:
                scanner = self.ensure_scanner(refresh=refresh)
                from .ocr import StubOcrEngine

                names = [e.name for e in scanner.engines]
                if names and names[0] == StubOcrEngine.name:
                    log.info("没有 OCR 后端可预热 (圈选识别会提示安装)")
                    return
                log.info("OCR 引擎已预热: %s (%.1fs)", "+".join(names),
                         _time.perf_counter() - t0)
            except Exception:                       # noqa: BLE001 - 预热失败不该影响启动
                log.warning("OCR 预热失败, 第一次圈选会慢一些", exc_info=True)

        threading.Thread(target=_warm, name="ocr-warmup", daemon=True).start()
        return True

    def finish_ocr_setup(self) -> None:
        """QApplication 就绪后, 在**主线程**上把 OCR 后端定下来。

        为什么非这样不可 (都是实测出来的段错误, 没有 traceback):
          * winrt (Windows 自带 OCR) 在 QApplication **之前** 初始化 → 进程段错误;
          * 在预热线程里构造 winrt、同时主线程在建 Qt 控件 → 同样段错误;
          * 反过来 (先 QApplication, 再主线程构造 winrt) → 正常。
        所以顺序固定: 建 QApplication → 这里同步构造 → 再建便签窗口。
        RapidOCR 的重模型早就在后台预热好了 (preload_heavy), 这里通常只是取缓存, 不会卡。
        """
        from .ocr import probe_engines

        try:
            probe_engines(force=True, allow_winrt=True)      # Qt 已就绪, 现在可以碰 winrt 了
        except Exception:                       # noqa: BLE001 - 探测失败不该挡住启动
            log.warning("OCR 后端探测失败, 圈选识别会按'不可用'处理", exc_info=True)
        scanner = self.ensure_scanner(refresh=True)
        # 明确记一行"最终是谁在干活": 排障时先看这句 —— 没有它就只能靠猜"到底能不能圈选"。
        names = [e.name for e in scanner.engines]
        if names and names[0] == "stub":
            log.info("圈选识别不可用: 没有 OCR 后端 (会提示安装, 便签输入行不受影响)")
        else:
            log.info("圈选识别就绪, OCR 后端: %s", "+".join(names))

    # ------------------------------------------------------------ 设置
    def apply_settings(self, new: Settings) -> Settings:
        """保存并立即生效 —— 含快捷键, 不需要重启。

        快捷键热切换的手感要求: 面板里改成 ctrl+alt+r, 点保存, 立刻就能按下试试。
        实现是让热键线程在同一消息循环里注销旧键、注册新键 (hotkey.GlobalHotkey.change)。
        新组合键被别的软件占用时 change 会抛 HotkeyError, 由设置面板把原因显示出来并
        **不关窗**; 此时旧键仍然有效, 也没落盘 —— 用户改完再保存。
        """
        from .hotkey import parse_hotkey

        parse_hotkey(new.hotkey)      # 格式非法就别进库: 存进去下次启动直接起不来
        current = self.effective_hotkey or self.settings.hotkey
        if self.hotkey is not None and new.hotkey != current:
            self.hotkey.change(new.hotkey)     # 失败抛 HotkeyError, 旧键仍然有效
            self.effective_hotkey = new.hotkey
            log.info("快捷键已热切换: %s -> %s", current, new.hotkey)
        new.save(self.store)
        self.settings = new
        self.center.quiet = new.quiet_hours()
        log.info("设置已保存: 等级=%s, 免打扰=%s %s-%s, 快捷键=%s",
                 {k: v.describe() for k, v in new.levels.items()},
                 new.quiet_enabled, new.quiet_start, new.quiet_end, new.hotkey)
        return new

    # ------------------------------------------------------------ 事件
    def _on_fire(self, fired) -> None:
        self.fired.append(fired)
        self.center.deliver(fired)

    # ------------------------------------------------------------ 创建
    def create_from_text(self, text: str, *, level: str = "日常", repeat: int | None = None,
                         first_lead: timedelta | None = None,
                         interval: timedelta | None = None,
                         now: datetime | None = None) -> tuple[Reminder | None, object]:
        """OCR/输入文本 -> 解析 -> 建卡片。返回 (reminder, parse_result)。

        计划来自用户设置里的等级配方 (settings), 不是写死的常量 —— 用户在设置面板
        改过"重要 = 提前 30 分钟/间隔 10 分钟/3 次"之后, 这里必须跟着变。
        """
        now = now or self.clock.now()
        parsed = parse_time(text, now=now)
        if not parsed.when:
            return None, parsed
        if repeat and interval is not None:
            plan = ReminderPlan.from_spec(first_lead or timedelta(0), interval, repeat)
        else:
            plan = self.settings.plan(level)
        rem = Reminder(title=text.strip(), source_text=text, event_at=parsed.when,
                       tz=str(now.tzinfo), level=level, plan=plan, created_at=now)
        return self.life.create(rem, now), parsed

    # ------------------------------------------------------------ 运行
    def serve(self, *, use_toast: bool = True, hotkey_spec: str | None = None) -> None:
        from .hotkey import enable_dpi_awareness, is_fullscreen

        enable_dpi_awareness()
        self.center.fullscreen_probe = is_fullscreen if self.settings.quiet_on_fullscreen else None
        self.center.quiet = self.settings.quiet_hours()
        if use_toast:
            self.center.notifier = WindowsToastNotifier()
        self.scheduler.start()
        spec = hotkey_spec or self.settings.hotkey
        hk = self._start_hotkey(spec)
        self.hotkey = hk
        self.effective_hotkey = hk.spec if hk else None
        # 预热**不能**放在这里: 此时 QApplication 还没建, 而在这个进程里先构造 Windows 自带
        # OCR (winrt) 再建 QApplication 会**段错误**(实测, 表现为启动后悄悄退出)。
        # 改到 _run_ui 里 QApplication 建好之后再做。
        self._run_ui(hk)

    def _start_hotkey(self, spec: str):
        """注册全局热键, 失败**不致命**。

        为什么不当致命错误: 组合键被别的软件占用时, 如果程序因此拒绝启动, 用户就永远进不去
        设置面板, 也就永远改不了那个键 —— 死锁。所以照常起来, 只把原因交给界面提示。
        """
        from .hotkey import GlobalHotkey, HotkeyError

        hk = GlobalHotkey(spec, callback=self._on_scan_hotkey)
        try:
            hk.start()
        except HotkeyError as exc:
            log.error("全局快捷键不可用, 以无热键模式继续启动: %s", exc)
            self.hotkey_error = str(exc)
            hk.stop()
            return None
        self.hotkey_error = None
        return hk

    def _on_scan_hotkey(self) -> None:
        """热键线程里被调用 —— 只允许做线程安全的事: 发个信号让 GUI 线程去开冻结层。

        这里以前是直接 `SelectionOverlay(self).start()`: 在工作线程里创建 QWidget,
        Qt 不允许, 实测结果是按了 Alt+Q 毫无反应 (也没有任何报错)。
        """
        log.info("收到圈选热键")
        win = getattr(self, "window", None)
        if win is None:
            log.warning("界面还没准备好, 忽略这次圈选热键")
            return
        win.scan_requested.emit()

    def _run_ui(self, hk=None) -> None:
        from PySide6.QtWidgets import QApplication

        from .ui.notepad import NotepadWindow

        qapp = QApplication.instance() or QApplication(sys.argv)
        # 托盘常驻程序: 关掉便签窗口不等于退出, 否则调度线程会跟着被杀掉
        qapp.setQuitOnLastWindowClosed(False)
        # QApplication 已就绪 —— 现在才能安全地构造 Windows 自带 OCR (winrt)。
        # 必须在**主线程**同步做完: 预热线程里和 Qt 建窗口同时碰 winrt 同样是段错误 (实测)。
        self.finish_ocr_setup()
        win = NotepadWindow(self)
        self.window = win          # 热键回调需要它来把"圈选"排队回 GUI 线程
        win.show()
        if self.hotkey_error:
            # 热键没注册上时, 用户是唯一能修的人 —— 必须让他知道去哪儿修。
            win.warn_hotkey(self.hotkey_error)
        try:
            qapp.exec()
        finally:
            if hk:
                hk.stop()
            self.window = None
            self.hotkey = None
            self.effective_hotkey = None
            self.scheduler.stop()
            self.store.close()

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        if self.hotkey is not None:
            self.hotkey.stop()
            self.hotkey = None
        self.scheduler.stop()
        self.store.close()


# ---------------------------------------------------------------- 自检 / 演示

TIMELINE = [
    ("2026-10-08 10:00", "用户在网页上按 Alt+Q 圈选到「明天下午三点开会」", None),
    ("2026-10-08 10:00", "OCR 出文本, 解析出 2026-10-09 15:00, 生成便签卡片", None),
    ("2026-10-09 14:49", "还没到第一次提醒时间", 0),
    ("2026-10-09 14:50", "第 1/3 次提醒 (提前 10 分钟)", 1),
    ("2026-10-09 14:55", "第 2/3 次提醒", 1),
    ("2026-10-09 15:00", "第 3/3 次提醒 (事件时刻)", 1),
    ("2026-10-09 15:01", "用户点「划掉」-> 事件销毁, 进入历史", None),
]


def selftest() -> int:
    import tempfile
    from contextlib import closing
    from pathlib import Path
    from zoneinfo import ZoneInfo

    from .clock import FakeClock

    tz = ZoneInfo("Asia/Shanghai")
    start = datetime(2026, 10, 8, 10, 0, tzinfo=tz)
    clock = FakeClock(start)
    notifier = LogNotifier()
    with tempfile.TemporaryDirectory() as td, \
            closing(Store(Path(td) / "selftest.db")) as store:
        # closing(...) 保证**即使自检中途抛异常也会关库**: 不关就把 instance.lock
        # 留在临时目录里, 临时目录清理会再抛一个 PermissionError,
        # 把真正的失败原因埋在 traceback 末尾 (打包版实测就是这样丢掉原因的)。
        sched = Scheduler(store, clock, max_sleep=0.01)
        center = NotificationCenter(store, notifier, quiet=QuietHours(False))
        life = Lifecycle(store, sched)
        sched.on_fire = lambda f: center.deliver(f)

        print("=" * 74)
        print("端到端自检 (假时钟驱动, 不依赖真实等待)")
        print("=" * 74)

        text = "明天下午三点开会"
        parsed = parse_time(text, now=clock.now())
        print(f"① 圈选文本 : {text}")
        print(f"   OCR 时间 : {parsed.when:%Y-%m-%d %H:%M %Z}  置信度 {parsed.confidence}"
              f"  匹配片段「{parsed.matched}」")
        for n in parsed.notes:
            print(f"   提示     : {n}")

        rem = Reminder(title=text, source_text=text, event_at=parsed.when, created_at=clock.now(),
                       level="重要",
                       plan=ReminderPlan.custom(3, -timedelta(minutes=10), timedelta(minutes=5)))
        rem = life.create(rem, clock.now())
        occs = store.occurrences_of(rem.id)
        print(f"② 生成卡片 : #{rem.id} {rem.title}  [{rem.level}]  失效于 {rem.expire_at:%m-%d %H:%M}")
        for o in occs:
            print(f"   触发点 {o.index + 1}/{len(occs)}: {o.fire_at:%m-%d %H:%M}")

        print("③ 时间线  :")
        for stamp, what, expect in TIMELINE:
            t = datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            clock.set_wall(t)
            before = len(notifier.sent)
            sched.poll(t)
            got = len(notifier.sent) - before
            flag = "✓" if expect is None or got == expect else "✗"
            print(f"   {flag} {stamp}  {what}" + (f"  [送达 {got}]" if expect is not None else ""))

        delivered = len(notifier.sent)
        print(f"④ 通知记录 : 共 {delivered} 次; 调度器状态 {sched.stats}")
        assert delivered == 3, f"应送达 3 次, 实际 {delivered}"

        # 优先逻辑: 用户点「划掉」
        life.complete(rem.id, clock.now())
        print(f"⑤ 划掉归档 : 状态={store.get_reminder(rem.id).status}, "
              f"便签剩余={len(store.list_active())}, 历史={len(life.history())} 条")

        # 兜底逻辑: 另一条事件用户没处理, 到失效时间静默清理
        text2 = "后天上午十点体检"
        p2 = parse_time(text2, now=clock.now())
        rem2 = life.create(Reminder(title=text2, event_at=p2.when, created_at=clock.now(),
                                    level="日常", plan=ReminderPlan.preset("日常")), clock.now())
        clock.set_wall(rem2.expire_at + timedelta(minutes=1))
        swept = life.sweep(clock.now())
        print(f"⑥ 兜底清理 : {clock.now():%m-%d %H:%M} 未处理 -> 静默清理 {swept} "
              f"(状态={store.get_reminder(rem2.id).status}), 便签剩余={len(store.list_active())}")

        ok = (delivered == 3
              and store.get_reminder(rem.id).status == "completed"
              and store.get_reminder(rem2.id).status == "expired"
              and store.list_active() == [])
        print("=" * 74)
        print("自检结果:", "通过" if ok else "失败")
        store.close()
        return 0 if ok else 1


def ocr_selftest() -> int:
    """离线 OCR 自检: 现画一张写着中文的图, 让产物里的引擎真的认一遍。

    为什么要有这一步: 模型权重、onnxruntime 的 DLL、cv2 这三样任意一个没进包, 用户都是在
    按下 Alt+Q、圈完文字之后才发现"没反应"。把它变成构建期就能发现的问题。
    退出码: 0 通过 / 1 识别或解析失败 / 3 产物里本来就没有 OCR 后端 / 4 缺 Pillow / 5 找不到中文字体
    """
    from .ocr import Scanner, Selection, StubOcrEngine, default_engines, probe_engines

    probe_engines(force=True, allow_winrt=True)   # 命令行进程不建 Qt, 可以让 winrt 一起参检
    engines = [e for e in default_engines() if not isinstance(e, StubOcrEngine)]
    if not engines:
        print("OCR 自检: 本产物未内置 OCR 后端 (精简版), 圈选识别需要安装 rapidocr-onnxruntime")
        return 3
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("OCR 自检: 缺少 Pillow, 无法生成自检图")
        return 4

    font = None
    for cand in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf",
                 r"C:\Windows\Fonts\simsun.ttc"):
        try:
            font = ImageFont.truetype(cand, 40)
            break
        except OSError:
            continue
    if font is None:
        print("OCR 自检: 找不到中文字体, 无法生成自检图")
        return 5

    img = Image.new("RGB", (520, 90), "white")
    ImageDraw.Draw(img).text((20, 20), "明天下午三点开会", fill="black", font=font)
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png = buf.getvalue()

    scanner = Scanner(engines=engines, capturer=lambda _bbox: png)
    res = scanner.scan(Selection(x=0, y=0, w=520, h=90))
    print(f"OCR 后端  : {', '.join(e.name for e in engines)}")
    print(f"识别结果  : {res.text!r}")
    print(f"解析时间  : {res.parsed.when if res.parsed else None}  置信度 {res.confidence}")
    hits = sum(1 for ch in "明天下午三点开会" if ch in res.text)
    ok = hits >= 6 and res.parsed is not None and res.parsed.when is not None
    print("OCR 自检  :", "通过" if ok else f"失败 (命中 {hits}/8)")
    return 0 if ok else 1


def ocr_image(path: str) -> int:
    """`--ocr-image 图.png`: 把一张现成的图当成"圈选结果"跑一遍真实识别链路。

    复测时最省事的一条路: 截一张屏 -> 一条命令看到产品会怎么判 (建卡? 不建卡? 为什么?),
    不用打开界面、也不用真的拖鼠标圈选。
    退出码: 0 正常输出结论 / 2 找不到图 / 3 没有 OCR 后端
    """
    import io
    from pathlib import Path

    from .ocr import Scanner, Selection, StubOcrEngine, default_engines, probe_engines

    p = Path(path)
    if not p.is_file():
        print(f"找不到图片: {p}")
        return 2
    probe_engines(force=True, allow_winrt=True)      # 命令行进程不建 Qt, 可以让 winrt 一起参检
    engines = [e for e in default_engines() if not isinstance(e, StubOcrEngine)]
    if not engines:
        print("这条命令需要 OCR 后端: python -m reminder_assistant --install-ocr")
        return 3

    try:
        from PIL import Image

        buf = io.BytesIO()
        Image.open(p).convert("RGB").save(buf, format="PNG")
        png = buf.getvalue()
    except Exception as exc:                          # noqa: BLE001 - 读不了图就说清楚
        print(f"读不了这张图 ({type(exc).__name__}: {exc}); 需要 Pillow: pip install pillow")
        return 2

    res = Scanner(engines=engines, capturer=lambda _bbox: png).scan(Selection(0, 0, 1, 1))
    print(f"图片      : {p}")
    print(f"识别文本  : {res.text!r}")
    print(f"挑中的行  : {res.focus!r}")
    when = res.parsed.when if res.parsed else None
    extra = ""
    if res.parsed and res.parsed.alternatives:
        extra = "   （备选 " + "、".join(f"{a:%m-%d %H:%M}" for a in res.parsed.alternatives) + "）"
    print(f"解析时间  : {when}{extra}")
    for note in (res.parsed.notes if res.parsed else ()):
        print(f"备注      : {note}")
    if res.rejected:
        print(f"结论      : **不建卡** —— {res.rejected}")
    elif when:
        print(f"结论      : 会建卡「{res.focus or res.text}」 → {when:%m-%d %H:%M}")
    else:
        print("结论      : 会交给用户补时间（认到了文字, 但没有时间表达）")
    return 0


def demo(text: str, level: str = "日常") -> int:
    now = SystemClock().now()
    parsed = parse_time(text, now=now)
    print(f"输入      : {text}")
    print(f"当前时间  : {now:%Y-%m-%d %H:%M %Z}")
    if not parsed.when:
        print("解析结果  : 未识别到时间表达")
        for n in parsed.notes:
            print(f"备注      : {n}")
        return 2
    print(f"解析结果  : {parsed.when:%Y-%m-%d %H:%M} ({(parsed.when - now).total_seconds() / 60:.0f} 分钟后)")
    print(f"匹配片段  : 「{parsed.matched}」  类型={parsed.kind}  置信度={parsed.confidence}")
    if parsed.alternatives:
        print("其它可能  : " + ", ".join(f"{a:%m-%d %H:%M}" for a in parsed.alternatives))
    if parsed.lead:
        print(f"提前量    : {parsed.lead}")
    if parsed.interval:
        print(f"间隔      : {parsed.interval}")
    for n in parsed.notes:
        print(f"备注      : {n}")

    print(f"\n[{level}] 提醒计划:")
    for i, off in enumerate(ReminderPlan.preset(level).offsets):
        print(f"  {i + 1}. {parsed.when + off:%m-%d %H:%M}  (事件{(off.total_seconds() / 60):+.0f} 分钟)")
    return 0


def _autostart_cli(action: str, data_dir) -> int:
    """开机自启的命令行入口。开机自启不依赖数据库, 因此不碰 Store / 不抢实例锁。"""
    from .autostart import Autostart, AutostartError

    mgr = Autostart(data_dir=data_dir)
    try:
        if action == "on":
            print("已开启开机自启:\n  " + mgr.enable())
        elif action == "refresh":
            mgr.enable(refresh=True)
            print("已按当前环境重写开机自启:\n  " + mgr.current())
        elif action == "off":
            print("已关闭开机自启。" if mgr.disable() else "开机自启本来就是关闭的。")
        else:
            st = mgr.status()
            print(f"开机自启: {'已开启' if st['enabled'] else '未开启'}   (作用域: {st['scope']})")
            if st["enabled"]:
                print(f"  已登记: {st['registered']}")
                if st["stale"]:
                    print(f"  需要修复: 当前环境应为 {st['expected']}")
                    print("  运行 python -m reminder_assistant --autostart refresh 即可修复")
    except AutostartError as exc:
        print(f"操作失败: {exc}", file=sys.stderr)
        return 4
    return 0


def _print_board(app, mode: str = "time") -> None:
    """--list: 把当前便签打成一棵"主任务 + 子任务"的树 (终端里也能看出二级目录)。"""
    board = app.life.board(mode=mode)
    items = board["active"]
    tops = [it for it in items if not it.get("depth")]
    print(f"便签上的事件 ({len(tops)} 条主任务, 共 {len(items)} 行)  "
          f"当前 {board['now']:%Y-%m-%d %H:%M}  分类={GROUP_MODE_LABELS.get(mode, mode)}")
    for it in items:
        nxt = f"{it['next_fire']:%m-%d %H:%M}" if it["next_fire"] else "-"
        pre = "    └ " if it.get("depth") else "  "
        dur = f"  持续 {it['duration_min']} 分钟" if it.get("duration_min") else ""
        kids = f"  ({it['child_count']} 个子任务)" if it.get("child_count") else ""
        print(f"{pre}#{it['id']:<3} [{it['level']}] {it['title'][:26]:<28} 下次 {nxt}"
              f"  {it['fired']}/{it['total']}{dur}{kids}")
    hist = board["history"]
    if hist:
        print(f"历史 ({len(hist)} 条):")
        for r in hist:
            print(f"  #{r.id:<3} {r.title[:28]:<30} {r.status}")


def _add_subtasks_cli(app, parent_id: int | None, specs: list[str]) -> int:
    """--parent N --subtasks 名称:分钟 … : 不打开界面也能把子任务建出来。

    存在的意义有两个: 演示时不用手点; 以及自动化里可以把"主任务 + 子任务 + 两条提醒"
    整条链路跑通 (界面上点向导是人工验收的部分)。
    """
    from .tasks import DEFAULT_DURATION_MIN, SubTaskSpec

    if parent_id is None:
        print("需要 --parent <主任务id>；先用 --list 看 id", file=sys.stderr)
        return 5
    parent = app.store.get_reminder(parent_id)
    if parent is None:
        print(f"找不到主任务 #{parent_id}", file=sys.stderr)
        return 5
    parsed: list[SubTaskSpec] = []
    for raw in specs:
        name, _, dur = raw.partition(":")
        try:
            minutes = int(dur) if dur else DEFAULT_DURATION_MIN
        except ValueError:
            print(f"持续时间不是数字: {raw}", file=sys.stderr)
            return 5
        parsed.append(SubTaskSpec(title=name.strip(), duration_min=max(1, minutes)))
    try:
        created = app.life.add_children(parent, parsed, app.clock.now())
    except Exception as exc:                     # noqa: BLE001 - CLI 要把原因说清楚
        print(f"创建子任务失败: {exc}", file=sys.stderr)
        app.close()
        return 5
    for c in created:
        print(f"已建子任务 #{c.id} {c.title}  开始 {c.event_at:%m-%d %H:%M}  "
              f"持续 {c.duration_min} 分钟")
    _print_board(app)
    app.close()
    return 0


def _account_cli(app, action: str, code: str | None, name: str | None,
                 role: str | None, with_qr: bool) -> int:
    """账户与设备绑定的命令行入口 (PRD 2.4)。

    存在的意义: 演示时不用手点, 出问题时能在没有界面的机器上一条命令看到真相
    ("到底绑了几台设备" "刚才那个码还在不在"); 打包产物也能用它证明这段代码真的进了包。
    """
    from . import account as acc

    book = app.account
    now = app.clock.now()
    if action == "status":
        info = acc.describe(book.reload(), now)
        print(f"账户识别码 : {info['account_id']}")
        print(f"数据目录   : {app.store.path.parent}")
        print(f"已绑定设备 : {len(info['devices'])} 台")
        for d in info["devices"]:
            print(f"  - {d['name']} ({d['platform']})  绑定于 {d['bound_at'][:16]}  #{d['id'][:6]}")
        if info["pending"]:
            for p in info["pending"]:
                print(f"待用绑定码 : {p['code']}  ({acc.ROLE_LABELS.get(p['role'], p['role'])}, "
                      f"还剩 {p['ttl_s'] // 60} 分 {p['ttl_s'] % 60} 秒)")
        else:
            print("待用绑定码 : 无（用 --account code 生成一个）")
        if info["locked_s"]:
            print(f"注意       : 连续输错被锁定, 还需 {info['locked_s'] // 60 + 1} 分钟")
        return 0

    if action == "code":
        try:
            acct, item = book.issue_code(now, role or acc.ROLE_DESKTOP)
        except acc.AccountError as exc:
            print(f"生成失败: {exc}", file=sys.stderr)
            return 4
        payload = acc.qr_payload(acct, item)
        print(f"绑定码     : {acc.format_code(item.code)}   (5 分钟内有效, 只能用一次)")
        print(f"二维码内容 : {payload}")
        if with_qr:
            from .qr import encode

            try:
                print(encode(payload, "M").ascii_art())
            except ValueError as exc:
                print(f"(二维码生成失败: {exc})", file=sys.stderr)
        print("手机端: 在手机端页面输入或扫这个码即可完成绑定。")
        return 0

    if action == "bind":
        if not code:
            print("需要把手机端显示的绑定码跟在 --account bind 后面", file=sys.stderr)
            return 5
        try:
            acct, dev = book.bind(code, now, name=name or "手机端(命令行绑定)",
                                  platform="手机", role=role)
        except acc.AccountError as exc:
            print(f"绑定失败: {exc}", file=sys.stderr)
            return 4
        print(f"绑定成功   : {dev.name} ({dev.platform})  #{dev.id[:6]}")
        print(f"设备令牌   : {dev.short_token}…（已发放；令牌不打印全量, 也不写日志）")
        print(f"识别码     : {acct.account_id}")
        return 0

    if action == "revoke":
        if not code:
            print("需要给 --account revoke 一个设备 id 前缀（先看 --account status）", file=sys.stderr)
            return 5
        acct, dev = book.revoke(code)
        if dev is None:
            print(f"没找到设备 {code}", file=sys.stderr)
            return 5
        print(f"已撤销     : {dev.name} ({dev.platform})，它的设备令牌同时失效")
        return 0

    print(f"不认识的账户操作: {action}", file=sys.stderr)
    return 5


def ocr_check() -> int:
    """`--ocr-check`: 这台机器上圈选识别到底行不行、不行是为什么。

    存在的理由: 客户反馈里最含糊的一类就是"OCR 没实现" —— 到底是没装包、包装了但加载失败、
    还是 Windows 缺中文 OCR 语言包? 一条命令打印清楚, 不用靠猜。
    退出码: 0 至少有一个可用后端 / 3 一个都没有。
    """
    from .ocr import diagnosis, probe_engines

    engines, notes = probe_engines(force=True, allow_winrt=True)   # 命令行: 不会建 Qt, 可以碰 winrt
    print(f"智能手账式悬浮提醒助手 {__import__('reminder_assistant').__version__}")
    print(f"当前解释器: {sys.executable}")
    print(diagnosis())
    print(f"\n结论: {'圈选识别可用' if engines[0].name != 'stub' else '圈选识别不可用'} "
          f"（引擎: {', '.join(e.name for e in engines)}）")
    if engines[0].name == "stub":
        print("修法: 在**这个解释器**下跑一句 ——")
        print(f'  "{sys.executable}" -m reminder_assistant --install-ocr')
        print("说明: 便签的「＋ / 输入行」不受影响, 照常可以加提醒。")
        return 3
    return 0


def install_ocr() -> int:
    """`--install-ocr`: 把圈选识别要用的依赖装进**当前解释器**。

    为什么要有这条命令: 客户反馈"按快捷键提示本机没装 OCR" —— 提示是对的, 但用户没路可走。
    装什么: mss (截屏) + Windows 自带 OCR 的投影包 (体积小、不需要模型);
    默认不装 rapidocr (100MB+), 想要更准的印刷体识别再手动装, 命令会打印出来。
    """
    import subprocess

    if sys.platform != "win32":
        print("这条命令目前只处理 Windows 环境; 其它平台请 pip install mss")
    pkgs = ["mss"]
    if sys.version_info >= (3, 13):                 # winsdk 在 3.13+ 编不出 wheel, 用新式投影包
        pkgs += ["winrt-runtime", "winrt-Windows.Media.Ocr", "winrt-Windows.Graphics.Imaging",
                 "winrt-Windows.Storage.Streams", "winrt-Windows.Globalization",
                 "winrt-Windows.Foundation", "winrt-Windows.Foundation.Collections"]
    else:
        pkgs += ["winsdk"]
    print(f"解释器: {sys.executable}")
    print("正在安装:", " ".join(pkgs))
    r = subprocess.run([sys.executable, "-m", "pip", "install", *pkgs])
    if r.returncode != 0:
        print("安装失败 (多半是网络)。可以手动执行:")
        print(f'  "{sys.executable}" -m pip install ' + " ".join(pkgs))
        return 4
    print("\n想要更准的印刷体识别 (可选, 约 100MB):")
    print(f'  "{sys.executable}" -m pip install rapidocr-onnxruntime')
    print()
    return ocr_check()


def _setup_logging(verbose: bool, data_dir) -> None:
    """控制台 + 落盘日志。

    打包成窗口化程序后没有控制台, 出问题只能靠日志文件 —— 用户说"开机没启动"
    的时候, 有没有这个文件就是能不能定位的分界线。日志只记路径与状态, 不记提醒内容。
    """
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())

    log_dir = (data_dir or default_data_dir()) / "logs"
    try:
        from logging.handlers import RotatingFileHandler

        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(log_dir / "app.log", maxBytes=1 << 20,
                                            backupCount=3, encoding="utf-8"))
    except OSError:
        pass          # 目录不可写 (只读介质/权限) 时不能因此起不来
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers or [logging.NullHandler()], force=True)


def main(argv: list[str] | None = None) -> int:
    # 必须在任何 print 之前: 中文控制台的 GBK 编码编不出 "✓",
    # 会以 UnicodeEncodeError 的形式把自检/演示打断 (打包版实测踩到)。
    configure_stdio()

    p = argparse.ArgumentParser(prog="reminder_assistant", description="智能手账式悬浮提醒助手")
    p.add_argument("--demo", metavar="TEXT", help="解析一段文本并打印提醒计划")
    p.add_argument("--level", default="日常", choices=list(LEVELS), help="事件等级")
    p.add_argument("--selftest", action="store_true", help="跑端到端自检")
    p.add_argument("--ocr-selftest", action="store_true",
                   help="跑离线 OCR 自检 (验证模型/DLL 真的进了产物)")
    p.add_argument("--ocr-check", action="store_true",
                   help="体检 OCR 后端: 哪个能用、不能用的具体原因、缺哪个包 (排障用)")
    p.add_argument("--install-ocr", action="store_true",
                   help="把圈选识别要用的依赖装进当前解释器 (mss + Windows 自带 OCR)")
    p.add_argument("--ocr-image", metavar="图片", default=None,
                   help="拿一张现成的图当\"圈选结果\"跑一遍识别, 看会不会建卡 (复测用)")
    p.add_argument("--list", action="store_true", help="列出当前便签上的事件 (含二级子任务)")
    p.add_argument("--subtasks", nargs="+", metavar="名称:分钟",
                   help="给 --parent 指定的主任务批量建子任务, 例: --parent 1 --subtasks 整理需求:30 写文档:45")
    p.add_argument("--parent", type=int, help="主任务 id (配合 --subtasks)")

    p.add_argument("--serve", action="store_true", help="常驻运行 (悬浮窗 + 全局热键)")
    p.add_argument("--headless", action="store_true", help="常驻但不显示悬浮窗")
    p.add_argument("--autostart", choices=["on", "off", "status", "refresh"],
                   help="开机自启: on 开启 / off 关闭 / status 查看 / refresh 修复失效路径")
    p.add_argument("--account", choices=["status", "code", "bind", "revoke"],
                   help="账户与设备绑定: status 查看识别码/已绑定设备 / code 生成绑定码 / "
                        "bind <码> 绑定 / revoke <设备id前缀> 撤销")
    p.add_argument("--account-value", metavar="码或设备id", default=None,
                   help="配合 --account bind / revoke 使用")
    p.add_argument("--device-name", default=None, help="绑定后的设备名, 默认「手机端(命令行绑定)」")
    p.add_argument("--account-role", choices=["desktop", "phone"], default=None,
                   help="限定绑定码的方向: desktop(手机端扫) / phone(手机端显示给电脑端)。"
                        "生成码时默认 desktop; 绑定时留空表示两种方向都接受")
    p.add_argument("--account-qr", action="store_true",
                   help="配合 --account code: 顺便在终端里打印二维码 (纯 ASCII)")
    p.add_argument("--data-dir", default=None, help=f"数据目录, 默认 {default_data_dir()}")
    p.add_argument("--demo-sync", action="store_true",
                   help="展示版: 开一个局域网同步桥, 手机浏览器打开网址即可当手机端 (演示用回环)")
    p.add_argument("--sync-port", type=int, default=8765, help="同步桥 WebSocket 端口")
    p.add_argument("--page-port", type=int, default=8766, help="手机端网页端口")
    p.add_argument("--no-toast", action="store_true", help="不用系统通知, 只在控制台输出")
    p.add_argument("--hotkey", default=None, help="覆盖设置里的全局快捷键, 如 alt+q")
    p.add_argument("--version", action="store_true", help="打印版本并退出")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    from pathlib import Path as _P

    data_dir = _P(args.data_dir) if args.data_dir else None

    if args.version:
        from . import __version__
        from .resources import is_frozen

        print(f"智能手账式悬浮提醒助手 {__version__}"
              f"{' (打包版)' if is_frozen() else ' (源码运行)'}")
        print(f"数据目录: {data_dir or default_data_dir()}")
        return 0

    if getattr(sys, "frozen", False):
        # 窗口化构建: 从终端调用时附着父控制台, 否则 print 会静默丢失
        from .winconsole import attach_parent_console

        attach_parent_console()

    _setup_logging(args.verbose, data_dir)

    if args.selftest:
        return selftest()
    if args.ocr_check:
        return ocr_check()
    if args.install_ocr:
        return install_ocr()
    if args.ocr_selftest:
        return ocr_selftest()
    if args.ocr_image:
        return ocr_image(args.ocr_image)
    if args.demo:
        return demo(args.demo, args.level)

    if args.autostart:
        return _autostart_cli(args.autostart, data_dir)

    try:
        app = App(data_dir, ui=args.serve,
                  notifier=ConsoleNotifier() if args.no_toast else WindowsToastNotifier())
    except StoreLocked as exc:
        report_startup_failure(str(exc))
        return 3

    if args.list:
        _print_board(app)
        app.close()
        return 0

    if args.account:
        try:
            return _account_cli(app, args.account, args.account_value, args.device_name,
                                args.account_role, args.account_qr)
        finally:
            app.close()

    if args.subtasks:
        return _add_subtasks_cli(app, args.parent, args.subtasks)

    # 没给任何子命令 = "把程序打开" —— 这是用户双击 exe 时的实际路径。
    # 这里原本是 print_help(): 而双击启动的窗口化程序没有控制台, 帮助被打印到虚无,
    # 用户看到的就是"双击完全没反应"。要看帮助请显式用 --help。
    if not (args.serve or args.headless):
        args.serve = True
        app.ensure_scanner()          # 双击启动也要能圈选, 否则是"窗口在、Alt+Q 没反应"

    if args.demo_sync:                       # 展示版: 同步桥和便签窗口一起跑
        from PySide6.QtWidgets import QApplication

        from .sync_demo import start_demo_sync

        # 必须先有 QApplication: 之后再建的 QTimer/QWebSocketServer 才会挂到它的
        # 事件分发器上。顺序反了的症状是"端口在听、TCP 也连得上、但握手永远不回",
        # 在无窗口模式下尤其难查 (窗口模式里 App.serve() 已经把 QApplication 建好了)。
        _qapp = QApplication.instance() or QApplication([])
        app.demo_sync = start_demo_sync(app, ws_port=args.sync_port, page_port=args.page_port)

    try:
        if args.serve:
            app.serve(use_toast=not args.no_toast, hotkey_spec=args.hotkey)
        else:
            app.scheduler.start()
            print("常驻中 (Ctrl+C 退出)。便签快捷键:", args.hotkey or app.settings.hotkey)
            if args.demo_sync:
                # 同步桥靠 Qt 事件循环收发消息; 无窗口模式下必须自己把它跑起来,
                # 否则会"端口听着、消息永远不来"(这里以前是 sleep 死循环)。
                from PySide6.QtWidgets import QApplication

                _qapp = QApplication.instance() or QApplication([])
                _qapp.exec()
            else:
                import time as _t

                while True:
                    _t.sleep(3600)
    except KeyboardInterrupt:
        pass
    except Exception as exc:                 # noqa: BLE001 - 双击时用户看不到 traceback
        report_startup_failure(f"{type(exc).__name__}: {exc}")
        return 4
    finally:
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
