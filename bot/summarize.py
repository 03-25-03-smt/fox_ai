"""Пересказ ссылок: статьи (через WebTools.fetch) и YouTube (субтитры, иначе Whisper).

YouTube читается через yt-dlp: сначала ручные субтитры, затем автоматические —
на русском, английском, немецком, чешском или языке видео. Если субтитров нет,
скачивается только аудиодорожка (не больше ~24 МБ) и распознаётся сервисом speech.
Длинный текст пересказывается по частям, затем части сводятся в итог.
"""

import asyncio
import html
import json
import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
YOUTUBE_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/)|youtu\.be/)([\w-]{11})", re.IGNORECASE
)
SUB_LANGS = ("ru", "en", "de", "cs", "uk")
SUB_FORMATS = ("vtt", "srv1", "json3")
MAX_VIDEO_SECONDS = 3 * 3600  # видео с субтитрами — до 3 ч
MAX_AUDIO_SECONDS = 45 * 60  # распознавать Whisper'ом — до 45 мин
MAX_AUDIO_BYTES = 24 * 1024 * 1024  # лимит сервиса speech — 25 МБ
CHUNK_CHARS = 12000  # кусок текста на один запрос пересказа (влезает в 8k токенов)
MAX_CHUNKS = 8


class LinkError(Exception):
    pass


@dataclass(frozen=True)
class Transcript:
    title: str
    text: str
    source: str  # «субтитры (ru)», «автосубтитры (en)», «распознано Whisper», «страница»
    duration: int | None = None  # секунды
    url: str = ""


def find_urls(text: str) -> list[str]:
    return [u.rstrip(".,;:!?") for u in URL_RE.findall(text or "")]


def only_link(text: str) -> str | None:
    """Ссылка, если сообщение — это (почти) только ссылка: «https://… » или «глянь https://…»."""
    urls = find_urls(text)
    if len(urls) != 1:
        return None
    rest = text.replace(urls[0], "").strip()
    return urls[0] if len(rest) <= 30 else None


def youtube_id(url: str) -> str | None:
    m = YOUTUBE_RE.search(url)
    return m.group(1) if m else None


