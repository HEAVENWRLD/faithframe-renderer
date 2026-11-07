# FaithFrame Renderer

External FFmpeg rendering worker for Gospel Inspiration. The FaithFrame Studio dashboard, database, permanent media storage and YouTube publishing remain in the original private Hatchable project.

## Production rendering

**FFmpeg Short Production Render** accepts only an opaque Short draft ID. It retrieves private task metadata from a separate API-only bridge, downloads the original media from short-lived signed storage links, renders the MP4 on GitHub Actions, then returns the MP4 to the bridge for collection into the original FaithFrame Studio storage.

The synthetic FFmpeg sample workflow has been removed after its successful end-to-end codec and MP4 validation. Production rendering never uploads private media as a GitHub Actions artifact.

The bridge has no dashboard or public media library. Its API endpoints are protected by a high-entropy bearer token. The main FaithFrame Studio dashboard remains private.

## Production connection

Do not enable production dispatch until the API-only bridge has been switched to public visibility in Hatchable Settings and the same random token has been saved in the bridge, the private studio and GitHub Actions Secrets.

- In the private **Daily AI YouTube Shorts Studio** project, set:
  - `GITHUB_ACTIONS_TOKEN`: fine-grained GitHub token restricted to `HEAVENWRLD/faithframe-renderer` with **Actions: Read and write** permission.
  - `FFMPEG_WORKER_TOKEN`: high-entropy shared bearer token.
  - `FFMPEG_RELAY_URL`: `https://faithframe-render.hatchable.site/api`.
  - `FFMPEG_EXTERNAL_RENDER_ENABLED`: set to `true` only after the other configuration is complete.
- In the private **FaithFrame Render Bridge** project, set the same `FFMPEG_WORKER_TOKEN`.
- In GitHub repository **Settings → Secrets and variables → Actions**, set the same `FFMPEG_WORKER_TOKEN`.

Never put signed media URLs or bearer tokens in workflow inputs, logs, or artifacts. The workflow input contains only the opaque draft ID.

The bridge stores the returned MP4 temporarily. The original private studio downloads it, saves it in its existing storage, and then acknowledges deletion of the temporary bridge copy. The existing YouTube upload job remains responsible for publishing and duplicate-upload protection.

## Renderer manifest

`renderer/render_short.py` accepts a local JSON manifest with:

- `title`: video title shown at the beginning.
- `scenes`: 4–30 local image paths, in display order.
- `narration`: local narration audio path.
- `music`: local background music path.
- `captions_srt`: local timed SRT caption path.
- `outro`: optional local outro image or SVG path.
- `output`: output MP4 path (optional).

Hatchable currently stores the full script as a single caption block. The renderer now splits that block into readable phrases, writes a correctly timed temporary SRT file using the actual narration duration, and burns those phrase-level captions into the video.

## Security

- Never commit videos, narration, music, generated images, signed storage URLs, API keys, access tokens, or environment files.
- Keep credentials in Hatchable project secrets or GitHub Actions Secrets.
- Workflows must not execute untrusted pull-request code with secrets.
- The main studio and its permanent storage remain private.
