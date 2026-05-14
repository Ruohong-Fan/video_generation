#!/usr/bin/env python3
"""
Flask backend for the Jimeng video generation web UI.
Wraps the `dreamina` CLI and exposes a simple REST API.
Tasks are persisted to tasks.json so history survives server restarts.
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import subprocess
import threading
import time
import uuid
import zipfile
from functools import wraps
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from werkzeug.security import generate_password_hash, check_password_hash
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    request,
    send_file,
    send_from_directory,
    session,
    stream_with_context,
)
from flask_cors import CORS

app = Flask(__name__, static_folder="web")
# Flask session — persists across requests via a signed cookie. The secret key
# defaults to a per-process random value; set FLASK_SECRET_KEY in the env if
# you want sessions to survive restarts.
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,  # 30 days when "remember me"
)
CORS(app, supports_credentials=True)

# ── Auth ─────────────────────────────────────────────────────────────────────
# Invite-only — no public sign-up. Credentials come from env so they aren't
# checked into source. If unset, falls back to admin/reel for local dev.
LOGIN_EMAIL = os.environ.get("REEL_LOGIN_EMAIL", "admin@reel.local").strip().lower()
LOGIN_PASSWORD = os.environ.get("REEL_LOGIN_PASSWORD", "reel")


def is_authed() -> bool:
    return bool(session.get("user"))


def require_auth(view):
    """Page-level guard — redirects browsers to /login, returns 401 to API clients."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if is_authed():
            return view(*args, **kwargs)
        accept = request.headers.get("Accept", "")
        if request.path.startswith("/api/") or "application/json" in accept:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return redirect("/login")
    return wrapped


# Endpoints reachable without a session. Anything not in this set goes
# through the auth check in `_global_auth_gate` below.
PUBLIC_ENDPOINTS = {
    "login_page",      # GET  /login
    "login_submit",    # POST /api/login
    "signup_submit",   # POST /api/signup
    "logout",          # POST /api/logout
    "whoami",          # GET  /api/me
    "static",          # /static/*
    "i18n_js",         # GET  /i18n.js — needed by the login page itself
    # Media files use UUID-prefixed names and are loaded by <img>/<video>
    # tags that don't carry session cookies on cross-origin requests.
    # Auth-gating these would break previews; the file names act as a
    # weak capability token.
    "uploaded_file",   # /uploads/<filename>
}


@app.before_request
def _global_auth_gate():
    if request.method == "OPTIONS":
        return None  # let CORS preflights through
    endpoint = request.endpoint
    if endpoint is None or endpoint in PUBLIC_ENDPOINTS:
        return None
    if is_authed():
        return None
    accept = request.headers.get("Accept", "")
    if request.path.startswith("/api/") or "application/json" in accept:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return redirect("/login")


# Per-worker mtime cache for the JSON state files. With gunicorn / uwsgi
# running >1 worker, the `projects` / `users` / `tasks` dicts are loaded
# once per worker process at import time. A delete handled by worker A
# updates A's memory + disk, but B keeps the stale entry. The next list
# request routed to B by the load balancer happily serves the ghost
# project. _refresh_state_if_stale runs before every request, stat()s the
# state files, and reloads any whose mtime advanced — so any other
# worker's writes land in our memory before we serve the response.
_state_mtimes = {"projects": 0.0, "users": 0.0, "tasks": 0.0}


def _refresh_state_if_stale():
    for key, path, loader in (
        ("projects", PROJECTS_FILE, _load_projects),
        ("users",    USERS_FILE,    _load_users),
        ("tasks",    TASKS_FILE,    load_tasks),
    ):
        try:
            if not path.exists():
                continue
            mtime = path.stat().st_mtime
            if _state_mtimes[key] < mtime:
                loader()
                _state_mtimes[key] = mtime
        except Exception:
            # A failed stat / load just leaves the in-memory state alone —
            # the loaders themselves no longer wipe to {} on parse error,
            # so a transient FS hiccup can't lose data.
            pass


@app.before_request
def _refresh_persisted_state():
    # Run after the auth gate (it returned None for authed requests).
    # Skip for the static endpoints — they don't read shared state and
    # stat-ing 3 files on every CSS/font fetch is wasted syscalls.
    if request.endpoint in {"static", "uploaded_file", "i18n_js"}:
        return None
    _refresh_state_if_stale()



BASE_DIR = Path(__file__).resolve().parent

# Persistent data lives OUTSIDE the repo by default so credentials and
# workflow state never get committed by accident. Override DREAMINA_DATA_DIR
# in production (e.g. /var/lib/dreamina-web) for a stable on-disk location.
_data_env = os.environ.get("DREAMINA_DATA_DIR", "").strip()
DATA_DIR = Path(_data_env).expanduser().resolve() if _data_env else (Path.home() / ".dreamina-web").resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

TASKS_FILE = BASE_DIR / "tasks.json"
WORKFLOW_FILE = BASE_DIR / "workflow.json"
PROJECTS_FILE = BASE_DIR / "projects.json"
# USERS_FILE lives under DATA_DIR by default. DREAMINA_USERS_FILE can point
# at a custom path (e.g. a secrets-mount). _migrate_legacy_users_file picks
# up an in-repo users.json from older deployments and relocates it once.
_users_env = os.environ.get("DREAMINA_USERS_FILE", "").strip()
USERS_FILE = Path(_users_env).expanduser().resolve() if _users_env else (DATA_DIR / "users.json")
USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
_LEGACY_USERS_FILE = BASE_DIR / "users.json"
print(f"[users] USERS_FILE={USERS_FILE} (exists={USERS_FILE.exists()}); legacy={_LEGACY_USERS_FILE} (exists={_LEGACY_USERS_FILE.exists()})", flush=True)
if _LEGACY_USERS_FILE.exists() and _LEGACY_USERS_FILE != USERS_FILE and not USERS_FILE.exists():
    try:
        USERS_FILE.write_text(_LEGACY_USERS_FILE.read_text())
        _LEGACY_USERS_FILE.unlink()
        print(f"[users] migrated {_LEGACY_USERS_FILE} → {USERS_FILE}", flush=True)
    except Exception as exc:
        print(f"[users] migration from {_LEGACY_USERS_FILE} failed: {exc}", flush=True)
WORKFLOWS_DIR = BASE_DIR / "workflows"
WORKFLOWS_DIR.mkdir(exist_ok=True)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "15"))
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT_SECONDS", "21600"))  # 6 hours; <=0 disables timeout

# task_id -> {status, label, result, error, created_at}
# Locks are RLocks so callers can hold the lock around a mutation AND call
# the matching save_X() helper (which acquires the same lock internally).
# A plain Lock here would deadlock the request thread against itself —
# this is exactly what bit share_project / signup_submit before.
tasks: dict[str, dict] = {}
_tasks_lock = threading.RLock()

# project_id -> {name, description, ..., owner: email, shared_with: {email: 'read'|'write'}}
projects: dict[str, dict] = {}
_projects_lock = threading.RLock()

# email -> {password_hash: str, created_at: float}
users: dict[str, dict] = {}
_users_lock = threading.RLock()


# ── Persistence ───────────────────────────────────────────────────────────────

def _atomic_write_json(path: Path, payload) -> None:
    """Write JSON to `path` atomically: dump to a sibling .tmp, then rename
    into place. Readers in other worker processes either see the previous
    full content or the new full content — never a half-truncated file.
    Without this, a read that races with a write could parse 0 bytes,
    raise, and trigger the loader's exception path."""
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, path)  # atomic on POSIX + Windows
    except Exception:
        pass


def save_tasks():
    with _tasks_lock:
        _atomic_write_json(TASKS_FILE, tasks)


def load_tasks():
    global tasks
    if TASKS_FILE.exists():
        try:
            loaded = json.loads(TASKS_FILE.read_text())
        except Exception:
            # Transient parse failure (e.g. partial read during a write,
            # though atomic writes should prevent this) — keep whatever is
            # in memory rather than wiping every task.
            return
        tasks = loaded
        # Mark any tasks that were mid-run as interrupted (only on startup;
        # the reload hook also calls load_tasks but the marking is idempotent).
        dirty = False
        for t in tasks.values():
            if t.get("status") in ("running", "queued"):
                t["status"] = "error"
                t["error"] = "Server restarted while task was running"
                dirty = True
        if dirty:
            save_tasks()


load_tasks()


def _save_projects():
    with _projects_lock:
        _atomic_write_json(PROJECTS_FILE, projects)


def _load_projects():
    global projects
    if PROJECTS_FILE.exists():
        try:
            projects = json.loads(PROJECTS_FILE.read_text())
        except Exception:
            # Keep existing in-memory state on parse failure rather than
            # wiping to {} — losing every project for a transient read
            # would be far worse than serving briefly-stale data.
            pass


# ── Users / accounts ──────────────────────────────────────────────────────────

def _save_users():
    with _users_lock:
        _atomic_write_json(USERS_FILE, users)


def _load_users():
    global users
    if USERS_FILE.exists():
        try:
            loaded = json.loads(USERS_FILE.read_text()) or {}
        except Exception:
            # Don't wipe in-memory accounts on a transient read failure.
            return
        users = loaded
    else:
        users = {}
    # Visibility on startup — if the data dir was wiped between deploys
    # (Docker without a persistent volume, CI rsync that misses dotfiles,
    # etc.) the operator sees which path was read and which accounts
    # survived, instead of having to guess why a particular email no
    # longer authenticates.
    print(f"[users] loaded {len(users)} account(s) from {USERS_FILE}: {sorted(users.keys())}", flush=True)


def _bootstrap_users():
    """Make sure the env-defined credential exists as a real account so the
    legacy single-user setup keeps working after the multi-account migration."""
    env_email = os.environ.get("REEL_LOGIN_EMAIL", "admin@reel.local").strip().lower()
    env_pw = os.environ.get("REEL_LOGIN_PASSWORD", "reel")
    if env_email and env_email not in users:
        users[env_email] = {
            "password_hash": generate_password_hash(env_pw),
            "created_at": time.time(),
            "is_admin": True,
        }
        _save_users()
        print(f"[users] bootstrapped admin {env_email}", flush=True)


def _seed_users_from_env():
    """Re-create test accounts that should always exist after a deploy.

    DREAMINA_SEED_USERS is a comma-separated list of `email:password`
    pairs. Any pair whose email isn't already in `users` is created with
    a fresh werkzeug hash and saved to the live USERS_FILE. Existing
    accounts are NOT modified — passwords already in place keep working
    even if the env var lags behind. Use `scripts/create_user.py
    <email> <pw> --replace` to rotate a password.

    Example:
        DREAMINA_SEED_USERS="eugeneyeung01@gmail.com:12345678"
        DREAMINA_SEED_USERS="alice@x.com:pw1,bob@y.com:pw2"

    The point of this hook is exactly the scenario the user reported:
    each deploy wipes the data dir, so signup-created accounts vanish
    while bootstrap (admin) and any account that happened to be in
    a baked-in legacy users.json survive. Seeding from env makes
    persistence explicit and deploy-script-driven instead of dependent
    on what's on the host's filesystem.
    """
    raw = os.environ.get("DREAMINA_SEED_USERS", "").strip()
    if not raw:
        return
    added = []
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        email, _, pw = pair.partition(":")
        email = email.strip().lower()
        pw = pw.strip()
        if not email or "@" not in email or len(pw) < 6:
            print(f"[users] skipping malformed seed entry: {pair!r}", flush=True)
            continue
        if email in users:
            continue
        users[email] = {
            "password_hash": generate_password_hash(pw),
            "created_at": time.time(),
        }
        added.append(email)
    if added:
        _save_users()
        print(f"[users] seeded {len(added)} account(s) from DREAMINA_SEED_USERS: {added}", flush=True)


def _migrate_projects():
    """One-time fixups for projects created before the ownership/ACL fields
    existed: stamp every project with an owner (the bootstrap account) and an
    empty shared_with map so authorisation checks have something to look at.
    Also rename legacy 'read'/'write' permission values to the new 'view'/'edit'
    naming."""
    env_email = os.environ.get("REEL_LOGIN_EMAIL", "admin@reel.local").strip().lower()
    fallback_owner = env_email if env_email in users else (next(iter(users), None))
    if not fallback_owner:
        return
    changed = False
    for p in projects.values():
        if not isinstance(p, dict):
            continue
        if not p.get("owner"):
            p["owner"] = fallback_owner
            changed = True
        if not isinstance(p.get("shared_with"), dict):
            p["shared_with"] = {}
            changed = True
        else:
            for email, perm in list(p["shared_with"].items()):
                if perm == "read":
                    p["shared_with"][email] = "view"; changed = True
                elif perm == "write":
                    p["shared_with"][email] = "edit"; changed = True
    if changed:
        _save_projects()


# Permission name normalisation: accept the new {view, edit} pair plus the
# legacy {read, write} pair from older clients.
_PERMISSION_ALIAS = {"read": "view", "write": "edit", "view": "view", "edit": "edit"}
def _normalise_permission(value: str | None) -> str | None:
    if not value:
        return None
    return _PERMISSION_ALIAS.get(str(value).strip().lower())


# ── Authorisation helpers ─────────────────────────────────────────────────────

def current_user() -> str | None:
    return session.get("user")


def _get_project(pid: str) -> dict | None:
    p = projects.get(pid)
    return p if isinstance(p, dict) else None


def user_can_view(pid: str, email: str | None = None) -> bool:
    """Owner or anyone the project is shared with (view OR edit)."""
    email = email or current_user()
    p = _get_project(pid)
    if not email or not p:
        return False
    if p.get("owner") == email:
        return True
    return email in (p.get("shared_with") or {})


