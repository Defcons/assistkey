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
"""

from __future__ import annotations

import asyncio
import json
import queue
import urllib.request

import miniaudio
import numpy as np
import sounddevice as sd
import websockets

SAMPLE_RATE = 16000
BLOCK = 1600  # 100 ms at 16 kHz
COMPLETION_TIMEOUT = 60   # s after release to get run-end before forcing the popup away
MAX_RECORD = 120          # s hard cap on recording (protects against a missed key-release)


def _ws_url(server: str) -> str:
    return server.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"


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
        self._release = asyncio.Event()
        self.pipelines: list[dict] = []
        self.preferred_pipeline: str | None = None

    # ---- connection ---------------------------------------------------------

    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def force_reconnect(self):
        """Drop the socket so pump() reconnects, re-reading credentials/config."""
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def connect(self):
        url, token = self.config.credentials()
        if not (url and token):
            raise RuntimeError("Home Assistant URL/token not configured")
        self.server = url.rstrip("/")
        self.token = token
        self.ws = await websockets.connect(_ws_url(self.server), max_size=None)
        hello = json.loads(await self.ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected handshake: {hello}")
        await self.ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        reply = json.loads(await self.ws.recv())
        if reply.get("type") != "auth_ok":
            raise RuntimeError(f"Auth failed: {reply}")
        self.ui(("status", f"Connected (HA {reply.get('ha_version')})"))

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
        """Route incoming text frames to whichever call/utterance awaits that id."""
        while True:
            try:
                async for raw in self.ws:
                    if isinstance(raw, bytes):
                        continue
                    msg = json.loads(raw)
                    q = self._routes.get(msg.get("id"))
                    if q is not None:
                        await q.put(msg)
            except (websockets.ConnectionClosed, OSError) as exc:
                self.ui(("status", f"Disconnected ({exc.__class__.__name__}); reconnecting…"))
                await self._reconnect()

    async def _reconnect(self):
        delay = 2
        while True:
            await asyncio.sleep(delay)
            try:
                await self.connect()
                await self.load_pipelines()
                return
            except Exception:  # noqa: BLE001 - keep retrying with backoff
                delay = min(delay * 2, 30)

    # ---- utterance ----------------------------------------------------------

    def signal_release(self):
        self._release.set()

    async def start_utterance(self):
        if self._active or self.ws is None:
            return
        self._active = True
        self._release = asyncio.Event()
        try:
            await self._run_utterance()
        except Exception as exc:  # noqa: BLE001 - surface, don't crash the loop
            self.ui(("error", str(exc)))
        finally:
            self._active = False

    async def _run_utterance(self):
        audio_q: "queue.Queue[bytes]" = queue.Queue()
        stop_capture = asyncio.Event()
        finished = asyncio.Event()

        try:
            stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=BLOCK,
                device=self.config.mic_device,
                callback=lambda indata, frames, t, status: audio_q.put(bytes(indata)),
            )
            stream.start()
        except Exception as exc:  # noqa: BLE001 - bad device index, etc.
            self.ui(("error", f"Mic error: {exc}"))
            return

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
        await self.ws.send(json.dumps(run_opts))

        handler_id: int | None = None
        buffered: list[bytes] = []
        streamed_any = False

        async def watch_release():
            await self._release.wait()
            self.ui(("thinking",))  # key released -> Listening popup out, Thinking popup in
            stop_capture.set()

        async def forward_audio():
            nonlocal handler_id, buffered
            loop = asyncio.get_event_loop()
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

        watcher = asyncio.ensure_future(watch_release())
        forwarder = asyncio.ensure_future(forward_audio())
        capper = asyncio.ensure_future(max_record())
        completer = asyncio.ensure_future(completion_watchdog())

        try:
            while True:
                msg = await events.get()
                if msg.get("type") == "__timeout__":
                    self.ui(("error", "No response — timed out"))
                    break
                if msg.get("type") == "result":
                    if not msg.get("success"):
                        self.ui(("error", str(msg.get("error"))))
                        stop_capture.set()
                        break
                    continue
                ev = msg.get("event", {})
                etype = ev.get("type")
                data = ev.get("data", {})

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
                        self.ui(("response_append", piece))
                elif etype == "intent-end":
                    if not streamed_any:
                        speech = (data.get("intent_output", {}).get("response", {})
                                  .get("speech", {}).get("plain", {}).get("speech"))
                        if speech:
                            self.ui(("response_final", speech))
                elif etype == "tts-end":
                    await self._play(data["tts_output"])
                elif etype == "error":
                    self.ui(("error", data.get("message", "pipeline error")))
                    stop_capture.set()
                elif etype == "run-end":
                    break
        finally:
            finished.set()
            stop_capture.set()
            self._routes.pop(msg_id, None)
            try:
                await forwarder  # robust: never raises out (all sends guarded)
            except Exception:  # noqa: BLE001 - defensive; done must still fire
                pass
            for task in (watcher, capper, completer):
                task.cancel()
            self.ui(("done",))  # ALWAYS fires -> popup always dismisses

    async def _play(self, tts_output: dict):
        url = tts_output["url"]
        full = url if url.startswith("http") else self.server + url

        def fetch_decode_play():
            req = urllib.request.Request(full, headers={"Authorization": f"Bearer {self.token}"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            decoded = miniaudio.decode(data, output_format=miniaudio.SampleFormat.SIGNED16)
            samples = np.frombuffer(decoded.samples, dtype=np.int16)
            if decoded.nchannels > 1:
                samples = samples.reshape(-1, decoded.nchannels)
            sd.play(samples, decoded.sample_rate, device=self.config.speaker_device)
            sd.wait()

        try:
            await asyncio.get_event_loop().run_in_executor(None, fetch_decode_play)
        except Exception as exc:  # noqa: BLE001 - playback failure shouldn't abort the run
            self.ui(("status", f"Playback error: {exc}"))
