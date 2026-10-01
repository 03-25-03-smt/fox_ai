"""HTTP-клиенты внешних сервисов: песочница, речь, генерация картинок."""

from dataclasses import dataclass
from typing import Any

import httpx


class ServiceError(Exception):
    pass


class _Client:
    name = "service"

    def __init__(self, base_url: str, timeout: float) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=httpx.Timeout(timeout, connect=10.0)
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def health(self) -> bool:
        try:
            resp = await self._client.get("/health", timeout=5.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ServiceError(f"{self.name} недоступен: {exc}") from exc
        if resp.status_code != 200:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise ServiceError(f"{self.name}: {detail}")
        return resp


# ---------------------------------------------------------------- песочница


@dataclass(frozen=True)
class ProcResult:
    exit_code: int | None
    signal: str | None
    timed_out: bool
    stdout: str
    stderr: str
    duration_ms: int


@dataclass(frozen=True)
class RunResult:
    compiled: bool
    compile_command: str
    compile_output: str
    run: ProcResult | None
    valgrind_log: str | None

    @property
    def has_problems(self) -> bool:
        if not self.compiled or self.run is None:
            return True
        r = self.run
        if r.timed_out or r.signal or (r.exit_code not in (0, None)):
            return True
        text = (r.stderr + (self.valgrind_log or "")).lower()
        return any(k in text for k in ("leak", "invalid read", "invalid write", "sanitizer", "uninitialised"))


class SandboxClient(_Client):
    name = "Песочница"

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url, timeout=300.0)

    async def run(
        self,
        files: dict[str, str],
        *,
        args: list[str] | None = None,
        stdin: str = "",
        check: str = "none",
        werror: bool = True,
        timeout: float = 10.0,
    ) -> RunResult:
        resp = await self._request("POST", "/run", json={
            "files": files, "args": args or [], "stdin": stdin,
            "check": check, "werror": werror, "timeout": timeout,
        })
        data = resp.json()
        run = ProcResult(**data["run"]) if data.get("run") else None
        return RunResult(
            compiled=data["compiled"], compile_command=data["compile_command"],
            compile_output=data["compile_output"], run=run, valgrind_log=data.get("valgrind_log"),
        )

    async def check_project(self, files: dict[str, str]) -> dict[str, Any]:
        resp = await self._request("POST", "/project", json={"files": files})
        return resp.json()


# ---------------------------------------------------------------- речь


class SpeechClient(_Client):
    name = "Сервис речи"

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url, timeout=180.0)

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg", language: str | None = None) -> str:
        """language — код языка речи (de, cs…), если известен; иначе Whisper определит сам."""
        params = {"language": language} if language else None
        resp = await self._request("POST", "/stt", files={"file": (filename, audio)}, params=params)
        return str(resp.json().get("text", "")).strip()

    async def synthesize(self, text: str, lang: str = "ru") -> bytes:
        """Возвращает OGG/Opus — формат голосовых сообщений Telegram. lang: ru, de, cs."""
        resp = await self._request("POST", "/tts", json={"text": text, "lang": lang})
        return resp.content


# ---------------------------------------------------------------- картинки


class ImageClient(_Client):
    name = "Генератор картинок"

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url, timeout=600.0)

    async def generate(self, prompt: str, *, seed: int | None = None) -> bytes:
        resp = await self._request("POST", "/generate", json={"prompt": prompt, "seed": seed})
        return resp.content

    async def unload(self) -> None:
        """Сразу освободить видеопамять (не ждать таймера простоя)."""
        await self._request("POST", "/unload")
