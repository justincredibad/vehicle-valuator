FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

EXPOSE 8000

CMD ["gunicorn", "--workers", "1", "--threads", "1", "--timeout", "180", \
     "--bind", "0.0.0.0:8000", "webapp:app"]
