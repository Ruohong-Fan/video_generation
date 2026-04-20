# Video Generation — Jimeng CLI Workflow

This repository contains a Claude Code skill and helper scripts for AI video and image generation using the **Jimeng CLI** (`dreamina`), ByteDance's official command-line tool for the Jimeng/Dreamina platform.

## What This Does

- Generates AI images via Jimeng 4.0+ (text-to-image, image-to-image)
- Generates AI videos via Seedance 2.0 (text-to-video, image-to-video, multi-frame-to-video)
- Provides a reusable Claude Code skill (`SKILL.md`) that agents load to handle generation requests

## Quick Start

```bash
# 1. Install the Jimeng CLI
bash setup.sh

# 2. Log in to your Jimeng account
dreamina login

# 3. Check your credit balance
dreamina user_credit

# 4. Generate something
dreamina text2video --prompt="A cat surfing a wave, cinematic, slow motion" --duration=5 --ratio=16:9 --video_resolution=720P --poll=120
```

## Repository Structure

```
video_generation/
├── CLAUDE.md              # This file
├── SKILL.md               # Claude Code skill definition
├── setup.sh               # CLI installer + environment check
├── scripts/
│   ├── text2video.sh      # Text-to-video helper
│   ├── image2video.sh     # Image-to-video helper
│   ├── text2image.sh      # Text-to-image helper
│   └── query.sh           # Poll async task results
└── references/
    ├── commands.md        # Full CLI reference
    └── prompting.md       # Prompting guide for quality results
```

## CLI Authentication

The CLI uses browser-based OAuth. Run `dreamina login` and follow the prompt. Credentials are stored in `~/.dreamina_cli/`. In headless environments, follow the manual credential import instructions shown on screen.

## Key CLI Commands

| Command | Purpose |
|---|---|
| `dreamina login` | Authenticate with Jimeng account |
| `dreamina user_credit` | Check remaining generation credits |
| `dreamina text2image` | Generate image from text prompt |
| `dreamina text2video` | Generate video from text prompt |
| `dreamina image2video` | Animate an existing image |
| `dreamina image2image` | Transform/restyle an image |
| `dreamina multiframe2video` | Generate video between two keyframes |
| `dreamina query_result` | Fetch result of an async task |
| `dreamina list_task` | List recent generation tasks |

## Models

- **Text-to-image**: Jimeng 4.0, 4.5, 5.0
- **Video generation**: Seedance 2.0 (5s/10s, up to 1080P)

## Notes

- Free trial credits: ~66 credits/day for Jimeng advanced members
- Video generation typically takes 30–120 seconds
- Use `--poll=<seconds>` for synchronous waiting; omit for async with `submit_id`
- Content must comply with Jimeng's content policy
