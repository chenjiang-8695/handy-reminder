"""设置面板与"高级事件"用例 (PRD 2.3)。

覆盖三件容易做丢的事:
  1. 等级配方能被用户改并活下去 (写盘 -> 重开会话仍在)
  2. 实时预览算出来的时间, 必须和调度器实际用的触发时间一致
     (预览和实际不一致是这类面板最典型也最难被发现的缺陷)
  3. 改某条提醒的频率, 要真的重排触发点, 且旧触发点不能再响
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from reminder_assistant.app import App
from reminder_assistant.clock import FakeClock
from reminder_assistant.models import LEVELS, ReminderPlan
from reminder_assistant.settings import MAX_REPEAT, LevelSpec, Settings
from reminder_assistant.store import Store

TZ = datetime(2026, 10, 8, 10, 0).astimezone().tzinfo
NOW = datetime(2026, 10, 8, 10, 0, tzinfo=TZ)


@pytest.fixture()
def env(tmp_path):
    app = App(tmp_path, notifier=None)
    app.clock = FakeClock(NOW)
    app.scheduler.clock = app.clock
    yield app
    app.close()


# ---------------------------------------------------------------- 默认值
def test_defaults_match_shipped_levels():
    s = Settings()
    for name, cfg in LEVELS.items():
        spec = s.spec(name)
        assert (spec.lead, spec.interval, spec.repeat) == (cfg["lead"], cfg["interval"], cfg["repeat"])
    # 出厂默认的计划必须与 models 里的预设一致, 否则"设置"一打开就悄悄改了行为
    for name in LEVELS:
        assert s.plan(name).offsets == ReminderPlan.preset(name).offsets


def test_unknown_level_falls_back_to_daily():
    s = Settings()
    assert s.spec("不存在的等级").repeat == s.spec("日常").repeat


# ---------------------------------------------------------------- 计划与预览
def test_from_spec_matches_prd_custom_example():
    """PRD 2.3: 事件 15:50, 第一次提前 1 小时, 之后间隔 20 分钟 -> 14:50/15:10/15:30。"""
    plan = ReminderPlan.from_spec(timedelta(hours=1), timedelta(minutes=20), 3)
    event = datetime(2026, 10, 8, 15, 50, tzinfo=TZ)
    assert [event + off for off in plan.offsets] == [
        datetime(2026, 10, 8, 14, 50, tzinfo=TZ),
        datetime(2026, 10, 8, 15, 10, tzinfo=TZ),
        datetime(2026, 10, 8, 15, 30, tzinfo=TZ)]


def test_from_spec_rejects_bad_input():
    with pytest.raises(ValueError):
        ReminderPlan.from_spec(timedelta(0), timedelta(minutes=5), 0)
    with pytest.raises(ValueError):
        ReminderPlan.from_spec(timedelta(0), timedelta(minutes=-1), 3)
    with pytest.raises(ValueError):
        ReminderPlan.from_spec(timedelta(minutes=-1), timedelta(minutes=5), 3)


def test_preview_matches_actual_scheduling():
    """预览的时间必须等于调度器真正会用的触发时间 —— 这是本面板的核心承诺。"""
    s = Settings()
    s.levels["重要"] = LevelSpec(lead=timedelta(minutes=45), interval=timedelta(minutes=15), repeat=4)
    event = NOW + timedelta(hours=5)
    preview = s.preview("重要", event)
    assert preview == [event + off for off in s.plan("重要").offsets]
    assert len(preview) == 4
    assert preview[0] == event - timedelta(minutes=45)
    assert (preview[1] - preview[0]) == timedelta(minutes=15)


def test_preview_custom_uses_last_used_recipe():
    s = Settings()
    s.custom = LevelSpec(lead=timedelta(hours=2), interval=timedelta(minutes=30), repeat=2)
    out = s.preview_custom(NOW + timedelta(days=1))
    assert len(out) == 2 and (out[1] - out[0]) == timedelta(minutes=30)


def test_level_describe_is_human_readable():
    assert LevelSpec(interval=timedelta(minutes=15), repeat=1).describe() == "到点提醒一次"
    assert "共 3 次" in LevelSpec(lead=timedelta(minutes=30), interval=timedelta(minutes=10),
                                repeat=3).describe()


# ---------------------------------------------------------------- 持久化
def test_settings_roundtrip_through_store(tmp_path):
    store = Store(tmp_path / "s.db")
    s = Settings.load(store)
    s.levels["紧急"] = LevelSpec(lead=timedelta(minutes=5), interval=timedelta(minutes=2), repeat=7)
    s.custom = LevelSpec(lead=timedelta(minutes=90), interval=timedelta(minutes=25), repeat=4)
    s.quiet_enabled = True
    s.quiet_start = datetime(2026, 1, 1, 23, 15).time()
    s.quiet_end = datetime(2026, 1, 1, 6, 45).time()
    s.hotkey = "ctrl+alt+r"
    s.save(store)
    store.close()

    reopened = Store(tmp_path / "s.db")
    got = Settings.load(reopened)
    assert got.levels["紧急"].repeat == 7
    assert got.levels["紧急"].lead == timedelta(minutes=5)
    assert got.custom.interval == timedelta(minutes=25)
    assert got.quiet_enabled is True
    assert (got.quiet_start.hour, got.quiet_start.minute) == (23, 15)
    assert (got.quiet_end.hour, got.quiet_end.minute) == (6, 45)
    assert got.hotkey == "ctrl+alt+r"
    assert got.quiet_hours().contains(datetime(2026, 10, 8, 23, 30, tzinfo=TZ))
    reopened.close()


def test_corrupt_settings_fall_back_to_defaults(tmp_path):
    store = Store(tmp_path / "s.db")
    store.set_meta("settings.v1", "{ 这不是 json")
    assert Settings.load(store).spec("日常").repeat == LEVELS["日常"]["repeat"]
    store.set_meta("settings.v1", '{"levels": {"日常": {"lead": "坏"}}}')
    assert Settings.load(store).spec("日常").repeat == LEVELS["日常"]["repeat"]
    store.close()


def test_repeat_out_of_range_is_clamped():
    bad = LevelSpec.from_json({"lead": 0, "interval": 60, "repeat": 9999},
                              LevelSpec(interval=timedelta(minutes=1), repeat=1))
    assert bad.repeat == MAX_REPEAT


def test_settings_object_is_copied_not_aliased():
    s = Settings()
    c = s.copy()
    c.levels["日常"] = LevelSpec(interval=timedelta(minutes=1), repeat=9)
    assert s.spec("日常").repeat == LEVELS["日常"]["repeat"]


# ---------------------------------------------------------------- 应用到应用
def test_apply_settings_takes_effect_for_new_reminders(env):
    s = env.settings.copy()
    s.levels["重要"] = LevelSpec(lead=timedelta(minutes=30), interval=timedelta(minutes=10), repeat=3)
    env.apply_settings(s)

    rem, _ = env.create_from_text("明天下午三点开会", level="重要", now=NOW)
    times = [o.fire_at for o in env.store.occurrences_of(rem.id)]
    assert len(times) == 3
    assert times[0] == datetime(2026, 10, 9, 14, 30, tzinfo=TZ)
    assert (times[1] - times[0]) == timedelta(minutes=10)


def test_apply_settings_updates_quiet_hours_immediately(env):
    s = env.settings.copy()
    s.quiet_enabled = True
    s.quiet_start = datetime(2026, 1, 1, 12, 0).time()
    s.quiet_end = datetime(2026, 1, 1, 13, 0).time()
    env.apply_settings(s)
    assert env.center.quiet.enabled is True
    assert env.center.quiet.contains(datetime(2026, 10, 8, 12, 30, tzinfo=TZ))
    assert not env.center.quiet.contains(datetime(2026, 10, 8, 14, 0, tzinfo=TZ))


def test_settings_are_reloaded_next_session(tmp_path):
    a = App(tmp_path, notifier=None)
    s = a.settings.copy()
    s.levels["日常"] = LevelSpec(lead=timedelta(minutes=7), interval=timedelta(minutes=3), repeat=5)
    a.apply_settings(s)
    a.close()

    b = App(tmp_path, notifier=None)
    assert b.settings.spec("日常").repeat == 5
    assert b.settings.spec("日常").lead == timedelta(minutes=7)
    assert b.center.quiet.enabled == b.settings.quiet_enabled
    b.close()


# ---------------------------------------------------------------- 改单条提醒
def test_change_plan_reschedules_and_old_occurrences_never_fire(env):
    rem, _ = env.create_from_text("明天下午三点开会", level="日常", now=NOW)
    assert len(env.store.occurrences_of(rem.id)) == 1      # 日常: 到点一次
    env.scheduler.refresh(rem.id)                          # 确保堆里就是旧的

    plan = ReminderPlan.from_spec(timedelta(minutes=20), timedelta(minutes=5), 3)
    base = env.life.change_plan(rem.id, plan)
    occs = env.store.occurrences_of(rem.id)
    assert len(occs) == 3, "改频率必须真的重排触发点, 而不是只改库里的 plan 字段"
    assert all(o.gen == base and o.state == "pending" for o in occs)
    assert [o.fire_at for o in occs] == [
        datetime(2026, 10, 9, 14, 40, tzinfo=TZ),
        datetime(2026, 10, 9, 14, 45, tzinfo=TZ),
        datetime(2026, 10, 9, 14, 50, tzinfo=TZ)]

    # 新计划的触发点是 14:40/14:45/14:50; 旧触发点是 15:00
    env.clock.set_wall(datetime(2026, 10, 9, 14, 51, tzinfo=TZ))
    fired = env.scheduler.poll(env.clock.now())
    assert [f.fire_at.strftime("%H:%M") for f in fired] == ["14:40", "14:45", "14:50"]

    env.clock.set_wall(datetime(2026, 10, 9, 15, 0, tzinfo=TZ))
    late = env.scheduler.poll(env.clock.now())
    assert late == [], f"改完频率后旧触发点(15:00)仍然响了: {[f.fire_at for f in late]}"


def test_change_plan_to_fewer_occurrences_leaves_no_orphans(env):
    rem, _ = env.create_from_text("明天下午三点开会", level="紧急", now=NOW)   # 5 次
    assert len(env.store.occurrences_of(rem.id)) == 5
    env.life.change_plan(rem.id, ReminderPlan.from_spec(timedelta(0), timedelta(minutes=5), 1))
    occs = env.store.occurrences_of(rem.id)
    assert len(occs) == 1 and occs[0].fire_at == datetime(2026, 10, 9, 15, 0, tzinfo=TZ)
    assert env.scheduler.size <= 6      # 堆里最多只剩新条目 + 可能未清理的旧条目


def test_change_plan_persists_across_reopen(env, tmp_path):
    rem, _ = env.create_from_text("明天下午三点开会", level="日常", now=NOW)
    env.life.change_plan(rem.id, ReminderPlan.from_spec(timedelta(minutes=15),
                                                      timedelta(minutes=5), 2))
    env.close()
    again = App(tmp_path, notifier=None)
    got = again.store.get_reminder(rem.id)
    assert len(got.plan.offsets) == 2
    assert len(again.store.occurrences_of(rem.id)) == 2
    again.close()


# ---------------------------------------------------------------- 界面
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def test_dialog_builds_and_preview_updates_live(env, qapp):
    from reminder_assistant.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(env)
    dlg.box_custom.setChecked(True)
    dlg.sp_repeat.setValue(3)
    dlg.sp_lead.setValue(60)
    dlg.sp_interval.setValue(20)
    text = dlg.lbl_preview.text()
    assert "共 3 次" in text and "14:00" in text and "15:00" in text      # 示例锚点 15:00

    dlg.sp_repeat.setValue(2)                                             # 改一下应当立刻反映
    assert "共 2 次" in dlg.lbl_preview.text()
    dlg.reject()


def test_dialog_picking_a_level_fills_the_fields(env, qapp):
    from reminder_assistant.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(env)
    dlg.box_custom.setChecked(True)
    dlg.cb_level.setCurrentText("紧急")
    spec = env.settings.spec("紧急")
    assert dlg.sp_repeat.value() == spec.repeat
    assert dlg.sp_lead.value() == int(spec.lead.total_seconds() // 60)
    dlg.reject()


def test_dialog_saves_level_edits_and_reset_restores_defaults(env, qapp):
    from reminder_assistant.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(env)
    lead, interval, repeat = dlg.level_widgets["重要"]
    repeat.setValue(2)
    dlg.accept()
    assert env.settings.spec("重要").repeat == 2
    assert Settings.load(env.store).spec("重要").repeat == 2      # 落盘了

    dlg2 = SettingsDialog(env)
    dlg2._restore_defaults()
    dlg2.accept()
    assert env.settings.spec("重要").repeat == LEVELS["重要"]["repeat"]


def test_reminder_scoped_dialog_reschedules_that_reminder(env, qapp):
    from PySide6.QtWidgets import QDialog

    from reminder_assistant.ui.settings_dialog import SettingsDialog

    rem, _ = env.create_from_text("明天下午三点开会", level="日常", now=NOW)
    dlg = SettingsDialog(env, reminder=rem)
    assert not dlg.box_levels.isVisible()          # 单条模式不显示等级表
    assert dlg.box_custom.isChecked()              # 默认处于"自定义"展开状态
    assert "共 1 次" in dlg.lbl_preview.text()
    assert "10-09 15:00" in dlg.lbl_preview.text()  # 预览用的是这条提醒的事件时间

    dlg.sp_repeat.setValue(3)
    dlg.sp_lead.setValue(30)
    dlg.sp_interval.setValue(10)
    dlg.accept()
    assert dlg.result() == QDialog.Accepted
    occs = env.store.occurrences_of(rem.id)
    assert [o.fire_at for o in occs] == [
        datetime(2026, 10, 9, 14, 30, tzinfo=TZ),
        datetime(2026, 10, 9, 14, 40, tzinfo=TZ),
        datetime(2026, 10, 9, 14, 50, tzinfo=TZ)]


def test_reminder_scoped_dialog_can_use_a_level_recipe(env, qapp):
    from reminder_assistant.ui.settings_dialog import SettingsDialog

    rem, _ = env.create_from_text("明天下午三点开会", level="日常", now=NOW)
    dlg = SettingsDialog(env, reminder=rem)
    dlg.box_custom.setChecked(False)               # 不自定义, 直接用等级配方
    dlg.cb_level.setCurrentText("重要")
    dlg.accept()
    times = [o.fire_at for o in env.store.occurrences_of(rem.id)]
    assert len(times) == LEVELS["重要"]["repeat"]
    assert times[0] == datetime(2026, 10, 9, 15, 0, tzinfo=TZ) - LEVELS["重要"]["lead"]


def test_dialog_reports_error_without_blocking(env, qapp, monkeypatch):
    """保存失败时必须提示用户, 但绝不能弹出会让无头环境永久阻塞的模态框。

    这是踩过的坑: accept() 里直接 QMessageBox.warning 会把测试跑死 150 秒。
    """
    from PySide6.QtWidgets import QDialog

    from reminder_assistant.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(env)
    seen: list[str] = []
    monkeypatch.setattr(dlg, "report_error", seen.append)

    def boom(*_a, **_k):
        raise RuntimeError("磁盘写满了")

    monkeypatch.setattr(env, "apply_settings", boom)
    dlg.accept()
    assert seen and "磁盘写满了" in seen[0]
    assert dlg.result() != QDialog.Accepted, "保存失败时不能关窗"


def test_note_row_exposes_attribute_and_subtask_entries(env, qapp, monkeypatch):
    """每行要能: ① 点「属性」改事件属性 ② 点「⋯」建子任务 —— 两处都是用户反馈"没看到"的入口。"""
    from PySide6.QtWidgets import QDialog

    from reminder_assistant.ui.notepad import NotepadWindow
    from reminder_assistant.ui.settings_dialog import SettingsDialog

    env.create_from_text("明天下午三点开会", now=NOW)
    win = NotepadWindow(env)
    rid = next(iter(win.pages[0]._rows), None)
    assert rid is not None, "便签上没有卡片, 无法验证事件属性入口"
    row = win.pages[0].row(rid)
    assert row.btn_advanced.text() == "属性"

    # 点击「属性」必须打开一条针对"这条提醒"的面板, 而不是全局设置
    opened: list = []
    monkeypatch.setattr(win, "_exec_dialog", lambda dlg: opened.append(dlg) or QDialog.Rejected)
    row.btn_advanced.click()
    assert len(opened) == 1
    assert isinstance(opened[0], SettingsDialog)
    assert opened[0].reminder is not None and opened[0].reminder.id == rid
    assert hasattr(opened[0], "box_attributes"), "属性面板里没有事件属性分组"

    # ⋯ 菜单里要有"创建子任务…(向导)"和"删除这条"
    texts = [a.text() for a in win._task_menu(rid).actions()]
    assert any("创建子任务" in t for t in texts), texts
    assert any("删除这条" in t for t in texts), texts
    win.close()


def test_child_row_menu_has_no_subtask_entry(env, qapp):
    """只允许二级: 子任务那行的 ⋯ 菜单里不该有"创建子任务"(点了也没用)。"""
    from reminder_assistant.ui.notepad import NotepadWindow

    parent, _ = env.create_from_text("明天下午三点开会", now=NOW)
    env.life.add_child(parent, "整理需求", 30, now=NOW)
    win = NotepadWindow(env)
    child = env.store.children_of(parent.uuid)[0]
    texts = [a.text() for a in win._task_menu(child.id).actions()]
    assert not any("创建子任务" in t for t in texts), texts
    win.close()


def test_tray_opens_global_settings(env, qapp, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from reminder_assistant.ui.notepad import NotepadWindow
    from reminder_assistant.ui.settings_dialog import SettingsDialog

    win = NotepadWindow(env)
    opened: list = []
    monkeypatch.setattr(win, "_exec_dialog", lambda dlg: opened.append(dlg) or QDialog.Rejected)
    win._open_settings()
    assert len(opened) == 1 and isinstance(opened[0], SettingsDialog)
    assert opened[0].reminder is None            # 全局设置模式: 显示等级表
    assert opened[0].box_levels.isVisibleTo(opened[0])
    win.close()


# ---------------------------------------------------------------- 快捷键热切换 (不重启)
class FakeHotkey:
    """常驻热键的替身: 记录换键调用, 可指定某个组合键"被占用"。"""

    def __init__(self, spec: str = "alt+q", busy=()):
        self.spec = spec
        self.busy = set(busy)
        self.calls: list[str] = []

    def change(self, spec: str, timeout: float = 3.0) -> str:
        from reminder_assistant.hotkey import HotkeyError

        self.calls.append(spec)
        if spec in self.busy:
            raise HotkeyError(f"换快捷键 {spec.upper()} 失败: Win32 错误 1409。"
                              f"原来的 {self.spec.upper()} 仍然有效。")
        self.spec = spec
        return spec

    def stop(self) -> None:
        pass


def test_apply_settings_swaps_hotkey_without_restart(env):
    env.hotkey = FakeHotkey("alt+q")
    env.effective_hotkey = "alt+q"
    s = env.settings.copy()
    s.hotkey = "ctrl+alt+r"
    env.apply_settings(s)

    assert env.hotkey.calls == ["ctrl+alt+r"], "改快捷键没有真的去换, 只改了库里的字段"
    assert env.hotkey.spec == "ctrl+alt+r"
    assert env.effective_hotkey == "ctrl+alt+r"
    assert env.settings.hotkey == "ctrl+alt+r"
    assert Settings.load(env.store).hotkey == "ctrl+alt+r"      # 落盘, 下次启动仍生效


def test_apply_settings_keeps_old_hotkey_when_new_one_is_taken(env):
    """新键被占用时: 抛错给面板显示, 旧键仍然有效, 而且什么都别落盘。"""
    from reminder_assistant.hotkey import HotkeyError

    env.hotkey = FakeHotkey("alt+q", busy={"ctrl+alt+r"})
    env.effective_hotkey = "alt+q"
    s = env.settings.copy()
    s.hotkey = "ctrl+alt+r"
    with pytest.raises(HotkeyError) as ei:
        env.apply_settings(s)

    assert "CTRL+ALT+R" in str(ei.value)
    assert env.hotkey.spec == "alt+q"                     # 旧键还在
    assert env.effective_hotkey == "alt+q"
    assert env.settings.hotkey == "alt+q"
    assert Settings.load(env.store).hotkey == "alt+q", "换键失败却把设置写进去了"
    assert s.levels == env.settings.levels                # 同一批改动也不生效, 保持一致


def test_apply_settings_rejects_malformed_hotkey(env):
    """非法组合键不能进库: 存进去下次启动就注册失败, 用户连程序都起不来。"""
    from reminder_assistant.hotkey import HotkeyError

    env.hotkey = FakeHotkey("alt+q")
    env.effective_hotkey = "alt+q"
    s = env.settings.copy()
    s.hotkey = "q"
    with pytest.raises(HotkeyError):
        env.apply_settings(s)
    assert env.hotkey.calls == []
    assert Settings.load(env.store).hotkey == "alt+q"


def test_apply_settings_does_not_swap_when_hotkey_unchanged(env):
    env.hotkey = FakeHotkey("alt+q")
    env.effective_hotkey = "alt+q"
    s = env.settings.copy()
    s.quiet_enabled = True
    env.apply_settings(s)
    assert env.hotkey.calls == [], "快捷键没改也去重注册了, 中间会有一小段空窗"
    assert env.center.quiet.enabled is True


def test_effective_hotkey_wins_over_saved_setting_on_swap(env):
    """CLI 的 --hotkey 覆盖了设置里的值时, 换键判断要以实际注册的键为准。"""
    env.hotkey = FakeHotkey("win+shift+f5")            # 命令行覆盖后的实际键
    env.effective_hotkey = "win+shift+f5"
    env.settings.hotkey = "alt+q"                      # 库里存的还是老值
    s = env.settings.copy()
    s.hotkey = "ctrl+alt+r"
    env.apply_settings(s)
    assert env.hotkey.calls == ["ctrl+alt+r"]
    assert env.effective_hotkey == "ctrl+alt+r"


# ---------------------------------------------------------------- 面板上的快捷键
def test_dialog_hotkey_state_is_live_and_promises_no_restart(env, qapp):
    from reminder_assistant.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(env)
    assert "重启" not in dlg.lbl_hotkey_state.text()

    dlg.ed_hotkey.setText("ctrl+alt+r")
    assert "可用" in dlg.lbl_hotkey_state.text()
    assert "立即生效" in dlg.lbl_hotkey_state.text()

    dlg.ed_hotkey.setText("q")                          # 没有修饰键
    assert "修饰键" in dlg.lbl_hotkey_state.text()

    dlg.ed_hotkey.setText("")
    assert "沿用" in dlg.lbl_hotkey_state.text()
    dlg.reject()


def test_dialog_uses_edited_hotkey_when_saving(env, qapp):
    from reminder_assistant.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(env)
    dlg.ed_hotkey.setText("ctrl+alt+r")
    dlg.accept()
    assert env.settings.hotkey == "ctrl+alt+r"
    assert Settings.load(env.store).hotkey == "ctrl+alt+r"


def _spec_taken(spec: str) -> bool:
    """这个组合键现在是不是被占着 (从测试线程试注册一次)。"""
    import ctypes

    from reminder_assistant.hotkey import parse_hotkey

    mods, vk = parse_hotkey(spec)
    if ctypes.windll.user32.RegisterHotKey(None, 98, mods, vk):
        ctypes.windll.user32.UnregisterHotKey(None, 98)
        return False
    return True


@pytest.mark.skipif(os.name != "nt", reason="真实热键注册只在 Windows")
def test_apply_settings_really_swaps_a_registered_hotkey(env):
    """端到端: 真热键线程 + 走 apply_settings 换键 —— 系统里的登记确实跟着变了。

    前面的用例用替身验证"调用关系", 这一条验证"真的生效": 换完之后旧键腾出来、新键被占住。
    """
    from reminder_assistant.hotkey import GlobalHotkey, HotkeyError

    hk = GlobalHotkey("alt+shift+f11")
    try:
        hk.start()
    except HotkeyError as exc:
        pytest.skip(f"本机快捷键被占用: {exc}")
    env.hotkey, env.effective_hotkey = hk, "alt+shift+f11"
    try:
        assert _spec_taken("alt+shift+f11") and not _spec_taken("alt+shift+f12")

        s = env.settings.copy()
        s.hotkey = "alt+shift+f12"
        env.apply_settings(s)

        assert env.effective_hotkey == "alt+shift+f12"
        assert _spec_taken("alt+shift+f12"), "新键没有真的注册上"
        assert not _spec_taken("alt+shift+f11"), "旧键没有被注销"
        assert Settings.load(env.store).hotkey == "alt+shift+f12"
    finally:
        hk.stop()


def test_dialog_keeps_window_open_when_hotkey_is_taken(env, qapp, monkeypatch):
    """键被别的软件占用: 面板必须说明原因且不关窗, 还得把刚改的键退回去。"""
    from PySide6.QtWidgets import QDialog

    from reminder_assistant.ui.settings_dialog import SettingsDialog

    env.hotkey = FakeHotkey("alt+q", busy={"ctrl+alt+r"})
    env.effective_hotkey = "alt+q"
    dlg = SettingsDialog(env)
    seen: list[str] = []
    monkeypatch.setattr(dlg, "report_error", seen.append)

    dlg.ed_hotkey.setText("ctrl+alt+r")
    dlg.accept()
    assert seen and "1409" in seen[0] and "ALT+Q" in seen[0]
    assert dlg.result() != QDialog.Accepted, "换键失败时不能关窗"
    assert dlg.ed_hotkey.text() == "ctrl+alt+r", "用户改的值该留在框里, 好继续改"
    assert Settings.load(env.store).hotkey == "alt+q"
