"""PRD 2.5 任务管理: 二级目录 / 分类排序 / 向导式创建子任务 / 事件属性。

用户测试反馈原话是"该功能我没有看到" —— 所以这些用例不只验数据, 还要验
**入口确实存在、点了真的有反应**: 菜单项、向导弹窗、分组按钮、事件属性分组。
另外专门钉住三条最容易做丢的:
  * 划掉主任务时子任务必须一起结束 (否则主任务没了、子任务的提醒还在响);
  * 改事件时间必须重排触发点 (只改库里的字段 = 提醒还按老时间响);
  * 向导中途取消/关窗不能丢已填内容, 也不能留下半截任务树。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from reminder_assistant.app import App
from reminder_assistant.clock import FakeClock
from reminder_assistant.models import LEVELS, Reminder, ReminderPlan
from reminder_assistant.store import StoreError
from reminder_assistant.tasks import (GROUP_MODES, SubTaskSpec, SubtaskError, WizardState,
                                      chain_times, group_label, order_tree)

TZ = datetime(2026, 10, 8, 10, 0).astimezone().tzinfo
NOW = datetime(2026, 10, 8, 10, 0, tzinfo=TZ)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture()
def env(tmp_path):
    app = App(tmp_path, notifier=None)
    app.clock = FakeClock(NOW)
    app.scheduler.clock = app.clock
    yield app
    app.close()


@pytest.fixture()
def parent(env):
    rem, _ = env.create_from_text("明天下午三点开会", level="日常", now=NOW)
    return rem


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6", reason="未安装 PySide6, 跳过界面用例")
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


# ====================================================================== 纯逻辑

def test_chain_times_follows_durations():
    """第二个子任务在第一个**结束**时开始 —— 持续时间不是装饰, 它决定顺推。"""
    start = datetime(2026, 10, 9, 15, 0, tzinfo=TZ)
    got = chain_times(start, [30, 45, 15])
    assert got == [datetime(2026, 10, 9, 15, 0, tzinfo=TZ),
                   datetime(2026, 10, 9, 15, 30, tzinfo=TZ),
                   datetime(2026, 10, 9, 16, 15, tzinfo=TZ)]
    assert chain_times(None, [30, 45]) == [None, None]      # 主任务没时间也不该炸


def test_wizard_state_navigates_and_keeps_values():
    st = WizardState.create(3, start_at=NOW, parent_title="开会")
    assert st.count == 3 and st.index == 0 and st.step_hint == "第 1/3 个子任务"
    st.set_current("整理需求", 30)
    assert st.next() and st.index == 1
    st.set_current("写文档", 45)
    assert st.next() and st.index == 2
    assert st.is_last and st.next() is False                # 最后一个不再往下走
    assert st.back() and st.index == 1
    assert st.specs[0].clean_title() == "整理需求"           # 来回走不丢
    assert st.specs[1].duration_min == 45


def test_wizard_state_validates_names():
    st = WizardState.create(2, start_at=NOW)
    assert "还没有名字" in st.validate_current()
    st.set_current("甲", 10)
    assert st.validate_current() is None
    st.goto(1)
    assert "第 2 个子任务" in st.validate_all(), "没填完的项要指名道姓"
    st.set_current("甲", 10)                                # 重名
    assert "重名" in st.validate_all()
    assert len(st.filled_specs()) == 2
    st.set_current("乙", 10)
    assert st.validate_all() is None


def test_wizard_draft_roundtrip_and_corrupt_draft():
    st = WizardState.create(3, start_at=NOW, parent_title="开会")
    st.set_current("整理需求", 25)
    st.next()
    st.set_current("写文档", 55)
    st.mode = "list"
    back = WizardState.from_draft(st.to_draft(), start_at=NOW)
    assert back is not None
    assert [(s.title, s.duration_min) for s in back.specs] == \
           [(s.title, s.duration_min) for s in st.specs]
    assert back.index == 1 and back.mode == "list"
    assert back.times() == st.times()
    assert WizardState.from_draft(None) is None
    assert WizardState.from_draft("{不是 json") is None
    assert WizardState.from_draft('{"specs": []}') is None


def _item(rid, title, when, level="日常", uuid=None, parent_uuid="", sort_key=0.0, depth=0):
    return {"id": rid, "uuid": uuid or f"u{rid}", "title": title, "level": level,
            "event_at": when, "next_fire": when, "parent_uuid": parent_uuid,
            "sort_key": sort_key, "depth": depth, "duration_min": 0}


def test_order_tree_keeps_children_under_their_parent():
    when = NOW + timedelta(hours=3)
    items = [_item(1, "父甲", when),
             _item(3, "父乙", when - timedelta(hours=1)),
             _item(2, "子甲B", when, parent_uuid="u1", sort_key=2, depth=1),
             _item(4, "子甲A", when, parent_uuid="u1", sort_key=1, depth=1)]
    out = order_tree(items, "time", NOW)
    assert [(o["id"], o["depth"]) for o in out] == [(3, 0), (1, 0), (4, 1), (2, 1)]
    # ↑ 父乙(更早)在前; 父甲后面紧跟它的子任务, 且按 sort_key 排序
    assert out[1]["child_count"] == 2


def test_order_tree_orphan_child_is_promoted():
    """父任务被删了/完成了, 子任务不能就此消失 —— 否则提醒还在响, 用户却看不到。"""
    when = NOW + timedelta(hours=1)
    out = order_tree([_item(9, "孤儿", when, parent_uuid="不存在的父", depth=1)], "time", NOW)
    assert len(out) == 1 and out[0]["depth"] == 0 and out[0]["orphan"] is True


def test_group_modes_label_and_sort():
    soon = NOW + timedelta(hours=1)
    tomorrow = NOW + timedelta(days=1, hours=2)
    items = [_item(1, "日常的", soon, "日常"), _item(2, "紧急的", tomorrow, "紧急")]
    by_time = order_tree(items, "time", NOW)
    assert [o["id"] for o in by_time] == [1, 2] and by_time[0]["group_label"] == ""
    by_level = order_tree(items, "level", NOW)
    assert [o["id"] for o in by_level] == [2, 1], "按重要程度: 紧急要排在日常前面"
    assert by_level[0]["group_label"] == "「紧急」"
    by_date = order_tree(items, "date", NOW)
    assert by_date[0]["group_label"] == "今天" and by_date[1]["group_label"] == "明天"
    assert group_label({"next_fire": None}, "date", NOW) == "没定时间的"
    assert set(GROUP_MODES) == {"time", "level", "date"}


# ====================================================================== 数据层

def test_duration_roundtrips_and_children_are_ordered(env, parent):
    a = env.life.add_child(parent, "第二步", 30, now=NOW)
    b = env.life.add_child(parent, "第一步", 15, now=NOW)
    assert (a.duration_min, b.duration_min) == (30, 15)
    assert env.store.get_reminder(a.id).duration_min == 30            # 真的落库了
    kids = env.store.children_of(parent.uuid)
    assert [k.title for k in kids] == ["第二步", "第一步"], "顺序要按创建顺序"
    assert [k.sort_key for k in kids] == [1.0, 2.0]
    assert b.parent_uuid == parent.uuid and b.depth == 1
    assert env.store.count_children(parent.uuid) == 2
    assert b.plan.offsets == parent.plan.offsets                      # 继承主任务的频率
    assert b.level == parent.level


def test_children_are_real_reminders(env, parent):
    child = env.life.add_child(parent, "写文档", 45, event_at=NOW + timedelta(hours=1), now=NOW)
    occs = env.store.occurrences_of(child.id)
    assert len(occs) == len(child.plan.offsets), "子任务没有触发点 = 到点不会提醒"
    assert env.store.get_reminder(parent.id).status == "active"       # 建子任务不影响主任务
    assert len(env.store.list_active()) == 2


def test_only_two_levels_allowed(env, parent):
    child = env.life.add_child(parent, "第一步", 10, now=NOW)
    with pytest.raises(SubtaskError, match="二级"):
        env.life.add_child(child, "第三步", 10, now=NOW)
    assert env.store.count_children(child.uuid) == 0


def test_add_children_batch_and_resume(env, parent):
    specs = [SubTaskSpec("整理需求", 30), SubTaskSpec("写文档", 45), SubTaskSpec("评审", 15)]
    made = env.life.add_children(parent, specs, now=NOW)
    assert [m.title for m in made] == ["整理需求", "写文档", "评审"]
    assert [m.event_at for m in made] == [
        datetime(2026, 10, 9, 15, 0, tzinfo=TZ),
        datetime(2026, 10, 9, 15, 30, tzinfo=TZ),
        datetime(2026, 10, 9, 16, 15, tzinfo=TZ)]


def test_update_attributes_rejects_unknown_field(env, parent):
    with pytest.raises(StoreError, match="不认识的属性"):
        env.store.update_attributes(parent.id, tittle="打错了")
    assert env.life.update_attributes(parent.id, title="改名了", duration_min=20).title == "改名了"


def test_complete_parent_takes_children_with_it(env, parent):
    """最典型的幽灵提醒: 主任务划掉了, 子任务还在响, 而便签上已经看不到它们。"""
    kids = env.life.add_children(parent, [SubTaskSpec("甲", 10), SubTaskSpec("乙", 10)], now=NOW)
    env.life.complete(parent.id)
    assert env.store.list_active() == []
    assert env.store.get_reminder(parent.id).status == "completed"
    assert all(env.store.get_reminder(k.id).status == "completed" for k in kids)
    assert env.store.pending_occurrences() == [], "完成后还有待触发点 = 还会响"


def test_delete_parent_removes_children(env, parent):
    kids = env.life.add_children(parent, [SubTaskSpec("甲", 10), SubTaskSpec("乙", 10)], now=NOW)
    env.life.remove(parent.id)
    assert env.store.get_reminder(parent.id) is None
    assert all(env.store.get_reminder(k.id) is None for k in kids), "子任务被留下了"
    assert env.store.list_active() == []


def test_changing_the_event_time_replans_occurrences(env, parent):
    """改时间只 UPDATE 字段是不够的 —— 触发点必须跟着重排, 否则还按老时间响。"""
    before = [o.fire_at for o in env.store.occurrences_of(parent.id)]
    assert before == [datetime(2026, 10, 9, 15, 0, tzinfo=TZ)]
    env.life.update_attributes(parent.id, event_at=datetime(2026, 10, 9, 18, 0, tzinfo=TZ))
    after = [o.fire_at for o in env.store.occurrences_of(parent.id)]
    assert after == [datetime(2026, 10, 9, 18, 0, tzinfo=TZ)]
    env.clock.set_wall(datetime(2026, 10, 9, 15, 0, tzinfo=TZ))
    assert env.scheduler.poll(env.clock.now()) == [], "旧时刻还在响"


def test_moving_parent_shifts_the_whole_chain(env, parent):
    kids = env.life.add_children(parent, [SubTaskSpec("甲", 30), SubTaskSpec("乙", 30)], now=NOW)
    env.life.update_attributes(parent.id, event_at=datetime(2026, 10, 9, 17, 0, tzinfo=TZ))
    assert [env.store.get_reminder(k.id).event_at for k in kids] == [
        datetime(2026, 10, 9, 17, 0, tzinfo=TZ),          # 平移 2 小时, 相互间隔不变
        datetime(2026, 10, 9, 17, 30, tzinfo=TZ)]
    assert [o.fire_at for o in env.store.occurrences_of(kids[0].id)] == \
           [datetime(2026, 10, 9, 17, 0, tzinfo=TZ)]


def test_drafts_survive_reopen(tmp_path):
    a = App(tmp_path, notifier=None)
    a.store.save_draft("subtask:u1", '{"specs":[{"title":"甲","duration_min":10}]}', "u1")
    a.close()
    b = App(tmp_path, notifier=None)
    assert "甲" in (b.store.load_draft("subtask:u1") or "")
    b.store.clear_draft("subtask:u1")
    assert b.store.load_draft("subtask:u1") is None
    b.close()


def test_board_returns_tree_with_child_count(env, parent):
    env.life.add_children(parent, [SubTaskSpec("甲", 30), SubTaskSpec("乙", 30)], now=NOW)
    rows = env.life.board(NOW)["active"]
    assert [(r["depth"], r["title"]) for r in rows] == [(0, "明天下午三点开会"), (1, "甲"), (1, "乙")]
    assert rows[0]["child_count"] == 2 and rows[0]["uuid"] == parent.uuid
    assert rows[1]["duration_min"] == 30
    assert env.life.board(NOW, mode="level")["mode"] == "level"


# ====================================================================== 向导界面

def _wizard(env, reminder, state=None):
    from reminder_assistant.ui.subtask_wizard import SubTaskWizardDialog

    return SubTaskWizardDialog(env, reminder, state=state)


def test_count_dialog_asks_how_many(qapp):
    from reminder_assistant.ui.subtask_wizard import CountDialog

    dlg = CountDialog("开会", default=3)
    assert "要创建几个子任务" in dlg.lbl_head.text()
    assert dlg.sp_count.value() == 3 and dlg.sp_count.suffix() == " 个"
    assert dlg.sp_count.minimum() == 1
    dlg.sp_count.setValue(7)
    assert dlg.count() == 7


def test_wizard_creates_the_filled_children(env, parent, qapp):
    """PRD 的原动线: 第一个填完点下一步, 依次弹第二个第三个, 最后点完成。"""
    from PySide6.QtWidgets import QDialog

    dlg = _wizard(env, parent, WizardState.create(3, start_at=parent.event_at))
    assert dlg.lbl_step.text().startswith("第 1/3 个子任务")
    try:
        dlg.ed_title.setText("整理需求")
        dlg.sp_duration.setValue(30)
        dlg._on_next()
        assert dlg.state.index == 1
        dlg.ed_title.setText("写文档")
        dlg.sp_duration.setValue(45)
        dlg._on_next()
        dlg.ed_title.setText("评审")
        dlg.sp_duration.setValue(15)
        assert dlg.state.is_last and not dlg.btn_next.isVisibleTo(dlg)
        dlg._on_finish()
        assert dlg.result() == QDialog.Accepted
    finally:
        dlg.close()

    kids = env.store.children_of(parent.uuid)
    assert [k.title for k in kids] == ["整理需求", "写文档", "评审"]
    assert [k.duration_min for k in kids] == [30, 45, 15]
    assert kids[1].event_at == kids[0].event_at + timedelta(minutes=30)
    assert env.store.load_draft(f"subtask:{parent.uuid}") is None, "完成后草稿该清掉"


def test_wizard_step_validation_blocks_empty_name(env, parent, qapp):
    dlg = _wizard(env, parent, WizardState.create(2, start_at=parent.event_at))
    seen: list[str] = []
    dlg.report_error = seen.append
    try:
        dlg.ed_title.setText("")
        dlg._on_next()
        assert dlg.state.index == 0 and seen and "名字" in seen[0]
    finally:
        dlg.close()


def test_wizard_cancel_keeps_a_draft_and_creates_nothing(env, parent, qapp):
    """PRD 风险预警第 4 条: 中途 Esc 不能把前面填的全丢掉。"""
    from reminder_assistant.ui.subtask_wizard import draft_key, resume_state

    dlg = _wizard(env, parent, WizardState.create(3, start_at=parent.event_at))
    dlg.ed_title.setText("整理需求")
    dlg.sp_duration.setValue(20)
    dlg._on_next()
    dlg.ed_title.setText("写文档")
    dlg.reject()                                  # 相当于按 Esc / 点取消
    assert env.store.children_of(parent.uuid) == [], "取消不该建出半截任务树"

    st = resume_state(env, parent)
    assert st is not None, "草稿没留下"
    assert (st.index, st.specs[0].title, st.specs[0].duration_min) == (1, "整理需求", 20)
    assert st.specs[1].title == "写文档"
    again = _wizard(env, parent, st)
    assert again.state.count == 3 and again.state.current.title == "写文档"
    again.reject()
    assert env.store.load_draft(draft_key(parent.uuid)) is not None


def test_wizard_list_view_edits_all_rows_at_once(env, parent, qapp):
    """10 个子任务不该弹 10 次窗 —— 「转普通列表视图」一次填完。"""
    from PySide6.QtWidgets import QDialog

    dlg = _wizard(env, parent, WizardState.create(3, start_at=parent.event_at))
    try:
        dlg._toggle_view()
        assert dlg.state.mode == "list" and dlg.table.rowCount() == 3
        for i, (name, dur) in enumerate([("甲", 10), ("乙", 20), ("丙", 30)]):
            dlg.table.item(i, 0).setText(name)
            dlg.table.item(i, 1).setText(str(dur))
        dlg.btn_add_row.click()
        dlg.table.item(3, 0).setText("丁")
        dlg.table.item(3, 1).setText("坏数字")      # 填坏不该弹错, 退回默认值
        dlg._toggle_view()                          # 回到分步视图
        assert dlg.state.mode == "step"
        assert [s.title for s in dlg.state.specs] == ["甲", "乙", "丙", "丁"]
        assert dlg.state.specs[3].duration_min == 30
        assert dlg.ed_title.text() == "甲"          # 分步页跟着列表走
        dlg._on_finish()
        assert dlg.result() == QDialog.Accepted
    finally:
        dlg.close()
    assert [k.title for k in env.store.children_of(parent.uuid)] == ["甲", "乙", "丙", "丁"]


def test_wizard_shows_the_whole_chain_before_saving(env, parent, qapp):
    dlg = _wizard(env, parent, WizardState.create(2, start_at=parent.event_at))
    try:
        dlg.ed_title.setText("甲")
        dlg.sp_duration.setValue(60)
        assert "15:00" in dlg.lbl_chain.text() and "60 分钟" in dlg.lbl_chain.text()
    finally:
        dlg.close()


# ====================================================================== 便签界面

def _win(env):
    from reminder_assistant.ui.notepad import NotepadWindow

    w = NotepadWindow(env)
    w.show()
    return w


def test_note_renders_children_under_parent_and_can_collapse(env, parent, qapp):
    env.life.add_children(parent, [SubTaskSpec("甲", 30), SubTaskSpec("乙", 30)], now=NOW)
    win = _win(env)
    try:
        rows = win.pages[0]._rows
        assert set(rows) == {parent.id, *[k.id for k in env.store.children_of(parent.uuid)]}
        child_row = rows[env.store.children_of(parent.uuid)[0].id]
        assert child_row.depth == 1 and child_row.text.text().startswith("└")
        assert rows[parent.id].btn_children.isVisibleTo(rows[parent.id])
        assert rows[parent.id].btn_children.text() == "▾2"

        win._toggle_children(parent.id)              # 收起二级目录
        assert set(win.pages[0]._rows) == {parent.id}
        assert win.pages[0].row(parent.id).btn_children.text() == "▸2"
        win._toggle_children(parent.id)              # 再展开
        assert len(win.pages[0]._rows) == 3
    finally:
        win.close()


def _visual_order(win) -> list[dict]:
    """便签上**从上到下**的行 (不能拿 _rows 的字典顺序当显示顺序 —— 那是创建顺序)。"""
    from reminder_assistant.ui.notepad import TaskRow

    out = []
    for i in range(win.pages[0]._lay.count()):
        w = win.pages[0]._lay.itemAt(i).widget()
        if isinstance(w, TaskRow):
            out.append(w.item)
    return out


def test_group_button_cycles_and_persists(env, parent, qapp):
    env.life.add_children(parent, [SubTaskSpec("甲", 30)], now=NOW)
    win = _win(env)
    try:
        assert win.group_mode == "time" and win.btn_group.text() == "⇅时间"
        win.btn_group.click()
        assert win.group_mode == "level" and win.btn_group.text() == "⇅重要"
        assert env.store.get_meta("task_group") == "level"
        # 按重要程度排: 紧急的主任务要冒到前面 (这里造一个紧急的)
        urgent = Reminder(title="交方案", event_at=NOW + timedelta(hours=9), created_at=NOW,
                          level="紧急", plan=ReminderPlan.preset("紧急"))
        env.life.create(urgent, NOW)
        win.refresh(force=True)
        order = _visual_order(win)
        assert order[0]["level"] == "紧急", [o["title"] for o in order]
        assert order[0]["group_label"] == "「紧急」"
        assert order[0]["title"] == "交方案"
        assert "「日常」" in [o["group_label"] for o in order]
        win.btn_group.click()
        assert win.group_mode == "date"
    finally:
        win.close()

    again = _win(env)
    try:
        assert again.group_mode == "date", "分类方式没有记住"
    finally:
        again.close()


def test_date_mode_puts_a_header_before_each_day(env, parent, qapp):
    """按开始日期分类: 每组前面要有"今天/明天"表头, 表头只出现一次。"""
    env.life.create(Reminder(title="今天的事", event_at=NOW + timedelta(hours=6), created_at=NOW,
                             level="日常", plan=ReminderPlan.preset("日常")), NOW)
    win = _win(env)
    try:
        win.btn_group.click()                      # -> level
        win.btn_group.click()                      # -> date
        assert win.group_mode == "date"
        win.refresh(force=True)
        labels = [it["group_label"] for it in _visual_order(win)]
        assert "今天" in labels and "明天" in labels, labels
        assert labels.count("明天") == 1, "同一个分组不该插两条表头"
        assert win.pages[0].header("今天") is not None
    finally:
        win.close()


def test_menu_opens_the_wizard_and_creates_children(env, parent, qapp):
    """从"看到菜单"到"库里真的多了子任务"的整条链路 (点向导 = 人工验收的部分)。"""
    from PySide6.QtWidgets import QDialog

    from reminder_assistant.ui.subtask_wizard import CountDialog, SubTaskWizardDialog

    win = _win(env)
    opened: list = []

    class AutoCount(CountDialog):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.sp_count.setValue(2)

    def fake_exec(dlg):
        opened.append(dlg)
        if isinstance(dlg, AutoCount):
            return QDialog.Accepted
        if isinstance(dlg, SubTaskWizardDialog):
            dlg.ed_title.setText("甲")
            dlg._on_next()
            dlg.ed_title.setText("乙")
            dlg._on_finish()
            return dlg.result()
        return QDialog.Rejected

    import reminder_assistant.ui.subtask_wizard as sw

    real_count = sw.CountDialog
    sw.CountDialog = AutoCount
    try:
        win._exec_dialog = fake_exec
        menu = win._task_menu(parent.id)
        sub = next(a for a in menu.actions() if "创建子任务" in a.text())
        sub.trigger()
    finally:
        sw.CountDialog = real_count
        win.close()

    assert [type(d).__name__ for d in opened] == ["AutoCount", "SubTaskWizardDialog"]
    assert [k.title for k in env.store.children_of(parent.uuid)] == ["甲", "乙"]
    assert len(env.store.list_active()) == 3


def test_menu_delete_asks_then_removes_the_tree(env, parent, qapp):
    env.life.add_children(parent, [SubTaskSpec("甲", 30)], now=NOW)
    win = _win(env)
    asked: list[str] = []
    try:
        win._confirm = lambda title, text: (asked.append(text), False)[1]   # 用户点了"否"
        win._delete_reminder(parent.id)
        assert asked and "子任务" in asked[0], "删除主任务时要提醒会连带删掉子任务"
        assert len(env.store.list_active()) == 2, "点了否还是删了"

        win._confirm = lambda title, text: True
        win._delete_reminder(parent.id)
        assert env.store.list_active() == []
    finally:
        win.close()


def test_completing_a_parent_also_strikes_out_children(env, parent, qapp):
    env.life.add_children(parent, [SubTaskSpec("甲", 30), SubTaskSpec("乙", 30)], now=NOW)
    win = _win(env)
    try:
        win.pages[0].row(parent.id).done.emit(parent.id, 0)
        win.refresh(force=True)
        assert env.store.list_active() == []
        assert win.pages[0]._rows == {}
    finally:
        win.close()


def test_page_edit_via_menu_and_attributes_dialog(env, parent, qapp):
    """点「属性」→ 改名称/持续时间 → 便签上的字跟着变。"""
    from reminder_assistant.ui.settings_dialog import SettingsDialog

    win = _win(env)
    seen: list = []
    try:
        def fake_exec(dlg):
            seen.append(dlg)
            assert isinstance(dlg, SettingsDialog)
            dlg.ed_title.setText("改成新名字")
            dlg.sp_duration.setValue(25)
            dlg.dt_event.setDateTime(dlg.dt_event.dateTime().addSecs(3600))
            dlg.accept()
            return dlg.result()

        win._exec_dialog = fake_exec
        win.pages[0].row(parent.id).btn_advanced.click()
        got = env.store.get_reminder(parent.id)
        assert got.title == "改成新名字" and got.duration_min == 25
        assert got.event_at == datetime(2026, 10, 9, 16, 0, tzinfo=TZ)      # 15:00 -> 16:00
        assert [o.fire_at for o in env.store.occurrences_of(parent.id)] == [got.event_at]
        assert win.pages[0].row(parent.id).item["title"] == "改成新名字"
    finally:
        win.close()


# ====================================================================== 事件属性面板

def test_attributes_panel_edits_title_time_level_duration_note(env, parent, qapp):
    from PySide6.QtWidgets import QDialog

    from reminder_assistant.ui.settings_dialog import SettingsDialog, to_qdatetime

    dlg = SettingsDialog(env, reminder=parent)
    assert dlg.box_attributes.isVisibleTo(dlg)
    assert dlg.ed_title.text() == "明天下午三点开会"
    assert dlg.cb_attr_level.currentText() == "日常"
    assert dlg.sp_duration.value() == 0
    try:
        dlg.ed_title.setText("周会")
        dlg.dt_event.setDateTime(to_qdatetime(datetime(2026, 10, 9, 16, 30, tzinfo=TZ)))
        dlg.sp_duration.setValue(90)
        dlg.cb_attr_level.setCurrentText("紧急")
        dlg.ed_note.setText("带上上周的数据")
        assert dlg.attributes_changed()["title"] == "周会"
        assert "16:30" in dlg.lbl_head.text(), "标题栏要跟着改的时间走"
        assert "16:30" in dlg.lbl_preview.text(), "预览也必须用改后的时间"
        # 频率这一层归"提醒频率"面板管: 想要紧急等级的 5 次提醒, 就把自定义勾掉、选等级
        dlg.box_custom.setChecked(False)
        dlg.cb_level.setCurrentText("紧急")
        assert "共 5 次" in dlg.lbl_preview.text()
        dlg.accept()
        assert dlg.result() == QDialog.Accepted
    finally:
        dlg.close()

    got = env.store.get_reminder(parent.id)
    assert (got.title, got.level, got.duration_min, got.note) == ("周会", "紧急", 90, "带上上周的数据")
    assert got.event_at == datetime(2026, 10, 9, 16, 30, tzinfo=TZ)
    assert dlg.result_attributes["title"] == "周会"
    times = [o.fire_at for o in env.store.occurrences_of(parent.id)]
    # 触发点必须 = 新的事件时间 + 计划偏移 (等级"紧急"的偏移都在事件之前, 所以最后一个不是事件时刻)
    assert times == [got.event_at + off for off in got.plan.offsets], "触发点没有跟着新时间重排"
    assert len(times) == 5                                    # 紧急: 5 次
    assert times[0] == got.event_at - LEVELS["紧急"]["lead"]


def test_attributes_panel_leaves_untouched_fields_alone(env, parent, qapp):
    from reminder_assistant.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(env, reminder=parent)
    try:
        dlg.ed_note.setText("只改备注")
        dlg.accept()
    finally:
        dlg.close()
    assert dlg.result_attributes == {"note": "只改备注"}
    got = env.store.get_reminder(parent.id)
    assert got.event_at == datetime(2026, 10, 9, 15, 0, tzinfo=TZ)   # 时间一个字没动
    assert got.title == "明天下午三点开会"


def test_global_settings_dialog_has_no_attribute_group(env, qapp):
    """全局设置里不该出现"事件属性"(那是针对单条提醒的), 免得改错对象。"""
    from reminder_assistant.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(env)
    try:
        assert not hasattr(dlg, "box_attributes")
        assert dlg.windowTitle() == "设置"
    finally:
        dlg.close()
