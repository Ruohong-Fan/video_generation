#!/usr/bin/env python3
"""
Flask backend for the Jimeng video generation web UI.
Wraps the `dreamina` CLI and exposes a simple REST API.
"""

import json
import os
import subprocess
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder="web")
CORS(app)

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

# In-memory task store: task_id -> {status, result, error, command, created_at}
tasks: dict[str, dict] = {}


def run_command(cmd: list[str]) -> tuple[bool, str]:
    """Run a dreamina CLI command and return (success, output)."""
    env = {**os.environ, "PATH": f"{os.environ.get('HOME', '')}/.local/bin:{os.environ.get('PATH', '')}"}
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        output = result.stdout.strip() or result.stderr.strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "Command timed out after 5 minutes"
    except FileNotFoundError:
        return False, "dreamina CLI not found. Run setup.sh to install."
    except Exception as e:
        return False, str(e)


import time

POLL_INTERVAL = 15   # seconds between query_result calls
POLL_TIMEOUT  = 1800 # give up after 30 minutes


def run_task(task_id: str, cmd: list[str]):
    """Execute a dreamina command in a background thread and store the result.

    If the initial command returns gen_status='querying' (task still queued),
    we keep polling with `dreamina query_result` until the task finishes or
    POLL_TIMEOUT is reached.
    """
    tasks[task_id]["status"] = "running"
    success, output = run_command(cmd)

    result_data = None
    try:
        result_data = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        result_data = {"raw": output}

    # If dreamina returned a submit_id but the task is still queued, keep polling
    submit_id = result_data.get("submit_id") if isinstance(result_data, dict) else None
    gen_status = result_data.get("gen_status", "") if isinstance(result_data, dict) else ""

    if submit_id and gen_status in ("querying", "processing", "waiting"):
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            queue_info = result_data.get("queue_info", {})
            queue_idx = queue_info.get("queue_idx", "?")
            tasks[task_id]["status"] = "running"
            tasks[task_id]["result"] = result_data  # show live queue position
            tasks[task_id]["queue"] = f"Queue position: {queue_idx}"

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
        tasks[task_id].pop("queue", None)
    else:
        tasks[task_id]["status"] = "error"
        tasks[task_id]["error"] = output if not submit_id else f"Timed out or failed (submit_id={submit_id})"
        tasks[task_id]["result"] = result_data


# ── Static files ─────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return send_from_directory("web", "index.html")


@app.get("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# ── API: Account ─────────────────────────────────────────────────────────────

@app.get("/api/credits")
def get_credits():
    success, output = run_command(["dreamina", "user_credit"])
    return jsonify({"ok": success, "output": output})


# ── API: Task status ──────────────────────────────────────────────────────────

@app.get("/api/task/<task_id>")
def get_task(task_id: str):
    task = tasks.get(task_id)
    if not task:
        return jsonify({"ok": False, "error": "Task not found"}), 404
    return jsonify({"ok": True, **task})


@app.get("/api/tasks")
def list_tasks():
    return jsonify({"ok": True, "tasks": tasks})


@app.get("/api/dreamina/tasks")
def list_dreamina_tasks():
    success, output = run_command(["dreamina", "list_task"])
    return jsonify({"ok": success, "output": output})


@app.get("/api/query/<submit_id>")
def query_result(submit_id: str):
    success, output = run_command(["dreamina", "query_result", f"--submit_id={submit_id}"])
    try:
        data = json.loads(output)
    except Exception:
        data = {"raw": output}
    return jsonify({"ok": success, "data": data})


# ── API: Generation ───────────────────────────────────────────────────────────

def _start_task(cmd: list[str], label: str) -> dict:
    task_id = str(uuid.uuid4())
    tasks[task_id] = {"status": "queued", "label": label, "result": None, "error": None}
    thread = threading.Thread(target=run_task, args=(task_id, cmd), daemon=True)
    thread.start()
    return {"ok": True, "task_id": task_id}


@app.post("/api/text2video")
def text2video():
    data = request.get_json(force=True)
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"ok": False, "error": "prompt is required"}), 400

    duration = str(data.get("duration", "5"))
    ratio = data.get("ratio", "16:9")
    resolution = data.get("resolution", "720P")

    cmd = [
        "dreamina", "text2video",
        f"--prompt={prompt}",
        f"--duration={duration}",
        f"--ratio={ratio}",
        f"--video_resolution={resolution}",
        "--poll=240",
    ]
    return jsonify(_start_task(cmd, f"text2video: {prompt[:60]}"))


@app.post("/api/text2image")
def text2image():
    data = request.get_json(force=True)
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"ok": False, "error": "prompt is required"}), 400

    ratio = data.get("ratio", "16:9")
    resolution = data.get("resolution", "2k")

    cmd = [
        "dreamina", "text2image",
        f"--prompt={prompt}",
        f"--ratio={ratio}",
        f"--resolution_type={resolution}",
        "--poll=90",
    ]
    return jsonify(_start_task(cmd, f"text2image: {prompt[:60]}"))


@app.post("/api/image2video")
def image2video():
    prompt = request.form.get("prompt", "").strip()
    duration = request.form.get("duration", "5")
    image_file = request.files.get("image")

    if not image_file:
        return jsonify({"ok": False, "error": "image file is required"}), 400

    filename = f"{uuid.uuid4()}_{image_file.filename}"
    save_path = UPLOAD_DIR / filename
    image_file.save(save_path)

    cmd = [
        "dreamina", "image2video",
        f"--image={save_path}",
        f"--duration={duration}",
        "--poll=240",
    ]
    if prompt:
        cmd.append(f"--prompt={prompt}")

    return jsonify(_start_task(cmd, f"image2video: {image_file.filename}"))


@app.post("/api/image2image")
def image2image():
    prompt = request.form.get("prompt", "").strip()
    ratio = request.form.get("ratio", "")
    image_file = request.files.get("image")

    if not image_file:
        return jsonify({"ok": False, "error": "image file is required"}), 400
    if not prompt:
        return jsonify({"ok": False, "error": "prompt is required"}), 400

    filename = f"{uuid.uuid4()}_{image_file.filename}"
    save_path = UPLOAD_DIR / filename
    image_file.save(save_path)

    cmd = [
        "dreamina", "image2image",
        f"--images={save_path}",
        f"--prompt={prompt}",
        "--poll=90",
    ]
    if ratio:
        cmd.append(f"--ratio={ratio}")

    return jsonify(_start_task(cmd, f"image2image: {prompt[:60]}"))


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
    port = int(os.environ.get("PORT", 5000))
    print(f"Starting Jimeng Video Generation UI...")
    print(f"Open http://localhost:{port} in your browser")
    app.run(host="0.0.0.0", port=port, debug=False)
