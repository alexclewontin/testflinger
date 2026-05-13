#!/bin/bash
set -eu

CONF="$SNAP_DATA/testflinger-agent.conf"
QEMU_CONFIG="$SNAP_DATA/qemu-device.yaml"
USER_DATA="$SNAP_DATA/cloud-init-user-data.yaml"
META_DATA="$SNAP_DATA/cloud-init-meta-data.yaml"
TOKEN_FILE="$SNAP_DATA/agent_token.json"
RUNTIME_DIR=/tmp/microtestflinger

config_get() {
    "$SNAP/bin/python3" - "$QEMU_CONFIG" "$1" <<'PY'
import sys, yaml
with open(sys.argv[1], encoding='utf-8') as f:
    config = yaml.safe_load(f) or {}
print(config.get(sys.argv[2]) or "")
PY
}

# Copy defaults to $SNAP_DATA on first run — edit files there to customise
if [ ! -f "$CONF" ]; then
    cp "$SNAP/etc/microtestflinger/testflinger-agent.conf" "$CONF"
fi
if [ ! -f "$QEMU_CONFIG" ]; then
    cp "$SNAP/etc/microtestflinger/qemu-device.yaml" "$QEMU_CONFIG"
fi
if [ ! -f "$USER_DATA" ]; then
    cp "$SNAP/etc/microtestflinger/cloud-init-user-data.yaml" "$USER_DATA"
fi
if [ ! -f "$META_DATA" ]; then
    cp "$SNAP/etc/microtestflinger/cloud-init-meta-data.yaml" "$META_DATA"
fi

# Create runtime dirs; pre-create image cache dir
mkdir -p "$RUNTIME_DIR" "$RUNTIME_DIR/run" "$RUNTIME_DIR/logs" "$RUNTIME_DIR/results"
mkdir -p "$(config_get qemu_image_dir)"

# Expose configs to the connector via /tmp (symlinks into $SNAP_DATA)
ln -sf "$QEMU_CONFIG" "$RUNTIME_DIR/qemu-device.yaml"
ln -sf "$SNAP/etc/microtestflinger/fake-device.yaml" "$RUNTIME_DIR/fake-device.yaml"

# Bootstrap agent credentials (idempotent)
"$SNAP/bin/python3" "$SNAP/bin/bootstrap-auth.py"

exec "$SNAP/bin/testflinger-agent" -c "$CONF" --token-file "$TOKEN_FILE"
