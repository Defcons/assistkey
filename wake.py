"""Wake-word listening for AssistKey (openWakeWord — the same engine HA uses).

A background thread continuously runs the mic through an openWakeWord model. On
detection it fires `on_wake`; the app then pauses this listener (so it doesn't
fight the utterance for the mic), runs one Assist utterance ended by Home
Assistant's own voice-activity detection, and resumes the listener afterwards.

Off by default — the user opts in from Settings.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np
import sounddevice as sd

log = logging.getLogger("assistkey.wake")

SAMPLE_RATE = 16000
FRAME = 1280  # 80 ms at 16 kHz — openWakeWord's expected chunk

# Wake words shipped with openWakeWord (model name -> friendly label).
WAKE_WORDS = [
    ("hey_jarvis", "Hey Jarvis"),
    ("alexa", "Alexa"),
    ("hey_mycroft", "Hey Mycroft"),
    ("hey_rhasspy", "Hey Rhasspy"),
]


class WakeListener:
    def __init__(self, config, on_wake):
        self.config = config
        self.on_wake = on_wake
        self._running = False
        self._paused = threading.Event()
        self._thread = None
        self._model = None
        self._loaded_word = None
        self._downloaded = False

    # ---- lifecycle ----------------------------------------------------------

    def start(self):
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def pause(self):
        self._paused.set()

    def resume(self):
        self._paused.clear()

    # ---- worker -------------------------------------------------------------

    def _run(self):
        while self._running:
            if not self.config.wake_enabled or self._paused.is_set():
                time.sleep(0.2)
                continue
            try:
                self._ensure_model()
                self._listen_once()
            except Exception:  # noqa: BLE001 - keep the thread alive, retry
                log.exception("wake listener error")
                time.sleep(1.5)

    def _ensure_model(self):
        word = self.config.wake_word
        if self._model is not None and self._loaded_word == word:
            return
        from openwakeword.model import Model
        from openwakeword.utils import download_models
        if not self._downloaded:
            # Mark done ONLY on success — else a transient first-enable download
            # failure would never be retried, and Model() below would then raise
            # forever (no model files), permanently disabling wake for the session.
            download_models()  # idempotent; fetches the ~10 MB model set once
            self._downloaded = True
        self._model = Model(wakeword_models=[word], inference_framework="onnx")
        self._loaded_word = word

    def _listen_once(self):
        """Open the mic and listen until a detection, a pause, or disable."""
        with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                               blocksize=FRAME, device=self.config.mic_device) as stream:
            self._model.reset()
            word = self._loaded_word
            while (self._running and self.config.wake_enabled
                   and not self._paused.is_set() and self._loaded_word == self.config.wake_word):
                data, _ = stream.read(FRAME)
                frame = np.frombuffer(bytes(data), dtype=np.int16)
                score = self._model.predict(frame).get(word, 0.0)
                if score >= self.config.wake_sensitivity:
                    self.on_wake()  # app pauses us + starts the utterance
                    return
