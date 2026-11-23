# pc-hands

Windows desktop control for AI agents, over UI Automation, with a careful **LINE Desktop** module that can type,
quote-reply and send messages with guard rails strong enough for an unattended bot.

- An HTTP service (`server.py`, 127.0.0.1:8792) and an MCP adapter (`mcp_server.py`) expose tools such as
  `list_windows`, `find_elements`, `click`, `type_text`, `press_keys`, `scroll`, `read_text`, `screenshot`, `ocr`.
- The LINE tools (`line_open_chat`, `line_prepare`, `line_send`, `line_cancel`) drive the LINE Desktop app by UIA
  only. No screenshots or vision model are in the loop.
- `line_cli.py` is a one-shot CLI over the same LINE module for bots. Each command prints one JSON result and
  returns a meaningful exit code.
- LINE's **Thai and English** UI both work. The language is detected for every snapshot.

Windows 10/11 only. Python 3.12. Tested with LINE Desktop 26.x.

> Driving a messenger sends real messages to real people. Read the [Safety model](#safety-model) section before
> you let anything call `line_send` or `line_cli.py send`.

## Install

    git clone https://github.com/OAKHEN1412/pc-hands
    cd pc-hands
    python -m pip install -r requirements.txt          # core + MCP adapter
    python -m pip install rapidocr onnxruntime          # optional, for the `ocr` tool

## Run the service

    echo some-long-random-secret > .pin                # every request must send it as header x-pin
    python server.py                                   # http://127.0.0.1:8792

    curl -H "x-pin: some-long-random-secret" http://127.0.0.1:8792/health

`POST /tool` with `{"name": ..., "args": {...}}` returns one of:

- `{"ok": true, "result": str}`
- `{"ok": false, "error": str[, "yielded": true]}`
- `{"ok": false, "needs_confirm": str}`

Environment variables: `HANDS_PIN` overrides `.pin`, and `HANDS_PORT` changes the port.
`install-autostart.ps1` starts the service at logon through `HKCU\...\Run`. It does not use a Scheduled Task,
because a task-launched process gets no working `SendInput`.

### MCP (Claude Code and other clients)

`mcp.example.json` shows the registration. `mcp_server.py` forwards to the HTTP service, so `server.py` must be
running.

    claude mcp add pc-hands -- python -X utf8 C:/path/to/pc-hands/mcp_server.py

`HANDS_FAST=1` turns off the red highlight and batches typing, which suits an agent session.

Set `HANDS_UNATTENDED=1` on any MCP registration that an unattended job uses. With it set, the `confirmed` flag is
stripped from every call, so such a job can prepare a LINE message but never send it.

## Desktop tools

| Tool | What it does |
|---|---|
| `screen_info` | Monitors and the mouse position |
| `list_windows` | Lists windows |
| `focus_window` | Brings a window to the front |
| `find_elements {window, query?}` | Returns UIA elements with ids (`#12`) |
| `click` | Clicks an element id or a point |
| `type_text` | Types Unicode text through SendInput (Thai works); text over 300 characters is pasted |
| `press_keys` | Presses keys |
| `scroll` | Scrolls |
| `read_text` | Reads text |
| `open_app` | Opens an app |
| `screenshot` | Takes a screenshot |
| `ocr {query?, window?, x,y,w,h?}` | Reads the screen with RapidOCR (PP-OCRv5, Thai and English, CPU). Returns `#oN` ids that `click` accepts |

Every action has these safeguards:

- A red frame highlights the target before a click or typing (the MCP "fast" mode skips it).
- The tool stops with `yielded` as soon as the real user moves the mouse or presses a key.
- Anything that looks like sending or posting needs `confirmed: true`.
- Some windows can never be controlled: regedit, Windows Security, UAC and credential prompts, elevated windows,
  and terminals running an agent.

## LINE module (`line.py`)

The module reads everything from the live window on every call: window, input box, header, message list and wheel
scale. Nothing is tied to a screen layout.

### `line_open_chat {chat}`

Searches for the chat and opens the chat-list row whose name equals `chat` exactly. It refuses when 0 or 2+ rows
match, and it checks that the header then equals `chat`.

### `line_prepare {chat, text?, image_path?, reply_to?}`

This is a dry run: it sets everything up but never sends. In order, it:

1. Opens or verifies the chat.
2. Closes any armed quote and clears the box.
3. Arms the quote-reply, if `reply_to` is given.
4. Pastes the image, if `image_path` is given.
5. Types the text. A newline becomes Shift+Enter, never Enter.
6. Runs a **one-pass check** of the header, the exact box text, the quote state (including "not armed" for a plain
   send) and the image thumbnail against the file.

It returns a `token`, which lives 120 s; only one send can be pending at a time.

`reply_to = {sender, text, time, date, index?, expected_count?}` finds the target by sender, **exact** text, time
and date:

- `sender: "*"` means any sender except your own account.
- `date` is ISO (`2026-11-19`) or LINE's separator text (`19 พฤศจิกายน 2026`, `November 19, 2026`, `วันนี้`,
  `Today`, …).
