"""Учитель языков: личный словарь, интервальное повторение, тесты, произношение.

Языки — немецкий и чешский, объяснения на русском. Слова хранятся в таблице vocab
и повторяются по схеме Leitner: правильный ответ — слово переезжает в следующую
коробку и вернётся позже, ошибка — снова в первую коробку, повтор завтра.
"""

import datetime
import difflib
import html
import json
import random
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from .db import Database, to_db_time
from .llm import LLMError


@dataclass(frozen=True)
class Language:
    code: str
    name: str  # «немецкий»
    name_in: str  # «на немецком»
    adverb: str  # «по-немецки»
    flag: str
    default_level: str


LANGS: dict[str, Language] = {
    "de": Language("de", "немецкий", "на немецком", "по-немецки", "🇩🇪", "A2"),
    "cs": Language("cs", "чешский", "на чешском", "по-чешски", "🇨🇿", "A1"),
}
LANG_ALIASES = {
    "de": "de", "ger": "de", "deu": "de", "нем": "de", "немецкий": "de", "deutsch": "de",
    "cs": "cs", "cz": "cs", "cze": "cs", "чеш": "cs", "чешский": "cs", "česky": "cs", "cesky": "cs",
}
LEVELS = ("A1", "A2", "B1", "B2", "C1")

# Темы словаря: модель относит каждое слово к одной из них
TOPICS = {
    "еда": "🍎", "дом": "🏠", "работа": "💼", "учёба": "🎓", "город": "🏙", "транспорт": "🚆",
    "путешествия": "✈️", "покупки": "🛍", "природа": "🌿", "животные": "🐾", "здоровье": "🩺",
    "люди": "👪", "чувства": "💭", "время": "⏰", "спорт": "⚽", "техника": "💻", "другое": "📦",
}


def parse_topic(text: str) -> str | None:
    """«еда», «Еда», «#еда», «уч» -> тема; None — не тема."""
    t = text.strip().lstrip("#").casefold().replace("ё", "е")
    if len(t) < 2:
        return None
    for topic in TOPICS:
        if topic.replace("ё", "е").startswith(t):
            return topic
    return None


# Через сколько дней повторять слово из коробки N (0 — новое слово)
INTERVALS = (1, 1, 3, 7, 21, 60)
MAX_BOX = len(INTERVALS) - 1

GERMAN_ARTICLES = ("der", "die", "das")
_ARTICLE_RE = re.compile(r"^(der|die|das|ein|eine|sich)\s+", re.IGNORECASE)


def parse_lang(token: str | None) -> str | None:
    return LANG_ALIASES.get((token or "").strip().lower())


def split_lang_arg(args: str | None) -> tuple[str | None, str]:
    """«de Hund» -> ("de", "Hund"); «Hund» -> (None, "Hund")."""
    text = (args or "").strip()
    first, _, rest = text.partition(" ")
    lang = parse_lang(first)
    return (lang, rest.strip()) if lang else (None, text)


# ---------------------------------------------------------------- алфавит


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def bare_word(word: str) -> str:
    """Слово без артикля / sich: «der Hund» -> «Hund»."""
    return _ARTICLE_RE.sub("", word.strip()).strip()


def german_key(word: str) -> str:
    """DIN 5007-1: ä = a, ö = o, ü = u, ß = ss; артикль при сортировке не учитывается."""
    text = bare_word(word).casefold().replace("ß", "ss")
    return _strip_accents(text)


# Чешский алфавит: č, ř, š, ž и ch — отдельные буквы (ch идёт после h);
# á, é, ě, í, ó, ú, ů, ý, ď, ť, ň при сортировке равны базовым буквам.
_CZECH_ORDER = "a b c č d e f g h ch i j k l m n o p q r ř s š t u v w x y z ž".split()
_CZECH_INDEX = {letter: i for i, letter in enumerate(_CZECH_ORDER)}
_CZECH_BASE = str.maketrans("áéěíóúůýďťň", "aeeiouuydtn")


