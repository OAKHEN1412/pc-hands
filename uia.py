"""UI Automation helpers (comtypes via pywinauto's IUIA singleton).
Import this module ONLY from the tool worker thread: COM objects are apartment-bound."""
import sys
import time

from pywinauto.uia_defines import IUIA

_u = IUIA()
DLL = _u.UIA_dll
# CUIAutomation8 + short timeouts: the default TransactionTimeout is 20 s, so one hung provider (LINE,
# a busy Explorer window) blocked the single worker for 40 s and every later tool failed. Fail fast instead.
UIA8_OK = False
try:
    import comtypes
    IUIA_ = comtypes.CoCreateInstance(DLL.CUIAutomation8._reg_clsid_, interface=DLL.IUIAutomation2,
                                      clsctx=comtypes.CLSCTX_INPROC_SERVER)
    IUIA_.ConnectionTimeout = 1000
    IUIA_.TransactionTimeout = 1500
    UIA8_OK = True
except Exception as e:
    IUIA_ = _u.iuia
    print(f"uia: CUIAutomation8 unavailable ({type(e).__name__}: {e}); using default UIA (no timeouts)",
          file=sys.stderr, flush=True)

CT = {50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox", 50004: "Edit", 50005: "Hyperlink",
      50006: "Image", 50007: "ListItem", 50008: "List", 50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
      50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar", 50015: "Slider", 50016: "Spinner",
      50017: "StatusBar", 50018: "Tab", 50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip",
      50023: "Tree", 50024: "TreeItem", 50025: "Custom", 50026: "Group", 50027: "Thumb", 50028: "DataGrid",
      50029: "DataItem", 50030: "Document", 50031: "SplitButton", 50032: "Window", 50033: "Pane",
      50034: "Header", 50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator"}
INTERACTIVE = {"Button", "CheckBox", "ComboBox", "Edit", "Hyperlink", "ListItem", "MenuItem", "RadioButton",
               "Slider", "Spinner", "TabItem", "TreeItem", "SplitButton", "Document", "DataItem", "Text"}

_cache = IUIA_.CreateCacheRequest()
for _p in (DLL.UIA_NamePropertyId, DLL.UIA_ControlTypePropertyId, DLL.UIA_BoundingRectanglePropertyId,
           DLL.UIA_IsOffscreenPropertyId, DLL.UIA_AutomationIdPropertyId, DLL.UIA_IsEnabledPropertyId):
    _cache.AddProperty(_p)
_COND = IUIA_.ControlViewCondition


class Node:
    __slots__ = ("el", "ctype", "name", "rect", "autoid", "enabled", "depth")

    def __init__(self, el, ctype, name, rect, autoid, enabled, depth):
        self.el, self.ctype, self.name, self.rect = el, ctype, name, rect
        self.autoid, self.enabled, self.depth = autoid, enabled, depth


def root_for(hwnd):
    return IUIA_.ElementFromHandleBuildCache(hwnd, _cache)


def _node(el, depth):
    r = el.CachedBoundingRectangle
    return Node(el, CT.get(el.CachedControlType, str(el.CachedControlType)), el.CachedName or "",
                (r.left, r.top, r.right - r.left, r.bottom - r.top), el.CachedAutomationId or "",
                bool(el.CachedIsEnabled), depth)


def walk(root_el, max_depth=14, max_nodes=1500, budget=0.9):
    """BFS over the control view with cached properties. Skips offscreen subtrees.
    Returns (nodes, truncated)."""
    t_end = time.monotonic() + budget
    out, visited = [], 0
    frontier = [(root_el, 0)]
    truncated = False
    while frontier:
        nxt = []
        for el, depth in frontier:
            if depth >= max_depth:
                continue
            if visited >= max_nodes or time.monotonic() > t_end:
                return out, True
            try:
                arr = el.FindAllBuildCache(DLL.TreeScope_Children, _COND, _cache)
            except Exception:
                truncated = True  # FindAll timed out or failed: this subtree's children are missing
                continue
            visited += 1
            for i in range(arr.Length):
                try:
                    ch = arr.GetElement(i)
                    if ch.CachedIsOffscreen:
                        continue
                    n = _node(ch, depth + 1)
                except Exception:
                    continue
                if n.rect[2] > 0 and n.rect[3] > 0:
                    out.append(n)
                nxt.append((ch, depth + 1))
        frontier = nxt
    return out, truncated


def live_rect(el):
    r = el.CurrentBoundingRectangle
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def element_text(el, limit=4000):
    """Best-effort text of an element: TextPattern, then ValuePattern, then Name."""
    try:
        p = el.GetCurrentPattern(DLL.UIA_TextPatternId).QueryInterface(DLL.IUIAutomationTextPattern)
        t = p.DocumentRange.GetText(limit)
        if t:
            return t
    except Exception:
        pass
    try:
        p = el.GetCurrentPattern(DLL.UIA_ValuePatternId).QueryInterface(DLL.IUIAutomationValuePattern)
        t = p.CurrentValue
        if t:
            return t
    except Exception:
        pass
    try:
        return el.CurrentName or ""
    except Exception:
        return ""


def element_at(x, y):
    from ctypes.wintypes import POINT
    return IUIA_.ElementFromPoint(POINT(x, y))
