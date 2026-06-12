FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ENV ASR_MODEL=base.en
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${ASR_MODEL}', device='cpu', compute_type='int8')"

COPY . .

ENV PORT=8080
CMD exec gunicorn --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT app:app
