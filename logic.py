"""Pure logic for pc-hands: confirm gate, forbidden windows, id cache, near filter, key parsing.
No UI / Win32 imports here so it can be unit-tested anywhere."""
import dataclasses
import datetime
import fnmatch
import itertools
import re
import secrets
import time
from typing import Optional


class ToolError(Exception):
    """Expected failure of a tool: the message (Thai) goes back to the caller."""


class NeedsConfirm(Exception):
    """The action contacts other people: the message asks for the user's confirmation."""

# ---------- monitors ----------

def monitor_name(x: int) -> str:
    """Label for a screen x: the primary monitor starts at x=0, a second monitor placed left of it has x<0."""
    return "LEFT" if x < 0 else "MAIN"

# ---------- forbidden windows ----------

FORBIDDEN_PROCS = {
    "regedit.exe", "regedt32.exe", "consent.exe", "credentialuibroker.exe",
    "logonui.exe", "sechealthui.exe", "securityhealthsystray.exe", "lockapp.exe",
}
FORBIDDEN_TITLE_RE = re.compile(
    r"windows security|ความปลอดภัยของ windows|user account control|การควบคุมบัญชีผู้ใช้"
    r"|credential|ข้อมูลประจำตัว|registry editor|ตัวแก้ไขรีจิสทรี|windows hello", re.I)
FORBIDDEN_OPEN = {"regedit", "regedt32"}
# Claude Code sessions run in these: keys/typing there = prompts or Alt+F4 into a live agent (happened in testing)
TERMINAL_PROCS = {"windowsterminal.exe", "openconsole.exe", "conhost.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
                  "claude.exe", "mintty.exe", "wezterm-gui.exe", "alacritty.exe"}


def forbidden_reason(title: str, process: str, elevated: bool = False):
    """Return a Thai reason string if this window must never be controlled, else None.
    `elevated` = target is admin and we are not (UIPI blocks input anyway)."""
    p = (process or "").lower()
    if p in FORBIDDEN_PROCS:
        return f"ห้ามควบคุมหน้าต่างนี้ ({process})"
    if FORBIDDEN_TITLE_RE.search(title or ""):
        return f"ห้ามควบคุมหน้าต่างนี้ ({title})"
    if p in TERMINAL_PROCS:
        return f"ห้ามควบคุมหน้าต่าง terminal ({process}) เพราะมี Claude Code ทำงานอยู่ งาน shell ให้ส่งต่อ Claude"
    if elevated:
        return f"หน้าต่างนี้เป็น admin ควบคุมไม่ได้ ({process or title})"
    return None

# ---------- confirm gate ----------

SEND_EN = re.compile(r"\b(send|submit|post|reply|publish|call|share)\b", re.I)
SEND_TH = ("ส่ง", "โพสต์", "ตอบกลับ", "เผยแพร่", "โทร", "แชร์")
MESSAGING_PROCS = ["line.exe", "outlook.exe", "ms-teams.exe", "teams.exe", "discord.exe",
                   "telegram.exe", "whatsapp*.exe"]


def is_send_name(name: str, process: str = "") -> bool:
    n = (name or "").strip()
    # LINE chat-list rows read "<label>, <last message preview>, <date>": judge the label only there,
    # else a chat whose last message was "ส่งไฟล์แล้ว" is gated like a Send button. Other apps: whole name.
    if (process or "").lower() == "line.exe":
        n = n.split(",")[0].strip()
    if not n or len(n) > 40:   # long strings are document text, not a button label
        return False
    return bool(SEND_EN.search(n)) or any(t in n for t in SEND_TH)


def is_messaging_proc(process: str) -> bool:
    p = (process or "").lower()
    return any(fnmatch.fnmatch(p, pat) for pat in MESSAGING_PROCS)


def keys_have_enter(keys: str) -> bool:
    return any(k.strip().lower() in ("enter", "return") for k in re.split(r"[+\s]+", keys or "") if k)


def confirm_needed(tool: str, args: dict, element_name: str = "", process: str = ""):
    """Return a Thai confirmation prompt if the action contacts other people, else None.
    Caller skips the check when args.confirmed is true (checked here too)."""
    if args.get("confirmed") is True:
        return None
    if tool == "click" and is_send_name(element_name, process):
        return f'การคลิก "{element_name.strip()}" จะติดต่อคนอื่น ต้องให้ผู้ใช้ยืนยันก่อน'
    if tool == "press_keys" and keys_have_enter(args.get("keys", "")) and is_messaging_proc(process):
        return f"การกด {args.get('keys')} ใน {process} อาจส่งข้อความหาคนอื่น ต้องให้ผู้ใช้ยืนยันก่อน"
    if tool == "line_send":   # element_name carries the chat + message summary
        return (f"การส่งข้อความใน LINE ({element_name}) ติดต่อคนอื่น ต้องให้ผู้ใช้ยืนยันข้อความนี้ก่อน "
                "แล้วเรียก line_send ซ้ำด้วย confirmed=true")
    if tool == "type_text" and "\n" in str(args.get("text", "")) and is_messaging_proc(process):
        return f"การพิมพ์ข้อความแล้วกด Enter ใน {process} จะส่งข้อความหาคนอื่น ต้องให้ผู้ใช้ยืนยันก่อน"
    return None

