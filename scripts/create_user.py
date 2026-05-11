#!/usr/bin/env python3
"""Provision or reset an account in the live users.json.

The server reads users.json from $DREAMINA_USERS_FILE if set, otherwise
from $DREAMINA_DATA_DIR/users.json (default $HOME/.dreamina-web/users.json).
This script writes to the same location, so an account created here is
visible to the next /api/login call without restarting the server.

Usage:
    python scripts/create_user.py <email> <password>
    python scripts/create_user.py <email> <password> --replace

Example:
    python scripts/create_user.py eugeneyeung01@gmail.com 12345678

Without --replace, refuses to overwrite an existing entry. With --replace,
re-hashes the password for an existing account (use this to reset a
forgotten password).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    from werkzeug.security import generate_password_hash
except ImportError:
    sys.exit("werkzeug is required: pip install werkzeug")


def _users_file() -> Path:
    explicit = os.environ.get("DREAMINA_USERS_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    data_env = os.environ.get("DREAMINA_DATA_DIR", "").strip()
    base = Path(data_env).expanduser().resolve() if data_env else (Path.home() / ".dreamina-web").resolve()
    return base / "users.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or reset a Reel Studio account.")
    parser.add_argument("email")
    parser.add_argument("password")
    parser.add_argument("--replace", action="store_true",
                        help="Overwrite an existing account's password instead of refusing")
    args = parser.parse_args()

    email = args.email.strip().lower()
    if "@" not in email:
        sys.exit(f"Not a valid email: {email!r}")
    if len(args.password) < 6:
        sys.exit("Password must be at least 6 characters")

    path = _users_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    users: dict = {}
    if path.exists():
        try:
            users = json.loads(path.read_text()) or {}
        except Exception as exc:
            sys.exit(f"Could not parse {path}: {exc}")

    existed = email in users
    if existed and not args.replace:
        sys.exit(f"Account {email} already exists. Pass --replace to reset its password.")

    users[email] = {
        "password_hash": generate_password_hash(args.password),
        "created_at": users.get(email, {}).get("created_at") or time.time(),
    }
    path.write_text(json.dumps(users, indent=2))
    action = "reset" if existed else "created"
    print(f"{action} {email} in {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