def user_can_edit(pid: str, email: str | None = None) -> bool:
    """Owner or a member with the edit permission. View-only members
    cannot mutate node parameters / topology."""
    email = email or current_user()
    p = _get_project(pid)
    if not email or not p:
        return False
    if p.get("owner") == email:
        return True
    return _normalise_permission((p.get("shared_with") or {}).get(email)) == "edit"


# Back-compat aliases — code still calling user_can_read/user_can_write
# keeps working, but new call sites should prefer the new names.
user_can_read  = user_can_view
user_can_write = user_can_edit


def user_owns(pid: str, email: str | None = None) -> bool:
    email = email or current_user()
    p = _get_project(pid)
    return bool(p and email and p.get("owner") == email)


_load_projects()
_load_users()
_bootstrap_users()
_seed_users_from_env()
_migrate_projects()

# Seed the per-worker mtime cache so the very first request doesn't
# redundantly reload these files we just loaded above. Subsequent
# requests reload only when a peer worker bumps the mtime.
for _key, _path in (("projects", PROJECTS_FILE), ("users", USERS_FILE), ("tasks", TASKS_FILE)):
    try:
        if _path.exists():
            _state_mtimes[_key] = _path.stat().st_mtime
    except Exception:
        pass


# ── CLI helper ────────────────────────────────────────────────────────────────

def run_command(cmd: list[str]) -> tuple[bool, str]:
    env = {**os.environ, "PATH": f"{os.environ.get('HOME', '')}/.local/bin:{os.environ.get('PATH', '')}"}
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
        output = result.stdout.strip() or result.stderr.strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "Command timed out after 5 minutes"
    except FileNotFoundError:
        return False, "dreamina CLI not found. Run setup.sh to install."
    except Exception as e:
        return False, str(e)


def _task_state(result_data: dict | None) -> str:
    if not isinstance(result_data, dict):
        return "unknown"

    status = str(result_data.get("gen_status") or result_data.get("status") or "").lower()
    if status in {"done", "success", "succeeded", "finish", "finished", "completed"}:
        return "done"
    if status in {"querying", "processing", "waiting", "queued", "queueing", "pending", "running"}:
        return "pending"
    if status in {"fail", "failed", "error"}:
        return "error"
    return "unknown"


def _should_keep_polling(result_data: dict | None, submit_id: str | None) -> bool:
    state = _task_state(result_data)
    if state == "pending":
        return True
    # Some CLI responses only include submit_id initially; treat that as async work in progress.
    if submit_id and state == "unknown":
        return True
    return False


# ── Task runner ───────────────────────────────────────────────────────────────

def run_task(task_id: str, cmd: list[str], post_process=None):
    # Outer try/except guarantees the daemon thread can't die quietly with
    # the task stranded in running/queued. Any unhandled exception lands
    # the task in 'error' with a diagnostic message so the client's
    # resumeTask / get_task recovery paths can pick it up instead of
    # showing a forever-spinning node.
    try:
        _run_task_inner(task_id, cmd, post_process)
    except Exception as exc:
        print(f"[run_task] {task_id} crashed: {exc!r}", flush=True)
        t = tasks.get(task_id)
        if isinstance(t, dict):
            t["status"] = "error"
            t["error"] = f"Task runner crashed: {exc}"
            save_tasks()


def _run_task_inner(task_id: str, cmd: list[str], post_process=None):
    tasks[task_id]["status"] = "running"
    tasks[task_id]["last_polled_at"] = time.time()
    save_tasks()

    _command_ok, output = run_command(cmd)

    result_data = None
    try:
        result_data = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        result_data = {"raw": output}

    submit_id = result_data.get("submit_id") if isinstance(result_data, dict) else None
    state = _task_state(result_data)
    last_pending_result = result_data if _should_keep_polling(result_data, submit_id) else None

    # Keep polling if task is still queued/processing
    if submit_id and _should_keep_polling(result_data, submit_id):
        deadline = (time.time() + POLL_TIMEOUT) if POLL_TIMEOUT > 0 else None
        while deadline is None or time.time() < deadline:
            queue_info = result_data.get("queue_info", {}) if isinstance(result_data, dict) else {}
            tasks[task_id]["result"] = result_data
            tasks[task_id]["status"] = "queued"
            tasks[task_id]["queue_idx"] = queue_info.get("queue_idx")
            tasks[task_id]["error"] = None
            tasks[task_id]["last_polled_at"] = time.time()
            save_tasks()

            time.sleep(POLL_INTERVAL)

            poll_ok, poll_output = run_command(["dreamina", "query_result", f"--submit_id={submit_id}"])
            try:
                polled_result = json.loads(poll_output)
            except (json.JSONDecodeError, ValueError):
                polled_result = {"raw": poll_output}

            poll_state = _task_state(polled_result)

            # Transient CLI/network timeouts should not fail a still-queued task.
            if not poll_ok and poll_state == "unknown":
                continue

            result_data = polled_result
            state = poll_state

            if _should_keep_polling(result_data, submit_id):
                last_pending_result = result_data

            if not _should_keep_polling(result_data, submit_id):
                break

        if deadline is not None and time.time() >= deadline and _should_keep_polling(result_data, submit_id):
            tasks[task_id]["status"] = "queued"
            tasks[task_id]["result"] = last_pending_result or result_data
            tasks[task_id]["error"] = f"Still queued after waiting {POLL_TIMEOUT // 3600 or POLL_TIMEOUT // 60}h; keep this task and query again later with submit_id={submit_id}"
            tasks[task_id]["last_polled_at"] = time.time()
            save_tasks()
            return

    state = _task_state(result_data)

    if state == "done":
        tasks[task_id]["status"] = "done"
        tasks[task_id]["result"] = result_data
        tasks[task_id]["error"] = None
        tasks[task_id].pop("queue_idx", None)
        if post_process:
            try:
                post_process(task_id)
            except Exception as exc:
                print(f"[task {task_id}] post-process failed: {exc}", flush=True)
        _cache_task_output(tasks[task_id])
    elif submit_id and _should_keep_polling(result_data, submit_id):
        tasks[task_id]["status"] = "queued"
        tasks[task_id]["result"] = last_pending_result or result_data
        tasks[task_id]["error"] = None
    else:
        tasks[task_id]["status"] = "error"
        tasks[task_id]["error"] = (
            output if not submit_id
            else (
                result_data.get("fail_reason")
                if isinstance(result_data, dict) and result_data.get("fail_reason")
                else f"Generation failed. submit_id={submit_id}"
            )
        )
        tasks[task_id]["result"] = result_data

    tasks[task_id]["last_polled_at"] = time.time()
    save_tasks()


def _start_task(cmd: list[str], label: str, post_process=None) -> dict:
    task_id = str(uuid.uuid4())
    tasks[task_id] = {
        "status": "queued",
        "label": label,
        "result": None,
        "error": None,
        "created_at": time.time(),
    }
    save_tasks()
    thread = threading.Thread(target=run_task, args=(task_id, cmd, post_process), daemon=True)
    thread.start()
    return {"ok": True, "task_id": task_id}


# ── Static files ──────────────────────────────────────────────────────────────

def _serve_html_no_cache(filename: str):
    """Same as send_from_directory("web", ...) but adds no-cache headers.
    Without these the browser can sit on a stale projects.html / workflow.html
    across deploys (we hit exactly this debugging the Duplicate flow) and end
    up calling endpoints with payloads from yesterday's JS."""
    resp = send_from_directory("web", filename)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.get("/")
def index():
    return _serve_html_no_cache("projects.html")


@app.get("/workflow")
def workflow_view():
    return _serve_html_no_cache("workflow.html")


@app.get("/i18n.js")
def i18n_js():
    resp = send_from_directory("web", "i18n.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.get("/login")
def login_page():
    if is_authed():
        return redirect("/")
    return send_from_directory("web", "login.html")


@app.post("/api/login")
def login_submit():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    remember = bool(data.get("remember"))
    if not email or not password:
        return jsonify({"ok": False, "error": "Enter your email and password to continue."}), 400
    user = users.get(email)
    if not user or not check_password_hash(user.get("password_hash") or "", password):
        return jsonify({"ok": False, "error": "Those credentials don't match an active account."}), 401
    session["user"] = email
    session.permanent = remember
    return jsonify({"ok": True, "user": email})


@app.post("/api/signup")
def signup_submit():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not email or "@" not in email:
        return jsonify({"ok": False, "error": "A valid email is required."}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "error": "Password must be at least 6 characters."}), 400
    if email in users:
        return jsonify({"ok": False, "error": "That email is already registered."}), 409
    with _users_lock:
        users[email] = {
            "password_hash": generate_password_hash(password),
            "created_at": time.time(),
        }
        _save_users()
    session["user"] = email
    session.permanent = True
    return jsonify({"ok": True, "user": email})


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
def whoami():
    email = session.get("user")
    info = users.get(email) if email else None
    return jsonify({
        "ok": True,
        "authed": is_authed(),
        "user": email,
        "is_admin": bool(info and info.get("is_admin")),
    })


@app.get("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.get("/api/media_proxy")
def media_proxy():
    source_url = request.args.get("url", "").strip()
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"}:
        return jsonify({"ok": False, "error": "A valid http(s) media URL is required"}), 400

    upstream_headers = {
        "User-Agent": request.headers.get("User-Agent", "Mozilla/5.0"),
    }
    if request.headers.get("Range"):
        upstream_headers["Range"] = request.headers["Range"]

    try:
        upstream = requests.get(source_url, headers=upstream_headers, stream=True, timeout=60)
    except requests.RequestException as exc:
        print(f"[media_proxy] upstream request failed for {parsed.netloc}{parsed.path}: {exc}", flush=True)
        return jsonify({"ok": False, "error": str(exc)}), 502

    if upstream.status_code >= 400:
        # 403 from a ByteDance CDN almost always means the signed URL's
        # x-expires has elapsed. Surface enough context that the user can
        # tell the cache pipeline never saved a local copy.
        expires = parse_qs(parsed.query).get("x-expires", [None])[0]
        expires_note = ""
        if expires and expires.isdigit():
            try:
                exp_ts = int(expires)
                age = int(time.time()) - exp_ts
                expires_note = f" x-expires={exp_ts} (expired {age}s ago)" if age > 0 else f" x-expires={exp_ts} (valid for {-age}s)"
            except Exception:
                pass
        print(f"[media_proxy] {upstream.status_code} from {parsed.netloc}{parsed.path}{expires_note}", flush=True)

    passthrough_headers = {}
    for key in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
        value = upstream.headers.get(key)
        if value:
            passthrough_headers[key] = value

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=1024 * 256):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(generate()),
        status=upstream.status_code,
        headers=passthrough_headers,
        direct_passthrough=True,
    )


# ── API: Account ──────────────────────────────────────────────────────────────

@app.get("/api/credits")
def get_credits():
    success, output = run_command(["dreamina", "user_credit"])
    return jsonify({"ok": success, "output": output})


# ── API: Tasks ────────────────────────────────────────────────────────────────

@app.get("/api/tasks")
def list_tasks():
    return jsonify({"ok": True, "tasks": tasks})


def _refresh_running_task(task_id: str) -> None:
    """Inline best-effort recovery for a task whose background poll thread
    has gone silent. Re-runs `dreamina query_result --submit_id=...` once,
    syncs the task state to whatever Jimeng now reports, runs the same
    post-cache step run_task does on completion, and persists.

    Called from /api/task/<id> when the cached state looks stale — the
    user's running node is stuck because we never noticed the upstream
    completed. Worst case (CLI fails, JSON malformed, etc.) the task
    stays in its current state and we log; the caller still gets a
    response, just with the same status it already had.
    """
    task = tasks.get(task_id)
    if not isinstance(task, dict):
        return
    result_data = task.get("result")
    submit_id = None
    if isinstance(result_data, dict):
        submit_id = result_data.get("submit_id")
        if not submit_id and isinstance(result_data.get("data"), dict):
            submit_id = result_data["data"].get("submit_id")
    if not submit_id:
        return

    ok, output = run_command(["dreamina", "query_result", f"--submit_id={submit_id}"])
    if not ok:
        print(f"[refresh_running_task] {task_id} CLI failed: {output[:200]}", flush=True)
        task["last_polled_at"] = time.time()
        save_tasks()
        return
    try:
        polled = json.loads(output)
    except Exception:
        print(f"[refresh_running_task] {task_id} non-JSON: {output[:200]}", flush=True)
        task["last_polled_at"] = time.time()
        save_tasks()
        return

    state = _task_state(polled)
    task["last_polled_at"] = time.time()
    if state == "done":
        task["status"] = "done"
        task["result"] = polled
        task["error"] = None
        task.pop("queue_idx", None)
        _cache_task_output(task)
        print(f"[refresh_running_task] {task_id} recovered → done", flush=True)
    elif state == "error":
        task["status"] = "error"
        task["result"] = polled
        task["error"] = (polled.get("fail_reason") if isinstance(polled, dict) else None) or "Generation failed"
        print(f"[refresh_running_task] {task_id} recovered → error", flush=True)
    elif _should_keep_polling(polled, submit_id):
        task["status"] = "queued"
        task["result"] = polled
        queue_info = polled.get("queue_info", {}) if isinstance(polled, dict) else {}
        task["queue_idx"] = queue_info.get("queue_idx")
    save_tasks()


