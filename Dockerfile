FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml /app/pyproject.toml
COPY requirements.txt /app/requirements.txt
COPY README.md /app/README.md
COPY src /app/src
COPY config /app/config

RUN pip install --no-cache-dir -e .

CMD ["python", "-m", "sise_ws", "scheduler"]

