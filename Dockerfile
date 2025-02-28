FROM python:3.13.2-alpine3.21

RUN apk add --no-cache gcc musl-dev libffi-dev ffmpeg
RUN pip install uv
WORKDIR /app

ADD pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-dev
ADD . .

CMD uv run main.py
