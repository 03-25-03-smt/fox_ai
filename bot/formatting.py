"""Разбиение длинных ответов и перевод Markdown модели в безопасный Telegram HTML."""

import html
import re

_FENCE_RE = re.compile(r"```([\w+#.-]*)[^\n]*\n?(.*?)(?:```|\Z)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_HEADER_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_THINK_RE = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL)


def strip_think(text: str) -> str:
    """Убирает рассуждения <think>...</think>, которые печатают некоторые модели."""
    return _THINK_RE.sub("", text)


def md_to_html(text: str) -> str:
    """Минимальная конвертация: блоки кода, `код`, **жирный**, # заголовки.

    Всё остальное экранируется, поэтому результат всегда валиден для parse_mode=HTML.
    """
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    def fence(m: re.Match[str]) -> str:
        lang, code = m.group(1), html.escape(m.group(2).rstrip("\n"), quote=False)
        if lang:
            return keep(f'<pre><code class="language-{html.escape(lang)}">{code}</code></pre>')
        return keep(f"<pre>{code}</pre>")

    text = _FENCE_RE.sub(fence, text)
    text = _INLINE_CODE_RE.sub(
        lambda m: keep(f"<code>{html.escape(m.group(1), quote=False)}</code>"), text
    )
    text = html.escape(text, quote=False)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _HEADER_RE.sub(r"<b>\1</b>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)


def split_markdown(text: str, limit: int = 3000) -> list[str]:
    """Режет текст на куски <= limit по строкам, не ломая блоки кода.

    Если разрез попадает внутрь ```блока```, он закрывается в конце куска
    и открывается заново в начале следующего.
    """
    if len(text) <= limit:
        return [text]

    budget = limit - 4  # запас под закрывающий "\n```"
    chunks: list[str] = []
    buf = ""
    has_content = False  # есть ли в buf что-то кроме переоткрытого ```
    fence_opener: str | None = None

    def flush() -> None:
        nonlocal buf, has_content
        if has_content:
            chunks.append(buf + ("\n```" if fence_opener is not None else ""))
        buf = fence_opener or ""
        has_content = False

    for line in text.split("\n"):
        rest = line
        while True:
            sep = "\n" if buf else ""
            space = budget - len(buf) - len(sep)
            if len(rest) <= space:
                buf += sep + rest
                has_content = True
                break
            fresh_space = budget - (len(fence_opener) + 1 if fence_opener else 0)
            if has_content and len(rest) <= fresh_space:
                flush()  # строка влезет целиком в следующий кусок
                continue
            if space <= 0:
                flush()
                continue
            # Строка длиннее целого куска — режем её по границе куска
            buf += sep + rest[:space]
            has_content = True
            rest = rest[space:]
            flush()
        if line.strip().startswith("```"):
            fence_opener = None if fence_opener is not None else line.strip()[: budget // 2]

    flush()
    return chunks