- `time` is `13:18`, `13.18` or `1:18 PM`.
- It refuses unless exactly `expected_count` rows match (default 1). `index` (1 = topmost) picks one of several
  identical rows, and image quotes are verified by pixels.

### `line_send {token, confirmed}`

Without `confirmed: true`, it only answers `needs_confirm` and nothing happens. With it, the tool:

1. Re-checks foreground, focus, and everything `line_prepare` checked.
2. Presses Enter **once**.
3. Checks the result: the box emptied, and the newest row shows the message.
4. Optionally audits LINE's local database (see below).

It never retries. The token is spent either way, and keyboard focus goes back to the window that had it before.

### `line_cancel {}`

Closes an armed quote, clears the box, forgets the token and reports the final state.

### What the code refuses to do

- Typing, Ctrl+A, Delete and Ctrl+V happen only while LINE is in front, enabled, and its input box has focus.
- A disabled main window (a leftover dialog) is reported, not clicked through.
- The "Delete" and "Unsend" menu items are never clicked.
- A right-click is hit-tested against the live row first.
- The quote bar is read from live geometry, because its UIA name goes stale.

### Database audit (optional)

After a send, `line.py` can confirm the message in LINE Desktop's local database with a read-only reader from
[line-desktop-mcp](https://github.com/bensonmaxai/line-desktop-mcp). It is off unless the reader is installed:

- The default location is `~/tools/line-desktop-mcp-v3.3.9`.
- Override the paths with `HANDS_LINE_READER_PY`, `HANDS_LINE_READER_SCRIPT` and `HANDS_LINE_SQLITE_DLL`, or turn
  the audit off with `HANDS_LINE_DB_AUDIT=0`.

The audit never takes part in the send itself. When it cannot run, results say `DB: ตรวจไม่ได้ (...)` ("could not
check").

## `line_cli.py` for bots

    python line_cli.py open    --chat NAME
    python line_cli.py prepare --chat NAME [--text-file F] [--image F] [--reply-json F] [--keep]   # dry run
    python line_cli.py send    --chat NAME [--text-file F] [--image F] [--reply-json F]            # needs bot-mode
    python line_cli.py cancel

The CLI runs the LINE module in-process; it needs no HTTP service and no MCP. It prints one JSON object (UTF-8).

| Exit | Meaning |
|---|---|
| 0 | Sent and confirmed: the box is empty, and the newest row or the DB shows the message |
| 1 | Error before anything was sent: bad arguments, an unreadable file, … |
| 2 | Refused or a check failed. **Nothing was sent.** Also returned when bot-mode is off, the chat is not allowed, or the rate limit is hit |
| 3 | The image has no valid review stamp. LINE was not touched |
| 4 | Enter **was** pressed but the result was not confirmed. Check by hand; never resend blindly |

### Bot-mode

**Bot-mode** is the only thing that lets `send` skip the human `confirmed` flag. It is a file on the machine,
never an environment variable, and a file inside the repo is refused:

    %LOCALAPPDATA%\pc-hands\bot-mode.json
    {"enabled": true, "allowed_chats": ["Chat A", "Chat B"], "max_sends_per_minute": 6}

- `"allowed_chats": "*"` allows every chat.
- `HANDS_BOT_MODE_FILE` moves the file.
- `enabled` must be literally `true`.

Every attempt is appended to `%LOCALAPPDATA%\pc-hands\line-cli.log`. Each entry holds the chat, a sha256 of the
text, the result and the time, but never the text itself.

### Image stamp

An image sent by the CLI needs `<image>.review.json` next to it containing `{"verdict": "PASS", "sha256": "<sha256
of the image bytes>"}`. The stamp lets a separate reviewer step approve exactly those bytes.

## Safety model

1. A human approves interactive sends: `line_send` needs `confirmed: true`, and MCP clients registered with
   `HANDS_UNATTENDED=1` can never pass it.
2. A bot sends only through `line_cli.py send`, gated by the bot-mode file (chat allowlist, rate limit) and, for
   images, the review stamp.
3. Every send presses Enter exactly once, after a one-pass check of the exact chat, text, quote and image. Exit 4
   tells the caller to look before doing anything else.
4. The PIN protects the HTTP service on localhost. Keep `.pin` private; it is git-ignored.

## Tests

    python -m pytest -q . --deselect test_hands.py::test_live_notepad

Run this way, the suite uses no GUI, and the LINE tests run against a fake LINE UIA tree. CI runs the same command
on `windows-latest`.

`test_hands.py::test_live_notepad` is a live test: it opens Notepad and drives the **real** mouse and keyboard, so
keep your hands off while it runs.

## Files

| File | Contents |
|---|---|
| `server.py` | HTTP service and its single UI worker thread |
| `mcp_server.py` | MCP adapter |
| `tools.py` | Tool implementations |
| `uia.py` | UIA tree walking |
| `winio.py` | Win32: DPI, windows, SendInput, input hooks, highlight, focus hand-back |
| `logic.py` | Pure logic: gates, forbidden windows, ids, key parsing, OCR matching, LINE row, quote and date parsing for both locales |
| `line.py` | LINE tools and the DB audit |
| `line_cli.py` | Bot CLI |
| `ocr.py` | RapidOCR engine |

## Measured results

These numbers come from one production deployment: an unattended LINE bot that answers product-spec questions in
customer chats. They were measured in November 2026 on LINE Desktop 26.6 (English UI) under Windows 11. Chats,
people and message contents are left out on purpose.

### Single LINE actions (`line_cli.py send`, real sends)

| Action | Time |
|---|---|
| Plain text | 2.5 s |
| Quote-reply text | 4.9 s |
| Image (stamped) | 3.0 s |
| Interactive path: DB read + `line_prepare` + `line_send` | 2.7 s |

The previous send path drove LINE through PowerShell scripts and screenshots. Each call spent about 0.8 s on
focus plus fixed sleeps, and a model read a screenshot to find the chat row. A send cost about 3.6 s, and opening
a chat about 3.3 s.

### One whole bot answer (question received → reply sent)

| | Before (screenshot path) | After (pc-hands), job A | After (pc-hands), job B |
|---|---|---|---|
| Question → reply on screen | 22.5 min | 12.1 min | 11.2 min |
| Time spent driving LINE | ~45 s | ~10 s | ~9 s |
| Model cost per job | $7.45 | $4.11 | $4.24 |
| Reviewer rejections before PASS | 2 | 1 | 1 |

Read this table with care:

- pc-hands only cuts the LINE part, from ~45 s to ~10 s per answer.
- Most of the end-to-end gain came from fewer reviewer rounds and a leaner agent pipeline, not from faster clicking.
- The sample is small: one job before and two after.

In the first four days live, the bot made 61 send attempts:

- 55 were sent and confirmed (exit 0). None ended unconfirmed (exit 4).
- 5 were refused by the pre-send check (exit 2), so nothing was sent. Each went out on a retry within about 30 s.
- 1 was refused by the rate limit (exit 2). It went out about 2.5 min later.

## Limits

- UIA is empty or partial in canvas, Qt and Electron apps without accessibility. Use `ocr` there, or `screenshot`
  plus a vision model for icons. LINE itself exposes enough through UIA on 26.x.
- Elevated windows cannot be driven (UIPI).
- LINE changes its UI between versions. The parsers accept the formats seen on 26.x in Thai and English; a new
  format makes a check fail and **refuse**, never guess.

## License

MIT, see [LICENSE](LICENSE).
