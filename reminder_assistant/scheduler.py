"""单线程最小堆调度器。

工程师清单第 1 条的五个考点, 对应五项设计:

1. 定时器漂移
   不累计倒计时。每次醒来都用 (fire_at - clock.now()) 重算剩余量, 误差不累积。
2. 系统休眠/待机
   Windows 上 time.monotonic() 在待机期间不推进, 而挂钟推进 -> 两者差值就是休眠时长。
   检测到后立刻扫描堆顶, 把已到期但没发出去的触发点补发 (带 late_by 标记)。
3. 系统时间被修改
   堆里存的是"绝对时刻", 所以改时间后重新计算即可自动纠正;
   同时上报 on_clock_change, 让 UI 提示"已按新时间重排"。
   绝对提醒 (明天 9 点) 永远锚定墙上时间, 不因休眠而漂移。
4. 漏触发 / 重复触发
   漏: 上面第 2 条。重复: fired_log 主键 + 代际号 gen 双重去重。
5. 延迟提醒更新原任务
   snooze() 走 store.reschedule_occurrence (原地 UPDATE + gen+1),
   旧堆条目因 gen 不匹配被丢弃, 不会多弹一次。
"""

from __future__ import annotations

import heapq
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from .clock import Clock, SystemClock
from .models import Reminder
from .store import Store

log = logging.getLogger("reminder.scheduler")

# 唤醒上限: 即使最近一个触发点还在 3 小时后, 也至少每 30 秒醒一次。
# 这不是轮询业务数据, 只是给"改时间/休眠/外部插入"一个安全网, 开销可忽略。
DEFAULT_MAX_SLEEP = 30.0
CLOCK_TOLERANCE = 2.0  # 秒; 挂钟与单调钟偏离超过它就认为时钟被改/休眠过


@dataclass(order=True)
class _Item:
    fire_at: datetime
    seq: int
    key: str = field(compare=False)
    reminder_id: int = field(compare=False)
    index: int = field(compare=False)
    gen: int = field(compare=False)


@dataclass
class Fired:
    reminder: Reminder
    index: int
    fire_at: datetime
    at: datetime
    late_by: float = 0.0       # 秒
    snoozed: bool = False

    @property
    def is_late(self) -> bool:
        return self.late_by > 0.5


