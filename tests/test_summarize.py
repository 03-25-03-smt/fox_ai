"""Пересказ ссылок и YouTube."""

import json

import pytest

from bot.summarize import (
    Transcript,
    YouTube,
    chunk_text,
    json3_to_text,
    only_link,
    pick_subtitles,
    vtt_to_text,
    youtube_id,
)

from .conftest import ADMIN

VTT = """WEBVTT
Kind: captions
Language: ru

00:00:00.000 --> 00:00:02.000
Привет<00:00:01.000><c> всем</c>

00:00:02.000 --> 00:00:04.000
Привет всем

00:00:04.000 --> 00:00:06.000
сегодня &amp; про указатели
"""


def test_link_detection():
    assert only_link("https://example.com/a?b=1") == "https://example.com/a?b=1"
    assert only_link("глянь https://example.com/x.") == "https://example.com/x"
    assert only_link("https://a.com https://b.com") is None
    assert only_link("вот ссылка https://a.com, но сначала объясни мне подробно, как работает fork") is None
    for url in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10", "https://youtu.be/dQw4w9WgXcQ",
                "https://youtube.com/shorts/dQw4w9WgXcQ", "https://m.youtube.com/watch?feature=x&v=dQw4w9WgXcQ"):
        assert youtube_id(url) == "dQw4w9WgXcQ"
    assert youtube_id("https://example.com/watch?v=dQw4w9WgXcQ") is None


def test_subtitle_parsing():
    assert vtt_to_text(VTT) == "Привет всем сегодня & про указатели"
    raw = json.dumps({"events": [{"segs": [{"utf8": "Hallo"}, {"utf8": " Welt"}]}, {"tStartMs": 1}]})
    assert json3_to_text(raw) == "Hallo Welt"


def test_pick_subtitles_prefers_manual_and_video_language():
    info = {
        "language": "de",
        "subtitles": {"en": [{"ext": "vtt", "url": "manual-en"}]},
        "automatic_captions": {"de-orig": [{"ext": "json3", "url": "auto-de"}],
                               "ru-de": [{"ext": "vtt", "url": "translated"}]},
    }
    assert pick_subtitles(info) == ("manual-en", "vtt", "субтитры (en)")
    del info["subtitles"]
    assert pick_subtitles(info) == ("auto-de", "json3", "автосубтитры (de)")
    assert pick_subtitles({"automatic_captions": {"ja": [{"ext": "vtt", "url": "x"}]}}) is None


def test_chunking():
    text = "Короткое предложение. " * 2000
    chunks = chunk_text(text, 1000)
    assert all(len(c) <= 1000 for c in chunks) and len(chunks) > 40
    assert all(c.endswith(".") for c in chunks[:-1])
    assert len(chunk_text("а" * 2500, 1000)) == 3  # без точек — режем по длине


async def test_youtube_falls_back_to_whisper(monkeypatch):
    from .fakes import FakeSpeech

    speech = FakeSpeech("распознанный текст видео")
    yt = YouTube(speech)
    monkeypatch.setattr(YouTube, "_info", lambda self, url: {"title": "Видео", "duration": 120})

    def fake_audio(self, url, folder):
        from pathlib import Path

        path = Path(folder) / "audio.webm"
        path.write_bytes(b"audio")
        return path

    monkeypatch.setattr(YouTube, "_audio", fake_audio)
    t = await yt.transcript("https://youtu.be/dQw4w9WgXcQ")
    assert t.source == "распознано Whisper" and t.text == "распознанный текст видео"
    await yt.close()

    monkeypatch.setattr(YouTube, "_info", lambda self, url: {"title": "Лекция", "duration": 3600})
    with pytest.raises(Exception, match="45 мин"):
        await YouTube(speech).transcript("https://youtu.be/dQw4w9WgXcQ")


async def test_sending_a_link_summarizes_page(env):
    env.web.page_text = "Статья про Docker. " * 50
    await env.send(ADMIN, "https://example.com/docker")
    assert env.web.fetched == ["https://example.com/docker"]
    prompt = env.last_user_prompt()
    assert "Статья про Docker" in prompt and "пересказ" in prompt
    assert env.llm.calls[-1]["tools"] is None
    assert "Page title" in " ".join(env.texts())


async def test_youtube_summary_and_question(env):
    await env.send(ADMIN, "/sum https://youtu.be/dQw4w9WgXcQ что такое разыменование?")
    assert env.youtube.urls == ["https://youtu.be/dQw4w9WgXcQ"]
    texts = " ".join(env.texts())
    assert "Указатели в C" in texts and "12:34" in texts and "автосубтитры" in texts
    assert "что такое разыменование?" in env.last_user_prompt()


async def test_long_text_summarized_in_parts(env):
    env.youtube.text = "Очень длинная лекция про алгоритмы. " * 1200  # ~43 тыс. знаков
    env.llm.chat_reply = "- конспект части"
    await env.send(ADMIN, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    part_calls = [c for c in env.llm.chat_calls if "Это часть" in c["messages"][-1]["content"]]
    assert len(part_calls) == 4
    assert "конспекты частей" in env.last_user_prompt() and "Часть 4:" in env.last_user_prompt()


async def test_page_without_text(env):
    env.web.page_text = "мало"
    await env.send(ADMIN, "/sum https://example.com/app")
    assert "почти нет текста" in env.last_text()


async def test_auto_summary_can_be_disabled(make_env):
    env = await make_env(auto_summary=False)
    await env.send(ADMIN, "https://example.com/docker")
    assert not env.web.fetched
    assert env.last_user_prompt() == "https://example.com/docker"


async def test_youtube_bot_check_hint(monkeypatch):
    def blocked(self, url):
        raise RuntimeError("ERROR: [youtube] x: Sign in to confirm you’re not a bot")

    monkeypatch.setattr(YouTube, "_info", blocked)
    with pytest.raises(Exception, match="YOUTUBE_COOKIES"):
        await YouTube().transcript("https://youtu.be/dQw4w9WgXcQ")


def test_transcript_dataclass():
    assert Transcript("t", "x", "страница").duration is None
