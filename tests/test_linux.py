"""Скрипты хоста Linux: агент linux/fox-agent.py и подготовка .env (linux/env_setup.py)."""

import importlib.util
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

LINUX = Path(__file__).resolve().parent.parent / "linux"


def load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, LINUX / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent_mod = load("fox_agent", "fox-agent.py")
env_setup = load("env_setup", "env_setup.py")

FAKE_SMI = """#!/bin/sh
echo "$@" >> "$FAKE_LOG"
case "$1" in
  --query-gpu=index,name,power.limit,power.default_limit)
    echo "0, NVIDIA GeForce RTX 3070, 220.00, 220.00"
    echo "1, Tesla P100-PCIE-16GB, 200.00, 250.00" ;;
  --query-gpu=index,name,temperature.gpu*)
    echo "0, NVIDIA GeForce RTX 3070, 45, 3, 300, 8192, 20.5, 30"
    echo "1, Tesla P100-PCIE-16GB, 61, 90, 9000, 16384, 180.1, [N/A]" ;;
  --query-gpu=name,uuid)
    echo "NVIDIA GeForce RTX 3070, GPU-3070"
    echo "Tesla P100-PCIE-16GB, GPU-p100" ;;
esac
"""
FAKE_DOCKER = """#!/bin/sh
echo "docker $@" >> "$FAKE_LOG"
case "$2" in
  config) printf 'bot\\nspeech\\nollama\\n' ;;
  logs) echo "log line" ;;
  ps) echo "bot Up" ;;
esac
"""


