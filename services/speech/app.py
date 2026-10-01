"""Fox AI speech: распознавание речи (faster-whisper) и синтез (Piper) -> OGG/Opus.

Модель Whisper грузится при первом запросе и выгружается после простоя,
чтобы освобождать видеопамять для других моделей на той же карте.
"""

import asyncio
import gc
import logging
import os
import re
import subprocess
import tempfile
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

log = logging.getLogger("speech")

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cuda")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE", "float16")
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE") or None  # None = автоопределение
IDLE_UNLOAD = float(os.environ.get("IDLE_UNLOAD_SECONDS", "600"))
VOICES_DIR = Path(os.environ.get("PIPER_VOICES_DIR", "/opt/voices"))
# Голос Piper для каждого языка: русский — ответы бота, немецкий и чешский — учитель языков
VOICES = {
    "ru": os.environ.get("PIPER_VOICE", "ru_RU-irina-medium"),
    "de": os.environ.get("PIPER_VOICE_DE", "de_DE-thorsten-medium"),
    "cs": os.environ.get("PIPER_VOICE_CS", "cs_CZ-jirka-medium"),
}
STT_LANGUAGES = {"ru", "de", "cs", "en", "uk"}
MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_TTS_CHARS = 2000

_lock = asyncio.Lock()
_whisper = None
_voices: dict[str, object] = {}
_last_used = 0.0


def _load_whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel

        log.info("loading whisper %s on %s (%s)", WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE)
        _whisper = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
    return _whisper


def _voice_path(lang: str) -> Path:
    name = VOICES[lang]
    return Path(name) if name.endswith(".onnx") else VOICES_DIR / f"{name}.onnx"


def _load_voice(lang: str):
    if lang not in _voices:
        from piper import PiperVoice

        _voices[lang] = PiperVoice.load(str(_voice_path(lang)))
    return _voices[lang]


def _transcribe(path: str, language: str | None = None) -> tuple[str, str]:
    model = _load_whisper()
    segments, info = model.transcribe(
        path, language=language or WHISPER_LANGUAGE, vad_filter=True, beam_size=5
    )
    text = " ".join(s.text.strip() for s in segments).strip()
    return text, info.language


def clean_for_speech(text: str) -> str:
    """Убирает то, что бессмысленно читать вслух: блоки кода, ссылки, markdown."""
    text = re.sub(r"```.*?(```|$)", " (код смотри в сообщении) ", text, flags=re.DOTALL)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_`#>|]", "", text)
    return re.sub(r"\s+", " ", text).strip()[:MAX_TTS_CHARS]


def _synthesize(text: str, lang: str = "ru") -> bytes:
    voice = _load_voice(lang)
    with tempfile.TemporaryDirectory() as tmp:
        wav_path, ogg_path = Path(tmp) / "out.wav", Path(tmp) / "out.ogg"
        with wave.open(str(wav_path), "wb") as wav:
            voice.synthesize_wav(text, wav)
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(wav_path),
             "-c:a", "libopus", "-b:a", "32k", "-application", "voip", str(ogg_path)],
            check=True, timeout=120,
        )
        return ogg_path.read_bytes()


async def _idle_unloader() -> None:
    global _whisper
    while True:
        await asyncio.sleep(30)
        if _whisper is not None and time.monotonic() - _last_used > IDLE_UNLOAD:
            async with _lock:
                log.info("unloading whisper after idle")
                _whisper = None
                gc.collect()


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(level=logging.INFO)
    task = asyncio.create_task(_idle_unloader())
    yield
    task.cancel()


app = FastAPI(title="fox_ai speech", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "ok": True,
        "whisper_loaded": _whisper is not None,
        "voices": {lang: _voice_path(lang).exists() for lang in VOICES},
    }


@app.post("/stt")
async def stt(file: UploadFile, language: str | None = None) -> dict[str, str]:
    """language — код языка (de, cs…), если он известен заранее; иначе автоопределение."""
    global _last_used
    if language is not None and language not in STT_LANGUAGES:
        raise HTTPException(400, f"Язык {language} не поддерживается")
    data = await file.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "Аудио больше 25 МБ")
    suffix = Path(file.filename or "audio.ogg").suffix or ".ogg"
    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        tmp.write(data)
        tmp.flush()
        async with _lock:
            try:
                text, language = await asyncio.to_thread(_transcribe, tmp.name, language)
            except Exception as exc:
                log.exception("transcription failed")
                raise HTTPException(500, f"Не удалось распознать: {exc}") from exc
            _last_used = time.monotonic()
    return {"text": text, "language": language}


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    lang: str = "ru"


@app.post("/tts")
async def tts(req: TTSRequest) -> Response:
    if req.lang not in VOICES:
        raise HTTPException(400, f"Нет голоса для языка {req.lang}")
    text = clean_for_speech(req.text)
    if not text:
        raise HTTPException(400, "Нечего озвучивать")
    try:
        audio = await asyncio.to_thread(_synthesize, text, req.lang)
    except Exception as exc:
        log.exception("tts failed")
        raise HTTPException(500, f"Не удалось озвучить: {exc}") from exc
    return Response(audio, media_type="audio/ogg")
