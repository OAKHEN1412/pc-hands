"""Tool implementations for pc-hands. All UI work happens on one worker thread (see server.py)."""
import glob
import os
import re
import shutil
import time
from pathlib import Path

import win32api
import win32gui

import winio
from logic import (IdCache, NeedsConfirm, ToolError, clip_region, confirm_needed, filter_near, forbidden_reason,
                   fuzzy_rank, monitor_name, parse_keys, poly_to_rect, reading_order, rect_center, FORBIDDEN_OPEN)
from winio import Highlight, Task, Yielded

HERE = Path(__file__).resolve().parent
SHOTS = HERE / "shots"
HL_SECS = 0.4
# fast mode (args.fast, sent by the Claude Code MCP): no red highlight, batched typing. Voice mode keeps both.
_FAST = False

ELEMS = IdCache("#", 120)
WINS = IdCache("w", 120)
OCRS = IdCache("#o", 120)   # ids from the ocr tool (#o5); looked up by _get_elem so click/scroll/type_text accept them

_uia = None


def U():
    """Lazy import so COM is initialised on the worker thread, not the importing thread."""
    global _uia
    if _uia is None:
        import uia
        _uia = uia
    return _uia


# ToolError / NeedsConfirm live in logic.py (shared with line.py, which tools.py loads lazily).


class Elem:
    def __init__(self, node, hwnd, pid, process):
        self.el, self.ctype, self.name, self.rect = node.el, node.ctype, node.name, node.rect
        self.autoid, self.enabled = node.autoid, node.enabled
        self.hwnd, self.pid, self.process = hwnd, pid, process


# ---------- windows ----------

def _wline(wid, w, active=False):
    flags = (" [admin]" if w["elevated"] else "") + (" <- active" if active else "")
    return f'[{wid}] {w["title"][:80]} — {w["process"]} (จอ {w["monitor"]}){flags}'


def _register_windows():
    ws = winio.enum_windows()
    fg = win32gui.GetForegroundWindow()
    return [(WINS.put(w, key=w["hwnd"]), w, w["hwnd"] == fg) for w in ws]


def check_forbidden(w):
    r = forbidden_reason(w["title"], w["process"], w.get("elevated", False))
    if r:
        raise ToolError(r)


def resolve_window(spec, default_fg=True):
    spec = (str(spec).strip() if spec else "")
    if not spec:
        if not default_fg:
            raise ToolError("ต้องระบุ window")
        w = winio.foreground_info()
        if not w:
            raise ToolError("ไม่มีหน้าต่างที่ active")
        return w
    if re.fullmatch(r"w?\d+", spec, re.I):
        w = WINS.get(spec)
        if w and win32gui.IsWindow(w["hwnd"]):
            return winio.window_info(w["hwnd"])
        raise ToolError(f"ไม่พบ {spec} (หมดอายุ) ให้ list_windows ใหม่")
    q = spec.lower()
    ws = winio.enum_windows()
    for w in ws:
        if w["title"].lower() == q:
            return w
    for w in ws:
        if q in w["title"].lower() or q in w["process"].lower():
            return w
    raise ToolError(f'ไม่พบหน้าต่าง "{spec}"')


def t_screen_info(a, task):
    lines = []
    for m in winio.monitors():
        x, y, w, h = m["rect"]
        lines.append(f'จอ {m["name"]}: {w}x{h} @ ({x},{y})' + (" หลัก" if m["primary"] else ""))
    fg = winio.foreground_info()
    if fg:
        lines.append(f'หน้าต่าง active: {fg["title"][:80]} — {fg["process"]} (จอ {fg["monitor"]})')
    cx, cy = win32api.GetCursorPos()
    lines.append(f"เมาส์อยู่ที่ ({cx},{cy}) จอ {monitor_name(cx)}")
    return "\n".join(lines)


def t_list_windows(a, task):
    rows = _register_windows()
    if not rows:
        return "ไม่มีหน้าต่าง"
    return "\n".join(_wline(i, w, act) for i, w, act in rows[:40])


