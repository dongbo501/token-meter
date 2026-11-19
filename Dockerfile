FROM python:3.13-alpine
LABEL org.opencontainers.image.title="Token Meter" \
      org.opencontainers.image.description="Weekly OpenAI-compatible API usage and budget dashboard" \
      org.opencontainers.image.source="https://github.com/dongbo501/token-meter"
RUN apk add --no-cache tzdata nodejs npm libstdc++ \
 && npm install --global @openai/codex@0.169.1 \
 && npm cache clean --force \
 && apk del npm \
 && addgroup -g 10001 meter \
 && adduser -D -H -u 10001 -G meter meter \
 && mkdir -p /data \
 && chown meter:meter /data
WORKDIR /app
COPY metering.py ledger.py worker.py server.py codex_runner.py ./
COPY static/ ./static/
ENV BIND=0.0.0.0 PORT=8080 DATA_DIR=/data PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER meter
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=4s --start-period=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2).close()"
CMD ["python","server.py"]
