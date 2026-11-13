# ZEUS Terminal (宙斯期权终端) as a container. Published as ghcr.io/jackson-cmd/zeus-options-terminal.
#
#   docker run -d --name zeus -p 127.0.0.1:8765:8765 -v zeus-data:/data \
#     ghcr.io/jackson-cmd/zeus-options-terminal
#
# then open http://localhost:8765. Keep "127.0.0.1:" in -p: ZEUS has no login, so anyone who
# can reach the port can read your positions. Settings, trades and caches live in the /data
# volume; the code in /app is read-only for the app's user. Manual: docs/USER_GUIDE.md §1.7.
FROM python:3.12-slim

# Container-only behaviour. ZEUS_CONTAINER marks the image (Settings and the MCP endpoint show
# the docker commands); HOST makes the server listen on every interface inside the container,
# which Docker's port mapping needs; ZEUS_PORT pins the port inside the container, so a port
# changed in Settings cannot break the -p mapping. The daily jobs (8:30 AI summary, 9:00 market
# brief, …) follow TZ: pass -e TZ=<your zone> to run them on your own clock.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ZEUS_CONTAINER=1 \
    ZEUS_HOME=/data \
    ZEUS_PORT=8765 \
    HOST=0.0.0.0 \
    TZ=America/New_York

# tzdata makes TZ work; tini reaps the helper processes ZEUS starts (journal refresh, AI CLI shim)
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home --shell /usr/sbin/nologin zeus

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY LICENSE ./
COPY zeus ./zeus
# ZEUS keeps its runtime folders under zeus/ as links into $ZEUS_HOME/runtime and makes them at
# start; the app's user cannot write to /app, so they are made here, with the bytecode.
RUN python -c "import sys; sys.path.insert(0, 'zeus'); import zeus_config as c; c.relocate_runtime_dirs()" \
 && python -m compileall -q zeus \
 && chown -R zeus:zeus /data

USER zeus
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/version', timeout=4)"]
ENTRYPOINT ["tini", "--"]
CMD ["python", "zeus/options_analyzer/optionstrat_server.py"]
