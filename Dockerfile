FROM python:3.12-alpine

WORKDIR /app
COPY app.py templates/index.html data /app/

ENV PORT=9000

EXPOSE 9000

CMD ["python", "/app/app.py"]
