#!/usr/bin/env python3
"""Готовит .env для linux/install.sh. Твои значения не трогает:

- дописывает новые настройки из .env.example (кроме BOT_TOKEN и ADMIN_IDS);
- генерирует SEARXNG_SECRET и GRAFANA_PASSWORD, если там ещё replace-me;
- GPU_LLM / GPU_IMAGEGEN / GPU_SPEECH=auto → UUID карты по имени из nvidia-smi;
- проверяет, что BOT_TOKEN и ADMIN_IDS заданы.

    python3 linux/env_setup.py [.env] [.env.example]
Код выхода 1 — в .env чего-то не хватает (подробности в выводе).
"""

import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

KEY_RE = re.compile(r"^\s*([A-Z0-9_]+)\s*=")
SKIP = {"BOT_TOKEN", "ADMIN_IDS"}  # их задаёшь сам, пример не подставляем
# Переменная → какую карту искать (часть имени). Для LLM имя берётся из LLM_GPU_NAME
GPU_VARS = {"GPU_LLM": "P100", "GPU_IMAGEGEN": "3070", "GPU_SPEECH": "1050"}


def list_gpus() -> list[tuple[str, str]]:
    """[(имя, UUID)] из nvidia-smi; пусто, если драйвера нет."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,uuid", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=30, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [tuple(p.strip() for p in line.split(",", 1)) for line in out.splitlines() if "," in line]


def value(lines: list[str], key: str) -> str | None:
    for line in reversed(lines):
        if (m := KEY_RE.match(line)) and m.group(1) == key:
            return line.split("=", 1)[1].strip()
    return None


def setup(env_path: Path, example_path: Path, gpus: list[tuple[str, str]]) -> list[str]:
    """Обновляет .env, возвращает список проблем (пусто — всё хорошо)."""
    lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    have = {m.group(1) for line in lines if (m := KEY_RE.match(line))}
    added = []
    for line in example_path.read_text(encoding="utf-8").splitlines():
        m = KEY_RE.match(line)
        if m and m.group(1) not in have and m.group(1) not in SKIP:
            if not added:
                lines += ["", f"# --- добавлено install.sh {time.strftime('%Y-%m-%d')}: новые настройки (описание в .env.example) ---"]
            lines.append(line)
            have.add(m.group(1))
            added.append(m.group(1))
    print(f"  добавлены: {', '.join(added)}" if added else "  новых настроек нет")

    problems = []
    names = dict(GPU_VARS, GPU_LLM=value(lines, "LLM_GPU_NAME") or "P100")
    # Сервис закомментирован в docker-compose.yml — его карта не нужна
    compose = env_path.parent / "docker-compose.yml"
    used = "\n".join(ln for ln in compose.read_text(encoding="utf-8").splitlines()
                     if not ln.lstrip().startswith("#")) if compose.exists() else ""
    for i, line in enumerate(lines):
        m = KEY_RE.match(line)
        key = m.group(1) if m else ""
        val = line.split("=", 1)[1].strip() if m else ""
        if key in ("SEARXNG_SECRET", "GRAFANA_PASSWORD") and val == "replace-me":
            secret = secrets.token_hex(32 if key == "SEARXNG_SECRET" else 8)
            lines[i] = f"{key}={secret}"
            print(f"  {key} сгенерирован" + (f": {secret} (логин admin)" if key == "GRAFANA_PASSWORD" else ""))
        elif key in names and val in ("", "auto") and f"${{{key}" in used:
            found = [uuid for name, uuid in gpus if names[key].lower() in name.lower()]
            if found:
                lines[i] = f"{key}={found[0]}"
                print(f"  {key} = {found[0]} ({names[key]})")
            else:
                problems.append(f"{key}: карта «{names[key]}» не найдена в nvidia-smi — впиши UUID вручную "
                                f"(nvidia-smi -L) или закомментируй сервис в docker-compose.yml")
        elif key in names and "#" in val:
            # Старый .env с Windows: «GPU_IMAGEGEN=0    # номер RTX 3070» — номера там были другие
            problems.append(f"{key}={val}: замени на {key}=auto (комментарий в строке значения — тоже убери)")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    token = value(lines, "BOT_TOKEN") or ""
    if not re.fullmatch(r"\d+:[\w-]{20,}", token):
        problems.append("BOT_TOKEN не задан — токен от @BotFather (nano .env)")
    if not re.match(r"\d", value(lines, "ADMIN_IDS") or ""):
        problems.append("ADMIN_IDS не задан — твой Telegram ID от @userinfobot (nano .env)")
    for key in ("BACKUP_HOST_DIR", "OLLAMA_MODELS_DIR"):
        if (path := value(lines, key)) and not path.startswith("/"):
            problems.append(f"{key}={path}: нужен абсолютный путь Linux, например "
                            + ("/backup/fox_ai" if key == "BACKUP_HOST_DIR" else "/var/lib/fox-ollama"))
    return problems


def main() -> int:
    env_path = Path(sys.argv[1] if len(sys.argv) > 1 else ".env")
    example = Path(sys.argv[2] if len(sys.argv) > 2 else ".env.example")
    problems = setup(env_path, example, list_gpus())
    for p in problems:
        print(f"  ⚠ {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