# ---------- id cache ----------

class IdCache:
    """Short-lived id -> object map. ids look like '#12' / 'w3'. Entries expire after ttl seconds."""

    def __init__(self, prefix: str, ttl: float = 120.0, clock=time.monotonic):
        self.prefix, self.ttl, self.clock = prefix, ttl, clock
        self._n = itertools.count(1)
        self._d = {}      # id -> (obj, ts)
        self._keys = {}   # dedupe key -> id

    def put(self, obj, key=None) -> str:
        now = self.clock()
        if key is not None and key in self._keys and self._keys[key] in self._d:
            i = self._keys[key]
        else:
            i = f"{self.prefix}{next(self._n)}"
            if key is not None:
                self._keys[key] = i
        self._d[i] = (obj, now)
        return i

    def get(self, ident):
        i = self.normalize(ident)
        item = self._d.get(i)
        if not item:
            return None
        if self.clock() - item[1] > self.ttl:
            del self._d[i]
            return None
        return item[0]

    def normalize(self, ident) -> str:
        s = str(ident).strip().lower()
        if s and s[0].isdigit():
            s = self.prefix + s
        return s

    def purge(self):
        now = self.clock()
        for i in [k for k, (_, ts) in self._d.items() if now - ts > self.ttl]:
            del self._d[i]


def rect_center(r):
    x, y, w, h = r
    return x + w // 2, y + h // 2


def filter_near(items, x, y, r, rect_of=lambda it: it.rect):
    """Keep items whose rect centre is within r px of (x,y); nearest first."""
    out = []
    for it in items:
        cx, cy = rect_center(rect_of(it))
        d = ((cx - x) ** 2 + (cy - y) ** 2) ** 0.5
        if d <= r:
            out.append((d, it))
    out.sort(key=lambda t: t[0])
    return [it for _, it in out]

# ---------- keys ----------

VK = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "windows": 0x5B, "cmd": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
}
for _i in range(1, 25):
    VK[f"f{_i}"] = 0x70 + _i - 1
for _c in "abcdefghijklmnopqrstuvwxyz":
    VK[_c] = ord(_c.upper())
for _c in "0123456789":
    VK[_c] = ord(_c)
VK.update({",": 0xBC, ".": 0xBE, "/": 0xBF, ";": 0xBA, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "'": 0xDE, "`": 0xC0, "\\": 0xDC})
MODS = {0x11, 0x12, 0x10, 0x5B}


def parse_keys(keys: str):
    """'ctrl+shift+s' -> [0x11, 0x10, 0x53]. Raises ValueError on unknown key."""
    parts = [p for p in re.split(r"\s*\+\s*", (keys or "").strip().lower()) if p]
    if not parts:
        raise ValueError("ไม่มี keys")
    out = []
    for p in parts:
        if p not in VK:
            raise ValueError(f"ไม่รู้จักคีย์ {p}")
        out.append(VK[p])
    return out

# ---------- OCR (pure parts) ----------

import difflib
import unicodedata


def _norm(s: str) -> str:
    """Case/width-insensitive, whitespace-free form used for fuzzy matching."""
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return "".join(ch for ch in s if not ch.isspace())


def _strip_marks(s: str) -> str:
    """Drop Thai tone marks / vowel diacritics (Mn): OCR confuses them most."""
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn")


def _partial(q: str, t: str) -> float:
    """Best SequenceMatcher ratio of q against any window of t (window len len(q)-1..len(q)+1)."""
    if not q or not t:
        return 0.0
    if q in t:
        return 1.0
    if len(t) <= len(q):
        return difflib.SequenceMatcher(None, q, t).ratio()
    best = 0.0
    for n in {max(1, len(q) - 1), len(q), len(q) + 1}:
        for i in range(0, max(1, len(t) - n + 1)):
            r = difflib.SequenceMatcher(None, q, t[i:i + n]).ratio()
            if r > best:
                best = r
    return best


def fuzzy_score(query: str, text: str) -> float:
    """0..1 similarity of a query to an OCR line (case-insensitive, Thai/English, tolerant of small OCR errors).
    Substring = 1.0. Queries of <=2 chars must match as an exact substring (too noisy otherwise)."""
    q, t = _norm(query), _norm(text)
    if not q or not t:
        return 0.0
    if q in t:
        return 1.0
    qs, ts = _strip_marks(q), _strip_marks(t)
    if qs and qs in ts:
        return 0.97
    if len(qs) <= 2:
        return 0.0
    return max(_partial(q, t), 0.97 * _partial(qs, ts))


def fuzzy_rank(query: str, items, text_of=lambda it: it[1], threshold: float = 0.6):
    """[(score, item)] with score >= threshold, best first (ties: shorter text first = tighter box)."""
    out = []
    for it in items:
        s = fuzzy_score(query, text_of(it))
        if s >= threshold:
            out.append((s, it))
    out.sort(key=lambda p: (-p[0], len(text_of(p[1]))))
    return out


