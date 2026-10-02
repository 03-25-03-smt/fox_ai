"""Дома с аквариумами: кто в каком доме, создание, приглашения, выход.

Любой пользователь бота (админ или добавленный через /adduser) может завести свой дом
(/aqstart) или вступить в чужой по коду приглашения (/aqjoin). AQUARIUM_OWNER_ID получает
дом автоматически: с аквариумами из AQUARIUM_TANKS и со всеми данными версии с одним
владельцем (home_id = 0).
"""

import datetime
import logging
import secrets
import zoneinfo

from .aquarium import HELPER, OWNER, Aquarium, Home, parse_tanks_spec
from .aquarium_brain import AquariumBrain
from .db import Database

log = logging.getLogger(__name__)

LEGACY_HOME = 0
# Таблицы с home_id: данные версии с одним владельцем переносятся в дом владельца
HOME_TABLES = ("aq_tanks", "aq_tasks", "aq_settings", "aq_achievements", "aq_facts", "aq_questions")
DEFAULT_HOME_FLAG = "default_home"


def zone(name: str | None, fallback: str = "UTC") -> datetime.tzinfo:
    for candidate in (name, fallback):
        if candidate:
            try:
                return zoneinfo.ZoneInfo(candidate)
            except (zoneinfo.ZoneInfoNotFoundError, ValueError):
                continue
    return datetime.UTC


def valid_zone(name: str) -> bool:
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return False
    return True


