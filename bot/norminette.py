"""Запуск настоящего norminette на присланном коде."""

import asyncio
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

MAX_SOURCE_BYTES = 256 * 1024
TIMEOUT = 30.0
_SAFE_NAME = re.compile(r"^[a-z0-9_]+\.(c|h)$")


class NorminetteError(Exception):
    pass


@dataclass(frozen=True)
class NormResult:
    filename: str
    ok: bool
    errors: int
    output: str


def safe_filename(name: str | None, source: str = "") -> str:
    """Имя файла без путей. Если имя не по норме 42 — подставляем нейтральное."""
    base = Path(name or "").name.lower()
    if _SAFE_NAME.match(base):
        return base
    is_header = base.endswith(".h") or (
        re.search(r"#\s*ifndef", source) is not None
        and re.search(r"#\s*define", source) is not None
        and "main(" not in source
    )
    return "file.h" if is_header else "file.c"


def extract_code(text: str) -> str:
    """Достаёт код из ```блока```, если он есть, иначе возвращает текст как есть."""
    match = re.search(r"```[\w+#.-]*\n(.*?)```", text, re.DOTALL)
    return match.group(1) if match else text


async def run_norminette(source: str, filename: str) -> NormResult:
    data = source.encode("utf-8")
    if len(data) > MAX_SOURCE_BYTES:
        raise NorminetteError("Файл слишком большой (максимум 256 КБ)")

    with tempfile.TemporaryDirectory(prefix="fox_norm_") as tmp:
        (Path(tmp) / filename).write_bytes(data)
        # Запускаем без shell, в отдельной папке и с относительным путём —
        # чтобы в выводе не было путей сервера.
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "norminette", "--no-colors", filename,
            cwd=tmp,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise NorminetteError("norminette не уложился в 30 секунд") from None

    output = stdout.decode(errors="replace").strip()
    errors = sum(1 for line in output.splitlines() if line.startswith("Error:"))
    ok = proc.returncode == 0 and errors == 0
    if not ok and errors == 0 and "Error" not in output:
        raise NorminetteError(f"norminette завершился с ошибкой:\n{output[-1000:]}")
    return NormResult(filename=filename, ok=ok, errors=errors, output=output)
