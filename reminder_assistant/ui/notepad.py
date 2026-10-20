"""便签式悬浮窗 (PRD 1.1 / 1.2 / 三章界面切换)。

关键实现决策, 都对应验收标准:
  * 无边框 + 半透明 + 置顶 + 不进任务栏 (WindowStaysOnTopHint | Tool | FramelessWindowHint)
  * 纸张底图缓存, 只有数据变化才重绘 (避免常驻 CPU 占用)
  * 行数据按 id 做差量更新, 不重建控件 (避免闪烁与内存泄漏)
  * 翻页用"折角 + 阴影扫过"模拟, 并采样帧耗时; 连续掉帧自动降级为淡入淡出,
    降级结果写回数据目录 (4.3 无感运行 + 1.2 性能降级机制)
  * 鼠标穿透 / 收起 / 置顶 / 托盘 全部可切换, 状态也持久化
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (QEasingCurve, QPoint, QRect, QRectF, QSize, Qt, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import QAction, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
                               QPushButton, QSizePolicy, QSystemTrayIcon, QVBoxLayout, QWidget)

from . import theme
from ..tasks import GROUP_MODE_LABELS, GROUP_MODES, MAX_DEPTH, DEFAULT_GROUP_MODE

NOTE_W, NOTE_H = 320, 460
COLLAPSED_H = 44
HEADER_H = 30               # 标题 + 右上角那排按钮
INPUT_H = 30                # 底部"写一句话"输入行
GRIP_W = 18                 # 右下角调整大小的把手
HIST_PER_PAGE = 8           # 历史每页条数 (客户反馈: 历史别再堆在一页里)
PAGER_LABEL_W = 64          # 左下角分页指示器宽度
MIN_W, MIN_H = 240, 220
EDGE_SNAP = 28              # 拖到离屏幕边缘这么近就自动贴边并收起

log = logging.getLogger("reminder.ui")


# ---------------------------------------------------------------- 单行任务

class TaskRow(QFrame):
    done = Signal(int, int)      # reminder_id, occurrence_index
    snooze = Signal(int, int)
    advanced = Signal(int)       # reminder_id -> 打开"事件属性/提醒频率"面板
    more = Signal(int)           # reminder_id -> 弹出"⋯"菜单 (子任务/删除)
    toggle_children = Signal(int)

    INDENT = 16                  # 子任务每层缩进的像素 (只允许二级)

    def __init__(self, item: dict, parent=None):
        super().__init__(parent)
        self.item = item
        self.depth = int(item.get("depth") or 0)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(theme.LINE_HEIGHT + 8)
        self._struck = 0.0            # 划掉动画进度 0..1

        lay = QHBoxLayout(self)
        lay.setContentsMargins(2 + self.INDENT * self.depth, 0, 2, 0)
        lay.setSpacing(6)

        # 主任务旁的"展开二级目录": 有子任务才给这个按钮, 平时不占地方
        self.btn_children = QPushButton("")
        self.btn_children.setFont(theme.handwriting_font(10, bold=False))
        self.btn_children.setFixedSize(18, 18)
        self.btn_children.setCursor(Qt.PointingHandCursor)
        self.btn_children.setStyleSheet(
            "QPushButton{background:transparent;border:0;padding:0;"
            f"color:{theme.INK_LIGHT};}}"
            "QPushButton:hover{color:#B5651D;}")
        self.btn_children.clicked.connect(lambda: self.toggle_children.emit(self.item["id"]))
        self.btn_children.hide()

        self.dot = QLabel("●")
        self.dot.setStyleSheet(
            f"color:{theme.LEVEL_COLORS.get(item.get('level', '日常'), '#888')};font-size:11px;")
        self.dot.setFixedWidth(12)

        raw_title = item["title"] or "(无内容)"
        if self.depth:
            raw_title = "└ " + raw_title
        title_room = 14 if not self.depth else 12
        if len(raw_title) > title_room:              # 一行放不下就加省略号, 比直接裁断好读
            raw_title = raw_title[:title_room] + "…"
        self.text = QLabel(theme.jitter(raw_title, seed=item["id"]))
        self.text.setFont(theme.handwriting_font(14 if not self.depth else 12, bold=True))
        self.text.setStyleSheet(f"color:{theme.INK};")
        self.text.setWordWrap(False)
        self.text.setToolTip(self._tooltip())

        when = _fmt(item.get("next_fire") or item.get("event_at"), short=True)
        dur = int(item.get("duration_min") or 0)
        self.time = QLabel(f"{when}" + (f" ·{dur}′" if dur else ""))
        self.time.setFont(theme.handwriting_font(11, bold=False))
        self.time.setStyleSheet(f"color:{theme.INK_LIGHT};")

        self.progress = QLabel(f"{item.get('fired', 0)}/{item.get('total', 1)}")
        self.progress.setFont(theme.handwriting_font(10, bold=False))
        self.progress.setStyleSheet(f"color:{theme.INK_LIGHT};")

        self.btn_done = QPushButton("划掉")
        self.btn_snooze = QPushButton("稍后")
        self.btn_advanced = QPushButton("属性")
        self.btn_more = QPushButton("⋯")
        for b in (self.btn_done, self.btn_snooze, self.btn_advanced, self.btn_more):
            b.setFont(theme.handwriting_font(10, bold=False))
            b.setFixedHeight(20)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(
                "QPushButton{background:rgba(255,255,255,0.55);border:1px solid #D9CFBC;"
                f"border-radius:9px;padding:0 6px;color:{theme.INK};}}"
                "QPushButton:hover{background:rgba(255,245,220,0.95);}")
            b.hide()
        self.btn_more.setFixedWidth(22)
        self.btn_done.clicked.connect(lambda: self.done.emit(self.item["id"], 0))
        self.btn_snooze.clicked.connect(lambda: self.snooze.emit(self.item["id"], 0))
        self.btn_advanced.clicked.connect(lambda: self.advanced.emit(self.item["id"]))
        self.btn_more.clicked.connect(lambda: self.more.emit(self.item["id"]))
        self.btn_advanced.setToolTip("事件属性与提醒频率: 名称 / 时间 / 重要程度 / 持续时间 / 备注")
        self.btn_more.setToolTip("更多: 创建子任务(向导) / 展开子任务 / 删除这条")

        lay.addWidget(self.btn_children)
        lay.addWidget(self.dot)
        lay.addWidget(self.text, 1)
        lay.addWidget(self.progress)
        lay.addWidget(self.time)
        lay.addWidget(self.btn_advanced)
        lay.addWidget(self.btn_snooze)
        lay.addWidget(self.btn_done)
        lay.addWidget(self.btn_more)

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(260)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_strike)
        self._anim.finished.connect(self._on_strike_done)

    def _tooltip(self) -> str:
        it = self.item
        dur = int(it.get("duration_min") or 0)
        lines = [it.get("title") or "(无内容)",
                 f"事件时间: {_fmt(it.get('event_at'))}",
                 f"下次提醒: {_fmt(it.get('next_fire'))}"]
        if dur:
            lines.append(f"持续时间: {dur} 分钟")
        if it.get("note"):
            lines.append(f"备注: {it['note']}")
        if it.get("depth"):
            lines.append("(子任务)")
        elif it.get("child_count"):
            lines.append(f"包含 {it['child_count']} 个子任务")
        return "\n".join(lines)

    def refresh_labels(self, item: dict) -> None:
        """差量更新: 数据变了就地改文字, 不重建控件 (重建会闪、也容易漏)。"""
        self.item = item
        self.time.setText(_fmt(item.get("next_fire") or item.get("event_at"), short=True)
                          + (f" ·{int(item['duration_min'])}′" if item.get("duration_min") else ""))
        self.progress.setText(f"{item.get('fired', 0)}/{item.get('total', 1)}")
        self.text.setToolTip(self._tooltip())
        n = int(item.get("child_count") or 0)
        self.btn_children.setVisible(bool(n) and not self.depth)
        if n:
            self.btn_children.setText(("▾" if item.get("expanded") else "▸") + str(n))

    # ---- 交互
    def enterEvent(self, ev):                       # noqa: N802
        self.btn_done.show()
        self.btn_snooze.show()
        self.btn_advanced.show()
        self.btn_more.show()
        super().enterEvent(ev)

    def leaveEvent(self, ev):                       # noqa: N802
        self.btn_done.hide()
        self.btn_snooze.hide()
        self.btn_advanced.hide()
        self.btn_more.hide()
        super().leaveEvent(ev)

    def strike_out(self) -> None:
        self._anim.stop()
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()

    def _on_strike(self, v) -> None:
        self._struck = float(v)
        self.update()

    def _on_strike_done(self) -> None:
        self.setVisible(False)

    def paintEvent(self, ev):                       # noqa: N802
        super().paintEvent(ev)
        if self._struck <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(theme.INK))
        pen.setWidthF(2.0)
        p.setPen(pen)
        y = self.height() / 2
        w = (self.text.geometry().right() - 2) * self._struck
        p.drawLine(16, int(y), 16 + int(w), int(y))
        p.end()


def _fmt(dt, short: bool = False) -> str:
    if not dt:
        return "--"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return "--"
    if short:
        today = datetime.now(dt.tzinfo).date()
        if dt.date() == today:
            return f"{dt:%H:%M}"
        return f"{dt:%m-%d %H:%M}"
    return f"{dt:%Y-%m-%d %H:%M}"


# ---------------------------------------------------------------- 分组表头

class GroupRow(QFrame):
    """一条分组线: "今天"/"「重要」" 这样的表头 (PRD 2.5 的分类方式)。

    用控件而不是在纸上直接画, 是为了让它跟着 rows_holder 的布局走 ——
    手画的位置在窗口缩放/收起时会和行错位。
    """

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(20)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 0, 2, 0)
        self.text = QLabel(label)
        self.text.setFont(theme.handwriting_font(11, bold=True))
        self.text.setStyleSheet(f"color:{theme.INK_LIGHT};")
        lay.addWidget(self.text)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background:rgba(150,135,110,0.35);")
        lay.addWidget(line, 1)

    def set_label(self, label: str) -> None:
        self.text.setText(label)


# ---------------------------------------------------------------- 一页纸

class NotePage(QWidget):
    advanced = Signal(int)               # 由 NotepadWindow 转接到"事件属性"面板
    done = Signal(int, int)              # 划掉
    snooze = Signal(int, int)            # 稍后
    more = Signal(int)                   # ⋯ 菜单
    toggle_children = Signal(int)        # 展开/收起二级目录

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._rows: dict[int, TaskRow] = {}
        self._headers: dict[str, GroupRow] = {}
        # 父控件**必须**给 (self): 没有父控件的 QWidget 一被 setVisible(True) 就会变成
        # 一扇真窗口 —— 实测症状是屏幕左上角冒出一块黑色圆角窗口, 内容是这句"便签是空的",
        # 字号/尺寸都是"整页", 用户以为程序坏了 (客户截图里那块黑窗就是它, 跟 OCR 无关)。
        # 无头用例看不见它 (offscreen 不建真窗口), 所以 tests 里另有一条"不许有野窗口"的用例。
        self._empty = QLabel("便签是空的\n点右上角 ＋ 写一句，或按 Alt+Q 圈住屏幕上的文字", self)
        self._empty.setFont(theme.handwriting_font(13, bold=False))
        self._empty.setAlignment(Qt.AlignCenter)
        # 必须允许换行: 居中 + 比控件宽的文字, Qt 会把两边各切掉一半 (实测"点右上角"和
        # "文字"两头都不见了)。
        self._empty.setWordWrap(True)
        self._empty.setStyleSheet(f"color:{theme.INK_LIGHT};")
        self.rows_holder = QWidget(self)
        self.rows_holder.setAttribute(Qt.WA_TranslucentBackground)
        lay = QVBoxLayout(self.rows_holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._lay = lay

    def _place_empty(self) -> None:
        """空态文案的矩形: 两侧留出纸的边距, 否则字会压到左边的装订线上。"""
        self._empty.setGeometry(theme.PADDING[0], 0,
                                max(60, self.width() - theme.PADDING[0] - theme.PADDING[2]),
                                self.height())

    def resizeEvent(self, ev):                      # noqa: N802
        self.rows_holder.setGeometry(theme.PADDING[0], theme.PADDING[1] + 30,
                                     self.width() - theme.PADDING[0] - theme.PADDING[2],
                                     self.height() - theme.PADDING[1] - theme.PADDING[3] - 30)
        # 空态文案同样要跟着页面尺寸走 (否则改窗口大小后它停在旧尺寸上, 看起来像"没居中")
        self._place_empty()
        super().resizeEvent(ev)

    def set_items(self, items: list[dict]) -> None:
        """差量更新: 只增删改变化的行, 不整体重建。

        items 是按显示顺序排好的扁平列表 (主任务后面紧跟它的子任务), 每项可带
        group_label —— 遇到就插一条分组线。_rows 仍以 reminder id 为键, 不因为
        多了分组/层级就换一套寻址方式 (换掉的话, 所有"按 id 找行"的地方都要跟着改)。
        """
        wanted = {it["id"]: it for it in items if it.get("id") is not None}
        for rid in list(self._rows):
            if rid not in wanted:
                row = self._rows.pop(rid)
                self._lay.removeWidget(row)
                row.deleteLater()
        for label in list(self._headers):
            if not any((it.get("group_label") or "") == label for it in items):
                head = self._headers.pop(label)
                self._lay.removeWidget(head)
                head.deleteLater()

        cursor = 0
        for it in items:
            label = it.get("group_label") or ""
            if label:
                head = self._headers.get(label)
                if head is None:
                    head = GroupRow(label, self.rows_holder)
                    self._headers[label] = head
                else:
                    head.set_label(label)
                self._lay.removeWidget(head)
                self._lay.insertWidget(cursor, head)
                head.show()
                cursor += 1
            rid = it.get("id")
            if rid is None:
                continue
            row = self._rows.get(rid)
            if row is None:
                row = TaskRow(it, self.rows_holder)
                row.advanced.connect(self.advanced.emit)
                row.done.connect(self.done.emit)
                row.snooze.connect(self.snooze.emit)
                row.more.connect(self.more.emit)
                row.toggle_children.connect(self.toggle_children.emit)
                self._rows[rid] = row
                self._lay.addWidget(row)
            row.refresh_labels(it)
            row.setVisible(True)
            self._lay.removeWidget(row)
            self._lay.insertWidget(cursor, row)
            cursor += 1
        self._empty.setVisible(not items)
        self._place_empty()

    def row(self, rid: int) -> TaskRow | None:
        return self._rows.get(rid)

    def header(self, label: str) -> GroupRow | None:
        return self._headers.get(label)


# ---------------------------------------------------------------- 翻页

class FlipOverlay(QWidget):
    """翻页视觉: 一条带渐变的"折角"扫过纸面。纯绘制, 不建子控件。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.progress = 0.0
        self.direction = 1
        self.hide()

    def paintEvent(self, ev):                        # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        edge = w * self.progress * self.direction
        band = max(28, w * 0.35)
        path = QPainterPath()
        path.moveTo(edge, 0)
        path.lineTo(edge + self.direction * band, 0)
        path.lineTo(edge + self.direction * band, h)
        path.lineTo(edge, h)
        path.closeSubpath()
        grad = QColor(0, 0, 0, 0)
        from PySide6.QtGui import QLinearGradient

        lg = QLinearGradient(edge, 0, edge + self.direction * band, 0)
        lg.setColorAt(0.0, QColor(90, 78, 60, 90))
        lg.setColorAt(0.45, QColor(255, 255, 255, 120))
        lg.setColorAt(1.0, grad)
        p.setPen(Qt.NoPen)
        p.setBrush(lg)
        p.drawPath(path)
        p.end()


