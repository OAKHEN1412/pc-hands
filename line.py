"""LINE desktop for pc-hands: open a chat, prepare a message (text / image / quote-reply), send it after a confirm.

Replaces the proof-of-concept scripts in poc/. Design rules (from the Fable review of the PoCs):
  * No geometry is hard-coded: everything is derived from the live window on every call (LineUI.snapshot()).
  * Nothing is ever typed, deleted or pasted unless LINE is the foreground window, enabled, and the focused element
    IS the target Edit (LineUI.guard). Enter is pressed only inside t_line_send, after the one-pass check, and only
    when the caller passed confirmed=true.
  * A message is identified by sender + EXACT body + time + date and an expected count; any other count is refused.
  * All world access (windows, UIA, input, clipboard, screen) goes through a backend object (Win) so the logic is
    unit-testable with a fake. Clicks and keys use winio with the Task, so user-input yield, forbidden-window and DPI
    handling stay. The tools click "ตอบกลับ" themselves through winio (NOT through tools.t_click), which is the one
    place the generic confirm gate on that name is bypassed: it is a menu item that only arms a quote, nothing is sent.
Every UI fact that could not be checked offline is marked GUESS: the live test must confirm those.
"""
import copy
import datetime
import json
import os
import struct
import subprocess
import threading
import time

from PIL import Image, ImageChops, ImageGrab, ImageOps, ImageStat

import tools
import winio
from logic import (LINE_MENU_BUTTONS, LINE_PASTE_IMAGE, LINE_PASTE_TEXT, LINE_QUOTE_PREFIXES, LINE_SEARCH_PREFIXES,
                   ANY_OTHER_SENDER, NeedsConfirm, OBJ, QUOTE_GAP_MIN, TokenStore, ToolError, confirm_needed,
                   line_assert_clickable, line_attach_dates, line_blockers, line_box_expected, line_box_text, line_check_state,
                   line_chat_row_matches, line_date_key, line_detect_locale, line_denied, line_header_name, line_is_media_body, line_media_verdict,
                   line_menu_ok, line_norm, line_norm_time, line_parse_row, line_pick_main, line_quote_fresh,
                   line_row_matches, line_row_sender, line_split_lines, line_summary, line_text_problem, line_wheel_chunks,
                   rect_center, rect_contains, rects_close)

SEARCH_PREFIXES = LINE_SEARCH_PREFIXES   # chat-list search Edit: Thai "ค้นหาข้อความและห้องแชท", English "Search for chats and messages"
CTRL_A, DEL, CTRL_V = [0x11, 0x41], [0x2E], [0x11, 0x56]
SHIFT_ENTER, ENTER, ESC = [0x10, 0x0D], [0x0D], [0x1B]
DEFAULT_PX = 91                  # px per wheel notch when it cannot be measured (measured 91 on the dev PC)
AVATAR_PX = 60                   # GUESS (from the PoCs): left part of a row holding the avatar, skipped by the bubble finder
SNAP_MAX_AGE = 1.5               # a snapshot is never reused when older than this (s), whatever happened since
REGION_TYPES = ("Pane", "Group", "Custom", "Window", "Document", "List")   # UIA containers a chat-pane region may be
POST_SEND_POLLS, POST_SEND_STEP = 30, 0.06   # post-Enter poll: 30 x 0.06 s = 1.8 s, the old 12 x 0.15 s
TOOL_BUDGET = 34.0              # server.py gives a tool 40 s; stop before it so the worker is never left running on
STORE = TokenStore(ttl=120.0)
_NOTCH = {"px": None}            # measured once per process ("per session")
_LAST_FG = {"hwnd": 0}           # last non-LINE foreground window seen at the start of a LINE tool call (hwnd only)
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")


# ---------------------------------------------------------------------------------------------------------
# image helpers (pure PIL, unit-tested with synthetic images)
# ---------------------------------------------------------------------------------------------------------

def _mean_sim(a, b, size):
    a, b = a.convert("RGB").resize(size), b.convert("RGB").resize(size)
    return 1 - sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3 / 255


def thumb_similarity(thumb, path):
    """Attach check, SECONDARY evidence: file (EXIF-rotated, centre-crop fitted to the thumbnail) vs the thumbnail."""
    with Image.open(path) as im:
        src = ImageOps.exif_transpose(im).convert("RGB")
    return _mean_sim(thumb, ImageOps.fit(src, thumb.size), (48, 24))


def crop_similarity(a, b):
    """Quote thumbnail vs a candidate bubble crop (fit the bubble to the thumbnail's size, compare at 24x24)."""
    return _mean_sim(a, ImageOps.fit(b.convert("RGB"), a.size), (24, 24))


