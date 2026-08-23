# ResearchJournal — AssistKey

_Append-only chronological ledger: what changed, what was tested, what was learned. Old entries are never rewritten; durable confirmed facts get promoted to KnowledgeBase. Rules: ~/.claude/CLAUDE.md §5._

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

**Also this session:** seeded the six-doc repo bible (was absent) — small-repo mode: `OrientationMap.md` at root, this + KnowledgeBase/ToDo/Testing in `docs/`.

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