def t_focus_window(a, task):
    w = resolve_window(a.get("window"), default_fg=False)
    check_forbidden(w)
    if not winio.bring_to_front(w["hwnd"]):
        raise ToolError(f'สลับไปที่ "{w["title"][:60]}" ไม่สำเร็จ')
    return f'สลับไปที่ "{w["title"][:60]}" ({w["process"]}) จอ {w["monitor"]} แล้ว'


# ---------- find_elements ----------

_CHROMIUM_WARM: set = set()
CHROMIUM_WARM_SECS = 3.0


def t_find_elements(a, task):
    u = U()
    near = a.get("near")
    w = None
    if near and not a.get("window"):
        # gaze point: search the window under that point, not the foreground one
        # (the user looked at one monitor while the foreground terminal on the other one was searched: refused)
        hw = win32gui.WindowFromPoint((int(near["x"]), int(near["y"])))
        if hw:
            w = winio.window_info(winio.root_hwnd(hw))
    w = w or resolve_window(a.get("window"))
    check_forbidden(w)
    limit = max(1, min(int(a.get("limit") or 30), 100))
    t_start = time.monotonic()
    nodes, trunc = u.walk(u.root_for(w["hwnd"]))
    # Chromium/Electron (Discord, VS Code, Slack...) builds its page a11y tree only after the first UIA
    # request, ~1-3 s later. First query on such a window: poll while the tree is still tiny (<= 15 nodes)
    # instead of returning empty (which used to push callers onto slow OCR). Once per hwnd; the whole warm-up,
    # walks included, is capped at CHROMIUM_WARM_SECS so it never blocks the single worker for long.
    if w["hwnd"] not in _CHROMIUM_WARM and win32gui.GetClassName(w["hwnd"]).startswith("Chrome_WidgetWin"):
        _CHROMIUM_WARM.add(w["hwnd"])
        deadline = t_start + CHROMIUM_WARM_SECS
        while len(nodes) <= 15 and time.monotonic() < deadline:
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))  # read-only: no yield on user input
            nodes, trunc = u.walk(u.root_for(w["hwnd"]))
    q = [t for t in str(a.get("query") or "").lower().split() if t]
    hits = []
    for n in nodes:
        hay = f"{n.ctype} {n.name} {n.autoid}".lower()
        if q:
            if all(t in hay for t in q):
                hits.append(n)
        elif n.ctype in u.INTERACTIVE and (n.name or n.ctype in ("Edit", "Document")):
            hits.append(n)
    if q:  # exact-name hits first, stable otherwise
        hits.sort(key=lambda n: 0 if n.name.lower() == " ".join(q) else 1)
    near = a.get("near")
    if near:
        hits = filter_near(hits, int(near["x"]), int(near["y"]), int(near.get("r", 200)), rect_of=lambda n: n.rect)
    hits = hits[:limit]
    head = f'[{WINS.put(w, key=w["hwnd"])}] {w["title"][:60]} — {w["process"]}'
    if not hits:
        extra = " (ต้นไม้ UIA ถูกตัดเพราะใหญ่/ช้า)" if trunc else ""
        return (f"{head}: ไม่พบ element{extra}" + (" — หน้าต่างนี้ไม่มี UIA (เช่น LINE/Qt, canvas) ใช้ ocr แทน (ocr คืน id #oN ใช้ click ได้)" if not nodes else ""))
    lines = []
    for n in hits:
        i = ELEMS.put(Elem(n, w["hwnd"], w["pid"], w["process"]))
        nm = n.name.replace('"', "'").replace("\n", " ")[:60]
        x, y, ww, hh = n.rect
        lines.append(f'{i} {n.ctype} "{nm}" @ ({x},{y},{ww},{hh})' + ("" if n.enabled else " [disabled]"))
    return head + f" ({len(hits)} รายการ{', ตัดผลบางส่วน' if trunc else ''})\n" + "\n".join(lines)


