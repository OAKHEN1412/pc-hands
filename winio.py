"""Win32 plumbing for pc-hands: DPI, monitors, windows, SendInput, user-input hooks, highlight."""
import ctypes
import ctypes.wintypes as wt
import os
import threading
import time

import win32api
import win32con
import win32gui
import win32process

from logic import monitor_name

# Per-monitor v2 DPI awareness so every coordinate is a physical pixel. Must run before any window exists.
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
advapi32.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
user32.GetAncestor.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]

# ---------- processes / windows ----------


def process_name(pid: int) -> str:
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        n = wt.DWORD(520)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(h)
    return ""


def is_self_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


_SELF_ADMIN = is_self_admin()


def pid_elevated(pid: int) -> bool:
    """True if the process is elevated and we are not (UIPI would block our input)."""
    if _SELF_ADMIN or pid in (0, os.getpid()):
        return False
    h = kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return True  # access denied -> assume elevated
    try:
        tok = wt.HANDLE()
        if not advapi32.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):
            return True
        try:
            elev = wt.DWORD(0)
            ret = wt.DWORD(0)
            if advapi32.GetTokenInformation(tok, 20, ctypes.byref(elev), 4, ctypes.byref(ret)):  # TokenElevation
                return bool(elev.value)
            return False
        finally:
            kernel32.CloseHandle(tok)
    finally:
        kernel32.CloseHandle(h)


def window_info(hwnd: int) -> dict:
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    proc = process_name(pid)
    return {"hwnd": hwnd, "title": win32gui.GetWindowText(hwnd), "pid": pid, "process": proc,
            "rect": (l, t, r - l, b - t), "monitor": monitor_name((l + r) // 2),
            "elevated": pid_elevated(pid)}


def _cloaked(hwnd) -> bool:
    v = wt.DWORD(0)
    dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(v), 4)
    return bool(v.value)


def enum_windows() -> list:
    """Visible top-level app windows in Z order (top first)."""
    out = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title.strip() or title == "Program Manager":
            return True
        ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if ex & win32con.WS_EX_TOOLWINDOW and not ex & win32con.WS_EX_APPWINDOW:
            return True
        if win32gui.GetWindow(hwnd, win32con.GW_OWNER) and not ex & win32con.WS_EX_APPWINDOW:
            return True
        if _cloaked(hwnd):
            return True
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        if r - l < 20 or b - t < 20:
            return True
        out.append(window_info(hwnd))
        return True

    win32gui.EnumWindows(cb, None)
    return out


def foreground_info():
    hwnd = win32gui.GetForegroundWindow()
    return window_info(hwnd) if hwnd else None


def root_hwnd(hwnd: int) -> int:
    return user32.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT


def bring_to_front(hwnd: int) -> bool:
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    for _ in range(3):
        # a synthetic Alt tap lets SetForegroundWindow succeed from a background process
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(0.05)
        if root_hwnd(win32gui.GetForegroundWindow()) == hwnd:
            return True
    return False


def monitors() -> list:
    out = []
    for hmon, _, (l, t, r, b) in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(hmon)
        out.append({"name": monitor_name(l), "rect": (l, t, r - l, b - t), "primary": bool(info.get("Flags", 0) & 1)})
    out.sort(key=lambda m: m["rect"][0])
    return out


def virtual_screen():
    gm = win32api.GetSystemMetrics
    return gm(76), gm(77), gm(78), gm(79)  # x, y, w, h


def point_on_screen(x, y) -> bool:
    return any(m["rect"][0] <= x < m["rect"][0] + m["rect"][2] and m["rect"][1] <= y < m["rect"][1] + m["rect"][3]
               for m in monitors())

# ---------- real-user input tracking (low-level hooks) ----------