def poly_to_rect(poly, ox: int = 0, oy: int = 0):
    """4-point polygon in crop pixels -> (x, y, w, h) in screen coordinates (crop origin ox,oy)."""
    xs = [float(p[0]) for p in poly]
    ys = [float(p[1]) for p in poly]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    return (int(round(x0)) + ox, int(round(y0)) + oy, max(1, int(round(x1 - x0))), max(1, int(round(y1 - y0))))


def clip_region(x, y, w, h, vscreen):
    """Intersect a region with the virtual screen (vx,vy,vw,vh); None if empty."""
    vx, vy, vw, vh = vscreen
    x0, y0, x1, y1 = max(vx, x), max(vy, y), min(vx + vw, x + w), min(vy + vh, y + h)
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def reading_order(rects_items, rect_of=lambda it: it[0]):
    """Top-to-bottom, then left-to-right."""
    return sorted(rects_items, key=lambda it: (rect_of(it)[1], rect_of(it)[0]))


# ======================================================================================================
# LINE desktop (pure parts; the GUI side is line.py). Every fact marked GUESS is checked by the live test.
# ======================================================================================================

#
# LOCALES. LINE Desktop 26.6 shows either a Thai or an English UI. Every UI string / pattern lives in the ONE table
# below (Thai = seen live 2026-11-18, English = captured live on the server 2026-11-19 unless marked GUESS). The
# parsers accept the UNION of both locales (no string of one locale can be read as the other), and
# line_detect_locale() names the UI per snapshot from the search-box / input-box names (None = unknown -> union only).
THAI_MONTHS = ("มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
               "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม")
EN_MONTHS = ("January", "February", "March", "April", "May", "June",
             "July", "August", "September", "October", "November", "December")
_MONTH_NO = {m: i + 1 for i, m in enumerate(THAI_MONTHS)}
_EN_MONTH_NO = {m: i + 1 for i, m in enumerate(EN_MONTHS)}
LINE_LOCALES = {
    "th": {
        "search": ("ค้นหา",),                                  # search Edit: "ค้นหาข้อความและห้องแชท"
        "input": ("พิมพ์ข้อความ",),                            # empty input Edit reads its name
        "input_prefix": ("แชทในชื่อ ",),                       # OpenChat input: "แชทในชื่อ <me>"
        "reply": ("ตอบกลับ",),                                 # context-menu item that arms a quote
        "deny": ("ลบ", "ยกเลิกข้อความ"),                       # destroy a message: never clicked
        # text menus have คัดลอก; image menus have no คัดลอก (seen live 2026-11-18)
        "peers": ("คัดลอก", "แชร์", "ส่งไปยัง Keep Memo", "บันทึก", "บันทึกที่โน้ต", "บันทึกเป็น..."),
        "media": ("รูป", "รูปภาพ", "สติกเกอร์", "วิดีโอ", "messageImage"),       # GUESS: bodies of media rows
        "today": ("วันนี้",), "yesterday": ("เมื่อวาน",),
        "paste_image": ("รูป",), "paste_text": ("ข้อความ",),
        "quote_prefix": ("ตอบกลับข้อความของ",),                # quote bar Group name starts with this
        "group_suffix": (r"\d+ คน",),                          # group header: "<name>, 10 คน"
        "separator": (),
    },
    "en": {
        "search": ("Search",),                                 # "Search for chats and messages"
        "input": ("Enter a message",),
        "input_prefix": ("Chat as ", "Chatting as "),          # GUESS: OpenChat input
        "reply": ("Reply",),                                   # GUESS: menu item names are not captured yet
        "deny": ("Delete", "Unsend", "Remove", "Delete for me", "Delete for everyone", "Unsend message"),   # GUESS
        "peers": ("Copy", "Share", "Save", "Save as...", "Keep", "Keep Memo", "Send to Keep Memo", "Save to Notes"),  # GUESS
        "media": ("Photo", "Photos", "Sticker", "Stickers", "Video", "Videos", "Image", "Images"),  # Photos = live; rest GUESS
        "today": ("Today",), "yesterday": ("Yesterday",),
        "paste_image": ("Image", "Photo", "Picture", "Send as image", "Send as photo"),   # GUESS: paste chooser
        "paste_text": ("Text", "Send as text"),                                         # GUESS
        "quote_prefix": ("A reply to ", "A reply from ", "Replying to ", "Reply to "),   # GUESS: quote bar name
        "group_suffix": (r"\d+ (?:people|members|person|member)",),                      # GUESS: header form
        "separator": ("Unread messages below",),              # live: a Text between message rows, not a message
    },
}


def _union(key):
    out = []
    for loc in LINE_LOCALES.values():
        out.extend(x for x in loc[key] if x not in out)
    return tuple(out)


