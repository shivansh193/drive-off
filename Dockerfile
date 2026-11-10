FROM node:24-slim AS web
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ backend/
COPY docs/ docs/
COPY --from=web /web/dist frontend/dist
ENV DATA_DIR=/app/data PYTHONUNBUFFERED=1
CMD ["sh", "-c", "uvicorn driveoff.main:app --app-dir backend --host 0.0.0.0 --port ${PORT:-8000}"]
