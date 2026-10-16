FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd -m zara
WORKDIR /srv/zara
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY core ./core
COPY device ./device
USER zara
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/v1/health',timeout=4)"
# OCI Always Free posture: single worker (no forked RAM copies), bounded
# concurrency/timeouts. Override via `docker run ... CMD` only if measured
# load proves it necessary — never preemptively.
CMD ["python", "-m", "uvicorn", "core.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--limit-concurrency", "64", "--timeout-keep-alive", "15", "--limit-max-requests", "2000"]