_TH_DATE_RE = re.compile(r"(\d{1,2}) (" + "|".join(THAI_MONTHS) + r") (\d{4})")
_EN_DATE_RE = re.compile(r"(" + "|".join(EN_MONTHS) + r") (\d{1,2}), (\d{4})")      # live: "November 19, 2026"
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")                                 # reply_to input only
# WHITELIST of date separators: "16 พฤศจิกายน 2026", "วันนี้", "เมื่อวาน", "November 19, 2026", "Today", "Yesterday".
# Anything else is a message or unknown. (Today is shown by its date in English UI; the aliases stay accepted.)
LINE_DATE_RE = re.compile(_TH_DATE_RE.pattern + "|" + _EN_DATE_RE.pattern + "|" + "|".join(
    re.escape(x) for x in _union("today") + _union("yesterday")))
_DATEISH_RE = re.compile("|".join(THAI_MONTHS) + r"|(?:" + "|".join(EN_MONTHS) + r") \d|\b\d{4}\b")
# time tail: Thai "9.35 น." | English "7:57 PM" (AM/PM required; the space may be NBSP/NNBSP), then an optional read
# receipt (English ", Read" seen live on own rows; "Read 3" / "อ่านแล้ว" GUESS).
LINE_TIME_TAIL = re.compile(
    r", (?:(?P<h1>\d{1,2})\.(?P<m1>\d{2}) น\.|(?P<h2>\d{1,2}):(?P<m2>\d{2})[ \u00a0\u202f]?(?P<ap>[AaPp][Mm]))"
    r"(?:, (?:Read|อ่านแล้ว)(?: \d+)?|,)?\s*$")      # ", " alone = LINE 26.6 own row with an empty receipt part
LINE_REPLY_HEAD = re.compile(
    r"^(?:(?P<sender>.+?) )?ตอบกลับข้อความของ (?P<who>.+?) ข้อความเดิมของ (?P=who) คือ (?P<rest>.*)$", re.S)
# live: "A reply from Alicewong to Shop Desk's message. Shop Desk's original message: ..."
# GUESS: own reply = "A reply to X's message. ..." (no "from")
LINE_REPLY_HEAD_EN = re.compile(
    r"^A reply (?:from (?P<sender>.+?) )?to (?P<who>.+?)['’]s message\. (?P=who)['’]s original message: (?P<rest>.*)$", re.S)
# live (LINE 26.6 English, own quote-reply row): "A reply to X's message. X's original message: <orig>. Reply: <body>."
# LINE adds one "." after the original and one after the reply. <orig> ends at the first ". Reply: ".
LINE_REPLY_HEAD_EN_REPLY = re.compile(
    r"^A reply (?:from (?P<sender>.+?) )?to (?P<who>.+?)['’]s message\. (?P=who)['’]s original message: "
    r"(?P<orig>.*?)\. Reply: (?P<reply>.*)$", re.S)
# GUESS: English quote bar name (the live dry run confirms it); content must still equal sender + body exactly
LINE_QUOTE_BAR_EN = re.compile(
    r"^(?:A reply (?:from You )?to|Replying to|Reply to) (?P<who>.+?)['’]s message\. (?P=who)['’]s original message: (?P<body>.*)$", re.S)
LINE_QUOTE_PREFIX = "ตอบกลับข้อความของ"
LINE_QUOTE_PREFIXES = _union("quote_prefix")
LINE_SEPARATORS = frozenset(_union("separator"))
LINE_IMAGE_BODIES = _union("media")
_MEDIA_FILE_RE = re.compile(r"^(?:Photos?, )?messageImage\S*$")     # live: own photo row "Photos, messageImage_1795….jpg"
# live 26.6 English: any image file name, e.g. "Photos, 01de88f342d90e8ecffadb40.png". body_guess is the bare name
# (the "Photos, " head is split off), so the optional prefix is for the full row text only.
# A typed text that is exactly an image file name also matches: arm_quote then refuses (no thumbnail), it never misfires.
_MEDIA_IMG_EXT = r"\.(?:png|jpe?g|gif|bmp|webp)$"
_MEDIA_EN_FILE_RE = re.compile(r"^(?:Photos?, )?.+" + _MEDIA_IMG_EXT, re.IGNORECASE)
LINE_DENY_BUTTONS = frozenset(_union("deny"))        # context-menu items that destroy a message: never clicked
LINE_MENU_BUTTONS = _union("reply")                  # a real message menu always has one of these ...
# ... plus at least one of these. The chat-background menu (เลือกทั้งหมด / ตั้งค่าวอลล์เปเปอร์) has none of them.
LINE_MENU_PEERS = frozenset(_union("peers"))
# (destructive items never count as evidence that this is the right menu)
LINE_PLACEHOLDERS = _union("input")                  # empty input Edit reads its name
LINE_INPUT_PREFIXES = _union("input_prefix")
LINE_SEARCH_PREFIXES = _union("search")
LINE_PASTE_IMAGE, LINE_PASTE_TEXT = _union("paste_image"), _union("paste_text")
_GROUP_SUFFIX_RE = re.compile(r", (?:" + "|".join(_union("group_suffix")) + r")$")
OBJ = "\ufffc"
THUMB_BLOCK = 0.85       # attach thumbnail similarity below this blocks the send (secondary evidence)
MEDIA_MARGIN = 0.05      # quote thumbnail: best candidate must beat the runner-up by this much
QUOTE_GAP_MIN = 30       # GUESS: px between list bottom and input top when a quote bar sits between them
TEXT_MAX = 2000
_LINE_BREAKS = ("\u2028", "\u2029")


