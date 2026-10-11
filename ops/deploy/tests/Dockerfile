FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git sudo util-linux procps \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir 'Django>=5.2.8,<5.3'
COPY requirements.txt /tmp/permission-requirements.txt
RUN python -m venv /opt/permission-runtime \
    && /opt/permission-runtime/bin/python -m pip install --no-cache-dir -r /tmp/permission-requirements.txt
WORKDIR /source
ENTRYPOINT ["python", "-B", "/source/ops/deploy/tests/test_permissions.py"]
