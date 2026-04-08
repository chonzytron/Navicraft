FROM python:3.12-slim

WORKDIR /app

# Install system dependencies and Python packages
# --pre is required because essentia-tensorflow uses pre-release versioning
COPY backend/requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --pre -r requirements.txt

# Verify essentia actually imports (catches missing shared libs on slim images)
RUN python -c "import essentia; import essentia.standard; print('essentia-tensorflow installed successfully')"

# Copy app
COPY backend/ /app/
COPY frontend/ /app/frontend/

# Create data directory
RUN mkdir -p /data

EXPOSE 8085

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8085/api/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8085"]
