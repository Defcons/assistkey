"""Always-on-top toast overlay + settings dialog (tkinter).

The Overlay shows a sequence of popups centred at the bottom-middle of whichever
monitor the cursor is on: Listening… → Thinking… → the reply. Each popup slides
up from the bottom as it appears and slides back down as it leaves; states
replace one another (the old popup slides out, the new one slides in). All public
methods must be called on the Tk main thread (the app drains a queue there).
"""

from __future__ import annotations

import asyncio
import ctypes
import threading
import time
from ctypes import wintypes

import tkinter as tk

import customtkinter as ctk
from pynput import keyboard as kb

import config as cfg
from assist_client import list_input_devices, list_output_devices, test_credentials

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def apply_dark_titlebar(win):
    """Make a window's native title bar dark (Windows DWM) — no white bar."""
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        val = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (20 newer, 19 older builds)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:  # noqa: BLE001 - cosmetic only
        pass


def monitor_workarea_at(x: int, y: int):
    """Work-area rect (left, top, right, bottom) of the monitor under a point.

    Uses the Windows monitor under the given screen coords so the toast lands on
    whichever display the user is on. Returns None if the API call fails.
    """
    try:
        user32 = ctypes.windll.user32
        hmon = user32.MonitorFromPoint(wintypes.POINT(x, y), 2)  # NEAREST
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return None
        r = mi.rcWork
        return r.left, r.top, r.right, r.bottom
    except Exception:  # noqa: BLE001 - fall back to primary screen
        return None


MAGIC = "#ff00ff"          # transparent knock-out colour for rounded corners
CARD = "#1e2230"
BORDER = "#333a4d"
TITLE_COL = "#e8eaed"
ACCENT = "#8ab4f8"
MUTED = "#9aa0a6"
TEXT = "#f1f3f4"
ERROR_COL = "#f28b82"

WIDTH = 620
PAD = 22
MARGIN = 44                # gap above the taskbar at rest
SLIDE = 46                 # px the popup travels while sliding in/out
APPEAR_MS = 200
VANISH_MS = 150

FONT_LABEL = ("Segoe UI Semibold", 10)
FONT_STATUS = ("Segoe UI", 15)
FONT_USER = ("Segoe UI", 12, "italic")
FONT_BODY = ("Segoe UI", 13)

HIDDEN, LISTENING, THINKING, RESPONSE, ERROR = "hidden", "listening", "thinking", "response", "error"

# settings-dialog palette (matches the popup look)
S_BG = "#1a1e2a"
S_FIELD = "#2b3142"
S_FG = "#e8eaed"
S_MUTED = "#9aa0a6"
S_ACCENT = "#8ab4f8"
S_ACCENT_HOVER = "#a0c4fb"
S_HOVER = "#343b50"
S_OK = "#81c995"
S_ERR = "#f28b82"


def _ease_out(t):
    return 1 - (1 - t) ** 3