def czech_key(word: str) -> str:
    text = word.strip().casefold().translate(_CZECH_BASE)
    out, i = [], 0
    while i < len(text):
        pair = text[i:i + 2]
        if pair == "ch":
            letter, i = pair, i + 2
        else:
            letter, i = text[i], i + 1
        if letter in _CZECH_INDEX:
            out.append(chr(0x41 + _CZECH_INDEX[letter]))
        elif letter.isspace() or letter == "-":
            out.append(" ")
        elif letter.isalnum():
            out.append(chr(0x100 + ord(_strip_accents(letter)[:1] or letter)))
    return "".join(out)


def sort_key(lang: str, word: str) -> str:
    return czech_key(word) if lang == "cs" else german_key(word)


def first_letter(lang: str, word: str) -> str:
    """Буква словаря, под которой стоит слово (для чешского «ch» и «č» — отдельные)."""
    text = (bare_word(word) if lang == "de" else word.strip()).casefold()
    if lang == "cs":
        if text.startswith("ch"):
            return "CH"
        letter = text[:1]
        return (letter if letter in "čřšž" else letter.translate(_CZECH_BASE)).upper()
    return _strip_accents(text[:1].replace("ß", "s")).upper()


# ---------------------------------------------------------------- данные


@dataclass(frozen=True)
class Word:
    id: int
    lang: str
    word: str
    translation: str
    pos: str
    grammar: str
    examples: list[dict[str, str]]
    tip: str
    box: int
    due: str
    correct: int
    wrong: int
    topic: str = ""

    @property
    def topic_label(self) -> str:
        topic = self.topic if self.topic in TOPICS else "другое"
        return f"{TOPICS[topic]} {topic}"

    @property
    def article(self) -> str | None:
        """Артикль немецкого существительного (der/die/das) или None."""
        if self.lang != "de":
            return None
        first = self.word.split(maxsplit=1)[0].lower() if self.word else ""
        return first if first in GERMAN_ARTICLES and " " in self.word else None


_WORD_COLS = "id, lang, word, translation, pos, grammar, examples, tip, box, due, correct, wrong, topic"


def _word(row: Any) -> Word:
    data = list(row)
    try:
        examples = json.loads(data[6] or "[]")
    except ValueError:
        examples = []
    data[6] = [e for e in examples if isinstance(e, dict) and e.get("text")]
    return Word(*data)


@dataclass
class LangState:
    current: str = "de"
    daily: bool = True
    plan_day: str | None = None
    plan_at: datetime.datetime | None = None
    plan_count: int = 0
    plan_words: list[int] = field(default_factory=list)
    plan_sent: bool = False
    streak: int = 0
    last_review: str | None = None


@dataclass
class Pending:
    """Чего бот ждёт от ученика следующим сообщением."""
    kind: str  # "speak" — голосовое с фразой, "sentence" — своё предложение со словом
    lang: str
    text: str  # фраза для произношения или слово для предложения
    word_id: int | None = None


@dataclass
class QuizItem:
    word: Word
    kind: str  # tr: слово -> перевод, rev: перевод -> слово, art: артикль, self: помню / не помню
    question: str
    options: list[str]
    answer: int


@dataclass
class QuizSession:
    words: list[Word]
    daily: bool = False
    index: int = 0
    correct: int = 0
    wrong: list[Word] = field(default_factory=list)
    current: QuizItem | None = None

    @property
    def finished(self) -> bool:
        return self.index >= len(self.words)


