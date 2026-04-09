"""
Voice transcription for Materia using faster-whisper.

The model is loaded lazily on first use to avoid startup RAM cost.
Uses the 'tiny' model (~77MB, ~300MB RAM) — lightweight enough for a 2GB LXC.
"""
import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_model = None


def _get_model():
    global _model
    if _model is None:
        logger.info("[Transcribe] Loading Whisper tiny model...")
        from faster_whisper import WhisperModel
        _model = WhisperModel("tiny", device="cpu", compute_type="int8")
        logger.info("[Transcribe] Whisper model loaded.")
    return _model


def _transcribe_sync(file_path: str) -> str:
    """Blocking transcription — run in an executor to keep async loop free."""
    model = _get_model()
    segments, _info = model.transcribe(file_path, language="en", beam_size=1)
    text = " ".join(s.text.strip() for s in segments).strip()
    return text or "(no speech detected)"


async def transcribe_file(file_path: str) -> str:
    """Transcribe a voice file asynchronously. Returns the transcribed text."""
    loop = asyncio.get_event_loop()
    try:
        text = await loop.run_in_executor(None, _transcribe_sync, file_path)
        logger.info(f"[Transcribe] Result: {text[:80]}")
        return text
    except Exception as e:
        logger.error(f"[Transcribe] Failed: {e}")
        return f"(transcription failed: {e})"
