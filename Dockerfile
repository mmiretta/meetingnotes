FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=5002

# ffmpeg converts browser recordings (webm/ogg/mp4) to WAV before transcription.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py demo.py gunicorn.conf.py babel.cfg ./
COPY templates/ templates/
COPY static/ static/
COPY translations/ translations/
COPY demo/ demo/
RUN pybabel compile -d translations

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown app:app /data
USER app

EXPOSE 5002
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '5002'), timeout=4)"

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
