"""设置面板里的"账户"模块 (PRD 2.4 / 客户反馈第 2 条)。

反馈原话是"绑定功能没有实现，设置面板上加一个账户模块"。所以这里做两件事:
  1. 把账户体系**放进设置面板**, 成为一个看得见、点得到的模块;
  2. 让"绑定"真的能完成: 生成二维码/绑定码 → 手机端用码 → 电脑端登记设备 → 可撤销。

规矩:
  * 任何失败都写在下面那行状态文字里, **不弹模态框** —— 无头环境里模态框会把整轮测试挂死,
    用户也不需要在设置面板里被弹窗打断;
  * 二维码是自己画的 (reminder_assistant.qr, 无第三方依赖), 画不出来时降级为
    "显示绑定码 + 手动输入", 这条路径永远可用;
  * 日志与界面都不显示设备令牌。
"""

from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget)

from .. import account as acct_mod
from .. import qr as qr_mod
from . import theme

log = logging.getLogger("reminder.ui.account")

QR_SIZE = 132          # 面板上二维码的边长 (px)
NO_CODE = "尚未生成"    # 还没生成绑定码时显示的占位文案 (也是"能不能复制"的判据)


def qr_pixmap(text: str, size: int = QR_SIZE) -> QPixmap | None:
    """把一段文本画成二维码。画不出来就返回 None (界面上必须降级, 不能抛)。"""
    try:
        code = qr_mod.encode(text, "M")
    except Exception:                                  # noqa: BLE001 - 内容过长等
        log.warning("生成二维码失败 (内容 %d 字节)", len(text.encode()), exc_info=True)
        return None
    border = 2
    modules = code.size + border * 2
    scale = max(1, size // modules)
    img = QImage(modules * scale, modules * scale, QImage.Format_RGB32)
    img.fill(QColor("white"))
    painter = QPainter(img)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("black"))
    for y in range(code.size):
        for x in range(code.size):
            if code.modules[y][x]:
                painter.drawRect((x + border) * scale, (y + border) * scale, scale, scale)
    painter.end()
    return QPixmap.fromImage(img)


