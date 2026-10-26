FROM python:3.13-slim

# Устанавливаем системные зависимости, необходимые для WeasyPrint (Cairo, Pango, GObject)
RUN apt-get update && apt-get install -y \
    libpango-1.0-0 \
    libharfbuzz0b \
    libpangoft2-1.0-0 \
    libffi-dev \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Запускаем FastAPI с автоматическим портом от Railway
CMD sh -c "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"
