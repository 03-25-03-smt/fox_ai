"""Настройки бота из переменных окружения / файла .env."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_ids(raw: str) -> frozenset[int]:
    """Разбирает строку вида '123, 456' в набор ID."""
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            ids.add(int(part))
    return frozenset(ids)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    bot_token: SecretStr
    admin_ids: str = ""

    # LLM
    ollama_url: str = "http://localhost:11434"
    default_model: str = "qwen2.5:7b"
    default_mode: str = "chat"
    history_limit: int = 20
    request_timeout: float = 600.0
    num_ctx: int = 8192  # контекст модели в токенах; больше = больше VRAM

    # Хранилище
    db_path: str = "data/fox_ai.sqlite3"

    # Эмбеддинги (память и база знаний)
    embed_model: str = "bge-m3"

    # Долговременная память
    memory_auto: bool = True
    memory_model: str = ""  # пусто = модель пользователя
    memory_top_k: int = 5
    memory_min_score: float = 0.45

    # База знаний (Norm, subjects)
    knowledge_dir: str = "knowledge"
    knowledge_top_k: int = 4
    knowledge_min_score: float = 0.4

    # Интернет
    searxng_url: str = ""  # пусто = интернет выключен
    max_tool_steps: int = 3

    @property
    def admins(self) -> frozenset[int]:
        return parse_ids(self.admin_ids)

    @property
    def web_enabled(self) -> bool:
        return bool(self.searxng_url)