def vtt_to_text(vtt: str) -> str:
    """WebVTT -> сплошной текст. Автосубтитры YouTube повторяют строки — убираем повторы."""
    lines: list[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT" or "-->" in line or line.isdigit():
            continue
        if line.startswith(("Kind:", "Language:", "NOTE", "STYLE")):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = html.unescape(line).strip()
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def srv_to_text(xml: str) -> str:
    """srv1 (XML YouTube timedtext) -> текст."""
    parts = [html.unescape(re.sub(r"<[^>]+>", "", p)) for p in re.findall(r"<text[^>]*>(.*?)</text>", xml, re.S)]
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def json3_to_text(raw: str) -> str:
    try:
        events = json.loads(raw).get("events") or []
    except (ValueError, AttributeError):
        return ""
    parts = ["".join(seg.get("utf8", "") for seg in ev.get("segs") or []) for ev in events]
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def subtitles_to_text(raw: str, fmt: str) -> str:
    if fmt == "vtt":
        return vtt_to_text(raw)
    if fmt == "json3":
        return json3_to_text(raw)
    return srv_to_text(raw)


def pick_subtitles(info: dict[str, Any]) -> tuple[str, str, str] | None:
    """(url, формат, описание) лучших субтитров: ручные лучше автоматических."""
    language = (info.get("language") or "").split("-")[0]
    prefs = [*([language] if language else []), *SUB_LANGS]
    for key, label in (("subtitles", "субтитры"), ("automatic_captions", "автосубтитры")):
        tracks: dict[str, list[dict]] = info.get(key) or {}
        for lang in prefs:
            # автосубтитры бывают «ru», «ru-orig», переводные «ru-en» — берём исходные
            candidates = [k for k in tracks if k == lang or k == f"{lang}-orig"]
            for name in candidates:
                for fmt in SUB_FORMATS:
                    for track in tracks[name]:
                        if track.get("ext") == fmt and track.get("url"):
                            return track["url"], fmt, f"{label} ({lang})"
    return None


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Режет по предложениям, чтобы куски не обрывались на полуслове."""
    if len(text) <= size:
        return [text]
    chunks, current = [], ""
    for sentence in re.split(r"(?<=[.!?…])\s+", text):
        if len(current) + len(sentence) > size and current:
            chunks.append(current.strip())
            current = ""
        current += sentence + " "
        while len(current) > size:  # предложение-монстр (автосубтитры без точек)
            chunks.append(current[:size])
            current = current[size:]
    if current.strip():
        chunks.append(current.strip())
    return chunks


def fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class YouTube:
    """Субтитры и аудио с YouTube через yt-dlp (блокирующий — работает в потоке)."""

    def __init__(self, speech=None, cookies: str = "") -> None:
        self.speech = speech
        self.cookies = cookies  # cookies.txt браузера — если YouTube просит «подтвердить, что не бот»
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=True)

    async def close(self) -> None:
        await self._http.aclose()

    def _opts(self) -> dict[str, Any]:
        opts: dict[str, Any] = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True}
        if self.cookies and Path(self.cookies).is_file():
            opts["cookiefile"] = self.cookies
        return opts

    def _info(self, url: str) -> dict[str, Any]:
        import yt_dlp  # тяжёлый импорт — только когда нужен

        with yt_dlp.YoutubeDL({**self._opts(), "skip_download": True}) as ydl:
            return ydl.extract_info(url, download=False)

    def _audio(self, url: str, folder: str) -> Path:
        import yt_dlp

        opts = {
            **self._opts(),
            # самая лёгкая аудиодорожка: Whisper'у качество не важно
            "format": "worstaudio[abr>=32]/worstaudio/bestaudio",
            "outtmpl": f"{folder}/audio.%(ext)s",
            "max_filesize": MAX_AUDIO_BYTES,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = list(Path(folder).glob("audio.*"))
        if not files:
            raise LinkError("аудио больше 24 МБ — слишком длинное видео для распознавания")
        return files[0]

    async def transcript(self, url: str) -> Transcript:
        try:
            info = await asyncio.to_thread(self._info, url)
        except Exception as exc:  # yt-dlp бросает свои DownloadError/ExtractorError
            if "not a bot" in str(exc) or "Sign in" in str(exc):
                raise LinkError("YouTube просит подтвердить, что это не бот. Экспортируй cookies браузера "
                                "в файл и укажи его в YOUTUBE_COOKIES (см. README)") from exc
            raise LinkError(f"YouTube не отдал видео: {str(exc)[:200]}") from exc
        title = str(info.get("title") or "видео")
        duration = info.get("duration")
        if duration and duration > MAX_VIDEO_SECONDS:
            raise LinkError(f"видео длиннее {MAX_VIDEO_SECONDS // 3600} ч")

        if picked := pick_subtitles(info):
            sub_url, fmt, label = picked
            try:
                resp = await self._http.get(sub_url)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("subtitles download failed: %s", exc)
            else:
                text = subtitles_to_text(resp.text, fmt)
                if len(text) > 50:
                    return Transcript(title, text, label, duration, url)

        if self.speech is None:
            raise LinkError("у видео нет субтитров, а распознавание речи выключено")
        if duration and duration > MAX_AUDIO_SECONDS:
            raise LinkError(f"у видео нет субтитров, а для распознавания оно длиннее {MAX_AUDIO_SECONDS // 60} мин")
        with tempfile.TemporaryDirectory(prefix="yt_") as tmp:
            try:
                path = await asyncio.to_thread(self._audio, url, tmp)
            except LinkError:
                raise
            except Exception as exc:
                raise LinkError(f"не удалось скачать аудио: {str(exc)[:200]}") from exc
            text = await self.speech.transcribe(path.read_bytes(), path.name)
        if not text:
            raise LinkError("в видео не удалось распознать речь")
        return Transcript(title, text, "распознано Whisper", duration, url)


SUMMARY_SYSTEM = (
    "Ты делаешь точные пересказы статей и видео для занятого человека. Пиши по-русски, "
    "без воды, ничего не выдумывай — только то, что есть в тексте."
)


def part_prompt(t: Transcript, part: str, index: int, total: int) -> str:
    return (f"Это часть {index} из {total} текста «{t.title}». Выпиши главное из этой части "
            f"5–10 пунктами (факты, цифры, выводы, советы).\n\n{part}")


def final_prompt(t: Transcript, body: str, *, from_parts: bool, question: str = "") -> str:
    kind = "видео" if youtube_id(t.url) else "страницы"
    what = "Ниже — конспекты частей" if from_parts else "Ниже — текст"
    ask = f"\n\nОтдельно ответь на вопрос пользователя: {question}" if question else ""
    return (
        f"{what} {kind} «{t.title}» ({t.source}).\n\n{body}\n\n"
        "Сделай пересказ: сначала 1–2 предложения — о чём это, затем 5–8 главных мыслей списком, "
        "в конце — вывод или что полезного можно взять. Если это инструкция или рецепт — сохрани "
        f"шаги и цифры.{ask}"
    )


async def fetch_transcript(url: str, web, youtube: YouTube | None) -> Transcript:
    if youtube_id(url):
        if youtube is None:
            raise LinkError("пересказ YouTube выключен")
        return await youtube.transcript(url)
    if web is None:
        raise LinkError("интернет выключен (не задан SEARXNG_URL)")
    title, text = await web.fetch(url, max_chars=CHUNK_CHARS * MAX_CHUNKS)
    if len(text) < 200:
        raise LinkError("на странице почти нет текста (возможно, она грузится скриптами)")
    return Transcript(title or url, text, "страница", None, url)