def find_bubble_point(img, rect, avatar_px=AVATAR_PX):
    """Point (screen coords) inside the message bubble of a row: longest run of non-background pixels on several
    lines of the row, skipping the avatar column. None when no bubble-like run exists."""
    img = img.convert("RGB")
    w, h = img.size
    px = img.load()
    bg = px[min(3, w - 1), min(3, h - 1)]
    best = None
    for fy in (0.55, 0.65, 0.75, 0.85, 0.5):
        yy = min(h - 1, int(h * fy))
        runs, s = [], None
        for x in range(min(avatar_px, w), w):
            d = sum(abs(a - b) for a, b in zip(px[x, yy], bg)) > 12
            if d and s is None:
                s = x
            if not d and s is not None:
                runs.append((s, x))
                s = None
        if s is not None:
            runs.append((s, w))
        runs = [r for r in runs if r[1] - r[0] > 40]
        if runs:
            r = max(runs, key=lambda r: r[1] - r[0])
            if not best or r[1] - r[0] > best[1] - best[0]:
                best = (r[0], r[1], yy)
    return (rect[0] + (best[0] + best[1]) // 2, rect[1] + best[2]) if best else None


def block(img, bg_xy, x_range=None):
    """Densest contiguous block of non-background pixels (the picture itself)."""
    img = img.convert("RGB")
    px = img.load()
    w, h = img.size
    bg = px[bg_xy]
    x1, x2 = x_range or (0, w)
    x1, x2 = max(0, x1), min(w, x2)
    if x2 - x1 < 4 or h < 4:
        return None

    def d(x, y):
        return sum(abs(a - b) for a, b in zip(px[x, y], bg)) > 40

    def longest(vals, thr):
        best, cur = (0, -1), None
        for i, v in enumerate(vals):
            cur = ((cur[0], i) if cur else (i, i)) if v >= thr else None
            if cur and cur[1] - cur[0] > best[1] - best[0]:
                best = cur
        return best

    rd = [sum(d(x, y) for x in range(x1, x2, 2)) for y in range(h)]
    if max(rd) == 0:
        return None
    r0, r1 = longest(rd, max(rd) * 0.5)
    cd = [sum(d(x, y) for y in range(r0, r1 + 1, 2)) for x in range(x1, x2)]
    if max(cd) == 0:
        return None
    c0, c1 = longest(cd, max(cd) * 0.5)
    return img.crop((x1 + c0, r0, x1 + c1 + 1, r1 + 1)) if r1 > r0 and c1 > c0 else None


def find_thumb(img):
    """Attachment thumbnail inside the input box crop: first contiguous band of rows that differ from the background."""
    img = img.convert("RGB")
    w, h = img.size
    px = img.load()
    bg = px[max(0, w - 5), h // 2]
    W = min(w, 500)

    def diff(x, y):
        return sum(abs(a - b) for a, b in zip(px[x, y], bg)) > 30

    # every pixel, and >= 4 hits per row: narrow (tall) thumbnails stay visible, a 1-px caret does not count
    # >= 2 differing pixels: a 1-px text caret never counts; a band may skip up to 2 sparse rows (a mostly-white image
    # such as a tall comparison sheet shrinks to an ~18 px wide thumbnail with near-empty rows inside it)
    rows = {y for y in range(h) if sum(diff(x, y) for x in range(W)) >= 2}
    if not rows:
        return None
    y1 = min(rows)
    y2 = y1
    while any(y2 + k in rows for k in (1, 2, 3)):
        y2 = next(y2 + k for k in (1, 2, 3) if y2 + k in rows)
    cols = {x for x in range(W) if any(diff(x, y) for y in range(y1, y2 + 1, 3))}
    if not cols:
        return None
    x1 = min(cols)
    x2 = x1
    while x2 + 1 in cols:
        x2 += 1
    return img.crop((x1, y1, x2 + 1, y2 + 1))


# ---------------------------------------------------------------------------------------------------------
# real backend
# ---------------------------------------------------------------------------------------------------------

class Win:
    """Everything that touches Windows. Tests replace this whole object."""

    def __init__(self):
        self.u = tools.U()
        self._procs = {}

    # windows
    def line_windows(self, hidden=False):
        import win32gui
        import win32process
        out = []

        def cb(h, _):
            try:
                vis = bool(win32gui.IsWindowVisible(h))
                if not vis and not hidden:
                    return True
                cls = win32gui.GetClassName(h)
                if not cls.startswith("Qt"):
                    return True
                pid = win32process.GetWindowThreadProcessId(h)[1]
                if pid not in self._procs:
                    self._procs[pid] = winio.process_name(pid).lower()
                if self._procs[pid] != "line.exe":
                    return True
                l, t, r, b = win32gui.GetWindowRect(h)
                out.append({"hwnd": h, "title": win32gui.GetWindowText(h), "cls": cls, "rect": (l, t, r - l, b - t),
                            "enabled": bool(win32gui.IsWindowEnabled(h)), "visible": vis, "pid": pid})
            except Exception:
                pass
            return True

        win32gui.EnumWindows(cb, None)
        return out

    def check_forbidden(self, w):
        tools.check_forbidden(winio.window_info(w["hwnd"]))

    def fg_root(self):
        import win32gui
        return winio.root_hwnd(win32gui.GetForegroundWindow())

    def window_at(self, x, y):
        import win32gui
        hw = win32gui.WindowFromPoint((int(x), int(y)))
        return winio.root_hwnd(hw) if hw else 0

    def is_enabled(self, hwnd):
        import win32gui
        return bool(win32gui.IsWindowEnabled(hwnd))

    def bring_to_front(self, hwnd):
        return winio.bring_to_front(hwnd)

    def window_alive(self, hwnd):
        """The window still exists and is visible (used before handing the focus back; nothing is sent to it)."""
        import win32gui
        try:
            return bool(win32gui.IsWindow(hwnd)) and bool(win32gui.IsWindowVisible(hwnd))
        except Exception:
            return False

    # UIA
    def tree(self, hwnd):
        return self.u.walk(self.u.root_for(hwnd), max_depth=14, max_nodes=4000, budget=2.0)

    def subtree(self, el):
        """UIA walk of ONE container element (same node format as tree()). A dead element yields ([], True)."""
        return self.u.walk(el, max_depth=14, max_nodes=4000, budget=2.0)

    def rect(self, el):
        return self.u.live_rect(el)

    def name(self, el):
        return el.CurrentName or ""

    def text(self, el):
        return self.u.element_text(el)

    def set_focus(self, el):
        el.SetFocus()

    def focused_matches(self, el):
        """The focused UIA element is `el` (runtime-id compare, or identical bounding rect)."""
        try:
            fe = self.u.IUIA_.GetFocusedElement()
        except Exception:
            return False
        try:
            if self.u.IUIA_.CompareElements(fe, el):
                return True
        except Exception:
            pass
        try:
            return tuple(self.u.live_rect(fe)) == tuple(self.u.live_rect(el))
        except Exception:
            return False

    def element_name_at(self, x, y):
        try:
            return self.u.element_at(int(x), int(y)).CurrentName or ""
        except Exception:
            return ""

    # input (all through winio: yield, DPI)
    def click(self, x, y, button, task, pid=None):
        tools._check_point(int(x), int(y), pid)    # on screen, not a forbidden window, not covered by another process
        winio.mouse_click(int(x), int(y), button, False, task)
        time.sleep(0.05)

    def wheel_event(self, x, y, direction, notches, task):
        winio.mouse_wheel_event(int(x), int(y), direction, notches, task)

    def press(self, vks, task):
        winio.press_vks(vks, task)

    def type_line(self, text, task):
        winio.type_unicode(text, task, fast=tools._FAST)

    def nap(self, secs):
        time.sleep(secs)    # plain sleep: read-only waits after an irreversible step must not yield

    def cursor(self):
        import win32api
        return win32api.GetCursorPos()

    def move_cursor(self, x, y):
        winio.move_cursor(x, y)

    # screen / clipboard
    def grab(self, bbox):
        x, y, w, h = bbox
        return ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).convert("RGB")

    @staticmethod
    def _with_clipboard(fn, tries=40):
        """Run fn() with the clipboard open. Right after a paste LINE itself holds the clipboard for a moment,
        so open/close can fail transiently (seen live: CloseClipboard 1418). Retry; close only what we opened."""
        import win32clipboard as wc
        last = None
        for _ in range(tries):
            opened = False
            try:
                wc.OpenClipboard()
                opened = True
                return fn(wc)
            except Exception as e:      # pywintypes.error: busy / not open
                last = e
            finally:
                if opened:
                    try:
                        wc.CloseClipboard()
                    except Exception:
                        pass
            time.sleep(0.025)
        raise ToolError(f"ใช้ clipboard ไม่ได้ (โปรแกรมอื่นถืออยู่): {last}")

    def clip_set_files(self, paths):
        data = struct.pack("IiiII", 20, 0, 0, 0, 1) + ("\0".join(paths) + "\0\0").encode("utf-16-le")

        def put(wc):
            wc.EmptyClipboard()
            wc.SetClipboardData(wc.CF_HDROP, data)
        self._with_clipboard(put)

    def clip_get_files(self):
        return self._with_clipboard(
            lambda wc: list(wc.GetClipboardData(wc.CF_HDROP)) if wc.IsClipboardFormatAvailable(wc.CF_HDROP) else [])


# ---------------------------------------------------------------------------------------------------------
# state discovery
# ---------------------------------------------------------------------------------------------------------

class Snap:
    """One read of the live LINE window. Cached rects are as of the read; use be.rect(el) for live values."""
    main = nodes = truncated = enabled = None
    locale = None                    # "th" | "en" | None (see logic.line_detect_locale)
    edit = search = list_node = header_node = None
    header = None
    row_x = None
    rows = ()
    top = bottom = 0
    wheel_pt = (0, 0)

    @property
    def msgs(self):
        return [r for r in self.rows if r.kind in ("msg", "reply")]

    def visible(self, row):
        return row.y >= self.top and row.y + row.h <= self.bottom


class _Be:
    """Transparent proxy around the backend: every INPUT action (click, key, typing, wheel, focus/foreground change,
    cursor move) bumps LineUI's generation counter, so a snapshot taken before it can never be reused after it."""
    INPUT = frozenset(("click", "press", "type_line", "wheel_event", "bring_to_front", "set_focus", "move_cursor"))

    def __init__(self, be, on_input):
        object.__setattr__(self, "_be", be)
        object.__setattr__(self, "_on_input", on_input)

    def __getattr__(self, name):
        v = getattr(self._be, name)
        if name in self.INPUT and callable(v):
            def acted(*a, **k):
                try:
                    return v(*a, **k)
                finally:
                    self._on_input()          # also when it raised half-way: the screen may have changed
            return acted
        return v

    def __setattr__(self, name, value):
        setattr(self._be, name, value)


class LineUI:
    def __init__(self, be, task, today=None, budget=TOOL_BUDGET, clock=time.monotonic):
        self._gen = 0                              # +1 on every input action (see _Be)
        self._cache = None                         # last snapshot + (gen, time, foreground) it was taken at
        self._region = None                        # chat-pane container learnt from a full walk (this call only)
        self.be, self.task = _Be(be, self._bump), task
        self.today = today or datetime.date.today()
        self.clock = clock
        self.t_end = clock() + budget

    # ---- plumbing ----
    def _bump(self):
        self._gen += 1

    def invalidate(self):
        """Forget the cached snapshot (something outside our own input may have changed the screen)."""
        self._cache = None

    def _tries(self, n, secs=0.05):
        """Poll helper: yields 0..n, sleeping `secs` BEFORE every attempt but the first (so a condition that already
        holds costs no sleep). n sleeps at most: the same worst-case wait as the old sleep-then-check loops."""
        for i in range(n + 1):
            if i:
                self.sleep(secs)
            yield i

    def _poll(self, cond, secs, step=0.02):
        """True as soon as cond() holds, checking before the first sleep; False after ~`secs` of sleeping."""
        n = max(1, int(round(secs / step)))
        for i in range(n + 1):
            if cond():
                return True
            if i < n:
                self.sleep(step)
        return False

    def _tick(self):
        self.task.check()
        if self.clock() > self.t_end:
            raise ToolError("หมดเวลาของเครื่องมือ (34 วิ) หยุดแล้ว ยังไม่ได้ส่งอะไร")

    def sleep(self, secs):
        self.task.sleep(secs)

    # ---- discovery ----
    def main(self, need_enabled=True):
        wins = self.be.line_windows()
        main = line_pick_main(wins)
        self.be.check_forbidden(main)
        main = dict(main, enabled=self.be.is_enabled(main["hwnd"]))
        if need_enabled and not main["enabled"]:
            raise ToolError(self._blocked_msg(main))
        return main

    def _blocked_msg(self, main):
        parts = []
        for w in sorted(line_blockers(self.be.line_windows(hidden=True), main), key=lambda w: w["title"] == "LINE")[:3]:
            txt = ""
            try:
                nodes, _ = self.be.tree(w["hwnd"])
                txt = " / ".join(n.name for n in nodes if n.ctype == "Text" and n.name)[:80]
            except Exception:
                pass
            parts.append(f'"{w["title"] or w["cls"]}"' + ("" if w.get("visible", True) else " [ซ่อน]")
                         + (f" ข้อความ: {txt}" if txt else ""))
        return ("หน้าต่างหลักของ LINE ถูกปิดใช้งาน เพราะมี dialog ค้างอยู่: " + ("; ".join(parts) or "(หาไม่เจอ)")
                + " — ให้ปิด dialog นั้นก่อน (เครื่องมือไม่ปิดให้)")

    def snapshot(self, need_enabled=True, reuse=False, main=None):
        """One read of the LINE window. reuse=True returns the previous snapshot instead of walking again, but ONLY
        when no input action (click/key/typing/wheel/focus or foreground change) happened since it was taken, the
        foreground and the window rect are unchanged and it is younger than SNAP_MAX_AGE. The window itself
        (exists / not forbidden / enabled) is always re-read, so a dialog that appeared since is still refused."""
        self._tick()
        if main is None:
            main = self.main(need_enabled)
        elif need_enabled and not main["enabled"]:
            raise ToolError(self._blocked_msg(main))
        if reuse:
            s = self._reusable(main)
            if s is not None:
                return s
        nodes, trunc = self.be.tree(main["hwnd"])
        s = self._build(main, nodes, trunc)
        self._remember(s)
        return s

    def _fg(self):
        try:
            return self.be.fg_root()
        except Exception:
            return None

    def _remember(self, s):
        self._cache = {"snap": s, "gen": self._gen, "t": self.clock(), "fg": self._fg()}

    def _reusable(self, main):
        c = self._cache
        if not c or c["gen"] != self._gen or self.clock() - c["t"] > SNAP_MAX_AGE:
            return None
        s = c["snap"]
        if s.main["hwnd"] != main["hwnd"] or tuple(s.main["rect"]) != tuple(main["rect"]):
            return None
        fg = self._fg()
        if fg is None or fg != c["fg"]:
            return None
        r = copy.copy(s)
        r.main, r.enabled = main, main["enabled"]
        return r

    @staticmethod
    def _covers(outer, inner, slack=2):
        return (outer[0] - slack <= inner[0] and outer[1] - slack <= inner[1]
                and outer[0] + outer[2] + slack >= inner[0] + inner[2]
                and outer[1] + outer[3] + slack >= inner[1] + inner[3])

    def _learn_region(self, snap):
        """Remember the smallest UIA container (not the whole window) that covers the input box, the message list and
        the header: later reads of THIS tool call may walk only it. A geometric guess: every use is validated (see
        _post_snapshot) and falls back to a full walk."""
        try:
            if snap.edit is None or snap.list_node is None or snap.header_node is None:
                return
            need = (snap.edit.rect, snap.list_node.rect, snap.header_node.rect)
            mw, mh = snap.main["rect"][2:]
            best = None
            for n in snap.nodes:
                if n.ctype in REGION_TYPES and n.rect[2] * n.rect[3] < mw * mh * 0.9 \
                        and all(self._covers(n.rect, r) for r in need):
                    if best is None or n.rect[2] * n.rect[3] < best.rect[2] * best.rect[3]:
                        best = n
            self._region = {"el": best.el, "rect": tuple(best.rect)} if best is not None else None
        except Exception:
            self._region = None

    def _post_snapshot(self):
        """Snapshot for the post-Enter poll: walk only the cached chat-pane container when it still yields the input
        box and message rows; otherwise (stale element, nothing found, any error) one full walk."""
        r = self._region
        if r is not None:
            try:
                self._tick()
                main = self.main(False)
                nodes, trunc = self.be.subtree(r["el"])
                if not trunc and nodes:
                    s = self._build(main, nodes, trunc)
                    if s.edit is not None and s.msgs:
                        return s
            except Exception:
                pass
            self._region = None
        return self.snapshot(need_enabled=False)

    def _build(self, main, nodes, trunc):
        s = Snap()
        s.main, s.nodes, s.truncated, s.enabled = main, nodes, trunc, main["enabled"]
        mx, my, mw, mh = main["rect"]
        srch = [n for n in nodes if n.ctype == "Edit" and n.name.startswith(SEARCH_PREFIXES)]
        s.search = min(srch, key=lambda n: n.rect[1]) if srch else None
        edits = [n for n in nodes if n.ctype == "Edit"]
        low = max(edits, key=lambda n: n.rect[1], default=None)      # lowest Edit = message input
        if low is not None and low.rect[1] < my + mh * 0.5:
            low = None
        s.edit = low
        s.locale = line_detect_locale(s.search.name if s.search else "", low.name if low else "")
        if low is None:
            s.top, s.bottom = my, my + mh
            return s
        # chat pane x: row Text elements sit ~10 px left of the input Edit (+-4)   [GUESS: 10 px]
        s.row_x = low.rect[0] - 10
        texts = [n for n in nodes if n.ctype == "Text" and n.name]
        s.rows = line_attach_dates([r for r in (self._row(n) for n in texts if abs(n.rect[0] - s.row_x) <= 4)], self.today)
        # message list container: the List holding the rows      [GUESS: UIA exposes it as a "List"]
        lists = []
        for n in nodes:
            if n.ctype == "List":
                inside = sum(1 for r in s.rows if rect_contains(n.rect, rect_center(r.rect)))
                if inside:
                    lists.append((inside, -n.rect[2] * n.rect[3], n))
        s.list_node = max(lists, key=lambda t: t[:2])[2] if lists else None
        # header Button: leftmost named Button right of the chat-list column, in the top quarter of the window  [GUESS]
        hdr = [n for n in nodes if n.ctype == "Button" and n.name and n.rect[0] >= s.row_x - 4
               and n.rect[1] < my + mh * 0.25 and n.rect[1] + n.rect[3] <= (s.list_node.rect[1] + 4 if s.list_node
                                                                          else my + mh * 0.25)]
        if not hdr:   # list container may start above the header: fall back to the top-quarter rule alone
            hdr = [n for n in nodes if n.ctype == "Button" and n.name and n.rect[0] >= s.row_x - 4
                   and n.rect[1] < my + mh * 0.25]
        s.header_node = min(hdr, key=lambda n: (n.rect[0], -n.rect[2])) if hdr else None
        s.header = line_header_name(s.header_node.name) if s.header_node else None
        # band
        hb = s.header_node.rect[1] + s.header_node.rect[3] if s.header_node else my + int(mh * 0.15)
        top0 = max(s.list_node.rect[1], hb) if s.list_node else hb + 4
        pinned = [n for n in texts if n.rect[0] > s.row_x + 4 and n.rect[0] < low.rect[0] + low.rect[2]
                  and top0 - 5 <= n.rect[1] <= top0 + 150]
        s.top = max([top0] + [n.rect[1] + n.rect[3] + 15 for n in pinned])    # pinned announcement overlays the top
        s.bottom = (s.list_node.rect[1] + s.list_node.rect[3]) if s.list_node else low.rect[1] - 8
        s.wheel_pt = ((s.row_x + low.rect[0] + low.rect[2]) // 2, (s.top + s.bottom) // 2)
        return s

    @staticmethod
    def _row(n):
        r = line_parse_row(n.name)
        r.rect, r.node = tuple(n.rect), n
        return r

    # ---- guards ----
    def guard(self, snap, target=None, what="กล่องพิมพ์"):
        """Raise unless LINE is foreground, enabled and `target` (default: the input Edit) is the focused element.
        Called right before EVERY Ctrl+A / Delete / Ctrl+V / typing."""
        t = target or snap.edit
        if t is None:
            raise ToolError("ไม่พบกล่องพิมพ์ใน LINE (เปิดแชทอยู่ไหม) ไม่พิมพ์")
        hw = snap.main["hwnd"]
        if self.be.fg_root() != hw:
            raise ToolError("LINE ไม่ได้อยู่หน้าสุด ไม่พิมพ์")
        if not self.be.is_enabled(hw):
            raise ToolError("หน้าต่าง LINE ถูกปิดใช้งาน (มี dialog ค้าง) ไม่พิมพ์")
        if not self.be.focused_matches(t.el):
            raise ToolError(f"โฟกัสไม่ได้อยู่ที่{what} ไม่พิมพ์")

    def _to_front(self, hw):
        """LINE is the foreground window afterwards, or ToolError. No-op when it already is."""
        if self.be.fg_root() == hw:
            return
        if not self.be.bring_to_front(hw):
            raise ToolError("สลับไปหน้าต่าง LINE ไม่สำเร็จ")
        self._poll(lambda: self.be.fg_root() == hw, 0.1)      # was a fixed 0.05 s nap

    def focus(self, snap, target=None, what="กล่องพิมพ์"):
        """Make `t` the focused element of the foreground LINE window. Nothing is done (no foreground change, no
        SetFocus) when LINE is already foreground, enabled and `t` already has the focus: that is exactly what guard()
        demands, so skipping cannot hide a bad state. Otherwise the old actions run, but wait on the real condition
        (foreground / focused) instead of fixed naps, then guard() decides."""
        t = target or snap.edit
        if t is None:
            raise ToolError("ไม่พบกล่องพิมพ์ใน LINE (เปิดแชทอยู่ไหม)")
        hw = snap.main["hwnd"]
        try:
            if self.be.fg_root() == hw and self.be.is_enabled(hw) and self.be.focused_matches(t.el):
                return
        except Exception:
            pass
        self._to_front(hw)
        for _ in range(3):
            try:
                self.be.set_focus(t.el)
            except Exception:
                pass
            if self._poll(lambda: self.be.focused_matches(t.el), 0.1):
                break
        self.guard(snap, t, what)

    # ---- box ----
    def box_text(self, snap):
        return line_box_text(self.be.text(snap.edit.el), snap.edit.name)

    def clear_box(self, snap):
        if self.box_text(snap) == "":
            return
        self.focus(snap)
        self.guard(snap)
        self.be.press(CTRL_A, self.task)
        self.guard(snap)
        self.be.press(DEL, self.task)
        for _ in self._tries(12):
            if self.box_text(snap) == "":
                return
        raise ToolError("ล้างกล่องข้อความไม่หมด")

    def type_message(self, snap, text, expect=None):
        """Multi-line text: Shift+Enter between lines, never an Enter/'\\n' (that would send).
        Afterwards wait (instead of a fixed 0.2 s / 0.05 s nap) until the box reads `expect` (what the one-pass check
        will demand); when it does not within the same time, carry on: observe() reports the mismatch as before."""
        for i, ln in enumerate(line_split_lines(text)):
            if i:
                self.guard(snap)
                self.be.press(SHIFT_ENTER, self.task)
            if ln:
                assert "\n" not in ln and "\t" not in ln
                self.guard(snap)
                self.be.type_line(ln, self.task)
        settle = 0.05 if tools._FAST else 0.2
        if expect is None:
            self.be.nap(settle)
            return
        t_end = self.clock() + settle
        while True:
            try:
                if self.box_text(snap) == expect:
                    return
            except Exception:
                pass
            if self.clock() >= t_end:
                return
            self.be.nap(0.01)

    # ---- wheel ----
    def scroll(self, snap, direction, notches):
        x, y = snap.wheel_pt
        hw = snap.main["hwnd"]
        if self.be.window_at(x, y) != hw:
            # another window (e.g. an Explorer window, seen live) covers the list: bring LINE forward once, re-check
            if self.be.fg_root() != hw:
                self.be.bring_to_front(hw)
                self.sleep(0.1)
            if self.be.window_at(x, y) != hw:
                raise ToolError("จุดเลื่อนไม่ได้อยู่บนหน้าต่าง LINE (มีหน้าต่างอื่นบัง)")
        for k in line_wheel_chunks(notches, 8):
            self.be.wheel_event(x, y, direction, k, self.task)
        self.sleep(0.08)

    def measure_px(self, snap):
        """px per wheel notch, measured once per process: wheel ONE notch, diff a unique row's y.
        Returns (px, scrolled): scrolled=True means the view moved, so the caller must re-read before using row y's."""
        if _NOTCH["px"]:
            return _NOTCH["px"], False
        ref = [r for r in snap.msgs if snap.visible(r) and sum(1 for q in snap.msgs if q.name == r.name) == 1]
        if not ref:
            return DEFAULT_PX, False
        r0, scrolled = ref[0], False
        for direction in ("up", "down"):
            self.scroll(snap, direction, 1)
            scrolled = True
            snap = self.snapshot()
            m = [r for r in snap.msgs if r.name == r0.name]
            if len(m) == 1 and 20 <= abs(m[0].y - r0.y) <= 250:
                _NOTCH["px"] = abs(m[0].y - r0.y)
                return _NOTCH["px"], True
            if len(m) == 1 and m[0].y != r0.y:
                break                       # moved by an implausible amount: do not trust it
        return DEFAULT_PX, scrolled

    # ---- locate ----
    def _matches(self, snap, sender, text, tm, dkey):
        return sorted((r for r in snap.msgs if r.date_key == dkey and line_row_matches(r, sender, text, tm)),
                      key=lambda r: r.y)

    def locate(self, sender, text, time, date, index=None, expected_count=1, show=False):
        """-> (snap, row, all_matches). EXACT body + time + date; refuses unless the count == expected_count.
        Scans the tree first (it already holds loaded history with real y); wheels up only to load older history."""
        if not text or not time or not date:
            raise ToolError("ต้องระบุ text, time และ date ของข้อความที่จะหา")
        dkey = line_date_key(date, self.today)
        if dkey is None:
            raise ToolError(f'date "{date}" ไม่ใช่วันที่แบบ "16 พฤศจิกายน 2026" / "November 19, 2026" / 2026-11-19 / วันนี้ / Today / เมื่อวาน / Yesterday')
        expected_count = int(expected_count or 1)
        if expected_count < 1:
            raise ToolError("expected_count ต้อง >= 1")
        if expected_count > 1 and index is None:
            raise ToolError("ข้อความซ้ำหลายแถว ต้องระบุ index (1 = บนสุด)")
        idx = int(index or 1)
        if not 1 <= idx <= expected_count:
            raise ToolError(f"index ต้องอยู่ระหว่าง 1 ถึง {expected_count}")
        tm = line_norm_time(time)
        if not tm:
            raise ToolError(f'time "{time}" ไม่ใช่รูปแบบ 13.18 / 13:18 / 1:18 PM')
        snap, ms = self._load_matches(sender, text, tm, dkey, expected_count)
        if not show:
            return snap, ms[idx - 1], ms
        for _ in range(14):
            row = ms[idx - 1]
            if snap.visible(row):
                return snap, row, ms
            px, scrolled = self.measure_px(snap)
            if scrolled:                    # measuring moved the view: re-read before computing the jump
                snap = self.snapshot()
                ms = self._matches(snap, sender, text, tm, dkey)
                if len(ms) != expected_count:
                    raise ToolError(f"หลังเลื่อน พบ {len(ms)} แถวที่ตรง ต้องการ {expected_count} ไม่ทำต่อ")
                continue
            k = round(((snap.top + snap.bottom) // 2 - (row.y + row.h // 2)) / px)
            if k == 0:
                k = 1 if row.y < snap.top else -1
            self.scroll(snap, "up" if k > 0 else "down", abs(k))     # chunked 8-notch events, then re-read + nudge
            snap = self.snapshot()
            ms = self._matches(snap, sender, text, tm, dkey)
            if len(ms) != expected_count:
                raise ToolError(f"หลังเลื่อน พบ {len(ms)} แถวที่ตรง ต้องการ {expected_count} ไม่ทำต่อ")
        raise ToolError("เลื่อนข้อความเข้าจอไม่สำเร็จ")

    def _load_matches(self, sender, text, tm, dkey, expected_count):
        stalled, last_top = 0, None
        for k in range(40):
            snap = self.snapshot(reuse=(k == 0))      # first read may reuse: nothing was input since the last one
            if snap.edit is None:
                raise ToolError("ยังไม่ได้เปิดแชท")
            if snap.truncated:
                raise ToolError("อ่านต้นไม้ UIA ของ LINE ไม่ครบ (ถูกตัด) จึงนับข้อความซ้ำไม่ได้ ไม่ทำต่อ")
            ms = self._matches(snap, sender, text, tm, dkey)
            keys = [r.date_key for r in snap.msgs if isinstance(r.date_key, tuple)]
            passed = bool(keys) and min(keys) < dkey            # history already reaches back before the target day
            # a duplicate of the same minute may hide just above the loaded edge: need a different row above the first match
            guarded = bool(ms) and any(r.y < ms[0].y and (r.date_key, r.time) != (dkey, tm) for r in snap.msgs)
            if len(ms) >= expected_count and (guarded or stalled >= 2):
                break
            if passed and len(ms) <= expected_count and (guarded or not ms):
                break
            top = min((r.y for r in snap.rows), default=None)
            stalled = stalled + 1 if top == last_top else 0
            last_top = top
            if stalled >= 3:
                break
            self.scroll(snap, "up", 24)
        if len(ms) != expected_count:
            raise ToolError(f"พบ {len(ms)} แถวที่ตรงทุกตัวอักษร (ต้องการ {expected_count}) ไม่ทำต่อ"
                            + (" — ข้อความอาจอยู่ไกลเกินประวัติที่โหลด" if not ms else ""))
        return snap, ms

    # ---- bubble + context menu ----
    def bubble_point(self, snap, row):
        lr = self.be.rect(row.node.el)
        img = self.be.grab(lr)
        pt = find_bubble_point(img, lr)
        if pt is None:
            raise ToolError("หา bubble ของข้อความในแถวไม่เจอ ไม่คลิก")
        if not (rect_contains(lr, pt) and snap.top <= pt[1] <= snap.bottom and rect_contains(snap.main["rect"], pt)):
            raise ToolError("จุดคลิกอยู่นอกแถว/นอกพื้นที่ข้อความ ไม่คลิก")
        return pt, lr

    def _new_popups(self, before):
        return [w for w in self.be.line_windows() if w["hwnd"] not in before and w.get("visible", True)]

    def menu_click(self, snap, row, want=LINE_MENU_BUTTONS):
        """Right-click the bubble of `row`, validate the context menu, click `want` (one name or any of several: the
        Thai and English names of Reply). Escape + raise otherwise."""
        wants = (want,) if isinstance(want, str) else tuple(want)
        pt, lr = self.bubble_point(snap, row)
        lr2 = self.be.rect(row.node.el)
        if not rects_close(lr, lr2, 3):
            raise ToolError("แถวข้อความเลื่อนระหว่างเตรียมคลิก ไม่คลิก")
        before = {w["hwnd"] for w in self.be.line_windows()}
        self.be.click(pt[0], pt[1], "right", self.task, snap.main["pid"])
        found = why = None
        for _ in range(50):
            for w in self._new_popups(before):
                nodes, _t = self.be.tree(w["hwnd"])
                btns = [n for n in nodes if n.ctype == "Button"]
                ok, why = line_menu_ok(w["rect"], pt, [b.name for b in btns])
                if ok:
                    found = (w, btns)
                    break
            if found:
                break
            self.sleep(0.03)
        if not found:
            self.be.press(ESC, self.task)
            raise ToolError("ไม่เจอเมนูคลิกขวาของข้อความ" + (f" ({why})" if why else "") + " กด Escape แล้ว ไม่ได้ทำอะไร")
        win, btns = found
        for w in wants:
            line_assert_clickable(w)
        item = next((b for b in btns if line_norm(b.name) in wants), None)
        if item is None:
            self.be.press(ESC, self.task)
            raise ToolError(f'เมนูไม่มีปุ่ม "{" / ".join(wants)}"')
        line_assert_clickable(item.name)
        r = self.be.rect(item.el)                     # re-read right before clicking
        c = rect_center(r)
        if not rect_contains(win["rect"], c):
            self.be.press(ESC, self.task)
            raise ToolError("ตำแหน่งปุ่มเมนูอยู่นอกหน้าต่างเมนู")
        for b in btns:                                # never land on a destructive item, whatever its name reads
            if line_denied(b.name) and rect_contains(self.be.rect(b.el), c):
                self.be.press(ESC, self.task)
                raise ToolError(f'จุดคลิกทับปุ่ม "{b.name}" หยุดแล้ว')
        self.be.click(c[0], c[1], "left", self.task, snap.main["pid"])
        return pt

    # ---- quote ----
    def quote_group(self, snap):
        g = [n for n in snap.nodes if n.ctype == "Group" and n.name.startswith(LINE_QUOTE_PREFIXES)]
        if not g or snap.edit is None:
            return None
        return min(g, key=lambda n: abs(n.rect[1] + n.rect[3] - snap.edit.rect[1]))

    def quote_armed(self, snap):
        """Is a quote bar showing? Read from LIVE geometry (never the stale node name): the bar sits between the
        message list and the input, so list.bottom -> input.top opens a gap.
        GUESS: gap >= QUOTE_GAP_MIN (30 px) means armed. Fallback without a list node: hit-test the Group's centre.
        If the two signals disagree the answer is 'armed' (the safe side: a plain send is then refused)."""
        geom = hit = None
        if snap.list_node is not None and snap.edit is not None:
            lr, er = self.be.rect(snap.list_node.el), self.be.rect(snap.edit.el)
            geom = (er[1] - (lr[1] + lr[3])) >= QUOTE_GAP_MIN
        g = self.quote_group(snap)
        if g is not None:
            try:
                gr = self.be.rect(g.el)
                if gr[2] > 0 and gr[3] > 0:
                    c = rect_center(gr)
                    nm = self.be.element_name_at(c[0], c[1])
                    # an own reply ROW also starts with the prefix: only the bar itself carries the Group's exact name
                    hit = nm.startswith(LINE_QUOTE_PREFIXES) and nm == self.be.name(g.el)
            except Exception:
                hit = None
        if geom is None and hit is None:
            if g is not None:
                raise ToolError("ตรวจสถานะ quote ไม่ได้ (ไม่มี list ให้วัดช่องว่างและ hit-test ไม่ได้) ไม่เดา")
            return False
        return bool(geom) or bool(hit)

    def quote_name(self, snap):
        g = self.quote_group(snap)
        return self.be.name(g.el) if g is not None else ""

    def close_quote(self, snap):
        g = self.quote_group(snap)
        if g is None:
            raise ToolError("มี quote ค้างแต่หา Group ของแถบ quote ไม่เจอ ปิดไม่ได้")
        gr = self.be.rect(g.el)                       # live, not the cached rect
        c = (gr[0] + gr[2] - 22, gr[1] + 15)          # the "x" has no UIA node: Group right-22, top+15
        if not (rect_contains(gr, c) and rect_contains(snap.main["rect"], c)):
            raise ToolError("ตำแหน่งปุ่มปิด quote อยู่นอกแถบ ไม่คลิก")
        if snap.edit is not None and not (gr[1] + gr[3] <= snap.edit.rect[1] + 6):
            raise ToolError("แถบ quote อยู่ผิดที่ (ไม่ได้อยู่เหนือกล่องพิมพ์) ไม่คลิก")
        self.be.click(c[0], c[1], "left", self.task, snap.main["pid"])
        for _ in self._tries(40):
            s2 = self.snapshot()          # the click above bumped the generation: always a fresh walk
            if not self.quote_armed(s2):
                return s2
        raise ToolError("ปิด quote ไม่สำเร็จ")

    def quote_thumb(self, snap):
        g = self.quote_group(snap)
        if g is None:
            raise ToolError("ไม่พบแถบ quote")
        x, y, w, h = self.be.rect(g.el)
        if w < 340 or h < 12:
            raise ToolError("แถบ quote เล็กเกินไปที่จะอ่านภาพย่อ")
        im = self.be.grab((x, y, w, h)).crop((0, 3, w, h - 3))   # skip the separator line on the bar's edge
        th = block(im, (w - 300, (h - 6) // 2), (w - 140, w - 34))  # thumbnail sits left of the close "x" (PoC offsets)
        if th is None:
            raise ToolError("อ่านภาพย่อใน quote ไม่ได้")
        return th

    def bubble_img(self, row):
        lr = self.be.rect(row.node.el)
        im = block(self.be.grab(lr), (3, 3), (AVATAR_PX, lr[2]))
        if im is None:
            raise ToolError("ตัดภาพ bubble ของรูปไม่ได้")
        return im

    def arm_quote(self, rt):
        """Quote-reply to the message `rt`. -> (quote_expectation, extra evidence). Raises (closing the bar) on any doubt."""
        sender, body = rt.get("sender"), rt.get("text")
        if not sender:
            raise ToolError("reply_to ต้องมี sender (ไม่รองรับการ quote ข้อความของตัวเอง)")
        media = bool(rt["media"]) if rt.get("media") is not None else line_is_media_body(body)
        count, index = int(rt.get("expected_count") or 1), rt.get("index")
        snap = self.snapshot(reuse=True)
        armed_before = self.quote_armed(snap)
        if armed_before:
            snap = self.close_quote(snap)
        cands = {}
        if media:     # capture EVERY identical bubble first, so the quote thumbnail can be told apart afterwards
            for k in range(1, count + 1):
                s, row, _ = self.locate(sender, body, rt["time"], rt["date"], k if count > 1 else None, count, show=True)
                cands[k] = self.bubble_img(row)
        snap, row, _ = self.locate(sender, body, rt["time"], rt["date"], index, count, show=True)
        if sender == ANY_OTHER_SENDER:       # the quote bar names the REAL sender: take it from the row that matched
            sender = line_row_sender(row, body)
            if not sender:
                raise ToolError("ไม่รู้ชื่อผู้ส่งของแถวที่ตรง ไม่ quote")
        target = int(index or 1)
        self.focus(snap)
        stale = self.quote_name(snap)
        self.menu_click(snap, row)
        q = {"sender": sender, "text": body, "time": rt["time"], "date": rt["date"], "media": media,
             "index": target, "count": count}
        extra = {"cands": cands, "target": target}
        last = ""
        for _ in self._tries(40):
            s2 = self.snapshot()          # menu_click bumped the generation: always a fresh walk
            if not self.quote_armed(s2):
                continue
            if media:
                try:
                    ok, why = self.media_verdict(s2, extra)
                except ToolError as e:      # bar still animating / thumbnail not readable yet: look again
                    ok, why = False, str(e)
                if ok:
                    return q, extra
                last = why
                continue
            name = self.quote_name(s2)
            last = name
            if line_quote_fresh(name, sender, body, stale, armed_before):
                return q, extra
        try:
            self.close_quote(self.snapshot())
        except ToolError:
            pass
        raise ToolError("quote ที่ขึ้นไม่ตรงกับข้อความเป้าหมาย ปิดแล้ว ไม่ส่ง" + (f" (เห็น {last!r})" if last else ""))

    def media_verdict(self, snap, extra):
        th = self.quote_thumb(snap)
        scores = {k: round(crop_similarity(th, img), 3) for k, img in extra["cands"].items()}
        return line_media_verdict(scores, extra["target"])

    # ---- chat ----
    def open_chat(self, chat):
        snap = self.snapshot()
        if snap.header == chat:
            return snap
        if snap.search is None:
            raise ToolError("ไม่พบช่องค้นหาแชทของ LINE")
        self.focus(snap, snap.search, "ช่องค้นหา")
        self.guard(snap, snap.search, "ช่องค้นหา")
        self.be.press(CTRL_A, self.task)
        self.guard(snap, snap.search, "ช่องค้นหา")
        self.be.type_line(chat, self.task)
        rows, prev = [], None
        for _ in self._tries(60):        # no nap before the first read; typing bumped the generation: fresh walks
            s2 = self.snapshot()
            rows = self._chat_rows(s2, chat)
            sig = [(r.name, tuple(r.rect)) for r in rows]
            if rows and sig == prev:
                snap = s2
                break
            prev = sig
        if not rows:
            raise ToolError(f'ไม่พบแชทชื่อ "{chat}" ในผลค้นหา')
        if len(rows) > 1:
            raise ToolError(f'พบแชทชื่อ "{chat}" ซ้ำ {len(rows)} รายการ ไม่เดา ให้ระบุชื่อให้ไม่ซ้ำ')
        r = rows[0]
        if self.be.name(r.el) != r.name:
            raise ToolError("แถวแชทเปลี่ยนไประหว่างรอ ไม่คลิก")
        c = rect_center(self.be.rect(r.el))
        if not rect_contains(snap.main["rect"], c):
            raise ToolError("แถวแชทอยู่นอกหน้าต่าง LINE")
        self.be.click(c[0], c[1], "left", self.task, snap.main["pid"])
        for _ in self._tries(60):
            s3 = self.snapshot()         # the click bumped the generation: a walk taken after it
            if s3.header == chat:
                return s3
        raise ToolError(f'คลิกแล้วแต่หัวแชทไม่ใช่ "{chat}" (เห็น "{s3.header}")')

    def _chat_rows(self, snap, chat):
        mx, my, mw, mh = snap.main["rect"]
        limit = (snap.row_x - 4) if snap.row_x is not None else mx + mw // 2     # chat-list column only
        out = []
        for n in snap.nodes:
            if n.ctype != "Button" or n is snap.header_node:
                continue
            if not line_chat_row_matches(n.name, chat) or n.rect[0] >= limit:
                continue
            if snap.search is not None and n.rect[0] < snap.search.rect[0] - 30:
                continue                    # left-rail buttons (e.g. "Keep Memo") are not chat rows
            if not (my <= n.rect[1] and n.rect[1] + n.rect[3] <= my + mh):   # rows of the old list linger off-screen
                continue
            out.append(n)
        return out

    # ---- image ----
    def attach_image(self, path):
        norm = lambda ps: [os.path.normcase(os.path.normpath(p)) for p in ps]
        snap = self.snapshot(reuse=True)
        self.be.clip_set_files([path])
        if norm(self.be.clip_get_files()) != norm([path]):
            raise ToolError("คลิปบอร์ดไม่ตรงกับไฟล์รูปก่อนวาง ไม่วาง")
        before = {w["hwnd"] for w in self.be.line_windows()}
        self.focus(snap)
        self.guard(snap)
        self.be.press(CTRL_V, self.task)
        if norm(self.be.clip_get_files()) != norm([path]):
            raise ToolError("คลิปบอร์ดเปลี่ยนระหว่างวาง รูปที่วางอาจไม่ใช่ไฟล์นี้")
        found = None
        for _ in range(60):
            for w in self._new_popups(before):
                nodes, _t = self.be.tree(w["hwnd"])
                btns = {line_norm(n.name): n for n in nodes if n.ctype == "Button"}
                img = next((btns[k] for k in LINE_PASTE_IMAGE if k in btns), None)
                if img is not None and any(k in btns for k in LINE_PASTE_TEXT):
                    found = (w, img)
                    break
            if found:
                break
            self.sleep(0.03)
        if not found:
            raise ToolError('ไม่เจอตัวเลือกวางเป็น "รูป" / "ข้อความ" (Image / Text) ของ LINE')
        w, btn = found
        c = rect_center(self.be.rect(btn.el))
        if not rect_contains(w["rect"], c):
            raise ToolError("ปุ่ม รูป อยู่นอกหน้าต่างตัวเลือก")
        line_assert_clickable(btn.name)
        self.be.click(c[0], c[1], "left", self.task, snap.main["pid"])
        if norm(self.be.clip_get_files()) != norm([path]):
            raise ToolError("คลิปบอร์ดเปลี่ยนหลังวางรูป ตรวจสอบไม่ได้ว่าเป็นไฟล์นี้")
        for _ in self._tries(40):
            if self.box_text(self.snapshot()) == OBJ:
                return
        raise ToolError("วางรูปแล้วแต่กล่องข้อความไม่มีรูป 1 รูปพอดี")

    def thumb_sim(self, snap, path):
        x, y, w, h = self.be.rect(snap.edit.el)
        th = find_thumb(self.be.grab((x, y, w, h)))
        return None if th is None else round(thumb_similarity(th, path), 3)

    # ---- one-pass check ----
    def observe(self, exp, extra):
        # reuse=True: the snapshot is only reused when nothing was input / focused since it was taken; the window's
        # enabled state is re-read, and box text / quote bar / focus below are live reads in any case
        snap = self.snapshot(need_enabled=False, reuse=True)
        obs = {"enabled": snap.enabled, "header": snap.header, "locale": snap.locale}
        if snap.edit is None:
            obs.update(box=None, armed=False)
            return snap, obs, ["ไม่พบกล่องพิมพ์"]
        extra_problems = []
        hw = snap.main["hwnd"]
        if self.be.fg_root() != hw:
            extra_problems.append("LINE ไม่ได้อยู่หน้าสุด")
        if not self.be.focused_matches(snap.edit.el):
            extra_problems.append("โฟกัสไม่ได้อยู่ที่กล่องพิมพ์")
        obs["box"] = self.box_text(snap)
        obs["armed"] = self.quote_armed(snap)
        q = exp.get("quote")
        if obs["armed"] and q is not None:
            if q.get("media"):
                try:
                    obs["media_ok"], obs["media_reason"] = self.media_verdict(snap, extra)
                except ToolError as e:
                    obs["media_ok"], obs["media_reason"] = False, str(e)
            else:
                obs["quote_name"] = self.quote_name(snap)
        if exp.get("n_images") and exp.get("image_path"):
            obs["thumb_sim"] = self.thumb_sim(snap, exp["image_path"])
        return snap, obs, extra_problems + line_check_state(exp, obs)

    # ---- orchestration ----
    def cleanup(self):
        """Best effort after a failed prepare: close the quote, empty the box. Never raises."""
        try:
            snap = self.snapshot()
            if self.quote_armed(snap):
                snap = self.close_quote(snap)
            self.clear_box(snap)
        except Exception:
            pass

    FOCUS_OK = " | focus: คืนให้หน้าต่างเดิมแล้ว"
    FOCUS_FAIL = " | focus: คืนไม่ได้ ระวัง Enter"
    FOCUS_SKIP = " | focus: ไม่มีหน้าต่างเดิมให้คืน (LINE ยังถือโฟกัส) ระวัง Enter"

    def restore_focus(self, prev):
        """Hand the keyboard focus back to the window that was foreground before prepare (the terminal running the
        agent) so a stray Enter from the human cannot reach LINE while the text is armed. ONLY activates the window
        (be.bring_to_front): never types, clicks or sends keys to it, and no forbidden-window check (internal).
        Never raises. Returns the note to append to the result."""
        try:
            if not prev:
                return self.FOCUS_SKIP
            try:
                line_hw = {w["hwnd"] for w in self.be.line_windows(hidden=True)}
            except Exception:
                line_hw = set()
            if prev in line_hw or not self.be.window_alive(prev):
                return self.FOCUS_SKIP
            if self.be.fg_root() == prev:
                return self.FOCUS_OK
            ok = self.be.bring_to_front(prev)
            return self.FOCUS_OK if ok and self.be.fg_root() == prev else self.FOCUS_FAIL
        except Exception:
            return self.FOCUS_FAIL

    def _is_line_hwnd(self, hw):
        try:
            return hw in {w["hwnd"] for w in self.be.line_windows(hidden=True)}
        except Exception:
            return False

    def start_prev(self):
        """The window to hand the focus back to at the end of this tool call: the foreground window NOW when it is
        not a LINE window (and remembered at module level), else the last non-LINE foreground remembered from an
        earlier LINE call if it still exists and is not LINE. 0 = none known."""
        try:
            fg = self.be.fg_root()
        except Exception:
            fg = 0
        if fg and not self._is_line_hwnd(fg):
            _LAST_FG["hwnd"] = fg
            return fg
        h = _LAST_FG["hwnd"]
        try:
            if h and not self._is_line_hwnd(h) and self.be.window_alive(h):
                return h
        except Exception:
            pass
        return 0

    def run_focused(self, fn, on_fail=None):
        """Run fn() (a LINE tool body), then hand the focus back to the window that was foreground when the call
        started (success AND failure; on failure `on_fail` runs first). -> (result, focus_note). Yielded is
        re-raised untouched. A failed hand-back note is appended to a ToolError message."""
        prev = self.start_prev()
        try:
            res = fn()
        except winio.Yielded:
            raise                  # the user took over: do not touch the PC any more
        except Exception as e:
            if on_fail:
                on_fail()
            note = self.restore_focus(prev)
            if note == self.FOCUS_OK:
                raise
            if isinstance(e, ToolError):
                raise ToolError(str(e) + note) from e
            raise
        return res, self.restore_focus(prev)

    def prepare(self, chat, text, image_path, reply_to):
        """Arm the chat/quote/text, then give the focus back to the previous foreground window (success AND
        failure). obs["focus_note"] carries the result of the hand-back."""
        (exp, extra, obs), note = self.run_focused(lambda: self._prepare(chat, text, image_path, reply_to),
                                                   on_fail=self.cleanup)   # cleanup: close an armed quote, empty the box
        obs["focus_note"] = note
        return exp, extra, obs

    def _prepare(self, chat, text, image_path, reply_to):
        self.open_chat(chat)           # opens the chat itself when needed (verifies the header); no separate open needed
        # reuse=True: open_chat just verified the header on a snapshot taken after its last input, so no new walk
        # unless something was input since (then the generation moved and this is a real re-read)
        snap = self.snapshot(reuse=True)
        if snap.edit is None or snap.header != chat:
            raise ToolError(f'แชทที่เปิดอยู่ไม่ใช่ "{chat}" (เห็น "{snap.header}")')
        if self.quote_armed(snap):
            snap = self.close_quote(snap)
        self.clear_box(snap)
        exp = {"chat": chat, "text": text or "", "n_images": 1 if image_path else 0, "image_path": image_path,
               "quote": None}
        extra = {}
        if reply_to:
            exp["quote"], extra = self.arm_quote(reply_to)
        if image_path:
            self.attach_image(image_path)
        if text:
            snap = self.snapshot(reuse=True)
            self.focus(snap)
            self.type_message(snap, text, expect=line_box_expected(1 if image_path else 0, text))
        snap = self.snapshot(reuse=True)       # typing bumped the generation: this one is a fresh walk
        self.focus(snap)                       # no-op when LINE is already foreground with the box focused
        snap, obs, problems = self.observe(exp, extra)
        if problems:
            raise ToolError("ตรวจก่อนส่งไม่ผ่าน: " + "; ".join(problems))
        return exp, extra, obs

    def send(self, exp, extra):
        """Re-check, press Enter ONCE, report what the screen shows (+ the DB audit segment). The caller has already
        enforced `confirmed`."""
        report, job = self.send_core(exp, extra)
        return report + _audit_tail(job)

    def send_core(self, exp, extra):
        """send() without waiting for the DB audit: -> (gui_report, audit_job_or_None). The audit reader subprocess is
        started right after the single Enter and runs while the GUI post-check polls; the caller joins it with
        _audit_tail(job) (t_line_send does that AFTER the focus hand-back)."""
        # LINE to the front BEFORE the one walk, so that walk is current for observe() (a foreground change after it
        # would invalidate it); focus() below is then normally a no-op
        main = self.main()
        self._to_front(main["hwnd"])
        snap = self.snapshot(main=main)
        self.focus(snap)                       # foreground + focused element asserted; no-op when already so
        snap, obs, problems = self.observe(exp, extra)     # reuses the walk when focus() did not input anything
        if problems:
            raise ToolError("ไม่ส่ง ตรวจก่อนส่งไม่ผ่าน: " + "; ".join(problems))
        self._learn_region(snap)
        self.guard(snap)                       # ... and once more right before Enter
        self.task.check()
        sent_after_ms = int(time.time() * 1000)
        self.be.press(ENTER, self.task)        # <- the only Enter in the whole module
        job = _AuditJob(exp, sent_after_ms, time.monotonic())    # never raises; reads the DB in parallel
        # irreversible from here on: no yield checks (a mouse move must not hide that it was sent), no retry
        self.task = _Plain()
        seen_box, seen_row = None, None
        for i in range(POST_SEND_POLLS):
            try:
                s2 = self._post_snapshot()     # taken after the Enter (the press bumped the generation)
                seen_box = self.box_text(s2) if s2.edit is not None else None
                last = sorted(s2.msgs, key=lambda r: r.y)[-3:]
                want = exp.get("text") or ""
                hit = [r for r in last if (want and line_row_matches(r, None, want)) or
                       (not want and line_is_media_body(r.body_guess or r.rest))]
                seen_row = (hit[-1] if hit else (last[-1] if last else None))
                if seen_box == "" and hit:
                    break
            except Exception:
                pass
            self.be.nap(POST_SEND_STEP)        # sleeps only between polls: a condition that holds at once costs none
        box_ok = seen_box == ""
        row_ok = bool(seen_row) and ((exp.get("text") and line_row_matches(seen_row, None, exp["text"])) or
                                     (not exp.get("text") and line_is_media_body(seen_row.body_guess or seen_row.rest)))
        job.post_check = {"box_empty": box_ok, "row_match": bool(row_ok)}    # structured copy for line_cli.py
        report = (f'กด Enter แล้ว 1 ครั้ง | กล่อง{"ว่างแล้ว" if box_ok else f" ยังมี {seen_box!r}"} | '
                  f'แถวล่าสุด: {(seen_row.text[:60] if seen_row else "ไม่เห็น")!r} {"ตรงกับข้อความ" if row_ok else "ยังไม่ยืนยันว่าตรง (ตรวจเองอีกครั้ง)"}')
        return report, job


# ---------------------------------------------------------------------------------------------------------
# post-send audit against the local LINE Desktop DB (read-only reader subprocess; never part of the send itself)
# ---------------------------------------------------------------------------------------------------------

_TOOLS_DIR = os.path.join(os.path.expanduser("~"), "tools")
_READER_ROOT = os.path.join(_TOOLS_DIR, "line-desktop-mcp-v3.3.9")
AUDIT_TZ = datetime.timezone(datetime.timedelta(hours=8))        # the DB stores Asia/Taipei dates/times
AUDIT_RETRY_AT = (0.0, 0.7, 1.5, 3.0)                            # seconds after the send; LINE may flush late
AUDIT_SLACK_MS = 5000                                            # clock slack between Enter and sourceTimestamp
AUDIT_TIMEOUT = 10.0


def _audit_cfg():
    return {
        "python": os.environ.get("HANDS_LINE_READER_PY") or os.path.join(_READER_ROOT, ".venv", "Scripts", "python.exe"),
        "script": os.environ.get("HANDS_LINE_READER_SCRIPT") or os.path.join(_READER_ROOT, "src", "extensions", "python", "line-reader.py"),
        "dll": os.environ.get("HANDS_LINE_SQLITE_DLL") or os.path.join(_TOOLS_DIR, "line-mcp-runtime", "sqlite3mc-2.5.1", "dll", "sqlite3mc_x64.dll"),
    }


def _audit_enabled():
    return os.environ.get("HANDS_LINE_DB_AUDIT", "1").strip().lower() not in ("0", "false", "off", "no")


def _nl(s):
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _taipei_hms(ms):
    """sourceTimestamp -> Thai time (Taipei - 1 h)."""
    t = datetime.datetime.fromtimestamp(ms / 1000.0, datetime.timezone.utc) + datetime.timedelta(hours=7)
    return t.strftime("%H:%M:%S")


def _read_scope(cfg, scope):
    """One reader call. Returns ('ok', dict) or ('err', code). Never raises."""
    env = dict(os.environ)
    env["LINE_MCP_SQLITE3MC_DLL"] = cfg["dll"]
    env["LINE_MCP_EXTENSIONS"] = "1"
    env.pop("CHAT_LOG_ON", None)
    if not (os.path.isfile(cfg["python"]) and os.path.isfile(cfg["script"])):
        return "err", "READER_MISSING"
    try:
        p = subprocess.run([cfg["python"], cfg["script"]], input=json.dumps(scope), capture_output=True, text=True,
                           encoding="utf-8", timeout=AUDIT_TIMEOUT, env=env, shell=False)
    except subprocess.TimeoutExpired:
        return "err", "TIMEOUT"
    except Exception as e:
        return "err", type(e).__name__
    try:
        d = json.loads(p.stdout)
    except Exception:
        return "err", f"BAD_JSON(rc={p.returncode})"
    if not isinstance(d, dict):
        return "err", "BAD_JSON"
    if d.get("ok") is not True:
        return "err", str(d.get("code") or "READER_FAILED")
    return "ok", d


def _audit_search(chat, sent_after_ms, t_send, scope_extra, match):
    """Read the chat on the AUDIT_RETRY_AT schedule until match(own_message) is true. -> ('confirmed', thai_time,
    related) | ('notfound',) | ('error', code). Read only; never raises. Only OWN messages newer than the send
    (minus AUDIT_SLACK_MS) are offered to match()."""
    try:
        cfg = _audit_cfg()
        now_ms = int(time.time() * 1000)
        d_from = datetime.datetime.fromtimestamp((min(sent_after_ms, now_ms) - 600_000) / 1000.0, AUDIT_TZ).date()
        d_to = datetime.datetime.fromtimestamp(max(sent_after_ms, now_ms) / 1000.0, AUDIT_TZ).date()
        scope = {"chatName": chat, "chatType": "auto", "dateFrom": d_from.isoformat(), "dateTo": d_to.isoformat(),
                 "mediaMode": "metadata", **scope_extra}
        t0 = t_send if t_send is not None else time.monotonic()
        for at in AUDIT_RETRY_AT:
            wait = at - (time.monotonic() - t0)
            if wait > 0:
                time.sleep(wait)
            kind, res = _read_scope(cfg, scope)
            if kind == "err":
                return ("error", res)
            own = res.get("ownSenderRef")
            for m in res.get("messages") or []:
                if not isinstance(m, dict) or not own or m.get("senderRef") != own:
                    continue
                ts = m.get("sourceTimestamp")
                if not isinstance(ts, (int, float)) or ts < sent_after_ms - AUDIT_SLACK_MS:
                    continue
                if match(m):
                    return ("confirmed", _taipei_hms(ts), bool(m.get("relatedSourceRef")))
        return ("notfound",)
    except Exception as e:
        return ("error", type(e).__name__)


def db_audit_sent(chat, text, sent_after_ms, quote=False, t_send=None):
    """Look for the just-sent text in the local DB. Returns ('confirmed', thai_time, related) | ('notfound',) |
    ('error', code). Read only; never raises; never retries the send (it only re-reads).
    t_send = time.monotonic() at the Enter: AUDIT_RETRY_AT is measured from it (default: from this call)."""
    try:
        want = _nl(text)
        lines = [ln for ln in want.split("\n") if ln.strip()]
        query = max(lines, key=len).strip()[:1000] if lines else ""
        extra = {"messageLimit": 20}
        if query:
            extra["query"] = query

        def same_text(m):
            got = m.get("text")
            return isinstance(got, str) and (_nl(got) == want or _nl(got).strip() == want.strip())
        return _audit_search(chat, sent_after_ms, t_send, extra, same_text)
    except Exception as e:
        return ("error", type(e).__name__)


def db_audit_image(chat, sent_after_ms, t_send=None):
    """Image-only send: an own message with contentType 1 (image) newer than the send, in that chat. The reader is not
    filtered by text here (no query); the few newest messages are checked. Same return shape as db_audit_sent."""
    try:
        def is_image(m):
            return m.get("contentType") == 1
        return _audit_search(chat, sent_after_ms, t_send, {"messageLimit": 10}, is_image)
    except Exception as e:
        return ("error", type(e).__name__)


def db_audit_segment(exp, sent_after_ms, t_send=None):
    """The report segment for line_send, or None when the audit is disabled. Never raises."""
    try:
        if not _audit_enabled():
            return None
        text = exp.get("text") or ""
        if not text.strip():
            if not exp.get("n_images"):
                return "DB: ตรวจไม่ได้ (ไม่มีข้อความ)"
            # image-only send: confirm by an own image message (contentType 1) newer than the Enter
            r = db_audit_image(exp.get("chat") or "", sent_after_ms, t_send=t_send)
            if r[0] == "confirmed":
                return f"DB: ยืนยัน ({r[1]} เวลาไทย, รูป)"
            if r[0] == "notfound":
                return "DB: ไม่พบรูป"
            return f"DB: ตรวจไม่ได้ ({r[1]})"
        r = db_audit_sent(exp.get("chat") or "", text, sent_after_ms, bool(exp.get("quote")), t_send=t_send)
        if r[0] == "confirmed":
            extra = ""
            if exp.get("quote"):
                extra = ", มี quote ผูก" if r[2] else ", ไม่เห็น quote ผูก"
            return f"DB: ยืนยัน ({r[1]} เวลาไทย{extra})"
        if r[0] == "notfound":
            return "DB: ไม่พบข้อความ"
        return f"DB: ตรวจไม่ได้ ({r[1]})"
    except Exception as e:
        return f"DB: ตรวจไม่ได้ ({type(e).__name__})"


AUDIT_JOIN_MAX = 25.0      # never wait longer than this for the audit thread (the server gives a tool 40 s)


class _AuditJob:
    """The DB audit running in a daemon thread, started right after the Enter. Touches no GUI, never raises:
    result() joins and returns the segment (None = disabled), or an error segment when the thread died / hung."""

    def __init__(self, exp, sent_after_ms, t_send):
        self._seg, self._err = None, None
        self._th = threading.Thread(target=self._run, args=(exp, sent_after_ms, t_send), name="line-audit", daemon=True)
        try:
            self._th.start()
        except Exception as e:
            self._th, self._err = None, f"DB: ตรวจไม่ได้ ({type(e).__name__})"

    def _run(self, exp, sent_after_ms, t_send):
        try:
            self._seg = db_audit_segment(exp, sent_after_ms, t_send=t_send)
        except BaseException as e:     # db_audit_segment already never raises; belt and braces for the thread
            self._err = f"DB: ตรวจไม่ได้ ({type(e).__name__})"

    def result(self, timeout=AUDIT_JOIN_MAX):
        try:
            if self._th is not None:
                self._th.join(timeout)
                if self._th.is_alive():
                    return "DB: ตรวจไม่ได้ (TIMEOUT)"
            return self._err or self._seg
        except Exception as e:
            return f"DB: ตรวจไม่ได้ ({type(e).__name__})"


def _audit_tail(job):
    """' | DB: ...' for the report ('' when there is no job or the audit is disabled). Never raises."""
    seg = job.result() if job is not None else None
    return (" | " + seg) if seg else ""


# ---------------------------------------------------------------------------------------------------------
# tools (registered lazily in tools.TOOLS)
# ---------------------------------------------------------------------------------------------------------

class _Plain:
    """Task stand-in after the irreversible step: never raises Yielded."""

    def check(self):
        pass

    def sleep(self, secs):
        time.sleep(secs)


def new_ui(task):
    return LineUI(Win(), task)


def _run(task, fn):
    ui = new_ui(task)
    orig = None
    try:
        orig = ui.be.cursor()
    except Exception:
        pass
    try:
        return fn(ui)
    finally:
        if orig is not None:
            try:
                ui.be.move_cursor(*orig)
            except Exception:
                pass


def _need_str(a, key):
    v = a.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ToolError(f"ต้องระบุ {key}")
    return v.strip()


def _evidence(exp, obs):
    q = exp.get("quote")
    ev = ["กล่องตรง"]
    ev.append("ไม่มี quote" if q is None else ("quote รูปผ่านพิกเซล" if q.get("media") else "quote ตรงทุกตัวอักษร"))
    if q is not None:
        ev.append(f'ผู้ส่งที่ quote: {q["sender"]}')
    if exp.get("n_images"):
        ev.append(f'รูปคล้ายไฟล์ {obs.get("thumb_sim")}')
    return ", ".join(ev)


def t_line_open_chat(a, task):
    chat = _need_str(a, "chat")
    if "\n" in chat:
        raise ToolError("ชื่อแชทห้ามมีขึ้นบรรทัดใหม่")
    snap, note = _run(task, lambda ui: ui.run_focused(lambda: ui.open_chat(chat)))
    return f'เปิดแชท "{snap.header}" แล้ว{note}'


def t_line_prepare(a, task):
    chat = _need_str(a, "chat")
    text, image = a.get("text"), a.get("image_path")
    if text in (None, ""):
        text = ""
    else:
        why = line_text_problem(text)
        if why:
            raise ToolError(why)
    if image in (None, ""):
        image = None
    else:
        image = os.path.abspath(str(image))
        if not os.path.isfile(image) or os.path.splitext(image)[1].lower() not in IMAGE_EXT:
            raise ToolError(f"ไม่พบไฟล์รูป หรือไม่ใช่นามสกุลรูป: {image}")
        if os.path.getsize(image) > 25 * 1024 * 1024:
            raise ToolError("ไฟล์รูปใหญ่เกิน 25 MB")
    if not text and not image:
        raise ToolError("ต้องมี text หรือ image_path อย่างน้อยหนึ่งอย่าง")
    rt = a.get("reply_to")
    if rt is not None:
        if not isinstance(rt, dict):
            raise ToolError("reply_to ต้องเป็น object {sender,text,time,date,index?,expected_count?}")
        for k in ("sender", "text", "time", "date"):
            if not isinstance(rt.get(k), str) or not rt[k].strip():
                raise ToolError(f"reply_to ต้องมี {k}")

    def go(ui):
        return ui.prepare(chat, text, image, rt)     # cleanup on failure + focus hand-back live inside prepare

    exp, extra, obs = _run(task, go)
    tok = STORE.put({"exp": exp, "extra": extra})
    return (f"เตรียมแล้ว ยังไม่ได้ส่ง | {line_summary(exp)} | ตรวจ: {_evidence(exp, obs)}{obs.get('focus_note', '')} | token={tok} (หมดอายุ 120 วิ) "
            "— ให้ผู้ใช้ยืนยันข้อความนี้ แล้วเรียก line_send {token, confirmed:true}")


def t_line_send(a, task):
    tok = _need_str(a, "token")
    ent = STORE.get(tok)
    if ent is None:
        raise ToolError("token หมดอายุหรือไม่มี ให้ line_prepare ใหม่ (ยังไม่ได้ส่งอะไร)")
    c = confirm_needed("line_send", a, line_summary(ent["exp"]), "LINE.exe")
    if c:
        raise NeedsConfirm(c)
    try:
        # focus goes home only AFTER send() returns (Enter pressed) or refused
        (out, job), note = _run(task, lambda ui: ui.run_focused(lambda: ui.send_core(ent["exp"], ent["extra"])))
    finally:
        STORE.drop(tok)       # one shot: a failed check also needs a fresh prepare
    # the DB audit has been running since the Enter, in parallel with the GUI post-check and the focus hand-back
    return out + _audit_tail(job) + note


def t_line_cancel(a, task):
    STORE.drop()

    def go(ui):
        snap = ui.snapshot()
        notes = []
        if ui.quote_armed(snap):
            snap = ui.close_quote(snap)
            notes.append("ปิด quote แล้ว")
        if snap.edit is not None:
            ui.clear_box(snap)
            notes.append("ล้างกล่องแล้ว")
        s2 = ui.snapshot()
        return (" | ".join(notes) or "ไม่มีอะไรค้าง") + \
               f' | สถานะ: quote {"ยังค้าง" if ui.quote_armed(s2) else "ไม่มี"}, กล่อง {"ว่าง" if ui.box_text(s2) == "" else "ไม่ว่าง"}'

    out, note = _run(task, lambda ui: ui.run_focused(lambda: go(ui)))
    return "ยกเลิกแล้ว " + out + note
