# Образ приложения «Букинист» для центрального сервера.
# Python 3.13 (совместим с зависимостями; на VPS свежий Python не обязателен).
FROM python:3.13-slim

# Системные библиотеки для psycopg2 (PostgreSQL) и сборки колёс.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости — слой кэшируется, пока requirements не менялись.
COPY requirements.txt requirements-prod.txt ./
RUN pip install --no-cache-dir -r requirements-prod.txt

# Затем код приложения.
COPY app ./app
COPY run.py ./

# Не пишем .pyc и не буферизуем логи — удобнее смотреть вывод контейнера.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Прод-запуск: без reload, один рабочий процесс (иначе фоновый опрос заказов
# задваивался бы). uvicorn слушает внутри сети docker, наружу отдаёт Caddy по HTTPS.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
