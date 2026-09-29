"""Клиент API интры 42 (https://api.intra.42.fr), OAuth2 client_credentials."""

import asyncio
import datetime
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

API = "https://api.intra.42.fr"
LOGIN_RE = re.compile(r"^[a-z0-9-]{2,20}$")
MAIN_CURSUS = "42cursus"


class IntraError(Exception):
    pass


@dataclass(frozen=True)
class ProjectInfo:
    name: str
    status: str
    final_mark: int | None
    validated: bool | None
    marked_at: datetime.datetime | None


@dataclass(frozen=True)
class IntraProfile:
    login: str
    display_name: str
    campus: str
    level: float | None
    grade: str | None
    blackhole_at: datetime.datetime | None
    wallet: int
    correction_points: int
    projects: list[ProjectInfo] = field(default_factory=list)

    @property
    def in_progress(self) -> list[ProjectInfo]:
        return [p for p in self.projects if p.status in ("in_progress", "waiting_for_correction",
                                                          "searching_a_group", "creating_group")]

    @property
    def finished(self) -> list[ProjectInfo]:
        done = [p for p in self.projects if p.status == "finished"]
        return sorted(done, key=lambda p: p.marked_at or datetime.datetime.min.replace(tzinfo=datetime.UTC),
                      reverse=True)

    def blackhole_days(self, now: datetime.datetime | None = None) -> int | None:
        if self.blackhole_at is None:
            return None
        now = now or datetime.datetime.now(datetime.UTC)
        return (self.blackhole_at - now).days


def _parse_time(value: Any) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_profile(data: dict[str, Any]) -> IntraProfile:
    cursus_users = data.get("cursus_users") or []
    main = next(
        (c for c in cursus_users if (c.get("cursus") or {}).get("slug") == MAIN_CURSUS),
        cursus_users[-1] if cursus_users else {},
    )
    main_cursus_id = main.get("cursus_id")
    projects = []
    for pu in data.get("projects_users") or []:
        if main_cursus_id and main_cursus_id not in (pu.get("cursus_ids") or [main_cursus_id]):
            continue
        project = pu.get("project") or {}
        if project.get("parent_id"):
            continue  # подпроекты (модули piscine и т.п.)
        projects.append(ProjectInfo(
            name=str(project.get("name", "?")),
            status=str(pu.get("status", "")),
            final_mark=pu.get("final_mark"),
            validated=pu.get("validated?"),
            marked_at=_parse_time(pu.get("marked_at")),
        ))
    campus = (data.get("campus") or [{}])[0].get("name", "")
    return IntraProfile(
        login=str(data.get("login", "")),
        display_name=str(data.get("displayname") or data.get("usual_full_name") or ""),
        campus=str(campus),
        level=main.get("level"),
        grade=main.get("grade"),
        blackhole_at=_parse_time(main.get("blackholed_at")),
        wallet=int(data.get("wallet") or 0),
        correction_points=int(data.get("correction_point") or 0),
        projects=projects,
    )


class IntraClient:
    def __init__(self, client_id: str, client_secret: str, base_url: str = API) -> None:
        self._id = client_id
        self._secret = client_secret
        self._client = httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(20.0, connect=10.0))
        self._token: str | None = None
        self._token_exp = 0.0
        self._lock = asyncio.Lock()
        self._cache: dict[str, tuple[float, IntraProfile]] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def _get_token(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._token_exp - 60:
                return self._token
            try:
                resp = await self._client.post("/oauth/token", data={
                    "grant_type": "client_credentials",
                    "client_id": self._id,
                    "client_secret": self._secret,
                })
            except httpx.HTTPError as exc:
                raise IntraError(f"Интра недоступна: {exc}") from exc
            if resp.status_code != 200:
                raise IntraError("Не удалось авторизоваться в интре — проверь INTRA_CLIENT_ID/SECRET")
            data = resp.json()
            self._token = data["access_token"]
            self._token_exp = time.monotonic() + float(data.get("expires_in", 3600))
            return self._token

    async def get_profile(self, login: str, max_age: float = 300.0) -> IntraProfile:
        login = login.strip().lower()
        if not LOGIN_RE.match(login):
            raise IntraError("Некорректный логин")
        cached = self._cache.get(login)
        if cached and time.monotonic() - cached[0] < max_age:
            return cached[1]
        for attempt in range(3):
            token = await self._get_token()
            try:
                resp = await self._client.get(f"/v2/users/{login}",
                                              headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as exc:
                raise IntraError(f"Интра недоступна: {exc}") from exc
            if resp.status_code == 429:  # лимит 2 запроса/сек
                await asyncio.sleep(1.0 + attempt)
                continue
            if resp.status_code == 401:
                self._token = None
                continue
            if resp.status_code == 404:
                raise IntraError(f"Пользователь {login} не найден")
            if resp.status_code != 200:
                raise IntraError(f"Интра ответила {resp.status_code}")
            profile = parse_profile(resp.json())
            self._cache[login] = (time.monotonic(), profile)
            return profile
        raise IntraError("Интра не отвечает, попробуй позже")
