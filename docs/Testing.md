# Testing — AssistKey

_STRICT pending-manual-test queue. Each entry: repro steps + explicit pass criteria (runnable COLD, weeks later) + what's already machine-verified. Confirmed → graduate the durable result to KnowledgeBase/Journal and DELETE the entry. Rules: ~/.claude/CLAUDE.md §5._

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
