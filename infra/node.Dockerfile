FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 USVISA_DATA_DIR=/data \
    CHROME_BINARY=/usr/bin/chromium CHROMEDRIVER_PATH=/usr/bin/chromedriver
RUN apt-get update && apt-get install -y --no-install-recommends chromium chromium-driver fonts-liberation ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements-node.lock ./
RUN pip install --no-cache-dir -r requirements-node.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home visa && mkdir /data && chown visa /data
USER visa
CMD ["usvisa-node"]
