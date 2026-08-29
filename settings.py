"""The AssistKey settings dialog (customtkinter) + its private widgets.

Split out of overlay.py 2026-08-29: the popup overlay is stable, landmine-dense
animation code that should be touched rarely; this dialog grows with every new
setting. Keeping them apart means a settings change never opens the popup's
state machine. Main thread only, like all UI in this app.
"""

from __future__ import annotations

import asyncio
import threading
import tkinter as tk

import customtkinter as ctk
from pynput import keyboard as kb

import autostart
import config as cfg
from assist_client import list_input_devices, list_output_devices, test_credentials
from wake import WAKE_WORDS
from winscreen import apply_dark_titlebar, list_monitors, monitor_workarea_at

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

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

    def __init__(self, root, config: cfg.Config, client, on_save,
                 suspend_hotkey=None, resume_hotkey=None):
        self.config = config
        self.client = client
        self.on_save = on_save
        self.pending_hotkey = list(config.hotkey)
        self._capture_listener = None
        self._capturing = False
        self._suspend_hotkey = suspend_hotkey or (lambda: None)
        self._resume_hotkey = resume_hotkey or (lambda: None)

        win = ctk.CTkToplevel(root)
        self.win = win
        win.title("AssistKey Settings")
        win.configure(fg_color=S_BG)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self._cancel)
        apply_dark_titlebar(win)
        win.after(80, lambda: apply_dark_titlebar(win))  # re-apply once mapped

        # Save/Cancel bar pinned at the BOTTOM (packed first so it stays visible even
        # when the content is taller than the screen and the body scrolls).
        br = ctk.CTkFrame(win, fg_color=S_BG)
        br.pack(side="bottom", fill="x", padx=22, pady=(6, 14))
        ctk.CTkButton(br, text="Save", command=self._save, width=100, height=36, corner_radius=10,
                      fg_color=S_ACCENT, hover_color=S_ACCENT_HOVER, text_color="#0b1020",
                      font=("Segoe UI Semibold", 12)).pack(side="right")
        ctk.CTkButton(br, text="Cancel", command=self._cancel, width=100, height=36, corner_radius=10,
                      fg_color=S_FIELD, hover_color=S_HOVER, text_color=S_FG).pack(side="right", padx=(0, 8))

        # Scrollable content fills the space above the buttons — keeps the dialog usable
        # on small screens as it grows (it now exceeds a 1080p work area).
        body = ctk.CTkScrollableFrame(win, fg_color=S_BG)
        body.pack(side="top", fill="both", expand=True, padx=16, pady=(16, 0))

        ctk.CTkLabel(body, text="AssistKey", text_color=S_FG,
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
        gr = ctk.CTkFrame(body, fg_color="transparent")
        gr.pack(fill="x", pady=4)
        ctk.CTkLabel(gr, text="Mic boost", text_color=S_MUTED, width=self.LABEL_W, anchor="w",
                     font=("Segoe UI", 12)).pack(side="left")
        self.gain_var = tk.IntVar(value=int(round(config.mic_gain_db)))
        gain = ctk.CTkSlider(gr, from_=0, to=30, number_of_steps=30, variable=self.gain_var,
                             command=self._on_gain, progress_color=S_ACCENT, button_color=S_ACCENT,
                             button_hover_color=S_ACCENT)
        gain.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.gain_label = ctk.CTkLabel(gr, text=self._gain_text(config.mic_gain_db), text_color=S_FG,
                                       width=40, font=("Segoe UI", 12))
        self.gain_label.pack(side="left")
        _Tooltip(gain, "Amplify a quiet mic before sending to Home Assistant. Watch the level bar "
                       "in the Listening popup and set it so your normal voice fills the bar without "
                       "maxing out (clipping hurts recognition). 0 = off.")
        self.highpass_var = self._toggle_row(
            body, "Reduce hum", config.mic_highpass,
            tip="A gentle high-pass filter that trims low-frequency hum, rumble and DC offset before "
                "sending to Home Assistant. Safe for speech. (Real noise suppression is intentionally "
                "not offered — modern speech-to-text usually does WORSE on heavily denoised audio.)")
        self.spk_map = [("Default speaker", None)] + [(lbl, idx) for idx, lbl in list_output_devices()]
        self.spk_var = tk.StringVar()
        self._option(body, "Speaker", self.spk_map, config.speaker_device, self.spk_var,
                     tip="Which speaker plays the assistant's spoken reply, or your Windows default.")
        self.pipe_map = [("Preferred", None)] + [(p["name"], p["id"]) for p in getattr(client, "pipelines", [])]
        self.pipe_var = tk.StringVar()
        self._option(body, "Assistant", self.pipe_map, config.pipeline, self.pipe_var,
                     tip="Which Home Assistant voice pipeline (assistant) to use. "
                         "'Preferred' follows your Home Assistant default.")
        self.followup_var = self._toggle_row(
            body, "Follow-up", config.follow_up_enabled,
            tip="When the assistant asks a question, keep listening for your answer "
                "without pressing the key again (ended by Home Assistant's voice detection).")

        # --- Wake word ---
        self._section(body, "WAKE WORD")
        wr = ctk.CTkFrame(body, fg_color="transparent")
        wr.pack(fill="x", pady=4)
        wlbl = ctk.CTkLabel(wr, text="Enable", text_color=S_MUTED, width=self.LABEL_W, anchor="w",
                            font=("Segoe UI", 12))
        wlbl.pack(side="left")
        self.wake_var = tk.BooleanVar(value=config.wake_enabled)
        sw = ctk.CTkSwitch(wr, text="", variable=self.wake_var, onvalue=True, offvalue=False,
                           width=44, progress_color=S_ACCENT, button_color=S_FG, fg_color=S_HOVER)
        sw.pack(side="left")
        _wake_tip = ("Also listen for a spoken wake word (like 'Hey Jarvis') — no key needed. "
                     "Runs locally with openWakeWord; downloads a small model on first enable. "
                     "Home Assistant's own voice detection ends each command.")
        for _w in (wlbl, sw):
            _Tooltip(_w, _wake_tip)

        self.wakeword_map = [(label, name) for name, label in WAKE_WORDS]
        self.wakeword_var = tk.StringVar()
        self._option(body, "Wake word", self.wakeword_map, config.wake_word, self.wakeword_var,
                     tip="Which wake word to listen for.")

        sr = ctk.CTkFrame(body, fg_color="transparent")
        sr.pack(fill="x", pady=4)
        ctk.CTkLabel(sr, text="Sensitivity", text_color=S_MUTED, width=self.LABEL_W, anchor="w",
                     font=("Segoe UI", 12)).pack(side="left")
        self.sens_var = tk.DoubleVar(value=config.wake_sensitivity)
        sens = ctk.CTkSlider(sr, from_=0.2, to=0.9, number_of_steps=14, variable=self.sens_var,
                             command=self._on_sens, progress_color=S_ACCENT, button_color=S_ACCENT,
                             button_hover_color=S_ACCENT)
        sens.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.sens_label = ctk.CTkLabel(sr, text=f"{config.wake_sensitivity:.2f}", text_color=S_FG,
                                       width=32, font=("Segoe UI", 12))
        self.sens_label.pack(side="left")
        _Tooltip(sens, "Higher = stricter (fewer false triggers, but you must say it more clearly).")

        # --- Popup ---
        self._section(body, "POPUP")
        self.screen_map = [("Main monitor", "primary"), ("Follow mouse", "cursor")]
        for i, m in enumerate(list_monitors()):
            w = m["work"]
            label = f"Monitor {i + 1} ({w[2] - w[0]}×{w[3] - w[1]})" + (" — main" if m["primary"] else "")
            self.screen_map.append((label, str(i)))
        self.screen_var = tk.StringVar()
        self._option(body, "Screen", self.screen_map, config.popup_monitor, self.screen_var,
                     tip="Which monitor the popups appear on. 'Main monitor' is your primary "
                         "display; 'Follow mouse' uses whichever screen your cursor is on.")

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
        _Tooltip(slider, "How many seconds the popup stays after the reply finishes being spoken.")

        # --- Startup ---
        self._section(body, "STARTUP")
        self.autostart_var = self._toggle_row(
            body, "Start at login", autostart.is_enabled(),
            tip="Launch AssistKey automatically when you sign in to Windows "
                "(adds a per-user startup entry; no admin needed).")

        # Size to content, but CAP the height to the monitor work area — then the body
        # scrolls instead of the Save button falling off the bottom of the screen.
        win.update_idletasks()
        area = monitor_workarea_at(*win.winfo_pointerxy())
        if area:
            left, top, right, bottom = area
        else:
            left, top, right, bottom = 0, 0, win.winfo_screenwidth(), win.winfo_screenheight()
        try:
            box = body._parent_canvas.bbox("all")   # full content extent of the scroll frame
            content_h = (box[3] - box[1]) if box else 700
        except Exception:  # noqa: BLE001 - internals could change; fall back to a safe cap
            content_h = 700
        # Fixed width — a CTkScrollableFrame doesn't report its content's width, so
        # winfo_reqwidth() collapses and would clip the rows. This fits the content
        # (same rows as the old ~402 px dialog) plus the scrollbar.
        w = 416
        desired_h = content_h + br.winfo_reqheight() + 80    # + button bar + paddings + slack
        area_h = (bottom - top) - 48                         # never taller than the work area
        if desired_h <= area_h:
            h = desired_h
            try:                    # content fits — hide the idle (non-scrolling) scrollbar
                body._scrollbar.grid_remove()
            except Exception:       # noqa: BLE001 - internals could change; harmless if so
                pass
        else:
            h = area_h              # capped: the scrollbar stays and is actually used
        x = left + (right - left - w) // 2
        y = top + max(20, (bottom - top - h) // 3)
        win.geometry(f"{w}x{h}+{x}+{y}")

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

    def _toggle_row(self, parent, label, initial, tip=""):
        r = ctk.CTkFrame(parent, fg_color="transparent")
        r.pack(fill="x", pady=4)
        lbl = ctk.CTkLabel(r, text=label, text_color=S_MUTED, width=self.LABEL_W, anchor="w",
                           font=("Segoe UI", 12))
        lbl.pack(side="left")
        var = tk.BooleanVar(value=bool(initial))
        sw = ctk.CTkSwitch(r, text="", variable=var, onvalue=True, offvalue=False,
                           width=44, progress_color=S_ACCENT, button_color=S_FG, fg_color=S_HOVER)
        sw.pack(side="left")
        if tip:
            _Tooltip(lbl, tip)
            _Tooltip(sw, tip)
        return var

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

    def _on_sens(self, value):
        self.sens_label.configure(text=f"{float(value):.2f}")

    @staticmethod
    def _gain_text(db) -> str:
        db = int(round(float(db)))
        return "off" if db <= 0 else f"+{db} dB"

    def _on_gain(self, value):
        self.gain_label.configure(text=self._gain_text(value))

    def _test(self):
        url, token = self.url_var.get(), self.token_var.get()
        self.test_result.configure(text="Testing…", text_color=S_MUTED)

        def worker():
            try:
                ok, msg = asyncio.run(test_credentials(url, token))
            except Exception as exc:  # noqa: BLE001
                ok, msg = False, str(exc)
            # The dialog may have been closed (destroyed) while the network test ran;
            # marshalling back onto a dead window would raise on this worker thread.
            try:
                if self.win.winfo_exists():
                    self.win.after(0, lambda: self.test_result.configure(
                        text=msg, text_color=(S_OK if ok else S_ERR)))
            except (tk.TclError, RuntimeError):
                pass  # RuntimeError: Tk torn down entirely (app quit mid-test)

        threading.Thread(target=worker, daemon=True).start()

    _MODS = ("ctrl", "alt", "shift", "cmd")

    def _capture_hotkey(self):
        self.capture_btn.configure(text="Press keys — Esc cancels", state="disabled")
        self.hotkey_var.set("…")
        self._capturing = True
        self._suspend_hotkey()   # don't let the live global hotkey fire while capturing
        held: set[str] = set()
        seen: set[str] = set()   # every key touched this chord (survives partial releases)

        def on_press(key):
            canon = cfg.key_to_canon(key)
            if canon == "esc":
                self.win.after(0, self._cancel_capture)
                return False
            held.add(canon)
            seen.add(canon)
            if canon not in self._MODS:  # a real key -> commit immediately
                combo = list(seen)
                self.win.after(0, lambda: self._finish_capture(combo))
                return False
            return None

        def on_release(key):
            held.discard(cfg.key_to_canon(key))
            # All keys released with only modifiers pressed -> commit a modifier-only combo.
            if not held and seen and all(k in self._MODS for k in seen):
                combo = list(seen)
                self.win.after(0, lambda: self._finish_capture(combo))
                return False
            return None

        self._capture_listener = kb.Listener(on_press=on_press, on_release=on_release)
        self._capture_listener.start()

    def _end_capture(self):
        if self._capture_listener is not None:
            self._capture_listener.stop()
            self._capture_listener = None
        if self._capturing:
            self._capturing = False
            self._resume_hotkey()

    def _finish_capture(self, combo):
        if not self._capturing:
            return  # already handled (guards a double schedule from press+release)
        self._end_capture()
        self.pending_hotkey = combo
        self.hotkey_var.set(cfg.hotkey_label(combo))
        self.capture_btn.configure(text="Change…", state="normal")

    def _cancel_capture(self):
        if not self._capturing:
            return
        self._end_capture()
        self.hotkey_var.set(cfg.hotkey_label(self.pending_hotkey))  # restore the previous combo
        self.capture_btn.configure(text="Change…", state="normal")

    def _value_for(self, mapping, var):
        return next((val for lbl, val in mapping if lbl == var.get()), None)

    def _save(self):
        self.config.ha_url = self.url_var.get().strip()
        self.config.ha_token = self.token_var.get().strip()
        self.config.hotkey = self.pending_hotkey or ["f9"]
        self.config.trigger_mode = self._value_for(self.trigger_map, self.trigger_var) or "hold"
        self.config.mic_device = self._value_for(self.mic_map, self.mic_var)
        self.config.mic_gain_db = float(self.gain_var.get())
        self.config.mic_highpass = bool(self.highpass_var.get())
        self.config.speaker_device = self._value_for(self.spk_map, self.spk_var)
        self.config.pipeline = self._value_for(self.pipe_map, self.pipe_var)
        self.config.wake_enabled = bool(self.wake_var.get())
        self.config.wake_word = self._value_for(self.wakeword_map, self.wakeword_var) or "hey_jarvis"
        self.config.wake_sensitivity = round(float(self.sens_var.get()), 2)
        self.config.popup_monitor = self._value_for(self.screen_map, self.screen_var) or "primary"
        self.config.dismiss_seconds = float(max(2, int(self.dismiss_var.get())))
        self.config.follow_up_enabled = bool(self.followup_var.get())
        self.config.save()
        autostart.set_enabled(bool(self.autostart_var.get()))  # system setting, not in config.json
        self.on_save()
        self._close()

    def _cancel(self):
        self._close()

    def _close(self):
        self._end_capture()  # stop any capture listener + resume the global hotkey
        self.win.destroy()