def line_plain(t) -> str:
    """Strip HTML tags (mentions appear as <a href=...>@Name</a> in UIA names)."""
    return re.sub(r"<[^>]+>", "", t or "")


def line_norm(t) -> str:
    return " ".join(line_plain(t).split())


_NEWLINE_RUN = re.compile(r" *(?:\r\n|\r|\n)+ *")


def line_nl_space(t) -> str:
    """Any run of newlines (\\r\\n, \\n, \\r), optionally with spaces around it, is one space. LINE's UIA row name joins
    the lines of a multi-line message with a single space, so both sides are compared on this form."""
    return _NEWLINE_RUN.sub(" ", t or "")


def line_is_date(text: str) -> bool:
    return bool(LINE_DATE_RE.fullmatch((text or "").strip()))


def line_dateish(text: str) -> bool:
    """Looks like a date but is not on the whitelist: the date of the rows below it is then UNKNOWN."""
    t = (text or "").strip()
    return len(t) < 40 and bool(_DATEISH_RE.search(t)) and not LINE_TIME_TAIL.search(t) and not line_is_date(t)


def line_date_key(text, today: Optional[datetime.date] = None):
    """'16 พฤศจิกายน 2026' / 'November 19, 2026' / '2026-11-19' (ISO, reply_to input) / 'วันนี้' / 'Today' /
    'เมื่อวาน' / 'Yesterday' -> (y, m, d); None when not a date we know. Buddhist years -> CE."""
    t = " ".join((text or "").split())
    today = today or datetime.date.today()
    low = t.casefold()
    if any(low == x.casefold() for x in _union("today")):
        return (today.year, today.month, today.day)
    if any(low == x.casefold() for x in _union("yesterday")):
        y = today - datetime.timedelta(days=1)
        return (y.year, y.month, y.day)
    m = _TH_DATE_RE.fullmatch(t)
    if m:
        y, mo, d = int(m.group(3)), _MONTH_NO[m.group(2)], int(m.group(1))
    else:
        m = _EN_DATE_RE.fullmatch(t)
        if m:
            y, mo, d = int(m.group(3)), _EN_MONTH_NO[m.group(1)], int(m.group(2))
        else:
            m = _ISO_DATE_RE.fullmatch(t)
            if not m:
                return None
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    y = y - 543 if y > 2400 else y
    try:
        datetime.date(y, mo, d)
    except ValueError:
        return None
    return (y, mo, d)


def line_norm_time(t) -> str:
    """'13.18 น.' / '13:18' / '09.05' / '7:57 PM' -> '13.18' / '13.18' / '9.05' / '19.57' (24 h, the H.MM form the
    Thai UI uses; the English 12 h clock is converted: 12 AM = 0, 12 PM = 12)."""
    m = re.match(r"^\s*(\d{1,2})[.:](\d{2})(?:[\s\u00a0\u202f]*([AaPp][Mm]))?", str(t or ""))
    if not m:
        return ""
    h = int(m.group(1))
    if m.group(3):
        if not 1 <= h <= 12:
            return ""
        h = h % 12 + (12 if m.group(3).lower() == "pm" else 0)
    return f"{h}.{m.group(2)}"


def _tail_time(m) -> str:
    """LINE_TIME_TAIL match -> 'H.MM' (24 h)."""
    if m.group("h1") is not None:
        return f"{int(m.group('h1'))}.{m.group('m1')}"
    return line_norm_time(f"{m.group('h2')}:{m.group('m2')} {m.group('ap')}")


def line_detect_locale(*names):
    """'th' | 'en' | None from the names of the search Edit and the input Edit (None = unknown/mixed: the parsers then
    simply accept both locales)."""
    seen = set()
    for n in names:
        n = (n or "").strip()
        for code, loc in LINE_LOCALES.items():
            if n.startswith(tuple(loc["search"]) + tuple(loc["input_prefix"])) or n in loc["input"]:
                seen.add(code)
    return seen.pop() if len(seen) == 1 else None


def line_chat_row_matches(name: str, chat: str) -> bool:
    """Chat-list Button name vs the wanted chat. Rows read '<chat>, <preview>, <date>' (groups '<chat>, 10 people, ...';
    'Keep Memo' has no tail), and a chat name may itself contain ', ': so accept exactly the name or the name followed
    by ', '; the caller refuses when more than one row qualifies, and the header check after the click decides."""
    n, c = (name or "").strip(), (chat or "").strip()
    return bool(c) and (n == c or n.startswith(c + ", "))


