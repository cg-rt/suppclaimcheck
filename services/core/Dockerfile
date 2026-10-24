# core (core-api and core-worker; compose sets the worker's command and health check) image (P1.01). Build from the repo root: docker build -f services/core/Dockerfile .
# Base images are pinned by digest; Dependabot proposes updates (D-023).
FROM ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b AS uv

FROM python:3.14.8-slim-trixie@sha256:fbecd461fc72d21b2143c6eaba25143738079154a6d0ea3da3aec724f50eed4a AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first, from the lockfile only, so a code change doesn't reinstall them.
COPY pyproject.toml uv.lock ./
COPY libs/common/pyproject.toml libs/common/
COPY services/web/pyproject.toml services/web/
COPY services/core/pyproject.toml services/core/
COPY services/gateway/pyproject.toml services/gateway/
RUN uv sync --locked --no-dev --no-install-workspace --package scc-core
COPY libs/common libs/common
COPY services/core services/core
RUN uv sync --locked --no-dev --no-editable --package scc-core

FROM python:3.14.8-slim-trixie@sha256:fbecd461fc72d21b2143c6eaba25143738079154a6d0ea3da3aec724f50eed4a
# pip is not needed at run time, and its vendored copies of urllib3, msgpack and setuptools are what
# image scanners flag (P1.11), so the final image drops it.
RUN rm -rf /usr/local/lib/python3.14/site-packages/pip /usr/local/lib/python3.14/site-packages/pip-* \
    /usr/local/lib/python3.14/ensurepip /usr/local/bin/pip /usr/local/bin/pip3*
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin scc
COPY --from=build /app/.venv /app/.venv
COPY VERSION /app/VERSION
ARG GIT_SHA=unknown
ENV PATH=/app/.venv/bin:$PATH     PYTHONUNBUFFERED=1     PYTHONDONTWRITEBYTECODE=1     SCC_VERSION_FILE=/app/VERSION     SCC_GIT_SHA=$GIT_SHA
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3     CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]
CMD ["python", "-m", "scc_core"]
