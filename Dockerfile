FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY mtec_rtu_proxy ./mtec_rtu_proxy
RUN pip install --no-cache-dir .

# Default listen port (override via LISTEN_PORT / .env)
EXPOSE 502

# Detect a wedged-but-alive proxy (accepts connections but returns no Modbus reply)
# so the restart policy can recover it. Probes localhost with one FC03 read.
HEALTHCHECK --interval=30s --timeout=15s --start-period=15s --retries=3 \
    CMD ["python", "-m", "mtec_rtu_proxy.healthcheck"]

ENTRYPOINT ["python", "-m", "mtec_rtu_proxy"]
