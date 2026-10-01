"""Сборка контекста для модели и цикл генерации с инструментами."""

import asyncio
import datetime
import json
import logging
import zoneinfo
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from .aquarium import Aquarium
from .aquarium_brain import AquariumBrain
from .briefing import BriefingStore
from .config import Settings
from .db import Database, User
from .docs import PersonalDocs
from .kitchen import Kitchen
from .knowledge import KnowledgeBase
from .lang import LangStore
from .llm import LLMError, OllamaClient, ToolsNotSupported
from .memory import MemoryStore
from .modes import Mode, get_mode, style_instructions
from .routing import choose_model
from .services import ServiceError
from .timeparse import parse_reminder
from .web import WebError, WebTools

log = logging.getLogger(__name__)

WEB_TOOLS: list[dict[str, Any]] = [
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
REMINDER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "set_reminder",
        "description": "Поставить пользователю напоминание. Используй, когда просят напомнить "
                       "о чём-то в определённое время.",
        "parameters": {
            "type": "object",
            "properties": {
                "when": {"type": "string", "description": "Когда: 'YYYY-MM-DD HH:MM' в местном "
                                                          "времени пользователя или 'через 20 минут'"},
                "text": {"type": "string", "description": "О чём напомнить"},
            },
            "required": ["when", "text"],
        },
    },
}

PYTHON_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "run_python",
        "description": "Выполнить код Python 3 в изолированной песочнице без интернета. Используй для "
                       "точных вычислений, конвертации единиц, процентов, бюджета, статистики, дат и "
                       "графиков. Доступны numpy, pandas, matplotlib, sympy, scipy. Результат печатай "
                       "через print(); графики строй matplotlib — картинка уйдёт пользователю сама.",
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "Код Python"}},
            "required": ["code"],
        },
    },
}
PYTHON_HINT = (
    "Для любых вычислений, где важна точность (арифметика, проценты, единицы, даты, статистика), "
    "и для графиков вызывай run_python, а не считай в уме."
)

WEB_HINT = (
    "У тебя есть доступ в интернет через инструменты web_search и fetch_url. "
    "Пользуйся ими, когда нужна свежая или точная информация. "
    "Не выдумывай ссылки; указывай источники, если брал данные из интернета."
)
REMINDER_HINT = "Если просят напомнить о чём-то — вызови инструмент set_reminder."
SUMMARY_PROMPT = (
    "Сожми переписку в краткое резюме (до 12 пунктов): о чём говорили, к каким выводам пришли, "
    "какие решения, код, договорённости и открытые вопросы. Пиши на языке переписки, "
    "без вступлений. Если есть предыдущее резюме — объедини его с новыми сообщениями."
)
GROUP_HINT = (
    "Это групповой чат. Сообщения участников помечены как «[Имя]: текст». "
    "Отвечай тому, кто обратился к тебе последним."
)


@dataclass(frozen=True)
class Event:
    kind: Literal["text", "status"]
    value: str


@dataclass(frozen=True)
class Turn:
    """Кто и где спрашивает."""
    user_id: int
    chat_id: int
    user: User | None  # None — гость в разрешённой группе
    is_group: bool = False
    author: str = ""

    @property
    def registered(self) -> bool:
        return self.user is not None


def format_search_results(results) -> str:
    if not results:
        return "Ничего не найдено."
    return "\n\n".join(f"{i}. {r.title}\n{r.url}\n{r.snippet}" for i, r in enumerate(results, 1))


