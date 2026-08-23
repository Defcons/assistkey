"""AssistClient: declining to start an utterance must still resync the app."""
import asyncio

import config as cfg
from assist_client import AssistClient


def test_start_utterance_when_disconnected_emits_done():
    # Regression: a wake trigger (or key-press) can arrive before HA is connected.
    # The app pauses wake-word listening around every utterance and only resumes
    # on ("done",)/("error",). If start_utterance returned silently, wake would
    # stay paused until restart — so it must emit a terminal ("done",).
    emitted = []
    client = AssistClient(cfg.Config(), ui=emitted.append)
    assert client.ws is None
    asyncio.run(client.start_utterance())
    assert ("done",) in emitted


def test_start_utterance_when_active_stays_silent():
    # If an utterance is already running, a second start must NOT emit — the
    # in-flight one owns the terminal ("done",).
    emitted = []
    client = AssistClient(cfg.Config(), ui=emitted.append)
    client._active = True
    client.ws = object()  # non-None so we'd proceed if the active-guard were wrong
    asyncio.run(client.start_utterance())
    assert emitted == []