class FlipController(QVariantAnimation):
    """翻页动画控制器, 自带掉帧检测与降级。

    PRD 1.2 要求"低配设备卡顿则平滑降级为淡入淡出"。判定依据是实测帧间隔,
    不是猜设备型号; 降级后写入 meta, 下次启动直接走淡入淡出。
    """

    def __init__(self, host, on_switch, on_frame=None):
        super().__init__(host)
        self.host = host
        self.on_switch = on_switch
        self.on_frame = on_frame
        self.setDuration(330)
        self.setStartValue(0.0)
        self.setEndValue(1.0)
        self.setEasingCurve(QEasingCurve.InOutCubic)
        self._switched = False
        self._frames: list[float] = []
        self._last = None
        self.mode = "flip"            # flip | fade
        self.on_downgrade = None
        self.valueChanged.connect(self._tick)

    def turn(self, direction: int = 1) -> None:
        self._switched = False
        self._frames.clear()
        self.direction = direction
        self.start()

    def _tick(self, value) -> None:
        import time

        now = time.perf_counter()
        if self._last is not None and self.mode == "flip":
            self._frames.append((now - self._last) * 1000.0)
        self._last = now
        if not self._switched and float(value) >= 0.5:
            self._switched = True
            self.on_switch()
        # 连续 3 次翻页平均帧间隔 > 33ms -> 降级
        if self.mode == "flip" and self.on_frame:
            self.on_frame(self._frames)
        if self._frames and sum(self._frames) / len(self._frames) > 33 and len(self._frames) > 8:
            self.mode = "fade"
            if self.on_downgrade:
                self.on_downgrade()


