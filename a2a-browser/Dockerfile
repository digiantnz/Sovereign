FROM python:3.12-slim

WORKDIR /app

# Install curl for healthcheck + system deps for playwright
RUN apt-get update && apt-get install -y curl \
    && rm -rf /var/lib/apt/lists/*

# Install playwright Python package first (needed to run playwright install)
# This layer is separate so requirements.txt changes don't re-download Chromium
RUN pip install --no-cache-dir "playwright>=1.49"

# Install Playwright Chromium to a fixed path accessible by non-root
# This is the slow layer (~400MB) — keep it stable
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium \
    && chmod -R o+rX /ms-playwright

# Install remaining app dependencies (changes here only re-run pip, not playwright)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./

RUN useradd -r -u 999 -m browser
USER browser

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
