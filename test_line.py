"""Unit tests for the LINE module (line.py + the LINE parts of logic.py). NO GUI: SendInput (winio._send), UIA,
clipboard, screenshots and windows are all faked. Run:  python -m pytest -q test_line.py
The fake below is a small stateful LINE: chat list, header, message rows with real y (also off screen), a context
menu popup, a quote bar whose UIA name goes stale, a paste chooser, an input box."""
import sys
import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import line  # noqa: E402
import logic  # noqa: E402
import tools  # noqa: E402
import winio  # noqa: E402
from logic import ToolError  # noqa: E402

TODAY = __import__("datetime").date(2026, 11, 18)
OBJ = "\ufffc"


class FakeTask:
    def check(self):
        pass

    def sleep(self, secs):
        pass


class El:
    def __init__(self, ident, rect_fn, name_fn=lambda: ""):
        self.ident, self.rect, self.name = ident, rect_fn, name_fn


def node(ident, ctype, rect_fn, name_fn=lambda: ""):
    return NS(el=El(ident, rect_fn, name_fn), ctype=ctype, name=name_fn(), rect=rect_fn(), autoid="", enabled=True)


COLORS = {"red": (200, 40, 40), "green": (40, 200, 40), "blue": (40, 40, 200)}


class FakeLine:
    """Backend double. Layout: window (0,0,1500,1000); chat list x=10; chat pane x=375; list y 110..890 (shrinks 80
    px while a quote bar is shown); input Edit (385,900,1000,60)."""
    MAIN = 100
    ROW_H, ROW_W, PX = 56, 1100, 91
    # UI strings + layout of the chat-list column: FakeLineEN below overrides them for the English UI
    S = {"search": "ค้นหาข้อความและห้องแชท", "input": "พิมพ์ข้อความ", "group_suffix": ", 10 คน", "hdr2": "ค้นหา",
         "menu": ["ตอบกลับ", "คัดลอก", "แชร์", "ยกเลิกข้อความ", "ลบ"], "menu_nocopy": ["ตอบกลับ", "ยกเลิกข้อความ", "ลบ"],
         "reply": "ตอบกลับ", "deny": "ลบ", "img": "รูป", "chooser": ["รูป", "ข้อความ"]}
    SX, CX, RAIL = 10, 10, False

    def fmt_row(self, sender, body, tm):
        return f"{sender}, {body}, {tm} น." if sender else f"{body}, {tm} น."

    def chat_row(self, c):
        return f"{c}, preview, 16/11"

    def quote_name(self, sender, body):
        return f"ตอบกลับข้อความของ {sender} ข้อความเดิมของ {sender} คือ {body}"

    def __init__(self, items=None, chat="Group A", chats=("Group A", "Teem")):
        self.chat, self.chats = chat, list(chats)
        self.items = items if items is not None else []   # ("date", text) | ("msg", sender|None, body, time, color)
        self.fg, self.main_enabled, self.focus_id = self.MAIN, True, "edit"
        self.search, self.box, self.armed = "", "", False
        self.qname = None                 # quote Group name; survives closing (stale)
        self.menu = self.chooser = None   # popup windows
        self.menu_rows = None
        self.clicks, self.keys, self.typed, self.sent, self.wheels = [], [], [], [], []
        self.clip, self.box_color, self.stale_image_quote = [], None, True
        self.pinned, self.dialogs, self.deny_overlap, self.menu_without_copy = False, [], False, False
        self.loaded_from = max(0, len(self.items) - 12)
        self.scroll = self.bottom_scroll()
        self.sel_all = False
        self.chooser_clicked = False
        self.events, self.focus_calls = [], []   # ordered ("focus", hwnd) / ("key", vks) log; every bring_to_front hwnd
        self.alive = {500}                       # non-LINE windows that exist and are visible (500 = the terminal)
        self.walks, self.subwalks = 0, 0         # full-window UIA walks / chat-pane-only walks (cost counters)
        self.pane_alive = True                   # False: the cached chat-pane element is stale (walk finds nothing)

    # --- model ---
    def n(self):
        return len(self.items)

    def list_bottom(self):
        return 890 - (80 if self.armed else 0)

    def bottom_scroll(self):
        return self.list_bottom() - (120 + 60 * self.n())

    def y_of(self, i):
        return 120 + 60 * i + self.scroll

    def row_rect(self, i):
        return (375, self.y_of(i), self.ROW_W, self.ROW_H)

    def row_name(self, i):
        it = self.items[i]
        if it[0] == "date":
            return it[1]
        _, sender, body, tm, _c = it
        return self.fmt_row(sender, body, tm)

    def edit_rect(self):
        return (385, 900, 1000, 60)

    def group_rect(self):
        return (375, 810, 1100, 80)

    # --- backend API ---
    def line_windows(self, hidden=False):
        w = [{"hwnd": self.MAIN, "title": "LINE", "cls": "Qt663QWindowIcon", "rect": (0, 0, 1500, 1000),
              "enabled": self.main_enabled, "visible": True, "pid": 7},
             {"hwnd": 111, "title": "LINE", "cls": "Qt663QWindowIcon", "rect": (1300, 800, 180, 80), "enabled": True,
              "visible": True, "pid": 7}]    # the small notification popup also titled "LINE"
        for p in (self.menu, self.chooser):
            if p:
                w.append(p)
        if hidden:
            w += self.dialogs
        return w

    def check_forbidden(self, w):
        pass

    def fg_root(self):
        return self.fg

    def is_enabled(self, hwnd):
        return self.main_enabled if hwnd == self.MAIN else True

    def bring_to_front(self, hwnd):
        self.events.append(("focus", hwnd))
        self.focus_calls.append(hwnd)
        self.fg = hwnd
        return True

    def window_alive(self, hwnd):
        return hwnd in self.alive

    def window_at(self, x, y):
        for p in (self.menu, self.chooser):
            if p and logic.rect_contains(p["rect"], (x, y)):
                return p["hwnd"]
        return self.MAIN

    def rect(self, el):
        return el.rect()

    def name(self, el):
        return el.name()

    def text(self, el):
        return self.box if self.box else self.S["input"]

    def set_focus(self, el):
        self.focus_id = el.ident

    def focused_matches(self, el):
        return self.focus_id == el.ident

    def element_name_at(self, x, y):
        if self.armed and logic.rect_contains(self.group_rect(), (x, y)):
            return self.qname or ""
        return ""

    def tree(self, hwnd):
        if self.menu and hwnd == self.menu["hwnd"]:
            return self.menu_nodes(), False
        if self.chooser and hwnd == self.chooser["hwnd"]:
            return [node(f"ch{i}", "Button", lambda i=i: (self.chooser["rect"][0] + 5, self.chooser["rect"][1] + 5 + 40 * i, 100, 30),
                         lambda i=i: self.S["chooser"][i]) for i in range(2)], False
        if hwnd != self.MAIN:
            return [node("d", "Text", lambda: (0, 0, 100, 20), lambda: "ส่งไฟล์")], False
        self.walks += 1
        out = [node("search", "Edit", lambda: (self.SX, 60, 300, 30), lambda: self.S["search"])]
        if self.RAIL:      # English UI: the left rail has a "Keep Memo" Button too (NOT a chat row)
            out.append(node("rail", "Button", lambda: (15, 905, 32, 32), lambda: "Keep Memo"))
        for i, c in enumerate([c for c in self.chats if self.search in c]):
            out.append(node(f"chat{c}", "Button", lambda i=i: (self.CX, 120 + 70 * i, 300, 60), lambda c=c: self.chat_row(c)))
        out.append(node("pane", "Pane", lambda: (370, 55, 1115, 915), lambda: ""))
        out.append(node("hdr", "Button", lambda: (385, 65, 200, 30), lambda: self.chat + (self.S["group_suffix"] if self.chat == "Group A" else "")))
        out.append(node("hdr2", "Button", lambda: (1300, 65, 40, 30), lambda: self.S["hdr2"]))
        out.append(node("list", "List", lambda: (375, 110, 1100, self.list_bottom() - 110), lambda: ""))
        if self.pinned:
            out.append(node("pin", "Text", lambda: (420, 115, 600, 50), lambda: "ประกาศ"))
        for i in range(self.loaded_from, self.n()):
            out.append(node(f"row{i}", "Text", lambda i=i: self.row_rect(i), lambda i=i: self.row_name(i)))
        if self.qname is not None:
            out.append(node("grp", "Group", self.group_rect, lambda: self.qname))
        out.append(node("edit", "Edit", self.edit_rect, lambda: self.S["input"]))
        return out, False

    def subtree(self, el):
        """The chat pane only: everything of tree() that sits inside the pane (a dead pane yields nothing)."""
        self.subwalks += 1
        if el.ident != "pane" or not self.pane_alive:
            return [], True
        nodes, trunc = self.tree(self.MAIN)
        self.walks -= 1                                    # not a full walk
        inside = [n for n in nodes if n.ctype != "Pane" and n.rect[0] >= 370]
        return inside, trunc

    def menu_nodes(self):
        names = self.S["menu_nocopy"] if self.menu_without_copy else self.S["menu"]
        x, y = self.menu["rect"][:2]
        out = []
        for i, nm in enumerate(names):
            if self.deny_overlap and nm == self.S["deny"]:
                out.append(node(f"m{i}", "Button", lambda x=x, y=y: (x, y + 4, 100, 30), lambda nm=nm: nm))
            else:
                out.append(node(f"m{i}", "Button", lambda x=x, y=y, i=i: (x, y + 4 + 34 * i, 100, 30), lambda nm=nm: nm))
        return out

    def click(self, x, y, button, task, pid=None):
        self.clicks.append((x, y, button))
        pt = (x, y)
        if self.menu and logic.rect_contains(self.menu["rect"], pt):
            nodes = self.menu_nodes()
            hit = [n for n in nodes if logic.rect_contains(n.rect, pt)]
            self.last_menu_hit = [n.name for n in hit]
            if any(n.name == self.S["reply"] for n in hit):
                self.arm()
            self.menu = None
            return
        if self.chooser and logic.rect_contains(self.chooser["rect"], pt):
            self.chooser_clicked = True
            self.box += OBJ
            self.box_color = tuple(Image.open(self.clip[0]).convert("RGB").getpixel((1, 1)))
            self.chooser = None
            return
        if button == "right":
            for i in range(self.loaded_from, self.n()):
                r = self.row_rect(i)
                if self.items[i][0] == "msg" and logic.rect_contains(r, pt) and r[0] + 100 <= x <= r[0] + 400:
                    self.menu_row = i
                    self.menu = {"hwnd": 200, "title": "", "cls": "Qt663QWindowPopup", "rect": (x + 10, y + 10, 150, 200),
                                 "enabled": True, "visible": True, "pid": 7}
            return
        g = self.group_rect()
        if self.armed and (x, y) == (g[0] + g[2] - 22, g[1] + 15):
            self.armed = False
            return
        for i, c in enumerate([c for c in self.chats if self.search in c]):
            if logic.rect_contains((self.CX, 120 + 70 * i, 300, 60), pt):
                self.chat, self.search = c, ""
                self.scroll = self.bottom_scroll()

    def arm(self):
        it = self.items[self.menu_row]
        self.armed = True
        media = it[2] == self.S["img"]
        self.qcolor = COLORS.get(it[4]) if media else None
        if media and self.qname is not None and self.stale_image_quote:
            pass                                   # image quotes do NOT refresh the UIA name (stays stale)
        else:
            self.qname = self.quote_name(it[1], it[2])
        if media and self.qname is None:
            self.qname = self.quote_name("X", self.S["img"])

    def wheel_event(self, x, y, direction, notches, task):
        self.wheels.append((direction, notches))
        d = self.PX * notches * (1 if direction == "up" else -1)
        self.scroll = max(self.bottom_scroll(), min(0, self.scroll + d))
        top_idx = max(0, int((110 - 120 - self.scroll) // 60))
        self.loaded_from = min(self.loaded_from, max(0, top_idx - 8))

    def press(self, vks, task):
        self.keys.append(tuple(vks))
        self.events.append(("key", tuple(vks)))
        v = tuple(vks)
        if v == (0x11, 0x41):
            self.sel_all = True
        elif v == (0x2E,):
            if self.sel_all:
                self.box = ""
            self.sel_all = False
        elif v == (0x10, 0x0D):
            self.box += "\n"
        elif v == (0x0D,):
            self.sent.append((self.chat, self.box, self.armed, self.qname if self.armed else None))
            self.items.append(("msg", None, self.box, "10.30", "red"))
            self.box, self.armed = "", False
            self.loaded_from = max(0, self.n() - 12)
            self.scroll = self.bottom_scroll()
        elif v == (0x11, 0x56) and self.clip:
            x, y, w, h = self.edit_rect()
            self.chooser = {"hwnd": 300, "title": "", "cls": "Qt663QWindowPopup", "rect": (x + 20, y - 120, 140, 90),
                            "enabled": True, "visible": True, "pid": 7}
        elif v == (0x1B,):
            self.menu = self.chooser = None

    def type_line(self, text, task):
        assert "\n" not in text and "\t" not in text
        self.typed.append(text)
        if self.focus_id == "search":
            self.search += text
        elif self.focus_id == "edit":
            self.box += text

    def nap(self, secs):
        pass

    def cursor(self):
        return (0, 0)

    def move_cursor(self, x, y):
        pass

    def grab(self, bbox):
        x, y, w, h = bbox
        im = Image.new("RGB", (w, h), (255, 255, 255))
        px = im.load()

        def fill(x0, y0, x1, y1, col):
            for yy in range(max(0, y0), min(h, y1)):
                for xx in range(max(0, x0), min(w, x1)):
                    px[xx, yy] = col

        for i in range(self.loaded_from, self.n()):
            r = self.row_rect(i)
            if self.items[i][0] == "msg" and (x, y, w, h) == tuple(r):
                fill(100, int(h * 0.2), 400, int(h * 0.8), COLORS[self.items[i][4]])
        if (x, y, w, h) == self.group_rect() and self.qcolor_now():
            fill(w - 120, 10, w - 60, h - 10, self.qcolor_now())
        if (x, y, w, h) == self.edit_rect() and OBJ in self.box and self.box_color:
            fill(10, 5, 70, 55, self.box_color)
        return im

    def qcolor_now(self):
        return getattr(self, "qcolor", None)

    def clip_set_files(self, paths):
        self.clip = list(paths)

    def clip_get_files(self):
        return list(self.clip)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    line._NOTCH["px"] = None
    line._LAST_FG["hwnd"] = 0
    line.STORE.drop()
    yield
    line.STORE.drop()


def ui_for(fake, **kw):
    return line.LineUI(fake, FakeTask(), today=TODAY, **kw)


def history():
    items = [("date", "10 พฤศจิกายน 2026")] + [("msg", "Zed", f"filler {k}", "8.00", "red") for k in range(30)]
    items += [("date", "15 พฤศจิกายน 2026"),
              ("msg", "Bob", "hello", "9.00", "red"),             # 32
              ("msg", "Bob", "hello world", "9.05", "red"),
              ("msg", "Al, Jr", "a, b", "9.06", "red"),
              ("date", "16 พฤศจิกายน 2026"),
              ("msg", "Carol", "รูป", "13.18", "red"),
              ("msg", "Carol", "รูป", "13.18", "green"),
              ("msg", "Carol", "รูป", "13.18", "blue"),
              ("msg", "Bob", "hello", "9.00", "red"),             # same sender/body/time, different day
              ("msg", None, "ok, then", "14.00", "red"),
              ("date", "วันนี้"),
              ("msg", "Bob", "late, news", "10.10", "red"),
              ("msg", "Bob", "x", "10.11", "red"), ("msg", "Bob", "y", "10.12", "red")]
    return items


class FakeLineEN(FakeLine):
    """English UI, strings as captured live from LINE 26.6.0.4004 (search/input names, Button chat rows, 12 h clock,
    ", Read" on own rows, 'Photos' bodies). GUESS (not captured yet): the context-menu item names, the quote-bar
    name, the group header suffix and the paste chooser."""
    S = {"search": "Search for chats and messages", "input": "Enter a message", "group_suffix": ", 10 people",
         "hdr2": "Search chat",
         "menu": ["Reply", "Copy", "Share", "Unsend", "Delete"], "menu_nocopy": ["Reply", "Unsend", "Delete"],
         "reply": "Reply", "deny": "Delete", "img": "Photos", "chooser": ["Image", "Text"]}
    SX, CX, RAIL = 74, 62, True

    def __init__(self, items=None, chat="Group A", chats=("Group A", "Teem")):
        super().__init__(items, chat, chats)

    def fmt_row(self, sender, body, tm):
        h, m = tm.split(".")
        t = f"{int(h) % 12 or 12}:{m} {'PM' if int(h) >= 12 else 'AM'}"
        return f"{sender}, {body}, {t}" if sender else f"{body}, {t}, Read"

    def chat_row(self, c):
        return f"{c}, You sent a photo., November 19, 2026"

    def quote_name(self, sender, body):
        return f"A reply to {sender}'s message. {sender}'s original message: {body}"


_EN_DATES = {"10 พฤศจิกายน 2026": "November 10, 2026", "15 พฤศจิกายน 2026": "November 15, 2026",
             "16 พฤศจิกายน 2026": "November 16, 2026", "วันนี้": "November 18, 2026"}   # English UI shows today by its date


def history_en():
    """history() as the English UI shows it, plus the 'Unread messages below' separator inside the last day."""
    out = []
    for it in history():
        if it[0] == "date":
            out.append(("date", _EN_DATES[it[1]]))
        else:
            out.append(tuple("Photos" if x == "รูป" else x for x in it))
    k = max(i for i, it in enumerate(out) if it[0] == "msg" and it[2] == "late, news")
    out.insert(k, ("date", "Unread messages below"))       # a Text between rows, not a message and not a date
    return out


# =================================== pure: parsing ===================================

def test_date_whitelist():
    for ok in ["16 พฤศจิกายน 2026", "1 มกราคม 2025", "วันนี้", "เมื่อวาน", "5 ธันวาคม 2569"]:
        assert logic.line_is_date(ok), ok
    for bad in ["16 พ.ย. 2026", "hello, 13.18 น.", "Bob, 16 พฤศจิกายน 2026, 9.00 น.", "พฤศจิกายน 2026", "16 พฤศจิกายน 26",
                "วันนี้ครับ", "มี 16 พฤศจิกายน 2026 นะ"]:
        assert not logic.line_is_date(bad), bad
    assert logic.line_date_key("16 พฤศจิกายน 2026") == (2026, 11, 16)
    assert logic.line_date_key("16 พฤศจิกายน 2569") == (2026, 11, 16)      # Buddhist year
    assert logic.line_date_key("วันนี้", TODAY) == (2026, 11, 18)
    assert logic.line_date_key("เมื่อวาน", TODAY) == (2026, 11, 17)
    assert logic.line_date_key("31 กุมภาพันธ์ 2026") is None
    assert logic.line_date_key("hello") is None


def test_plain_strips_html_mentions():
    assert logic.line_plain('Bob, <a href="x">@Carol</a> ดูให้หน่อย, 9.00 น.') == "Bob, @Carol ดูให้หน่อย, 9.00 น."
    r = logic.line_parse_row('Bob, <a href="x">@Carol</a> ดูให้หน่อย, 9.00 น.')
    assert (r.kind, r.sender_guess, r.body_guess, r.time) == ("msg", "Bob", "@Carol ดูให้หน่อย", "9.00")


def test_parse_message_rows():
    r = logic.line_parse_row("Carol-AC 💚, รูป, 13.18 น.")
    assert (r.kind, r.sender_guess, r.body_guess, r.time) == ("msg", "Carol-AC 💚", "รูป", "13.18")
    r = logic.line_parse_row("Bob, late, news, again, 9.05 น.")      # body contains ", "
    assert r.rest == "Bob, late, news, again" and r.time == "9.05"
    assert logic.line_row_matches(r, "Bob", "late, news, again")
    assert not logic.line_row_matches(r, "Bob", "late, news")           # exact, not prefix/substring
    assert not logic.line_row_matches(r, "Bob", "news, again")
    assert not logic.line_row_matches(r, "Bo", "late, news, again")
    own = logic.line_parse_row("ok, then, 14.00 น.")                    # own message: no sender prefix
    assert logic.line_row_matches(own, None, "ok, then") and logic.line_row_matches(own, "", "ok, then")
    # KNOWN LIMIT of the row text: "ok, then" (own message) and sender "ok" + body "then" read identically. Matching is
    # textual identity, so both interpretations match; the expected_count check is what keeps this from being guessed.
    assert logic.line_row_matches(own, "ok", "then")
    assert not logic.line_row_matches(own, "ok", "ok, then")
    assert logic.line_row_matches(r, "Bob", "late, news, again", "9.05") and not logic.line_row_matches(r, "Bob", "late, news, again", "9.06")
    assert logic.line_row_matches(r, "Bob", "late, news, again", "09.05 น.")   # time normalised


def test_parse_reply_rows():
    name = "Bob ตอบกลับข้อความของ Al ข้อความเดิมของ Al คือ เดิม, จริง, ตอบ, 9.05 น."
    r = logic.line_parse_row(name)
    assert r.kind == "reply" and r.reply_sender == "Bob" and r.time == "9.05"
    assert logic.line_row_matches(r, "Bob", "ตอบ") and logic.line_row_matches(r, "Bob", "จริง, ตอบ")
    assert not logic.line_row_matches(r, "Al", "ตอบ")
    assert not logic.line_row_matches(r, "Bob", "เดิม, จริง, ตอบ")      # whole remainder is orig + reply: tail only when shorter
    own = logic.line_parse_row("ตอบกลับข้อความของ Al ข้อความเดิมของ Al คือ q, mine, 9.05 น.")
    assert own.kind == "reply" and own.reply_sender is None and logic.line_row_matches(own, None, "mine")


MULTI_TH = ("รุ่น A-100 เทียบเป็นรุ่น B-200 ค่ะ (ตามตาราง)\n"
            "ต่างที่ความสูงบ่า +0.5 มม.\n"
            "ฝากส่งสเปคให้ลูกค้าด้วยนะคะ")


def test_multiline_text_matches_space_joined_row_name():
    # LINE's UIA row name joins the lines of a multi-line message with one space
    name = MULTI_TH.replace("\n", " ") + ", 2:06 PM"
    r = logic.line_parse_row(name)
    assert r.kind == "msg" and r.time == "14.06"
    assert logic.line_row_matches(r, None, MULTI_TH)
    assert logic.line_row_matches(r, None, MULTI_TH, "2:06 PM")
    assert logic.line_row_matches(r, None, MULTI_TH.replace("\n", "\r\n"))       # CRLF is a newline run too
    assert logic.line_row_matches(r, None, MULTI_TH.replace("\n", " \n "))      # spaces around the run are optional
    assert logic.line_row_matches(r, "*", MULTI_TH) is False                     # own row never matches "any other"
    assert not logic.line_row_matches(r, None, MULTI_TH.replace("\n", " \n \n") + "x")
    assert not logic.line_row_matches(r, None, MULTI_TH, "14:07 PM")            # time still compared


def test_multiline_row_sender_and_reply_forms():
    r = logic.line_parse_row("Bob, " + MULTI_TH.replace("\n", " ") + ", 2:06 PM")
    assert r.kind == "msg" and r.sender_guess == "Bob"
    assert logic.line_row_matches(r, "Bob", MULTI_TH)
    assert logic.line_row_matches(r, "*", MULTI_TH) and logic.line_row_sender(r, MULTI_TH) == "Bob"
    assert not logic.line_row_matches(r, "Al", MULTI_TH)
    rr = logic.line_parse_row("A reply to Bob's message. Bob's original message: q, "
                              + MULTI_TH.replace("\n", " ") + ", 2:06 PM")
    assert rr.kind == "reply" and logic.line_row_matches(rr, None, MULTI_TH)
    en = logic.line_parse_row("A reply from Bob to Me's message. Me's original message: x. Reply: "
                              + MULTI_TH.replace("\n", " ") + ". , 2:06 PM")
    assert en.reply_raw is not None and logic.line_row_matches(en, "Bob", MULTI_TH)


def test_multiline_matching_still_exact_one_character_refused():
    r = logic.line_parse_row(MULTI_TH.replace("\n", " ") + ", 2:06 PM")
    assert not logic.line_row_matches(r, None, MULTI_TH.replace("มม.", "มม"))        # one character off
    assert not logic.line_row_matches(r, None, MULTI_TH.replace("ลูกค้า", "ลูกค้า "))  # an extra plain space is refused
    assert not logic.line_row_matches(r, None, MULTI_TH.replace("\n", "  "))        # two spaces: not a newline run
    assert not logic.line_row_matches(r, None, MULTI_TH.replace("\n", ""))           # newline run removed entirely



def test_date_attach_nearest_separator_above():
    def mk(name, y):
        r = logic.line_parse_row(name)
        r.rect = (375, y, 1000, 50)
        return r
    rows = [mk("Bob, hi, 9.00 น.", 400), mk("15 พฤศจิกายน 2026", 100), mk("16 พฤศจิกายน 2026", 300),
            mk("Al, yo, 9.00 น.", 150), mk("Bob, late, 9.00 น.", 500), mk("Bob, orphan, 1.00 น.", 20)]
    logic.line_attach_dates(rows)
    keys = {r.text: r.date_key for r in rows}
    assert keys["Al, yo, 9.00 น."] == (2026, 11, 15)
    assert keys["Bob, hi, 9.00 น."] == (2026, 11, 16) and keys["Bob, late, 9.00 น."] == (2026, 11, 16)
    assert keys["Bob, orphan, 1.00 น."] is None                                   # nothing above it


def test_unknown_date_separator_poisons_dates_below():
    a, b, c = logic.line_parse_row("15 พฤศจิกายน 2026"), logic.line_parse_row("16 พ.ย. 2026"), logic.line_parse_row("Bob, hi, 9.00 น.")
    for i, r in enumerate((a, b, c)):
        r.rect = (375, i * 100, 1000, 50)
    logic.line_attach_dates([a, b, c])
    assert c.date_key == "?"        # a date-looking but unlisted separator: never matched by date


# =================================== pure: quote / menu / wheel / token ===================================

def test_quote_expected_name_and_stale():
    exp = logic.line_expected_quote("Bob", "hello, world")
    assert exp == "ตอบกลับข้อความของ Bob ข้อความเดิมของ Bob คือ hello, world"
    assert logic.line_quote_equal(exp.replace(" ", "  ") + " ", "Bob", "hello, world")          # whitespace normalised
    assert logic.line_quote_equal('ตอบกลับข้อความของ Bob ข้อความเดิมของ Bob คือ <a href="x">@Al</a> hi', "Bob", "@Al hi")
    assert not logic.line_quote_equal(exp[:-3], "Bob", "hello, world")                          # prefix is not enough
    assert not logic.line_quote_equal(exp + "!", "Bob", "hello, world")
    stale = logic.line_expected_quote("Bob", "old")
    # fresh name differs from stale and equals expected: ok
    assert logic.line_quote_fresh(exp, "Bob", "hello, world", stale, armed_before=False)
    # name still the stale one (does not equal expected): refused
    assert not logic.line_quote_fresh(stale, "Bob", "hello, world", stale, armed_before=False)
    # name equals expected BUT is identical to the stale name: only ok when stale already was expected and bar was not armed
    assert logic.line_quote_fresh(exp, "Bob", "hello, world", exp, armed_before=False)
    assert not logic.line_quote_fresh(exp, "Bob", "hello, world", exp, armed_before=True)
    assert logic.line_quote_fresh(exp, "Bob", "hello, world", "", armed_before=True)   # no stale name at all


def test_menu_denylist():
    for name in ["ลบ", "ยกเลิกข้อความ", " ลบ "]:
        with pytest.raises(ToolError):
            logic.line_assert_clickable(name)
    logic.line_assert_clickable("ตอบกลับ")
    logic.line_assert_clickable("รูป")


def test_menu_validation():
    near = (110, 110, 150, 200)
    both = ["ตอบกลับ", "คัดลอก", "แชร์", "ลบ"]
    assert logic.line_menu_ok(near, (100, 100), both)[0]
    assert logic.line_menu_ok(near, (100, 100), ["ตอบกลับ", "บันทึก", "แชร์"])[0]     # image menu: no คัดลอก, still OK
    assert not logic.line_menu_ok(near, (100, 100), ["ตอบกลับ", "เลือกทั้งหมด"])[0]  # no message-menu peer
    assert not logic.line_menu_ok(near, (100, 100), ["ตอบกลับ", "ยกเลิกข้อความ", "ลบ"])[0]  # destructive items are no evidence
    assert not logic.line_menu_ok(near, (100, 100), ["คัดลอก"])[0]                  # no ตอบกลับ
    assert not logic.line_menu_ok((1500, 900, 150, 200), (100, 100), both)[0]       # window far from the click
    assert not logic.line_menu_ok(near, (100, 100), [])[0]


def test_wheel_chunks():
    assert logic.line_wheel_chunks(0) == []
    assert logic.line_wheel_chunks(8) == [8] and logic.line_wheel_chunks(9) == [8, 1]
    c = logic.line_wheel_chunks(538)
    assert sum(c) == 538 and max(c) == 8 and len(c) == 68


def test_winio_wheel_event_one_event_max_8(monkeypatch):
    sent = []
    monkeypatch.setattr(winio, "_send", lambda *inputs: sent.append([(i.mi.mouseData, i.mi.dwFlags) for i in inputs]))
    monkeypatch.setattr(winio, "move_cursor", lambda x, y: None)
    monkeypatch.setattr(winio.time, "sleep", lambda s: None)
    winio.mouse_wheel_event(10, 10, "up", 8, FakeTask())
    assert sent == [[(960, 0x800)]]
    winio.mouse_wheel_event(10, 10, "down", 3, FakeTask())
    assert sent[1] == [((-360) & 0xFFFFFFFF, 0x800)]
    with pytest.raises(ValueError):
        winio.mouse_wheel_event(10, 10, "up", 9, FakeTask())


def test_token_ttl():
    t = [0.0]
    s = logic.TokenStore(ttl=120, clock=lambda: t[0])
    tok = s.put({"a": 1})
    assert s.get(tok) == {"a": 1} and s.get(tok.upper()) == {"a": 1}
    t[0] = 119
    assert s.get(tok) == {"a": 1}
    t[0] = 121
    assert s.get(tok) is None and s.get(tok) is None
    t[0] = 200
    a = s.put({"n": "a"})
    b = s.put({"n": "b"})            # a newer prepare forgets older tokens (one box)
    assert s.get(a) is None and s.get(b) == {"n": "b"}
    s.drop(b)
    assert s.get(b) is None
    assert s.get(None) is None and s.get("nope") is None


def test_text_problem_rules():
    assert logic.line_text_problem("สวัสดี\nบรรทัดสอง") is None
    assert logic.line_text_problem("a\tb")
    assert logic.line_text_problem("a\n")
    assert logic.line_text_problem("a" * 2001)
    assert logic.line_text_problem("x" + OBJ)
    assert logic.line_text_problem("a\x07b")
    assert logic.line_text_problem(5)


def test_check_state_rules():
    exp = {"chat": "Teem", "text": "hi", "n_images": 0, "quote": None}
    obs = {"enabled": True, "header": "Teem", "box": "hi", "armed": False}
    assert logic.line_check_state(exp, obs) == []
    assert logic.line_check_state(exp, dict(obs, header="Other"))
    assert logic.line_check_state(exp, dict(obs, box="hi "))
    assert logic.line_check_state(exp, dict(obs, armed=True))          # plain send must not carry a leftover quote
    assert logic.line_check_state(exp, dict(obs, enabled=False))
    q = dict(exp, quote={"sender": "Bob", "text": "x", "media": False})
    good = dict(obs, armed=True, quote_name=logic.line_expected_quote("Bob", "x"))
    assert logic.line_check_state(q, good) == []
    assert logic.line_check_state(q, dict(good, quote_name=logic.line_expected_quote("Bob", "y")))
    assert logic.line_check_state(q, dict(good, armed=False))
    img = {"chat": "Teem", "text": "", "n_images": 1, "quote": None}
    iobs = {"enabled": True, "header": "Teem", "box": OBJ, "armed": False, "thumb_sim": 0.9}
    assert logic.line_check_state(img, iobs) == []
    assert logic.line_check_state(img, dict(iobs, thumb_sim=0.80))      # below 0.85 blocks
    assert logic.line_check_state(img, dict(iobs, box=OBJ + OBJ))
    assert logic.line_check_state(img, dict(iobs, box=OBJ + "x"))
    assert logic.line_check_state(img, dict(iobs, thumb_sim=None))


def test_media_verdict():
    assert logic.line_media_verdict({1: 0.95, 2: 0.80, 3: 0.82}, 1)[0]
    assert not logic.line_media_verdict({1: 0.90, 2: 0.88}, 1)[0]       # margin < 0.05
    assert not logic.line_media_verdict({1: 0.80, 2: 0.95}, 1)[0]       # wrong bubble
    assert logic.line_media_verdict({1: 0.9}, 1)[0] and not logic.line_media_verdict({1: 0.5}, 1)[0]


def test_box_text_normalisation():
    assert logic.line_box_text("พิมพ์ข้อความ") == "" and logic.line_box_text("แชทในชื่อ Me") == ""
    assert logic.line_box_text("a\r\nb") == "a\nb" and logic.line_box_text("a\u2028b") == "a\nb"
    assert logic.line_box_text(OBJ + "\n") == OBJ


# =================================== gate ===================================

def test_gate_cases():
    c = logic.confirm_needed
    assert c("type_text", {"text": "hi\nthere"}, "", "LINE.exe")                         # any newline in LINE
    assert c("click", {}, "DAN, ส่งไฟล์แล้ว, 16/11", "LINE.exe") is None               # chat row: label only
    assert c("click", {}, "Message, Send", "chrome.exe")                                # whole name outside LINE
    assert c("click", {}, "ตอบกลับ", "LINE.exe")                                         # generic gate still blocks the menu item
    assert c("press_keys", {"keys": "enter"}, "", "LINE.exe")
    assert c("line_send", {}, "แชท x", "LINE.exe")
    assert c("line_send", {"confirmed": "true"}, "แชท x", "LINE.exe")                   # only JSON true passes
    assert c("line_send", {"confirmed": True}, "แชท x", "LINE.exe") is None


def test_line_tools_bypass_generic_name_gate_only_internally():
    """The line tools click ตอบกลับ through winio themselves (never via t_click), so the generic gate on that name does
    not stop them; the denylist and menu validation in line.py are what protects the menu."""
    f = FakeLine(history())
    ui = ui_for(f)
    snap, row, _ = ui.locate("Bob", "late, news", "10.10", "วันนี้", show=True)
    ui.menu_click(snap, row)
    assert f.armed and f.last_menu_hit == ["ตอบกลับ"]
    assert logic.confirm_needed("click", {}, "ตอบกลับ", "LINE.exe")


# =================================== typing (winio level, _send stubbed) ===================================

def _flat(inputs):
    return [(i.type, i.ki.vk, i.ki.scan, i.ki.dwFlags) for i in inputs if i.type == 1]


@pytest.fixture
def sendlog(monkeypatch):
    log = []
    monkeypatch.setattr(winio, "_send", lambda *inputs: log.extend(_flat(inputs)))
    monkeypatch.setattr(winio.time, "sleep", lambda s: None)
    return log


def test_fast_and_slow_typing_same_order(sendlog):
    text = "ab\nก\tc d😀\n\nz"
    winio.type_unicode(text, FakeTask(), fast=False)
    slow = list(sendlog)
    sendlog.clear()
    winio.type_unicode(text, FakeTask(), fast=True)
    assert slow == list(sendlog) and slow
    # sanity: the Enter/Tab really sit between the text around them
    vks = [(e[1], e[2]) for e in slow if e[3] == 0 and e[1] in (0x0D, 0x09)]
    assert [v[0] for v in vks] == [0x0D, 0x09, 0x0D, 0x0D]
    first_enter = next(i for i, e in enumerate(slow) if e[1] == 0x0D)
    assert [chr(e[2]) for e in slow[:first_enter] if e[3] == 4] == ["a", "b"]


class SendWin(FakeLine):
    """FakeLine whose keys/typing go through the REAL winio functions (with _send stubbed)."""

    def press(self, vks, task):
        winio.press_vks(vks, task)

    def type_line(self, text, task):
        winio.type_unicode(text, task, fast=False)


def test_multiline_typing_uses_shift_enter(sendlog):
    f = SendWin(history())
    ui = ui_for(f)
    snap = ui.snapshot()
    ui.type_message(snap, "บรรทัด1\nline2\n\nline4")
    held, bare_enter, shifted_enter = set(), 0, 0
    for _t, vk, _scan, fl in sendlog:
        if vk == 0x10:
            held.add(0x10) if fl == 0 else held.discard(0x10)
        if vk == 0x0D and fl == 0:
            shifted_enter += 1 if 0x10 in held else 0
            bare_enter += 0 if 0x10 in held else 1
    assert bare_enter == 0 and shifted_enter == 3
    chars = "".join(chr(e[2]) for e in sendlog if e[3] == 4)
    assert chars == "บรรทัด1line2line4"
    assert all(e[1] != 0x0D or e[3] != 4 for e in sendlog)


def test_focus_guard_refuses_before_any_input(sendlog):
    for tweak in ("fg", "disabled", "focus"):
        f = SendWin(history())
        f.box = "keep me"
        ui = ui_for(f)
        snap = ui.snapshot()
        if tweak == "fg":
            f.fg = 999
        elif tweak == "disabled":
            f.main_enabled = False
        else:
            f.focus_id = "search"
        with pytest.raises(ToolError):
            ui.type_message(snap, "hello")                  # typing only guards: never moves focus itself
        f.bring_to_front = lambda h: False                  # and when focusing cannot be fixed either...
        f.set_focus = lambda el: None
        with pytest.raises(ToolError):
            ui.clear_box(snap)                              # ...Ctrl+A / Delete must not be sent
        assert sendlog == [], tweak
        assert f.box == "keep me"


def test_disabled_main_names_the_blocking_dialog():
    f = FakeLine(history())
    f.main_enabled = False
    f.dialogs = [{"hwnd": 400, "title": "ส่งไฟล์", "cls": "Qt663QWindowIcon", "rect": (0, 0, 300, 200), "enabled": True,
                  "visible": False, "pid": 7}]
    with pytest.raises(ToolError) as e:
        ui_for(f).snapshot()
    assert "ส่งไฟล์" in str(e.value) and "ซ่อน" in str(e.value)


# =================================== state discovery ===================================

def test_snapshot_derives_geometry_from_the_window():
    f = FakeLine(history())
    s = ui_for(f).snapshot()
    assert s.edit.el.ident == "edit"
    assert s.row_x == 375 and s.header == "Group A"                      # ", 10 คน" stripped
    assert s.list_node.el.ident == "list" and s.top == 110 and s.bottom == 890
    f.pinned = True
    assert ui_for(f).snapshot().top == 165 + 15                          # pushed below the pinned announcement
    f.armed = True
    assert ui_for(f).snapshot().bottom == 810


def test_quote_armed_from_geometry_not_from_stale_name():
    f = FakeLine(history())
    f.qname = logic.line_expected_quote("Bob", "old")                     # stale Group left in the tree
    ui = ui_for(f)
    assert not ui.quote_armed(ui.snapshot())                              # Group present, bar not shown
    f.armed = True
    assert ui.quote_armed(ui.snapshot())


# =================================== locate ===================================

def test_locate_exact_not_substring():
    f = FakeLine(history())
    ui = ui_for(f)
    snap, row, ms = ui.locate("Bob", "hello", "9.00", "15 พฤศจิกายน 2026")
    assert row.text == "Bob, hello, 9.00 น." and len(ms) == 1
    with pytest.raises(ToolError):                                       # "hello" is a prefix of "hello world": not a match
        ui.locate("Bob", "hell", "9.00", "15 พฤศจิกายน 2026")
    with pytest.raises(ToolError):
        ui.locate("Bob", "hello world", "9.00", "15 พฤศจิกายน 2026")    # wrong minute
    with pytest.raises(ToolError):
        ui.locate("Bo", "hello", "9.00", "15 พฤศจิกายน 2026")           # sender must be exact too


def test_locate_uses_date_and_today_alias():
    f = FakeLine(history())
    ui = ui_for(f)
    a = ui.locate("Bob", "hello", "9.00", "15 พฤศจิกายน 2026")[1]
    b = ui.locate("Bob", "hello", "9.00", "16 พฤศจิกายน 2026")[1]
    assert a.y != b.y and a.date_key == (2026, 11, 15) and b.date_key == (2026, 11, 16)
    c = ui.locate("Bob", "late, news", "10.10", "18 พฤศจิกายน 2026")[1]    # "วันนี้" separator == explicit date
    assert c.date_key == (2026, 11, 18)
    with pytest.raises(ToolError):
        ui.locate("Bob", "hello", "9.00", "17 พฤศจิกายน 2026")             # no such day
    with pytest.raises(ToolError):
        ui.locate("Bob", "hello", "9.00", "yesterday-ish")
    with pytest.raises(ToolError):
        ui.locate("Bob", "hello", "", "15 พฤศจิกายน 2026")                  # identity needs time and date


def test_locate_comma_bodies_and_own_messages():
    ui = ui_for(FakeLine(history()))
    assert ui.locate("Al, Jr", "a, b", "9.06", "15 พฤศจิกายน 2026")[1].text == "Al, Jr, a, b, 9.06 น."
    assert ui.locate("", "ok, then", "14.00", "16 พฤศจิกายน 2026")[1].text == "ok, then, 14.00 น."


def test_locate_duplicates_count_and_index():
    f = FakeLine(history())
    ui = ui_for(f)
    args = ("Carol", "รูป", "13.18", "16 พฤศจิกายน 2026")
    with pytest.raises(ToolError) as e:                                  # 3 identical rows, caller expects 1
        ui.locate(*args)
    assert "3" in str(e.value)
    with pytest.raises(ToolError):                                       # expects 2, finds 3
        ui.locate(*args, index=1, expected_count=2)
    with pytest.raises(ToolError):                                       # expects 4, finds 3
        ui.locate(*args, index=1, expected_count=4)
    with pytest.raises(ToolError):                                       # duplicates need an index
        ui.locate(*args, expected_count=3)
    with pytest.raises(ToolError):
        ui.locate(*args, index=4, expected_count=3)
    rows = [ui.locate(*args, index=k, expected_count=3)[1] for k in (1, 2, 3)]
    assert [r.y for r in rows] == sorted(r.y for r in rows) and len({r.y for r in rows}) == 3
    assert [r.node.name for r in rows] == ["Carol, รูป, 13.18 น."] * 3
    # index N really is the Nth row from the top: its colour in the fake is red, green, blue
    for k, r in enumerate(rows):
        i = next(j for j in range(f.n()) if f.row_rect(j)[1] == r.y)
        assert f.items[i][4] == ["red", "green", "blue"][k]


def test_locate_loads_older_history_with_8_notch_events():
    f = FakeLine(history())
    assert f.loaded_from > 32                                            # the target row is not in the tree yet
    ui = ui_for(f)
    snap, row, _ = ui.locate("Bob", "hello", "9.00", "15 พฤศจิกายน 2026")
    assert f.wheels and all(d == "up" and 1 <= k <= 8 for d, k in f.wheels)
    assert row.text == "Bob, hello, 9.00 น."


def test_locate_show_brings_row_into_band_with_chunked_wheel():
    f = FakeLine(history())
    ui = ui_for(f)
    snap, row, _ = ui.locate("Bob", "hello", "9.00", "15 พฤศจิกายน 2026", show=True)
    assert snap.visible(row)
    assert f.wheels and max(k for _d, k in f.wheels) <= 8
    assert line._NOTCH["px"] == 91                                       # measured at runtime from one notch
    # and back down to a recent row
    snap, row, _ = ui.locate("Bob", "y", "10.12", "วันนี้", show=True)
    assert snap.visible(row) and row.text == "Bob, y, 10.12 น."


def test_locate_refuses_when_tree_truncated():
    f = FakeLine(history())
    orig = f.tree
    f.tree = lambda hwnd: (orig(hwnd)[0], True)
    with pytest.raises(ToolError):
        ui_for(f).locate("Bob", "late, news", "10.10", "วันนี้")


# =================================== menu / quote flow ===================================

def test_menu_click_refuses_bad_menu_and_never_clicks_destructive():
    f = FakeLine(history())
    f.menu_without_copy = True                                           # looks like some other popup
    ui = ui_for(f)
    snap, row, _ = ui.locate("Bob", "late, news", "10.10", "วันนี้", show=True)
    with pytest.raises(ToolError):
        ui.menu_click(snap, row)
    assert (0x1B,) in f.keys and not f.armed                             # Escape pressed, nothing clicked in the menu
    assert [c for c in f.clicks if c[2] == "left"] == []

    f = FakeLine(history())
    f.deny_overlap = True                                                # "ลบ" button drawn on top of "ตอบกลับ"
    ui = ui_for(f)
    snap, row, _ = ui.locate("Bob", "late, news", "10.10", "วันนี้", show=True)
    with pytest.raises(ToolError):
        ui.menu_click(snap, row)
    assert not f.armed and [c for c in f.clicks if c[2] == "left"] == []


def test_menu_click_requires_point_inside_row_and_band():
    f = FakeLine(history())
    ui = ui_for(f)
    snap, row, _ = ui.locate("Bob", "late, news", "10.10", "วันนี้", show=True)
    snap.top = row.y + row.h + 10                                        # pretend the band no longer contains the row
    with pytest.raises(ToolError):
        ui.bubble_point(snap, row)
    assert f.clicks == []


def test_arm_text_quote_and_name_check():
    f = FakeLine(history())
    ui = ui_for(f)
    rt = {"sender": "Bob", "text": "late, news", "time": "10.10", "date": "วันนี้"}
    q, extra = ui.arm_quote(rt)
    assert f.armed and q["media"] is False
    assert f.qname == "ตอบกลับข้อความของ Bob ข้อความเดิมของ Bob คือ late, news"


def test_arm_quote_refused_when_quote_name_wrong():
    f = FakeLine(history())
    f.stale_image_quote = True
    orig = f.arm

    def bad_arm():
        orig()
        f.qname = "ตอบกลับข้อความของ Bob ข้อความเดิมของ Bob คือ something else"   # clicked the wrong message's quote
    f.arm = bad_arm
    ui = ui_for(f)
    with pytest.raises(ToolError):
        ui.arm_quote({"sender": "Bob", "text": "late, news", "time": "10.10", "date": "วันนี้"})
    assert not f.armed                                                   # closed again after the refusal


def test_arm_quote_refuses_stale_name_left_from_previous_quote():
    f = FakeLine(history())
    f.qname = logic.line_expected_quote("Bob", "late, news")             # stale Group already carries the SAME text
    orig = f.arm
    f.arm = lambda: (setattr(f, "armed", True))                          # UIA name never refreshes (as for image quotes)
    ui = ui_for(f)
    # bar was not armed before and the stale name equals the expected one -> accepted by the rule
    q, _ = ui.arm_quote({"sender": "Bob", "text": "late, news", "time": "10.10", "date": "วันนี้"})
    assert f.armed
    f2 = FakeLine(history())
    f2.qname = logic.line_expected_quote("Bob", "something old")
    f2.arm = lambda: setattr(f2, "armed", True)                          # stale + different text -> refused
    with pytest.raises(ToolError):
        ui_for(f2).arm_quote({"sender": "Bob", "text": "late, news", "time": "10.10", "date": "วันนี้"})
    assert not f2.armed


def test_arm_quote_closes_leftover_quote_first():
    f = FakeLine(history())
    f.armed = True
    f.qname = logic.line_expected_quote("Zed", "filler 1")
    ui = ui_for(f)
    ui.arm_quote({"sender": "Bob", "text": "late, news", "time": "10.10", "date": "วันนี้"})
    g = f.group_rect()
    assert any((c[0], c[1]) == (g[0] + g[2] - 22, g[1] + 15) for c in f.clicks)   # the x was clicked (right-22, top+15)
    assert f.qname.endswith("late, news")


def test_media_quote_picks_target_by_pixels():
    for target in (1, 2, 3):
        f = FakeLine(history())
        ui = ui_for(f)
        rt = {"sender": "Carol", "text": "รูป", "time": "13.18", "date": "16 พฤศจิกายน 2026", "index": target,
              "expected_count": 3}
        q, extra = ui.arm_quote(rt)
        assert q["media"] and f.armed and set(extra["cands"]) == {1, 2, 3}
        i = next(j for j in range(f.n()) if f.menu_row == j)
        assert f.items[i][4] == ["red", "green", "blue"][target - 1]      # the bubble the menu was opened on


def test_media_quote_refused_when_quote_thumb_is_another_image():
    f = FakeLine(history())
    orig = f.arm

    def wrong():
        orig()
        f.qcolor = COLORS["blue"]            # LINE quoted another one of the identical images
    f.arm = wrong
    ui = ui_for(f)
    with pytest.raises(ToolError):
        ui.arm_quote({"sender": "Carol", "text": "รูป", "time": "13.18", "date": "16 พฤศจิกายน 2026", "index": 1,
                      "expected_count": 3})
    assert not f.armed


# =================================== open chat / prepare / send ===================================

def test_open_chat_exact_match_and_refusals():
    f = FakeLine(history(), chat="Teem", chats=("Group A", "Teem", "Teem2"))
    f.items = []
    ui = ui_for(f)
    f.chat = "Group A"
    ui2 = ui_for(f)
    snap = ui2.open_chat("Teem")                                        # exact "Teem", not "Teem2"
    assert snap.header == "Teem" and f.typed == ["Teem"] and (0x11, 0x41) in f.keys
    # no match
    with pytest.raises(ToolError):
        ui_for(f).open_chat("Nobody")
    # two exact rows
    g = FakeLine(history(), chats=("Dup", "Dup", "Other"))
    with pytest.raises(ToolError) as e:
        ui_for(g).open_chat("Dup")
    assert "ซ้ำ" in str(e.value)


def test_open_chat_already_open_does_nothing():
    f = FakeLine(history())
    snap = ui_for(f).open_chat("Group A")
    assert snap.header == "Group A" and f.keys == [] and f.clicks == []


def test_prepare_plain_text_then_send(monkeypatch):
    f = FakeLine(history())
    f.armed, f.qname = True, logic.line_expected_quote("Zed", "stale")      # leftover quote from a crashed run
    ui = ui_for(f)
    exp, extra, obs = ui.prepare("Group A", "สวัสดี\nสอง", None, None)
    assert f.box == "สวัสดี\nสอง" and not f.armed                          # leftover quote closed, text typed, no Enter
    assert f.sent == [] and (0x0D,) not in f.keys and (0x10, 0x0D) in f.keys
    out = ui.send(exp, extra)
    assert f.sent == [("Group A", "สวัสดี\nสอง", False, None)] and f.keys.count((0x0D,)) == 1
    assert "กล่องว่าง" in out


def test_send_refuses_when_state_changed_after_prepare():
    for change in ("box", "chat", "quote", "dialog"):
        f = FakeLine(history())
        ui = ui_for(f)
        exp, extra, _ = ui.prepare("Group A", "hello", None, None)
        if change == "box":
            f.box = "hello!!"
        elif change == "chat":
            f.chat = "Teem"
        elif change == "quote":
            f.armed, f.qname = True, logic.line_expected_quote("Zed", "x")
        else:
            f.main_enabled = False
        with pytest.raises(ToolError):
            ui_for(f).send(exp, extra)
        assert f.sent == [] and (0x0D,) not in f.keys, change


def test_send_guard_right_before_enter_catches_a_race():
    """Focus/foreground lost between the one-pass check and the keypress: Enter must not be sent."""
    for race in ("fg", "focus", "disabled"):
        f = FakeLine(history())
        exp, extra, _ = ui_for(f).prepare("Group A", "hello", None, None)
        ui = ui_for(f)
        real = ui.observe

        def observe_then_race(e, x, real=real, race=race, f=f):
            out = real(e, x)
            if race == "fg":
                f.fg = 999
            elif race == "focus":
                f.focus_id = "search"
            else:
                f.main_enabled = False
            return out
        ui.observe = observe_then_race
        with pytest.raises(ToolError):
            ui.send(exp, extra)
        assert f.sent == [] and (0x0D,) not in f.keys, race


def test_prepare_with_text_quote():
    f = FakeLine(history())
    ui = ui_for(f)
    exp, extra, obs = ui.prepare("Group A", "ตอบแล้ว", None,
                                 {"sender": "Bob", "text": "late, news", "time": "10.10", "date": "วันนี้"})
    assert f.armed and f.box == "ตอบแล้ว" and exp["quote"]["sender"] == "Bob"
    ui_for(f).send(exp, extra)
    assert f.sent[0][2] is True and f.sent[0][3].endswith("late, news")


def test_prepare_image_with_caption_and_wrong_clipboard(tmp_path):
    p = tmp_path / "a.png"
    Image.new("RGB", (400, 200), COLORS["red"]).save(p)
    f = FakeLine(history())
    ui = ui_for(f)
    exp, extra, obs = ui.prepare("Group A", "caption", str(p), None)
    assert f.box == OBJ + "caption" and f.chooser_clicked and obs["thumb_sim"] >= 0.95
    ui_for(f).send(exp, extra)
    assert f.sent[0][1] == OBJ + "caption"
    # clipboard that does not hold our file after the paste -> refuse
    g = FakeLine(history())
    orig = g.press

    def evil(vks, task):
        orig(vks, task)
        if tuple(vks) == (0x11, 0x56):
            g.clip = [str(tmp_path / "other.png")]
    g.press = evil
    with pytest.raises(ToolError):
        ui_for(g).prepare("Group A", "", str(p), None)
    assert g.sent == []


def test_image_thumbnail_below_threshold_blocks(tmp_path):
    p = tmp_path / "a.png"
    Image.new("RGB", (400, 200), COLORS["red"]).save(p)
    f = FakeLine(history())
    ui = ui_for(f)
    exp, extra, _ = ui.prepare("Group A", "", str(p), None)
    f.box_color = COLORS["blue"]                                          # a different picture is in the box now
    with pytest.raises(ToolError) as e:
        ui_for(f).send(exp, extra)
    assert "0.85" in str(e.value) and f.sent == []


# =================================== focus hand-back after prepare ===================================

TERM = 500


def test_prepare_restores_focus_to_previous_window_without_touching_it():
    f = FakeLine(history())
    f.fg = TERM
    exp, extra, obs = ui_for(f).prepare("Group A", "hello", None,
                                        {"sender": "Bob", "text": "late, news", "time": "10.10", "date": "วันนี้"})
    assert f.fg == TERM and obs["focus_note"] == line.LineUI.FOCUS_OK
    assert f.events[-1] == ("focus", TERM)                     # the very last thing done = activate the terminal
    assert f.events.count(("focus", TERM)) == 1
    last_focus_main = max(i for i, e in enumerate(f.events) if e == ("focus", f.MAIN))
    assert all(e[0] == "focus" or i < last_focus_main for i, e in enumerate(f.events))   # no key after the hand-back
    assert f.box == "hello" and f.armed and f.sent == []        # text + quote still armed (fake: LINE keeps them)
    assert (0x0D,) not in f.keys


def test_prepare_failure_also_restores_focus_and_cleans_up():
    f = FakeLine(history())
    f.fg = TERM
    ui = ui_for(f)

    def refuse(e, x):
        f.fg = f.MAIN                                           # LINE holds the focus and the text is typed ...
        raise ToolError("ตรวจก่อนส่งไม่ผ่าน: test")
    ui.observe = refuse
    with pytest.raises(ToolError) as e:
        ui.prepare("Group A", "hello", None, None)
    assert f.fg == TERM and f.events[-1] == ("focus", TERM) and f.box == "" and not f.armed   # ... then cleaned + handed back
    assert "คืนไม่ได้" not in str(e.value)


def test_restore_skipped_when_previous_window_is_line_or_gone():
    f = FakeLine(history())                                     # LINE itself was foreground
    _, _, obs = ui_for(f).prepare("Group A", "hello", None, None)
    assert obs["focus_note"] == line.LineUI.FOCUS_SKIP and f.focus_calls == [] and f.fg == f.MAIN
    g = FakeLine(history())                                     # the small LINE notification popup (111) is LINE too
    g.fg = 111
    _, _, obs = ui_for(g).prepare("Group A", "hello", None, None)
    assert obs["focus_note"] == line.LineUI.FOCUS_SKIP and TERM not in g.focus_calls
    h = FakeLine(history())                                     # previous window was closed meanwhile
    h.fg, h.alive = TERM, set()
    _, _, obs = ui_for(h).prepare("Group A", "hello", None, None)
    assert obs["focus_note"] == line.LineUI.FOCUS_SKIP and TERM not in h.focus_calls and h.box == "hello"


def test_restore_failure_is_reported_but_prepare_succeeds():
    for how in ("false", "raises"):
        f = FakeLine(history())
        f.fg = TERM
        orig = f.bring_to_front

        def b(h, orig=orig, how=how):
            if h == TERM:
                if how == "raises":
                    raise OSError("boom")
                return False
            return orig(h)
        f.bring_to_front = b
        exp, extra, obs = ui_for(f).prepare("Group A", "hello", None, None)
        assert obs["focus_note"] == line.LineUI.FOCUS_FAIL and f.box == "hello", how
        assert "ระวัง Enter" in line.LineUI.FOCUS_FAIL


def test_send_refocuses_line_before_checks_and_enter():
    f = FakeLine(history())
    f.fg = TERM
    exp, extra, _ = ui_for(f).prepare("Group A", "hello", None, None)
    assert f.fg == TERM
    f.events.clear()
    ui = ui_for(f)
    real = ui.observe

    def observe(e, x):
        f.events.append(("observe", f.fg))
        return real(e, x)
    ui.observe = observe
    ui.send(exp, extra)
    assert f.events[0] == ("focus", f.MAIN)                     # LINE comes forward first ...
    assert f.events[1] == ("observe", f.MAIN)                   # ... then the full re-check runs with LINE foreground ...
    assert f.events.index(("key", (0x0D,))) > 1 and f.events.count(("key", (0x0D,))) == 1   # ... then the single Enter
    assert f.sent == [("Group A", "hello", False, None)]


def test_send_with_terminal_focused_does_not_send_if_line_cannot_be_raised():
    f = FakeLine(history())
    f.fg = TERM
    exp, extra, _ = ui_for(f).prepare("Group A", "hello", None, None)
    f.bring_to_front = lambda h: False
    with pytest.raises(ToolError):
        ui_for(f).send(exp, extra)
    assert f.sent == [] and (0x0D,) not in f.keys


def test_tool_result_carries_focus_note(monkeypatch):
    f = FakeLine(history())
    f.fg = TERM
    monkeypatch.setattr(line, "new_ui", lambda task: line.LineUI(f, task, today=TODAY))
    r = tools.run_tool("line_prepare", {"chat": "Group A", "text": "hi"})
    assert r["ok"] and line.LineUI.FOCUS_OK in r["result"] and "token=" in r["result"], r
    assert f.fg == TERM


# ---- focus hand-back for every LINE tool (open_chat / cancel / send), remembered non-LINE window ----

def _tools_on(monkeypatch, f):
    monkeypatch.setattr(line, "new_ui", lambda task: line.LineUI(f, task, today=TODAY))


def test_open_chat_tool_restores_focus_and_reports(monkeypatch):
    f = FakeLine(history())
    f.fg = TERM
    _tools_on(monkeypatch, f)
    r = tools.run_tool("line_open_chat", {"chat": "Teem"})
    assert r["ok"] and r["result"].endswith(line.LineUI.FOCUS_OK), r
    assert f.fg == TERM and f.events[-1] == ("focus", TERM) and f.chat == "Teem"
    assert (0x0D,) not in f.keys


def test_cancel_tool_restores_focus_and_reports(monkeypatch):
    f = FakeLine(history())
    f.fg = TERM
    f.box = "left over"
    _tools_on(monkeypatch, f)
    r = tools.run_tool("line_cancel", {})
    assert r["ok"] and f.box == "" and r["result"].endswith(line.LineUI.FOCUS_OK), r
    assert f.fg == TERM and f.events[-1] == ("focus", TERM)


def test_send_tool_restores_after_enter_and_after_refusal(monkeypatch):
    f = FakeLine(history())
    f.fg = TERM
    _tools_on(monkeypatch, f)
    r = tools.run_tool("line_prepare", {"chat": "Group A", "text": "hello"})
    tok = r["result"].split("token=")[1].split()[0]
    f.events.clear()
    r = tools.run_tool("line_send", {"token": tok, "confirmed": True})
    assert r["ok"] and r["result"].endswith(line.LineUI.FOCUS_OK), r
    ev = f.events
    enter = ev.index(("key", (0x0D,)))
    assert ev[0] == ("focus", f.MAIN) and ev.count(("key", (0x0D,))) == 1          # focus LINE first, one Enter
    assert ev.index(("focus", f.MAIN)) < enter and ev[-1] == ("focus", TERM)         # restore comes after the Enter
    assert all(e[0] != "key" for e in ev[enter + 1:]) and ev.index(("focus", TERM)) > enter
    assert f.fg == TERM and len(f.sent) == 1
    # refusal: the armed box is changed behind our back -> checks fail, nothing is sent, focus still goes home
    r = tools.run_tool("line_prepare", {"chat": "Group A", "text": "hello"})
    tok = r["result"].split("token=")[1].split()[0]
    f.box = "tampered"
    f.events.clear()
    r = tools.run_tool("line_send", {"token": tok, "confirmed": True})
    assert r["ok"] is False and len(f.sent) == 1
    assert ("key", (0x0D,)) not in f.events and f.fg == TERM and f.events[-1] == ("focus", TERM)
    assert line.LineUI.FOCUS_FAIL not in r["error"]


def test_prepare_after_open_chat_uses_terminal_remembered_from_before(monkeypatch):
    f = FakeLine(history())
    f.fg = TERM
    _tools_on(monkeypatch, f)
    assert tools.run_tool("line_open_chat", {"chat": "Teem"})["ok"]
    f.fg = f.MAIN                                   # the live failure: LINE was left in front by open_chat
    r = tools.run_tool("line_prepare", {"chat": "Teem", "text": "hi"})
    assert r["ok"] and line.LineUI.FOCUS_OK in r["result"], r
    assert f.fg == TERM and f.events[-1] == ("focus", TERM)
    assert f.box == "hi" and f.sent == [] and (0x0D,) not in f.keys


def test_remembered_fallback_skipped_when_gone_or_line(monkeypatch):
    f = FakeLine(history())
    f.fg = TERM
    _tools_on(monkeypatch, f)
    assert tools.run_tool("line_open_chat", {"chat": "Teem"})["ok"]
    f.fg, f.alive = f.MAIN, set()                   # remembered terminal closed meanwhile
    n = len(f.focus_calls)
    r = tools.run_tool("line_cancel", {})
    assert r["ok"] and r["result"].endswith(line.LineUI.FOCUS_SKIP) and TERM not in f.focus_calls[n:]
    g = FakeLine(history())
    g.fg = g.MAIN
    line._LAST_FG["hwnd"] = g.MAIN                  # a LINE window can never be a hand-back target
    _tools_on(monkeypatch, g)
    r = tools.run_tool("line_cancel", {})
    assert r["ok"] and r["result"].endswith(line.LineUI.FOCUS_SKIP) and g.focus_calls == []
    h = FakeLine(history())
    h.fg = h.MAIN                                   # nothing remembered at all
    line._LAST_FG["hwnd"] = 0
    _tools_on(monkeypatch, h)
    r = tools.run_tool("line_open_chat", {"chat": "Group A"})
    assert r["ok"] and r["result"].endswith(line.LineUI.FOCUS_SKIP) and h.focus_calls == []


def test_yielded_is_not_followed_by_a_hand_back():
    f = FakeLine(history())
    f.fg = TERM

    def boom():
        f.fg = f.MAIN
        raise winio.Yielded("user")
    with pytest.raises(winio.Yielded):
        ui_for(f).run_focused(boom, on_fail=lambda: pytest.fail("no cleanup on Yielded"))
    assert f.fg == f.MAIN and f.focus_calls == []


# =================================== tools / token flow ===================================

def test_line_send_without_confirmed_needs_confirm_and_touches_nothing(monkeypatch):
    def boom(task):
        raise AssertionError("the GUI must not be touched without confirmation")
    monkeypatch.setattr(line, "new_ui", boom)
    tok = line.STORE.put({"exp": {"chat": "Teem", "text": "hi", "n_images": 0, "quote": None}, "extra": {}})
    for args in ({"token": tok}, {"token": tok, "confirmed": False}, {"token": tok, "confirmed": "true"},
                 {"token": tok, "confirmed": 1}):
        r = tools.run_tool("line_send", dict(args))
        assert r["ok"] is False and "needs_confirm" in r and "Teem" in r["needs_confirm"], (args, r)
    assert line.STORE.get(tok) is not None                                 # still usable after the confirm round-trip


def test_line_send_unknown_or_expired_token(monkeypatch):
    monkeypatch.setattr(line, "new_ui", lambda task: pytest.fail("no GUI"))
    r = tools.run_tool("line_send", {"token": "deadbeef", "confirmed": True})
    assert r["ok"] is False and "token" in r["error"]
    t = [0.0]
    line.STORE.clock = lambda: t[0]
    try:
        tok = line.STORE.put({"exp": {"chat": "c", "text": "t", "n_images": 0, "quote": None}, "extra": {}})
        t[0] = 500
        r = tools.run_tool("line_send", {"token": tok, "confirmed": True})
        assert r["ok"] is False and "หมดอายุ" in r["error"]
    finally:
        line.STORE.clock = __import__("time").monotonic


def test_tools_end_to_end_with_fake(monkeypatch, tmp_path):
    f = FakeLine(history())
    monkeypatch.setattr(line, "new_ui", lambda task: line.LineUI(f, task, today=TODAY))
    r = tools.run_tool("line_open_chat", {"chat": "Group A"})
    assert r["ok"], r
    r = tools.run_tool("line_prepare", {"chat": "Group A", "text": "ทดสอบ\nสอง",
                                        "reply_to": {"sender": "Bob", "text": "late, news", "time": "10.10", "date": "วันนี้"}})
    assert r["ok"] and "token=" in r["result"], r
    tok = r["result"].split("token=")[1].split()[0]
    assert f.sent == []
    r = tools.run_tool("line_send", {"token": tok})
    assert r.get("needs_confirm") and f.sent == []
    r = tools.run_tool("line_send", {"token": tok, "confirmed": True})
    assert r["ok"] and len(f.sent) == 1, r
    assert tools.run_tool("line_send", {"token": tok, "confirmed": True})["ok"] is False     # token is one-shot


def test_prepare_validates_arguments(monkeypatch):
    monkeypatch.setattr(line, "new_ui", lambda task: pytest.fail("no GUI"))
    for args in ({}, {"chat": "x"}, {"chat": "x", "text": "a\n"}, {"chat": "x", "text": "a\tb"},
                 {"chat": "x", "image_path": "C:/nope/none.png"}, {"chat": "x", "text": "a", "reply_to": {"sender": "b"}},
                 {"chat": "x", "text": "a", "reply_to": "str"}):
        r = tools.run_tool("line_prepare", dict(args))
        assert r["ok"] is False, args


def test_cancel_closes_quote_and_clears_box(monkeypatch):
    f = FakeLine(history())
    f.armed, f.qname, f.box = True, logic.line_expected_quote("Zed", "x"), "left over"
    monkeypatch.setattr(line, "new_ui", lambda task: line.LineUI(f, task, today=TODAY))
    line.STORE.put({"exp": {}, "extra": {}})
    r = tools.run_tool("line_cancel", {})
    assert r["ok"] and not f.armed and f.box == "" and "ว่าง" in r["result"]
    assert line.STORE._d == {}


# =================================== image helpers ===================================

def test_thumbnail_similarity_uses_exif_orientation(tmp_path):
    im = Image.new("RGB", (200, 100), (255, 255, 255))
    for x in range(100):
        for y in range(100):
            im.putpixel((x, y), (200, 30, 30))                          # left half red
    p = tmp_path / "rot.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6                                                    # rotate 90 CW on display
    im.save(p, exif=exif)
    shown = Image.new("RGB", (100, 200), (255, 255, 255))
    for x in range(100):
        for y in range(100):
            shown.putpixel((x, y), (200, 30, 30))                       # after the rotation red is on TOP
    assert line.thumb_similarity(shown, str(p)) > 0.95
    flipped = shown.transpose(Image.FLIP_TOP_BOTTOM)
    assert line.thumb_similarity(flipped, str(p)) < 0.85


def test_bubble_and_thumb_finders():
    im = Image.new("RGB", (800, 56), (255, 255, 255))
    for x in range(100, 400):
        for y in range(11, 45):
            im.putpixel((x, y), (200, 40, 40))
    pt = line.find_bubble_point(im, (375, 500, 800, 56))
    assert pt is not None and 375 + 100 <= pt[0] <= 375 + 400 and 500 <= pt[1] <= 556
    assert line.find_bubble_point(Image.new("RGB", (800, 56), (255, 255, 255)), (0, 0, 800, 56)) is None
    box = Image.new("RGB", (1000, 60), (255, 255, 255))
    for x in range(10, 70):
        for y in range(5, 55):
            box.putpixel((x, y), (40, 200, 40))
    th = line.find_thumb(box)
    assert th is not None and abs(th.size[0] - 60) <= 2 and abs(th.size[1] - 50) <= 2


def test_find_thumb_narrow_tall_thumbnail_at_left_edge():
    im = Image.new("RGB", (200, 80), (255, 255, 255))
    for x in range(4, 22):                       # 18 px wide, 60 px tall, x=4
        for y in range(10, 70):
            im.putpixel((x, y), (40, 200, 40))
    th = line.find_thumb(im)
    assert th is not None and th.size == (18, 60)
    assert th.getpixel((0, 0)) == (40, 200, 40) and th.getpixel((17, 59)) == (40, 200, 40)


def test_find_thumb_ignores_lone_caret():
    im = Image.new("RGB", (200, 80), (255, 255, 255))
    for y in range(10, 60):                      # 1-px vertical caret only
        im.putpixel((50, y), (0, 0, 0))
    assert line.find_thumb(im) is None


def test_find_thumb_caret_next_to_thumbnail_does_not_extend_crop():
    im = Image.new("RGB", (200, 80), (255, 255, 255))
    for x in range(4, 22):
        for y in range(10, 70):
            im.putpixel((x, y), (40, 200, 40))
    for y in range(20, 50):                      # caret 4 px right of the thumbnail, inside its row band
        im.putpixel((26, y), (0, 0, 0))
    th = line.find_thumb(im)
    assert th is not None and th.size == (18, 60)


# =================================== MCP wrapper ===================================

def test_mcp_drops_confirmed_when_unattended(monkeypatch):
    pytest.importorskip("mcp")
    monkeypatch.setenv("HANDS_PIN", "x")
    import mcp_server
    seen = {}

    class R:
        def read(self):
            return b'{"ok": false, "needs_confirm": "need yes"}'

    def fake_urlopen(req, timeout=0):
        import json
        seen["args"] = json.loads(req.data)["args"]
        return R()

    monkeypatch.setattr(mcp_server.urllib.request, "urlopen", fake_urlopen)
    # Without psutil (or under `claude -p`) the adapter fails safe to unattended; this test covers the env switch.
    monkeypatch.setattr(mcp_server, "_PRINT_MODE", False)
    monkeypatch.delenv("HANDS_UNATTENDED", raising=False)
    out = mcp_server.line_send("abc", True)
    assert seen["args"]["confirmed"] is True and "approved THIS message" in out
    monkeypatch.setenv("HANDS_UNATTENDED", "1")
    out = mcp_server.line_send("abc", True)
    assert "confirmed" not in seen["args"] and "unattended" in out and "never set confirmed" in out
    names = {t for t in ("line_open_chat", "line_prepare", "line_send", "line_cancel") if hasattr(mcp_server, t)}
    assert len(names) == 4


def test_fake_backend_matches_the_real_backend():
    """Every backend call LineUI makes exists on the real Win class with the same parameters as on the fake, so the
    tests above exercise the real call signatures."""
    src = (HERE / "line.py").read_text(encoding="utf-8")
    used = set(re.findall(r"self\.be\.(\w+)", src))
    assert used
    for name in sorted(used):
        assert hasattr(line.Win, name), f"Win.{name} missing"
        assert hasattr(FakeLine, name), f"FakeLine.{name} missing"
        a = list(inspect.signature(getattr(line.Win, name)).parameters)
        b = list(inspect.signature(getattr(FakeLine, name)).parameters)
        assert a == b, (name, a, b)


# =================================== tools.py: Chromium warm-up ===================================

def _warmup_env(monkeypatch, n_nodes):
    clock = {"t": 0.0, "walks": 0, "sleeps": 0}

    def sleep(s):
        clock["t"] += s
        clock["sleeps"] += 1

    def walk(root, **kw):
        clock["t"] += 0.4                                        # every UIA walk costs 0.4 s
        clock["walks"] += 1
        return ([NS(ctype="Text", name="n", rect=(0, 0, 1, 1), autoid="", enabled=True, el=None)] * n_nodes, False)

    monkeypatch.setattr(tools, "_uia", NS(walk=walk, root_for=lambda h: None, INTERACTIVE=set()))
    monkeypatch.setattr(tools, "time", NS(monotonic=lambda: clock["t"], sleep=sleep))
    monkeypatch.setattr(tools, "resolve_window", lambda spec: {"hwnd": 5, "title": "t", "process": "p.exe", "pid": 1,
                                                               "monitor": "MAIN", "rect": (0, 0, 1, 1)})
    monkeypatch.setattr(tools, "check_forbidden", lambda w: None)
    monkeypatch.setattr(tools.win32gui, "GetClassName", lambda h: "Chrome_WidgetWin_1")
    tools._CHROMIUM_WARM.discard(5)
    return clock


def test_chromium_warmup_deadline_includes_walk_time(monkeypatch):
    clock = _warmup_env(monkeypatch, 3)                          # tree stays tiny: poll, but never past ~3 s
    tools.t_find_elements({"query": "x"}, FakeTask())
    assert clock["t"] <= 3.0 + 0.4 and clock["walks"] >= 3        # old code: 8 rounds of 0.5 s + walks = 7.6 s
    tools._CHROMIUM_WARM.discard(5)


def test_chromium_warmup_stops_immediately_for_a_populated_tree(monkeypatch):
    clock = _warmup_env(monkeypatch, 40)                         # > 15 nodes: no polling at all
    tools.t_find_elements({"query": "x"}, FakeTask())
    assert clock["sleeps"] == 0 and clock["walks"] == 1
    tools._CHROMIUM_WARM.discard(5)


def test_tools_registered():
    for n in ("line_open_chat", "line_prepare", "line_send", "line_cancel"):
        assert n in tools.TOOLS


# ---------------------------------------------------------------------------------------------------------
# post-send DB audit (subprocess mocked; nothing real is read)
# ---------------------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _audit_off_by_default(monkeypatch):
    monkeypatch.setenv("HANDS_LINE_DB_AUDIT", "0")          # the GUI-fake tests must never spawn the real reader


SENT_MS = 1795055700000
OWN = "sender:618ede825d0ff444dcbc076d"


def _msg(text, sender=OWN, ts=SENT_MS + 2359, related=None, ctype=0):
    return {"sender": "me", "senderRef": sender, "sourceTimestamp": ts, "text": text, "relatedSourceRef": related,
            "contentType": ctype}


def _audit_env(monkeypatch, tmp_path, replies):
    """replies: list of subprocess outcomes (dict result / Exception / raw stdout str); the last one repeats."""
    py, sc = tmp_path / "py.exe", tmp_path / "reader.py"
    py.write_text("x")
    sc.write_text("x")
    monkeypatch.setenv("HANDS_LINE_DB_AUDIT", "1")
    monkeypatch.setenv("HANDS_LINE_READER_PY", str(py))
    monkeypatch.setenv("HANDS_LINE_READER_SCRIPT", str(sc))
    calls = {"n": 0, "scopes": [], "env": []}

    def fake_run(cmd, **kw):
        i = min(calls["n"], len(replies) - 1)
        calls["n"] += 1
        calls["scopes"].append(json.loads(kw["input"]))
        calls["env"].append(kw["env"])
        assert kw.get("shell") is False and kw.get("encoding") == "utf-8" and kw.get("timeout") == 10.0
        r = replies[i]
        if isinstance(r, Exception):
            raise r
        out = r if isinstance(r, str) else json.dumps(r)
        return NS(stdout=out, stderr="", returncode=0)

    monkeypatch.setattr(line.subprocess, "run", fake_run)
    monkeypatch.setattr(line.time, "sleep", lambda s: None)
    return calls


def _ok(*msgs):
    return {"ok": True, "ownSenderRef": OWN, "messages": list(msgs)}


def _seg(text="hello", **kw):
    return line.db_audit_segment({"chat": "Chat A", "text": text, "n_images": 0, "quote": kw.get("quote")}, SENT_MS)


def test_audit_confirmed(monkeypatch, tmp_path):
    calls = _audit_env(monkeypatch, tmp_path, [_ok(_msg("hello"))])
    assert _seg() == "DB: ยืนยัน (09:35:02 เวลาไทย)"          # 10:35:02 Taipei -> 09:35:02 Thai
    s = calls["scopes"][0]
    assert s["query"] == "hello" and s["chatName"] == "Chat A" and s["chatType"] == "auto" and s["mediaMode"] == "metadata"
    assert s["dateFrom"] <= s["dateTo"] and calls["env"][0]["LINE_MCP_EXTENSIONS"] == "1" and "CHAT_LOG_ON" not in calls["env"][0]


def test_audit_not_found_retries_then_reports(monkeypatch, tmp_path):
    calls = _audit_env(monkeypatch, tmp_path, [_ok()])
    assert _seg() == "DB: ไม่พบข้อความ" and calls["n"] == len(line.AUDIT_RETRY_AT)


def test_audit_late_flush_found_on_retry(monkeypatch, tmp_path):
    calls = _audit_env(monkeypatch, tmp_path, [_ok(), _ok(), _ok(_msg("hello"))])
    assert _seg().startswith("DB: ยืนยัน") and calls["n"] == 3


def test_audit_reader_error_codes(monkeypatch, tmp_path):
    _audit_env(monkeypatch, tmp_path, [{"ok": False, "code": "LINE_BUILD_UNVERIFIED"}])
    assert _seg() == "DB: ตรวจไม่ได้ (LINE_BUILD_UNVERIFIED)"
    _audit_env(monkeypatch, tmp_path, ["not json"])
    assert _seg().startswith("DB: ตรวจไม่ได้ (BAD_JSON")
    _audit_env(monkeypatch, tmp_path, [OSError("boom")])
    assert _seg() == "DB: ตรวจไม่ได้ (OSError)"


def test_audit_timeout(monkeypatch, tmp_path):
    calls = _audit_env(monkeypatch, tmp_path, [line.subprocess.TimeoutExpired("x", 10)])
    assert _seg() == "DB: ตรวจไม่ได้ (TIMEOUT)" and calls["n"] == 1      # no retry storm after a timeout


def test_audit_reader_missing(monkeypatch, tmp_path):
    _audit_env(monkeypatch, tmp_path, [_ok(_msg("hello"))])
    monkeypatch.setenv("HANDS_LINE_READER_SCRIPT", str(tmp_path / "nope.py"))
    assert _seg() == "DB: ตรวจไม่ได้ (READER_MISSING)"


def test_audit_wrong_sender_and_old_timestamp_not_confirmed(monkeypatch, tmp_path):
    _audit_env(monkeypatch, tmp_path, [_ok(_msg("hello", sender="sender:other"))])
    assert _seg() == "DB: ไม่พบข้อความ"
    _audit_env(monkeypatch, tmp_path, [_ok(_msg("hello", ts=SENT_MS - 6000))])         # older than the slack
    assert _seg() == "DB: ไม่พบข้อความ"
    _audit_env(monkeypatch, tmp_path, [_ok(_msg("hello", ts=SENT_MS - 4000))])         # within clock slack
    assert _seg().startswith("DB: ยืนยัน")


def test_audit_text_must_match_fully(monkeypatch, tmp_path):
    _audit_env(monkeypatch, tmp_path, [_ok(_msg("hello world"))])
    assert _seg("hello") == "DB: ไม่พบข้อความ"


def test_audit_multiline_uses_longest_line_and_normalises_newlines(monkeypatch, tmp_path):
    text = "สวัสดี\nบรรทัดที่ยาวที่สุดของข้อความ\nสาม"
    calls = _audit_env(monkeypatch, tmp_path, [_ok(_msg("สวัสดี\r\nบรรทัดที่ยาวที่สุดของข้อความ\r\nสาม"))])
    assert _seg(text).startswith("DB: ยืนยัน")
    assert calls["scopes"][0]["query"] == "บรรทัดที่ยาวที่สุดของข้อความ"
    _audit_env(monkeypatch, tmp_path, [_ok(_msg("สวัสดี\nบรรทัดที่ยาวที่สุดของข้อความ\nสาม"))])
    assert _seg(text).startswith("DB: ยืนยัน")


def test_audit_quote_reports_related_ref(monkeypatch, tmp_path):
    _audit_env(monkeypatch, tmp_path, [_ok(_msg("hello", related="message:abc"))])
    assert "มี quote ผูก" in _seg(quote={"sender": "Bob"})
    _audit_env(monkeypatch, tmp_path, [_ok(_msg("hello"))])
    assert "ไม่เห็น quote ผูก" in _seg(quote={"sender": "Bob"})


def _img_seg():
    return line.db_audit_segment({"chat": "Chat A", "text": "", "n_images": 1}, SENT_MS)


def test_audit_image_only_confirmed_by_own_image_message(monkeypatch, tmp_path):
    calls = _audit_env(monkeypatch, tmp_path, [_ok(_msg("", ts=SENT_MS + 2359, ctype=1))])
    assert _img_seg() == "DB: ยืนยัน (09:35:02 เวลาไทย, รูป)"
    s = calls["scopes"][0]
    assert "query" not in s and s["messageLimit"] == 10 and s["chatName"] == "Chat A" and s["mediaMode"] == "metadata"


def test_audit_image_only_older_image_is_notfound(monkeypatch, tmp_path):
    calls = _audit_env(monkeypatch, tmp_path, [_ok(_msg("", ts=SENT_MS - 60_000, ctype=1))])
    assert _img_seg() == "DB: ไม่พบรูป" and calls["n"] == len(line.AUDIT_RETRY_AT)


def test_audit_image_only_other_sender_is_notfound(monkeypatch, tmp_path):
    _audit_env(monkeypatch, tmp_path, [_ok(_msg("", sender="sender:other", ctype=1))])
    assert _img_seg() == "DB: ไม่พบรูป"


def test_audit_image_only_text_message_is_not_a_match(monkeypatch, tmp_path):
    _audit_env(monkeypatch, tmp_path, [_ok(_msg("hello"))])
    assert _img_seg() == "DB: ไม่พบรูป"


def test_audit_image_only_reader_error(monkeypatch, tmp_path):
    _audit_env(monkeypatch, tmp_path, [{"ok": False, "code": "LINE_BUILD_UNVERIFIED"}])
    assert _img_seg() == "DB: ตรวจไม่ได้ (LINE_BUILD_UNVERIFIED)"
    _audit_env(monkeypatch, tmp_path, [OSError("boom")])
    assert _img_seg() == "DB: ตรวจไม่ได้ (OSError)"


def test_audit_image_only_disabled_and_never_raises(monkeypatch, tmp_path):
    calls = _audit_env(monkeypatch, tmp_path, [_ok()])
    monkeypatch.setenv("HANDS_LINE_DB_AUDIT", "0")
    assert line.db_audit_segment({"chat": "c", "text": "hello", "n_images": 0}, SENT_MS) is None and calls["n"] == 0
    monkeypatch.setenv("HANDS_LINE_DB_AUDIT", "1")
    monkeypatch.setattr(line, "_read_scope", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert _img_seg() == "DB: ตรวจไม่ได้ (RuntimeError)"


def test_audit_never_raises(monkeypatch, tmp_path):
    _audit_env(monkeypatch, tmp_path, [_ok()])
    monkeypatch.setattr(line, "_read_scope", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert _seg() == "DB: ตรวจไม่ได้ (RuntimeError)"
    monkeypatch.setattr(line, "db_audit_sent", lambda *a, **k: (_ for _ in ()).throw(ValueError("y")))
    assert _seg() == "DB: ตรวจไม่ได้ (ValueError)"


def test_send_appends_db_segment(monkeypatch, tmp_path):
    f = FakeLine(history())
    ui = ui_for(f)
    exp, extra, _ = ui.prepare("Group A", "hello", None, None)
    _audit_env(monkeypatch, tmp_path, [_ok(_msg("hello", ts=int(__import__("time").time() * 1000)))])
    out = ui.send(exp, extra)
    assert "กด Enter แล้ว 1 ครั้ง" in out and out.endswith(" เวลาไทย)") and "| DB: ยืนยัน" in out


# ---------------------------------------------------------------------------------------------------------
# speed work: snapshot reuse / invalidation, polling instead of naps, focus skip, parallel DB audit
# ---------------------------------------------------------------------------------------------------------

class RecTask(FakeTask):
    """A task that records every sleep (what the polling loops wait)."""

    def __init__(self):
        self.sleeps = []

    def sleep(self, secs):
        self.sleeps.append(secs)


def rec_ui(fake, **kw):
    t = RecTask()
    return line.LineUI(fake, t, today=TODAY, **kw), t


def _inputs(f):
    """Every input action the proxy must treat as 'the screen may have changed'."""
    return {
        "press": lambda ui: ui.be.press([0x41], ui.task),
        "type_line": lambda ui: ui.be.type_line("x", ui.task),
        "click": lambda ui: ui.be.click(900, 500, "left", ui.task, 7),
        "wheel": lambda ui: ui.be.wheel_event(900, 500, "up", 1, ui.task),
        "bring_to_front": lambda ui: ui.be.bring_to_front(f.MAIN),
        "set_focus": lambda ui: ui.be.set_focus(El("edit", lambda: (0, 0, 1, 1))),
        "move_cursor": lambda ui: ui.be.move_cursor(1, 1),
    }


def test_snapshot_reuse_only_without_input():
    f = FakeLine(history())
    ui = ui_for(f)
    a = ui.snapshot()
    b = ui.snapshot(reuse=True)
    assert f.walks == 1 and b.edit is a.edit and b.header == a.header     # nothing happened: one walk serves both
    ui.snapshot()                                                         # reuse is opt-in: a plain call always walks
    assert f.walks == 2


@pytest.mark.parametrize("action", ["press", "type_line", "click", "wheel", "bring_to_front", "set_focus", "move_cursor"])
def test_every_input_action_invalidates_the_snapshot(action):
    f = FakeLine(history())
    ui = ui_for(f)
    ui.snapshot()
    _inputs(f)[action](ui)
    before = f.walks
    ui.snapshot(reuse=True)
    assert f.walks == before + 1, action                                  # taken after the action, never the old one
    ui.snapshot(reuse=True)
    assert f.walks == before + 1                                          # and that new one is reusable again


def test_failed_input_also_invalidates():
    f = FakeLine(history())
    ui = ui_for(f)
    ui.snapshot()

    def boom(vks, task):
        raise RuntimeError("half-way")
    f.press = boom
    with pytest.raises(RuntimeError):
        ui.be.press([0x41], ui.task)
    ui.snapshot(reuse=True)
    assert f.walks == 2


def test_snapshot_not_reused_when_foreground_rect_age_or_dialog_changed():
    f = FakeLine(history())
    now = {"t": 100.0}
    ui = ui_for(f, clock=lambda: now["t"])
    ui.snapshot()
    f.fg = 500                                                            # the user switched window (no input of ours)
    ui.snapshot(reuse=True)
    assert f.walks == 2
    now["t"] += line.SNAP_MAX_AGE + 0.1                                   # too old
    ui.snapshot(reuse=True)
    assert f.walks == 3
    orig = f.line_windows
    f.line_windows = lambda hidden=False: [dict(w, rect=(0, 0, 1400, 1000)) if w["hwnd"] == f.MAIN else w
                                           for w in orig(hidden)]
    ui.snapshot(reuse=True)                                               # window resized
    assert f.walks == 4
    f.line_windows = orig
    ui.snapshot()
    f.main_enabled = False                                                # a dialog appeared meanwhile
    with pytest.raises(ToolError):
        ui.snapshot(reuse=True)                                           # reuse never skips the enabled check
    assert ui.snapshot(need_enabled=False, reuse=True).enabled is False   # observe() reads it as data


def test_observe_never_uses_a_snapshot_from_before_the_last_input():
    """The check after an input must see the result of that input (header changed by a click here)."""
    f = FakeLine(history(), chats=("Group A", "Teem"))
    ui = ui_for(f)
    exp = {"chat": "Group A", "text": "", "n_images": 0, "quote": None}
    snap = ui.snapshot()
    assert ui.observe(exp, {})[2] == []                                   # all good, from the cached walk
    assert f.walks == 1
    ui.be.click(10 + 5, 120 + 5, "left", ui.task, 7)                      # a click ...
    f.chat = "Teem"                                                       # ... and whatever LINE did with it
    _, obs, problems = ui.observe(exp, {})
    assert obs["header"] == "Teem" and any("Teem" in p for p in problems)
    assert f.walks == 2 and snap.header == "Group A"


def test_focus_is_skipped_when_line_is_foreground_and_box_focused():
    f = FakeLine(history())
    ui = ui_for(f)
    snap = ui.snapshot()
    gen = ui._gen
    ui.focus(snap)
    assert f.events == [] and f.focus_calls == [] and ui._gen == gen       # nothing brought forward, nothing focused
    ui.snapshot(reuse=True)
    assert f.walks == 1                                                   # and the snapshot stayed valid


def test_focus_acts_when_not_foreground_or_not_focused_and_guard_still_decides():
    f = FakeLine(history())
    ui, task = rec_ui(f)
    snap = ui.snapshot()
    f.fg, f.focus_id = 500, "search"
    ui.focus(snap)
    assert f.focus_calls == [f.MAIN] and f.focus_id == "edit" and f.fg == f.MAIN
    assert task.sleeps == []                                              # conditions held at once: no nap at all
    f2 = FakeLine(history())
    ui2, task2 = rec_ui(f2)
    snap2 = ui2.snapshot()
    f2.focus_id = "search"
    f2.set_focus = lambda el: None                                        # focus never lands
    with pytest.raises(ToolError):
        ui2.focus(snap2)
    assert sum(task2.sleeps) >= 0.1                                       # waited at least the old 2 x 0.05 s


def test_open_chat_polls_exit_early_and_time_out_like_before():
    f = FakeLine(history(), chats=("Group A", "Teem"))
    ui, task = rec_ui(f)
    assert ui.open_chat("Teem").header == "Teem"
    assert task.sleeps == [0.05]                  # old code: 3 fixed sleeps (2 search reads + 1 header read)
    assert f.walks == 4                           # first read, 2 search reads (stable), 1 header read
    g = FakeLine(history(), chats=("Group A",))
    ui, task = rec_ui(g)
    with pytest.raises(ToolError):
        ui.open_chat("Nobody")
    assert task.sleeps == [0.05] * 60             # the same worst case wait as the old 60 x sleep(0.05)
    h = FakeLine(history(), chats=("Group A", "Teem"))
    h.click = lambda x, y, b, t, pid=None: h.clicks.append((x, y, b))      # the click never opens the chat
    ui, task = rec_ui(h)
    with pytest.raises(ToolError) as e:
        ui.open_chat("Teem")
    assert "หัวแชท" in str(e.value) and task.sleeps == [0.05] + [0.05] * 60


def test_clear_box_and_type_message_poll_instead_of_nap(monkeypatch):
    f = FakeLine(history())
    f.box = "old"
    naps = []
    f.nap = lambda s: naps.append(s)
    ui, task = rec_ui(f)
    snap = ui.snapshot()
    ui.clear_box(snap)
    assert f.box == "" and task.sleeps == []                              # old code: always one 0.05 s sleep
    ui.type_message(snap, "hi", expect="hi")
    assert naps == [] and f.box == "hi"                                   # old code: always a 0.2 s nap
    ui.type_message(snap, "!", expect="never")                            # never reaches it: waits, then goes on
    assert naps and f.box == "hi!"
    monkeypatch.setattr(tools, "_FAST", True)
    n = len(naps)
    t0 = __import__("time").monotonic()
    ui.type_message(snap, "?", expect="never")
    assert len(naps) > n and __import__("time").monotonic() - t0 < 0.5
    ui.type_message(snap, "z")                                            # no expectation: the old fixed nap
    assert naps[-1] == 0.05


def test_prepare_on_open_chat_walks_twice_not_five_times():
    f = FakeLine(history())
    f.fg = TERM
    ui = ui_for(f)
    ui.prepare("Group A", "hello", None, None)
    assert f.walks == 2 and f.box == "hello"      # first read (header + quote + box all proven by it) + the one after typing


def test_prepare_on_closed_chat_opens_it_itself_without_redundant_walks():
    f = FakeLine(history(), chats=("Group A", "Teem"))
    f.items = [("date", "วันนี้"), ("msg", "Bob", "hi", "9.00", "red")]
    f.fg = TERM
    ui, task = rec_ui(f)
    exp, extra, obs = ui.prepare("Teem", "hello", None, None)
    assert f.chat == "Teem" and f.box == "hello" and obs["header"] == "Teem"
    # first read, 2 search reads, 1 header read (verified, then reused by _prepare), 1 after typing
    assert f.walks == 5 and f.typed == ["Teem", "hello"]
    assert task.sleeps == [0.05]


def test_prepare_tool_on_closed_chat_needs_no_open_chat_call(monkeypatch):
    f = FakeLine(history(), chats=("Group A", "Teem"))
    f.fg = TERM
    _tools_on(monkeypatch, f)
    r = tools.run_tool("line_prepare", {"chat": "Teem", "text": "สวัสดี"})
    assert r["ok"] and "token=" in r["result"] and f.chat == "Teem" and f.box == "สวัสดี", r
    assert r["result"].count(line.LineUI.FOCUS_OK) == 1 and f.fg == TERM


def test_send_walks_once_before_enter_and_only_the_pane_after():
    f = FakeLine(history())
    exp, extra, _ = ui_for(f).prepare("Group A", "hello", None, None)
    f.fg = TERM
    f.walks = f.subwalks = 0
    f.events.clear()
    ui = ui_for(f)
    out = ui.send(exp, extra)
    assert f.walks == 1 and f.subwalks == 1       # old: 3 full walks (check, observe, post-Enter)
    assert f.sent == [("Group A", "hello", False, None)] and "กล่องว่างแล้ว" in out and "ตรงกับข้อความ" in out
    assert f.events[0] == ("focus", f.MAIN)       # LINE is brought forward before the walk the check relies on


def test_send_falls_back_to_a_full_walk_when_the_cached_pane_is_stale():
    f = FakeLine(history())
    exp, extra, _ = ui_for(f).prepare("Group A", "hello", None, None)
    f.walks = 0
    f.pane_alive = False                          # the cached container element died: subtree finds nothing
    out = ui_for(f).send(exp, extra)
    assert f.subwalks == 1 and f.walks == 2       # pre-Enter walk + the fallback full walk
    assert "ตรงกับข้อความ" in out and f.keys.count((0x0D,)) == 1


class _LateRow(FakeLine):
    """The new row shows up in the UIA tree only after `delay` post-Enter reads (LINE repaints late)."""
    delay, hold = 2, None

    def press(self, vks, task):
        super().press(vks, task)
        if tuple(vks) == (0x0D,):
            self.hold = self.items.pop()

    def tree(self, hwnd):
        if self.hold is not None:
            if self.delay <= 0:
                self.items.append(self.hold)
                self.hold = None
            else:
                self.delay -= 1
        return super().tree(hwnd)


def test_post_send_poll_exits_when_the_row_appears_and_waits_as_long_as_before():
    f = _LateRow(history())
    f.pane_alive = False                          # full walks only: one read == one tree() call
    exp, extra, _ = ui_for(f).prepare("Group A", "hello", None, None)
    naps = []
    f.nap = lambda s: naps.append(s)
    out = ui_for(f).send(exp, extra)
    assert len(naps) == 2 and "ตรงกับข้อความ" in out                      # two misses, then the row: no fixed 0.15 s naps
    g = _LateRow(history())
    g.pane_alive = False
    g.delay = 10 ** 6                             # the row never shows
    exp, extra, _ = ui_for(g).prepare("Group A", "hello", None, None)
    naps2 = []
    g.nap = lambda s: naps2.append(s)
    out = ui_for(g).send(exp, extra)
    assert len(naps2) == line.POST_SEND_POLLS and sum(naps2) >= 12 * 0.15 - 1e-9     # >= the old 1.8 s of waiting
    assert "ยังไม่ยืนยันว่าตรง" in out and g.keys.count((0x0D,)) == 1                 # still exactly one Enter


# ---- parallel DB audit ----

def test_db_audit_runs_in_parallel_with_the_gui_post_check(monkeypatch):
    import threading
    f = FakeLine(history())
    exp, extra, _ = ui_for(f).prepare("Group A", "hello", None, None)
    gui_polled = threading.Event()
    started = threading.Event()
    orig_sub = f.subtree

    def sub(el):
        if f.sent:
            gui_polled.set()                       # the post-Enter GUI read happens ...
        return orig_sub(el)
    f.subtree = sub

    def audit(e, ms, t_send=None):
        started.set()
        assert gui_polled.wait(5), "audit was not started before the GUI post-check finished"
        return "DB: ยืนยัน (09:00:00 เวลาไทย)"
    monkeypatch.setattr(line, "db_audit_segment", audit)
    out = ui_for(f).send(exp, extra)
    assert started.is_set() and out.endswith(" | DB: ยืนยัน (09:00:00 เวลาไทย)") and "กล่องว่างแล้ว" in out


def test_db_audit_overlaps_the_focus_hand_back_in_the_tool(monkeypatch):
    import time as _t
    f = FakeLine(history())
    f.fg = TERM
    _tools_on(monkeypatch, f)
    r = tools.run_tool("line_prepare", {"chat": "Group A", "text": "hello"})
    tok = r["result"].split("token=")[1].split()[0]
    f.fg = TERM
    seen = {}

    def audit(e, ms, t_send=None):
        end = _t.monotonic() + 5
        while f.fg != TERM and _t.monotonic() < end:        # waits for the hand-back to the terminal
            _t.sleep(0.005)
        seen["fg"] = f.fg
        return "DB: ยืนยัน (10:00:00 เวลาไทย)"
    monkeypatch.setattr(line, "db_audit_segment", audit)
    r = tools.run_tool("line_send", {"token": tok, "confirmed": True})
    assert r["ok"], r
    assert seen["fg"] == TERM                              # the audit only finished once focus was home: it overlapped
    res = r["result"]
    assert res.index("กด Enter แล้ว 1 ครั้ง") < res.index("| DB: ยืนยัน") < res.index(line.LineUI.FOCUS_OK)


def test_db_audit_thread_never_raises_or_hangs(monkeypatch):
    import threading
    monkeypatch.setattr(line, "db_audit_segment", lambda e, ms, t_send=None: "DB: x")
    assert line._AuditJob({}, 1, 0.0).result() == "DB: x"

    def boom(e, ms, t_send=None):
        raise RuntimeError("x")
    monkeypatch.setattr(line, "db_audit_segment", boom)
    assert line._AuditJob({}, 1, 0.0).result() == "DB: ตรวจไม่ได้ (RuntimeError)"

    gate = threading.Event()
    monkeypatch.setattr(line, "db_audit_segment", lambda e, ms, t_send=None: gate.wait(5) and "late")
    hung = line._AuditJob({}, 1, 0.0)
    assert hung.result(timeout=0.05) == "DB: ตรวจไม่ได้ (TIMEOUT)"
    gate.set()
    assert line._audit_tail(None) == ""
    monkeypatch.setattr(line, "db_audit_segment", lambda e, ms, t_send=None: None)    # audit disabled -> no segment
    assert line._audit_tail(line._AuditJob({}, 1, 0.0)) == ""


def test_send_still_reports_when_the_audit_blows_up(monkeypatch):
    f = FakeLine(history())
    exp, extra, _ = ui_for(f).prepare("Group A", "hello", None, None)
    monkeypatch.setattr(line, "db_audit_segment", lambda e, ms, t_send=None: (_ for _ in ()).throw(ValueError("y")))
    out = ui_for(f).send(exp, extra)
    assert "กด Enter แล้ว 1 ครั้ง" in out and out.endswith("| DB: ตรวจไม่ได้ (ValueError)") and len(f.sent) == 1


def test_audit_retry_schedule_is_measured_from_the_send(monkeypatch, tmp_path):
    waits = []
    _audit_env(monkeypatch, tmp_path, [_ok()])               # never found: walks the whole schedule
    monkeypatch.setattr(line.time, "sleep", lambda s: waits.append(round(s, 1)))
    t_send = line.time.monotonic() - 2.0                     # Enter was pressed 2 s ago (sleep is mocked: no time passes)
    assert line.db_audit_sent("Chat A", "hello", SENT_MS, t_send=t_send) == ("notfound",)
    assert waits == [1.0]                                    # only the 3.0 s attempt still has to wait (3.0 - 2.0)
    waits.clear()
    assert line.db_audit_sent("Chat A", "hello", SENT_MS) == ("notfound",)      # default: measured from the call
    assert waits == [0.7, 1.5, 3.0]


# =================================== English UI (LINE 26.6, strings captured live 2026-11-19) ===================================

def en_ui(**kw):
    f = FakeLineEN(history_en(), chats=("Group A", "Teem", "Keep Memo"), **kw)
    return f, ui_for(f)


@pytest.mark.parametrize("raw,want", [("7:57 PM", "19.57"), ("12:10 PM", "12.10"), ("10:34 AM", "10.34"),
                                      ("12:05 AM", "0.05"), ("1:00 AM", "1.00"), ("11:59 PM", "23.59"),
                                      ("7:57 PM", "19.57"), ("7:57 pm", "19.57"),
                                      ("19:57", "19.57"), ("09:05", "9.05"), ("9.35 น.", "9.35"), ("13.18", "13.18"),
                                      ("0:30", "0.30")])
def test_norm_time_both_locales_and_24h(raw, want):
    assert logic.line_norm_time(raw) == want


def test_norm_time_rejects_garbage():
    for bad in ["", "noon", "13:18 PM", "0:30 AM", "7", None]:
        assert logic.line_norm_time(bad) == "", bad


def test_en_dates():
    for ok in ["November 19, 2026", "November 14, 2026", "January 1, 2025", "Today", "Yesterday"]:
        assert logic.line_is_date(ok), ok
    for bad in ["Nov 19, 2026", "November 2026", "November 19", "Bob, November 19, 2026, 9:00 AM", "Todays", "Unread messages below"]:
        assert not logic.line_is_date(bad), bad
    assert logic.line_date_key("November 19, 2026") == (2026, 11, 19)
    assert logic.line_date_key("February 30, 2026") is None
    assert logic.line_date_key("Today", TODAY) == (2026, 11, 18) and logic.line_date_key("Yesterday", TODAY) == (2026, 11, 17)
    assert logic.line_date_key("today", TODAY) == (2026, 11, 18)
    # ISO is accepted as reply_to input (never as a separator)
    assert logic.line_date_key("2026-11-19") == (2026, 11, 19) and logic.line_date_key("2026-02-30") is None
    assert logic.line_date_key("2569-11-19") == (2026, 11, 19)
    assert not logic.line_is_date("2026-11-19")
    assert logic.line_date_key("วันนี้", TODAY) == (2026, 11, 18)            # Thai forms keep working next to them


def test_en_message_rows_from_the_live_sample():
    r = logic.line_parse_row("Alicewong, ส่งถามช่างยัง, 7:57 PM")
    assert (r.kind, r.sender_guess, r.body_guess, r.time) == ("msg", "Alicewong", "ส่งถามช่างยัง", "19.57")
    assert logic.line_row_matches(r, "Alicewong", "ส่งถามช่างยัง", "19:57")
    assert logic.line_row_matches(r, "Alicewong", "ส่งถามช่างยัง", "7:57 PM")
    assert not logic.line_row_matches(r, "Alicewong", "ส่งถามช่างยัง", "7:57")          # 7 AM is not 7 PM
    own = logic.line_parse_row("รับทราบค่ะ ส่งของแล้ว, 4:50 PM, Read")             # own row with the read receipt
    assert (own.kind, own.rest, own.time) == ("msg", "รับทราบค่ะ ส่งของแล้ว", "16.50")
    assert logic.line_row_matches(own, None, "รับทราบค่ะ ส่งของแล้ว", "16:50")
    no_read = logic.line_parse_row("รับทราบค่ะ ส่งของแล้ว, 4:50 PM")              # ", Read" is optional
    assert no_read.rest == own.rest and no_read.time == own.time
    both = logic.line_parse_row("ok, then, 12:10 PM, Read")
    assert both.rest == "ok, then" and both.time == "12.10" and logic.line_row_matches(both, None, "ok, then")
    am = logic.line_parse_row("Alicewong, hi, 10:34 AM")
    assert am.time == "10.34"
    grp = logic.line_parse_row("Bob, seen by many, 9:00 AM, Read 3")                        # GUESS: group read count
    assert grp.rest == "Bob, seen by many" and grp.time == "9.00"


def test_en_row_without_ampm_is_not_a_message():
    assert logic.line_parse_row("meet at, 5:30").kind == "unknown"       # a body that ends like a 24 h time stays a body


def test_en_reply_rows():
    name = ("A reply from Alicewong to Shop Desk's message. Shop Desk's original message: ใช่ค่ะ รุ่นนี้, old, "
            "ตอบ, 10:56 AM")
    r = logic.line_parse_row(name)
    assert r.kind == "reply" and r.reply_sender == "Alicewong" and r.time == "10.56"
    assert logic.line_row_matches(r, "Alicewong", "ตอบ") and not logic.line_row_matches(r, "Shop Desk", "ตอบ")
    own = logic.line_parse_row("A reply to Bob's message. Bob's original message: q, mine, 4:50 PM, Read")   # GUESS own form
    assert own.kind == "reply" and own.reply_sender is None and logic.line_row_matches(own, None, "mine")


LIVE_PLAIN = "ทดสอบระบบส่งใหม่ของ bot ไม่ต้องตอบครับ, 1:14 PM"
LIVE_QUOTE_ORIG = "แคปหน้าออกพาร์ทส่งมาให้ดูหน่อย"
LIVE_QUOTE_BODY = "ทดสอบตอบกลับแบบ quote ด้วยระบบใหม่ ไม่ต้องตอบครับ"
LIVE_QUOTE = (f"A reply to Alicewong's message. Alicewong's original message: {LIVE_QUOTE_ORIG}. "
              f"Reply: {LIVE_QUOTE_BODY}., 1:14 PM, ")


def test_en_quote_reply_live_rows_parse_and_match():
    p = logic.line_parse_row(LIVE_PLAIN)
    assert p.kind == "msg" and p.time == "13.14" and logic.line_row_matches(p, None, "ทดสอบระบบส่งใหม่ของ bot ไม่ต้องตอบครับ")
    q = logic.line_parse_row(LIVE_QUOTE)
    assert (q.kind, q.reply_sender, q.time) == ("reply", None, "13.14")
    assert q.quoted == LIVE_QUOTE_ORIG and q.body_guess == LIVE_QUOTE_BODY           # one "." stripped from each
    assert logic.line_row_matches(q, None, LIVE_QUOTE_BODY)                          # the reply text
    assert logic.line_row_matches(q, None, LIVE_QUOTE_BODY, "13:14") and not logic.line_row_matches(q, None, LIVE_QUOTE_BODY, "1:14")
    assert not logic.line_row_matches(q, None, LIVE_QUOTE_ORIG)                      # never the quoted original
    assert not logic.line_row_matches(q, "Alicewong", LIVE_QUOTE_BODY)              # own row: no sender
    # the same row with a read receipt, and with "Read 3": the receipt part does not change the parse
    for tail in (", Read", ", Read 3"):
        r = logic.line_parse_row(LIVE_QUOTE[:-2] + tail)
        assert r.kind == "reply" and r.time == "13.14" and logic.line_row_matches(r, None, LIVE_QUOTE_BODY)
    # the sent text itself ends with "." -> LINE shows ".." -> the sent text (with its ".") matches, the stripped one does not
    dot = logic.line_parse_row(LIVE_QUOTE.replace(f"{LIVE_QUOTE_BODY}.,", f"{LIVE_QUOTE_BODY}..,"))
    assert dot.body_guess == LIVE_QUOTE_BODY + "." and logic.line_row_matches(dot, None, LIVE_QUOTE_BODY + ".")
    assert not logic.line_row_matches(dot, None, LIVE_QUOTE_BODY)


def test_en_quote_reply_from_the_other_side():
    name = ("A reply from Alicewong to Shop Desk's message. Shop Desk's original message: ใช่ค่ะ รุ่นนี้. "
            "Reply: ตอบ., 10:56 AM")
    r = logic.line_parse_row(name)
    assert r.kind == "reply" and r.reply_sender == "Alicewong" and r.time == "10.56" and r.quoted == "ใช่ค่ะ รุ่นนี้"
    assert logic.line_row_matches(r, "Alicewong", "ตอบ") and logic.line_row_matches(r, "Alicewong", "ตอบ.")
    assert not logic.line_row_matches(r, "Shop Desk", "ตอบ") and not logic.line_row_matches(r, None, "ตอบ")
    assert not logic.line_row_matches(r, "Alicewong", "ใช่ค่ะ รุ่นนี้")


def test_thai_quote_reply_and_plain_rows_unchanged():
    name = "Bob ตอบกลับข้อความของ Al ข้อความเดิมของ Al คือ เดิม, จริง, ตอบ, 9.05 น., "      # empty receipt part too
    r = logic.line_parse_row(name)
    assert r.kind == "reply" and r.reply_sender == "Bob" and r.time == "9.05" and r.reply_raw is None
    assert logic.line_row_matches(r, "Bob", "ตอบ") and logic.line_row_matches(r, "Bob", "จริง, ตอบ")
    assert not logic.line_row_matches(r, "Bob", "เดิม, จริง, ตอบ")
    t = logic.line_parse_row("ทดสอบระบบส่งใหม่ของ bot ไม่ต้องตอบครับ, 9.35 น., อ่านแล้ว 2")
    assert t.kind == "msg" and t.time == "9.35" and logic.line_row_matches(t, None, "ทดสอบระบบส่งใหม่ของ bot ไม่ต้องตอบครับ")


def test_send_core_post_check_confirms_english_quote_reply(monkeypatch):
    class _QuoteSentEN(FakeLineEN):
        """After the Enter of an armed quote, LINE 26.6 shows the own quote-reply row (live shape)."""
        def press(self, vks, task):
            body, quoted, armed = self.box, self.qname, self.armed
            super().press(vks, task)
            if tuple(vks) == (0x0D,) and armed:
                self.items[-1] = ("qrow", f"{quoted}. Reply: {body}., 10:30 AM, ")

        def row_name(self, i):
            it = self.items[i]
            return it[1] if it[0] == "qrow" else super().row_name(i)

    f = _QuoteSentEN(history_en() + [("msg", "Alicewong", LIVE_QUOTE_ORIG, "10.00", "red")])
    f.pane_alive = False
    ui = ui_for(f)
    rt = {"sender": "Alicewong", "text": LIVE_QUOTE_ORIG, "time": "10:00 AM", "date": "2026-11-18"}
    exp, extra, _ = ui.prepare("Group A", LIVE_QUOTE_BODY, None, rt)
    assert f.armed and f.qname.endswith(LIVE_QUOTE_ORIG)
    out = ui_for(f).send(exp, extra)
    assert f.keys.count((0x0D,)) == 1 and "ตรงกับข้อความ" in out and "ยังไม่ยืนยัน" not in out


def test_en_media_rows():
    r = logic.line_parse_row("Photos, messageImage_1795065533799.jpg, 12:19 PM, Read")      # own photo
    assert r.kind == "msg" and r.time == "12.19" and logic.line_is_media_body(r.body_guess or r.rest)
    o = logic.line_parse_row("Alicewong, Photos, 6:44 PM")                                  # photo from the other side
    assert o.body_guess == "Photos" and logic.line_is_media_body(o.body_guess) and logic.line_row_matches(o, "Alicewong", "Photos")
    assert logic.line_is_media_body("Photos") and logic.line_is_media_body("รูป")
    assert not logic.line_is_media_body("photos of the site") and not logic.line_is_media_body("hello")


def test_send_core_post_check_confirms_english_image_send(tmp_path):
    class _ImageSentEN(FakeLineEN):
        """After the Enter of a pasted image, LINE 26.6 shows our own row as 'Photos, <hash>.png, <time>, Read'."""
        def press(self, vks, task):
            img = self.box == OBJ                      # the pasted image is the whole box (no caption)
            super().press(vks, task)
            if tuple(vks) == (0x0D,) and img:
                self.items[-1] = ("qrow", "Photos, 01de88f342d90e8ecffadb40.png, 2:15 PM, Read")

        def row_name(self, i):
            it = self.items[i]
            return it[1] if it[0] == "qrow" else super().row_name(i)

    p = tmp_path / "cmp.png"
    Image.new("RGB", (400, 200), COLORS["red"]).save(p)
    f = _ImageSentEN(history_en())
    f.pane_alive = False
    ui = ui_for(f)
    exp, extra, _ = ui.prepare("Group A", "", str(p), None)
    out = ui_for(f).send(exp, extra)
    assert f.keys.count((0x0D,)) == 1 and f.sent[0][1] == OBJ
    assert "ตรงกับข้อความ" in out and "ยังไม่ยืนยัน" not in out


def test_en_media_row_any_image_file_name():
    r = logic.line_parse_row("Photos, 01de88f342d90e8ecffadb40.png, 2:15 PM")                  # live hash name
    assert r.kind == "msg" and r.time == "14.15" and logic.line_is_media_body(r.body_guess or r.rest)
    r = logic.line_parse_row("Photos, 01de88f342d90e8ecffadb40.png, 2:15 PM, Read")
    assert logic.line_is_media_body(r.body_guess or r.rest)
    for name in ("a.JPG", "b.jpeg", "c.gif", "d.bmp", "e.WEBP", "My Shot 2.png"):
        r = logic.line_parse_row(f"Photos, {name}, 2:15 PM, Read")
        assert logic.line_is_media_body(r.body_guess or r.rest), name
    assert logic.line_is_media_body("Photos, 01de88f342d90e8ecffadb40.png")                  # prefixed form, bare name
    assert logic.line_is_media_body("messageImage_1795065533799.jpg")                       # Thai-style file kept
    assert not logic.line_is_media_body("Photos, notes.pdf") and not logic.line_is_media_body("Photos, ")
    assert not logic.line_is_media_body("hello") and not logic.line_is_media_body("photos of the site")


def test_en_unread_separator_is_neither_message_nor_date():
    r = logic.line_parse_row("Unread messages below")
    assert r.kind == "sep" and not logic.line_is_date(r.text)
    a, s, b = (logic.line_parse_row(x) for x in ("November 19, 2026", "Unread messages below", "Bob, hi, 9:00 AM"))
    for i, row in enumerate((a, s, b)):
        row.rect = (375, i * 100, 1000, 50)
    logic.line_attach_dates([a, s, b])
    assert b.date_key == (2026, 11, 19) and s.date_key == (2026, 11, 19)       # the separator does not poison the date
    c = logic.line_parse_row("Nov 19, 2026")                                       # unlisted date form still poisons
    assert logic.line_dateish(c.text)


def test_en_locale_detection_and_ui_strings():
    d = logic.line_detect_locale
    assert d("Search for chats and messages", "Enter a message") == "en"
    assert d("ค้นหาข้อความและห้องแชท", "พิมพ์ข้อความ") == "th"
    assert d("Search for chats and messages", "") == "en" and d("", "Enter a message") == "en"
    assert d("ค้นหาข้อความและห้องแชท", "Enter a message") is None           # mixed: unknown, parsers accept both
    assert d("", "") is None and d(None, "x") is None
    assert logic.line_box_text("Enter a message") == "" and logic.line_box_text("พิมพ์ข้อความ") == ""
    assert logic.line_box_text("hello", "Enter a message") == "hello"
    assert logic.line_header_name("Group, 10 people") == "Group" and logic.line_header_name("กลุ่ม X, 10 คน") == "กลุ่ม X"
    assert logic.line_header_name("Alicewong") == "Alicewong"


def test_en_chat_row_name_rule():
    m = logic.line_chat_row_matches
    assert m("Alicewong, You sent a photo., November 19, 2026", "Alicewong")
    assert m("กลุ่มเพื่อนร่วมงาน ทีมขาย, 10 people, You sent a photo., November 19, 2026", "กลุ่มเพื่อนร่วมงาน ทีมขาย")
    assert m("Keep Memo", "Keep Memo")
    assert m("Al, Jr, a, b, November 19, 2026", "Al, Jr")           # a chat name with ", " inside
    assert not m("Alicewong2, hi, November 19, 2026", "Alicewong") and not m("Alicewong", "Oak")
    assert not m("Alicewong, x", "") and not m("", "Alicewong")


def test_en_menu_quote_and_deny_rules():
    near = (110, 110, 150, 200)
    assert logic.line_menu_ok(near, (100, 100), ["Reply", "Copy", "Share", "Delete"])[0]
    assert logic.line_menu_ok(near, (100, 100), ["Reply", "Save", "Share"])[0]
    assert not logic.line_menu_ok(near, (100, 100), ["Reply", "Select all"])[0]
    assert not logic.line_menu_ok(near, (100, 100), ["Reply", "Delete", "Unsend"])[0]
    assert not logic.line_menu_ok(near, (100, 100), ["Copy"])[0]
    for name in ["Delete", "Unsend", " Delete "]:
        with pytest.raises(ToolError):
            logic.line_assert_clickable(name)
    logic.line_assert_clickable("Reply")
    bar = "A reply to Bob's message. Bob's original message: hello, world"
    assert logic.line_quote_equal(bar, "Bob", "hello, world")
    assert not logic.line_quote_equal(bar, "Bob", "hello") and not logic.line_quote_equal(bar, "Al", "hello, world")
    assert not logic.line_quote_equal(bar + "!", "Bob", "hello, world")
    assert logic.line_quote_fresh(bar, "Bob", "hello, world", "", armed_before=True)
    assert logic.line_quote_equal(logic.line_expected_quote("Bob", "x"), "Bob", "x")       # Thai bar still fine


def test_en_quote_bar_one_trailing_period():
    # live LINE 26.6 English: the bar appends exactly one "." after the original message body
    bar = "A reply to Bob's message. Bob's original message: hello, world."
    assert logic.line_quote_equal(bar, "Bob", "hello, world")
    assert logic.line_quote_fresh(bar, "Bob", "hello, world", "", armed_before=True)
    # two trailing periods: refused when the target has no period
    assert not logic.line_quote_equal(bar + ".", "Bob", "hello, world")
    # different body still refused
    assert not logic.line_quote_equal(bar, "Bob", "hello")
    assert not logic.line_quote_equal(bar, "Bob", "hello, world!")
    # sender must still match exactly
    assert not logic.line_quote_equal(bar, "Al", "hello, world")
    # target that itself ends with "." : LINE shows "done.." (the bar may also show it plain); "done..." is refused
    bar_p = "A reply to Bob's message. Bob's original message: done."
    assert logic.line_quote_equal(bar_p, "Bob", "done.")
    assert logic.line_quote_equal(bar_p, "Bob", "done")
    assert logic.line_quote_equal(bar_p + ".", "Bob", "done.")
    assert not logic.line_quote_equal(bar_p + "..", "Bob", "done.")
    assert not logic.line_quote_equal(bar_p, "Bob", "done..")
    # Thai bar: a trailing "." is never dropped
    thai = logic.line_expected_quote("Bob", "hello, world")
    assert not logic.line_quote_equal(thai + ".", "Bob", "hello, world")
    assert logic.line_quote_equal(logic.line_expected_quote("Bob", "done."), "Bob", "done.")


def test_en_snapshot_geometry_and_locale():
    f, ui = en_ui()
    s = ui.snapshot()
    assert s.locale == "en" and s.edit is not None and s.search is not None and s.header == "Group A"   # ", 10 people" stripped
    assert {r.kind for r in s.rows} >= {"date", "msg", "sep"}
    assert ui_for(FakeLine(history())).snapshot().locale == "th"


@pytest.mark.parametrize("date,tm", [("2026-11-15", "9:00"), ("2026-11-15", "09:00"), ("November 15, 2026", "9.00"),
                                      ("November 15, 2026", "9:00 AM"), ("15 พฤศจิกายน 2026", "09:00")])
def test_en_locate_accepts_iso_date_and_24h_time(date, tm):
    f, ui = en_ui()
    snap, row, ms = ui.locate("Bob", "hello", tm, date)
    assert row.text == "Bob, hello, 9:00 AM" and len(ms) == 1 and row.date_key == (2026, 11, 15)


def test_en_locate_pm_time_and_today_by_date():
    f, ui = en_ui()
    snap, row, ms = ui.locate("Carol", "Photos", "13:18", "2026-11-16", index=2, expected_count=3)
    assert row.text == "Carol, Photos, 1:18 PM" and len(ms) == 3
    assert ui.locate("Bob", "late, news", "10:10", "2026-11-18")[1].date_key == (2026, 11, 18)   # "November 18, 2026" is today
    assert ui.locate("Bob", "late, news", "10:10", "Today")[1].date_key == (2026, 11, 18)         # alias still accepted
    assert ui.locate("", "ok, then", "14:00", "2026-11-16")[1].text == "ok, then, 2:00 PM, Read"  # own row, read receipt


def test_en_locate_refusals():
    f, ui = en_ui()
    with pytest.raises(ToolError):                                       # three identical photos, expected_count 1
        ui.locate("Carol", "Photos", "13:18", "2026-11-16")
    with pytest.raises(ToolError):
        ui.locate("Bob", "hello", "9:00", "2026-11-17")                   # no such day
    with pytest.raises(ToolError):
        ui.locate("Bob", "hello", "21:00", "2026-11-15")                  # wrong time (9 PM)
    with pytest.raises(ToolError):
        ui.locate("Bob", "hello", "9:00", "2026-13-45")                   # not a date
    with pytest.raises(ToolError):
        ui.locate("Bob", "hello", "25:00 PM", "2026-11-15")               # not a time


def test_en_unread_separator_is_ignored_by_locate():
    f, ui = en_ui()
    s = ui.snapshot()
    assert any(r.kind == "sep" for r in s.rows) and all(r.kind != "sep" for r in s.msgs)
    snap, row, ms = ui.locate("Bob", "x", "10:11", "2026-11-18")           # below the separator: still dated today
    assert row.date_key == (2026, 11, 18) and len(ms) == 1


def test_en_open_chat_exact_rail_button_and_ambiguity():
    f, ui = en_ui()
    snap = ui.open_chat("Keep Memo")                                     # the left rail also has a "Keep Memo" Button
    assert snap.header == "Keep Memo" and all(x >= 62 for x, _y, _b in f.clicks)
    g = FakeLineEN(history_en(), chats=("Al", "Al, Jr", "Other"))
    with pytest.raises(ToolError) as e:                                  # "Al" also prefixes the row of "Al, Jr"
        ui_for(g).open_chat("Al")
    assert "ซ้ำ" in str(e.value)
    h = FakeLineEN(history_en(), chats=("Dup", "Dup"))
    with pytest.raises(ToolError):
        ui_for(h).open_chat("Dup")
    with pytest.raises(ToolError):
        ui_for(FakeLineEN(history_en())).open_chat("Nobody")
    assert ui_for(FakeLineEN(history_en())).open_chat("Group A").header == "Group A"


def test_en_prepare_plain_text_then_send_matches_own_row_with_read():
    f, ui = en_ui()
    exp, extra, obs = ui.prepare("Group A", "Thanks, got it", None, None)
    assert obs["locale"] == "en" and f.box == "Thanks, got it" and not f.armed and f.sent == []
    out = ui.send(exp, extra)
    assert f.sent == [("Group A", "Thanks, got it", False, None)] and f.keys.count((0x0D,)) == 1
    assert f.row_name(f.n() - 1) == "Thanks, got it, 10:30 AM, Read"        # what LINE shows for the own message
    assert "กล่องว่างแล้ว" in out and "ตรงกับข้อความ" in out


def test_en_prepare_with_reply_by_iso_date_and_24h_time():
    f, ui = en_ui()
    rt = {"sender": "Bob", "text": "late, news", "time": "10:10", "date": "2026-11-18"}
    exp, extra, obs = ui.prepare("Group A", "got it", None, rt)
    assert f.armed and exp["quote"]["sender"] == "Bob" and f.qname == "A reply to Bob's message. Bob's original message: late, news"
    ui_for(f).send(exp, extra)
    assert f.sent[0][2] is True and f.sent[0][3].endswith("late, news")


def test_en_reply_refused_when_the_quote_bar_names_another_message():
    f, ui = en_ui()
    orig = f.arm

    def bad():
        orig()
        f.qname = f.quote_name("Bob", "something else")
    f.arm = bad
    with pytest.raises(ToolError):
        ui.arm_quote({"sender": "Bob", "text": "late, news", "time": "10:10", "date": "2026-11-18"})
    assert not f.armed


@pytest.mark.parametrize("target", [1, 2, 3])
def test_en_image_row_quote_by_pixels(target):
    f, ui = en_ui()
    rt = {"sender": "Carol", "text": "Photos", "time": "13:18", "date": "2026-11-16", "index": target, "expected_count": 3}
    q, extra = ui.arm_quote(rt)
    assert q["media"] and f.armed and set(extra["cands"]) == {1, 2, 3}
    assert f.items[f.menu_row][4] == ["red", "green", "blue"][target - 1]


def test_en_menu_never_lands_on_unsend_or_delete():
    f, ui = en_ui()
    f.deny_overlap = True                 # "Delete" drawn over "Reply": the click point would hit it
    snap, row, _ = ui.locate("Bob", "late, news", "10:10", "2026-11-18", show=True)
    with pytest.raises(ToolError):
        ui.menu_click(snap, row)
    assert not f.armed


def test_en_cancel_closes_quote_and_clears_box():
    f, ui = en_ui()
    ui.prepare("Group A", "draft", None, {"sender": "Bob", "text": "late, news", "time": "10:10", "date": "2026-11-18"})
    assert f.armed and f.box == "draft"
    snap = ui.snapshot()
    snap = ui.close_quote(snap)
    ui.clear_box(snap)
    assert not f.armed and f.box == "" and f.sent == []


# =================================== sender "*" = any sender except our own account ===================================

def wild_items():
    return [("date", "วันนี้"),
            ("msg", "สมชาย", "ขอราคา", "10.10", "red"),          # unique other-side row (Thai name, Thai body)
            ("msg", "Bob", "same text", "10.20", "red"),         # two different senders, same text + time + date
            ("msg", "Carol", "same text", "10.20", "red"),
            ("msg", None, "my own words", "10.30", "red"),       # our own row
            ("msg", None, "ok, then", "10.40", "red"),           # own row whose body contains ", "
            ("msg", "Bob", "tail", "10.50", "red"),
            ("msg", "Bob", "x", "10.51", "red"), ("msg", "Bob", "y", "10.52", "red")]


def wild_rt(text, tm, **kw):
    return dict({"sender": "*", "text": text, "time": tm, "date": "วันนี้"}, **kw)


@pytest.mark.parametrize("mk", [lambda: FakeLine(wild_items()), lambda: FakeLineEN(wild_items())])
def test_wildcard_sender_matches_a_unique_other_row_and_reports_its_name(mk):
    f = mk()
    ui = ui_for(f)
    q, _ = ui.arm_quote(wild_rt("ขอราคา", "10.10"))
    assert f.armed and q["sender"] == "สมชาย" and q["text"] == "ขอราคา"      # the REAL name, not "*"
    assert f.qname == f.quote_name("สมชาย", "ขอราคา")
    exp = {"chat": "Group A", "text": "x", "n_images": 0, "image_path": None, "quote": q}
    assert "สมชาย" in line._evidence(exp, {}) and "สมชาย" in logic.line_summary(exp)


def test_wildcard_sender_english_body_and_bob():
    f = FakeLineEN(wild_items())
    q, _ = ui_for(f).arm_quote(wild_rt("tail", "10:50"))
    assert q["sender"] == "Bob" and f.armed


def test_wildcard_refuses_two_senders_with_same_text_time_date_unless_counted():
    f = FakeLine(wild_items())
    with pytest.raises(ToolError):
        ui_for(f).arm_quote(wild_rt("same text", "10.20"))
    assert not f.armed and f.clicks == []
    with pytest.raises(ToolError):                                    # expected_count > 1 without an index: still refused
        ui_for(FakeLine(wild_items())).arm_quote(wild_rt("same text", "10.20", expected_count=2))
    f = FakeLine(wild_items())                                        # explicit count + index: the 2nd row is Carol
    q, _ = ui_for(f).arm_quote(wild_rt("same text", "10.20", expected_count=2, index=2))
    assert q["sender"] == "Carol" and f.armed
    f = FakeLine(wild_items())
    with pytest.raises(ToolError):                                    # wrong count is refused as before
        ui_for(f).arm_quote(wild_rt("same text", "10.20", expected_count=3, index=1))


def test_wildcard_never_matches_our_own_rows():
    f = FakeLine(wild_items())
    with pytest.raises(ToolError):
        ui_for(f).arm_quote(wild_rt("my own words", "10.30"))
    assert not f.armed and f.clicks == []
    # own body "ok, then": the exact text matches nothing, and the tail "then" resolves to a bogus name "ok" ->
    # the quote bar (which names OUR name, not "ok") fails the exact check and the bar is closed again
    with pytest.raises(ToolError):
        ui_for(FakeLine(wild_items())).arm_quote(wild_rt("ok, then", "10.40"))
    f = FakeLine(wild_items())
    with pytest.raises(ToolError):
        ui_for(f).arm_quote(wild_rt("then", "10.40"))
    assert not f.armed


def test_wildcard_row_match_and_sender_helpers():
    r = logic.line_parse_row("Bob, hi, there, 9.00 น.")
    assert not logic.line_row_matches(r, "*", "there") and logic.line_row_matches(r, "Bob", "hi, there")
    assert logic.line_row_matches(r, "*", "hi, there") and logic.line_row_sender(r, "hi, there") == "Bob"
    own = logic.line_parse_row("my own words, 9.00 น.")
    assert not logic.line_row_matches(own, "*", "my own words") and logic.line_row_matches(own, None, "my own words")
    assert logic.line_row_sender(own, "my own words") is None
    assert not logic.line_row_matches(r, "*", "hi, there", "9.01") and logic.line_row_matches(r, "*", "hi, there", "9.00")
