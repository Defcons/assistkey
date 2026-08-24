# ResearchJournal — AssistKey

_Append-only chronological ledger: what changed, what was tested, what was learned. Old entries are never rewritten; durable confirmed facts get promoted to KnowledgeBase._

---

## 2026-08-23 — Popup smoothness: kill the laggy slide

**Symptom (user):** the toast popup + text feel static and laggy; the slide in/out animation stutters.

**Diagnosis (measured, not guessed):** `probe_after.py` timed `root.after(16)` under Tk 8.6 / Python 3.14 on Windows 11:
- default timer resolution → **23 ms mean, 31 ms max** between frames (≈43 fps, jittery);
- after `winmm.timeBeginPeriod(1)` → **16.1 ms, rock steady** (true 60 fps).
Root cause: Windows' ~15.6 ms default timer granularity rounds `after(16)` up, and the old `_tween` counted fixed *steps* (`dur_ms/16`) rather than wall-clock time, so every dropped/late frame stretched the whole animation instead of skipping ahead → visible drag.

**Change (`overlay.py`, ~67/-18):**
1. `_hold_timer_res(ms)` / `_drop_timer_res` — raise the 1 ms timer resolution only while animating, self-releasing via a single tracked `after` job; balanced by a `_timer_raised` bool so the begin/end pair is never unbalanced and the idle tray app doesn't pin the timer.
2. `_tween` rewritten **time-based** — progress read from `time.monotonic()` each frame; a late frame skips ahead; duration self-corrects.
3. `_start_reveal` (reply fade) made time-based **and** lightweight — recolours one cached canvas item (`_body_item`) via `itemconfigure` instead of `c.delete("all")` + full rebuild + a pointless per-frame `geometry`/`-alpha`.