# ---------------------------------------------------------------- 主窗口

class NotepadWindow(QWidget):
    # 热键是在**热键线程**里触发的, 而建窗口必须在 GUI 线程 —— 用信号过一道。
    # (直接在线程里 SelectionOverlay(...).start() 是无效的: Qt 不允许在工作线程建控件,
    #  实测表现就是"按下 Alt+Q 什么都没发生", 而且一条日志都不给。)
    scan_requested = Signal()

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.paper = theme.PaperCache()
        self.setWindowTitle("智能手账")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(NOTE_W, NOTE_H)

        theme.load_bundled_fonts()

        from .overlay import SelectionOverlay

        self.overlay_cls = SelectionOverlay

        # _collapsed 必须在造页之前就位: _make_page() 会调 _place_page(), 而后者要按
        # "是否收起"决定页高 —— 顺序反了就是构造期 AttributeError(整个窗口起不来)。
        self._collapsed = False
        self.pages = [self._make_page(), self._make_page()]
        self.page_index = 0
        self.pages[0].setVisible(True)
        self._hist_total = 0                       # 历史总条数
        self._hist_pages = 1                       # 历史占几页 (每页 HIST_PER_PAGE 条)
        self._pending_dir = 1                      # 翻页动画进行到一半时该往哪边走

        self.flipper = FlipOverlay(self)
        self.flipper.hide()
        self.flip = FlipController(self, self._switch_page)
        self.flip.finished.connect(self.flipper.hide)
        self.flip.on_downgrade = lambda: self.app.store.set_meta("flip_mode", "fade")

        self.overlay = None          # 正在圈选的冻结层 (必须存住, 见 _start_scan)
        self._drag: QPoint | None = None
        self._last_fingerprint = None
        self._user_size = QSize(NOTE_W, NOTE_H)      # 用户自己拖出来的大小 (收起后要还原)
        self._resizing: QPoint | None = None
        self._collapsed_uuids: set[str] = set()      # 被用户收起的二级目录 (默认展开)
        self.group_mode = "time"                     # 排序/分类方式 (PRD 2.5)
        self.setMinimumSize(MIN_W, MIN_H)

        self.scan_requested.connect(self._start_scan)
        self._build_tools()
        self._build_tray()
        self._restore_state()
        self._first_run_hint()

        self.timer = QTimer(self)
        self.timer.setInterval(2000)                 # 2 秒一次廉价查询, 只在数据变化时重绘
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh(force=True)

    # ------------------------------------------------------------ 便签自带的入口
    def _build_tools(self) -> None:
        """便签右上角的 ＋ / ⚙ / － / × 和底部的"写一句话"。

        为什么必须有这些 (实测反馈): 原来所有操作都只挂在**托盘右键菜单**上, 而 Win11 默认把
        新托盘图标折进"隐藏的图标"里 —— 用户看到的就是一块"像图片、点不动"的纸。
        便签自己必须能: 添加、设置、贴边收起、关掉。
        """
        self.tools = QWidget(self)
        row = QHBoxLayout(self.tools)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        self.btn_add = self._tool_button("＋", "添加一条提醒（也可以在下面直接写一句话按回车）",
                                         self._focus_editor)
        self.btn_settings = self._tool_button("⚙", "设置：账户与设备绑定 / 等级频率 / 免打扰 / 快捷键 / 开机自启",
                                              self._open_settings)
        self.btn_stow = self._tool_button("－", "贴边收起：收成屏幕边上的一条，再点一下展开",
                                          self.stow_to_edge)
        self.btn_close = self._tool_button("×", "关闭便签（收进托盘，提醒继续运行）", self.close)
        self.tool_buttons = (self.btn_add, self.btn_settings, self.btn_stow, self.btn_close)
        for b in self.tool_buttons:
            row.addWidget(b)

        # 分类/排序方式 (PRD 2.5): 时间(默认) → 重要程度 → 开始日期。
        # 单独一个控件、不进 tool_buttons: 它是"视图开关", 不是操作按钮。
        self.btn_group = QPushButton("⇅时间", self)
        self.btn_group.setFont(theme.handwriting_font(10, bold=False))
        self.btn_group.setFixedHeight(20)
        self.btn_group.setCursor(Qt.PointingHandCursor)
        self.btn_group.setStyleSheet(
            "QPushButton{background:rgba(255,252,244,0.9);border:1px solid #D9CFBC;"
            f"border-radius:9px;padding:0 6px;color:{theme.INK_LIGHT};}}"
            "QPushButton:hover{background:rgba(255,238,200,1.0);}")
        self.btn_group.clicked.connect(self.cycle_group_mode)
        self.btn_group.setToolTip("任务分类方式：按时间(默认) / 按重要程度 / 按开始日期。"
                                  "默认严格按时间排序。")
        self._sync_group_button()

        # 分页入口 (客户反馈: "历史事件堆在一页, 请添加多页")。
        # 必须**看得见**: 上一版的经验是"功能做了但只在托盘菜单里 = 用户找不到",
        # 所以做成便签左下角一排 ◀ 今日/历史 x/y ▶, 和滚轮 / ←→ 键是同一套动作。
        self.page_pager = QWidget(self)
        pager_row = QHBoxLayout(self.page_pager)
        pager_row.setContentsMargins(0, 0, 0, 0)
        pager_row.setSpacing(2)
        self.btn_prev_page = self._tool_button(
            "◀", "上一页：从「已翻过去的页」往回翻（滚轮上滚、← 键也行）",
            lambda: self.turn_page(-1))
        self.btn_next_page = self._tool_button(
            "▶", "下一页：往后翻历史（滚轮下滚、→ 键也行）",
            lambda: self.turn_page(1))
        self.page_label_widget = QLabel("今日", self.page_pager)
        self.page_label_widget.setFont(theme.handwriting_font(10, bold=False))
        self.page_label_widget.setStyleSheet(
            f"QLabel{{color:{theme.INK_LIGHT};background:transparent;}}")
        self.page_label_widget.setAlignment(Qt.AlignCenter)
        self.page_label_widget.setFixedWidth(PAGER_LABEL_W)
        pager_row.addWidget(self.btn_prev_page)
        pager_row.addWidget(self.page_label_widget)
        pager_row.addWidget(self.btn_next_page)
        self._sync_pager()

        self.editor = QLineEdit(self)
        self.editor.setPlaceholderText("写一句「明天下午三点开会」，回车记上")
        self.editor.setFont(theme.handwriting_font(12, bold=False))
        self.editor.setStyleSheet(
            "QLineEdit{background:rgba(255,255,255,0.55);border:1px dashed #D9CFBC;"
            f"border-radius:5px;padding:0 6px;color:{theme.INK};}}"
            "QLineEdit:focus{border:1px solid #C9AE82;background:rgba(255,252,240,0.95);}")
        self.editor.returnPressed.connect(self._submit_editor)
        self.editor.setToolTip("时间可以用中文说：明天下午三点 / 10 分钟后 / 下周一上午十点")

    def _tool_button(self, text: str, tip: str, slot) -> QPushButton:
        b = QPushButton(text, self)
        b.setFont(theme.handwriting_font(13, bold=True))
        b.setFixedSize(20, 20)
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip(tip)
        b.setStyleSheet(
            "QPushButton{background:rgba(255,252,244,0.96);border:1px solid #D9CFBC;"
            f"border-radius:10px;color:{theme.INK};padding:0;}}"
            "QPushButton:hover{background:rgba(255,238,200,1.0);border:1px solid #C9AE82;}")
        b.clicked.connect(slot)
        return b

    def _layout_parts(self) -> None:
        """按钮排 / 输入行 / 分页器 的位置 (窗口 resize 时重算)。"""
        w, h = self.width(), self.height()
        bw = 22 * len(self.tool_buttons)
        self.tools.setGeometry(w - theme.PADDING[2] - bw, 4, bw, 22)
        gw = 56
        self.btn_group.setGeometry(w - theme.PADDING[2] - bw - gw - 4, 5, gw, 20)
        bottom = h - theme.PADDING[3] - INPUT_H
        pager_w = 20 * 2 + PAGER_LABEL_W + 4
        self.page_pager.setGeometry(theme.PADDING[0], bottom, pager_w, INPUT_H)
        self.editor.setGeometry(theme.PADDING[0] + pager_w + 6, bottom,
                                max(80, w - theme.PADDING[0] - theme.PADDING[2] - pager_w - 6),
                                INPUT_H)
        show = not self._collapsed
        self.tools.setVisible(show)
        self.btn_group.setVisible(show)
        self.page_pager.setVisible(show)
        self.editor.setVisible(show)

    def _focus_editor(self) -> None:
        if self._collapsed:
            self.toggle_collapse()
        self.editor.setFocus()

    def _submit_editor(self) -> None:
        text = self.editor.text().strip()
        if text and self.add_from_text(text):
            self.editor.clear()

    def add_from_text(self, text: str) -> bool:
        """在便签上写一句话就直接建卡 (PRD "无需切换应用即可添加日程" 的最短路径)。"""
        rem, parsed = self.app.create_from_text(text)
        if rem is None:
            why = "；".join(parsed.notes) if getattr(parsed, "notes", None) else "没识别到时间"
            self._notify(f"没听出时间：{text}\n{why}。可以试试「明天下午三点」「10 分钟后」")
            return False
        self.refresh(force=True)
        self._notify(f"已记上：{rem.title}  {rem.event_at:%m-%d %H:%M}")
        return True

    def scan_available(self) -> bool:
        """圈选识别 (Alt+Q) 现在能不能用 —— 没有 OCR 后端时要在界面上说清, 而不是按了没反应。

        判定必须是**真的能构造出引擎** (probe, 进程内缓存): 只查"包在不在"会在
        "装了但加载失败"时误判成可用 —— 用户按下 Alt+Q 会先弹出一个全屏层, 圈完才被告知
        "未安装任何 OCR 后端"(客户反馈:"系统提示没装 OCR, 但识别窗口还在")。
        扫描器通常已在启动时预热好, 这里多数情况只是读一下缓存。
        """
        from ..ocr import StubOcrEngine

        scanner = getattr(self.app, "scanner", None)
        if scanner is None:
            try:
                scanner = self.app.ensure_scanner()
            except Exception:                        # noqa: BLE001 - 建不起来就是"不可用"
                log.exception("构建 OCR 扫描器失败")
                return False
        engines = getattr(scanner, "engines", None) or []
        return bool(engines) and not isinstance(engines[0], StubOcrEngine)

    def ocr_problem(self) -> str:
        """圈选用不了时, 给用户一句**能据此行动**的原因。"""
        from ..ocr import StubOcrEngine, diagnosis

        scanner = getattr(self.app, "scanner", None)
        engines = getattr(scanner, "engines", None) or []
        if engines and isinstance(engines[0], StubOcrEngine):
            try:
                scanner.engines[0].recognize(b"")     # 取出 Stub 里那句真实的报错
            except Exception as exc:                 # noqa: BLE001
                return str(exc)
        if not getattr(self.app, "scanner", None):
            try:
                return diagnosis()
            except Exception:                        # noqa: BLE001
                pass
        return "本机没有可用的 OCR 后端"

    def _first_run_hint(self) -> None:
        """第一次跑起来时告诉用户入口在哪 —— 托盘图标默认是被 Win11 折叠的。"""
        fresh = self.app.store.get_meta("hint_shown") != "1"
        if fresh:
            self.app.store.set_meta("hint_shown", "1")
            tips = "智能手账已就位：右上角 ＋ 添加提醒，⚙ 打开设置，× 收进托盘。"
            if not self.scan_available():
                tips += "\n圈选识别 (Alt+Q) 需要装 OCR 后端；现在可以直接用 ＋ 写一句话添加。"
            self._notify(tips)
        # 2.5 的任务管理是后加的: 老用户已经看过上面那条提示, 再也看不到引导,
        # 于是"功能有、用户找不到" —— 换一个 key 再指一次路。
        if self.app.store.get_meta("hint_v25_shown") != "1":
            self.app.store.set_meta("hint_v25_shown", "1")
            self._notify("新:任务可以分两级了 —— 每行悬停点「⋯」→「创建子任务…（向导）」；"
                         "点「属性」改名称/时间/重要程度/持续时间；"
                         "右上角 ⇅ 切换按时间 / 重要程度 / 日期 分类。")
        # 2.4 的账户绑定同样是"客户说看不到"的那类功能: 换 key 再指一次路,
        # 并说清它在哪儿 (设置面板的第一个模块)。
        if self.app.store.get_meta("hint_v24bind_shown") != "1":
            self.app.store.set_meta("hint_v24bind_shown", "1")
            self._notify("新:手机端可以绑定了 —— 点右上角 ⚙ → 「账户与设备绑定」，"
                         "点「生成绑定码（手机扫码用）」，手机端扫码或输码即可完成绑定。")
        # 2.5.3 的历史分页同样是"新入口" —— 老用户看过前几条引导, 不换 key 就再也看不到指引,
        # 而分页不看说明不一定发现得了 (客户反馈原话就是"历史堆在一页")。
        if self.app.store.get_meta("hint_v253_shown") != "1":
            self.app.store.set_meta("hint_v253_shown", "1")
            self._notify("新:历史记录分页了 —— 便签左下角的 ◀ ▶ 翻页（滚轮、←/→ 键也行）。\n"
                         f"最前面是「今日手账」页，往后是已翻过去的事件，每页 {HIST_PER_PAGE} 条。")

    # ---- 贴边
    def _edge_x(self, side: str, geo) -> int:
        """贴到左/右边缘时窗口的 x。

        QRect.right() 是"最右边那一列像素"的下标, 所以要让窗口正好贴齐, 得 +1 ——
        否则会留一条 1px 的缝, 视觉上像没贴住。
        """
        return geo.left() if side == "left" else geo.right() - self.width() + 1

    def _screen_geo(self):
        scr = QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        return scr.availableGeometry()

    def apply_edge_snap(self, threshold: int = EDGE_SNAP) -> str | None:
        """松手后调用: 贴近屏幕左/右边缘就吸附过去并收成一条 (贴边隐藏)。"""
        geo = self._screen_geo()
        left_gap = self.x() - geo.left()
        right_gap = geo.right() - (self.x() + self.width())
        if min(left_gap, right_gap) > threshold:
            return None
        side = "left" if left_gap <= right_gap else "right"
        self.move(self._edge_x(side, geo), min(max(self.y(), geo.top()),
                                               max(geo.top(), geo.bottom() - COLLAPSED_H)))
        self.app.store.set_meta("stowed_side", side)
        if not self._collapsed:
            self.toggle_collapse()
        return side

    def stow_to_edge(self) -> str:
        """贴到最近的那条边并收起 (点 － 或托盘菜单都会走这里)。"""
        geo = self._screen_geo()
        middle = geo.center().x()
        side = "left" if self.frameGeometry().center().x() < middle else "right"
        self.move(self._edge_x(side, geo), self.y())
        self.app.store.set_meta("stowed_side", side)
        if not self._collapsed:
            self.toggle_collapse()
        return side

    # ---- 分类 / 排序 (PRD 2.5)
    def _sync_group_button(self) -> None:
        short = {"time": "⇅时间", "level": "⇅重要", "date": "⇅日期"}
        self.btn_group.setText(short.get(self.group_mode, "⇅时间"))
        self.btn_group.setToolTip(f"任务分类方式：{GROUP_MODE_LABELS.get(self.group_mode, '按时间')}。"
                                  "点一下切换（按时间 / 按重要程度 / 按开始日期）。")

    def cycle_group_mode(self) -> str:
        """切下一个分类方式并落盘 —— 用户选过一次, 下次打开还是它。"""
        order = list(GROUP_MODES)
        self.group_mode = order[(order.index(self.group_mode) + 1) % len(order)]
        self.app.store.set_meta("task_group", self.group_mode)
        self._sync_group_button()
        self.refresh(force=True)
        self._notify(f"任务列表改为{GROUP_MODE_LABELS[self.group_mode]}")
        return self.group_mode

    def set_group_mode(self, mode: str) -> None:
        self.group_mode = mode if mode in GROUP_MODES else "time"
        self.app.store.set_meta("task_group", self.group_mode)
        self._sync_group_button()
        self.refresh(force=True)

    # ------------------------------------------------------------ 翻页 (今日 + 历史分页)
    def _make_page(self) -> NotePage:
        """造一页纸并接好信号。

        2.5.2 起页数不再固定为 2 页: 历史按 HIST_PER_PAGE 条一页, 有多少历史就有多少页
        (客户反馈: "历史事件会堆在一页, 请添加多页")。所以建页这件事要能重复调用。
        """
        pg = NotePage(self)
        pg.setVisible(False)
        pg.advanced.connect(self._open_advanced)
        pg.snooze.connect(self._snooze)
        pg.done.connect(self._complete)
        pg.more.connect(self._popup_task_menu)
        pg.toggle_children.connect(self._toggle_children)
        self._place_page(pg)
        return pg

    def _place_page(self, pg: NotePage) -> None:
        page_h = self.height() if self._collapsed else max(60, self.height() - INPUT_H - 6)
        pg.setGeometry(0, 0, self.width(), page_h)

    def _ensure_page_count(self, n: int) -> None:
        while len(self.pages) < n:
            self.pages.append(self._make_page())

    def _visible_items(self, items: list[dict]) -> list[dict]:
        """把被收起的二级目录里的子任务摘掉, 并给父行标上展开状态。"""
        kids: dict[str, int] = {}
        for it in items:
            pu = it.get("parent_uuid") or ""
            if pu:
                kids[pu] = kids.get(pu, 0) + 1
        out = []
        for it in items:
            if it.get("depth"):
                if (it.get("parent_uuid") or "") in self._collapsed_uuids:
                    continue
            else:
                it = dict(it)
                it["expanded"] = (it.get("uuid") or "") not in self._collapsed_uuids
                it["child_count"] = kids.get(it.get("uuid") or "", it.get("child_count", 0))
            out.append(it)
        return out

    def _toggle_children(self, rid: int) -> None:
        rem = self.app.store.get_reminder(rid)
        if rem is None or not rem.uuid:
            return
        if rem.uuid in self._collapsed_uuids:
            self._collapsed_uuids.discard(rem.uuid)
        else:
            self._collapsed_uuids.add(rem.uuid)
        self.refresh(force=True)

    def refresh(self, force: bool = False) -> None:
        active = self._visible_items(self.app.life.board(mode=self.group_mode)["active"])
        hist_total = self.app.life.history_count()
        hist_pages = max(1, (hist_total + HIST_PER_PAGE - 1) // HIST_PER_PAGE)
        if self.page_index > hist_pages:
            self.page_index = hist_pages          # 历史变少(归档清理)时别停在空页上
        hist = self._hist_items(self.page_index)  # 只取当前这一页, 不把几百条历史全捞进来
        fp = (self.group_mode, self.page_index, hist_total,
              tuple((d["id"], d["title"], d["fired"], d["total"], str(d["next_fire"]),
                     d["level"], d.get("depth"), d.get("child_count"), d.get("expanded"),
                     d.get("duration_min"), d.get("group_label")) for d in active)
              + tuple((h["id"], h["status"], h["title"]) for h in hist))
        if fp == self._last_fingerprint and not force:
            return                                    # 没变化就不重绘
        self._last_fingerprint = fp
        self._hist_total = hist_total
        self._hist_pages = hist_pages
        self._ensure_page_count(hist_pages + 1)
        for i, pg in enumerate(self.pages):
            pg.set_items(active if i == 0 else (hist if i == self.page_index else []))
            pg.setVisible(i == self.page_index and not self._collapsed)
        self._sync_pager()
        self.setWindowTitle(f"智能手账 · {len([d for d in active if not d.get('depth')])} 条")
        self.update()

    def _hist_items(self, page_index: int) -> list[dict]:
        """历史第 `page_index` 页 (1 起) 的条目; 第 0 页是"今日手账", 没有历史条目。"""
        if page_index <= 0:
            return []
        rows = self.app.life.history(HIST_PER_PAGE, (page_index - 1) * HIST_PER_PAGE)
        return [{"id": r.id, "title": r.title, "level": r.level, "event_at": r.event_at,
                 "next_fire": None, "fired": 0, "total": len(r.plan.offsets),
                 "status": r.status} for r in rows]

    def page_label(self) -> str:
        """左下角的分页指示: 第 0 页是今日, 之后是历史各页。"""
        if self.page_index == 0:
            return "今日" if self._hist_total == 0 else f"今日 · 历史{self._hist_pages}页"
        return f"历史 {self.page_index}/{self._hist_pages}"

    def _sync_pager(self) -> None:
        if hasattr(self, "page_label_widget"):
            self.page_label_widget.setText(self.page_label())
            self.btn_prev_page.setEnabled(self.page_index > 0)
            self.btn_next_page.setEnabled(self.page_index < self._hist_pages)

    # ------------------------------------------------------------ 绘制
    def paintEvent(self, ev):                        # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        dpr = self.devicePixelRatioF()
        h = COLLAPSED_H if self._collapsed else self.height()
        # 阴影
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(*theme.SHADOW))
        p.drawRoundedRect(QRectF(3, 4, self.width() - 6, h - 6), 6, 6)
        pm = self.paper.get(self.width(), h, dpr)
        p.drawPixmap(0, 0, pm)

        p.setPen(QPen(QColor(theme.INK_LIGHT)))
        p.setFont(theme.handwriting_font(12, bold=True))
        title = ("今日手账" if self.page_index == 0
                 else f"已翻过去的页 {self.page_index}/{self._hist_pages}")
        p.drawText(QRect(theme.PADDING[0], 6, 180, 22), Qt.AlignLeft | Qt.AlignVCenter, title)
        p.setFont(theme.handwriting_font(10, bold=False))
        # 日期挪到标题右边: 右上角那排是 ＋/⚙/－/× 按钮, 半透明按钮底会让日期像乱码一样透出来
        stamp = datetime.now().strftime("%m-%d %H:%M") + ("  · 已收起" if self._collapsed else "")
        p.drawText(QRect(theme.PADDING[0] + 86, 6, 130, 22), Qt.AlignLeft | Qt.AlignVCenter, stamp)
        p.end()

    def resizeEvent(self, ev):                       # noqa: N802
        if hasattr(self, "tools"):
            self._layout_parts()
        if not self._collapsed and hasattr(self, "_user_size"):
            self._user_size = QSize(self.width(), self.height())
        # 便签内容区要给底部的输入行让出位置, 否则最后一行会被输入框压住
        for pg in self.pages:
            self._place_page(pg)
        self.flipper.setGeometry(0, 0, self.width(), self.height())
        super().resizeEvent(ev)

    def _switch_page(self) -> None:
        """翻到下一页/上一页 (动画走到一半时由 FlipController 调, 没有参数)。"""
        step = 1 if getattr(self, "_pending_dir", 1) >= 0 else -1
        target = max(0, min(self._hist_pages, self.page_index + step))
        self.pages[self.page_index].setVisible(False)
        self.page_index = target
        self._ensure_page_count(target + 1)
        self.pages[target].setVisible(not self._collapsed)
        self.refresh()                    # 历史页是"按页取的", 换页必须重新铺这一页
        self.update()

    def turn_page(self, direction: int = 1) -> None:
        step = 1 if direction >= 0 else -1
        target = self.page_index + step
        if not 0 <= target <= self._hist_pages:
            return                        # 到头了: 第 0 页是今日, 后面是历史各页
        self._pending_dir = step
        if self.flip.mode == "fade":
            self._switch_page()
            return
        self.flipper.direction = direction
        self.flipper.show()
        self.flip.on_frame = self._on_flip_frame
        self.flip.turn(direction)

    def _on_flip_frame(self, frames) -> None:
        if frames:
            self.flipper.progress = min(1.2, self.flip.currentTime() / max(1, self.flip.duration()))
            self.flipper.update()
        if self.flip.state() != QVariantAnimation.Running:
            self.flipper.hide()

    # ------------------------------------------------------------ 交互
    def _in_grip(self, pos: QPoint) -> bool:
        """右下角那 18×18 是"拖大小"的把手。"""
        return (not self._collapsed and pos.x() >= self.width() - GRIP_W
                and pos.y() >= self.height() - GRIP_W)

    def mousePressEvent(self, ev):                   # noqa: N802
        if ev.button() != Qt.LeftButton:
            return super().mousePressEvent(ev)
        pos = ev.position().toPoint()
        if self._collapsed:
            # 收起状态就是屏幕边上那一条: 点一下展开 —— 这是"贴边隐藏"之后唯一的拿法
            self.toggle_collapse()
            ev.accept()
            return
        if self._in_grip(pos):
            self._resizing = pos
            self.setCursor(Qt.SizeFDiagCursor)
            ev.accept()
            return
        self._drag = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
        ev.accept()

    def mouseMoveEvent(self, ev):                    # noqa: N802
        if self._resizing and ev.buttons() & Qt.LeftButton:
            pos = ev.position().toPoint()
            self.resize(max(MIN_W, self.width() + pos.x() - self._resizing.x()),
                        max(MIN_H, self.height() + pos.y() - self._resizing.y()))
            ev.accept()
            return
        if self._drag and ev.buttons() & Qt.LeftButton:
            self.move(ev.globalPosition().toPoint() - self._drag)
            ev.accept()

    def mouseReleaseEvent(self, ev):                 # noqa: N802
        if self._resizing:
            self._resizing = None
            self.unsetCursor()
            self._user_size = QSize(self.width(), self.height())
            self.app.store.set_meta("window_size", f"{self.width()},{self.height()}")
        if self._drag:
            self._drag = None
            self.app.store.set_meta("window_pos", f"{self.x()},{self.y()}")
            self.apply_edge_snap()                   # 拖到屏幕边上就自动贴边收起
        super().mouseReleaseEvent(ev)

    def wheelEvent(self, ev):                        # noqa: N802
        self.turn_page(1 if ev.angleDelta().y() < 0 else -1)

    def keyPressEvent(self, ev):                     # noqa: N802
        if ev.key() in (Qt.Key_Left, Qt.Key_PageUp):
            self.turn_page(-1)
        elif ev.key() in (Qt.Key_Right, Qt.Key_PageDown, Qt.Key_Space):
            self.turn_page(1)
        elif ev.key() == Qt.Key_Escape:
            self.hide()
        super().keyPressEvent(ev)

    def toggle_collapse(self) -> None:
        self._collapsed = not self._collapsed
        if self._collapsed:
            self._expanded_h = self.height()
            self.setFixedHeight(COLLAPSED_H)
        else:
            w = max(MIN_W, self._user_size.width() or NOTE_W)
            h = max(MIN_H, self._user_size.height() or getattr(self, "_expanded_h", NOTE_H))
            self.setMinimumHeight(MIN_H)
            self.setMaximumHeight(16777215)          # 解开收起时用的固定高度
            self.resize(w, h)
        self.pages[self.page_index].setVisible(not self._collapsed)
        self.editor.setVisible(not self._collapsed)
        self.tools.setVisible(not self._collapsed)
        if hasattr(self, "page_pager"):
            self.page_pager.setVisible(not self._collapsed)
        if hasattr(self, "btn_group"):
            self.btn_group.setVisible(not self._collapsed)
        self.app.store.set_meta("collapsed", "1" if self._collapsed else "0")
        if hasattr(self, "tools"):
            self._layout_parts()
        self.update()

    def is_collapsed(self) -> bool:
        return self._collapsed

    def toggle_click_through(self) -> bool:
        on = not self.testAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, on)
        self.app.store.set_meta("click_through", "1" if on else "0")
        return on

    # ---- 托盘
    def _build_tray(self) -> None:                   # pragma: no cover - 需要桌面
        from PySide6.QtGui import QIcon, QPixmap

        from ..autostart import Autostart

        self.autostart = Autostart(data_dir=Path(self.app.store.path).parent)
        pix = QPixmap(32, 32)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(theme.PAPER))
        p.setPen(QPen(QColor(theme.INK_LIGHT)))
        p.drawRoundedRect(QRectF(4, 3, 24, 26), 3, 3)
        p.drawLine(8, 12, 24, 12)
        p.drawLine(8, 18, 24, 18)
        p.end()

        try:
            self.tray = QSystemTrayIcon(QIcon(pix), self)
        except Exception:                            # 无托盘的环境 (远程/无头) 不能拖垮主界面
            self.tray = None
            return
        self.tray.setToolTip("智能手账提醒")
        menu = QMenu()
        self.act_autostart = QAction("开机自动启动", self)
        self.act_autostart.setCheckable(True)
        self.act_autostart.setChecked(self.autostart.is_enabled())
        self.act_autostart.triggered.connect(self._toggle_autostart)
        acts = [
            ("显示 / 收起便签", self.toggle_collapse),
            ("贴边收起", self.stow_to_edge),
            ("立即圈选识别 (Alt+Q)", self._start_scan),
            ("翻到上一页", lambda: self.turn_page(-1)),
            ("翻到下一页", lambda: self.turn_page(1)),
            ("切换任务分类方式 (时间/重要程度/日期)", self.cycle_group_mode),
            ("账户与设备绑定… (识别码 / 二维码 / 已绑定手机)", self._open_settings),
            ("设置… (等级/频率/免打扰)", self._open_settings),
            ("切换鼠标穿透", self._on_click_through),
        ]
        for text, slot in acts:
            a = QAction(text, self)
            a.triggered.connect(slot)
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(self.act_autostart)
        menu.addSeparator()
        quit_act = QAction("退出", self)
        quit_act.triggered.connect(QApplication.instance().quit)
        menu.addAction(quit_act)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self.toggle_collapse()
                                    if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()

    def _start_scan(self) -> None:
        """打开冻结层。**必须把对象存住** —— 这行原来是 `self.overlay_cls(self.app).start()`:

        冻结层是个没有父对象的顶层窗口, 用临时值接住的话, start() 一返回 Python 就把它回收,
        窗口一闪即灭。实测表现就是"按了 Alt+Q 什么都没发生, 日志也没有任何异常" —— 本项目的
        圈选入口因此从来没真正生效过。
        """
        if self.overlay is not None:
            return                                    # 已经在圈选中, 忽略重复触发
        # 没有 OCR 后端时**不要**再弹一个什么都做不了的全屏层 (客户反馈:"黑窗口不美观" +
        # "系统提示没装 OCR, 但识别窗口还在"): 直接说清原因和怎么修。
        if not self.scan_available():
            # 提示要"能据此行动": 说清原因 (哪个解释器缺什么) + 一句修法 + 不依赖 OCR 的替代路径
            self._notify(f"圈选识别用不了：{self.ocr_problem()}\n"
                         "一句命令就能修好：python -m reminder_assistant --install-ocr；\n"
                         "在那之前，可以直接在便签下面的输入行写一句"
                         "（如「明天下午三点开会」）回车添加。")
            self._surface_note()
            return
        log.info("打开冻结层 (GUI 线程: %s)", threading.current_thread().name)
        try:
            # 压暗与否由设置决定 (设置面板里那个开关): 关掉就是"看不见的全屏层, 只有一条选框"
            dim = bool(getattr(getattr(self.app, "settings", None), "scan_dim", True))
            overlay = self.overlay_cls(self.app, dim=dim)
            overlay.destroyed.connect(lambda *_: setattr(self, "overlay", None))
            # 识别结果一定要回到便签上给反馈 (以前这里什么都不接, 用户看到的是"没反应")
            if hasattr(overlay, "picked"):
                overlay.picked.connect(self._on_scan_result)
            if hasattr(overlay, "scanning"):
                overlay.scanning.connect(self._on_scan_started)
            self.overlay = overlay
            overlay.start()
        except Exception:
            # 热键/托盘都走这条路: 异常必须就地吞掉并降级, 否则按 Alt+Q 会毫无反应
            self.overlay = None
            log.exception("打开冻结层失败")
            self._notify("圈选失败, 详见日志")

    # ---- 圈选结果反馈 (PRD 2.1 "智能创建与删除")
    def _surface_note(self) -> None:
        """把便签弄到用户眼前: 收起就展开, 藏进托盘就显示出来。"""
        if not self.isVisible():
            self.show()
        if self._collapsed:
            self.toggle_collapse()
        self.raise_()
        self.activateWindow()

    def _on_scan_started(self) -> None:
        """松手之后马上说一句"正在识别": OCR 第一次要加载模型, 静默等几秒最像"没反应"。"""
        self._notify("正在识别圈中的文字…（第一次会慢几秒，之后就快了）")

    def _on_scan_result(self, outcome) -> None:
        """把一次圈选的结论变成用户看得见的动作: 提示 + 刷新 + (必要时) 预填输入行。

        这是客户反馈第 2 条("ocr识别后没有反馈, 也没有在弹窗上新建事件")的落点:
        识别成功要当场看到卡片和一句话; 识别到字但没有时间要能接着改; 彻底失败要说清原因。
        """
        action = getattr(outcome, "action", "")
        try:
            if action == "cancelled":
                return
            if action == "created":
                self._surface_note()
                self.refresh(force=True)
                self._notify(f"已记上：{outcome.title}  ·  {outcome.when}")
                return
            if action == "deleted":
                self.refresh(force=True)
                self._notify(f"已划掉 {outcome.count} 条：{outcome.title}")
                return
            if action == "noise":
                # 有候选时间、但都像时间戳/时刻表: 说清读到了什么、为什么不建卡, 并给两条能走的路
                self._surface_note()
                self._notify(f"{outcome.reason}\n"
                             "只框住那一句话再圈一次，或直接在输入行里写一句"
                             "（例如「明天8点吃饭」「10月20日 13:35 出发去丹阳」）回车添加。")
                return
            if action == "manual":
                # 认到了字但没有时间: 放进输入行让用户补一个, 比"什么都不发生"有用得多
                self._surface_note()
                if outcome.text and not self.editor.text().strip():
                    self.editor.setText(outcome.text)
                    self.editor.setFocus()
                    self.editor.selectAll()
                self._notify(f"识别到「{outcome.text[:24]}」，但里面没有时间。\n"
                             "已放进便签的输入行，补个时间（如「明天下午三点」）回车即可；"
                             "也可以在输入行里直接改。")
                return
            if action == "failed":
                self._surface_note()
                self._notify(f"圈选没成功：{outcome.reason}\n"
                             "可以直接在便签输入行里写一句（如「明天下午三点开会」）回车添加。")
                return
        except Exception:                            # noqa: BLE001 - 反馈路径出错也不该砸掉便签
            log.exception("圈选结果反馈失败")

    # ---- 卡片上的三个动作
    def _complete(self, rid: int, index: int = 0) -> None:
        self.app.life.complete(rid)
        row = self.pages[self.page_index].row(rid)
        if row is not None:
            row.strike_out()            # 先划掉再随下次刷新消失, 给用户一个"已处理"的反馈
        QTimer.singleShot(320, lambda: self.refresh(force=True))

    def _snooze(self, rid: int, index: int = 0) -> None:
        try:
            when = self.app.life.snooze(rid, index, minutes=5)
            self._notify(f"已推迟到 {when:%H:%M}")
        except Exception as exc:                     # noqa: BLE001
            self._notify(f"推迟失败: {exc}")
        self.refresh(force=True)

    def _exec_dialog(self, dlg) -> int:
        """模态执行对话框。

        单独抽成方法同样是为了可测: 真实 exec() 会开一个模态事件循环,
        在无头环境下没有用户去关闭它 —— 会让整轮测试永久挂住 (实测踩过, 卡了 250 秒)。

        走 ui.dialogs: 便签是 WS_EX_TOPMOST 的 Tool 窗口, 对话框必须**自己**也置顶,
        否则它永远待在便签下面 (客户反馈第 1 条: "模态窗口固定在悬浮窗后, 被遮盖")。
        """
        from .dialogs import present

        present(dlg, self)
        return dlg.exec()

    def _open_advanced(self, rid: int) -> None:
        """PRD 的"高级事件"面板: 改这一条提醒的**事件属性**(名称/时间/等级/持续时间/备注)
        与提醒频率, 当场重排并刷新便签。"""
        from PySide6.QtWidgets import QDialog

        from .settings_dialog import SettingsDialog

        rem = self.app.store.get_reminder(rid)
        if rem is None:
            return
        dlg = SettingsDialog(self.app, reminder=rem)
        if self._exec_dialog(dlg) == QDialog.Accepted:
            self.refresh(force=True)
            if dlg.result_attributes.get("title"):
                self._notify(f"事件属性已更新：{dlg.result_attributes['title']}")
            if dlg.result_plan is not None:
                self._notify(f"提醒频率已更新: {len(dlg.result_plan.offsets)} 次")

    # ------------------------------------------------------------ ⋯ 菜单 / 二级目录
    def _task_menu(self, rid: int):
        """一行任务上的"更多"菜单。抽成独立方法是为了能在测试里直接拿到菜单检查项。"""
        rem = self.app.store.get_reminder(rid)
        menu = QMenu(self)
        if rem is None:
            return menu
        act_attr = menu.addAction("事件属性…（名称/时间/重要程度/持续时间/备注）")
        act_attr.triggered.connect(lambda: self._open_advanced(rid))
        if rem.depth < MAX_DEPTH:
            act_sub = menu.addAction("创建子任务…（向导）")
            act_sub.triggered.connect(lambda: self._open_subtask_wizard(rid))
        n = self.app.store.count_children(rem.uuid)
        if n:
            expanded = rem.uuid not in self._collapsed_uuids
            act_toggle = menu.addAction(("收起" if expanded else "展开") + f"二级目录（{n} 个子任务）")
            act_toggle.triggered.connect(lambda: self._toggle_children(rid))
        menu.addSeparator()
        act_del = menu.addAction("删除这条（连带子任务）")
        act_del.triggered.connect(lambda: self._delete_reminder(rid))
        return menu

    def _popup_task_menu(self, rid: int) -> None:
        menu = self._task_menu(rid)
        row = self.pages[self.page_index].row(rid)
        pos = row.mapToGlobal(QPoint(row.width() - 24, row.height())) if row else self.pos()
        try:
            self._exec_menu(menu, pos)
        except Exception:                       # noqa: BLE001 - 菜单打不开不该砸掉便签
            log.exception("弹出任务菜单失败")

    def _exec_menu(self, menu, pos):
        """菜单的模态执行。同样抽成接缝: 无头环境下 exec() 会一直等用户点。

        顺带置顶: 便签自己带 WS_EX_TOPMOST, 挂在它上面的弹出菜单如果不显式置顶,
        会被便签压住 —— 表现就是"点了 ⋯ 什么也没出现"。
        """
        try:
            menu.setWindowFlags(menu.windowFlags() | Qt.WindowStaysOnTopHint)
        except Exception:                       # noqa: BLE001 - 设不上也要能弹
            log.debug("菜单置顶设置失败", exc_info=True)
        return menu.exec(pos)

    def _confirm(self, title: str, text: str) -> bool:
        """删除前的确认。接缝同上 (无头环境不能弹模态框)。

        同样必须置顶: 确认框被便签盖住时, 用户点"删除"之后什么也看不到, 只会以为删掉了。
        """
        from .dialogs import ask

        return ask(self, title, text)

    def _delete_reminder(self, rid: int) -> None:
        rem = self.app.store.get_reminder(rid)
        if rem is None:
            return
        n = self.app.store.count_children(rem.uuid)
        extra = f"（连同 {n} 个子任务一起）" if n else ""
        if not self._confirm("删除提醒", f"删除「{rem.title}」{extra}？删除后不可恢复。"):
            return
        self.app.life.remove(rid)
        self._notify(f"已删除：{rem.title}")
        self.refresh(force=True)

    def _open_subtask_wizard(self, rid: int) -> None:
        """向导式创建子任务: 先问几个, 再一个一个填 (PRD 2.5)。"""
        from PySide6.QtWidgets import QDialog

        from .subtask_wizard import CountDialog, SubTaskWizardDialog, resume_state

        parent = self.app.store.get_reminder(rid)
        if parent is None:
            return
        if parent.depth >= MAX_DEPTH:
            self._notify("只支持二级目录：子任务下面不能再建子任务")
            return
        state = resume_state(self.app, parent)
        if state is None:
            count_dlg = CountDialog(parent.title, default=3)
            if self._exec_dialog(count_dlg) != QDialog.Accepted:
                return
            from ..tasks import WizardState

            state = WizardState.create(count_dlg.count(), start_at=parent.event_at,
                                       parent_title=parent.title)
        dlg = SubTaskWizardDialog(self.app, parent, state=state)
        if self._exec_dialog(dlg) == QDialog.Accepted:
            created = dlg.created
            self._notify(f"已在「{parent.title}」下创建 {len(created)} 个子任务，"
                         f"到点会照常提醒")
            self._collapsed_uuids.discard(parent.uuid)
            self.refresh(force=True)

    def _open_settings(self) -> None:
        from .settings_dialog import SettingsDialog

        # 不给 parent: 对话框不挂在便签下面 (客户反馈: 挂在悬浮窗下的模态框永远被它压住),
        # 置顶与居中由 ui.dialogs 统一负责。
        self._exec_dialog(SettingsDialog(self.app))

    def _on_click_through(self) -> None:
        on = self.toggle_click_through()
        self._notify("鼠标穿透已" + ("开启" if on else "关闭"))

    def warn_hotkey(self, message: str) -> None:
        """热键没注册上时告诉用户去哪儿修。

        这里刻意不用模态框: 它在启动路径上, 卡住窗口比少一条提示更糟。托盘气泡 + 日志足够,
        而且设置面板里本来就会实时显示组合键能不能用 —— 用户能当场换一个。
        """
        log.warning("热键不可用: %s", message)
        self._notify(f"圈选快捷键没注册上: {message}\n可在托盘菜单的「设置…」里换一个组合键。")

    def _notify(self, text: str) -> None:
        """托盘气泡。整体容错: 它会在 closeEvent 里被调用, 抛异常会让窗口关不掉。"""
        tray, show = getattr(self, "tray", None), None
        try:
            show = getattr(tray, "showMessage", None)
            if show:
                show("智能手账", text)
        except Exception:
            log.debug("托盘气泡发送失败", exc_info=True)

    def _toggle_autostart(self) -> None:
        """托盘里切开机自启。失败必须回滚勾选状态并说清原因。"""
        from ..autostart import AutostartError

        try:
            on = self.autostart.toggle()
            self.act_autostart.setChecked(on)
            self._notify("开机自启已" + ("开启" if on else "关闭"))
        except AutostartError as exc:
            self.act_autostart.setChecked(self.autostart.is_enabled())
            self._notify(f"开机自启设置失败: {exc}")

    # ---- 状态持久化
    def _restore_state(self) -> None:
        meta = self.app.store.get_meta
        geo = self._screen_geo()
        size = meta("window_size")
        if size:
            try:
                w, h = (int(v) for v in size.split(","))
                self._user_size = QSize(max(MIN_W, w), max(MIN_H, h))
                self.resize(self._user_size)
            except ValueError:
                pass
        pos = meta("window_pos")
        if pos:
            try:
                x, y = (int(v) for v in pos.split(","))
                self.move(x, y)
            except ValueError:
                self.move(geo.right() - NOTE_W - 24, geo.top() + 60)
        else:
            self.move(geo.right() - NOTE_W - 24, geo.top() + 60)
        side = meta("stowed_side")
        if side in ("left", "right"):                # 上次贴边收着的, 原位还回去
            self.move(self._edge_x(side, geo),
                      max(geo.top(), min(self.y(), max(geo.top(), geo.bottom() - COLLAPSED_H))))
        if meta("collapsed") == "1":
            self.toggle_collapse()
        if meta("click_through") == "1":
            self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # 免打扰时段由 Settings 管 (app.serve 里已装配到 center), 不再读 meta 里的旧键,
        # 避免设置面板与旧开关两个真相来源互相覆盖
        self.flip.mode = meta("flip_mode", "flip") or "flip"
        # 任务分类方式 (PRD 2.5): 上次选的排序方式要还在, 默认严格按时间
        self.group_mode = meta("task_group", DEFAULT_GROUP_MODE) or DEFAULT_GROUP_MODE
        if self.group_mode not in GROUP_MODES:
            self.group_mode = DEFAULT_GROUP_MODE
        self._sync_group_button()


    def minimize_on_close(self) -> bool:
        """点关闭时是否应该收进托盘 (有托盘就收, 没有就真退出)。"""
        return getattr(self, "tray", None) is not None and QSystemTrayIcon.isSystemTrayAvailable()

    def closeEvent(self, ev):                        # noqa: N802
        if self.minimize_on_close():
            self.hide()
            self._notify("已收进托盘, 提醒继续运行")
            ev.ignore()
            return
        self.app.store.set_meta("flip_mode", self.flip.mode)
        self.timer.stop()
        super().closeEvent(ev)
        # 没有托盘时, "关闭"必须是真退出 —— 否则窗口没了、进程还在后台, 用户再也找不回来
        app = QApplication.instance()
        if app is not None:
            app.quit()
