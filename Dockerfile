# 1. Name the stage "requirements-stage"
FROM python:3.11-slim-bookworm AS requirements-stage

WORKDIR /tmp

# 2. Install Poetry AND the export plugin
RUN pip install poetry poetry-plugin-export

COPY pyproject.toml poetry.lock ./

# 3. Export requirements to /tmp/requirements.txt
RUN poetry export -f requirements.txt --output requirements.txt --without-hashes

# --- Final Stage ---
FROM python:3.11-slim-bookworm

WORKDIR /app

# 1. Install System Tools (OpenCV dependencies)
# Note: I removed 'chromium' and 'chromium-driver' because Playwright manages its own.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx \
    libglib2.0-0 \
    # We remove the manual library installs (libnss3 etc) because 
    # 'playwright install-deps' will handle them automatically below.
    && rm -rf /var/lib/apt/lists/*

# 2. Copy requirements
COPY --from=requirements-stage /tmp/requirements.txt /app/requirements.txt

# 3. Install Python dependencies (including playwright)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

# 4. INSTALL PLAYWRIGHT BROWSERS & SYSTEM DEPS
# This is the critical missing step.
# 'install-deps' installs the OS libraries (like libasound2, libgtk, etc.) needed by the browser.
RUN playwright install chromium && playwright install-deps

# Copy application code
COPY . /app

EXPOSE 8002

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]