class UserInput:
    """Tracks last REAL user input. Injected events (ours or anyone's SendInput) are ignored."""
    JITTER = 8

    def __init__(self):
        self.last_input = 0.0          # time.monotonic() of last real input; 0 = never
        self.last_pos = win32api.GetCursorPos()
        self.seen = 0                  # hook callbacks seen (real + injected), for diagnostics
        self.ready = threading.Event()
        self._thread = None

    def idle_ms(self) -> int:
        if not self.last_input:
            return 10 ** 9
        return int((time.monotonic() - self.last_input) * 1000)

    def note_own_move(self, x, y):
        self.last_pos = (x, y)

    def start(self):
        self._thread = threading.Thread(target=self._run, name="input-hooks", daemon=True)
        self._thread.start()
        self.ready.wait(3)

    def _run(self):
        class MSLL(ctypes.Structure):
            _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD), ("time", wt.DWORD), ("extra", ULONG_PTR)]

        class KBLL(ctypes.Structure):
            _fields_ = [("vk", wt.DWORD), ("scan", wt.DWORD), ("flags", wt.DWORD), ("time", wt.DWORD), ("extra", ULONG_PTR)]

        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)
        user32.SetWindowsHookExW.restype = wt.HHOOK
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
        user32.CallNextHookEx.restype = LRESULT
        user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]

        def mouse_cb(n, wparam, lparam):
            try:
                if n >= 0:
                    self.seen += 1
                    s = ctypes.cast(lparam, ctypes.POINTER(MSLL)).contents
                    if not s.flags & 0x03:  # LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED
                        if wparam == 0x0200:  # WM_MOUSEMOVE
                            lx, ly = self.last_pos
                            if abs(s.pt.x - lx) >= self.JITTER or abs(s.pt.y - ly) >= self.JITTER:
                                self.last_input = time.monotonic()
                                self.last_pos = (s.pt.x, s.pt.y)
                        else:
                            self.last_input = time.monotonic()
                            self.last_pos = (s.pt.x, s.pt.y)
            except Exception:
                pass
            return user32.CallNextHookEx(None, n, wparam, lparam)

        def key_cb(n, wparam, lparam):
            try:
                if n >= 0:
                    self.seen += 1
                    s = ctypes.cast(lparam, ctypes.POINTER(KBLL)).contents
                    if not s.flags & 0x12:  # LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED
                        self.last_input = time.monotonic()
            except Exception:
                pass
            return user32.CallNextHookEx(None, n, wparam, lparam)

        self._mcb, self._kcb = HOOKPROC(mouse_cb), HOOKPROC(key_cb)  # keep refs alive
        hm = user32.SetWindowsHookExW(14, self._mcb, None, 0)
        hk = user32.SetWindowsHookExW(13, self._kcb, None, 0)
        self.ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(hm)
        user32.UnhookWindowsHookEx(hk)


class Yielded(Exception):
    pass


USER = UserInput()


class Task:
    """Per-tool-call guard: check() raises Yielded if the user touched mouse/keyboard since start."""

    def __init__(self):
        self.t0 = time.monotonic()
        USER.note_own_move(*win32api.GetCursorPos())

    def check(self):
        if USER.last_input > self.t0:
            raise Yielded()

    def sleep(self, secs):
        end = time.monotonic() + secs
        while time.monotonic() < end:
            self.check()
            time.sleep(min(0.02, max(0, end - time.monotonic())))
        self.check()

