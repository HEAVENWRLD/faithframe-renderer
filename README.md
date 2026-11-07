# FaithFrame Renderer

FFmpeg rendering worker for the Gospel Inspiration video automation project.

## Current status

The repository has one manual GitHub Actions test:

- **FFmpeg Short Render Test** — builds synthetic source media and renders a sample 9:16 MP4 using the renderer in `renderer/render_short.py`.

The Short Render Test exercises scene images, slow zoom/pan, timed SRT captions, narration, background music, an outro card, Gospel Inspiration title branding, H.264 video, and AAC audio.

The test uses synthetic media only. It does not access Hatchable storage, Activepieces, YouTube, private media, or production credentials. The renderer is not connected to production yet.

## Run the vertical Short test

1. Open the **Actions** tab.
2. Select **FFmpeg Short Render Test**.
3. Click **Run workflow** on the `main` branch.
4. Wait for the job to finish.
5. Download the `gospel-inspiration-render-test` artifact to preview the MP4.

The output should be a 720×1280 portrait MP4 with H.264 video and AAC audio.

## Renderer manifest

`renderer/render_short.py` accepts a local JSON manifest with these fields:

- `title`: video title shown at the beginning.
- `scenes`: 4–30 local image paths, in display order.
- `narration`: local narration audio path.
- `music`: local background music path.
- `captions_srt`: local timed SRT caption path.
- `outro`: optional local outro image or SVG path.
- `output`: output MP4 path (optional).

All paths are resolved relative to the manifest file. The renderer deliberately does not download remote URLs or read credentials. A future authenticated handoff will download private media to the runner before invoking this script.

## Security

- Never commit videos, narration, music, generated images, private media URLs, API keys, access tokens, or environment files.
- Keep credentials in GitHub Actions Secrets when a later integration requires them.
- Workflows must not execute untrusted pull-request code with secrets.
- Do not put signed storage URLs or secret values in workflow inputs, artifacts, or logs.
- Production rendering and publishing are not connected yet.
