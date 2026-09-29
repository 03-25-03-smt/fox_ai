FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HF_HOME=/models/hf

WORKDIR /srv
# torch с CUDA 12.6 (работает с драйвером 580; RTX 3070 = sm_86)
RUN pip install --no-cache-dir torch==2.14.0 --index-url https://download.pytorch.org/whl/cu126
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

RUN useradd --create-home --uid 1000 imagegen && mkdir -p /models && chown imagegen /models
USER imagegen

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