@app.get("/api/task/<task_id>")
def get_task(task_id: str):
    task = tasks.get(task_id)
    if not task:
        return jsonify({"ok": False, "error": "Task not found"}), 404
    # On-demand recovery: if the background polling thread died (unhandled
    # exception, server-process death without the load_tasks reset firing,
    # etc.) a task can sit in running/queued forever while Jimeng has long
    # since produced the video. Force a single re-query when the cached
    # state is stale — anything older than ~3 polling cycles is suspicious.
    # The Jimeng CLI call adds a few seconds to the request only in this
    # recovery path; healthy tasks fall through with cached state.
    if task.get("status") in ("running", "queued"):
        last_polled = task.get("last_polled_at") or task.get("created_at") or 0
        if time.time() - last_polled > max(POLL_INTERVAL * 3, 45):
            _refresh_running_task(task_id)
            task = tasks.get(task_id) or task
    return jsonify({"ok": True, **task})


@app.delete("/api/task/<task_id>")
def delete_task(task_id: str):
    tasks.pop(task_id, None)
    save_tasks()
    return jsonify({"ok": True})


@app.delete("/api/tasks")
def clear_tasks():
    """Delete all completed/errored tasks, keep running ones."""
    to_remove = [k for k, v in tasks.items() if v["status"] in ("done", "error")]
    for k in to_remove:
        tasks.pop(k)
    save_tasks()
    return jsonify({"ok": True, "removed": len(to_remove)})


@app.get("/api/query/<submit_id>")
def query_result(submit_id: str):
    success, output = run_command(["dreamina", "query_result", f"--submit_id={submit_id}"])
    try:
        data = json.loads(output)
    except Exception:
        data = {"raw": output}
    return jsonify({"ok": success, "data": data})


# ── API: Generation ───────────────────────────────────────────────────────────

def _parse_ratio(ratio: str) -> tuple[float, float] | None:
    if not ratio:
        return None
    try:
        rw_str, rh_str = ratio.split(":", 1)
        rw, rh = float(rw_str), float(rh_str)
        if rw > 0 and rh > 0:
            return rw, rh
    except Exception:
        pass
    return None


def _video_finalize_post_process(ratio: str | None = None, audio_path: str | None = None):
    """Build a post_process callback that downloads the dreamina-generated video,
    optionally center-crops it to `ratio`, optionally muxes `audio_path` onto it,
    saves it under uploads/, and rewrites the task result's serve_path to the
    local file. Best-effort: any failure leaves the original CDN result untouched.
    """
    want_ratio = _parse_ratio(ratio) if ratio else None
    has_audio = bool(audio_path and Path(audio_path).exists())
    if not want_ratio and not has_audio:
        return None

    def _post(task_id: str) -> None:
        task = tasks.get(task_id)
        if not isinstance(task, dict):
            return
        result_data = task.get("result")
        video_url = _extract_output_url(result_data) if isinstance(result_data, dict) else None
        if not video_url:
            return
        local_video = _download_media(video_url, hint_ext=".mp4")
        if not local_video or local_video.startswith("http"):
            print(f"[finalize] failed to download {video_url}", flush=True)
            return

        # Decide whether a crop is actually needed (skip if already within 2%).
        crop_filter = None
        if want_ratio:
            size = _probe_image_size(local_video)
            if size:
                w, h = size
                target = want_ratio[0] / want_ratio[1]
                actual = w / h
                if abs(actual - target) / target >= 0.02:
                    if actual > target:
                        new_w = int(round(h * target)); new_w -= new_w % 2
                        new_h = h - (h % 2)
                        x = (w - new_w) // 2; y = 0
                    else:
                        new_h = int(round(w / target)); new_h -= new_h % 2
                        new_w = w - (w % 2)
                        x = 0; y = (h - new_h) // 2
                    crop_filter = f"crop={new_w}:{new_h}:{x}:{y}"

        # No-op? Don't burn ffmpeg cycles.
        if not crop_filter and not has_audio:
            return

        out_path = UPLOAD_DIR / f"final_{uuid.uuid4()}.mp4"
        cmd = ["ffmpeg", "-y", "-loglevel", "warning", "-i", local_video]
        if has_audio:
            cmd += ["-i", audio_path]
        if crop_filter:
            cmd += ["-vf", crop_filter, "-c:v", "libx264", "-preset", "fast", "-crf", "20"]
        else:
            cmd += ["-c:v", "copy"]
        if has_audio:
            cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "192k", "-shortest"]
        else:
            cmd += ["-c:a", "copy"]
        cmd += ["-movflags", "+faststart", str(out_path)]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=300)
        except Exception as exc:
            print(f"[finalize] ffmpeg failed: {exc}", flush=True)
            return
        if not out_path.exists() or out_path.stat().st_size == 0:
            return

        serve_path = "/uploads/" + out_path.name
        merged = dict(result_data) if isinstance(result_data, dict) else {}
        merged["serve_path"] = serve_path
        merged["local_path"] = str(out_path)
        merged["original_url"] = video_url
        if has_audio:
            merged["audio_muxed"] = True
        if crop_filter:
            merged["ratio_enforced"] = ratio
        task["result"] = merged
        print(f"[finalize] {video_url} → {serve_path}"
              f"{f' crop={crop_filter}' if crop_filter else ''}"
              f"{' +audio' if has_audio else ''}", flush=True)
    return _post


# Kept for callers that only do audio muxing (back-compat shim).
def _mux_audio_post_process(audio_path: str):
    return _video_finalize_post_process(ratio=None, audio_path=audio_path)


def _resolve_audio_for_mux(request) -> str | None:
    """Pull `audio_path` from a video-generation request body (JSON or multipart)
    and resolve it to a local file path. Returns None when no audio was supplied."""
    if request.content_type and "multipart" in request.content_type:
        ref = (request.form.get("audio_path") or "").strip()
    else:
        data = request.get_json(silent=True) or {}
        ref = (data.get("audio_path") or "").strip() if isinstance(data, dict) else ""
    if not ref:
        return None
    return _resolve_clip_path(ref)


def _resolve_video_ref(request) -> str | None:
    """Pull a `video_path` (camera-motion reference) from the request body."""
    if request.content_type and "multipart" in request.content_type:
        ref = (request.form.get("video_path") or "").strip()
    else:
        data = request.get_json(silent=True) or {}
        ref = (data.get("video_path") or "").strip() if isinstance(data, dict) else ""
    if not ref:
        return None
    return _resolve_clip_path(ref)


@app.post("/api/text2video")
def text2video():
    data = request.get_json(force=True)
    prompt = data.get("prompt", "").strip()
    model_version = data.get("model_version", "").strip()
    if not prompt:
        return jsonify({"ok": False, "error": "prompt is required"}), 400
    ratio = data.get("ratio", "16:9")
    cmd = [
        "dreamina", "text2video",
        f"--prompt={prompt}",
        f"--duration={data.get('duration', '5')}",
        f"--ratio={ratio}",
        f"--video_resolution={data.get('resolution', '720P')}",
        "--poll=240",
    ]
    if model_version:
        cmd.append(f"--model_version={model_version}")
    audio_path = _resolve_audio_for_mux(request)
    # text2video natively supports --ratio; still enforce in post-process as a
    # belt-and-braces guard in case the CLI drifts back to a default.
    post = _video_finalize_post_process(ratio=ratio, audio_path=audio_path)
    return jsonify(_start_task(cmd, f"text2video: {prompt[:60]}", post_process=post))


@app.post("/api/text2image")
def text2image():
    data = request.get_json(force=True)
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"ok": False, "error": "prompt is required"}), 400
    cmd = [
        "dreamina", "text2image",
        f"--prompt={prompt}",
        f"--ratio={data.get('ratio', '16:9')}",
        f"--resolution_type={data.get('resolution', '2k')}",
        "--poll=90",
    ]
    return jsonify(_start_task(cmd, f"text2image: {prompt[:60]}"))


@app.post("/api/text2audio")
def text2audio():
    data = request.get_json(silent=True) or {}
    prompt = data.get("prompt", "").strip()
    duration = str(data.get("duration", "30")).strip()
    if not prompt:
        return jsonify({"ok": False, "error": "prompt is required"}), 400

    api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not api_key:
        return jsonify({
            "ok": False,
            "error": "MINIMAX_API_KEY is not set. Export it before starting the server.",
        }), 500

    task_id = str(uuid.uuid4())
    tasks[task_id] = {
        "status": "queued",
        "label": f"text2audio: {prompt[:60]}",
        "result": None,
        "error": None,
        "created_at": time.time(),
    }
    save_tasks()
    thread = threading.Thread(
        target=_run_minimax_audio_task,
        args=(task_id, prompt, duration, api_key),
        daemon=True,
    )
    thread.start()
    return jsonify({"ok": True, "task_id": task_id})


# ── MiniMax TTS async implementation ─────────────────────────────────────────

MINIMAX_CREATE_URL = "https://api.minimax.io/v1/t2a_async_v2"
MINIMAX_QUERY_URL  = "https://api.minimax.io/v1/query/t2a_async_query_v2"
MINIMAX_FILE_URL   = "https://api.minimax.io/v1/files/retrieve"
MINIMAX_MODEL      = os.environ.get("MINIMAX_MODEL", "speech-02-hd")
MINIMAX_VOICE_ID   = os.environ.get("MINIMAX_VOICE_ID", "English_expressive_narrator")


