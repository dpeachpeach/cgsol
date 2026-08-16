# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm@sha256:bb3a5d38989ec658710f06b08bc23cb78d079eb852405e42b124fdf430281454

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.5.11@sha256:7e479fa39802632c25b4e5c14ddfab9c5f443cd7c89626a0408d31a0b7afc193 /uv /usr/local/bin/uv

# Locked, so the image an evaluator builds next month is the one that worked.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY orchestrator ./orchestrator
COPY devin ./devin
COPY seed ./seed
COPY fixtures ./fixtures
RUN uv sync --frozen --no-dev

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=12 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "8000"]
