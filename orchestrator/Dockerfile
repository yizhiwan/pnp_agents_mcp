# =============================================================================
# Orchestrator image.
#
# Only the orchestrator's own modules are baked in. config_loader.py, the agent
# YAMLs, the prompts and config/ all arrive through the read-only /pack mount,
# so a configuration change is a container restart rather than a rebuild.
#
# Multi-stage, non-root, read-only-root-filesystem friendly.
# =============================================================================
ARG PYTHON_IMAGE=python:3.12-slim

# --- builder -----------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends build-essential; \
    rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY requirements.txt /tmp/requirements.txt
RUN set -eux; \
    pip install --upgrade pip; \
    pip install -r /tmp/requirements.txt

# --- runtime -----------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

ARG APP_UID=10003
ARG APP_GID=10003
ARG APP_USER=orch

LABEL org.opencontainers.image.title="pnp-agents-mcp orchestrator" \
      org.opencontainers.image.description="HTTP front door that runs configured agents against the gateway with MCP tools" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    PACK_DIR=/pack \
    STATION=local \
    ORCHESTRATOR_BIND_ADDR=0.0.0.0 \
    ORCHESTRATOR_PORT=8080 \
    LOG_LEVEL=info \
    LOG_FORMAT=json

COPY --from=builder /opt/venv /opt/venv

RUN set -eux; \
    groupadd --gid "${APP_GID}" "${APP_USER}"; \
    useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home \
            --home-dir /tmp --shell /usr/sbin/nologin "${APP_USER}"

WORKDIR /app
COPY --chown=${APP_UID}:${APP_GID} main.py mcp_bridge.py /app/

USER ${APP_UID}:${APP_GID}

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD ["python", "-c", "import os,sys,urllib.request;\
port=os.environ.get('ORCHESTRATOR_PORT','8080');\
url=f'http://127.0.0.1:{port}/health';\
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status == 200 else 1)"]

# uvicorn is launched through main() so the bind address and port come from
# configuration rather than being fixed in the image's CMD.
ENTRYPOINT ["python", "-u", "/app/main.py"]
