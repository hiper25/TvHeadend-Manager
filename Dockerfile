FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TVHMON_DATA_DIR=/data
WORKDIR /app
COPY app.py /app/app.py
COPY static /app/static
RUN useradd --system --uid 10001 --home /app tvh && mkdir -p /data && chown tvh:tvh /data
USER tvh
VOLUME ["/data"]
EXPOSE 8088
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import socket; s=socket.create_connection(('127.0.0.1',8088),2); s.close()" || exit 1
ENTRYPOINT ["python", "/app/app.py"]
