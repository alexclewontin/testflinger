#!/bin/bash
set -e

SNAP_DATA="${SNAP_DATA:-/var/snap/microtestflinger/current}"
mkdir -p "$SNAP_DATA/mongodb"

exec "$SNAP/bin/mongod" \
    --dbpath "$SNAP_DATA/mongodb" \
    --bind_ip 127.0.0.1 \
    --port 27017 \
    --logpath "$SNAP_DATA/mongod.log" \
    --logappend