def _ease_in(t):
    return t ** 3


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

        self._state = HIDDEN
        self._target = HIDDEN
        self._user_text = ""
        self._response = ""
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
        self._hardhide_job = None
        self._last_change = time.monotonic()
        self._watchdog()  # periodic safety net against a stuck popup

    def _touch(self):
        self._last_change = time.monotonic()

    # ---- public API (main thread) ------------------------------------------

    def listening(self):
        self._cancel_dismiss()
        self._user_text = ""
        self._response = ""
        self._touch()
        self._enter(LISTENING)

    def thinking(self):
        self._touch()
        self._enter(THINKING)

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
        self._cancel_dismiss()
        self._response = message
        self._enter(ERROR)

    def done(self):
        self._schedule_dismiss()

    def hide(self):
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
                if time.monotonic() - self._last_change > limit:
                    self._hard_hide()
        except Exception:  # noqa: BLE001 - a watchdog must never throw
            pass
        self.root.after(3000, self._watchdog)

    def open_settings(self, client, on_save):
        SettingsDialog(self.root, self.config, client, on_save)

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
        if self._state == ERROR:
            self._schedule_dismiss()
        elif self._dirty:
            self._dirty = False
            self._refresh()

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
        y = PAD
        st = self._state

        if st == LISTENING:
            y = self._status_row(y, "Listening", self._pulse_colour())
        elif st == THINKING:
            y = self._status_row(y, "Thinking", ACCENT)
            if self._user_text:
                item = c.create_text(WIDTH / 2, y + 8, anchor="n",
                                     text=f"“{self._user_text}”", fill=MUTED,
                                     font=FONT_USER, width=WIDTH - 2 * PAD, justify="center")
                y = c.bbox(item)[3]
        elif st == RESPONSE:
            c.create_text(WIDTH / 2, y, anchor="n", text="JARVIS", fill=ACCENT, font=FONT_LABEL)
            y += 22
            item = c.create_text(WIDTH / 2, y, anchor="n", text=self._response or "…",
                                 fill=TEXT, font=FONT_BODY, width=WIDTH - 2 * PAD, justify="center")
            y = c.bbox(item)[3]
        elif st == ERROR:
            c.create_text(WIDTH / 2, y, anchor="n", text="ERROR", fill=ERROR_COL, font=FONT_LABEL)
            y += 22
            item = c.create_text(WIDTH / 2, y, anchor="n", text=self._response, fill=ERROR_COL,
                                 font=FONT_BODY, width=WIDTH - 2 * PAD, justify="center")
            y = c.bbox(item)[3]

        h = int(y + PAD)
        card = c.create_polygon(_round_points(1, 1, WIDTH - 1, h - 1, 18),
                                smooth=True, fill=CARD, outline=BORDER, width=1)
        c.tag_lower(card)
        self._h = h

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

    # ---- geometry -----------------------------------------------------------

    def _compute_area(self):
        px, py = self.win.winfo_pointerxy()
        area = monitor_workarea_at(px, py)
        if area:
            return area
        return (0, 0, self.win.winfo_screenwidth(), self.win.winfo_screenheight() - 48)

    def _rest_y(self):
        _, _, _, bottom = self._area
        return bottom - self._h - MARGIN

    def _x(self):
        left, _, right, _ = self._area
        return left + (right - left - WIDTH) // 2

    def _place(self, y):
        self.win.geometry(f"{WIDTH}x{self._h}+{int(self._x())}+{int(y)}")

    # ---- animation ----------------------------------------------------------

    def _tween(self, dur_ms, frame, done, ease):
        self._cancel_slide()
        steps = max(1, int(dur_ms / 16))

        def run(i):
            broken = False
            try:
                frame(ease(i / steps))
            except Exception:  # noqa: BLE001 - a frame error must still complete the transition
                broken = True
            if broken or i >= steps:
                self._slide_job = None
                if done:
                    try:
                        done()
                    except Exception:  # noqa: BLE001 - never leave _animating wedged
                        self._animating = False
                return
            self._slide_job = self.root.after(16, lambda: run(i + 1))

        run(0)

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
        self._dismiss_job = self.root.after(ms, self.hide)

    def _cancel_dismiss(self):
        if self._dismiss_job is not None:
            self.root.after_cancel(self._dismiss_job)
            self._dismiss_job = None


class _Tooltip:
    """Small dark hover tooltip for a widget."""

    def __init__(self, widget, text, delay=400):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.tip = None
        self._after = None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<ButtonPress>", self._leave, add="+")

    def _enter(self, _e=None):
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _leave(self, _e=None):
        self._cancel()
        self._destroy()

    def _cancel(self):
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _show(self):
        if self.tip is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 14
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except tk.TclError:
            return
        self.tip = tk.Toplevel(self.widget)
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        try:
            self.tip.attributes("-alpha", 0.97)
        except tk.TclError:
            pass
        tk.Label(self.tip, text=self.text, bg="#0e1220", fg=S_FG, font=("Segoe UI", 9),
                 justify="left", wraplength=300, padx=10, pady=7, bd=0,
                 highlightthickness=1, highlightbackground=S_HOVER).pack()
        self.tip.geometry(f"+{x}+{y}")

    def _destroy(self):
        if self.tip is not None:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
            self.tip = None


