FROM python:3.12.0-alpine3.18

RUN pip3 install poetry

WORKDIR /app

ADD poetry.lock pyproject.toml /app/
RUN poetry install
RUN apk add --no-cache ffmpeg
ADD . .

CMD poetry run python3 main.py
