# Jimeng CLI — Full Command Reference

The Jimeng CLI binary is named `dreamina`. Install via:
```bash
curl -s https://jimeng.jianying.com/cli | bash
```

---

## Account Commands

### `dreamina login`
Authenticate using browser-based OAuth. Opens a browser window (requires Chrome). In headless environments, follow the manual credential import instructions.

### `dreamina relogin`
Re-authenticate if your session has expired.

### `dreamina logout`
Clear stored credentials from `~/.dreamina_cli/`.

### `dreamina user_credit`
Display your current credit balance.

---

## Generation Commands

All generation commands support these universal flags:
- `--poll=<seconds>` — Wait up to N seconds for the task to complete (polls every second). Omit for immediate async return with a `submit_id`.

---

### `dreamina text2image`
Generate a still image from a text prompt.

| Flag | Required | Values | Default |
|---|---|---|---|
| `--prompt` | yes | any string | — |
| `--ratio` | no | `1:1` `16:9` `9:16` `4:3` `3:4` | `1:1` |
| `--resolution_type` | no | `1k` `2k` `4k` | `2k` |
| `--poll` | no | seconds (int) | async |

**Example:**
```bash
dreamina text2image \
  --prompt="A misty mountain lake at dawn, photorealistic, golden light" \
  --ratio=16:9 \
  --resolution_type=2k \
  --poll=60
```

---

### `dreamina text2video`
Generate a video clip from a text prompt.

| Flag | Required | Values | Default |
|---|---|---|---|
| `--prompt` | yes | any string | — |
| `--duration` | no | `5` `10` | `5` |
| `--ratio` | no | `16:9` `9:16` `1:1` | `16:9` |
| `--video_resolution` | no | `480P` `720P` `1080P` | `720P` |
| `--poll` | no | seconds (int) | async |

**Example:**
```bash
dreamina text2video \
  --prompt="Camera slowly pushes in on a fox sitting in a snowy forest, cinematic, 4K" \
  --duration=5 \
  --ratio=16:9 \
  --video_resolution=720P \
  --poll=120
```

---

### `dreamina image2video`
Animate an existing image into a video clip.

| Flag | Required | Values | Default |
|---|---|---|---|
| `--image` | yes | local path or URL | — |
| `--prompt` | no | motion description | — |
| `--duration` | no | `5` `10` | `5` |
| `--poll` | no | seconds (int) | async |

**Example:**
```bash
dreamina image2video \
  --image ./photo.jpg \
  --prompt="Gentle camera pan right, leaves rustle softly in the breeze" \
  --duration=5 \
  --poll=120
```

---

### `dreamina image2image`
Transform or restyle an image based on a prompt.

| Flag | Required | Values | Default |
|---|---|---|---|
| `--images` | yes | local path or URL | — |
| `--prompt` | yes | transformation description | — |
| `--ratio` | no | `1:1` `16:9` `9:16` | source ratio |
| `--resolution_type` | no | `1k` `2k` `4k` | `2k` |
| `--poll` | no | seconds (int) | async |

**Example:**
```bash
dreamina image2image \
  --images ./portrait.jpg \
  --prompt="Oil painting style, impressionist, warm tones" \
  --poll=60
```

---

### `dreamina multiframe2video`
Generate a video that transitions between two keyframe images.

| Flag | Required | Values | Default |
|---|---|---|---|
| `--first_frame` | yes | local path or URL | — |
| `--last_frame` | yes | local path or URL | — |
| `--prompt` | no | scene/motion description | — |
| `--duration` | no | `5` `10` | `5` |
| `--poll` | no | seconds (int) | async |

**Example:**
```bash
dreamina multiframe2video \
  --first_frame ./start.png \
  --last_frame ./end.png \
  --prompt="Smooth transition, cinematic lighting" \
  --duration=5 \
  --poll=120
```

---

### `dreamina multimodal2video`
Generate video using combined text and image inputs.

```bash
dreamina multimodal2video \
  --image ./reference.png \
  --prompt="A futuristic cityscape at night, neon lights, rain" \
  --duration=5 \
  --poll=120
```

---

## Task Management Commands

### `dreamina query_result`
Fetch the result of an async (non-polled) task.

```bash
dreamina query_result --submit_id=<id>
```

### `dreamina list_task`
List recent generation tasks with their status and IDs.

```bash
dreamina list_task
```

### `dreamina list_capabilities`
Show all available models and feature flags for your account.

```bash
dreamina list_capabilities
```

---

## Response Format

Successful responses return JSON:
```json
{
  "ok": true,
  "submit_id": "abc123",
  "status": "done",
  "data": {
    "url": "https://...",
    "local_path": "/path/to/output.mp4"
  }
}
```

Async responses (no `--poll`) return:
```json
{
  "ok": true,
  "submit_id": "abc123",
  "status": "queued"
}
```
