FROM python:3.12-slim

# Install system deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends git curl && \
    rm -rf /var/lib/apt/lists/*

# Install nanobot from source + bridge server deps
RUN git clone --depth 1 https://github.com/HKUDS/nanobot.git /opt/nanobot && \
    pip install --no-cache-dir -e "/opt/nanobot[all]"

# Install bridge + skill runtime deps from requirements.txt
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Create app dir and workspace
RUN mkdir -p /app /workspace /memory /skills

COPY server.py /app/server.py

WORKDIR /workspace

# Nanobot config lives at /workspace/.nanobot/config.json (volume-mounted)
ENV NANOBOT_CONFIG=/workspace/.nanobot/config.json

# Bridge REST server — Rex talks here, nanobot executes tasks
EXPOSE 8080

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", "--app-dir", "/app", "--log-level", "info"]
