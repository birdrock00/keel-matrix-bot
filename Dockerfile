FROM python:3.13-slim

# Build-time version argument.
ARG BUILD_VERSION=0.0.0-dev

# Install pinned runtime dependencies.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the bot script
COPY keel_matrix_bot.py /app/keel_matrix_bot.py

WORKDIR /app
RUN mkdir -p /mem

# Ensure Python output is unbuffered for real-time logging
ENV PYTHONUNBUFFERED=1
ENV KEEL_MATRIX_BOT_STATE_FILE=/mem/keel_matrix_bot_state.json
ENV KEEL_MATRIX_BOT_HTTP_HOST=0.0.0.0
ENV KEEL_MATRIX_BOT_HTTP_PORT=8080

# Propagate build version so the bot can report it at runtime
ENV KEEL_MATRIX_BOT_VERSION=${BUILD_VERSION}

EXPOSE 8080
VOLUME ["/mem"]

# Run the bot; runtime config should come from env/args at deploy time.
CMD ["python", "/app/keel_matrix_bot.py", "--listen"]
