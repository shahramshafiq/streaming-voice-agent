"""Reads every setting from the .env file.

Nothing configurable (keys, model names, URLs, thresholds) is hardcoded anywhere else.
If a value is missing or has the wrong type, startup stops and lists ALL the problems at once.
"""
import os
from dataclasses import dataclass
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()


class ConfigError(Exception):
    """Raised when .env is missing a value or a value has the wrong type."""


class _Reader:
    """Small helper that reads typed values and remembers every problem it finds."""

    def __init__(self):
        self.problems = []

    def _raw(self, name):
        value = os.getenv(name)
        if value is None or not value.strip():
            self.problems.append(f"{name} is missing or empty")
            return None
        return value.strip()

    def text(self, name):
        value = self._raw(name)
        return value if value is not None else ""

    def integer(self, name):
        value = self._raw(name)
        if value is None:
            return 0
        try:
            return int(value)
        except ValueError:
            self.problems.append(f"{name} must be a whole number, got {value!r}")
            return 0

    def number(self, name):
        value = self._raw(name)
        if value is None:
            return 0.0
        try:
            return float(value)
        except ValueError:
            self.problems.append(f"{name} must be a number, got {value!r}")
            return 0.0

    def optional_number(self, name):
        """Blank or missing means 'off' (returns None)."""
        value = os.getenv(name)
        if value is None or not value.strip():
            return None
        try:
            return float(value.strip())
        except ValueError:
            self.problems.append(f"{name} must be a number or left empty, got {value!r}")
            return None

    def flag(self, name):
        value = self._raw(name)
        if value is None:
            return False
        if value.lower() in ("true", "1", "yes"):
            return True
        if value.lower() in ("false", "0", "no"):
            return False
        self.problems.append(f"{name} must be true or false, got {value!r}")
        return False

    def number_list(self, name):
        value = self._raw(name)
        if value is None:
            return []
        try:
            return [int(part) for part in value.split(",")]
        except ValueError:
            self.problems.append(f"{name} must look like 50,80,120,160, got {value!r}")
            return []


@dataclass(frozen=True)
class Settings:
    # server
    host: str
    port: int
    log_level: str

    # OpenAI (LLM)
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    openai_max_output_tokens: int
    openai_keepalive_expiry_seconds: float
    openai_warm_interval_seconds: float
    history_max_messages: int
    system_prompt: str

    # Deepgram (STT, Flux)
    deepgram_api_key: str
    deepgram_ws_url: str
    deepgram_model: str
    deepgram_encoding: str
    deepgram_sample_rate: int
    deepgram_eot_threshold: float
    deepgram_eager_eot_threshold: Optional[float]
    deepgram_eot_timeout_ms: int

    # ElevenLabs (TTS)
    elevenlabs_api_key: str
    elevenlabs_ws_base_url: str
    elevenlabs_voice_id: str
    elevenlabs_model: str
    elevenlabs_output_format: str
    tts_sample_rate: int  # worked out from the output format, e.g. pcm_16000 -> 16000
    elevenlabs_inactivity_timeout: int
    elevenlabs_chunk_schedule: List[int]
    elevenlabs_stability: float
    elevenlabs_similarity: float
    elevenlabs_speaker_boost: bool
    elevenlabs_auto_mode: bool
    tts_connect_timeout_seconds: float
    turn_timeout_seconds: float

    # text chunking
    first_chunk_chars: int
    chunk_target_chars: int
    chunk_max_extra_chars: int

    # browser audio
    mic_chunk_ms: int

    # behaviour
    barge_in_enabled: bool
    idle_timeout_seconds: float
    drain_grace_seconds: float


