"""Fox AI imagegen: генерация картинок через diffusers (по умолчанию SDXL-Turbo).

Пайплайн грузится при первом запросе и выгружается после простоя, чтобы
не держать видеопамять RTX 3070 занятой постоянно.
"""

import asyncio
import gc
import io
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

log = logging.getLogger("imagegen")

MODEL = os.environ.get("IMAGEGEN_MODEL", "stabilityai/sdxl-turbo")
STEPS = int(os.environ.get("IMAGEGEN_STEPS", "4"))
GUIDANCE = float(os.environ.get("IMAGEGEN_GUIDANCE", "0.0"))
SIZE = int(os.environ.get("IMAGEGEN_SIZE", "768"))
OFFLOAD = os.environ.get("IMAGEGEN_CPU_OFFLOAD", "1") == "1"  # экономит VRAM на 8 ГБ картах
DEVICE = os.environ.get("IMAGEGEN_DEVICE", "cuda")  # cpu — только для проверки, очень медленно
VARIANT = os.environ.get("IMAGEGEN_VARIANT", "fp16") or None
IDLE_UNLOAD = float(os.environ.get("IDLE_UNLOAD_SECONDS", "600"))

_lock = asyncio.Lock()
_pipe = None
_last_used = 0.0


def _load():
    global _pipe
    if _pipe is None:
        import torch
        from diffusers import AutoPipelineForText2Image

        log.info("loading %s on %s (offload=%s)", MODEL, DEVICE, OFFLOAD)
        dtype = torch.float32 if DEVICE == "cpu" else torch.float16
        pipe = AutoPipelineForText2Image.from_pretrained(MODEL, torch_dtype=dtype, variant=VARIANT)
        if DEVICE == "cpu":
            pipe.to("cpu")
        elif OFFLOAD:
            pipe.enable_model_cpu_offload()
        else:
            pipe.to(DEVICE)
        _pipe = pipe
    return _pipe


def _unload() -> None:
    global _pipe
    _pipe = None
    gc.collect()
    try:
        import torch

        torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001 — torch может быть без CUDA
        pass


def _generate(prompt: str, negative: str | None, seed: int | None, steps: int, size: int) -> bytes:
    import torch

    pipe = _load()
    generator = torch.Generator("cpu").manual_seed(seed) if seed is not None else None
    image = pipe(
        prompt=prompt,
        negative_prompt=negative or None,
        num_inference_steps=steps,
        guidance_scale=GUIDANCE,
        width=size,
        height=size,
        generator=generator,
    ).images[0]
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


async def _idle_unloader() -> None:
    while True:
        await asyncio.sleep(30)
        if _pipe is not None and time.monotonic() - _last_used > IDLE_UNLOAD:
            async with _lock:
                log.info("unloading pipeline after idle")
                _unload()


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(level=logging.INFO)
    task = asyncio.create_task(_idle_unloader())
    yield
    task.cancel()


app = FastAPI(title="fox_ai imagegen", lifespan=lifespan)


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1000)
    negative_prompt: str | None = Field(None, max_length=500)
    seed: int | None = None
    steps: int | None = Field(None, ge=1, le=50)
    size: int | None = Field(None, ge=256, le=1024)


@app.get("/health")
async def health() -> dict[str, object]:
    return {"ok": True, "model": MODEL, "loaded": _pipe is not None}


@app.post("/unload")
async def unload() -> dict[str, bool]:
    async with _lock:
        was_loaded = _pipe is not None
        _unload()
    return {"unloaded": was_loaded}


@app.post("/generate")
async def generate(req: GenerateRequest) -> Response:
    global _last_used
    size = (req.size or SIZE) // 8 * 8
    async with _lock:
        try:
            png = await asyncio.to_thread(
                _generate, req.prompt, req.negative_prompt, req.seed, req.steps or STEPS, size
            )
        except Exception as exc:
            log.exception("generation failed")
            raise HTTPException(500, f"Не удалось сгенерировать: {exc}") from exc
        _last_used = time.monotonic()
    return Response(png, media_type="image/png")
