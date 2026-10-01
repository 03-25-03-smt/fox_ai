"""Учитель языков: алфавит, повторение, словарь, тесты, произношение, ежедневная рассылка."""

import datetime
import json
import random

from aiogram.methods import SendMessage, SendVoice

from bot.lang import (
    LangStore,
    TutorError,
    Word,
    build_question,
    compare_speech,
    czech_key,
    first_letter,
    german_key,
    parse_lookup,
    split_lang_arg,
)
from bot.tasks import send_daily_reviews

from .conftest import ADMIN, FRIEND

HUND = {
    "lang": "de", "word": "der Hund", "lemma": "Hund", "translation": "собака",
    "pos": "сущ.", "grammar": "м. р., мн. die Hunde",
    "examples": [{"text": "Der Hund schläft.", "ru": "Собака спит."}],
    "tip": "Hund ~ англ. hound",
}


def last_text(env) -> str:
    """Последний текст (голосовые с озвучкой идут без подписи)."""
    return [t for t in env.texts() if t][-1]


def mode_buttons(env) -> str:
    markup = env.session.of_type(SendMessage)[-1].reply_markup
    return " ".join(b.text for row in markup.inline_keyboard for b in row)


def entry(word: str, translation: str, lang: str = "de", **extra) -> dict:
    return {"lang": lang, "word": word, "translation": translation, "examples": [], **extra}


# ---------------------------------------------------------------- чистая логика


def test_german_alphabet_ignores_articles_and_umlauts():
    words = ["die Zeit", "der Apfel", "das Öl", "sich freuen", "die Straße", "der Ofen"]
    ordered = sorted(words, key=german_key)
    assert ordered == ["der Apfel", "sich freuen", "der Ofen", "das Öl", "die Straße", "die Zeit"]
    assert first_letter("de", "das Öl") == "O"


def test_czech_alphabet_has_its_own_letters():
    words = ["chleba", "cesta", "čaj", "hrad", "řeka", "rok", "šest", "salát", "žena", "zima", "dům"]
    ordered = sorted(words, key=czech_key)
    assert ordered == ["cesta", "čaj", "dům", "hrad", "chleba", "rok", "řeka", "salát", "šest", "zima", "žena"]
    assert first_letter("cs", "chleba") == "CH"
    assert first_letter("cs", "čaj") == "Č"
    assert first_letter("cs", "áno") == "A"


def test_split_lang_arg():
    assert split_lang_arg("cs pes") == ("cs", "pes")
    assert split_lang_arg("немецкий собака") == ("de", "собака")
    assert split_lang_arg("Hund") == (None, "Hund")


def test_parse_lookup_validates_and_trims():
    data = parse_lookup(json.dumps({**HUND, "lang": "cs"}), "de", forced=True)
    assert data["lang"] == "de"  # язык задан явно — модель его не меняет
    assert data["examples"] == [{"text": "Der Hund schläft.", "ru": "Собака спит."}]
    assert parse_lookup(json.dumps({**HUND, "lang": "cs"}), "de", forced=False)["lang"] == "cs"
    for bad in ("не json", "[]", json.dumps({"word": "x"})):
        try:
            parse_lookup(bad, "de", forced=False)
        except TutorError:
            continue
        raise AssertionError(bad)


def test_compare_speech_finds_missed_words():
    check = compare_speech("Der Hund schläft im Garten.", "der hund schläft garten")
    assert check.score == 80 and check.missed == ["im"]
    assert compare_speech("Dobrý den!", "Dobrý den").score == 100


def _word(i: int, word: str, translation: str, lang: str = "de") -> Word:
    return Word(i, lang, word, translation, "", "", [], "", 0, "2026-01-01", 0, 0)


def test_quiz_questions():
    rng = random.Random(1)
    hund = _word(1, "der Hund", "собака")
    pool = [hund, _word(2, "die Katze", "кошка"), _word(3, "das Haus", "дом"), _word(4, "gehen", "идти")]
    kinds = set()
    for _ in range(40):
        q = build_question(hund, pool, rng)
        kinds.add(q.kind)
        if q.kind == "art":
            assert q.options[q.answer] == "der"
        else:
            assert q.options[q.answer] == ("собака" if q.kind == "tr" else "der Hund")
            assert len(set(q.options)) == len(q.options) == 4
    assert kinds == {"art", "tr", "rev"}
    alone = build_question(_word(5, "pes", "собака", "cs"), [], rng)
    assert alone.kind == "self"


async def test_leitner_intervals(env):
    store = LangStore(env.db)
    await env.db.add_user(ADMIN)
    word, created = await store.add_word(ADMIN, HUND, "2026-10-01")
    assert created and word.due == "2026-10-02" and word.article == "der"
    _, again = await store.add_word(ADMIN, {**HUND, "word": "Hund"}, "2026-10-01")
    assert not again  # то же слово без артикля — дубликат
    assert await store.record_answer(ADMIN, word, True, "2026-10-02") == 1
    word = await store.get_word(ADMIN, word.id)
    assert await store.record_answer(ADMIN, word, True, "2026-10-03") == 3
    word = await store.get_word(ADMIN, word.id)
    assert word.box == 2 and word.due == "2026-10-06"
    await store.record_answer(ADMIN, word, False, "2026-10-06")
    word = await store.get_word(ADMIN, word.id)
    assert word.box == 1 and word.due == "2026-10-07" and word.wrong == 1


