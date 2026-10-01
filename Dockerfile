FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# git — для /project <ссылка на репозиторий>, шрифт DejaVu — кириллица в /export pdf
RUN apt-get update \
    && apt-get install -y --no-install-recommends git fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

# Не запускаем от root
RUN useradd --create-home --uid 1000 fox \
    && mkdir -p /app/data /app/backups /app/knowledge \
    && chown fox:fox /app/data /app/backups /app/knowledge
USER fox

CMD ["python", "-m", "bot"]
