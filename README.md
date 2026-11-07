# FaithFrame Renderer

External FFmpeg rendering worker for Gospel Inspiration, connected to the existing FaithFrame Studio dashboard and Hatchable storage.

## Workflows

- **FFmpeg Short Render Test** creates synthetic media and renders a sample 9:16 MP4. It is safe to run for visual checks and produces a short-lived sample artifact.
- **FFmpeg Short Production Render** accepts only an opaque Hatchable draft ID. The runner authenticates to Hatchable, downloads private source assets directly to the temporary GitHub runner, renders the Short, and returns the MP4 to the existing Hatchable storage.

The production workflow does not upload private videos as GitHub artifacts. It does not put media URLs, signed URLs, or credentials in workflow inputs. The workflow input is only a task ID; a separate secret authorizes the media handoff.

## Test the renderer

1. Open the **Actions** tab.
2. Select **FFmpeg Short Render Test**.
3. Click **Run workflow** on the `main` branch.
4. Download the `gospel-inspiration-render-test` artifact to preview the MP4.

The output should be a 720×1280 portrait MP4 with H.264 video and AAC audio.

## Production connection prerequisites

The production workflow is staged but must not be used until the Hatchable endpoints are deployed and both sides have matching credentials.

- Add the repository secret `FFMPEG_WORKER_TOKEN` in **Settings → Secrets and variables → Actions**.
- Add the same `FFMPEG_WORKER_TOKEN` value in Hatchable project secrets.
- Add `GITHUB_ACTIONS_TOKEN` in Hatchable project secrets. It must be a fine-grained GitHub token scoped to this repository with **Actions: Read and write** permission so Hatchable can dispatch the workflow.
- Never commit any of these values or place them in workflow inputs, run logs, or artifacts.

Hatchable remains responsible for the dashboard, database, private media storage, and YouTube publishing. The existing renderer remains the fallback until the external path has passed an end-to-end production test.

## Renderer manifest

`renderer/render_short.py` accepts a local JSON manifest with these fields:

- `title`: video title shown at the beginning.
- `scenes`: 4–30 local image paths, in display order.
- `narration`: local narration audio path.
- `music`: local background music path.
- `captions_srt`: local timed SRT caption path.
- `outro`: optional local outro image or SVG path.
- `output`: output MP4 path (optional).

The renderer handles Hatchable's current single-block script captions by splitting them into short, timed phrases based on the actual narration duration. It downloads no remote URLs and reads no credentials; the authenticated worker handles private downloads before invoking it.

## Security

- Never commit videos, narration, music, generated images, private media URLs, API keys, access tokens, or environment files.
- Keep all credentials in Hatchable project secrets or GitHub Actions Secrets.
- Workflows must not execute untrusted pull-request code with secrets.
- Do not put signed storage URLs or secret values in workflow inputs, artifacts, or logs.
- The production workflow is manual-dispatch only and accepts an opaque task ID.
