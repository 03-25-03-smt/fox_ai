"""Связь с агентом на Windows (windows/fox-agent.ps1) через общую папку.

Бот кладёт запрос requests/<id>.json, агент выполняет команду из своего белого списка
(логи и перезапуск контейнеров, лимит мощности P100, перезапуск Ollama) и пишет ответ в
responses/<id>.json. Сетевого порта нет, Docker-сокет в контейнер не пробрасывается.
Агент раз в 15 с обновляет status.json — по нему бот понимает, что агент жив.
"""

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

COMMANDS = {"logs", "restart", "ps", "power", "ollama-restart"}
SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
STATUS_MAX_AGE = 60.0


class HostError(Exception):
    pass


@dataclass(frozen=True)
class HostResult:
    ok: bool
    output: str


class HostAgent:
    def __init__(self, folder: str) -> None:
        self.folder = Path(folder)

    def status(self) -> dict[str, Any] | None:
        """Содержимое status.json, если агент жив (файл свежий)."""
        path = self.folder / "status.json"
        try:
            if time.time() - path.stat().st_mtime > STATUS_MAX_AGE:
                return None
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @property
    def alive(self) -> bool:
        return self.status() is not None

    async def run(self, cmd: str, *args: str, timeout: float = 60.0) -> HostResult:
        if cmd not in COMMANDS:
            raise HostError(f"неизвестная команда {cmd}")
        if not self.alive:
            raise HostError("агент на Windows не запущен (windows\\fox-agent.ps1, см. README)")
        req_dir, resp_dir = self.folder / "requests", self.folder / "responses"
        req_dir.mkdir(parents=True, exist_ok=True)
        rid = uuid.uuid4().hex
        body = json.dumps({"id": rid, "cmd": cmd, "args": list(args), "created": time.time()})
        tmp = req_dir / f".{rid}.tmp"
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, req_dir / f"{rid}.json")  # агент никогда не увидит половину файла
        resp = resp_dir / f"{rid}.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            if resp.exists():
                try:
                    data = json.loads(resp.read_text(encoding="utf-8-sig"))
                except ValueError:
                    continue  # ещё пишется
                resp.unlink(missing_ok=True)
                return HostResult(bool(data.get("ok")), str(data.get("output") or ""))
        (req_dir / f"{rid}.json").unlink(missing_ok=True)
        raise HostError("агент не ответил вовремя")