class _Dropdown(ctk.CTkFrame):
    """A dark, rounded dropdown that matches the field width (native menus force a
    white OS border and can't be aligned/left-justified, so we roll our own)."""

    def __init__(self, master, values, variable):
        super().__init__(master, fg_color=S_FIELD, corner_radius=10, height=34)
        self.pack_propagate(False)
        self._values = list(values)
        self._var = variable
        self._menu = None
        self._label = ctk.CTkLabel(self, textvariable=variable, anchor="w",
                                   text_color=S_FG, font=("Segoe UI", 12))
        self._label.pack(side="left", fill="x", expand=True, padx=(12, 0))
        self._arrow = ctk.CTkLabel(self, text="▾", text_color=S_MUTED, width=18, font=("Segoe UI", 12))
        self._arrow.pack(side="right", padx=(0, 10))
        for w in (self, self._label, self._arrow):
            w.bind("<Button-1>", self._toggle)

    def _toggle(self, _e=None):
        self._close() if self._menu is not None else self._open()

    def _open(self):
        self.update_idletasks()
        fx, fy, fw, fh = (self.winfo_rootx(), self.winfo_rooty(),
                          self.winfo_width(), self.winfo_height())
        self._menu = tk.Toplevel(self)
        self._menu.overrideredirect(True)
        self._menu.attributes("-topmost", True)
        self._menu.configure(bg=S_BG)
        frame = ctk.CTkFrame(self._menu, fg_color=S_FIELD, corner_radius=10,
                             border_width=1, border_color=S_HOVER)
        frame.pack(fill="both", expand=True)
        for val in self._values:
            ctk.CTkButton(frame, text=val, anchor="w", fg_color="transparent",
                          hover_color=S_HOVER, text_color=S_FG, corner_radius=6, height=30,
                          font=("Segoe UI", 12), command=lambda v=val: self._select(v)).pack(
                          fill="x", padx=4, pady=2)
        self._menu.update_idletasks()
        mh = self._menu.winfo_reqheight()
        self._menu.geometry(f"{fw}x{mh}+{fx}+{fy + fh + 3}")  # match field width, sit under it
        self._arrow.configure(text="▴")
        self._menu.grab_set()  # route outside clicks here so we can dismiss
        self._menu.bind("<Button-1>", self._click_out, add="+")

    def _click_out(self, e):
        m = self._menu
        if m is None:
            return
        inside = (m.winfo_rootx() <= e.x_root <= m.winfo_rootx() + m.winfo_width()
                  and m.winfo_rooty() <= e.y_root <= m.winfo_rooty() + m.winfo_height())
        if not inside:
            self._close()

    def _select(self, v):
        self._var.set(v)
        self._close()

    def _close(self):
        if self._menu is not None:
            try:
                self._menu.grab_release()
                self._menu.destroy()
            except tk.TclError:
                pass
            self._menu = None
        try:
            self._arrow.configure(text="▾")
        except tk.TclError:
            pass


