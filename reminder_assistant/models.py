"""领域模型。

一次"提醒事件"(Reminder) 会派生多条"触发点"(Occurrence)。
PRD 里的自定义规则 "先设重复次数, 再设第一次提醒时间, 之后只设间隔"
正好映射成: ReminderPlan -> [(offset, ), ...] -> Occurrence 列表。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Iterable

# ---- 事件等级 (2.3 预设等级事件) ----------------------------------------
LEVELS: dict[str, dict] = {
    # name -> (首次相对事件的提前量, 间隔, 次数, 通知强度)
    "日常": {"lead": timedelta(0), "interval": timedelta(0), "repeat": 1, "urgency": "normal"},
    "重要": {"lead": timedelta(minutes=30), "interval": timedelta(minutes=10), "repeat": 3, "urgency": "normal"},
    "紧急": {"lead": timedelta(hours=1), "interval": timedelta(minutes=5), "repeat": 5, "urgency": "critical"},
}
DEFAULT_LEVEL = "日常"


@dataclass(frozen=True)
class ReminderPlan:
    """提醒计划。offsets[i] 是第 i 个触发点相对事件时刻的偏移 (负数 = 提前)。"""

    offsets: tuple[timedelta, ...]

    @staticmethod
    def preset(level: str = DEFAULT_LEVEL) -> "ReminderPlan":
        """出厂预设等级的计划。用户改过的等级走 settings.Settings.plan()。"""
        c = LEVELS.get(level, LEVELS[DEFAULT_LEVEL])
        return ReminderPlan.from_spec(c["lead"], c["interval"], c["repeat"])

    @staticmethod
    def from_spec(lead: timedelta, interval: timedelta, repeat: int) -> "ReminderPlan":
        """按"提前量 + 间隔 + 次数"生成计划 —— 设置面板用的就是这个入口。

        紧急(lead=1h, interval=5m, repeat=5) 于 11:00 的事件
        -> 10:00 10:05 10:10 10:15 10:20
        """
        if repeat < 1:
            raise ValueError("提醒次数必须 >= 1")
        if interval < timedelta(0):
            raise ValueError("间隔不能为负")
        if lead < timedelta(0):
            raise ValueError("提前量不能为负")
        return ReminderPlan(tuple(interval * i - lead for i in range(repeat)))

    @staticmethod
    def custom(repeat: int, first_offset: timedelta, interval: timedelta) -> "ReminderPlan":
        """完全自定义: 次数 + 第一次相对事件的提前量(负数) + 后续间隔。

        PRD 例: 事件 15:50, 第一次提前 1 小时 -> 14:50, 间隔 20 分钟
        -> offsets = (-60m, -40m, -20m)
        """
        if repeat < 1:
            raise ValueError("repeat 必须 >= 1")
        if interval < timedelta(0):
            raise ValueError("interval 不能为负")
        if first_offset > timedelta(0):
            raise ValueError("first_offset 表示提前量, 必须是负数或 0")
        return ReminderPlan(tuple(first_offset + interval * i for i in range(repeat)))

    def to_json(self) -> str:
        return json.dumps([o.total_seconds() for o in self.offsets])

    @staticmethod
    def from_json(s: str) -> "ReminderPlan":
        return ReminderPlan(tuple(timedelta(seconds=x) for x in json.loads(s)))


@dataclass
class Occurrence:
    """一个具体触发点。gen 用于"延迟提醒更新原任务"时的失效判定。"""

    reminder_id: int
    index: int
    fire_at: datetime
    gen: int = 0
    state: str = "pending"  # pending|firing|fired|missed|cancelled
    fired_at: datetime | None = None
    late_by: float = 0.0  # 秒; 因休眠/关机错过后的迟到量

    @property
    def key(self) -> str:
        return f"{self.reminder_id}#{self.index}"


@dataclass
class Reminder:
    id: int | None = None
    title: str = ""
    source_text: str = ""  # OCR 原始文本
    event_at: datetime | None = None  # 事件时刻 (可为 None: 仅相对时间)
    tz: str = ""  # IANA 时区名, 用于跨时区/夏令时重算
    level: str = DEFAULT_LEVEL
    plan: ReminderPlan = field(default_factory=ReminderPlan.preset)
    status: str = "active"  # active|completed|expired
    created_at: datetime | None = None
    completed_at: datetime | None = None
    expire_at: datetime | None = None  # 兜底清理时刻 (2.2 过期自动清理)
    bbox: tuple[int, int, int, int] | None = None  # 圈选区域, 可回跳原位置 (仅本机, 不上云)
    note: str = ""
    duration_min: int = 0     # 事件属性: 预计持续时间(分钟), 0 = 未设置

    # ---- 跨端同步字段 (阶段一, 见 docs/云端同步与数据模型设计.md 2.2) ----
    # 全部带默认值: 老的构造调用 (关键字参数) 一个字都不用改。
    uuid: str = ""            # 同步主键 / 云端 id (UUIDv7)
    parent_uuid: str = ""     # 二级目录: 所属主任务的 uuid (空 = 自己是主任务)
    depth: int = 0            # 0=主任务, 1=子任务 (PRD 2.5 只允许二级)
    sort_key: float = 0.0     # 同级内的排序键 (分数索引, 插入不用重排邻居)
    batch_id: str = ""        # 向导式批量创建的批次号
    version: int = 0          # 权威版本号 (阶段二起用于冲突判定)
    server_seq: int = 0       # 云端单调序号 (阶段二写入)
    dirty: int = 0            # 1=有未同步的本地改动
    sync_state: str = "clean"  # clean|dirty|conflicted

    # ---- 触发点展开 ----------------------------------------------------
    def occurrences(self) -> list[Occurrence]:
        if self.id is None:
            raise ValueError("Reminder 未持久化, 无 id")
        if self.event_at is None:
            raise ValueError("需要 event_at 才能展开相对触发点")
        out: list[Occurrence] = []
        for i, off in enumerate(self.plan.offsets):
            fire = self.event_at + off
            # 已经过期的触发点直接标记 missed, 不做无声丢弃
            out.append(Occurrence(reminder_id=self.id, index=i, fire_at=fire))
        return out

    def with_id(self, new_id: int) -> "Reminder":
        return replace(self, id=new_id)


def merge_absolute(rem: Reminder, times: Iterable[datetime]) -> Reminder:
    """把 OCR 抽到的多个绝对时间合并成计划(用于"提前X分钟"以外的显式时间点)。"""
    ts = sorted(times)
    if not ts:
        return rem
    base = ts[-1]
    rem.event_at = base
    rem.plan = ReminderPlan(tuple(t - base for t in ts))
    return rem
