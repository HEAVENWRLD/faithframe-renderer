# FaithFrame Renderer

A public, GitHub Actions-based FFmpeg worker for the Gospel Inspiration video automation project.

## Current status

The repository currently contains a manual **FFmpeg Smoke Test** workflow. It creates a five-second synthetic video to confirm that the GitHub-hosted runner can install and execute FFmpeg, then uploads the sample as a one-day artifact.

This test uses generated color bars and a tone only. It does not access private media, Hatchable storage, Activepieces, YouTube, or production credentials.

## Run the smoke test

1. Open the **Actions** tab.
2. Select **FFmpeg Smoke Test**.
3. Select **Run workflow** on the main branch.
4. Wait for the job to finish successfully.
5. Download the `ffmpeg-smoke-test` artifact from the run.

## Security rules

- Never commit videos, narration, music, private media URLs, API keys, access tokens, or environment files.
- Keep credentials in GitHub Actions Secrets when a later integration requires them.
- Do not add pull-request triggers that execute untrusted code with secrets.
- Production rendering is not connected yet. The smoke test is intentionally isolated.
