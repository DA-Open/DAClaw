FROM ghcr.io/astral-sh/uv:0.11.6-python3.13-trixie@sha256:b3c543b6c4f23a5f2df22866bd7857e5d304b67a564f4feab6ac22044dde719b AS uv_source
FROM tianon/gosu:1.19-trixie@sha256:3b176695959c71e123eb390d427efc665eeb561b1540e82679c15e992006b8b9 AS gosu_source
FROM debian:13.4

# Disable Python stdout buffering to ensure logs are printed immediately
ENV PYTHONUNBUFFERED=1

# Store Playwright browsers outside the volume mount so the build-time
# install survives the /opt/data volume overlay at runtime.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/daclaw/.playwright

# Install system dependencies in one layer, clear APT cache
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential nodejs npm python3 ripgrep ffmpeg gcc python3-dev libffi-dev procps git && \
    rm -rf /var/lib/apt/lists/*

# Non-root user for runtime; UID can be overridden via DACLAW_UID at runtime
RUN useradd -u 10000 -m -d /opt/data daclaw

COPY --chmod=0755 --from=gosu_source /gosu /usr/local/bin/
COPY --chmod=0755 --from=uv_source /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/

COPY . /opt/daclaw
WORKDIR /opt/daclaw

# Install Node dependencies and Playwright as root (--with-deps needs apt)
RUN npm install --prefer-offline --no-audit && \
    npx playwright install --with-deps chromium --only-shell && \
    cd /opt/daclaw/scripts/whatsapp-bridge && \
    npm install --prefer-offline --no-audit && \
    npm cache clean --force

# Hand ownership to daclaw user, then install Python deps in a virtualenv
RUN chown -R daclaw:daclaw /opt/daclaw
USER daclaw

RUN uv venv && \
    uv pip install --no-cache-dir -e ".[all]"

USER root
RUN chmod +x /opt/daclaw/docker/entrypoint.sh

ENV DACLAW_HOME=/opt/data
VOLUME [ "/opt/data" ]
ENTRYPOINT [ "/opt/daclaw/docker/entrypoint.sh" ]
