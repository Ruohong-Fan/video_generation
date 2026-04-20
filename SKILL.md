---
name: jimeng-video-generation
description: Generate AI videos and images using the Jimeng (Dreamina) CLI. Use this skill when the user asks to create a video, animate an image, generate an image, or produce AI-generated visual media. Triggers on phrases like "generate a video of", "create an image of", "animate this image", "make a video", "text to video", "image to video".
---

# Jimeng Video Generation Skill

Generate AI images and videos using the Jimeng CLI (`dreamina` command), powered by Jimeng 4.0+ (text-to-image) and Seedance 2.0 (video generation).

## Prerequisites

1. Jimeng CLI installed (run `setup.sh` or install manually):
   ```bash
   curl -s https://jimeng.jianying.com/cli | bash
   ```
2. Logged in to your Jimeng account:
   ```bash
   dreamina login
   ```
3. Sufficient credits (`dreamina user_credit` to check balance).

## Workflow

### Step 1 — Understand the request
Identify what the user wants:
- **Text → Image**: a still image from a description
- **Text → Video**: a video clip from a description
- **Image → Video**: animate an existing image
- **Image → Image**: transform/restyle an image

### Step 2 — Check CLI availability
```bash
which dreamina || echo "NOT_INSTALLED"
dreamina user_credit
```
If not installed, run `bash setup.sh` first.

### Step 3 — Generate

#### Text to Image
```bash
dreamina text2image \
  --prompt="<detailed prompt>" \
  --ratio=<ratio> \
  --resolution_type=<resolution> \
  --poll=60
```
- `--ratio`: `1:1` | `16:9` | `9:16` | `4:3` | `3:4`
- `--resolution_type`: `1k` | `2k` | `4k` (default: `2k`)

#### Text to Video
```bash
dreamina text2video \
  --prompt="<detailed prompt>" \
  --duration=<seconds> \
  --ratio=<ratio> \
  --video_resolution=<res> \
  --poll=120
```
- `--duration`: `5` | `10` (seconds)
- `--ratio`: `16:9` | `9:16` | `1:1`
- `--video_resolution`: `480P` | `720P` | `1080P`

#### Image to Video
```bash
dreamina image2video \
  --image <path_or_url> \
  --prompt="<motion description>" \
  --duration=5 \
  --poll=120
```

#### Image to Image
```bash
dreamina image2image \
  --images <path_or_url> \
  --prompt="<transformation description>" \
  --poll=60
```

#### Multi-frame to Video
```bash
dreamina multiframe2video \
  --first_frame <path> \
  --last_frame <path> \
  --prompt="<scene description>" \
  --duration=5 \
  --poll=120
```

### Step 4 — Handle async results
If a command returns a `submit_id` instead of a direct result (poll timed out):
```bash
dreamina query_result --submit_id=<id>
```
Retry every 15–30 seconds until status is `done`.

### Step 5 — Present results
- Show the output file path or URL to the user.
- Confirm the file was saved locally if applicable.
- Offer to refine with a follow-up generation.

## Prompting Guidelines

For best results, prompts should be descriptive and specific:

**Images**: `"<subject>, <style>, <lighting>, <composition>, <mood>"`
> `"A lone astronaut on a red rocky planet, cinematic wide shot, golden hour lighting, dust particles in atmosphere, photorealistic"`

**Videos**: Lead with camera movement, then subject action, then environment:
> `"Camera slowly pushes in, a red fox leaps through a snowy forest, slow motion, cinematic, 4K"`

**Image-to-video**: Describe the motion/animation, not the static scene:
> `"Gentle camera pan left, leaves rustle softly in the breeze"`

## Task Management

Use `dreamina list_task` to view recent generation history and retrieve past results.

## Troubleshooting

| Error | Fix |
|---|---|
| `not logged in` | Run `dreamina login` or `dreamina relogin` |
| `insufficient credits` | Check `dreamina user_credit`; top up account |
| `AigcComplianceConfirmationRequired` | Visit jimeng.jianying.com web app and confirm content policy |
| `command not found: dreamina` | Run `bash setup.sh` to install |
| Poll timeout (task still queued) | Run `dreamina query_result --submit_id=<id>` |
| Chrome not found (login) | Follow on-screen manual credential import instructions |

## References

- [Full CLI command reference](references/commands.md)
- [Video prompting guide](references/prompting.md)
