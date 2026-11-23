"""stdio MCP server that exposes the pc-hands tools to an MCP client (Claude Code, `claude -p`, ...).

Wraps POST http://127.0.0.1:8792/tool (header x-pin = <repo>/.pin, env HANDS_PIN overrides). Pure stdlib + `mcp`
(no torch/numpy). Env: HANDS_URL (default http://127.0.0.1:8792).

Unattended jobs: the `confirmed` argument is deliberately NOT exposed. A tool that touches other people
(send/post/call) comes back as text telling Claude to skip it and report it, never to retry.

The one exception is `line_send`, which takes `confirmed: bool` for INTERACTIVE sessions (the human approves the exact
message in the conversation, then Claude calls line_send again with confirmed=true). So that an unattended
job can never use it, set env HANDS_UNATTENDED=1 on that job's MCP registration: with it, `call()` strips
`confirmed` from every request, and line_send always answers NEEDS_CONFIRMATION. (Whatever launches the
unattended job has to set that variable; this file only honours it.)
"""
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent
HANDS_URL = os.environ.get("HANDS_URL", "http://127.0.0.1:8792")
FAST = os.environ.get("HANDS_FAST") == "1"  # no highlight, batched typing (Claude Code session)
PIN = (os.environ.get("HANDS_PIN") or (ROOT / ".pin").read_text(encoding="utf-8")).strip()

mcp = FastMCP("pc-hands")


def _launched_by_print_mode() -> bool:
    """True when an ancestor is `claude -p/--print` (an unattended job). Such a job loads the user-scope pc-hands too,
    so the env flag alone is not enough: whichever registration wins, a headless run must never confirm."""
    try:
        import psutil
        p = psutil.Process().parent()
        for _ in range(6):
            if p is None:
                break
            if "claude" in p.name().lower():
                cmd = p.cmdline()
                if "-p" in cmd or "--print" in cmd:
                    return True
            p = p.parent()
    except Exception:
        return True   # cannot tell -> treat as unattended (fail safe: no confirming)
    return False


_PRINT_MODE = _launched_by_print_mode()


def unattended() -> bool:
    return os.environ.get("HANDS_UNATTENDED") == "1" or _PRINT_MODE


def call(name: str, args: dict) -> str:
    args = {k: v for k, v in args.items() if v is not None}
    if unattended():
        args.pop("confirmed", None)   # an unattended job can never confirm anything, whatever the model passes
    if FAST:
        args["fast"] = True
    req = urllib.request.Request(HANDS_URL + "/tool", json.dumps({"name": name, "args": args}).encode("utf-8"),
                                {"Content-Type": "application/json", "x-pin": PIN})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=60).read())
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read())
        except Exception:
            return f"ERROR: hands service returned HTTP {e.code}"
    except Exception as e:
        return f"ERROR: hands service (pc-hands on {HANDS_URL}) is not reachable: {e}"
    if d.get("yielded"):
        return (f"YIELDED: {d.get('error', '')} The user touched the mouse/keyboard, so the action was aborted. "
                "Stop controlling the desktop now and do not retry; report this in your summary.")
    if d.get("needs_confirm") and name == "line_send" and not unattended():
        return (f"NEEDS_CONFIRMATION: {d['needs_confirm']} Show the user the chat and the exact message above. Only after "
                "the user has explicitly approved THIS message in this conversation, call line_send again with the same "
                "token and confirmed=true. Never confirm on your own.")
    if d.get("needs_confirm"):
        return (f"NEEDS_CONFIRMATION: {d['needs_confirm']} This action contacts other people and needs the user's "
                "spoken confirmation. This is an unattended job: do NOT retry it and never set confirmed yourself. "
                "Skip the action and say in your final summary that it still needs the user's go-ahead.")
    if d.get("ok"):
        return str(d.get("result", ""))
    return "ERROR: " + str(d.get("error") or "failed")


@mcp.tool()
def screen_info() -> str:
    """Show both monitors of this PC and the active window (title, process)."""
    return call("screen_info", {})


@mcp.tool()
def list_windows() -> str:
    """List visible windows with ids like [w1] title - process (monitor)."""
    return call("list_windows", {})


@mcp.tool()
def focus_window(window: str) -> str:
    """Switch to a window. `window` = id from list_windows (e.g. w3) or part of its title."""
    return call("focus_window", {"window": window})


@mcp.tool()
def find_elements(query: Optional[str] = None, window: Optional[str] = None, near_x: Optional[int] = None,
                  near_y: Optional[int] = None, near_r: int = 250, limit: int = 30) -> str:
    """Search the UI Automation tree for buttons/fields/menus. Returns ids (valid 120 s) with names and rects.
    Optional near_x/near_y/near_r restricts to elements near a screen point."""
    near = {"x": near_x, "y": near_y, "r": near_r} if near_x is not None and near_y is not None else None
    return call("find_elements", {"query": query, "window": window, "near": near, "limit": limit})


