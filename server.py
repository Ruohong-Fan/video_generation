#!/usr/bin/env python3
"""
Flask backend for the Jimeng video generation web UI.
Wraps the `dreamina` CLI and exposes a simple REST API.
Tasks are persisted to tasks.json so history survives server restarts.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
import time
import uuid
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    request,
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
    "logout",          # POST /api/logout
    "whoami",          # GET  /api/me
    "static",          # /static/*
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



BASE_DIR = Path(__file__).resolve().parent

UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

TASKS_FILE = BASE_DIR / "tasks.json"
WORKFLOW_FILE = BASE_DIR / "workflow.json"
PROJECTS_FILE = BASE_DIR / "projects.json"
WORKFLOWS_DIR = BASE_DIR / "workflows"
WORKFLOWS_DIR.mkdir(exist_ok=True)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "15"))
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT_SECONDS", "21600"))  # 6 hours; <=0 disables timeout

# task_id -> {status, label, result, error, created_at}
tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()

# project_id -> {name, description, created_at, updated_at}
projects: dict[str, dict] = {}
_projects_lock = threading.Lock()


# ── Persistence ───────────────────────────────────────────────────────────────

def save_tasks():
    with _tasks_lock:
        try:
            TASKS_FILE.write_text(json.dumps(tasks, indent=2))
        except Exception:
            pass


def load_tasks():
    global tasks
    if TASKS_FILE.exists():
        try:
            tasks = json.loads(TASKS_FILE.read_text())
            # Mark any tasks that were mid-run as interrupted
            for t in tasks.values():
                if t.get("status") in ("running", "queued"):
                    t["status"] = "error"
                    t["error"] = "Server restarted while task was running"
            save_tasks()
        except Exception:
            tasks = {}


load_tasks()


def _save_projects():
    with _projects_lock:
        try:
            PROJECTS_FILE.write_text(json.dumps(projects, indent=2))
        except Exception:
            pass


def _load_projects():
    global projects
    if PROJECTS_FILE.exists():
        try:
            projects = json.loads(PROJECTS_FILE.read_text())
        except Exception:
            projects = {}


_load_projects()


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

def run_task(task_id: str, cmd: list[str]):
    tasks[task_id]["status"] = "running"
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
            save_tasks()
            return

    state = _task_state(result_data)

    if state == "done":
        tasks[task_id]["status"] = "done"
        tasks[task_id]["result"] = result_data
        tasks[task_id]["error"] = None
        tasks[task_id].pop("queue_idx", None)
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

    save_tasks()


def _start_task(cmd: list[str], label: str) -> dict:
    task_id = str(uuid.uuid4())
    tasks[task_id] = {
        "status": "queued",
        "label": label,
        "result": None,
        "error": None,
        "created_at": time.time(),
    }
    save_tasks()
    thread = threading.Thread(target=run_task, args=(task_id, cmd), daemon=True)
    thread.start()
    return {"ok": True, "task_id": task_id}


# ── Static files ──────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return send_from_directory("web", "projects.html")


@app.get("/workflow")
def workflow_view():
    return send_from_directory("web", "workflow.html")


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
    # Constant-time comparison so timing doesn't leak info about the secret.
    if not (secrets.compare_digest(email, LOGIN_EMAIL)
            and secrets.compare_digest(password, LOGIN_PASSWORD)):
        return jsonify({"ok": False, "error": "Those credentials don't match an active account."}), 401
    session["user"] = email
    session.permanent = remember
    return jsonify({"ok": True})


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
def whoami():
    return jsonify({"ok": True, "authed": is_authed(), "user": session.get("user")})


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
        return jsonify({"ok": False, "error": str(exc)}), 502

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


@app.get("/api/task/<task_id>")
def get_task(task_id: str):
    task = tasks.get(task_id)
    if not task:
        return jsonify({"ok": False, "error": "Task not found"}), 404
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

@app.post("/api/text2video")
def text2video():
    data = request.get_json(force=True)
    prompt = data.get("prompt", "").strip()
    model_version = data.get("model_version", "").strip()
    if not prompt:
        return jsonify({"ok": False, "error": "prompt is required"}), 400
    cmd = [
        "dreamina", "text2video",
        f"--prompt={prompt}",
        f"--duration={data.get('duration', '5')}",
        f"--ratio={data.get('ratio', '16:9')}",
        f"--video_resolution={data.get('resolution', '720P')}",
        "--poll=240",
    ]
    if model_version:
        cmd.append(f"--model_version={model_version}")
    return jsonify(_start_task(cmd, f"text2video: {prompt[:60]}"))


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
    cmd = [
        "dreamina", "text2audio",
        f"--prompt={prompt}",
        f"--duration={duration}",
        "--poll=120",
    ]
    return jsonify(_start_task(cmd, f"text2audio: {prompt[:60]}"))


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


def _download_media(url: str, hint_ext: str = "") -> str:
    """Download a CDN media URL into uploads/; return local path (or url on failure).

    Extension priority:
      1. Content-Type response header (most reliable for CDN files)
      2. Path component of the URL
      3. Caller-supplied hint_ext  (e.g. ".mp4" for video tasks)
      4. ".bin" as last resort (never misidentify video as image)
    """
    try:
        parsed = urlparse(url)
        url_suffix = Path(parsed.path).suffix.lower()
        if len(url_suffix) > 6:
            url_suffix = ""

        resp = requests.get(url, timeout=60, stream=True, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()

        ct = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        suffix = _MIME_TO_EXT.get(ct) or url_suffix or hint_ext or ".bin"

        local = UPLOAD_DIR / f"dl_{uuid.uuid4()}{suffix}"
        with open(local, "wb") as f:
            for chunk in resp.iter_content(65536):
                if chunk:
                    f.write(chunk)
        return str(local)
    except Exception:
        return url  # fall back to passing the URL directly


# Keep old name as alias so existing callers don't break
_download_image = _download_media


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
    model_version = _get_param(request, "model_version", "").strip()
    image_ref = _resolve_image_input(request)
    if not image_ref:
        return jsonify({"ok": False, "error": "image is required — upload failed or URL could not be downloaded (it may have expired)"}), 400
    cmd = [
        "dreamina", "image2video",
        f"--image={image_ref}",
        f"--duration={duration}",
        "--poll=240",
    ]
    if prompt:
        cmd.append(f"--prompt={prompt}")
    if model_version:
        cmd.append(f"--model_version={model_version}")
    return jsonify(_start_task(cmd, f"image2video: {prompt[:60] or image_ref[-40:]}"))


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


# ── API: Projects ─────────────────────────────────────────────────────────────

@app.get("/api/projects")
def list_projects():
    return jsonify({"ok": True, "projects": projects})


@app.post("/api/projects")
def create_project():
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
    }
    _save_projects()
    return jsonify({"ok": True, "id": pid, **projects[pid]})


@app.get("/api/projects/<pid>")
def get_project(pid: str):
    p = projects.get(pid)
    if not p:
        return jsonify({"ok": False, "error": "Project not found"}), 404
    return jsonify({"ok": True, "id": pid, **p})


@app.put("/api/projects/<pid>")
def update_project(pid: str):
    p = projects.get(pid)
    if not p:
        return jsonify({"ok": False, "error": "Project not found"}), 404
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if name:
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
    return jsonify({"ok": True, "id": pid, **p})


@app.delete("/api/projects/<pid>")
def delete_project(pid: str):
    if pid not in projects:
        return jsonify({"ok": False, "error": "Project not found"}), 404
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
    wf_path = WORKFLOWS_DIR / f"{pid}.json"
    if wf_path.exists():
        try:
            return jsonify({"ok": True, "workflow": json.loads(wf_path.read_text())})
        except Exception:
            pass
    return jsonify({"ok": True, "workflow": {"nodes": {}, "edges": []}})


@app.put("/api/projects/<pid>/workflow")
def save_project_workflow(pid: str):
    if pid not in projects:
        return jsonify({"ok": False, "error": "Project not found"}), 404
    data = request.get_json(force=True)
    try:
        (WORKFLOWS_DIR / f"{pid}.json").write_text(json.dumps(data, indent=2))
        projects[pid]["updated_at"] = time.time()
        _save_projects()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"Starting Jimeng Video Generation UI...")
    print(f"Open http://localhost:{port} in your browser")
    app.run(host="0.0.0.0", port=port, debug=False)
