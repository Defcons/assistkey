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
