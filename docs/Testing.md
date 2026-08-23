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
