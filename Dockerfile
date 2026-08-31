FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system analytics \
    && useradd --system --gid analytics --home-dir /nonexistent analytics

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install --no-cache-dir .

USER analytics

EXPOSE 9395

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9395/health', timeout=3)"]

CMD ["uvicorn", "knowflow_analytics.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "9395"]
