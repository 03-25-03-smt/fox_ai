"""Состояние видеокарт.

Два источника:
- файл со снимком `nvidia-smi` (GPU_STATS_FILE). На Windows бот живёт в Docker/WSL2 и
  не видит P100 (она в режиме TCC), поэтому снимок раз в 20 с пишет хост —
  скрипт windows/gpu-stats.ps1;
- сам `nvidia-smi`, если он доступен (запуск без Docker или на Linux).
"""

import asyncio
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

QUERY = "index,name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,fan.speed"
# Старее этого снимок считается протухшим: скрипт на хосте не работает
STATS_MAX_AGE = 180.0


@dataclass(frozen=True)
class GpuInfo:
    index: int
    name: str
    temperature: int | None
    utilization: int | None
    memory_used: int | None  # MiB
    memory_total: int | None
    power: float | None
    fan: int | None


def _num(value: str, cast=int):
    value = value.strip()
    try:
        return cast(float(value))
    except ValueError:
        return None  # "[N/A]", "[Not Supported]"


def parse_nvidia_smi(output: str) -> list[GpuInfo]:
    gpus = []
    for line in output.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 8:
            continue
        gpus.append(GpuInfo(
            index=_num(parts[0]) or 0, name=parts[1], temperature=_num(parts[2]),
            utilization=_num(parts[3]), memory_used=_num(parts[4]), memory_total=_num(parts[5]),
            power=_num(parts[6], float), fan=_num(parts[7]),
        ))
    return gpus


def read_stats_file(path: str, max_age: float = STATS_MAX_AGE) -> list[GpuInfo] | None:
    """None — файла нет, он пустой или протух."""
    file = Path(path)
    try:
        if time.time() - file.stat().st_mtime > max_age:
            return None
        # utf-8-sig: PowerShell 5 любит дописывать BOM
        gpus = parse_nvidia_smi(file.read_text(encoding="utf-8-sig", errors="replace"))
    except OSError:
        return None
    return gpus or None


async def query_gpus(stats_file: str = "") -> list[GpuInfo] | None:
    """None — данных о GPU нет."""
    if stats_file:
        return await asyncio.to_thread(read_stats_file, stats_file)
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi", f"--query-gpu={QUERY}", "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), 15)
    except (OSError, TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    return parse_nvidia_smi(stdout.decode(errors="replace"))
