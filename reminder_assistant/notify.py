"""通知投递: 优先级 / 去重 / 免打扰降级。

工程师清单第 2 条 (是否打扰用户) 与第 4 条 (通知去重) 的落点:
  * 去重     -> 通知 key = reminder#index#fire_at, 落 notifications 表主键,
                崩溃重启后重放不会二次弹窗 (at-least-once + 幂等消费)
  * 免打扰   -> QuietHours (默认 22:30-07:30) + 全屏/演示模式检测,
                命中就进延迟队列, 出时段后合并成一条摘要, 避免"补弹十连"
  * 分级     -> models.LEVELS 决定声音/时长/是否置顶
  * 降级     -> 系统 toast 不可用 -> 托盘气泡 -> 日志, 任何一层失败都不影响调度
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Callable, Protocol

from .models import LEVELS, Reminder
from .scheduler import Fired
from .store import Store

log = logging.getLogger("reminder.notify")


# ---------------------------------------------------------------- 抽象

class Notifier(Protocol):
    def send(self, title: str, body: str, *, urgency: str = "normal",
             key: str = "", actions: tuple[str, ...] = ()) -> bool:
        ...


class LogNotifier:
    """默认 / 测试用: 只记录, 不弹窗。"""

    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    def send(self, title, body, *, urgency="normal", key="", actions=()) -> bool:
        self.sent.append((title, body, urgency))
        log.info("[通知/%s] %s | %s", urgency, title, body)
        return True


class ConsoleNotifier(LogNotifier):
    def send(self, title, body, *, urgency="normal", key="", actions=()) -> bool:
        super().send(title, body, urgency=urgency, key=key, actions=actions)
        print(f"\a🔔 [{urgency}] {title}\n   {body}")
        return True


class WindowsToastNotifier:
    """Windows 原生通知, 三级降级。

    1. win11toast (若已安装)               —— 支持按钮回调
    2. PowerShell + Windows.UI.Notifications (系统自带, 无依赖)
    3. 直接返回 False, 由上层换 ConsoleNotifier
    """

    def __init__(self, app_id: str = "HandyReminder"):
        self.app_id = app_id
        import importlib.util

        self._backend = ("win11toast" if importlib.util.find_spec("win11toast") is not None
                         else ("powershell" if sys.platform == "win32" else "none"))

    def send(self, title, body, *, urgency="normal", key="", actions=()) -> bool:
        if self._backend == "none":
            return False
        if self._backend == "win11toast":  # pragma: no cover
            try:
                import win11toast

                win11toast.notify(body, title, app_id=self.app_id,
                                  duration="long" if urgency == "critical" else "short",
                                  audio=None if urgency != "critical" else {"silent": "false"})
                return True
            except Exception:
                log.exception("win11toast 失败, 降级到 PowerShell")
                self._backend = "powershell"
        return self._via_powershell(title, body, urgency, actions)

    def _via_powershell(self, title, body, urgency, actions) -> bool:  # pragma: no cover
        if sys.platform != "win32" or not shutil.which("powershell"):
            return False
        # 注意: 所有用户文本都经单引号转义后拼接, 走参数化 XML 模板, 不做字符串命令注入
        def esc(s: str) -> str:
            return s.replace("'", "''").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
            "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('ToastText02');"
            f"$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode('{esc(title)}')) > $null;"
            f"$t.GetElementsByTagName('text')[1].AppendChild($t.CreateTextNode('{esc(body)}')) > $null;"
            f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{esc(self.app_id)}')"
            ".Show([Windows.UI.Notifications.ToastNotification]::new($t));"
        )
        try:
            # **必须**用 CREATE_NO_WINDOW + SW_HIDE: 从没有控制台的进程 (pythonw / 打包版
            # console=False) 起 powershell, 系统会给它**新建一个可见的黑框窗口** ——
            # 客户反馈"还是有 python 的窗口"里那个一闪而过的窗口就是它。
            flags = 0
            startup = None
            if sys.platform == "win32":
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = 0                     # SW_HIDE
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                               capture_output=True, timeout=10,
                               creationflags=flags, startupinfo=startup)
            if r.returncode != 0:
                log.warning("PowerShell toast 失败: %s", r.stderr.decode(errors="ignore")[:200])
            return r.returncode == 0
        except Exception:
            log.exception("PowerShell toast 异常")
            return False


# ---------------------------------------------------------------- 免打扰

@dataclass
class QuietHours:
    enabled: bool = False
    start: time = time(22, 30)
    end: time = time(7, 30)

    def contains(self, when: datetime) -> bool:
        if not self.enabled:
            return False
        t = when.timetz().replace(tzinfo=None)
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end      # 跨零点


@dataclass
class NotificationCenter:
    store: Store
    notifier: Notifier
    quiet: QuietHours = field(default_factory=QuietHours)
    fullscreen_probe: Callable[[], bool] | None = None
    deferred: list[Reminder] = field(default_factory=list)

    # ---- 交付 --------------------------------------------------------
    def deliver(self, fired: Fired, now: datetime | None = None) -> str:
        """返回 delivered | deferred | duplicate | dropped。"""
        now = now or fired.at
        key = self.key_for(fired)
        if not self.store.try_record_notification(key, now):
            return "duplicate"
        if self.quiet.contains(now) or self._in_fullscreen():
            self.deferred.append(fired.reminder)
            return "deferred"
        ok = self._send(fired.reminder, key=key, late_by=fired.late_by)
        return "delivered" if ok else "dropped"

    def flush_deferred(self, now: datetime | None = None) -> int:
        """出了免打扰时段, 把期间攒下的提醒合并成一条摘要发出去。"""
        now = now or datetime.now()
        if not self.deferred or self.quiet.contains(now) or self._in_fullscreen():
            return 0
        batch, self.deferred = self.deferred, []
        titles = "、".join(r.title for r in batch[:3])
        more = f" 等 {len(batch)} 项" if len(batch) > 3 else ""
        self.notifier.send("免打扰期间累积的提醒", f"{titles}{more}",
                           urgency="normal", key=f"digest:{now.date().isoformat()}")
        return len(batch)

    # ---- 内部 --------------------------------------------------------
    def _send(self, rem: Reminder, *, key: str, late_by: float = 0.0) -> bool:
        cfg = LEVELS.get(rem.level, LEVELS["日常"])
        body = rem.title or rem.source_text or "(无内容)"
        if late_by > 60:
            body += f"\n(延迟 {int(late_by // 60)} 分钟后送达)"
        actions = ("完成", "5分钟后", "稍后")
        try:
            return self.notifier.send("智能手账提醒", body, urgency=cfg["urgency"],
                                      key=key, actions=actions)
        except Exception:
            log.exception("通知发送失败 key=%s", key)
            return False

    def _in_fullscreen(self) -> bool:
        if not self.fullscreen_probe:
            return False
        try:
            return bool(self.fullscreen_probe())
        except Exception:
            return False

    @staticmethod
    def key_for(fired: Fired) -> str:
        return f"n:{fired.reminder.id}#{fired.index}@{fired.fire_at.isoformat()}"