def _run_minimax_audio_task(task_id: str, text: str, duration: str, api_key: str) -> None:
    """Create a MiniMax TTS task, poll until done, download the audio, update tasks."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def fail(msg: str) -> None:
        tasks[task_id]["status"] = "error"
        tasks[task_id]["error"] = msg
        save_tasks()

    # 1. Create async task
    try:
        tasks[task_id]["status"] = "running"
        save_tasks()
        resp = requests.post(
            MINIMAX_CREATE_URL,
            headers=headers,
            json={
                "model": MINIMAX_MODEL,
                "text": text,
                "voice_setting": {"voice_id": MINIMAX_VOICE_ID, "speed": 1.0, "vol": 10},
                "audio_setting": {"format": "mp3", "audio_sample_rate": 32000, "bitrate": 128000},
            },
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        return fail(f"MiniMax create request failed: {exc}")

    base = body.get("base_resp", {})
    if base.get("status_code", 0) != 0:
        return fail(f"MiniMax error: {base.get('status_msg', body)}")

    mm_task_id = body.get("task_id")
    file_id    = body.get("file_id")
    if not mm_task_id:
        return fail(f"MiniMax did not return a task_id: {body}")

    print(f"[minimax] task_id={mm_task_id} file_id={file_id}", flush=True)

    # 2. Poll until Succeeded (up to ~5 min)
    deadline = time.time() + 300
    poll_interval = 3
    while time.time() < deadline:
        time.sleep(poll_interval)
        poll_interval = min(poll_interval * 1.5, 15)  # back off gently
        try:
            qr = requests.get(
                MINIMAX_QUERY_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                params={"task_id": mm_task_id},
                timeout=15,
            )
            qr.raise_for_status()
            qbody = qr.json()
        except Exception as exc:
            print(f"[minimax] poll error: {exc}", flush=True)
            continue

        status = qbody.get("status", "")
        print(f"[minimax] poll status={status}", flush=True)
        if status in ("Success", "Succeeded"):
            file_id = qbody.get("file_id") or file_id
            break
        if status not in ("Processing", "Pending", ""):
            return fail(f"MiniMax task failed with status: {status} — {qbody}")
    else:
        return fail("MiniMax TTS timed out after 5 minutes")

    if not file_id:
        return fail("MiniMax task succeeded but returned no file_id")

    # 3. Retrieve audio URL
    try:
        fr = requests.get(
            MINIMAX_FILE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            params={"file_id": file_id},
            timeout=15,
        )
        fr.raise_for_status()
        fbody = fr.json()
    except Exception as exc:
        return fail(f"MiniMax file retrieve failed: {exc}")

    file_info = fbody.get("file") if isinstance(fbody.get("file"), dict) else {}
    audio_url = (
        file_info.get("download_url")
        or file_info.get("audio_url")
        or fbody.get("audio_url")
        or fbody.get("download_url")
    )
    filename = file_info.get("filename") or ""
    if not audio_url:
        return fail(f"MiniMax file retrieve returned no audio_url: {fbody}")

    # 4. Download audio to uploads/ for local serving.
    # The async TTS endpoint packages the audio inside a .tar archive — extract it.
    is_tar = filename.lower().endswith(".tar") or urlparse(audio_url).path.lower().endswith(".tar")
    local_path = _download_media(audio_url, hint_ext=".tar" if is_tar else ".mp3")
    if local_path and not local_path.startswith("http") and is_tar:
        local_path = _extract_audio_from_tar(local_path)

    if not local_path or local_path.startswith("http"):
        # Serve the CDN URL directly if download/extract failed
        tasks[task_id]["status"] = "done"
        tasks[task_id]["result"] = {"url": audio_url}
        tasks[task_id]["error"] = None
        save_tasks()
        return

    serve_path = "/uploads/" + Path(local_path).name
    tasks[task_id]["status"] = "done"
    tasks[task_id]["result"] = {"url": audio_url, "serve_path": serve_path}
    tasks[task_id]["error"] = None
    save_tasks()
    print(f"[minimax] done → {serve_path}", flush=True)


def _resolve_image_input(request) -> str | None:
    """Return a path-or-URL usable by `dreamina --image=...`.
    Accepts either a multipart upload OR JSON with image_url.
    """
    if request.content_type and "multipart" in request.content_type:
        image_file = request.files.get("image")
        if image_file:
            save_path = UPLOAD_DIR / f"{uuid.uuid4()}_{image_file.filename}"
            image_file.save(save_path)
            return str(save_path)
        return None

    data = request.get_json(silent=True) or {}
    image_ref = data.get("image_url") or data.get("image") or None
    if not image_ref:
        return None

    parsed = urlparse(str(image_ref))

    # /uploads/<file> paths have no URL scheme but are NOT filesystem paths —
    # resolve them to the actual local file before anything else.
    if parsed.path.startswith("/uploads/"):
        local = UPLOAD_DIR / parsed.path.removeprefix("/uploads/")
        if local.exists():
            return str(local.resolve())
        return None

    # Already a filesystem path (no scheme)
    if not parsed.scheme or parsed.scheme not in {"http", "https"}:
        return str(image_ref)

    # Remote URL: download locally so the CLI gets a file it can upload.
    # _download_image returns the URL itself on failure — the CLI only accepts
    # local paths, so treat a URL return as a failed download.
    local = _download_image(str(image_ref))
    if local and not local.startswith("http"):
        return local
    return None


_MIME_TO_EXT = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
}


def _download_media(url: str, hint_ext: str = "", *, retries: int = 3) -> str:
    """Download a CDN media URL into uploads/; return local path (or url on failure).

    Extension priority:
      1. Content-Type response header (most reliable for CDN files)
      2. Path component of the URL
      3. Caller-supplied hint_ext  (e.g. ".mp4" for video tasks)
      4. ".bin" as last resort (never misidentify video as image)

    Retries transient failures (5xx, network errors) with exponential backoff
    so a single CDN hiccup doesn't strand the task with an ephemeral signed
    URL that will 403 once x-expires elapses. 4xx is permanent — bail fast.
    """
    parsed = urlparse(url)
    url_suffix = Path(parsed.path).suffix.lower()
    if len(url_suffix) > 6:
        url_suffix = ""

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=60, stream=True, headers={"User-Agent": "Mozilla/5.0"})
            if 400 <= resp.status_code < 500:
                # Signed URL expired / signature invalid / permission denied —
                # retrying won't help. Surface it loudly.
                print(f"[download_media] {resp.status_code} on {parsed.netloc}{parsed.path} — not retrying", flush=True)
                resp.close()
                return url
            resp.raise_for_status()

            ct = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            suffix = _MIME_TO_EXT.get(ct) or url_suffix or hint_ext or ".bin"

            local = UPLOAD_DIR / f"dl_{uuid.uuid4()}{suffix}"
            with open(local, "wb") as f:
                for chunk in resp.iter_content(65536):
                    if chunk:
                        f.write(chunk)
            return str(local)
        except Exception as exc:
            last_err = exc
            if attempt < retries:
                wait = 2 ** (attempt - 1)
                print(f"[download_media] attempt {attempt}/{retries} failed for {parsed.netloc}: {exc} — retrying in {wait}s", flush=True)
                time.sleep(wait)
    print(f"[download_media] gave up after {retries} attempts on {parsed.netloc}{parsed.path}: {last_err}", flush=True)
    return url  # fall back to passing the URL directly


# Keep old name as alias so existing callers don't break
_download_image = _download_media


_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".webm"}


def _extract_audio_from_tar(tar_path: str) -> str | None:
    """Extract the audio file from a tar archive into uploads/.
    Prefers members with a known audio extension; falls back to the largest
    file if no extension matches. Returns the new local path, or None on
    failure. Removes the tar on success.
    """
    import tarfile

    try:
        src = Path(tar_path)
        with tarfile.open(src, "r:*") as tf:
            members = [m for m in tf.getmembers() if m.isfile()]
            if not members:
                return None
            audio_member = next(
                (m for m in members if Path(m.name).suffix.lower() in _AUDIO_EXTS),
                None,
            )
            if audio_member is None:
                # Fall back to the largest file (audio dominates a TTS bundle).
                audio_member = max(members, key=lambda m: m.size)
            suffix = Path(audio_member.name).suffix.lower()
            if suffix not in _AUDIO_EXTS:
                suffix = ".mp3"
            out_path = UPLOAD_DIR / f"dl_{uuid.uuid4()}{suffix}"
            extracted = tf.extractfile(audio_member)
            if extracted is None:
                return None
            with open(out_path, "wb") as f:
                while True:
                    chunk = extracted.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
        try:
            src.unlink()
        except OSError:
            pass
        return str(out_path)
    except Exception as exc:
        print(f"[minimax] tar extract failed: {exc}", flush=True)
        return None


def _extract_output_url(result_data: dict) -> str | None:
    """Find the CDN output URL inside a completed task result (mirrors frontend extractOutputUrl)."""
    if not isinstance(result_data, dict):
        return None
    r = result_data
    d = r.get("data")
    checks = [
        r.get("url"), r.get("video_url"), r.get("image_url"),
    ]
    if isinstance(d, dict):
        checks += [d.get("url"), d.get("video_url"), d.get("image_url")]
    for v in checks:
        if v and isinstance(v, str) and v.startswith("http"):
            return v
    # urls list
    if isinstance(r.get("urls"), list) and r["urls"]:
        return r["urls"][0]
    if isinstance(d, list) and d and isinstance(d[0], dict):
        return d[0].get("url")
    # item_list
    for container in (r, d if isinstance(d, dict) else {}):
        if not isinstance(container, dict):
            continue
        item_list = container.get("item_list")
        if isinstance(item_list, list) and item_list:
            it = item_list[0]
            if isinstance(it, dict):
                url = (it.get("video") or {}).get("url") or (it.get("image") or {}).get("url") or it.get("url")
                if url:
                    return url
    # result_json
    for container in (r, d if isinstance(d, dict) else {}):
        if not isinstance(container, dict):
            continue
        rj = container.get("result_json")
        if isinstance(rj, dict):
            vids = rj.get("videos", [])
            if vids:
                return vids[0].get("video_url") or vids[0].get("url")
            imgs = rj.get("images", [])
            if imgs:
                return imgs[0].get("image_url") or imgs[0].get("url")
    return None


def _cache_task_output(task: dict) -> None:
    """Download the generated output to uploads/ for browser display (serve_path).
    Never overwrites local_path — that is the CLI's own saved file and must stay
    intact so it can be re-used as input to subsequent CLI commands."""
    result_data = task.get("result")
    if not isinstance(result_data, dict):
        return
    # Already have a browser-accessible copy
    if result_data.get("serve_path"):
        return
    if isinstance(result_data.get("data"), dict) and result_data["data"].get("serve_path"):
        return
    url = _extract_output_url(result_data)
    if not url or not url.startswith("http"):
        return
    label = task.get("label", "")
    if "video" in label.lower():
        hint = ".mp4"
    elif "audio" in label.lower():
        hint = ".mp3"
    else:
        hint = ".jpg"
    downloaded = _download_media(url, hint_ext=hint)
    if downloaded and not downloaded.startswith("http"):
        serve_path = "/uploads/" + Path(downloaded).name
        result_data["serve_path"] = serve_path
        if isinstance(result_data.get("data"), dict):
            result_data["data"]["serve_path"] = serve_path
    else:
        # Caching failed — the task will keep the ephemeral signed URL and
        # 403 once x-expires elapses (typically a few hours). Mark it so the
        # owner can spot it in /api/tasks and re-run the node before then.
        print(f"[cache_task_output] could not cache {label!r} → client will use ephemeral CDN URL: {url}", flush=True)
        task["cache_failed"] = True


def _refresh_task_media(task_id: str) -> tuple[bool, str | dict]:
    """Re-query Jimeng for a fresh signed URL using the task's stored
    submit_id, download it into uploads/, and patch the task with the new
    serve_path + url. Used when an originally-cached URL has expired (or
    never got a local copy) — Jimeng signs a new short-lived URL on every
    query_result call, so this works as long as the asset is still on the
    CDN's side (typically days, not weeks).

    Returns (ok, result_or_error). On success result is
    {"servePath": "/uploads/...", "url": "<fresh signed url>"}.
    """
    task = tasks.get(task_id)
    if not task:
        return False, "Task not found"
    result_data = task.get("result")
    if not isinstance(result_data, dict):
        return False, "Task has no result to refresh"
    submit_id = result_data.get("submit_id")
    if not submit_id and isinstance(result_data.get("data"), dict):
        submit_id = result_data["data"].get("submit_id")
    if not submit_id:
        return False, "Task has no submit_id — cannot re-query Jimeng"

    ok, output = run_command(["dreamina", "query_result", f"--submit_id={submit_id}"])
    if not ok:
        return False, f"query_result failed: {output[:200]}"
    try:
        fresh = json.loads(output)
    except Exception:
        return False, "query_result returned non-JSON output"

    new_url = _extract_output_url(fresh)
    if not new_url or not new_url.startswith("http"):
        return False, "Refreshed payload has no URL — asset may have been removed by Jimeng"

    label = task.get("label", "")
    if "video" in label.lower():
        hint = ".mp4"
    elif "audio" in label.lower():
        hint = ".mp3"
    else:
        hint = ".jpg"
    downloaded = _download_media(new_url, hint_ext=hint)
    if not downloaded or downloaded.startswith("http"):
        return False, "Download of refreshed URL failed"

    serve_path = "/uploads/" + Path(downloaded).name
    result_data["url"] = new_url
    result_data["serve_path"] = serve_path
    if isinstance(result_data.get("data"), dict):
        result_data["data"]["url"] = new_url
        result_data["data"]["serve_path"] = serve_path
    task.pop("cache_failed", None)
    save_tasks()
    print(f"[refresh_task_media] {task_id} → {serve_path}", flush=True)
    return True, {"servePath": serve_path, "url": new_url}


@app.post("/api/refresh_media")
def refresh_media_endpoint():
    """Look up which task owns a given (likely-expired) CDN url, then call
    Jimeng again via that task's submit_id to obtain a fresh signed URL and
    download it locally. The client calls this from the onerror handler on
    media tags so a 403 silently self-heals into a permanent /uploads/ path.
    """
    data = request.get_json(force=True, silent=True) or {}
    stale = (data.get("url") or "").strip()
    if not stale:
        return jsonify({"ok": False, "error": "url is required"}), 400

    # Match either against the original CDN url or the cached serve_path —
    # the client may have either after a previous successful refresh.
    needle_path = stale.split("?")[0] if "?" in stale else stale
    matched_id: str | None = None
    for tid, task in tasks.items():
        result = task.get("result")
        if not isinstance(result, dict):
            continue
        candidates = [_extract_output_url(result), result.get("serve_path")]
        if isinstance(result.get("data"), dict):
            candidates += [result["data"].get("serve_path"), result["data"].get("url")]
        for c in candidates:
            if not c:
                continue
            if c == stale or c.split("?")[0] == needle_path:
                matched_id = tid
                break
        if matched_id:
            break

    if not matched_id:
        return jsonify({"ok": False, "error": "No task on this server owns that URL"}), 404

    ok, payload = _refresh_task_media(matched_id)
    if not ok:
        return jsonify({"ok": False, "error": payload}), 502
    return jsonify({"ok": True, **payload})


def _get_param(request, key: str, default=""):
    if request.content_type and "multipart" in request.content_type:
        return request.form.get(key, default)
    data = request.get_json(silent=True) or {}
    return data.get(key, default)


@app.post("/api/download_url")
def download_url_endpoint():
    """Download a remote URL to uploads/ and return a stable local path."""
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url.startswith("http"):
        return jsonify({"ok": False, "error": "invalid url"}), 400
    local_path = _download_image(url)
    if local_path.startswith("http"):
        return jsonify({"ok": False, "error": "download failed or url expired"}), 502
    serve_path = "/uploads/" + Path(local_path).name
    return jsonify({"ok": True, "localPath": serve_path})


@app.post("/api/image2video")
def image2video():
    prompt = _get_param(request, "prompt", "").strip()
    duration = _get_param(request, "duration", "5")
    ratio = _get_param(request, "ratio", "").strip()
    model_version = _get_param(request, "model_version", "").strip()
    image_ref = _resolve_image_input(request)
    if not image_ref:
        return jsonify({"ok": False, "error": "image is required — upload failed or URL could not be downloaded (it may have expired)"}), 400
    # Re-frame the image to the requested ratio; the CLI also takes --ratio,
    # but giving it a source that already matches avoids any internal letterbox.
    if ratio:
        image_ref = _crop_image_to_ratio(image_ref, ratio)
    cmd = [
        "dreamina", "image2video",
        f"--image={image_ref}",
        f"--duration={duration}",
        "--poll=240",
    ]
    if ratio:
        cmd.append(f"--ratio={ratio}")
    if prompt:
        cmd.append(f"--prompt={prompt}")
    if model_version:
        cmd.append(f"--model_version={model_version}")
    # image2video: --ratio is supported (consistent with multimodal2video). The
    # --audio flag isn't confirmed for image2video, so keep ffmpeg post-mux.
    audio_path = _resolve_audio_for_mux(request)
    post = _video_finalize_post_process(ratio=ratio, audio_path=audio_path)
    return jsonify(_start_task(cmd, f"image2video: {prompt[:60] or image_ref[-40:]}", post_process=post))


@app.post("/api/multimodal2video")
def multimodal2video():
    prompt = _get_param(request, "prompt", "").strip()
    duration = _get_param(request, "duration", "5")
    ratio = _get_param(request, "ratio", "").strip()
    model_version = _get_param(request, "model_version", "").strip()
    image_ref = _resolve_image_input(request)
    if not image_ref:
        return jsonify({"ok": False, "error": "image is required for multimodal2video — upload failed or URL could not be downloaded"}), 400
    if not prompt:
        return jsonify({"ok": False, "error": "prompt is required for multimodal2video"}), 400
    if ratio:
        image_ref = _crop_image_to_ratio(image_ref, ratio)
    cmd = [
        "dreamina", "multimodal2video",
        f"--image={image_ref}",
        f"--prompt={prompt}",
        f"--duration={duration}",
        "--poll=240",
    ]
    if ratio:
        cmd.append(f"--ratio={ratio}")
    if model_version:
        cmd.append(f"--model_version={model_version}")
    audio_path = _resolve_audio_for_mux(request)
    if audio_path:
        cmd.append(f"--audio={audio_path}")
    # Optional video reference for camera-motion guidance (multimodal2video accepts --video)
    video_ref = _resolve_video_ref(request)
    if video_ref:
        cmd.append(f"--video={video_ref}")
    post = _video_finalize_post_process(ratio=ratio, audio_path=None)
    return jsonify(_start_task(cmd, f"multimodal2video: {prompt[:60]}", post_process=post))


@app.post("/api/image2image")
def image2image():
    prompt = _get_param(request, "prompt", "").strip()
    ratio = _get_param(request, "ratio", "")
    image_ref = _resolve_image_input(request)
    if not image_ref:
        return jsonify({"ok": False, "error": "image or image_url is required"}), 400
    if not prompt:
        return jsonify({"ok": False, "error": "prompt is required"}), 400
    cmd = [
        "dreamina", "image2image",
        f"--images={image_ref}",
        f"--prompt={prompt}",
        "--poll=90",
    ]
    if ratio:
        cmd.append(f"--ratio={ratio}")
    return jsonify(_start_task(cmd, f"image2image: {prompt[:60]}"))


# ── API: Text generation via Doubao (multi-modal in, text out) ───────────────

# Chat Completions endpoint — stable, supported for all Ark models.
DOUBAO_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
DOUBAO_MODEL = os.environ.get("DOUBAO_MODEL", "doubao-seed-2-0-pro-260215")

_IMG_EXT_TO_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}


def _image_to_data_url(local_path: str) -> str | None:
    """Read a local image file and return a base64 data URL the API can fetch."""
    import base64
    try:
        p = Path(local_path)
        if not p.exists() or not p.is_file():
            return None
        mime = _IMG_EXT_TO_MIME.get(p.suffix.lower(), "image/jpeg")
        with open(p, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception:
        return None


def _resolve_image_url_for_doubao(ref: str) -> str | None:
    """Turn a frontend-supplied image reference into something the Doubao API
    can fetch. Public http(s) URLs pass through; local /uploads/ paths and
    absolute filesystem paths are inlined as base64 data URLs."""
    if not ref:
        return None
    if ref.startswith(("http://", "https://", "data:")):
        return ref
    candidate = ref
    if ref.startswith("/uploads/"):
        candidate = str(UPLOAD_DIR / ref[len("/uploads/"):])
    return _image_to_data_url(candidate)


@app.post("/api/text")
def text_generate():
    """Multi-modal text generation via Doubao Ark Responses API.

    Body shape:
      {
        "prompt": "Write a 30s product script…",
        "inputs": [
          {"type": "text",  "content": "Product: AeroBrew kettle"},
          {"type": "text",  "content": "Selling points: 90s boil, app-controlled"},
          {"type": "image", "url":  "/uploads/xxx.jpg"},
          {"type": "image", "url":  "https://…/photo.png"},
          {"type": "video", "url":  "/uploads/yyy.mp4", "label": "Hero teaser"}
        ]
      }

    The Doubao endpoint currently accepts text + image content parts. Videos
    are surfaced to the model as a textual reference line so the prompt still
    gets that context even though the bytes can't be sent directly.
    """
    api_key = os.environ.get("ARK_API_KEY", "").strip()
    if not api_key:
        return jsonify({
            "ok": False,
            "error": "ARK_API_KEY is not set on the server. Export it before starting the server.",
        }), 500

    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    raw_inputs = data.get("inputs") or []
    if not prompt and not raw_inputs:
        return jsonify({"ok": False, "error": "prompt or inputs is required"}), 400

    # Chat Completions content parts: images use {type:"image_url", image_url:{url:...}},
    # text uses {type:"text", text:...}  (same shape as OpenAI vision API).
    content_parts: list[dict] = []
    video_notes: list[str] = []
    skipped_images: list[str] = []

    for item in raw_inputs:
        if not isinstance(item, dict):
            continue
        kind = (item.get("type") or "").lower()
        if kind == "text":
            txt = (item.get("content") or item.get("text") or "").strip()
            if txt:
                content_parts.append({"type": "text", "text": txt})
        elif kind == "image":
            ref = item.get("url") or item.get("path") or ""
            resolved = _resolve_image_url_for_doubao(ref)
            if resolved:
                content_parts.append({"type": "image_url", "image_url": {"url": resolved}})
            else:
                skipped_images.append(ref)
        elif kind == "video":
            ref = item.get("url") or item.get("path") or ""
            label = item.get("label") or "Video"
            if ref:
                video_notes.append(f"- {label}: {ref}")

    # The user prompt itself goes last so it sits next to the model's response.
    if video_notes:
        content_parts.append({
            "type": "text",
            "text": "Reference videos (URLs, frames not transmitted):\n" + "\n".join(video_notes),
        })
    if prompt:
        content_parts.append({"type": "text", "text": prompt})

    if not content_parts:
        return jsonify({"ok": False, "error": "no usable content after resolving inputs"}), 400

    payload = {
        "model": DOUBAO_MODEL,
        "messages": [{"role": "user", "content": content_parts}],
    }

    print(f"[doubao] POST {DOUBAO_URL} model={DOUBAO_MODEL} parts={len(content_parts)}", flush=True)

    try:
        resp = requests.post(
            DOUBAO_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )
    except requests.RequestException as e:
        print(f"[doubao] request exception: {e}", flush=True)
        return jsonify({"ok": False, "error": f"Doubao request failed: {e}"}), 200

    if not resp.ok:
        try:
            err = resp.json()
        except Exception:
            err = {"raw": resp.text[:1000]}
        print(f"[doubao] HTTP {resp.status_code} response: {err}", flush=True)
        # Always return 200 to the browser so the JSON body survives proxies/devtools.
        # The frontend reads `ok`/`error` to know it failed.
        hint = ""
        if resp.status_code == 503:
            hint = (" — model may not be activated for this API key. "
                    "Check Volces Ark console: 推理接入点 (Inference Endpoints) "
                    "and confirm '" + DOUBAO_MODEL + "' is enabled, "
                    "or set DOUBAO_MODEL=<your endpoint id, e.g. ep-xxxxxxxx>.")
        return jsonify({
            "ok": False,
            "error": f"Doubao API error (HTTP {resp.status_code}){hint}",
            "detail": err,
            "upstream_status": resp.status_code,
        }), 200

    body = resp.json()
    text = _extract_doubao_text(body)
    if not text:
        print(f"[doubao] empty text in response: {body}", flush=True)
        return jsonify({"ok": False, "error": "Doubao returned no text", "detail": body}), 200

    return jsonify({
        "ok": True,
        "text": text,
        "model": DOUBAO_MODEL,
        "skipped_images": skipped_images,
    })


def _extract_doubao_text(body: dict) -> str:
    """Extract assistant reply from a Chat Completions response."""
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        msg = (choices[0] or {}).get("message") or {}
        c = msg.get("content")
        if isinstance(c, str):
            return c.strip()
        # Some vision models return content as a list of parts
        if isinstance(c, list):
            return "\n".join(
                p.get("text", "") for p in c
                if isinstance(p, dict) and p.get("type") in ("text", "output_text")
            ).strip()
    return ""


# ── Video Edit (FFmpeg + LLM edit plan) ───────────────────────────────────────

import re as _re


def _resolve_clip_path(ref: str | None) -> str | None:
    """Resolve a clip reference (serve path, upload path, or URL) to a local file."""
    if not ref:
        return None
    if ref.startswith("/uploads/"):
        local = UPLOAD_DIR / ref.removeprefix("/uploads/")
        return str(local) if local.exists() else None
    if ref.startswith("/") and Path(ref).exists():
        return ref
    if ref.startswith(("http://", "https://")):
        downloaded = _download_media(ref, hint_ext=".mp4")
        return downloaded if downloaded and not downloaded.startswith("http") else None
    if Path(ref).exists():
        return ref
    return None


def _probe_duration(path: str) -> float | None:
    """Return video duration in seconds using ffprobe, or None on failure."""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=20,
        )
        info = json.loads(probe.stdout)
        for s in info.get("streams", []):
            if s.get("codec_type") == "video":
                d = s.get("duration")
                if d:
                    return round(float(d), 3)
        d = info.get("format", {}).get("duration")
        return round(float(d), 3) if d else None
    except Exception:
        return None


def _probe_image_size(path: str) -> tuple[int, int] | None:
    """Return (width, height) of an image (or first video frame), or None on failure."""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-select_streams", "v:0", "-show_entries", "stream=width,height", path],
            capture_output=True, text=True, timeout=10,
        )
        info = json.loads(probe.stdout)
        streams = info.get("streams") or []
        if not streams:
            return None
        w, h = streams[0].get("width"), streams[0].get("height")
        if not w or not h:
            return None
        return int(w), int(h)
    except Exception:
        return None


def _crop_image_to_ratio(image_path: str, ratio: str) -> str:
    """Center-crop `image_path` so its width:height matches `ratio` (e.g. "9:16").
    Returns a new path on success; returns the original path if the image already
    matches the ratio, the ratio is invalid, or anything goes wrong (so callers can
    fall through safely without breaking the upstream CLI call)."""
    try:
        rw_str, rh_str = ratio.split(":", 1)
        rw, rh = float(rw_str), float(rh_str)
        if rw <= 0 or rh <= 0:
            return image_path
    except Exception:
        return image_path

    size = _probe_image_size(image_path)
    if not size:
        return image_path
    w, h = size
    target = rw / rh
    actual = w / h
    # Within 1% — leave alone so we don't waste a re-encode for trivial differences.
    if abs(actual - target) / target < 0.01:
        return image_path

    if actual > target:
        # Source is wider than target → crop sides
        new_w = int(round(h * target))
        new_w -= new_w % 2  # keep even for x264 friendliness
        new_h = h - (h % 2)
        x = (w - new_w) // 2
        y = 0
    else:
        # Source is taller than target → crop top/bottom
        new_h = int(round(w / target))
        new_h -= new_h % 2
        new_w = w - (w % 2)
        x = 0
        y = (h - new_h) // 2

    suffix = Path(image_path).suffix.lower() or ".jpg"
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".jpg"
    out_path = UPLOAD_DIR / f"crop_{uuid.uuid4()}{suffix}"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", image_path,
        "-vf", f"crop={new_w}:{new_h}:{x}:{y}",
        "-frames:v", "1",
        str(out_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
    except Exception as exc:
        print(f"[crop_image] ffmpeg failed for {image_path} → {ratio}: {exc}", flush=True)
        return image_path
    if not out_path.exists() or out_path.stat().st_size == 0:
        return image_path
    print(f"[crop_image] {image_path} ({w}x{h}) → {out_path.name} ({new_w}x{new_h}) for ratio {ratio}", flush=True)
    return str(out_path)


def _generate_edit_plan(clips: list[str], clip_meta: list[dict],
                        audio_path: str | None, instructions: str, api_key: str,
                        subtitle_path: str | None = None,
                        watermark_path: str | None = None,
                        pip_path: str | None = None) -> dict:
    clips_desc = "\n".join(
        f"  Clip {m['index']}: {m.get('duration', '?')}s"
        for m in clip_meta
    )
    extras = []
    if audio_path:     extras.append("Background audio: available (mixed at background_audio_volume)")
    if subtitle_path:  extras.append("Subtitle file: available (will be burned in automatically)")
    if watermark_path: extras.append("Watermark image: available (set 'watermark' to position it)")
    if pip_path:       extras.append("PIP source video: available (set clip's 'pip' to use it)")
    extras_line = ("\n" + "\n".join(extras)) if extras else ""

    schema_example = (
        '{"clips":['
        '{"index":0,"trim_start":0.0,"trim_end":null,"speed":1.0,"mute":false,'
        '"color":{"brightness":0.0,"contrast":1.0,"saturation":1.0,"gamma":1.0},'
        '"text":"Optional caption","text_position":"bottom","text_size":42,'
        '"text_start":0.0,"text_end":3.0,'
        '"pip":{"clip_index":1,"x":"main_w-overlay_w-20","y":"20","scale":0.25,"start":0.0,"end":3.0}'
        '}],'
        '"fade_in":0.3,"fade_out":0.0,'
        '"background_audio_volume":0.3,'
        '"transition":{"type":"fade","duration":0.5},'
        '"global_color":{"brightness":0.0,"contrast":1.05,"saturation":1.1},'
        '"watermark":{"x":"main_w-overlay_w-30","y":"main_h-overlay_h-30","scale":0.15,"opacity":0.85},'
        '"title":{"text":"Chapter One","duration":2.0,"size":72,"color":"white"}}'
    )

    rules = [
        "speed 0.25-4.0; trim_end null = use until end of clip",
        "include only the clips you want (can reorder or repeat)",
        "mute defaults to false — KEEP original audio unless told to remove it",
        "fade_in / fade_out in seconds (0 to skip)",
        "color fields: brightness -1..1, contrast 0..2, saturation 0..3, gamma 0.1..10. Omit any field that should stay neutral; omit the whole color object when nothing changes",
        "per-clip 'text' (or 'title') is a SHORT on-screen caption / lower-third / chapter card. text_position: 'top' | 'center' | 'bottom'. text_start/text_end are seconds from the start of THAT trimmed clip; omit both to display the whole time",
        "transition.type: fade | fadeblack | fadewhite | dissolve | wipeleft | wiperight | wipeup | wipedown | slideleft | slideright | slideup | slidedown | circleopen | circleclose. duration in seconds (typically 0.3-0.8). Omit the transition object for hard cuts",
        "global_color is applied AFTER concatenation to the whole video; use it for an overall look",
        "watermark uses the supplied watermark image — set x/y as ffmpeg overlay expressions (default puts it bottom-right with a 30px margin); only include this object when a watermark image is available",
        "pip puts another clip-or-PIP-source picture-in-picture on top of the current clip. clip_index references one of the input clips by index; omit clip_index to use the supplied PIP source",
        "title prepends a centered title card (black background) at the start of the final video",
    ]

    prompt = (
        "You are a professional video editor. Generate a video edit plan as JSON.\n\n"
        f"Available clips (0-indexed):\n{clips_desc}{extras_line}\n\n"
        f"Instructions: {instructions or 'Combine all clips into a smooth, cohesive final video.'}\n\n"
        "Output ONLY valid JSON — no markdown fences, no explanation. Schema example:\n"
        f"{schema_example}\n\n"
        "Rules:\n- " + "\n- ".join(rules) + "\n\n"
        "Only include fields you actually want to change from defaults. Keep the JSON compact."
    )
    payload = {
        "model": DOUBAO_MODEL,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    }
    resp = requests.post(
        DOUBAO_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    text = _extract_doubao_text(resp.json())
    m = _re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"No JSON in LLM response: {text[:300]}")
    plan = json.loads(m.group())
    if not isinstance(plan.get("clips"), list):
        raise ValueError("Plan missing 'clips' array")
    return plan


def _atempo_chain(speed: float) -> list[str]:
    """Build a chain of atempo filters for speeds outside the 0.5-2.0 range."""
    parts: list[str] = []
    s = speed
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5")
        s *= 2.0
    if abs(s - 1.0) > 0.01:
        parts.append(f"atempo={s:.4f}")
    return parts


# ── Edit-plan filter helpers ────────────────────────────────────────────────

_XFADE_TYPES = {
    "fade", "fadeblack", "fadewhite", "dissolve",
    "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown",
    "circleopen", "circleclose", "radial",
    "smoothleft", "smoothright", "smoothup", "smoothdown",
    "pixelize", "diagtl", "diagtr", "diagbl", "diagbr",
}


def _ffmpeg_escape_text(text: str) -> str:
    """Escape user-supplied text for the ffmpeg drawtext filter."""
    return (
        text.replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace("'", "’")  # smart quote — drawtext mangles raw apostrophes
            .replace(",", "\\,")
            .replace("[", "\\[")
            .replace("]", "\\]")
            .replace(";", "\\;")
            .replace("%", "\\%")
    )


def _eq_filter(color: dict | None) -> str | None:
    """Build an `eq=…` filter from a color dict, or None when there's nothing to do."""
    if not isinstance(color, dict):
        return None
    parts: list[str] = []
    for key, lo, hi, ident in (
        ("brightness", -1.0, 1.0, 0.0),
        ("contrast",    0.0, 2.0, 1.0),
        ("saturation",  0.0, 3.0, 1.0),
        ("gamma",       0.1, 10.0, 1.0),
    ):
        v = color.get(key)
        if v is None:
            continue
        try:
            v = max(lo, min(hi, float(v)))
        except (TypeError, ValueError):
            continue
        if abs(v - ident) < 1e-3:
            continue
        parts.append(f"{key}={v}")
    return f"eq={':'.join(parts)}" if parts else None


