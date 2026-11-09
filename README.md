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

The Worker requires these encrypted secrets:

- \`FFMPEG_WORKER_TOKEN\` — a long random shared token used by ActivePieces, the Worker and GitHub Actions.
- \`POLLINATIONS_API_KEY\` — Pollinations text API key.
- \`HF_TOKEN\` — Hugging Face token with Read access to public Spaces; this is for the public ZeroGPU Space, not paid Inference Providers.
- \`CLOUDFLARE_ACCOUNT_ID\` — Cloudflare account ID for Workers AI.
- \`CLOUDFLARE_API_TOKEN\` — Cloudflare API token with Workers AI permissions.
- \`FISH_API_KEY\` — Fish Audio developer API key.
- \`GITHUB_TOKEN\` — fine-grained GitHub token with Actions: Read and write on this repository.

Never place any secret in Git, Wrangler \`vars\`, workflow inputs, task IDs, logs or a public asset URL.

## GitHub Actions setup

In \`HEAVENWRLD/faithframe-renderer\` → Settings → Secrets and variables → Actions:

- Add secret \`FFMPEG_WORKER_TOKEN\`, using the same value as the Cloudflare Worker secret.
- Add variable \`RENDER_API_BASE_URL\`, set to the deployed Cloudflare Worker HTTPS URL.

The Worker uses a repository-scoped token to dispatch the workflows. The workflow receives only a task UUID, then fetches the actual task and media through the authenticated Worker API.

## ActivePieces setup

ActivePieces will need the deployed Worker URL and the same \`FFMPEG_WORKER_TOKEN\` configured as a Bearer credential on the API request step. Keep all production flows disabled until the provider secrets, render-worker secrets and YouTube uploader are configured and the test render has completed successfully.

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
