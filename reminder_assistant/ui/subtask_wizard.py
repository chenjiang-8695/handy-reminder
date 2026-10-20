"""向导式创建子任务 (PRD 2.5)。

交互照着 PRD 的原话做:
    第一个弹窗问"要建几个子任务" -> 然后一个接一个地填「名称 + 持续时间」,
    点「下一步」弹下一个 …… 直到点「完成」或「取消」。

同时把 PRD 自己列的第 4 条风险预警正面解决掉, 而不是留给用户:
  * 要建 10 个? 有「转普通列表视图」, 一屏填完再一次保存 (不用点 10 次下一步);
  * 中途按了 Esc / 关窗? 每一步都写进 draft 表, 下次打开原样恢复 (不会"前面填的全丢");
  * 关掉时**不写库**: 只有点「完成」才真的建子任务, 取消不会留下半截任务树。

对话框只负责把 tasks.WizardState 搬到控件上 —— 状态机与校验全在 tasks.py 里, 有单测。
"""

from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QSpinBox, QStackedWidget, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..tasks import DEFAULT_DURATION_MIN, MAX_WIZARD_COUNT, SubTaskSpec, WizardState

log = logging.getLogger("reminder.ui.subtask")

MAX_DURATION_MIN = 24 * 60


def draft_key(parent_uuid: str) -> str:
    return f"subtask:{parent_uuid}"


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%m-%d %H:%M") if dt else "时间未定"


class CountDialog(QDialog):
    """第一个弹窗: 问要创建几个子任务 (PRD 2.5 的原话就是"要创建几个子任务的询问")。"""

    def __init__(self, parent_title: str = "", default: int = 3, parent=None):
        super().__init__(parent)
        self.setWindowTitle("创建子任务")
        self.setMinimumWidth(320)
        lay = QVBoxLayout(self)
        head = QLabel(f"「{parent_title or '主任务'}」下要创建几个子任务？")
        head.setWordWrap(True)
        self.lbl_head = head
        lay.addWidget(head)
        row = QHBoxLayout()
        row.addWidget(QLabel("子任务个数"))
        self.sp_count = QSpinBox()
        self.sp_count.setRange(1, MAX_WIZARD_COUNT)
        self.sp_count.setValue(max(1, min(MAX_WIZARD_COUNT, default)))
        self.sp_count.setSuffix(" 个")
        row.addWidget(self.sp_count)
        row.addStretch(1)
        lay.addLayout(row)
        tip = QLabel("接下来会依次让你填每个子任务的名称与持续时间；"
                     "嫌一个个填麻烦，可以随时「转普通列表视图」一次填完。"
                     "中途关掉也不会丢，下次会接着上次的填。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#7A7267;")
        lay.addWidget(tip)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("开始")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def count(self) -> int:
        return int(self.sp_count.value())