class AccountSection(QGroupBox):
    """账户与设备绑定。自己读写账户, 不需要设置面板的"保存"按钮 (绑定是即时动作)。"""

    def __init__(self, app, parent: QWidget | None = None):
        super().__init__("账户与设备绑定（电脑端 ↔ 手机端）", parent)
        self.app = app
        # 走 app.account (AccountBook): 单一入口, 每次读-改-写。
        # 自己 load() 一份的话, 手机端在同步桥上完成绑定时, 这边稍后的一次保存会把
        # 那台设备整段覆盖掉 —— 表现是"手机上绑好了, 电脑端设备列表里没有"。
        self.book = app.account
        self.account = self.book.reload()
        self._last_payload: str | None = None
        self._last_pixmap: QPixmap | None = None

        lay = QVBoxLayout(self)
        lay.setSpacing(6)

        # ---- 识别码 ----
        row = QHBoxLayout()
        row.addWidget(QLabel("本机识别码"))
        self.lbl_account = QLabel(self.account.account_id)
        self.lbl_account.setFont(theme.handwriting_font(13, bold=True))
        self.lbl_account.setStyleSheet("color:#2E2A25;letter-spacing:1px;")
        # 客户反馈: "识别码可以复制" —— 保留按钮, 再加上手选 (两条路都留)
        self.lbl_account.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_account.setToolTip("这台电脑的账户身份号，不会变。**绑定用不到它** —— "
                                    "绑定用的是下面生成的「绑定码」。")
        row.addWidget(self.lbl_account)
        self.btn_copy = QPushButton("复制")
        self.btn_copy.setFixedWidth(52)
        self.btn_copy.clicked.connect(self._copy_account)
        row.addWidget(self.btn_copy)
        row.addStretch(1)
        lay.addLayout(row)

        # 两个码各自的用途必须写在面板上 (客户反馈"识别码好像不起作用", 根子是没说清哪个是干嘛的)
        clue = QLabel("识别码 = 这台电脑的身份号（一直不变，绑定用不到它）；"
                      "绑定码 = 真正用来绑定的那一串，给一台手机用一次，5 分钟内有效。")
        clue.setWordWrap(True)
        clue.setStyleSheet("color:#7A7267;")
        lay.addWidget(clue)

        # ---- 生成绑定码 (扫码) ----
        row2 = QHBoxLayout()
        self.btn_code = QPushButton("生成绑定码（手机扫码用）")
        self.btn_code.clicked.connect(self._issue)
        row2.addWidget(self.btn_code)
        self.lbl_code = QLabel(NO_CODE)
        self.lbl_code.setFont(theme.handwriting_font(13, bold=True))
        self.lbl_code.setStyleSheet("color:#B5651D;letter-spacing:2px;")
        self.lbl_code.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_code.setToolTip("绑定码：手机端用它完成绑定，只能用一次、5 分钟有效。")
        row2.addWidget(self.lbl_code)
        self.btn_copy_code = QPushButton("复制")
        self.btn_copy_code.setFixedWidth(52)
        self.btn_copy_code.setToolTip("复制这串绑定码（贴到手机端的「绑定码」框里）")
        self.btn_copy_code.clicked.connect(self._copy_code)
        row2.addWidget(self.btn_copy_code)
        row2.addStretch(1)
        lay.addLayout(row2)

        body = QHBoxLayout()
        self.qr_view = QLabel()
        self.qr_view.setFixedSize(QR_SIZE, QR_SIZE)
        self.qr_view.setStyleSheet("border:1px solid #D9CFBC;background:#fff;")
        self.qr_view.setAlignment(Qt.AlignCenter)
        body.addWidget(self.qr_view)
        tips = QLabel("手机端扫这个码（或用相机扫出那串码，粘到手机端页面的“绑定”框里）。\n"
                      "码只用一次，5 分钟内有效；过期就重新生成一个。")
        tips.setWordWrap(True)
        tips.setStyleSheet("color:#7A7267;")
        body.addWidget(tips, 1)
        lay.addLayout(body)

        # ---- 手动绑定 (手机端生成的码) ----
        row3 = QHBoxLayout()
        self.ed_code = QLineEdit()
        self.ed_code.setPlaceholderText("或粘贴手机端显示的绑定码，如 AB3D-7F9K")
        self.ed_code.returnPressed.connect(self._manual_bind)
        row3.addWidget(self.ed_code, 1)
        self.btn_bind = QPushButton("绑定")
        self.btn_bind.setFixedWidth(64)
        self.btn_bind.clicked.connect(self._manual_bind)
        row3.addWidget(self.btn_bind)
        lay.addLayout(row3)

        # ---- 已绑定设备 ----
        lay.addWidget(QLabel("已绑定的设备"))
        self.list_devices = QListWidget()
        self.list_devices.setFixedHeight(64)
        lay.addWidget(self.list_devices)
        row4 = QHBoxLayout()
        self.btn_revoke = QPushButton("撤销选中的设备绑定")
        self.btn_revoke.clicked.connect(self._revoke)
        row4.addWidget(self.btn_revoke)
        row4.addStretch(1)
        lay.addLayout(row4)

        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet("color:#4C6B4F;")
        lay.addWidget(self.lbl_status)

        # 手机端在另一端点了绑定, 这一屏要跟着变 —— 只在可见时轮询, 隐了就停。
        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self.refresh_devices)
        self.refresh_devices()

    # ------------------------------------------------------------ 生命周期
    def showEvent(self, ev):                           # noqa: N802
        super().showEvent(ev)
        self.refresh_devices()
        self._timer.start()

    def hideEvent(self, ev):                           # noqa: N802
        self._timer.stop()
        super().hideEvent(ev)

    # ------------------------------------------------------------ 内部
    def _status(self, text: str, bad: bool = False) -> None:
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet("color:#B4553F;" if bad else "color:#4C6B4F;")

    def _copy_account(self) -> None:
        try:
            QApplication.clipboard().setText(self.account.account_id)
            self._status(f"识别码已复制：{self.account.account_id}")
        except Exception as exc:                       # noqa: BLE001 - 剪贴板可能不可用
            log.debug("复制识别码失败", exc_info=True)
            self._status(f"复制失败，请手动抄下：{self.account.account_id}（{exc}）", bad=True)

    def _copy_code(self) -> None:
        """复制当前这串**绑定码** (客户反馈: 识别码能复制、绑定码不能)。

        复制的是标签上真正显示的那串 —— 重新生成后自然就复制新的, 不需要另存一份状态。
        """
        text = self.lbl_code.text().strip()
        if not text or text == NO_CODE:
            self._status("还没有绑定码可复制：先点左边「生成绑定码（手机扫码用）」", bad=True)
            return
        try:
            QApplication.clipboard().setText(text)
            self._status(f"绑定码已复制：{text}（贴到手机端的「绑定码」框里，5 分钟内有效）")
        except Exception as exc:                       # noqa: BLE001
            log.debug("复制绑定码失败", exc_info=True)
            self._status(f"复制失败，请手动抄下：{text}（{exc}）", bad=True)

    def _issue(self) -> None:
        """生成一个新的绑定码并画二维码 (界面路径上的异常一律就地降级)。"""
        try:
            now = datetime.now().astimezone()
            self.account, code = self.book.issue_code(now, acct_mod.ROLE_DESKTOP)
            payload = acct_mod.qr_payload(self.account, code)
            self.lbl_code.setText(acct_mod.format_code(code.code))
            self._show_qr(payload)
            self._status(f"绑定码已生成：{acct_mod.format_code(code.code)}，"
                         f"5 分钟内有效、只能用一次。手机端绑定后会出现在下面的设备列表里。")
        except acct_mod.AccountError as exc:
            self._status(str(exc), bad=True)
        except Exception as exc:                       # noqa: BLE001
            log.exception("生成绑定码失败")
            self._status(f"生成绑定码失败：{exc}", bad=True)

    def _show_qr(self, payload: str) -> None:
        if payload != self._last_payload or self._last_pixmap is None:
            self._last_payload = payload
            self._last_pixmap = qr_pixmap(payload)
        pix = self._last_pixmap
        if pix is None:
            # 降级: 二维码画不出来也**不能**挡住绑定 —— 手动输入那条路永远可用
            self.qr_view.setPixmap(QPixmap())
            self.qr_view.setText("二维码不可用，\n请把上面的码抄给手机端")
            self._status("二维码生成失败（内容过长？），请让手机端用上面的绑定码手动绑定。", bad=True)
            return
        self.qr_view.setText("")
        self.qr_view.setPixmap(pix)

    def _manual_bind(self) -> None:
        """手动绑定: 支持整段扫码结果, 也支持手打的码。"""
        raw = self.ed_code.text().strip()
        if not raw:
            self._status("请先填手机端显示的绑定码", bad=True)
            return
        parsed = acct_mod.parse_qr_payload(raw)
        text = parsed["code"] if parsed else raw
        try:
            now = datetime.now().astimezone()
            self.account, dev = self.book.bind(text, now, name="手机端(手动绑定)",
                                               platform="手机", role=acct_mod.ROLE_PHONE)
            self.ed_code.clear()
            self.refresh_devices()
            self._status(f"绑定成功：{dev.name}（{dev.platform}）。"
                         f"手机端现在可以收/改任务了；换手机或重装后请在这里撤销旧的绑定。")
        except acct_mod.AccountError as exc:
            self._status(str(exc), bad=True)
        except Exception as exc:                       # noqa: BLE001
            log.exception("手动绑定失败")
            self._status(f"绑定失败：{exc}", bad=True)

    def bind_from_remote(self, raw: str, *, name: str, platform: str,
                         role: str = acct_mod.ROLE_DESKTOP) -> tuple[dict | None, str | None]:
        """手机端在另一端用码绑定 (由同步桥调用)。

        返回 (设备信息, 错误原因)。写入后立即落盘 —— 演示时电脑端的设备列表要**马上**出现。
        """
        try:
            now = datetime.now().astimezone()
            self.account, dev = self.book.bind(raw, now, name=name, platform=platform, role=role)
            self.refresh_devices()
            self._status(f"手机端已完成绑定：{dev.name}")
            return ({"id": dev.id, "name": dev.name, "platform": dev.platform,
                     "token": dev.token}, None)
        except acct_mod.AccountError as exc:
            return None, str(exc)
        except Exception as exc:                       # noqa: BLE001
            log.exception("远端绑定失败")
            return None, f"绑定失败：{exc}"

    def refresh_devices(self) -> None:
        """把设备列表重画一遍 (按 id 差量太啰嗦, 这个列表本来就短)。"""
        try:
            self.account = self.book.reload()
            current = self.list_devices.currentItem()
            keep = current.data(Qt.UserRole) if current else None
            self.list_devices.clear()
            for d in self.account.devices:
                when = datetime.fromisoformat(d.bound_at).strftime("%m-%d %H:%M")
                item = QListWidgetItem(f"{d.name} · {d.platform} · 绑定于 {when} · #{d.id[:6]}")
                item.setData(Qt.UserRole, d.id)
                self.list_devices.addItem(item)
                if d.id == keep:
                    self.list_devices.setCurrentItem(item)
            self.lbl_account.setText(self.account.account_id)
            if not self.account.devices:
                self._status("还没有绑定任何手机端。点「生成绑定码」再用手机端扫码/输码。")
        except Exception:                              # noqa: BLE001 - 刷新失败不该砸掉面板
            log.exception("刷新设备列表失败")

    def _revoke(self) -> None:
        try:
            item = self.list_devices.currentItem()
            if item is None:
                self._status("请先在列表里选中要撤销的设备", bad=True)
                return
            dev = acct_mod.revoke_device(self.account, str(item.data(Qt.UserRole)))
            if dev is None:
                self._status("这条设备已经不在了", bad=True)
                return
            self.book.revoke(dev.id)
            # 顺序要紧: refresh_devices() 在没有设备时会把状态行改成"还没有绑定任何手机端",
            # 先刷新再写提示, 用户才看得到"已撤销" (否则以为没删掉)。
            self.refresh_devices()
            self._status(f"已撤销「{dev.name}」的绑定，它的设备令牌同时失效。")
        except Exception as exc:                       # noqa: BLE001
            log.exception("撤销绑定失败")
            self._status(f"撤销失败：{exc}", bad=True)
