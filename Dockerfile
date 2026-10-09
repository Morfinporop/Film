# Образ для bothost: приложение в /app, порт 3000, БД в /app/data/film.db
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=3000 \
    DB_PATH=/app/data/film.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py index.html ./

# Папка данных принадлежит непривилегированному пользователю
RUN useradd -m app && mkdir -p /app/data && chown -R app /app
USER app

EXPOSE 3000
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '3000'), timeout=4)"]

CMD ["python", "main.py"]