def _drawtext_filter(text: str, position: str = "bottom", size: int = 42,
                     color: str = "white", start: float | None = None,
                     end: float | None = None) -> str:
    """Build a drawtext filter for an on-screen caption / lower-third / title."""
    text_esc = _ffmpeg_escape_text(text)
    y_expr = {
        "top":    "max(40,h*0.07)",
        "center": "(h-text_h)/2",
        "bottom": "h-text_h-max(40,h*0.08)",
    }.get(position, "h-text_h-max(40,h*0.08)")
    color_safe = color.replace(":", "")
    # In filter_complex, "," and ":" inside an option value confuse the parser
    # — single-quote any expression that may contain them.
    parts = [
        f"text='{text_esc}'",
        "x='(w-text_w)/2'",
        f"y='{y_expr}'",
        f"fontsize={int(size)}",
        f"fontcolor={color_safe}",
        "borderw=3",
        "bordercolor=black@0.75",
    ]
    if start is not None and end is not None and end > start:
        parts.append(f"enable='between(t,{float(start):.3f},{float(end):.3f})'")
    return "drawtext=" + ":".join(parts)


def _execute_ffmpeg_edit(
    clips: list[str],
    audio_path: str | None,
    plan: dict,
    output_path: str,
    subtitle_path: str | None = None,
    watermark_path: str | None = None,
    pip_path: str | None = None,
) -> None:
    """Process clips per edit plan and write the final video to output_path.

    Stage 1 (per clip): trim, speed, color/eq, drawtext overlays, optional PIP.
    Stage 2 (combine):  concat -OR- xfade chain when transitions are configured.
    Stage 3 (finalize): subtitle burn-in, watermark overlay, fade in/out, bg audio mix.
    """
    import shutil
    import tempfile

    plan_clips = plan.get("clips") or []
    if not plan_clips:
        raise ValueError("Edit plan has no clips")

    _env = {**os.environ, "PATH": f"{os.environ.get('HOME', '')}/.local/bin:{os.environ.get('PATH', '')}"}

    def run_ff(cmd: list[str], timeout: int = 300) -> None:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_env)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "")[-800:])

    has_audio_stream = lambda p: bool(subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "a",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", p],
        capture_output=True, text=True, timeout=15, env=_env,
    ).stdout.strip())

    tmp = Path(tempfile.mkdtemp())
    try:
        processed: list[str] = []
        processed_durations: list[float] = []

        # ── Stage 1: per-clip processing ──────────────────────────────────
        for i, pc in enumerate(plan_clips):
            ci = int(pc.get("index", i))
            if ci >= len(clips) or not clips[ci]:
                continue
            src = clips[ci]
            if not Path(src).exists():
                raise ValueError(f"Clip file not found: {src}")

            trim_s = max(0.0, float(pc.get("trim_start") or 0))
            trim_e = pc.get("trim_end")
            speed = max(0.25, min(4.0, float(pc.get("speed") or 1.0)))
            mute = bool(pc.get("mute"))
            color = pc.get("color")
            text_overlay = pc.get("text") or pc.get("title")  # 'text' or 'title' both accepted
            text_pos = pc.get("text_position", "bottom")
            text_size = int(pc.get("text_size") or 42)
            pip = pc.get("pip") if isinstance(pc.get("pip"), dict) else None

            out_tmp = str(tmp / f"c{i:02d}.mp4")
            has_audio = has_audio_stream(src)

            # Build the per-clip filter chain on [0:v]
            v_filters: list[str] = []
            if abs(speed - 1.0) > 0.01:
                v_filters.append(f"setpts={1.0/speed:.6f}*PTS")
            eq = _eq_filter(color)
            if eq:
                v_filters.append(eq)
            if isinstance(text_overlay, str) and text_overlay.strip():
                v_filters.append(_drawtext_filter(
                    text_overlay.strip(), text_pos, text_size,
                    pc.get("text_color") or "white",
                    pc.get("text_start"), pc.get("text_end"),
                ))
            v_chain = ",".join(v_filters) if v_filters else None

            # PIP (picture-in-picture) — overlay either another clip or the
            # external pip_path on top of this clip.
            pip_input_path: str | None = None
            if pip:
                if pip.get("clip_index") is not None:
                    idx = int(pip["clip_index"])
                    if 0 <= idx < len(clips) and Path(clips[idx]).exists():
                        pip_input_path = clips[idx]
                elif pip_path and Path(pip_path).exists():
                    pip_input_path = pip_path

            cmd = ["ffmpeg", "-y", "-loglevel", "warning", "-ss", str(trim_s)]
            if trim_e is not None:
                cmd += ["-t", str(max(0.1, float(trim_e) - trim_s))]
            cmd += ["-i", src]

            needs_null_audio = mute or not has_audio
            anull_idx: int | None = None
            pip_idx: int | None = None
            if needs_null_audio:
                cmd += ["-f", "lavfi", "-i",
                        "anullsrc=channel_layout=stereo:sample_rate=44100"]
                anull_idx = 1
            if pip_input_path:
                cmd += ["-i", pip_input_path]
                pip_idx = (anull_idx + 1) if anull_idx is not None else 1

            # Build filter_complex.
            # NOTE: -map uses raw stream specs ("0:v", "1:a") for direct streams
            # and "[label]" only for filter outputs. Inside filter_complex, every
            # input must be bracketed.
            fc_parts: list[str] = []
            v_map = "0:v"   # for -map
            v_in  = "[0:v]" # for filter_complex input chaining
            if v_chain:
                fc_parts.append(f"[0:v]{v_chain}[vmain]")
                v_map = "[vmain]"; v_in = "[vmain]"
            if pip_idx is not None:
                pip_scale = float((pip or {}).get("scale") or 0.25)
                pip_x = (pip or {}).get("x") or "main_w-overlay_w-20"
                pip_y = (pip or {}).get("y") or "20"
                pip_start = (pip or {}).get("start")
                pip_end = (pip or {}).get("end")
                pip_enable = ""
                if pip_start is not None and pip_end is not None and float(pip_end) > float(pip_start):
                    pip_enable = f":enable='between(t,{float(pip_start):.3f},{float(pip_end):.3f})'"
                fc_parts.append(
                    f"[{pip_idx}:v]scale=iw*{pip_scale}:ih*{pip_scale}[pipv]"
                )
                fc_parts.append(f"{v_in}[pipv]overlay=x='{pip_x}':y='{pip_y}'{pip_enable}[vout]")
                v_map = "[vout]"; v_in = "[vout]"

            af_parts = _atempo_chain(speed) if not needs_null_audio else []
            a_map = f"{anull_idx}:a" if anull_idx is not None else "0:a"
            if af_parts:
                fc_parts.append(f"[0:a]{','.join(af_parts)}[aout]")
                a_map = "[aout]"

            if fc_parts:
                cmd += ["-filter_complex", ";".join(fc_parts),
                        "-map", v_map, "-map", a_map]
            else:
                cmd += ["-map", "0:v", "-map", a_map]

            cmd += [
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                "-shortest", "-movflags", "+faststart", out_tmp,
            ]
            run_ff(cmd)
            if Path(out_tmp).exists() and Path(out_tmp).stat().st_size > 0:
                processed.append(out_tmp)
                d = _probe_duration(out_tmp)
                processed_durations.append(float(d) if d else 0.0)

        if not processed:
            raise ValueError("No clips were processed successfully")

        # ── Plan-level options ────────────────────────────────────────────
        fade_in = max(0.0, float(plan.get("fade_in") or 0))
        fade_out = max(0.0, float(plan.get("fade_out") or 0))
        bg_vol = max(0.0, min(1.0, float(plan.get("background_audio_volume") or 0.3)))
        bg_audio = audio_path if (audio_path and Path(audio_path).exists()) else None
        global_eq = _eq_filter(plan.get("global_color"))
        sub_file = subtitle_path if (subtitle_path and Path(subtitle_path).exists()) else None
        wm_file = watermark_path if (watermark_path and Path(watermark_path).exists()) else None
        wm_cfg = plan.get("watermark") if isinstance(plan.get("watermark"), dict) else {}
        title_cfg = plan.get("title") if isinstance(plan.get("title"), dict) else None
        trans = plan.get("transition") if isinstance(plan.get("transition"), dict) else None
        trans_type = (trans or {}).get("type") if trans else None
        trans_dur = max(0.0, float((trans or {}).get("duration") or 0)) if trans else 0.0
        if trans_type and trans_type not in _XFADE_TYPES:
            trans_type = "fade"

        # ── Optional title card prepended before the clip chain ──────────
        if isinstance(title_cfg, dict) and title_cfg.get("text"):
            title_text = str(title_cfg.get("text"))
            title_dur = max(0.5, float(title_cfg.get("duration") or 2.0))
            title_size = int(title_cfg.get("size") or 64)
            title_color = title_cfg.get("color") or "white"
            # Match the first processed clip's resolution so concat/xfade works.
            ref_size = _probe_image_size(processed[0]) or (1920, 1080)
            tw, th = ref_size
            title_path = str(tmp / "title.mp4")
            run_ff([
                "ffmpeg", "-y", "-loglevel", "warning",
                "-f", "lavfi", "-i", f"color=c=black:s={tw}x{th}:d={title_dur}",
                "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100:d={title_dur}",
                "-vf", _drawtext_filter(title_text, "center", title_size, title_color),
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                "-shortest", "-pix_fmt", "yuv420p", title_path,
            ])
            if Path(title_path).exists():
                d = _probe_duration(title_path)
                processed.insert(0, title_path)
                processed_durations.insert(0, float(d) if d else title_dur)

        # ── Stage 2: combine clips (concat OR xfade chain) ────────────────
        n = len(processed)
        combined_path = str(tmp / "combined.mp4")
        if trans_type and trans_dur > 0 and n >= 2:
            # Build xfade/acrossfade chain, accumulating offsets.
            cmd = ["ffmpeg", "-y", "-loglevel", "warning"]
            for p in processed:
                cmd += ["-i", p]
            fc: list[str] = []
            v_prev = "[0:v]"
            a_prev = "[0:a]"
            running = processed_durations[0]
            for k in range(1, n):
                offset = max(0.0, running - trans_dur)
                v_out = f"[v{k}]" if k < n - 1 else "[vfin]"
                a_out = f"[a{k}]" if k < n - 1 else "[afin]"
                fc.append(
                    f"{v_prev}[{k}:v]xfade=transition={trans_type}"
                    f":duration={trans_dur:.3f}:offset={offset:.3f}{v_out}"
                )
                fc.append(f"{a_prev}[{k}:a]acrossfade=d={trans_dur:.3f}{a_out}")
                v_prev, a_prev = v_out, a_out
                running += processed_durations[k] - trans_dur
            cmd += ["-filter_complex", ";".join(fc),
                    "-map", "[vfin]", "-map", "[afin]",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", combined_path]
            run_ff(cmd, timeout=900)
        elif n == 1:
            shutil.copy2(processed[0], combined_path)
        else:
            cmd = ["ffmpeg", "-y", "-loglevel", "warning"]
            for p in processed:
                cmd += ["-i", p]
            concat_in = "".join(f"[{j}:v:0][{j}:a:0]" for j in range(n))
            fc = [f"{concat_in}concat=n={n}:v=1:a=1[v][a]"]
            cmd += ["-filter_complex", ";".join(fc),
                    "-map", "[v]", "-map", "[a]",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", combined_path]
            run_ff(cmd, timeout=900)

        # ── Stage 3: finalize (subtitles, watermark, eq, fades, bg audio) ─
        total_dur = _probe_duration(combined_path) or sum(processed_durations)
        cmd = ["ffmpeg", "-y", "-loglevel", "warning", "-i", combined_path]
        if bg_audio:
            cmd += ["-i", bg_audio]
            bg_idx = 1
        else:
            bg_idx = None
        if wm_file:
            cmd += ["-i", wm_file]
            wm_idx = bg_idx + 1 if bg_idx is not None else 1
        else:
            wm_idx = None

        fc: list[str] = []
        # v_in / a_in are the "current" labels used as filter_complex inputs
        # (always bracketed). v_map / a_map are what we pass to -map: a raw
        # stream specifier when nothing has filtered the stream yet, or the
        # last filter output's bracketed label otherwise.
        v_in,  a_in  = "[0:v]", "[0:a]"
        v_map, a_map = "0:v",  "0:a"
        if global_eq:
            fc.append(f"{v_in}{global_eq}[vge]")
            v_in = v_map = "[vge]"
        if sub_file:
            sub_safe = sub_file.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
            fc.append(f"{v_in}subtitles='{sub_safe}'[vsb]")
            v_in = v_map = "[vsb]"
        if wm_idx is not None:
            wm_scale = float(wm_cfg.get("scale") or 0.15)
            wm_x = wm_cfg.get("x") or "main_w-overlay_w-30"
            wm_y = wm_cfg.get("y") or "main_h-overlay_h-30"
            wm_opacity = max(0.0, min(1.0, float(wm_cfg.get("opacity") or 0.85)))
            fc.append(
                f"[{wm_idx}:v]scale=iw*{wm_scale}:-1,format=rgba,"
                f"colorchannelmixer=aa={wm_opacity}[wm]"
            )
            fc.append(f"{v_in}[wm]overlay=x='{wm_x}':y='{wm_y}'[vwm]")
            v_in = v_map = "[vwm]"
        if fade_in > 0:
            fc.append(f"{v_in}fade=t=in:st=0:d={fade_in:.2f}[vfi]")
            fc.append(f"{a_in}afade=t=in:st=0:d={fade_in:.2f}[afi]")
            v_in = v_map = "[vfi]"
            a_in = a_map = "[afi]"
        if fade_out > 0 and total_dur > fade_out:
            fo_st = max(0.0, total_dur - fade_out)
            fc.append(f"{v_in}fade=t=out:st={fo_st:.2f}:d={fade_out:.2f}[vfo]")
            fc.append(f"{a_in}afade=t=out:st={fo_st:.2f}:d={fade_out:.2f}[afo]")
            v_in = v_map = "[vfo]"
            a_in = a_map = "[afo]"
        if bg_idx is not None:
            fc.append(
                f"{a_in}[{bg_idx}:a]amix=inputs=2:duration=first:weights=1|{bg_vol:.2f}[amix]"
            )
            a_in = a_map = "[amix]"

        if fc:
            cmd += ["-filter_complex", ";".join(fc), "-map", v_map, "-map", a_map]
        else:
            cmd += ["-map", "0:v", "-map", "0:a"]

        cmd += [
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", output_path,
        ]
        run_ff(cmd, timeout=900)

        if not Path(output_path).exists():
            raise RuntimeError("FFmpeg produced no output file")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_video_edit_task(task_id: str, clips: list[str], audio_path: str | None,
                         instructions: str, api_key: str,
                         subtitle_path: str | None = None,
                         watermark_path: str | None = None,
                         pip_path: str | None = None) -> None:
    tasks[task_id]["status"] = "running"
    save_tasks()
    try:
        # Probe clip durations for the LLM
        clip_meta = [{"index": i, "duration": _probe_duration(p)} for i, p in enumerate(clips)]

        # Generate edit plan (LLM or fallback)
        edit_plan: dict | None = None
        if api_key:
            try:
                edit_plan = _generate_edit_plan(
                    clips, clip_meta, audio_path, instructions, api_key,
                    subtitle_path=subtitle_path,
                    watermark_path=watermark_path,
                    pip_path=pip_path,
                )
                print(f"[video_edit] LLM plan: {json.dumps(edit_plan)}", flush=True)
            except Exception as exc:
                print(f"[video_edit] LLM plan failed ({exc}); using default", flush=True)

        if not edit_plan:
            edit_plan = {
                "clips": [
                    {"index": i, "trim_start": 0,
                     "trim_end": m["duration"] if m.get("duration") else None,
                     "speed": 1.0, "mute": False}
                    for i, m in enumerate(clip_meta)
                ],
                "fade_in": 0.3,
                "fade_out": 0.0,
                "background_audio_volume": 0.3,
            }

        tasks[task_id]["edit_plan"] = edit_plan
        save_tasks()

        output_path = str(UPLOAD_DIR / f"edit_{uuid.uuid4()}.mp4")
        _execute_ffmpeg_edit(
            clips, audio_path, edit_plan, output_path,
            subtitle_path=subtitle_path,
            watermark_path=watermark_path,
            pip_path=pip_path,
        )

        serve_path = "/uploads/" + Path(output_path).name
        tasks[task_id]["status"] = "done"
        tasks[task_id]["result"] = {
            "url": serve_path,
            "serve_path": serve_path,
            "local_path": output_path,
            "edit_plan": edit_plan,
        }
        tasks[task_id]["error"] = None
        save_tasks()
    except Exception as exc:
        tasks[task_id]["status"] = "error"
        tasks[task_id]["error"] = str(exc)
        save_tasks()


# ── API: Upload and Workflow ──────────────────────────────────────────────────

@app.post("/api/upload")
def upload():
    """Generic file upload. Returns a URL the frontend can use as an input."""
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "file is required"}), 400
    filename = f"{uuid.uuid4()}_{f.filename}"
    save_path = UPLOAD_DIR / filename
    f.save(save_path)
    return jsonify({"ok": True, "url": f"/uploads/{filename}", "path": str(save_path)})


