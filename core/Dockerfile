FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY sovereign-a2a /sovereign-a2a
RUN pip install --no-cache-dir /sovereign-a2a

COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/. /app/

RUN useradd -r -s /bin/false sovereign && chown -R sovereign:sovereign /app
USER sovereign

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
