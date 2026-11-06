FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py data-generation.py ./
COPY templates/ ./templates/

RUN mkdir -p /app/data && \
    useradd --create-home appuser

USER appuser

EXPOSE 9000

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:?PORT must be set} --workers 2 app:app"]