class LangStore:
    def __init__(self, db: Database, rng: random.Random | None = None) -> None:
        self.db = db
        self.rng = rng or random.Random()
        self.pending: dict[int, Pending] = {}
        self.quizzes: dict[int, QuizSession] = {}
        self.readings: dict[int, "Reading"] = {}  # последний текст для чтения

    # ------------------------------------------------------------ уровень и язык

    async def get_level(self, user_id: int, lang: str) -> str:
        row = await self.db._fetchone(
            "SELECT level FROM lang_levels WHERE user_id = ? AND lang = ?", (user_id, lang)
        )
        return row[0] if row else LANGS[lang].default_level

    async def set_level(self, user_id: int, lang: str, level: str) -> None:
        await self.db._exec(
            "INSERT INTO lang_levels (user_id, lang, level) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, lang) DO UPDATE SET level = excluded.level",
            (user_id, lang, level),
        )

    async def get_state(self, user_id: int) -> LangState:
        row = await self.db._fetchone(
            "SELECT current, daily, plan_day, plan_at, plan_words, plan_sent, streak, last_review, "
            "plan_count "
            "FROM lang_state WHERE user_id = ?", (user_id,)
        )
        if row is None:
            return LangState()
        plan_at = (datetime.datetime.strptime(row[3], "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.UTC)
                   if row[3] else None)
        return LangState(
            current=row[0] if row[0] in LANGS else "de", daily=bool(row[1]), plan_day=row[2],
            plan_at=plan_at, plan_words=json.loads(row[4] or "[]"), plan_sent=bool(row[5]),
            streak=row[6], last_review=row[7], plan_count=row[8],
        )

    async def _update_state(self, user_id: int, **fields: Any) -> None:
        await self.db._exec("INSERT OR IGNORE INTO lang_state (user_id) VALUES (?)", (user_id,))
        sets = ", ".join(f"{name} = ?" for name in fields)  # имена полей — только из кода
        await self.db._exec(f"UPDATE lang_state SET {sets} WHERE user_id = ?", (*fields.values(), user_id))

    async def set_current(self, user_id: int, lang: str) -> None:
        await self._update_state(user_id, current=lang)

    async def set_daily(self, user_id: int, on: bool) -> None:
        await self._update_state(user_id, daily=int(on))

    # ------------------------------------------------------------ словарь

    async def add_word(self, user_id: int, entry: dict[str, Any], today: str) -> tuple[Word, bool]:
        """Сохраняет слово. (слово, True) — новое; (уже сохранённое, False) — было в словаре."""
        lang, word = entry["lang"], entry["word"]
        lemma = bare_word(entry.get("lemma") or word).casefold()
        if existing := await self.find_word(user_id, lang, lemma):
            return existing, False
        examples = json.dumps(entry.get("examples") or [], ensure_ascii=False)
        cur = await self.db._exec(
            "INSERT INTO vocab (user_id, lang, word, lemma, sort_key, translation, pos, grammar, "
            "examples, tip, due, topic) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, lang, word, lemma, sort_key(lang, word), entry["translation"],
             entry.get("pos", ""), entry.get("grammar", ""), examples, entry.get("tip", ""),
             _add_days(today, INTERVALS[0]), entry.get("topic") if entry.get("topic") in TOPICS else "другое"),
        )
        return await self.get_word(user_id, int(cur.lastrowid)), True

    async def find_word(self, user_id: int, lang: str, lemma: str) -> Word | None:
        row = await self.db._fetchone(
            f"SELECT {_WORD_COLS} FROM vocab WHERE user_id = ? AND lang = ? AND lemma = ?",
            (user_id, lang, bare_word(lemma).casefold()),
        )
        return _word(row) if row else None

    async def get_word(self, user_id: int, word_id: int) -> Word | None:
        row = await self.db._fetchone(
            f"SELECT {_WORD_COLS} FROM vocab WHERE user_id = ? AND id = ?", (user_id, word_id)
        )
        return _word(row) if row else None

    async def delete_word(self, user_id: int, word_id: int) -> bool:
        cur = await self.db._exec("DELETE FROM vocab WHERE user_id = ? AND id = ?", (user_id, word_id))
        return cur.rowcount > 0

    async def set_topic(self, user_id: int, word_id: int, topic: str) -> bool:
        cur = await self.db._exec("UPDATE vocab SET topic = ? WHERE user_id = ? AND id = ?", (topic, user_id, word_id))
        return cur.rowcount > 0

    async def topic_counts(self, user_id: int, lang: str) -> dict[str, int]:
        rows = await self.db._fetchall(
            "SELECT topic, COUNT(*) FROM vocab WHERE user_id = ? AND lang = ? GROUP BY topic", (user_id, lang)
        )
        out: dict[str, int] = {}
        for topic, n in rows:
            key = topic if topic in TOPICS else "другое"
            out[key] = out.get(key, 0) + n
        return out

    async def list_words(self, user_id: int, lang: str) -> list[Word]:
        """Словарь в алфавитном порядке языка."""
        rows = await self.db._fetchall(
            f"SELECT {_WORD_COLS} FROM vocab WHERE user_id = ? AND lang = ? "
            "ORDER BY sort_key, word COLLATE NOCASE", (user_id, lang)
        )
        return [_word(r) for r in rows]

    async def counts(self, user_id: int, today: str) -> dict[str, tuple[int, int]]:
        """{язык: (всего слов, к повторению сегодня)}."""
        rows = await self.db._fetchall(
            "SELECT lang, COUNT(*), SUM(due <= ?) FROM vocab WHERE user_id = ? GROUP BY lang",
            (today, user_id),
        )
        return {lang: (total, due or 0) for lang, total, due in rows}

    async def review_words(self, user_id: int, today: str, count: int, lang: str | None = None,
                           topic: str | None = None) -> list[Word]:
        """Слова на повторение: сначала просроченные, затем те, что знаешь хуже всего."""
        where, params = "user_id = ?", [user_id]
        if lang:
            where += " AND lang = ?"
            params.append(lang)
        if topic == "другое":
            where += f" AND topic NOT IN ({', '.join('?' for t in TOPICS if t != 'другое')})"
            params += [t for t in TOPICS if t != "другое"]
        elif topic:
            where += " AND topic = ?"
            params.append(topic)
        rows = await self.db._fetchall(
            f"SELECT {_WORD_COLS} FROM vocab WHERE {where} "
            "ORDER BY (due <= ?) DESC, due, box, wrong - correct DESC LIMIT ?",
            (*params, today, count),
        )
        words = [_word(r) for r in rows]
        self.rng.shuffle(words)
        return words

    async def words_by_ids(self, user_id: int, ids: list[int]) -> list[Word]:
        words = [await self.get_word(user_id, i) for i in ids]
        return [w for w in words if w is not None]

    async def record_answer(self, user_id: int, word: Word, correct: bool, today: str) -> int:
        """Обновляет коробку слова. Возвращает, через сколько дней повтор."""
        box = min(word.box + 1, MAX_BOX) if correct else 1
        days = INTERVALS[box]
        await self.db._exec(
            "UPDATE vocab SET box = ?, due = ?, correct = correct + ?, wrong = wrong + ? "
            "WHERE user_id = ? AND id = ?",
            (box, _add_days(today, days), int(correct), int(not correct), user_id, word.id),
        )
        return days

    async def recent_words(self, user_id: int, lang: str, limit: int = 30) -> list[Word]:
        rows = await self.db._fetchall(
            f"SELECT {_WORD_COLS} FROM vocab WHERE user_id = ? AND lang = ? ORDER BY id DESC LIMIT ?",
            (user_id, lang, limit),
        )
        return [_word(r) for r in rows]

    async def weak_words(self, user_id: int, lang: str, limit: int = 10) -> list[Word]:
        rows = await self.db._fetchall(
            f"SELECT {_WORD_COLS} FROM vocab WHERE user_id = ? AND lang = ? AND wrong > 0 "
            "ORDER BY wrong - correct DESC, wrong DESC LIMIT ?",
            (user_id, lang, limit),
        )
        return [_word(r) for r in rows]

    # ------------------------------------------------------------ уроки

    async def log_lesson(self, user_id: int, lang: str, kind: str, topic: str) -> None:
        await self.db._exec(
            "INSERT INTO lang_log (user_id, lang, kind, topic) VALUES (?, ?, ?, ?)",
            (user_id, lang, kind, topic[:200]),
        )

    async def recent_lessons(self, user_id: int, lang: str, limit: int = 10) -> list[str]:
        rows = await self.db._fetchall(
            "SELECT kind, topic FROM lang_log WHERE user_id = ? AND lang = ? ORDER BY id DESC LIMIT ?",
            (user_id, lang, limit),
        )
        return [f"{kind}: {topic}" for kind, topic in rows]

    async def context_for(self, user_id: int) -> str:
        """Блок системного промпта режима «Учитель языков»."""
        state = await self.get_state(user_id)
        current = LANGS[state.current]
        lines = [f"Сейчас ученик занимается языком: {current.name} {current.flag}."]
        for code, lang in LANGS.items():
            lines.append(f"Уровень ученика ({lang.name}): {await self.get_level(user_id, code)}.")
        words = await self.recent_words(user_id, current.code, 30)
        if words:
            lines.append("Слова из его словаря (используй их в примерах и заданиях): "
                         + ", ".join(f"{w.word} — {w.translation}" for w in words))
        weak = await self.weak_words(user_id, current.code, 10)
        if weak:
            lines.append("Слова, в которых он часто ошибается: " + ", ".join(w.word for w in weak))
        lessons = await self.recent_lessons(user_id, current.code, 8)
        if lessons:
            lines.append("Недавние уроки и тесты (не повторяй их без просьбы): " + "; ".join(lessons))
        return "\n".join(lines)

    # ------------------------------------------------------------ ежедневное повторение

    def plan_time(self, day: datetime.date, tz: datetime.tzinfo, start_hour: int, end_hour: int) -> datetime.datetime:
        """Случайный момент дня в окне [start_hour:00, end_hour:00] по местному времени."""
        start = datetime.datetime.combine(day, datetime.time(start_hour), tzinfo=tz)
        minutes = max((end_hour - start_hour) * 60, 0)
        return start + datetime.timedelta(minutes=self.rng.randint(0, minutes))

    async def save_plan(self, user_id: int, day: str, at: datetime.datetime, count: int) -> None:
        await self._update_state(
            user_id, plan_day=day, plan_at=to_db_time(at), plan_count=count, plan_words="[]", plan_sent=0
        )

    async def mark_plan_sent(self, user_id: int, words: list[int]) -> None:
        await self._update_state(user_id, plan_sent=1, plan_words=json.dumps(words))

    async def finish_review(self, user_id: int, today: str) -> int:
        """Отмечает, что сегодня было повторение. Возвращает серию дней подряд."""
        state = await self.get_state(user_id)
        if state.last_review == today:
            return state.streak
        streak = state.streak + 1 if state.last_review == _add_days(today, -1) else 1
        await self._update_state(user_id, streak=streak, last_review=today)
        return streak

    # ------------------------------------------------------------ тест по словарю

    async def start_quiz(self, user_id: int, words: list[Word], *, daily: bool = False) -> QuizSession:
        session = QuizSession(words=list(words), daily=daily)
        self.quizzes[user_id] = session
        return session

    async def next_question(self, user_id: int) -> QuizItem | None:
        session = self.quizzes.get(user_id)
        if session is None or session.finished:
            return None
        word = session.words[session.index]
        pool = await self.list_words(user_id, word.lang)
        session.current = build_question(word, pool, self.rng)
        return session.current


