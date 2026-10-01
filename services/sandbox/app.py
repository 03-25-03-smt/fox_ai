"""Fox AI sandbox: компиляция и запуск C-кода, valgrind/ASan, проверка проектов 42,
запуск Python (вычисления, таблицы, графики matplotlib -> PNG).

Сервис рассчитан на запуск в отдельном контейнере: без интернета (internal-сеть),
read-only rootfs, без capabilities, с лимитами памяти/процессов. Внутри каждый запуск
дополнительно ограничен rlimit'ами и таймаутом, работает в своей временной папке.
"""

import asyncio
import base64
import os
import re
import resource
import shutil
import signal
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

MAX_FILES = 300
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_OUTPUT = 64 * 1024
MAX_LOG = 24 * 1024
OUTPUT_FILE_LIMIT = 8 * 1024 * 1024  # RLIMIT_FSIZE: программа не забьёт диск
MEMORY_LIMIT = 1024 * 1024 * 1024  # RLIMIT_AS для обычного запуска
NPROC_LIMIT = int(os.environ.get("SANDBOX_NPROC", "96"))  # процессов на uid (защита от форк-бомб)
# Добивать процессы вне активных запусков. Включается ТОЛЬКО внутри контейнера (см. Dockerfile):
# на обычной машине это убило бы все остальные процессы пользователя.
REAP_STRAYS = os.environ.get("SANDBOX_REAP_STRAYS") == "1"
COMPILE_TIMEOUT = 60
MAKE_TIMEOUT = 180
CONCURRENCY = int(os.environ.get("SANDBOX_CONCURRENCY", "2"))
WORK_ROOT = os.environ.get("SANDBOX_WORKDIR", tempfile.gettempdir())
SKIP_DIRS = ("mlx", "minilibx", ".git")
COMPILER_LINE = re.compile(r"(^|\s|/)(cc|gcc|clang|c\+\+|g\+\+|ar|ranlib)\s", re.MULTILINE)

app = FastAPI(title="fox_ai sandbox")
_slots = asyncio.Semaphore(CONCURRENCY)


class RunRequest(BaseModel):
    files: dict[str, str]
    args: list[str] = Field(default_factory=list, max_length=64)
    stdin: str = Field("", max_length=64 * 1024)
    check: Literal["none", "valgrind", "asan"] = "none"
    werror: bool = True
    timeout: float = Field(10.0, gt=0, le=60)


class ProcResult(BaseModel):
    exit_code: int | None = None
    signal: str | None = None
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0


class RunResponse(BaseModel):
    compiled: bool
    compile_command: str
    compile_output: str
    run: ProcResult | None = None
    valgrind_log: str | None = None


class PythonRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64 * 1024)
    timeout: float = Field(20.0, gt=0, le=60)


class PythonResponse(BaseModel):
    run: ProcResult
    images: list[str] = Field(default_factory=list)  # PNG в base64


class ProjectRequest(BaseModel):
    files: dict[str, str]


class NormReport(BaseModel):
    ok: bool
    errors: int
    files_checked: int
    output: str


class MakefileReport(BaseModel):
    exists: bool
    path: str | None = None
    rules: dict[str, bool] = Field(default_factory=dict)
    has_name: bool = False
    uses_wildcard: bool = False
    has_flags: bool = False
    issues: list[str] = Field(default_factory=list)


class BuildReport(BaseModel):
    attempted: bool
    ok: bool = False
    output: str = ""
    relinks: bool | None = None
    relink_output: str = ""


class ProjectResponse(BaseModel):
    files: list[str]
    norminette: NormReport
    makefile: MakefileReport
    build: BuildReport


# ---------------------------------------------------------------- утилиты


def _safe_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts or any(
        p.startswith("-") for p in path.parts
    ):
        raise HTTPException(400, f"Недопустимое имя файла: {name!r}")
    return path


