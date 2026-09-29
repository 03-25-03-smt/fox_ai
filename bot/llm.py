"""Клиент для Ollama HTTP API."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

EMBED_BATCH = 32


class LLMError(Exception):
    """Ошибка при обращении к модели."""


class ToolsNotSupported(LLMError):
    """Модель не умеет вызывать инструменты (tool calling)."""


@dataclass
class StreamChunk:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class OllamaClient:
    def __init__(self, base_url: str, timeout: float = 600.0, num_ctx: int | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=10.0),
        )
        # Размер контекста: по умолчанию у Ollama он маленький, а у нас
        # системный промпт + память + база знаний + история.
        self._options: dict[str, Any] = {"num_ctx": num_ctx} if num_ctx else {}

    async def close(self) -> None:
        await self._client.aclose()

    async def list_models(self) -> list[str]:
        try:
            resp = await self._client.get("/api/tags")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama недоступна: {exc}") from exc
        return sorted(m["name"] for m in resp.json().get("models", []))

    async def chat_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Отдаёт ответ модели по кусочкам по мере генерации."""
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        if tools:
            payload["tools"] = tools
        if self._options:
            payload["options"] = self._options
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
                        break
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama недоступна: {exc}") from exc

    async def chat(
        self, model: str, messages: list[dict[str, Any]], json_mode: bool = False
    ) -> str:
        """Ответ целиком, без стрима. json_mode=True заставляет модель вернуть JSON."""
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if json_mode:
            payload["format"] = "json"
        if self._options:
            payload["options"] = self._options
        data = await self._post("/api/chat", payload)
        return data.get("message", {}).get("content", "")

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        result: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH):
            data = await self._post("/api/embed", {"model": model, "input": texts[i : i + EMBED_BATCH]})
            result.extend(data.get("embeddings", []))
        if len(result) != len(texts):
            raise LLMError("Ollama вернула не все эмбеддинги")
        return result

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
