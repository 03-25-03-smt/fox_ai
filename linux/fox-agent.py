#!/usr/bin/env python3
"""Агент Fox AI на хосте Linux: команды бота из белого списка и снимки nvidia-smi.

Бот (в Docker) кладёт запрос в data/host/requests/<id>.json, агент выполняет его и пишет ответ
в data/host/responses/<id>.json. Сетевого порта нет, Docker-сокет в контейнер не пробрасывается,
произвольные команды не выполняются:
    logs <сервис> <строк>   docker compose logs
    restart <сервис>        docker compose restart (в том числе ollama)
    ps                      docker compose ps
    power <Вт>|default      лимит мощности LLM-карты (nvidia-smi -pl)
Каждые 15 с агент обновляет data/host/status.json (жив, текущий лимит мощности), каждые 20 с —
data/gpu/gpu.csv (температуры всех карт для /status, бережного режима и Grafana).

Работает от root (nvidia-smi -pl и docker) как служба systemd fox-agent (linux/install.sh).
Папка requests принадлежит пользователю контейнера бота, всё остальное — root: бот не может
подменить ответы или подсунуть агенту ссылку на чужой файл. Вручную, с выводом в консоль:
    sudo python3 linux/fox-agent.py --power-limit 200
"""

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

GPU_QUERY = "index,name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,fan.speed"
ID_RE = re.compile(r"^([0-9a-f]{32})\.json$")
SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
MAX_AGE = 120          # старые запросы (бот уже не ждёт) не выполняем
MAX_REQUEST = 64 * 1024
MAX_OUTPUT = 200_000
STATUS_EVERY = 15
GPU_EVERY = 20


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def run(cmd: list[str], cwd: Path | None = None, timeout: float = 60) -> tuple[bool, str]:
    """Без shell: аргументы уходят программе как есть."""
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return False, f"не найдена программа {cmd[0]}"
    except subprocess.TimeoutExpired:
        return False, f"{cmd[0]}: не уложился в {int(timeout)} с"
    out = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    return proc.returncode == 0, out


