"""Клиент для Ollama HTTP API."""

import json
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

EMBED_BATCH = 32
MODELS_CACHE_TTL = 60.0


class LLMError(Exception):
    """Ошибка при обращении к модели."""


class ToolsNotSupported(LLMError):
    """Модель не умеет вызывать инструменты (tool calling)."""


@dataclass
class StreamChunk:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class SpeedSample:
    model: str
    tokens: int
    tokens_per_sec: float
    at: float


@dataclass(frozen=True)
class LoadedModel:
    name: str
    size_vram: int
    size: int


class OllamaClient:
    def __init__(self, base_url: str, timeout: float = 600.0, num_ctx: int | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=10.0),
        )
        # Размер контекста: по умолчанию у Ollama он маленький, а у нас
        # системный промпт + память + база знаний + история.
        self._base_options: dict[str, Any] = {"num_ctx": num_ctx} if num_ctx else {}
        self.speeds: deque[SpeedSample] = deque(maxlen=50)
        self._models_cache: tuple[float, list[str]] | None = None

    async def close(self) -> None:
        await self._client.aclose()

    def _options(self, extra: dict[str, Any] | None) -> dict[str, Any]:
        opts = dict(self._base_options)
        if extra:
            opts.update({k: v for k, v in extra.items() if v is not None})
        return opts

    async def list_models(self, cached: bool = False) -> list[str]:
        if cached and self._models_cache and time.monotonic() - self._models_cache[0] < MODELS_CACHE_TTL:
            return self._models_cache[1]
        data = await self._get("/api/tags")
        models = sorted(m["name"] for m in data.get("models", []))
        self._models_cache = (time.monotonic(), models)
        return models

    async def loaded_models(self) -> list[LoadedModel]:
        data = await self._get("/api/ps")
        return [
            LoadedModel(m.get("name", "?"), int(m.get("size_vram", 0)), int(m.get("size", 0)))
            for m in data.get("models", [])
        ]

    async def unload(self, model: str) -> None:
        """Выгрузить модель из видеопамяти (keep_alive=0)."""
        await self._post("/api/generate", {"model": model, "keep_alive": 0})

    async def unload_all(self) -> list[str]:
        names = [m.name for m in await self.loaded_models()]
        for name in names:
            await self.unload(name)
        return names

    async def pull(self, model: str) -> AsyncIterator[dict[str, Any]]:
        """Скачивает модель, отдаёт события прогресса Ollama: {status, total, completed}."""
        try:
            async with self._client.stream("POST", "/api/pull", json={"model": model, "stream": True},
                                           timeout=httpx.Timeout(None, connect=10.0)) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="replace")
                    raise LLMError(_extract_error(body) or f"HTTP {resp.status_code}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if "error" in data:
                        raise LLMError(str(data["error"]))
                    yield data
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama недоступна: {exc}") from exc
        self._models_cache = None

    async def delete(self, model: str) -> None:
        try:
            resp = await self._client.request("DELETE", "/api/delete", json={"model": model})
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama недоступна: {exc}") from exc
        if resp.status_code != 200:
            raise LLMError(_extract_error(resp.text) or f"HTTP {resp.status_code}")
        self._models_cache = None

    async def bench(self, model: str, prompt: str, num_predict: int = 200) -> dict[str, float]:
        """Один прогон без стрима: скорость генерации, чтения промпта и загрузки модели."""
        data = await self._post("/api/generate", {
            "model": model, "prompt": prompt, "stream": False,
            "options": {**self._options(None), "num_predict": num_predict, "temperature": 0},
        })
        self._record_speed(model, data)

        def rate(count: str, duration: str) -> float:
            c, d = data.get(count) or 0, data.get(duration) or 0
            return c / (d / 1e9) if d else 0.0

        return {
            "gen_tps": rate("eval_count", "eval_duration"),
            "prompt_tps": rate("prompt_eval_count", "prompt_eval_duration"),
            "load_s": (data.get("load_duration") or 0) / 1e9,
            "total_s": (data.get("total_duration") or 0) / 1e9,
            "tokens": float(data.get("eval_count") or 0),
        }

    async def chat_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Отдаёт ответ модели по кусочкам по мере генерации."""
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        if tools:
            payload["tools"] = tools
        if opts := self._options(options):
            payload["options"] = opts
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="replace")
                    raise _make_error(_extract_error(body) or f"HTTP {resp.status_code}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if "error" in data:
                        raise _make_error(str(data["error"]))
                    msg = data.get("message", {})
                    chunk = StreamChunk(msg.get("content", ""), msg.get("tool_calls") or [])
                    if chunk.content or chunk.tool_calls:
                        yield chunk
                    if data.get("done"):
                        self._record_speed(model, data)
                        break
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama недоступна: {exc}") from exc

    def _record_speed(self, model: str, data: dict[str, Any]) -> None:
        count, duration = data.get("eval_count"), data.get("eval_duration")
        if count and duration:
            self.speeds.append(SpeedSample(model, int(count), count / (duration / 1e9), time.time()))

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        json_mode: bool = False,
        options: dict[str, Any] | None = None,
    ) -> str:
        """Ответ целиком, без стрима. json_mode=True заставляет модель вернуть JSON."""
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if json_mode:
            payload["format"] = "json"
        if opts := self._options(options):
            payload["options"] = opts
        data = await self._post("/api/chat", payload)
        self._record_speed(model, data)
        return data.get("message", {}).get("content", "")

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        result: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH):
            data = await self._post("/api/embed", {"model": model, "input": texts[i : i + EMBED_BATCH]})
            result.extend(data.get("embeddings", []))
        if len(result) != len(texts):
            raise LLMError("Ollama вернула не все эмбеддинги")
        return result

    async def _get(self, path: str) -> dict[str, Any]:
        try:
            resp = await self._client.get(path)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama недоступна: {exc}") from exc
        return resp.json()

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama недоступна: {exc}") from exc
        if resp.status_code != 200:
            raise _make_error(_extract_error(resp.text) or f"HTTP {resp.status_code}")
        return resp.json()


def _make_error(message: str) -> LLMError:
    if "does not support tools" in message:
        return ToolsNotSupported(message)
    return LLMError(message)


def _extract_error(body: str) -> str | None:
    try:
        return str(json.loads(body).get("error")) or None
    except (ValueError, AttributeError):
        return body.strip() or None
