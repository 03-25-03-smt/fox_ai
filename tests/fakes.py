"""Общие фейки для тестов: LLM без GPU и веб без сети."""

import hashlib
import re

from bot.llm import StreamChunk, ToolsNotSupported
from bot.web import SearchResult

DIM = 64


def fake_vector(text: str) -> list[float]:
    """Bag-of-words эмбеддинг: тексты с общими словами получаются похожими."""
    vec = [0.0] * DIM
    for word in re.findall(r"\w+", text.lower()):
        h = int(hashlib.md5(word.encode()).hexdigest(), 16)
        vec[h % DIM] += 1.0
    return vec


class FakeLLM:
    def __init__(self, reply: str = "Ответ с кодом:\n```c\nint main(void);\n```") -> None:
        self.reply = reply
        self.calls: list[dict] = []
        # Сценарий инструментов: список списков tool_calls по шагам
        self.tool_script: list[list[dict]] = []
        self.supports_tools = True
        self.facts_json = '{"facts": []}'

    async def list_models(self) -> list[str]:
        return ["bge-m3:latest", "llama3.1:8b", "qwen2.5:7b"]

    async def chat_stream(self, model, messages, tools=None):
        self.calls.append({"model": model, "messages": [dict(m) for m in messages], "tools": tools})
        if tools and not self.supports_tools:
            raise ToolsNotSupported("model does not support tools")
        if tools and self.tool_script:
            yield StreamChunk(tool_calls=self.tool_script.pop(0))
            return
        for part in self.reply.split(" "):
            yield StreamChunk(part + " ")

    async def chat(self, model, messages, json_mode=False):
        return self.facts_json

    async def embed(self, model, texts):
        return [fake_vector(t) for t in texts]


class FakeWeb:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.fetched: list[str] = []

    async def search(self, query: str, limit: int = 6):
        self.queries.append(query)
        return [SearchResult("Ollama release", "https://example.com/ollama", "Ollama 0.34 released")]

    async def fetch(self, url: str):
        self.fetched.append(url)
        return "Page title", "Page body text"

    async def close(self) -> None:
        pass
