# duhview as a ReviewBench agent. Build from the repository root:
#   docker build --platform linux/amd64 -f integrations/reviewbench/Dockerfile -t duhview-reviewbench .
FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md LICENSE /opt/duhview/
COPY src /opt/duhview/src
RUN pip install --no-cache-dir /opt/duhview
ENTRYPOINT ["duhview", "reviewbench"]
