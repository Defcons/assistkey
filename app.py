"""AssistKey: system-tray push-to-talk app for Home Assistant Assist.

Hold the hotkey to talk to your HA voice assistant. A tray icon shows the
connection state and a popup shows Listening, your words and the reply.

Threads:
  - main thread     : tkinter GUI (overlay + settings), drains a UI queue
  - asyncio thread  : persistent HA WebSocket + one utterance at a time
  - pynput listener : global hotkey (its own thread)
  - pystray icon    : tray menu (detached thread)
Cross-thread: keyboard -> asyncio via run_coroutine_threadsafe; asyncio/tray ->
GUI via a thread-safe queue polled with root.after.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import tkinter as tk
import pystray
from PIL import Image, ImageDraw
from pynput import keyboard as kb

import winsound

import config as cfg
import diag
from assist_client import AssistClient, AuthFailed
from overlay import Overlay
from wake import WakeListener

log = logging.getLogger("assistkey.app")

IDLE_COL = (154, 160, 166)         # connected, ready
ACTIVE_COL = (129, 201, 149)       # listening / working
DISCONNECTED_COL = (200, 110, 100)  # not connected to Home Assistant


def kill_previous_instances():
    """Kill any other running copy of the app before we start.

    Matches on the executable path (`assistkey\\.venv\\Scripts\\python*.exe`)
    rather than the command line, because a run.bat launch has a relative
    command line with no folder name in it.

    The venv python is a launcher that starts the real interpreter as a child,
    so one instance is two PIDs. Both are excluded so we don't kill ourselves.

    In the PyInstaller build the process is AssistKey.exe, so match that name
    instead; the venv path check would pick up unrelated python.exe processes.
    """
    mine = {os.getpid(), os.getppid()}
    keep = " -and ".join(f"$_.ProcessId -ne {p}" for p in mine)
    if getattr(sys, "frozen", False):
        name = Path(sys.executable).name  # AssistKey.exe
        ps = (
            f"Get-CimInstance Win32_Process -Filter \"Name='{name}'\" "
            f"| Where-Object {{ {keep} }} "
            "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        )
    else:
        venv = str(Path(sys.executable).resolve().parent.parent)  # ...\assistkey\.venv
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" "
            f"| Where-Object {{ $_.ExecutablePath -like '{venv}\\*' -and {keep} }} "
            "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        )
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001 - best-effort; never block startup
        pass


def make_icon(color) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # mic capsule
    d.rounded_rectangle([24, 12, 40, 40], radius=8, fill=color)
    # arc/stand
    d.arc([18, 20, 46, 46], start=20, end=160, fill=color, width=4)
    d.line([32, 46, 32, 54], fill=color, width=4)
    d.line([24, 54, 40, 54], fill=color, width=4)
    return img


class HotkeyListener:
    """Global hotkey with two modes, read live from config.trigger_mode:

    hold:   talk while the keys are held; releasing ends the utterance.
    toggle: one press starts, the next press ends.
    """

    def __init__(self, config: cfg.Config, on_down, on_up):
        self.config = config
        self.on_down = on_down
        self.on_up = on_up
        self._pressed: set[str] = set()
        self._latched = False   # current physical hold already acted on
        self._talking = False   # an utterance is currently open
        self._suspended = False  # ignore all keys (while Settings captures a new hotkey)
        self._listener = kb.Listener(on_press=self._press, on_release=self._release)

    def start(self):
        self._listener.start()

    def reset(self):
        self._pressed.clear()
        self._latched = False
        self._talking = False

    def suspend(self):
        """Ignore keys while Settings is capturing a new hotkey, so pressing the
        current one there doesn't start an utterance."""
        self._suspended = True
        self.reset()

    def resume(self):
        self._suspended = False
        self.reset()

    def mark_idle(self):
        """The utterance ended by itself (done/error), so reset the toggle state."""
        self._talking = False

    def _press(self, key):
        if self._suspended:
            return
        self._pressed.add(cfg.key_to_canon(key))
        target = self.config.hotkey_set
        if self._latched or not target or not (target <= self._pressed):
            return
        self._latched = True
        if self.config.trigger_mode == "toggle":
            if self._talking:
                self._talking = False
                self.on_up()
            else:
                self._talking = True
                self.on_down()
        else:  # hold
            self._talking = True
            self.on_down()

    def _release(self, key):
        if self._suspended:
            return
        canon = cfg.key_to_canon(key)
        self._pressed.discard(canon)
        if canon in self.config.hotkey_set:
            self._latched = False
            if self.config.trigger_mode != "toggle" and self._talking:
                self._talking = False
                self.on_up()


