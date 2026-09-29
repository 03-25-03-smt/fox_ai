"""Интернет: поиск через SearXNG и чтение страниц с защитой от SSRF."""

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_PAGE_CHARS = 8000
MAX_REDIRECTS = 5
USER_AGENT = "Mozilla/5.0 (compatible; FoxAI/1.0)"
TEXT_TYPES = ("text/html", "text/plain", "application/xhtml+xml", "application/json")


class WebError(Exception):
    pass


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "form", "iframe", "template"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
             "section", "article", "pre", "blockquote", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return parser.title.strip(), text.strip()


async def ensure_public_url(url: str) -> None:
    """Разрешаем только http(s) на публичные адреса: не пускаем модель в локальную сеть,
    к Ollama, роутеру, metadata-сервисам и т.п."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise WebError("Разрешены только http/https ссылки")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, port, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise WebError(f"Не удалось найти сайт {parts.hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise WebError("Доступ к локальным и приватным адресам запрещён")


class WebTools:
    def __init__(self, searxng_url: str, timeout: float = 20.0) -> None:
        self._searxng_url = searxng_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def search(self, query: str, limit: int = 6) -> list[SearchResult]:
        """Поиск через свой SearXNG (он внутренний, поэтому SSRF-проверка не нужна)."""
        try:
            resp = await self._client.get(
                f"{self._searxng_url}/search",
                params={"q": query, "format": "json", "safesearch": 1},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise WebError(f"Поиск недоступен: {exc}") from exc
        results = []
        for item in resp.json().get("results", [])[:limit]:
            results.append(SearchResult(
                title=str(item.get("title", "")).strip(),
                url=str(item.get("url", "")),
                snippet=str(item.get("content", "")).strip()[:400],
            ))
        return results

    async def fetch(self, url: str) -> tuple[str, str]:
        """Скачивает страницу и возвращает (заголовок, текст)."""
        for _ in range(MAX_REDIRECTS + 1):
            await ensure_public_url(url)
            try:
                async with self._client.stream("GET", url) as resp:
                    if resp.is_redirect and "location" in resp.headers:
                        url = urljoin(url, resp.headers["location"])
                        continue
                    if resp.status_code >= 400:
                        raise WebError(f"Сайт ответил {resp.status_code}")
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype and ctype not in TEXT_TYPES:
                        raise WebError(f"Не текстовая страница ({ctype})")
                    body = bytearray()
                    async for part in resp.aiter_bytes():
                        body.extend(part)
                        if len(body) > MAX_PAGE_BYTES:
                            break
                    raw = bytes(body[:MAX_PAGE_BYTES]).decode(resp.encoding or "utf-8", errors="replace")
            except httpx.HTTPError as exc:
                raise WebError(f"Не удалось открыть страницу: {exc}") from exc
            if ctype in ("text/html", "application/xhtml+xml") or raw.lstrip().startswith("<"):
                title, text = html_to_text(raw)
            else:
                title, text = "", raw
            return title, text[:MAX_PAGE_CHARS]
        raise WebError("Слишком много редиректов")