def write_atomic(path: Path, text: str, mode: int = 0o644) -> None:
    """Через временный файл: читатель никогда не увидит половину. O_EXCL не идёт по ссылкам."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def prepare_dir(path: Path, mode: int, uid: int | None = None) -> None:
    """Папка без символьных ссылок, с нужным владельцем и правами (владельца меняем только под root)."""
    path.mkdir(parents=True, exist_ok=True)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        sys.exit(f"{path} — не папка (символьная ссылка?), агент не запущен")
    if os.geteuid() == 0:
        os.chown(path, 0 if uid is None else uid, 0 if uid is None else uid, follow_symlinks=False)
    os.chmod(path, mode)


class Agent:
    def __init__(self, root: Path, gpu_name: str, bot_uid: int) -> None:
        self.root = root
        self.gpu_name = gpu_name
        self.host_dir = root / "data" / "host"
        self.req_dir = self.host_dir / "requests"
        self.resp_dir = self.host_dir / "responses"
        self.gpu_file = root / "data" / "gpu" / "gpu.csv"
        prepare_dir(root / "data" / "gpu", 0o755)
        prepare_dir(self.host_dir, 0o755)
        prepare_dir(self.resp_dir, 0o755)
        prepare_dir(self.req_dir, 0o700, uid=bot_uid)  # сюда пишет бот
        self.req_fd = os.open(self.req_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.pool = ThreadPoolExecutor(max_workers=2)

    # ------------------------------------------------------------- GPU

    def gpus(self) -> list[list[str]]:
        ok, out = run(["nvidia-smi", f"--query-gpu={GPU_QUERY}", "--format=csv,noheader,nounits"], timeout=15)
        return [[p.strip() for p in line.split(",")] for line in out.splitlines() if ok and line.count(",") == 7]

    def llm_gpu(self) -> dict | None:
        ok, out = run(["nvidia-smi", "--query-gpu=index,name,power.limit,power.default_limit",
                       "--format=csv,noheader,nounits"], timeout=15)
        for line in out.splitlines() if ok else []:
            p = [x.strip() for x in line.split(",")]
            if len(p) == 4 and self.gpu_name.lower() in p[1].lower():
                try:
                    return {"index": p[0], "name": p[1], "limit": float(p[2]), "default": float(p[3])}
                except ValueError:
                    return None
        return None

    def write_gpu_stats(self) -> None:
        rows = self.gpus()
        if rows:
            write_atomic(self.gpu_file, "\n".join(", ".join(r) for r in rows) + "\n")

    def write_status(self) -> None:
        gpu = self.llm_gpu()
        power = f"{gpu['name']}: лимит {int(gpu['limit'])} из {int(gpu['default'])} Вт" if gpu else "нет данных"
        write_atomic(self.host_dir / "status.json",
                     json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "power": power}, ensure_ascii=False))

    def set_power(self, value: str) -> tuple[bool, str]:
        gpu = self.llm_gpu()
        if gpu is None:
            return False, f"карта {self.gpu_name} не найдена"
        if value == "default":
            watts = int(gpu["default"])
        elif value.isdigit():
            watts = int(value)
        else:
            return False, "лимит — число ватт или default"
        if not 100 <= watts <= 300:
            return False, "лимит вне 100–300 Вт"
        ok, out = run(["nvidia-smi", "-i", gpu["index"], "-pl", str(watts)], timeout=30)
        if not ok:
            return False, f"nvidia-smi: {out.strip()} (агент запущен не от root?)"
        return True, f"лимит {gpu['name']}: {watts} Вт"

    # ------------------------------------------------------------- docker compose

    def compose(self, *args: str, timeout: float = 60) -> tuple[bool, str]:
        return run(["docker", "compose", *args], cwd=self.root, timeout=timeout)

    def services(self) -> set[str]:
        ok, out = self.compose("config", "--services", timeout=30)
        return set(out.split()) if ok else set()

    def handle(self, cmd: str, args: list[str]) -> tuple[bool, str]:
        if cmd == "ps":
            return self.compose("ps", "--format", "table {{.Service}}\t{{.Status}}")
        if cmd in ("logs", "restart"):
            service = args[0] if args else ""
            if not SERVICE_RE.match(service) or service not in self.services():
                return False, f"нет сервиса {service}"
            if cmd == "restart":
                return self.compose("restart", service, timeout=170)
            lines = int(args[1]) if len(args) > 1 and args[1].isdigit() else 80
            if not 1 <= lines <= 2000:
                lines = 80
            return self.compose("logs", "--no-color", "--tail", str(lines), service)
        if cmd == "power":
            return self.set_power(args[0] if args else "")
        return False, f"неизвестная команда {cmd}"

    # ------------------------------------------------------------- запросы

    def read_request(self, name: str) -> dict | None:
        """Читает и удаляет запрос. Ссылки, папки и FIFO вместо файла не открываем."""
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.req_fd)
        except OSError:
            self.unlink_request(name)  # символьная ссылка и прочее
            return None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            raw = os.read(fd, MAX_REQUEST + 1)
        finally:
            os.close(fd)
            self.unlink_request(name)
        if len(raw) > MAX_REQUEST:
            return None
        try:
            req = json.loads(raw.decode("utf-8-sig"))
        except ValueError:
            return None
        return req if isinstance(req, dict) else None

    def process(self, rid: str, req: dict) -> None:
        cmd = str(req.get("cmd", ""))
        args = req.get("args") or []
        if not isinstance(args, list) or len(args) > 4 or any(not isinstance(a, str) or len(a) > 40 for a in args):
            ok, out = False, "неверные аргументы"
        else:
            try:
                age = time.time() - float(req.get("created", 0))
            except (TypeError, ValueError):
                age = MAX_AGE + 1
            if age > MAX_AGE:
                ok, out = False, "запрос устарел"
            else:
                try:
                    ok, out = self.handle(cmd, args)
                except Exception as exc:  # noqa: BLE001 — агент не должен падать из-за одного запроса
                    ok, out = False, f"ошибка агента: {exc}"
        out = out[-MAX_OUTPUT:]
        write_atomic(self.resp_dir / f"{rid}.json", json.dumps({"ok": ok, "output": out}, ensure_ascii=False))
        log(f"{cmd} {' '.join(map(str, args))} -> {ok}")

    def poll_requests(self) -> None:
        for name in os.listdir(self.req_fd):
            m = ID_RE.match(name)
            if not m:
                self.drop_stale(name)
                continue
            if (req := self.read_request(name)) is None or req.get("id") != m.group(1):
                continue
            self.pool.submit(self.process, m.group(1), req)

    def unlink_request(self, name: str) -> None:
        try:
            os.unlink(name, dir_fd=self.req_fd)  # dir_fd + unlink: ссылка удаляется сама, а не её цель
        except OSError:
            pass

    def drop_stale(self, name: str) -> None:
        """Недописанные .tmp бота и мусор старше 10 минут."""
        try:
            if time.time() - os.lstat(name, dir_fd=self.req_fd).st_mtime > 600:
                self.unlink_request(name)
        except OSError:
            pass

    def clean_responses(self) -> None:
        """Ответы удаляет бот, но если он не успел (перезапуск) — убираем сами."""
        for path in self.resp_dir.iterdir():
            try:
                if time.time() - path.lstat().st_mtime > 300:
                    path.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------- цикл

    def loop(self) -> None:
        last_status = last_gpu = last_clean = 0.0
        log(f"Fox AI agent: жду команды в {self.req_dir}")
        while True:
            now = time.monotonic()
            try:
                if now - last_gpu >= GPU_EVERY:
                    last_gpu = now
                    self.write_gpu_stats()
                if now - last_status >= STATUS_EVERY:
                    last_status = now
                    self.write_status()
                if now - last_clean >= 60:
                    last_clean = now
                    self.clean_responses()
                self.poll_requests()
            except OSError as exc:
                log(f"ошибка: {exc}")
            time.sleep(0.7)


def main() -> None:
    parser = argparse.ArgumentParser(description="Агент Fox AI на хосте")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent, help="папка проекта")
    parser.add_argument("--gpu-name", default="P100", help="карта LLM (часть имени из nvidia-smi -L)")
    parser.add_argument("--power-limit", type=int, default=0, help="лимит мощности при старте, Вт (0 = не менять)")
    parser.add_argument("--bot-uid", type=int, default=1000, help="uid пользователя в контейнере бота")
    opts = parser.parse_args()
    agent = Agent(opts.root.resolve(), opts.gpu_name, opts.bot_uid)
    if opts.power_limit > 0:
        log(agent.set_power(str(opts.power_limit))[1])
    agent.loop()


if __name__ == "__main__":
    main()