class App:
    def __init__(self):
        kill_previous_instances()  # only one copy may hold the hotkey and mic
        self.config = cfg.Config.load()
        diag.log_config(self.config)

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.report_callback_exception = self._log_exception
        self.overlay = Overlay(self.root, self.config)
        self.overlay.on_cancel = lambda: self.ui_queue.put(("cancel",))  # click popup to stop
        self.ui_queue: "queue.Queue" = queue.Queue()
        self._connected = False
        self._follow_up_next = False   # the next Listening is an auto follow-up
        self._quitting = False         # stops _drain rescheduling once the root is destroyed

        self.loop = asyncio.new_event_loop()
        self.client = AssistClient(self.config, ui=lambda cmd: self.ui_queue.put(cmd))
        self.client.loop = self.loop

        self.hotkey = HotkeyListener(self.config, on_down=self._hotkey_down,
                                     on_up=self._hotkey_up)
        self.wake = WakeListener(self.config, on_wake=self._on_wake)
        self.icon = pystray.Icon(
            "assistkey", make_icon(DISCONNECTED_COL), "AssistKey",
            menu=pystray.Menu(
                pystray.MenuItem("Settings…", lambda: self.ui_queue.put(("open_settings",)),
                                 default=True),  # clicking the tray icon opens Settings
                pystray.MenuItem("Stop", lambda: self.ui_queue.put(("cancel",))),
                pystray.MenuItem("Open log", lambda: self.ui_queue.put(("open_log",))),
                pystray.MenuItem("Report an issue…", lambda: self.ui_queue.put(("report_issue",))),
                pystray.MenuItem("Quit", lambda: self.ui_queue.put(("quit",))),
            ),
        )

    # ---- lifecycle ----------------------------------------------------------

    def run(self):
        threading.Thread(target=self._run_loop, daemon=True).start()
        self.hotkey.start()
        self.wake.start()  # idles until wake_enabled is set in Settings
        self.icon.run_detached()
        self.root.after(20, self._drain)
        if not self.config.is_configured():
            # First run: open Settings so the user can enter their HA details.
            self.root.after(400, lambda: self.ui_queue.put(("open_settings",)))
        self.root.mainloop()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.set_exception_handler(diag.asyncio_exception_handler)

        async def bootstrap():
            while True:
                if not self.config.is_configured():
                    self.ui_queue.put(("status", "Not configured — open Settings → Home Assistant"))
                    await asyncio.sleep(3)
                    continue
                try:
                    await self.client.connect()
                    await self.client.load_pipelines()
                    break
                except Exception as exc:  # noqa: BLE001 - retry until HA is reachable
                    if isinstance(exc, AuthFailed):
                        self.ui_queue.put(("status", "Authentication failed — create a new "
                                                     "token in Home Assistant and update it "
                                                     "in Settings"))
                    else:
                        self.ui_queue.put(("status", f"Connect failed: {exc}; retrying…"))
                    await asyncio.sleep(3)
            # pump() handles the errors it expects. If anything else escapes,
            # restart it rather than let this thread die (tray alive, hotkey dead).
            while True:
                try:
                    await self.client.pump()
                except Exception:  # noqa: BLE001 - the loop thread must never die
                    log.exception("pump crashed; recovering")
                    self.ui_queue.put(("disconnected",))
                    self.ui_queue.put(("status", "Connection error; recovering…"))
                    await asyncio.sleep(3)

        self.loop.run_until_complete(bootstrap())

    # ---- hotkey -> asyncio --------------------------------------------------

    def _hotkey_down(self):
        # Runs on the pynput thread. An exception here would stop the listener for
        # good and leave the hotkey dead, so catch everything.
        try:
            if self.config.wake_enabled:
                self.wake.pause()  # free the mic for the utterance
            # restart_utterance cancels any reply still playing, so one press
            # always starts listening. notify_unavailable shows "Reconnecting…"
            # instead of a raw error when we're not connected.
            asyncio.run_coroutine_threadsafe(
                self.client.restart_utterance(notify_unavailable=True), self.loop)
        except Exception:  # noqa: BLE001 - never let the hotkey listener die
            log.exception("hotkey_down failed")

    def _hotkey_up(self):
        try:
            self.loop.call_soon_threadsafe(self.client.signal_release)
        except Exception:  # noqa: BLE001 - never let the hotkey listener die
            log.exception("hotkey_up failed")

    def _on_wake(self):
        # Runs on the wake thread. There's no key release here: HA's voice
        # detection ends the utterance.
        self.wake.pause()
        try:
            winsound.Beep(760, 110)
        except Exception:  # noqa: BLE001
            pass
        # Like the hotkey: cancel any reply still playing and start listening.
        asyncio.run_coroutine_threadsafe(self.client.restart_utterance(), self.loop)

    # ---- UI queue drain (main thread) --------------------------------------

    def _drain(self):
        try:
            while True:
                cmd = self.ui_queue.get_nowait()
                try:
                    self._handle(cmd)
                except Exception:  # noqa: BLE001 - one bad command must not freeze the drain loop
                    log.exception("error handling UI command %r", cmd)
        except queue.Empty:
            pass
        if not self._quitting:
            self.root.after(20, self._drain)  # keep polling until quit

    def _set_idle_icon(self):
        """Tray icon at rest: grey when connected, red when not."""
        self.icon.icon = make_icon(IDLE_COL if self._connected else DISCONNECTED_COL)

    def _handle(self, cmd):
        name, *args = cmd
        if name == "assistant":
            self.overlay.set_assistant(args[0])
        elif name == "connected":
            self._connected = True
            if not self.client.is_active():
                self._set_idle_icon()
        elif name == "disconnected":
            self._connected = False
            if not self.client.is_active():
                self._set_idle_icon()
        elif name == "listening":
            self.icon.icon = make_icon(ACTIVE_COL)
            follow_up = self._follow_up_next
            self._follow_up_next = False
            self.overlay.listening(follow_up=follow_up)
        elif name == "thinking":
            self.overlay.thinking()
        elif name == "level":
            self.overlay.set_level(args[0])
        elif name == "user_text":
            self.overlay.set_user_text(args[0])
        elif name == "response_reset":
            self.overlay.response_reset()
        elif name == "response_append":
            self.overlay.response_append(args[0])
        elif name == "response_final":
            self.overlay.response_final(args[0])
        elif name == "cancel":
            self.client.request_cancel()
        elif name == "error":
            self._set_idle_icon()
            self.hotkey.mark_idle()
            self.wake.resume()  # resume wake-word listening (no-op if it wasn't paused)
            self.overlay.error(args[0])
        elif name == "done":
            if self.client.consume_follow_up():
                # HA wants an answer: keep the wake word paused and listen again
                # (HA's voice detection ends it), labelled as a follow-up.
                log.info("done -> follow-up, auto-listening again")
                self._follow_up_next = True
                asyncio.run_coroutine_threadsafe(self.client.start_utterance(), self.loop)
            else:
                log.info("done -> overlay.done() (starts the dismiss timer)")
                self._set_idle_icon()
                self.hotkey.mark_idle()
                self.wake.resume()
                self.overlay.done()
        elif name == "status":
            self.icon.title = f"AssistKey — {args[0]}"
        elif name == "open_settings":
            self.overlay.open_settings(self.client, on_save=self._on_settings_saved,
                                       suspend_hotkey=self.hotkey.suspend,
                                       resume_hotkey=self.hotkey.resume)
        elif name == "open_log":
            try:
                os.startfile(diag.LOG_PATH)  # noqa: S606 - open the log in the default viewer
            except Exception:  # noqa: BLE001
                log.exception("could not open log file")
        elif name == "report_issue":
            # Opens a prefilled GitHub issue in the browser for the user to review
            # and submit. Nothing is sent automatically.
            try:
                webbrowser.open(diag.build_issue_url(self.config))
                log.info("opened issue-report draft")
            except Exception:  # noqa: BLE001
                log.exception("could not open issue-report draft")
        elif name == "quit":
            self._quit()
        else:
            # Log it so a misspelled event name doesn't fail silently.
            log.warning("unknown ui command %r", cmd)

    def _on_settings_saved(self):
        self.hotkey.reset()
        self.icon.title = f"AssistKey — {cfg.hotkey_label(self.config.hotkey)} to talk"
        # Reconnect if the URL or token changed.
        asyncio.run_coroutine_threadsafe(self.client.force_reconnect(), self.loop)

    def _log_exception(self, exc, val, tb):
        log.error("Tk callback exception", exc_info=(exc, val, tb))

    def _quit(self):
        log.info("quit requested")
        self._quitting = True
        try:
            # Cancel first so a reply that's playing stops now. Otherwise exit
            # waits for the playback thread and the reply keeps talking after the
            # tray icon is gone.
            self.client.request_cancel()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.icon.stop()
        except Exception:  # noqa: BLE001
            pass
        self.root.quit()
        self.root.destroy()


if __name__ == "__main__":
    diag.setup()
    try:
        App().run()
    except Exception:  # noqa: BLE001 - last resort: make sure the crash is in the log
        log.exception("fatal error")
