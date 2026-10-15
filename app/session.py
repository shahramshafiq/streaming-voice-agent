"""One browser connection = one VoiceSession.

The session is the conductor. It listens to the microphone audio from the browser, forwards
it to Deepgram, reacts to Deepgram's turn events, starts and cancels Turns (LLM + TTS), and keeps
the browser informed with small JSON events.

Messages from the browser:
  binary                      microphone audio (16-bit PCM, mono)
  {"type": "start"}           begin the conversation
  {"type": "stop"}            end the conversation
  {"type": "playback_started"}   the speakers just began playing the reply
  {"type": "playback_progress", "played_ms": N}   how much of the reply has been played
  {"type": "playback_drained"}   the speakers finished playing everything

Messages to the browser:
  binary                      the reply audio (16-bit PCM, mono)
  JSON events: config, ready, state, user_speaking, transcript_interim, transcript_final,
               assistant_text, audio_end, clear, interrupted, metrics, error, idle_timeout
"""
import asyncio
import json
import logging
import re
import time

from .stt import DeepgramFluxStream
from .timing import TurnTimer
from .tts import ElevenLabsSession
from .turn import Turn

log = logging.getLogger("session")


def _same_text(a, b):
    """Compare two transcripts ignoring capitals, punctuation and spacing."""
    return re.sub(r"\W+", " ", a.lower()).strip() == re.sub(r"\W+", " ", b.lower()).strip()


