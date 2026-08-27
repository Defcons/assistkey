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

---

## 2026-08-24 — Barge-in: Listening must always override a shown response

User: "if we get a response from Jarvis/Assistant and we click the hotkey to talk
again, the new 'Listening' popup should always override the answer from the first
ask." Traced the actual current behaviour rather than assuming it already worked.

**Root cause.** `AssistClient.is_active()` stays true for the WHOLE utterance,
including while the reply is being SPOKEN (TTS `sd.wait()` inside `_play`, awaited
from `_run_utterance`'s main loop) — not just STT/thinking. `app._hotkey_down` special-
cased "already active" to call `request_cancel()` and RETURN, never calling
`start_utterance` again. So: press hotkey while a reply is playing -> it stops the
reply -> and does NOTHING ELSE. In hold-mode this is worse than it sounds: the user
is physically holding the key expecting to talk, but no new utterance ever starts —
they have to release and press again.

**Fix: `AssistClient.restart_utterance()`.** Cancels whatever's active (if anything),
waits for it to actually wind down (`self._idle`, an `asyncio.Event` mirroring
`_active`), then starts fresh. `app._hotkey_down` (and, for consistency, `_on_wake` —
flagged to the user as an extra, same-root-cause fix beyond what was literally asked)
now call this instead of `start_utterance` directly.

**A real race surfaced along the way, not just the headline behaviour.** The
cancelled utterance's own `("done",)` is queued (in `_run_utterance`'s finally)
BEFORE `_active`/`_idle` flip — so by the time `restart_utterance`'s wait unblocks,
that stale `("done",)` is already ahead of the new utterance's `("listening",)` in
`ui_queue`. `_handle`'s "done" branch calls `hotkey.mark_idle()` (`_talking=False`);
if that fires while the user is STILL HOLDING the key down for their new command,
the eventual release's `_talking` check silently fails and `signal_release()` never
reaches the new utterance — it would just keep recording until `MAX_RECORD` (120s).
Fixed at the source: `request_cancel(suppress_done=True)` + a new `_emit_done()`
helper that consumes a one-shot `_suppress_next_done` flag, so the cancelled run's
terminal signal never reaches `_handle` at all when it's about to be immediately
superseded. Plain `request_cancel()` (tray Stop, popup click) is unaffected — those
callers WANT the normal done/dismiss.

**Verified two ways:**
1. Unit tests (`tests/test_assist_client.py`): `_emit_done` suppression is exactly
   one-shot and doesn't affect plain `request_cancel()`; `restart_utterance` branches
   correctly for idle/active/timeout (a `wait_for` mock that properly `.close()`s the
   un-awaited coroutine, to keep the suite warning-free).
