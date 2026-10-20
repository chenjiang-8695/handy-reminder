"""圈划识别冻结层 (PRD 2.1 唤醒机制)。

交互: 按 Alt+Q -> 全屏截图作为背景并压暗 (屏幕"冻结") -> 鼠标拖出选区
      -> 松手即对该区域做 OCR + 时间语义抽取 -> 弹结果卡 (可改时间/等级/频率)
      -> Esc 取消, 右键取消

工程要点:
  * 冻结 = 先抓一张全屏图铺满, 而不是真去锁住其他窗口 —— 不干扰任何应用。
  * **冻图必须走与 OCR 同一条截图链路** (`ocr.grab_screen_png`, mss 优先)。
    这里原来用 `QScreen.grabWindow(0)`: 在 Qt6/Windows 上它经常返回**空图**,
    而空图 + 压暗填充 = 用户看到一整块**黑色窗口**(客户反馈原话:"ocr识别单独跳出的
    黑色窗口不美观")。抓不到就**不压暗**, 让用户看见自己正常的屏幕, 只剩选框。
  * 坐标: 选区在逻辑坐标下量出来, 乘以 devicePixelRatio 换成物理像素再截图,
    否则 150% 缩放的屏幕上圈 A 会截到 B (多屏 DPI 不一致时尤其明显)。
  * 多屏: 每个 QScreen 一个覆盖层, 一次 Alt+Q 全部铺满。
  * 识别完之后**一定要有反馈**: 结论由 `apply_result()` 算出来, 通过 `picked` 交给便签窗口
    去提示/刷新/预填 —— 以前这里静默改库, 用户看到的就是"识别了但什么都没发生"。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

log = logging.getLogger("reminder.overlay")

DIM = 100          # 冻结压暗程度 (客户反馈"黑窗口不美观", 从 118 调轻一档, 选区和字仍看得清)
MIN_SIZE = 6       # 小于这个像素的拖动当误触


@dataclass
class ScanOutcome:
    """一次圈选的结论 —— 它是界面反馈的唯一依据。

    以前识别完是"直接改库、什么都不说": 建好了用户不知道, 没建成功用户也不知道。
    把结论显式化之后, 便签窗口才能对每一种情况都给一句话。
    """

    action: str                    # created | deleted | manual | noise | failed | cancelled
    text: str = ""
    title: str = ""
    when: str = ""
    reminder_id: int | None = None
    count: int = 0
    reason: str = ""
    engine: str = ""

    @property
    def ok(self) -> bool:
        return self.action in ("created", "deleted")


def _screen_physical_bbox(screen) -> tuple[int, int, int, int]:
    """屏幕的逻辑几何 -> 物理像素 bbox (截图用; 多屏各自换算)。"""
    geo, dpr = screen.geometry(), float(screen.devicePixelRatio() or 1.0)
    x0 = round(geo.x() * dpr)
    y0 = round(geo.y() * dpr)
    x1 = round((geo.x() + geo.width()) * dpr)
    y1 = round((geo.y() + geo.height()) * dpr)
    return (x0, y0, max(x0 + 1, x1), max(y0 + 1, y1))


def apply_result(app, res) -> ScanOutcome:
    """把识别结果落地, 并返回"发生了什么"。**不碰界面** —— 反馈由窗口负责。

    三种落地点: 圈到已有卡片 -> 划掉; 有时间 -> 新建; 只有文字 -> 交给用户补时间。
    """
    if res is None:
        return ScanOutcome(action="cancelled")

    text = (res.text or "").strip()
    # 建卡标题用"挑出来的那一行", 而不是整段 OCR 文本 —— 圈到聊天记录/列表时, 整段里
    # 通常还带着时间戳、状态栏之类的内容 (客户反馈: 抗干扰太弱)。
    focus = (getattr(res, "focus", "") or text).strip()
    if res.action == "delete":
        titles = []
        for rem in res.matched_reminders:
            app.life.complete(rem.id)
            titles.append(rem.title or "(无内容)")
            log.info("圈划删除提醒 #%s %s", rem.id, rem.title)
        return ScanOutcome(action="deleted", count=len(titles),
                           title="、".join(titles[:3]), text=text, engine=res.engine)

    if res.parsed is not None and res.parsed.when:
        from ..models import Reminder

        rem = Reminder(title=focus or res.parsed.matched,
                       source_text=res.text, event_at=res.parsed.when,
                       created_at=app.clock.now(), plan=app.settings.plan("日常"))
        rem = app.life.create(rem, app.clock.now())
        log.info("圈划新建提醒: %s @ %s", rem.title, res.parsed.when)
        return ScanOutcome(action="created", text=focus, title=rem.title,
                           when=res.parsed.when.strftime("%m-%d %H:%M"),
                           reminder_id=rem.id, engine=res.engine)

    # 认到了时间, 但全都不像日程 (时间戳/时刻表) -> 明确说"这段里没读到像日程的话",
    # 不要把整屏文字回填进输入行让用户自己删。
    rejected = getattr(res, "rejected", "")
    if rejected:
        log.info("圈选到的时间表达都被判成噪声: %s", rejected)
        return ScanOutcome(action="noise", text=text, engine=res.engine, reason=rejected)

    # 到这里就是"没建卡": 要么没认到字, 要么认到了但没有时间。原因要能说清。
    if text:
        log.info("识别到文字但没解析出时间, 交给用户补: %r", text)
        return ScanOutcome(action="manual", text=text, engine=res.engine,
                           reason="识别到了文字，但里面没有时间")
    reason = _failure_reason(res)
    log.info("圈选没得到可用文字: %s", reason)
    return ScanOutcome(action="failed", engine=res.engine, reason=reason)


def _failure_reason(res) -> str:
    engine = getattr(res, "engine", "") or ""
    if engine.startswith("error:"):
        detail = engine.split("error:", 1)[1].strip()
        if "OCR" in detail or "后端" in detail:
            return f"没有可用的 OCR 后端（{detail}）"
        return f"识别过程出错：{detail}"
    return "没识别出文字（圈小一点/避开反光试试）"



class _Dumping:
    """包住 scanner, 把真正抓给 OCR 的那张 PNG 落盘, 用来对齐"圈到哪"与"识别到哪"。"""

    def __init__(self, inner, folder: str):
        self._inner = inner
        self._folder = Path(folder)

    def scan_against(self, sel, *args, **kwargs):
        from ..ocr import grab_screen_png

        try:
            self._folder.mkdir(parents=True, exist_ok=True)
            shot = grab_screen_png(sel.bbox)
            (self._folder / f"scan-{time.time_ns()}.png").write_bytes(shot)
            log.info("识别截图已存: %s", self._folder / "scan-*.png")
        except Exception:
            log.debug("存识别截图失败", exc_info=True)
        return self._inner.scan_against(sel, *args, **kwargs)


class SelectionOverlay(QWidget):
    picked = Signal(object)      # ScanOutcome
    scanning = Signal()          # 开始识别 (界面据此给一句"正在识别…", 否则用户对着空屏幕等)

    def __init__(self, app, parent=None, capturer=None, dim: bool = True):
        super().__init__(parent)
        self.app = app
        # 截图链路做成可注入的接缝: 测试里换成假图, 也不依赖真实屏幕
        if capturer is None:
            from ..ocr import grab_screen_png

            capturer = grab_screen_png
        self.capturer = capturer
        # 压暗开关来自设置: 关掉 = 屏幕完全不变暗, 只浮一层选框 (客户"能不能隐藏掉"的那个诉求)
        self.dim_enabled = bool(dim)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        self._origin: QPoint | None = None
        self._current: QPoint | None = None
        self._shots: list[tuple[QRect, QPixmap]] = []
        self._screen = None

    # ------------------------------------------------------------ 启动
    def start(self) -> "SelectionOverlay":
        self._freeze_all_screens()
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self.grabKeyboard()
        return self

    def dimming(self) -> bool:
        """现在是不是"压暗"状态。

        两种情况都不压暗: ① 抓不到冻图 —— 否则就是一块纯黑窗口; ② 用户在设置里关了压暗。
        """
        return bool(self._shots) and self.dim_enabled

    def _freeze_all_screens(self) -> None:
        """把每块屏幕各抓一张图铺上, 屏幕立刻看起来被"冻住"了。

        用与 OCR **同一条**截图链路 (mss 优先), 不用 `QScreen.grabWindow(0)` ——
        后者在 Qt6/Windows 上常常返回空图, 空图 + 压暗 = 用户看到的"黑色窗口"。
        抓不到就跳过那块屏 (不压暗), 用户在正常桌面上圈选, 选框照样看得见。
        """
        failed: list[str] = []
        for screen in QGuiApplication.screens():
            geo = QRect(screen.geometry())
            try:
                png = self.capturer(_screen_physical_bbox(screen))
            except Exception as exc:                 # noqa: BLE001 - 抓图失败不能挡住圈选
                failed.append(f"{geo.width()}x{geo.height()}: {exc}")
                continue
            img = QImage.fromData(png or b"")
            if img.isNull():
                failed.append(f"{geo.width()}x{geo.height()}: 空图")
                continue
            self._shots.append((geo, QPixmap.fromImage(img)))
        if self._shots:
            log.info("冻结层: %d 块屏已冻结 (共 %d 块)", len(self._shots),
                     len(QGuiApplication.screens()))
        if failed:
            log.warning("冻结层: 有屏幕没抓到图, 改为不压暗: %s", "; ".join(failed))

    def paintEvent(self, ev):                       # noqa: N802
        p = QPainter(self)
        # 只在真的抓到图的地方压暗: 抓不到就不画底色, 让用户看到自己正常的屏幕。
        # (以前是无条件 fillRect 一层黑, 抓图一失败就整屏发黑。)
        for rect, pm in self._shots:
            p.drawPixmap(rect, pm)
            if self.dim_enabled:                        # 用户在设置里关了压暗就不画这层黑
                p.fillRect(rect, QColor(0, 0, 0, DIM))

        # 选区: 高亮 + 描边 + 尺寸提示
        if self._origin and self._current:
            sel = QRect(self._origin, self._current).normalized()
            pm, shot_rect, scale = self._shot_at(sel)
            if not pm.isNull():
                # 抓到的图是**物理像素**, 选区是**逻辑坐标** —— 直接 copy(sel) 会裁到左上角
                # 1/scale 处的内容, 于是"框里亮出来的字"和"松手后真正识别的字"差一大截
                # (150% 缩放下实测差 1.5 倍)。先按实测比例把选区换算到图上的像素再裁。
                p.drawPixmap(sel, pm, self._sel_to_pixels(sel, shot_rect, scale))
            pen = QPen(QColor(255, 214, 102), 2, Qt.SolidLine)
            p.setPen(pen)
            p.drawRect(sel)
            p.setPen(QPen(QColor(255, 255, 255, 220)))
            p.setFont(self.font())
            p.drawText(sel.adjusted(4, 4, 4, 4), Qt.AlignLeft | Qt.AlignTop,
                       f"{sel.width()}×{sel.height()}")

        hint = "圈选要识别的文字 · 松开鼠标后自动识别 · Esc 取消"
        if not self.dimming():
            # 没压暗时白字可能落在浅色背景上看不清, 垫一条深色底
            band = self.rect().adjusted(0, 18, 0, -self.height() + 46)
            p.fillRect(band, QColor(0, 0, 0, 150))
        p.setPen(QPen(QColor(255, 255, 255, 235)))
        p.drawText(self.rect().adjusted(0, 24, 0, 0), Qt.AlignHCenter | Qt.AlignTop, hint)
        p.end()

    def _shot_at(self, sel: QRect) -> tuple[QPixmap, QRect, float]:
        """选出选区所在的屏幕截图, 连同"该图覆盖的屏幕逻辑矩形"和"逻辑->物理比例"。

        比例取**实测值** `图宽 / 该屏逻辑宽`, 不用 `devicePixelRatio()` 想当然: 抓图是物理像素,
        而 Qt 的逻辑坐标与 dpr 在某些环境下并不自洽 (实测过两者差 1.5 倍的情形), 用实测比例才能
        保证"高亮显示的那块"与"送去 OCR 的那块"永远是同一块。
        """
        for rect, pm in self._shots:
            if rect.contains(sel.topLeft()) or rect.contains(sel.center()):
                scale = pm.width() / rect.width() if rect.width() else 1.0
                return pm, QRect(rect), scale
        if self._shots:
            rect, pm = self._shots[0]
            scale = pm.width() / rect.width() if rect.width() else 1.0
            return pm, QRect(rect), scale
        return QPixmap(), QRect(sel), 1.0

    @staticmethod
    def _sel_to_pixels(sel: QRect, shot_rect: QRect, scale: float) -> QRect:
        """把逻辑坐标的选区换算成截图上的物理像素矩形 (拷贝/高亮用)。"""
        return QRect(round((sel.x() - shot_rect.x()) * scale),
                     round((sel.y() - shot_rect.y()) * scale),
                     max(1, round(sel.width() * scale)),
                     max(1, round(sel.height() * scale)))

    # ------------------------------------------------------------ 鼠标
    def mousePressEvent(self, ev):                   # noqa: N802
        if ev.button() == Qt.RightButton:
            self._finish(None)
            return
        self._origin = ev.position().toPoint()
        self._current = self._origin
        self.update()

    def mouseMoveEvent(self, ev):                    # noqa: N802
        self._current = ev.position().toPoint()
        self.update()

    def mouseReleaseEvent(self, ev):                 # noqa: N802
        if ev.button() != Qt.LeftButton or not self._origin:
            return
        sel = QRect(self._origin, ev.position().toPoint()).normalized()
        self._origin = self._current = None
        if sel.width() < MIN_SIZE or sel.height() < MIN_SIZE:
            self._finish(None)
            return
        self.hide()
        self.releaseKeyboard()
        QApplication.processEvents()
        # 告诉界面"开始识别了": 第一次要加载 OCR 模型, 可能好几秒 —— 这段时间里
        # 屏幕上什么都没有, 用户只会觉得"按了没用"(客户反馈"识别后没有反馈"的一半原因)。
        self.scanning.emit()
        result = self._scan(sel)
        self._finish(result)

    def keyPressEvent(self, ev):                     # noqa: N802
        if ev.key() == Qt.Key_Escape:
            self._finish(None)

    # ------------------------------------------------------------ 识别
    def _scan(self, sel: QRect):
        screen = QGuiApplication.screenAt(sel.center()) or QGuiApplication.primaryScreen()
        dpr = screen.devicePixelRatio()
        geo = screen.geometry()
        # 逻辑坐标 -> 该屏物理像素 (多屏 DPI 不一致时必须逐屏换算)。
        # 比例优先用冻结图的实测值, 拿不到才退回 devicePixelRatio。
        pm, shot_rect, scale = self._shot_at(sel)
        if pm.isNull():
            shot_rect, scale = QRect(geo), dpr
        phys = self._sel_to_pixels(sel, shot_rect, scale)
        bbox = (phys.x(), phys.y(), phys.width(), phys.height())

        from ..ocr import Scanner, Selection

        # 排障用: 反馈"圈的位置和识别的不一样"时, 这条日志能立刻区分是坐标换算错
        # 还是别的原因; 配上 HANDYREMINDER_DUMP_SCAN=<目录> 还能把送去 OCR 的图存下来
        log.info("圈选选区 逻辑%s -> 物理框%s (dpr=%s, 实测比例=%s, 屏%s@%s)",
                 (sel.x(), sel.y(), sel.width(), sel.height()), bbox, dpr, round(scale, 3),
                 (geo.width(), geo.height()), (geo.x(), geo.y()))

        scanner = self.app.scanner or Scanner(clock=self.app.clock)
        dump = os.environ.get("HANDYREMINDER_DUMP_SCAN")
        if dump:
            # 排障用: 把"实际送去 OCR 的那张图"和"圈的位置"一起留证
            scanner = _Dumping(scanner, dump)
        try:
            res = scanner.scan_against(Selection(*bbox), self.app.store.list_active(),
                                       now=self.app.clock.now())
        except Exception as exc:
            log.exception("圈选识别失败")
            from ..ocr import ScanResult

            return ScanResult(text="", parsed=None, bbox=bbox, engine=f"error:{exc}",
                              confidence=0.0)
        return res

    def _finish(self, result) -> None:
        """收尾: 落地 + 把结论交给便签窗口去提示 (界面反馈不在这一层做)。"""
        self.hide()
        self.releaseKeyboard()
        for _rect, pm in self._shots:
            pm.detach() if hasattr(pm, "detach") else None
        self._shots.clear()
        if result is not None:
            try:
                outcome = apply_result(self.app, result)
            except Exception as exc:                 # noqa: BLE001 - 落地失败也必须给用户一句话
                log.exception("圈选结果落地失败")
                outcome = ScanOutcome(action="failed", reason=f"建卡失败：{exc}",
                                      engine=getattr(result, "engine", ""))
            self.picked.emit(outcome)
        self.deleteLater()

    # ------------------------------------------------------------ 落地
    def _handle(self, res) -> ScanOutcome:
        """兼容旧调用点: 落地并返回结论 (反馈由便签窗口的 `_on_scan_result` 显示)。"""
        return apply_result(self.app, res)