# ---------- SendInput ----------


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("extra", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("vk", wt.WORD), ("scan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("extra", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("msg", wt.DWORD), ("lo", wt.WORD), ("hi", wt.WORD)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _key(vk=0, scan=0, flags=0):
    i = INPUT(type=1)
    i.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return i


def _mouse(flags, data=0):
    i = INPUT(type=0)
    i.mi = MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0)
    return i


EXTENDED = {0x25, 0x26, 0x27, 0x28, 0x21, 0x22, 0x23, 0x24, 0x2D, 0x2E, 0x5B}


def press_vks(vks, task: Task):
    """Hold all but last as modifiers, tap the last, release in reverse."""
    def flag(vk, up):
        return (0x1 if vk in EXTENDED else 0) | (0x2 if up else 0)
    for vk in vks[:-1]:
        _send(_key(vk, 0, flag(vk, False)))
    task.check()
    last = vks[-1]
    _send(_key(last, 0, flag(last, False)), _key(last, 0, flag(last, True)))
    for vk in reversed(vks[:-1]):
        _send(_key(vk, 0, flag(vk, True)))


def type_unicode(text: str, task: Task, fast: bool = False):
    """Type via KEYEVENTF_UNICODE (works for Thai). newline -> Enter, tab -> Tab.
    fast: one SendInput per 32 chars instead of per char + pauses."""
    n = 0
    batch = [] if fast else None
    for ch in text.replace("\r\n", "\n"):
        if ch in "\n\t" and batch:
            _send(*batch)   # keep order: text typed so far must land before the Enter/Tab
            batch.clear()
        if ch == "\n":
            press_vks([0x0D], task)
        elif ch == "\t":
            press_vks([0x09], task)
        else:
            data = ch.encode("utf-16-le")
            for k in range(0, len(data), 2):
                u = int.from_bytes(data[k:k + 2], "little")
                if batch is not None:
                    batch += [_key(0, u, 0x4), _key(0, u, 0x4 | 0x2)]
                else:
                    _send(_key(0, u, 0x4), _key(0, u, 0x4 | 0x2))
        n += 1
        if batch is not None:
            if len(batch) >= 64:
                _send(*batch); batch.clear(); task.check()
        elif n % 8 == 0:
            task.check()
            time.sleep(0.005)
    if batch:
        _send(*batch)


def paste_text(text: str, task: Task):
    """Clipboard paste with restore of previous text clipboard (for long text)."""
    import win32clipboard as wc
    old = None
    try:
        wc.OpenClipboard()
        try:
            if wc.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                old = wc.GetClipboardData(win32con.CF_UNICODETEXT)
        finally:
            wc.CloseClipboard()
    except Exception:
        pass
    wc.OpenClipboard()
    try:
        wc.EmptyClipboard()
        wc.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        wc.CloseClipboard()
    try:
        press_vks([0x11, 0x56], task)
        time.sleep(0.2)
    finally:
        if old is not None:
            wc.OpenClipboard()
            try:
                wc.EmptyClipboard()
                wc.SetClipboardData(win32con.CF_UNICODETEXT, old)
            finally:
                wc.CloseClipboard()


def move_cursor(x, y):
    USER.note_own_move(x, y)
    win32api.SetCursorPos((x, y))


def mouse_click(x, y, button="left", double=False, task: Task = None):
    down, up = {"left": (0x2, 0x4), "right": (0x8, 0x10), "middle": (0x20, 0x40)}[button]
    move_cursor(x, y)
    time.sleep(0.03)
    if task:
        task.check()
    for _ in range(2 if double else 1):
        _send(_mouse(down))
        time.sleep(0.02)
        _send(_mouse(up))
        time.sleep(0.04)


def mouse_wheel_event(x, y, direction, notches, task: Task = None):
    """ONE wheel event carrying `notches` notches (Qt caps a single event at ~8 notches: callers chunk)."""
    if notches > 8:
        raise ValueError("one wheel event must carry <= 8 notches")
    move_cursor(x, y)
    time.sleep(0.03)
    if task:
        task.check()
    flag, sign = {"up": (0x800, 1), "down": (0x800, -1)}[direction]
    _send(_mouse(flag, sign * 120 * notches))


def mouse_wheel(x, y, direction, amount):
    move_cursor(x, y)
    time.sleep(0.03)
    flag, sign = {"up": (0x800, 1), "down": (0x800, -1), "right": (0x1000, 1), "left": (0x1000, -1)}[direction]
    for _ in range(amount):
        _send(_mouse(flag, sign * 120))
        time.sleep(0.02)

# ---------- highlight ----------


_HL_CLASS = "HandsHighlight"
_hl_registered = False
_hl_brush = None


def _hl_proc(hwnd, msg, wp, lp):
    if msg == 0x14:  # WM_ERASEBKGND: fill everything (clipped to the frame region) red
        win32gui.FillRect(wp, win32gui.GetClientRect(hwnd), _hl_brush)
        return 1
    return win32gui.DefWindowProc(hwnd, msg, wp, lp)


class Highlight:
    """Topmost, click-through, no-activate red frame around a rect, on its own thread."""

    def __init__(self, rect, border=4):
        self.rect, self.border = rect, border
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.th = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self.th.start()
        self.ready.wait(1)
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.th.join(1)

    def _run(self):
        global _hl_registered
        hwnd = None
        try:
            if not _hl_registered:
                wc = win32gui.WNDCLASS()
                wc.lpszClassName = _HL_CLASS
                global _hl_brush
                _hl_brush = win32gui.CreateSolidBrush(0x0000E0)  # BGR red
                wc.lpfnWndProc = _hl_proc
                wc.hInstance = win32api.GetModuleHandle(None)
                win32gui.RegisterClass(wc)
                _hl_registered = True
            x, y, w, h = self.rect
            b = self.border
            x, y, w, h = x - b, y - b, w + 2 * b, h + 2 * b
            ex = (win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE
                  | win32con.WS_EX_TRANSPARENT | win32con.WS_EX_LAYERED)
            hwnd = win32gui.CreateWindowEx(ex, _HL_CLASS, "", win32con.WS_POPUP, x, y, w, h,
                                           0, 0, win32api.GetModuleHandle(None), None)
            win32gui.SetLayeredWindowAttributes(hwnd, 0, 255, win32con.LWA_ALPHA)
            gdi = ctypes.WinDLL("gdi32")
            gdi.CreateRectRgn.restype = ctypes.c_void_p
            gdi.CombineRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
            user32.SetWindowRgn.argtypes = [wt.HWND, ctypes.c_void_p, wt.BOOL]
            outer = gdi.CreateRectRgn(0, 0, w, h)
            inner = gdi.CreateRectRgn(b, b, max(b, w - b), max(b, h - b))
            gdi.CombineRgn(outer, outer, inner, 4)  # RGN_DIFF: frame only
            user32.SetWindowRgn(hwnd, outer, True)
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, x, y, w, h, win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW)
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
        except Exception as e:
            print("highlight failed:", e)
        finally:
            self.ready.set()
        while not self.stop.is_set():
            win32gui.PumpWaitingMessages()
            time.sleep(0.01)
        if hwnd:
            try:
                win32gui.DestroyWindow(hwnd)
            except Exception:
                pass