# ---------- action helpers ----------

def _get_elem(args):
    ent = (OCRS if str(args["id"]).strip().lower().startswith("#o") else ELEMS).get(args["id"])
    if ent is None:
        raise ToolError(f'ไม่พบ id {args["id"]} (หมดอายุ 120 วิ) ให้ find_elements ใหม่')
    if not win32gui.IsWindow(ent.hwnd):
        raise ToolError("หน้าต่างของ element นี้ปิดไปแล้ว ให้ find_elements ใหม่")
    return ent


def _live_rect(ent):
    if ent.el is None:   # OCR box: no live UIA element, the rect from the last ocr call is all we have
        return ent.rect
    try:
        r = U().live_rect(ent.el)
        if r[2] > 0 and r[3] > 0:
            return r
    except Exception:
        pass
    return ent.rect


def _check_point(x, y, pid=None):
    if not winio.point_on_screen(x, y):
        raise ToolError(f"จุด ({x},{y}) อยู่นอกจอ (element ถูกเลื่อนออกไป?)")
    hw = win32gui.WindowFromPoint((x, y))
    if not hw:
        raise ToolError("ไม่มีหน้าต่างที่จุดนั้น")
    wi = winio.window_info(winio.root_hwnd(hw))
    check_forbidden(wi)
    if pid is not None and wi["pid"] != pid:
        raise ToolError(f'มีหน้าต่างอื่นบังอยู่ ("{wi["title"][:40]}") ให้ focus_window ก่อน')
    return wi


def _highlight_wait(rect, task):
    if _FAST:
        task.check()
        return
    with Highlight(rect):
        task.sleep(HL_SECS)


def t_click(a, task):
    button = a.get("button", "left")
    if button not in ("left", "right", "middle"):
        raise ToolError("button ต้องเป็น left/right/middle")
    if a.get("id") not in (None, ""):
        ent = _get_elem(a)
        w = winio.window_info(ent.hwnd)
        check_forbidden(w)
        c = confirm_needed("click", a, ent.name, w["process"])
        if c:
            raise NeedsConfirm(c)
        if not ent.enabled:
            raise ToolError(f'"{ent.name}" ถูกปิดใช้งาน (disabled)')
        if winio.root_hwnd(win32gui.GetForegroundWindow()) != ent.hwnd:
            if not winio.bring_to_front(ent.hwnd):
                raise ToolError("สลับไปหน้าต่างของ element ไม่สำเร็จ")
            task.sleep(0.1)
        rect = _live_rect(ent)
        x, y = rect_center(rect)
        _check_point(x, y, ent.pid)
        label = f'{ent.ctype} "{ent.name[:40]}"'
    else:
        if "x" not in a or "y" not in a:
            raise ToolError("ต้องระบุ id หรือ x,y")
        x, y = int(a["x"]), int(a["y"])
        wi = _check_point(x, y)
        try:
            name = U().element_at(x, y).CurrentName or ""
        except Exception:
            name = ""
        c = confirm_needed("click", a, name, wi["process"])
        if c:
            raise NeedsConfirm(c)
        rect = (x - 15, y - 15, 30, 30)
        label = f'({x},{y})' + (f' "{name[:40]}"' if name else "")
    orig = win32api.GetCursorPos()
    _highlight_wait(rect, task)
    task.check()
    winio.mouse_click(x, y, button, bool(a.get("double")), task)
    time.sleep(0.05)
    cx, cy = win32api.GetCursorPos()
    if abs(cx - x) < 8 and abs(cy - y) < 8:   # only restore if the user did not move it meanwhile
        winio.move_cursor(*orig)
    return f'{"ดับเบิลคลิก" if a.get("double") else "คลิก"}{"ขวา" if button == "right" else ""} {label} แล้ว'