async def test_streak(env):
    store = LangStore(env.db)
    await env.db.add_user(ADMIN)
    assert await store.finish_review(ADMIN, "2026-10-01") == 1
    assert await store.finish_review(ADMIN, "2026-10-01") == 1
    assert await store.finish_review(ADMIN, "2026-10-02") == 2
    assert await store.finish_review(ADMIN, "2026-10-05") == 1


# ---------------------------------------------------------------- через Telegram


async def test_word_lookup_saves_and_speaks(env):
    env.llm.facts_json = json.dumps(HUND)
    await env.send(ADMIN, "/w Hund")
    card = last_text(env)
    assert "der Hund" in card and "собака" in card and "Der Hund schläft." in card
    assert "Сохранил в словарь" in card
    assert env.speech.voices == ["de"] and "Der Hund schläft." in env.speech.synthesized[0]
    assert env.llm.chat_calls[-1]["model"] == "qwen2.5:7b"

    calls = len(env.llm.chat_calls)
    await env.send(ADMIN, "/w Hund")  # повторно модель не спрашиваем
    assert "Уже есть в словаре" in last_text(env) and len(env.llm.chat_calls) == calls


async def test_lang_is_private(env):
    await env.send(FRIEND, "/w Hund")
    assert "только владельцу" in env.last_text()
    await env.send(FRIEND, "/mode")
    assert "Учитель языков" not in mode_buttons(env) and "Защита" in mode_buttons(env)
    await env.send(ADMIN, "/mode")
    assert "Учитель языков" in mode_buttons(env)
    await env.click(FRIEND, "mode:lang")
    assert (await env.db.get_user(FRIEND)).mode is None


async def test_dictionary_alphabetical(env):
    store = env.assistant.lang
    await env.db.add_user(ADMIN)
    for w, t in (("die Zeit", "время"), ("der Apfel", "яблоко"), ("das Öl", "масло")):
        await store.add_word(ADMIN, entry(w, t), "2026-10-01")
    await store.add_word(ADMIN, entry("pes", "собака", "cs"), "2026-10-01")
    await env.send(ADMIN, "/dict")
    text = env.last_text()
    assert text.index("Apfel") < text.index("Öl") < text.index("Zeit")
    assert "pes" not in text
    await env.send(ADMIN, "/dict cs")
    assert "pes" in env.last_text()
    await env.send(ADMIN, "/dict de Z")
    assert "Zeit" in env.last_text() and "Apfel" not in env.last_text()


async def _quiz_answer(env) -> tuple[int, int]:
    """Номер правильного варианта и неправильного для текущего вопроса."""
    item = env.assistant.lang.quizzes[ADMIN].current
    return item.answer, (item.answer + 1) % len(item.options)


async def test_quiz_flow(env):
    store = env.assistant.lang
    await env.db.add_user(ADMIN)
    for w, t in (("die Katze", "кошка"), ("das Haus", "дом"), ("gehen", "идти")):
        await store.add_word(ADMIN, entry(w, t), "2026-01-01")
    await env.send(ADMIN, "/quiz 2")
    assert "Повторяем 2 сл." in " ".join(env.texts())
    right, wrong = await _quiz_answer(env)
    await env.click(ADMIN, f"lq:a:{right}")
    assert "✅ Верно" in " ".join(env.texts()[-2:])
    right, wrong = await _quiz_answer(env)
    await env.click(ADMIN, f"lq:a:{wrong}")
    texts = " ".join(env.texts()[-2:])
    assert "Итог: 1/2" in texts and "Повторим завтра" in texts
    assert ADMIN not in store.quizzes
    assert (await store.get_state(ADMIN)).streak == 1

    await env.click(ADMIN, "lq:a:0")  # тест закончился — кнопка больше не считается
    assert (await store.get_state(ADMIN)).streak == 1


async def test_lesson_switches_mode_and_uses_dictionary(env):
    store = env.assistant.lang
    await env.db.add_user(ADMIN)
    await store.add_word(ADMIN, entry("der Hund", "собака"), "2026-10-01")
    await env.send(ADMIN, "/lesson de Perfekt")
    assert (await env.db.get_user(ADMIN)).mode == "lang"
    system = env.system_prompt()
    assert "преподаватель немецкого и чешского" in system
    assert "Уровень ученика (немецкий): A2" in system
    assert "der Hund — собака" in system
    assert "Perfekt" in env.last_user_prompt()
    assert await store.recent_lessons(ADMIN, "de") == ["lesson: Perfekt"]

    await env.send(ADMIN, "/lang cs A1")
    await env.send(ADMIN, "Ahoj, jak se máš?")
    assert "чешский" in env.system_prompt()
    assert env.llm.calls[-1]["model"] == "qwen2.5:7b"