class Assistant:
    def __init__(
        self,
        settings: Settings,
        llm: OllamaClient,
        db: Database,
        memory: MemoryStore,
        knowledge: KnowledgeBase,
        web: WebTools | None,
        docs: PersonalDocs | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.db = db
        self.memory = memory
        self.knowledge = knowledge
        self.web = web
        self.docs = docs or PersonalDocs(db, llm, settings.embed_model)
        self.lang = LangStore(db)
        self.kitchen = Kitchen(db)
        self.briefing = BriefingStore(db)
        self.sandbox = None  # SandboxClient: инструмент run_python
        # Картинки, которые построил run_python во время ответа: chat_id -> PNG
        self.images: defaultdict[int, list[bytes]] = defaultdict(list)
        self.aquarium = Aquarium(db, self._zone(settings.aquarium_timezone or settings.timezone))
        self.aquarium_brain = AquariumBrain(db)
        self._no_tools: set[str] = set()  # модели, которые не умеют tool calling
        self._summary_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ------------------------------------------------------------ настройки запроса

    def tz(self, user: User | None) -> zoneinfo.ZoneInfo:
        return self._zone(user.tz if user else None, self.settings.timezone)

    @staticmethod
    def _zone(*names: str | None) -> zoneinfo.ZoneInfo:
        for name in (*names, "UTC"):
            if name:
                try:
                    return zoneinfo.ZoneInfo(name)
                except (zoneinfo.ZoneInfoNotFoundError, ValueError):
                    continue
        return zoneinfo.ZoneInfo("UTC")

    def mode_for(self, turn: Turn) -> Mode:
        return get_mode(turn.user.mode if turn.user else None, self.settings.default_mode)

    async def resolve_model(self, turn: Turn, text: str, mode: Mode | None = None) -> str:
        s = self.settings
        if turn.user and turn.user.model:
            return turn.user.model
        mode = mode or self.mode_for(turn)
        if mode.key == "lang":
            return s.tutor_model  # автовыбор отдал бы короткие реплики 3b-модели, а она путает языки
        if not s.auto_model:
            return s.default_model
        try:
            available = await self.llm.list_models(cached=True)
        except LLMError:
            available = None
        return choose_model(
            text, default=s.default_model, code=s.code_model, fast=s.fast_model,
            available=available, prefer_code=mode.prefer_code_model,
        ).model

    def options_for(self, turn: Turn) -> dict[str, Any]:
        if turn.user and turn.user.temperature is not None:
            return {"temperature": turn.user.temperature}
        return {}

    # ------------------------------------------------------------ контекст

    async def _safe(self, coro, what: str):
        try:
            return await coro
        except LLMError as exc:
            log.warning("%s failed: %s", what, exc)
            return []

    async def build_messages(
        self,
        turn: Turn,
        mode: Mode,
        user_text: str,
        *,
        images: list[str] | None = None,
        with_tools_hint: bool = True,
    ) -> list[dict[str, Any]]:
        s = self.settings
        now = datetime.datetime.now(self.tz(turn.user))
        parts = [mode.prompt]
        if turn.user and (style := style_instructions(turn.user.persona, turn.user.length)):
            parts.append(style)
        parts.append(f"Сейчас {now:%Y-%m-%d %H:%M} ({now.tzinfo}), {_weekday(now)}.")
        if turn.is_group:
            parts.append(GROUP_HINT)

        if turn.registered:
            facts = await self._safe(
                self.memory.search(turn.user_id, user_text, s.memory_top_k, s.memory_min_score), "memory"
            )
            if facts:
                parts.append("Что ты знаешь о собеседнике (из прошлых разговоров):\n"
                             + "\n".join(f"- {f}" for f in facts))

        if summary := await self.db.get_summary(turn.chat_id):
            parts.append(f"Краткое содержание более ранней части разговора:\n{summary}")

        if mode.use_knowledge:
            passages = await self._safe(
                self.knowledge.search(user_text, s.knowledge_top_k, s.knowledge_min_score), "knowledge"
            )
            if passages:
                parts.append("Выдержки из базы знаний (в скобках — источник):\n\n"
                             + "\n\n".join(f"[{p.source}]\n{p.text}" for p in passages))

        if turn.registered:
            doc_passages = await self._safe(
                self.docs.search(turn.user_id, user_text, s.docs_top_k, s.docs_min_score), "docs"
            )
            if doc_passages:
                parts.append("Выдержки из личных документов пользователя (в скобках — файл):\n\n"
                             + "\n\n".join(f"[{p.source}]\n{p.text}" for p in doc_passages))

        if mode.key == "lang" and turn.registered:
            parts.append(await self.lang.context_for(turn.user_id))

        if mode.key == "aquarium":
            today = self.aquarium.now().date()
            parts.append(await self.aquarium_brain.context(
                self.aquarium, await self.aquarium.care_summary(today)))

        if mode.key == "defense":
            state = await self.db.get_state(turn.chat_id)
            if state.defense_code:
                parts.append(f"Код студента на защите:\n```\n{state.defense_code}\n```")

        if with_tools_hint:
            if self.web is not None:
                parts.append(WEB_HINT)
            if self.sandbox is not None:
                parts.append(PYTHON_HINT)
            if turn.registered:
                parts.append(REMINDER_HINT)

        history = await self.db.get_history(turn.chat_id, s.history_limit)
        user_msg: dict[str, Any] = {"role": "user", "content": user_text}
        if images:
            user_msg["images"] = images
        return [{"role": "system", "content": "\n\n".join(parts)}, *history, user_msg]

    def tools_for(self, turn: Turn) -> list[dict[str, Any]]:
        tools = list(WEB_TOOLS) if self.web is not None else []
        if self.sandbox is not None:
            tools.append(PYTHON_TOOL)
        if turn.registered:
            tools.append(REMINDER_TOOL)
        return tools

    # ------------------------------------------------------------ генерация

    async def run(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        turn: Turn | None = None,
        allow_tools: bool = True,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[Event]:
        """Генерирует ответ. Если модель вызывает инструменты — выполняет их и продолжает."""
        available_tools = self.tools_for(turn) if turn else (list(WEB_TOOLS) if self.web else [])
        use_tools = allow_tools and bool(available_tools) and model not in self._no_tools
        steps = self.settings.max_tool_steps

        for step in range(steps + 1):
            tools = available_tools if use_tools and step < steps else None
            content = ""
            calls: list[dict[str, Any]] = []
            try:
                async for chunk in self.llm.chat_stream(model, messages, tools=tools, options=options):
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
                async for chunk in self.llm.chat_stream(model, messages, options=options):
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
                result = await self._exec_tool(name, args, turn)
                messages.append({"role": "tool", "tool_name": name, "content": result})

    async def _exec_tool(self, name: str, args: dict[str, Any], turn: Turn | None) -> str:
        try:
            if name == "set_reminder":
                return await self._tool_reminder(args, turn)
            if name == "run_python":
                return await self._tool_python(args, turn)
            if self.web is None:
                return "Ошибка: интернет выключен"
            if name == "web_search":
                return format_search_results(await self.web.search(str(args.get("query", ""))))
            if name == "fetch_url":
                title, text = await self.web.fetch(str(args.get("url", "")))
                return f"{title}\n\n{text}" if title else text
        except (WebError, ServiceError) as exc:
            return f"Ошибка: {exc}"
        return f"Ошибка: неизвестный инструмент {name}"

    async def _tool_python(self, args: dict[str, Any], turn: Turn | None) -> str:
        if self.sandbox is None:
            return "Ошибка: песочница выключена"
        code = str(args.get("code", "")).strip()
        if not code:
            return "Ошибка: пустой код"
        result = await self.sandbox.python(code)
        if turn is not None and result.images:
            self.images[turn.chat_id].extend(result.images)
        return result.as_text()

    async def _tool_reminder(self, args: dict[str, Any], turn: Turn | None) -> str:
        if turn is None or not turn.registered:
            return "Ошибка: напоминания доступны только зарегистрированным пользователям"
        when, text = str(args.get("when", "")).strip(), str(args.get("text", "")).strip()
        if not text:
            return "Ошибка: не указан текст напоминания"
        tz = self.tz(turn.user)
        now = datetime.datetime.now(tz)
        due = None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                due = datetime.datetime.strptime(when, fmt).replace(tzinfo=tz)
                break
            except ValueError:
                continue
        if due is None and (parsed := parse_reminder(f"{when} {text}", now)):
            due = parsed.due
        if due is None:
            return f"Ошибка: не понял время «{when}». Попроси пользователя уточнить."
        if due <= now:
            return "Ошибка: это время уже прошло"
        rid = await self.db.add_reminder(turn.user_id, turn.chat_id, text, due)
        return f"Готово: напоминание #{rid} на {due:%Y-%m-%d %H:%M} ({tz}) — «{text}»"

    # ------------------------------------------------------------ фоновые задачи

    async def summarize_if_needed(self, chat_id: int) -> bool:
        """Сворачивает старую часть диалога в резюме, чтобы не терять контекст."""
        s = self.settings
        async with self._summary_locks[chat_id]:
            rows = await self.db.messages_to_summarize(chat_id, keep=s.history_limit)
            if len(rows) < s.summary_batch:
                return False
            previous = await self.db.get_summary(chat_id)
            dialog = "\n".join(
                f"{'Пользователь' if r.role == 'user' else 'Ассистент'}: {r.content[:1500]}" for r in rows
            )
            prompt = (f"Предыдущее резюме:\n{previous}\n\n" if previous else "") + f"Новые сообщения:\n{dialog}"
            try:
                summary = await self.llm.chat(s.helper_model, [
                    {"role": "system", "content": SUMMARY_PROMPT},
                    {"role": "user", "content": prompt[:24000]},
                ])
            except LLMError as exc:
                log.warning("summary failed: %s", exc)
                return False
            summary = summary.strip()
            if not summary:
                return False
            await self.db.set_summary(chat_id, summary[:4000], rows[-1].id)
            log.info("chat %s: summarized %d messages", chat_id, len(rows))
            return True

    async def after_reply(self, turn: Turn, user_text: str, *, extract_memory: bool = True) -> None:
        if extract_memory and turn.registered and self.settings.memory_auto:
            await self.memory.extract_and_store(turn.user_id, user_text, self.settings.helper_model)
        if (extract_memory and self.mode_for(turn).key == "aquarium"
                and turn.user_id in self.settings.aquarium_members):
            # Аквариумист запоминает из разговора то, что узнал об аквариуме
            await self.aquarium_brain.learn(self.llm, self.settings.default_model, user_text,
                                            await self.aquarium.tanks(), source="chat")
        await self.summarize_if_needed(turn.chat_id)

    async def translate_to_english(self, text: str) -> str:
        """Для генерации картинок: SDXL лучше понимает английский."""
        if text.isascii():
            return text
        try:
            out = await self.llm.chat(self.settings.helper_model, [
                {"role": "system", "content": "Translate the image description to English. "
                                              "Output only the translation, no quotes."},
                {"role": "user", "content": text},
            ])
        except LLMError:
            return text
        return out.strip().strip('"') or text


_WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _weekday(dt: datetime.datetime) -> str:
    return _WEEKDAYS_RU[dt.weekday()]


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
    if name == "set_reminder":
        return f"⏰ Ставлю напоминание: {args.get('text', '')}"
    if name == "run_python":
        return "🐍 Считаю на Python…"
    return f"🛠 {name}"
