FROM python:3.11-slim
WORKDIR /app
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY iacpaas_mcp ./iacpaas_mcp
COPY fixtures ./fixtures
COPY .env.example ./
RUN useradd --uid 10001 --create-home atlas && mkdir -p /data/state /data/logs && chown -R atlas:atlas /data
USER atlas
ENV PYTHONUNBUFFERED=1 IACPAAS_DATA_DIR=/data/state IACPAAS_LOG_DIR=/data/logs IACPAAS_ENV_FILE=/data/state/.env
EXPOSE 8770
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8770/api/health', timeout=3)"
CMD ["python", "-m", "iacpaas_mcp", "serve", "--host", "0.0.0.0"]
