# Gospel Inspiration Media Renderer

Cloudflare Worker + Cloudflare R2 + GitHub Actions + FFmpeg renderer for the Gospel Inspiration channel.

This repository contains the media API, the FFmpeg workers and the render workflows. ActivePieces is responsible for daily scheduling, starting production, tracking the publishing queue and coordinating publication. Cloudflare Workers call the original generation services and keep private media in R2. GitHub Actions renders final videos with FFmpeg.

There is no Hatchable runtime, storage bucket, database or bridge in this architecture.

## Production architecture

1. ActivePieces starts the Shorts or long-video production workflow on its Africa/Nairobi schedule.
2. The Cloudflare Worker authenticates the request and calls the original services:
   - Pollinations text generation (\`openai/gpt-5.4-nano\`) for scripts.
   - Authenticated Hugging Face public ZeroGPU Space as the text-generation fallback.
   - Cloudflare Workers AI FLUX.2 Klein 4B for scene images and thumbnails.
   - Fish Audio \`s2.1-pro-free\` for narration.
3. Generated assets and render-task metadata are stored privately in Cloudflare R2.
4. The Worker dispatches the appropriate GitHub Actions workflow with an opaque task UUID only.
5. The GitHub worker downloads the private assets through the authenticated Worker API, generates the original ambient music locally, renders with FFmpeg and returns the MP4 to R2.
6. ActivePieces collects the render result, stores metadata in its Publishing Queue and coordinates YouTube publication. A separate resumable uploader is required for reliable large-video uploads; the YouTube connector's metadata API is not a substitute for binary media upload.

Ambient music is generated locally by the renderer from original oscillator-based notes. Captions are derived from the script and timed across the real narration duration; there is no speech-to-text dependency.

## Cloudflare Worker setup

Worker source and configuration:

- \`cloudflare/faithframe-api/src/index.js\`
- \`cloudflare/faithframe-api/wrangler.jsonc\`

Create the R2 bucket named \`gospel-inspiration-media\`, then deploy the Worker from \`cloudflare/faithframe-api\`.

Create these encrypted GitHub repository secrets in `HEAVENWRLD/faithframe-renderer` → Settings → Secrets and variables → Actions. The Cloudflare deployment workflow synchronizes the runtime secrets to the Worker:

- `CLOUDFLARE_API_TOKEN` — Cloudflare account API token with **Edit Cloudflare Workers**, scoped to the account containing this Worker; used only by the deployment action.
- `CLOUDFLARE_ACCOUNT_ID` — Cloudflare account ID.
- `ACTIVEPIECES_API_TOKEN` — a unique high-entropy token used by ActivePieces for production, publishing-dispatch and cleanup requests.
- `FFMPEG_WORKER_TOKEN` — a separate high-entropy token used by GitHub render workers to access private task and media endpoints.
- `POLLINATIONS_API_KEY` — Pollinations text API key.
- `HF_TOKEN` — Hugging Face token with Read access to public Spaces; this is for the public ZeroGPU Space, not paid Inference Providers.
- `WORKERS_AI_API_TOKEN` — a separate Cloudflare API token with Account > Workers AI > Read and Edit permissions for FLUX.2 Klein image generation.
- `FISH_API_KEY` — Fish Audio developer API key.
- `RENDER_GITHUB_TOKEN` — fine-grained GitHub token with Actions: Read and write on this repository. The Worker receives this as its private `GITHUB_TOKEN` secret.
- `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, and `YOUTUBE_REFRESH_TOKEN` — OAuth credentials for the Gospel Inspiration channel.

Do not reuse `ACTIVEPIECES_API_TOKEN` as `FFMPEG_WORKER_TOKEN`. Never place a secret in Git, Wrangler `vars`, workflow inputs, task IDs, logs or a public asset URL.

## GitHub Actions setup

In `HEAVENWRLD/faithframe-renderer` → Settings → Secrets and variables → Actions, add this repository variable:

- `RENDER_API_BASE_URL` — the deployed Cloudflare Worker HTTPS URL (its `workers.dev` address).

The Worker receives only opaque task UUIDs for workflow dispatch. GitHub render workers retrieve source assets and tasks through the authenticated Worker API using `FFMPEG_WORKER_TOKEN`.

The deployment workflow is `.github/workflows/deploy-cloudflare-worker.yml`. It is manual until the credentials are configured. After adding the listed repository secrets, open **Actions** → **Deploy Gospel Inspiration Cloudflare Worker** → **Run workflow**. It checks the required secrets first, then deploys the Worker and synchronizes its encrypted runtime secrets.
## ActivePieces setup

ActivePieces will need the deployed Worker URL and `ACTIVEPIECES_API_TOKEN` configured as the Bearer credential on all production, publishing-dispatch and maintenance API request steps. This token is deliberately different from `FFMPEG_WORKER_TOKEN`. Keep all flows disabled until the Worker is deployed, the tokens are configured, and test renders and a YouTube test upload succeed.
## R2 storage cleanup and retention

The ActivePieces maintenance flow calls `POST /v1/maintenance/cleanup` once per day (03:00 Africa/Nairobi) using `ACTIVEPIECES_API_TOKEN`. The Worker scans private R2 task records and removes media under these rules:

- After YouTube upload is confirmed, remove generated source images/audio and the local rendered MP4 after 48 hours. Keep the small task record so a completed YouTube video ID continues to prevent duplicate publishing.
- Failed or abandoned render jobs retain their media for 14 days, then their large media is removed and the task metadata is marked expired.
- Items requiring manual review retain their media for 60 days. A saved resumable YouTube upload session is protected from automatic deletion.
- Unreferenced partial-generation assets are removed after 24 hours.
- Future scheduled videos, active renders, queued/in-progress/retryable uploads and tasks with saved unfinished resumable sessions are protected.
- Each cleanup run deletes at most 300 R2 objects. If more work remains, the next daily run continues. Task JSON records are not automatically deleted.

The cleanup endpoint accepts `{"dryRun":true}` for a non-destructive report. The normal scheduled request uses `{"dryRun":false}`. It returns counts of checked tasks, protected uploads, deleted objects and bytes freed. No broad R2 lifecycle expiration rule is applied to the active `jobs/` or `tasks/` prefixes, because that could remove videos that are still waiting to publish.

## Long-video renderer

The long workflow is `.github/workflows/ffmpeg-long-production-render.yml`. It reads a 12-section task, generates 12 landscape scene images with FLUX.2 Klein 4B and 12 narration clips with Fish Audio `s2.1-pro-free`, and also generates a 16:9 thumbnail with FLUX. It downloads the media privately from R2 and invokes `renderer/render_long.py`.

The long renderer creates phrase-level subtitles from each section's narration, adds gentle image motion and chapter labels, concatenates the 12 sections, mixes the locally generated oscillator-based ambient bed, adds a three-second subscribe end card, and encodes a 1280×720 H.264/AAC MP4. It rejects a final runtime outside 8–12 minutes and validates the codecs and output size before returning the file to R2. The thumbnail is saved separately at `/v1/tasks/{taskId}/thumbnail`.

## Current Short renderer manifest

The Short renderer accepts a local JSON manifest containing:

- \`title\`
- \`scenes\` (4–30 local image paths)
- \`narration\`
- \`music\`
- \`captions_srt\`
- \`outro\` (optional)
- \`output\` (optional)

The worker writes this manifest from the private task retrieved from R2. The renderer outputs H.264/AAC MP4 at 720×1280, 24 fps, with phrase-level captions, moving stills, title/brand overlay and a three-second subscribe ending.

## Security and privacy

- Generated images, narration, music, scripts, render-task JSON and MP4s remain in private R2 objects.
- The R2 bucket is not public and the Worker API requires a bearer token.
- GitHub Actions inputs contain only opaque UUIDs; they do not contain media URLs or credentials.
- Do not commit videos, generated images, audio, signed storage URLs, API keys, access tokens or environment files.
- The renderer validates media size, task identity, scene sequence and final MP4 format before accepting the result.
