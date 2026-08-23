# OrientationMap — AssistKey

_Last verified: 2026-08-23 — popup smoothed (timer-res + time-based tweens); hardening pass (wake pause/resume, atomic save, pump)._

## What this is
A Windows **system-tray push-to-talk app** for Home Assistant Assist. Hold a hotkey → talk → release; always-on-top toast shows Listening → your words → the streaming reply, which is also spoken. Python 3.12+ / tkinter + customtkinter, asyncio HA WebSocket, pynput hotkey, pystray tray. Entry: `app.py` (run via `AssistKey.vbs` silent or `run.bat` with a console).

## The repo bible (this file is one of six — §5)
- **`OrientationMap.md`** (this) = injected hub: shape, subsystem index, global landmines. Small repo ⇒ domain detail is inline here (no NavigationMap yet; split out when this nears ~20 KB).
- **`docs/KnowledgeBase.md`** = the MODEL: FACT/HYP-tagged behaviour (animation timing facts, pipeline, threads). Read first for behaviour/design.
- **`docs/ResearchJournal.md`** = HISTORY: how each fact was learned.
- **`docs/ToDo.md`** = deferral ledger. **`docs/Testing.md`** = pending human verdicts.

## Layout & conventions
- Flat repo, ~6 modules at root; automated tests in `tests/`; screenshots in `docs/`.
- Config + secrets in `config.json` (git-ignored — holds the HA token). Logs to `assistkey.log` (git-ignored).
- Style: terse, purposeful docstrings; broad `except Exception  # noqa: BLE001` guards around anything cosmetic/best-effort so the UI/pump never wedges. Match it.

## Subsystem index
- **App / wiring** — tray, hotkey modes, asyncio loop, single-instance, UI queue drain. Entry: `app.py` (`App`, `HotkeyListener`, `kill_previous_instances`). Threads + cross-thread rules: see KnowledgeBase §Architecture.
- **Overlay + Settings** — the toast popup (Listening/Thinking/Response/Error state machine + slide/fade animation) and the settings dialog. Entry: `overlay.py` (`Overlay`, `SettingsDialog`, `_Dropdown`, `_Tooltip`). Animation internals + timing facts: KnowledgeBase §Popup overlay animation.
- **Assist client** — persistent HA WebSocket, one utterance at a time: mic capture, pipeline events → UI callback, TTS playback. Entry: `assist_client.py` (`AssistClient`, `test_credentials`, device listing).
- **Config** — `config.json` load/save, credential resolution, hotkey (canonical key) serialization. Entry: `config.py` (`Config`, `key_to_canon`, `hotkey_label`).
- **Wake word** — optional openWakeWord listener, off by default. Entry: `wake.py` (`WakeListener`, `WAKE_WORDS`).

## Global landmines
- **Never launch the real app to test.** `app.kill_previous_instances()` matches by exe path under `…\.venv\Scripts\python*.exe` and kills the user's running copy, seizing the mic + hotkey. Verify overlay/animation with a standalone `Overlay` on a `tk.Tk()` (see the probe scripts pattern), not by running `app.py`.
- **Overlay is main-thread-only.** Every `Overlay` method assumes the Tk main thread; asyncio/tray/wake code must route through the `ui_queue` (`app.App._drain`/`_handle`), never call the overlay directly.
- **Cosmetic code must never wedge the pump or the transition state machine.** Animation/redraw/timer paths swallow exceptions and always complete the transition (`_animating` reset); the `_drain` loop always reschedules. Preserve this when editing.
- **`config.json` holds the HA long-lived token** — git-ignored; never commit it, never echo it into logs/docs. Written atomically (`config.Config.save` → temp + `os.replace`) so a force-kill mid-write can't truncate away the token.
- **Every `wake.pause()` must be balanced by a `wake.resume()`.** Wake is paused before each utterance (`app._hotkey_down`/`_on_wake`) and resumed ONLY when the app sees a terminal `("done",)`/`("error",)`. So `AssistClient.start_utterance` MUST emit one even when it declines (e.g. ws not yet connected) — a silent early-return leaves wake-word listening paused until restart. Preserve this if you add another utterance entry point or early-return.

## Deferred
See `docs/ToDo.md` (deferrals) + `docs/Testing.md` (pending human verification — popup smoothness visual check).
