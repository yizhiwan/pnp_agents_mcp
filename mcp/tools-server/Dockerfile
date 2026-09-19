# =============================================================================
# MCP tools server image.
#
# Multi-stage: the runtime layer has no compiler and no pip cache. Runs as a
# non-root user under a read-only root filesystem — the process writes nothing
# outside /tmp (mounted tmpfs by compose).
#
# The same image serves both transports. compose runs it with MCP_TRANSPORT=http;
# `docker run -i -e MCP_TRANSPORT=stdio ...` speaks stdio on the container's
# stdin/stdout. Nothing station-specific is baked in.
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

# Copy only the requirements first so dependency layers cache independently of
# source edits.
COPY requirements.txt /tmp/requirements.txt
RUN set -eux; \
    pip install --upgrade pip; \
    pip install -r /tmp/requirements.txt

# --- runtime -----------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

ARG APP_UID=10002
ARG APP_GID=10002
ARG APP_USER=mcpsrv

LABEL org.opencontainers.image.title="pnp-agents-mcp tools-server" \
      org.opencontainers.image.description="Transport-agnostic MCP tools server (search_web, get_weather, run_shell_safe)" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    MCP_SERVER_NAME=tools-server \
    MCP_TRANSPORT=http \
    MCP_BIND_HOST=0.0.0.0 \
    MCP_PORT=8081 \
    MCP_MOUNT_PATH=/mcp \
    LOG_LEVEL=info \
    LOG_FORMAT=json

COPY --from=builder /opt/venv /opt/venv

# coreutils is already present in the base image and supplies the binaries the
# default SHELL_ALLOWLIST names (echo, ls, cat, pwd, date, uname, df).
RUN set -eux; \
    groupadd --gid "${APP_GID}" "${APP_USER}"; \
    useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home \
            --home-dir /tmp --shell /usr/sbin/nologin "${APP_USER}"

WORKDIR /app
COPY --chown=${APP_UID}:${APP_GID} mcp_server.py /app/mcp_server.py

USER ${APP_UID}:${APP_GID}

EXPOSE 8081

# No curl in slim; urllib is built in. Only meaningful for MCP_TRANSPORT=http —
# in stdio mode there is no listener and compose does not run this service.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD ["python", "-c", "import os,sys,urllib.request;\
port=os.environ.get('MCP_PORT','8081');\
url=f'http://127.0.0.1:{port}/health';\
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status == 200 else 1)"]

ENTRYPOINT ["python", "-u", "/app/mcp_server.py"]