class Scheduler:
    def __init__(self, store: Store, clock: Clock | None = None, *,
                 on_fire: Callable[[Fired], None] | None = None,
                 on_clock_change: Callable[[float, float], None] | None = None,
                 on_expire: Callable[[list[int]], None] | None = None,
                 max_sleep: float = DEFAULT_MAX_SLEEP,
                 late_grace: timedelta = timedelta(hours=12),
                 sweep_interval: float = 60.0):
        self.store = store
        self.clock = clock or SystemClock()
        self.on_fire = on_fire
        self.on_clock_change = on_clock_change
        self.on_expire = on_expire
        self.max_sleep = max_sleep
        self.late_grace = late_grace
        self.sweep_interval = sweep_interval

        self._heap: list[_Item] = []
        self._gen: dict[str, int] = {}
        self._seq = 0
        self._cv = threading.Condition(threading.RLock())
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._last_wall: datetime | None = None
        self._last_mono: float | None = None
        self._last_sweep = 0.0

        self.stats = {"fired": 0, "late": 0, "missed": 0, "skipped_dup": 0,
                      "clock_changes": 0, "suspends": 0, "expired": 0, "wakeups": 0}

    # ------------------------------------------------------------ 装载
    def load(self) -> int:
        """启动/重启后从库里重建堆 (崩溃恢复的入口)。"""
        with self._cv:
            self._heap.clear()
            self._gen.clear()
            n = 0
            for occ in self.store.pending_occurrences():
                self._push(occ.reminder_id, occ.index, occ.fire_at, occ.gen)
                n += 1
            self._cv.notify_all()
        return n

    def schedule(self, rem: Reminder) -> int:
        """把一个提醒的所有触发点推进堆。"""
        if rem.id is None:
            raise ValueError("提醒需要先落库")
        with self._cv:
            for occ in self.store.pending_occurrences(only_active=False):
                if occ.reminder_id != rem.id:
                    continue
                self._push(occ.reminder_id, occ.index, occ.fire_at, occ.gen)
            self._cv.notify_all()
        return self.size

    def _push(self, rid: int, idx: int, fire_at: datetime, gen: int) -> None:
        self._seq += 1
        self._gen[_key(rid, idx)] = gen
        heapq.heappush(self._heap, _Item(fire_at, self._seq, _key(rid, idx), rid, idx, gen))

    @property
    def size(self) -> int:
        with self._cv:
            return len(self._heap)

    def wake(self) -> None:
        """外部插入/改期后立刻叫醒线程, 避免等到 max_sleep。"""
        with self._cv:
            self._cv.notify_all()

    def refresh(self, rid: int) -> int:
        """某条提醒的触发点在库里被重排后, 把它重新压进堆。

        旧条目不用手动删: 它们带着旧代际号, 会在冒到堆顶时被 _prune_head 丢掉。
        """
        n = 0
        with self._cv:
            for occ in self.store.pending_occurrences(only_active=False):
                if occ.reminder_id == rid and occ.state == "pending":
                    self._push(rid, occ.index, occ.fire_at, occ.gen)
                    n += 1
            self._cv.notify_all()
        return n

    # ------------------------------------------------------------ 核心
    def next_due(self) -> datetime | None:
        with self._cv:
            self._prune_head()
            return self._heap[0].fire_at if self._heap else None

    def _prune_head(self) -> None:
        while self._heap:
            top = self._heap[0]
            if self._gen.get(top.key, top.gen) != top.gen:
                heapq.heappop(self._heap)   # 代际过期 (被 snooze/改期淘汰)
            else:
                break

    def poll(self, now: datetime | None = None, *, limit: int = 64) -> list[Fired]:
        """处理所有已到期的触发点。测试里直接调用它, 线程里由 loop 调用。"""
        now = now or self.clock.now()
        out: list[Fired] = []
        while len(out) < limit:
            with self._cv:
                self._prune_head()
                if not self._heap or self._heap[0].fire_at > now:
                    break
                item = heapq.heappop(self._heap)
            fired = self._fire(item, now)
            if fired:
                out.append(fired)
        return out

    def _fire(self, item: _Item, now: datetime) -> Fired | None:
        late_by = (now - item.fire_at).total_seconds()
        if late_by > self.late_grace.total_seconds():
            # 迟到太久 (比如关机两天后开机), 静默判错过, 不再打扰
            self.store.mark_missed(item.reminder_id, item.index, now, late_by)
            self.stats["missed"] += 1
            log.info("触发点 %s 迟到 %.0fs 超过宽限, 判为错过", item.key, late_by)
            return None
        if not self.store.try_record_fire(f"{item.key}@{item.gen}", now):
            self.stats["skipped_dup"] += 1      # 崩溃重启后重放, 不重复弹
            return None
        self.store.mark_fired(item.reminder_id, item.index, now, max(late_by, 0.0))
        rem = self.store.get_reminder(item.reminder_id)
        if rem is None or rem.status != "active":
            return None
        fired = Fired(reminder=rem, index=item.index, fire_at=item.fire_at, at=now,
                      late_by=max(late_by, 0.0))
        self.stats["fired"] += 1
        if fired.is_late:
            self.stats["late"] += 1
        if self.on_fire:
            try:
                self.on_fire(fired)
            except Exception:                   # 通知失败不能拖垮调度线程
                log.exception("on_fire 回调失败: %s", item.key)
        return fired

    # ------------------------------------------------------------ 用户操作
    def snooze(self, rid: int, idx: int, delta: timedelta | None = None,
               until: datetime | None = None) -> datetime:
        """延迟提醒 = 改期原任务 (gen+1), 不是新建一条。"""
        occ = self.store.occurrence(rid, idx)
        if occ is None:
            raise KeyError(f"触发点 {rid}#{idx} 不存在")
        when = until or (self.clock.now() + (delta or timedelta(minutes=5)))
        gen = self.store.reschedule_occurrence(rid, idx, when)
        with self._cv:
            self._push(rid, idx, when, gen)
            self._cv.notify_all()
        return when

    def complete(self, rid: int) -> int:
        n = self.store.complete_reminder(rid, self.clock.now())
        with self._cv:
            self._prune_head()
            self._cv.notify_all()
        return n

    def add(self, rem: Reminder) -> Reminder:
        """落库 + 进堆 + 叫醒线程。"""
        rem = self.store.add_reminder(rem)
        with self._cv:
            for occ in rem.occurrences():
                self._push(rem.id, occ.index, occ.fire_at, occ.gen)
            self._cv.notify_all()
        return rem

    # ------------------------------------------------------------ 主循环
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread:
            self._thread.join(timeout)
            self._thread = None

    def _loop(self) -> None:
        self._last_wall = self.clock.now()
        self._last_mono = self.clock.mono()
        while not self._stopping.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("调度循环异常, 5 秒后重试")
                time.sleep(5)

    def _tick(self) -> None:
        now, mono = self.clock.now(), self.clock.mono()
        self._detect_clock_events(now, mono)
        self.stats["wakeups"] += 1

        # 到期的先处理, 处理完再算下一次该睡多久
        self.poll(now)

        self._maybe_sweep(now, mono)

        nxt = self.next_due()
        wait = self.max_sleep if nxt is None else min(
            self.max_sleep, max(0.0, (nxt - self.clock.now()).total_seconds()))
        with self._cv:
            self._cv.wait(wait)

    def _detect_clock_events(self, now: datetime, mono: float) -> None:
        if self._last_wall is None:
            self._last_wall, self._last_mono = now, mono
            return
        d_wall = (now - self._last_wall).total_seconds()
        d_mono = mono - self._last_mono
        drift = d_wall - d_mono
        if abs(drift) > CLOCK_TOLERANCE:
            # 挂钟跑得远超单调钟 -> 中间机器睡着了; 否则是系统时间被改/NTP 跳变
            if d_wall > CLOCK_TOLERANCE and d_mono < d_wall * 0.5:
                self.stats["suspends"] += 1
                log.warning("检测到休眠/待机: 挂钟前进 %.0fs, 单调钟前进 %.0fs, 开始补发",
                            d_wall, d_mono)
            else:
                self.stats["clock_changes"] += 1
                log.warning("检测到系统时间被修改: 秒差 %.0fs", drift)
            if self.on_clock_change:
                try:
                    self.on_clock_change(d_wall, d_mono)
                except Exception:
                    log.exception("on_clock_change 回调失败")
        self._last_wall, self._last_mono = now, mono

    def _maybe_sweep(self, now: datetime, mono: float) -> None:
        if mono - self._last_sweep < self.sweep_interval:
            return
        self._last_sweep = mono
        ids = self.store.expire_due(now)
        if ids:
            self.stats["expired"] += len(ids)
            log.info("兜底清理 %d 条过期提醒", len(ids))
            with self._cv:
                self._prune_head()
            if self.on_expire:
                try:
                    self.on_expire(ids)
                except Exception:
                    log.exception("on_expire 回调失败")

    # ------------------------------------------------------------ 诊断
    def diagnose(self) -> dict:
        d = dict(self.stats)
        d.update({"heap": self.size, **self.store.stats()})
        d["next_due"] = self.next_due().isoformat() if self.next_due() else None
        return d


def _key(rid: int, idx: int) -> str:
    return f"{rid}#{idx}"
