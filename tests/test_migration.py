"""阶段一 T1/T2 用例: 老库迁移 / 幂等 / 结构不漂移 / uuid 回填。

这些用例的价值全在"注入"上 —— 正常路径谁都能跑通, 要证明的是
"迁移失败不留半截库""重复跑不会变形""两条建库路径最终一模一样"。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import timedelta

import pytest

from reminder_assistant.store import SCHEMA_VERSION, Store, new_uuid

# 2.3.2 版真实结构。**逐字照抄, 不要"顺手优化"** —— 它就是用户机器上老库的真身,
# 改一个字, 这个用例就不再证明任何事。
LEGACY_SCHEMA = """
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
  note         TEXT    NOT NULL DEFAULT ''
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
"""

TABLES = ("reminders", "occurrences", "fired_log", "notifications", "meta", "outbox", "draft")


def make_legacy_db(path, rows: int = 2) -> None:
    """造一个 2.3.2 版的老库: 有任务、触发点、响过记录、meta。"""
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    for i in range(rows):
        conn.execute(
            "INSERT INTO reminders(title, source_text, event_at, tz, level, plan, status,"
            " created_at, expire_at, bbox, note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (f"老任务{i}", "明天下午三点开会", f"2026-10-08T1{i}:00:00+08:00", "Asia/Shanghai",
             "日常", "[-3600.0]", "active", "2026-10-07T09:00:00+08:00",
             f"2026-10-09T1{i}:00:00+08:00", "[1, 2, 3, 4]", "备注"))
        for idx in range(2):
            conn.execute(
                "INSERT INTO occurrences(reminder_id, idx, gen, fire_at, state, late_by)"
                " VALUES(?,?,?,?, 'pending', 0)",
                (i + 1, idx, 0, f"2026-10-08T1{i}:{idx}0:00+08:00"))
    conn.execute("INSERT INTO fired_log(key, fired_at) VALUES('1#0@0','2026-10-07T10:00:00+08:00')")
    conn.execute("INSERT INTO notifications(key, delivered_at)"
                 " VALUES('1#0@0','2026-10-07T10:00:01+08:00')")
    conn.execute("INSERT INTO meta(k, v) VALUES('settings','{}')")
    conn.commit()
    conn.close()


def raw(path) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def columns(conn, table: str) -> list[tuple]:
    return [(r["name"], r["type"].upper(), r["notnull"], r["dflt_value"], r["pk"])
            for r in conn.execute(f"PRAGMA table_info({table})")]


def indexes(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,))
        if r["name"] and not r["name"].startswith("sqlite_autoindex")}


# ---------------------------------------------------------------- 数据不丢

def test_legacy_db_upgrades_without_data_loss(tmp_path):
    db = tmp_path / "old.db"
    make_legacy_db(db)

    with Store(db) as s:
        assert s.user_version() == SCHEMA_VERSION
        rems = s.list_active()
        assert [r.title for r in rems] == ["老任务0", "老任务1"]
        assert rems[0].plan.offsets == (timedelta(hours=-1),)          # 触发点规则没变形
        assert len(s.occurrences_of(rems[0].id)) == 2                  # 触发点一行不少
        assert s.has_fired("1#0@0") is True                            # 响过记录还在
        assert s.get_meta("settings") == "{}"
        assert all(r.uuid and len(r.uuid) == 36 for r in rems)          # 老行都补上了 uuid
        assert len({r.uuid for r in rems}) == 2                         # 且互不相同
        assert all(r.depth == 0 and r.sync_state == "clean" for r in rems)
        # 2.4 新增的"持续时间"列: 老行必须落在默认值上, 而不是 None (None 会在界面上炸)
        assert all(r.duration_min == 0 for r in rems)


def test_migrated_db_still_writable(tmp_path):
    db = tmp_path / "old.db"
    make_legacy_db(db, rows=1)
    from reminder_assistant.models import Reminder, ReminderPlan
    from datetime import datetime, timedelta, timezone

    with Store(db) as s:
        tz = timezone(timedelta(hours=8))
        rem = s.add_reminder(Reminder(title="迁移后新建", tz="Asia/Shanghai",
                                      event_at=datetime(2026, 10, 9, 15, 0, tzinfo=tz),
                                      plan=ReminderPlan.preset("日常")))
        assert rem.id == 2 and rem.uuid
        assert s.get_reminder(2).uuid == rem.uuid


# ---------------------------------------------------------------- 幂等

def test_migration_is_idempotent(tmp_path):
    db = tmp_path / "old.db"
    make_legacy_db(db)

    with Store(db) as s:                       # 第一次: 补列 + 回填
        assert s.migrate() == 0                # 已在 __init__ 里迁移过, 这次是空操作
        assert s.user_version() == SCHEMA_VERSION
    with closing(raw(db)) as c:
        cols_first = columns(c, "reminders")
        idx_first = indexes(c, "reminders")

    for _ in range(2):                         # 再开两次
        with Store(db) as s:
            assert s.user_version() == SCHEMA_VERSION
            assert s.migrate() == 0
    with closing(raw(db)) as c:
        assert columns(c, "reminders") == cols_first
        assert indexes(c, "reminders") == idx_first


# ---------------------------------------------------------------- 结构不漂移

def test_new_and_migrated_schema_match(tmp_path):
    """新建库 与 老库迁移后 必须一模一样 —— 这是最容易悄悄走偏的地方。"""
    old_db, new_db = tmp_path / "old.db", tmp_path / "new.db"
    make_legacy_db(old_db)
    with Store(old_db):
        pass
    with Store(new_db):
        pass

    with closing(raw(old_db)) as co, closing(raw(new_db)) as cn:
        for t in TABLES:
            assert columns(co, t) == columns(cn, t), f"{t} 列定义漂移"
            assert indexes(co, t) == indexes(cn, t), f"{t} 索引漂移"
        assert co.execute("PRAGMA user_version").fetchone()[0] == \
               cn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


# ---------------------------------------------------------------- uuid 唯一性

def test_blank_uuids_do_not_collide_but_real_ones_must(tmp_path):
    """部分唯一索引: 老数据留下的空 uuid 可以并存, 真 uuid 重复必须被挡住。"""
    db = tmp_path / "t.db"
    with Store(db) as s:
        s.conn.execute("INSERT INTO reminders(title, created_at) VALUES('裸行1','x')")
        s.conn.execute("INSERT INTO reminders(title, created_at) VALUES('裸行2','y')")
        s.conn.commit()                                    # 两行 uuid='' 不冲突

        dup = new_uuid()
        s.conn.execute("INSERT INTO reminders(title, created_at, uuid) VALUES('a','x',?)", (dup,))
        with pytest.raises(sqlite3.IntegrityError):
            s.conn.execute("INSERT INTO reminders(title, created_at, uuid) VALUES('b','x',?)", (dup,))
        s.conn.rollback()


# ---------------------------------------------------------------- 失败回滚

def test_failed_migration_leaves_no_half_upgraded_db(tmp_path, monkeypatch):
    """中途失败必须整批回滚: 版本号不动、列不加 —— 半截库最难查。"""
    db = tmp_path / "old.db"
    make_legacy_db(db)

    def boom(self):
        raise RuntimeError("模拟迁移中途失败")

    monkeypatch.setattr(Store, "_backfill_uuids", boom)

    with pytest.raises(RuntimeError, match="模拟迁移中途失败"):
        Store(db)

    monkeypatch.undo()
    with closing(raw(db)) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "uuid" not in {r["name"] for r in c.execute("PRAGMA table_info(reminders)")}
        assert c.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 2

    with Store(db) as s:                       # 修好之后照样能升上去
        assert s.user_version() == SCHEMA_VERSION
        assert all(r.uuid for r in s.list_active())


def test_failed_migration_releases_instance_lock(tmp_path, monkeypatch):
    """迁移失败后实例锁必须放掉, 否则用户重启一次就永远打不开了。"""
    db = tmp_path / "old.db"
    make_legacy_db(db)
    monkeypatch.setattr(Store, "_backfill_uuids",
                        lambda self: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        Store(db)
    monkeypatch.undo()
    with Store(db) as s:                       # 能再开起来 = 锁没被留死
        assert s.stats()["active"] == 2
