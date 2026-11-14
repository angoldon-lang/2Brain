FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends pst-utils && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY static ./static
RUN pip install --no-cache-dir -e .
RUN useradd -m app && mkdir /data && chown app /data
USER app
CMD ["secondbrain", "serve", "-c", "/app/config.docker.toml"]
