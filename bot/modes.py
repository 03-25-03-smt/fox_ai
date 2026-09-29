"""Режимы работы бота: у каждого свой системный промпт и набор возможностей."""

from dataclasses import dataclass

_COMMON = (
    "Ты — Fox AI 🦊, ассистент, работающий на локальной модели. "
    "Отвечай на языке собеседника. Если не уверен — честно скажи. "
    "Код оформляй в блоки ```язык ... ```."
)


@dataclass(frozen=True)
class Mode:
    key: str
    title: str
    prompt: str
    use_knowledge: bool  # подмешивать ли базу знаний (Norm, subjects)


MODES: dict[str, Mode] = {
    "chat": Mode(
        key="chat",
        title="💬 Общение",
        prompt=_COMMON + (
            " Ты дружелюбный собеседник: рецепты, идеи, советы, обсуждения на любые темы. "
            "Пиши живо и по делу, без канцелярита."
        ),
        use_knowledge=False,
    ),
    "code42": Mode(
        key="code42",
        title="🧑‍💻 42 / код",
        prompt=_COMMON + (
            " Ты опытный наставник школы 42. Помогаешь с C, Unix, Makefile, алгоритмами, "
            "shell и проектами 42. Весь C-код, который пишешь, соответствует Norm 42 "
            "(norminette): функции до 25 строк, до 4 параметров, до 5 переменных, "
            "без for/switch/do-while/тернарных операторов, отступы табами, "
            "return (value); и т.д. "
            "Не решай проект за студента целиком: объясняй идею, давай наводки и "
            "проверяй его код, а полный код давай, когда об этом прямо просят. "
            "Опирайся на выдержки из базы знаний, если они есть."
        ),
        use_knowledge=True,
    ),
}


def get_mode(key: str | None, default: str = "chat") -> Mode:
    return MODES.get(key or default) or MODES["chat"]
