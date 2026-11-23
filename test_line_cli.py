"""Tests for line_cli.py (command-line front end of the LINE module). NO GUI: the backend is test_line.FakeLine.
Run:  python -m pytest -q test_line_cli.py
Nothing here touches the real LINE, the real %LOCALAPPDATA% or a real bot-mode.json (all paths are under tmp_path)."""
import ast
import hashlib
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import line  # noqa: E402
import line_cli  # noqa: E402
from logic import ToolError  # noqa: E402
from test_line import COLORS, TODAY, TERM, FakeLine, FakeLineEN, FakeTask, history, history_en  # noqa: E402

OBJ = "\ufffc"
BOT_FILE = "bot-mode.json"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    line._NOTCH["px"] = None
    line._LAST_FG["hwnd"] = 0
    line.STORE.drop()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("HANDS_BOT_MODE_FILE", str(tmp_path / "cfg" / BOT_FILE))
    monkeypatch.setenv("HANDS_LINE_DB_AUDIT", "0")
    monkeypatch.setattr(line_cli, "TASK_FACTORY", FakeTask)
    monkeypatch.setattr(line_cli, "UI_KW", {"today": TODAY})
    yield
    line.STORE.drop()


def install(monkeypatch, fake):
    fake.fg = TERM
    monkeypatch.setattr(line_cli, "BACKEND_FACTORY", lambda: fake)
    return fake


def bot_on(tmp_path, **kw):
    cfg = {"enabled": True, "allowed_chats": ["Group A"], "max_sends_per_minute": 6}
    cfg.update(kw)
    p = tmp_path / "cfg" / BOT_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def wfile(tmp_path, name, text, bom=False):
    p = tmp_path / name
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return str(p)


def run(capsys, *argv):
    code = line_cli.main(list(argv))
    out = capsys.readouterr().out.strip().splitlines()
    return code, json.loads(out[-1])


def untouched(f):
    return f.keys == [] and f.clicks == [] and f.typed == [] and f.sent == [] and f.events == [] and f.clip == []


def log_lines(tmp_path):
    p = tmp_path / "local" / "pc-hands" / "line-cli.log"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def stamp(img, verdict="PASS", sha=None, bom=False):
    sha = sha or hashlib.sha256(Path(img).read_bytes()).hexdigest()
    Path(str(img) + ".review.json").write_bytes((b"\xef\xbb\xbf" if bom else b"") +
                                                json.dumps({"verdict": verdict, "sha256": sha}).encode())


def make_png(tmp_path, name="a.png", color="red"):
    p = tmp_path / name
    Image.new("RGB", (400, 200), COLORS[color]).save(p)
    return p


# =================================== bot-mode switch ===================================

def test_send_refused_when_bot_mode_off_and_nothing_touched(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    t = wfile(tmp_path, "t.txt", "hello")
    monkeypatch.setenv("HANDS_BOT_MODE", "1")             # an environment variable can never switch it on
    monkeypatch.setenv("HANDS_BOT_ENABLED", "true")
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", t)
    assert code == 2 and res["sent"] is False and res["ok"] is False and "not enabled" in res["error"]
    assert untouched(f)
    assert [r["result"] for r in log_lines(tmp_path)] == ["refused-botmode"]


@pytest.mark.parametrize("cfg", [
    {"enabled": False, "allowed_chats": "*"},
    {"enabled": "true", "allowed_chats": "*"},
    {"enabled": 1, "allowed_chats": "*"},
    {"allowed_chats": "*"},
    {"enabled": True},
    {"enabled": True, "allowed_chats": []},
    {"enabled": True, "allowed_chats": "Group A"},
    {"enabled": True, "allowed_chats": "*", "max_sends_per_minute": 0},
    {"enabled": True, "allowed_chats": "*", "max_sends_per_minute": True},
])
def test_send_refused_for_every_bad_config(tmp_path, monkeypatch, capsys, cfg):
    f = install(monkeypatch, FakeLine(history()))
    p = tmp_path / "cfg" / BOT_FILE
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(cfg), encoding="utf-8")
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"))
    assert code == 2 and res["sent"] is False and untouched(f)