@dataclasses.dataclass
class LineRow:
    name: str                       # raw UIA name
    text: str                       # plain(name)
    kind: str                       # date | msg | reply | sep ("Unread messages below") | unknown
    rest: str = ""                  # text before ", H.MM น."
    time: str = ""
    sender_guess: Optional[str] = None   # text before the first ", " (a message BODY for own messages)
    body_guess: str = ""
    reply_sender: Optional[str] = None   # reply rows only; None = own reply
    quoted: str = ""                # reply rows: "<orig>, <reply>" (both may contain ", " so it is not split)
    reply_raw: Optional[str] = None  # English "Reply: <body>." rows only: the body exactly as shown (LINE's "." kept)
    rect: tuple = (0, 0, 0, 0)
    node: object = None
    date_key: object = None         # (y,m,d) | "?" (unknown separator above) | None (no separator above)

    @property
    def y(self):
        return self.rect[1]

    @property
    def h(self):
        return self.rect[3]


def line_parse_row(name: str) -> LineRow:
    p = line_plain(name)
    if line_is_date(p):
        return LineRow(name, p, "date")
    if p.strip() in LINE_SEPARATORS:
        return LineRow(name, p, "sep")
    m = LINE_TIME_TAIL.search(p)
    if not m:
        return LineRow(name, p, "unknown")
    rest = p[:m.start()]
    tm = _tail_time(m)
    rp = LINE_REPLY_HEAD.match(rest)
    if rp:
        return LineRow(name, p, "reply", rest, tm, reply_sender=rp.group("sender"), quoted=rp.group("rest"))
    ep = LINE_REPLY_HEAD_EN_REPLY.match(rest)
    if ep:
        raw = ep.group("reply")
        body = raw[:-1] if raw.endswith(".") else raw            # the one "." LINE appends
        return LineRow(name, p, "reply", rest, tm, body_guess=body, reply_sender=ep.group("sender"),
                       quoted=ep.group("orig"), reply_raw=raw)
    rp = LINE_REPLY_HEAD_EN.match(rest)                           # older comma form, kept for the GUESS fixtures
    if rp:
        return LineRow(name, p, "reply", rest, tm, reply_sender=rp.group("sender"), quoted=rp.group("rest"))
    head, sep, body = rest.partition(", ")
    return LineRow(name, p, "msg", rest, tm, head if sep else None, body if sep else rest)


def line_attach_dates(rows, today: Optional[datetime.date] = None):
    """rows: LineRow with .rect set, any order. Sets date_key from the nearest separator ABOVE (by y).
    A date-looking Text that is not whitelisted makes the rows below it '?' (never matched by date).
    `today` must be the same date the caller resolves the target with, or 'วันนี้'/'เมื่อวาน' disagree."""
    cur = None
    for r in sorted(rows, key=lambda r: r.rect[1]):
        if r.kind == "date":
            cur = line_date_key(r.text, today)
            r.date_key = cur
            continue
        if r.kind == "unknown" and line_dateish(r.text):
            cur = "?"
        r.date_key = cur
    return rows


ANY_OTHER_SENDER = "*"     # reply_to.sender: any sender except our own account (own rows never match it)


def line_row_sender(row: LineRow, text: str):
    """The GUI sender name of an OTHER-side row whose body is exactly `text`, else None (own rows, other bodies).
    msg: rest == "<name>, <text>" with a non-empty name that has no ", " in it (else "Bob, hi, there" would pass for
    the text "there" from sender "Bob, hi"); reply: the reply sender, body = tail as in line_row_matches."""
    text = line_nl_space(line_plain(text))
    if row.kind == "msg":
        rest = line_nl_space(row.rest)
        tail = ", " + text
        if rest.endswith(tail) and len(rest) > len(tail):
            name = rest[:-len(tail)]
            return name if name.strip() and ", " not in name else None
        return None
    if row.kind == "reply" and row.reply_sender:
        return row.reply_sender
    return None


def line_row_matches(row: LineRow, sender, text, tm=None) -> bool:
    """EXACT body identity. sender falsy = own message (own rows carry no sender prefix); sender "*" = any other
    sender (never an own row).
    Reply rows hold '<orig>, <reply>' so only the tail is compared (reply bodies may contain ', ')."""
    text = line_nl_space(line_plain(text))
    wild = sender == ANY_OTHER_SENDER
    if row.kind == "msg":
        if wild:
            ok = line_row_sender(row, text) is not None
        else:
            ok = line_nl_space(row.rest) == line_nl_space(f"{sender}, {text}" if sender else text)
    elif row.kind == "reply":
        if wild:
            sender_ok = bool(row.reply_sender)
        else:
            sender_ok = row.reply_sender == sender if sender else row.reply_sender is None
        if row.reply_raw is not None:      # English: "Reply: <body>." -> same "." rule as the quote bar
            ok = sender_ok and _quote_body_en_eq(row.reply_raw, text)
        else:
            quoted = line_nl_space(row.quoted)
            ok = sender_ok and quoted.endswith(", " + text) and len(quoted) > len(text) + 2
    else:
        return False
    return ok and (not tm or row.time == line_norm_time(tm))


def line_is_media_body(body: str) -> bool:
    b = line_plain(body).strip()
    return b in LINE_IMAGE_BODIES or bool(_MEDIA_FILE_RE.match(b)) or bool(_MEDIA_EN_FILE_RE.match(b))


def line_expected_quote(sender: str, body: str) -> str:
    return f"{LINE_QUOTE_PREFIX} {sender} ข้อความเดิมของ {sender} คือ {body}"