class SubTaskWizardDialog(QDialog):
    """分步向导 + 列表视图, 共用同一份 WizardState。"""

    def __init__(self, app, reminder, state: WizardState | None = None, parent=None):
        super().__init__(parent)
        self.app = app
        self.reminder = reminder
        self.key = draft_key(reminder.uuid)
        self.state = state or WizardState.create(
            3, start_at=reminder.event_at, parent_title=reminder.title)
        self._restored = state is not None
        self.created: list = []          # 真的建出来的子任务 (供调用方提示条数)

        self.setWindowTitle("创建子任务")
        self.setMinimumWidth(430)
        root = QVBoxLayout(self)

        head = QLabel(f"主任务「{reminder.title or '未命名'}」"
                      f"  起点 {_fmt(self.state.start_at)}")
        head.setStyleSheet("color:#5A5245;")
        root.addWidget(head)
        if self._restored:
            hint = QLabel(f"已恢复上次没填完的草稿（{self.state.step_hint}）。")
            hint.setStyleSheet("color:#8A6D3B;")
            root.addWidget(hint)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self._build_step_page())
        self.stack.addWidget(self._build_list_page())
        root.addWidget(self.stack)

        self.buttons = QDialogButtonBox()
        self.btn_prev = QPushButton("上一步")
        self.btn_next = QPushButton("下一步")
        self.btn_finish = QPushButton("完成")
        self.btn_cancel = QPushButton("取消")
        self.btn_toggle = QPushButton("转普通列表视图")
        self.btn_prev.clicked.connect(self._on_prev)
        self.btn_next.clicked.connect(self._on_next)
        self.btn_finish.clicked.connect(self._on_finish)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_toggle.clicked.connect(self._toggle_view)
        for b in (self.btn_prev, self.btn_next, self.btn_finish, self.btn_cancel, self.btn_toggle):
            self.buttons.addButton(b, QDialogButtonBox.ActionRole)
        root.addWidget(self.buttons)

        self._load_current()
        self._refresh_nav()

    # ------------------------------------------------------------ 分步页
    def _build_step_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        self.lbl_step = QLabel()
        self.lbl_step.setStyleSheet("font-weight:bold;")
        lay.addWidget(self.lbl_step)
        row = QHBoxLayout()
        self.ed_title = QLineEdit()
        self.ed_title.setPlaceholderText("子任务名称, 例如「整理需求」")
        self.sp_duration = QSpinBox()
        self.sp_duration.setRange(1, MAX_DURATION_MIN)
        self.sp_duration.setSuffix(" 分钟")
        self.sp_duration.setValue(DEFAULT_DURATION_MIN)
        self.sp_duration.setFixedWidth(110)
        row.addWidget(self.ed_title, 1)
        row.addWidget(QLabel("持续"))
        row.addWidget(self.sp_duration)
        lay.addLayout(row)
        self.lbl_chain = QLabel()
        self.lbl_chain.setWordWrap(True)
        self.lbl_chain.setStyleSheet("color:#7A7267;")
        lay.addWidget(self.lbl_chain)
        self.ed_title.textChanged.connect(self._on_step_edited)
        self.sp_duration.valueChanged.connect(self._on_step_edited)
        return page

    def _on_step_edited(self) -> None:
        self.state.set_current(self.ed_title.text(), self.sp_duration.value())
        self._refresh_chain()
        self._save_draft()

    def _load_current(self) -> None:
        cur = self.state.current
        self.ed_title.setText(cur.title)
        self.sp_duration.setValue(cur.duration_min)
        self._refresh_chain()

    def _refresh_chain(self) -> None:
        rows = self.state.plan_rows()
        lines = []
        for i, (name, dur, start) in enumerate(rows):
            mark = "▶ " if (self.stack.currentIndex() == 0 and i == self.state.index) else "  "
            lines.append(f"{mark}{i + 1}. {name} —— {_fmt(start)} 起，{dur} 分钟")
        self.lbl_chain.setText("\n".join(lines))

    def _refresh_nav(self) -> None:
        self.lbl_step.setText(self.state.step_hint + ("" if self.state.is_last else "（填完点「下一步」）"))
        self.btn_prev.setEnabled(self.state.index > 0)
        self.btn_next.setVisible(not self.state.is_last)
        self.btn_finish.setVisible(True)
        self.btn_toggle.setText("转普通列表视图" if self.stack.currentIndex() == 0 else "回到分步视图")
        self.stack.setCurrentIndex(0 if self.state.mode == "step" else 1)

    # ------------------------------------------------------------ 列表页
    def _build_list_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        tip = QLabel("一次填完再保存（子任务多的时候比连点十次「下一步」省事）。"
                     "持续时间决定下一个子任务的开始时间。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#7A7267;")
        lay.addWidget(tip)
        self.table = QTableWidget(0, 2, page)
        self.table.setHorizontalHeaderLabels(["子任务名称", "持续时间(分钟)"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 260)
        lay.addWidget(self.table)
        row = QHBoxLayout()
        self.btn_add_row = QPushButton("加一行")
        self.btn_del_row = QPushButton("删掉最后一行")
        self.btn_add_row.clicked.connect(lambda: self._table_add_row(SubTaskSpec()))
        self.btn_del_row.clicked.connect(self._table_del_row)
        row.addWidget(self.btn_add_row)
        row.addWidget(self.btn_del_row)
        row.addStretch(1)
        lay.addLayout(row)
        return page

    def _table_add_row(self, spec: SubTaskSpec) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(spec.title))
        item = QTableWidgetItem(str(spec.duration_min))
        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.table.setItem(r, 1, item)

    def _table_del_row(self) -> None:
        if self.table.rowCount() > 1:
            self.table.removeRow(self.table.rowCount() - 1)
            self._pull_table()

    def _fill_table(self) -> None:
        self.table.setRowCount(0)
        for spec in self.state.specs:
            self._table_add_row(spec)

    def _pull_table(self) -> None:
        """把表格里的内容读回状态机。数字填坏的格子按默认值处理, 不弹错。"""
        specs: list[SubTaskSpec] = []
        for r in range(self.table.rowCount()):
            title_item = self.table.item(r, 0)
            dur_item = self.table.item(r, 1)
            try:
                dur = int((dur_item.text() if dur_item else "").strip())
            except (TypeError, ValueError):
                dur = DEFAULT_DURATION_MIN
            specs.append(SubTaskSpec(title=(title_item.text() if title_item else ""),
                                     duration_min=max(1, min(MAX_DURATION_MIN, dur))))
        if specs:
            self.state.specs = specs
            self.state.goto(self.state.index)
        self._save_draft()

    # ------------------------------------------------------------ 导航
    def _toggle_view(self) -> None:
        if self.stack.currentIndex() == 0:
            self._on_step_edited()
            self.state.mode = "list"
            self._fill_table()
        else:
            self._pull_table()
            self.state.mode = "step"
            self._load_current()
        self._refresh_nav()
        self._refresh_chain()
        self._save_draft()

    def _on_prev(self) -> None:
        self._on_step_edited()
        self.state.back()
        self._load_current()
        self._refresh_nav()

    def _on_next(self) -> None:
        err = self.state.validate_current()
        if err:
            self.report_error(err)
            return
        self._on_step_edited()
        self.state.next()
        self._load_current()
        self._refresh_nav()
        self.ed_title.setFocus()

    def _on_finish(self) -> None:
        self.collect()
        err = self.state.validate_all()
        if err:
            self.report_error(err)
            return
        specs = self.state.filled_specs()
        times = [t for _, _, t in self.state.plan_rows()]
        try:
            created = []
            for spec, when in zip(specs, times):
                created.append(self.app.life.add_child(
                    self.reminder, spec.clean_title(), spec.duration_min, event_at=when))
        except Exception as exc:                      # noqa: BLE001 - 界面上要看到原因
            log.exception("创建子任务失败")
            self.report_error(str(exc))
            return
        self.created = created
        self.app.store.clear_draft(self.key)
        log.info("向导创建了 %d 个子任务 (主任务 #%s)", len(created), self.reminder.id)
        super().accept()

    # ------------------------------------------------------------ 草稿
    def collect(self) -> WizardState:
        if self.stack.currentIndex() == 0:
            self._on_step_edited()
        else:
            self._pull_table()
        return self.state

    def _save_draft(self) -> None:
        try:
            self.app.store.save_draft(self.key, self.state.to_draft(), self.reminder.uuid)
        except Exception:                            # noqa: BLE001 - 草稿存不下不该拦住建任务
            log.debug("保存向导草稿失败", exc_info=True)

    def reject(self) -> None:                        # noqa: D102 - 取消/关窗: 只留草稿, 不建任务
        self.collect()
        self._save_draft()
        super().reject()

    def closeEvent(self, ev):                        # noqa: N802
        if self.result() != QDialog.Accepted:
            self.collect()
            self._save_draft()
        super().closeEvent(ev)

    def report_error(self, message: str) -> None:
        """错误提示接缝: 抽成方法是为了让测试替换掉它 (模态框在无头环境会永久阻塞)。

        走 ui.dialogs 让它也置顶 —— 否则提示框被便签盖住, 用户只看到向导没动静。
        """
        from .dialogs import warn

        warn(self, "还差一点", message)


def resume_state(app, reminder) -> WizardState | None:
    """打开向导前先看有没有草稿 —— 有就恢复到上次填到的那一步。"""
    raw = app.store.load_draft(draft_key(reminder.uuid))
    return WizardState.from_draft(raw, start_at=reminder.event_at)
