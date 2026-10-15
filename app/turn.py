"""One assistant reply: LLM text -> chunks -> TTS audio -> browser.

A Turn can start in "draft" mode: the LLM starts writing as soon as Deepgram says the user
PROBABLY finished (EagerEndOfTurn). The Turn then waits at a gate. If the user really finished
(EndOfTurn) the gate opens and the text that is already written is spoken immediately.
If the user kept talking (TurnResumed) the draft is simply thrown away.
"""
import asyncio
import logging
import uuid
from contextlib import suppress

from .chunker import extract_speakable_chunks

log = logging.getLogger("turn")


class Turn:
    def __init__(self, turn_id, user_text, history, settings, llm, tts, timer, send_event, send_audio, on_speaking):
        self.turn_id = turn_id
        self.user_text = user_text
        self.history = history  # a copy of the conversation so far
        self.settings = settings
        self.llm = llm
        self.tts = tts
        self.timer = timer
        self.send_event = send_event
        self.send_audio = send_audio
        self.on_speaking = on_speaking  # called once, when the first audio arrives

        self.gate = asyncio.Event()  # opened by commit(); a draft waits here
        self.task = None
        self.draft_hit = False  # True if the early (draft) answer was the one we used
        self.eot_lag_ms = None

        self.answer_parts = []
        self.sent_chunks = []  # the text that was sent to TTS, in order
        self.audio_bytes = 0  # audio received from TTS so far
        self.finished = False  # True once ALL audio has been generated

        self._context_id = None
        self._context_open = False

    # ---------------------------------------------------------------- control
    def start(self):
        self.task = asyncio.create_task(self._run())
        return self.task

    def commit(self):
        """The user really finished: allow this turn to start speaking."""
        self.timer.mark("committed")
        self.gate.set()

    async def cancel(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
        if self.task is not None:
            with suppress(asyncio.CancelledError, Exception):
                await self.task

    @property
    def answer(self):
        return "".join(self.answer_parts).strip()

    @property
    def audio_ms(self):
        # 16-bit mono PCM: 2 bytes per sample
        return self.audio_bytes / (self.settings.tts_sample_rate * 2) * 1000

    def heard_text(self, played_ms):
        """Best guess of the words the user actually heard before interrupting.

        It is an estimate: the share of audio that was played is applied to the text that was sent.
        """
        sent = " ".join(self.sent_chunks)
        if not sent or self.audio_bytes == 0 or played_ms <= 0:
            return ""
        ratio = min(1.0, played_ms / max(self.audio_ms, 1.0))
        cut = int(len(sent) * ratio)
        if cut >= len(sent):
            return sent
        text = sent[:cut]
        if not sent[cut].isspace():  # do not end in the middle of a word
            space = text.rfind(" ")
            text = text[:space] if space > 0 else ""
        return text.strip()

    # ---------------------------------------------------------------- the work
    async def _run(self):
        queue = asyncio.Queue()
        producer = asyncio.create_task(self._read_llm(queue))
        try:
            await self.gate.wait()  # a draft pauses here until the user has really finished

            self._context_id = str(uuid.uuid4())
            await self.tts.start_context(
                self._context_id,
                on_audio=self._on_audio,
                on_first_audio=self._on_first_audio,
            )
            self._context_open = True

            text_buffer = ""
            first_chunk_sent = False

            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item

                self.answer_parts.append(item)
                text_buffer += item
                await self.send_event({"type": "assistant_text", "text": item})

                chunks, text_buffer = extract_speakable_chunks(
                    text_buffer,
                    first_chunk=not first_chunk_sent,
                    first_chunk_chars=self.settings.first_chunk_chars,
                    target_chars=self.settings.chunk_target_chars,
                    max_extra_chars=self.settings.chunk_max_extra_chars,
                )
                for chunk in chunks:
                    # the very first phrase is flushed so ElevenLabs speaks it at once
                    await self._send_chunk(chunk, flush=not first_chunk_sent)
                    first_chunk_sent = True

            chunks, text_buffer = extract_speakable_chunks(
                text_buffer,
                force=True,
                first_chunk=not first_chunk_sent,
                first_chunk_chars=self.settings.first_chunk_chars,
                target_chars=self.settings.chunk_target_chars,
                max_extra_chars=self.settings.chunk_max_extra_chars,
            )
            for chunk in chunks:
                await self._send_chunk(chunk, flush=False)

            if not self.answer:
                raise RuntimeError("The model returned no text.")

            await self.tts.finish_context(self._context_id)
            self._context_open = False
            self.finished = True
            log.info("turn %s: all audio generated", self.turn_id)

        finally:
            producer.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await producer
            if self._context_open and not self.finished:
                with suppress(Exception):
                    await self.tts.cancel_context(self._context_id)

    async def _read_llm(self, queue):
        """Runs in the background: put every piece of LLM text on the queue."""
        messages = [
            {"role": "system", "content": self.settings.system_prompt},
            *self.history,
            {"role": "user", "content": self.user_text},
        ]
        self.timer.mark("llm_request")
        stream = self.llm.stream_text(messages)
        try:
            async for delta in stream:
                self.timer.mark("llm_first_token")
                await queue.put(delta)
            await queue.put(None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("turn %s: LLM failed: %s: %s", self.turn_id, type(exc).__name__, exc)
            await queue.put(RuntimeError(f"The language model failed: {type(exc).__name__}: {exc}"))
        finally:
            with suppress(Exception):
                await stream.aclose()  # close the OpenAI HTTP stream right away

    async def _send_chunk(self, chunk, flush):
        self.timer.mark("first_chunk_sent")
        self.sent_chunks.append(chunk)
        await self.tts.send_text(self._context_id, chunk + " ", flush=flush)

    async def _on_first_audio(self):
        self.timer.mark("tts_first_audio")
        await self.on_speaking(self)

    async def _on_audio(self, audio_bytes):
        self.audio_bytes += len(audio_bytes)
        self.timer.mark("audio_to_browser")
        await self.send_audio(audio_bytes)