#!/usr/bin/env python3
"""
Flask backend for the Jimeng video generation web UI.
Wraps the `dreamina` CLI and exposes a simple REST API.
Tasks are persisted to tasks.json so history survives server restarts.
"""

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder="web")
CORS(app)

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

TASKS_FILE = Path("tasks.json")
WORKFLOW_FILE = Path("workflow.json")
POLL_INTERVAL = 15    # seconds between query_result calls
POLL_TIMEOUT  = 1800  # give up after 30 minutes

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


# ── Task runner ───────────────────────────────────────────────────────────────

def run_task(task_id: str, cmd: list[str]):
    tasks[task_id]["status"] = "running"
    save_tasks()

    success, output = run_command(cmd)

    result_data = None
    try:
        result_data = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        result_data = {"raw": output}

    submit_id = result_data.get("submit_id") if isinstance(result_data, dict) else None
    gen_status = result_data.get("gen_status", "") if isinstance(result_data, dict) else ""

    # Keep polling if task is still queued/processing
    if submit_id and gen_status in ("querying", "processing", "waiting"):
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            queue_info = result_data.get("queue_info", {})
            tasks[task_id]["result"] = result_data
            tasks[task_id]["queue_idx"] = queue_info.get("queue_idx")
            save_tasks()

            time.sleep(POLL_INTERVAL)

            ok, poll_output = run_command(["dreamina", "query_result", f"--submit_id={submit_id}"])
            try:
                result_data = json.loads(poll_output)
            except (json.JSONDecodeError, ValueError):
                result_data = {"raw": poll_output}

            gen_status = result_data.get("gen_status", "") if isinstance(result_data, dict) else ""
            if gen_status not in ("querying", "processing", "waiting"):
                break

        success = gen_status == "done"

    if success or (isinstance(result_data, dict) and result_data.get("gen_status") == "done"):
        tasks[task_id]["status"] = "done"
        tasks[task_id]["result"] = result_data
        tasks[task_id].pop("queue_idx", None)
    else:
        tasks[task_id]["status"] = "error"
        tasks[task_id]["error"] = (
            output if not submit_id
            else f"Timed out or failed. submit_id={submit_id}"
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
    return send_from_directory("web", "index.html")


@app.get("/workflow")
def workflow_view():
    return send_from_directory("web", "workflow.html")


@app.get("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


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
    return data.get("image_url") or data.get("image") or None


def _get_param(request, key: str, default=""):
    if request.content_type and "multipart" in request.content_type:
        return request.form.get(key, default)
    data = request.get_json(silent=True) or {}
    return data.get(key, default)


@app.post("/api/image2video")
def image2video():
    prompt = _get_param(request, "prompt", "").strip()
    duration = _get_param(request, "duration", "5")
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