class SettingsDialog:
    """Modern rounded settings dialog (customtkinter) with a dark title bar."""

    LABEL_W = 108

    def __init__(self, root, config: cfg.Config, client, on_save):
        self.config = config
        self.client = client
        self.on_save = on_save
        self.pending_hotkey = list(config.hotkey)
        self._capture_listener = None

        win = ctk.CTkToplevel(root)
        self.win = win
        win.title("Voice PTT Settings")
        win.configure(fg_color=S_BG)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self._cancel)
        apply_dark_titlebar(win)
        win.after(80, lambda: apply_dark_titlebar(win))  # re-apply once mapped

        body = ctk.CTkFrame(win, fg_color=S_BG)
        body.pack(fill="both", expand=True, padx=22, pady=16)

        ctk.CTkLabel(body, text="Voice PTT", text_color=S_FG,
                     font=("Segoe UI Semibold", 16)).pack(anchor="w")
        ctk.CTkLabel(body, text="Talk to Home Assistant from your PC", text_color=S_MUTED,
                     font=("Segoe UI", 11)).pack(anchor="w", pady=(0, 4))

        # --- Home Assistant ---
        self._section(body, "HOME ASSISTANT")
        _url, _token = config.credentials()  # show effective values (incl. env-var fallback)
        self.url_var = tk.StringVar(value=_url)
        self._entry(body, "Server URL", self.url_var, placeholder="https://homeassistant.local:8123",
                    tip="Your Home Assistant address, including https:// and port — e.g.\n"
                        "https://homeassistant.local:8123 or your external URL.\n"
                        "Must be https: browsers/Windows block microphone capture over plain http.")
        self.token_var = tk.StringVar(value=_token)
        self._entry(body, "Access token", self.token_var, mask=True, placeholder="Long-lived access token",
                    tip="A Long-Lived Access Token — it lets this app connect to your Home Assistant.\n"
                        "Get one in HA: click your profile (bottom-left) → scroll to "
                        "'Long-lived access tokens' → Create Token → copy it here.")
        ctk.CTkLabel(body, text="Home Assistant → your Profile → Long-lived access tokens",
                     text_color=S_MUTED, font=("Segoe UI", 10), wraplength=440,
                     justify="left").pack(anchor="w", pady=(1, 4))
        trow = ctk.CTkFrame(body, fg_color="transparent")
        trow.pack(fill="x", pady=(2, 2))
        test_btn = ctk.CTkButton(trow, text="Test connection", command=self._test, width=130, height=30,
                                 corner_radius=10, fg_color=S_FIELD, hover_color=S_HOVER, text_color=S_FG)
        test_btn.pack(side="left")
        _Tooltip(test_btn, "Check that this URL and token can reach Home Assistant.")
        self.test_result = ctk.CTkLabel(trow, text="", text_color=S_MUTED, font=("Segoe UI", 10),
                                        wraplength=290, justify="left")
        self.test_result.pack(side="left", padx=(12, 0))

        # --- Voice ---
        self._section(body, "VOICE")
        hk = ctk.CTkFrame(body, fg_color="transparent")
        hk.pack(fill="x", pady=4)
        hk_label = ctk.CTkLabel(hk, text="Hotkey", text_color=S_MUTED, width=self.LABEL_W, anchor="w",
                                font=("Segoe UI", 12))
        hk_label.pack(side="left")
        self.hotkey_var = tk.StringVar(value=cfg.hotkey_label(self.pending_hotkey))
        hk_box = ctk.CTkLabel(hk, textvariable=self.hotkey_var, fg_color=S_FIELD, corner_radius=10,
                              text_color=S_FG, anchor="w", height=32, font=("Segoe UI", 12))
        hk_box.pack(side="left", fill="x", expand=True, ipadx=10)
        self.capture_btn = ctk.CTkButton(hk, text="Change…", command=self._capture_hotkey,
                                         width=90, height=32, corner_radius=10, fg_color=S_FIELD,
                                         hover_color=S_HOVER, text_color=S_FG)
        self.capture_btn.pack(side="left", padx=(8, 0))
        _hk_tip = ("The key you hold (or tap) to talk. Click Change…, then press the key or "
                   "combination you want (e.g. F9, or Ctrl+Space).")
        for _w in (hk_label, hk_box, self.capture_btn):
            _Tooltip(_w, _hk_tip)

        self.trigger_map = [("Hold to talk", "hold"), ("Tap to toggle", "toggle")]
        self.trigger_var = tk.StringVar()
        self._option(body, "Trigger", self.trigger_map, config.trigger_mode, self.trigger_var,
                     tip="How the hotkey works:\n• Hold to talk — mic is open only while you hold it.\n"
                         "• Tap to toggle — tap once to start, tap again to stop.")
        self.mic_map = [("Default microphone", None)] + [(lbl, idx) for idx, lbl in list_input_devices()]
        self.mic_var = tk.StringVar()
        self._option(body, "Microphone", self.mic_map, config.mic_device, self.mic_var,
                     tip="Which microphone to record your voice from, or your Windows default.")
        self.spk_map = [("Default speaker", None)] + [(lbl, idx) for idx, lbl in list_output_devices()]
        self.spk_var = tk.StringVar()
        self._option(body, "Speaker", self.spk_map, config.speaker_device, self.spk_var,
                     tip="Which speaker plays the assistant's spoken reply, or your Windows default.")
        self.pipe_map = [("Preferred", None)] + [(p["name"], p["id"]) for p in getattr(client, "pipelines", [])]
        self.pipe_var = tk.StringVar()
        self._option(body, "Assistant", self.pipe_map, config.pipeline, self.pipe_var,
                     tip="Which Home Assistant voice pipeline (assistant) to use. "
                         "'Preferred' follows your Home Assistant default.")

        # --- Popup ---
        self._section(body, "POPUP")
        dr = ctk.CTkFrame(body, fg_color="transparent")
        dr.pack(fill="x", pady=4)
        ctk.CTkLabel(dr, text="Dismiss after", text_color=S_MUTED, width=self.LABEL_W, anchor="w",
                     font=("Segoe UI", 12)).pack(side="left")
        self.dismiss_var = tk.IntVar(value=int(config.dismiss_seconds))
        slider = ctk.CTkSlider(dr, from_=2, to=20, number_of_steps=18, variable=self.dismiss_var,
                               command=self._on_dismiss, progress_color=S_ACCENT, button_color=S_ACCENT,
                               button_hover_color=S_ACCENT)
        slider.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.dismiss_label = ctk.CTkLabel(dr, text=f"{int(config.dismiss_seconds)}s",
                                          text_color=S_FG, width=28, font=("Segoe UI", 12))
        self.dismiss_label.pack(side="left")
        _Tooltip(slider, "How many seconds a reply popup stays on screen before it slides away.")

        # --- Buttons ---
        br = ctk.CTkFrame(body, fg_color="transparent")
        br.pack(fill="x", pady=(18, 0))
        ctk.CTkButton(br, text="Save", command=self._save, width=100, height=36, corner_radius=10,
                      fg_color=S_ACCENT, hover_color=S_ACCENT_HOVER, text_color="#0b1020",
                      font=("Segoe UI Semibold", 12)).pack(side="right")
        ctk.CTkButton(br, text="Cancel", command=self._cancel, width=100, height=36, corner_radius=10,
                      fg_color=S_FIELD, hover_color=S_HOVER, text_color=S_FG).pack(side="right", padx=(0, 8))

        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        area = monitor_workarea_at(*win.winfo_pointerxy())  # centre on the active monitor
        if area:
            left, top, right, bottom = area
            x = left + (right - left - w) // 2
            y = top + max(40, (bottom - top - h) // 3)
        else:
            x = (win.winfo_screenwidth() - w) // 2
            y = max(40, (win.winfo_screenheight() - h) // 3)
        win.geometry(f"+{x}+{y}")

    # ---- widgets ------------------------------------------------------------

    def _section(self, parent, text):
        ctk.CTkLabel(parent, text=text, text_color=S_ACCENT,
                     font=("Segoe UI Semibold", 10)).pack(anchor="w", pady=(12, 2))

    def _entry(self, parent, label, var, mask=False, placeholder="", tip=""):
        r = ctk.CTkFrame(parent, fg_color="transparent")
        r.pack(fill="x", pady=4)
        lbl = ctk.CTkLabel(r, text=label, text_color=S_MUTED, width=self.LABEL_W, anchor="w",
                           font=("Segoe UI", 12))
        lbl.pack(side="left")
        ent = ctk.CTkEntry(r, textvariable=var, corner_radius=10, fg_color=S_FIELD, border_width=0,
                           height=34, font=("Segoe UI", 12), placeholder_text=placeholder,
                           show=("•" if mask else ""))
        ent.pack(side="left", fill="x", expand=True)
        if tip:
            _Tooltip(lbl, tip)
            _Tooltip(ent, tip)

    def _option(self, parent, label, mapping, current, var, tip=""):
        r = ctk.CTkFrame(parent, fg_color="transparent")
        r.pack(fill="x", pady=4)
        lbl = ctk.CTkLabel(r, text=label, text_color=S_MUTED, width=self.LABEL_W, anchor="w",
                           font=("Segoe UI", 12))
        lbl.pack(side="left")
        values = [lbl_ for lbl_, _ in mapping]
        var.set(next((lbl_ for lbl_, val in mapping if val == current), values[0] if values else ""))
        dd = _Dropdown(r, values, var)
        dd.pack(side="left", fill="x", expand=True)
        if tip:
            _Tooltip(lbl, tip)

    # ---- actions ------------------------------------------------------------

    def _on_dismiss(self, value):
        self.dismiss_label.configure(text=f"{int(float(value))}s")

    def _test(self):
        url, token = self.url_var.get(), self.token_var.get()
        self.test_result.configure(text="Testing…", text_color=S_MUTED)

        def worker():
            try:
                ok, msg = asyncio.run(test_credentials(url, token))
            except Exception as exc:  # noqa: BLE001
                ok, msg = False, str(exc)
            self.win.after(0, lambda: self.test_result.configure(
                text=msg, text_color=(S_OK if ok else S_ERR)))

        threading.Thread(target=worker, daemon=True).start()

    def _capture_hotkey(self):
        self.capture_btn.configure(text="Press keys…", state="disabled")
        self.hotkey_var.set("…")
        pressed: set[str] = set()

        def on_press(key):
            canon = cfg.key_to_canon(key)
            pressed.add(canon)
            if canon not in ("ctrl", "alt", "shift", "cmd"):
                combo = list(pressed)
                self.win.after(0, lambda: self._finish_capture(combo))
                return False
            return None

        self._capture_listener = kb.Listener(on_press=on_press)
        self._capture_listener.start()

    def _finish_capture(self, combo):
        if self._capture_listener is not None:
            self._capture_listener.stop()
            self._capture_listener = None
        self.pending_hotkey = combo
        self.hotkey_var.set(cfg.hotkey_label(combo))
        self.capture_btn.configure(text="Change…", state="normal")

    def _value_for(self, mapping, var):
        return next((val for lbl, val in mapping if lbl == var.get()), None)

    def _save(self):
        self.config.ha_url = self.url_var.get().strip()
        self.config.ha_token = self.token_var.get().strip()
        self.config.hotkey = self.pending_hotkey or ["f9"]
        self.config.trigger_mode = self._value_for(self.trigger_map, self.trigger_var) or "hold"
        self.config.mic_device = self._value_for(self.mic_map, self.mic_var)
        self.config.speaker_device = self._value_for(self.spk_map, self.spk_var)
        self.config.pipeline = self._value_for(self.pipe_map, self.pipe_var)
        self.config.dismiss_seconds = float(max(2, int(self.dismiss_var.get())))
        self.config.save()
        self.on_save()
        self._close()

    def _cancel(self):
        self._close()

    def _close(self):
        if self._capture_listener is not None:
            self._capture_listener.stop()
            self._capture_listener = None
        self.win.destroy()
