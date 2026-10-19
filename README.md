<div align="center">

# Streaming Voice Agent

A voice assistant you talk to in the browser: it listens, thinks and answers out loud, and stops when you interrupt.

![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-WebSockets-009688?style=flat-square&logo=fastapi&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-Responses%20API-412991?style=flat-square)
![Deepgram](https://img.shields.io/badge/Deepgram-Flux-13EF93?style=flat-square)
![ElevenLabs](https://img.shields.io/badge/ElevenLabs-Flash%20v2.5-000000?style=flat-square)

</div>

## Overview

Voice assistants feel slow when each step waits for the one before it. This project streams every stage instead: microphone audio goes to Deepgram Flux for speech to text, the reply is written by an OpenAI model, and ElevenLabs turns it into speech phrase by phrase while the model is still writing. The pipeline is written by hand on FastAPI and WebSockets, without a voice platform, and the page measures where the time goes on every reply.

## Key Features

- **Turn detection from Deepgram Flux events:** `StartOfTurn`, `Update`, `EagerEndOfTurn`, `TurnResumed` and `EndOfTurn` decide when a reply is drafted, started or cancelled (`app/stt.py`, `app/session.py`).
- **Early answer drafting:** on `EagerEndOfTurn` the LLM starts writing before the user has definitely finished. The draft waits at a gate, is spoken if the final transcript matches, and is discarded if the user keeps talking. It can be switched off by leaving `DEEPGRAM_EAGER_EOT_THRESHOLD` empty (`app/turn.py`, `app/session.py`).
- **Barge-in:** speaking over the assistant clears playback, cancels the LLM and TTS work, and keeps only an estimate of the words the user actually heard in the history. It can be switched off for speaker setups.
- **Speech starts before the reply is complete:** LLM text is cut into short phrases, and the first one is flushed to ElevenLabs as soon as it is ready (`app/chunker.py`, `app/turn.py`).
- **Persistent connections:** one ElevenLabs WebSocket per browser session with one context per reply, and an OpenAI HTTPS connection kept warm with a periodic background request (`app/tts.py`, `app/llm.py`).
- **Per-reply latency table:** every turn is timed with a stopwatch and the page shows each step in milliseconds, plus the average of the last 10 replies (`app/timing.py`, `static/app.js`).
- **Configuration with fail-fast validation:** every key, model name, URL and threshold is read from `.env`, and startup stops with a list of all problems at once (`app/config.py`).
- **Browser audio with AudioWorklets:** the microphone is downsampled to 16-bit PCM chunks and the reply is played from a ring buffer that reports playback progress back to the server.

## Tech Stack

| Area | Technology |
|---|---|
| Language | Python, JavaScript (browser) |
| Server | FastAPI, Uvicorn (WebSocket endpoint for the browser) |
| Provider connections | `websockets` client for Deepgram and ElevenLabs, `openai` Python SDK for OpenAI |
| Speech to text | Deepgram Flux (`flux-general-en`) |
| Language model | OpenAI `gpt-4.1-mini` through the Responses API |
| Text to speech | ElevenLabs Flash v2.5 (`eleven_flash_v2_5`) |
| Browser audio | Web Audio API, AudioWorklet |
| Configuration | python-dotenv, `.env` |

## Project Structure

```text
app/
  main.py            FastAPI app: page, /health and the /ws/voice WebSocket
  session.py         One browser connection: turn events, barge-in, history
  turn.py            One reply: LLM text to phrases to audio
  stt.py             Deepgram Flux WebSocket client
  tts.py             ElevenLabs WebSocket client (one context per reply)
  llm.py             OpenAI client with connection warm-up
  chunker.py         Cuts LLM text into speakable phrases
  timing.py          Stopwatch for the latency table
  config.py          Reads and validates .env
static/
  index.html, style.css, app.js   The browser page
  mic-worklet.js     Microphone to 16 kHz PCM chunks
  player-worklet.js  Ring-buffer playback with progress reports
.env.example         Every setting the program uses
```

## Getting Started

### Prerequisites

- Python 3.12 or newer
- API keys for OpenAI, Deepgram (with Flux available on the account) and ElevenLabs
- A microphone and a Chromium-based browser (the only kind tested); headphones recommended

### Installation

```bash
git clone https://github.com/shahramshafiq/streaming-voice-agent.git
cd streaming-voice-agent
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS or Linux
# source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in the three keys:

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | OpenAI access |
| `DEEPGRAM_API_KEY` | Deepgram access |
| `ELEVENLABS_API_KEY` | ElevenLabs access |

All other settings (models, URLs, thresholds, chunk sizes) are already filled in with default values in `.env.example`. Every setting is required, so keep all lines in `.env`.

### Run

```bash
python -m uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`, press **Start conversation**, allow the microphone and talk.

## Usage

- `GET /` serves the voice page, `GET /health` returns `{"status": "ok"}`, and `/ws/voice` is the WebSocket the page uses for microphone audio, control messages and reply audio.
- Use headphones. On speakers, set `BARGE_IN_ENABLED=false` so the assistant ignores sound while it is talking.
- The server stops listening after `IDLE_TIMEOUT_SECONDS` (180 by default) without speech, to save Deepgram credits. The Stop button always works.
- Settings worth tuning in `.env`:

| Setting | Effect |
|---|---|
| `DEEPGRAM_EOT_THRESHOLD` | How sure Deepgram must be that you finished. Lower is faster but can cut you off. |
| `DEEPGRAM_EAGER_EOT_THRESHOLD` | When the LLM may start early. Leave empty to turn early drafting off. |
| `FIRST_CHUNK_CHARS` | Length of the first spoken phrase. Smaller starts faster, larger sounds smoother. |
| `OPENAI_MODEL` | The language model. |
| `OPENAI_MAX_OUTPUT_TOKENS` | Hard limit on answer length. |
| `MIC_CHUNK_MS` | Microphone chunk size in milliseconds. 80 is the value recommended for Flux. |
| `LOG_LEVEL` | Set to `DEBUG` for step-by-step output in the terminal. |

## Limitations and Roadmap

- English only for now, because the default Deepgram model is `flux-general-en`.
- The words heard before an interruption are an estimate based on how much audio had played, and conversation history is kept in memory for the session only.
- There are no automated tests yet. The phrase chunker in `app/chunker.py` is a pure function and the natural first target.
- Planned: compare other models and endpoints for the delay to the first word, and tune the Deepgram thresholds with more real conversations.

## Author

**Shahram Shafiq** - [GitHub](https://github.com/shahramshafiq) | [LinkedIn](https://www.linkedin.com/in/shahramshafiq/)
