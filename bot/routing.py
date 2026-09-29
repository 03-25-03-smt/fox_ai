"""Автовыбор модели под запрос: код -> coder-модель, болтовня -> быстрая, остальное -> основная."""

import re
from dataclasses import dataclass

CODE_PATTERNS = re.compile(
    r"```|#include|\bint\s+main\b|\bmalloc\b|\bfree\(|\bsegfault|\bsegmentation\b|"
    r"\bnorminette\b|\bmakefile\b|\bvalgrind\b|\bpointer\b|указател|\bgcc\b|\bcc\s+-|"
    r"\bpython\b|\bbash\b|\bgit\b|\bdocker\b|\bsql\b|\bjavascript\b|\bregex\b|"
    r"функци[юяи]|компил|ошибк[аиу] (в|при) (коде|компиляции)|\bdebug|\bstack trace|"
    r"\berror:|\bwarning:|traceback|\blibft\b|\bft_\w+|\bminishell\b|\bpipex\b|\bphilosophers\b|"
    r"\bpush_swap\b|\bget_next_line\b|\bprintf\b|\bcub3d\b|\bminirt\b|\bso_long\b|\bfdf\b",
    re.IGNORECASE,
)
SHORT_CHAT = 40  # символов — короткая реплика ("привет", "спасибо", "ок")
SHORT_WORDS = 6


@dataclass(frozen=True)
class ModelChoice:
    model: str
    reason: str


def _pick(preferred: str, available: list[str] | None, fallback: str) -> str | None:
    if not preferred:
        return None
    if available is None:
        return preferred
    names = set(available)
    if preferred in names:
        return preferred
    if ":" not in preferred and f"{preferred}:latest" in names:
        return f"{preferred}:latest"
    return None if preferred != fallback else fallback


def choose_model(
    text: str,
    *,
    default: str,
    code: str,
    fast: str,
    available: list[str] | None,
    prefer_code: bool = False,
) -> ModelChoice:
    """available=None — список моделей неизвестен (Ollama не ответила), доверяем настройкам."""
    if prefer_code or CODE_PATTERNS.search(text):
        if model := _pick(code, available, default):
            return ModelChoice(model, "код")
    elif len(text) <= SHORT_CHAT and len(text.split()) <= SHORT_WORDS and "\n" not in text and "?" not in text:
        if model := _pick(fast, available, default):
            return ModelChoice(model, "короткая реплика")
    return ModelChoice(default, "основная")
