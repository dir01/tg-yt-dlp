FROM python:3.13.2-alpine3.21

RUN apk add --no-cache gcc musl-dev libffi-dev ffmpeg
RUN pip install uv
WORKDIR /app

ADD pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-dev
ADD . .

# Stamped by the release workflow; exported as tg_yt_dlp.build.timestamp so a
# stale image can be alerted on.
ARG BUILD_TIMESTAMP=0
ENV BUILD_TIMESTAMP=$BUILD_TIMESTAMP
ENV PYTHONUNBUFFERED=1

CMD ["/app/.venv/bin/python", "main.py"]
