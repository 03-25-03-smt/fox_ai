"""Метрики Prometheus."""

import time
import types

from prometheus_client import generate_latest

from bot import metrics
from bot.app import GpuQueue
from bot.llm import OllamaClient

from .conftest import ADMIN


def value(name: str, **labels) -> float:
    return metrics.REGISTRY.get_sample_value(name, labels) or 0


def test_collector_reads_queue_and_gpu(tmp_path):
    stats = tmp_path / "gpu.csv"
    stats.write_text("1, Tesla P100-PCIE-16GB, 71, 88, 9800, 16384, 190.50, [N/A]\n")
    app = types.SimpleNamespace(queue=GpuQueue(2), throttled=True, started_at=time.time() - 60, host=None,
                                settings=types.SimpleNamespace(gpu_stats_file=str(stats)))
    families = {f.name: f for f in metrics.AppCollector(app).collect()}
    assert families["fox_throttled"].samples[0].value == 1
    assert {s.labels["state"]: s.value for s in families["fox_queue"].samples} == {
        "active": 0, "waiting": 0, "limit": 2}
    temp = families["fox_gpu_temperature_celsius"].samples[0]
    assert temp.labels == {"gpu": "Tesla P100-PCIE-16GB"} and temp.value == 71
    assert not families["fox_gpu_fan_percent"].samples  # у P100 вентилятора нет: [N/A]


def total_requests() -> float:
    return sum(s.value for m in metrics.REGISTRY.collect() if m.name == "fox_requests"
               for s in m.samples if s.name == "fox_requests_total")


async def test_requests_are_counted(env):
    before = total_requests()
    await env.send(ADMIN, "привет")
    assert total_requests() == before + 1
    assert b"fox_response_seconds_bucket" in generate_latest(metrics.REGISTRY)


async def test_ollama_speed_is_counted():
    client = OllamaClient("http://x")
    before = value("fox_tokens_total", model="m")
    client._record_speed("m", {"eval_count": 50, "eval_duration": 1_000_000_000})
    assert value("fox_tokens_total", model="m") == before + 50
    assert value("fox_tokens_per_second_count", model="m") >= 1
    await client.close()
