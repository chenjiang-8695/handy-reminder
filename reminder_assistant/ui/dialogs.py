"""模态对话框的统一呈现方式 (客户测试反馈第 1 条)。

反馈原话: "模态窗口不要固定在悬浮窗后，弹出后覆盖悬浮窗不要被遮盖"。

根因不是"忘了 raise" —— 便签主窗是 Qt.Tool | WindowStaysOnTopHint, 在 Windows 上带
WS_EX_TOPMOST 标记; 而用 parent=便签 建出来的对话框**自己不是 topmost** (Qt 不会把
置顶标记传给子窗口)。于是它稳定地待在非置顶那一层, 被便签整个压住: 看得见边角、
点不到控件。

所以这里的规矩是:
  * 每个要显示的对话框都显式设置 WindowStaysOnTopHint —— 让"在上面"这件事跟着对话框走,
    而不是指望父子层级或调用顺序;
  * 应用级模态 + 居中摆到**宿主窗口所在的那块屏幕**上 (对话框往往比便签大得多, 居中之后
    它自然覆盖在便签之上, 这正是用户要的);
  * 宿主(便签)自己也是 topmost, 用户点它有可能把它顶到对话框前面 —— 挂一个事件过滤器,
    宿主一被激活就把还活着的对话框再抬上来;
  * 无父对象的对话框必须自己存引用: 不然 Python 一回收, 窗口一闪即灭 (实测过)。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QEvent, QObject, QPoint, QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QWidget

log = logging.getLogger("reminder.ui.dialogs")

_LIVE: set[QDialog] = set()

# 只取真正存在的枚举: QEvent 里没有 Activate (只有 WindowActivate / ApplicationActivate)。
# 写错一个名字的后果不是"过滤器不生效", 而是**每次事件都抛 AttributeError** ——
# 异常从 Python 覆写的 Qt 虚函数里逃出去, 在窗口析构时直接把进程打成 access violation
# (没有 traceback, 只在测试退出时崩)。
_ACTIVATION_EVENTS = tuple(t for t in (
    getattr(QEvent, "WindowActivate", None),
    getattr(QEvent, "ApplicationActivate", None),
    getattr(QEvent, "FocusIn", None),
) if t is not None)


def _alive(obj) -> bool:
    """C++ 侧是否还在。别只依赖 try/except RuntimeError: 悬空指针的操作是**段错误**,
    在无头环境里表现为整轮测试直接崩掉 (没有 traceback, 只有 access violation)。"""
    try:
        from shiboken6 import isValid

        return bool(isValid(obj))
    except Exception:                                  # noqa: BLE001 - 没有 shiboken 就退回对象判空
        return obj is not None


def keep_alive(dlg: QDialog) -> QDialog:
    """存住引用直到对话框结束。无父对象的顶层窗口没人引用就会一闪即灭。"""
    _LIVE.add(dlg)

    def _forget(*_a) -> None:
        _LIVE.discard(dlg)

    dlg.finished.connect(_forget)
    dlg.destroyed.connect(_forget)
    return dlg


class _RaiseKeeper(QObject):
    """宿主被激活时, 把还开着的对话框重新抬到最上面。"""

    def __init__(self, host: QWidget):
        super().__init__(host)
        self.host = host
        host.installEventFilter(self)

    def eventFilter(self, obj, ev):                    # noqa: N802 - Qt 接口
        try:
            if ev.type() not in _ACTIVATION_EVENTS:
                return False
            for dlg in list(_LIVE):
                if not _alive(dlg):
                    _LIVE.discard(dlg)
                    continue
                try:
                    if dlg.isVisible():
                        dlg.raise_()
                except Exception:                      # noqa: BLE001
                    _LIVE.discard(dlg)
        except Exception:                              # noqa: BLE001
            # 绝不能让异常逃出 Qt 虚函数: 那会变成段错误而不是可读的报错
            log.debug("抬起对话框失败", exc_info=True)
        return False


def watch_host(host: QWidget | None) -> None:
    """给宿主挂一个"把我上面的对话框抬回来"的过滤器。

    过滤器**挂在宿主对象自己身上** (setattr), 不用全局字典按 id(host) 记 ——
    id 会被复用: 上一个便签被销毁后, 新便签可能拿到同一个 id, 于是全局表里那条
    "已挂过"的记录指向一个已经被销毁的 C++ 对象, Qt 再往它投事件就是段错误。
    """
    if host is None:
        return
    if getattr(host, "_hr_dialog_keeper", None) is not None:
        return
    try:
        host._hr_dialog_keeper = _RaiseKeeper(host)    # noqa: SLF001 - 故意挂在宿主上
    except Exception:                                  # noqa: BLE001 - 兜底, 不影响弹窗
        log.debug("宿主事件过滤器安装失败", exc_info=True)


def _screen_rect(host: QWidget | None):
    scr = None
    if host is not None:
        try:
            geo = host.frameGeometry()
            scr = QApplication.screenAt(geo.center())
            if scr is None and geo.width():
                scr = QApplication.screenAt(QPoint(geo.left() + 1, geo.top() + 1))
        except Exception:                              # noqa: BLE001
            scr = None
    if scr is None:
        scr = QApplication.primaryScreen()
    return scr.availableGeometry() if scr is not None else None


def center_on_screen(dlg: QWidget, host: QWidget | None = None) -> None:
    """把对话框摆到宿主所在那块屏幕的中间, 并保证不出屏。"""
    rect = _screen_rect(host)
    if rect is None:
        return
    size = dlg.sizeHint()
    w = max(size.width(), dlg.minimumWidth() or 0)
    h = max(size.height(), dlg.minimumHeight() or 0)
    w = min(w, max(320, rect.width() - 60))
    h = min(h, max(240, rect.height() - 60))
    dlg.resize(w, h)
    dlg.move(rect.left() + max(0, (rect.width() - w) // 2),
             rect.top() + max(0, (rect.height() - h) // 3))


def present(dlg: QDialog, host: QWidget | None = None, *, center: bool = True,
            modal: bool = True) -> QDialog:
    """把对话框变成"一定盖在便签之上"的模态框。所有弹窗路径都必须经过这里。"""
    try:
        dlg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        dlg.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        if modal:
            dlg.setWindowModality(Qt.ApplicationModal)
        if center:
            center_on_screen(dlg, host)
    except Exception:                                  # noqa: BLE001 - 设置失败也要能弹出来
        log.debug("对话框置顶/居中设置失败", exc_info=True)
    keep_alive(dlg)
    watch_host(host)

    def _raise() -> None:
        if not _alive(dlg):
            _LIVE.discard(dlg)
            return
        try:
            dlg.raise_()
            dlg.activateWindow()
        except Exception:                              # noqa: BLE001
            _LIVE.discard(dlg)

    # show 之后立刻抬一次: exec() 会在自己的事件循环里跑, 这个 0ms 定时器正好落在那一刻
    QTimer.singleShot(0, _raise)
    return dlg


def exec_modal(dlg: QDialog, host: QWidget | None = None, exec_fn=None) -> int:
    """present + 模态执行。exec_fn 是给测试用的接缝 (无头环境里真 exec 会永久阻塞)。"""
    present(dlg, host)
    runner = exec_fn or (lambda d: d.exec())
    return int(runner(dlg))


# ---------------------------------------------------------------- 简单提示框


def _box(host: QWidget | None, icon, title: str, text: str,
         buttons=QMessageBox.Ok, default=None) -> QMessageBox:
    box = QMessageBox(host)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(text)
    box.setStandardButtons(buttons)
    if default is not None:
        box.setDefaultButton(default)
    box.setTextInteractionFlags(Qt.TextSelectableByMouse)   # 报错内容要能复制出来
    return box


def warn(host: QWidget | None, title: str, text: str) -> None:
    """提示框。同样要置顶 —— 否则用户只看到便签, 以为"点了没反应"。"""
    box = _box(host, QMessageBox.Warning, title, text)
    exec_modal(box, host)


def ask(host: QWidget | None, title: str, text: str) -> bool:
    """确认框 (是/否)。返回用户是否点了"是"。"""
    box = _box(host, QMessageBox.Question, title, text,
               QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
    return exec_modal(box, host) == QMessageBox.Yes.value