@app.get("/api/workflow")
def get_workflow():
    if WORKFLOW_FILE.exists():
        try:
            return jsonify({"ok": True, "workflow": json.loads(WORKFLOW_FILE.read_text())})
        except Exception:
            pass
    return jsonify({"ok": True, "workflow": {"nodes": {}, "edges": []}})


@app.put("/api/workflow")
def save_workflow():
    data = request.get_json(force=True)
    try:
        WORKFLOW_FILE.write_text(json.dumps(data, indent=2))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/multiframe2video")
def multiframe2video():
    prompt = request.form.get("prompt", "").strip()
    duration = request.form.get("duration", "5")
    first_frame = request.files.get("first_frame")
    last_frame = request.files.get("last_frame")
    if not first_frame or not last_frame:
        return jsonify({"ok": False, "error": "first_frame and last_frame are required"}), 400
    first_path = UPLOAD_DIR / f"{uuid.uuid4()}_{first_frame.filename}"
    last_path = UPLOAD_DIR / f"{uuid.uuid4()}_{last_frame.filename}"
    first_frame.save(first_path)
    last_frame.save(last_path)
    cmd = [
        "dreamina", "multiframe2video",
        f"--first_frame={first_path}",
        f"--last_frame={last_path}",
        f"--duration={duration}",
        "--poll=240",
    ]
    if prompt:
        cmd.append(f"--prompt={prompt}")
    return jsonify(_start_task(cmd, f"multiframe2video: {first_frame.filename} → {last_frame.filename}"))


