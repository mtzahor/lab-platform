# syntax=docker/dockerfile:1.7

# The multi-architecture index is pinned. Dependabot proposes digest updates.
ARG PYTHON_IMAGE="python:3.11.16-slim-bookworm@sha256:2e32f7d302adc1c37428355c1e646897c0c53f4fd60b6a551245fb90ee129f91"

FROM ghcr.io/astral-sh/uv:0.12.5 AS uv
FROM ${PYTHON_IMAGE} AS build

COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /src

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/lab-platform

COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY apps ./apps
COPY packages ./packages

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime

ARG LAB_VERSION="0.0.0-dev"
ARG LAB_REVISION="unknown"
ARG LAB_BUILD_DATE="1970-01-01T00:00:00Z"
ARG LAB_RELEASE_CHANNEL="nightly"

LABEL org.opencontainers.image.title="Lab Platform Control Plane" \
      org.opencontainers.image.description="Open-source Lab Platform control plane and dashboard" \
      org.opencontainers.image.source="https://github.com/mtzahor/lab-platform" \
      org.opencontainers.image.documentation="https://github.com/mtzahor/lab-platform/tree/main/docs" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${LAB_VERSION}" \
      org.opencontainers.image.revision="${LAB_REVISION}" \
      org.opencontainers.image.created="${LAB_BUILD_DATE}"

ENV PATH="/opt/lab-platform/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LAB_BUILD_COMMIT="${LAB_REVISION}" \
    LAB_BUILD_DATE="${LAB_BUILD_DATE}" \
    LAB_RELEASE_CHANNEL="${LAB_RELEASE_CHANNEL}" \
    LAB_HEALTHCHECK_URL="http://127.0.0.1:8443/health/ready"

ADD --chmod=0644 \
    --checksum=sha256:0144068502a1eddd2a0280ede10ef607d1ec592ce819940991203941564e8e76 \
    https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    /usr/local/share/keyrings/postgres.gpg.asc

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && echo "deb [ signed-by=/usr/local/share/keyrings/postgres.gpg.asc ] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install --yes --no-install-recommends postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/* /etc/apt/sources.list.d/pgdg.list \
        /usr/local/share/keyrings/postgres.gpg.asc

RUN groupadd --gid 10001 lab-platform \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /var/lib/lab-platform \
        --shell /usr/sbin/nologin lab-platform \
    && install -d -o 10001 -g 10001 /etc/lab-platform /var/lib/lab-platform/artifacts \
        /var/lib/lab-platform/backups /run/lab-platform-demo \
    && /usr/local/bin/python -m pip uninstall --yes setuptools wheel pip \
    && rm -f /usr/local/bin/pip

COPY --from=build /opt/lab-platform /opt/lab-platform
COPY --chmod=0555 docker/healthcheck.py /usr/local/bin/lab-healthcheck
COPY --chmod=0555 docker/demo-bootstrap.py /usr/local/libexec/lab-platform/demo-bootstrap.py

USER 10001:10001
WORKDIR /var/lib/lab-platform
EXPOSE 8443

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=4 \
    CMD ["lab-healthcheck"]

ENTRYPOINT ["lab-control-plane"]
CMD ["serve", "--config", "/etc/lab-platform/control-plane.yaml"]