def build_question(word: Word, pool: list[Word], rng: random.Random) -> QuizItem:
    """Вопрос по слову; варианты ответа — другие слова из словаря того же языка."""
    others = [w for w in pool if w.id != word.id]
    if word.article and rng.random() < 0.35:
        noun = html.escape(bare_word(word.word))
        return QuizItem(word, "art", f"Какой артикль? <b>{noun}</b> ({html.escape(word.translation)})",
                        list(GERMAN_ARTICLES), GERMAN_ARTICLES.index(word.article))
    if not others:
        return QuizItem(word, "self", f"Помнишь перевод? <b>{html.escape(word.word)}</b>",
                        ["✅ Помню", "❌ Не помню"], 0)
    kind = rng.choice(("tr", "rev"))
    attr = "translation" if kind == "tr" else "word"
    correct = getattr(word, attr)
    distractors = list({getattr(w, attr) for w in others if getattr(w, attr) != correct})
    rng.shuffle(distractors)
    options = [correct, *distractors[:3]]
    rng.shuffle(options)
    flag = LANGS[word.lang].flag
    question = (f"{flag} Как переводится <b>{html.escape(word.word)}</b>?" if kind == "tr"
                else f"{flag} Как {LANGS[word.lang].adverb}: <b>{html.escape(word.translation)}</b>?")
    return QuizItem(word, kind, question, options, options.index(correct))


