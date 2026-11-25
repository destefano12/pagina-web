# syntax=docker/dockerfile:1
# API del motor 3D: liviana, sin herramientas de reconstrucción.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
COPY docker/requirements-api.txt docker/bloqueo-api.txt /tmp/
RUN --mount=type=secret,id=ca,required=false if [ -s /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi \
 && pip install --no-cache-dir -r /tmp/requirements-api.txt -c /tmp/bloqueo-api.txt && useradd -r -u 10001 motor
COPY app/api /app/api
COPY app/comun /app/comun
COPY sql /sql
USER motor
WORKDIR /app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/salud')"
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
