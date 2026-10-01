"""Метрики Prometheus: запросы, скорость моделей, очередь, GPU (из снимка gpu-stats.ps1).

HTTP-эндпоинт /metrics поднимается на METRICS_PORT внутри docker-сети (наружу не публикуется),
его опрашивает сервис prometheus, графики — в Grafana.
"""

import time

from prometheus_client import CollectorRegistry, Counter, Histogram, start_http_server
from prometheus_client.core import GaugeMetricFamily

from .gpu import read_stats_file

REGISTRY = CollectorRegistry()

REQUESTS = Counter("fox_requests", "Ответы модели", ["mode", "model"], registry=REGISTRY)
ERRORS = Counter("fox_errors", "Ошибки модели", ["model"], registry=REGISTRY)
TOKENS = Counter("fox_tokens", "Сгенерировано токенов", ["model"], registry=REGISTRY)
TOOLS = Counter("fox_tool_calls", "Вызовы инструментов", ["tool"], registry=REGISTRY)
RESPONSE_SECONDS = Histogram(
    "fox_response_seconds", "Время ответа (от запроса до конца генерации)", ["mode"],
    buckets=(1, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 300), registry=REGISTRY,
)
TPS = Histogram(
    "fox_tokens_per_second", "Скорость генерации", ["model"],
    buckets=(5, 10, 15, 20, 25, 30, 40, 50, 60, 80, 100, 150), registry=REGISTRY,
)


def record_speed(model: str, tokens: int, tps: float) -> None:
    TOKENS.labels(model).inc(tokens)
    TPS.labels(model).observe(tps)


class AppCollector:
    """Значения, которые читаются в момент опроса: очередь, режим GPU, температуры."""

    def __init__(self, app) -> None:
        self.app = app

    def collect(self):
        app = self.app
        q = app.queue
        queue = GaugeMetricFamily("fox_queue", "Очередь генераций", labels=["state"])
        queue.add_metric(["active"], q.active)
        queue.add_metric(["waiting"], q.waiting)
        queue.add_metric(["limit"], q.limit)
        yield queue
        yield GaugeMetricFamily("fox_throttled", "Бережный режим из-за перегрева", value=int(app.throttled))
        yield GaugeMetricFamily("fox_uptime_seconds", "Аптайм бота", value=time.time() - app.started_at)
        host = app.host
        yield GaugeMetricFamily("fox_host_agent_up", "Агент Windows отвечает",
                                value=int(bool(host and host.alive)))

        gpus = read_stats_file(app.settings.gpu_stats_file) if app.settings.gpu_stats_file else None
        families = {
            "temperature": GaugeMetricFamily("fox_gpu_temperature_celsius", "Температура GPU", labels=["gpu"]),
            "utilization": GaugeMetricFamily("fox_gpu_utilization_percent", "Загрузка GPU", labels=["gpu"]),
            "memory_used": GaugeMetricFamily("fox_gpu_memory_used_mib", "Занято видеопамяти", labels=["gpu"]),
            "memory_total": GaugeMetricFamily("fox_gpu_memory_total_mib", "Всего видеопамяти", labels=["gpu"]),
            "power": GaugeMetricFamily("fox_gpu_power_watts", "Потребление GPU", labels=["gpu"]),
            "fan": GaugeMetricFamily("fox_gpu_fan_percent", "Вентилятор GPU", labels=["gpu"]),
        }
        for g in gpus or []:
            for field, family in families.items():
                value = getattr(g, field)
                if value is not None:
                    family.add_metric([g.name], value)
        yield from families.values()


def start(app, port: int) -> None:
    REGISTRY.register(AppCollector(app))
    start_http_server(port, addr="0.0.0.0", registry=REGISTRY)