# ---------------------------------------------------------------- произношение


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", text.casefold())


@dataclass(frozen=True)
class SpeechCheck:
    score: int  # 0..100
    missed: list[str]  # слова фразы, которых не было в распознанном


def compare_speech(expected: str, heard: str) -> SpeechCheck:
    """Сравнивает фразу с тем, что распознал Whisper, по словам."""
    want, got = _words(expected), _words(heard)
    if not want:
        return SpeechCheck(100, [])
    matcher = difflib.SequenceMatcher(a=want, b=got, autojunk=False)
    matched = set()
    for block in matcher.get_matching_blocks():
        matched.update(range(block.a, block.a + block.size))
    missed = [w for i, w in enumerate(want) if i not in matched]
    return SpeechCheck(round(100 * len(matched) / len(want)), missed)


# ---------------------------------------------------------------- запросы к модели


class TutorError(LLMError):
    """Модель вернула ответ, который не получилось разобрать."""


def lookup_messages(query: str, lang_hint: str, forced: bool, level: str) -> list[dict[str, str]]:
    lang = LANGS[lang_hint]
    which = (f"Слово {lang.name_in} или по-русски." if forced else
             f"Язык слова: немецкий (de) или чешский (cs); если неясно — скорее {lang.name} ({lang.code}).")
    return [
        {"role": "system", "content": (
            "Ты — точный двуязычный словарь и преподаватель для русскоязычного ученика. "
            "Отвечаешь только JSON без пояснений."
        )},
        {"role": "user", "content": (
            f"Запрос ученика: «{query}». {which}\n"
            f"Если запрос по-русски — переведи его на {LANGS[lang_hint].name} язык.\n"
            "Верни JSON:\n"
            '{"lang": "de" или "cs", '
            '"word": "словарная форма; немецкое существительное с артиклем (der Hund), '
            'возвратный глагол с sich", '
            '"lemma": "то же без артикля", '
            '"translation": "перевод на русский, 1–3 значения через запятую", '
            '"pos": "часть речи по-русски кратко", '
            '"grammar": "главное: для сущ. — род и мн. ч. (немецкий) или род и родительный '
            'падеж (чешский); для глаголов — 3 л. ед. ч. и прошедшее (Perfekt / минулý čas); '
            'иначе пусто", '
            f'"examples": [3 объекта {{"text": "простое предложение уровня {level} с этим словом", '
            '"ru": "перевод"}], '
            '"tip": "одна короткая подсказка по-русски: как запомнить, частая ошибка или '
            'устойчивое сочетание", '
            f'"topic": "тема слова, одна из: {", ".join(TOPICS)}"}}'
        )},
    ]