def load_settings():
    r = _Reader()

    output_format = r.text("ELEVENLABS_OUTPUT_FORMAT")
    tts_rate = 0
    if output_format:
        if output_format.startswith("pcm_") and output_format[4:].isdigit():
            tts_rate = int(output_format[4:])
        else:
            r.problems.append(
                f"ELEVENLABS_OUTPUT_FORMAT must be a pcm format like pcm_16000, got {output_format!r}"
            )

    settings = Settings(
        host=r.text("HOST"),
        port=r.integer("PORT"),
        log_level=r.text("LOG_LEVEL"),
        openai_api_key=r.text("OPENAI_API_KEY"),
        openai_base_url=r.text("OPENAI_BASE_URL"),
        openai_model=r.text("OPENAI_MODEL"),
        openai_max_output_tokens=r.integer("OPENAI_MAX_OUTPUT_TOKENS"),
        openai_keepalive_expiry_seconds=r.number("OPENAI_KEEPALIVE_EXPIRY_SECONDS"),
        openai_warm_interval_seconds=r.number("OPENAI_WARM_INTERVAL_SECONDS"),
        history_max_messages=r.integer("HISTORY_MAX_MESSAGES"),
        system_prompt=r.text("SYSTEM_PROMPT"),
        deepgram_api_key=r.text("DEEPGRAM_API_KEY"),
        deepgram_ws_url=r.text("DEEPGRAM_WS_URL"),
        deepgram_model=r.text("DEEPGRAM_MODEL"),
        deepgram_encoding=r.text("DEEPGRAM_ENCODING"),
        deepgram_sample_rate=r.integer("DEEPGRAM_SAMPLE_RATE"),
        deepgram_eot_threshold=r.number("DEEPGRAM_EOT_THRESHOLD"),
        deepgram_eager_eot_threshold=r.optional_number("DEEPGRAM_EAGER_EOT_THRESHOLD"),
        deepgram_eot_timeout_ms=r.integer("DEEPGRAM_EOT_TIMEOUT_MS"),
        elevenlabs_api_key=r.text("ELEVENLABS_API_KEY"),
        elevenlabs_ws_base_url=r.text("ELEVENLABS_WS_BASE_URL"),
        elevenlabs_voice_id=r.text("ELEVENLABS_VOICE_ID"),
        elevenlabs_model=r.text("ELEVENLABS_MODEL"),
        elevenlabs_output_format=output_format,
        tts_sample_rate=tts_rate,
        elevenlabs_inactivity_timeout=r.integer("ELEVENLABS_INACTIVITY_TIMEOUT"),
        elevenlabs_chunk_schedule=r.number_list("ELEVENLABS_CHUNK_SCHEDULE"),
        elevenlabs_stability=r.number("ELEVENLABS_STABILITY"),
        elevenlabs_similarity=r.number("ELEVENLABS_SIMILARITY"),
        elevenlabs_speaker_boost=r.flag("ELEVENLABS_SPEAKER_BOOST"),
        elevenlabs_auto_mode=r.flag("ELEVENLABS_AUTO_MODE"),
        tts_connect_timeout_seconds=r.number("TTS_CONNECT_TIMEOUT_SECONDS"),
        turn_timeout_seconds=r.number("TURN_TIMEOUT_SECONDS"),
        first_chunk_chars=r.integer("FIRST_CHUNK_CHARS"),
        chunk_target_chars=r.integer("CHUNK_TARGET_CHARS"),
        chunk_max_extra_chars=r.integer("CHUNK_MAX_EXTRA_CHARS"),
        mic_chunk_ms=r.integer("MIC_CHUNK_MS"),
        barge_in_enabled=r.flag("BARGE_IN_ENABLED"),
        idle_timeout_seconds=r.number("IDLE_TIMEOUT_SECONDS"),
        drain_grace_seconds=r.number("DRAIN_GRACE_SECONDS"),
    )

    # extra sanity checks, only once every value was read without problems
    if not r.problems:
        if any(value < 50 for value in settings.elevenlabs_chunk_schedule):
            r.problems.append("ELEVENLABS_CHUNK_SCHEDULE values must all be 50 or more (ElevenLabs rejects smaller ones)")
        if not 20 <= settings.mic_chunk_ms <= 250:
            r.problems.append("MIC_CHUNK_MS should be between 20 and 250 (80 is recommended for Flux)")
        if settings.deepgram_eager_eot_threshold is not None:
            if not 0.3 <= settings.deepgram_eager_eot_threshold <= 0.9:
                r.problems.append("DEEPGRAM_EAGER_EOT_THRESHOLD must be between 0.3 and 0.9, or empty to switch it off")
            elif settings.deepgram_eager_eot_threshold > settings.deepgram_eot_threshold:
                r.problems.append("DEEPGRAM_EAGER_EOT_THRESHOLD must not be higher than DEEPGRAM_EOT_THRESHOLD")
        if settings.deepgram_encoding != "linear16":
            r.problems.append("DEEPGRAM_ENCODING must be linear16 (the browser sends 16-bit PCM)")

    if r.problems:
        raise ConfigError("\n".join(f"  - {problem}" for problem in r.problems))
    return settings


try:
    settings = load_settings()
except ConfigError as error:
    raise SystemExit(f"\nYour .env file has problems:\n{error}\n\nCompare it with .env.example and try again.\n")