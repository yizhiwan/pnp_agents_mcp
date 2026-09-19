# =============================================================================
# Model gateway image — LiteLLM proxy behind a validating entrypoint.
#
# Multi-stage so the runtime layer carries no compiler toolchain. Runs as a
# non-root user and works with a read-only root filesystem: everything mutable
# (the filtered config, caches) lives under /tmp, which compose mounts as tmpfs.
#
# Nothing station-specific is baked in: no keys, no ports beyond a default, no
# absolute host paths. All build inputs are ARGs.
# =============================================================================
ARG PYTHON_IMAGE=python:3.12-slim

# --- builder -----------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

ARG LITELLM_VERSION=1.63.14
ARG PYYAML_VERSION=6.0.2

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential is needed by a few LiteLLM transitive wheels on arm64.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends build-essential; \
    rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

RUN set -eux; \
    pip install --upgrade pip; \
    pip install "litellm[proxy]==${LITELLM_VERSION}" "PyYAML==${PYYAML_VERSION}"

# --- runtime -----------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

ARG APP_UID=10001
ARG APP_GID=10001
ARG APP_USER=gateway

LABEL org.opencontainers.image.title="pnp-agents-mcp gateway" \
      org.opencontainers.image.description="LiteLLM model gateway for the Agent Pack" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    GATEWAY_PORT=4000 \
    GATEWAY_BIND_ADDR=0.0.0.0 \
    GATEWAY_RUNTIME_CONFIG=/tmp/litellm.runtime.yaml

COPY --from=builder /opt/venv /opt/venv

RUN set -eux; \
    groupadd --gid "${APP_GID}" "${APP_USER}"; \
    useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home \
            --home-dir /tmp --shell /usr/sbin/nologin "${APP_USER}"

WORKDIR /app

# Build context is ./gateway, so this is litellm_config.yaml + start.sh.
# start.sh prefers /pack/gateway/litellm_config.yaml (the read-only pack mount)
# when present, and falls back to this baked copy for standalone `docker run`.
COPY --chown=${APP_UID}:${APP_GID} . /app
RUN chmod 0555 /app/start.sh

USER ${APP_UID}:${APP_GID}

EXPOSE 4000

# No curl in the slim image; urllib is already present and needs no extra layer.
HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=5 \
  CMD ["python", "-c", "import os,sys,urllib.request;\
port=os.environ.get('GATEWAY_PORT','4000');\
url=f'http://127.0.0.1:{port}/health/liveliness';\
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status == 200 else 1)"]

ENTRYPOINT ["/app/start.sh"]
