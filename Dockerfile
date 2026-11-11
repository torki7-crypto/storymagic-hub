FROM python:3.13-slim

# Системные библиотеки для WeasyPrint (Pango, HarfBuzz) и шрифты для печатных PDF
# (DejaVu Serif покрывает латиницу и кириллицу, Liberation Serif похож на Times)
RUN apt-get update && apt-get install -y \
    libpango-1.0-0 \
    libharfbuzz0b \
    libpangoft2-1.0-0 \
    libffi-dev \
    shared-mime-info \
    fontconfig \
    fonts-dejavu-core \
    fonts-liberation \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Запускаем FastAPI с автоматическим портом от Railway
CMD sh -c "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"