def _focus_for_typing(ent, task):
    w = winio.window_info(ent.hwnd)
    check_forbidden(w)
    if winio.root_hwnd(win32gui.GetForegroundWindow()) != ent.hwnd:
        if not winio.bring_to_front(ent.hwnd):
            raise ToolError("สลับไปหน้าต่างของ element ไม่สำเร็จ")
    rect = _live_rect(ent)
    _highlight_wait(rect, task)
    try:
        ent.el.SetFocus()
    except Exception:
        x, y = rect_center(rect)
        _check_point(x, y, ent.pid)
        winio.mouse_click(x, y, task=task)
    task.sleep(0.05)
    if winio.root_hwnd(win32gui.GetForegroundWindow()) != ent.hwnd:
        raise ToolError("โฟกัสหน้าต่างไม่สำเร็จ ไม่ได้พิมพ์")
    return w


def t_type_text(a, task):
    text = a.get("text")
    if not isinstance(text, str) or text == "":
        raise ToolError("ต้องระบุ text")
    if a.get("id") not in (None, ""):
        ent = _get_elem(a)
        w = winio.window_info(ent.hwnd)
        check_forbidden(w)
        c = confirm_needed("type_text", a, "", w["process"])
        if c:
            raise NeedsConfirm(c)
        orig = win32api.GetCursorPos()
        _focus_for_typing(ent, task)
        cx, cy = win32api.GetCursorPos()
        if (cx, cy) != orig and abs(cx - rect_center(_live_rect(ent))[0]) < 8:
            winio.move_cursor(*orig)
    else:
        w = winio.foreground_info()
        if not w:
            raise ToolError("ไม่มีหน้าต่างที่ active")
        check_forbidden(w)
        c = confirm_needed("type_text", a, "", w["process"])
        if c:
            raise NeedsConfirm(c)
        try:
            if _FAST:
                raise LookupError  # skip focused-element highlight
            fr = U().IUIA_.GetFocusedElement()
            r = U().live_rect(fr)
            if r[2] > 0 and r[3] > 0:
                _highlight_wait(r, task)
        except Yielded:
            raise
        except Exception:
            pass
    task.check()
    if len(text) > 300:
        winio.paste_text(text, task)
    else:
        winio.type_unicode(text, task, fast=_FAST)
    time.sleep(0.05 if _FAST else 0.2)  # let the target app drain its input queue before the next tool reads it
    return f'พิมพ์ {len(text)} ตัวอักษรใน "{w["title"][:40]}" แล้ว'


def t_press_keys(a, task):
    keys = str(a.get("keys") or "")
    try:
        vks = parse_keys(keys)
    except ValueError as e:
        raise ToolError(str(e))
    w = winio.foreground_info()
    if not w:
        raise ToolError("ไม่มีหน้าต่างที่ active")
    check_forbidden(w)
    c = confirm_needed("press_keys", a, "", w["process"])
    if c:
        raise NeedsConfirm(c)
    task.check()
    winio.press_vks(vks, task)
    time.sleep(0.05)
    return f'กด {keys} ใน "{w["title"][:40]}" แล้ว'


def t_scroll(a, task):
    d = str(a.get("direction") or "").lower()
    if d not in ("up", "down", "left", "right"):
        raise ToolError("direction ต้องเป็น up/down/left/right")
    amount = max(1, min(int(a.get("amount") or 5), 50))
    if a.get("id") not in (None, ""):
        ent = _get_elem(a)
        check_forbidden(winio.window_info(ent.hwnd))
        x, y = rect_center(_live_rect(ent))
        pid = ent.pid
    elif "x" in a and "y" in a:
        x, y, pid = int(a["x"]), int(a["y"]), None
    else:
        w = winio.foreground_info()
        if not w:
            raise ToolError("ไม่มีหน้าต่างที่ active")
        check_forbidden(w)
        x, y = rect_center(w["rect"])
        pid = w["pid"]
    _check_point(x, y, pid)
    orig = win32api.GetCursorPos()
    task.check()
    winio.mouse_wheel(x, y, d, amount)
    time.sleep(0.05)
    cx, cy = win32api.GetCursorPos()
    if abs(cx - x) < 8 and abs(cy - y) < 8:
        winio.move_cursor(*orig)
    return f"เลื่อน {d} {amount} ครั้งที่ ({x},{y}) แล้ว"