@app.post("/api/video_edit")
def video_edit():
    """LLM-planned video editing.

    Supported plan operations: trim/speed/mute/concat, color grading (eq), per-clip
    text overlays, transitions (xfade), subtitle burn-in, watermark + picture-in-
    picture overlay, fades, optional background audio mix, and an optional title
    card prepended to the final video.
    """
    api_key = os.environ.get("ARK_API_KEY", "").strip()
    data = request.get_json(force=True)
    raw_clips = data.get("clips") or []
    audio_ref = (data.get("audio_path") or "").strip()
    subtitle_ref = (data.get("subtitle_path") or "").strip()
    watermark_ref = (data.get("watermark_path") or "").strip()
    pip_ref = (data.get("pip_path") or "").strip()
    instructions = (data.get("instructions") or "").strip()

    resolved: list[str] = []
    for c in raw_clips:
        ref = (c.get("path") or c.get("url") or "").strip()
        local = _resolve_clip_path(ref)
        if not local:
            return jsonify({"ok": False, "error": f"Clip not found: {ref}"}), 400
        resolved.append(local)

    if not resolved:
        return jsonify({"ok": False, "error": "At least one video clip is required"}), 400

    resolved_audio     = _resolve_clip_path(audio_ref)     if audio_ref     else None
    resolved_subtitle  = _resolve_clip_path(subtitle_ref)  if subtitle_ref  else None
    resolved_watermark = _resolve_clip_path(watermark_ref) if watermark_ref else None
    resolved_pip       = _resolve_clip_path(pip_ref)       if pip_ref       else None

    task_id = str(uuid.uuid4())
    tasks[task_id] = {
        "status": "queued",
        "label": f"video_edit: {len(resolved)} clip(s)",
        "result": None,
        "error": None,
        "created_at": time.time(),
    }
    save_tasks()
    threading.Thread(
        target=_run_video_edit_task,
        args=(task_id, resolved, resolved_audio, instructions, api_key),
        kwargs={
            "subtitle_path":  resolved_subtitle,
            "watermark_path": resolved_watermark,
            "pip_path":       resolved_pip,
        },
        daemon=True,
    ).start()
    return jsonify({"ok": True, "task_id": task_id})


# ── API: Projects ─────────────────────────────────────────────────────────────

def _project_view(pid: str, p: dict, me: str | None) -> dict:
    """Project payload returned to clients — includes the requester's role
    ('owner' | 'edit' | 'view' | None) so the UI can render the right
    affordances."""
    if p.get("owner") == me:
        role = "owner"
    else:
        role = _normalise_permission((p.get("shared_with") or {}).get(me))
    return {"id": pid, **p, "your_role": role}


@app.get("/api/projects")
def list_projects():
    me = current_user()
    visible = {
        pid: _project_view(pid, p, me)
        for pid, p in projects.items()
        if p.get("owner") == me or me in (p.get("shared_with") or {})
    }
    return jsonify({"ok": True, "projects": visible})


@app.post("/api/projects")
def create_project():
    me = current_user()
    if not me:
        return jsonify({"ok": False, "error": "Not signed in"}), 401
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name is required"}), 400
    pid = str(uuid.uuid4())
    now = time.time()
    projects[pid] = {
        "name": name,
        "description": data.get("description", ""),
        "tags": data.get("tags", []),
        "favorite": bool(data.get("favorite", False)),
        "archived": bool(data.get("archived", False)),
        "created_at": now,
        "updated_at": now,
        "owner": me,
        "shared_with": {},
    }
    _save_projects()

    # Optional deep-duplicate: copy the source project's workflow.json
    # (nodes / edges / projectInputs / projectVars / view) into the new
    # project so the duplicate is immediately useful as a fresh template.
    # Generated outputs are intentionally NOT copied — the new project
    # gets a clean slate so the user can re-run nodes without confusion
    # over which project a result actually belongs to. In-flight task
    # state is also stripped so the duplicate doesn't try to poll for
    # foreign tasks.
    source_pid = (data.get("copy_from") or "").strip()
    copy_status: str | None = None
    if source_pid:
        if source_pid not in projects:
            copy_status = f"source-missing:{source_pid}"
            print(f"[create_project] {copy_status}", flush=True)
        elif not user_can_view(source_pid):
            copy_status = f"no-view-access:{source_pid}"
            print(f"[create_project] {copy_status}", flush=True)
        else:
            src_path = WORKFLOWS_DIR / f"{source_pid}.json"
            src_size = src_path.stat().st_size if src_path.exists() else 0
            print(f"[create_project] source={source_pid} src_path={src_path} exists={src_path.exists()} size={src_size}", flush=True)
            if not src_path.exists():
                copy_status = f"source-workflow-empty:{source_pid}"
                print(f"[create_project] {copy_status} (no {src_path.name})", flush=True)
            else:
                try:
                    raw_text = src_path.read_text()
                    wf = json.loads(raw_text)
                    # nodes may be an array (current format) or an object
                    # keyed by id (legacy). Reset run-state on both shapes
                    # without changing the container type.
                    def _reset_node(n):
                        if not isinstance(n, dict):
                            return
                        n["status"] = "idle"
                        n["taskId"] = None
                        n["queueIdx"] = None
                        n["error"] = None
                        n["results"] = []
                    raw_nodes = wf.get("nodes")
                    if isinstance(raw_nodes, list):
                        for n in raw_nodes:
                            _reset_node(n)
                        node_count = len(raw_nodes)
                    elif isinstance(raw_nodes, dict):
                        for n in raw_nodes.values():
                            _reset_node(n)
                        node_count = len(raw_nodes)
                    else:
                        node_count = 0
                    dst_path = WORKFLOWS_DIR / f"{pid}.json"
                    dst_text = json.dumps(wf, indent=2)
                    dst_path.write_text(dst_text)
                    # Read back to verify the bytes actually landed. If the
                    # destination ends up with zero nodes (filesystem quirk,
                    # unexpected JSON shape, etc.) we surface that as a
                    # distinct status so the user sees "wrote-empty" instead
                    # of a confident "copied:N-nodes" lie.
                    try:
                        verify = json.loads(dst_path.read_text())
                        v_nodes = verify.get("nodes") or []
                        v_count = len(v_nodes) if isinstance(v_nodes, (list, dict)) else 0
                    except Exception as vexc:
                        v_count = -1
                        print(f"[create_project] dst readback parse failed: {vexc}", flush=True)
                    copy_status = f"copied:{node_count}-nodes" if node_count > 0 and v_count == node_count else (
                        f"empty-source:{node_count}-nodes" if node_count == 0 else f"readback-mismatch:src={node_count}-dst={v_count}"
                    )
                    print(f"[create_project] {source_pid} → {pid}: src={node_count} nodes (size={src_size}B), wrote dst (size={dst_path.stat().st_size}B), readback={v_count} nodes, edges={len(wf.get('edges') or [])}, inputs={len(wf.get('projectInputs') or [])}, vars={len(wf.get('projectVars') or [])}", flush=True)
                except Exception as exc:
                    copy_status = f"copy-failed:{exc}"
                    print(f"[create_project] copy_from={source_pid} failed: {exc}", flush=True)

    payload = {"ok": True, **_project_view(pid, projects[pid], me)}
    if copy_status is not None:
        payload["copy_status"] = copy_status
    return jsonify(payload)


