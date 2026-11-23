"""Command-line front end of the pc-hands LINE module (line.py), for unattended (bot) use.

Runs line.LineUI IN-PROCESS (no hands HTTP service, no MCP): `python line_cli.py <command> ...`
Result: ONE JSON object on stdout (UTF-8). Exit codes:
  0  ok (send: sent AND confirmed by the post-check)
  1  other error (bad arguments / unreadable file / unexpected exception before anything was sent)
  2  refused or check failed, NOTHING was sent (also: bot-mode off / chat not allowed / rate limit)
  3  image review stamp refused, LINE was not touched
  4  the Enter WAS pressed but the post-check did not confirm the message (or something broke after the Enter)

Commands (every one hands the keyboard focus back to the window that was foreground, like the MCP tools):
  open    --chat NAME
  prepare --chat NAME [--text-file F] [--image F] [--reply-json F] [--keep]   dry run, never presses Enter
  send    --chat NAME [--text-file F] [--image F] [--reply-json F]            needs bot-mode (see below)
  cancel                                                                       close quote bar + empty the box

--reply-json = {"sender","text","time","date", "index"?, "expected_count"?}; sender "*" = any sender but our own account.
date is "2026-11-19" (ISO) or the LINE
separator text ("19 พฤศจิกายน 2026", "November 19, 2026", วันนี้, Today ...), time is 24 h "13:18" (or "13.18", "1:18 PM").
Thai and English LINE UI both work; nothing here depends on the UI language (the module detects it per snapshot).

Bot-mode (the ONLY thing that lets `send` skip the human `confirmed` flag) is a JSON file on the machine, never in the
repo and never switched on by an environment variable:
  %LOCALAPPDATA%\\pc-hands\\bot-mode.json  (the path may be moved with env HANDS_BOT_MODE_FILE; a path inside this
  repo is refused)   {"enabled": true, "allowed_chats": ["Chat A", "Chat B"] | "*", "max_sends_per_minute": 6}
Every send attempt is appended (chat, sha256 of the text, result, time; never the text) to
%LOCALAPPDATA%\\pc-hands\\line-cli.log. Interactive MCP/HTTP line_send still needs `confirmed`; this changes nothing there.
"""
import argparse
import contextlib
import datetime
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import line
import tools
import winio
from logic import ToolError, line_text_problem

ROOT = Path(__file__).resolve().parent
CLI_BUDGET = 120.0            # seconds for one command (prepare + check + Enter + post-check); line.py's own default is 34
DEFAULT_MAX_PER_MINUTE = 6
IMAGE_MAX = 25 * 1024 * 1024
EXIT_OK, EXIT_ERR, EXIT_REFUSED, EXIT_STAMP, EXIT_UNCONFIRMED = 0, 1, 2, 3, 4

# seams for the tests (the real backend only exists on Windows with LINE running)
BACKEND_FACTORY = lambda: line.Win()          # noqa: E731
TASK_FACTORY = lambda: winio.Task()           # noqa: E731
UI_KW = {}


class CliExit(Exception):
    def __init__(self, result, code):
        super().__init__(result.get("error") or "")
        self.result, self.code = result, code


def refuse(cmd, msg, code=EXIT_REFUSED, **extra):
    d = {"ok": False, "cmd": cmd, "sent": False, "error": msg}
    d.update(extra)
    return CliExit(d, code)


# ---------------------------------------------------------------------------------------------------------
# paths, bot-mode switch, rate limit, log
# ---------------------------------------------------------------------------------------------------------

def data_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return Path(base) / "pc-hands"


def bot_mode_path():
    env = os.environ.get("HANDS_BOT_MODE_FILE")
    return Path(env) if env else data_dir() / "bot-mode.json"


def state_path(cfg_path):
    return cfg_path.with_name(cfg_path.stem + ".state.json")


