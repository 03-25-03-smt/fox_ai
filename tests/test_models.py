"""/pull /rm /bench и клиент Ollama для них."""

import json

import httpx
import respx

from bot.handlers.models import progress_bar
from bot.llm import OllamaClient

from .conftest import ADMIN, FRIEND


def test_progress_bar():
    assert progress_bar(1, 2, 10) == "[█████░░░░░] 50%"
    assert progress_bar(0, 0, 4) == "[░░░░] 0%"


async def test_pull_rm_bench_commands(env):
    await env.send(ADMIN, "/pull qwen2.5:14b")
    assert "скачана" in env.last_text() and "qwen2.5:14b" in env.llm.models
    await env.send(ADMIN, "/pull bad:model")
    assert "does not exist" in env.last_text()
    await env.send(ADMIN, "/pull ../etc")
    assert "Использование" in env.last_text()

    await env.send(ADMIN, "/rm qwen2.5:7b")  # модель по умолчанию трогать нельзя
    assert "указана в .env" in env.last_text() and "qwen2.5:7b" in env.llm.models
    await env.send(ADMIN, "/rm llama3.1:8b")
    assert "удалена" in env.last_text() and "llama3.1:8b" not in env.llm.models

    await env.send(ADMIN, "/bench qwen2.5:3b qwen2.5:7b")
    text = env.last_text()
    assert text.index("qwen2.5:3b") < text.index("qwen2.5:7b") and "<b>80</b>" in text
    await env.send(ADMIN, "/bench nope:1b")
    assert "Нет таких моделей" in env.last_text()


async def test_models_admin_only(env):
    await env.send(FRIEND, "/pull qwen2.5:14b")
    assert "qwen2.5:14b" not in env.llm.models


async def test_ollama_client_pull_delete_bench():
    with respx.mock:
        lines = [{"status": "pulling", "total": 10, "completed": 5}, {"status": "success"}]
        respx.post("http://o/api/pull").respond(content="\n".join(json.dumps(x) for x in lines).encode())
        delete = respx.delete("http://o/api/delete").respond(200)
        respx.post("http://o/api/generate").respond(json={
            "eval_count": 100, "eval_duration": 2_000_000_000, "prompt_eval_count": 50,
            "prompt_eval_duration": 100_000_000, "load_duration": 3_000_000_000, "total_duration": 5_500_000_000})
        client = OllamaClient("http://o")
        events = [e async for e in client.pull("m")]
        assert events[-1]["status"] == "success"
        await client.delete("m")
        assert json.loads(delete.calls.last.request.content) == {"model": "m"}
        r = await client.bench("m", "hi")
        assert (r["gen_tps"], r["prompt_tps"], r["load_s"]) == (50.0, 500.0, 3.0)
        respx.post("http://o/api/pull").respond(content=b'{"error": "no space left"}')
        try:
            [e async for e in client.pull("m")]
        except Exception as exc:
            assert "no space" in str(exc)
        respx.post("http://o/api/pull").mock(side_effect=httpx.ConnectError("down"))
        try:
            [e async for e in client.pull("m")]
        except Exception as exc:
            assert "недоступна" in str(exc)
        await client.close()
