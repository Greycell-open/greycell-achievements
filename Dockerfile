# Greycell Achievements sync server: one process, one SQLite file in /data.
#
# Reproducible: the base image is pinned by digest (python:3.12-slim, Python
# 3.12.14) and every Python package is installed from a lock file with hashes.
# To move either, run scripts/lock.sh after changing the digest here.
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
WORKDIR /app
COPY requirements ./requirements
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements/server.lock
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps --no-build-isolation .

# The full test suite, in exactly this environment:
#   docker build --target test .
# The build fails if any test fails.
FROM base AS test
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements/dev.lock
COPY examples ./examples
COPY tests ./tests
RUN python -m pytest -q -p no:warnings -p no:cacheprovider tests

# greycell.app's community stats (community_server.py): anonymous counts in,
# one snapshot a day out. No access log, so no addresses are written anywhere.
#   docker build --target community -t greycell-community .
FROM base AS community
ENV COMMUNITY_DB=/data/community.sqlite
RUN useradd --system --uid 10002 --home /data community && mkdir -p /data && chown community /data
USER community
VOLUME ["/data"]
EXPOSE 8794
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if b'ok' in urllib.request.urlopen('http://127.0.0.1:8794/api/achievements/healthz').read() else 1)"
CMD ["python", "-m", "uvicorn", "openachievements.community_server:app", "--host", "0.0.0.0", "--port", "8794", "--no-access-log"]

# The server. Last, so a plain `docker build .` or `docker compose up` builds it.
FROM base AS server
ENV OA_DATA_DIR=/data OA_PORT=8787
RUN useradd --system --uid 10001 --home /data oa && mkdir -p /data && chown oa /data
USER oa
VOLUME ["/data"]
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz').read()==b'ok' else 1)"
CMD ["openachievements-server"]
