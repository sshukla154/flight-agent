# syntax=docker/dockerfile:1
#
# Phase 8: CLI-only image. The FastAPI service (src/flightagent/api/) is
# deliberately NOT containerized here -- it hardcodes DEFAULT_HOST =
# "127.0.0.1" and ships with no authentication (see its own module
# docstring); wrapping it in a container with a port mapping would mean
# overriding that safety choice, which is out of scope for packaging and
# belongs to a future phase that also adds real auth.
#
# See src/flightagent/config/loader.py::_find_repo_root: it walks UPWARD
# from wherever this package is installed until it finds a pyproject.toml,
# then reads config/defaults.toml as that file's sibling -- a plain
# filesystem read at IMPORT TIME, not a packaged resource. Both
# pyproject.toml and config/ MUST be present in the runtime image at the
# level this walk terminates, or every invocation crashes with ConfigError
# before argument parsing even happens.

########################################################################
# Stage 1: builder -- installs uv, resolves+builds the venv only here.
########################################################################
FROM python:3.12-slim AS builder

# Pinned to the exact uv version verified locally this session, not
# "latest" -- reproducible builds shouldn't drift with an upstream release.
RUN pip install --no-cache-dir uv==0.12.4

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependency layer: copied and synced BEFORE any source, so an app-code
# change never invalidates this (expensive) layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Project layer: --no-editable installs a real wheel into .venv's
# site-packages, so the runtime stage never needs to carry src/ at all --
# only .venv, pyproject.toml, and config/ (see the loader.py note above).
COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev --no-editable

########################################################################
# Stage 2: runtime -- no build toolchain, no uv, non-root user.
########################################################################
FROM python:3.12-slim AS runtime

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/pyproject.toml /app/pyproject.toml
COPY --from=builder /app/config /app/config

ENV PATH="/app/.venv/bin:${PATH}"

RUN groupadd --gid 10001 flightagent \
    && useradd --uid 10001 --gid flightagent --no-create-home \
       --shell /usr/sbin/nologin flightagent \
    && mkdir -p /data \
    && chown -R flightagent:flightagent /data

# /app is baked-in code (root-owned); /data is the ONLY writable
# directory and becomes the runtime CWD, so the app's existing
# relative-path config (out/, data/runs, cache/flightagent.sqlite3)
# resolves under /data/... with zero code changes.
WORKDIR /data
USER flightagent

ENTRYPOINT ["flightagent"]
# No --all flag exists on `flightagent run` -- this is the real flag
# combination that produces the full 10-origin x 8-destination x 2-mode
# fan-out (see docker-compose.yml's own comment).
CMD ["run", "--origin", "AMS", "--date", "2027-07-17", "--max-stops", "0", \
     "--all-destinations", "--all-origins"]
