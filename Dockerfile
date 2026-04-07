FROM python:3.11-slim

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot source
COPY bot.py .

# Hugging Face Spaces runs as non-root
RUN useradd -m botuser && chown -R botuser /app
USER botuser

# HF Spaces expects port 7860
EXPOSE 7860

CMD ["python", "-u", "bot.py"]
