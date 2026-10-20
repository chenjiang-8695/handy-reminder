"""时钟抽象。

为什么要抽象时钟: 调度器的三个核心考点 (定时器漂移、休眠补发、系统改时间)
都只能通过"可注入、可比对"的双时钟来验证。
    * wall  : 挂钟时间, 带时区, 会被 NTP / 用户 / 休眠影响
    * mono  : 单调时钟, 不受改时间影响, 但休眠期间是否推进取决于平台
在 Windows 上 time.monotonic() 基于 QPC, 休眠期间**不**推进;
GetTickCount64 也不推进。因此 "mono 停滞 + wall 前进" 就是休眠/待机的指纹。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:  # 带时区的挂钟时间
        ...

    def mono(self) -> float:  # 单调秒
        ...


def local_tz():
    """本机时区 (tzdata/zoneinfo), 失败则退化为固定偏移。"""
    try:
        from datetime import datetime as _dt

        return _dt.now().astimezone().tzinfo
    except Exception:  # pragma: no cover
        return timezone(timedelta(hours=8))


class SystemClock:
    def __init__(self, tz=None):
        self.tz = tz or local_tz()

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def mono(self) -> float:
        return time.monotonic()


class FakeClock:
    """测试时钟: 可分别推进 wall 与 mono, 从而伪造休眠和改时间。"""

    def __init__(self, start: datetime, mono0: float = 1000.0, tz=None):
        self.tz = tz or (start.tzinfo or timezone.utc)
        self._now = start if start.tzinfo else start.replace(tzinfo=self.tz)
        self._mono = mono0

    def now(self) -> datetime:
        return self._now

    def mono(self) -> float:
        return self._mono

    def advance(self, **kw) -> None:
        """advance(seconds=..., minutes=..., hours=..., days=..., mono=...)"""
        delta = timedelta(**{k: v for k, v in kw.items() if k != "mono"})
        self._now += delta
        self._mono += kw.get("mono", delta.total_seconds())

    def set_wall(self, when: datetime, *, mono_delta: float = 0.0) -> None:
        """只改挂钟 (模拟用户改时间 / NTP 跳变)。"""
        self._now = when if when.tzinfo else when.replace(tzinfo=self.tz)
        self._mono += mono_delta