@pytest.fixture
def agent(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("nvidia-smi", FAKE_SMI), ("docker", FAKE_DOCKER)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.log"))
    root = tmp_path / "project"
    root.mkdir()
    a = agent_mod.Agent(root, "P100", os.getuid())
    a.log = tmp_path / "calls.log"
    yield a
    a.pool.shutdown(wait=True)
    os.close(a.req_fd)


def request(a, cmd: str, *args: str, created: float | None = None) -> str:
    rid = os.urandom(16).hex()
    body = {"id": rid, "cmd": cmd, "args": list(args), "created": created or time.time()}
    (a.req_dir / f"{rid}.json").write_text(json.dumps(body))
    return rid


def answer(a, rid: str) -> dict:
    a.poll_requests()
    a.pool.shutdown(wait=True)
    a.pool = agent_mod.ThreadPoolExecutor(max_workers=2)
    return json.loads((a.resp_dir / f"{rid}.json").read_text())


def test_agent_dirs_and_stats(agent):
    assert stat.S_IMODE(os.stat(agent.req_dir).st_mode) == 0o700
    agent.write_gpu_stats()
    agent.write_status()
    from bot.gpu import read_stats_file

    gpus = read_stats_file(str(agent.gpu_file))
    assert [(g.name, g.temperature, g.fan) for g in gpus] == [
        ("NVIDIA GeForce RTX 3070", 45, 30), ("Tesla P100-PCIE-16GB", 61, None)]
    status = json.loads((agent.host_dir / "status.json").read_text())
    assert status["power"] == "Tesla P100-PCIE-16GB: лимит 200 из 250 Вт"


def test_agent_commands(agent):
    assert answer(agent, request(agent, "logs", "speech", "30"))["output"].strip() == "log line"
    assert answer(agent, request(agent, "restart", "ollama"))["ok"]
    assert answer(agent, request(agent, "ps"))["output"].strip() == "bot Up"
    assert answer(agent, request(agent, "power", "150")) == {"ok": True, "output": "лимит Tesla P100-PCIE-16GB: 150 Вт"}
    assert answer(agent, request(agent, "power", "default"))["output"].endswith("250 Вт")
    calls = agent.log.read_text()
    assert "docker compose logs --no-color --tail 30 speech" in calls
    assert "docker compose restart ollama" in calls
    assert "-i 1 -pl 150" in calls and "-i 1 -pl 250" in calls
    assert list(agent.req_dir.iterdir()) == []  # запросы удаляются


def test_agent_rejects_bad_requests(agent):
    assert answer(agent, request(agent, "logs", "../../etc"))["output"] == "нет сервиса ../../etc"
    assert answer(agent, request(agent, "restart", "postgres"))["output"] == "нет сервиса postgres"
    assert answer(agent, request(agent, "power", "999"))["output"] == "лимит вне 100–300 Вт"
    assert answer(agent, request(agent, "power", "; reboot"))["ok"] is False
    assert answer(agent, request(agent, "sh", "-c", "id"))["output"] == "неизвестная команда sh"
    assert answer(agent, request(agent, "ps", "x" * 100))["output"] == "неверные аргументы"
    assert answer(agent, request(agent, "ps", created=time.time() - 600))["output"] == "запрос устарел"
    assert "reboot" not in agent.log.read_text()


def test_agent_ignores_symlinks_and_junk(agent, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    rid = os.urandom(16).hex()
    (agent.req_dir / f"{rid}.json").symlink_to(secret)
    (agent.req_dir / f"{os.urandom(16).hex()}.json").write_text("не json")
    (agent.req_dir / "notes.txt").write_text("мусор")
    agent.poll_requests()
    agent.pool.shutdown(wait=True)
    assert secret.read_text() == "top secret"  # цель ссылки не тронута
    assert not (agent.resp_dir / f"{rid}.json").exists()
    assert [p.name for p in agent.req_dir.iterdir()] == ["notes.txt"]  # свежий мусор ждёт 10 минут


def test_agent_refuses_symlinked_dir(tmp_path):
    root = tmp_path / "project"
    (root / "data").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (root / "data" / "host").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(SystemExit):
        agent_mod.Agent(root, "P100", os.getuid())


def test_env_setup(tmp_path, capsys):
    example = tmp_path / ".env.example"
    example.write_text("BOT_TOKEN=123456:replace-me\nADMIN_IDS=111\nSEARXNG_SECRET=replace-me\n"
                       "GRAFANA_PASSWORD=replace-me\nGPU_LLM=auto\nGPU_IMAGEGEN=auto\nGPU_SPEECH=auto\nNEW_OPTION=1\n")
    (tmp_path / "docker-compose.yml").write_text(
        'a: ["${GPU_LLM:?x}"]\nb: ["${GPU_IMAGEGEN:?x}"]\n# c: ["${GPU_SPEECH:?x}"]\n')
    env = tmp_path / ".env"
    env.write_text("BOT_TOKEN=123456:replace-me\nSEARXNG_SECRET=replace-me\nGPU_LLM=auto\nGPU_IMAGEGEN=GPU-mine\n"
                   "GPU_SPEECH=auto\n")
    gpus = [("NVIDIA GeForce RTX 3070", "GPU-3070"), ("Tesla P100-PCIE-16GB", "GPU-p100")]
    problems = env_setup.setup(env, example, gpus)
    text = env.read_text()
    assert "GPU_LLM=GPU-p100" in text and "GPU_IMAGEGEN=GPU-mine" in text  # своё не трогаем
    assert "GPU_SPEECH=auto" in text  # speech закомментирован — карта не нужна
    assert "NEW_OPTION=1" in text and "ADMIN_IDS" not in text
    assert "replace-me\nGPU" not in text and len(text.split("SEARXNG_SECRET=")[1].split()[0]) == 64
    assert [p.split(" ")[0] for p in problems] == ["BOT_TOKEN", "ADMIN_IDS"]

    env.write_text(env.read_text().replace("123456:replace-me", "123456:" + "A" * 35) + "ADMIN_IDS=42\n")
    assert env_setup.setup(env, example, gpus) == []
    assert "новых настроек нет" in capsys.readouterr().out

    env.write_text(env.read_text() + "BACKUP_HOST_DIR=backups\n")
    assert [p.split("=")[0] for p in env_setup.setup(env, example, gpus)] == ["BACKUP_HOST_DIR"]


@pytest.mark.skipif(sys.platform != "linux", reason="bash-скрипты")
def test_shell_scripts_parse():
    import subprocess

    for script in ("setup-host.sh", "install.sh"):
        subprocess.run(["bash", "-n", str(LINUX / script)], check=True)