# ---------- read_text ----------

def t_read_text(a, task):
    u = U()
    if a.get("id") not in (None, ""):
        ent = _get_elem(a)
        check_forbidden(winio.window_info(ent.hwnd))
        if ent.el is None:
            return ent.name
        t = u.element_text(ent.el, 4000)
        return t[:4000] if t.strip() else f'{ent.ctype} "{ent.name}" ไม่มีข้อความ'
    w = resolve_window(a.get("window"))
    check_forbidden(w)
    nodes, _ = u.walk(u.root_for(w["hwnd"]))
    parts, seen = [], set()
    for n in nodes:
        if n.ctype in ("Edit", "Document"):
            t = u.element_text(n.el, 4000).strip()
        elif n.ctype in ("Text", "ListItem", "Button", "MenuItem", "TabItem", "CheckBox", "Hyperlink", "TreeItem", "DataItem"):
            t = n.name.strip()
        else:
            continue
        if t and t not in seen:
            seen.add(t)
            parts.append(t)
    out = "\n".join(parts)
    return out[:4000] if out else f'ไม่พบข้อความใน "{w["title"][:50]}" (UIA ว่าง ลอง screenshot)'


# ---------- screenshot ----------

def t_screenshot(a, task):
    from PIL import ImageGrab
    SHOTS.mkdir(exist_ok=True)
    vx, vy, vw, vh = winio.virtual_screen()
    im = ImageGrab.grab(all_screens=True)
    if all(k in a for k in ("x", "y", "w", "h")):
        x, y, w, h = (int(a[k]) for k in ("x", "y", "w", "h"))
        box = (max(vx, x) - vx, max(vy, y) - vy, min(vx + vw, x + w) - vx, min(vy + vh, y + h) - vy)
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ToolError("พื้นที่ที่ขอ อยู่นอกจอ")
        im = im.crop(box)
    p = SHOTS / f"shot-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.png"
    im.save(p)
    old = sorted(glob.glob(str(SHOTS / "shot-*.png")))[:-20]
    for f in old:
        try:
            os.remove(f)
        except OSError:
            pass
    return f"{p} ({im.width}x{im.height})"


# ---------- ocr ----------

class OcrBox:
    """Same surface as Elem (click/scroll/type_text only read these fields); el=None marks a screen-OCR box."""
    ctype = "OCR"
    enabled = True
    autoid = ""
    el = None

    def __init__(self, text, rect, hwnd, pid, process):
        self.name, self.rect, self.hwnd, self.pid, self.process = text, rect, hwnd, pid, process


