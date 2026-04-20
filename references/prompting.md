# Jimeng Video & Image Prompting Guide

Effective prompts are the single biggest factor in output quality. This guide covers patterns for Seedance 2.0 (video) and Jimeng 4.x (image).

---

## General Principles

1. **Be specific** — vague prompts produce mediocre, generic results.
2. **Front-load the most important element** — the model weighs earlier tokens more heavily.
3. **Separate concerns** — subject, action, style, camera, lighting.
4. **English or Chinese both work** — Jimeng is optimized for both; Chinese may yield more natural results for Chinese-aesthetic content.

---

## Image Prompts (Jimeng 4.x)

### Formula
```
[Subject], [Action/Pose], [Environment], [Style], [Lighting], [Camera/Composition], [Mood]
```

### Examples

**Photorealistic:**
```
A lone lighthouse on a rocky cliff, waves crashing below, stormy sky, photorealistic, dramatic side lighting, wide angle shot, moody and cinematic
```

**Illustration:**
```
A tiny dragon sleeping on a pile of glowing crystals in a cave, fantasy art, soft ambient light, detailed scales, Studio Ghibli inspired
```

**Portrait:**
```
Close-up portrait of an elderly fisherman, weathered face, warm golden hour light, shallow depth of field, film photography style, Canon 85mm
```

**Product:**
```
A sleek black smartwatch on a white marble surface, studio lighting, clean background, commercial photography, 8K
```

### Useful Style Keywords
- Photorealism: `photorealistic`, `RAW photo`, `8K`, `DSLR`, `natural lighting`
- Art styles: `oil painting`, `watercolor`, `pencil sketch`, `Studio Ghibli`, `concept art`
- Cinema: `cinematic`, `anamorphic lens`, `film grain`, `35mm`
- Quality boosters: `highly detailed`, `masterpiece`, `sharp focus`, `award-winning`

---

## Video Prompts (Seedance 2.0)

### Formula
```
[Camera movement], [Subject] [Action], [Environment], [Style], [Mood/Tone]
```

Camera movement should usually come **first** — Seedance 2.0 responds well to leading with cinematography.

### Camera Movement Keywords
| Motion | Keywords |
|---|---|
| Push in | `camera slowly pushes in`, `zoom in gradually` |
| Pull out | `camera pulls back to reveal`, `slow zoom out` |
| Pan | `camera pans left/right`, `slow horizontal pan` |
| Tilt | `camera tilts up to reveal sky`, `tilt down` |
| Orbit | `camera orbits around subject`, `360 tracking shot` |
| Handheld | `handheld camera movement`, `slight camera shake` |
| Static | `static shot`, `locked camera`, `tripod shot` |
| Drone | `aerial drone shot`, `bird's eye view descending` |

### Examples

**Nature:**
```
Camera slowly pushes in on a waterfall in a lush green jungle, mist rising, golden morning light, cinematic, 4K, peaceful
```

**Urban:**
```
Aerial drone shot descending onto a busy Tokyo street at night, neon reflections on wet pavement, cinematic wide shot, rain
```

**Action:**
```
Tracking shot following a cheetah sprinting across the savanna, dust trail, golden hour, slow motion at 120fps, National Geographic style
```

**Abstract/Artistic:**
```
Camera orbits slowly around a glowing geometric crystal structure floating in space, deep blues and purples, particle effects, ambient light
```

**Character:**
```
Static close-up shot, a young woman opens her eyes and smiles softly, bokeh background of autumn leaves, warm golden hour light, cinematic portrait
```

---

## Image-to-Video Prompts

When animating a static image, describe **only the motion** — not the scene (the model already sees the image).

### Good
```
Gentle camera drift to the right, hair blows softly in the breeze, leaves fall slowly
```

### Avoid
```
A woman standing in a forest (this just re-describes the image — add motion instead)
```

### Examples
- `"Slow camera push in, clouds drift across the sky"`
- `"Subject blinks and smiles gently, subtle head movement"`
- `"Water ripples outward in slow motion"`
- `"Camera tilts up to reveal the full mountain peak, wind moves the grass"`

---

## Aspect Ratio Guide

| Use Case | Ratio |
|---|---|
| YouTube / landscape video | `16:9` |
| TikTok / Instagram Reels | `9:16` |
| Square social post | `1:1` |
| Standard photo / print | `4:3` |
| Portrait photo | `3:4` |

---

## Duration Tips

- **5 seconds**: Single action, mood piece, product reveal
- **10 seconds**: Multiple actions, more complex narrative, establishing shot

---

## Negative Prompting Patterns

To avoid common artifacts, add to your prompt:
```
..., no blur, no noise, no watermark, sharp focus
```

For videos:
```
..., smooth motion, no flickering, consistent lighting
```

---

## Credit Efficiency

- Images cost fewer credits than videos
- 720P videos cost fewer credits than 1080P
- 5-second videos cost fewer credits than 10-second videos
- Test your concept with an image first, then animate
