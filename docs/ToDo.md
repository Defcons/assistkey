# ToDo — AssistKey

_STRICT deferral ledger: the moment anything is set aside, it lands here. Done → checked off and pruned on the next touch. An entry that can't say why it's still open gets deleted. Never mirror Testing.md (pending human verdicts live there ALONE). Rules: ~/.claude/CLAUDE.md §5._

_Last updated: 2026-08-23_

---

## Open

- [ ] **Hotkey-capture interference** — while the Settings "Change…" capture listener is running, the main global `HotkeyListener` is still live, so pressing the *current* hotkey during capture can start a real utterance. Fix = pause the main listener while `SettingsDialog._capture_hotkey` is active. Deferred: behavioural change with regression risk, out of scope for the 2026-08-23 correctness pass.
- [ ] **Hotkey capture has no cancel + can't be modifier-only** — capture only finishes on a non-modifier key (`overlay.py` `_capture_hotkey`), so there's no Esc-to-cancel and you can't bind e.g. Ctrl-only. Minor UX; add an Esc cancel + allow committing a modifier combo.
- [ ] **Error popup if a key is pressed mid-reconnect** — during reconnect `self.ws` is the old *closed* socket (not None), so `start_utterance` proceeds and the first `ws.send` throws → an "error" popup. Could check `ws.state`/connection health before starting and show a gentler "reconnecting…" instead. Low priority.
- [ ] **No guard against two Settings dialogs** — each tray "Settings…" click builds a new `SettingsDialog`; two can stack. Add a single-instance guard (focus the existing one). Low priority.
- [ ] **Optional popup polish, only if the smoothness fix isn't enough** — after the human visual check (see `Testing.md`), consider a slightly longer/eased or larger-travel slide. Deferred until the measured 60 fps fix is confirmed by eye — don't gild first.

## Done

- [x] 2026-08-23 — Hardening pass: wake pause/resume terminal-emit fix (+test), atomic `config.save`, `pump()` bad-frame guard, `config.py` docstring, `get_running_loop`. See Journal 2026-08-23.
- [x] 2026-08-23 — Popup slide/reply-fade smoothness (timer resolution + time-based tweens + lightweight reveal). See Journal 2026-08-23.
- [x] 2026-08-23 — Seed the six-doc repo bible (was absent).
