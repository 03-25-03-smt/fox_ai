"""Агент Windows (через общую папку), /logs /restart /power, бережный режим P100, ночная выгрузка."""

import asyncio
import datetime
import json
import time

import pytest

from bot.app import GpuQueue
from bot.gpu import GpuInfo
from bot.host import HostAgent, HostError
from bot.tasks import gentle_mode, night_unload

from .conftest import ADMIN, FRIEND


class FakeAgent:
    """Отвечает на запросы в папке, как windows/fox-agent.ps1."""

    def __init__(self, folder) -> None:
        self.folder = folder
        self.seen: list[tuple[str, list[str]]] = []
        (folder / "requests").mkdir(parents=True, exist_ok=True)
        (folder / "responses").mkdir(parents=True, exist_ok=True)
        self.heartbeat()

    def heartbeat(self) -> None:
        (self.folder / "status.json").write_text(json.dumps({"power": "Tesla P100: лимит 250 из 250 Вт"}))

    async def serve(self) -> None:
        while True:
            for f in (self.folder / "requests").glob("*.json"):
                req = json.loads(f.read_text())
                f.unlink()
                self.seen.append((req["cmd"], req["args"]))
                out = {"logs": "строка лога\n" * 3, "restart": "", "ps": "bot Up", "power": "лимит 150 Вт"}
                (self.folder / "responses" / f"{req['id']}.json").write_text(
                    json.dumps({"ok": True, "output": out.get(req["cmd"], "")}))
            await asyncio.sleep(0.05)


@pytest.fixture
async def agent_env(make_env, tmp_path):
    env = await make_env(host_dir=str(tmp_path / "host"))
    env.app.host = HostAgent(env.settings.host_dir)
    env.agent = FakeAgent(tmp_path / "host")
    task = asyncio.create_task(env.agent.serve())
    yield env
    task.cancel()


async def test_host_agent_dead_or_unknown(tmp_path):
    agent = HostAgent(str(tmp_path))
    assert not agent.alive
    with pytest.raises(HostError, match="не запущен"):
        await agent.run("ps")
    with pytest.raises(HostError, match="неизвестная"):
        await agent.run("format", "c:")


async def test_logs_restart_ps_power(agent_env):
    env = agent_env
    await env.send(ADMIN, "/logs speech 30")
    assert "строка лога" in env.last_text()
    await env.send(ADMIN, "/logs ../../etc")
    assert "Использование" in env.last_text()
    await env.send(ADMIN, "/restart speech")
    await env.send(ADMIN, "/restart ollama")
    await env.send(ADMIN, "/ps")
    await env.send(ADMIN, "/power 150")
    await env.send(ADMIN, "/power")
    assert "лимит 250 из 250" in env.last_text()
    assert env.agent.seen == [("logs", ["speech", "30"]), ("restart", ["speech"]), ("ollama-restart", []),
                              ("ps", []), ("power", ["150"])]
    await env.send(FRIEND, "/restart bot")
    assert len(env.agent.seen) == 5


async def test_without_agent(env):
    await env.send(ADMIN, "/logs bot")
    assert "не настроен" in env.last_text()


async def test_queue_limit_changes_on_the_fly():
    q = GpuQueue(2)
    await q.set_limit(1)
    order = []

    async def job(name):
        async with q.slot():
            order.append(f"{name}+")
            await asyncio.sleep(0.02)
            order.append(f"{name}-")

    await asyncio.gather(job("a"), job("b"))
    assert order == ["a+", "a-", "b+", "b-"]  # строго по одной
    await q.set_limit(5)
    assert q.limit == 2  # больше настроенного не бывает


def p100(temp: int) -> list[GpuInfo]:
    return [GpuInfo(1, "Tesla P100-PCIE-16GB", temp, 90, 9000, 16384, 200.0, None),
            GpuInfo(0, "NVIDIA GeForce RTX 3070", 90, 10, 1, 8192, 30.0, 40)]


async def test_gentle_mode_on_overheat(agent_env):
    env = agent_env
    env.app.queue = GpuQueue(2)
    assert await gentle_mode(env.bot, env.app, p100(75)) is None  # 3070 горячая, но LLM не на ней
    assert await gentle_mode(env.bot, env.app, p100(81)) == "on"
    assert env.app.queue.limit == 1 and env.app.throttled
    assert "бережный режим" in env.last_text() and "лимит 150 Вт" in env.last_text()
    assert await gentle_mode(env.bot, env.app, p100(78)) is None  # гистерезис
    assert await gentle_mode(env.bot, env.app, p100(70)) == "off"
    assert env.app.queue.limit == 2
    assert env.agent.seen == [("power", ["150"]), ("power", ["default"])]


async def test_night_unload(env):
    paris = env.assistant.tz(None)
    night = datetime.datetime(2026, 10, 5, 3, 0, tzinfo=paris)
    env.app.queue.last_used = time.monotonic()
    assert not await night_unload(env.app, night)  # недавно пользовались
    env.app.queue.last_used = time.monotonic() - 3600
    assert not await night_unload(env.app, night.replace(hour=14))  # днём не трогаем
    assert await night_unload(env.app, night)
    assert env.llm.unloaded == ["*"]
