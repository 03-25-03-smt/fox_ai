"""Сборка контекста для модели (режим, память, база знаний) и цикл с инструментами."""

import datetime
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from .config import Settings
from .db import Database
from .knowledge import KnowledgeBase
from .llm import LLMError, OllamaClient, ToolsNotSupported
from .memory import MemoryStore
from .modes import Mode
from .web import WebError, WebTools

log = logging.getLogger(__name__)

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Поиск в интернете. Используй для свежей информации, новостей, "
                           "документации, цен, версий программ и всего, чего ты не знаешь точно.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Поисковый запрос"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Открыть веб-страницу и прочитать её текст.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "Полный URL http(s)"}},
                "required": ["url"],
            },
        },
    },
]

WEB_HINT = (
    "\n\nУ тебя есть доступ в интернет через инструменты web_search и fetch_url. "
    "Пользуйся ими, когда нужна свежая или точная информация. "
    "Не выдумывай ссылки; указывай источники, если брал данные из интернета."
)


@dataclass(frozen=True)
class Event:
    kind: Literal["text", "status"]
    value: str


def format_search_results(results) -> str:
    if not results:
        return "Ничего не найдено."
    return "\n\n".join(
        f"{i}. {r.title}\n{r.url}\n{r.snippet}" for i, r in enumerate(results, 1)
    )


class Assistant:
    def __init__(
        self,
        settings: Settings,
        llm: OllamaClient,
        db: Database,
        memory: MemoryStore,
        knowledge: KnowledgeBase,
        web: WebTools | None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.db = db
        self.memory = memory
        self.knowledge = knowledge
        self.web = web
        self._no_tools: set[str] = set()  # модели, которые не умеют tool calling

    async def build_messages(self, user_id: int, mode: Mode, user_text: str) -> list[dict[str, Any]]:
        s = self.settings
        system = mode.prompt + f"\n\nСегодня {datetime.date.today().isoformat()}."

        try:
            facts = await self.memory.search(user_id, user_text, s.memory_top_k, s.memory_min_score)
        except LLMError as exc:
            log.warning("memory search failed: %s", exc)
            facts = []
        if facts:
            system += "\n\nЧто ты знаешь о собеседнике (из прошлых разговоров):\n" + "\n".join(
                f"- {f}" for f in facts
            )

        if mode.use_knowledge:
            try:
                passages = await self.knowledge.search(
                    user_text, s.knowledge_top_k, s.knowledge_min_score
                )
            except LLMError as exc:
                log.warning("knowledge search failed: %s", exc)
                passages = []
            if passages:
                system += "\n\nВыдержки из базы знаний (в скобках — источник):\n\n" + "\n\n".join(
                    f"[{p.source}]\n{p.text}" for p in passages
                )

        if self.web is not None:
            system += WEB_HINT

        history = await self.db.get_history(user_id, s.history_limit)
        return [
            {"role": "system", "content": system},
            *history,
            {"role": "user", "content": user_text},
        ]

    async def run(
        self, model: str, messages: list[dict[str, Any]], allow_tools: bool = True
    ) -> AsyncIterator[Event]:
        """Генерирует ответ. Если модель вызывает инструменты — выполняет их и продолжает."""
        use_tools = allow_tools and self.web is not None and model not in self._no_tools
        steps = self.settings.max_tool_steps

        for step in range(steps + 1):
            tools = TOOLS if use_tools and step < steps else None
            content = ""
            calls: list[dict[str, Any]] = []
            try:
                async for chunk in self.llm.chat_stream(model, messages, tools=tools):
                    if chunk.content:
                        content += chunk.content
                        yield Event("text", chunk.content)
                    calls.extend(chunk.tool_calls)
            except ToolsNotSupported:
                if content:
                    raise
                log.info("model %s does not support tools, disabling", model)
                self._no_tools.add(model)
                use_tools = False
                async for chunk in self.llm.chat_stream(model, messages):
                    if chunk.content:
                        yield Event("text", chunk.content)
                return

            if not calls:
                return

            messages.append({"role": "assistant", "content": content, "tool_calls": calls})
            for call in calls:
                fn = call.get("function", {})
                name, args = fn.get("name", ""), _parse_args(fn.get("arguments"))
                yield Event("status", _status_text(name, args))
                result = await self._exec_tool(name, args)
                messages.append({"role": "tool", "tool_name": name, "content": result})

    async def _exec_tool(self, name: str, args: dict[str, Any]) -> str:
        if self.web is None:
            return "Ошибка: интернет выключен"
        try:
            if name == "web_search":
                return format_search_results(await self.web.search(str(args.get("query", ""))))
            if name == "fetch_url":
                title, text = await self.web.fetch(str(args.get("url", "")))
                return f"{title}\n\n{text}" if title else text
        except WebError as exc:
            return f"Ошибка: {exc}"
        return f"Ошибка: неизвестный инструмент {name}"


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _status_text(name: str, args: dict[str, Any]) -> str:
    if name == "web_search":
        return f"🔎 Ищу: {args.get('query', '')}"
    if name == "fetch_url":
        return f"🌐 Читаю: {args.get('url', '')}"
    return f"🛠 {name}"