def t_ocr(a, task):
    import ocr as ocrmod
    from PIL import ImageGrab
    import numpy as np
    vs = winio.virtual_screen()
    head = ""
    if all(a.get(k) not in (None, "") for k in ("x", "y", "w", "h")):
        reg = clip_region(*(int(a[k]) for k in ("x", "y", "w", "h")), vs)
        if reg is None:
            raise ToolError("พื้นที่ที่ขอ อยู่นอกจอ")
        cx, cy = rect_center(reg)
        hw = win32gui.WindowFromPoint((cx, cy))
        if hw:
            check_forbidden(winio.window_info(winio.root_hwnd(hw)))
        head = f"พื้นที่ ({reg[0]},{reg[1]},{reg[2]},{reg[3]})"
    else:
        w = resolve_window(a.get("window"))
        check_forbidden(w)
        reg = clip_region(*w["rect"], vs)
        if reg is None:
            raise ToolError("หน้าต่างนี้อยู่นอกจอ")
        head = f'[{WINS.put(w, key=w["hwnd"])}] {w["title"][:50]} — {w["process"]}'
        hw = win32gui.WindowFromPoint(rect_center(reg))
        if hw and winio.root_hwnd(hw) != w["hwnd"]:
            head += " (หน้าต่างนี้ถูกบังบางส่วน ผลอาจเป็นข้อความของหน้าต่างอื่น ให้ focus_window ก่อน)"
    limit = max(1, min(int(a.get("limit") or 40), 200))
    query = str(a.get("query") or "").strip()
    t0 = time.time()
    im = ImageGrab.grab(all_screens=True)
    rx, ry, rw, rh = reg
    im = im.crop((rx - vs[0], ry - vs[1], rx - vs[0] + rw, ry - vs[1] + rh)).convert("RGB")
    try:
        raw = ocrmod.recognize(np.asarray(im))
    except RuntimeError as e:
        raise ToolError(str(e))
    items = [(poly_to_rect(p, rx, ry), t.strip(), s) for p, t, s in raw]
    items = reading_order(items)
    if query:
        ranked = fuzzy_rank(query, items, text_of=lambda it: it[1])
        shown = [(it, sc) for sc, it in ranked[:limit]]
        total = len(ranked)
    else:
        shown = [(it, None) for it in items[:limit]]
        total = len(items)
    ms = int((time.time() - t0) * 1000)
    if not shown:
        return f"{head}: OCR ไม่พบข้อความ" + (f' ที่ตรงกับ "{query}"' if query else "") + f" ({len(items)} บรรทัดบนจอ, {ms} ms) — ถ้าเป็นไอคอน/รูป ใช้ look"
    cache_hw = {}
    lines = []
    for (rect, text, _), sc in shown:
        cx, cy = rect_center(rect)
        hw = win32gui.WindowFromPoint((cx, cy))
        root = winio.root_hwnd(hw) if hw else 0
        if root not in cache_hw:
            cache_hw[root] = winio.window_info(root) if root else None
        wi = cache_hw[root]
        i = OCRS.put(OcrBox(text, rect, root, wi["pid"] if wi else None, wi["process"] if wi else ""))
        nm = text.replace('"', "'").replace("\n", " ")[:80]
        lines.append(f'{i} "{nm}" @ ({rect[0]},{rect[1]},{rect[2]},{rect[3]})' + (f" ~{sc:.2f}" if sc is not None and sc < 1 else ""))
    more = f", แสดง {len(shown)} จาก {total}" if total > len(shown) else ""
    return f"{head} OCR {len(items)} บรรทัด {ms} ms{more}\n" + "\n".join(lines)


# ---------- open_app ----------

ALIASES = {"โน้ตแพด": "notepad", "สมุดบันทึก": "notepad", "เครื่องคิดเลข": "calc", "calculator": "calc",
           "เพ้นท์": "mspaint", "paint": "mspaint", "ไลน์": "line", "เอ็กซ์พลอเรอร์": "explorer",
           "file explorer": "explorer", "ไฟล์เอ็กซ์พลอเรอร์": "explorer", "เวิร์ด": "word", "เอ็กเซล": "excel",
           "ครอม": "chrome", "โครม": "chrome", "เอดจ์": "edge", "เอจ์": "edge",
           "settings": "ms-settings:", "การตั้งค่า": "ms-settings:", "ตั้งค่า": "ms-settings:"}
_lnk_cache = (0.0, [])


