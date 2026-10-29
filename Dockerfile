# Denial Recovery Agent - application image (app and RQ worker share it).
# The container never exposes a port on its own: docker-compose.yml publishes 8000 on 127.0.0.1 only.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

# WeasyPrint runtime libraries (PDF rendering of appeal letters) and a base font set.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libcairo2 libgdk-pixbuf-2.0-0 \
        shared-mime-info fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so the layer is cached while application code changes.
COPY pyproject.toml uv.lock README.md ./
RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev --extra pdf --no-install-project

# Application code, then the project itself (console script `denial-agent`).
COPY alembic.ini ./
COPY app ./app
COPY synthetic ./synthetic
RUN uv sync --frozen --no-dev --extra pdf

# Run as a non-root user; ./data is mounted here by docker-compose.yml.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin denial \
    && mkdir -p /app/data/exports \
    && chown -R denial:denial /app
USER denial

EXPOSE 8000
# Apply migrations, then serve. Reachable only through the compose port binding (127.0.0.1:8000).
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
