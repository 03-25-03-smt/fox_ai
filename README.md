# 🦊 Fox AI

Telegram-бот на локальных LLM: помощник по коду и 42 (norminette, песочница, тесты,
защита, интра), собеседник для друзей (рецепты, обсуждения), с памятью, интернетом,
голосом, фото и генерацией картинок. Всё работает на своём железе через Docker.

Идеи на будущее — в [future_updates.md](future_updates.md).

---

## Установка с нуля (Ubuntu 22.04 / 24.04)

### 1. Обновить систему

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl openssl
```

### 2. Драйвер NVIDIA

Ветка **580** — последняя, которая поддерживает Tesla P100 (Pascal), и подходит для RTX 3070.

> Сейчас бот настроен на **одну RTX 3070** (`.env.example`). Пока нет охлаждения для P100 —
> лучше вынуть её из компьютера: даже без нагрузки пассивная карта в корпусе греется.
> Значения для P100 + 3070 подписаны в `.env.example` комментариями `P100:`.

```bash
sudo apt install -y nvidia-driver-580
sudo reboot
```

После перезагрузки — обе карты должны быть видны:

```bash
nvidia-smi
nvidia-smi -L   # номер RTX 3070 → GPU_MAIN и GPU_AUX в .env
```

> ⚠️ У P100 нет своего вентилятора. Без направленного обдува она перегреется за минуты.

### 3. Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker            # или перелогиниться
sudo systemctl enable docker
docker run --rm hello-world
```

### 4. NVIDIA Container Toolkit (GPU внутри контейнеров)

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update && sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

docker run --rm --gpus all ubuntu nvidia-smi   # проверка: видны обе карты
```

### 5. Папки для моделей и бэкапов на HDD

Модели весят 5–20 ГБ каждая, системного SSD на 120 ГБ не хватит.

```bash
lsblk -f                                  # найти HDD, например /dev/sdb1
sudo mkdir -p /mnt/hdd
sudo mount /dev/sdb1 /mnt/hdd             # подставь свой раздел
echo "UUID=$(sudo blkid -s UUID -o value /dev/sdb1) /mnt/hdd ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
sudo mkdir -p /mnt/hdd/ollama /mnt/hdd/fox_ai_backups
sudo chown -R 1000:1000 /mnt/hdd/fox_ai_backups
```

> Если диск новый и без файловой системы — сначала `sudo mkfs.ext4 /dev/sdb1`
> (⚠️ стирает всё на разделе). Когда появится NVMe — перенеси туда `/mnt/hdd/ollama`
> и поменяй `OLLAMA_MODELS_DIR` в `.env`.

### 6. Telegram-бот

1. В [@BotFather](https://t.me/BotFather): `/newbot` → получить **токен**.
2. Там же `/setprivacy` → выбрать бота → **Disable** (чтобы бот видел @упоминания в группах).
3. Свой Telegram ID узнать у [@userinfobot](https://t.me/userinfobot).

### 7. Скачать проект и настроить

```bash
git clone https://github.com/03-25-03-smt/fox_ai.git
cd fox_ai
cp .env.example .env
openssl rand -hex 32        # скопировать — это SEARXNG_SECRET
nano .env
```

Минимум, что заполнить в `.env`:

| Переменная | Что это |
|---|---|
| `BOT_TOKEN` | токен от BotFather |
| `ADMIN_IDS` | твой Telegram ID |
| `SEARXNG_SECRET` | строка из `openssl rand -hex 32` |
| `OLLAMA_MODELS_DIR` | `/mnt/hdd/ollama` |
| `BACKUP_HOST_DIR` | `/mnt/hdd/fox_ai_backups` |
| `GPU_MAIN`, `GPU_AUX` | номер RTX 3070 из `nvidia-smi -L` |
| `INTRA_CLIENT_ID/SECRET` | необязательно: [приложение в интре](https://profile.intra.42.fr/oauth/applications) для `/42` |

```bash
mkdir -p data/bot data/models data/backups
```

### 8. Запуск

```bash
docker compose up -d --build     # первая сборка ~10–20 минут
docker compose ps                # все сервисы должны быть Up
docker compose logs -f bot       # ждём «Fox AI (@имя_бота) запущен», выход: Ctrl+C
```

Не нужны голос или картинки — закомментируй сервисы `speech` / `imagegen`
в `docker-compose.yml` и добавь в `.env` пустые `SPEECH_URL=` / `IMAGEGEN_URL=`.

### 9. Скачать модели

```bash
docker compose exec ollama ollama pull bge-m3              # эмбеддинги: память и база знаний (обязательно)
docker compose exec ollama ollama pull qwen2.5:7b          # основная модель
docker compose exec ollama ollama pull qwen2.5-coder:7b    # для кода (на P100 — :14b)
docker compose exec ollama ollama pull qwen2.5:3b          # быстрая: короткие ответы, факты, резюме
docker compose exec ollama ollama pull qwen2.5vl:7b        # для фото
```

Проверить, что модель работает на видеокарте (в колонке PROCESSOR должно быть `GPU`):

```bash
docker compose exec ollama ollama run qwen2.5:7b "Привет!"
docker compose exec ollama ollama ps
```

### 10. Первый запрос в Telegram

1. Открыть своего бота → `/start` — придёт список команд.
2. `/reindex` — проиндексировать базу знаний (Norm 42).
3. Написать: `Привет! Объясни, что такое указатель в C` — бот ответит, текст появляется по ходу генерации.
4. `/status` — температура карт, загруженные модели, скорость.

Готово 🎉

---

## Дальше

```bash
# Дать доступ другу (он пишет боту /start и присылает тебе свой ID)
#   в Telegram: /adduser <id> Имя

# Обновить бота
git pull && docker compose up -d --build

# Логи и перезапуск
docker compose logs -f bot
docker compose restart bot

# Остановить всё
docker compose down
```

Что где:

| Сервис | Зачем |
|---|---|
| `ollama` | LLM-модели на GPU |
| `searxng` | поиск в интернете (без внешних API) |
| `sandbox` | компиляция и запуск C-кода: `/run`, `/valgrind`, `/asan`, `/tests`, проверка проектов (без интернета) |
| `speech` | голосовые сообщения (Whisper) и ответы голосом (Piper), сейчас на CPU |
| `imagegen` | `/draw` — картинки (SDXL-Turbo); на одной 3070 на время рисования LLM выгружается |
| `bot` | сам Telegram-бот |

В папку `knowledge/` можно положить PDF Norm и subjects — бот будет на них опираться
в режиме «42 / код» (после `/reindex`).

## Разработка

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```
