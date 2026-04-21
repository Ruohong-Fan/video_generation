#!/usr/bin/env python3
"""
Flask backend for the Jimeng video generation web UI.
Wraps the `dreamina` CLI and exposes a simple REST API.
Tasks are persisted to tasks.json so history survives server restarts.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context
from flask_cors import CORS

app = Flask(__name__, static_folder="web")
CORS(app)

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

TASKS_FILE = Path("tasks.json")
WORKFLOW_FILE = Path("workflow.json")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "15"))
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT_SECONDS", "21600"))  # 6 hours; <=0 disables timeout

# task_id -> {status, label, result, error, created_at}
tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()


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
    return send_from_directory("web", "workflow.html")


@app.get("/workflow")
def workflow_view():
    return send_from_directory("web", "workflow.html")


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
    upload_path = parsed.path if parsed.scheme in {"http", "https"} else str(image_ref)
    if upload_path.startswith("/uploads/"):
        local_path = UPLOAD_DIR / upload_path.removeprefix("/uploads/")
        if local_path.exists():
            return str(local_path)

    return str(image_ref)


def _get_param(request, key: str, default=""):
    if request.content_type and "multipart" in request.content_type:
        return request.form.get(key, default)
    data = request.get_json(silent=True) or {}
    return data.get(key, default)


@app.post("/api/image2video")
def image2video():
    prompt = _get_param(request, "prompt", "").strip()
    duration = _get_param(request, "duration", "5")
    model_version = _get_param(request, "model_version", "").strip()
    image_ref = _resolve_image_input(request)
    if not image_ref:
        return jsonify({"ok": False, "error": "image or image_url is required"}), 400
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"Starting Jimeng Video Generation UI...")
    print(f"Open http://localhost:{port} in your browser")
    app.run(host="0.0.0.0", port=port, debug=False)
