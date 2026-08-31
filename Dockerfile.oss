# Open-source edition: the shared analytics core + bundled web UI, no RAGFlow.
FROM node:20-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web ./
RUN npm run build

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    KNOWFLOW_OSS_WEB_DIST=/app/web/dist \
    KNOWFLOW_OSS_HOST=0.0.0.0 \
    KNOWFLOW_OSS_DATA_DIR=/data

WORKDIR /app

RUN groupadd --system analytics \
    && useradd --system --gid analytics --home-dir /nonexistent analytics \
    && mkdir -p /data && chown analytics:analytics /data

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --no-cache-dir .
COPY --from=web /web/dist ./web/dist

USER analytics
VOLUME ["/data"]
EXPOSE 9395

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9395/health', timeout=3)"]

CMD ["knowflow-analytics-oss"]