2. **A real end-to-end harness** (not committed — a one-off `probe_barge_in.py`,
   pattern matches this session's other verification scripts) that drives the ACTUAL
   `_run_utterance` control flow with only the I/O boundaries faked (audio primitives,
   TTS fetch/decode, the websocket object) — confirmed a genuine `PortAudioError`-free
   run: a real reply streams and starts being "spoken" (mid-playback, `is_active()`
   true), a simulated hotkey press mid-speech triggers `restart_utterance`, and the
   full event sequence came out exactly as intended: `[..., 'response_final',
   'assistant', 'listening', 'done']` — a SECOND `listening` fires (the override),
   TTS is confirmed stopped, and exactly ONE `done` appears total (the new
   utterance's, not the cancelled one's). Two harness bugs surfaced and were fixed
   along the way (a fake `ws` missing `.state.name=="OPEN"`, and forgetting to set
   `client.loop` — both caught by the SAME `_ws_is_open`/`if loop is not None` guards
   this codebase already has, which is itself a small vote of confidence in those
   guards).

**Accepted residual edge case (documented in KnowledgeBase, not fixed):** if the
user's key-*release* physically lands inside the brief cancel-to-restart window
(typically single-digit ms), that release can apply to the OLD, already-superseded
`_release` event and be lost — the new utterance keeps recording until `MAX_RECORD`
or another release. Not closed: a realistic hold-to-talk press-then-release gesture
is never that fast, and existing safety nets bound the consequence.

**Verified:** 54 tests pass (+4). Full regression clean.

---

## 2026-08-24 — "Popup stays a lot longer than 2s after it finished talking"

Confirmed `dismiss_seconds` really is 2.0 in `config.json` (ruled out empirically,
not assumed) and the app was live/connected. Then read the actual dismiss chain
fresh rather than trust memory from earlier in the session.

**Found a real, concrete gap:** `overlay.py` had ZERO logging. There's an existing
22 s watchdog (`_watchdog`, `dismiss_seconds + 20` for a RESPONSE/ERROR popup) whose
entire job is to rescue a popup if the normal dismiss never fires — and it force-hides
completely silently. If THAT'S what's actually dismissing the user's popup instead of
the real 2 s timer, there was previously no way to know from the log.

**Also found, semi-accidentally, a second real and plausible mechanism:** while
building a verification harness, a broken TTS URL revealed that `_play()`'s HTTP
fetch (`urlopen(..., timeout=15)`) has no faster fallback and its own generic
`"playback failed"` warning carries no duration — so a slow or hanging TTS fetch
silently extends how long the popup stays up (the user hears nothing, but the app is
still internally blocked fetching/decoding) with zero visibility into how long that
took. This is a second, independent way to produce exactly this symptom.

**Fix: instrumented the full chain, not a guess-and-patch.** Added logging across
every step from `tts-end` through to the dismiss timer actually firing:
`assist_client`: `"tts-end received"` → `"tts playback finished (%.2fs)"` (brackets
fetch+decode+play) → `"run-end received"` → `_emit_done`'s `"emitting done"`.
`app.py`: `"done -> overlay.done()"`. `overlay.py` (previously silent): `"dismiss
scheduled: hide() in %d ms"`, `"hide() firing"`, `"dismiss interrupted %.2fs early"`
(if something re-entered before it fired), and critically the watchdog's
`"watchdog force-hiding a stuck %s popup after %.1fs (limit %.1fs)"` — the one line
that, if present, definitively proves the configured dismiss delay was never the
actual bottleneck.

**Verified via a probe harness** (real `_run_utterance`, faked I/O boundaries,
pattern reused from the barge-in verification earlier today): a clean fast
completion produced the expected chain in the right order with sane timing (0.15s);
a broken URL concretely demonstrated the fetch-timeout blind spot (7+ seconds,
previously invisible).

**Not yet root-caused against the user's actual incident** — didn't have enough
signal to pick between "HA-side latency," "TTS fetch/decode slowness," or "a
genuinely stuck popup rescued by the watchdog" from code-reading alone, and
couldn't reproduce their exact conversation/timing myself. The instrumentation
exists so the NEXT occurrence is a direct read of assistkey.log instead of a guess.
Tests: 54 pass (logging additions are print-only, no behavioural change to verify
beyond the existing suite).

---

## 2026-08-25 — "See my words as understood" — researched, tried one design, shipped a better one

User: can we show the words I'm speaking during "Listening..."? Checked HA's own
developer docs directly (developers.home-assistant.io/docs/voice/pipelines/) rather
than trust memory: the assist_pipeline WebSocket API has exactly ONE event that
carries transcribed text, `stt-end`, fired once after the full recording is
processed — no partial/interim transcript event exists. Confirmed this hasn't
changed recently (TTS got streaming in HA 2025.7; STT still hasn't, as of the HA
2026.7 build this app is connected to). So live word-by-word text while still
holding the key is not achievable from AssistKey's side — a genuine HA-protocol
constraint, not a missing feature here.

**Iteration 1 (built, then reverted):** redesigned the Listening→Thinking handoff
to morph in place (no slide-out/slide-in) plus a smooth height-grow when the
transcript arrives, so the HANDOFF itself would feel more continuous given the
words can only appear after release. Implemented `_morph_to`/`_morph_height` in
`overlay.py`, verified via a probe (alpha stayed ≥0.97 throughout vs. a real slide's
dip to 0), added 3 tests, updated README/KnowledgeBase. **User: "revert this, I
liked it more like it was"** — reverted cleanly via `git restore` (all uncommitted,
so a clean revert to HEAD, no manual unwinding needed) — and asked instead for the
words to stay visible for ~1s before the reply, so they have time to confirm they
were understood.