@mcp.tool()
def click(id: Optional[str] = None, x: Optional[int] = None, y: Optional[int] = None, button: str = "left",
          double: bool = False) -> str:
    """Click an element by id (from find_elements; preferred) or by screen x,y. Sending/posting/calling is gated."""
    return call("click", {"id": id, "x": x, "y": y, "button": button, "double": double})


@mcp.tool()
def type_text(text: str, id: Optional[str] = None) -> str:
    """Type text (Thai OK). With `id` the element is focused first."""
    return call("type_text", {"text": text, "id": id})


@mcp.tool()
def press_keys(keys: str) -> str:
    """Press a key combo such as ctrl+s, enter, alt+tab, win+d."""
    return call("press_keys", {"keys": keys})


@mcp.tool()
def scroll(direction: str, amount: int = 5, id: Optional[str] = None, x: Optional[int] = None,
           y: Optional[int] = None) -> str:
    """Scroll up/down/left/right, optionally at an element id or x,y."""
    return call("scroll", {"direction": direction, "amount": amount, "id": id, "x": x, "y": y})


@mcp.tool()
def open_app(target: str) -> str:
    """Open a program, file or URL (name, path or https://...)."""
    return call("open_app", {"target": target})


@mcp.tool()
def read_text(id: Optional[str] = None, window: Optional[str] = None) -> str:
    """Read the text of an element or window (cut at 4000 chars)."""
    return call("read_text", {"id": id, "window": window})


@mcp.tool()
def screenshot(x: Optional[int] = None, y: Optional[int] = None, w: Optional[int] = None,
               h: Optional[int] = None) -> str:
    """Save a PNG of the screen (optionally the x,y,w,h region) and return its file path. Use the Read tool to view it."""
    return call("screenshot", {"x": x, "y": y, "w": w, "h": h})


@mcp.tool()
def ocr(query: Optional[str] = None, window: Optional[str] = None, x: Optional[int] = None, y: Optional[int] = None,
        w: Optional[int] = None, h: Optional[int] = None, limit: int = 40) -> str:
    """Fast on-screen OCR (Thai + English, ~0.4-1.5 s, no vision model). Returns text lines like
    `#o5 "kirn521" @ (x,y,w,h)` in screen coordinates; ids can be passed to click. Use it where find_elements is empty
    (LINE, canvas/Electron apps). `query` = fuzzy filter (best match first). Default region = foreground window;
    `window` = id/title, or give x,y,w,h for a smaller (faster) region."""
    return call("ocr", {"query": query, "window": window, "x": x, "y": y, "w": w, "h": h, "limit": limit})


@mcp.tool()
def line_open_chat(chat: str) -> str:
    """Open a LINE desktop chat by its exact name (search box, exact-name row, header verified). Refuses when the
    name matches no chat or more than one."""
    return call("line_open_chat", {"chat": chat})


@mcp.tool()
def line_prepare(chat: str, text: Optional[str] = None, image_path: Optional[str] = None,
                 reply_to: Optional[dict] = None) -> str:
    """Prepare a LINE message WITHOUT sending: open/verify the chat, clear the box, optionally quote-reply, attach an
    image, type the text (newlines become Shift+Enter), then run the one-pass check. Returns a `token` (120 s).
    reply_to = {sender, text, time ("13.18"), date ("16 พฤศจิกายน 2026" / "วันนี้"), index?, expected_count?}: the
    message is identified by sender + EXACT text + time + date and refused unless exactly expected_count rows match
    (give index, 1 = topmost, when expected_count > 1)."""
    return call("line_prepare", {"chat": chat, "text": text, "image_path": image_path, "reply_to": reply_to})


@mcp.tool()
def line_send(token: str, confirmed: bool = False) -> str:
    """Send the message prepared by line_prepare (re-checks everything, presses Enter once, reports what it sees).
    `confirmed` may be true ONLY after the human explicitly approved this exact chat + message in this conversation;
    never set it on your own or in an unattended job (with env HANDS_UNATTENDED=1 it is dropped and the call is
    refused). Without it the call returns NEEDS_CONFIRMATION and sends nothing."""
    return call("line_send", {"token": token, "confirmed": bool(confirmed)})


@mcp.tool()
def line_cancel() -> str:
    """Abandon a prepared LINE message: close an armed quote, clear the input box, forget the token."""
    return call("line_cancel", {})


if __name__ == "__main__":
    mcp.run()
