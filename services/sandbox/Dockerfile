FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SANDBOX_REAP_STRAYS=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev make valgrind \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

# Непривилегированный пользователь; код пользователей запускается от него же
RUN useradd --create-home --uid 10001 runner
USER runner

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
