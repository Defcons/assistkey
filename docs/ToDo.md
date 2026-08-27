# ToDo — AssistKey

_STRICT deferral ledger: the moment anything is set aside, it lands here. Done → checked off and pruned on the next touch. An entry that can't say why it's still open gets deleted. Never mirror Testing.md (pending human verdicts live there ALONE)._

_Last updated: 2026-08-24_

---

## Open

- [ ] **Optional popup motion polish** — only if the smoothness fix isn't enough after the human visual check (see `Testing.md`): a slightly longer/eased or larger-travel slide. Deferred until the measured 60 fps fix is confirmed by eye.
- [ ] **Follow-up field path is best-effort** — `continue_conversation` is read from two known spots in `intent-end`; if a future/older HA nests it elsewhere, follow-up silently won't trigger. Revisit against a live HA that asks a follow-up question (needs a pipeline/agent that sets it). Confirm the actual path, then pin it.
- [ ] **Narrow residual race in `restart_utterance`** — if the user's key-release physically lands inside the (typically single-digit-ms) cancel-to-restart window, it can apply to the old, already-superseded `_release` event and be lost, leaving the new utterance recording until `MAX_RECORD` (120s) or another release. Deliberately not closed (see KnowledgeBase §Pipeline/audio + Journal 2026-08-24) — a realistic hold-to-talk gesture is never that fast, and existing safety nets bound the consequence. Revisit only if real-world reports suggest it's not as rare as assumed.

## Done

- [x] 2026-08-24 — Diagnostics logging: `diag.py` (rotating `assistkey.log`, timestamps, all-thread crash capture, redacted config, tray "Open log"). Replaced the raw stdout redirect + launch-time `_rotate_logs`.
- [x] 2026-08-24 — **Removed** the hotkey-capture (key-suppression) feature — its global keyboard hook lagged the whole system and there's no lag-free way to do it in this stack. See KnowledgeBase §Architecture (tried & removed).
- [x] 2026-08-24 — Fixed Save-stalls-the-app: reconnect only on credential change + try-connect-first (no 2s pre-sleep/backoff).
- [x] 2026-08-23 — Feature pass (9): CI, rotating log, tray connection colour + Stop, barge-in/cancel, follow-up + conversation continuity, mic level meter, DPAPI token-at-rest, autostart checkbox, PyInstaller exe (+ frozen-aware single-instance). See Journal 2026-08-23.
- [x] 2026-08-23 — Settings/hotkey UX pass (capture interference, Esc cancel, modifier-only, mid-reconnect decline, single-instance dialog).
- [x] 2026-08-23 — Hardening pass (wake pause/resume, atomic save, pump guard, docstring, get_running_loop).
- [x] 2026-08-23 — Popup slide/reply-fade smoothness (timer resolution + time-based tweens + lightweight reveal).
- [x] 2026-08-23 — Seed the project documentation set.
- [x] 2026-08-24 — Pre-filled GitHub "Report an issue…" (tray action; user-reviewed draft, not auto-upload). See Journal 2026-08-24.
- [x] 2026-08-24 — Barge-in always restarts Listening: hotkey/wake now call `restart_utterance()` instead of `start_utterance()`; fixed a real race where the cancelled utterance's stale `done` could clobber hotkey/wake state mid-restart. See Journal 2026-08-24.
- [x] 2026-08-27 — Fixed a severe `pump()` busy loop: a clean (1000) websocket close ended `async for` with no exception, so `while True` spun on the dead socket at ~284k/sec, pegging a core and lagging ALL system input (GIL-starved keyboard hook). Now always reconnects after the loop ends. See Journal 2026-08-27.

## Open (added 2026-08-27)
- [ ] **Thread-count watch (low priority, unconfirmed):** the CPU/lag report also noted 32 threads; couldn't reproduce (fresh idle app = 4; openWakeWord already caps onnxruntime to 1 thread/session). Likely idle native audio-stream pools under active use, not a leak — idle threads weren't the CPU cause (the pump spin was). IF thread count grows unbounded over a long session AFTER the pump fix, capture a live thread-name dump (`sys._current_frames()` sampler — py-spy 0.4.2 can't read Python 3.14) and investigate the audio-stream lifecycle in `_run_utterance`.

## Open (added 2026-08-27 audit)
- [x] 2026-08-27 — **TTS fetch made interruptible** (was: uninterruptible 15 s `urlopen`, the root of the barge-in delay + popup-linger classes). `_play` now races the fetch/playback executor future against `self._cancel` (`asyncio.wait` FIRST_COMPLETED) and returns immediately on a barge-in (~15 ms measured); timeout dropped to `TTS_FETCH_TIMEOUT=10 s`. Test + probes. See KB §Pipeline/audio + Journal 2026-08-27.
- [ ] **[LOW] `Config.load()` does no type validation** (config.py). A hand-edited but valid-JSON wrong type (`"hotkey": null`, `"dismiss_seconds": "abc"`) constructs fine (dataclasses don't enforce types) and crashes/degrades DOWNSTREAM instead of falling back to defaults — undercutting the "corrupt config shouldn't crash startup" intent for the non-syntax case. Only reachable via manual edits (the app's atomic save never writes bad types). Coerce/validate critical fields on load.
- [ ] **[LOW] DPAPI decrypt-failure leaves ciphertext as the "token"** (dpapi.unprotect returns the `dpapi:` string on failure). If config is copied to another Windows account, the app then "connects" with garbage (auth fails / reconnect loops) and Settings shows the garbled token; re-saving re-persists it. Treating an undecryptable token as empty would prompt a clean re-entry. By-design today (KB §Credentials) — revisit if it confuses anyone.
- [ ] **[LOW] No floor between reconnects on a pathological rapid accept-then-clean-close** (`pump`/`_reconnect`). `_reconnect` only backs off on connect FAILURE; if HA accepted then immediately clean-closed repeatedly, pump would reconnect at connect() speed (hot loop hammering HA). Unlikely, but a small min-interval would make it impossible.
- [x] 2026-08-27 — Full-code bug audit after the pump scare: 7 fixes (wake-stuck barge-in [HIGH], wake model-download flag, repeated-error dismiss, quit TclError, auth-fail socket leak, hotkey callback isolation, settings-test teardown race). Timer-res/animating/after-leaks/hotkey-capture/dpapi audited CLEAN with probes. See Journal 2026-08-27.