async def test_sentence_check(env):
    env.llm.facts_json = json.dumps(HUND)
    await env.send(ADMIN, "/w Hund")
    word_id = (await env.assistant.lang.find_word(ADMIN, "de", "Hund")).id
    await env.click(ADMIN, f"lw:sent:{word_id}")
    assert "Напиши своё предложение" in env.last_text()
    await env.send(ADMIN, "Ich habe ein Hund.")
    prompt = env.last_user_prompt()
    assert "der Hund" in prompt and "Ich habe ein Hund." in prompt
    assert ADMIN not in env.assistant.lang.pending


async def test_speak_checks_pronunciation(env):
    store = env.assistant.lang
    await env.db.add_user(ADMIN)
    await store.add_word(ADMIN, HUND, "2026-01-01")
    await env.send(ADMIN, "/speak de")
    assert "Der Hund schläft." in " ".join(env.texts())
    assert env.speech.voices[-1] == "de"

    env.speech.text = "der Hund schläft"
    await env.send_voice(ADMIN)
    assert env.speech.languages[-1] == "de"  # Whisper слушает именно немецкий
    assert "100%" in env.last_text()
    env.speech.text = "der Mund"
    await env.send_voice(ADMIN)
    assert "Не расслышал" in env.last_text() and "schläft" in env.last_text()

    await env.click(ADMIN, "ls:stop")
    assert ADMIN not in store.pending
    env.speech.text = "напомни через 10 минут выключить плиту"
    await env.send_voice(ADMIN)  # обычное голосовое снова идёт в чат
    assert env.speech.languages[-1] is None


async def test_daily_review_random_window(env):
    store = env.assistant.lang
    store.rng = random.Random(7)
    await env.db.add_user(ADMIN)
    for i in range(20):
        await store.add_word(ADMIN, entry(f"wort{i}", f"слово{i}"), "2026-01-01")
    paris = env.assistant.tz(None)
    morning = datetime.datetime(2026, 10, 1, 6, 0, tzinfo=paris)
    assert await send_daily_reviews(env.bot, env.app, morning) == 0
    state = await store.get_state(ADMIN)
    local_at = state.plan_at.astimezone(paris)
    assert 8 <= local_at.hour <= 17 and 5 <= state.plan_count <= 15

    assert await send_daily_reviews(env.bot, env.app, state.plan_at) == 1
    push = env.session.of_type(SendMessage)[-1]
    assert push.chat_id == ADMIN and f"Сегодня {state.plan_count}" in push.text
    assert await send_daily_reviews(env.bot, env.app, state.plan_at) == 0  # раз в день

    await env.click(ADMIN, "lq:daily")
    assert len(store.quizzes[ADMIN].words) == state.plan_count and store.quizzes[ADMIN].daily

    tomorrow = morning + datetime.timedelta(days=1)
    await send_daily_reviews(env.bot, env.app, tomorrow)
    assert (await store.get_state(ADMIN)).plan_day == "2026-10-02"

    await env.send(ADMIN, "/lang daily off")
    assert await send_daily_reviews(env.bot, env.app, tomorrow + datetime.timedelta(hours=12)) == 0


async def test_daily_review_not_late_evening(env):
    store = env.assistant.lang
    await env.db.add_user(ADMIN)
    paris = env.assistant.tz(None)
    await store.save_plan(ADMIN, "2026-10-01", datetime.datetime(2026, 10, 1, 9, 0, tzinfo=paris), 5)
    late = datetime.datetime(2026, 10, 1, 22, 0, tzinfo=paris)  # бот был выключен весь день
    assert await send_daily_reviews(env.bot, env.app, late) == 0
    assert (await store.get_state(ADMIN)).plan_sent
    assert not env.session.of_type(SendVoice)


# ---------------------------------------------------------------- сервис речи


def test_speech_service_languages():
    from fastapi.testclient import TestClient

    from .test_units import load_service

    speech = load_service("speech")
    assert speech._voice_path("de").name == "de_DE-thorsten-medium.onnx"
    assert speech._voice_path("cs").name == "cs_CZ-jirka-medium.onnx"
    client = TestClient(speech.app)  # без lifespan: модели не грузим
    assert client.post("/tts", json={"text": "Hallo", "lang": "fr"}).status_code == 400
    resp = client.post("/stt", params={"language": "xx"}, files={"file": ("v.ogg", b"x")})
    assert resp.status_code == 400


async def test_speech_client_sends_language():
    import respx

    from bot.services import SpeechClient

    with respx.mock:
        stt = respx.post("http://sp/stt").respond(json={"text": "Hallo"})
        tts = respx.post("http://sp/tts").respond(content=b"OggS")
        client = SpeechClient("http://sp")
        assert await client.transcribe(b"x", language="de") == "Hallo"
        assert await client.synthesize("Ahoj", lang="cs") == b"OggS"
        await client.close()
    assert stt.calls.last.request.url.params["language"] == "de"
    assert json.loads(tts.calls.last.request.content) == {"text": "Ahoj", "lang": "cs"}
