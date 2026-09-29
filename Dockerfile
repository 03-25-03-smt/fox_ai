FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

# Не запускаем от root
RUN useradd --create-home --uid 1000 fox && mkdir -p /app/data && chown fox:fox /app/data
USER fox

CMD ["python", "-m", "bot"]
