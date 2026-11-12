# syntax=docker/dockerfile:1
# Multi-stage build: the builder compiles a wheel, the runtime stage only installs it,
# so the final image carries no build tooling. Base tag is pinned for reproducible builds.

# ---- Builder stage: build the project wheel ----
FROM python:3.12.16-slim-bookworm AS builder
WORKDIR /build
# Copy only what the build needs (.dockerignore removes the rest).
COPY pyproject.toml README.md LICENSE NOTICE THIRD_PARTY_NOTICES.md ./
COPY src ./src
# --no-deps: build only our own wheel; dependencies are resolved in the runtime stage.
RUN pip wheel . --no-deps --wheel-dir /wheels

# ---- Runtime stage ----
FROM python:3.12.16-slim-bookworm AS runtime
# No .pyc clutter, unbuffered logs so `docker logs` shows output immediately.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Non-root user with a fixed uid (stable file ownership across hosts and volumes).
# The data dir is created and chowned BEFORE `USER`, so a named volume mounted here
# inherits the collector:collector ownership on first use (Spool and PID file writes).
RUN groupadd --gid 10001 collector \
    && useradd --uid 10001 --gid collector --no-create-home --shell /usr/sbin/nologin collector \
    && mkdir -p /var/lib/plc-data-collector \
    && chown collector:collector /var/lib/plc-data-collector

# Install the wheel straight from the builder stage (bind mount, so the wheel is not
# stored in a runtime layer). constraints.txt pins every transitive dependency, so the
# image resolves the same versions as CI and THIRD_PARTY_NOTICES.md.
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels \
    --mount=type=bind,source=constraints.txt,target=/constraints.txt \
    pip install -c /constraints.txt /wheels/*.whl

WORKDIR /app
ENV PLC_DATA_COLLECTOR_DATA_DIR=/var/lib/plc-data-collector
USER collector
VOLUME ["/var/lib/plc-data-collector"]
EXPOSE 8080

# Liveness only: /health/live is 200 whenever the API process is serving.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health/live').status==200 else 1)"]

CMD ["plc-data-collector", "--run-service"]