class AquariumHomes:
    def __init__(self, db: Database, default_tz: str, owner_id: int = 0, tanks_spec: str = "") -> None:
        self.db = db
        self.default_tz = default_tz
        self.owner_id = owner_id
        self.tanks_spec = tanks_spec
        self._cache: dict[int, Aquarium] = {}
        self._default_checked = False

    # ------------------------------------------------------------ загрузка

    async def _load(self, home_id: int) -> Aquarium | None:
        if home_id in self._cache:
            return self._cache[home_id]
        row = await self.db._fetchone(
            "SELECT id, name, owner_id, tz, invite_code FROM aq_homes WHERE id = ?", (home_id,))
        if row is None:
            return None
        home = Home(*row)
        aq = Aquarium(self.db, home, zone(home.tz, self.default_tz))
        aq.brain = AquariumBrain(self.db, home.id)
        self._cache[home_id] = aq
        return aq

    async def home_id_of(self, user_id: int) -> int | None:
        row = await self.db._fetchone("SELECT home_id FROM aq_members WHERE user_id = ?", (user_id,))
        return row[0] if row else None

    async def for_user(self, user_id: int) -> Aquarium | None:
        await self.ensure_default()
        home_id = await self.home_id_of(user_id)
        return await self._load(home_id) if home_id is not None else None

    async def touch_name(self, user_id: int, name: str) -> None:
        """Имя участника — из Telegram, если ещё не записано (хозяин дома по умолчанию)."""
        if name:
            await self.db._exec("UPDATE aq_members SET name = ? WHERE user_id = ? AND name = ''",
                                (name[:40], user_id))

    async def get(self, home_id: int) -> Aquarium | None:
        return await self._load(home_id)

    async def all(self) -> list[Aquarium]:
        """Дома, в которых есть хотя бы один участник."""
        await self.ensure_default()
        rows = await self.db._fetchall("SELECT DISTINCT home_id FROM aq_members ORDER BY home_id")
        return [aq for (hid,) in rows if (aq := await self._load(hid))]

    async def ensure_default(self) -> None:
        """Один раз: дом для AQUARIUM_OWNER_ID с данными старой версии и AQUARIUM_TANKS."""
        if self._default_checked or not self.owner_id:
            return
        self._default_checked = True
        if await self.db._fetchone("SELECT 1 FROM aq_settings WHERE home_id = ? AND key = ?",
                                   (LEGACY_HOME, DEFAULT_HOME_FLAG)):
            return
        if await self.home_id_of(self.owner_id) is None:
            aq = await self.create(self.owner_id, "", "Мои аквариумы")
            for table in HOME_TABLES:
                await self.db._exec(f"UPDATE {table} SET home_id = ? WHERE home_id = ?", (aq.id, LEGACY_HOME))
            await aq.seed(self.tanks_spec)
            log.info("aquarium: default home %s for %s", aq.id, self.owner_id)
            home_id = aq.id
        else:
            home_id = await self.home_id_of(self.owner_id)
        await self.db._exec("INSERT OR REPLACE INTO aq_settings (home_id, key, value) VALUES (?, ?, ?)",
                            (LEGACY_HOME, DEFAULT_HOME_FLAG, str(home_id)))

    # ------------------------------------------------------------ создание и участники

    async def create(self, user_id: int, user_name: str, name: str = "", tanks_spec: str = "") -> Aquarium:
        """Новый дом, user_id — хозяин. Пользователь не должен состоять в другом доме."""
        await self.db.add_user(user_id, user_name)
        cur = await self.db._exec("INSERT INTO aq_homes (name, owner_id) VALUES (?, ?)",
                                  ((name or f"Дом {user_name}".strip())[:60], user_id))
        home_id = int(cur.lastrowid)
        await self.db._exec("INSERT INTO aq_members (user_id, home_id, role, name) VALUES (?, ?, ?, ?)",
                            (user_id, home_id, OWNER, user_name[:40]))
        aq = await self._load(home_id)
        if tanks_spec:
            for tank_name, volume in parse_tanks_spec(tanks_spec):
                await aq.add_tank(tank_name, volume)
        return aq

    async def new_invite(self, aq: Aquarium) -> str:
        """Новый код приглашения; старый перестаёт работать."""
        code = secrets.token_hex(3).upper()
        await aq._set_home(invite_code=code)
        return code

    async def join(self, user_id: int, user_name: str, code: str) -> Aquarium | None:
        code = code.strip().upper()
        if not code:
            return None
        row = await self.db._fetchone("SELECT id FROM aq_homes WHERE invite_code = ?", (code,))
        if row is None:
            return None
        await self.db.add_user(user_id, user_name)
        await self.db._exec("INSERT INTO aq_members (user_id, home_id, role, name) VALUES (?, ?, ?, ?)",
                            (user_id, row[0], HELPER, user_name[:40]))
        return await self._load(row[0])

    async def remove_member(self, aq: Aquarium, user_id: int) -> int | None:
        """Убирает участника. Ушёл хозяин — хозяином становится следующий участник
        (возвращает его id). Ушёл последний — дом удаляется со всеми данными."""
        await self.db._exec("DELETE FROM aq_members WHERE user_id = ? AND home_id = ?", (user_id, aq.id))
        await self.db._exec("UPDATE aq_plan SET assignee_id = NULL WHERE assignee_id = ? "
                            "AND tank_id IN (SELECT id FROM aq_tanks WHERE home_id = ?)", (user_id, aq.id))
        rest = await aq.members()
        if not rest:
            await self.delete_home(aq)
            return None
        if aq.is_owner(user_id):
            heir = rest[0].user_id
            await self.db._exec("UPDATE aq_members SET role = ? WHERE user_id = ?", (OWNER, heir))
            await aq._set_home(owner_id=heir)
            return heir
        return None

    async def delete_home(self, aq: Aquarium) -> None:
        tanks = "SELECT id FROM aq_tanks WHERE home_id = ?"
        await self.db._exec(f"DELETE FROM aq_water WHERE tank_id IN ({tanks})", (aq.id,))
        await self.db._exec(f"DELETE FROM aq_plan WHERE tank_id IN ({tanks})", (aq.id,))
        for table in HOME_TABLES:
            await self.db._exec(f"DELETE FROM {table} WHERE home_id = ?", (aq.id,))
        await self.db._exec("DELETE FROM aq_members WHERE home_id = ?", (aq.id,))
        await self.db._exec("DELETE FROM aq_homes WHERE id = ?", (aq.id,))
        self._cache.pop(aq.id, None)

    async def set_tz(self, aq: Aquarium, name: str) -> None:
        await aq._set_home(tz=name)
        aq.tz = zone(name, self.default_tz)
