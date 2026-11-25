FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project
COPY src ./src
RUN uv sync --frozen
ENV OUTBOX_HOST=0.0.0.0 OUTBOX_PORT=8025 OUTBOX_DATA_DIR=/data
VOLUME /data
EXPOSE 8025
CMD ["/app/.venv/bin/outbox"]
