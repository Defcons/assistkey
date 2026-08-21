# AssistKey — push-to-talk for Home Assistant Assist

A lightweight Windows **system-tray app** that lets you talk to your
[Home Assistant](https://www.home-assistant.io/) voice assistant from your PC:
**hold a hotkey, speak, release.** No wake word, no always-listening mic — the
microphone only opens while you hold the key.

A sequence of smooth, always-on-top popups shows **Listening…**, your
transcribed words, then the assistant's reply as it streams in — and the reply
is spoken aloud.

> Works with any Home Assistant [Assist pipeline](https://www.home-assistant.io/voice_control/):
> local Whisper/Piper, cloud, or an LLM conversation agent — whatever you've set up.

![A reply popup](docs/popup.png)

## Features

- **Hold-to-talk** (default) or **tap-to-toggle** on a configurable hotkey.
- **Optional wake word** (off by default) — say "Hey Jarvis" (or Alexa / Hey
  Mycroft / Hey Rhasspy) to talk hands-free. Runs locally with
  [openWakeWord](https://github.com/dscripka/openWakeWord); Home Assistant's own
  voice-activity detection ends each command.
- **Streaming UI** — popups slide up from the bottom-middle of whichever monitor
  your cursor is on: Listening → Thinking (with your recognised words) → the
  streaming reply, then it slides away.
- **Spoken replies** through your chosen output device.
- **Everything configurable** in a modern settings dialog: Home Assistant URL &
  token, hotkey, trigger mode, microphone, speaker, which assistant pipeline,
  and how long popups linger.
- **Single instance** — launching again (or after an update) cleanly replaces
  the running copy, so you never get duplicate hotkey listeners.
- **Robust** — the popup is guaranteed to dismiss and the app self-recovers even
  if the connection drops or a pipeline stalls (watchdogs + a hard-hide backstop).

## Requirements

- Windows 10 or 11
- Python 3.12+ (with tkinter — the standard python.org installer includes it)
- A microphone
- A Home Assistant instance reachable over **HTTPS/WSS**, with a voice pipeline
  configured (Settings → Voice assistants)

## Install

```bat
git clone https://github.com/Defcons/assistkey.git
cd assistkey
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run

- **Silent tray app:** double-click **`AssistKey.vbs`** (no console window).
- **With a console** (handy the first time / for troubleshooting): `run.bat`.

On first run the **Settings** dialog opens automatically. Enter your:

1. **Server URL** — e.g. `https://homeassistant.local:8123` (or your external
   HTTPS URL). A secure (`https`) URL is required — browsers/OS block microphone
   capture over plain `http`.
2. **Access token** — in Home Assistant, click your profile (bottom-left) →
   **Long-lived access tokens** → **Create token**.

Click **Test connection** to verify, then **Save**. You're ready — **hold F9**
and speak.

Right-click the tray icon any time for **Settings…** or **Quit**.

### Start automatically at login

Put a shortcut to **`AssistKey.vbs`** in your Startup folder
(`Win+R` → `shell:startup`).

## Settings

<img src="docs/settings.png" width="360" alt="Settings dialog">

| Setting | Notes |
|---|---|
| **Server URL / Access token** | Your Home Assistant connection. |
| **Hotkey** | Click *Change…* then press the key (or combo, e.g. `Ctrl`+`Space`). |
| **Trigger** | *Hold to talk* (hold while speaking) or *Tap to toggle* (tap on, tap off). |
| **Microphone / Speaker** | Input and output devices, or the system defaults. |
| **Assistant** | Which HA pipeline to use; *Preferred* follows your HA default. |
| **Wake word** | Enable hands-free listening and pick the word (Hey Jarvis / Alexa / …). Off by default; downloads a small local model on first enable. |
| **Sensitivity** | Wake-word detection threshold — higher is stricter (fewer false triggers). |
| **Dismiss after** | How long a reply popup lingers before sliding away. |

Settings are saved to `config.json` next to the app and applied immediately —
no restart needed. **`config.json` contains your access token, so it is
git-ignored and never committed.**

## A note on "live" transcription

The assistant's **reply streams in word-by-word**. Your **own speech** appears
the moment you release the key, not letter-by-letter as you talk — Home
Assistant's speech-to-text returns the transcript once, at the end of the
utterance. Releasing the key is what ends the utterance (true push-to-talk; no
silence detection).

## Development

```bat
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest tests/
```

| File | Role |
|---|---|
| `app.py` | Entry point: tray, hotkey, asyncio loop, single-instance, GUI wiring. |
| `assist_client.py` | Async HA Assist pipeline client: audio capture, streaming, playback. |
| `wake.py` | Optional wake-word listener (openWakeWord). |
| `overlay.py` | The popup overlay + the settings dialog. |
| `config.py` | `config.json` load/save + credential resolution + hotkey serialization. |
| `tests/` | Automated tests (config, hotkey modes, overlay stuck-recovery). |

Errors are written to `assistkey.log` next to the app (the silent launcher has
no console).

## License

MIT — see [LICENSE](LICENSE).
