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

    # --- LLM ---
    ollama_url: str = "http://localhost:11434"
    default_model: str = "qwen2.5:7b"  # универсальная модель
    auto_model: bool = True  # если пользователь не выбрал модель — выбирать по запросу
    code_model: str = "qwen2.5-coder:7b"  # для кода (автовыбор); на P100 — qwen2.5-coder:14b
    fast_model: str = "qwen2.5:3b"  # для коротких реплик, фактов, резюме, перевода
    vision_model: str = "qwen2.5vl:7b"  # для фото
    default_mode: str = "chat"
    history_limit: int = 20
    summary_batch: int = 10  # сколько старых сообщений сворачивать в резюме за раз
    request_timeout: float = 600.0
    num_ctx: int = 8192  # контекст модели в токенах; больше = больше VRAM

    # --- Хранилище ---
    db_path: str = "data/fox_ai.sqlite3"
    timezone: str = "Europe/Paris"  # часовой пояс по умолчанию (напоминания, дата в промпте)

    # --- Эмбеддинги, память, база знаний, личные документы ---
    embed_model: str = "bge-m3"
    memory_auto: bool = True
    memory_model: str = ""  # пусто = FAST_MODEL
    memory_top_k: int = 5
    memory_min_score: float = 0.45
    knowledge_dir: str = "knowledge"
    knowledge_top_k: int = 4
    knowledge_min_score: float = 0.4
    docs_top_k: int = 3
    docs_min_score: float = 0.4

    # --- Интернет ---
    searxng_url: str = ""  # пусто = интернет выключен
    max_tool_steps: int = 3

    # --- Внешние сервисы (пусто = выключено) ---
    sandbox_url: str = ""  # песочница для запуска C-кода
    speech_url: str = ""  # распознавание и синтез речи
    imagegen_url: str = ""  # генерация картинок

    # --- 42 intra API (https://profile.intra.42.fr/oauth/applications) ---
    intra_client_id: str = ""
    intra_client_secret: SecretStr = SecretStr("")
    intra_check_hour: int = 10  # во сколько проверять blackhole (локальное время)

    # --- Лимиты ---
    daily_limit: int = 0  # запросов к моделям в день на пользователя (0 = без лимита, админам не действует)
    max_concurrent: int = 1  # одновременных генераций на GPU, остальные ждут в очереди
    # Одна видеокарта: на время /draw выгружать LLM из VRAM и не пускать другие генерации
    imagegen_exclusive: bool = True

    # --- Учитель языков (немецкий, чешский) ---
    lang_user_ids: str = ""  # кому доступен словарь и уроки; пусто = только админы
    lang_model: str = ""  # модель для уроков и словаря; пусто = DEFAULT_MODEL
    lang_daily_from: int = 8  # ежедневное повторение — в случайное время в этом окне (часы)
    lang_daily_to: int = 17
    lang_daily_min: int = 5  # и случайное число слов
    lang_daily_max: int = 15

    # --- Аквариум (бывший fish_helper) ---
    aquarium_caretaker_ids: str = ""  # кто ухаживает (получает задачи); пусто = модуль выключен
    aquarium_owner_id: str = ""  # кому отчёты и тревоги; пусто = первый из ADMIN_IDS
    aquarium_owner_name: str = "владельцу"  # «Я передал …»
    aquarium_timezone: str = ""  # пусто = TIMEZONE
    aquarium_ask_ids: str = ""  # кому агент задаёт вопросы об аквариуме; пусто = владельцу
    aquarium_ask_hour: int = 18  # во сколько (местное время) задавать вопрос дня

    # --- Мониторинг и бэкапы ---
    gpu_temp_alert: int = 85  # °C, выше — предупреждение админам
    # Снимок nvidia-smi, который пишет хост (windows/gpu-stats.ps1); пусто = звать nvidia-smi
    gpu_stats_file: str = ""
    backup_dir: str = ""  # пусто = бэкапы выключены
    backup_keep: int = 14
    backup_hour: int = 4

    @property
    def admins(self) -> frozenset[int]:
        return parse_ids(self.admin_ids)

    @property
    def lang_users(self) -> frozenset[int]:
        return parse_ids(self.lang_user_ids) or self.admins

    @property
    def tutor_model(self) -> str:
        return self.lang_model or self.default_model

    @property
    def aquarium_caretakers(self) -> frozenset[int]:
        return parse_ids(self.aquarium_caretaker_ids)

    @property
    def aquarium_enabled(self) -> bool:
        return bool(self.aquarium_caretakers)

    @property
    def aquarium_owner(self) -> int:
        return min(parse_ids(self.aquarium_owner_id) or self.admins, default=0)

    @property
    def aquarium_members(self) -> frozenset[int]:
        """Кому доступен аквариум: ухаживающие и владелец."""
        if not self.aquarium_enabled:
            return frozenset()
        return self.aquarium_caretakers | {self.aquarium_owner} - {0}

    @property
    def aquarium_askees(self) -> frozenset[int]:
        return parse_ids(self.aquarium_ask_ids) or frozenset({self.aquarium_owner}) - {0}

    @property
    def web_enabled(self) -> bool:
        return bool(self.searxng_url)

    @property
    def intra_enabled(self) -> bool:
        return bool(self.intra_client_id and self.intra_client_secret.get_secret_value())

    @property
    def helper_model(self) -> str:
        """Маленькая модель для служебных задач."""
        return self.memory_model or self.fast_model or self.default_model
