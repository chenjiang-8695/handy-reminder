"""展示版同步桥用例: 纯逻辑 + 真 WebSocket 往返 + 手机端网页可服务。

这里刻意不用桩 WebSocket: 真起一个 QWebSocketServer, 真接一个 QWebSocket 客户端,
真发消息 —— 演示那天会走的就是这条代码路径。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="未安装 PySide6, 跳过同步桥测试")

from PySide6.QtCore import QUrl                              # noqa: E402
from PySide6.QtNetwork import QAbstractSocket                 # noqa: E402
from PySide6.QtWebSockets import QWebSocket                  # noqa: E402
from PySide6.QtWidgets import QApplication                    # noqa: E402

from reminder_assistant.app import App                        # noqa: E402
from reminder_assistant.clock import FakeClock                # noqa: E402
from reminder_assistant.models import Reminder, ReminderPlan  # noqa: E402
from reminder_assistant.sync_demo import (                    # noqa: E402
    DemoHub, PageServer, SyncDemoServer, parse_attr_fields, view_rows)

TZ = datetime(2026, 10, 8, 10, 0).astimezone().tzinfo
NOW = datetime(2026, 10, 8, 10, 0, tzinfo=TZ)


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def app_env(tmp_path, qapp):
    a = App(tmp_path)
    a.clock = FakeClock(NOW)
    a.scheduler.clock = a.clock
    a.life.create(Reminder(title="开会", event_at=NOW + timedelta(hours=5), created_at=NOW,
                           level="日常", plan=ReminderPlan.preset("日常")), NOW)
    yield a
    a.close()


def pump(qapp, cond, ms: int = 3000) -> bool:
    end = time.time() + ms / 1000
    while time.time() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    qapp.processEvents()
    return cond()


class Phone:
    """扮演手机浏览器那一端: 真 WebSocket 客户端 + 收到的消息流水。"""

    def __init__(self, qapp, port: int):
        self.qapp, self.got = qapp, []
        self.ws = QWebSocket()
        self.ws.textMessageReceived.connect(lambda s: self.got.append(json.loads(s)))
        self.ws.open(QUrl(f"ws://127.0.0.1:{port}/"))
        assert pump(qapp, lambda: self.ws.state() == QAbstractSocket.SocketState.ConnectedState), \
            "连不上同步桥"
        assert pump(qapp, lambda: any(m["t"] == "snapshot" for m in self.got)), "没收到快照"

    def send(self, msg: dict) -> None:
        self.ws.sendTextMessage(json.dumps(msg, ensure_ascii=False))

    def deltas(self, ev: str | None = None) -> list[dict]:
        return [m for m in self.got if m["t"] == "delta" and (ev is None or m["ev"] == ev)]

    def notices(self) -> list[str]:
        return [m["text"] for m in self.got if m["t"] == "notice"]

    def close(self) -> None:
        self.ws.close()


# ---------------------------------------------------------------- 纯逻辑

def rows(*, uuid: str, title: str = "开会", next_fire: str = "2026-10-08T15:00:00+08:00",
         level: str = "日常", note: str = "", duration_min: int = 0,
         depth: int = 0, parent_uuid: str = "") -> dict:
    return {"uuid": uuid, "title": title, "level": level, "status": "active",
            "event_at": "2026-10-08T15:00:00+08:00", "next_fire": next_fire,
            "fired": 0, "total": 1, "overdue": False, "version": 1,
            "depth": depth, "parent_uuid": parent_uuid, "duration_min": duration_min,
            "note": note}


def test_first_refresh_reports_every_task_and_advances_seq():
    hub = DemoHub()
    assert hub.seq == 0
    msgs = hub.refresh([rows(uuid="a"), rows(uuid="b")])
    assert [m["ev"] for m in msgs] == ["upsert", "upsert"]
    assert [m["seq"] for m in msgs] == [1, 2]          # 序号单调, 和正式版 server_seq 同形
    assert hub.snapshot()["seq"] == 2
    assert [t["uuid"] for t in hub.snapshot()["tasks"]] == ["a", "b"]


def test_unchanged_rows_emit_nothing():
    hub = DemoHub()
    hub.refresh([rows(uuid="a")])
    assert hub.refresh([rows(uuid="a")]) == []          # 轮询不能每 0.5 秒刷一次无意义广播


def test_a_bump_does_not_keep_emitting_forever():
    """version 是镜像自己的记账字段: 涨过一次之后, 内容没变就不能再广播。"""
    hub = DemoHub()
    hub.refresh([rows(uuid="a")])
    hub.refresh([rows(uuid="a", title="改名")])         # 内容变了 -> 一条 upsert
    assert hub.refresh([rows(uuid="a", title="改名")]) == []
    assert hub.refresh([rows(uuid="a", title="改名")]) == []
    assert hub.tasks["a"]["version"] == 2


def test_changed_field_bumps_version_and_emits_once():
    hub = DemoHub()
    hub.refresh([rows(uuid="a")])
    msgs = hub.refresh([rows(uuid="a", next_fire="2026-10-08T15:05:00+08:00")])
    assert len(msgs) == 1
    assert msgs[0]["task"]["version"] == 2              # 版本号涨了 = 手机端能看出这是新的一版


def test_vanished_task_emits_remove():
    hub = DemoHub()
    hub.refresh([rows(uuid="a"), rows(uuid="b")])
    msgs = hub.refresh([rows(uuid="a")])
    assert [m["ev"] for m in msgs] == ["remove"]
    assert msgs[0]["task"]["uuid"] == "b"
    assert [t["uuid"] for t in hub.snapshot()["tasks"]] == ["a"]


def test_apply_op_refuses_unknown_task():
    hub = DemoHub()
    hub.refresh([rows(uuid="a")])
    out = hub.apply_op({"op": "complete", "uuid": "不存在"})
    assert out[0]["op"] == "reject"


def test_apply_op_add_requires_text():
    assert DemoHub().apply_op({"op": "add", "text": "   "})[0]["op"] == "reject"
    assert DemoHub().apply_op({"op": "add", "text": "明天下午三点开会"})[0]["op"] == "add"
    assert DemoHub().apply_op({"op": "乱来"})[0]["op"] == "reject"


# ---- 手机端改属性 / 建子任务 (PRD 2.5 那两项用户说"没看到"的功能) ----------

def test_parse_attr_fields_reports_only_what_really_changed():
    cur = rows(uuid="a")
    fields, err = parse_attr_fields({"title": "开会", "level": "日常", "duration_min": 0,
                                     "note": "", "event_at": "2026-10-08T15:00"},
                                    cur, levels=("日常", "重要", "紧急"))
    assert err is None and fields == {}, "原样提交不该触发一次触发点重排"


def test_parse_attr_fields_attaches_the_reminders_own_timezone():
    """浏览器 datetime-local 给的是没有时区的钟面时间 —— 不回填时区就会偏一小时。"""
    fields, err = parse_attr_fields({"event_at": "2026-10-09T09:30"}, rows(uuid="a"),
                                    levels=("日常",))
    assert err is None
    when = fields["event_at"]
    assert when.utcoffset() == timedelta(hours=8)
    assert (when.hour, when.minute) == (9, 30) and when.day == 9


def test_parse_attr_fields_rejects_nonsense():
    cur = rows(uuid="a")
    assert parse_attr_fields({"title": "   "}, cur, levels=("日常",))[1] == "名称不能为空"
    assert "不认识的重要程度" in (parse_attr_fields({"level": "随便"}, cur,
                                                   levels=("日常", "重要", "紧急"))[1] or "")
    assert "持续时间" in (parse_attr_fields({"duration_min": 99999}, cur, levels=("日常",))[1] or "")
    assert "持续时间" in (parse_attr_fields({"duration_min": "半小时"}, cur, levels=("日常",))[1] or "")
    assert parse_attr_fields({"event_at": "明天下午"}, cur, levels=("日常",))[1] == "没看懂这个时间"


def test_apply_op_attr_needs_a_live_task_and_a_real_change():
    hub = DemoHub(levels=("日常", "重要", "紧急"))
    hub.refresh([rows(uuid="a")])
    assert hub.apply_op({"op": "attr", "uuid": "没这条", "fields": {"title": "x"}})[0]["op"] == "reject"
    assert hub.apply_op({"op": "attr", "uuid": "a", "fields": {"title": "开会"}})[0]["reason"] == "什么都没改"
    assert hub.apply_op({"op": "attr", "uuid": "a", "fields": "乱来"})[0]["op"] == "reject"
    ok = hub.apply_op({"op": "attr", "uuid": "a",
                       "fields": {"title": "改成线上会", "duration_min": 45}})
    assert ok[0]["op"] == "attr"
    assert ok[0]["fields"] == {"title": "改成线上会", "duration_min": 45}


def test_apply_op_add_child_only_under_a_top_level_task():
    hub = DemoHub()
    hub.refresh([rows(uuid="p"), rows(uuid="c", depth=1, parent_uuid="p")])
    assert hub.apply_op({"op": "add_child", "uuid": "没这条", "text": "x"})[0]["op"] == "reject"
    assert "只支持二级" in (hub.apply_op({"op": "add_child", "uuid": "c",
                                         "text": "孙任务"})[0]["reason"] or "")
    assert hub.apply_op({"op": "add_child", "uuid": "p", "text": "   "})[0]["reason"] == "子任务还没有名字"
    assert "持续时间" in (hub.apply_op({"op": "add_child", "uuid": "p", "text": "甲",
                                      "duration_min": -5})[0]["reason"] or "")
    ok = hub.apply_op({"op": "add_child", "uuid": "p", "text": "整理需求", "duration_min": 30})
    assert ok[0] == {"op": "add_child", "uuid": "p", "text": "整理需求", "duration_min": 30}


def test_snapshot_carries_the_computer_side_level_names():
    hub = DemoHub(levels=("日常", "重要", "紧急"))
    assert hub.snapshot()["levels"] == ["日常", "重要", "紧急"]
    assert DemoHub().snapshot()["levels"] == ["日常", "重要", "紧急"]   # 没传也要有默认


def test_view_rows_carries_note_so_the_phone_can_show_and_edit_it(app_env):
    app_env.life.update_attributes(app_env.store.list_active()[0].id, note="带电脑")
    got = view_rows(app_env.store, app_env.life, NOW)
    assert got[0]["note"] == "带电脑"


def test_view_rows_exposes_uuid(app_env):
    got = view_rows(app_env.store, app_env.life, NOW)
    assert len(got) == 1
    assert got[0]["uuid"] == app_env.store.list_active()[0].uuid
    assert got[0]["title"] == "开会" and got[0]["total"] == 1


# ---------------------------------------------------------------- 真 WebSocket 往返

def test_phone_gets_snapshot_then_completes_and_pc_really_changes(qapp, app_env):
    server = SyncDemoServer(app_env.store, app_env.life, port=0, clock=app_env.clock)
    phone = Phone(qapp, server.port)
    try:
        snap = phone.got[0]
        assert snap["t"] == "snapshot" and len(snap["tasks"]) == 1
        uuid = snap["tasks"][0]["uuid"]

        phone.send({"t": "op", "op": "complete", "uuid": uuid})

        assert pump(qapp, lambda: app_env.store.list_active() == []), "电脑端没被手机端改到"
        assert app_env.store.list_history()[0].status == "completed"
        assert pump(qapp, lambda: bool(phone.deltas("remove"))), "手机端没收到它自己这次操作的回执"
        assert phone.deltas("remove")[-1]["task"]["uuid"] == uuid
    finally:
        phone.close()
        server.close()


def test_phone_can_add_a_task_using_the_same_time_parser(qapp, app_env):
    server = SyncDemoServer(app_env.store, app_env.life, port=0, clock=app_env.clock)
    phone = Phone(qapp, server.port)
    try:
        phone.send({"t": "op", "op": "add", "text": "明天下午三点开会"})

        def added():
            return [r for r in app_env.store.list_active() if r.title == "明天下午三点开会"]

        assert pump(qapp, lambda: bool(added())), "手机端新建没落到电脑端"
        rem = added()[0]
        assert rem.event_at == datetime(2026, 10, 9, 15, 0, tzinfo=rem.event_at.tzinfo)
        assert pump(qapp, lambda: any(d["task"]["title"] == "明天下午三点开会"
                                      for d in phone.deltas("upsert"))), "手机端没收到新卡的推送"
    finally:
        phone.close()
        server.close()


def test_phone_add_without_time_gets_a_notice_not_a_card(qapp, app_env):
    server = SyncDemoServer(app_env.store, app_env.life, port=0, clock=app_env.clock)
    phone = Phone(qapp, server.port)
    try:
        phone.send({"t": "op", "op": "add", "text": "记得买牛奶"})
        assert pump(qapp, lambda: bool(phone.notices()))
        assert "没识别出时间" in phone.notices()[0]
        assert len(app_env.store.list_active()) == 1        # 没建出卡片来
    finally:
        phone.close()
        server.close()


def test_delete_from_phone_removes_it_from_pc(qapp, app_env):
    server = SyncDemoServer(app_env.store, app_env.life, port=0, clock=app_env.clock)
    phone = Phone(qapp, server.port)
    try:
        uuid = phone.got[0]["tasks"][0]["uuid"]
        phone.send({"t": "op", "op": "delete", "uuid": uuid})
        assert pump(qapp, lambda: app_env.store.list_active() == [])
        assert app_env.store.list_history() == []           # 真删, 不进历史
        assert app_env.store.id_of_uuid(uuid) is None
    finally:
        phone.close()
        server.close()


# ---- 手机端改属性 / 建子任务: 真 WebSocket 往返, 且真的落到电脑端账本上 ----

def test_phone_edits_attributes_and_pc_replans_to_the_new_time(qapp, app_env):
    server = SyncDemoServer(app_env.store, app_env.life, port=0, clock=app_env.clock,
                            levels=app_env.settings.level_names(),
                            plan_for=app_env.settings.plan)
    phone = Phone(qapp, server.port)
    try:
        uuid = phone.got[0]["tasks"][0]["uuid"]
        phone.send({"t": "op", "op": "attr", "uuid": uuid, "fields": {
            "title": "改成线上会", "event_at": "2026-10-08T18:00",
            "level": "重要", "duration_min": 45, "note": "带电脑"}})

        def changed():
            rem = app_env.store.list_active()[0]
            return rem.title == "改成线上会" and rem.duration_min == 45

        assert pump(qapp, changed), "手机端改的属性没落到电脑端"
        rem = app_env.store.list_active()[0]
        assert rem.level == "重要" and rem.note == "带电脑"
        assert (rem.event_at.hour, rem.event_at.minute) == (18, 0)
        assert rem.event_at.utcoffset() == NOW.utcoffset()      # 没被当 UTC 存进去
        # 改了时间就要按新时间重排 —— 否则"改完还按老时间响"
        fires = [app_env.store.occurrence(rem.id, i) for i in range(len(rem.plan.offsets))]
        assert fires and all(o.fire_at <= rem.event_at for o in fires)
        # 改了等级就要换频率 (重要: 提前 30 分 + 每 10 分一次, 共 3 次)
        assert len(rem.plan.offsets) == 3
        assert pump(qapp, lambda: any(d["task"]["title"] == "改成线上会"
                                      for d in phone.deltas("upsert"))), "手机端没收到回执"
    finally:
        phone.close()
        server.close()


def test_phone_add_child_chains_start_time_after_the_existing_children(qapp, app_env):
    parent = app_env.store.list_active()[0]
    app_env.life.add_children(parent, [{"title": "整理需求", "duration_min": 30},
                                       {"title": "写文档", "duration_min": 45}], NOW)
    server = SyncDemoServer(app_env.store, app_env.life, port=0, clock=app_env.clock)
    phone = Phone(qapp, server.port)
    try:
        snap = phone.got[0]["tasks"]
        assert sum(1 for t in snap if t["depth"] == 1) == 2      # 手机端能看到二级目录
        phone.send({"t": "op", "op": "add_child", "uuid": parent.uuid,
                    "text": "评审", "duration_min": 15})

        def kid():
            return [r for r in app_env.store.list_active() if r.title == "评审"]

        assert pump(qapp, lambda: bool(kid())), "手机端建的子任务没落到电脑端"
        child = kid()[0]
        assert child.parent_uuid == parent.uuid and child.depth == 1
        # 15:30 那条持续 45 分 -> 新的接在 16:15, 不是又挤在父任务那一刻
        assert (child.event_at.hour, child.event_at.minute) == (16, 15)
        assert app_env.store.occurrence(child.id, 0) is not None  # 子任务是**真提醒**
        assert pump(qapp, lambda: any(d["task"]["title"] == "评审" and d["task"]["depth"] == 1
                                      for d in phone.deltas("upsert")))
    finally:
        phone.close()
        server.close()


def test_phone_cannot_build_a_grandchild(qapp, app_env):
    parent = app_env.store.list_active()[0]
    app_env.life.add_children(parent, [{"title": "子任务甲", "duration_min": 30}], NOW)
    child = [r for r in app_env.store.list_active() if r.depth == 1][0]
    server = SyncDemoServer(app_env.store, app_env.life, port=0, clock=app_env.clock)
    phone = Phone(qapp, server.port)
    try:
        phone.send({"t": "op", "op": "add_child", "uuid": child.uuid,
                    "text": "孙任务", "duration_min": 10})
        assert pump(qapp, lambda: bool(phone.notices()))
        assert "只支持二级" in phone.notices()[0]
        assert not [r for r in app_env.store.list_active() if r.title == "孙任务"]
    finally:
        phone.close()
        server.close()


def test_snapshot_carries_levels_so_the_phone_picker_matches_the_pc(qapp, app_env):
    server = SyncDemoServer(app_env.store, app_env.life, port=0, clock=app_env.clock,
                            levels=app_env.settings.level_names())
    phone = Phone(qapp, server.port)
    try:
        assert phone.got[0]["levels"] == app_env.settings.level_names()
    finally:
        phone.close()
        server.close()


def test_bad_json_does_not_kill_the_bridge(qapp, app_env):
    server = SyncDemoServer(app_env.store, app_env.life, port=0, clock=app_env.clock)
    phone = Phone(qapp, server.port)
    try:
        phone.ws.sendTextMessage("这不是 JSON")
        assert pump(qapp, lambda: bool(phone.notices()))
        phone.send({"t": "op", "op": "complete", "uuid": phone.got[0]["tasks"][0]["uuid"]})
        assert pump(qapp, lambda: app_env.store.list_active() == [])   # 桥还活着
    finally:
        phone.close()
        server.close()


def test_pc_side_change_pushes_to_phone_within_one_tick(qapp, app_env):
    """演示的一半动线: 电脑端新建 -> 手机端自动出现。"""
    server = SyncDemoServer(app_env.store, app_env.life, port=0, clock=app_env.clock)
    phone = Phone(qapp, server.port)
    try:
        app_env.life.create(Reminder(title="电脑端新建的", event_at=NOW + timedelta(hours=2),
                                     created_at=NOW, level="日常",
                                     plan=ReminderPlan.preset("日常")), NOW)
        assert pump(qapp, lambda: any(d["task"]["title"] == "电脑端新建的"
                                      for d in phone.deltas("upsert")))
    finally:
        phone.close()
        server.close()


# ---------------------------------------------------------------- 手机端网页

def test_page_server_serves_a_self_contained_page(qapp):
    pages = PageServer(0, ws_port=8765)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{pages.port}/", timeout=5) as r:
            body = r.read().decode("utf-8")
        assert r.status == 200 and "智能手账" in body
        assert '"ws://" + location.hostname' in body            # 连的是当前主机的 WS 口
        assert "const WS_PORT = 8765;" in body                   # 端口注入正确
        assert "http://" not in body and "https://" not in body   # 不引任何外部资源
    finally:
        pages.close()


def test_page_offers_attribute_editing_and_subtask_creation(qapp):
    """用户在手机端"没看到"的就是这两项 —— 页面上必须有可见入口。"""
    pages = PageServer(0, ws_port=8765)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{pages.port}/", timeout=5) as r:
            body = r.read().decode("utf-8")
        for token in ("openAttr(", "openChild(", "事件属性", "＋子任务", "重要程度",
                      'op: "attr"', 'op: "add_child"', "datetime-local"):
            assert token in body, token
        assert "http://" not in body and "https://" not in body   # 依旧不引外部资源
    finally:
        pages.close()


def test_page_server_404s_other_paths(qapp):
    pages = PageServer(0, ws_port=8765)
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{pages.port}/secret", timeout=5)
        assert e.value.code == 404
    finally:
        pages.close()


def test_start_demo_sync_prints_both_urls(app_env, capsys):
    from reminder_assistant.sync_demo import start_demo_sync

    bundle = start_demo_sync(app_env, ws_port=0, page_port=0)
    try:
        out = capsys.readouterr().out
        assert "手机端网页" in out and str(bundle.pages.port) in out
        assert bundle.phone_url.startswith("http://") and bundle.local_url.endswith("/")
        assert bundle.server.port > 0
    finally:
        bundle.close()


# ---------------------------------------------------------------- 绑定 (PRD 2.4)
class GatedPhone:
    """要求先绑定时, 手机端连上只会收到 hello (没有任务快照)。"""

    def __init__(self, qapp, port: int):
        self.qapp, self.got = qapp, []
        self.ws = QWebSocket()
        self.ws.textMessageReceived.connect(lambda s: self.got.append(json.loads(s)))
        self.ws.open(QUrl(f"ws://127.0.0.1:{port}/"))
        assert pump(qapp, lambda: self.ws.state() == QAbstractSocket.SocketState.ConnectedState)
        assert pump(qapp, lambda: bool(self.got)), "没收到 hello"

    def send(self, msg: dict) -> None:
        self.ws.sendTextMessage(json.dumps(msg, ensure_ascii=False))

    def wait(self, kind: str) -> dict | None:
        pump(self.qapp, lambda: any(m["t"] == kind for m in self.got))
        return next((m for m in self.got if m["t"] == kind), None)

    def last_notice(self) -> str:
        texts = [m["text"] for m in self.got if m["t"] == "notice"]
        return texts[-1] if texts else ""

    def close(self) -> None:
        self.ws.close()


def test_unbound_phone_gets_no_tasks_and_no_actions(app_env, qapp):
    """没绑定就不该拿到任务列表, 也不该能改任务 —— 否则"绑定"就只是个装饰。"""
    from reminder_assistant.sync_demo import SyncDemoServer

    before = len(app_env.store.list_active())
    server = SyncDemoServer(app_env.store, app_env.life, port=0, clock=app_env.clock,
                            account=app_env.account, require_bind=True)
    phone = GatedPhone(qapp, server.port)
    try:
        assert phone.got[0]["t"] == "hello" and phone.got[0]["need_bind"] is True
        assert not [m for m in phone.got if m["t"] == "snapshot"], "没绑定却推了任务"
        phone.send({"t": "op", "op": "add", "text": "明天下午三点开会"})
        # 回包是异步的: 断言前必须推动事件循环, 否则拿到的是"还没收到"而不是产品行为
        assert pump(qapp, lambda: "还没绑定" in phone.last_notice())
        assert len(app_env.store.list_active()) == before, "没绑定的手机端竟然真的建了卡"
    finally:
        phone.close()
        server.close()


def test_phone_binds_with_the_desktop_code_and_then_syncs(app_env, qapp):
    """演示动线: 电脑端生成码 → 手机端输码 → 绑定成功 → 任务列表出现。"""
    from datetime import datetime

    from reminder_assistant import account as A
    from reminder_assistant.sync_demo import SyncDemoServer

    _acct, code = app_env.account.issue_code(datetime.now().astimezone(), A.ROLE_DESKTOP)
    server = SyncDemoServer(app_env.store, app_env.life, port=0, clock=app_env.clock,
                            account=app_env.account, require_bind=True)
    phone = GatedPhone(qapp, server.port)
    try:
        phone.send({"t": "op", "op": "bind", "code": A.format_code(code.code),
                    "name": "手机端(测试)"})
        bound = phone.wait("bound")
        assert bound and bound["device"]["name"] == "手机端(测试)"
        assert bound["device"]["token"].startswith("dev_")
        assert bound["account_id"] == app_env.account.reload().account_id
        assert phone.wait("snapshot"), "绑定成功后应当收到任务快照"
        # 电脑端的设备列表当场就有这台手机
        devs = app_env.account.reload().devices
        assert [d.name for d in devs] == ["手机端(测试)"]

        # 绑定之后操作真的落到桌面端
        phone.send({"t": "op", "op": "add", "text": "明天下午三点开会"})
        assert pump(qapp, lambda: len(app_env.store.list_active()) == 1)
    finally:
        phone.close()
        server.close()


def test_phone_resumes_with_token_and_fails_after_revoke(app_env, qapp):
    """重连用设备令牌恢复绑定; 电脑端撤销后, 同一个令牌必须失效。"""
    from datetime import datetime

    from reminder_assistant import account as A
    from reminder_assistant.sync_demo import SyncDemoServer

    _acct, code = app_env.account.issue_code(datetime.now().astimezone(), A.ROLE_DESKTOP)
    _acct, dev = app_env.account.bind(code.code, datetime.now().astimezone(),
                                      name="手机端(测试)", platform="手机端")
    server = SyncDemoServer(app_env.store, app_env.life, port=0, clock=app_env.clock,
                            account=app_env.account, require_bind=True)
    phone = GatedPhone(qapp, server.port)
    try:
        phone.send({"t": "op", "op": "resume", "token": dev.token})
        assert phone.wait("bound"), "有效令牌应当能恢复绑定"
        assert phone.wait("snapshot")
    finally:
        phone.close()

    app_env.account.revoke(dev.id)
    phone2 = GatedPhone(qapp, server.port)
    try:
        phone2.send({"t": "op", "op": "resume", "token": dev.token})
        assert pump(qapp, lambda: "失效" in phone2.last_notice())
        assert not [m for m in phone2.got if m["t"] == "snapshot"], "撤销后不该再拿到任务"
    finally:
        phone2.close()
        server.close()


def test_phone_can_issue_its_own_code_and_desktop_binds_it(app_env, qapp):
    """另一条合法路径: 手机端点"生成我的绑定码", 用户在电脑端手动输入。"""
    from reminder_assistant import account as A
    from reminder_assistant.sync_demo import SyncDemoServer

    server = SyncDemoServer(app_env.store, app_env.life, port=0, clock=app_env.clock,
                            account=app_env.account, require_bind=True)
    phone = GatedPhone(qapp, server.port)
    try:
        phone.send({"t": "op", "op": "phone_code"})
        pair = phone.wait("pair_code")
        assert pair and len(pair["code"]) == 9, pair          # XXXX-XXXX
        # 方向必须对: 手机端生成的码不能当"电脑端生成的码"去扫码绑定
        phone.send({"t": "op", "op": "bind", "code": pair["code"], "name": "手机端"})
        assert pump(qapp, lambda: "方向不对" in phone.last_notice())
        # 电脑端手动输入则可用 (用同一个假时钟: 绑定码 5 分钟就过期, 别拿真实时间去过期它)
        _acct, dev = app_env.account.bind(pair["code"], app_env.clock.now(),
                                         name="手机端(手动绑定)", platform="手机",
                                         role=A.ROLE_PHONE)
        assert dev.name == "手机端(手动绑定)"
    finally:
        phone.close()
        server.close()


def test_demo_page_has_the_binding_gate(app_env):
    """手机端网页要能看到"先绑定"这一步, 而不是直接摆出任务列表。"""
    from reminder_assistant.sync_demo import PageServer

    pages = PageServer(0, ws_port=8765)
    try:
        body = urllib.request.urlopen(f"http://127.0.0.1:{pages.port}/", timeout=5) \
            .read().decode("utf-8")
        for token in ("先绑定这台手机", 'id="bind-code"', 'id="bind-go"', 'id="phone-code"',
                      'op: "bind"', 'op: "resume"', "hr_device_token"):
            assert token in body, token
        assert "http://" not in body and "https://" not in body
    finally:
        pages.close()


def test_demo_sync_requires_binding_and_announces_the_account(app_env, capsys):
    from reminder_assistant.sync_demo import start_demo_sync

    bundle = start_demo_sync(app_env, ws_port=0, page_port=0)
    try:
        out = capsys.readouterr().out
        assert bundle.server.require_bind is True
        assert app_env.account.account.account_id in out, "启动信息里要写清识别码"
        assert "账户与设备绑定" in out
    finally:
        bundle.close()
