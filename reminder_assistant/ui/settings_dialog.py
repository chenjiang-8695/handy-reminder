"""设置面板 (PRD 2.3)。

两种用法共用一个类:
  * 全局设置 (托盘 → 设置…): 改等级配方、免打扰时段、热键、开机自启
  * 单条提醒的"高级事件" (卡片上的按钮): 只改这一条的提醒频率, 并立刻重排

PRD 里最容易被做丢的一点是**"用户设完要知道到底会在什么时候响"**。所以这个面板的核心
不是那几个数字输入框, 而是下面那行实时预览: 用户改一次次数/间隔, 立刻看到
"14:00 → 14:20 → 14:40 (共 3 次)", 而不是等保存之后回便签上看。
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from PySide6.QtCore import QDateTime, QEvent, Qt, QTime
from PySide6.QtWidgets import (QAbstractSpinBox, QCheckBox, QComboBox, QDateTimeEdit, QDialog,
                               QDialogButtonBox, QFormLayout, QFrame, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QSpinBox,
                               QTimeEdit, QVBoxLayout, QWidget)

from ..models import Reminder, ReminderPlan
from ..settings import MAX_MINUTES, MAX_REPEAT, LevelSpec, Settings

log = logging.getLogger("reminder.ui.settings")

WEEK_MINUTES = 7 * 24 * 60
MAX_DURATION_MIN = 24 * 60


def _spin(lo: int, hi: int, value: int, suffix: str = "") -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(value)
    if suffix:
        s.setSuffix(suffix)
    s.setFixedWidth(96)
    return s


def to_qdatetime(dt: datetime) -> QDateTime:
    """aware datetime -> QDateTimeEdit 能显示的值 (按本机时区)。

    控件只认本地时刻, 所以先 astimezone() 再摘掉 tzinfo —— 直接把 aware 值塞进去,
    在不同时区/夏令时下会悄悄偏移一小时, 用户改完时间看到的提醒就不在点上。
    """
    local = dt.astimezone().replace(tzinfo=None)
    return QDateTime(local)


def from_qdatetime(qdt: QDateTime, like: datetime | None) -> datetime:
    """QDateTimeEdit 的值 -> aware datetime (沿用原来的时区)。"""
    naive = qdt.toPython()
    if naive.tzinfo is not None:
        return naive
    tz = (like.tzinfo if like is not None else None) or datetime.now().astimezone().tzinfo
    return naive.replace(tzinfo=tz)


class SettingsDialog(QDialog):
    def __init__(self, app, reminder: Reminder | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.app = app
        self.settings: Settings = app.settings.copy()
        self.reminder = reminder
        self.result_plan: ReminderPlan | None = None
        self.result_attributes: dict = {}      # 这次真的改到的属性 (供界面提示)

        self.setWindowTitle("事件属性 · 提醒频率" if reminder else "设置")
        self.setMinimumWidth(430)

        # 面板内容比屏幕还长 (账户模块 + 等级表 + 自定义 + 免打扰 + 常规), 所以要能滚:
        # 内容进 QScrollArea, 底部的按钮留在外面 —— 滚到哪儿都点得到"保存/取消"。
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)   # 只上下滚
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(6)

        if reminder:
            head = QLabel(f"「{reminder.title or '未命名'}」\n事件时间 {reminder.event_at:%Y-%m-%d %H:%M}")
            head.setStyleSheet("color:#5A5245;")
            self.lbl_head = head
            root.addWidget(head)
            self._build_attributes(root)

        self._build_levels(root)
        self._build_custom(root)
        if not reminder:
            # 账户与设备绑定 (PRD 2.4): 客户反馈"绑定功能没有实现, 设置面板上加一个账户模块",
            # 所以它跟等级/免打扰/快捷键一样是设置面板的一个模块, 而不是藏在托盘菜单里。
            self._build_account(root)
            self._build_quiet(root)
            self._build_general(root)
        root.addStretch(1)

        self.scroll_area.setWidget(content)
        outer.addWidget(self.scroll_area, 1)
        self._route_wheel_to_scroll()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("应用到这条提醒" if reminder else "保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        if not reminder:
            restore = QPushButton("恢复默认等级")
            restore.clicked.connect(self._restore_defaults)
            buttons.addButton(restore, QDialogButtonBox.ResetRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self._sync_preview()

    # ------------------------------------------------------------ 滚轮翻页
    def _route_wheel_to_scroll(self) -> None:
        """把输入控件上的滚轮事件改成"翻页", 而不是"顺手改数值"。

        这是设置面板最容易惹人生气的地方: 面板要滚动, 用户把鼠标停在哪就在哪滚 ——
        而 QSpinBox / QComboBox / QTimeEdit **默认吃掉滚轮并改自己的值**:
        用户以为在翻页, 实际上把「提醒次数」从 3 改成了 2、把「第一次提前」改成了别的数,
        而且大多数人不会回头检查每个数字。所以这些控件上的滚轮一律转发给滚动区。
        """
        for w in self.scroll_area.widget().findChildren(QWidget):
            if isinstance(w, (QAbstractSpinBox, QComboBox, QDateTimeEdit)):
                w.installEventFilter(self)
        # 滚动区自己也走同一套步长: 否则"在输入框上滚"和"在空白处滚"速度不一样,
        # 用户会觉得面板忽快忽慢。
        self.scroll_area.viewport().installEventFilter(self)

    def _wheel_step(self) -> int:
        """一格滚轮滚多少像素 (跟行高走, 高分屏/大字号下也不会"一格只挪一点点")。"""
        return max(24, self.fontMetrics().height() * 3)

    def eventFilter(self, obj, ev):                    # noqa: N802 - Qt 接口
        try:
            if ev.type() == QEvent.Wheel:
                delta = ev.angleDelta().y() or ev.pixelDelta().y()
                if delta:
                    bar = self.scroll_area.verticalScrollBar()
                    bar.setValue(bar.value() - int(delta * self._wheel_step() / 120))
                ev.accept()
                return True                            # 不往下传 = 输入框就不会改数值
        except Exception:                              # noqa: BLE001 - 异常不能逃出 Qt 虚函数
            log.debug("转发滚轮失败", exc_info=True)
        return super().eventFilter(obj, ev)

    # ------------------------------------------------------------ 事件属性 (2.5)
    def _build_attributes(self, root: QVBoxLayout) -> None:
        """单条提醒的"事件属性"面板。

        这是用户反馈里"对事件属性的设置功能我没有看到"那一项: 原来"高级"里只有提醒频率,
        标题/时间/等级/持续时间/备注都改不了 —— 建错了只能划掉重建。
        """
        rem = self.reminder
        box = QGroupBox("事件属性")
        form = QFormLayout(box)

        self.ed_title = QLineEdit(rem.title or "")
        self.ed_title.setPlaceholderText("这条提醒叫什么")
        form.addRow("名称", self.ed_title)

        seed_dt = rem.event_at or self.app.clock.now()
        self.dt_event = QDateTimeEdit(to_qdatetime(seed_dt))
        self.dt_event.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.dt_event.setCalendarPopup(True)
        self.dt_event.setFixedWidth(160)
        form.addRow("事件时间", self.dt_event)

        self.sp_duration = _spin(0, MAX_DURATION_MIN, int(rem.duration_min or 0), " 分钟")
        self.sp_duration.setToolTip("预计要花多久。0 = 不填；子任务用它顺推下一个子任务的开始时间。")
        form.addRow("持续时间", self.sp_duration)

        self.cb_attr_level = QComboBox()
        self.cb_attr_level.addItems(self.settings.level_names())
        self.cb_attr_level.setCurrentText(rem.level if rem.level in self.settings.levels
                                          else self.settings.level_names()[0])
        self.cb_attr_level.setToolTip("等级决定默认提醒频率与兜底清理时限")
        form.addRow("重要程度", self.cb_attr_level)

        self.ed_note = QLineEdit(rem.note or "")
        self.ed_note.setPlaceholderText("备注（可留空）")
        form.addRow("备注", self.ed_note)

        self.ed_title.textChanged.connect(self._on_attr_changed)
        self.dt_event.dateTimeChanged.connect(self._on_attr_changed)
        root.addWidget(box)
        self.box_attributes = box

    def _on_attr_changed(self, *_a) -> None:
        try:
            self.lbl_head.setText(f"「{self.ed_title.text() or '未命名'}」\n"
                                  f"事件时间 {self.collect_attributes()['event_at']:%Y-%m-%d %H:%M}")
        except Exception:                            # noqa: BLE001 - 纯显示, 出错不该影响改属性
            log.debug("更新标题预览失败", exc_info=True)
        self._sync_preview()

    def collect_attributes(self) -> dict:
        """面板上的事件属性 —— 只返回真的能写回库的字段。"""
        assert self.reminder is not None
        return {
            "title": self.ed_title.text().strip() or self.reminder.title,
            "event_at": from_qdatetime(self.dt_event.dateTime(), self.reminder.event_at),
            "level": self.cb_attr_level.currentText(),
            "duration_min": int(self.sp_duration.value()),
            "note": self.ed_note.text().strip(),
        }

    def attributes_changed(self) -> dict:
        """只挑出用户真改了的字段 —— 没改的字段也写一遍会平白触发一次重排。"""
        out = {}
        for k, v in self.collect_attributes().items():
            old = getattr(self.reminder, k, None)
            if isinstance(v, datetime) and isinstance(old, datetime):
                if abs((v - old).total_seconds()) > 1:
                    out[k] = v
            elif v != old:
                out[k] = v
        return out

    # ------------------------------------------------------------ 等级配方
    def _build_levels(self, root: QVBoxLayout) -> None:
        box = QGroupBox("事件等级 (每个等级绑定自己的提醒频率)")
        grid = QGridLayout(box)
        grid.addWidget(QLabel("等级"), 0, 0)
        for col, text in enumerate(("第一次提前", "后续间隔", "提醒次数"), start=1):
            grid.addWidget(QLabel(text), 0, col)

        self.level_widgets: dict[str, tuple[QSpinBox, QSpinBox, QSpinBox]] = {}
        for row, (name, spec) in enumerate(self.settings.levels.items(), start=1):
            grid.addWidget(QLabel(name), row, 0)
            lead = _spin(0, MAX_MINUTES, int(spec.lead.total_seconds() // 60), " 分")
            interval = _spin(0, MAX_MINUTES, int(spec.interval.total_seconds() // 60), " 分")
            repeat = _spin(1, MAX_REPEAT, spec.repeat, " 次")
            for col, w in enumerate((lead, interval, repeat), start=1):
                grid.addWidget(w, row, col)
                w.valueChanged.connect(self._sync_preview)
            self.level_widgets[name] = (lead, interval, repeat)
        self.box_levels = box
        root.addWidget(box)
        if self.reminder:
            box.setVisible(False)          # 单条提醒模式下, 等级表在这层没有意义

    # ------------------------------------------------------------ 自定义 (展开式)
    def _build_custom(self, root: QVBoxLayout) -> None:
        box = QGroupBox("高级事件 / 自定义")
        box.setCheckable(True)             # 勾上才展开 —— PRD 说的"展开式设置"
        box.setChecked(bool(self.reminder))
        box.toggled.connect(lambda _on: self._sync_preview())
        self.box_custom = box

        lay = QVBoxLayout(box)
        form = QFormLayout()
        seed = self._seed_spec()
        self.cb_level = QComboBox()
        self.cb_level.addItems(self.settings.level_names() + ["自定义"])
        self.cb_level.currentTextChanged.connect(self._on_level_picked)
        form.addRow("起始: 套用等级", self.cb_level)

        self.sp_repeat = _spin(1, MAX_REPEAT, seed.repeat, " 次")
        self.sp_lead = _spin(0, MAX_MINUTES, int(seed.lead.total_seconds() // 60), " 分钟")
        self.sp_interval = _spin(0, MAX_MINUTES, int(seed.interval.total_seconds() // 60), " 分钟")
        form.addRow("提醒次数", self.sp_repeat)
        form.addRow("第一次提前事件", self.sp_lead)
        form.addRow("之后每次间隔", self.sp_interval)
        for w in (self.sp_repeat, self.sp_lead, self.sp_interval):
            w.valueChanged.connect(self._sync_preview)
        lay.addLayout(form)

        self.lbl_preview = QLabel()
        self.lbl_preview.setWordWrap(True)
        self.lbl_preview.setStyleSheet(
            "background:rgba(255,255,255,0.6);border:1px solid #D9CFBC;border-radius:4px;"
            "padding:6px;color:#2E2A25;")
        lay.addWidget(self.lbl_preview)
        root.addWidget(box)

    def _seed_spec(self) -> LevelSpec:
        if self.reminder is not None and self.reminder.plan.offsets:
            offs = self.reminder.plan.offsets
            lead = -offs[0]
            interval = (offs[1] - offs[0]) if len(offs) > 1 else timedelta(minutes=15)
            return LevelSpec(lead=max(lead, timedelta(0)),
                             interval=max(interval, timedelta(0)), repeat=len(offs))
        return self.settings.custom

    # ------------------------------------------------------------ 账户与设备绑定 (2.4)
    def _build_account(self, root: QVBoxLayout) -> None:
        """设置面板里的"账户"模块。

        抽成一个独立控件 (ui/account_section.py) 是因为它要自己读写账户、自己刷新设备列表 ——
        塞进这个对话框的构造函数里会让"绑定"变成保存设置的一部分, 而绑定是即时动作:
        用户生成码、手机端绑上、面板当场就要看到设备。
        """
        from .account_section import AccountSection

        try:
            self.account_section = AccountSection(self.app, self)
            root.addWidget(self.account_section)
        except Exception:                            # noqa: BLE001 - 账户模块坏了不能连设置都打不开
            log.exception("账户模块构建失败")
            self.account_section = None
            warn = QLabel("账户模块加载失败，详见日志（不影响其它设置）。")
            warn.setStyleSheet("color:#B4553F;")
            root.addWidget(warn)

    # ------------------------------------------------------------ 免打扰 / 常规
    def _build_quiet(self, root: QVBoxLayout) -> None:
        box = QGroupBox("免打扰")
        lay = QVBoxLayout(box)
        self.chk_quiet = QCheckBox("在以下时段不弹通知 (改为时段结束后合并成一条摘要)")
        self.chk_quiet.setChecked(self.settings.quiet_enabled)
        lay.addWidget(self.chk_quiet)
        row = QHBoxLayout()
        row.addWidget(QLabel("从"))
        self.te_start = QTimeEdit(QTime(self.settings.quiet_start.hour, self.settings.quiet_start.minute))
        self.te_start.setDisplayFormat("HH:mm")
        row.addWidget(self.te_start)
        row.addWidget(QLabel("到"))
        self.te_end = QTimeEdit(QTime(self.settings.quiet_end.hour, self.settings.quiet_end.minute))
        self.te_end.setDisplayFormat("HH:mm")
        row.addWidget(self.te_end)
        row.addStretch(1)
        lay.addLayout(row)
        self.chk_fullscreen = QCheckBox("全屏程序 (游戏/放映/远程桌面) 运行时也不打扰")
        self.chk_fullscreen.setChecked(self.settings.quiet_on_fullscreen)
        lay.addWidget(self.chk_fullscreen)
        root.addWidget(box)

    def _build_general(self, root: QVBoxLayout) -> None:
        box = QGroupBox("常规")
        form = QFormLayout(box)
        self.ed_hotkey = QLineEdit(self.settings.hotkey)
        self.ed_hotkey.setPlaceholderText("alt+q")
        self.ed_hotkey.setToolTip("格式: 修饰键+按键, 如 alt+q / ctrl+alt+r / win+shift+f5")
        form.addRow("圈选快捷键", self.ed_hotkey)
        self.lbl_hotkey_state = QLabel()
        self.lbl_hotkey_state.setStyleSheet("color:#7A7267;")
        form.addRow("", self.lbl_hotkey_state)
        self.ed_hotkey.textChanged.connect(self._check_hotkey)
        self._check_hotkey(self.ed_hotkey.text())
        self.chk_scan_dim = QCheckBox("圈选识别时压暗屏幕（关掉后屏幕不变暗，只显示一条选框）")
        self.chk_scan_dim.setChecked(self.settings.scan_dim)
        self.chk_scan_dim.setToolTip(
            "刚打开圈选时那一层变暗的效果。关掉它就等于「看不见的识别层」：\n"
            "屏幕保持原样，只在你拖选的地方出现一条黄色选框。")
        form.addRow("", self.chk_scan_dim)
        self.chk_autostart = QCheckBox("开机自动启动")
        try:
            from ..autostart import Autostart

            self.chk_autostart.setChecked(
                Autostart(data_dir=self.app.store.path.parent).is_enabled())
        except Exception:
            self.chk_autostart.setEnabled(False)
        form.addRow("", self.chk_autostart)
        hint = QLabel("提示: 快捷键保存后立即生效, 不用重启；若新组合键已被其它软件占用, "
                      "保存时会提示你换一个, 原来的键不受影响。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#7A7267;")
        form.addRow("", hint)
        root.addWidget(box)

    def _check_hotkey(self, text: str) -> None:
        """边输入边判断组合键能不能用。

        格式错误当场说清楚, 比"点了保存才弹一个红叉"好 —— 尤其是这里还有一个
        "键被别的软件占用了"是校验不出来的, 只能等保存时由系统回答。
        """
        from ..hotkey import HotkeyError, parse_hotkey

        raw = (text or "").strip()
        if not raw:
            self.lbl_hotkey_state.setText("留空则沿用当前快捷键。")
            self.lbl_hotkey_state.setStyleSheet("color:#7A7267;")
            return
        try:
            parse_hotkey(raw)
        except HotkeyError as exc:
            self.lbl_hotkey_state.setText(str(exc))
            self.lbl_hotkey_state.setStyleSheet("color:#B4553F;")
        else:
            self.lbl_hotkey_state.setText(f"{raw.upper()} 格式可用, 保存后立即生效。")
            self.lbl_hotkey_state.setStyleSheet("color:#4C6B4F;")

    # ------------------------------------------------------------ 交互
    def _on_level_picked(self, name: str) -> None:
        if name == "自定义":
            return
        spec = self.settings.spec(name)
        self.sp_repeat.setValue(spec.repeat)
        self.sp_lead.setValue(int(spec.lead.total_seconds() // 60))
        self.sp_interval.setValue(int(spec.interval.total_seconds() // 60))

    def using_custom(self) -> bool:
        return self.box_custom.isChecked()

    def collect_spec(self) -> LevelSpec:
        return LevelSpec(lead=timedelta(minutes=self.sp_lead.value()),
                         interval=timedelta(minutes=self.sp_interval.value()),
                         repeat=self.sp_repeat.value())

    def collect_plan(self) -> ReminderPlan:
        return self.collect_spec().plan()

    def anchor_time(self) -> datetime:
        """预览用的锚点: 单条提醒模式下必须是**面板上刚改的时间**, 而不是库里的旧值。

        否则用户把事件从 15:00 改成 16:00, 预览还在显示 15:00 的那串时间 ——
        看完预览点保存, 提醒时间对不上, 属于这类面板最典型的"预览与实际不一致"。
        """
        if self.reminder is not None:
            if hasattr(self, "dt_event"):
                return from_qdatetime(self.dt_event.dateTime(), self.reminder.event_at)
            if self.reminder.event_at is not None:
                return self.reminder.event_at
        now = self.app.clock.now()
        return now.replace(hour=15, minute=0, second=0, microsecond=0)

    def preview_text(self) -> str:
        anchor = self.anchor_time()
        name = self.cb_level.currentText()
        if self.using_custom() or name not in self.settings.levels:
            spec, label = self.collect_spec(), "自定义"
        else:
            spec, label = self.settings.spec(name), f"等级「{name}」"
            if self.reminder is not None:
                label += " (可在右侧再微调)"
        try:
            plan = spec.plan()
        except ValueError as exc:
            return f"参数不合法: {exc}"
        shown = " → ".join(f"{(anchor + off):%m-%d %H:%M}" for off in plan.offsets[:8])
        tail = f" … 共 {len(plan.offsets)} 次" if len(plan.offsets) > 8 else f"  (共 {len(plan.offsets)} 次)"
        return f"{label}: 若事件在 {anchor:%m-%d %H:%M}\n将在 {shown}{tail}"

    def _sync_preview(self) -> None:
        if hasattr(self, "lbl_preview"):
            self.lbl_preview.setText(self.preview_text())

    def _restore_defaults(self) -> None:
        self.settings.reset_levels()
        for name, (lead, interval, repeat) in self.level_widgets.items():
            spec = self.settings.spec(name)
            lead.setValue(int(spec.lead.total_seconds() // 60))
            interval.setValue(int(spec.interval.total_seconds() // 60))
            repeat.setValue(spec.repeat)
        self._sync_preview()

    # ------------------------------------------------------------ 落地
    def collect_settings(self) -> Settings:
        """把面板上的改动写回一个 Settings 对象 (不改 app.settings, 由调用方决定何时生效)。"""
        s = self.settings
        for name, (lead, interval, repeat) in self.level_widgets.items():
            try:
                s.levels[name] = LevelSpec(lead=timedelta(minutes=lead.value()),
                                           interval=timedelta(minutes=interval.value()),
                                           repeat=repeat.value(),
                                           urgency=s.spec(name).urgency)
            except ValueError as exc:
                log.warning("等级 %s 参数被忽略: %s", name, exc)
        s.custom = self.collect_spec()
        if hasattr(self, "chk_quiet"):
            s.quiet_enabled = self.chk_quiet.isChecked()
            s.quiet_start = _to_time(self.te_start.time())
            s.quiet_end = _to_time(self.te_end.time())
            s.quiet_on_fullscreen = self.chk_fullscreen.isChecked()
            s.hotkey = (self.ed_hotkey.text().strip() or s.hotkey)
        if hasattr(self, "chk_scan_dim"):
            s.scan_dim = self.chk_scan_dim.isChecked()
        return s

    def accept(self) -> None:
        try:
            if self.reminder is not None:
                # 顺序不能反: 先落属性 (可能改了事件时间 -> 触发点重排),
                # 再按面板上的频率重排一次。反过来的话, 先按旧时间排好的触发点
                # 会被属性更新覆盖回去, 用户改的时间就不生效。
                changed = self.attributes_changed()
                if changed:
                    self.reminder = self.app.life.update_attributes(self.reminder.id, **changed)
                    self.result_attributes = changed
                self.result_plan = self._plan_for_reminder()
                self.app.life.change_plan(self.reminder.id, self.result_plan)
            else:
                new = self.collect_settings()
                self.app.apply_settings(new)
                self.settings = new
        except Exception as exc:                     # noqa: BLE001 - 界面上必须看到原因
            log.exception("设置保存失败")
            self.report_error(str(exc))
            return                                   # 保存失败就不关窗, 让用户能改
        super().accept()

    def report_error(self, message: str) -> None:
        """保存失败时的提示。

        单独抽成方法是为了让测试能替换掉它: 模态对话框在无头环境下会**永久阻塞**
        (没有用户去点确定), 之前就因为这个把测试跑死了 150 秒。
        走 ui.dialogs 是为了让它也**置顶** —— 否则提示框会被置顶的便签盖住, 用户看到的是
        "点了保存什么都没发生, 也没关窗"。
        """
        from .dialogs import warn

        warn(self, "保存失败", message)

    def _plan_for_reminder(self) -> ReminderPlan:
        """单条提醒模式: 勾了"高级/自定义"就用面板上的参数, 否则用选中的等级配方。"""
        if self.using_custom() or self.cb_level.currentText() not in self.settings.levels:
            return self.collect_plan()
        return self.settings.spec(self.cb_level.currentText()).plan()


def _to_time(qt: QTime) -> time:
    return time(qt.hour(), qt.minute(), qt.second())
