"""任务二级目录与向导式创建的**纯逻辑** (PRD 2.5)。

这里不 import Qt、不碰数据库, 原因是这类交互的坑全在"状态"上而不是在画界面上:
  * 向导走到第 3 个时按了 Esc —— 前面填的不能丢 (PRD 风险预警第 4 条的"极易引发暴躁");
  * 想建 10 个子任务 —— 必须能"转普通列表视图"一次填完, 不能弹 10 次窗;
  * 子任务的开始时间 —— 由主任务时间 + 前面几项的持续时间顺推, 不是一个一个手输。

界面层 (ui/subtask_wizard.py, ui/notepad.py) 只负责把这里的状态搬到控件上。
纯逻辑意味着这些语义可以在无头环境下逐条断言, 演示当天不会翻车。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

log = logging.getLogger("reminder.tasks")

# 只允许二级: 0 = 主任务, 1 = 子任务 (PRD 2.5 "二级目录结构")
MAX_DEPTH = 1

GROUP_MODES = ("time", "level", "date")
GROUP_MODE_LABELS = {"time": "按时间(默认)", "level": "按重要程度", "date": "按开始日期"}
DEFAULT_GROUP_MODE = "time"

_LEVEL_RANK = {"紧急": 0, "重要": 1, "日常": 2}
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

MAX_WIZARD_COUNT = 20
DEFAULT_DURATION_MIN = 30


class SubtaskError(RuntimeError):
    """二级目录规则被违反 (例如想给子任务再挂子任务)。"""


# ====================================================================== 时间顺推

def chain_times(start: datetime | None, durations_min: list[int],
                offsets_min: list[int] | None = None) -> list[datetime | None]:
    """子任务的事件时刻: 第 1 个接在 start, 之后依次 + 前一个的持续时间。

    起点缺省时返回一列 None —— 调用方 (向导) 会退化成"没有绝对时间的子任务",
    而不是抛异常: 用户盯着对话框填名字的时候, 不该因为主任务没时间就报错。
    """
    if start is None:
        return [None] * len(durations_min)
    out: list[datetime | None] = []
    cursor = start
    for i, dur in enumerate(durations_min):
        if offsets_min and i < len(offsets_min):
            cursor = start + timedelta(minutes=offsets_min[i])
        out.append(cursor)
        cursor = cursor + timedelta(minutes=max(0, int(dur)))
    return out


# ====================================================================== 向导状态

@dataclass
class SubTaskSpec:
    title: str = ""
    duration_min: int = DEFAULT_DURATION_MIN

    def clean_title(self) -> str:
        return self.title.strip()

    def is_filled(self) -> bool:
        return bool(self.clean_title())

    def to_json(self) -> dict:
        return {"title": self.title, "duration_min": int(self.duration_min)}

    @staticmethod
    def from_json(d: dict) -> "SubTaskSpec":
        try:
            dur = int(d.get("duration_min", DEFAULT_DURATION_MIN))
        except (TypeError, ValueError):
            dur = DEFAULT_DURATION_MIN
        return SubTaskSpec(title=str(d.get("title") or ""), duration_min=max(1, min(24 * 60, dur)))


@dataclass
class WizardState:
    """向导式创建的完整状态机 —— 可以随时序列化成草稿, 也可以摊平成一张表。

    step 模式: 一次填一个, 点"下一步"进下一个 (PRD 的原始描述);
    list 模式: "转普通列表视图", 一屏填完再保存 (PRD 风险预警第 4 条给的对策)。
    两种模式共用同一份 specs, 来回切换不丢数据。
    """

    specs: list[SubTaskSpec] = field(default_factory=list)
    index: int = 0
    mode: str = "step"          # step | list
    start_at: datetime | None = None
    parent_title: str = ""

    @staticmethod
    def create(count: int, start_at: datetime | None = None,
               parent_title: str = "", duration_min: int = DEFAULT_DURATION_MIN) -> "WizardState":
        count = max(1, min(MAX_WIZARD_COUNT, int(count)))
        return WizardState(specs=[SubTaskSpec(duration_min=duration_min) for _ in range(count)],
                           start_at=start_at, parent_title=parent_title)

    # ------------------------------------------------------------ 导航
    @property
    def count(self) -> int:
        return len(self.specs)

    @property
    def current(self) -> SubTaskSpec:
        return self.specs[min(self.index, self.count - 1)]

    @property
    def is_last(self) -> bool:
        return self.index >= self.count - 1

    @property
    def step_hint(self) -> str:
        """给对话框标题用: "第 2/3 个子任务"。"""
        return f"第 {self.index + 1}/{self.count} 个子任务"

    def set_current(self, title: str, duration_min: int) -> None:
        self.current.title = title
        self.current.duration_min = max(1, min(24 * 60, int(duration_min)))

    def next(self) -> bool:
        """进下一个。已经是最后一个则原地不动并返回 False (由调用方改成"完成")。"""
        if self.is_last:
            return False
        self.index += 1
        return True

    def back(self) -> bool:
        if self.index <= 0:
            return False
        self.index -= 1
        return True

    def goto(self, i: int) -> None:
        self.index = max(0, min(self.count - 1, int(i)))

    # ------------------------------------------------------------ 校验
    def validate_current(self) -> str | None:
        """返回给用户看的错误文本, 没有错误返回 None。

        只要求"名字非空": 持续时间有默认值, 强制用户填只会让向导更烦。
        """
        if not self.current.clean_title():
            return f"第 {self.index + 1} 个子任务还没有名字"
        dup = [s.clean_title() for s in self.specs if s.is_filled()]
        if len(dup) != len(set(dup)):
            return "有两个子任务重名了, 改一个吧"
        return None

    def validate_all(self, require_all: bool = True) -> str | None:
        if require_all:
            empty = [i + 1 for i, s in enumerate(self.specs) if not s.is_filled()]
            if empty:
                return "第 " + "、".join(str(i) for i in empty) + " 个子任务还没有名字"
        names = [s.clean_title() for s in self.specs if s.is_filled()]
        if len(names) != len(set(names)):
            return "有两个子任务重名了, 改一个吧"
        return None

    # ------------------------------------------------------------ 结果
    def filled_specs(self) -> list[SubTaskSpec]:
        return [s for s in self.specs if s.is_filled()]

    def times(self) -> list[datetime | None]:
        return chain_times(self.start_at, [s.duration_min for s in self.specs])

    def plan_rows(self) -> list[tuple[str, int, datetime | None]]:
        """给界面的预览: (名字, 持续分钟, 开始时刻)。"""
        return [(s.clean_title() or "(未填)", s.duration_min, t)
                for s, t in zip(self.specs, self.times())]

    # ------------------------------------------------------------ 草稿
    def to_draft(self) -> str:
        return json.dumps({
            "v": 1, "mode": self.mode, "index": self.index, "parent": self.parent_title,
            "start": self.start_at.isoformat() if self.start_at else None,
            "specs": [s.to_json() for s in self.specs],
        }, ensure_ascii=False)

    @staticmethod
    def from_draft(raw: str | None, start_at: datetime | None = None) -> "WizardState | None":
        if not raw:
            return None
        try:
            d = json.loads(raw)
            specs = [SubTaskSpec.from_json(x) for x in (d.get("specs") or [])]
        except (ValueError, TypeError):
            log.warning("向导草稿损坏, 忽略: %r", raw[:80] if raw else raw)
            return None
        if not specs:
            return None
        st = WizardState(specs=specs, index=int(d.get("index", 0)),
                         mode=str(d.get("mode") or "step"),
                         start_at=start_at, parent_title=str(d.get("parent") or ""))
        st.goto(st.index)
        return st


# ====================================================================== 排序 / 分组

def _time_key(item: dict):
    return (item.get("next_fire") or item.get("event_at") or datetime.max, item.get("id") or 0)


def _rank(level: str) -> int:
    return _LEVEL_RANK.get(level, 3)


def sort_key_for(mode: str, now: datetime | None = None):
    """顶层任务 (主任务) 的排序键。初始默认严格按时间 —— 这是 PRD 的硬要求。"""
    if mode == "level":
        return lambda it: (_rank(it.get("level") or ""),) + _time_key(it)
    return _time_key


def group_label(item: dict, mode: str, now: datetime | None = None) -> str:
    """分组表头文本。'time' 模式不分组 (返回空串)。"""
    if mode == "level":
        return f"「{item.get('level') or '日常'}」"
    if mode != "date":
        return ""
    dt = item.get("next_fire") or item.get("event_at")
    if dt is None:
        return "没定时间的"
    if now is None:
        now = datetime.now(dt.tzinfo)
    days = (dt.date() - now.date()).days
    if days == 0:
        return "今天"
    if days == 1:
        return "明天"
    if days == 2:
        return "后天"
    if days < 0:
        return f"{dt:%m-%d} 已过期"
    return f"{dt:%m-%d} {_WEEKDAYS[dt.weekday()]}"


def order_tree(items: list[dict], mode: str = DEFAULT_GROUP_MODE,
               now: datetime | None = None) -> list[dict]:
    """把扁平的任务列表排成"主任务 + 它下面的子任务"的顺序, 并打好分组表头。

    返回的是**扁平列表** (parent 后面紧跟它的 children), 每项带 depth/group_label;
    界面按 depth 缩进、遇到 group_label 就画一条分组线。不做嵌套结构是因为
    便签界面本来就是"一行一行往下写"的, 嵌套数据在差量更新时反而更难保持稳定。

    孤儿 (父任务已完成/已删除) 会被提升到顶层: 否则用户看不到它们, 而提醒还在响 ——
    这正是 PRD 里最恶心的那类"幽灵提醒"。
    """
    parents: list[dict] = []
    children: dict[str, list[dict]] = {}
    for it in items:
        pu = it.get("parent_uuid") or ""
        if pu:
            children.setdefault(pu, []).append(it)
        else:
            parents.append(it)

    parents.sort(key=sort_key_for(mode, now))
    out: list[dict] = []
    live: set[str] = set()
    for p in parents:
        row = dict(p)
        row["depth"] = 0
        live.add(row.get("uuid") or "")
        out.append(row)
        kids = sorted(children.get(row.get("uuid") or "", []),
                      key=lambda x: (x.get("sort_key") or 0.0, x.get("id") or 0))
        for k in kids:
            krow = dict(k)
            krow["depth"] = MAX_DEPTH
            krow["orphan"] = False
            live.add(krow.get("uuid") or "")
            out.append(krow)
        row["child_count"] = len(kids)

    orphans = [it for it in items if (it.get("parent_uuid") or "") and (it.get("uuid") or "") not in live]
    for it in sorted(orphans, key=_time_key):
        row = dict(it)
        row["depth"] = 0
        row["orphan"] = True
        out.append(row)

    prev = None
    for row in out:
        if row["depth"]:
            row["group_label"] = ""
            continue
        lab = group_label(row, mode, now)
        row["group_label"] = lab if lab != prev else ""
        prev = lab
    return out