**Verified (machine):**
- `probe_overlay.py`: slide-in frame interval median **17 ms** (was ~23 ms); tween is time-based (span tracks target).
- `probe_reveal.py`: timer raised *during* animation and drops to False when idle; reveal completes to 1.0; **18 `itemconfigure` recolours + 2 full `_draw`s** across a whole reply (was ~18 full rebuilds).
- `pytest tests/` → 15 passed (stuck-recovery, dismissal, frame-exception-doesn't-wedge invariants intact).

**Not verified:** on-screen visual smoothness by a human — didn't launch the full app because `kill_previous_instances()` would kill the user's running copy and grab the mic/hotkey. Logged in `Testing.md`.

**Also this session:** seeded the project documentation set — small-repo layout: `OrientationMap.md` at root, this + KnowledgeBase/ToDo/Testing in `docs/`.

---

## 2026-08-23 — Repo hardening pass (correctness + robustness)

Full read of all six modules for bugs/leaks/races. Fixed the high-confidence, low-risk items; deferred the UX judgement calls to ToDo.

**Fixed:**
1. **Wake-word silently dies (real bug).** `AssistClient.start_utterance` early-returned when `self.ws is None` (not yet connected). But callers pause wake *before* calling it (`app._hotkey_down`/`_on_wake`), and wake only resumes on a terminal `("done",)`/`("error",)`. So a wake trigger (or key-press) during startup left wake-word listening paused **until app restart**. Fix: split the guard — an already-active call stays silent (the running one owns the terminal emit); the ws-None decline now emits `("done",)` so the app resyncs (idle icon, `mark_idle`, `wake.resume`). Regression test added (`tests/test_assist_client.py`).
2. **Non-atomic config save.** `config.save()` did a truncating `write_text`; since the app force-kills older instances at startup, a mid-write kill could corrupt config.json and lose the token. Now writes a temp file + `os.replace` (atomic on same volume). Verified: round-trips, no `.tmp` left behind.
3. **`pump()` fragility.** Only `ConnectionClosed`/`OSError` were caught; any other exception (e.g. a malformed frame through `json.loads`) would escape, end `run_until_complete`, and zombie the asyncio thread (tray alive, hotkey dead). Now guards `json.loads` and skips a bad frame.
4. **Stale docstring** in `config.py` ("No secrets live here — credentials come from env") — the token IS persisted to config.json; corrected to match reality.
5. **`get_event_loop()` → `get_running_loop()`** in `forward_audio` and `_play` (both inside running coroutines) — future-proof, avoids the 3.14 deprecation path.

**Verified:** `pytest tests/` 17 passed (+2 new); all modules import + `py_compile` clean; atomic-save probe.

**Deferred (see ToDo):** hotkey-capture interference (main listener stays live during "Change…" capture), no cancel/modifier-only in capture, possible error-popup if a key is pressed mid-reconnect, no guard against opening two Settings dialogs. All behavioural/UX changes with regression risk — not folded into a correctness pass.

---

## 2026-08-23 — Settings/hotkey UX pass (the deferred ToDo items)

Cleared the four deferred UX items from the 2026-08-23 hardening pass.

1. **Hotkey-capture interference.** The Settings "Change…" capture ran a second `pynput` listener while the global `HotkeyListener` stayed live, so pressing the *current* hotkey during capture could start an utterance. Added `HotkeyListener.suspend()/resume()` (a `_suspended` flag short-circuits `_press`/`_release` + `reset()`), threaded `suspend_hotkey`/`resume_hotkey` from `app._handle` → `Overlay.open_settings` → `SettingsDialog`, and bracketed capture with them (`_end_capture` guarantees resume on finish/cancel/close). Test: `test_suspend_ignores_keys_then_resume_restores`.
2. **Capture cancel + modifier-only.** Esc now cancels (restores the prior combo); a non-modifier commits immediately; a modifier-only chord (e.g. Ctrl+Shift) commits on full release. `seen` accumulates the chord across partial releases; `_capturing` dedupes overlapping press+release commits.
3. **Mid-reconnect decline.** `start_utterance` now gates on `_ws_is_open` (state == OPEN), not `ws is None` — during reconnect `self.ws` is the old *closed* socket, so the None-check let a doomed send raise a raw-exception error popup. Deliberate key-press → gentle `("error", "Reconnecting…")` (`notify_unavailable=True` from `_hotkey_down`); wake false-positive stays silent. Tests added.
4. **Settings single-instance.** `Overlay.open_settings` re-focuses a live dialog instead of stacking a new one. Smoke-tested (`probe_settings_guard.py`).

**Verified:** `pytest tests/` 21 passed (+4); all modules `py_compile` clean; settings-guard probe. **Deferred still:** only the optional popup-motion polish, gated on the human smoothness check (Testing.md). **Manual-test needed:** the capture UX (Esc / modifier-only / no-interference) — can't be unit-tested through a real pynput+Tk dialog; logged in Testing.md.

---

## 2026-08-23 — Feature pass (nine additions across reliability, UX, security, distribution)

All nine recommended items, implemented + tested + documented in one pass. 29 tests pass (+8).

**Reliability**
1. **CI** — `.github/workflows/ci.yml` runs pytest on windows-latest.
2. **Rotating log** — `app._rotate_logs` keeps `assistkey.log.1..3` of previous non-empty runs instead of truncating on every launch (empty clean runs aren't rolled). `.gitignore` → `assistkey.log*`.
3. **Tray connection colour** — client emits `("connected",)`/`("disconnected",)`; `App._set_idle_icon` shows red `DISCONNECTED_COL` vs grey `IDLE_COL` (green while active). Tray gains a "Stop" item.

**Features**
4. **Barge-in / cancel** — `AssistClient.request_cancel()` (`sd.stop()` + `_cancel` event → `__cancel__` sentinel; `finally` still emits done). Triggers: hotkey press while `is_active()`, tray Stop, click the popup (`Overlay._on_click`/`on_cancel`).
5. **Follow-up + conversation continuity** — captures `conversation_id`, replays within `CONVERSATION_TTL`; `follow_up_enabled` (default off) auto-listens when HA sends `continue_conversation`. App keeps wake paused across the follow-up chain to avoid mic contention. Best-effort re: HA field path (no-op if absent).
6. **Mic level meter** — RawInputStream callback emits `("level", peak)`; `Overlay.set_level` draws a bar in the Listening popup.

**Security**
7. **DPAPI token at rest** — new `dpapi.py` (`CryptProtectData`, per-user); `config.save`/`load` encrypt/decrypt behind a `dpapi:` marker, plaintext fallback, legacy migration. Plaintext never hits disk (test asserts).

**Distribution**
8. **Autostart** — new `autostart.py` (HKCU Run key, stdlib winreg); Settings "Start at login" checkbox.
9. **PyInstaller exe** — `AssistKey.spec` + `build.bat` → `dist/AssistKey.exe` (~90 MB). Built (exit 0) and launch-tested (no import crash). Fixed `kill_previous_instances` to branch on `sys.frozen` (match `AssistKey.exe` vs venv `python*.exe`) — the venv heuristic would have mis-targeted `python.exe` under a distributed exe's folder.

**Verified:** 29 tests pass; all modules `py_compile`; DPAPI/config/autostart/rotate probes; PyInstaller build + alive check. **Manual-test needed:** barge-in, level meter, follow-up, autostart checkbox, connection colour, exe on a clean machine — logged in Testing.md.

---

## 2026-08-24 — Removed hotkey capture (system lag); added diagnostics logging

**Hotkey capture removed.** The opt-in key-suppression (pynput `win32_event_filter` +
`suppress_event`) lagged the user's *entire computer* — a low-level keyboard hook runs
Python on every keystroke and stalls all input under GIL contention. There is no
lag-free way to suppress a hold-to-talk key in this stack (`RegisterHotKey` gives no
clean hold/release), so per the user's call it was removed entirely: dropped
`config.suppress_hotkey`, `_canon_to_vks`, `_win32_filter`/`_suppress_vk_event`/
`_build_listener`, the Settings "Capture key" toggle, and the suppression tests.
`HotkeyListener` is back to a plain `kb.Listener(on_press, on_release)` (the
suspend/resume for settings-capture stays — that's unrelated). KnowledgeBase carries a
"do not reintroduce" note.

**Diagnostics logging added (`diag.py`).** Goal: field crashes must be diagnosable.
- Rotating `assistkey.log` (+.1/.2/.3, 1 MB, appended across runs) with timestamped,
  levelled, thread-tagged lines (replaces the old truncating stdout redirect +
  launch-time `_rotate_logs`).
- Crash capture in EVERY thread: `sys.excepthook`, `threading.excepthook`,
  asyncio `loop.set_exception_handler`, Tk `report_callback_exception`; stdout/stderr
  redirected into the log (no console under pythonw).
- `redact_config` logs a config snapshot with the token as set/none — token never written.
- Key events now logged (connect/disconnect/reconnect/utterance-fail/mic/playback);
  `wake.py`/`config.py` `print()`s replaced with `logging`.
- Tray gains **Open log**; README has a Troubleshooting section for bug reports.

**Also:** confirmed the earlier Save-lag fix (reconnect only on cred change + try-first).
The app backbone is sound — measured 0.0% idle CPU, 5 MB RSS, single instance.

**Verified:** 35 tests pass (suppression tests removed; +5 `test_diag.py`).

---

## 2026-08-24 — Pre-filled GitHub issue reporting (not auto-upload)

User asked for "auto-send error logs to our public repo." Flagged two real problems
before building the literal version: (1) this app's logs can contain the real HA URL,
device names, and error text — auto-uploading that to a PUBLIC repo with no human in
the loop is a genuine privacy exposure for every future user, not just David; (2)
there's no safe way to make it *fully* automatic from a distributed client — creating
GitHub issues needs an API token, and any token embedded in a public exe is
extractable/abusable. Presented three tiers (prefilled-draft-for-review / automatic-
via-a-hosted-backend / manual-only); user picked **prefilled draft**.

**Implementation (`diag.py` + `app.py`):**
- `find_error_excerpt(paths)` — scans `assistkey.log` then `.log.1` newest-first for
  the most recent ERROR/CRITICAL record (a record's traceback lines carry no
  timestamp, so a block runs until the next timestamped line); falls back to a plain
  tail if nothing rose above WARNING. Capped at 1500 chars with a truncation notice.
- `build_issue_url(config)` — fills a GitHub `issues/new?title=&body=&labels=bug` URL:
  a repro template, a bolded "this is a PUBLIC issue, review before submitting"
  warning ABOVE the excerpt, the excerpt itself, and a system/config summary.
- **`redact_config_public`** (new, stricter than the existing `redact_config` used for
  the local log) masks the HA URL down to just its scheme — the one field fully under
  our control, so there's no reason to expose the hostname by default even though the
  template also tells the user to check. The excerpt itself can't be safely
  auto-redacted (might mangle the trace), hence the visible warning instead.
- Tray **"Report an issue…"** → `webbrowser.open(diag.build_issue_url(self.config))`.
  No GitHub token anywhere in the client — the user's own GitHub login creates the
  issue when they click Submit.

**Verified:** 43 tests pass (+8: excerpt selection prefers the latest error over older
WARNING noise, traceback-block boundaries, previous-log fallback, truncation, no
token/no hostname in the output, real-error content reaches the body). Manually built
a realistic log + real Config and inspected the decoded URL body end-to-end — correct
template, warning, excerpt, and masked config line; URL length 1414 chars (well within
practical limits).

**Not yet checkable:** the target repo doesn't exist publicly yet, so the opened link
currently 404s — logged in Testing.md, this resolves whenever the repo is published
(see the earlier "public repo readiness" work).

---

## 2026-08-24 — "Report an issue…" tightened to fully anonymous

Follow-up to the same-day pre-filled-issue work. User: "just remove auto filling of
all the privacy stuff, keep it anonymous" — a stronger stance than the masked-URL/
excerpt-with-warning version just shipped. Rather than layer on more redaction,
removed the auto-fill entirely: `diag.build_issue_url()` now takes NO arguments and
touches no file — no log excerpt, no config summary, no URL (masked or otherwise).
Deleted `find_error_excerpt`, `_tail`, `_clip`, `redact_config_public`, `_mask_url`,
and their constants/regexes — all now-dead code the earlier version needed and this
one doesn't. The draft body is a static repro template plus generic, non-identifying
software-version facts (Python version, OS build, source-vs-exe) — the same for every
install, nothing that traces back to a person or a network. The user still attaches
`assistkey.log` themselves (tray → Open log) if and when they choose to.

**Verified:** URL length dropped from 1414 to 601 chars. `pytest tests/` 39 pass (11
excerpt/masking tests removed, 4 new ones assert the function takes only `repo_url`,
the body contains no config-shaped keys/URL scheme/log-excerpt marker, and a
token-shaped env var still can't reach the output). Manually built the real URL and
inspected the decoded body — confirmed empty of anything user- or machine-specific.

The local `assistkey.log` itself is UNCHANGED (still records connect/disconnect/
errors/redacted config for your own troubleshooting via Open log) — only the
*public-issue* path lost its auto-fill.

---

## 2026-08-24 — "Report an issue…" settles: auto-pull the error, redact it

Third iteration of the same-day feature. User: "I want 'Auto-pulled most recent
error + traceback' but in that log we should rather exclude privacy data." So: bring
back `find_error_excerpt` (un-deleted from the fully-anonymous version), but pipe its
output through a new `_redact(text, config)` before it reaches the URL, instead of
either (a) relying on a human to catch it, or (b) refusing to include the excerpt at all.

**`_redact` — two passes:**
1. Exact replace of the user's OWN configured HA URL and host (from `config.credentials()`)
   → labelled `[home-assistant-url]` / `[home-assistant-host]`. Most precise pass, since
   we know the exact string.
2. Generic regexes for anything else that might slip in: any other `https?://` URL →
   `[url]`; a JWT-shaped string → `[token]` (defence in depth — nothing currently logs
   the token, but a future log line shouldn't be able to leak one); a `C:\Users\<name>\`
   segment → `C:\Users\[user]\` (keeps the rest of the path — still useful for "which
   file/line" without the identifying username); IPv4 addresses → `[ip]`.

Errs toward over-redacting (losing a little context is fine; leaking an address or path
isn't). The issue template keeps a short "still public, give it a glance" line — the
regex set can't be proven exhaustive against log lines nobody's written yet, so the
human glance stays the backstop, not the primary defence anymore.

**Verified:**
- 49 tests pass (+10 vs the anonymous version: `_redact` per-pattern unit tests,
  `find_error_excerpt` restored, an end-to-end test with a realistic log asserting the
  URL/username/IP are gone while the real error text survives).
- Ran it against this machine's REAL config (actual HA URL, actual token) and a
  realistic crash log: real host absent from the URL, real token absent, "David" absent
  from the body, a sample IP absent — while "connect failed" / exception type / file+line
  all came through intact. Inspected the decoded body directly.

Net effect vs the two earlier iterations today: the draft is now informative (a real
maintainer has something to go on) AND doesn't require the user to manually redact
anything — the previous two versions each gave up one of those.
