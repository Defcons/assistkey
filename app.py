"""AssistKey — system-tray push-to-talk app for Home Assistant Assist.

Hold the configured hotkey to talk to Jarvis. A tray icon shows state; an
always-on-top toast shows Listening / your words / the streaming reply.

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
from assist_client import AssistClient
from overlay import Overlay
from wake import WakeListener

log = logging.getLogger("assistkey.app")

IDLE_COL = (154, 160, 166)         # connected, ready
ACTIVE_COL = (129, 201, 149)       # listening / working
DISCONNECTED_COL = (200, 110, 100)  # not connected to Home Assistant


def kill_previous_instances():
    """Terminate any other instance of this app before we start.

    Matches by the process's EXECUTABLE PATH (always the full
    `assistkey\\.venv\\Scripts\\python*.exe`), NOT its command line — because a
    run.bat launch records a *relative* command line (`.venv\\Scripts\\python.exe
    -u app.py`) with no folder name in it, which a command-line match misses
    (that's how a stray duplicate survived and fought over the mic). The exe path
    is fully resolved regardless of how the process was started.

    The venv python is a launcher shim that spawns the real interpreter as a
    child, so ONE instance is two PIDs (self + parent shim). We exclude both so
    the killer never takes down its own process tree.

    Frozen (PyInstaller) build: the process IS `AssistKey.exe`, so match that name
    directly — the venv-python path heuristic would otherwise select unrelated
    `python.exe` processes under the exe's folder.
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
    """Global hotkey with two modes (read live from config.trigger_mode):

    hold   — talk while the combo is held; release ends the utterance.
    toggle — one full press starts; the next full press ends.
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
        """Stop reacting to keys — used while the Settings dialog is capturing a
        new hotkey, so pressing the *current* hotkey there can't start an utterance."""
        self._suspended = True
        self.reset()

    def resume(self):
        self._suspended = False
        self.reset()

    def mark_idle(self):
        """The utterance ended on its own (done/error) — resync toggle state."""
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
        kill_previous_instances()  # replace any running copy (no duplicate F9 listeners)
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
            # First run / no credentials — open Settings so the user can connect.
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
                    self.ui_queue.put(("status", f"Connect failed: {exc}; retrying…"))
                    await asyncio.sleep(3)
            await self.client.pump()

        self.loop.run_until_complete(bootstrap())

    # ---- hotkey -> asyncio --------------------------------------------------

    def _hotkey_down(self):
        if self.config.wake_enabled:
            self.wake.pause()  # free the mic for the utterance
        # restart_utterance cancels any in-flight reply first (even mid-TTS
        # playback) so the Listening popup always takes over — one press always
        # gets you talking, instead of the first press just stopping the old
        # reply and requiring a second press to actually start listening.
        # notify_unavailable: a deliberate key-press deserves feedback if we're
        # not connected yet (a gentle "Reconnecting…" instead of a raw error).
        asyncio.run_coroutine_threadsafe(
            self.client.restart_utterance(notify_unavailable=True), self.loop)

    def _hotkey_up(self):
        self.loop.call_soon_threadsafe(self.client.signal_release)

    def _on_wake(self):
        # Runs on the wake thread: pause listening, chime, run one utterance.
        # No key release — Home Assistant's VAD ends the utterance.
        self.wake.pause()
        try:
            winsound.Beep(760, 110)
        except Exception:  # noqa: BLE001
            pass
        # Same barge-in behaviour as the hotkey (see _hotkey_down): if a reply is
        # still active when the wake word fires, cancel it and start listening
        # right away instead of silently doing nothing.
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
        self.root.after(20, self._drain)  # ALWAYS reschedule — the UI pump must never die

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
                # HA asked to continue: keep wake paused, auto-listen (VAD-ended),
                # and mark the next Listening popup as a follow-up so it's clearly labelled.
                self._follow_up_next = True
                asyncio.run_coroutine_threadsafe(self.client.start_utterance(), self.loop)
            else:
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
            # Opens a PRE-FILLED GitHub issue draft in the user's browser for them to
            # review and submit themselves — nothing is sent automatically/silently.
            try:
                webbrowser.open(diag.build_issue_url(self.config))
                log.info("opened issue-report draft")
            except Exception:  # noqa: BLE001
                log.exception("could not open issue-report draft")
        elif name == "quit":
            self._quit()

    def _on_settings_saved(self):
        self.hotkey.reset()
        self.icon.title = f"AssistKey — {cfg.hotkey_label(self.config.hotkey)} to talk"
        # Apply any credential change: drop the socket so it reconnects with new creds
        # (also kicks the bootstrap loop if we were never connected).
        asyncio.run_coroutine_threadsafe(self.client.force_reconnect(), self.loop)

    def _log_exception(self, exc, val, tb):
        log.error("Tk callback exception", exc_info=(exc, val, tb))

    def _quit(self):
        log.info("quit requested")
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
