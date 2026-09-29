"""Загрузка проекта 42 из zip-архива или публичного git-репозитория в словарь {путь: текст}."""

import asyncio
import io
import os
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .web import WebError, ensure_public_url

TEXT_SUFFIXES = {".c", ".h", ".cpp", ".hpp", ".tpp", ".py", ".sh", ".md", ".txt", ".s", ".asm", ".mk"}
TEXT_NAMES = {"Makefile", "makefile", "GNUmakefile", "CMakeLists.txt", ".gitignore"}
SKIP_DIRS = {".git", "__MACOSX", "node_modules", ".vscode", ".idea", "build", "obj"}
MAX_FILES = 300
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
GIT_TIMEOUT = 90
ALLOWED_GIT_HOSTS = {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org"}


class ProjectError(Exception):
    pass


def _wanted(path: PurePosixPath) -> bool:
    if any(part in SKIP_DIRS or part.startswith("._") for part in path.parts):
        return False
    return path.name in TEXT_NAMES or path.suffix.lower() in TEXT_SUFFIXES


def _strip_common_root(files: dict[str, str]) -> dict[str, str]:
    """Если всё лежит в одной папке (archive/proj/...), убираем её из путей."""
    while files:
        firsts = {PurePosixPath(p).parts[0] for p in files}
        if len(firsts) != 1 or any(len(PurePosixPath(p).parts) == 1 for p in files):
            break
        root = firsts.pop()
        files = {str(PurePosixPath(*PurePosixPath(p).parts[1:])): c for p, c in files.items()}
        del root
    return files


class _Collector:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.total = 0
        self.skipped = 0

    def add(self, rel: PurePosixPath, data: bytes) -> None:
        if not _wanted(rel):
            return
        if len(data) > MAX_FILE_BYTES or len(self.files) >= MAX_FILES or self.total + len(data) > MAX_TOTAL_BYTES:
            self.skipped += 1
            return
        self.files[rel.as_posix()] = data.decode("utf-8", errors="replace")
        self.total += len(data)


def files_from_zip(data: bytes) -> tuple[dict[str, str], int]:
    """Возвращает (файлы, сколько пропущено из-за лимитов). Архив читается в памяти."""
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ProjectError("Архив больше 20 МБ")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ProjectError("Это не zip-архив") from exc
    collector = _Collector()
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            rel = PurePosixPath(info.filename.replace("\\", "/"))
            if rel.is_absolute() or ".." in rel.parts:
                continue
            if info.file_size > MAX_FILE_BYTES:  # не распаковываем «zip-бомбы»
                if _wanted(rel):
                    collector.skipped += 1
                continue
            collector.add(rel, archive.read(info))
    if not collector.files:
        raise ProjectError("В архиве нет исходников (.c/.h/Makefile)")
    return _strip_common_root(collector.files), collector.skipped


def _collect_dir(root: Path) -> tuple[dict[str, str], int]:
    collector = _Collector()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            rel = PurePosixPath(path.relative_to(root).as_posix())
            if _wanted(rel) and path.stat().st_size <= MAX_FILE_BYTES:
                collector.add(rel, path.read_bytes())
            elif _wanted(rel):
                collector.skipped += 1
    return collector.files, collector.skipped


async def files_from_git(url: str) -> tuple[dict[str, str], int]:
    parts = urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in ALLOWED_GIT_HOSTS:
        raise ProjectError(
            "Поддерживаются только публичные https-репозитории на "
            + ", ".join(sorted(ALLOWED_GIT_HOSTS))
        )
    if parts.username or parts.password:
        raise ProjectError("Не присылай ссылки с логином/токеном")
    try:
        await ensure_public_url(url)
    except WebError as exc:
        raise ProjectError(str(exc)) from exc

    with tempfile.TemporaryDirectory(prefix="fox_git_") as tmp:
        target = Path(tmp) / "repo"
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": tmp,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_LFS_SKIP_SMUDGE": "1",
        }
        proc = await asyncio.create_subprocess_exec(
            "git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
            "-c", "http.followRedirects=false", "-c", "core.symlinks=false",
            "clone", "--depth", "1", "--single-branch", "--no-tags", "--", url, str(target),
            env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise ProjectError("git clone не уложился в 90 секунд") from None
        if proc.returncode != 0:
            msg = stderr.decode(errors="replace").strip().splitlines()
            raise ProjectError("Не удалось склонировать: " + (msg[-1] if msg else "ошибка git"))
        files, skipped = await asyncio.to_thread(_collect_dir, target)
    if not files:
        raise ProjectError("В репозитории нет исходников (.c/.h/Makefile)")
    return files, skipped


def sources_digest(files: dict[str, str], limit: int = 14000) -> str:
    """Склейка исходников для промпта: сначала заголовки и Makefile, потом .c."""
    def order(path: str) -> tuple[int, str]:
        name = PurePosixPath(path).name
        if name in TEXT_NAMES:
            return 0, path
        return (1 if path.endswith(".h") else 2 if path.endswith(".c") else 3), path

    out, size = [], 0
    for path in sorted(files, key=order):
        block = f"// ===== {path} =====\n{files[path]}\n"
        if size + len(block) > limit:
            out.append(f"// ...и ещё {len(files) - len(out)} файлов не поместились")
            break
        out.append(block)
        size += len(block)
    return "\n".join(out)
