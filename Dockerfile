FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SORTER_DATA_DIR=/config

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./

# Runs as an unprivileged user; docker-compose overrides the ids with your NAS 'media' user.
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin app
USER 1000

EXPOSE 8090
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=4)" || exit 1

ENTRYPOINT ["python", "/app/audiobook_sorter.py", "--config", "/config/config.toml"]
