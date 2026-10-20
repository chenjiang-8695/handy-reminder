"""悬浮窗无头冒烟测试。

用 Qt 的 offscreen 平台插件真实构造窗口、渲染纸张、跑翻页动画 —— 不依赖桌面会话。
这能验证"窗口能建出来、能画、能翻页、能划掉", 但**不能**验证真实观感
(字体渲染效果、动画顺滑度、托盘/热键), 那部分必须人工在桌面上验收。
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="未安装 PySide6, 跳过 UI 冒烟测试")

from PySide6.QtCore import QEvent, QPoint, QPointF, QSize, Qt   # noqa: E402
from PySide6.QtGui import QMouseEvent, QPixmap                  # noqa: E402
from PySide6.QtWidgets import QApplication                 # noqa: E402

from reminder_assistant.app import App                     # noqa: E402
from reminder_assistant.clock import FakeClock             # noqa: E402
from reminder_assistant.models import Reminder, ReminderPlan  # noqa: E402
from reminder_assistant.ui import theme                    # noqa: E402
from reminder_assistant.ui.notepad import (  # noqa: E402
    COLLAPSED_H, HIST_PER_PAGE, MIN_H, MIN_W, NOTE_H, NotepadWindow)

TZ = datetime(2026, 10, 8, 10, 0).astimezone().tzinfo
NOW = datetime(2026, 10, 8, 10, 0, tzinfo=TZ)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def app_env(tmp_path, qapp):
    a = App(tmp_path)
    a.clock = FakeClock(NOW)
    a.scheduler.clock = a.clock
    for title, mins, level in (("明天下午三点开会", 300, "重要"), ("交周报", 60, "日常")):
        a.life.create(Reminder(title=title, event_at=NOW + timedelta(minutes=mins),
                               created_at=NOW, level=level, plan=ReminderPlan.preset(level)),
                      NOW)
    yield a
    a.close()


# ---------------------------------------------------------------- 纸张 / 字体
def test_paper_cache_renders_pixmap(qapp):
    cache = theme.PaperCache()
    pm = cache.get(320, 460, 1.0)
    assert isinstance(pm, QPixmap) and pm.width() == 320 and pm.height() == 460
    assert cache.get(320, 460, 1.0) is pm              # 命中缓存, 不重画


def test_handwriting_font_is_bold(qapp):
    f = theme.handwriting_font(14, bold=True)
    assert f.bold() and f.pointSize() == 14


def test_jitter_keeps_all_characters(qapp):
    out = theme.jitter("明天下午三点开会", seed=1)
    assert out.replace("\u200b", "") == "明天下午三点开会"


# ---------------------------------------------------------------- 便签自带的入口
def _press(win, x, y):
    win.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(x, y), QPointF(x, y),
                                    Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))


def _move(win, x, y):
    win.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(x, y), QPointF(x, y),
                                   Qt.NoButton, Qt.LeftButton, Qt.NoModifier))


def _release(win, x, y):
    win.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(x, y), QPointF(x, y),
                                      Qt.LeftButton, Qt.NoButton, Qt.NoModifier))


def test_notepad_has_its_own_controls(app_env, qapp, monkeypatch):
    """便签自己必须能: 添加 / 设置 / 贴边收起 / 关闭。

    实测反馈: 这些东西原来只挂在托盘右键菜单里, 而 Win11 默认把新托盘图标折进"隐藏的图标",
    用户看到的就是一块"像图片、点不动"的纸。
    """
    opened: list[str] = []
    monkeypatch.setattr(NotepadWindow, "_open_settings", lambda self: opened.append("settings"))

    win = NotepadWindow(app_env)
    win.show()
    try:
        assert [b.text() for b in win.tool_buttons] == ["＋", "⚙", "－", "×"]
        assert all(b.isVisibleTo(win) for b in win.tool_buttons)
        assert win.editor.isVisibleTo(win) and win.editor.placeholderText()
        win.btn_add.click()
        # offscreen 下窗口没有激活, hasFocus() 不稳; focusWidget 是同一件事的确定写法
        assert win.focusWidget() is win.editor, "＋ 应该把光标送进输入行"
        win.btn_settings.click()
        assert opened == ["settings"]
        win.btn_stow.click()
        assert win.is_collapsed(), "－ 按钮没有把便签贴边收起"
    finally:
        win.close()


def test_editor_creates_a_reminder_on_enter(app_env, qapp):
    """在便签上写一句话回车就建卡 —— 不用去圈选, 也不用来回切窗口。"""
    win = NotepadWindow(app_env)
    win.show()
    before = len(app_env.store.list_active())
    try:
        win.editor.setText("明天下午三点开会")
        win.editor.returnPressed.emit()
        assert len(app_env.store.list_active()) == before + 1
        assert win.editor.text() == "", "建卡成功后输入框该清空"

        win.editor.setText("随便写点没有时间的话")
        win.editor.returnPressed.emit()
        assert len(app_env.store.list_active()) == before + 1, "没识别到时间不该建卡"
        assert win.editor.text() == "随便写点没有时间的话", "失败要保留原句让用户改"
    finally:
        win.close()


def test_resize_grip_resizes_and_persists(app_env, qapp):
    from PySide6.QtWidgets import QApplication

    win = NotepadWindow(app_env)
    win.show()
    win.move(120, 120)
    try:
        assert not win._in_grip(QPoint(10, 10))
        assert win._in_grip(QPoint(win.width() - 2, win.height() - 2))

        px, py = win.width() - 2, win.height() - 2
        _press(win, px, py)
        assert win._resizing is not None, "右下角没进入拖拽调整大小状态"
        w0, h0 = win.width(), win.height()
        _move(win, px + 40, py + 30)
        _release(win, px + 40, py + 30)
        assert (win.width(), win.height()) == (w0 + 40, h0 + 30)
        assert app_env.store.get_meta("window_size") == f"{w0 + 40},{h0 + 30}"

        # 缩到最小值以下要被挡住 (避免拖成一个看不见的条)
        _press(win, win.width() - 2, win.height() - 2)
        _move(win, -500, -500)
        _release(win, -500, -500)
        assert win.width() >= MIN_W and win.height() >= MIN_H

        # 换个窗口再开: 大小要记住
        win.close()
        again = NotepadWindow(app_env)
        assert again.width() >= MIN_W and again.height() >= MIN_H
        assert (again.width(), again.height()) == (win.width(), win.height())
        again.close()
    finally:
        if QApplication.instance():
            win.close()


def test_dragging_to_the_screen_edge_stows_the_note(app_env, qapp):
    win = NotepadWindow(app_env)
    win.show()
    try:
        geo = win._screen_geo()
        # 拖到贴着右边缘的位置松手 -> 自动吸附 + 收起
        win.move(geo.right() - win.width() - 4, geo.top() + 80)
        side = win.apply_edge_snap()
        assert side == "right" and win.is_collapsed()
        assert win.x() + win.width() == geo.right() + 1
        assert app_env.store.get_meta("stowed_side") == "right"

        # 收起状态下点一下 -> 展开 (贴边隐藏之后唯一的拿法)
        _press(win, 10, 10)
        assert not win.is_collapsed()

        # 离边缘远的时候不该乱吸
        win.move(geo.left() + 120, geo.top() + 120)
        assert win.apply_edge_snap() is None
    finally:
        win.close()


def test_collapse_remembers_the_size_the_user_dragged(app_env, qapp):
    win = NotepadWindow(app_env)
    win.show()
    try:
        win.resize(400, 520)
        win.toggle_collapse()
        assert win.height() == COLLAPSED_H
        assert not win.editor.isVisibleTo(win), "收起时不该还留着输入框"
        win.toggle_collapse()
        assert (win.width(), win.height()) == (400, 520), "展开要回到用户自己拖出来的大小"
    finally:
        win.close()


def test_scan_overlay_is_kept_alive_by_the_window(app_env, qapp):
    """冻结层必须被窗口持有 —— 只写 `SelectionOverlay(app).start()` 会被 GC 掉, 窗口一闪即灭。

    这是本项目最隐蔽的一个缺陷: 按 Alt+Q 毫无反应, 没有任何异常、日志也不报,
    因为对象确实"成功创建并显示了", 只是随即被回收。
    """
    import gc

    from reminder_assistant.ui.notepad import NotepadWindow

    win = NotepadWindow(app_env)
    try:
        win._start_scan()
        assert win.overlay is not None, "圈选对象没有被窗口存住"
        assert win.overlay.isVisible(), "冻结层没有显示出来"
        gc.collect()                                  # 正是这一步以前把它收走了
        assert win.overlay is not None and win.overlay.isVisible()
        # 重复触发不该开第二层
        first = win.overlay
        win._start_scan()
        assert win.overlay is first
        win.overlay._finish(None)                     # 取消 (Esc/右键) -> 引用要被清掉
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        qapp.processEvents()                          # deleteLater 是延迟的, 要给它一轮事件循环
        assert win.overlay is None, "圈选结束后窗口还占着冻结层"
    finally:
        win.close()


def test_hotkey_callback_hops_to_the_gui_thread(app_env, qapp, monkeypatch):
    """Alt+Q 的回调跑在热键线程里, 绝不能在那里开冻结层 —— 必须排队回 GUI 线程。

    踩过: 热键线程里直接 SelectionOverlay(app).start() → 按 Alt+Q 毫无反应,
    而且一条日志都不给 (Qt 不允许工作线程建控件, 往往是静默失败)。
    """
    from reminder_assistant.ui import overlay as overlay_mod
    from reminder_assistant.ui.notepad import NotepadWindow

    made: list = []

    def fake_start(self):
        made.append(threading.current_thread())
        self.deleteLater()

    monkeypatch.setattr(overlay_mod.SelectionOverlay, "start", fake_start)
    win = NotepadWindow(app_env)
    app_env.window = win
    try:
        worker = threading.Thread(target=app_env._on_scan_hotkey, name="fake-hotkey")
        worker.start()
        worker.join()
        assert made == [], "冻结层被在工作线程里创建了 (Qt 不允许, 会静默失效)"
        qapp.processEvents()                     # 跨线程信号靠事件循环派发
        assert made and made[0] is threading.main_thread(), "动作没有回到 GUI 线程"
    finally:
        win.close()


def test_hotkey_callback_without_window_is_harmless(app_env):
    """界面还没起来时按热键, 只该记一条日志, 不能炸。"""
    app_env.window = None
    app_env._on_scan_hotkey()


def test_first_run_tells_the_user_where_the_entries_are(app_env, qapp, monkeypatch):
    notes: list[str] = []
    monkeypatch.setattr(NotepadWindow, "_notify", lambda self, text: notes.append(text))
    win = NotepadWindow(app_env)
    assert notes and "＋" in notes[0], "第一次运行要指一下便签上的入口"
    win.close()
    first = len(notes)
    NotepadWindow(app_env).close()
    assert len(notes) == first, "提示只该出现一次, 不要每次都弹"


def test_upgrade_hint_points_at_the_new_task_entries(app_env, qapp, monkeypatch):
    """老库已经有 hint_shown=1 时, 后来加的功能还得被各指一次路。

    用户反馈原话就是"该功能我没有看到" —— 功能做了但界面上一句话都没有, 等于没做。
    每加一批功能就换一个 meta key 再弹一次, 所以这里断言的是"两条引导都在, 且各只弹一次"。
    """
    app_env.store.set_meta("hint_shown", "1")          # 装作是个老用户
    notes: list[str] = []
    monkeypatch.setattr(NotepadWindow, "_notify", lambda self, text: notes.append(text))
    win = NotepadWindow(app_env)
    win.close()
    joined = "\n".join(notes)
    assert "创建子任务" in joined and "属性" in joined and "⇅" in joined, notes
    assert "账户与设备绑定" in joined, f"2.4 的绑定功能没有指路: {notes}"
    again: list[str] = []
    monkeypatch.setattr(NotepadWindow, "_notify", lambda self, text: again.append(text))
    NotepadWindow(app_env).close()
    assert again == [], "这些引导都只该出现一次"


# ---------------------------------------------------------------- 交互
def test_window_builds_and_lists_active_reminders(app_env, qapp):
    win = NotepadWindow(app_env)
    assert win.pages[0]._rows, "便签上没有渲染出任何事件行"
    titles = {r.item["title"] for r in win.pages[0]._rows.values()}
    assert titles == {"明天下午三点开会", "交周报"}
    assert len(win.pages[1]._rows) == 0                # 历史页为空
    win.close()


def test_refresh_is_diff_based(app_env, qapp):
    win = NotepadWindow(app_env)
    before = set(win.pages[0]._rows)
    win.refresh()
    assert set(win.pages[0]._rows) == before, "数据没变时不应重建行控件"
    app_env.life.create(Reminder(title="新增一条", event_at=NOW + timedelta(minutes=10),
                                 created_at=NOW, plan=ReminderPlan.preset("日常")), NOW)
    win.refresh()
    assert len(win.pages[0]._rows) == len(before) + 1
    win.close()


def test_page_flip_switches_pages(app_env, qapp):
    win = NotepadWindow(app_env)
    assert win.page_index == 0
    win.flip.mode = "fade"
    win.turn_page(1)
    assert win.page_index == 1
    win.turn_page(-1)
    assert win.page_index == 0
    win.close()


def test_flip_degrades_when_frames_are_slow(app_env, qapp):
    win = NotepadWindow(app_env)
    win.flip.mode = "flip"
    for _ in range(12):
        win.flip._frames.append(45.0)                  # 伪造 ~22fps 的帧间隔
    win.flip._tick(0.6)
    assert win.flip.mode == "fade"
    assert app_env.store.get_meta("flip_mode") == "fade"
    win.close()


def test_collapse_and_click_through(app_env, qapp):
    win = NotepadWindow(app_env)
    win.toggle_collapse()
    assert win.height() == 44 and app_env.store.get_meta("collapsed") == "1"
    on = win.toggle_click_through()
    assert on is True and win.testAttribute(Qt.WA_TransparentForMouseEvents)
    win.toggle_click_through()
    win.close()


def test_window_renders_without_error(app_env, qapp):
    win = NotepadWindow(app_env)
    win.resize(NOTE_H, NOTE_H)
    pm = win.grab()                                    # 触发真实绘制
    assert not pm.isNull() and pm.size() == QSize(NOTE_H, NOTE_H)
    win.close()


def test_overlay_maps_logical_selection_to_physical_pixels(app_env, qapp):
    """R7 的核心算法: 逻辑坐标 -> 该屏物理像素。多屏不同缩放比就靠这一步。"""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QGuiApplication

    from reminder_assistant.ocr import ScanResult
    from reminder_assistant.ui.overlay import SelectionOverlay

    seen = {}

    class FakeScanner:
        def scan_against(self, sel, active, now=None):
            seen["bbox"] = sel.bbox
            seen["active"] = list(active)
            return ScanResult(text="明天下午三点开会", parsed=None, bbox=sel.bbox)

    app_env.scanner = FakeScanner()
    ov = SelectionOverlay(app_env)
    screen = QGuiApplication.primaryScreen()
    geo, dpr = screen.geometry(), screen.devicePixelRatio()
    ov._scan(QRect(geo.x() + 10, geo.y() + 20, 100, 40))
    assert seen["bbox"] == (int(10 * dpr), int(20 * dpr), int(110 * dpr), int(60 * dpr))
    assert len(seen["active"]) == 2                     # 圈划删除要带上现存卡片


def test_overlay_uses_the_grabbed_screenshot_scale_not_dpr(app_env, qapp):
    """比例要取冻结图的**实测值** (图宽/屏逻辑宽), 不能想当然用 devicePixelRatio。

    踩过: 高亮预览用逻辑坐标去裁物理像素的截图, 于是"框里亮出来的内容"比圈的位置
    偏了 1.5 倍 —— 用户看到的就是"识别和鼠标落点错位"。这条用例把"高亮裁的那块"
    与"送去 OCR 的那块"钉成同一个矩形。
    """
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QGuiApplication, QPixmap

    from reminder_assistant.ocr import ScanResult
    from reminder_assistant.ui.overlay import SelectionOverlay

    seen = {}

    class FakeScanner:
        def scan_against(self, sel, active, now=None):
            seen["bbox"] = sel.bbox
            return ScanResult(text="", parsed=None, bbox=sel.bbox)

    app_env.scanner = FakeScanner()
    ov = SelectionOverlay(app_env)
    geo = QGuiApplication.primaryScreen().geometry()
    # 人为造一张"逻辑 1 像素 = 图上 2 像素"的冻结图 (与真实 dpr 无关)
    ov._shots.append((QRect(geo), QPixmap(geo.width() * 2, geo.height() * 2)))
    sel = QRect(geo.x() + 10, geo.y() + 20, 100, 40)

    ov._scan(sel)
    # Selection.bbox 是 (x, y, x+w, y+h) 的角点形式
    assert seen["bbox"] == (20, 40, 220, 120), f"没有按实测比例换算: {seen['bbox']}"

    pm, shot_rect, scale = ov._shot_at(sel)
    assert scale == 2.0
    highlighted = ov._sel_to_pixels(sel, shot_rect, scale)
    corners = (highlighted.x(), highlighted.y(),
               highlighted.x() + highlighted.width(), highlighted.y() + highlighted.height())
    assert corners == seen["bbox"], \
        f"高亮显示的那块 {corners} 和送去识别的那块 {seen['bbox']} 不是同一块 (正是'错位'的成因)"
    assert highlighted != sel, "高亮又退化成直接在物理图上裁逻辑坐标了"


def test_overlay_reports_engine_errors_instead_of_crashing(app_env, qapp):
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QGuiApplication

    from reminder_assistant.ui.overlay import SelectionOverlay

    class BoomScanner:
        def scan_against(self, sel, active, now=None):
            raise RuntimeError("未安装任何 OCR 后端")

    app_env.scanner = BoomScanner()
    ov = SelectionOverlay(app_env)
    geo = QGuiApplication.primaryScreen().geometry()
    res = ov._scan(QRect(geo.x(), geo.y(), 60, 30))     # 不能抛回热键回调
    assert res.text == "" and res.engine.startswith("error:")


def test_strike_out_marks_done(app_env, qapp):
    """在卡片上点"划掉": 事件应当立刻从 active 列表消失并进历史。"""
    win = NotepadWindow(app_env)
    rid = next(iter(win.pages[0]._rows))
    row = win.pages[0].row(rid)
    row.done.connect(lambda r, i: app_env.life.complete(r))
    row.done.emit(rid, 0)
    assert app_env.store.get_reminder(rid).status == "completed"
    win.refresh()
    assert rid not in win.pages[0]._rows
    win.flip.mode = "fade"                             # 不等动画: 直接翻到历史那一页
    win.turn_page(1)
    assert win.page_index == 1
    assert rid in win.pages[1]._rows                   # 翻页后能看到历史
    win.close()


def test_empty_note_does_not_spawn_stray_windows(app_env, qapp):
    """空便签不许冒出"野窗口"。

    客户截图里那块"黑色 OCR 弹窗"其实是空态标签: `QLabel(...)` 当时**没有父控件**,
    而 setVisible(True) 会把没有父控件的 QWidget 变成一扇真窗口 —— 真机上就是屏幕左上角
    一块黑窗盖住桌面 (无头用例看不见它, 所以这条要单独钉住)。
    """
    before = {w for w in qapp.topLevelWidgets() if w.isVisible()}
    win = NotepadWindow(app_env)

    win.pages[0].set_items([])                          # 空便签: 正是黑窗出现的条件
    assert win.pages[0]._empty.parent() is win.pages[0]  # 必须挂在页面上, 不能是"孤儿"
    assert win.pages[0]._empty.isVisibleTo(win)          # 提示照常显示, 只是不再是窗口

    win.refresh(force=True)
    strays = [w for w in qapp.topLevelWidgets()
              if w.isVisible() and w is not win and w not in before]
    assert not strays, [type(w).__name__ for w in strays]
    win.close()


def test_empty_hint_fits_and_wraps(app_env, qapp):
    """空态提示必须整句看得见。

    居中 + 文字比控件宽时, Qt 把两边各切一半 (实测"点右上角"和"文字"两头都不见了),
    所以这个标签必须允许换行、并且两侧留出纸的边距。
    """
    from PySide6.QtCore import QRect, Qt

    win = NotepadWindow(app_env)
    pg = win.pages[0]
    pg.set_items([])
    assert pg._empty.wordWrap(), "不换行就会被切两头"
    assert pg._empty.x() >= theme.PADDING[0], "左边要留出装订线"
    assert pg._empty.width() <= pg.width()
    fm = pg._empty.fontMetrics()
    laid = fm.boundingRect(QRect(0, 0, pg._empty.width(), 10_000), Qt.TextWordWrap,
                           pg._empty.text())
    assert laid.width() <= pg._empty.width(), (laid.width(), pg._empty.width())
    win.close()


# ---------------------------------------------------------------- 历史分页 (客户 10.15 反馈 2)
def _fill_history(a, n: int) -> list[int]:
    """造 n 条"已翻过去"的条目 (建了就划掉), 返回它们的 id。"""
    ids = []
    for i in range(n):
        rem = a.life.create(Reminder(title=f"历史条目 {i}", event_at=NOW + timedelta(minutes=10 + i),
                                     created_at=NOW, level="日常", plan=ReminderPlan.preset("日常")),
                            NOW)
        a.life.complete(rem.id, NOW)
        ids.append(rem.id)
    return ids


def test_history_gets_multiple_pages(app_env, qapp):
    """客户反馈：历史事件全堆在一页 → 现在按每页 HIST_PER_PAGE 条分成多页。"""
    _fill_history(app_env, HIST_PER_PAGE * 2 + 3)
    win = NotepadWindow(app_env)
    win.flip.mode = "fade"
    assert win._hist_pages == 3
    assert "历史3页" in win.page_label()                # 今日页上就看得见"历史有几页"
    win.turn_page(1)
    assert win.page_index == 1 and len(win.pages[1]._rows) == HIST_PER_PAGE
    first_page = set(win.pages[1]._rows)
    win.turn_page(1)
    assert win.page_index == 2 and len(win.pages[2]._rows) == HIST_PER_PAGE
    assert not (set(win.pages[2]._rows) & first_page)    # 两页不重复
    win.turn_page(1)
    assert win.page_index == 3 and len(win.pages[3]._rows) == 3
    win.close()


def test_history_paging_stops_at_the_ends(app_env, qapp):
    """翻到头不再翻: 第 0 页是今日, 最后一页之后没有内容。"""
    _fill_history(app_env, HIST_PER_PAGE + 1)
    win = NotepadWindow(app_env)
    win.flip.mode = "fade"
    assert win._hist_pages == 2
    win.turn_page(-1)
    assert win.page_index == 0                          # 今日页再往回翻没反应
    assert not win.btn_prev_page.isEnabled()
    win.turn_page(1)
    win.turn_page(1)
    assert win.page_index == 2                          # 停在最后一页
    assert not win.btn_next_page.isEnabled()
    win.close()


def test_history_page_shrinks_when_archive_is_pruned(app_env, qapp):
    """归档清理后历史变少: 别停在空页上(否则用户看到一页白纸, 以为数据没了)。"""
    _fill_history(app_env, HIST_PER_PAGE * 2)
    win = NotepadWindow(app_env)
    win.flip.mode = "fade"
    win.turn_page(1)
    win.turn_page(1)
    assert win.page_index == 2
    app_env.store.conn.execute("UPDATE reminders SET status='expired' WHERE status='completed'")
    app_env.life.archive_prune(days=0, now=NOW + timedelta(days=1))
    win.refresh()
    assert win._hist_pages == 1 and win.page_index == 1
    win.close()


def test_history_pager_is_visible_on_the_note(app_env, qapp):
    """入口必须看得见 (上一版的教训: 功能只在托盘菜单里 = 用户找不到)。"""
    win = NotepadWindow(app_env)
    assert win.page_pager.isVisibleTo(win) and win.btn_next_page.isVisibleTo(win)
    assert win.page_label() == "今日"                    # 没有历史时就是"今日"
    win.close()
