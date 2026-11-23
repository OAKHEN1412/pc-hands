"""Self-test for pc-hands. Run:  python test_hands.py   (or pytest -k logic for the pure parts)

Part 1 is pure logic (no UI). Part 2 starts its own server on port 8798 and drives ONE Notepad window
that it opens itself, then closes it with "Don't save". Takes ~15 s and moves the real mouse/keyboard,
so do not touch the PC while it runs (user input makes the tools yield on purpose)."""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from logic import (IdCache, confirm_needed, filter_near, forbidden_reason, is_send_name, monitor_name,  # noqa: E402
                   parse_keys)

PORT = 8798
PIN = "selftest-" + os.urandom(4).hex()


# ---------------- part 1: pure logic ----------------

def test_logic_confirm_gate():
    for n in ["Send", "ส่ง", "Submit", "โพสต์", "Reply", "ตอบกลับ", "Publish", "เผยแพร่", "Call", "โทร", "Share", "แชร์", "send message"]:
        assert is_send_name(n), n
    for n in ["Save", "Cancel", "Sent items", "บันทึก", "", "x" * 80 + " send"]:
        assert not is_send_name(n), n
    assert confirm_needed("click", {}, "Send", "notepad.exe")
    assert confirm_needed("click", {"confirmed": True}, "Send", "LINE.exe") is None
    assert confirm_needed("click", {}, "Save", "LINE.exe") is None
    assert confirm_needed("press_keys", {"keys": "enter"}, "", "LINE.exe")
    assert confirm_needed("press_keys", {"keys": "ctrl+Enter"}, "", "OUTLOOK.EXE")
    assert confirm_needed("press_keys", {"keys": "enter"}, "", "WhatsApp.Root.exe")
    assert confirm_needed("press_keys", {"keys": "enter"}, "", "notepad.exe") is None
    assert confirm_needed("press_keys", {"keys": "ctrl+s"}, "", "LINE.exe") is None
    assert confirm_needed("press_keys", {"keys": "enter", "confirmed": True}, "", "LINE.exe") is None
    assert confirm_needed("type_text", {"text": "hi\n"}, "", "Discord.exe")
    assert confirm_needed("type_text", {"text": "hi"}, "", "Discord.exe") is None
    assert confirm_needed("type_text", {"text": "hi\n"}, "", "notepad.exe") is None


def test_logic_forbidden():
    assert forbidden_reason("Registry Editor", "regedit.exe")
    assert forbidden_reason("Windows Security", "SecHealthUI.exe")
    assert forbidden_reason("User Account Control", "consent.exe")
    assert forbidden_reason("x", "CredentialUIBroker.exe")
    assert forbidden_reason("Some app", "app.exe", elevated=True)
    assert forbidden_reason("Untitled - Notepad", "Notepad.exe") is None


def test_logic_id_cache():
    t = [0.0]
    c = IdCache("#", ttl=120, clock=lambda: t[0])
    a = c.put("A")
    b = c.put("B")
    assert (a, b) == ("#1", "#2")
    assert c.get("#1") == "A" and c.get("2") == "B" and c.get(2) == "B"
    t[0] = 119
    assert c.get("#1") == "A"
    t[0] = 125
    assert c.get("#1") is None
    w = IdCache("w", clock=lambda: t[0])
    assert w.put("x", key=77) == w.put("y", key=77) == "w1"
    assert w.get("W1") == "y"


def test_logic_near_sort():
    items = [SimpleNamespace(n=n, rect=r) for n, r in
             [("far", (1000, 1000, 10, 10)), ("mid", (140, 95, 20, 10)), ("close", (95, 95, 10, 10)), ("edge", (300, 100, 10, 10))]]
    got = filter_near(items, 100, 100, 100)
    assert [i.n for i in got] == ["close", "mid"], [i.n for i in got]
    assert filter_near(items, 100, 100, 5000)[0].n == "close"


def test_logic_keys_monitors():
    assert parse_keys("ctrl+shift+s") == [0x11, 0x10, 0x53]
    assert parse_keys("Enter") == [0x0D]
    assert parse_keys("alt+tab") == [0x12, 0x09]
    try:
        parse_keys("ctrl+nonsense")
        assert False
    except ValueError:
        pass
    assert monitor_name(-1920) == "LEFT" and monitor_name(0) == "MAIN" and monitor_name(1919) == "MAIN"


def test_yield_logic():
    import winio
    t = winio.Task()
    t.check()  # nothing happened since start
    winio.USER.last_input = time.monotonic() + 1  # simulate real user input after task start
    try:
        t.check()
        assert False, "should yield"
    except winio.Yielded:
        pass
    winio.USER.last_input = 0.0


def test_gate_via_tools_fake_element():
    """click on an element named 'Send' must return needs_confirm without touching the UI."""
    import tools
    import winio
    fg = winio.foreground_info()
    fake = SimpleNamespace(el=None, ctype="Button", name="Send", rect=(10, 10, 50, 20), autoid="", enabled=True,
                           hwnd=fg["hwnd"], pid=fg["pid"], process=fg["process"])
    i = tools.ELEMS.put(fake)
    orig, tools.check_forbidden = tools.check_forbidden, lambda w: None  # foreground is usually the (forbidden) terminal
    try:
        r = tools.run_tool("click", {"id": i})
    finally:
        tools.check_forbidden = orig
    assert r.get("needs_confirm") and not r["ok"], r
    print("   gate:", r)


