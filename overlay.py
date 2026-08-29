"""Always-on-top toast popup overlay (tkinter).

The Overlay shows a sequence of popups centred at the bottom-middle of whichever
monitor the cursor is on: Listening… → Thinking… → the reply. Each popup slides
up from the bottom as it appears and slides back down as it leaves; states
replace one another (the old popup slides out, the new one slides in). All public
methods must be called on the Tk main thread (the app drains a queue there).

The settings dialog lives in settings.py (split 2026-08-29 — this file is the
stable, landmine-dense animation code; that one grows with every new setting);
SettingsDialog is re-exported here for compatibility.
"""

from __future__ import annotations

import ctypes
import logging
import time

import tkinter as tk

import config as cfg
from settings import SettingsDialog
from winscreen import list_monitors, monitor_workarea_at, primary_workarea

log = logging.getLogger("assistkey.overlay")


MAGIC = "#ff00ff"          # transparent knock-out colour for rounded corners
CARD = "#20242f"           # slightly warmer/softer card
BORDER = "#333a4d"
TITLE_COL = "#c9d0da"      # softened (was near-white, too harsh)
ACCENT = "#93b7f2"         # a touch softer blue
MUTED = "#9aa0a6"
TEXT = "#d6dce4"           # soft light-grey reply text, gentler contrast
ERROR_COL = "#f0a6a0"      # softer red
REVEAL_FROM = "#2b303c"    # colour the reply fades UP from (near the card)

WIDTH = 620
PAD = 24
MARGIN = 44                # gap above the taskbar at rest
SLIDE = 40                 # px the popup travels while sliding in/out
APPEAR_MS = 300            # gentler entrance
VANISH_MS = 180

FONT_LABEL = ("Segoe UI Semibold", 10)
FONT_STATUS = ("Segoe UI Semilight", 17)
FONT_USER = ("Segoe UI Light", 13, "italic")
FONT_BODY = ("Segoe UI Semilight", 15)

HIDDEN, LISTENING, THINKING, RESPONSE, ERROR = "hidden", "listening", "thinking", "response", "error"


def _ease_out(t):
    return 1 - (1 - t) ** 3


def _ease_in(t):
    return t ** 3


def _lerp_color(a, b, t):
    a, b = a.lstrip("#"), b.lstrip("#")
    return "#" + "".join(
        f"{round(int(a[i:i + 2], 16) + (int(b[i:i + 2], 16) - int(a[i:i + 2], 16)) * t):02x}"
        for i in (0, 2, 4))


def _round_points(x0, y0, x1, y1, r):
    return [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
        x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
    ]


