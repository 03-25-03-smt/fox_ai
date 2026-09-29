FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HF_HOME=/models/hf

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# CUDA-библиотеки для ctranslate2 (faster-whisper) ставятся pip-пакетами
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib

# Русский голос Piper (~60 МБ) скачиваем при сборке
RUN python -m piper.download_voices --download-dir /opt/voices ru_RU-irina-medium

COPY app.py .
RUN useradd --create-home --uid 1000 speech && mkdir -p /models && chown speech /models
USER speech

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
