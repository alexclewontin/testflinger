#!/bin/bash
set -eu

# Source server environment
if [ -f "$SNAP_DATA/server.env" ]; then
    set -a
    source "$SNAP_DATA/server.env"
    set +a
fi

# Defaults
export MONGODB_HOST="${MONGODB_HOST:-localhost}"
export MONGODB_DATABASE="${MONGODB_DATABASE:-microtestflinger_db}"
export JWT_SIGNING_KEY="${JWT_SIGNING_KEY:-microtestflinger-dev-key}"
export WEB_SECRET_KEY="${WEB_SECRET_KEY:-microtestflinger-web-key}"
export TESTFLINGER_SECRETS_MASTER_KEY="${TESTFLINGER_SECRETS_MASTER_KEY:-$(head -c 64 /dev/urandom | base64)}"

exec "$SNAP/bin/gunicorn" --bind 0.0.0.0:5000 "testflinger.application:create_flask_app()"
