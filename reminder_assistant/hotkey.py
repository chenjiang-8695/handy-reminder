"""全局快捷键 (Alt+Q) 与"勿扰"探测。

工程师清单第 2 条相关:
  * 全局热键  -> Win32 RegisterHotKey (不需要管理员, 不装驱动, 不hook键盘)
                注册失败 (被别的软件占了) 必须显式上报, 而不是静默失效
                换键必须**就地生效**: 同一消息循环里 Unregister + Register 即可,
                不能让用户为了改一个快捷键重启程序
  * 多屏/DPI  -> 进程声明 Per-Monitor-V2 DPI 感知, 否则圈选坐标在高 DPI 屏上会错位
  * 全屏/演示 -> 探测前台窗口是否铺满所在显示器, 命中则把通知降级为不打扰
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from typing import Callable

try:
    # 非 Windows 平台取不到 wintypes; 只有消息循环需要它, 纯解析逻辑不受影响。
    # 不做这层保护的话, 连 parse_hotkey 都没法在非 Windows 上导入/单测。
    from ctypes import wintypes
except Exception:                       # noqa: BLE001
    wintypes = None

log = logging.getLogger("reminder.hotkey")

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x0008, 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_HOTKEY_SWAP = 0x8000 + 1             # 自定义线程消息: "请换快捷键"

_MODS = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
         "shift": MOD_SHIFT, "win": MOD_WIN, "meta": MOD_WIN}


class HotkeyError(RuntimeError):
    pass


# ---------------------------------------------------------------- DPI

def enable_dpi_awareness() -> bool:
    """必须在创建任何窗口之前调用。

    不做这一步的后果: 150% 缩放的显示器上, Qt 报告的是逻辑坐标,
    Win32 截图/圈选拿到的是物理坐标, 圈出来的框和实际选中的文字会整体偏移。
    """
    if sys.platform != "win32":
        return False
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        return bool(ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
    except Exception:
        try:
            return bool(ctypes.windll.shcore.SetProcessDpiAwareness(2))
        except Exception:
            log.warning("无法声明 DPI 感知, 高缩放屏幕上圈选可能偏移", exc_info=True)
            return False


# ---------------------------------------------------------------- 全屏探测

def is_fullscreen() -> bool:
    """前台窗口是否铺满其所在显示器 (游戏/放映/全屏视频场景)。"""
    if sys.platform != "win32":
        return False
    try:
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return False
        rect = wintypes.RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        MONITOR_DEFAULTTONEAREST = 2
        hmon = u.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        info = wintypes.RECT()
        if not u.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return False
        app = (rect.left, rect.top, rect.right, rect.bottom)
        screen = (info.left, info.top, info.right, info.bottom)
        return app == screen
    except Exception:
        return False


# ---------------------------------------------------------------- 热键

def parse_hotkey(spec: str) -> tuple[int, int]:
    """'alt+q' -> (MOD_ALT, 0x51)。支持 alt/ctrl/shift/win + 单键。"""
    parts = [p.strip().lower() for p in spec.replace("+", " ").split() if p.strip()]
    if not parts:
        raise HotkeyError(f"非法快捷键: {spec!r}")
    mods = 0
    key = None
    for p in parts:
        if p in _MODS:
            mods |= _MODS[p]
        else:
            key = p
    if key is None or mods == 0:
        raise HotkeyError(f"快捷键需要至少一个修饰键: {spec!r}")
    if len(key) == 1:
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit():
        vk = 0x70 + int(key[1:]) - 1
    else:
        vk = {"space": 0x20, "enter": 0x0D, "esc": 0x1B, "tab": 0x09}.get(key)
        if vk is None:
            raise HotkeyError(f"不认识的按键: {key!r}")
    return mods | MOD_NOREPEAT, vk


# ---------------------------------------------------------------- 注册后端

def win_register(hotkey_id: int, mods: int, vk: int) -> str | None:
    """注册成功返回 None, 失败返回给人看的说明 (含 Win32 错误码)。

    做成模块级函数是为了能在测试里替换掉: "新键失败 -> 旧键注册回去"这条回滚语义
    必须能在没有 Win32 桌面的环境里被确定性验证。
    """
    if ctypes.windll.user32.RegisterHotKey(None, hotkey_id, mods, vk):
        return None
    return f"Win32 错误 {ctypes.windll.kernel32.GetLastError()} (通常是被其它软件占用了)"


def win_unregister(hotkey_id: int) -> None:
    ctypes.windll.user32.UnregisterHotKey(None, hotkey_id)


class HotkeyBinding:
    """"当前该按哪个组合键"这份状态 + 换键决策 (纯逻辑, 不碰线程/消息循环)。

    换键最容易踩的两个坑都在这里挡住:
      * 新组合键注册失败时必须把旧键注册回去。否则用户看到"保存失败"之后, 连原来的键
        都不好使了 —— 比"没改成"糟得多。
      * 成功后 spec/mods/vk 三者要一起更新, 否则下一次回滚会用错误的旧值。
    """

    def __init__(self, spec: str, register: Callable[[int, int, int], str | None],
                 unregister: Callable[[int], None], hotkey_id: int = 1):
        self.hotkey_id = hotkey_id
        self._register = register
        self._unregister = unregister
        self._mods, self._vk = parse_hotkey(spec)
        self.spec = spec

    @property
    def mods(self) -> int:
        return self._mods

    @property
    def vk(self) -> int:
        return self._vk

    def register(self) -> None:
        """把当前组合键登记到系统。失败抛 HotkeyError (带可操作提示)。"""
        err = self._register(self.hotkey_id, self._mods, self._vk)
        if err:
            raise HotkeyError(f"注册全局快捷键 {self.spec.upper()} 失败: {err}。"
                              "请在设置里换一个组合键。")

    def set_spec(self, spec: str) -> str:
        """只更新配置, 不碰系统 (还没启动时用)。非法组合照样抛 HotkeyError。"""
        mods, vk = parse_hotkey(spec)
        self.spec, self._mods, self._vk = spec, mods, vk
        return self.spec

    def swap(self, spec: str) -> str:
        """就地换成新组合键, 返回生效后的 spec。

        非法组合在动系统之前就被 parse_hotkey 挡下; 注册失败则回滚到旧键并抛错。
        也就是**要么新键生效, 要么旧键照常可用**, 不存在"两边都没了"的中间态。
        """
        if spec == self.spec:
            return self.spec
        mods, vk = parse_hotkey(spec)
        self._unregister(self.hotkey_id)
        err = self._register(self.hotkey_id, mods, vk)
        if err is None:
            self.spec, self._mods, self._vk = spec, mods, vk
            return self.spec
        back = self._register(self.hotkey_id, self._mods, self._vk)
        if back is not None:
            log.error("快捷键 %s 回滚失败: %s", self.spec, back)
            raise HotkeyError(f"换快捷键 {spec.upper()} 失败 ({err}), 而且原来的 "
                              f"{self.spec.upper()} 也注册不回去了; 请重启程序或换一个组合键。")
        raise HotkeyError(f"换快捷键 {spec.upper()} 失败: {err}。"
                          f"原来的 {self.spec.upper()} 仍然有效。")


class GlobalHotkey:
    """在独立线程里跑 Windows 消息循环, 收到 WM_HOTKEY 就回调。

    为什么不用 keyboard/pynput 之类的库: 它们要靠低级键盘钩子 (WH_KEYBOARD_LL),
    会被安全软件当成可疑行为, 且在部分游戏/远程桌面下失灵。
    RegisterHotKey 是系统级正规接口。

    换键走 PostThreadMessage + 线程内 Unregister/Register: RegisterHotKey(NULL, ...)
    登记在**调用线程**的消息队列上, 所以注销/重注册必须回到同一个线程里做。
    """

    def __init__(self, spec: str = "alt+q", callback: Callable[[], None] | None = None,
                 hotkey_id: int = 1, register: Callable | None = None,
                 unregister: Callable | None = None):
        self.callback = callback
        self.hotkey_id = hotkey_id
        self.binding = HotkeyBinding(spec, register or win_register,
                                     unregister or win_unregister, hotkey_id=hotkey_id)
        self._thread: threading.Thread | None = None
        self._tid: int | None = None
        self._ready = threading.Event()
        self._error: str | None = None
        self._swap_lock = threading.Lock()
        self._swap_pending: str | None = None
        self._swap_done = threading.Event()
        self._swap_error: str | None = None

    @property
    def spec(self) -> str:
        return self.binding.spec

    # ---- 生命周期
    def start(self) -> None:
        if sys.platform != "win32":
            raise HotkeyError("全局热键目前只实现了 Windows 后端")
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, name="hotkey", daemon=True)
        self._thread.start()
        if not self._ready.wait(3):
            raise HotkeyError("热键线程启动超时")
        if self._error:
            raise HotkeyError(self._error)

    def stop(self) -> None:
        if self._tid:
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(2)
            self._thread = None
        self._tid = None
        # 线程已经不在了, 正在等结果的换键请求不能一直挂着
        self._swap_pending = None
        self._swap_done.set()

    def change(self, spec: str, timeout: float = 3.0) -> str:
        """热切换快捷键, 不用重启。返回生效后的组合键。

        还没启动时只改配置 (由 start 负责注册)。注册失败抛 HotkeyError, 且**旧键仍然有效**;
        上层应当把异常消息显示给用户, 让 ta 换一个键。
        """
        spec = (spec or "").strip()
        parse_hotkey(spec)                  # 非法组合立刻报错, 免得白等一次线程往返
        if self._thread is None or not self._thread.is_alive():
            return self.binding.set_spec(spec)
        if spec == self.binding.spec:
            return spec
        with self._swap_lock:
            self._swap_pending = spec
            self._swap_done.clear()
            self._swap_error = None
            if not ctypes.windll.user32.PostThreadMessageW(self._tid, WM_HOTKEY_SWAP, 0, 0):
                raise HotkeyError("热键线程已退出, 无法切换快捷键; 请重启程序。")
            if not self._swap_done.wait(timeout):
                raise HotkeyError("切换快捷键超时 (热键线程没有响应)。")
            if self._swap_error:
                raise HotkeyError(self._swap_error)
        return self.binding.spec

    # ---- 线程体
    def _run(self) -> None:  # pragma: no cover - 需要 Windows 桌面会话
        u = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._tid = kernel32.GetCurrentThreadId()
        try:
            self.binding.register()
        except HotkeyError as exc:
            self._error = str(exc)
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == self.hotkey_id:
                    try:
                        if self.callback:
                            self.callback()
                    except Exception:
                        log.exception("热键回调异常")
                elif msg.message == WM_HOTKEY_SWAP:
                    self._handle_swap()
        finally:
            u.UnregisterHotKey(None, self.hotkey_id)

    def _handle_swap(self) -> None:     # pragma: no cover - 需要 Windows 桌面会话
        spec = self._swap_pending or self.binding.spec
        self._swap_pending = None
        try:
            self.binding.swap(spec)
            self._swap_error = None
            log.info("快捷键已热切换到 %s", self.binding.spec.upper())
        except HotkeyError as exc:
            self._swap_error = str(exc)
            log.warning("快捷键热切换失败: %s", exc)
        except Exception as exc:                    # noqa: BLE001 - 不能拖垮消息循环
            self._swap_error = f"切换快捷键失败: {exc}"
            log.exception("快捷键热切换异常")
        finally:
            self._swap_done.set()
