#!/usr/bin/env python3
"""Bootstrap agent credentials in MongoDB for local dev use.

Run once before starting the agent. Idempotent.
"""

import json
import os
import sys
from pathlib import Path

import bcrypt
import pymongo
import requests

SNAP_DATA = os.environ.get("SNAP_DATA", "/var/snap/microtestflinger/current")
SERVER_URL = "http://127.0.0.1:5000"
MONGO_URL = "mongodb://localhost:27017"
DB_NAME = os.environ.get("MONGODB_DATABASE", "microtestflinger_db")
AGENT_CLIENT_ID = "microtestflinger-agent"
AGENT_CLIENT_KEY = "microtestflinger-dev-secret"
TOKEN_FILE = Path(SNAP_DATA) / "agent_token.json"


def seed_client(db):
    """Insert agent client credentials into MongoDB."""
    hashed = bcrypt.hashpw(
        AGENT_CLIENT_KEY.encode(), bcrypt.gensalt()
    ).decode()
    permissions = {
        "client_id": AGENT_CLIENT_ID,
        "client_secret_hash": hashed,
        "role": "agent",
        "allowed_queues": ["*"],
        "max_priority": {"*": 100},
        "max_reservation_time": {"*": 86400},
    }
    db.client_permissions.update_one(
        {"client_id": AGENT_CLIENT_ID},
        {"$set": permissions},
        upsert=True,
    )
    print(f"[auth] Seeded client: {AGENT_CLIENT_ID}")


def get_refresh_token():
    """Exchange client credentials for a refresh token."""
    resp = requests.post(
        f"{SERVER_URL}/v1/oauth2/token",
        auth=(AGENT_CLIENT_ID, AGENT_CLIENT_KEY),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def main():
    client = pymongo.MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    db = client[DB_NAME]

    seed_client(db)

    if TOKEN_FILE.exists():
        print(f"[auth] Token file already exists: {TOKEN_FILE}")
        return 0

    print("[auth] Requesting refresh token from server...")
    try:
        tokens = get_refresh_token()
    except Exception as exc:
        print(f"[auth] Failed to get refresh token: {exc}", file=sys.stderr)
        return 1

    TOKEN_FILE.write_text(json.dumps({"refresh_token": tokens["refresh_token"]}))
    print(f"[auth] Token written to {TOKEN_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
