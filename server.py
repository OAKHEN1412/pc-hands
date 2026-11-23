"""pc-hands - Windows desktop control over UI Automation. HTTP on 127.0.0.1:8792.

GET  /health            -> {"ok": true, "user_idle_ms": int, "uia8": bool|null}
POST /tool {name,args}  -> {"ok":true,"result":str} | {"ok":false,"error":str[,"yielded":true]} | {"ok":false,"needs_confirm":str}
Every request needs header x-pin == content of <repo>/.pin (env HANDS_PIN overrides, for tests).
"""
import hmac
import json
import os
import queue
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import winio  # sets DPI awareness; must come before anything that creates windows
import tools

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = int(os.environ.get("HANDS_PORT", "8792"))
TOOL_TIMEOUT = 40


def load_pin() -> str:
    env = os.environ.get("HANDS_PIN")
    if env:
        return env.strip()
    return (ROOT / ".pin").read_text(encoding="utf-8").strip()


PIN = load_pin()

# All UIA/COM/input work is serialised on one worker thread (COM apartments + one tool at a time).
_jobs: "queue.Queue" = queue.Queue()
# uia.UIA8_OK, stored by the worker thread after it imports uia (COM objects must not be created on the
# main thread). None until the worker has imported uia.
UIA8_OK = None


def _worker():
    global UIA8_OK
    try:
        import uia  # creates the COM objects here, on the worker thread
        UIA8_OK = uia.UIA8_OK
    except Exception:
        UIA8_OK = None  # tools will hit the same import error and report it per call
    while True:
        name, args, box, ev = _jobs.get()
        try:
            box.append(tools.run_tool(name, args))
        except BaseException as e:  # never kill the worker
            box.append({"ok": False, "error": f"{type(e).__name__}: {e}"})
        ev.set()


def call_tool(name, args) -> dict:
    box, ev = [], threading.Event()
    _jobs.put((name, args, box, ev))
    if not ev.wait(TOOL_TIMEOUT):
        return {"ok": False, "error": "เครื่องมือใช้เวลานานเกินไป"}
    return box[0]


class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows reuse lets two servers share a port silently


class Handler(BaseHTTPRequestHandler):
    server_version = "hands/1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        if hmac.compare_digest((self.headers.get("x-pin") or "").encode(), PIN.encode()):
            return True
        self._send(401, {"ok": False, "error": "pin ไม่ถูกต้อง"})
        return False

    def do_GET(self):
        if not self._authed():
            return
        if self.path.split("?")[0] == "/health":
            idle = winio.USER.idle_ms()
            return self._send(200, {"ok": True, "user_idle_ms": min(idle, 2 ** 31 - 1), "uia8": UIA8_OK})
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self._authed():
            return
        if self.path.split("?")[0] != "/tool":
            return self._send(404, {"ok": False, "error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            name, args = req["name"], req.get("args") or {}
        except Exception:
            return self._send(400, {"ok": False, "error": "body ต้องเป็น JSON {name, args}"})
        self._send(200, call_tool(str(name), args))


def main():
    winio.USER.start()
    threading.Thread(target=_worker, name="tool-worker", daemon=True).start()
    threading.Thread(target=lambda: __import__("ocr").warm(), name="ocr-warm", daemon=True).start()
    srv = Server((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"hands listening on http://{HOST}:{PORT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
