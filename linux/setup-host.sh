#!/usr/bin/env bash
# Подготовка Ubuntu 24.04 к Fox AI — один раз, после установки драйвера NVIDIA (README, шаг 4):
#   sudo bash linux/setup-host.sh
#
# 1. Docker Engine + compose (официальный репозиторий Docker)
# 2. NVIDIA Container Toolkit — видеокарты в контейнерах
# 3. Данные Docker (образы, тома, база бота) — на HDD 1 ТБ: /data/docker
#    Docker стартует только когда /data смонтирован, иначе он молча начал бы писать на SSD
# 4. nvidia-persistenced (лимит мощности P100 держится, карты не «засыпают»), без сна и гибернации
# 5. Пользователь, запустивший sudo, — в группе docker (docker без sudo)
#
# --data-root PATH   куда положить данные Docker (по умолчанию /data/docker)

set -euo pipefail

DATA_ROOT=/data/docker
while [[ $# -gt 0 ]]; do
    case "$1" in
        --data-root) DATA_ROOT="$2"; shift 2 ;;
        *) echo "неизвестный параметр: $1" >&2; exit 2 ;;
    esac
done

say() { printf '\n== %s\n' "$*"; }
die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "запусти через sudo: sudo bash linux/setup-host.sh"
. /etc/os-release
[[ "${ID:-}" == ubuntu ]] || echo "Внимание: скрипт проверен на Ubuntu 24.04, у тебя ${PRETTY_NAME:-?}"
CODENAME="${UBUNTU_CODENAME:-$VERSION_CODENAME}"
MOUNT_POINT="$(findmnt -n -o TARGET --target "$(dirname "$DATA_ROOT")" 2>/dev/null || true)"
[[ -n "$MOUNT_POINT" && "$MOUNT_POINT" != / ]] \
    || die "$(dirname "$DATA_ROOT") нет или он на системном диске: сначала смонтируй HDD 1 ТБ (README, шаг 3)"

say "Драйвер NVIDIA"
command -v nvidia-smi >/dev/null && nvidia-smi -L || die "nvidia-smi не работает: поставь драйвер (README, шаг 4) и перезагрузись"

say "Docker Engine"
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
    apt-get update
    apt-get install -y ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $CODENAME stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
    echo "  уже установлен: $(docker --version)"
fi

say "NVIDIA Container Toolkit"
if ! command -v nvidia-ctk >/dev/null; then
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
        | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
        | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        > /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt-get update
    apt-get install -y nvidia-container-toolkit
else
    echo "  уже установлен: $(nvidia-ctk --version | head -n1)"
fi
nvidia-ctk runtime configure --runtime=docker >/dev/null

say "Данные Docker: $DATA_ROOT (диск $MOUNT_POINT)"
mkdir -p "$DATA_ROOT"
python3 - "$DATA_ROOT" <<'PY'
import json, pathlib, sys
path = pathlib.Path("/etc/docker/daemon.json")
cfg = json.loads(path.read_text()) if path.exists() and path.read_text().strip() else {}
cfg["data-root"] = sys.argv[1]
# Ротация логов и для контейнеров, запущенных не из docker-compose.yml
cfg.setdefault("log-driver", "json-file")
cfg.setdefault("log-opts", {"max-size": "20m", "max-file": "3"})
path.write_text(json.dumps(cfg, indent=2) + "\n")
PY
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/fox-ai.conf <<EOF
# Fox AI: данные Docker на HDD — без него Docker не запускается
[Unit]
RequiresMountsFor=$DATA_ROOT
EOF
OLD_ROOT=/var/lib/docker
if [[ "$DATA_ROOT" != "$OLD_ROOT" && -d "$OLD_ROOT" && -n "$(ls -A "$OLD_ROOT" 2>/dev/null)" ]]; then
    echo "  на SSD остались старые данные Docker в $OLD_ROOT — после проверки можно удалить: sudo rm -rf $OLD_ROOT"
fi
systemctl daemon-reload
systemctl enable docker containerd >/dev/null
systemctl restart docker
echo "  Docker Root Dir: $(docker info --format '{{.DockerRootDir}}')"

say "Режим сервера"
systemctl enable --now nvidia-persistenced >/dev/null 2>&1 || nvidia-smi -pm 1 >/dev/null || true
# Сервер не должен засыпать (на Ubuntu Desktop по умолчанию сон через 20 минут простоя)
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null

if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]]; then
    usermod -aG docker "$SUDO_USER"
    echo "  $SUDO_USER добавлен в группу docker (заработает после перезахода или перезагрузки)"
fi

say "Проверка: видеокарты внутри контейнера"
docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L

printf '\nГотово. Перезагрузись (sudo reboot), затем — README, шаг 6.\n'
