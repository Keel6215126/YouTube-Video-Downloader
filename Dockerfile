FROM brainicism/bgutil-ytdlp-pot-provider:1.3.1-deno AS pot_provider
FROM denoland/deno:bin-2.9.2 AS deno

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DOWNLOAD_ROOT=/tmp/railway-youtube-downloader \
    POT_PROVIDER_HOME=/opt/bgutil-provider \
    POT_PROVIDER_URL=http://127.0.0.1:4416 \
    DENO_DIR=/opt/bgutil-provider/.cache/deno \
    DENO_NO_PROMPT=1 \
    DENO_NO_UPDATE_CHECK=1

COPY --from=deno /deno /usr/local/bin/deno
COPY --from=pot_provider /app /opt/bgutil-provider

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && deno --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p "${DOWNLOAD_ROOT}" "${DENO_DIR}" \
    && chown -R appuser:appuser /app "${DOWNLOAD_ROOT}" /opt/bgutil-provider

USER appuser

EXPOSE 8080

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --proxy-headers --forwarded-allow-ips=*"]
