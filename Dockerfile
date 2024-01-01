FROM python:3.12.1-alpine3.18

RUN apk add --no-cache gcc musl-dev libffi-dev ffmpeg
RUN pip3 install poetry

WORKDIR /app

ADD poetry.lock pyproject.toml /app/
RUN poetry install
ADD . .

CMD poetry run python3 main.py
