FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY main.py ./
COPY family_safety_bot ./family_safety_bot

RUN pip install --no-cache-dir .

CMD ["python", "main.py"]
