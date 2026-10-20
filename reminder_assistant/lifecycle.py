"""事件生命周期: 完成 / 过期静默清理 / 历史归档。

PRD 2.2 的"优先逻辑 + 兜底逻辑"在这里落地:
    优先: 用户点"划掉" -> complete() 立即终止未来触发点并写 completed_at
    兜底: 到 expire_at 仍未处理 -> sweep() 在后台静默转 expired 并取消触发点
          (不弹窗、不写日志到用户可见面, 只在界面里留一条历史)
每次 sweep 都是 O(过期条数) 的索引扫描, 不是全表轮询。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import Reminder, ReminderPlan
from .scheduler import Scheduler
from .store import Store, StoreError
from .tasks import DEFAULT_GROUP_MODE, MAX_DEPTH, SubtaskError, chain_times, order_tree

log = logging.getLogger("reminder.lifecycle")

# 兜底失效时间: 事件时刻之后多久仍未处理就静默清理
DEFAULT_DEADLINES: dict[str, timedelta] = {
    "日常": timedelta(hours=24),
    "重要": timedelta(hours=8),
    "紧急": timedelta(hours=2),
}


@dataclass
class Lifecycle:
    store: Store
    scheduler: Scheduler
    deadlines: dict[str, timedelta] = None

    def __post_init__(self):
        self.deadlines = dict(DEFAULT_DEADLINES if self.deadlines is None else self.deadlines)

    def deadline_for(self, level: str) -> timedelta:
        return self.deadlines.get(level, DEFAULT_DEADLINES["日常"])

    # ------------------------------------------------------------ 创建
    def create(self, rem: Reminder, now: datetime | None = None) -> Reminder:
        now = now or self.scheduler.clock.now()
        rem.created_at = rem.created_at or now
        if rem.event_at is not None and rem.expire_at is None:
            rem.expire_at = rem.event_at + self.deadline_for(rem.level)
        return self.scheduler.add(rem)

    # ------------------------------------------------------------ 二级目录 (2.5)
    def add_child(self, parent: Reminder, title: str, duration_min: int = 0,
                  event_at: datetime | None = None, level: str | None = None,
                  plan: ReminderPlan | None = None, now: datetime | None = None) -> Reminder:
        """在一条主任务下建子任务。

        三条规矩:
          * 只允许二级 —— 给子任务再挂子任务会被当场拦住 (PRD 2.5);
          * sort_key 取同级最大值 +1, 所以用户看到的顺序就是创建顺序;
          * 子任务是一条**真提醒**: 它有自己的触发点, 到点照样弹窗 (不是只画在界面上的装饰)。
        """
        if parent.id is None or not parent.uuid:
            raise SubtaskError("主任务还没入库, 不能建子任务")
        if parent.depth >= MAX_DEPTH:
            raise SubtaskError("只支持二级目录: 子任务下面不能再建子任务")
        now = now or self.scheduler.clock.now()
        child = Reminder(
            title=title.strip(), source_text=parent.title,
            event_at=event_at if event_at is not None else parent.event_at,
            tz=parent.tz or str(now.tzinfo), level=level or parent.level,
            plan=plan or parent.plan, created_at=now,
            duration_min=max(0, int(duration_min)),
            parent_uuid=parent.uuid, depth=parent.depth + 1,
            sort_key=self.store.next_sort_key(parent.uuid))
        return self.create(child, now)

    def add_children(self, parent: Reminder, specs: list, now: datetime | None = None) -> list[Reminder]:
        """按向导的结果批量建子任务。specs 里每项要有 title / duration_min / 可选 event_at。

        开始时间是**顺推**出来的: 第 1 个接在主任务的事件时刻上, 之后每个 = 上一个 + 它的持续时间。
        这件事必须在这里做, 不能只放在向导对话框里 —— CLI(--subtasks)、同步桥、以后的手机端
        都会走这个入口, 少一处就会出现"所有子任务都挤在同一时刻"。
        主任务没有绝对时间时用"现在"起步 (否则触发点无从展开)。
        一次性建完而不是"每填一格写一条": 用户中途取消时不能留下半截任务树。
        """
        now = now or self.scheduler.clock.now()
        durations: list[int] = []
        explicit: list[datetime | None] = []
        for s in specs:
            if isinstance(s, dict):
                durations.append(int(s.get("duration_min") or 0))
                explicit.append(s.get("event_at"))
            else:
                durations.append(int(getattr(s, "duration_min", 0) or 0))
                explicit.append(getattr(s, "event_at", None))
        times = chain_times(parent.event_at or now, durations)
        out: list[Reminder] = []
        for spec, when, given in zip(specs, times, explicit):
            out.append(self.add_child(
                parent,
                title=getattr(spec, "title", "") if not isinstance(spec, dict) else spec.get("title", ""),
                duration_min=getattr(spec, "duration_min", 0) if not isinstance(spec, dict)
                else spec.get("duration_min", 0),
                event_at=given if given is not None else when,
                now=now))
        log.info("主任务 #%s 下新增 %d 个子任务", parent.id, len(out))
        return out

    def children(self, rid: int) -> list[Reminder]:
        return self.store.children_of_id(rid)

    # ------------------------------------------------------------ 事件属性 (2.5)
    def update_attributes(self, rid: int, now: datetime | None = None, **fields) -> Reminder:
        """改一条提醒的属性, 并让改动**真的生效**。

        改时间是最容易做错的一步: 只 UPDATE 库里的 event_at, 触发点还是老的 ——
        用户改完时间, 提醒还是按老时间响。所以这里一律重排触发点并唤醒调度器。
        改主任务的时间时, 它的子任务整条链按同样的位移跟着走 (保持相对间隔)。
        """
        rem = self.store.get_reminder(rid)
        if rem is None:
            raise StoreError(f"提醒 {rid} 不存在")
        old_event = rem.event_at
        if not self.store.update_attributes(rid, **fields):
            return rem
        updated = self.store.get_reminder(rid)
        if updated is None:
            return rem
        if "event_at" in fields and updated.event_at != old_event:
            self._replan(updated)
            if updated.depth == 0 and old_event and updated.event_at:
                self._shift_children(updated, updated.event_at - old_event)
        return updated

    def _replan(self, rem: Reminder) -> None:
        """按当前的 event_at + plan 重排触发点。没有绝对时间就跳过 (无从排起)。"""
        if rem.event_at is None:
            return
        self.store.replace_plan(rem.id, rem.plan)
        self.scheduler.refresh(rem.id)
        log.info("提醒 #%s 的触发点已按新时间重排", rem.id)

    def _shift_children(self, parent: Reminder, delta) -> None:
        if delta == timedelta(0):
            return
        for child in self.store.children_of(parent.uuid):
            if child.event_at is None:
                continue
            self.store.update_attributes(
                child.id, event_at=child.event_at + delta,
                expire_at=(child.expire_at + delta) if child.expire_at else None)
            again = self.store.get_reminder(child.id)
            if again is not None:
                self._replan(again)
        log.info("主任务 #%s 的时间位移 %s, 子任务一起平移", parent.id, delta)

    # ------------------------------------------------------------ 优先逻辑
    def complete(self, rid: int, now: datetime | None = None) -> int:
        """用户"划掉": 事件立即销毁, 未来触发点全部取消, 归档进历史。

        主任务被划掉时, 它下面的子任务一起完成 —— 否则子任务的提醒会在
        "主任务已经没有了"的情况下继续响, 而用户在便签上已经看不到它们。
        """
        now = now or self.scheduler.clock.now()
        cancelled = self.store.complete_tree(rid, now)
        self.scheduler.wake()
        return cancelled

    def remove(self, rid: int, now: datetime | None = None) -> int:
        """真删一条 (连带子任务, 见 store.delete_tree) —— 与"划掉"是两件事。"""
        n = self.store.delete_tree(rid)
        self.scheduler.wake()
        return n

    def snooze(self, rid: int, idx: int = 0, minutes: int = 5) -> datetime:
        return self.scheduler.snooze(rid, idx, delta=timedelta(minutes=minutes))

    def change_plan(self, rid: int, plan: ReminderPlan, now: datetime | None = None) -> int:
        """改某条提醒的提醒频率 (PRD"高级事件"面板)。

        注意不能只改库里的 plan 字段: 必须真的重排触发点并重建堆,
        否则用户改完界面还是按旧频率响。
        """
        store = self.store
        base = store.replace_plan(rid, plan)
        self.scheduler.refresh(rid)
        log.info("提醒 #%s 的频率已重排, 共 %d 次 (代际 %d)", rid, len(plan.offsets), base)
        return base

    # ------------------------------------------------------------ 兜底逻辑
    def sweep(self, now: datetime | None = None) -> list[int]:
        now = now or self.scheduler.clock.now()
        ids = self.store.expire_due(now)
        if ids:
            self.scheduler.wake()
        return ids

    # ------------------------------------------------------------ 历史
    def history(self, limit: int = 50, offset: int = 0) -> list[Reminder]:
        return self.store.list_history(limit, offset)

    def history_count(self) -> int:
        """历史总条数 —— 便签按"每页 N 条"分页时用它算页数。"""
        return self.store.count_history()

    def archive_prune(self, days: int = 30, now: datetime | None = None) -> int:
        return self.store.prune(timedelta(days=days), now)

    # ------------------------------------------------------------ 快照
    def board(self, now: datetime | None = None, mode: str = DEFAULT_GROUP_MODE) -> dict:
        """便签界面要的数据: 正在进行的 + 最近归档的。

        active 是**主任务后面紧跟它的子任务**的顺序 (tasks.order_tree), 每项带
        depth / group_label: 界面只要按这些字段缩进和画分组线, 不用再自己排一遍。
        mode 决定顶层排序: time (默认, 按时间) / level (按重要程度) / date (按开始日期)。
        """
        now = now or self.scheduler.clock.now()
        active = self.store.list_active()
        out = []
        for rem in active:
            occs = [self.store.occurrence(rem.id, i) for i in range(len(rem.plan.offsets))]
            nxt = next((o for o in occs if o and o.state == "pending"), None)
            out.append({
                "id": rem.id, "uuid": rem.uuid, "title": rem.title, "level": rem.level,
                "event_at": rem.event_at, "expire_at": rem.expire_at,
                "next_fire": nxt.fire_at if nxt else None,
                "fired": sum(1 for o in occs if o and o.state == "fired"),
                "total": len(rem.plan.offsets),
                "overdue": bool(rem.expire_at and rem.expire_at <= now),
                "depth": rem.depth, "parent_uuid": rem.parent_uuid, "sort_key": rem.sort_key,
                "duration_min": rem.duration_min, "note": rem.note, "status": rem.status,
            })
        return {"now": now, "mode": mode, "active": order_tree(out, mode, now),
                "history": self.history(20)}
