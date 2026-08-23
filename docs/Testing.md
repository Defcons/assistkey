# Testing — AssistKey

_STRICT pending-manual-test queue. Each entry: repro steps + explicit pass criteria (runnable COLD, weeks later) + what's already machine-verified. Confirmed → graduate the durable result to KnowledgeBase/Journal and DELETE the entry._

_Last updated: 2026-08-23_

---

## PENDING — Popup animation smoothness (visual confirmation)

**Why human-only:** the fix is measured at the frame-cadence level, but "does it *look* smooth now" needs eyes. The full app can't be launched by the agent to check — `app.kill_previous_instances()` would terminate the user's running AssistKey and seize the mic + hotkey.

**Already machine-verified (2026-08-23):**
- Slide frame interval median 17 ms (≈60 fps), was ~23 ms with 31 ms jitter.
- Tweens time-based; reply fade recolours one item (18×) instead of rebuilding the canvas.
- `pytest tests/` 15/15 pass.

**Repro:** run the app (`run.bat` for a console, or `AssistKey.vbs` silent). Hold the hotkey (`|` per config, or your setting), speak a short command, release.

**Pass criteria (all must hold):**
1. The popup slides **up** into place with no visible stutter/steps — one continuous glide, ~0.3 s.
2. The reply text **fades in** smoothly (colour rises from dim to full) with no flicker or jump.
3. Streaming replies grow/reposition without tearing or lag.
4. The popup slides **down** and disappears cleanly after the dismiss delay; never leaves a ghost.
5. No increase in idle CPU when no popup is showing (timer resolution must drop back — `_timer_raised` False when idle; machine-checked, but confirm the machine doesn't feel busier).

**If still not smooth:** capture whether it's the *slide* (geometry) or the *fade* (alpha/colour); consider that DWM compositing of `-alpha` + `geometry` per frame on a heavily loaded GPU can still cost — next lever would be reducing simultaneous alpha+move, or a shorter travel (`SLIDE`).

---

## PENDING — Hotkey capture UX (Settings → "Change…")

**Why human-only:** the capture flow drives a real `pynput` listener into the Tk dialog; the branch logic (Esc/modifier-only/commit) and the suspend/resume of the global hotkey can't be exercised through the unit harness. The `HotkeyListener.suspend` mechanism itself IS unit-tested (`test_suspend_ignores_keys_then_resume_restores`); this is the end-to-end UI check.

**Repro:** run the app, right-click tray → Settings…, click **Change…** next to Hotkey.

**Pass criteria:**
1. **No interference** — while it says "Press keys — Esc cancels", press your *current* talk hotkey. No Listening popup / no utterance should start (the global hotkey is suspended during capture).
2. **Normal key** — press e.g. F8. It commits immediately and the box shows "F8".
3. **Combo** — press Ctrl+Space. Commits "Ctrl + Space".
4. **Modifier-only** — click Change…, press and hold Ctrl+Shift, release both. Commits "Ctrl + Shift" (previously impossible).
5. **Esc cancels** — click Change…, press Esc. The box reverts to the previously shown hotkey; nothing changes; the button returns to "Change…".
6. **After any of the above**, the global hotkey works again (hold it → Listening) — i.e. resume always fired.
7. **Single-instance** — with Settings open, trigger Settings again from the tray. The existing window is focused, not a second dialog.
8. **Mid-reconnect** — (hard to stage) if you press the hotkey while HA is reconnecting, you get a brief "Reconnecting to Home Assistant…" popup, not a raw error string.

---

## PENDING — Feature pass (2026-08-23): manual verification

All are machine-verified where possible (29 unit tests, build+launch of the exe); these need a human with a live Home Assistant.

1. **Tray connection colour** — start with HA unreachable (wrong URL): tray icon is **red**. Fix the URL / connect: turns **grey**. Hold the hotkey: **green** while Listening/working, back to grey after. Drop HA (stop it): returns to red within a few seconds.
2. **Barge-in / cancel** — while a reply is **speaking**, press the hotkey → speech stops immediately and the popup dismisses. Repeat with tray **Stop** and with **clicking the popup**. During **Thinking**, clicking the popup / tray Stop also aborts cleanly (no stuck popup).
3. **Mic level meter** — hold the hotkey and speak: a bar under "Listening" rises/falls with your voice; silence → near-empty.
4. **Follow-up** (Settings → Voice → Follow-up ON; needs an HA agent that asks a question) — after a reply that asks something, the app re-listens automatically (no key press), ends on your silence (HA VAD), and the answer continues the same conversation. With Follow-up OFF, it dismisses as before. If it never auto-listens, capture the `intent-end` payload to confirm where HA put `continue_conversation` (see ToDo).
5. **Conversation continuity** — with Follow-up OFF, issue a command, then within ~60 s press again and give a context-dependent follow-up ("...and turn it off") — HA should keep context.
6. **DPAPI token** — open `config.json` after saving: the token shows as `"dpapi:..."`, not plaintext. Test connection still works; a fresh launch stays connected (decrypts). (An existing plaintext token migrates to encrypted on the next Save.)
7. **Start at login** — Settings → Startup → toggle ON → Save. Check `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` has an `AssistKey` value; sign out/in → app starts silently. Toggle OFF → value removed.
8. **Standalone exe** — on a machine WITHOUT the dev venv, run `dist\AssistKey.exe`: tray app starts, Settings opens, hold-to-talk works, wake-word downloads its model on first enable. (Build with `build.bat`.)
9. **Rotating log** — force an error (e.g. bad token so a traceback logs), relaunch, confirm the prior log is preserved as `assistkey.log.1`.

---

## PENDING — QA round (2026-08-23): hotkey capture, tray click, follow-up

1. **Hotkey suppression** — with the app running and hotkey = a normal key (e.g. `K`): focus a text field in another app, hold the hotkey to talk → the field must NOT receive "KKKK". Other keys type normally. ⚠ The captured key is unusable elsewhere while running — if you need to type it, pick an F-key hotkey. For a combo (e.g. Ctrl+Space): Ctrl still works alone (Ctrl+C etc.), only the full combo is swallowed.
2. **Tray click → Settings** — double-click the tray icon (single-click where the OS supports it) opens Settings; right-click still shows the menu.
3. **Follow-up on a question** — with Follow-up ON, trigger a request the agent can't parse so it replies with a question ("What did you mean?"). The app should auto-listen for your answer (reply ends with "?"). Statements ("Turned on the lights.") should NOT re-listen.