def _start_menu_links():
    global _lnk_cache
    if time.time() - _lnk_cache[0] < 60:
        return _lnk_cache[1]
    roots = [os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
             os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs")]
    links = []
    for r in roots:
        for p in glob.glob(os.path.join(r, "**", "*.lnk"), recursive=True):
            links.append((Path(p).stem.lower(), p))
    _lnk_cache = (time.time(), links)
    return links


URL_RE = re.compile(r"^(https?://|www\.)|^[\w-]+(\.[\w-]+)+(/\S*)?$", re.I)


def t_open_app(a, task):
    target = str(a.get("target") or "").strip()
    if not target:
        raise ToolError("ต้องระบุ target")
    low = ALIASES.get(target.lower(), target.lower())
    stem = Path(low).stem
    if stem in FORBIDDEN_OPEN or low in FORBIDDEN_OPEN:
        raise ToolError("ห้ามเปิดโปรแกรมนี้")
    if URL_RE.match(target) and not os.path.exists(target) and not shutil.which(target):
        url = target if re.match(r"^https?://", target, re.I) else "https://" + target
        os.startfile(url)  # default browser
        return f"เปิดเว็บ {url} แล้ว"
    if os.path.exists(target):
        os.startfile(target)
        return f"เปิด {target} แล้ว"
    if low.endswith(":"):  # shell URI e.g. ms-settings:
        os.startfile(low)
        return f"เปิด {low} แล้ว"
    links = _start_menu_links()
    # exact name, then exe on PATH, then a link whose name starts with / contains a whole word of the target
    # (plain substring picked "EcoStruxure Operator Terminal Expert" for "terminal"); shortest name wins
    pick = next((p for s, p in links if s == low), None)
    exe = None if pick else shutil.which(low) or shutil.which(low + ".exe")
    if exe:
        os.startfile(exe)
        return f"เปิด {exe} แล้ว"
    if not pick:
        word = re.compile(r"(^|)" + re.escape(low) + r"")
        cands = sorted((s, p) for s, p in links if s.startswith(low)) or sorted((s, p) for s, p in links if word.search(s))
        pick = min(cands, key=lambda c: len(c[0]))[1] if cands else None
    if pick:
        os.startfile(pick)
        return f'เปิด "{Path(pick).stem}" จากเมนู Start แล้ว'
    try:
        os.startfile(low)  # App Paths / shell verbs (e.g. notepad, calc)
        return f"เปิด {low} แล้ว"
    except OSError:
        raise ToolError(f'ไม่พบโปรแกรม "{target}"')


def _line_tool(fn_name):
    """LINE tools live in line.py, which imports this module: load it on first use (also keeps COM/PIL lazy)."""
    def run(a, task):
        import line
        return getattr(line, fn_name)(a, task)
    run.__name__ = fn_name
    return run


TOOLS = {
    "screen_info": t_screen_info, "list_windows": t_list_windows, "focus_window": t_focus_window,
    "find_elements": t_find_elements, "click": t_click, "type_text": t_type_text, "press_keys": t_press_keys,
    "scroll": t_scroll, "open_app": t_open_app, "read_text": t_read_text, "screenshot": t_screenshot, "ocr": t_ocr,
    "line_open_chat": _line_tool("t_line_open_chat"), "line_prepare": _line_tool("t_line_prepare"),
    "line_send": _line_tool("t_line_send"), "line_cancel": _line_tool("t_line_cancel"),
}


def run_tool(name, args) -> dict:
    """Execute one tool and map the outcome to the response contract."""
    fn = TOOLS.get(name)
    if fn is None:
        return {"ok": False, "error": f"ไม่รู้จักเครื่องมือ {name}"}
    if not isinstance(args, dict):
        return {"ok": False, "error": "args ต้องเป็น object"}
    ELEMS.purge()
    WINS.purge()
    OCRS.purge()
    global _FAST
    _FAST = bool(args.pop("fast", False))
    task = Task()
    try:
        return {"ok": True, "result": fn(args, task)}
    except Yielded:
        return {"ok": False, "error": "ผู้ใช้ขยับเมาส์ หยุดแล้ว", "yielded": True}
    except NeedsConfirm as e:
        return {"ok": False, "needs_confirm": str(e)}
    except ToolError as e:
        return {"ok": False, "error": str(e)}
    except (KeyError, ValueError, TypeError) as e:
        return {"ok": False, "error": f"args ไม่ถูกต้อง: {e!r}"}
    except Exception as e:  # keep the server alive; report to the LLM
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