**Iteration 2 (shipped):** before building a hold-timer, user reconsidered: "Or
maybe we can show it WHILE responding?" — better than a fixed delay, since it adds
zero latency (the reply appears exactly as fast as before) AND the words stay
visible for the FULL reply duration, not just a fixed second. Implemented: `_draw`'s
RESPONSE branch now also renders the quoted `_user_text` (same `MUTED`/`FONT_USER`
quoted style already used in THINKING) above the `ASSISTANT` label, when present.
No new timers, no state-machine changes — purely additive to the existing draw
call. `_user_text` already resets to `""` in `listening()` at the start of every
utterance, so there's no risk of a stale transcript bleeding into a later reply.

**Verified:** a probe confirmed the transcript persists into RESPONSE and a fresh
utterance clears it correctly. Wrote 2 regression tests; the first version was
flaky (called `response_reset()`+`response_append()` back-to-back with pumps too
short for the prior Listening→Thinking slide to have settled, which correctly
chains a second transition but meant the test inspected content before it had
fully caught up) — not a feature bug, a test-timing bug; fixed with more realistic
pump durations and confirmed stable across 3 repeated runs. 56 tests pass overall.

---

## 2026-08-27 — CPU pegs a core + system-wide input lag: `pump()` clean-close busy loop

**Report (with hard evidence):** AssistKey pegged ~95% of one core, sustained (51%
lifetime avg over a 19h session); quitting it made desktop-wide keyboard/mouse lag
vanish instantly; ~36 MB working set (NOT memory); 32 threads. Correct instinct
from the reporter: a busy loop and/or a slow low-level input hook.

**Investigation.** py-spy is out (can't read Python 3.14.3 — "failed to find python
version"), and the reported process had already been quit, so no live attach.
Read the code for the hot loop instead. The `while True` loops in `app.py` (`_drain`,
`bootstrap`) are all correctly bounded (await/break/reschedule). The smoking gun was
`AssistClient.pump()`:
```
while True:
    try:
        async for raw in self.ws: ...
    except (websockets.ConnectionClosed, OSError): reconnect
```
A `websockets` async iterator **stops silently (no exception) on a normal 1000
close** (which HA sends on restart/update/idle) — so the `except` never fires, and
`while True` re-enters `async for` on the dead socket, which ALSO returns instantly:
a tight no-await loop. Holding the GIL, it starves pynput's WH_KEYBOARD_LL callback →
ALL system input lags. Explains every symptom, including "only after a while" (needs
one clean close to trigger).

**Proven, not assumed.** A local-websockets probe: on a normal close the first
`async for` returns with `raised=False`; re-iterating the closed socket runs 100,000
empty passes in 352 ms = **~284,000 spins/sec, no exception, no await**. That is the
pegged core.

**Fix (`assist_client.py`).** Set a `reason` in BOTH the try-fell-through
(`"closed cleanly"`) and `except` branches, then ALWAYS `await self._reconnect()`
before looping. `_reconnect` awaits (connect + backoff sleep), so a spin is now
impossible on either close path.

**Verified 3 ways:** (1) unit tests — a cleanly-closed fake ws makes pump reconnect
(with a `wait_for` timeout + a `sleep(0)` yield in the fake so a REGRESSION fails
fast instead of hanging); error-close still reconnects. (2) A regression-catch probe
ran the OLD logic against the same fake and confirmed it times out (reconnect never
called) — i.e. the test genuinely discriminates. (3) End-to-end: the REAL fixed
pump() against a REAL local ws that keeps clean-closing → 17 bounded reconnects/sec
(paced by reconnect's await), not 284k. Fresh app launched via VBS with the fix:
**0.0% idle CPU**, stable threads.

**The 32 threads — investigated, NOT the cause, not reproduced.** openWakeWord already
caps its onnxruntime to `inter_op_num_threads=1`/`intra_op_num_threads=1`, so wake is
not a thread explosion. A fresh idle instance shows 4 threads. The 32 were flagged by
the reporter as a *possibility*; most consistent with idle native audio-stream pools
under active voice use, and idle threads don't peg a core regardless — the single
busy-looping asyncio thread did. Left as: if thread count still grows unbounded over a
long session AFTER this fix, revisit with a live thread-name dump (see ToDo).

**Aside:** py-spy 0.4.2 cannot profile Python 3.14 — for future live profiling, either
a py-spy build with 3.14 support or an in-process `sys._current_frames()` sampler.
