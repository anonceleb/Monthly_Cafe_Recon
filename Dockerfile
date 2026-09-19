FROM python:3.12-slim

# Cloud defaults: listen publicly, refuse to start without a password, mark the session cookie Secure.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy \
    RECON_HOST=0.0.0.0 RECON_REQUIRE_AUTH=1 RECON_COOKIE_SECURE=1 \
    PATH="/app/.venv/bin:$PATH"

RUN pip install --no-cache-dir uv && useradd --create-home --uid 10001 app
WORKDIR /app

# Dependencies first so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY recon ./recon
COPY config ./config
RUN uv sync --frozen --no-dev && chown -R app:app /app

USER app
EXPOSE 8790
# PORT is injected by the host (Render); RECON_PASSWORD, RECON_SECRET and DATABASE_URL must be supplied at runtime.
CMD ["python", "-m", "recon", "serve"]
