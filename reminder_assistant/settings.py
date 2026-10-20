"""用户设置（PRD 2.3）。

PRD 那句"每个等级绑定不同的自定义提醒频率、时间"意味着等级不能被写死成常量：
用户要能改"重要 = 提前 30 分钟、间隔 10 分钟、提醒 3 次"这种组合，并且下次开会话还在。
所以等级、自定义事件参数、免打扰时段、热键全部落到 store 的 meta 表（JSON 一行），
`models.LEVELS` 只作为出厂默认值。

改计划这件事有两层含义，别混：
  * 改"等级"的默认参数  -> 以后新建的提醒按新参数走（已存在的提醒不动）
  * 改"某条提醒"的计划  -> 要重排它的触发点（走 lifecycle.change_plan）
PRD 的"高级事件"面板两种都要覆盖。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta

from .models import LEVELS, ReminderPlan
from .notify import QuietHours

log = logging.getLogger("reminder.settings")

META_KEY = "settings.v1"
MAX_REPEAT = 20
MAX_MINUTES = 24 * 60


@dataclass(frozen=True)
class LevelSpec:
    """一个等级的提醒配方：第一次提前 lead，之后每 interval 一次，共 repeat 次。"""

    lead: timedelta = timedelta(0)
    interval: timedelta = timedelta(minutes=15)
    repeat: int = 1
    urgency: str = "normal"

    def __post_init__(self):
        if self.repeat < 1:
            raise ValueError("重复次数必须 >= 1")
        if self.interval < timedelta(0) or self.lead < timedelta(0):
            raise ValueError("时间参数不能为负")

    def plan(self) -> ReminderPlan:
        return ReminderPlan.from_spec(self.lead, self.interval, self.repeat)

    def to_json(self) -> dict:
        return {"lead": self.lead.total_seconds(), "interval": self.interval.total_seconds(),
                "repeat": self.repeat, "urgency": self.urgency}

    @staticmethod
    def from_json(d: dict, fallback: "LevelSpec") -> "LevelSpec":
        try:
            return LevelSpec(lead=timedelta(seconds=float(d["lead"])),
                             interval=timedelta(seconds=float(d["interval"])),
                             repeat=max(1, min(MAX_REPEAT, int(d["repeat"]))),
                             urgency=str(d.get("urgency", fallback.urgency)))
        except (KeyError, TypeError, ValueError):
            log.warning("等级配置损坏, 回退到默认值: %r", d)
            return fallback

    def describe(self) -> str:
        lead = int(self.lead.total_seconds() // 60)
        interval = int(self.interval.total_seconds() // 60)
        if self.repeat == 1 and lead == 0:
            return "到点提醒一次"
        head = f"提前 {lead} 分钟" if lead else "到点"
        if self.repeat == 1:
            return f"{head}提醒一次"
        return f"{head}起, 每 {interval} 分钟一次, 共 {self.repeat} 次"


def default_levels() -> dict[str, LevelSpec]:
    return {name: LevelSpec(lead=c["lead"], interval=c["interval"], repeat=c["repeat"],
                            urgency=c["urgency"])
            for name, c in LEVELS.items()}


def _parse_hhmm(s: str, fallback: time) -> time:
    try:
        h, m = str(s).split(":")
        return time(int(h) % 24, int(m) % 60)
    except (ValueError, AttributeError):
        return fallback


@dataclass
class Settings:
    levels: dict[str, LevelSpec] = field(default_factory=default_levels)
    # "高级事件 / 完全自定义" 面板里最后用过的配方, 下次打开记住
    custom: LevelSpec = field(default_factory=lambda: LevelSpec(
        lead=timedelta(hours=1), interval=timedelta(minutes=20), repeat=3))
    quiet_enabled: bool = False
    quiet_start: time = time(22, 30)
    quiet_end: time = time(7, 30)
    hotkey: str = "alt+q"
    quiet_on_fullscreen: bool = True
    # 圈选识别时要不要压暗屏幕。默认压暗 (选区和字更清楚); 关掉 = 屏幕完全不变暗、
    # 只浮一层选框 —— 直接回应客户"那个黑色的识别窗口能不能隐藏掉"。
    scan_dim: bool = True

    # ------------------------------------------------------------ 计划
    def level_names(self) -> list[str]:
        return list(self.levels)

    def spec(self, level: str) -> LevelSpec:
        return self.levels.get(level) or self.levels.get("日常") or default_levels()["日常"]

    def plan(self, level: str) -> ReminderPlan:
        return self.spec(level).plan()

    def plan_for_custom(self, spec: LevelSpec | None = None) -> ReminderPlan:
        return (spec or self.custom).plan()

    def preview(self, level: str, event_at: datetime) -> list[datetime]:
        """给界面上"这条提醒会在什么时候响"的实时预览用。"""
        return [event_at + off for off in self.plan(level).offsets]

    def preview_custom(self, event_at: datetime, spec: LevelSpec | None = None) -> list[datetime]:
        return [event_at + off for off in self.plan_for_custom(spec).offsets]

    def quiet_hours(self) -> QuietHours:
        q = QuietHours(enabled=self.quiet_enabled, start=self.quiet_start, end=self.quiet_end)
        return q

    # ------------------------------------------------------------ 持久化
    def to_json(self) -> str:
        return json.dumps({
            "levels": {k: v.to_json() for k, v in self.levels.items()},
            "custom": self.custom.to_json(),
            "quiet_enabled": self.quiet_enabled,
            "quiet_start": self.quiet_start.strftime("%H:%M"),
            "quiet_end": self.quiet_end.strftime("%H:%M"),
            "hotkey": self.hotkey,
            "quiet_on_fullscreen": self.quiet_on_fullscreen,
            "scan_dim": self.scan_dim,
        }, ensure_ascii=False)

    @staticmethod
    def from_json(raw: str | None) -> "Settings":
        s = Settings()
        if not raw:
            return s
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("设置内容损坏, 使用出厂默认值")
            return s
        for name, payload in (d.get("levels") or {}).items():
            if name in s.levels and isinstance(payload, dict):
                s.levels[name] = LevelSpec.from_json(payload, s.levels[name])
        if isinstance(d.get("custom"), dict):
            s.custom = LevelSpec.from_json(d["custom"], s.custom)
        s.quiet_enabled = bool(d.get("quiet_enabled", s.quiet_enabled))
        s.quiet_start = _parse_hhmm(d.get("quiet_start", ""), s.quiet_start)
        s.quiet_end = _parse_hhmm(d.get("quiet_end", ""), s.quiet_end)
        s.hotkey = str(d.get("hotkey") or s.hotkey)
        s.quiet_on_fullscreen = bool(d.get("quiet_on_fullscreen", s.quiet_on_fullscreen))
        s.scan_dim = bool(d.get("scan_dim", s.scan_dim))
        return s

    def save(self, store) -> None:
        store.set_meta(META_KEY, self.to_json())

    @staticmethod
    def load(store) -> "Settings":
        return Settings.from_json(store.get_meta(META_KEY))

    def reset_levels(self) -> None:
        self.levels = default_levels()

    def copy(self) -> "Settings":
        return replace(self, levels=dict(self.levels))
