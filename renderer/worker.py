#!/usr/bin/env python3
"""Authenticated GitHub Actions worker for private Gospel Inspiration Shorts."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

MAX_ASSET_BYTES = 64 * 1024 * 1024
MAX_TOTAL_ASSET_BYTES = 350 * 1024 * 1024
MAX_OUTPUT_BYTES = 24 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


def api_request(url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Hatchable task request failed with HTTP " + str(exc.code) + ".") from None
    except Exception:
        raise RuntimeError("Could not reach the Hatchable renderer endpoint.") from None
    try:
        result = json.loads(raw.decode("utf-8"))
    except Exception:
        raise RuntimeError("Hatchable returned an unreadable renderer response.") from None
    if not isinstance(result, dict):
        raise RuntimeError("Hatchable returned an invalid renderer response.")
    return result


def safe_extension(content_type: str, fallback: str) -> str:
    mime = content_type.split(";", 1)[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/m4a": ".m4a",
    }.get(mime, fallback)


def download_asset(url: str, destination: Path, limit: int) -> int:
    if not isinstance(url, str) or not url.startswith("https://"):
        raise RuntimeError("Hatchable supplied an invalid secure media address.")
    request = urllib.request.Request(url, headers={"User-Agent": "FaithFrame-Renderer/1.0"})
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > limit:
                raise RuntimeError("A source media file exceeds the configured download limit.")
            while True:
                chunk = response.read(CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise RuntimeError("A source media file exceeds the configured download limit.")
                output.write(chunk)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("A private source media download failed with HTTP " + str(exc.code) + ".") from None
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("A private source media file could not be downloaded.") from None
    if total < 1:
        raise RuntimeError("A source media file was empty.")
    return total


def upload_result(url: str, token: str, task_id: str, video_path: Path) -> dict[str, Any]:
    size = video_path.stat().st_size
    if size < 50_000 or size > MAX_OUTPUT_BYTES:
        raise RuntimeError("Rendered MP4 must be between 50 KB and 24 MB for the current Hatchable upload path.")
    with video_path.open("rb") as source:
        signature = source.read(12)
        if len(signature) < 12 or signature[4:8] != b"ftyp":
            raise RuntimeError("Renderer output is not a valid MP4 file.")
        source.seek(0)
        video_bytes = source.read()

    boundary = "----FaithFrame" + uuid.uuid4().hex
    boundary_bytes = boundary.encode("ascii")
    body = b"".join([
        b"--" + boundary_bytes + b"\r\n",
        b'Content-Disposition: form-data; name="taskId"\r\n\r\n',
        task_id.encode("ascii") + b"\r\n",
        b"--" + boundary_bytes + b"\r\n",
        b'Content-Disposition: form-data; name="video"; filename="rendered-short.mp4"\r\n',
        b"Content-Type: video/mp4\r\n\r\n",
        video_bytes,
        b"\r\n--" + boundary_bytes + b"--\r\n",
    ])
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read(256 * 1024)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Hatchable rejected the rendered MP4 with HTTP " + str(exc.code) + ".") from None
    except Exception:
        raise RuntimeError("Could not return the rendered MP4 to Hatchable.") from None
    try:
        result = json.loads(raw.decode("utf-8"))
    except Exception:
        raise RuntimeError("Hatchable returned an unreadable render-completion response.") from None
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Hatchable did not confirm that the rendered MP4 was saved.")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()

    task_id = str(args.task_id).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}", task_id):
        raise ValueError("The supplied render task ID is not a valid UUID.")

    token = os.environ.get("FFMPEG_WORKER_TOKEN", "").strip()
    api_base = os.environ.get("HATCHABLE_API_BASE_URL", "").rstrip("/")
    if len(token) < 32:
        raise RuntimeError("The GitHub Actions secret FFMPEG_WORKER_TOKEN is missing or too short.")
    if not api_base.startswith("https://"):
        raise RuntimeError("HATCHABLE_API_BASE_URL must be an HTTPS address.")

    print("Requesting the private Short render task from Hatchable.")
    task = api_request(api_base + "/tasks/get", token, {"taskId": task_id})
    if str(task.get("taskId", "")) != task_id:
        raise RuntimeError("Hatchable returned a different render task ID.")

    scene_assets = task.get("sceneAssets")
    if not isinstance(scene_assets, list) or not 4 <= len(scene_assets) <= 30:
        raise RuntimeError("The render task must contain between 4 and 30 saved scenes.")
    scene_assets = sorted(scene_assets, key=lambda item: int(item.get("sceneNumber", 0)))
    if [int(item.get("sceneNumber", 0)) for item in scene_assets] != list(range(1, len(scene_assets) + 1)):
        raise RuntimeError("The saved scene images are incomplete or out of order.")

    narration = task.get("narration") or {}
    music = task.get("music") or {}
    outro = task.get("outro") or {}
    for label, item in (("narration", narration), ("music", music), ("outro", outro)):
        if not isinstance(item, dict) or not item.get("downloadUrl"):
            raise RuntimeError("Hatchable has not saved the required " + label + " asset.")

    with tempfile.TemporaryDirectory(prefix="faithframe-short-") as temporary:
        root = Path(temporary)
        assets_dir = root / "assets"
        output_dir = root / "output"
        assets_dir.mkdir()
        output_dir.mkdir()
        total_downloaded = 0
        scene_paths: list[str] = []

        for index, asset in enumerate(scene_assets, start=1):
            content_type = str(asset.get("contentType") or "image/jpeg")
            path = assets_dir / ("scene-" + str(index).zfill(2) + safe_extension(content_type, ".jpg"))
            total_downloaded += download_asset(str(asset["downloadUrl"]), path, MAX_ASSET_BYTES)
            scene_paths.append("assets/" + path.name)

        narration_path = assets_dir / ("narration" + safe_extension(str(narration.get("contentType") or "audio/mpeg"), ".mp3"))
        music_path = assets_dir / ("music" + safe_extension(str(music.get("contentType") or "audio/wav"), ".wav"))
        outro_path = assets_dir / ("outro" + safe_extension(str(outro.get("contentType") or "image/svg+xml"), ".svg"))
        total_downloaded += download_asset(str(narration["downloadUrl"]), narration_path, MAX_ASSET_BYTES)
        total_downloaded += download_asset(str(music["downloadUrl"]), music_path, MAX_ASSET_BYTES)
        total_downloaded += download_asset(str(outro["downloadUrl"]), outro_path, MAX_ASSET_BYTES)
        if total_downloaded > MAX_TOTAL_ASSET_BYTES:
            raise RuntimeError("The total source media size exceeds the worker limit.")

        caption_path = assets_dir / "captions.srt"
        caption_path.write_text(str(task.get("captionSrt") or ""), encoding="utf-8")
        if caption_path.stat().st_size < 10:
            raise RuntimeError("The saved draft does not contain usable timed captions.")

        output_path = output_dir / "rendered-short.mp4"
        manifest_path = root / "manifest.json"
        manifest = {
            "title": str(task.get("title") or "A Message of Faith"),
            "scenes": scene_paths,
            "narration": "assets/" + narration_path.name,
            "music": "assets/" + music_path.name,
            "captions_srt": "assets/captions.srt",
            "outro": "assets/" + outro_path.name,
            "output": "output/rendered-short.mp4",
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        print("Downloaded private source assets. Starting FFmpeg render.")
        subprocess.run(
            [sys.executable, "renderer/render_short.py", "--manifest", str(manifest_path)],
            check=True,
        )
        print("FFmpeg render passed its MP4, codec, size and duration checks.")

        result_url = api_base + "/tasks/submit"
        saved = upload_result(result_url, token, task_id, output_path)
        print(
            "Hatchable saved the completed Short.",
            "Size:", output_path.stat().st_size,
            "bytes. Publish queued:", bool(saved.get("publishQueued")),
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("RENDER WORKER FAILED:", str(exc), file=sys.stderr)
        raise SystemExit(1)
