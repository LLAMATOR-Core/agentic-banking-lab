FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY services ./services

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

ARG APP_MODULE
ENV APP_MODULE=${APP_MODULE}
CMD ["sh", "-c", "uvicorn ${APP_MODULE}:app --host 0.0.0.0 --port ${PORT:-8000}"]

