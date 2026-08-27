# Testing — AssistKey

_Pending manual checks: what needs a human — eyes, a live Home Assistant, or a second machine — that automated tests + probes couldn't cover. Each has repro + pass criteria (runnable cold) + what's already machine-verified. Confirm one → delete it (graduate any durable result to KnowledgeBase/Journal). Ordered most-valuable first. Many B-items are probably already fine from daily use; they just haven't been formally rubber-stamped._

_Last updated: 2026-08-27 (dead-mic hang fix; repo now public)_

---

## A — Regression checks (things that broke and were fixed — most worth a look)

### A1. No system-wide input lag / idle stays cold ⭐ highest value
Two separate root causes fixed here: the removed hotkey-capture keyboard hook (2026-08-24) and the `pump()` clean-close busy loop (2026-08-27). Machine-verified: fresh app idles **0.0 % CPU**; the busy loop is proven fixed 3 ways.
- Leave AssistKey running a full session. Its `pythonw.exe` (Task Manager) sits ~0 % CPU throughout — **including across a Home Assistant restart/update** (what used to trigger the spin).
- Typing/mouse anywhere in Windows feels completely normal the whole time.
- After an HA restart: tray goes red→grey within seconds; `assistkey.log` shows `connection closed cleanly; reconnecting` then `connected` — a single reconnect, NOT a flood.

### A2. Barge-in: a hotkey press during a reply always restarts Listening
Machine-verified end-to-end (fake I/O): mid-TTS barge-in stops audio, emits no stale `done`, starts a fresh utterance.
- **Mid-speech:** while a reply is being spoken aloud, press the hotkey → it cuts off and Listening appears in **one** press. (During a genuinely *slow* TTS fetch this can still take up to ~15 s — that's the uninterruptible-fetch item in ToDo, not a regression.)
- **Hold-mode, natural gesture:** barge in, hold, speak, release → ends on release (→ Thinking), not recording until timeout.
- **Toggle-mode:** same.
- **Wake word (if on):** saying it while a reply plays also barges in. (Flag if you'd rather it never interrupt.)

### A3. Wake survives a barge-in during a reply (audit fix 2026-08-27)
With wake-word ON: get a reply, press the hotkey to barge in during it, then confirm the wake word still works afterward (say it → it responds). Before the fix, that sequence left wake paused until restart.

### A4. Two quick errors don't stick the popup (audit fix 2026-08-27)
HA disconnected → press the hotkey twice within a second (two "Reconnecting…" popups). The popup slides away after ~2 s (`dismiss_seconds`), NOT ~22 s. `assistkey.log` shows no `watchdog force-hiding a stuck error popup`.

### A5. Popup looks smooth
Machine-verified: ~60 fps slide, one-item reply fade, idle timer-resolution drops back.
- Hold hotkey, speak, release: Listening slides up smoothly (no steps); reply fades in without flicker; streaming text grows without tearing; popup slides away cleanly, no ghost. Your recognised words stay visible through the reply.

---

## B — Feature confirmations (never formally checked; likely fine from daily use)

### B1. Hotkey capture (Settings → Change…)
The capture flow drives a real pynput listener into the dialog; can't be unit-tested end-to-end.
1. While capturing ("Press keys — Esc cancels"), pressing your *current* hotkey does NOT start an utterance (global hotkey suspended).
2. A normal key (e.g. F8) commits immediately; a combo (Ctrl+Space) commits; a **modifier-only** combo (Ctrl+Shift, commit on release) works.
3. **Esc** cancels (reverts to the previous hotkey).
4. After any of those, the global hotkey works again (resume fired).
5. Triggering Settings again while it's open focuses the existing window (no second dialog).

### B2. Tray icon states & menu
- Red when HA unreachable, grey when connected, green while listening/working.
- Double-click tray → Settings; right-click → menu (Settings / Stop / Open log / Report an issue… / Quit).
- **Stop**, or **clicking the popup**, while a reply speaks → stops + dismisses (no new Listening — correct for these, unlike a hotkey press). Same during Thinking, no stuck popup.

### B3. Mic level meter
Hold + speak → a bar under "Listening" tracks your voice; silence → near-empty.

### B4. Follow-up & conversation continuity (needs an agent that asks questions)
- Follow-up ON: a reply that asks something → auto re-listens (no keypress), popup reads **"Follow-up — answer now"**, ends on your silence (HA VAD), answer continues the conversation. A statement reply does NOT re-listen.
- Follow-up OFF: issue a command, then within ~60 s a context-dependent follow-up ("…and turn it off") — HA keeps context.
- If follow-up never triggers, grab the `intent-end` payload (see the field-path item in ToDo).

### B5. Settings save is instant
Change a non-connection setting (e.g. Dismiss after) → Save → stays connected (tray grey), works immediately, no multi-second stall. Changing URL/token reconnects, but promptly.

### B6. Token encrypted at rest (DPAPI)
After Save, `config.json` shows `"ha_token": "dpapi:…"`, not plaintext; Test connection works; a fresh launch stays connected (decrypts). A pre-existing plaintext token migrates on the next Save.

### B7. Start at login
Settings → Startup ON → Save → `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\AssistKey` exists → sign out/in starts it silently. OFF removes the value.

### B8. Diagnostics log
- `assistkey.log` has timestamped content (session-start banner, redacted `config:` line, `connected …`).
- Your real token never appears (search for it — the `config:` line shows `token=set`).
- Cause an error (bad mic device / wrong URL) → recorded with a traceback + level, including background-thread errors.
- Log rolls to `.1/.2/.3` at ~1 MB; history survives restarts (appended, not truncated).

---

## C — Gated (need setup before they can be checked)

### C1. Standalone .exe — needs a machine WITHOUT the dev venv
Build with `build.bat`, copy `dist\AssistKey.exe` to a clean Windows box: tray starts, Settings opens, hold-to-talk works, wake-word downloads its model on first enable.

### C2. "Report an issue…" — now UNBLOCKED (repo is public as of 2026-08-27)
Machine-verified: a draft built against your REAL config + a realistic crash log has no HA host / Windows username / IP / token, while the real error text (message, exception type, file/line) survives. The repo is now public, so the link no longer 404s. Quick human click-through: tray → **Report an issue…** → a prefilled GitHub draft opens in the browser; the excerpt is the most recent error, redacted; your token is absent; nothing is sent until you click Submit.

---

## Retired (resolved — kept out of the queue for reference)
- **"Diagnose popup lingers past `dismiss_seconds`" (2026-08-24):** root-caused in the 2026-08-27 audit. Two mechanisms, both handled — the repeated-`error()` dismiss bug (FIXED, now A4) and the uninterruptible 15 s TTS fetch (a genuine cause of lingering, now the deferred item in ToDo). The `tts playback finished (%.2fs)` / watchdog logging stays in place to pinpoint any future case instantly.