def _write_files(root: Path, files: dict[str, str]) -> list[str]:
    if not files:
        raise HTTPException(400, "Нет файлов")
    if len(files) > MAX_FILES:
        raise HTTPException(400, f"Слишком много файлов (максимум {MAX_FILES})")
    if sum(len(c.encode()) for c in files.values()) > MAX_TOTAL_BYTES:
        raise HTTPException(400, "Файлы слишком большие (максимум 2 МБ)")
    written = []
    for name, content in files.items():
        rel = _safe_path(name)
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(rel.as_posix())
    return sorted(written)


_SERVER_SID = os.getsid(0)
_active_sids: set[int] = set()


def _session_of(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # поле comm в скобках может содержать пробелы — режем по последней ')'
    fields = stat[stat.rindex(")") + 2 :].split()
    return int(fields[3])  # state ppid pgrp session ...


def reap_strays() -> int:
    """Убивает все процессы, которые не принадлежат серверу или активному запуску
    (остатки форк-бомб, демоны, сбежавшие через setsid)."""
    killed = 0
    for _ in range(20):
        found = 0
        for entry in os.listdir("/proc"):
            if not entry.isdigit() or int(entry) == os.getpid():
                continue
            sid = _session_of(int(entry))
            if sid is None or sid == _SERVER_SID or sid in _active_sids:
                continue
            try:
                os.kill(int(entry), signal.SIGKILL)
                found += 1
            except (ProcessLookupError, PermissionError):
                pass
        killed += found
        if not found:
            break
        time.sleep(0.05)
    return killed


def _limits(cpu_seconds: int, limit_memory: bool):
    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_NPROC, (NPROC_LIMIT, NPROC_LIMIT))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (OUTPUT_FILE_LIMIT, OUTPUT_FILE_LIMIT))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        if limit_memory:
            resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))

    return apply


def _read_capped(path: Path, limit: int = MAX_OUTPUT) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as f:
        data = f.read(limit + 1)
    text = data[:limit].decode(errors="replace")
    return text + ("\n…[вывод обрезан]" if len(data) > limit else "")


def _tail(text: str, limit: int = MAX_LOG) -> str:
    return text if len(text) <= limit else "…[начало обрезано]\n" + text[-limit:]


async def _run(
    argv: list[str],
    cwd: Path,
    timeout: float,
    *,
    stdin: str = "",
    env: dict[str, str] | None = None,
    limit_memory: bool = True,
) -> ProcResult:
    """Запускает процесс в своей группе с rlimit'ами; вывод пишется в файлы (не в память)."""
    out_path, err_path, in_path = cwd / ".fox_stdout", cwd / ".fox_stderr", cwd / ".fox_stdin"
    in_path.write_text(stdin, encoding="utf-8")
    base_env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(cwd), "LANG": "C.UTF-8"}
    start = time.monotonic()
    with in_path.open("rb") as fin, out_path.open("wb") as fout, err_path.open("wb") as ferr:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=fin,
            stdout=fout,
            stderr=ferr,
            env={**base_env, **(env or {})},
            start_new_session=True,
            preexec_fn=_limits(int(timeout) + 1, limit_memory),
        )
        _active_sids.add(proc.pid)  # start_new_session: sid == pid
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except TimeoutError:
            timed_out = True
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # дети в той же группе
            except (ProcessLookupError, PermissionError):
                pass
            if proc.returncode is None:
                await proc.wait()
            _active_sids.discard(proc.pid)
            if REAP_STRAYS:
                await asyncio.to_thread(reap_strays)
    code = proc.returncode
    result = ProcResult(
        exit_code=code if code is not None and code >= 0 else None,
        signal=signal.Signals(-code).name if code is not None and code < 0 else None,
        timed_out=timed_out,
        stdout=_read_capped(out_path),
        stderr=_read_capped(err_path),
        duration_ms=int((time.monotonic() - start) * 1000),
    )
    for p in (out_path, err_path, in_path):
        p.unlink(missing_ok=True)
    return result


def _skipped(path: str) -> bool:
    return any(part.lower().startswith(SKIP_DIRS) for part in PurePosixPath(path).parts)


# ---------------------------------------------------------------- /run


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "ok": True,
        "valgrind": shutil.which("valgrind") is not None,
        "norminette": shutil.which("norminette") is not None,
        "python": True,
    }


