FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 USVISA_DATA_DIR=/data
WORKDIR /app
COPY requirements-coordinator.lock ./
RUN pip install --no-cache-dir -r requirements-coordinator.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home visa && mkdir /data && chown visa /data
USER visa
CMD ["usvisa-coordinator", "serve", "--host", "0.0.0.0", "--port", "8000"]
