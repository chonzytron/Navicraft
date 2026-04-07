FROM python:3.12-slim

WORKDIR /app

# Install system dependencies: curl for health check, libs for essentia audio analysis
COPY backend/requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl libfftw3-3 libavcodec60 libavformat60 libavutil58 \
        libswresample4 libsamplerate0 libyaml-0-2 libtag1v5 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

# Copy app
COPY backend/ /app/
COPY frontend/ /app/frontend/

# Create data directory
RUN mkdir -p /data

EXPOSE 8085

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8085/api/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8085"]