class Overlay:
    def __init__(self, root: tk.Tk, config: cfg.Config):
        self.root = root
        self.config = config
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.0)
        self.win.configure(bg=MAGIC)
        try:
            self.win.attributes("-transparentcolor", MAGIC)
        except tk.TclError:
            pass  # non-Windows: corners just won't be transparent

        self.canvas = tk.Canvas(self.win, bg=MAGIC, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._on_click)  # click the popup to stop a run

        self._state = HIDDEN
        self._target = HIDDEN
        self._user_text = ""
        self._response = ""
        self._assistant = "Assistant"  # reply label — set from the HA pipeline name
        self._dots = 0
        self._pulse = 0
        self._h = 0
        self._area = None
        self._shown = False
        self._animating = False
        self._dirty = False
        self._pulse_job = None
        self._slide_job = None
        self._dismiss_job = None
        self._dismiss_at = None  # monotonic time the current dismiss timer targets, for logging
        self._hardhide_job = None
        self._reveal = 1.0       # 0..1 soft fade-in of reply text
        self._reveal_job = None
        self._timer_raised = False   # Windows 1 ms timer resolution held while animating
        self._timer_job = None
        self._body_item = None       # reply text item id, so the reveal recolours it cheaply
        self._settings = None        # the open SettingsDialog, if any (single-instance guard)
        self._level = 0.0            # 0..1 mic level for the Listening meter
        self._level_item = None      # the level-bar fill rect, updated cheaply per frame
        self._level_geom = None
        self._follow_up = False      # this Listening popup is an automatic follow-up
        self.on_cancel = None        # set by the app: click the popup to stop / cancel
        self._last_change = time.monotonic()
        self._watchdog()  # periodic safety net against a stuck popup

    def _touch(self):
        self._last_change = time.monotonic()

    # ---- public API (main thread) ------------------------------------------

    def listening(self, follow_up=False):
        self._cancel_dismiss()
        self._user_text = ""
        self._response = ""
        self._level = 0.0
        self._follow_up = follow_up
        self._touch()
        self._enter(LISTENING)

    def set_level(self, v):
        """Feed the Listening mic meter (0..1). Attack fast, release slow. Updates
        only the bar rectangle — no full redraw — so it stays cheap at ~10/s."""
        v = max(0.0, min(1.0, float(v)))
        self._level = v if v > self._level else self._level * 0.6 + v * 0.4
        if (self._state == LISTENING and self._shown and not self._animating
                and self._level_item is not None):
            try:
                x0, y, x1, h = self._level_geom
                w = (x1 - x0) * self._level
                self.canvas.coords(self._level_item, x0, y, x0 + max(0.001, w), y + h)
            except (tk.TclError, TypeError, ValueError):
                pass

    def _on_click(self, _e=None):
        # Click the popup to stop a run in progress (barge-in / dismiss a wake mis-fire).
        if self._state in (LISTENING, THINKING, RESPONSE) and self.on_cancel:
            self.on_cancel()

    def thinking(self):
        self._touch()
        self._enter(THINKING)

    def set_assistant(self, name: str):
        self._assistant = (name or "Assistant").strip() or "Assistant"

    def set_user_text(self, text: str):
        self._user_text = (text or "").strip()
        self._touch()
        self._refresh_if(THINKING)

    def response_reset(self):
        self._response = ""
        self._touch()
        self._enter(RESPONSE)

    def response_append(self, token: str):
        self._response += token
        self._touch()
        if self._state == RESPONSE and self._target == RESPONSE:
            self._refresh_if(RESPONSE)
        else:
            self._enter(RESPONSE)

    def response_final(self, text: str):
        self._response = text
        self._touch()
        if self._state == RESPONSE and self._target == RESPONSE:
            self._refresh_if(RESPONSE)
        else:
            self._enter(RESPONSE)

    def error(self, message: str):
        self._response = message
        self._touch()               # keep _last_change fresh so the watchdog measures from NOW
        self._enter(ERROR)
        # ALWAYS (re)start the dismiss timer here. ERROR otherwise self-schedules its
        # dismiss ONLY from _finish (the transition path); a repeated error() on an
        # already-settled error popup takes _enter's same-state _refresh branch, which
        # skips _finish — so the previously-scheduled dismiss would be cancelled and
        # never replaced, leaving the popup up until the ~22 s watchdog. Two quick
        # ("error","Reconnecting…") events (e.g. hotkey pressed twice while HA is down)
        # hit exactly this. _schedule_dismiss cancels-then-reschedules, so it's safe on
        # both the transition path (redundant with _finish) and the settled path.
        self._schedule_dismiss()

    def done(self):
        self._schedule_dismiss()

    def hide(self):
        log.info("hide() firing (state=%s)", self._state)
        self._cancel_dismiss()
        self._enter(HIDDEN)
        self._arm_hardhide(VANISH_MS + 500)  # guarantee withdrawal even if animation wedges

    def _hard_hide(self):
        """Unconditional reset: withdraw the window and clear all state.

        The last line of defence against a stuck popup — never depends on the
        transition state machine, so a wedged animation can't block it.
        """
        self._cancel_slide()
        self._cancel_dismiss()
        self._stop_pulse()
        if self._hardhide_job is not None:
            self.root.after_cancel(self._hardhide_job)
            self._hardhide_job = None
        if self._reveal_job is not None:
            self.root.after_cancel(self._reveal_job)
            self._reveal_job = None
        if self._timer_job is not None:
            self.root.after_cancel(self._timer_job)
        self._drop_timer_res()
        self._reveal = 1.0
        self._animating = False
        self._shown = False
        self._state = HIDDEN
        self._target = HIDDEN
        try:
            self.win.attributes("-alpha", 0.0)
            self.win.withdraw()
        except tk.TclError:
            pass

    def _arm_hardhide(self, ms):
        if self._hardhide_job is not None:
            self.root.after_cancel(self._hardhide_job)
        self._hardhide_job = self.root.after(int(ms), self._hard_hide_if_hiding)

    def _hard_hide_if_hiding(self):
        self._hardhide_job = None
        if self._target == HIDDEN and (self._shown or self._animating):
            self._hard_hide()

    def _watchdog(self):
        # Force a stuck terminal popup away. Listening is exempt (a hold is
        # legitimately open-ended); the pipeline's own watchdog ends that.
        try:
            if self._shown and self._state != LISTENING:
                limit = 90 if self._state == THINKING else self.config.dismiss_seconds + 20
                elapsed = time.monotonic() - self._last_change
                if elapsed > limit:
                    # This should be rare: the normal dismiss timer (_schedule_dismiss)
                    # is meant to have hidden it long before this fires. If you're
                    # reading this in the log, THIS — not the configured dismiss delay —
                    # is why the popup stayed up so long; it means "done" never reached
                    # done()/hide() for this popup.
                    log.warning("watchdog force-hiding a stuck %s popup after %.1fs (limit %.1fs)",
                               self._state, elapsed, limit)
                    self._hard_hide()
        except Exception:  # noqa: BLE001 - a watchdog must never throw
            pass
        self.root.after(3000, self._watchdog)

    def open_settings(self, client, on_save, suspend_hotkey=None, resume_hotkey=None):
        # Single-instance: focus the existing dialog instead of stacking a new one.
        if self._settings is not None:
            try:
                if self._settings.win.winfo_exists():
                    self._settings.win.deiconify()
                    self._settings.win.lift()
                    self._settings.win.focus_force()
                    return
            except (tk.TclError, AttributeError):
                pass  # stale reference (window already destroyed) — fall through
        self._settings = SettingsDialog(self.root, self.config, client, on_save,
                                        suspend_hotkey=suspend_hotkey, resume_hotkey=resume_hotkey)

    # ---- transitions --------------------------------------------------------

    def _enter(self, target):
        self._target = target
        if self._animating:
            return  # the running animation chases the latest target on finish
        if target == self._state and self._shown:
            self._refresh()
            return
        self._transition()

    def _transition(self):
        self._animating = True
        if self._shown and self._state != HIDDEN:
            self._slide_out(self._after_out)
        else:
            self._after_out()

    def _after_out(self):
        target = self._target
        if target == HIDDEN:
            if self._shown:
                self.win.withdraw()
                self._shown = False
            self._state = HIDDEN
            self._finish()
            return
        self._state = target
        self._area = self._compute_area()
        self._stop_pulse()
        self._dots = 0
        self._pulse = 0
        self._reveal = 0.0 if target in (RESPONSE, ERROR) else 1.0
        try:
            self._draw()
            self._shown = True
            self.win.deiconify()
            self.win.lift()
            self.win.attributes("-topmost", True)  # re-assert on every show
            self.win.attributes("-alpha", 0.0)
            self._place(self._rest_y() + SLIDE)
        except Exception:  # noqa: BLE001 - a draw/place failure must never wedge or crash
            self._animating = False
            self._hard_hide()
            return
        if self._state in (LISTENING, THINKING):
            self._start_pulse()
        self._slide_in(self._finish)

    def _finish(self):
        self._animating = False
        if self._target != self._state:
            self._transition()
            return
        if self._state in (RESPONSE, ERROR):
            self._start_reveal()
        if self._state == ERROR:
            self._schedule_dismiss()
        if self._dirty:
            self._dirty = False
            self._refresh()

    def _start_reveal(self):
        if self._reveal_job is not None:
            self.root.after_cancel(self._reveal_job)
            self._reveal_job = None
        dur = 0.28
        self._hold_timer_res(int(dur * 1000) + 200)
        start = time.monotonic()

        def step():
            p = (time.monotonic() - start) / dur
            last = p >= 1.0
            self._reveal = 1.0 if last else _ease_out(p)
            # Only the reply text's colour changes here — recolour that one item
            # instead of clearing and rebuilding the whole canvas every frame.
            try:
                if self._shown and not self._animating and self._state in (RESPONSE, ERROR):
                    target = TEXT if self._state == RESPONSE else ERROR_COL
                    self.canvas.itemconfigure(
                        self._body_item, fill=_lerp_color(REVEAL_FROM, target, self._reveal))
            except tk.TclError:
                pass  # item was replaced by a streaming redraw — it carries the colour itself
            if not last and self._shown and not self._animating and self._state in (RESPONSE, ERROR):
                self._reveal_job = self.root.after(16, step)
            else:
                self._reveal = 1.0
                self._reveal_job = None

        step()

    def _refresh(self):
        if not self._shown or self._animating:
            return
        try:
            self._draw()
            self.win.attributes("-alpha", 0.97)
            self._place(self._rest_y())
        except Exception:  # noqa: BLE001 - never let a redraw crash the caller
            pass

    def _refresh_if(self, state):
        if self._state != state:
            return
        if self._animating:
            self._dirty = True
        else:
            self._refresh()

    # ---- drawing ------------------------------------------------------------

    def _draw(self):
        c = self.canvas
        c.delete("all")
        self._body_item = None
        self._level_item = None
        y = PAD
        st = self._state

        if st == LISTENING:
            y = self._status_row(y, "Follow-up" if self._follow_up else "Listening",
                                 self._pulse_colour())
            if self._follow_up:
                item = c.create_text(WIDTH / 2, y + 6, anchor="n",
                                     text="answer now — no need to press the key",
                                     fill=MUTED, font=FONT_USER,
                                     width=WIDTH - 2 * PAD, justify="center")
                y = c.bbox(item)[3]
            y = self._level_bar(y + 12)
        elif st == THINKING:
            y = self._status_row(y, "Thinking", ACCENT)
            if self._user_text:
                item = c.create_text(WIDTH / 2, y + 8, anchor="n",
                                     text=f"“{self._user_text}”", fill=MUTED,
                                     font=FONT_USER, width=WIDTH - 2 * PAD, justify="center")
                y = c.bbox(item)[3]
        elif st == RESPONSE:
            if self._user_text:
                # Keep your recognised words visible alongside the reply — same
                # quoted style as Thinking — so you can confirm you were
                # understood correctly for as long as the reply is shown.
                item = c.create_text(WIDTH / 2, y, anchor="n", text=f"“{self._user_text}”",
                                     fill=MUTED, font=FONT_USER,
                                     width=WIDTH - 2 * PAD, justify="center")
                y = c.bbox(item)[3] + 10
            c.create_text(WIDTH / 2, y, anchor="n", text=self._assistant.upper(),
                          fill=ACCENT, font=FONT_LABEL)
            y += 24
            item = c.create_text(WIDTH / 2, y, anchor="n", text=self._response or "…",
                                 fill=_lerp_color(REVEAL_FROM, TEXT, self._reveal),
                                 font=FONT_BODY, width=WIDTH - 2 * PAD, justify="center")
            self._body_item = item
            y = c.bbox(item)[3]
        elif st == ERROR:
            c.create_text(WIDTH / 2, y, anchor="n", text="ERROR", fill=ERROR_COL, font=FONT_LABEL)
            y += 24
            item = c.create_text(WIDTH / 2, y, anchor="n", text=self._response,
                                 fill=_lerp_color(REVEAL_FROM, ERROR_COL, self._reveal),
                                 font=FONT_BODY, width=WIDTH - 2 * PAD, justify="center")
            self._body_item = item
            y = c.bbox(item)[3]

        h = int(y + PAD)
        card = c.create_polygon(_round_points(1, 1, WIDTH - 1, h - 1, 18),
                                smooth=True, fill=CARD, outline=BORDER, width=1)
        c.tag_lower(card)
        self._h = h

        # A visible ✕ to abort — a discoverable affordance for the click-anywhere-to-cancel
        # already wired to _on_click. Only in the states where cancelling means something.
        if st in (LISTENING, THINKING, RESPONSE):
            c.create_text(WIDTH - 15, 13, anchor="ne", text="✕",
                          fill=MUTED, font=("Segoe UI", 14))

    def _status_row(self, y, label, colour):
        c = self.canvas
        text = label + "." * (self._dots % 4)
        item = c.create_text(WIDTH / 2, y, anchor="n", text=text, fill=TITLE_COL, font=FONT_STATUS)
        x0, y0, _, y1 = c.bbox(item)
        cy = (y0 + y1) / 2
        c.create_oval(x0 - 26, cy - 6, x0 - 14, cy + 6, fill=colour, outline="")
        return y1

    def _pulse_colour(self):
        shades = ["#5b7c4a", "#6fae5f", "#81c995", "#a5d6a0"]
        return shades[self._pulse % len(shades)]

    def _level_bar(self, y):
        c = self.canvas
        x0, x1 = PAD + 6, WIDTH - PAD - 6
        h = 6
        c.create_rectangle(x0, y, x1, y + h, fill=REVEAL_FROM, outline="")  # track
        w = (x1 - x0) * max(0.0, min(1.0, self._level))
        self._level_item = c.create_rectangle(x0, y, x0 + max(0.001, w), y + h,
                                              fill=ACCENT, outline="")       # fill (updated live)
        self._level_geom = (x0, y, x1, h)
        return y + h

    # ---- geometry -----------------------------------------------------------

    def _compute_area(self):
        pref = getattr(self.config, "popup_monitor", "primary")
        if pref == "cursor":
            area = monitor_workarea_at(*self.win.winfo_pointerxy())
            if area:
                return area
        elif pref not in ("primary", "cursor"):
            mons = list_monitors()
            try:
                idx = int(pref)
                if 0 <= idx < len(mons):
                    return mons[idx]["work"]
            except (ValueError, TypeError):
                pass
        return primary_workarea()  # default + fallback: the main monitor

    def _rest_y(self):
        _, _, _, bottom = self._area
        return bottom - self._h - MARGIN

    def _x(self):
        left, _, right, _ = self._area
        return left + (right - left - WIDTH) // 2

    def _place(self, y):
        self.win.geometry(f"{WIDTH}x{self._h}+{int(self._x())}+{int(y)}")

    # ---- animation ----------------------------------------------------------

    def _hold_timer_res(self, ms):
        """Raise the Windows timer resolution to 1 ms for ~`ms`, then let it drop.

        Default granularity is ~15.6 ms, so a 60 fps `after(16)` tween actually
        fires at ~23–31 ms and stutters. Idempotent and self-releasing: repeated
        calls just extend the hold, and a single tracked job always lowers it
        again, so a mostly-idle tray app isn't pinning the system timer.
        """
        try:
            if not self._timer_raised:
                ctypes.windll.winmm.timeBeginPeriod(1)
                self._timer_raised = True
        except Exception:  # noqa: BLE001 - non-Windows / missing winmm: just skip
            pass
        if self._timer_job is not None:
            self.root.after_cancel(self._timer_job)
        self._timer_job = self.root.after(int(ms), self._drop_timer_res)

    def _drop_timer_res(self):
        self._timer_job = None
        try:
            if self._timer_raised:
                ctypes.windll.winmm.timeEndPeriod(1)
                self._timer_raised = False
        except Exception:  # noqa: BLE001
            pass

    def _tween(self, dur_ms, frame, done, ease):
        # Time-based: progress is read from the wall clock each frame, so a late
        # frame skips ahead to stay on schedule instead of dragging the whole
        # animation out (which is what read as "laggy"). Duration self-corrects.
        self._cancel_slide()
        self._hold_timer_res(dur_ms + 200)
        dur = max(1, dur_ms) / 1000.0
        start = time.monotonic()

        def run():
            p = (time.monotonic() - start) / dur
            last = p >= 1.0
            broken = False
            try:
                frame(ease(1.0 if last else p))
            except Exception:  # noqa: BLE001 - a frame error must still complete the transition
                broken = True
            if broken or last:
                self._slide_job = None
                if done:
                    try:
                        done()
                    except Exception:  # noqa: BLE001 - never leave _animating wedged
                        self._animating = False
                return
            self._slide_job = self.root.after(16, run)

        run()

    def _slide_in(self, done):
        rest = self._rest_y()

        def frame(p):
            self.win.attributes("-alpha", 0.97 * p)
            self._place(rest + SLIDE * (1 - p))

        self._tween(APPEAR_MS, frame, done, _ease_out)

    def _slide_out(self, done):
        rest = self._rest_y()
        start = self.win.attributes("-alpha")

        def frame(p):
            self.win.attributes("-alpha", start * (1 - p))
            self._place(rest + SLIDE * p)

        self._tween(VANISH_MS, frame, done, _ease_in)

    def _cancel_slide(self):
        if self._slide_job is not None:
            self.root.after_cancel(self._slide_job)
            self._slide_job = None

    # ---- pulse (Listening/Thinking dots) ------------------------------------

    def _start_pulse(self):
        if self._pulse_job is None:
            self._pulse_tick()

    def _pulse_tick(self):
        self._dots += 1
        self._pulse += 1
        try:
            if self._state in (LISTENING, THINKING) and self._shown and not self._animating:
                self._draw()
                self.win.attributes("-alpha", 0.97)
                self._place(self._rest_y())
        except Exception:  # noqa: BLE001 - pulse redraw must never wedge the loop
            pass
        if self._state in (LISTENING, THINKING):
            self._pulse_job = self.root.after(350, self._pulse_tick)
        else:
            self._pulse_job = None

    def _stop_pulse(self):
        if self._pulse_job is not None:
            self.root.after_cancel(self._pulse_job)
            self._pulse_job = None

    # ---- dismiss ------------------------------------------------------------

    def _schedule_dismiss(self):
        self._cancel_dismiss()
        ms = int(self.config.dismiss_seconds * 1000)
        self._dismiss_at = time.monotonic() + ms / 1000
        log.info("dismiss scheduled: hide() in %d ms (state=%s)", ms, self._state)
        self._dismiss_job = self.root.after(ms, self.hide)

    def _cancel_dismiss(self):
        if self._dismiss_job is not None:
            if self._dismiss_at is not None:
                remaining = self._dismiss_at - time.monotonic()
                if remaining > 0.05:  # >just the routine self-cleanup when hide() itself fires
                    log.info("dismiss interrupted %.2fs early by a new state (was state=%s)",
                             remaining, self._state)
            self._dismiss_at = None
            self.root.after_cancel(self._dismiss_job)
            self._dismiss_job = None
