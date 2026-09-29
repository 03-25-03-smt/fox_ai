"""Состояние видеокарт через nvidia-smi (в контейнер бота пробрасывается только утилита)."""

import asyncio
import shutil
from dataclasses import dataclass

QUERY = "index,name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,fan.speed"


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


async def query_gpus() -> list[GpuInfo] | None:
    """None — nvidia-smi недоступен."""
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
