import socket

import httpx
import pytest
import respx

from bot.norminette import NorminetteError, extract_code, run_norminette, safe_filename
from bot.web import WebError, WebTools, ensure_public_url, html_to_text


# ---------------------------------------------------------------- norminette


async def test_norminette_finds_errors():
    result = await run_norminette("int main(){return 0;}\n", "main.c")
    assert not result.ok
    assert result.errors > 0
    assert "INVALID_HEADER" in result.output
    assert "/tmp" not in result.output  # пути сервера не утекают


async def test_norminette_too_big():
    with pytest.raises(NorminetteError):
        await run_norminette("x" * (300 * 1024), "big.c")


def test_safe_filename():
    assert safe_filename("ft_strlen.c") == "ft_strlen.c"
    assert safe_filename("../../etc/passwd.c") == "passwd.c"
    assert safe_filename("My File.c") == "file.c"
    assert safe_filename(None, "#ifndef FT_H\n# define FT_H\n#endif") == "file.h"


def test_extract_code():
    assert extract_code("смотри:\n```c\nint x;\n```\nвот") == "int x;\n"
    assert extract_code("int y;") == "int y;"


# ---------------------------------------------------------------- web


def test_html_to_text():
    title, text = html_to_text(
        "<html><head><title>Hi</title><style>p{}</style></head>"
        "<body><nav>menu</nav><p>Hello&nbsp;world</p><script>evil()</script><p>Second</p></body></html>"
    )
    assert title == "Hi"
    assert "Hello" in text and "Second" in text
    assert "evil" not in text and "menu" not in text


@pytest.fixture
def fake_dns(monkeypatch):
    table = {"public.test": "93.184.216.34", "evil.test": "192.168.1.1", "meta.test": "169.254.169.254"}

    async def getaddrinfo(self, host, port, **kwargs):
        if host not in table:
            raise socket.gaierror("not found")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], port))]

    import asyncio

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", getaddrinfo)
    return table


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:11434/api/tags",
    "http://localhost/",
    "http://[::1]/",
    "http://evil.test/",
    "http://meta.test/latest/meta-data",
    "file:///etc/passwd",
    "ftp://public.test/",
])
async def test_ssrf_blocked(fake_dns, url):
    fake_dns["localhost"] = "127.0.0.1"
    with pytest.raises(WebError):
        await ensure_public_url(url)


async def test_public_url_allowed(fake_dns):
    await ensure_public_url("https://public.test/page")


@respx.mock
async def test_fetch_blocks_redirect_to_private(fake_dns):
    respx.get("http://public.test/go").respond(302, headers={"location": "http://evil.test/admin"})
    tools = WebTools("http://searx")
    with pytest.raises(WebError, match="приватным"):
        await tools.fetch("http://public.test/go")
    await tools.close()


@respx.mock
async def test_fetch_html(fake_dns):
    respx.get("http://public.test/a").respond(
        200, html="<title>T</title><p>Body</p>", headers={"content-type": "text/html"}
    )
    tools = WebTools("http://searx")
    assert await tools.fetch("http://public.test/a") == ("T", "Body")
    await tools.close()


@respx.mock
async def test_fetch_rejects_binary(fake_dns):
    respx.get("http://public.test/f.zip").respond(200, content=b"PK", headers={"content-type": "application/zip"})
    tools = WebTools("http://searx")
    with pytest.raises(WebError, match="Не текстовая"):
        await tools.fetch("http://public.test/f.zip")
    await tools.close()


@respx.mock
async def test_searxng_search():
    route = respx.get("http://searx/search").respond(json={"results": [
        {"title": "A", "url": "https://a", "content": "aaa"},
        {"title": "B", "url": "https://b", "content": "bbb"},
    ]})
    tools = WebTools("http://searx/")
    results = await tools.search("q", limit=1)
    assert [r.title for r in results] == ["A"]
    assert route.calls.last.request.url.params["format"] == "json"
    await tools.close()


@respx.mock
async def test_searxng_down():
    respx.get("http://searx/search").mock(side_effect=httpx.ConnectError("refused"))
    tools = WebTools("http://searx")
    with pytest.raises(WebError):
        await tools.search("q")
    await tools.close()
