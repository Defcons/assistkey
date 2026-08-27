# OrientationMap — AssistKey

_Last verified: 2026-08-27 — pump() fix + full audit (7 fixes) + interruptible TTS fetch + opt-in mic boost/high-pass._

## What this is
A Windows **system-tray push-to-talk app** for Home Assistant Assist. Hold a hotkey → talk → release; always-on-top toast shows Listening → your words → the streaming reply, which is also spoken. Python 3.12+ / tkinter + customtkinter, asyncio HA WebSocket, pynput hotkey, pystray tray. Entry: `app.py` (run via `AssistKey.vbs` silent or `run.bat` with a console).

## Project documentation
- **`OrientationMap.md`** (this) = the map: project shape, subsystem index, global landmines. Small repo ⇒ per-domain detail is inline here.
- **`docs/KnowledgeBase.md`** = how it behaves: facts/assumptions about the animation, pipeline, threads. Read first for behaviour/design.
- **`docs/ResearchJournal.md`** = history: how each fact was learned.
- **`docs/ToDo.md`** = deferred work. **`docs/Testing.md`** = checks still needing manual verification.

## Layout & conventions
- Flat repo, ~6 modules at root; automated tests in `tests/`; screenshots in `docs/`.
- Config + secrets in `config.json` (git-ignored — holds the HA token). Rotating log `assistkey.log` (+.1/.2/.3, git-ignored `assistkey.log*`) via `diag.py`.
- Style: terse, purposeful docstrings; broad `except Exception  # noqa: BLE001` guards around anything cosmetic/best-effort so the UI/pump never wedges. Match it.

## Subsystem index
- **App / wiring** — tray (icon reflects connected/active/disconnected; menu Settings/Stop/Open log/Quit), hotkey modes + barge-in, asyncio loop, single-instance, UI queue drain. Entry: `app.py` (`App`, `HotkeyListener`, `kill_previous_instances`). Threads + cross-thread rules: see KnowledgeBase §Architecture.
- **Diagnostics / logging** — rotating `assistkey.log` with timestamps + all-thread crash capture; token never logged. Entry: `diag.py` (`setup`, `log_config`, `redact_config`, `asyncio_exception_handler`). Details: KnowledgeBase §Architecture.
- **Overlay + Settings** — the toast popup (Listening/Thinking/Response/Error state machine + slide/fade animation, mic-level meter, click-to-stop) and the settings dialog. Entry: `overlay.py` (`Overlay`, `SettingsDialog`, `_Dropdown`, `_Tooltip`). Animation internals + timing facts: KnowledgeBase §Popup overlay animation.
- **Assist client** — persistent HA WebSocket, one utterance at a time: mic capture, pipeline events → UI callback, TTS playback, barge-in cancel, conversation continuity + follow-up. Entry: `assist_client.py` (`AssistClient`, `_ws_is_open`, `test_credentials`).
- **Config** — `config.json` load/save (atomic; token DPAPI-encrypted at rest), credential resolution, hotkey serialization. Entry: `config.py` (`Config`, `key_to_canon`, `hotkey_label`).
- **Credentials at rest** — DPAPI (per-user) encrypt/decrypt of the HA token; graceful plaintext fallback. Entry: `dpapi.py` (`protect`, `unprotect`, `is_protected`). Wired into `config.load`/`save`.
- **Autostart** — per-user Run-key "start at login" toggle (stdlib `winreg`, no admin). Entry: `autostart.py` (`is_enabled`, `set_enabled`). Checkbox in `SettingsDialog`.
- **Wake word** — optional openWakeWord listener, off by default. Entry: `wake.py` (`WakeListener`, `WAKE_WORDS`).
- **Packaging / CI** — single-file exe via `AssistKey.spec` + `build.bat` → `dist/AssistKey.exe`; tests run on Windows via `.github/workflows/ci.yml`.

## Global landmines
- **A `websockets` `async for` ends WITHOUT raising on a clean (1000) close.** Any `while True: async for x in ws:` MUST handle the no-exception exit (reconnect / break), never re-enter the loop on the dead socket — that's a tight no-await 100%-CPU spin, and because it holds the GIL it starves the pynput keyboard hook and lags ALL Windows input. This bit `AssistClient.pump()` hard (fixed 2026-08-27); see KnowledgeBase §Pipeline/audio. `_request_once` is safe (bounded, raises on close).
- **Never launch the real app to test** *(routine dev)*. `app.kill_previous_instances()` matches by exe path under `…\.venv\Scripts\python*.exe` and kills the user's running copy, seizing the mic + hotkey. Verify overlay/animation with a standalone `Overlay` on a `tk.Tk()` (see the probe scripts pattern), not by running `app.py`. (Exception: diagnosing a genuine RUNTIME bug like a CPU/hang — launching + measuring is then the right call, as with the 2026-08-27 busy-loop hunt.)
- **Overlay is main-thread-only.** Every `Overlay` method assumes the Tk main thread; asyncio/tray/wake code must route through the `ui_queue` (`app.App._drain`/`_handle`), never call the overlay directly.
- **Cosmetic code must never wedge the pump or the transition state machine.** Animation/redraw/timer paths swallow exceptions and always complete the transition (`_animating` reset); the `_drain` loop always reschedules. Preserve this when editing.
- **`config.json` holds the HA token (DPAPI-encrypted at rest)** — git-ignored; never commit it, never echo it into logs/docs. `config.save` writes it via `dpapi.protect` (per-user encryption) atomically (temp + `os.replace`); `config.load` decrypts to plaintext in memory. Legacy plaintext tokens still load and migrate on next save.
- **`kill_previous_instances` branches on `sys.frozen`** — the venv build matches `python*.exe` under `…\.venv\`; the PyInstaller exe matches its own `AssistKey.exe` name. Editing single-instance logic must keep BOTH paths correct, or a distributed exe could target unrelated `python.exe` processes.
- **Never call `AssistClient.start_utterance()` from a fresh trigger (hotkey/wake) — call `restart_utterance()`.** `is_active()` stays true for the WHOLE utterance including TTS playback, so a plain `start_utterance()` silently no-ops while a reply is still being spoken. `restart_utterance()` cancels-with-`suppress_done` first if needed, so the cancelled run's `("done",)` can't race the new run's `("listening",)` and clobber `HotkeyListener`/wake state mid-gesture. See KnowledgeBase §Pipeline/audio.
- **Every `wake.pause()` must be balanced by a `wake.resume()`.** Wake is paused before each utterance (`app._hotkey_down`/`_on_wake`) and resumed ONLY when the app sees a terminal `("done",)`/`("error",)`. So `AssistClient.start_utterance` MUST emit one even when it declines (e.g. ws not yet connected) — a silent early-return leaves wake-word listening paused until restart. Preserve this if you add another utterance entry point or early-return.

## Deferred
See `docs/ToDo.md` (deferrals) + `docs/Testing.md` (pending human verification — popup smoothness visual check).