@app.get("/api/projects/<pid>")
def get_project(pid: str):
    p = projects.get(pid)
    if not p:
        return jsonify({"ok": False, "error": "Project not found"}), 404
    if not user_can_read(pid):
        return jsonify({"ok": False, "error": "You don't have access to this project"}), 403
    return jsonify({"ok": True, **_project_view(pid, p, current_user())})


@app.put("/api/projects/<pid>")
def update_project(pid: str):
    p = projects.get(pid)
    if not p:
        return jsonify({"ok": False, "error": "Project not found"}), 404
    me = current_user()
    data = request.get_json(force=True)
    # 'favorite' / 'archived' are personal flags but for now stored on the
    # project — let any viewer toggle them. Everything else (name, tags,
    # description) requires edit access.
    metadata_keys = {"name", "description", "tags"}
    wants_metadata = any(k in data for k in metadata_keys)
    if wants_metadata and not user_can_edit(pid):
        return jsonify({"ok": False, "error": "You only have view access to this project"}), 403
    if not user_can_view(pid):
        return jsonify({"ok": False, "error": "You don't have access to this project"}), 403
    name = (data.get("name") or "").strip()
    if name and "name" in data:
        p["name"] = name
    if "description" in data:
        p["description"] = data["description"]
    if "tags" in data:
        p["tags"] = data["tags"]
    if "favorite" in data:
        p["favorite"] = bool(data["favorite"])
    if "archived" in data:
        p["archived"] = bool(data["archived"])
    p["updated_at"] = time.time()
    _save_projects()
    return jsonify({"ok": True, **_project_view(pid, p, me)})


@app.delete("/api/projects/<pid>")
def delete_project(pid: str):
    if pid not in projects:
        return jsonify({"ok": False, "error": "Project not found"}), 404
    if not user_owns(pid):
        return jsonify({"ok": False, "error": "Only the project owner can delete it"}), 403
    projects.pop(pid)
    _save_projects()
    wf_path = WORKFLOWS_DIR / f"{pid}.json"
    if wf_path.exists():
        wf_path.unlink()
    return jsonify({"ok": True})


@app.get("/api/projects/<pid>/workflow")
def get_project_workflow(pid: str):
    if pid not in projects:
        return jsonify({"ok": False, "error": "Project not found"}), 404
    if not user_can_view(pid):
        return jsonify({"ok": False, "error": "You don't have access to this project"}), 403
    wf_path = WORKFLOWS_DIR / f"{pid}.json"
    if wf_path.exists():
        try:
            return jsonify({"ok": True, "workflow": json.loads(wf_path.read_text())})
        except Exception:
            pass
    return jsonify({"ok": True, "workflow": {"nodes": {}, "edges": []}})


@app.post("/api/projects/<pid>/inputs/download")
def download_inputs_package(pid: str):
    """Bulk-download selected Input-tab entries as a single .zip.

    Client posts {"ids": [...]} where each id is either a projectInput id
    (file-typed variable) or a projectVar id (text variable). Files land in
    the archive under their original `name`; text variables collapse into a
    single variables.json. Read access on the project is required."""
    if pid not in projects:
        return jsonify({"ok": False, "error": "Project not found"}), 404
    if not user_can_view(pid):
        return jsonify({"ok": False, "error": "You don't have access to this project"}), 403

    payload = request.get_json(silent=True) or {}
    ids = [str(i) for i in (payload.get("ids") or []) if i]
    if not ids:
        return jsonify({"ok": False, "error": "ids is required"}), 400

    wf_path = WORKFLOWS_DIR / f"{pid}.json"
    if not wf_path.exists():
        return jsonify({"ok": False, "error": "No workflow saved for this project"}), 404
    try:
        wf = json.loads(wf_path.read_text())
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Workflow parse failed: {exc}"}), 500

    inputs_by_id = {e.get("id"): e for e in (wf.get("projectInputs") or []) if isinstance(e, dict) and e.get("id")}
    vars_by_id   = {v.get("id"): v for v in (wf.get("projectVars")  or []) if isinstance(v, dict) and v.get("id")}

    selected_files: list[tuple[Path, str]] = []
    selected_vars: dict[str, str] = {}
    used_names: set[str] = set()
    missing: list[str] = []

    def _unique(name: str) -> str:
        # Don't let two files share a name in the same zip — append " (N)"
        # before the suffix until we find a free slot.
        if name not in used_names:
            used_names.add(name)
            return name
        stem = Path(name).stem
        suffix = Path(name).suffix
        i = 2
        while True:
            cand = f"{stem} ({i}){suffix}"
            if cand not in used_names:
                used_names.add(cand)
                return cand
            i += 1

    for iid in ids:
        if iid in inputs_by_id:
            entry = inputs_by_id[iid]
            url_or_path = (entry.get("url") or entry.get("path") or "").strip()
            if not url_or_path:
                missing.append(iid)
                continue
            if url_or_path.startswith("/uploads/"):
                local = UPLOAD_DIR / url_or_path.removeprefix("/uploads/")
            elif url_or_path.startswith("http"):
                # External CDN URL — skip; bulk-fetching foreign hosts in a
                # download request would block and could fail mid-archive.
                missing.append(iid)
                continue
            else:
                local = Path(url_or_path)
            if not local.exists():
                missing.append(iid)
                continue
            zip_name = _unique((entry.get("name") or "").strip() or local.name)
            selected_files.append((local, zip_name))
        elif iid in vars_by_id:
            v = vars_by_id[iid]
            key = (v.get("key") or v.get("id") or "").strip()
            if key:
                selected_vars[key] = v.get("value", "")
        else:
            missing.append(iid)

    if not selected_files and not selected_vars:
        return jsonify({"ok": False, "error": "Nothing to download (selection had no resolvable files or variables)"}), 400

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for local, name in selected_files:
            try:
                zf.write(local, arcname=name)
            except Exception as exc:
                print(f"[download_inputs] zip add failed {local} → {name}: {exc}", flush=True)
        if selected_vars:
            zf.writestr("variables.json", json.dumps(selected_vars, indent=2, ensure_ascii=False))
    buf.seek(0)

    proj_name = (projects[pid].get("name") or "project").strip()
    # Strip anything outside word chars / CJK / space / dash to keep the
    # filename safe for Content-Disposition; Flask will utf-8-encode it.
    safe_name = re.sub(r"[^\w一-鿿\s\-]", "_", proj_name).strip("_ ") or "project"
    zip_filename = f"{safe_name}-inputs.zip"

    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=zip_filename,
        max_age=0,
    )


# Fields a view-only collaborator is allowed to update on each node when the
# workflow is saved. Everything else (prompt, params, position, title, upload
# slot, edges, etc.) is creative content and stays under the owner's control.
_VIEW_NODE_FIELDS = {"results", "taskId", "status", "error", "queueIdx", "progress"}


def _merge_workflow_for_view(existing: dict, incoming: dict) -> dict:
    """Build a workflow payload that respects view-only access:
       - projectInputs / projectVars are taken from the incoming payload (the
         user is allowed to edit those).
       - For each existing node, run-state fields (results, taskId, status,
         error, queueIdx, progress) are copied from the incoming payload —
         everything else (prompt, params, position, etc.) is kept as the
         owner had it.
       - Edges and node topology come from `existing`; the view user can't
         add or remove nodes or wires.
       - The view (pan/zoom) is left as the owner's saved copy.
    """
    if not isinstance(existing, dict):
        existing = {}
    if not isinstance(incoming, dict):
        incoming = {}
    merged = dict(existing)

    # Project-scope assets / variables: view users own these.
    if "projectInputs" in incoming:
        merged["projectInputs"] = incoming.get("projectInputs") or []
    if "projectVars" in incoming:
        merged["projectVars"] = incoming.get("projectVars") or []

    # Nodes: keep the existing list, only refresh run-state from incoming.
    existing_nodes = existing.get("nodes") or []
    incoming_nodes = incoming.get("nodes") or []
    incoming_by_id = {n.get("id"): n for n in incoming_nodes if isinstance(n, dict) and n.get("id")}
    out_nodes = []
    for n in existing_nodes:
        if not isinstance(n, dict):
            out_nodes.append(n); continue
        m = dict(n)
        inc = incoming_by_id.get(n.get("id"))
        if isinstance(inc, dict):
            for f in _VIEW_NODE_FIELDS:
                if f in inc:
                    m[f] = inc[f]
        out_nodes.append(m)
    merged["nodes"] = out_nodes
    return merged


@app.put("/api/projects/<pid>/workflow")
def save_project_workflow(pid: str):
    if pid not in projects:
        return jsonify({"ok": False, "error": "Project not found"}), 404
    if not user_can_view(pid):
        return jsonify({"ok": False, "error": "You don't have access to this project"}), 403
    data = request.get_json(force=True)
    wf_path = WORKFLOWS_DIR / f"{pid}.json"
    try:
        if not user_can_edit(pid):
            # View-only: merge with the existing workflow so the user can update
            # project inputs / variables and run-state but never the graph itself.
            try:
                existing = json.loads(wf_path.read_text()) if wf_path.exists() else {}
            except Exception:
                existing = {}
            data = _merge_workflow_for_view(existing, data)
        wf_path.write_text(json.dumps(data, indent=2))
        projects[pid]["updated_at"] = time.time()
        _save_projects()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Project sharing / ACL ─────────────────────────────────────────────────────

@app.get("/api/projects/<pid>/members")
def list_project_members(pid: str):
    p = projects.get(pid)
    if not p:
        return jsonify({"ok": False, "error": "Project not found"}), 404
    if not user_can_view(pid):
        return jsonify({"ok": False, "error": "You don't have access to this project"}), 403
    members = [
        {"email": email, "permission": _normalise_permission(perm) or "view"}
        for email, perm in (p.get("shared_with") or {}).items()
    ]
    me = current_user()
    your_role = "owner" if p.get("owner") == me else _normalise_permission((p.get("shared_with") or {}).get(me))
    return jsonify({
        "ok": True,
        "owner": p.get("owner"),
        "members": members,
        "your_role": your_role,
    })


@app.post("/api/projects/<pid>/members")
def share_project(pid: str):
    p = projects.get(pid)
    if not p:
        return jsonify({"ok": False, "error": "Project not found"}), 404
    if not user_owns(pid):
        return jsonify({"ok": False, "error": "Only the project owner can share it"}), 403
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()
    permission = _normalise_permission(data.get("permission") or "view")
    if not email:
        return jsonify({"ok": False, "error": "Email is required"}), 400
    if permission not in ("view", "edit"):
        return jsonify({"ok": False, "error": "Permission must be 'view' or 'edit'"}), 400
    if email == p.get("owner"):
        return jsonify({"ok": False, "error": "The owner already has full access"}), 400
    if email not in users:
        return jsonify({"ok": False, "error": "No account exists for that email"}), 404
    with _projects_lock:
        sw = p.setdefault("shared_with", {})
        sw[email] = permission
        p["updated_at"] = time.time()
        _save_projects()
    return jsonify({"ok": True, "email": email, "permission": permission})


@app.delete("/api/projects/<pid>/members/<email>")
def unshare_project(pid: str, email: str):
    p = projects.get(pid)
    if not p:
        return jsonify({"ok": False, "error": "Project not found"}), 404
    if not user_owns(pid):
        return jsonify({"ok": False, "error": "Only the project owner can unshare it"}), 403
    email = (email or "").strip().lower()
    with _projects_lock:
        sw = p.get("shared_with") or {}
        if email in sw:
            del sw[email]
            p["updated_at"] = time.time()
            _save_projects()
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"Starting Jimeng Video Generation UI...")
    print(f"Open http://localhost:{port} in your browser")
    app.run(host="0.0.0.0", port=port, debug=False)
