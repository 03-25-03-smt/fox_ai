"""Бэкап SQLite (онлайн, через sqlite3 backup API) со сжатием и ротацией."""

import asyncio
import datetime
import gzip
import shutil
import sqlite3
from pathlib import Path

PREFIX = "fox_ai-"
SUFFIX = ".sqlite3.gz"


def _backup_sync(db_path: str, backup_dir: Path, keep: int) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    raw = backup_dir / f".{PREFIX}{stamp}.sqlite3.tmp"
    target = backup_dir / f"{PREFIX}{stamp}{SUFFIX}"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(raw)
    try:
        src.backup(dst)  # консистентная копия даже при активной записи
    finally:
        dst.close()
        src.close()
    with raw.open("rb") as fin, gzip.open(target, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout)
    raw.unlink()
    for old in sorted(backup_dir.glob(f"{PREFIX}*{SUFFIX}"))[:-keep]:
        old.unlink()
    return target


async def backup_database(db_path: str, backup_dir: str, keep: int) -> Path:
    return await asyncio.to_thread(_backup_sync, db_path, Path(backup_dir), max(keep, 1))


def list_backups(backup_dir: str) -> list[Path]:
    path = Path(backup_dir)
    return sorted(path.glob(f"{PREFIX}*{SUFFIX}")) if path.is_dir() else []