def line_quote_equal(name: str, sender: str, body: str) -> bool:
    """The quote bar names exactly this sender + body. Thai: whole-string identity. English (GUESS form): the bar's
    sender and body must each be identical, nothing else is trusted."""
    n = line_norm(name)
    if n == line_norm(line_expected_quote(sender, body)):
        return True
    m = LINE_QUOTE_BAR_EN.match(n)
    return bool(m) and m.group("who") == line_norm(sender) and _quote_body_en_eq(m.group("body"), body)


def _quote_body_en_eq(bar_body: str, body: str) -> bool:
    """English bar body vs target text. LINE 26.6 English appends exactly one "." after the original message, so the
    bar must equal the target or the target plus one "." (a target ending in "." therefore shows as "..")."""
    b, t = line_norm(bar_body), line_norm(body)
    return b == t or b == t + "."


def line_quote_fresh(name: str, sender: str, body: str, stale: str, armed_before: bool) -> bool:
    """Text quote check without trusting the stale UIA name: the CURRENT name must equal the expected text exactly and
    must differ from the stale name captured before the click, unless the stale name already was the expected one AND
    the bar was not armed before (then there is nothing that could be stale)."""
    if not line_quote_equal(name, sender, body):
        return False
    if line_norm(name) != line_norm(stale):
        return True
    return line_quote_equal(stale, sender, body) and not armed_before


def line_denied(name: str) -> bool:
    return line_norm(name) in LINE_DENY_BUTTONS


def line_assert_clickable(name: str):
    if line_denied(name):
        raise ToolError(f'ห้ามคลิกปุ่ม "{name}" (ลบ/ยกเลิกข้อความ) หยุดแล้ว')


def rect_gap(rect, pt) -> float:
    """Distance from a point to a (x,y,w,h) rect (0 when inside)."""
    x, y, w, h = rect
    dx = max(x - pt[0], 0, pt[0] - (x + w))
    dy = max(y - pt[1], 0, pt[1] - (y + h))
    return (dx * dx + dy * dy) ** 0.5


def rect_contains(rect, pt) -> bool:
    x, y, w, h = rect
    return x <= pt[0] < x + w and y <= pt[1] < y + h


def rects_close(a, b, tol=2) -> bool:
    return all(abs(p - q) <= tol for p, q in zip(a, b))


def line_menu_ok(win_rect, click_pt, button_names, max_dist=400):
    """A bubble context menu: a (new, visible) LINE window near the click holding Buttons ตอบกลับ AND คัดลอก."""
    names = {line_norm(n) for n in button_names}
    if rect_gap(win_rect, click_pt) > max_dist:
        return False, "เมนูอยู่ไกลจากจุดคลิก"
    if not names & set(LINE_MENU_BUTTONS):
        return False, "เมนูไม่มีปุ่ม " + " / ".join(LINE_MENU_BUTTONS)
    if not names & LINE_MENU_PEERS:
        return False, "เมนูไม่เหมือนเมนูของข้อความ (ไม่มี คัดลอก/แชร์/บันทึก/Keep Memo | Copy/Share/Save) ปุ่มที่เห็น: " + ", ".join(sorted(names))
    return True, ""


def line_wheel_chunks(notches: int, per_event: int = 8):
    """Qt caps ONE wheel event at ~8 notches, so n notches = back-to-back events of <= 8."""
    out = []
    n = int(notches)
    while n > 0:
        k = min(per_event, n)
        out.append(k)
        n -= k
    return out


def line_header_name(name: str) -> str:
    """Group headers read '<name>, 10 คน' (English UI: '<name>, 10 people', GUESS for the header)."""
    return _GROUP_SUFFIX_RE.sub("", (name or "").strip())


def line_box_expected(n_images: int, text: str) -> str:
    return OBJ * n_images + (text or "")


def line_box_text(raw: str, placeholder: str = "") -> str:
    """UIA text of the input Edit -> comparable text. Empty box reads its placeholder name; line breaks -> \\n;
    trailing newlines tolerated (GUESS: the PoC stripped newlines around an image)."""
    t = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    for b in _LINE_BREAKS:
        t = t.replace(b, "\n")
    if t in LINE_PLACEHOLDERS or (placeholder and t == placeholder) or t.startswith(LINE_INPUT_PREFIXES):
        return ""
    return t.rstrip("\n")


def line_text_problem(text) -> Optional[str]:
    """Why this message text must not be typed into LINE (None = fine)."""
    if not isinstance(text, str):
        return "text ต้องเป็นข้อความ"
    if len(text) > TEXT_MAX:
        return f"ข้อความยาวเกิน {TEXT_MAX} ตัวอักษร"
    if "\t" in text:
        return "ห้ามมี Tab ในข้อความ (Tab ย้ายโฟกัสออกจากกล่อง)"
    if OBJ in text or any(b in text for b in _LINE_BREAKS):
        return "ข้อความมีอักขระควบคุมที่ห้ามใช้"
    if any(ord(c) < 32 and c not in "\r\n" for c in text):
        return "ข้อความมีอักขระควบคุมที่ห้ามใช้"
    if text.replace("\r\n", "\n").endswith("\n"):
        return "ข้อความห้ามลงท้ายด้วยขึ้นบรรทัดใหม่"
    return None


