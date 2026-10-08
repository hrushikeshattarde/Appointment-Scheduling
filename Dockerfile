# The appointments board and its agent loops (`facility-profiles serve`), for one server or Fargate.
#
#   docker build -t pickup-booking .
#
# Settings come from the environment (FP_*, TPRO_*; deploy/README.md), the store lives in /data,
# and deploy/serve.sh turns the loop settings into `serve` options.

FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --extra api --extra aws --extra pdf
COPY src ./src
RUN uv sync --frozen --no-dev --extra api --extra aws --extra pdf

FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown app /data
COPY --from=build /app /app
COPY --chmod=755 deploy/serve.sh /usr/local/bin/serve.sh
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    TZ=America/New_York \
    FP_DATABASE_URL=sqlite:////data/board.db \
    FP_BOOKING_DRAFTS_DIR=/data/drafts
USER app
WORKDIR /app
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/health', timeout=5)"
ENTRYPOINT ["/usr/local/bin/serve.sh"]
