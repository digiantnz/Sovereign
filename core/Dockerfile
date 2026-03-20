FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY sovereign-a2a /sovereign-a2a
RUN pip install --no-cache-dir /sovereign-a2a

COPY core/app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Source code is NOT baked into the image.
# /app is volume-mounted from RAID at runtime (see compose.yml sovereign-core volumes).
# Only rebuild when requirements.txt or sovereign-a2a changes.
# sovereign user uid=999 matches existing RAID audit/security file ownership.

RUN useradd -r -u 999 -s /bin/false sovereign
USER sovereign

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