def line_split_lines(text: str):
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def line_media_verdict(scores: dict, target, margin: float = MEDIA_MARGIN, single_min: float = 0.80):
    """Quote-reply to an image: best-matching candidate bubble must be the target with a clear margin."""
    if not scores or target not in scores:
        return False, "ไม่มีคะแนนของรูปเป้าหมาย"
    best = max(scores, key=scores.get)
    others = [v for k, v in scores.items() if k != target]
    if best != target:
        return False, f"quote เป็นรูปลำดับ {best} ไม่ใช่ {target} ({scores})"
    if others and scores[target] - max(others) < margin:
        return False, f"รูปที่ quote แยกจากรูปอื่นไม่ชัด (ห่าง {scores[target] - max(others):.2f} < {margin}) ({scores})"
    if not others and scores[target] < single_min:
        return False, f"รูปที่ quote ไม่คล้ายรูปเป้าหมาย ({scores[target]:.2f} < {single_min})"
    return True, ""


def line_check_state(exp: dict, obs: dict):
    """The ONE-PASS check (pure). exp = stored expectation, obs = what the screen shows right now.
    Returns a list of Thai problems; empty = safe to send."""
    p = []
    if not obs.get("enabled", True):
        p.append("หน้าต่าง LINE ถูกปิดใช้งาน (มี dialog ค้าง)")
    if obs.get("header") != exp["chat"]:
        p.append(f'แชทที่เปิดคือ "{obs.get("header")}" ไม่ใช่ "{exp["chat"]}"')
    want = line_box_expected(exp.get("n_images", 0), exp.get("text", ""))
    if obs.get("box") != want:
        p.append(f'กล่องข้อความไม่ตรง (เห็น {obs.get("box")!r} ต้องเป็น {want!r})')
    q = exp.get("quote")
    if q is None:
        if obs.get("armed"):
            p.append("มี quote ค้างอยู่ ข้อความจะถูกส่งเป็นการตอบกลับ")
    elif not obs.get("armed"):
        p.append("quote ไม่ได้เปิดอยู่")
    elif q.get("media"):
        if not obs.get("media_ok"):
            p.append("quote รูป: " + str(obs.get("media_reason") or "ตรวจพิกเซลไม่ผ่าน"))
    elif not line_quote_equal(obs.get("quote_name") or "", q["sender"], q["text"]):
        p.append(f'quote ไม่ตรง (เห็น {obs.get("quote_name")!r})')
    if exp.get("n_images"):
        sim = obs.get("thumb_sim")
        if sim is None:
            p.append("ไม่พบภาพย่อของรูปในกล่อง")
        elif sim < THUMB_BLOCK:
            p.append(f"ภาพในกล่องไม่คล้ายไฟล์ ({sim:.2f} < {THUMB_BLOCK}) บล็อกเพราะต่ำกว่าเกณฑ์")
    return p


def line_pick_main(wins):
    """Main window = largest VISIBLE top-level titled exactly 'LINE' (a small notification popup has the same title)."""
    c = [w for w in wins if w.get("title") == "LINE" and w.get("visible", True)]
    if not c:
        raise ToolError("ไม่พบหน้าต่าง LINE (เปิดโปรแกรมอยู่ไหม)")
    return max(c, key=lambda w: w["rect"][2] * w["rect"][3])


def line_blockers(wins, main):
    """Other enabled LINE top-levels (a leftover dialog makes the main window DISABLED)."""
    return [w for w in wins if w["hwnd"] != main["hwnd"] and w.get("enabled", True)
            and w["rect"][2] > 0 and w["rect"][3] > 0]


def line_summary(exp: dict) -> str:
    t = (exp.get("text") or "").replace("\n", " / ")
    parts = [f'แชท "{exp["chat"]}"']
    if t:
        parts.append(f'ข้อความ "{t[:80]}{"…" if len(t) > 80 else ""}"')
    if exp.get("n_images"):
        parts.append("รูป 1")
    q = exp.get("quote")
    if q:
        parts.append(f'ตอบกลับ {q["sender"]}: "{q["text"][:40]}"')
    return " | ".join(parts)


class TokenStore:
    """Pending LINE sends (token -> expectation), in memory with a TTL. One pending send at a time: the input box is
    one shared resource, so put() forgets every older token."""

    def __init__(self, ttl: float = 120.0, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._d = {}

    def put(self, entry: dict) -> str:
        self._d.clear()
        tok = secrets.token_hex(4)
        self._d[tok] = (entry, self.clock())
        return tok

    def get(self, tok):
        item = self._d.get(str(tok or "").strip().lower())
        if not item:
            return None
        if self.clock() - item[1] > self.ttl:
            self._d.clear()
            return None
        return item[0]

    def drop(self, tok=None):
        if tok is None:
            self._d.clear()
        else:
            self._d.pop(str(tok).strip().lower(), None)
