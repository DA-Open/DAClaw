FROM ghcr.io/astral-sh/uv:0.11.6-python3.13-trixie@sha256:b3c543b6c4f23a5f2df22866bd7857e5d304b67a564f4feab6ac22044dde719b AS uv_source
FROM debian:13.4

# Disable Python stdout buffering to ensure logs are printed immediately
ENV PYTHONUNBUFFERED=1

# Store Playwright browsers outside the volume mount so the build-time
# install survives the /opt/data volume overlay at runtime.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/daclaw/.playwright

# npm registry used at build time; override on slow networks, e.g.
#   docker build --build-arg NPM_REGISTRY=https://registry.npmmirror.com .
# Build args are visible in `docker history`: never pass a URL containing a token.
ARG NPM_REGISTRY=https://registry.npmjs.org
# The WhatsApp bridge pulls a dependency straight from GitHub at build time;
# pass --build-arg INSTALL_WHATSAPP_BRIDGE=false where GitHub is unreliable.
ARG INSTALL_WHATSAPP_BRIDGE=true

# Install system dependencies in one layer, clear APT cache.
# gosu comes from Debian so the build does not need Docker Hub.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential nodejs npm python3 ripgrep ffmpeg gcc python3-dev libffi-dev procps git gosu ca-certificates libodbc2 && \
    rm -rf /var/lib/apt/lists/*

# Non-root user for runtime; UID can be overridden via DACLAW_UID at runtime
RUN useradd -u 10000 -m -d /opt/data daclaw

COPY --chmod=0755 --from=uv_source /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/

# uv-managed Python 3.11 (the version DAClaw is tested on) lives inside the image.
# No uv cache: the daclaw user's HOME is /opt/data, the volume, so a cache there
# would only bloat the image and be hidden by the mount anyway.
ENV UV_PYTHON=3.11 \
    UV_PYTHON_INSTALL_DIR=/opt/daclaw/.python \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

WORKDIR /opt/daclaw
RUN chown daclaw:daclaw /opt/daclaw

# Python deps first, pinned by uv.lock, so code-only changes reuse this layer.
# requirements-extra.txt adds what uv.lock does not cover yet; it is installed
# with the lock as constraints so the final sync does not move anything back.
COPY --chown=daclaw:daclaw pyproject.toml uv.lock README.md ./
COPY --chown=daclaw:daclaw docker/requirements-extra.txt docker/
USER daclaw
RUN uv sync --frozen --extra all --no-install-project && \
    uv export --frozen --extra all --no-hashes --no-emit-project -o /tmp/lock-constraints.txt && \
    uv pip install -c /tmp/lock-constraints.txt -r docker/requirements-extra.txt && \
    rm /tmp/lock-constraints.txt
USER root

# Node dependencies and Playwright's browser, installed as root (--with-deps
# needs apt) from the manifests alone so code changes do not re-run this layer.
# - whatsapp-bridge's lockfile resolves a public GitHub dependency over git+ssh;
#   fetch it over HTTPS instead, since the image has no ssh client or GitHub key.
# - @askjo/camofox-browser's postinstall downloads the Camoufox browser (~3.8 GB
#   with its temp files) into root's cache; DAClaw talks to an external Camoufox
#   server (CAMOFOX_URL), so the download is removed in this same layer.
COPY --chown=daclaw:daclaw package.json package-lock.json ./
COPY --chown=daclaw:daclaw scripts/whatsapp-bridge/package.json scripts/whatsapp-bridge/package-lock.json scripts/whatsapp-bridge/
RUN git config --global url."https://github.com/".insteadOf "ssh://git@github.com/" && \
    git config --global --add url."https://github.com/".insteadOf "git@github.com:" && \
    npm install --registry "$NPM_REGISTRY" --prefer-offline --no-audit && \
    npx playwright install --with-deps chromium --only-shell && \
    if [ "$INSTALL_WHATSAPP_BRIDGE" = "true" ]; then \
        (cd scripts/whatsapp-bridge && npm install --registry "$NPM_REGISTRY" --prefer-offline --no-audit) && \
        chown -R daclaw:daclaw scripts/whatsapp-bridge/node_modules; \
    fi && \
    rm -rf /root/.cache/camoufox /tmp/camoufox-* && \
    npm cache clean --force && \
    chown -R daclaw:daclaw node_modules .playwright

COPY --chown=daclaw:daclaw . /opt/daclaw

# Build the web UI into daclaw_cli/web_dist, then remove web/: `daclaw dashboard`
# rebuilds the UI on every start whenever npm and web/package.json are both
# present, which would need network access at runtime.
RUN (cd web && npm install --registry "$NPM_REGISTRY" --no-audit && npm run build) && \
    rm -rf web && \
    npm cache clean --force && \
    chown -R daclaw:daclaw daclaw_cli/web_dist

# Install the project itself; --inexact keeps the requirements-extra.txt
# packages, which a plain (exact) sync would uninstall. pip check fails the
# build if the extras and the lock ever disagree again.
USER daclaw
RUN uv sync --frozen --extra all --inexact && \
    uv pip check

USER root
RUN chmod +x /opt/daclaw/docker/entrypoint.sh

ENV DACLAW_HOME=/opt/data
VOLUME [ "/opt/data" ]
# Web dashboard (`daclaw dashboard`, see docker-compose.yml)
EXPOSE 9119

# Record the source commit, e.g. --build-arg VCS_REF=$(git rev-parse HEAD);
# declared last so a new commit does not invalidate the layers above.
ARG VCS_REF=unknown
LABEL org.opencontainers.image.source="https://github.com/DA-Open/DAClaw" \
      org.opencontainers.image.revision="$VCS_REF"

ENTRYPOINT [ "/opt/daclaw/docker/entrypoint.sh" ]
