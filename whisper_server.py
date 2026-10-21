"""Minimal speech-to-text HTTP service built on faster-whisper.

POST /transcribe  (multipart/form-data)
    file      audio file (any format ffmpeg understands)
    model     optional Whisper model size (tiny, base, small, medium, large-v2, large-v3)
    language  optional ISO code; omit or "auto" to auto-detect
returns {"text": ..., "language": ..., "language_probability": ...}

Environment:
    WHISPER_MODEL          model loaded at startup (default: base)
    WHISPER_DEVICE         auto | cuda | cpu (default: auto)
    WHISPER_COMPUTE_TYPE   e.g. int8, int8_float16, float16 (default: int8)
    WHISPER_ALLOWED_MODELS comma-separated list of models clients may request
    WHISPER_BEAM_SIZE      default: 5
"""

import os
import subprocess
import tempfile
import threading
from contextlib import asynccontextmanager
from typing import Optional

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from faster_whisper import WhisperModel
from starlette.concurrency import run_in_threadpool

DEFAULT_MODEL = os.getenv("WHISPER_MODEL", "base")
DEVICE = os.getenv("WHISPER_DEVICE", "auto")
COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
BEAM_SIZE = int(os.getenv("WHISPER_BEAM_SIZE", "5"))
ALLOWED_MODELS = {m.strip() for m in os.getenv(
    "WHISPER_ALLOWED_MODELS", "tiny,base,small,medium,large-v2,large-v3").split(",") if m.strip()}

_models: dict = {}
_lock = threading.Lock()


def get_model(name: str) -> WhisperModel:
    with _lock:
        if name not in _models:
            print(f"[whisper] loading model {name} (device={DEVICE}, compute_type={COMPUTE_TYPE})", flush=True)
            _models[name] = WhisperModel(name, device=DEVICE, compute_type=COMPUTE_TYPE)
            print(f"[whisper] model {name} ready", flush=True)
        return _models[name]


@asynccontextmanager
async def lifespan(_app):
    await run_in_threadpool(get_model, DEFAULT_MODEL)
    yield


app = FastAPI(title="Whisper speech-to-text", lifespan=lifespan)


def _decode(path: str) -> np.ndarray:
    """Decode any audio format to 16 kHz mono float32 with ffmpeg."""
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True, check=True).stdout
    return np.frombuffer(out, np.int16).astype(np.float32) / 32768.0


def _transcribe(name: str, path: str, language: Optional[str]) -> dict:
    try:
        audio = _decode(path)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=400, detail="could not decode audio: " + e.stderr.decode(errors="replace")[-300:])
    segments, info = get_model(name).transcribe(audio, beam_size=BEAM_SIZE, language=language)
    text = " ".join(s.text.strip() for s in segments)
    return {
        "language": info.language,
        "language_probability": round(info.language_probability, 4),
        "text": text.strip(),
    }


@app.get("/health")
def health():
    return {"status": "ok", "loaded_models": sorted(_models)}


@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    model: Optional[str] = Form(None),
    language: Optional[str] = Form(None),
):
    name = model or DEFAULT_MODEL
    if name not in ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail=f"model not allowed: {name}")

    suffix = os.path.splitext(file.filename or "")[1] or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        while chunk := await file.read(1024 * 1024):
            tmp.write(chunk)
        tmp_path = tmp.name
    try:
        lang = language if language and language != "auto" else None
        return await run_in_threadpool(_transcribe, name, tmp_path, lang)
    finally:
        os.remove(tmp_path)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "9000")))
