# Base image with a pinned Python version.
FROM python:3.11-slim

# Application code lives in /app inside the container.
WORKDIR /app

# Install dependencies first so the Docker layer cache is reused
# when only application code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application source after dependencies are installed.
COPY app/ app/

# Create an unprivileged user and switch to it for runtime security.
RUN groupadd --system appuser \
    && useradd --system --gid appuser --home-directory /app --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

# Entrypoint: start the bot (mode is selected by USE_MEMORY from the environment).
CMD ["python", "-m", "app.main"]