def parse_lookup(raw: str, lang_hint: str, forced: bool) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise TutorError("модель вернула не JSON") from exc
    if not isinstance(data, dict):
        raise TutorError("модель вернула не JSON-объект")
    word = str(data.get("word") or "").strip()
    translation = str(data.get("translation") or "").strip()
    if not word or not translation:
        raise TutorError("модель не нашла перевод")
    lang = data.get("lang") if data.get("lang") in LANGS and not forced else lang_hint
    examples = []
    for ex in data.get("examples") or []:
        if isinstance(ex, dict) and str(ex.get("text") or "").strip():
            examples.append({"text": str(ex["text"]).strip(), "ru": str(ex.get("ru") or "").strip()})
    return {
        "lang": lang,
        "word": word[:100],
        "lemma": str(data.get("lemma") or word).strip()[:100],
        "translation": translation[:200],
        "pos": str(data.get("pos") or "").strip()[:40],
        "grammar": str(data.get("grammar") or "").strip()[:200],
        "examples": examples[:3],
        "tip": str(data.get("tip") or "").strip()[:300],
        "topic": parse_topic(str(data.get("topic") or "")) or "другое",
    }


def phrase_messages(lang: str, level: str, words: list[str]) -> list[dict[str, str]]:
    use = f" Используй одно из слов: {', '.join(words)}." if words else ""
    return [
        {"role": "system", "content": "Ты преподаватель языка. Отвечаешь только JSON."},
        {"role": "user", "content": (
            f"Придумай одно короткое (5–10 слов) естественное предложение {LANGS[lang].name_in} "
            f"уровня {level} для тренировки произношения.{use} "
            'Верни JSON: {"text": "предложение", "ru": "перевод на русский"}'
        )},
    ]


