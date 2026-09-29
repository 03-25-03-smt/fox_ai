import json

import httpx
import pytest
import respx

from bot.llm import LLMError, OllamaClient, ToolsNotSupported

BASE = "http://ollama.test"


@pytest.fixture
async def client():
    c = OllamaClient(BASE)
    yield c
    await c.close()


def ndjson(*objs) -> bytes:
    return "".join(json.dumps(o) + "\n" for o in objs).encode()


@respx.mock
async def test_list_models(client):
    respx.get(f"{BASE}/api/tags").respond(
        json={"models": [{"name": "qwen2.5:7b"}, {"name": "llama3.1:8b"}]}
    )
    assert await client.list_models() == ["llama3.1:8b", "qwen2.5:7b"]


@respx.mock
async def test_chat_stream(client):
    route = respx.post(f"{BASE}/api/chat").respond(
        content=ndjson(
            {"message": {"role": "assistant", "content": "При"}, "done": False},
            {"message": {"role": "assistant", "content": "вет"}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True},
        )
    )
    msgs = [{"role": "user", "content": "hi"}]
    chunks = [c async for c in client.chat_stream("qwen2.5:7b", msgs)]
    assert "".join(c.content for c in chunks) == "Привет"
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"model": "qwen2.5:7b", "messages": msgs, "stream": True}


@respx.mock
async def test_chat_model_not_found(client):
    respx.post(f"{BASE}/api/chat").respond(404, json={"error": "model 'x' not found"})
    with pytest.raises(LLMError, match="not found"):
        [c async for c in client.chat_stream("x", [])]


@respx.mock
async def test_chat_error_mid_stream(client):
    respx.post(f"{BASE}/api/chat").respond(
        content=ndjson({"message": {"content": "a"}}, {"error": "out of memory"})
    )
    with pytest.raises(LLMError, match="out of memory"):
        [c async for c in client.chat_stream("m", [])]


@respx.mock
async def test_ollama_down(client):
    respx.get(f"{BASE}/api/tags").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(LLMError, match="недоступна"):
        await client.list_models()


@respx.mock
async def test_chat_stream_tool_calls_and_options():
    client = OllamaClient(BASE, num_ctx=8192)
    call = {"function": {"name": "web_search", "arguments": {"query": "x"}}}
    route = respx.post(f"{BASE}/api/chat").respond(
        content=ndjson({"message": {"content": "", "tool_calls": [call]}, "done": True})
    )
    chunks = [c async for c in client.chat_stream("m", [], tools=[{"type": "function"}])]
    assert chunks[0].tool_calls == [call]
    sent = json.loads(route.calls.last.request.content)
    assert sent["tools"] == [{"type": "function"}]
    assert sent["options"] == {"num_ctx": 8192}
    await client.close()


@respx.mock
async def test_tools_not_supported(client):
    respx.post(f"{BASE}/api/chat").respond(
        400, json={"error": "registry.ollama.ai/library/gemma:2b does not support tools"}
    )
    with pytest.raises(ToolsNotSupported):
        [c async for c in client.chat_stream("gemma:2b", [], tools=[{}])]


@respx.mock
async def test_embed_batches(client):
    def reply(request):
        inputs = json.loads(request.content)["input"]
        return httpx.Response(200, json={"embeddings": [[float(len(t))] for t in inputs]})

    route = respx.post(f"{BASE}/api/embed").mock(side_effect=reply)
    texts = [f"t{i}" for i in range(40)]
    vectors = await client.embed("bge-m3", texts)
    assert len(vectors) == 40
    assert route.call_count == 2  # батчи по 32


@respx.mock
async def test_chat_json_mode(client):
    route = respx.post(f"{BASE}/api/chat").respond(json={"message": {"content": '{"facts": []}'}})
    assert await client.chat("m", [], json_mode=True) == '{"facts": []}'
    assert json.loads(route.calls.last.request.content)["format"] == "json"


@respx.mock
async def test_options_speed_and_ps():
    client = OllamaClient(BASE, num_ctx=4096)
    route = respx.post(f"{BASE}/api/chat").respond(content=ndjson(
        {"message": {"content": "ok"}, "done": True, "eval_count": 50, "eval_duration": 2_000_000_000}
    ))
    [c async for c in client.chat_stream("m", [], options={"temperature": 0.2, "top_p": None})]
    assert json.loads(route.calls.last.request.content)["options"] == {"num_ctx": 4096, "temperature": 0.2}
    assert client.speeds[-1].tokens_per_sec == 25.0

    respx.get(f"{BASE}/api/ps").respond(json={"models": [{"name": "qwen2.5:7b", "size_vram": 5, "size": 6}]})
    [loaded] = await client.loaded_models()
    assert (loaded.name, loaded.size_vram) == ("qwen2.5:7b", 5)
    await client.close()
