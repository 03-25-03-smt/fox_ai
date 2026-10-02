#!/usr/bin/env bash
# Установка и обновление Fox AI одной командой (обычным пользователем, из папки проекта;
# sudo спросит пароль сам):
#   bash linux/install.sh --no-pull --models    первая установка
#   bash linux/install.sh                       обновление
#
# 1. git pull
# 2. .env: новые настройки из .env.example, секреты, UUID видеокарт (linux/env_setup.py)
# 3. папки: модели Ollama, бэкапы
# 4. агент fox-agent (служба systemd от root): /logs, /restart, /power, температуры карт
# 5. docker compose up -d --build, чистка старых образов
# 6. --models: скачать модели из .env в Ollama (~25 ГБ)
#
# --no-pull         не делать git pull
# --models          скачать/обновить модели Ollama
# --power-limit N   постоянный лимит мощности P100, Вт (по умолчанию 200; 0 — заводской)

set -euo pipefail

PULL=1 MODELS=0 POWER=200
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-pull) PULL=0; shift ;;
        --models) MODELS=1; shift ;;
        --power-limit) POWER="$2"; shift 2 ;;
        *) echo "неизвестный параметр: $1" >&2; exit 2 ;;
    esac
done
[[ "$POWER" =~ ^[0-9]+$ ]] || { echo "--power-limit — число ватт" >&2; exit 2; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
say() { printf '\n== %s\n' "$*"; }
die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
# Значение из .env без source: там могут быть любые символы
env_get() { sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" .env | tail -n1 | sed 's/[[:space:]]*$//'; }

[[ $EUID -ne 0 ]] || die "запускай обычным пользователем (без sudo): bash linux/install.sh"
docker info >/dev/null 2>&1 || die "docker недоступен без sudo: сначала sudo bash linux/setup-host.sh и перезагрузка"

# 1. Код
if [[ $PULL -eq 1 ]]; then
    say "git pull"
    git pull --ff-only || die "git pull не удался (есть свои изменения в файлах? git status)"
fi

# 2. .env
say ".env"
if [[ ! -f .env ]]; then
    cp .env.example .env
    chmod 600 .env
    die ".env создан из .env.example — впиши BOT_TOKEN и ADMIN_IDS (nano .env) и запусти снова"
fi
chmod 600 .env
python3 linux/env_setup.py .env .env.example || die "поправь .env (nano .env) и запусти снова"

# 3. Папки на хосте. uid 1000 — пользователь бота в контейнере
say "папки"
MODELS_DIR="$(env_get OLLAMA_MODELS_DIR)"; MODELS_DIR="${MODELS_DIR:-/var/lib/fox-ollama}"
BACKUP_DIR="$(env_get BACKUP_HOST_DIR)"; BACKUP_DIR="${BACKUP_DIR:-$ROOT/data/backups}"
mkdir -p data
sudo mkdir -p "$MODELS_DIR" "$BACKUP_DIR"
sudo chown 1000:1000 "$BACKUP_DIR"
sudo chmod 750 "$BACKUP_DIR"
echo "  модели Ollama: $MODELS_DIR ($(df -h --output=avail "$MODELS_DIR" | tail -n1 | tr -d ' ') свободно)"
echo "  бэкапы:        $BACKUP_DIR"

# 4. Агент. Копия в /usr/local/lib: root выполняет только файл, который может менять root
say "агент fox-agent"
GPU_NAME="$(env_get LLM_GPU_NAME)"; GPU_NAME="${GPU_NAME:-P100}"
[[ "$GPU_NAME" =~ ^[A-Za-z0-9\ _.-]+$ ]] || die "LLM_GPU_NAME: только буквы, цифры, пробел, _ . -"
sudo install -D -m 0755 -o root -g root linux/fox-agent.py /usr/local/lib/fox-ai/fox-agent.py
sudo tee /etc/systemd/system/fox-agent.service >/dev/null <<EOF
[Unit]
Description=Fox AI host agent: /logs, /restart, /power, GPU stats
After=docker.service nvidia-persistenced.service
Wants=docker.service

[Service]
ExecStart=/usr/bin/python3 /usr/local/lib/fox-ai/fox-agent.py --root "$ROOT" --gpu-name "$GPU_NAME" --power-limit $POWER
Restart=always
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable fox-agent >/dev/null
sudo systemctl restart fox-agent
# Агент создаёт data/host и data/gpu с нужными владельцами — до первого docker compose up
for _ in $(seq 20); do [[ -d data/host/requests ]] && break; sleep 0.5; done
systemctl is-active --quiet fox-agent || die "агент не запустился: journalctl -u fox-agent -n 50"
echo "  запущен, лимит P100: $([[ $POWER -gt 0 ]] && echo "$POWER Вт" || echo заводской)"

# 5. Контейнеры
say "docker compose up -d --build (первый раз — 20–30 минут)"
docker compose up -d --build
docker image prune -f >/dev/null
docker compose ps

# 6. Модели
if [[ $MODELS -eq 1 ]]; then
    say "модели Ollama"
    for _ in $(seq 30); do docker compose exec -T ollama ollama list >/dev/null 2>&1 && break; sleep 2; done
    models=()
    for key in EMBED_MODEL DEFAULT_MODEL CODE_MODEL FAST_MODEL VISION_MODEL; do
        m="$(env_get "$key")"
        [[ -n "$m" && " ${models[*]} " != *" $m "* ]] && models+=("$m")
    done
    for m in "${models[@]}"; do
        echo "  -> $m"
        docker compose exec -T ollama ollama pull "$m" || echo "  ⚠ не удалось скачать $m"
    done
    docker compose exec -T ollama ollama list
fi

printf '\nГотово. Проверь в Telegram: /status, /ps, /power. Логи бота: docker compose logs -f bot\n'
printf 'Grafana: http://localhost:3000, Open WebUI: http://localhost:3001 (только с этого ПК или через SSH-туннель)\n'
