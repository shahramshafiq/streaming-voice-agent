# Voice Agent: speech to text, LLM, text to speech

A voice assistant you talk to in the browser. You speak, it listens, thinks, and answers out loud. You can cut in while it is talking and it stops and listens to you.

I built this to understand how a real-time voice agent works from the inside, so every piece is written by hand instead of using a voice platform. The main goal was to keep the gap between "I stopped talking" and "I hear the answer" as short as I could, and to measure that gap honestly.

## What it uses

| Job | Service |
|---|---|
| Hearing you (speech to text) | Deepgram Flux (`flux-general-en`) |
| Thinking (the language model) | OpenAI, `gpt-4.1-mini` through the Responses API |
| Speaking (text to speech) | ElevenLabs Flash v2.5 |
| Server | Python, FastAPI, WebSockets |
| Browser audio | AudioWorklet for the microphone and for playback |

The model names, URLs and every setting live in `.env`, so you can swap any of them without touching the code.

## How it works

Your microphone audio goes to the server in small 80 ms pieces. The server forwards it to Deepgram. Deepgram Flux decides when you have finished speaking and tells the server with a few events:

- `StartOfTurn`: you started talking. If the assistant is speaking at that moment, it stops (barge-in).
- `Update`: the words so far. These show up live on the page.
- `EagerEndOfTurn`: you have probably finished. The server asks the LLM for an answer right away, but nothing is spoken yet.
- `TurnResumed`: you kept talking, so the early answer is thrown away.
- `EndOfTurn`: you really finished. If the early answer matches what you said, it is spoken immediately.

Once a turn is confirmed, the LLM text is cut into short phrases. The first phrase is sent to ElevenLabs as soon as it is ready, so speech starts while the LLM is still writing. The audio comes back to the browser and plays through a small ring buffer.

```
microphone -> server -> Deepgram
                           |  turn events
                           v
                  LLM (OpenAI) -> phrases -> ElevenLabs
                                                  |  audio
speakers <- browser player <----------------------+
```

A few details that took some work:

- The OpenAI connection is kept warm with a tiny free request every 20 seconds, so a reply never pays for opening a new connection.
- The ElevenLabs connection is opened when you press Start and reused for every reply.
- When you interrupt, the history keeps only the words you actually heard. This is an estimate based on how much audio had played.
- Every reply is timed, and the page shows where the time went.

## Setup

You need Python 3.12 or newer and three API keys: OpenAI, Deepgram and ElevenLabs.

```powershell
git clone https://github.com/shahramshafiq/voice-agent-stt-llm-tts.git
cd voice-agent-stt-llm-tts
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in the three keys. Then start the server:

```powershell
python -m uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`, press **Start conversation**, allow the microphone and talk.

If a value in `.env` is missing or wrong, the server stops at startup and lists every problem at once.

## Using it

- Wear headphones. Without them the assistant can hear itself through your speakers.
- If you test on speakers, set `BARGE_IN_ENABLED=false` so it ignores sound while it is talking.
- Deepgram bills while the microphone is streaming, even in silence. The page stops listening after `IDLE_TIMEOUT_SECONDS` with no speech, and the Stop button always works.
- I tested it in a Chromium based browser. Safari and Firefox are untested.

## Settings you will probably change

All of these are in `.env.example` with a short comment.

| Setting | What it does |
|---|---|
| `DEEPGRAM_EOT_THRESHOLD` | How sure Deepgram must be that you finished. Lower is faster but can cut you off. |
| `DEEPGRAM_EAGER_EOT_THRESHOLD` | How sure it must be before the LLM starts early. Leave it empty to turn the early start off. |
| `FIRST_CHUNK_CHARS` | How short the first spoken phrase is. Smaller starts faster, larger sounds smoother. |
| `OPENAI_MAX_OUTPUT_TOKENS` | Hard limit on answer length. |
| `OPENAI_MODEL` | The language model. |
| `MIC_CHUNK_MS` | Size of the microphone pieces. 80 is what Deepgram recommends for Flux. |
| `BARGE_IN_ENABLED` | Whether you can interrupt the assistant. |

## Project layout

```
app/
  config.py          reads and checks .env
  logging_setup.py   log format
  chunker.py         cuts LLM text into speakable phrases
  timing.py          the stopwatch used for latency numbers
  llm.py             OpenAI client, kept warm
  stt.py             Deepgram Flux connection
  tts.py             ElevenLabs connection (one socket, one context per reply)
  turn.py            one reply: LLM text to phrases to audio
  session.py         one browser connection: turn events, barge-in, history
  main.py            FastAPI app and the WebSocket
static/
  index.html  style.css  app.js
  mic-worklet.js     microphone to 16 kHz audio chunks
  player-worklet.js  plays the reply and reports playback progress
```

## Latency

The page has a latency table for the last reply. Every number is in milliseconds, counted from the moment the system decided you had finished. A negative number means that work started earlier, while the early draft was running.

In one test session on a laptop in Pakistan (the servers are far away, which adds travel time), four replies took between 1.0 and 2.8 seconds from the end of my sentence until I heard sound. Where the time went on the warm replies:

| Step | Typical time |
|---|---|
| Deciding I had finished talking | 320 to 1200 ms |
| OpenAI writing its first word | 700 to 925 ms (the early draft hides part of this) |
| First phrase to the first sound from ElevenLabs | about 285 ms |
| Audio reaching the speakers | 10 to 45 ms |

The first reply of a session was slower, because OpenAI took about 2 seconds to answer. These numbers come from a handful of replies on one day, so treat them as a rough picture, not a benchmark.

## Known limits

- English only for now, because `flux-general-en` is an English model.
- The "words you heard" after an interruption is an estimate.
- Replies are plain text. The assistant is told not to use lists or markdown because they sound odd when spoken.
- It runs locally. Moving the server closer to the API providers would cut a few hundred milliseconds, but I have not tried that yet.

## Troubleshooting

- **"Could not connect" when pressing Start:** check the Deepgram and ElevenLabs keys. Flux also has to be available on your Deepgram account.
- **No sound:** check that the browser tab is not muted and that you pressed Start (browsers block audio until you click).
- **It answers itself or interrupts itself:** use headphones, or set `BARGE_IN_ENABLED=false`.
- **Old page after an update:** press Ctrl+F5.
- **More detail in the terminal:** set `LOG_LEVEL=DEBUG` in `.env`.

## What is next

I want to compare a few other models for the first-word delay (`gpt-4.1-nano`, the Chat Completions endpoint, and a faster provider), then tune the Deepgram thresholds with more real conversations.
