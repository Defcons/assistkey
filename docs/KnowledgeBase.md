# KnowledgeBase — AssistKey

_The distilled MODEL: how AssistKey actually behaves. Every claim tagged FACT / HYPOTHESIS / ASSUMPTION / UNKNOWN. Read this first for behaviour/design reasoning. Numbers owned by code live in code — this points to them. Rules: ~/.claude/CLAUDE.md §5._

_Last verified: 2026-08-23 — UX pass recorded (hotkey capture, settings single-instance, mid-reconnect decline)._

## Architecture

- **FACT** — Four threads (see `app.py` module docstring): main = tkinter GUI (overlay + settings) draining a `queue.Queue` via `root.after(20, _drain)`; asyncio thread = persistent HA WebSocket, one utterance at a time; pynput listener = global hotkey; pystray = tray menu. Cross-thread: keyboard→asyncio via `run_coroutine_threadsafe`; asyncio/tray→GUI via the queue.
- **FACT** — All `Overlay` public methods must be called on the Tk main thread. The app guarantees this by only calling them from `_handle` (the queue drain).
- **FACT** — Single-instance is enforced by `app.kill_previous_instances()` at startup, matching other processes by **executable path** under `…\assistkey\.venv\Scripts\python*.exe` (NOT command line — a relative `run.bat` launch has no folder name to match). ⇒ Launching the real app kills any already-running copy and seizes the mic + hotkey; do NOT launch it just to smoke-test.
- **FACT** (invariant) — Wake pause/resume is asymmetric across threads: `app._hotkey_down`/`_on_wake` call `wake.pause()`; only a terminal `("done",)`/`("error",)` in `app._handle` calls `wake.resume()`. `AssistClient.start_utterance` emits `("done",)` even on the ws-not-connected decline so the pair always closes (fixed 2026-08-23; before, a wake trigger during startup left wake paused until restart). Test: `tests/test_assist_client.py`.
- **FACT** — `config.Config.save()` is atomic (temp file + `os.replace`) — the app force-kills older instances, so a truncating write could otherwise corrupt config.json and lose the token.
- **FACT** — `AssistClient.pump()` tolerates an unparseable frame (guards `json.loads`) and only reconnects on `ConnectionClosed`/`OSError`; other exceptions would kill the asyncio thread and zombie the app (tray alive, hotkey dead), so the parse guard matters.

## Popup overlay animation (`overlay.py`)

- **FACT** (measured 2026-08-23) — Windows default timer granularity is ~15.6 ms, so `root.after(16, …)` actually fires at **~23 ms mean / 31 ms max** (≈43 fps, jittery). Raising it with `winmm.timeBeginPeriod(1)` makes the same `after(16)` fire at a **rock-steady 16.1 ms** (true 60 fps). This jitter was the popup's "laggy" slide. Evidence: `probe_after.py` A/B run.
- **FACT** — `Overlay._hold_timer_res(ms)` raises the 1 ms resolution for a bounded, self-releasing window (a single tracked `after` job lowers it via `_drop_timer_res`), so the mostly-idle tray app doesn't pin the system timer. Balanced by the `_timer_raised` boolean — exactly one `timeBeginPeriod`/`timeEndPeriod` pair.
- **FACT** — Tweens (`_tween`) are **time-based**: per-frame progress `p = (now - start) / dur` from `time.monotonic()`, not a fixed step count. A late frame skips ahead to stay on schedule instead of dragging the animation out. Durations: `APPEAR_MS=300`, `VANISH_MS=180`, reply reveal `0.28 s` (all in `overlay.py`).
- **FACT** — The reply fade (`_start_reveal`) only changes the body text **colour** (lerp `REVEAL_FROM`→`TEXT`/`ERROR_COL`). It recolours the one cached canvas item `_body_item` via `itemconfigure` each frame — it does NOT rebuild the canvas. `_draw` records `_body_item`; a streaming redraw replaces it and carries the current colour itself (reveal step swallows the resulting `TclError`). Measured: 18 recolours + 2 full `_draw`s across a whole reply (was ~18 full rebuilds).
- **FACT** — Rounded corners use a magenta (`MAGIC = #ff00ff`) `-transparentcolor` knock-out; on non-Windows the corners just aren't transparent (TclError swallowed).
- **FACT** — Stuck-popup defence is layered: `_watchdog` (every 3 s, force terminal popups away; Listening is exempt), `_arm_hardhide` backstop after `hide()`, and `_hard_hide` as the unconditional reset that never depends on the transition state machine.
- **ASSUMPTION** — On-screen visual smoothness matches the measured cadence; the numbers are machine-verified but a human hasn't eyeballed the final build (see `Testing.md`).

## Pipeline / audio (`assist_client.py`)

- **FACT** — Push-to-talk: mic opens only while the hotkey is held; `signal_release` ends the utterance (true PTT, no silence detection). STT returns the user transcript once, at the end — hence "your words appear on release, not letter-by-letter" (README).
- **FACT** — 16 kHz mono, 100 ms blocks (`SAMPLE_RATE=16000`, `BLOCK=1600`). `COMPLETION_TIMEOUT=60 s`, `MAX_RECORD=120 s` hard cap.
- **FACT** — Device lists prefer WASAPI to de-duplicate Windows' multi-host-API device explosion (`_list_devices`).
- **FACT** — `start_utterance` gates on `_ws_is_open(self.ws)` (socket present AND `state.name == "OPEN"`), not `ws is None`: during a reconnect `self.ws` is the previous *closed* socket, so a None-check would let a doomed send through and surface a raw-exception error popup. A deliberate key-press while unavailable passes `notify_unavailable=True` → a gentle `("error", "Reconnecting…")`; a wake false-positive stays silent (`("done",)`). Test: `tests/test_assist_client.py`.

## Settings dialog (`overlay.py` — `SettingsDialog`)

- **FACT** (gotcha) — During hotkey capture ("Change…") a SECOND `pynput.keyboard.Listener` runs alongside the app's global `HotkeyListener`. Both see every key, so capture calls `suspend_hotkey`/`resume_hotkey` (wired to `app.HotkeyListener.suspend`/`resume`) to stop the global one firing an utterance mid-capture. Any new capture path must keep that pair balanced (resume on finish/cancel/close) — `_end_capture` centralises it.
- **FACT** — Capture: Esc cancels (restores the prior combo); a non-modifier key commits immediately; a modifier-only combo (e.g. Ctrl+Shift) commits when all keys are released. `seen` accumulates across partial releases so the full chord is captured. `_capturing` guards against a double-commit from overlapping press+release schedules.
- **FACT** — `Overlay.open_settings` is single-instance: it keeps `self._settings` and re-focuses a live dialog instead of stacking a new one (stale ref when the window is gone → builds fresh). Test: `probe_settings_guard.py` pattern.

## Wake word (`wake.py`)

- **FACT** — Optional, off by default (`wake_enabled`). openWakeWord, local model downloaded on first enable. HA's own VAD ends a wake-triggered utterance (no key release). On `_hotkey_down`/`_on_wake` the app calls `wake.pause()` to free the mic.
