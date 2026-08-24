# ToDo — AssistKey

_STRICT deferral ledger: the moment anything is set aside, it lands here. Done → checked off and pruned on the next touch. An entry that can't say why it's still open gets deleted. Never mirror Testing.md (pending human verdicts live there ALONE)._

_Last updated: 2026-08-23_

---

## Open

- [ ] **Optional popup motion polish** — only if the smoothness fix isn't enough after the human visual check (see `Testing.md`): a slightly longer/eased or larger-travel slide. Deferred until the measured 60 fps fix is confirmed by eye.
- [ ] **Follow-up field path is best-effort** — `continue_conversation` is read from two known spots in `intent-end`; if a future/older HA nests it elsewhere, follow-up silently won't trigger. Revisit against a live HA that asks a follow-up question (needs a pipeline/agent that sets it). Confirm the actual path, then pin it.

## Done

- [x] 2026-08-24 — Diagnostics logging: `diag.py` (rotating `assistkey.log`, timestamps, all-thread crash capture, redacted config, tray "Open log"). Replaced the raw stdout redirect + launch-time `_rotate_logs`.
- [x] 2026-08-24 — **Removed** the hotkey-capture (key-suppression) feature — its global keyboard hook lagged the whole system and there's no lag-free way to do it in this stack. See KnowledgeBase §Architecture (tried & removed).
- [x] 2026-08-24 — Fixed Save-stalls-the-app: reconnect only on credential change + try-connect-first (no 2s pre-sleep/backoff).
- [x] 2026-08-23 — Feature pass (9): CI, rotating log, tray connection colour + Stop, barge-in/cancel, follow-up + conversation continuity, mic level meter, DPAPI token-at-rest, autostart checkbox, PyInstaller exe (+ frozen-aware single-instance). See Journal 2026-08-23.
- [x] 2026-08-23 — Settings/hotkey UX pass (capture interference, Esc cancel, modifier-only, mid-reconnect decline, single-instance dialog).
- [x] 2026-08-23 — Hardening pass (wake pause/resume, atomic save, pump guard, docstring, get_running_loop).
- [x] 2026-08-23 — Popup slide/reply-fade smoothness (timer resolution + time-based tweens + lightweight reveal).
- [x] 2026-08-23 — Seed the project documentation set.