def load_bot_mode(path=None):
    """-> (cfg, None) when bot-mode is on, else (None, reason). The switch is the FILE only: enabled must be literally
    true, allowed_chats must be present, a file inside the repo is refused. Never raises."""
    try:
        path = Path(path) if path else bot_mode_path()
        rp = path.resolve()
        try:
            rp.relative_to(ROOT)
            return None, f"bot-mode file {rp} is inside the repo: refused (keep it outside, e.g. %LOCALAPPDATA%\\pc-hands)"
        except ValueError:
            pass
        if not path.is_file():
            return None, f"bot-mode is not enabled (no config file at {path})"
        try:
            cfg = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as e:
            return None, f"bot-mode config {path} is unreadable ({type(e).__name__})"
        if not isinstance(cfg, dict):
            return None, f"bot-mode config {path} must be a JSON object"
        if cfg.get("enabled") is not True:
            return None, f'bot-mode is not enabled ("enabled" in {path} is not literally true)'
        chats = cfg.get("allowed_chats")
        if not (chats == "*" or (isinstance(chats, list) and chats and all(isinstance(c, str) and c.strip() for c in chats))):
            return None, f'bot-mode config {path}: "allowed_chats" must be "*" or a non-empty list of chat names'
        lim = cfg.get("max_sends_per_minute", DEFAULT_MAX_PER_MINUTE)
        if isinstance(lim, bool) or not isinstance(lim, int) or lim < 1:
            return None, f'bot-mode config {path}: "max_sends_per_minute" must be an integer >= 1'
        return {"path": path, "allowed_chats": chats, "max": lim}, None
    except Exception as e:
        return None, f"bot-mode config could not be checked ({type(e).__name__})"


def chat_allowed(cfg, chat):
    a = cfg["allowed_chats"]
    return a == "*" or chat.strip() in [c.strip() for c in a]


