"""持久化层 (SQLite / WAL)。

针对工程师清单里"数据与可靠性"的要求逐条落实:

* 提醒持久化      -> reminders + occurrences 两张表, 事务写入
* 崩溃恢复        -> occurrences.state='firing' 表示"已出队但未必送达";
                     启动时 recover_inflight() 把它们退回 pending, 重投一次
* 通知去重        -> fired_log / notifications 两把主键锁, INSERT OR IGNORE 天然幂等
* 多实例冲突      -> 数据目录独占锁 (msvcrt/fcntl), 第二个实例拿到 StoreLocked
* 存储失败降级    -> 写入异常抛出 StoreError, 由上层降级为内存模式并提示用户
* 最小权限/隐私   -> 只落本地文件; 内容默认不加密但预留 cipher 钩子 (见 docs)
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .models import Occurrence, Reminder, ReminderPlan


class StoreError(RuntimeError):
    pass


class StoreLocked(StoreError):
    """已有另一个实例在跑, 本实例必须退出或转只读。"""


def _s(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _d(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


SCHEMA_VERSION = 2

# reminders 的同步列 (列名, 建列声明) —— **唯一定义**。
# 新建库走下面的 SCHEMA, 老库走 _add_missing_columns() 的 ALTER;
# 两条路径必须收敛到同一结构, tests/test_migration.py 有一条"结构不漂移"用例钉住。
_REMINDER_SYNC_COLUMNS: tuple[tuple[str, str], ...] = (
    ("uuid", "TEXT NOT NULL DEFAULT ''"),
    ("parent_uuid", "TEXT NOT NULL DEFAULT ''"),
    ("depth", "INTEGER NOT NULL DEFAULT 0"),
    ("sort_key", "REAL NOT NULL DEFAULT 0"),
    ("batch_id", "TEXT NOT NULL DEFAULT ''"),
    ("version", "INTEGER NOT NULL DEFAULT 0"),
    ("server_seq", "INTEGER NOT NULL DEFAULT 0"),
    ("dirty", "INTEGER NOT NULL DEFAULT 0"),
    ("sync_state", "TEXT NOT NULL DEFAULT 'clean'"),
    # 2.5 任务管理: 事件属性里的"持续时间"。放在最后是因为老库只能逐列 append ——
    # SCHEMA 里的列序必须与这里的顺序一致, 否则"结构不漂移"用例会红。
    ("duration_min", "INTEGER NOT NULL DEFAULT 0"),
)

# 扩展字段的信任列表: update_attributes() 只认这些名字 (table 列名 -> 取值函数)。
# 有它才不会出现"界面上多了一个输入框就顺手 UPDATE 一个不存在的列"。
ATTRIBUTE_FIELDS: tuple[str, ...] = ("title", "event_at", "level", "note", "duration_min",
                                     "parent_uuid", "depth", "sort_key", "expire_at")



def new_uuid() -> str:
    """同步主键: UUIDv7 (时间有序, 按创建顺序自然排序)。"""
    return str(uuid.uuid7())


SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders(
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  title        TEXT    NOT NULL DEFAULT '',
  source_text  TEXT    NOT NULL DEFAULT '',
  event_at     TEXT,
  tz           TEXT    NOT NULL DEFAULT '',
  level        TEXT    NOT NULL DEFAULT '日常',
  plan         TEXT    NOT NULL DEFAULT '[]',
  status       TEXT    NOT NULL DEFAULT 'active',
  created_at   TEXT    NOT NULL,
  completed_at TEXT,
  expire_at    TEXT,
  bbox         TEXT,
  note         TEXT    NOT NULL DEFAULT '',
  -- 跨端同步列: 列序与 _add_missing_columns() 的 ALTER 顺序一致, 见 _REMINDER_SYNC_COLUMNS
  uuid         TEXT    NOT NULL DEFAULT '',       -- 同步主键 (UUIDv7)
  parent_uuid  TEXT    NOT NULL DEFAULT '',       -- 二级目录: 主任务 uuid
  depth        INTEGER NOT NULL DEFAULT 0,        -- 0=主任务 1=子任务
  sort_key     REAL    NOT NULL DEFAULT 0,        -- 同级内分数排序键
  batch_id     TEXT    NOT NULL DEFAULT '',       -- 向导式批量创建批次
  version      INTEGER NOT NULL DEFAULT 0,        -- 权威版本号
  server_seq   INTEGER NOT NULL DEFAULT 0,        -- 云端单调序号 (阶段二写)
  dirty        INTEGER NOT NULL DEFAULT 0,        -- 1=有未同步改动
  sync_state   TEXT    NOT NULL DEFAULT 'clean',  -- clean|dirty|conflicted
  duration_min INTEGER NOT NULL DEFAULT 0         -- 事件属性: 持续时间(分钟)
);
CREATE INDEX IF NOT EXISTS ix_rem_status_expire ON reminders(status, expire_at);

CREATE TABLE IF NOT EXISTS occurrences(
  reminder_id INTEGER NOT NULL,
  idx         INTEGER NOT NULL,
  gen         INTEGER NOT NULL DEFAULT 0,
  fire_at     TEXT    NOT NULL,
  state       TEXT    NOT NULL DEFAULT 'pending',
  fired_at    TEXT,
  late_by     REAL    NOT NULL DEFAULT 0,
  PRIMARY KEY(reminder_id, idx),
  FOREIGN KEY(reminder_id) REFERENCES reminders(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_occ_state_fire ON occurrences(state, fire_at);

CREATE TABLE IF NOT EXISTS fired_log(
  key TEXT PRIMARY KEY, fired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications(
  key TEXT PRIMARY KEY, delivered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);

-- 待发箱: 离线期间的操作积压在这里, 联网后按 id 顺序重放 (阶段二消费; 阶段一只写模型)
CREATE TABLE IF NOT EXISTS outbox(
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  op_id        TEXT    NOT NULL UNIQUE,        -- 幂等键, 重放一百次只生效一次
  uuid         TEXT    NOT NULL,               -- 目标提醒
  op_type      TEXT    NOT NULL,               -- create|update|complete|uncomplete|delete|move|replan|expire_batch
  payload      TEXT    NOT NULL DEFAULT '{}',
  base_version INTEGER NOT NULL DEFAULT 0,     -- 产生该操作时本端的版本号
  created_at   TEXT    NOT NULL,
  state        TEXT    NOT NULL DEFAULT 'pending',  -- pending|inflight|acked|dead
  attempts     INTEGER NOT NULL DEFAULT 0,
  last_error   TEXT,
  depend_on    TEXT                            -- 同批次的前序 op_id (向导批量创建)
);
CREATE INDEX IF NOT EXISTS ix_outbox_state ON outbox(state, id);

-- 向导式创建的草稿: 中途 Esc 不丢已填内容 (PRD 2.5)
CREATE TABLE IF NOT EXISTS draft(
  id          TEXT PRIMARY KEY,
  parent_uuid TEXT NOT NULL DEFAULT '',
  payload     TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path | None = None, *, exclusive: bool = True):
        self.path = Path(path) if path else default_data_dir() / "reminders.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._lockfile = None
        if exclusive:
            self._lockfile = _acquire_dir_lock(self.path.parent)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        try:
            self.migrate()          # 老库补列/回填 uuid; 新库是空操作
        except Exception:
            self.close()            # 迁移失败别把实例锁和连接留在那儿
            raise
        self.recover_inflight()

    # ------------------------------------------------------------ 迁移
    def user_version(self) -> int:
        with self._lock:
            return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> int:
        """把库升到 SCHEMA_VERSION。幂等: 重复调用是空操作。返回本次回填的行数。

        整个升级包在**一个事务**里 —— SQLite 的 DDL 也是事务性的, 所以中途失败会整批回滚,
        不会留下"加了三列、第四列失败"的半截库 (半截库最难查: 程序不报错, 只是某个字段永远是默认值)。
        新建的库由 SCHEMA 直接建到最新结构, 这里只补旧库缺的东西。
        """
        with self._lock:
            filled = 0
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self._add_missing_columns()
                filled = self._backfill_uuids()
                # 唯一索引必须在 uuid 回填之后建。且必须是**部分索引**:
                # 老代码/测试可能绕过 add_reminder 直接插 uuid='' 的行,
                # 全表唯一索引会在第二行这种数据上直接冲突。
                self.conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_rem_uuid"
                    " ON reminders(uuid) WHERE uuid <> ''")
                self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self.conn.commit()
                return filled
            except Exception:
                self.conn.rollback()
                raise

    def _add_missing_columns(self) -> list[str]:
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(reminders)")}
        added: list[str] = []
        for name, decl in _REMINDER_SYNC_COLUMNS:
            if name not in have:
                self.conn.execute(f"ALTER TABLE reminders ADD COLUMN {name} {decl}")
                added.append(name)
        return added

    def _backfill_uuids(self) -> int:
        """给老行补 uuid。SQLite 没有生成 UUID 的函数, 只能逐行生成再回填。"""
        rows = self.conn.execute(
            "SELECT id FROM reminders WHERE uuid IS NULL OR uuid=''").fetchall()
        for r in rows:
            self.conn.execute("UPDATE reminders SET uuid=? WHERE id=?", (new_uuid(), int(r["id"])))
        return len(rows)

    # ------------------------------------------------------------ 基础
    def close(self) -> None:
        with self._lock:
            if getattr(self, "_closed", False):
                return                       # 幂等: 关闭两次不应抛异常 (退出路径会重复调用)
            try:
                self.conn.commit()
            except Exception:
                pass
            finally:
                self.conn.close()
                self._closed = True
            if self._lockfile:
                _release_dir_lock(self._lockfile)
                self._lockfile = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ 提醒 CRUD
    def add_reminder(self, rem: Reminder) -> Reminder:
        rem.created_at = rem.created_at or datetime.now(rem.event_at.tzinfo if rem.event_at else None)
        if rem.expire_at is None and rem.event_at is not None:
            rem.expire_at = rem.event_at + timedelta(hours=24)
        with self._lock, self.conn:
            rem.uuid = rem.uuid or new_uuid()      # 同步主键由客户端生成: 断网也要能建任务
            cur = self.conn.execute(
                "INSERT INTO reminders(title, source_text, event_at, tz, level, plan, status,"
                " created_at, expire_at, bbox, note, uuid, parent_uuid, depth, sort_key, batch_id,"
                " duration_min)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rem.title, rem.source_text, _s(rem.event_at), rem.tz, rem.level,
                 rem.plan.to_json(), rem.status, _s(rem.created_at), _s(rem.expire_at),
                 json.dumps(rem.bbox) if rem.bbox else None, rem.note,
                 rem.uuid, rem.parent_uuid, rem.depth, rem.sort_key, rem.batch_id,
                 int(rem.duration_min)))
            rem.id = int(cur.lastrowid)
            for i, occ in enumerate(rem.occurrences()):
                self.conn.execute(
                    "INSERT INTO occurrences(reminder_id, idx, gen, fire_at, state)"
                    " VALUES(?,?,?,?, 'pending')",
                    (rem.id, i, occ.gen, _s(occ.fire_at)))
        return rem

    def get_reminder(self, rid: int) -> Reminder | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM reminders WHERE id=?", (rid,)).fetchone()
        if not row:
            return None
        return Reminder(
            id=row["id"], title=row["title"], source_text=row["source_text"],
            event_at=_d(row["event_at"]), tz=row["tz"], level=row["level"],
            plan=ReminderPlan.from_json(row["plan"]), status=row["status"],
            created_at=_d(row["created_at"]), completed_at=_d(row["completed_at"]),
            expire_at=_d(row["expire_at"]),
            bbox=tuple(json.loads(row["bbox"])) if row["bbox"] else None,
            note=row["note"], duration_min=int(row["duration_min"] or 0),
            uuid=row["uuid"], parent_uuid=row["parent_uuid"], depth=row["depth"],
            sort_key=row["sort_key"], batch_id=row["batch_id"], version=row["version"],
            server_seq=row["server_seq"], dirty=row["dirty"], sync_state=row["sync_state"])

    def list_active(self) -> list[Reminder]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM reminders WHERE status='active' ORDER BY id").fetchall()
        return [r for r in (self.get_reminder(row["id"]) for row in rows) if r]

    # ------------------------------------------------------------ 事件属性 (2.5)
    def update_attributes(self, rid: int, **fields) -> bool:
        """改一条提醒的属性 (标题/事件时间/等级/持续时间/备注)。

        只认 ATTRIBUTE_FIELDS 里的名字 —— 传错名字直接抛错, 而不是拼出一条
        `UPDATE reminders SET typo=?` 的坏 SQL。返回是否真的改到了行。
        注意: 改 event_at 之后**触发点必须重排**, 那是 Lifecycle 的事 (它还要唤醒调度器),
        这里只落库。
        """
        bad = set(fields) - set(ATTRIBUTE_FIELDS)
        if bad:
            raise StoreError(f"不认识的属性: {', '.join(sorted(bad))}")
        if not fields:
            return False
        cols, vals = [], []
        for k, v in fields.items():
            cols.append(f"{k}=?")
            vals.append(_s(v) if isinstance(v, datetime) else v)
        vals.append(rid)
        with self._lock, self.conn:
            cur = self.conn.execute(f"UPDATE reminders SET {', '.join(cols)} WHERE id=?", vals)
        return cur.rowcount > 0

    # ------------------------------------------------------------ 二级目录 (2.5)
    def children_of(self, parent_uuid: str) -> list[Reminder]:
        """按 sort_key 取子任务 —— 顺序稳定, 用户看到的顺序就是创建时的顺序。"""
        if not parent_uuid:
            return []
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM reminders WHERE parent_uuid=? AND status='active'"
                " ORDER BY sort_key, id", (parent_uuid,)).fetchall()
        return [r for r in (self.get_reminder(row["id"]) for row in rows) if r]

    def children_of_id(self, rid: int) -> list[Reminder]:
        rem = self.get_reminder(rid)
        return self.children_of(rem.uuid) if rem else []

    def count_children(self, parent_uuid: str) -> int:
        if not parent_uuid:
            return 0
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) c FROM reminders WHERE parent_uuid=? AND status='active'",
                (parent_uuid,)).fetchone()
        return int(row["c"])

    def next_sort_key(self, parent_uuid: str) -> float:
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(sort_key), 0) m FROM reminders WHERE parent_uuid=?",
                (parent_uuid or "",)).fetchone()
        return float(row["m"]) + 1.0

    def complete_tree(self, rid: int, when: datetime | None = None) -> int:
        """完成一条**主任务**时把它下面的子任务一起完成。

        不做这一步就会出现最典型的幽灵提醒: 主任务划掉了, 子任务的提醒还在响,
        而且用户在便签上已经看不到它们 (它们不再是顶层行)。
        """
        when = when or datetime.now()
        total = self.complete_reminder(rid, when)
        rem = self.get_reminder(rid)
        for child in (self.children_of(rem.uuid) if rem else []):
            total += self.complete_reminder(child.id, when)
        return total

    def delete_tree(self, rid: int) -> int:
        """真删一条主任务: 连带删掉它的子任务 (parent_uuid 不是外键, 不会自动级联)。"""
        rem = self.get_reminder(rid)
        kids = self.children_of(rem.uuid) if rem else []
        n = 0
        for child in kids:
            n += self.delete_reminder(child.id)
        return n + self.delete_reminder(rid)

    # ------------------------------------------------------------ 向导草稿 (2.5)
    def save_draft(self, key: str, payload: str, parent_uuid: str = "") -> None:
        """暂存向导草稿: 用户中途按 Esc 也不会丢已填内容 (PRD 风险预警第 4 条)。"""
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO draft(id, parent_uuid, payload, updated_at) VALUES(?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,"
                " parent_uuid=excluded.parent_uuid, updated_at=excluded.updated_at",
                (key, parent_uuid, payload, _s(datetime.now())))

    def load_draft(self, key: str) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT payload FROM draft WHERE id=?", (key,)).fetchone()
        return row["payload"] if row else None

    def clear_draft(self, key: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM draft WHERE id=?", (key,))


    def list_history(self, limit: int = 50, offset: int = 0) -> list[Reminder]:
        """已归档的条目, 最近的在前面。

        `offset` 是给便签的**历史分页**用的 (客户反馈: "历史事件会堆在一页, 请添加多页") ——
        界面只取当前那一页, 历史再多也不用一次全捞进内存。
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM reminders WHERE status<>'active'"
                " ORDER BY COALESCE(completed_at, expire_at) DESC LIMIT ? OFFSET ?",
                (limit, offset)).fetchall()
        return [r for r in (self.get_reminder(row["id"]) for row in rows) if r]

    def count_history(self) -> int:
        """历史总条数 (分页要算页数; 只 count 不取行, 便签每 2 秒轮询也便宜)。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM reminders WHERE status<>'active'").fetchone()
        return int(row["n"]) if row else 0

    def update_title(self, rid: int, title: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("UPDATE reminders SET title=? WHERE id=?", (title, rid))

    def delete_reminder(self, rid: int) -> int:
        """物理删除 (级联删触发点)。"划掉"是 complete, 这个才是真删 ——
        数据模型里 completed 和 deleted 是两个语义 (见设计文档 9.2)。"""
        with self._lock, self.conn:
            cur = self.conn.execute("DELETE FROM reminders WHERE id=?", (rid,))
        return cur.rowcount

    def id_of_uuid(self, uuid_: str) -> int | None:
        """同步键 -> 本地自增 id。设备间只交换 uuid, 本地表仍以自增 id 为主键。"""
        with self._lock:
            row = self.conn.execute("SELECT id FROM reminders WHERE uuid=?", (uuid_,)).fetchone()
        return int(row["id"]) if row else None

    # ------------------------------------------------------------ 触发点
    def pending_occurrences(self, only_active: bool = True) -> list[Occurrence]:
        sql = ("SELECT o.* FROM occurrences o JOIN reminders r ON r.id=o.reminder_id"
               " WHERE o.state IN ('pending','firing')")
        if only_active:
            sql += " AND r.status='active'"
        sql += " ORDER BY o.fire_at"
        with self._lock:
            rows = self.conn.execute(sql).fetchall()
        return [Occurrence(reminder_id=r["reminder_id"], index=r["idx"], gen=r["gen"],
                           fire_at=_d(r["fire_at"]), state=r["state"],
                           fired_at=_d(r["fired_at"]), late_by=r["late_by"]) for r in rows]

    def occurrences_of(self, rid: int) -> list[Occurrence]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM occurrences WHERE reminder_id=? ORDER BY idx", (rid,)).fetchall()
        return [Occurrence(reminder_id=r["reminder_id"], index=r["idx"], gen=r["gen"],
                           fire_at=_d(r["fire_at"]), state=r["state"],
                           fired_at=_d(r["fired_at"]), late_by=r["late_by"]) for r in rows]

    def occurrence(self, rid: int, idx: int) -> Occurrence | None:
        with self._lock:
            r = self.conn.execute("SELECT * FROM occurrences WHERE reminder_id=? AND idx=?",
                                  (rid, idx)).fetchone()
        if not r:
            return None
        return Occurrence(reminder_id=r["reminder_id"], index=r["idx"], gen=r["gen"],
                          fire_at=_d(r["fire_at"]), state=r["state"],
                          fired_at=_d(r["fired_at"]), late_by=r["late_by"])

    def reschedule_occurrence(self, rid: int, idx: int, new_fire: datetime) -> int:
        """延迟提醒必须更新原任务: 原地 UPDATE + gen+1, 而不是插一条新记录。

        gen 同时是 fired_log 去重键的一部分 ("rid#idx@gen"), 所以改期后再触发
        不会被当成"重复触发"吞掉; 而堆里那条旧 gen 的条目会被判为过期条目丢弃。
        已 fired 的触发点也允许改期 —— 这正是"响了之后点 5 分钟后再说"的场景。
        """
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE occurrences SET fire_at=?, gen=gen+1, state='pending', late_by=0"
                " WHERE reminder_id=? AND idx=? AND state IN ('pending','firing','fired')",
                (_s(new_fire), rid, idx))
            if cur.rowcount == 0:
                raise StoreError(f"触发点 {rid}#{idx} 不存在或已终结, 无法改期")
            row = self.conn.execute(
                "SELECT gen FROM occurrences WHERE reminder_id=? AND idx=?", (rid, idx)).fetchone()
        return int(row["gen"])

    def mark_fired(self, rid: int, idx: int, when: datetime, late_by: float = 0.0) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE occurrences SET state='fired', fired_at=?, late_by=? WHERE reminder_id=? AND idx=?",
                (_s(when), late_by, rid, idx))

    def mark_missed(self, rid: int, idx: int, when: datetime, late_by: float = 0.0) -> None:
        """迟到太久 (超过 late_grace), 直接判为错过, 不打扰用户。"""
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE occurrences SET state='missed', fired_at=?, late_by=? WHERE reminder_id=? AND idx=?",
                (_s(when), late_by, rid, idx))

    def replace_plan(self, rid: int, plan: ReminderPlan) -> int:
        """重排一条提醒的全部触发点 (PRD"高级事件"面板改频率时用)。

        做法是"删掉旧行 + 插入新行, 且新一代号从旧最大值+1 开始":
          * 行数变少时不会残留孤儿触发点 (原地 UPDATE 做不到这点)
          * 代际号必然递增 -> 调度器堆里那些旧条目会被判定为过期条目自动丢弃,
            不会出现"改完频率反而多响一次"
        返回新的代际号。
        """
        rem = self.get_reminder(rid)
        if rem is None:
            raise StoreError(f"提醒 {rid} 不存在")
        if rem.event_at is None:
            raise StoreError("该提醒没有事件时刻, 无法按提前量重排")
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(gen), 0) AS g FROM occurrences WHERE reminder_id=?",
                (rid,)).fetchone()
            base = int(row["g"]) + 1
            self.conn.execute("DELETE FROM occurrences WHERE reminder_id=?", (rid,))
            for i, off in enumerate(plan.offsets):
                self.conn.execute(
                    "INSERT INTO occurrences(reminder_id, idx, gen, fire_at, state)"
                    " VALUES(?,?,?,?, 'pending')", (rid, i, base, _s(rem.event_at + off)))
            self.conn.execute("UPDATE reminders SET plan=? WHERE id=?", (plan.to_json(), rid))
        return base

    def cancel_pending(self, rid: int) -> int:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE occurrences SET state='cancelled'"
                " WHERE reminder_id=? AND state IN ('pending','firing')", (rid,))
        return cur.rowcount

    # ------------------------------------------------------------ 幂等
    def try_record_fire(self, key: str, when: datetime) -> bool:
        """返回 True 表示"本次由我触发"。重复调用返回 False (防重复触发)。"""
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO fired_log(key, fired_at) VALUES(?,?)", (key, _s(when)))
        return cur.rowcount == 1

    def try_record_notification(self, key: str, when: datetime) -> bool:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO notifications(key, delivered_at) VALUES(?,?)",
                (key, _s(when)))
        return cur.rowcount == 1

    def has_fired(self, key: str) -> bool:
        with self._lock:
            return self.conn.execute("SELECT 1 FROM fired_log WHERE key=?", (key,)).fetchone() is not None

    # ------------------------------------------------------------ 生命周期
    def complete_reminder(self, rid: int, when: datetime | None = None) -> int:
        """用户"划掉/完成": 立刻终止未来所有触发点。"""
        when = when or datetime.now()
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE reminders SET status='completed', completed_at=? WHERE id=? AND status='active'",
                (_s(when), rid))
            cur = self.conn.execute(
                "UPDATE occurrences SET state='cancelled'"
                " WHERE reminder_id=? AND state IN ('pending','firing')", (rid,))
        return cur.rowcount

    def expire_due(self, now: datetime, grace: timedelta = timedelta(0)) -> list[int]:
        """兜底逻辑: 到点未处理的事件静默清理。返回被清理的 id 列表。"""
        cutoff = _s(now - grace)
        with self._lock, self.conn:
            rows = self.conn.execute(
                "SELECT id FROM reminders WHERE status='active' AND expire_at IS NOT NULL"
                " AND expire_at <= ?", (cutoff,)).fetchall()
            ids = [r["id"] for r in rows]
            for rid in ids:
                self.conn.execute(
                    "UPDATE reminders SET status='expired', completed_at=? WHERE id=?", (_s(now), rid))
                self.conn.execute(
                    "UPDATE occurrences SET state='cancelled'"
                    " WHERE reminder_id=? AND state IN ('pending','firing')", (rid,))
        return ids

    def prune(self, older_than: timedelta = timedelta(days=30), now: datetime | None = None) -> int:
        """历史归档清理: 已结束的记录保留一段时间供"翻页查阅历史"。"""
        now = now or datetime.now()
        cutoff = _s(now - older_than)
        with self._lock, self.conn:
            cur = self.conn.execute(
                "DELETE FROM reminders WHERE status<>'active' AND completed_at IS NOT NULL"
                " AND completed_at < ?", (cutoff,))
        return cur.rowcount

    def recover_inflight(self) -> int:
        """崩溃恢复: 上次进程死在"已出队/未确认"状态的触发点退回 pending。"""
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE occurrences SET state='pending' WHERE state='firing'")
        return cur.rowcount

    def stats(self) -> dict:
        with self._lock:
            active = self.conn.execute(
                "SELECT COUNT(*) c FROM reminders WHERE status='active'").fetchone()["c"]
            pend = self.conn.execute(
                "SELECT COUNT(*) c FROM occurrences WHERE state='pending'").fetchone()["c"]
        return {"active": active, "pending": pend}

    # ------------------------------------------------------------ meta
    def set_meta(self, k: str, v: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("INSERT INTO meta(k,v) VALUES(?,?)"
                              " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def get_meta(self, k: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default


# ---------------------------------------------------------------- 目录与实例锁

def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
    return Path(base) / "HandyReminder"


def _acquire_dir_lock(directory: Path):
    """同目录唯一实例锁。返回句柄, 失败抛 StoreLocked。"""
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / "instance.lock"
    fh = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:  # 已被占用
        fh.close()
        raise StoreLocked(
            "已有另一个实例占用数据目录 (同一个提醒不会重复弹两次)。"
            "如需并行运行, 请用 --data-dir 指定不同目录。") from exc
    fh.seek(0)
    fh.truncate()
    fh.write(f"pid={os.getpid()}\n".encode())
    fh.flush()
    return fh


def _release_dir_lock(fh) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        fh.close()
