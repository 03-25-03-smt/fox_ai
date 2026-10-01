"""Контейнер сервисов бота + очередь на GPU и дневные лимиты."""

import asyncio
import datetime
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from .assistant import Assistant
from .briefing import WeatherClient
from .config import Settings
from .db import Database
from .intra import IntraClient
from .services import ImageClient, SandboxClient, SpeechClient


class GpuQueue:
    """Ограничивает число одновременных генераций; считает, сколько ждут."""

    def __init__(self, slots: int) -> None:
        self._sem = asyncio.Semaphore(max(slots, 1))
        self.slots = max(slots, 1)
        self.waiting = 0
        self.active = 0

    @property
    def busy(self) -> bool:
        return self.active >= self.slots

    @asynccontextmanager
    async def slot(self):
        self.waiting += 1
        try:
            await self._sem.acquire()
        finally:
            self.waiting -= 1
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1
            self._sem.release()


@dataclass
class App:
    settings: Settings
    db: Database
    assistant: Assistant
    sandbox: SandboxClient | None = None
    speech: SpeechClient | None = None
    imagegen: ImageClient | None = None
    intra: IntraClient | None = None
    youtube: object | None = None  # summarize.YouTube: субтитры и аудио с YouTube
    weather: WeatherClient = field(default_factory=WeatherClient)
    queue: GpuQueue = field(default_factory=lambda: GpuQueue(2))
    image_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    chat_locks: defaultdict[int, asyncio.Lock] = field(default_factory=lambda: defaultdict(asyncio.Lock))
    started_at: float = field(default_factory=time.time)
    bot_username: str = ""
    background: set[asyncio.Task] = field(default_factory=set)
    last_gpu_alert: float = 0.0

    @property
    def llm(self):
        return self.assistant.llm

    def spawn(self, coro) -> asyncio.Task:
        """Фоновая задача, на которую держим ссылку (иначе её может собрать GC)."""
        task = asyncio.create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    def today(self) -> str:
        return datetime.datetime.now(self.assistant.tz(None)).date().isoformat()

    async def check_limit(self, user_id: int) -> tuple[bool, int]:
        """(можно ли, сколько осталось). Админам и при DAILY_LIMIT=0 — без ограничений."""
        limit = self.settings.daily_limit
        if limit <= 0 or user_id in self.settings.admins:
            return True, -1
        used = await self.db.get_usage(user_id, self.today())
        return used < limit, max(limit - used, 0)

    async def count_usage(self, user_id: int) -> None:
        await self.db.increment_usage(user_id, self.today())

    async def close(self) -> None:
        for task in list(self.background):
            task.cancel()
        for client in (self.sandbox, self.speech, self.imagegen, self.intra, self.youtube, self.weather,
                       self.assistant.web):
            if client is not None:
                await client.close()
        await self.assistant.llm.close()
        await self.db.close()