@contextlib.contextmanager
def _lock(path, timeout=10.0, stale=30.0):
    lp = str(path) + ".lock"
    t_end = time.time() + timeout
    while True:
        try:
            os.close(os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except OSError:
            try:
                if time.time() - os.path.getmtime(lp) > stale:
                    os.remove(lp)
                    continue
            except OSError:
                pass
            if time.time() > t_end:
                raise RuntimeError("rate-limit lock is busy")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            os.remove(lp)
        except OSError:
            pass


def rate_take(spath, limit, now=None):
    """Sliding 60 s window kept in a small state file next to the config. Takes one slot. -> (ok, info).
    Fails closed: an unreadable state file or a busy lock refuses the send."""
    now = time.time() if now is None else now
    try:
        with _lock(spath):
            sends = []
            if Path(spath).exists():
                d = json.loads(Path(spath).read_text(encoding="utf-8"))
                sends = [float(t) for t in d["sends"]]
            sends = [t for t in sends if now - 60 < t <= now + 5]
            if len(sends) >= limit:
                return False, f"rate limit: {len(sends)} send attempts in the last 60 s (max {limit}); retry in {int(60 - (now - min(sends))) + 1} s"
            sends.append(now)
            tmp = str(spath) + ".tmp"
            Path(tmp).write_text(json.dumps({"sends": sends}), encoding="utf-8")
            os.replace(tmp, spath)
            return True, ""
    except Exception as e:
        return False, f"rate limit state unusable ({type(e).__name__}: {e}); delete {spath} after checking it"


def log_path():
    return data_dir() / "line-cli.log"


def log_event(**rec):
    """One JSON line per send attempt. NEVER the message text (only its sha256). Never raises."""
    try:
        rec = {"ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "pid": os.getpid(), **rec}
        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------------------------------------

def sha256_text(t):
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def need_chat(cmd, chat):
    chat = (chat or "").strip()
    if not chat or "\n" in chat or "\r" in chat:
        raise refuse(cmd, "--chat must be a non-empty single-line chat name", EXIT_ERR)
    return chat


def read_text_file(cmd, path):
    """UTF-8 (BOM ok), CRLF -> LF, trailing newlines dropped (the module refuses a text ending in a newline);
    inner newlines stay and are typed with Shift+Enter."""
    try:
        raw = Path(path).read_bytes()
        text = raw.decode("utf-8-sig")
    except Exception as e:
        raise refuse(cmd, f"cannot read text file {path} as UTF-8 ({type(e).__name__})", EXIT_ERR)
    text = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    if not text.strip():
        raise refuse(cmd, f"text file {path} is empty", EXIT_ERR)
    why = line_text_problem(text)
    if why:
        raise refuse(cmd, why)
    return text


def read_reply_json(cmd, path):
    try:
        rt = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception as e:
        raise refuse(cmd, f"cannot read reply json {path} ({type(e).__name__})", EXIT_ERR)
    if not isinstance(rt, dict):
        raise refuse(cmd, "reply json must be an object {sender,text,time,date,index?,expected_count?}", EXIT_ERR)
    for k in ("sender", "text", "time", "date"):
        if not isinstance(rt.get(k), str) or not rt[k].strip():
            raise refuse(cmd, f"reply json needs a non-empty string {k}", EXIT_ERR)
    for k in ("index", "expected_count"):
        v = rt.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
            raise refuse(cmd, f"reply json {k} must be an integer >= 1", EXIT_ERR)
    return rt


def image_gate(path):
    """Image review gate for unattended sends. <image>.review.json must say verdict PASS and carry the sha256 of the
    CURRENT bytes of the image. Touches nothing. -> (reason, bytes, sha) ; reason "" = pass."""
    if not path:
        return "no --image given", None, None
    p = os.path.abspath(path)
    if not os.path.isfile(p):
        return f"image not found: {p}", None, None
    sf = p + ".review.json"
    if not os.path.isfile(sf):
        return "no .review.json next to the image", None, None
    try:
        st = json.loads(Path(sf).read_text(encoding="utf-8-sig"))
        if not isinstance(st, dict):
            raise ValueError
    except Exception:
        return "stamp file unreadable", None, None
    if st.get("verdict") != "PASS":
        return f"stamp verdict is {st.get('verdict')!r}, not PASS", None, None
    want = st.get("sha256")
    if not isinstance(want, str) or not want.strip():
        return "stamp has no sha256", None, None
    try:
        data = Path(p).read_bytes()          # read ONCE: hash these bytes, and these exact bytes get pasted
    except Exception:
        return "image unreadable", None, None
    sha = hashlib.sha256(data).hexdigest()
    if want.strip().lower() != sha:
        return "image changed after review (sha256 differs)", None, None
    return "", data, sha


@contextlib.contextmanager
def staged_image(cmd, path):
    """Gate the image (exit 3), then hand LINE a private copy of the VERIFIED bytes, so the file cannot change between
    the check and the paste. Yields (staged_path, sha256) or (None, None) when no image was asked for."""
    if not path:
        yield None, None
        return
    why, data, sha = image_gate(path)
    if why:
        raise refuse(cmd, f"image has no valid review stamp ({why})", EXIT_STAMP, stamp_refused=True)
    ext = os.path.splitext(path)[1].lower()
    if ext not in line.IMAGE_EXT:
        raise refuse(cmd, f"not an image extension: {ext}")
    if len(data) > IMAGE_MAX:
        raise refuse(cmd, "image is larger than 25 MB")
    d = data_dir() / "stage"
    d.mkdir(parents=True, exist_ok=True)
    sp = d / f"{sha[:24]}{ext}"
    sp.write_bytes(data)
    try:
        yield str(sp), sha
    finally:
        try:
            sp.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------------------------------------
# running the module
# ---------------------------------------------------------------------------------------------------------

class _Spy:
    """Backend wrapper that counts the Enter key presses, so an error after the Enter is never reported as 'nothing sent'."""

    def __init__(self, be):
        self._be = be
        self.enters = 0

    def __getattr__(self, name):
        return getattr(self._be, name)

    def press(self, vks, task):
        if list(vks) == list(line.ENTER):
            self.enters += 1
        return self._be.press(vks, task)


def make_ui():
    spy = _Spy(BACKEND_FACTORY())
    return line.LineUI(spy, TASK_FACTORY(), budget=CLI_BUDGET, **UI_KW), spy


def with_cursor(ui, fn):
    """Put the mouse pointer back where it was (like line._run)."""
    orig = None
    try:
        orig = ui.be.cursor()
    except Exception:
        pass
    try:
        return fn()
    finally:
        if orig is not None:
            try:
                ui.be.move_cursor(*orig)
            except Exception:
                pass


def cancel_body(ui):
    """Close an armed quote bar and empty the box; reports the state afterwards (like line.t_line_cancel)."""
    snap = ui.snapshot()
    out = {"quote_closed": False, "box_cleared": False}
    if ui.quote_armed(snap):
        snap = ui.close_quote(snap)
        out["quote_closed"] = True
    if snap.edit is not None:
        if ui.box_text(snap) != "":
            out["box_cleared"] = True
        ui.clear_box(snap)
    s2 = ui.snapshot()
    out["quote_armed_after"] = bool(ui.quote_armed(s2))
    out["box_empty_after"] = (ui.box_text(s2) == "") if s2.edit is not None else None
    out["clean"] = (not out["quote_armed_after"]) and out["box_empty_after"] is not False
    return out


def _quoted_sender(exp):
    """The GUI name of the quoted message's sender (the real name even when reply json said "*"); None without a quote."""
    q = exp.get("quote")
    return q["sender"] if q else None


def _obs_summary(obs):
    return {k: obs[k] for k in ("header", "locale", "armed", "thumb_sim", "media_ok") if k in obs}


# ---------------------------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------------------------

def cmd_open(ns):
    chat = need_chat("open", ns.chat)
    ui, spy = make_ui()
    try:
        snap, note = with_cursor(ui, lambda: ui.run_focused(lambda: ui.open_chat(chat)))
    except ToolError as e:
        return {"ok": False, "cmd": "open", "sent": False, "chat": chat, "error": str(e)}, EXIT_REFUSED
    return {"ok": True, "cmd": "open", "sent": False, "chat": snap.header, "focus": note.strip(" |")}, EXIT_OK


def load_message_inputs(cmd, ns):
    text = read_text_file(cmd, ns.text_file) if ns.text_file else ""
    rt = read_reply_json(cmd, ns.reply_json) if ns.reply_json else None
    if not text and not ns.image:
        raise refuse(cmd, "give --text-file and/or --image", EXIT_ERR)
    return text, rt


def cmd_prepare(ns):
    chat = need_chat("prepare", ns.chat)
    text, rt = load_message_inputs("prepare", ns)
    with staged_image("prepare", ns.image) as (image, _sha):
        ui, spy = make_ui()

        def body():
            exp, extra, obs = ui._prepare(chat, text, image, rt)      # arms + full pre-send check; never an Enter
            cleanup = None
            if not ns.keep:
                try:
                    cleanup = cancel_body(ui)
                except ToolError as e:
                    cleanup = {"clean": False, "error": str(e)}
            return exp, obs, cleanup

        try:
            (exp, obs, cleanup), note = with_cursor(ui, lambda: ui.run_focused(body, on_fail=ui.cleanup))
        except ToolError as e:
            return {"ok": False, "cmd": "prepare", "sent": False, "chat": chat, "check_ok": False, "error": str(e)}, EXIT_REFUSED
    res = {"ok": True, "cmd": "prepare", "sent": False, "chat": chat, "check_ok": True, "kept": bool(ns.keep),
           "summary": line.line_summary(exp), "evidence": line._evidence(exp, obs), "obs": _obs_summary(obs),
           "quoted_sender": _quoted_sender(exp), "focus": note.strip(" |")}
    code = EXIT_OK
    if cleanup is not None:
        res["cleanup"] = cleanup
        if not cleanup.get("clean"):
            res["ok"], code = False, EXIT_ERR
            res["error"] = "check passed but the quote/box could not be cleared afterwards: " + str(cleanup.get("error") or cleanup)
    return res, code


def cmd_send(ns):
    chat = need_chat("send", ns.chat)
    rec = {"cmd": "send", "chat": chat}
    try:
        res, code = _send(ns, chat, rec)
    except CliExit as e:
        res, code = e.result, e.code
    except Exception as e:
        res, code = {"ok": False, "cmd": "send", "sent": False, "error": f"{type(e).__name__}: {e}"}, EXIT_ERR
    res.setdefault("exit", code)
    if "result" not in rec:
        rec["result"] = {EXIT_STAMP: "refused-image", EXIT_REFUSED: "refused"}.get(code, "error-" + str(code))
    rec["exit"] = code
    log_event(**rec)
    return res, code


def _send(ns, chat, rec):
    cfg, why = load_bot_mode()
    if cfg is None:
        rec["result"] = "refused-botmode"
        raise refuse("send", why, bot_mode=False)
    if not chat_allowed(cfg, chat):
        rec["result"] = "refused-chat"
        raise refuse("send", f"chat {chat!r} is not in allowed_chats of the bot-mode config", bot_mode=True)
    try:
        text, rt = load_message_inputs("send", ns)
    except CliExit:
        rec["result"] = "refused-input"
        raise
    if text:
        rec["text_sha256"] = sha256_text(text)
    rec["reply"] = bool(rt)
    with staged_image("send", ns.image) as (image, sha):
        if sha:
            rec["image_sha256"] = sha
        ok, why = rate_take(state_path(cfg["path"]), cfg["max"])
        if not ok:
            rec["result"] = "refused-rate"
            raise refuse("send", why, rate_limited=True)
        ui, spy = make_ui()

        def body():
            exp, extra, obs = ui._prepare(chat, text, image, rt)       # arm + check #1
            report, job = ui.send_core(exp, extra)                      # check #2, THE Enter, GUI post-check, audit starts
            return exp, obs, report, job

        try:
            (exp, obs, report, job), note = with_cursor(ui, lambda: ui.run_focused(body, on_fail=ui.cleanup))
        except ToolError as e:
            if spy.enters:
                rec["result"] = "sent-error"
                return {"ok": False, "cmd": "send", "sent": True, "confirmed": False, "chat": chat,
                        "error": "Enter was pressed, then: " + str(e)}, EXIT_UNCONFIRMED
            rec["result"] = "refused-check"
            return {"ok": False, "cmd": "send", "sent": False, "chat": chat, "error": str(e)}, EXIT_REFUSED
        except Exception as e:
            if spy.enters:
                rec["result"] = "sent-error"
                return {"ok": False, "cmd": "send", "sent": True, "confirmed": False, "chat": chat,
                        "error": f"Enter was pressed, then {type(e).__name__}: {e}"}, EXIT_UNCONFIRMED
            rec["result"] = "error"
            return {"ok": False, "cmd": "send", "sent": False, "chat": chat,
                    "error": f"{type(e).__name__}: {e}"}, EXIT_ERR
    # the Enter was pressed: from here on any problem is "sent, not confirmed" (4), never "nothing sent"
    try:
        seg = job.result() if job is not None else None    # the DB audit ran while the focus was handed back
        post = dict(getattr(job, "post_check", None) or {"box_empty": False, "row_match": False})
        db = _db_state(seg)
        # confirmed = box empty AND (GUI row matches OR DB audit confirmed); a DB "notfound" never confirms
        if not post["box_empty"] or db == "notfound":
            confirmed_by = None
        elif post["row_match"] and db == "confirmed":
            confirmed_by = "gui+db"
        elif db == "confirmed":
            confirmed_by = "db"
        elif post["row_match"]:
            confirmed_by = "gui"
        else:
            confirmed_by = None
        confirmed = confirmed_by is not None
        rec["result"] = "sent-confirmed" if confirmed else "sent-unconfirmed"
        rec["post_check"], rec["db"] = post, db
        return {"ok": confirmed, "cmd": "send", "sent": True, "confirmed": confirmed, "confirmed_by": confirmed_by,
                "chat": chat, "post_check": post, "db_audit": {"state": db, "detail": seg}, "report": report,
                "evidence": line._evidence(exp, obs), "quoted_sender": _quoted_sender(exp),
                "focus": note.strip(" |")}, (EXIT_OK if confirmed else EXIT_UNCONFIRMED)
    except Exception as e:
        rec["result"] = "sent-error"
        return {"ok": False, "cmd": "send", "sent": True, "confirmed": False, "chat": chat, "report": report,
                "error": f"Enter was pressed, then {type(e).__name__}: {e}"}, EXIT_UNCONFIRMED


def _db_state(seg):
    if not seg:
        return "disabled"
    if seg.startswith("DB: ยืนยัน"):
        return "confirmed"
    if seg.startswith("DB: ไม่พบข้อความ") or seg.startswith("DB: ไม่พบรูป"):
        return "notfound"
    if seg.startswith("DB: ข้าม"):
        return "skipped"
    return "unavailable"


def cmd_cancel(ns):
    line.STORE.drop()
    ui, spy = make_ui()
    try:
        out, note = with_cursor(ui, lambda: ui.run_focused(lambda: cancel_body(ui)))
    except ToolError as e:
        return {"ok": False, "cmd": "cancel", "sent": False, "error": str(e)}, EXIT_ERR
    code = EXIT_OK if out["clean"] else EXIT_ERR
    return {"ok": out["clean"], "cmd": "cancel", "sent": False, **out, "focus": note.strip(" |")}, code


# ---------------------------------------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message):          # argparse would exit 2, which here means "refused"
        raise CliExit({"ok": False, "sent": False, "error": f"usage: {message}"}, EXIT_ERR)


def build_parser():
    p = _Parser(prog="line_cli", description=__doc__.split("\n")[0])
    p.add_argument("--yield-to-user", action="store_true",
                   help="stop when a human touches the mouse/keyboard (starts the input hooks; off by default for unattended bots)")
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    o = sub.add_parser("open")
    o.add_argument("--chat", required=True)
    for name in ("prepare", "send"):
        s = sub.add_parser(name)
        s.add_argument("--chat", required=True)
        s.add_argument("--text-file")
        s.add_argument("--image")
        s.add_argument("--reply-json")
        if name == "prepare":
            s.add_argument("--keep", action="store_true", help="leave the armed message in LINE instead of cancelling")
    sub.add_parser("cancel")
    return p


COMMANDS = {"open": cmd_open, "prepare": cmd_prepare, "send": cmd_send, "cancel": cmd_cancel}


def emit(res):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.stdout.write(json.dumps(res, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv=None):
    try:
        ns = build_parser().parse_args(argv)
        tools._FAST = True                    # no highlight, batched typing (same as the MCP sessions)
        if ns.yield_to_user:
            winio.USER.start()
        res, code = COMMANDS[ns.cmd](ns)
    except CliExit as e:
        res, code = e.result, e.code
    except Exception as e:
        res, code = {"ok": False, "sent": False, "error": f"{type(e).__name__}: {e}"}, EXIT_ERR
    res.setdefault("exit", code)
    emit(res)
    return code


if __name__ == "__main__":
    sys.exit(main())
