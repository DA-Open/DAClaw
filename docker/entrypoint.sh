#!/bin/bash
# Docker/Podman entrypoint: bootstrap config files into the mounted volume, then run daclaw.
set -e

DACLAW_HOME="${DACLAW_HOME:-/opt/data}"
INSTALL_DIR="/opt/daclaw"

# --- Privilege dropping via gosu ---
# When started as root (the default for Docker, or fakeroot in rootless Podman),
# optionally remap the daclaw user/group to match host-side ownership, fix volume
# permissions, then re-exec as daclaw.
if [ "$(id -u)" = "0" ]; then
    if [ -n "$DACLAW_UID" ] && [ "$DACLAW_UID" != "$(id -u daclaw)" ]; then
        echo "Changing daclaw UID to $DACLAW_UID"
        usermod -u "$DACLAW_UID" daclaw
    fi

    if [ -n "$DACLAW_GID" ] && [ "$DACLAW_GID" != "$(id -g daclaw)" ]; then
        echo "Changing daclaw GID to $DACLAW_GID"
        # -o allows non-unique GID (e.g. macOS GID 20 "staff" may already exist
        # as "dialout" in the Debian-based container image)
        groupmod -o -g "$DACLAW_GID" daclaw 2>/dev/null || true
    fi

    actual_daclaw_uid=$(id -u daclaw)
    if [ "$(stat -c %u "$DACLAW_HOME" 2>/dev/null)" != "$actual_daclaw_uid" ]; then
        echo "$DACLAW_HOME is not owned by $actual_daclaw_uid, fixing"
        # In rootless Podman the container's "root" is mapped to an unprivileged
        # host UID — chown will fail.  That's fine: the volume is already owned
        # by the mapped user on the host side.
        chown -R daclaw:daclaw "$DACLAW_HOME" 2>/dev/null || \
            echo "Warning: chown failed (rootless container?) — continuing anyway"
    fi

    echo "Dropping root privileges"
    exec gosu daclaw "$0" "$@"
fi

# --- Running as daclaw from here ---
source "${INSTALL_DIR}/.venv/bin/activate"

# Create essential directory structure.  Cache and platform directories
# (cache/images, cache/audio, platforms/whatsapp, etc.) are created on
# demand by the application — don't pre-create them here so new installs
# get the consolidated layout from get_daclaw_dir().
# The "home/" subdirectory is a per-profile HOME for subprocesses (git,
# ssh, gh, npm …).  Without it those tools write to /root which is
# ephemeral and shared across profiles.  See issue #4426.
mkdir -p "$DACLAW_HOME"/{cron,sessions,logs,hooks,memories,skills,skins,plans,workspace,home}

# .env
if [ ! -f "$DACLAW_HOME/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$DACLAW_HOME/.env"
    # API keys go in here; keep it private even if the mounted dir is not
    chmod 600 "$DACLAW_HOME/.env"
fi

# config.yaml
if [ ! -f "$DACLAW_HOME/config.yaml" ]; then
    cp "$INSTALL_DIR/cli-config.yaml.example" "$DACLAW_HOME/config.yaml"
fi

# SOUL.md
if [ ! -f "$DACLAW_HOME/SOUL.md" ]; then
    cp "$INSTALL_DIR/docker/SOUL.md" "$DACLAW_HOME/SOUL.md"
fi

# Sync bundled skills (manifest-based so user edits are preserved)
if [ -d "$INSTALL_DIR/skills" ]; then
    python3 "$INSTALL_DIR/tools/skills_sync.py"
fi

exec daclaw "$@"