# ---------------- part 2: live Notepad ----------------

def call(name, args=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/tool", json.dumps({"name": name, "args": args or {}}).encode(),
                                 {"x-pin": PIN, "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=40))


def notepad_hwnds():
    import winio
    return {w["hwnd"]: w for w in winio.enum_windows() if w["process"].lower() == "notepad.exe"}


def wait_for(fn, secs, step=0.2):
    end = time.time() + secs
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


def test_live_notepad():
    import win32con
    import win32gui
    import winio
    before = set(notepad_hwnds())
    env = dict(os.environ, HANDS_PIN=PIN, HANDS_PORT=str(PORT), PYTHONUTF8="1")
    srv = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env, cwd=str(HERE))
    hwnd = None
    try:
        assert wait_for(lambda: _health(), 8), "server did not start"
        # auth
        try:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{PORT}/health", headers={"x-pin": "wrong"}))
            assert False, "bad pin accepted"
        except urllib.error.HTTPError as e:
            assert e.code == 401
        t0 = time.time()
        print("  screen_info:", call("screen_info")["result"].replace("\n", " | "))
        r = call("open_app", {"target": "notepad"})
        assert r["ok"], r
        new = wait_for(lambda: set(notepad_hwnds()) - before, 6)
        assert new, "notepad window did not appear"
        hwnd = next(iter(new))
        title = notepad_hwnds()[hwnd]["title"]
        lw = call("list_windows")["result"]
        wid = re.search(r"\[(w\d+)\] " + re.escape(title), lw).group(1)
        r = call("focus_window", {"window": wid})
        assert r["ok"], r
        time.sleep(0.5)
        _dismiss_dialogs(wid)  # Notepad restores the previous session and may complain about missing files
        # Notepad reopens the user's session tabs: work in a brand-new tab so nothing of theirs is touched
        assert call("press_keys", {"keys": "ctrl+n"})["ok"]
        time.sleep(0.6)
        fe = call("find_elements", {"window": wid})["result"]
        print("  find_elements:", fe.splitlines()[:5])
        m = re.search(r"^(#\d+) (?:Edit|Document) ", fe, re.M)
        assert m, "no edit area found"
        eid = m.group(1)
        text = "สวัสดีครับ Hello ทดสอบ 123"
        r = call("type_text", {"text": text, "id": eid})
        assert r["ok"], r
        r = call("press_keys", {"keys": "enter"})
        assert r["ok"], r  # notepad is not a messaging app: no confirm
        r = call("type_text", {"text": "บรรทัดสอง line2"})
        assert r["ok"], r
        r = call("press_keys", {"keys": "ctrl+a"})
        assert r["ok"], r
        got = call("read_text", {"id": eid})
        assert got["ok"] and "สวัสดีครับ Hello ทดสอบ 123" in got["result"] and "บรรทัดสอง line2" in got["result"], got
        print("  read_text:", repr(got["result"]))
        got2 = call("read_text", {"window": wid})
        assert got2["ok"] and "Hello" in got2["result"], got2
        r = call("scroll", {"direction": "down", "amount": 2, "id": eid})
        assert r["ok"], r
        shot = call("screenshot", {"x": 0, "y": 0, "w": 200, "h": 100})
        assert shot["ok"] and Path(shot["result"].split(" (")[0]).exists(), shot
        print("  screenshot:", shot["result"])
        h = json.load(urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{PORT}/health", headers={"x-pin": PIN})))
        print("  health:", h)
        print(f"  live tools took {time.time() - t0:.1f}s")
        # close the new tab WITHOUT saving, then the window
        assert call("press_keys", {"keys": "ctrl+w"})["ok"]
        res = wait_for(lambda: _dont_save(wid), 3)
        assert res, "no save dialog found"
        r = call("click", {"id": res})
        print("  close dialog click:", r)
        assert r["ok"], r
        time.sleep(0.3)
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        assert wait_for(lambda: hwnd not in notepad_hwnds(), 4), "notepad did not close"
        hwnd = None
    finally:
        if hwnd and win32gui.IsWindow(hwnd):
            print("  WARNING: notepad window left open (hwnd %s)" % hwnd)
        srv.kill()  # our own PID only


def _health():
    try:
        return json.load(urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{PORT}/health", headers={"x-pin": PIN}), timeout=1))["ok"]
    except Exception:
        return False


def _dont_save(wid):
    out = call("find_elements", {"window": wid, "limit": 80})["result"]
    for line in out.splitlines():
        if line.startswith("#") and re.search(r"don.t save|ไม่(?:ต้อง)?บันทึก", line, re.I):
            return line.split()[0]
    return None


def _dismiss_dialogs(wid):
    for _ in range(4):
        out = call("find_elements", {"window": wid, "query": "cannot find the path"})["result"]
        if "ไม่พบ element" in out:
            return
        ok = re.search(r'^(#\d+) Button "(?:ตกลง|OK)"', call("find_elements", {"window": wid, "limit": 80})["result"], re.M)
        if not ok:
            return
        call("click", {"id": ok.group(1)})
        time.sleep(0.6)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v) and k != "test_live_notepad"]
    tests.append(test_live_notepad)
    failed = 0
    for fn in tests:
        print(fn.__name__)
        try:
            fn()
            print("  PASS")
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print("  FAIL", repr(e))
    print("ALL PASSED" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
