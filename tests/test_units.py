"""Юнит-тесты: разбор времени, автовыбор модели, интра, GPU, проекты, миграция БД, сервисы."""

import datetime
import importlib.util
import io
import sqlite3
import sys
import zipfile
import zoneinfo
from pathlib import Path

import httpx
import pytest
import respx

from bot.db import Database
from bot.gpu import parse_nvidia_smi
from bot.intra import IntraClient, IntraError, parse_profile
from bot.projects import ProjectError, files_from_git, files_from_zip, sources_digest
from bot.routing import choose_model
from bot.timeparse import parse_reminder

ROOT = Path(__file__).resolve().parent.parent
TZ = zoneinfo.ZoneInfo("Europe/Paris")
NOW = datetime.datetime(2026, 9, 29, 12, 0, tzinfo=TZ)  # вторник


def load_service(name: str):
    path = ROOT / "services" / name / "app.py"
    spec = importlib.util.spec_from_file_location(f"svc_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- время


@pytest.mark.parametrize(("text", "due", "body"), [
    ("через 20 минут снять пасту", "29.09 12:20", "снять пасту"),
    ("снять пасту через 20 минут", "29.09 12:20", "снять пасту"),
    ("через полчаса проверить духовку", "29.09 12:30", "проверить духовку"),
    ("через 1 час 30 минут выйти", "29.09 13:30", "выйти"),
    ("завтра в 9 защита minishell", "30.09 09:00", "защита minishell"),
    ("в пятницу в 18:30 пицца", "02.10 18:30", "пицца"),
    ("во вторник в 11 созвон", "06.10 11:00", "созвон"),
    ("31.12 23:59 новый год", "31.12 23:59", "новый год"),
    ("2026-10-05 08:00 сдать проект", "05.10 08:00", "сдать проект"),
    ("в 7 вечера кино", "29.09 19:00", "кино"),
    ("в 11 кофе", "30.09 11:00", "кофе"),
    ("купить хлеб завтра в 10", "30.09 10:00", "купить хлеб"),
    ("in 2 hours call bob", "29.09 14:00", "call bob"),
    ("напомни мне через 5 мин что чайник", "29.09 12:05", "чайник"),
])
def test_parse_reminder(text, due, body):
    parsed = parse_reminder(text, NOW)
    assert parsed is not None
    assert parsed.due.strftime("%d.%m %H:%M") == due
    assert parsed.text == body


@pytest.mark.parametrize("text", ["2 яйца купить", "сегодня купить", "через минут", "в 25:00 x", ""])
def test_parse_reminder_rejects(text):
    assert parse_reminder(text, NOW) is None


# ---------------------------------------------------------------- автовыбор модели

MODELS = ["qwen2.5:7b", "qwen2.5-coder:14b", "qwen2.5:3b"]


@pytest.mark.parametrize(("text", "expected"), [
    ("почему segfault в моём ft_split?", "qwen2.5-coder:14b"),
    ("```c\nint x;\n```", "qwen2.5-coder:14b"),
    ("как работает fork в minishell", "qwen2.5-coder:14b"),
    ("привет!", "qwen2.5:3b"),
    ("спасибо", "qwen2.5:3b"),
    ("придумай рецепт ужина из курицы и риса, без духовки", "qwen2.5:7b"),
    ("как дела?", "qwen2.5:7b"),
])
def test_choose_model(text, expected):
    choice = choose_model(text, default="qwen2.5:7b", code="qwen2.5-coder:14b", fast="qwen2.5:3b",
                          available=MODELS)
    assert choice.model == expected


def test_choose_model_falls_back_when_not_pulled():
    choice = choose_model("segfault", default="qwen2.5:7b", code="deepseek-coder", fast="",
                          available=["qwen2.5:7b"])
    assert choice.model == "qwen2.5:7b"
    assert choose_model("hi", default="d", code="c", fast="f:latest", available=None).model == "f:latest"


# ---------------------------------------------------------------- интра

INTRA_USER = {
    "login": "vborodii", "displayname": "Fox Student", "wallet": 120, "correction_point": 4,
    "campus": [{"name": "Paris"}],
    "cursus_users": [
        {"cursus_id": 9, "cursus": {"slug": "c-piscine"}, "level": 9.1, "blackholed_at": None},
        {"cursus_id": 21, "cursus": {"slug": "42cursus"}, "level": 5.42, "grade": "Learner",
         "blackholed_at": "2026-10-20T10:00:00.000Z"},
    ],
    "projects_users": [
        {"status": "finished", "final_mark": 125, "validated?": True, "cursus_ids": [21],
         "marked_at": "2026-09-01T10:00:00.000Z", "project": {"name": "Libft", "parent_id": None}},
        {"status": "in_progress", "final_mark": None, "validated?": None, "cursus_ids": [21],
         "project": {"name": "minishell", "parent_id": None}},
        {"status": "finished", "final_mark": 80, "validated?": True, "cursus_ids": [9],
         "project": {"name": "C Piscine Shell 00", "parent_id": None}},
    ],
}


def test_parse_profile():
    p = parse_profile(INTRA_USER)
    assert (p.login, p.campus, p.level, p.grade) == ("vborodii", "Paris", 5.42, "Learner")
    assert [x.name for x in p.in_progress] == ["minishell"]
    assert [x.name for x in p.finished] == ["Libft"]
    now = datetime.datetime(2026, 9, 29, 10, tzinfo=datetime.UTC)
    assert p.blackhole_days(now) == 21


@respx.mock
async def test_intra_client_token_and_cache():
    token = respx.post("https://api.intra.42.fr/oauth/token").respond(
        json={"access_token": "t", "expires_in": 7200})
    user = respx.get("https://api.intra.42.fr/v2/users/vborodii").respond(json=INTRA_USER)
    client = IntraClient("id", "secret")
    assert (await client.get_profile("VBorodii")).login == "vborodii"
    await client.get_profile("vborodii")
    assert token.call_count == 1 and user.call_count == 1
    assert user.calls.last.request.headers["authorization"] == "Bearer t"
    respx.get("https://api.intra.42.fr/v2/users/nobody").respond(404)
    with pytest.raises(IntraError, match="не найден"):
        await client.get_profile("nobody")
    with pytest.raises(IntraError, match="Некорректный"):
        await client.get_profile("../admin")
    await client.close()


# ---------------------------------------------------------------- GPU


def test_parse_nvidia_smi():
    out = ("0, Tesla P100-PCIE-16GB, 71, 98, 15321, 16384, 201.33, [N/A]\n"
           "1, NVIDIA GeForce RTX 3070, 45, 3, 900, 8192, 30.10, 35\n")
    p100, rtx = parse_nvidia_smi(out)
    assert (p100.temperature, p100.utilization, p100.fan) == (71, 98, None)
    assert (rtx.index, rtx.memory_total, rtx.fan) == (1, 8192, 35)


# ---------------------------------------------------------------- проекты


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_zip_filters_and_strips_root():
    files, skipped = files_from_zip(_zip({
        "proj/Makefile": b"all:", "proj/src/main.c": b"int main;", "proj/inc/a.h": b"",
        "proj/.git/config": b"x", "proj/obj/main.o": b"\x00", "__MACOSX/proj/._main.c": b"x",
        "proj/big.c": b"x" * 300_000,
    }))
    assert set(files) == {"Makefile", "src/main.c", "inc/a.h"}
    assert skipped == 1


def test_zip_rejects_traversal_and_garbage():
    files, _ = files_from_zip(_zip({"../evil.c": b"x", "ok.c": b"int y;"}))
    assert set(files) == {"ok.c"}
    with pytest.raises(ProjectError):
        files_from_zip(b"not a zip")
    with pytest.raises(ProjectError):
        files_from_zip(_zip({"readme.png": b"x"}))


@pytest.mark.parametrize("url", [
    "http://github.com/a/b", "https://evil.example/a/b", "https://user:pass@github.com/a/b",
    "https://192.168.1.1/a", "file:///etc/passwd",
])
async def test_git_url_validation(url):
    with pytest.raises(ProjectError):
        await files_from_git(url)


def test_sources_digest_order_and_limit():
    digest = sources_digest({"b.c": "int b;", "Makefile": "all:", "a.h": "int a;"})
    assert digest.index("Makefile") < digest.index("a.h") < digest.index("b.c")
    assert "не поместились" in sources_digest({f"f{i}.c": "x" * 500 for i in range(10)}, limit=1200)


# ---------------------------------------------------------------- миграция БД v1 -> v2


async def test_migration_from_v1(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '', model TEXT,
                                added_by INTEGER, created_at TEXT NOT NULL DEFAULT (datetime('now')));
            CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                                   role TEXT NOT NULL, content TEXT NOT NULL,
                                   created_at TEXT NOT NULL DEFAULT (datetime('now')));
            INSERT INTO users (id, name, model) VALUES (1, 'old', 'qwen2.5:7b');
            INSERT INTO messages (user_id, role, content) VALUES (1, 'user', 'q'), (1, 'assistant', 'a');
        """)
    db = Database(str(path))
    await db.connect()
    user = await db.get_user(1)
    assert (user.name, user.model, user.voice_reply) == ("old", "qwen2.5:7b", False)
    assert await db.get_history(1, 10) == [{"role": "user", "content": "q"},
                                           {"role": "assistant", "content": "a"}]
    await db.close()
    db = Database(str(path))  # повторный запуск — без ошибок и дублей
    await db.connect()
    assert len(await db.get_history(1, 10)) == 2
    await db.close()


# ---------------------------------------------------------------- сервисы


def test_sandbox_makefile_analysis():
    sandbox = load_service("sandbox")
    good = ("NAME = libft.a\nCFLAGS = -Wall -Wextra -Werror\nSRCS = a.c\n\nall: $(NAME)\n\n"
            "$(NAME): $(SRCS:.c=.o)\n\tar rcs $@ $^\n\nclean:\n\trm -f *.o\n\n"
            "fclean: clean\n\trm -f $(NAME)\n\nre: fclean all\n\n.PHONY: all clean fclean re\n")
    assert sandbox.analyze_makefile(good).issues == []
    bad = sandbox.analyze_makefile("SRCS = $(wildcard *.c)\n$(NAME):\n\tcc $(SRCS)\n")
    joined = " ".join(bad.issues)
    assert "wildcard" in joined and "fclean" in joined and "NAME" in joined and "-Werror" in joined


def test_sandbox_rejects_bad_paths():
    sandbox = load_service("sandbox")
    for name in ("../x.c", "/etc/x.c", "-rf.c", "a/../../b.c"):
        with pytest.raises(sandbox.HTTPException):
            sandbox._safe_path(name)
    assert str(sandbox._safe_path("src/main.c")) == "src/main.c"


def test_speech_text_cleanup():
    speech = load_service("speech")
    out = speech.clean_for_speech("**Итак**, вот код:\n```c\nint x;\n```\nСм. https://x.dev `ok`")
    assert "int x" not in out and "https" not in out and "*" not in out
    assert out.startswith("Итак, вот код:")


@respx.mock
async def test_service_clients():
    from bot.services import SandboxClient, ServiceError, SpeechClient

    respx.post("http://sb/run").respond(json={
        "compiled": True, "compile_command": "cc", "compile_output": "", "valgrind_log": None,
        "run": {"exit_code": None, "signal": "SIGSEGV", "timed_out": False, "stdout": "",
                "stderr": "", "duration_ms": 1},
    })
    result = await SandboxClient("http://sb").run({"main.c": "x"})
    assert result.run.signal == "SIGSEGV" and result.has_problems

    respx.post("http://sp/stt").respond(json={"text": " привет "})
    respx.post("http://sp/tts").respond(content=b"OggS")
    speech = SpeechClient("http://sp")
    assert await speech.transcribe(b"x") == "привет"
    assert await speech.synthesize("hi") == b"OggS"
    respx.post("http://sp/stt").respond(500, json={"detail": "GPU OOM"})
    with pytest.raises(ServiceError, match="GPU OOM"):
        await speech.transcribe(b"x")
    respx.get("http://sp/health").mock(side_effect=httpx.ConnectError("down"))
    assert not await speech.health()