def test_send_refused_for_unreadable_config(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    p = tmp_path / "cfg" / BOT_FILE
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"))
    assert code == 2 and "unreadable" in res["error"] and untouched(f)


def test_config_file_inside_the_repo_is_refused(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    monkeypatch.setenv("HANDS_BOT_MODE_FILE", str(line_cli.ROOT / "bot-mode.json"))
    cfg, why = line_cli.load_bot_mode()
    assert cfg is None and "inside the repo" in why
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"))
    assert code == 2 and untouched(f)


def test_default_config_path_is_localappdata(tmp_path, monkeypatch):
    monkeypatch.delenv("HANDS_BOT_MODE_FILE")
    assert line_cli.bot_mode_path() == tmp_path / "local" / "pc-hands" / "bot-mode.json"


def test_chat_not_in_allowed_chats_refused(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    bot_on(tmp_path, allowed_chats=["Teem"])
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"))
    assert code == 2 and "not in allowed_chats" in res["error"] and untouched(f)
    assert [r["result"] for r in log_lines(tmp_path)] == ["refused-chat"]


def test_wildcard_allows_any_chat(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    bot_on(tmp_path, allowed_chats="*")
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"))
    assert code == 0 and len(f.sent) == 1


# =================================== rate limit ===================================

def test_rate_limit_blocks_the_third_send_in_a_minute(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    bot_on(tmp_path, max_sends_per_minute=2)
    t = wfile(tmp_path, "t.txt", "hello")
    assert run(capsys, "send", "--chat", "Group A", "--text-file", t)[0] == 0
    assert run(capsys, "send", "--chat", "Group A", "--text-file", t)[0] == 0
    n_keys = len(f.keys)
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", t)
    assert code == 2 and res.get("rate_limited") and "rate limit" in res["error"]
    assert len(f.sent) == 2 and len(f.keys) == n_keys                 # the third attempt touched nothing
    assert (tmp_path / "cfg" / "bot-mode.state.json").exists()        # persisted next to the config
    assert [r["result"] for r in log_lines(tmp_path)][-1] == "refused-rate"


def test_rate_limit_window_slides_and_persists(tmp_path):
    sp = tmp_path / "s.json"
    assert line_cli.rate_take(sp, 2, now=1000.0)[0] and line_cli.rate_take(sp, 2, now=1010.0)[0]
    ok, why = line_cli.rate_take(sp, 2, now=1020.0)
    assert not ok and "retry in" in why
    assert line_cli.rate_take(sp, 2, now=1061.0)[0]                    # the first slot is older than 60 s
    assert not line_cli.rate_take(sp, 2, now=1062.0)[0]


def test_rate_limit_fails_closed_on_corrupt_state(tmp_path):
    sp = tmp_path / "s.json"
    sp.write_text("garbage", encoding="utf-8")
    ok, why = line_cli.rate_take(sp, 5, now=1.0)
    assert not ok and "unusable" in why


def test_refusals_before_the_gate_do_not_use_a_slot(tmp_path, monkeypatch, capsys):
    install(monkeypatch, FakeLine(history()))
    bot_on(tmp_path, max_sends_per_minute=1, allowed_chats=["Teem"])
    run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"))
    assert not (tmp_path / "cfg" / "bot-mode.state.json").exists()


# =================================== send flow ===================================

def _trace(monkeypatch, f):
    calls = []
    for name in ("_prepare", "observe", "send_core", "_post_snapshot"):
        real = getattr(line.LineUI, name)

        def wrapped(self, *a, _real=real, _name=name, **k):
            calls.append(_name)
            return _real(self, *a, **k)
        monkeypatch.setattr(line.LineUI, name, wrapped)
    real_press = f.press

    def press(vks, task):
        if tuple(vks) == (0x0D,):
            calls.append("ENTER")
        return real_press(vks, task)
    f.press = press
    return calls


def test_enabled_send_runs_prepare_check_single_enter_postcheck(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    bot_on(tmp_path)
    calls = _trace(monkeypatch, f)
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "สวัสดี\nสอง"))
    assert code == 0 and res["ok"] and res["sent"] and res["confirmed"], res
    assert f.sent == [("Group A", "สวัสดี\nสอง", False, None)]
    assert f.keys.count((0x0D,)) == 1 and (0x10, 0x0D) in f.keys               # one Enter; the newline was Shift+Enter
    assert calls.count("ENTER") == 1 and calls.count("send_core") == 1 and calls.count("_prepare") == 1
    e = calls.index("ENTER")
    assert calls.index("_prepare") < calls.index("observe") < calls.index("send_core") < e
    assert calls[:e].count("observe") >= 2                                     # check in prepare + check in send_core
    assert "_post_snapshot" in calls[e:] and "_post_snapshot" not in calls[:e]  # the post-check comes after the Enter
    assert res["post_check"] == {"box_empty": True, "row_match": True}
    assert res["db_audit"]["state"] == "disabled"


def test_send_db_audit_confirmed_and_notfound(tmp_path, monkeypatch, capsys):
    bot_on(tmp_path)
    t = wfile(tmp_path, "t.txt", "hello")
    f = install(monkeypatch, FakeLine(history()))
    monkeypatch.setattr(line, "db_audit_segment", lambda e, ms, t_send=None: "DB: ยืนยัน (10:30:00 เวลาไทย)")
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", t)
    assert code == 0 and res["db_audit"]["state"] == "confirmed"
    f2 = install(monkeypatch, FakeLine(history()))
    monkeypatch.setattr(line, "db_audit_segment", lambda e, ms, t_send=None: "DB: ไม่พบข้อความ")
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", t)
    assert code == 4 and res["sent"] is True and res["confirmed"] is False and len(f2.sent) == 1
    monkeypatch.setattr(line, "db_audit_segment", lambda e, ms, t_send=None: "DB: ตรวจไม่ได้ (READER_MISSING)")
    install(monkeypatch, FakeLine(history()))
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", t)
    assert code == 0 and res["db_audit"]["state"] == "unavailable"             # an absent reader never fails a good send


def test_send_unconfirmed_when_box_not_cleared_is_exit_4(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    bot_on(tmp_path)
    real = f.press

    def stuck(vks, task):                       # the Enter reaches LINE but the box keeps its text
        if tuple(vks) == (0x0D,):
            f.events.append(("key", (0x0D,)))
            f.keys.append((0x0D,))
            f.sent.append((f.chat, f.box, f.armed, None))
            return
        return real(vks, task)
    f.press = stuck
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"))
    assert code == 4 and res["sent"] is True and res["confirmed"] is False and res["post_check"]["box_empty"] is False
    assert log_lines(tmp_path)[-1]["result"] == "sent-unconfirmed"


def _force_post(monkeypatch, box_empty, row_match):
    real = line.LineUI.send_core

    def wrapped(self, exp, extra):
        report, job = real(self, exp, extra)
        job.post_check = {"box_empty": box_empty, "row_match": row_match}
        return report, job
    monkeypatch.setattr(line.LineUI, "send_core", wrapped)


def _db(monkeypatch, state_text):
    monkeypatch.setattr(line, "db_audit_segment", lambda e, ms, t_send=None: state_text)


def test_confirmed_by_db_when_gui_row_check_fails(tmp_path, monkeypatch, capsys):
    bot_on(tmp_path)
    _db(monkeypatch, "DB: ยืนยัน (10:30:00 เวลาไทย)")
    install(monkeypatch, FakeLine(history()))
    _force_post(monkeypatch, box_empty=True, row_match=False)
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"))
    assert code == 0 and res["confirmed"] is True and res["confirmed_by"] == "db"
    assert log_lines(tmp_path)[-1]["result"] == "sent-confirmed"


def test_confirmed_by_gui_and_db_and_gui_only(tmp_path, monkeypatch, capsys):
    bot_on(tmp_path)
    _db(monkeypatch, "DB: ยืนยัน (10:30:00 เวลาไทย)")
    install(monkeypatch, FakeLine(history()))
    _force_post(monkeypatch, box_empty=True, row_match=True)
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"))
    assert code == 0 and res["confirmed_by"] == "gui+db"
    _db(monkeypatch, "DB: ตรวจไม่ได้ (READER_MISSING)")
    install(monkeypatch, FakeLine(history()))
    _force_post(monkeypatch, box_empty=True, row_match=True)
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"))
    assert code == 0 and res["confirmed_by"] == "gui" and res["db_audit"]["state"] == "unavailable"


def test_image_only_send_confirmed_by_db_image_audit(tmp_path, monkeypatch, capsys):
    bot_on(tmp_path)
    img = make_png(tmp_path)
    stamp(img)
    _db(monkeypatch, "DB: ยืนยัน (10:30:00 เวลาไทย, รูป)")
    install(monkeypatch, FakeLine(history()))
    _force_post(monkeypatch, box_empty=True, row_match=False)
    code, res = run(capsys, "send", "--chat", "Group A", "--image", str(img))
    assert code == 0 and res["confirmed"] is True and res["confirmed_by"] == "db"
    assert res["db_audit"]["state"] == "confirmed" and log_lines(tmp_path)[-1]["result"] == "sent-confirmed"


def test_image_only_send_db_notfound_is_exit_4(tmp_path, monkeypatch, capsys):
    bot_on(tmp_path)
    img = make_png(tmp_path)
    stamp(img)
    _db(monkeypatch, "DB: ไม่พบรูป")
    install(monkeypatch, FakeLine(history()))
    _force_post(monkeypatch, box_empty=True, row_match=True)
    code, res = run(capsys, "send", "--chat", "Group A", "--image", str(img))
    assert code == 4 and res["confirmed"] is False and res["db_audit"]["state"] == "notfound"


def test_db_notfound_is_exit_4_even_when_gui_row_matches(tmp_path, monkeypatch, capsys):
    bot_on(tmp_path)
    _db(monkeypatch, "DB: ไม่พบข้อความ")
    install(monkeypatch, FakeLine(history()))
    _force_post(monkeypatch, box_empty=True, row_match=True)
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"))
    assert code == 4 and res["confirmed"] is False and res["confirmed_by"] is None


def test_box_not_empty_is_exit_4_even_when_db_confirms(tmp_path, monkeypatch, capsys):
    bot_on(tmp_path)
    _db(monkeypatch, "DB: ยืนยัน (10:30:00 เวลาไทย)")
    install(monkeypatch, FakeLine(history()))
    _force_post(monkeypatch, box_empty=False, row_match=True)
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"))
    assert code == 4 and res["confirmed"] is False and res["confirmed_by"] is None


def test_error_after_the_enter_is_exit_4_not_nothing_sent(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    bot_on(tmp_path)
    real = line.LineUI.send_core

    def boom(self, exp, extra):
        real(self, exp, extra)
        raise RuntimeError("late")
    monkeypatch.setattr(line.LineUI, "send_core", boom)
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"))
    assert code == 4 and res["sent"] is True and len(f.sent) == 1


def test_refused_check_before_the_enter_is_exit_2_and_cleans_up(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    bot_on(tmp_path)

    def refuse(self, exp, extra):
        f.box += "X"                            # something dirty is in LINE when the check refuses
        raise ToolError("ไม่ส่ง ตรวจก่อนส่งไม่ผ่าน: test")
    monkeypatch.setattr(line.LineUI, "send_core", refuse)
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"))
    assert code == 2 and res["sent"] is False and f.sent == [] and (0x0D,) not in f.keys
    assert f.box == "" and f.fg == TERM         # the box was emptied, the focus handed back
    assert log_lines(tmp_path)[-1]["result"] == "refused-check"


def test_unknown_chat_is_exit_2(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    bot_on(tmp_path, allowed_chats="*")
    code, res = run(capsys, "send", "--chat", "No Such Chat", "--text-file", wfile(tmp_path, "t.txt", "hello"))
    assert code == 2 and f.sent == [] and (0x0D,) not in f.keys


def test_unexpected_exception_before_the_enter_is_exit_1(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    bot_on(tmp_path)
    monkeypatch.setattr(line.LineUI, "_prepare", lambda *a, **k: (_ for _ in ()).throw(KeyError("x")))
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"))
    assert code == 1 and res["sent"] is False and f.sent == []


def test_usage_and_input_errors_are_exit_1(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    bot_on(tmp_path)
    assert run(capsys, "send")[0] == 1                                          # argparse error: never 2
    assert run(capsys, "frobnicate")[0] == 1
    assert run(capsys, "send", "--chat", "Group A")[0] == 1                     # no text, no image
    assert run(capsys, "send", "--chat", "Group A", "--text-file", str(tmp_path / "missing.txt"))[0] == 1
    assert run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "e.txt", "\n"))[0] == 1
    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"\xff\xfe\x00bad")
    assert run(capsys, "send", "--chat", "Group A", "--text-file", str(bad))[0] == 1
    assert untouched(f)


def test_text_the_module_forbids_is_refused_untouched(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    bot_on(tmp_path)
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "a\tb"))
    assert code == 2 and untouched(f)


def test_text_file_utf8_bom_crlf_and_trailing_newline(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    bot_on(tmp_path)
    code, res = run(capsys, "send", "--chat", "Group A",
                    "--text-file", wfile(tmp_path, "t.txt", "บรรทัด1\r\nline2\n\nline4\r\n", bom=True))
    assert code == 0
    assert f.sent[0][1] == "บรรทัด1\nline2\n\nline4"                            # newlines kept (Shift+Enter), no trailing one
    assert f.typed == ["บรรทัด1", "line2", "line4"]


# =================================== log ===================================

def test_log_has_hash_chat_result_but_never_the_text(tmp_path, monkeypatch, capsys):
    install(monkeypatch, FakeLine(history()))
    bot_on(tmp_path)
    secret = "รหัสลับ-SECRET-1234 ราคา 99"
    rj = wfile(tmp_path, "r.json", json.dumps({"sender": "Bob", "text": "late, news", "time": "10.10", "date": "วันนี้"}, ensure_ascii=False))
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", secret), "--reply-json", rj)
    assert code == 0
    raw = (tmp_path / "local" / "pc-hands" / "line-cli.log").read_text(encoding="utf-8")
    assert "SECRET" not in raw and "รหัสลับ" not in raw and "late, news" not in raw and "99" not in raw.replace(
        hashlib.sha256(secret.encode()).hexdigest(), "")
    (rec,) = log_lines(tmp_path)
    assert rec["text_sha256"] == hashlib.sha256(secret.encode("utf-8")).hexdigest()
    assert rec["chat"] == "Group A" and rec["result"] == "sent-confirmed" and rec["exit"] == 0 and rec["reply"] is True
    assert "ts" in rec and "text" not in rec


# =================================== focus hand-back ===================================

def test_every_command_hands_the_focus_back(tmp_path, monkeypatch, capsys):
    bot_on(tmp_path)
    t = wfile(tmp_path, "t.txt", "hello")
    for argv in (["open", "--chat", "Teem"], ["prepare", "--chat", "Group A", "--text-file", t],
                 ["send", "--chat", "Group A", "--text-file", t], ["cancel"]):
        f = install(monkeypatch, FakeLine(history()))
        code, res = run(capsys, *argv)
        assert code == 0, (argv, res)
        assert f.fg == TERM and "คืนให้หน้าต่างเดิมแล้ว" in res["focus"], argv
        if argv[0] == "cancel":                 # nothing armed: LINE is never brought forward, so there is nothing to hand back
            continue
        assert f.events[-1] == ("focus", TERM), argv
        assert all(e[0] == "focus" for e in f.events[f.events.index(("focus", TERM)):]), argv   # no key after the hand-back


def test_focus_handed_back_on_refusal_too(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    bot_on(tmp_path, allowed_chats="*")
    code, res = run(capsys, "send", "--chat", "No Such Chat", "--text-file", wfile(tmp_path, "t.txt", "hello"))
    assert code == 2 and f.fg == TERM and f.events[-1] == ("focus", TERM)


# =================================== prepare (dry run) ===================================

def test_prepare_is_a_dry_run_without_bot_mode(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))                    # NO bot-mode file: prepare must not need it
    code, res = run(capsys, "prepare", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"),
                    "--reply-json", wfile(tmp_path, "r.json", json.dumps(
                        {"sender": "Bob", "text": "late, news", "time": "10.10", "date": "วันนี้"}, ensure_ascii=False)))
    assert code == 0 and res["check_ok"] and res["sent"] is False and res["kept"] is False
    assert f.typed == ["hello"] and f.clicks                          # it really armed everything ...
    assert (0x0D,) not in f.keys and f.sent == []                     # ... never pressed Enter ...
    assert f.box == "" and f.armed is False                           # ... and cancelled (box + quote cleared)
    assert res["cleanup"]["clean"] is True and f.fg == TERM
    assert log_lines(tmp_path) == []                                  # only send attempts are logged


def test_prepare_keep_leaves_the_message_armed(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    rj = wfile(tmp_path, "r.json", json.dumps({"sender": "Bob", "text": "late, news", "time": "10.10", "date": "วันนี้"}, ensure_ascii=False))
    code, res = run(capsys, "prepare", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"),
                    "--reply-json", rj, "--keep")
    assert code == 0 and res["kept"] is True and "cleanup" not in res
    assert f.box == "hello" and f.armed and (0x0D,) not in f.keys and f.sent == []
    code, res = run(capsys, "cancel")                                 # the separate cancel command clears it
    assert code == 0 and res["ok"] and res["quote_closed"] and res["box_cleared"]
    assert f.box == "" and not f.armed and f.sent == []


def test_prepare_check_failure_is_exit_2_and_cleans(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    real = line.LineUI.observe

    def bad(self, exp, extra):
        snap, obs, problems = real(self, exp, extra)
        return snap, obs, problems + ["forced mismatch"]
    monkeypatch.setattr(line.LineUI, "observe", bad)
    code, res = run(capsys, "prepare", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"))
    assert code == 2 and res["check_ok"] is False and f.box == "" and f.sent == [] and (0x0D,) not in f.keys


def test_prepare_cleanup_failure_is_reported(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    monkeypatch.setattr(line_cli, "cancel_body", lambda ui: (_ for _ in ()).throw(ToolError("cannot clear")))
    code, res = run(capsys, "prepare", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"))
    assert code == 1 and res["check_ok"] is True and res["ok"] is False and "cannot clear" in res["error"]


def test_open_command(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    code, res = run(capsys, "open", "--chat", "Teem")
    assert code == 0 and res["chat"] == "Teem" and f.chat == "Teem" and f.sent == [] and (0x0D,) not in f.keys
    f = install(monkeypatch, FakeLine(history()))
    code, res = run(capsys, "open", "--chat", "Nope")
    assert code == 2 and res["ok"] is False


def test_cancel_when_nothing_is_armed(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    code, res = run(capsys, "cancel")
    assert code == 0 and res["clean"] and not res["quote_closed"] and not res["box_cleared"]


# =================================== reply json ===================================

def test_reply_json_is_passed_to_the_module_unchanged(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    bot_on(tmp_path)
    rt = {"sender": "Bob", "text": "late, news", "time": "10.10", "date": "วันนี้", "index": 1, "expected_count": 1}
    seen = []
    real = line.LineUI.arm_quote
    monkeypatch.setattr(line.LineUI, "arm_quote", lambda self, r: (seen.append(dict(r)), real(self, r))[1])
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "ตอบแล้ว"),
                    "--reply-json", wfile(tmp_path, "r.json", json.dumps(rt, ensure_ascii=False), bom=True))
    assert code == 0 and seen == [rt]
    assert f.sent[0][2] is True and f.sent[0][3].endswith("late, news")        # sent as a quote-reply


@pytest.mark.parametrize("rt", [[1], {"sender": "Bob"}, {"sender": "", "text": "a", "time": "1.0", "date": "วันนี้"},
                                {"sender": "B", "text": "a", "time": "1.0", "date": "วันนี้", "index": 0}])
def test_bad_reply_json_is_exit_1_untouched(tmp_path, monkeypatch, capsys, rt):
    f = install(monkeypatch, FakeLine(history()))
    bot_on(tmp_path)
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "x"),
                    "--reply-json", wfile(tmp_path, "r.json", json.dumps(rt)))
    assert code == 1 and untouched(f)


# =================================== image review gate ===================================

def test_image_without_stamp_is_exit_3_and_touches_nothing(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    bot_on(tmp_path)
    img = make_png(tmp_path)
    for cmd in ("send", "prepare"):
        code, res = run(capsys, cmd, "--chat", "Group A", "--image", str(img))
        assert code == 3 and res["sent"] is False and "no .review.json" in res["error"], cmd
        assert untouched(f)
    assert log_lines(tmp_path)[-1]["result"] == "refused-image"


@pytest.mark.parametrize("case", ["missing_image", "bad_sha", "fail_verdict", "lowercase_verdict", "no_verdict",
                                  "no_sha", "bad_json", "tampered"])
def test_image_gate_refusals(tmp_path, monkeypatch, capsys, case):
    f = install(monkeypatch, FakeLine(history()))
    bot_on(tmp_path)
    img = make_png(tmp_path)
    if case == "missing_image":
        img = tmp_path / "gone.png"
    elif case == "bad_sha":
        stamp(img, sha="0" * 64)
    elif case == "fail_verdict":
        stamp(img, verdict="FAIL")
    elif case == "lowercase_verdict":
        stamp(img, verdict="pass")
    elif case == "no_verdict":
        Path(str(img) + ".review.json").write_text(json.dumps({"sha256": hashlib.sha256(img.read_bytes()).hexdigest()}))
    elif case == "no_sha":
        Path(str(img) + ".review.json").write_text(json.dumps({"verdict": "PASS"}))
    elif case == "bad_json":
        Path(str(img) + ".review.json").write_text("{nope")
    elif case == "tampered":
        stamp(img)
        Image.new("RGB", (400, 200), COLORS["blue"]).save(img)                  # changed AFTER the review
    code, res = run(capsys, "send", "--chat", "Group A", "--image", str(img), "--text-file", wfile(tmp_path, "t.txt", "c"))
    assert code == 3 and res["sent"] is False and untouched(f), (case, res)


def test_image_with_valid_stamp_passes_and_pastes_the_verified_bytes(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    bot_on(tmp_path)
    img = make_png(tmp_path)
    stamp(img, bom=True)                                                       # PowerShell-style BOM + upper-case sha
    sha = hashlib.sha256(img.read_bytes()).hexdigest()
    Path(str(img) + ".review.json").write_text(json.dumps({"verdict": "PASS", "sha256": sha.upper()}), encoding="utf-8")
    staged = {}
    real = line.LineUI.attach_image

    def spy(self, path):
        staged["path"], staged["bytes"] = path, Path(path).read_bytes()
        return real(self, path)
    monkeypatch.setattr(line.LineUI, "attach_image", spy)
    code, res = run(capsys, "prepare", "--chat", "Group A", "--image", str(img))
    assert code == 0 and res["check_ok"], res
    assert staged["bytes"] == img.read_bytes() and Path(staged["path"]) != img     # a private copy of the verified bytes
    assert not Path(staged["path"]).exists()                                       # and it is removed afterwards
    assert f.chooser_clicked and f.box == "" and f.sent == []
    code, res = run(capsys, "send", "--chat", "Group A", "--image", str(img), "--text-file", wfile(tmp_path, "t.txt", "caption"))
    assert f.sent and f.sent[0][1] == OBJ + "caption" and f.keys.count((0x0D,)) == 1
    rec = log_lines(tmp_path)[-1]
    assert rec["image_sha256"] == sha and rec["text_sha256"] == hashlib.sha256(b"caption").hexdigest()


def test_bot_mode_is_checked_before_the_image_gate(tmp_path, monkeypatch, capsys):
    f = install(monkeypatch, FakeLine(history()))
    code, res = run(capsys, "send", "--chat", "Group A", "--image", str(make_png(tmp_path)))
    assert code == 2 and untouched(f)                                          # bot-mode off wins: 2, not 3


# =================================== English LINE UI (nothing locale-specific in the CLI) ===================================

def _en(monkeypatch):
    return install(monkeypatch, FakeLineEN(history_en(), chats=("Group A", "Teem", "Keep Memo")))


def test_en_open_command(tmp_path, monkeypatch, capsys):
    f = _en(monkeypatch)
    code, res = run(capsys, "open", "--chat", "Keep Memo")           # also a left-rail button name
    assert code == 0 and res["chat"] == "Keep Memo" and f.chat == "Keep Memo" and f.sent == [] and (0x0D,) not in f.keys
    f = _en(monkeypatch)
    code, res = run(capsys, "open", "--chat", "Nope")
    assert code == 2 and res["ok"] is False


def test_en_prepare_dry_run_with_iso_date_and_24h_time_reply(tmp_path, monkeypatch, capsys):
    f = _en(monkeypatch)                                              # no bot-mode file: prepare is a dry run
    rj = wfile(tmp_path, "r.json", json.dumps({"sender": "Bob", "text": "late, news", "time": "10:10", "date": "2026-11-18"}))
    code, res = run(capsys, "prepare", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"), "--reply-json", rj)
    assert code == 0 and res["check_ok"] and res["sent"] is False and res["obs"]["locale"] == "en", res
    assert f.typed == ["hello"] and f.clicks and (0x0D,) not in f.keys and f.sent == []
    assert f.box == "" and f.armed is False and res["cleanup"]["clean"] is True and f.fg == TERM


def test_en_prepare_plain_keep_then_cancel(tmp_path, monkeypatch, capsys):
    f = _en(monkeypatch)
    code, res = run(capsys, "prepare", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"), "--keep")
    assert code == 0 and res["kept"] is True and f.box == "hello" and not f.armed and f.sent == []
    code, res = run(capsys, "cancel")
    assert code == 0 and res["ok"] and res["box_cleared"] and f.box == "" and f.sent == []


def test_en_cancel_closes_an_armed_quote(tmp_path, monkeypatch, capsys):
    f = _en(monkeypatch)
    rj = wfile(tmp_path, "r.json", json.dumps({"sender": "Bob", "text": "late, news", "time": "10:10", "date": "2026-11-18"}))
    code, res = run(capsys, "prepare", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"), "--reply-json", rj, "--keep")
    assert code == 0 and f.armed
    code, res = run(capsys, "cancel")
    assert code == 0 and res["quote_closed"] and res["box_cleared"] and not f.armed and f.box == ""


def test_en_send_plain_confirms_own_row_with_read_receipt(tmp_path, monkeypatch, capsys):
    f = _en(monkeypatch)
    bot_on(tmp_path)
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "Thanks, got it"))
    assert code == 0 and res["ok"] and res["sent"] and res["confirmed"], res
    assert f.sent == [("Group A", "Thanks, got it", False, None)] and f.keys.count((0x0D,)) == 1
    assert f.row_name(f.n() - 1) == "Thanks, got it, 10:30 AM, Read"
    assert res["post_check"] == {"box_empty": True, "row_match": True}


def test_en_send_quote_reply_by_iso_date_and_24h_time(tmp_path, monkeypatch, capsys):
    f = _en(monkeypatch)
    bot_on(tmp_path)
    rt = {"sender": "Bob", "text": "hello", "time": "09:00", "date": "2026-11-15"}
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "ตอบแล้ว"),
                    "--reply-json", wfile(tmp_path, "r.json", json.dumps(rt)))
    assert code == 0 and res["confirmed"], res
    assert f.sent[0][2] is True and f.sent[0][3].endswith("original message: hello")


def test_en_send_refused_when_the_quoted_message_is_not_unique(tmp_path, monkeypatch, capsys):
    f = _en(monkeypatch)
    bot_on(tmp_path)
    rt = {"sender": "Carol", "text": "Photos", "time": "13:18", "date": "2026-11-16"}     # three identical photos
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "x"),
                    "--reply-json", wfile(tmp_path, "r.json", json.dumps(rt)))
    assert code == 2 and res["sent"] is False and f.sent == [] and (0x0D,) not in f.keys


def test_en_prepare_quote_of_a_photo(tmp_path, monkeypatch, capsys):
    f = _en(monkeypatch)
    rt = {"sender": "Carol", "text": "Photos", "time": "13:18", "date": "2026-11-16", "index": 2, "expected_count": 3}
    code, res = run(capsys, "prepare", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "x"),
                    "--reply-json", wfile(tmp_path, "r.json", json.dumps(rt)))
    assert code == 0 and res["check_ok"] and f.items[f.menu_row][4] == "green", res


# =================================== hygiene ===================================

def test_no_hardcoded_user_paths():
    for name in ("line_cli.py", "line.py", "winio.py", "tools.py", "uia.py", "logic.py"):
        src = (HERE / name).read_text(encoding="utf-8")
        assert "C:\\Users" not in src and "C:/Users" not in src, name
    cfg = line._audit_cfg()
    home = str(Path.home())
    assert all(v.startswith(home) for v in cfg.values())                        # defaults resolve per user


def test_requirements_list_matches_the_cli_imports():
    local = {p.stem for p in HERE.glob("*.py")}
    roots = set()
    for name in ("line_cli", "line", "tools", "winio", "uia", "logic"):
        for n in ast.walk(ast.parse((HERE / f"{name}.py").read_text(encoding="utf-8"))):
            if isinstance(n, ast.Import):
                roots |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                roots.add(n.module.split(".")[0])
    third = {r for r in roots if r not in sys.stdlib_module_names and r not in local}
    # tools.py imports ocr/numpy lazily inside the OCR tool only; ocr is a local module, numpy is not on the CLI path
    third.discard("numpy")
    dist = {"PIL": "pillow", "pywinauto": "pywinauto", "comtypes": "comtypes"}
    want = {dist.get(r, "pywin32" if r.startswith(("win32", "pythoncom", "pywintypes")) else r) for r in third}
    req = {ln.split("==")[0].strip().lower() for ln in (HERE / "requirements-line.txt").read_text().splitlines()
           if ln.strip() and not ln.startswith("#")}
    assert req == want, (req, want)
    assert all("==" in ln for ln in (HERE / "requirements-line.txt").read_text().splitlines()
               if ln.strip() and not ln.startswith("#"))


# =================================== sender "*" ===================================

def test_wildcard_reply_reports_the_real_sender_in_prepare_and_send(tmp_path, monkeypatch, capsys):
    items = [("date", "วันนี้"), ("msg", "สมชาย", "ขอราคา", "10.10", "red"), ("msg", "Bob", "x", "10.11", "red"),
             ("msg", "Bob", "y", "10.12", "red")]
    f = install(monkeypatch, FakeLine(items))
    bot_on(tmp_path)
    rj = wfile(tmp_path, "r.json", json.dumps({"sender": "*", "text": "ขอราคา", "time": "10.10", "date": "วันนี้"}, ensure_ascii=False))
    code, res = run(capsys, "prepare", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"), "--reply-json", rj)
    assert code == 0 and res["quoted_sender"] == "สมชาย" and "สมชาย" in res["evidence"] and f.sent == []
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"), "--reply-json", rj)
    assert code == 0 and res["confirmed"] and res["quoted_sender"] == "สมชาย" and "สมชาย" in res["evidence"], res
    assert f.sent[0][2] is True and f.sent[0][3].endswith("ขอราคา")


def test_wildcard_reply_refused_on_own_row_sends_nothing(tmp_path, monkeypatch, capsys):
    items = [("date", "วันนี้"), ("msg", None, "my own words", "10.10", "red"), ("msg", "Bob", "x", "10.11", "red"),
             ("msg", "Bob", "y", "10.12", "red")]
    f = install(monkeypatch, FakeLine(items))
    bot_on(tmp_path)
    rj = wfile(tmp_path, "r.json", json.dumps({"sender": "*", "text": "my own words", "time": "10.10", "date": "วันนี้"}))
    code, res = run(capsys, "send", "--chat", "Group A", "--text-file", wfile(tmp_path, "t.txt", "hello"), "--reply-json", rj)
    assert code == 2 and res["sent"] is False and f.sent == [] and (0x0D,) not in f.keys
