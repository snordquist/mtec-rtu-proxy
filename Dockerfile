FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY mtec_rtu_proxy ./mtec_rtu_proxy
RUN pip install --no-cache-dir .

# Default listen port (override via LISTEN_PORT / .env)
EXPOSE 502

ENTRYPOINT ["python", "-m", "mtec_rtu_proxy"]
