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
from pathlib import Path

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
        self._purged_cache = False   # one self-heal purge per session, max

    # ---- lifecycle ----------------------------------------------------------

    def start(self):
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

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
        try:
            self._model = Model(wakeword_models=[word], inference_framework="onnx")
        except Exception:
            # A crash/power-loss during the FIRST download leaves a truncated
            # .onnx that passes download_models' exists-check forever — bricking
            # wake across sessions until someone deletes the file by hand
            # (openwakeword streams straight to the final filename, no
            # temp+rename). Self-heal: purge the cache once and re-download on
            # the next retry pass. A non-file load failure purges/refetches at
            # most once, then just keeps raising into _run's logged retry.
            if not self._purged_cache:
                self._purged_cache = True
                self._downloaded = False
                self._purge_model_cache()
            raise
        self._loaded_word = word

    @staticmethod
    def _purge_model_cache():
        try:
            import openwakeword
            models = Path(openwakeword.__file__).parent / "resources" / "models"
            for f in models.glob("*.onnx"):
                f.unlink(missing_ok=True)
            log.warning("purged the openwakeword model cache; re-downloading next pass")
        except Exception:  # noqa: BLE001 - best-effort; _run logs the load error anyway
            log.exception("could not purge the wake-model cache")

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