def parse_phrase(raw: str) -> tuple[str, str]:
    try:
        data = json.loads(raw)
        text = str(data.get("text") or "").strip()
    except (ValueError, AttributeError) as exc:
        raise TutorError("модель вернула не JSON") from exc
    if not text:
        raise TutorError("модель не придумала фразу")
    return text[:300], str(data.get("ru") or "").strip()[:300]


def _add_days(day: str, days: int) -> str:
    return (datetime.date.fromisoformat(day) + datetime.timedelta(days=days)).isoformat()


# ---------------------------------------------------------------- тексты для чтения


def reading_messages(lang: str, level: str, topic: str, known: list[str]) -> list[dict[str, str]]:
    words = f" Обязательно используй несколько слов, которые ученик уже знает: {', '.join(known)}." if known else ""
    return [
        {"role": "system", "content": "Ты преподаватель языка и пишешь тексты для чтения. Отвечаешь только JSON."},
        {"role": "user", "content": (
            f"Напиши связный интересный текст {LANGS[lang].name_in} для ученика уровня {level} на тему «{topic}»: "
            f"{'60–100' if level == 'A1' else '100–160' if level == 'A2' else '150–220'} слов, короткие предложения, "
            f"грамматика строго уровня {level}, 5–8 новых для ученика полезных слов.{words}\n"
            'JSON: {"title": "заголовок на языке", "text": "текст", "ru": "перевод всего текста на русский", '
            '"new_words": [{"word": "словарная форма (немецкие сущ. с артиклем)", "translation": "перевод", '
            '"pos": "часть речи"}], "questions": ["3 вопроса по тексту на изучаемом языке"]}'
        )},
    ]


@dataclass
class Reading:
    lang: str
    topic: str
    title: str
    text: str
    ru: str
    new_words: list[dict[str, str]]
    questions: list[str]


def parse_reading(raw: str, lang: str, topic: str) -> Reading:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise TutorError("модель вернула не JSON") from exc
    text = str((data or {}).get("text") or "").strip() if isinstance(data, dict) else ""
    if len(text) < 40:
        raise TutorError("модель не написала текст")
    words = [{"word": str(w.get("word") or "").strip()[:100], "translation": str(w.get("translation") or "").strip()[:200],
              "pos": str(w.get("pos") or "").strip()[:40]}
             for w in data.get("new_words") or [] if isinstance(w, dict) and w.get("word") and w.get("translation")]
    questions = [str(q).strip() for q in data.get("questions") or [] if str(q).strip()]
    return Reading(lang, topic, str(data.get("title") or "").strip()[:100] or topic, text[:3000],
                   str(data.get("ru") or "").strip()[:3500], words[:10], questions[:5])
