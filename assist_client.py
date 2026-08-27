"""Async Home Assistant Assist pipeline client for push-to-talk.

Owns a persistent WebSocket connection to HA and runs one utterance at a time:
capture mic audio while the hotkey is held, stream it into assist_pipeline,
surface pipeline events to the UI via a callback, and play the TTS reply on the
configured output device.

UI-agnostic: it calls `self.ui((command, *args))`. The app translates those into
overlay updates. Commands emitted:
    ("listening",)                  mic open, waiting for speech
    ("thinking",)                   key released, STT/LLM running
    ("user_text", text)             final recognised speech
    ("response_reset",)             assistant reply is about to stream
    ("response_append", token)      a chunk of the assistant reply
    ("response_final", text)        full reply (fallback if nothing streamed)
    ("error", message)
    ("done",)                       reply finished (audio played) -> fade out
    ("status", text)                connection status (tray/debug)

`restart_utterance()` is the barge-in entry point: it cancels whatever's active
(suppressing ITS ("done",) so it can't race the new run's ("listening",)) before
starting fresh — see the class docstring on `request_cancel`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import time
import urllib.request

import miniaudio
import numpy as np
import sounddevice as sd
import websockets

log = logging.getLogger("assistkey.client")

SAMPLE_RATE = 16000
BLOCK = 1600  # 100 ms at 16 kHz
COMPLETION_TIMEOUT = 60   # s after release to get run-end before forcing the popup away
MAX_RECORD = 120          # s hard cap on recording (protects against a missed key-release)
CONVERSATION_TTL = 60     # s a conversation_id is reused so follow-ups keep context
TTS_FETCH_TIMEOUT = 10    # s cap on fetching a TTS clip (a barge-in also unblocks _play early)


def _ws_url(server: str) -> str:
    return server.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"


def _ws_is_open(ws) -> bool:
    """True only if the socket exists and is OPEN.

    During a reconnect `self.ws` is the previous, *closed* socket (not None), so a
    plain ``ws is None`` check would wave a doomed send through. `ClientConnection`
    exposes a ``state`` enum (CONNECTING/OPEN/CLOSING/CLOSED); compare by name to
    stay independent of the websockets version.
    """
    return getattr(getattr(ws, "state", None), "name", None) == "OPEN"


async def test_credentials(url: str, token: str) -> tuple[bool, str]:
    """Try to connect + authenticate. Returns (ok, human-readable message).

    Used by the Settings 'Test connection' button.
    """
    url = (url or "").strip()
    if not url:
        return False, "Enter a Home Assistant URL."
    if not (token or "").strip():
        return False, "Enter a long-lived access token."
    try:
        async with websockets.connect(_ws_url(url.rstrip("/")), open_timeout=8) as ws:
            await ws.recv()  # auth_required
            await ws.send(json.dumps({"type": "auth", "access_token": token.strip()}))
            reply = json.loads(await ws.recv())
            if reply.get("type") != "auth_ok":
                return False, "Authentication failed — check the token."
            return True, f"Connected — Home Assistant {reply.get('ha_version', '?')}"
    except Exception as exc:  # noqa: BLE001 - report any failure to the user
        return False, f"Could not connect: {exc}"


def list_input_devices() -> list[tuple[int, str]]:
    return _list_devices("max_input_channels")


def list_output_devices() -> list[tuple[int, str]]:
    return _list_devices("max_output_channels")


def _list_devices(cap: str) -> list[tuple[int, str]]:
    """One clean entry per real device.

    Windows exposes every device under multiple host APIs (MME, DirectSound,
    WASAPI, WDM-KS), which makes the raw list huge and full of duplicates. We
    prefer WASAPI (full names, one per device); if that's unavailable we fall
    back to the default device's host API, and only then to everything.
    """
    devices = sd.query_devices()

    def collect(api_idx):
        return [(i, d["name"]) for i, d in enumerate(devices)
                if d[cap] > 0 and d["hostapi"] == api_idx]

    for i, api in enumerate(sd.query_hostapis()):
        if "WASAPI" in api["name"]:
            got = collect(i)
            if got:
                return got

    ref = sd.default.device[0 if cap == "max_input_channels" else 1]
    try:
        got = collect(devices[ref]["hostapi"])
        if got:
            return got
    except (IndexError, TypeError):
        pass

    return [(i, d["name"]) for i, d in enumerate(devices) if d[cap] > 0]


class AssistClient:
    def __init__(self, config, ui):
        self.config = config
        self.ui = ui
        self.server = ""
        self.token = ""
        self.ws = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._next_id = 1
        self._routes: dict[int, asyncio.Queue] = {}
        self._active = False
        self._idle = asyncio.Event()
        self._idle.set()                        # nothing running yet
        self._release = asyncio.Event()
        self._cancel = asyncio.Event()          # barge-in: abort the current run
        self._suppress_next_done = False        # see restart_utterance
        self.pipelines: list[dict] = []
        self.preferred_pipeline: str | None = None
        self._conversation_id: str | None = None  # thread multi-turn context
        self._conv_deadline = 0.0                 # monotonic; forget the id after this
        self._follow_up_requested = False         # last reply asked to continue

    # ---- connection ---------------------------------------------------------

    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def force_reconnect(self):
        """Reconnect ONLY if the credentials changed. A plain settings save (popup
        timing, hotkey, wake word, …) must not drop a healthy connection — doing so
        made every Save strand the app for seconds while it reconnected."""
        url, token = self.config.credentials()
        url = url.rstrip("/")
        if _ws_is_open(self.ws) and url == self.server and token == self.token:
            return  # nothing connection-relevant changed; keep the live socket
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:  # noqa: BLE001
                pass

    def active_pipeline_name(self) -> str:
        """The name of the pipeline in use (whatever the user named it in HA)."""
        pid = self.config.pipeline or self.preferred_pipeline
        for p in self.pipelines:
            if p["id"] == pid:
                return p.get("name") or "Assistant"
        return "Assistant"

    async def connect(self):
        url, token = self.config.credentials()
        if not (url and token):
            raise RuntimeError("Home Assistant URL/token not configured")
        self.server = url.rstrip("/")
        self.token = token
        ws = await websockets.connect(_ws_url(self.server), max_size=None)
        try:
            hello = json.loads(await ws.recv())
            if hello.get("type") != "auth_required":
                raise RuntimeError(f"Unexpected handshake: {hello}")
            await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            reply = json.loads(await ws.recv())
            if reply.get("type") != "auth_ok":
                raise RuntimeError(f"Auth failed: {reply}")
        except BaseException:
            # Close the just-opened socket on ANY failure (auth error, cancel, bad
            # handshake) so a failed connect doesn't leak an open connection each
            # retry — and only publish self.ws once it's fully authenticated.
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
            raise
        self.ws = ws
        log.info("connected to Home Assistant %s at %s", reply.get("ha_version"), self.server)
        self.ui(("status", f"Connected (HA {reply.get('ha_version')})"))
        self.ui(("connected",))

    async def _request_once(self, payload: dict) -> dict:
        """Send a command and read the socket directly for its reply.

        Used during connect/reconnect, BEFORE pump() owns the socket, so it must
        not rely on the id-routing that pump provides.
        """
        msg_id = self.next_id()
        await self.ws.send(json.dumps({"id": msg_id, **payload}))
        async for raw in self.ws:
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            if msg.get("id") == msg_id:
                return msg
        raise RuntimeError("connection closed awaiting reply")

    async def load_pipelines(self):
        res = await self._request_once({"type": "assist_pipeline/pipeline/list"})
        if res.get("success"):
            self.pipelines = res["result"]["pipelines"]
            self.preferred_pipeline = res["result"].get("preferred_pipeline")

    async def pump(self):
        """Route incoming text frames to whichever call/utterance awaits that id.

        The `async for` ends on EITHER a clean close or an error close, and BOTH
        must reconnect. A clean close (HA closing with a normal 1000 code — on a
        restart, update, or idle timeout) does NOT raise: the websockets async
        iterator stops silently. So there is no exception to catch — the loop just
        falls through to the reconnect below. Missing this was a real bug: `while
        True` would immediately re-enter `async for` on the now-dead socket, which
        also returns instantly, giving a tight no-await loop that pegged the
        asyncio thread at 100% CPU. Because that spin holds the GIL, pynput's
        keyboard-hook callback fell behind and ALL Windows input lagged. Always
        reconnecting (which awaits) makes a spin impossible. See ResearchJournal
        2026-08-27.
        """
        while True:
            try:
                async for raw in self.ws:
                    if isinstance(raw, bytes):
                        continue
                    try:
                        msg = json.loads(raw)
                    except (ValueError, TypeError):
                        continue  # ignore an unparseable frame, don't kill the pump
                    q = self._routes.get(msg.get("id"))
                    if q is not None:
                        await q.put(msg)
                reason = "closed cleanly"   # async for ended with NO exception
            except (websockets.ConnectionClosed, OSError) as exc:
                reason = exc.__class__.__name__
            log.warning("connection %s; reconnecting", reason)
            self.ui(("disconnected",))
            self.ui(("status", f"Disconnected ({reason}); reconnecting…"))
            await self._reconnect()   # awaits (connect + backoff sleep) -> never spins

    async def _reconnect(self):
        delay = 1
        while True:
            try:
                await self.connect()          # try immediately — a healthy reconnect is instant
                await self.load_pipelines()
                return
            except Exception as exc:  # noqa: BLE001 - keep retrying with backoff
                log.warning("reconnect failed (%s); retrying in %ds", exc.__class__.__name__, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    # ---- utterance ----------------------------------------------------------

    def signal_release(self):
        self._release.set()

    def is_active(self) -> bool:
        return self._active

    def request_cancel(self, suppress_done: bool = False):
        """Barge-in: stop any TTS playback now and end the in-flight run. Any thread.

        suppress_done=True additionally skips the terminal ("done",) signal for
        THIS cancelled run — used by `restart_utterance`, where a new utterance is
        about to replace it immediately: letting the old run's ("done",) through
        would race the new run's ("listening",) and could reset hotkey/wake state
        mid-gesture (see KnowledgeBase). Plain callers (tray Stop, clicking the
        popup) don't pass this — there the normal done/dismiss is exactly right.
        """
        if suppress_done:
            self._suppress_next_done = True
        try:
            sd.stop()  # unblock _play's sd.wait() immediately
        except Exception:  # noqa: BLE001
            pass
        loop = self.loop
        if loop is not None:
            loop.call_soon_threadsafe(self._cancel.set)

    def _emit_done(self):
        """Fire the terminal ("done",) signal, unless it was suppressed for an
        internal restart hand-off — consumed once, so the NEXT genuine completion
        emits normally."""
        if self._suppress_next_done:
            self._suppress_next_done = False
            log.info("done suppressed (restart hand-off)")
        else:
            log.info("emitting done")
            self.ui(("done",))

    def consume_follow_up(self) -> bool:
        """True once if the last reply asked to continue AND follow-up is enabled."""
        want = self._follow_up_requested and self.config.follow_up_enabled
        self._follow_up_requested = False
        return want

    @staticmethod
    def _wants_follow_up(io: dict, reply_text: str) -> bool:
        """Follow-up is warranted if HA flags continue_conversation, OR the reply is
        itself a question (e.g. 'What did you mean?') — many agents ask a clarifying
        question without setting the flag."""
        flagged = bool(io.get("continue_conversation")
                       or io.get("response", {}).get("continue_conversation"))
        return flagged or reply_text.strip().endswith("?")

    async def start_utterance(self, notify_unavailable: bool = False):
        if self._active:
            return  # one already running; it will emit its own ("done",)
        if not _ws_is_open(self.ws):
            # Not connected yet, or mid-reconnect (a closed, non-None socket).
            # Decline gracefully. We must still emit a terminal signal: the caller
            # may have paused wake-word listening, and only ("done",)/("error",)
            # resumes it — otherwise wake stays paused until the app is restarted.
            if notify_unavailable:
                self.ui(("error", "Reconnecting to Home Assistant…"))
            else:
                self.ui(("done",))
            return
        self._active = True
        self._idle.clear()
        self._release = asyncio.Event()
        self._cancel = asyncio.Event()
        self._follow_up_requested = False
        try:
            await self._run_utterance()
        except Exception as exc:  # noqa: BLE001 - surface, don't crash the loop
            log.exception("utterance failed")
            self.ui(("error", str(exc)))
        finally:
            self._active = False
            self._idle.set()

    async def restart_utterance(self, notify_unavailable: bool = False):
        """Barge-in entry point: if a reply is currently active (including mid
        TTS playback — `is_active()` stays true for the whole utterance), cancel
        it WITHOUT letting its normal ("done",) fire, wait for it to actually wind
        down, then start fresh. This is what makes pressing the hotkey always land
        the user in Listening, instead of `start_utterance` silently no-op'ing
        because `_active` is still true.
        """
        if self._active:
            self.request_cancel(suppress_done=True)
            try:
                await asyncio.wait_for(self._idle.wait(), timeout=5)
            except asyncio.TimeoutError:
                # The old utterance did NOT wind down in time — almost always
                # because it's stuck in a slow TTS fetch (`_play`'s urlopen, which
                # sd.stop() can't interrupt). We are NOT going to replace it now, so
                # we must NOT swallow its terminal ("done",): that signal is the ONLY
                # thing that resumes wake-word listening and resets hotkey state. Leave
                # `_suppress_next_done` armed and the old run's done would be eaten →
                # wake stuck paused forever (see ResearchJournal 2026-08-27 audit).
                # Un-arm it and let the old run resolve normally (its queued __cancel__
                # ends it once the fetch returns); the barge-in becomes a plain cancel.
                log.warning("restart_utterance: previous utterance stuck (>5s, likely a slow "
                            "TTS fetch); letting it resolve normally instead of suppressing its done")
                self._suppress_next_done = False
                return
        await self.start_utterance(notify_unavailable=notify_unavailable)

    async def _run_utterance(self):
        audio_q: "queue.Queue[bytes]" = queue.Queue()
        stop_capture = asyncio.Event()
        finished = asyncio.Event()

        def on_audio(indata, frames, t, status):
            b = bytes(indata)
            audio_q.put(b)
            # Emit a mic level (0..1 peak) for the Listening meter. Cheap; ~10/s.
            arr = np.frombuffer(b, dtype=np.int16)
            if arr.size:
                self.ui(("level", float(np.abs(arr).max()) / 32768.0))

        try:
            stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=BLOCK,
                device=self.config.mic_device, callback=on_audio,
            )
            stream.start()
        except Exception as exc:  # noqa: BLE001 - bad device index, etc.
            log.error("mic open failed (device=%s): %s", self.config.mic_device, exc)
            self.ui(("error", f"Mic error: {exc}"))
            return

        self.ui(("assistant", self.active_pipeline_name()))
        self.ui(("listening",))

        msg_id = self.next_id()
        events: asyncio.Queue = asyncio.Queue()
        self._routes[msg_id] = events

        run_opts = {
            "id": msg_id,
            "type": "assist_pipeline/run",
            "start_stage": "stt",
            "end_stage": "tts",
            "input": {"sample_rate": SAMPLE_RATE},
        }
        if self.config.pipeline:
            run_opts["pipeline"] = self.config.pipeline
        if self._conversation_id and time.monotonic() < self._conv_deadline:
            run_opts["conversation_id"] = self._conversation_id  # continue the same chat
        await self.ws.send(json.dumps(run_opts))

        handler_id: int | None = None
        buffered: list[bytes] = []
        streamed_any = False
        reply_text = ""      # accumulated reply, to detect a trailing question

        async def watch_release():
            await self._release.wait()
            self.ui(("thinking",))  # key released -> Listening popup out, Thinking popup in
            stop_capture.set()

        async def forward_audio():
            nonlocal handler_id, buffered
            loop = asyncio.get_running_loop()
            try:
                while True:
                    try:
                        chunk = await loop.run_in_executor(None, audio_q.get, True, 0.1)
                    except queue.Empty:
                        if stop_capture.is_set() and audio_q.empty():
                            break
                        continue
                    if handler_id is None:
                        buffered.append(chunk)
                        continue
                    try:
                        for b in buffered:
                            await self.ws.send(bytes([handler_id]) + b)
                        buffered = []
                        await self.ws.send(bytes([handler_id]) + chunk)
                    except Exception:  # noqa: BLE001 - dropped connection: stop forwarding, don't crash
                        break
            finally:
                try:
                    stream.stop(); stream.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    if handler_id is not None:
                        await self.ws.send(bytes([handler_id]))  # empty frame = end of speech
                except Exception:  # noqa: BLE001
                    pass

        async def max_record():
            # Hard cap: if the key-release is somehow missed, end recording anyway.
            await asyncio.sleep(MAX_RECORD)
            if not stop_capture.is_set():
                self.ui(("thinking",))
                stop_capture.set()

        async def completion_watchdog():
            # After recording ends, the pipeline must finish within a bound;
            # otherwise force the utterance to resolve so the popup never hangs.
            await stop_capture.wait()
            try:
                await asyncio.wait_for(finished.wait(), timeout=COMPLETION_TIMEOUT)
            except asyncio.TimeoutError:
                await events.put({"type": "__timeout__"})

        async def watch_cancel():
            await self._cancel.wait()
            stop_capture.set()
            await events.put({"type": "__cancel__"})

        watcher = asyncio.ensure_future(watch_release())
        forwarder = asyncio.ensure_future(forward_audio())
        capper = asyncio.ensure_future(max_record())
        completer = asyncio.ensure_future(completion_watchdog())
        canceller = asyncio.ensure_future(watch_cancel())

        try:
            while True:
                msg = await events.get()
                if msg.get("type") == "__timeout__":
                    self.ui(("error", "No response — timed out"))
                    break
                if msg.get("type") == "__cancel__":
                    break  # barge-in: silent stop; finally emits ("done",)
                if msg.get("type") == "result":
                    if not msg.get("success"):
                        self.ui(("error", str(msg.get("error"))))
                        stop_capture.set()
                        break
                    continue
                ev = msg.get("event", {})
                etype = ev.get("type")
                data = ev.get("data", {})
                if isinstance(data, dict) and data.get("conversation_id"):
                    self._conversation_id = data["conversation_id"]
                    self._conv_deadline = time.monotonic() + CONVERSATION_TTL

                if etype == "run-start":
                    handler_id = data["runner_data"]["stt_binary_handler_id"]
                elif etype == "stt-end":
                    stop_capture.set()
                    self.ui(("thinking",))  # no-op if already thinking; safety if release missed
                    self.ui(("user_text", data["stt_output"]["text"].strip()))
                elif etype == "intent-start":
                    self.ui(("response_reset",))
                elif etype == "intent-progress":
                    delta = data.get("chat_log_delta", {}) or {}
                    piece = delta.get("content")
                    if piece:
                        streamed_any = True
                        reply_text += piece
                        self.ui(("response_append", piece))
                elif etype == "intent-end":
                    io = data.get("intent_output", {}) or {}
                    if not streamed_any:
                        speech = (io.get("response", {}).get("speech", {})
                                  .get("plain", {}).get("speech"))
                        if speech:
                            reply_text = speech
                            self.ui(("response_final", speech))
                    cid = io.get("conversation_id")
                    if cid:
                        self._conversation_id = cid
                        self._conv_deadline = time.monotonic() + CONVERSATION_TTL
                    self._follow_up_requested = self._wants_follow_up(io, reply_text)
                elif etype == "tts-end":
                    log.info("tts-end received; starting playback")
                    t_play_start = time.monotonic()
                    await self._play(data["tts_output"])
                    log.info("tts playback finished (%.2fs)", time.monotonic() - t_play_start)
                elif etype == "error":
                    self.ui(("error", data.get("message", "pipeline error")))
                    stop_capture.set()
                elif etype == "run-end":
                    log.info("run-end received")
                    break
        finally:
            finished.set()
            stop_capture.set()
            self._routes.pop(msg_id, None)
            try:
                await forwarder  # robust: never raises out (all sends guarded)
            except Exception:  # noqa: BLE001 - defensive; done must still fire
                pass
            for task in (watcher, capper, completer, canceller):
                task.cancel()
            self._emit_done()  # fires -> popup dismisses; suppressed once for a restart hand-off

    async def _play(self, tts_output: dict):
        url = tts_output["url"]
        full = url if url.startswith("http") else self.server + url
        cancel = self._cancel   # this utterance's barge-in event

        def fetch_decode_play():
            req = urllib.request.Request(full, headers={"Authorization": f"Bearer {self.token}"})
            with urllib.request.urlopen(req, timeout=TTS_FETCH_TIMEOUT) as resp:
                data = resp.read()
            if cancel.is_set():
                return  # barged in during the fetch — don't start playback at all
            decoded = miniaudio.decode(data, output_format=miniaudio.SampleFormat.SIGNED16)
            samples = np.frombuffer(decoded.samples, dtype=np.int16)
            if decoded.nchannels > 1:
                samples = samples.reshape(-1, decoded.nchannels)
            sd.play(samples, decoded.sample_rate, device=self.config.speaker_device)
            sd.wait()  # request_cancel()'s sd.stop() interrupts this

        # Race the fetch/playback against a barge-in. `sd.stop()` only unblocks the
        # PLAYBACK (`sd.wait`), not the `urlopen` fetch — so without this, a barge-in
        # during a slow fetch left the utterance loop stuck at `await self._play` for
        # up to the whole timeout (unresponsive barge-in + lingering popup). Now a
        # barge-in returns _play immediately; the executor finishes its soon-to-time-
        # out fetch in the background, sees `cancel.is_set()`, and skips playback — a
        # short-lived, harmless orphan.
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, fetch_decode_play)
        waiter = asyncio.ensure_future(cancel.wait())
        try:
            await asyncio.wait({fut, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if fut.done():
                await fut  # completed on its own — surface any fetch/decode/play error
            else:
                log.info("tts playback cancelled during fetch")
                fut.add_done_callback(self._retire_orphan_play)
        except Exception as exc:  # noqa: BLE001 - playback failure shouldn't abort the run
            log.warning("playback failed: %s", exc)
            self.ui(("status", f"Playback error: {exc}"))
        finally:
            waiter.cancel()

    @staticmethod
    def _retire_orphan_play(fut):
        # The utterance already moved on (barge-in during the fetch). Retrieve any
        # error so asyncio doesn't warn about an un-retrieved exception.
        if not fut.cancelled():
            exc = fut.exception()
            if exc:
                log.info("orphaned tts fetch ended: %s", exc.__class__.__name__)