@app.post("/run", response_model=RunResponse)
async def run_code(req: RunRequest) -> RunResponse:
    async with _slots:
        with tempfile.TemporaryDirectory(prefix="run_", dir=WORK_ROOT) as tmp:
            root = Path(tmp)
            names = _write_files(root, req.files)
            sources = [n for n in names if n.endswith(".c")]
            if not sources:
                raise HTTPException(400, "Нет .c файлов для компиляции")
            include_dirs = sorted({str(PurePosixPath(n).parent) for n in names if n.endswith(".h")})

            flags = ["-Wall", "-Wextra"] + (["-Werror"] if req.werror else [])
            if req.check == "asan":
                flags += ["-g", "-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
            elif req.check == "valgrind":
                flags += ["-g", "-O0"]
            argv = ["cc", *flags, *[f"-I{d}" for d in include_dirs], *sources, "-o", "fox_prog", "-lm"]
            compile_res = await _run(argv, root, COMPILE_TIMEOUT, limit_memory=False)
            compile_output = _tail((compile_res.stdout + compile_res.stderr).strip())
            compiled = compile_res.exit_code == 0 and (root / "fox_prog").exists()
            command = " ".join(a for a in argv if not a.startswith("-I"))
            if not compiled:
                return RunResponse(compiled=False, compile_command=command, compile_output=compile_output)

            prog = ["./fox_prog", *req.args]
            env: dict[str, str] = {}
            vg_log = None
            timeout = req.timeout
            if req.check == "valgrind":
                prog = [
                    "valgrind", "--leak-check=full", "--show-leak-kinds=all", "--track-fds=yes",
                    "--error-exitcode=42", "--log-file=.fox_valgrind", *prog,
                ]
                timeout = min(req.timeout * 4, 120)
            elif req.check == "asan":
                env = {
                    "ASAN_OPTIONS": "detect_leaks=1:abort_on_error=0:color=never",
                    "UBSAN_OPTIONS": "print_stacktrace=1:color=never",
                }
            run = await _run(
                prog, root, timeout, stdin=req.stdin, env=env, limit_memory=req.check == "none"
            )
            if req.check == "valgrind":
                vg_log = _tail(_read_capped(root / ".fox_valgrind", 256 * 1024))
            return RunResponse(
                compiled=True, compile_command=command, compile_output=compile_output,
                run=run, valgrind_log=vg_log,
            )


# ---------------------------------------------------------------- /python

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 2 * 1024 * 1024
# Обёртка: графики matplotlib сохраняются в файлы вместо plt.show()
PY_PRELUDE = """\
import os as _os
_os.environ.setdefault("MPLBACKEND", "Agg")
"""
PY_EPILOGUE = """
try:
    import matplotlib.pyplot as _plt
    for _i, _n in enumerate(_plt.get_fignums()[:4]):
        _plt.figure(_n).savefig(f"fox_plot_{_i}.png", dpi=110, bbox_inches="tight")
except ImportError:
    pass
"""


@app.post("/python", response_model=PythonResponse)
async def run_python(req: PythonRequest) -> PythonResponse:
    async with _slots:
        with tempfile.TemporaryDirectory(prefix="py_", dir=WORK_ROOT) as tmp:
            root = Path(tmp)
            code = req.code.replace("plt.show()", "pass")
            # Пользовательский код — в отдельном файле: номера строк в трейсбеке совпадают с его кодом
            (root / "user_code.py").write_text(code, encoding="utf-8")
            (root / "main.py").write_text(
                PY_PRELUDE
                + "_ns = {'__name__': '__main__'}\n"
                + "exec(compile(open('user_code.py', encoding='utf-8').read(), 'user_code.py', 'exec'), _ns)\n"
                + PY_EPILOGUE,
                encoding="utf-8",
            )
            run = await _run(
                ["python3", "-I", "main.py"], root, req.timeout,
                env={"MPLBACKEND": "Agg", "MPLCONFIGDIR": str(root), "OPENBLAS_NUM_THREADS": "1"},
            )
            images = []
            for path in sorted(root.glob("fox_plot_*.png"))[:MAX_IMAGES]:
                data = path.read_bytes()
                if len(data) <= MAX_IMAGE_BYTES:
                    images.append(base64.b64encode(data).decode())
            return PythonResponse(run=run, images=images)


# ---------------------------------------------------------------- /project

RULES = ("$(NAME)", "all", "clean", "fclean", "re")


def analyze_makefile(text: str) -> MakefileReport:
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    body = "\n".join(lines)
    rules = {
        rule: re.search(rf"^{re.escape(rule)}\s*:(?!=)", body, re.MULTILINE) is not None
        for rule in RULES
    }
    rules["bonus"] = re.search(r"^bonus\s*:(?!=)", body, re.MULTILINE) is not None
    has_name = re.search(r"^NAME\s*[:+?]?=", body, re.MULTILINE) is not None
    wildcard = bool(re.search(r"\$\(\s*wildcard|\$\(\s*shell\s+(find|ls)|\*\.c", body))
    flags = all(f in body for f in ("-Wall", "-Wextra", "-Werror"))
    issues = [f"Нет обязательного правила {r}" for r in RULES if not rules[r]]
    if not has_name:
        issues.append("Не задана переменная NAME")
    if wildcard:
        issues.append("Используется wildcard/поиск файлов — исходники нужно перечислять явно")
    if not flags:
        issues.append("Нет флагов -Wall -Wextra -Werror")
    first_rule = re.search(r"^([A-Za-z_$()][\w$().-]*)\s*:(?!=)", body, re.MULTILINE)
    if first_rule and first_rule.group(1) != "all":
        issues.append(f"Правило по умолчанию — {first_rule.group(1)}, а должно быть all")
    return MakefileReport(
        exists=True, rules=rules, has_name=has_name, uses_wildcard=wildcard,
        has_flags=flags, issues=issues,
    )


def _count_norm_errors(output: str) -> int:
    return sum(1 for line in output.splitlines() if line.startswith("Error:"))


@app.post("/project", response_model=ProjectResponse)
async def check_project(req: ProjectRequest) -> ProjectResponse:
    async with _slots:
        with tempfile.TemporaryDirectory(prefix="proj_", dir=WORK_ROOT) as tmp:
            root = Path(tmp)
            names = _write_files(root, req.files)

            norm_files = [n for n in names if n.endswith((".c", ".h")) and not _skipped(n)]
            if norm_files:
                res = await _run(
                    ["norminette", "--no-colors", *norm_files], root, 120, limit_memory=False
                )
                output = _tail((res.stdout + res.stderr).strip())
                errors = _count_norm_errors(res.stdout)
                norm = NormReport(ok=res.exit_code == 0 and errors == 0, errors=errors,
                                  files_checked=len(norm_files), output=output)
            else:
                norm = NormReport(ok=True, errors=0, files_checked=0, output="Нет .c/.h файлов")

            makefiles = sorted(
                (n for n in names if PurePosixPath(n).name == "Makefile" and not _skipped(n)),
                key=lambda n: len(PurePosixPath(n).parts),
            )
            if not makefiles:
                return ProjectResponse(
                    files=names, norminette=norm,
                    makefile=MakefileReport(exists=False, issues=["Нет Makefile"]),
                    build=BuildReport(attempted=False),
                )
            mk_path = makefiles[0]
            report = analyze_makefile((root / mk_path).read_text(encoding="utf-8", errors="replace"))
            report.path = mk_path
            mk_dir = root / PurePosixPath(mk_path).parent

            first = await _run(["make", "-j2"], mk_dir, MAKE_TIMEOUT, limit_memory=False)
            build = BuildReport(
                attempted=True,
                ok=first.exit_code == 0 and not first.timed_out,
                output=_tail((first.stdout + first.stderr).strip(), 8000),
            )
            if build.ok:
                second = await _run(["make"], mk_dir, MAKE_TIMEOUT, limit_memory=False)
                again = (second.stdout + second.stderr).strip()
                build.relinks = bool(COMPILER_LINE.search(again))
                build.relink_output = _tail(again, 3000)
                if build.relinks:
                    report.issues.append("Relink: повторный make снова что-то компилирует/линкует")
            return ProjectResponse(files=names, norminette=norm, makefile=report, build=build)
