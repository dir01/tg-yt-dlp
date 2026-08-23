# uv ships as a static binary; copying it from its own image keeps the version
# pinned and skips a pip install on every build.
FROM ghcr.io/astral-sh/uv:0.12.5 AS uv

FROM python:3.14.7-alpine3.24 AS builder

# The toolchain is only needed to build wheels that have no musl release. It
# stays in this stage and never reaches the image that ships.
RUN apk add --no-cache gcc musl-dev libffi-dev
COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev


FROM python:3.14.7-alpine3.24

RUN apk add --no-cache ffmpeg

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY main.py telemetry.py ./

# Stamped by the release workflow; exported as tg_yt_dlp.build.timestamp so a
# stale image can be alerted on.
ARG BUILD_TIMESTAMP=0
ENV BUILD_TIMESTAMP=$BUILD_TIMESTAMP \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# TMP_DIR defaults to /app/tmp, so it has to exist and be writable by the
# unprivileged user the container runs as.
RUN adduser -D -u 10001 app && mkdir -p /app/tmp && chown -R app:app /app
USER app

EXPOSE 9100

CMD ["python", "main.py"]
