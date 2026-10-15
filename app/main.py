"""The FastAPI application: the web page, a health check and the voice WebSocket."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .llm import LLMClient
from .logging_setup import setup_logging
from .session import VoiceSession

setup_logging(settings.log_level)
log = logging.getLogger("main")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app):
    # one shared LLM client for every browser session, kept warm in the background
    llm = LLMClient(settings)
    llm.start_warmup()
    app.state.llm = llm
    log.info("ready: LLM=%s STT=%s TTS=%s", settings.openai_model, settings.deepgram_model, settings.elevenlabs_model)
    yield
    await llm.close()
    log.info("shut down")


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.websocket("/ws/voice")
async def voice_socket(websocket: WebSocket):
    await websocket.accept()
    log.info("browser connected")
    session = VoiceSession(websocket, settings, websocket.app.state.llm)
    await session.run()
    log.info("browser disconnected")