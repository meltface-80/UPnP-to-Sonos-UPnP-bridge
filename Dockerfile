FROM python:3.12-slim

LABEL org.opencontainers.image.title="Sonos UPnP Bridge for Audirvana" \
      org.opencontainers.image.description="Presents Sonos players as standard UPnP/DLNA MediaRenderers" \
      org.opencontainers.image.source="https://github.com/meltface-80/UPnP-to-Sonos-UPnP-bridge-for-Audirvana-" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HTTP_PORT=1500

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY sonosbridge ./sonosbridge

RUN useradd --system --uid 10001 --no-create-home bridge
USER bridge

# Informational only: the bridge needs host networking for SSDP multicast, so
# published ports do not apply in normal use.
EXPOSE 1500/tcp
EXPOSE 1900/udp

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('HTTP_PORT','1500')+'/status.json', timeout=4).status==200 else 1)"

ENTRYPOINT ["python", "-m", "sonosbridge"]