class VoiceSession:
    def __init__(self, websocket, settings, llm):
        self.ws = websocket
        self.settings = settings
        self.llm = llm
        self.tts = ElevenLabsSession(settings)
        self.stt = None

        self.history = []  # [{"role": "user"/"assistant", "content": "..."}]
        self.turn = None  # the reply the user is waiting for or hearing
        self.draft = None  # a reply started early, waiting for EndOfTurn
        self.state = "idle"  # idle, connecting, listening, thinking, speaking
        self.awaiting_drain = False  # all audio generated, speakers still playing it
        self.played_ms = 0
        self.turn_counter = 0
        self.last_activity = time.monotonic()
        self.closed = False

        self._ignore_user_turn = False  # barge-in off: ignore speech that started while the bot talked
        self._send_lock = asyncio.Lock()  # one message at a time to the browser
        self._lock = asyncio.Lock()  # one state change at a time
        self._drain_task = None
        self._idle_task = None
        self._background = set()  # keeps references to small background tasks

    # ================================================================ sending to the browser
    async def send_event(self, event):
        if self.closed:
            return
        try:
            async with self._send_lock:
                await self.ws.send_json(event)
        except Exception as exc:
            log.debug("could not send %s to the browser: %s", event.get("type"), type(exc).__name__)

    async def send_audio(self, data):
        if self.closed:
            return
        try:
            async with self._send_lock:
                await self.ws.send_bytes(data)
        except Exception as exc:
            log.debug("could not send audio to the browser: %s", type(exc).__name__)

    async def _set_state(self, value):
        self.state = value
        await self.send_event({"type": "state", "value": value})

    # ================================================================ main loop (browser -> server)
    async def run(self):
        await self.send_event({
            "type": "config",
            "mic_sample_rate": self.settings.deepgram_sample_rate,
            "mic_chunk_ms": self.settings.mic_chunk_ms,
            "tts_sample_rate": self.settings.tts_sample_rate,
            "barge_in_enabled": self.settings.barge_in_enabled,
        })
        try:
            while True:
                message = await self.ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    await self._on_mic_audio(message["bytes"])
                elif message.get("text") is not None:
                    try:
                        control = json.loads(message["text"])
                    except json.JSONDecodeError:
                        log.warning("the browser sent a message that is not JSON")
                        continue
                    await self._on_control(control)
        except Exception:
            log.exception("browser connection failed")
        finally:
            await self.shutdown()

    async def _on_mic_audio(self, data):
        if self.stt is not None and self.state != "idle":
            await self.stt.send_audio(data)

    async def _on_control(self, message):
        kind = message.get("type")
        if kind == "start":
            await self._start_pipeline()
        elif kind == "stop":
            await self._stop_pipeline()
        elif kind == "playback_started":
            await self._on_playback_started()
        elif kind == "playback_progress":
            self.played_ms = int(message.get("played_ms", 0))
        elif kind == "playback_drained":
            await self._on_playback_drained()
        else:
            log.debug("unknown message from the browser: %s", kind)

    # ================================================================ start / stop
    async def _start_pipeline(self):
        if self.state != "idle":
            return
        await self._set_state("connecting")

        self.stt = DeepgramFluxStream(self.settings, self._on_stt_event, self._on_stt_closed)
        # connect to Deepgram and ElevenLabs at the same time
        results = await asyncio.gather(self.stt.connect(), self.tts.connect(), return_exceptions=True)
        failures = [r for r in results if isinstance(r, Exception)]
        if failures:
            log.error("could not start: %s", failures)
            await self.send_event({
                "type": "error",
                "message": "Could not connect: " + "; ".join(f"{type(f).__name__}: {f}" for f in failures),
            })
            await self._stop_pipeline()
            return

        self.last_activity = time.monotonic()
        self.stt.bytes_sent = 0
        await self._set_state("listening")
        await self.send_event({"type": "ready"})
        self._idle_task = asyncio.create_task(self._idle_watchdog())
        log.info("session ready")

    async def _stop_pipeline(self, error_message=None):
        async with self._lock:
            if self._idle_task is not None:
                self._idle_task.cancel()
                self._idle_task = None
            await self._cancel_draft_nolock()
            if self.turn is not None:
                turn, self.turn = self.turn, None
                await turn.cancel()
            self._cancel_drain_watchdog()
            self.awaiting_drain = False
            self._ignore_user_turn = False
            if self.stt is not None:
                await self.stt.close()
                self.stt = None
            await self.send_event({"type": "clear"})
            if error_message:
                await self.send_event({"type": "error", "message": error_message})
            await self._set_state("idle")

    async def shutdown(self):
        if self.closed:
            return
        await self._stop_pipeline()
        self.closed = True
        await self.tts.close()
        log.info("session closed")

    async def _idle_watchdog(self):
        """Stop listening if nobody has spoken for a while (saves Deepgram credits)."""
        while True:
            await asyncio.sleep(5)
            idle_for = time.monotonic() - self.last_activity
            if self.state == "listening" and idle_for > self.settings.idle_timeout_seconds:
                log.info("no activity for %.0f s, stopping", idle_for)
                await self.send_event({"type": "idle_timeout"})
                self._spawn(self._stop_pipeline())
                return

    async def _on_stt_closed(self, reason):
        if self.closed or self.state == "idle":
            return
        # run in a separate task: stop_pipeline closes the STT reader that is calling us
        self._spawn(self._stop_pipeline(error_message=f"Speech recognition stopped: {reason}"))

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ================================================================ Deepgram events
    async def _on_stt_event(self, event):
        kind = event.get("type")
        if kind == "TurnInfo":
            async with self._lock:
                await self._handle_turn_info(event)
        elif kind == "Connected":
            log.info("Deepgram connected")
        elif kind and "error" in kind.lower():
            log.error("Deepgram error: %s", event)
            await self.send_event({"type": "error", "message": f"Deepgram: {event}"})
        else:
            log.debug("Deepgram message: %s", kind)

    async def _handle_turn_info(self, event):
        name = event.get("event")
        transcript = (event.get("transcript") or "").strip()

        if name == "StartOfTurn":
            self.last_activity = time.monotonic()
            await self.send_event({"type": "user_speaking"})
            if self.turn is not None:
                if self.settings.barge_in_enabled:
                    await self._interrupt_nolock()
                else:
                    log.info("barge-in is off: ignoring speech while the assistant talks")
                    self._ignore_user_turn = True
            elif self.draft is not None:
                await self._cancel_draft_nolock()

        elif name == "Update":
            if transcript and not self._ignore_user_turn:
                await self.send_event({"type": "transcript_interim", "text": transcript})

        elif name == "EagerEndOfTurn":
            can_draft = self.settings.deepgram_eager_eot_threshold is not None
            if can_draft and transcript and self.turn is None and self.draft is None and not self._ignore_user_turn:
                timer = TurnTimer()
                timer.mark("draft_started")
                self.draft = self._new_turn(transcript, timer)
                log.info("early draft started for %r", transcript)

        elif name == "TurnResumed":
            if self.draft is not None:
                log.info("user kept talking: draft discarded")
                await self._cancel_draft_nolock()

        elif name == "EndOfTurn":
            await self._on_end_of_turn(event, transcript)

    # ================================================================ turns
    def _new_turn(self, user_text, timer):
        self.turn_counter += 1
        turn = Turn(
            turn_id=self.turn_counter,
            user_text=user_text,
            history=list(self.history),
            settings=self.settings,
            llm=self.llm,
            tts=self.tts,
            timer=timer,
            send_event=self.send_event,
            send_audio=self.send_audio,
            on_speaking=self._on_speaking,
        )
        turn.start().add_done_callback(lambda task, t=turn: self._spawn(self._on_turn_done(t, task)))
        return turn

    async def _cancel_draft_nolock(self):
        if self.draft is not None:
            draft, self.draft = self.draft, None
            await draft.cancel()

    async def _on_end_of_turn(self, event, transcript):
        self.last_activity = time.monotonic()

        if self._ignore_user_turn:
            self._ignore_user_turn = False
            log.info("ignored a user turn that started while the assistant was talking")
            return
        if not transcript:
            await self._cancel_draft_nolock()
            return
        if self.turn is not None:  # should not happen, but never run two replies at once
            await self._interrupt_nolock()

        if self.draft is not None and _same_text(self.draft.user_text, transcript):
            turn, self.draft = self.draft, None
            turn.draft_hit = True
        else:
            await self._cancel_draft_nolock()
            turn = self._new_turn(transcript, TurnTimer())

        turn.eot_lag_ms = self._eot_lag_ms(event)
        self.turn = turn
        self.played_ms = 0
        self.awaiting_drain = False
        turn.commit()  # opens the gate: the reply may now be spoken
        log.info("turn %d committed (draft_hit=%s): %r", turn.turn_id, turn.draft_hit, transcript)

        await self.send_event({"type": "transcript_final", "text": transcript})
        await self._set_state("thinking")

    def _eot_lag_ms(self, event):
        """How long after the user's last word did Deepgram tell us they were done?"""
        if self.stt is None:
            return None
        words = event.get("words") or []
        last_end = words[-1].get("end") if words else None
        if last_end is None:
            last_end = event.get("audio_window_end")
        if last_end is None:
            return None
        return int(max(0.0, (self.stt.audio_seconds_sent - float(last_end)) * 1000))

    async def _on_speaking(self, turn):
        if turn is self.turn and self.state == "thinking":
            await self._set_state("speaking")

    async def _on_turn_done(self, turn, task):
        """Called when a Turn's task ends (finished, failed or cancelled)."""
        async with self._lock:
            if turn is not self.turn or task.cancelled():
                return  # a draft that was discarded, or a turn that was interrupted
            error = task.exception()
            if error is not None:
                log.error("turn %d failed: %s: %s", turn.turn_id, type(error).__name__, error)
                self._record(turn.user_text, "")
                self.turn = None
                await self.send_event({"type": "clear"})
                await self.send_event({"type": "error", "message": str(error)})
                await self._set_state("listening")
                return
            # all audio was generated; tell the browser no more is coming and wait for it to finish playing
            await self.send_event({"type": "audio_end"})
            self.awaiting_drain = True
            wait = turn.audio_ms / 1000 + self.settings.drain_grace_seconds
            self._drain_task = asyncio.create_task(self._drain_watchdog(turn, wait))

    async def _drain_watchdog(self, turn, wait):
        """Safety net in case the browser never reports that it finished playing."""
        await asyncio.sleep(wait)
        async with self._lock:
            if turn is self.turn and self.awaiting_drain:
                log.warning("the browser never reported the end of playback, finishing anyway")
                await self._finish_playback_nolock()

    def _cancel_drain_watchdog(self):
        if self._drain_task is not None:
            self._drain_task.cancel()
            self._drain_task = None

    # ================================================================ playback reports from the browser
    async def _on_playback_started(self):
        turn = self.turn
        if turn is None:
            return
        turn.timer.mark("audio_played")
        metrics = {"type": "metrics", "draft_hit": turn.draft_hit, "eot_lag_ms": turn.eot_lag_ms}
        metrics.update(turn.timer.summary())
        played = metrics.get("audio_played_ms")
        if played is not None and turn.eot_lag_ms is not None:
            metrics["perceived_ms"] = turn.eot_lag_ms + played
        log.info("turn %d latency: %s", turn.turn_id, {k: v for k, v in metrics.items() if k != "type"})
        await self.send_event(metrics)

    async def _on_playback_drained(self):
        async with self._lock:
            if self.turn is not None and self.awaiting_drain:
                await self._finish_playback_nolock()

    async def _finish_playback_nolock(self):
        turn, self.turn = self.turn, None
        self.awaiting_drain = False
        self._cancel_drain_watchdog()
        self.played_ms = 0
        self._record(turn.user_text, turn.answer)
        await self._set_state("listening")

    # ================================================================ barge-in
    async def _interrupt_nolock(self):
        """The user started talking while the assistant was thinking or speaking."""
        turn, self.turn = self.turn, None
        if turn is None:
            return
        log.info("barge-in on turn %d (played %d ms)", turn.turn_id, self.played_ms)

        await self.send_event({"type": "clear"})  # 1. silence the speakers first
        await turn.cancel()  # 2. stop the LLM and the TTS
        self._cancel_drain_watchdog()

        heard = turn.heard_text(self.played_ms)  # 3. remember only what was actually heard
        self._record(turn.user_text, heard)
        self.awaiting_drain = False
        self.played_ms = 0
        await self.send_event({"type": "interrupted", "heard": heard})
        await self._set_state("listening")

    # ================================================================ conversation memory
    def _record(self, user_text, assistant_text):
        self.history.append({"role": "user", "content": user_text})
        if assistant_text.strip():
            self.history.append({"role": "assistant", "content": assistant_text.strip()})
        self.history = self.history[-self.settings.history_max_messages:]
        while self.history and self.history[0]["role"] != "user":
            self.history.pop(0)  # never start the history with an orphan assistant message