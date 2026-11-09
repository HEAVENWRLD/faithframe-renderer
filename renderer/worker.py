#!/usr/bin/env python3
"""Authenticated GitHub Actions worker for private Gospel Inspiration Shorts.

All generated media lives in Cloudflare R2. GitHub Actions downloads assets
through the authenticated Cloudflare Worker API, renders locally with FFmpeg,
and returns the MP4 to R2. No Hatchable runtime or storage is used.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path
from typing import Any

CHUNK_BYTES = 1024 * 1024
MAX_ASSET_BYTES = 64 * 1024 * 1024
MAX_TOTAL_ASSET_BYTES = 350 * 1024 * 1024
MAX_OUTPUT_BYTES = 100 * 1024 * 1024


def request_json(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Cloudflare render API returned HTTP " + str(exc.code) + " while reading task metadata.") from None
    except Exception:
        raise RuntimeError("Could not reach the Cloudflare render API.") from None
    try:
        result = json.loads(raw.decode("utf-8"))
    except Exception:
        raise RuntimeError("Cloudflare returned unreadable task metadata.") from None
    if not isinstance(result, dict):
        raise RuntimeError("Cloudflare returned invalid task metadata.")
    return result


def download_asset(api_base: str, token: str, key: str, destination: Path, limit: int) -> int:
    if not isinstance(key, str) or not key or ".." in key or len(key) > 500:
        raise RuntimeError("Task contains an invalid R2 asset key.")
    encoded_key = urllib.parse.quote(key, safe="/")
    url = api_base + "/v1/assets/" + encoded_key
    request = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + token, "User-Agent": "Gospel-Inspiration-Renderer/2.0"},
        method="GET",
    )
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=180) as response, destination.open("wb") as output:
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
        raise RuntimeError("A Cloudflare R2 asset download failed with HTTP " + str(exc.code) + ".") from None
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("A source media asset could not be downloaded.") from None
    if total < 1:
        raise RuntimeError("A source media asset was empty.")
    return total


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


def make_ambient_music_wav(destination: Path, seed_text: str) -> None:
    """Create an original 62-second ambient instrumental; no external music API."""
    sample_rate = 22050
    seconds = 62
    sample_count = sample_rate * seconds
    progressions = [
        (50, [62, 66, 69, 74]),
        (47, [59, 62, 66, 71]),
        (43, [55, 59, 62, 67]),
        (45, [57, 61, 64, 69]),
    ]
    seed = 0
    for char in seed_text or "faith":
        seed = (seed * 31 + ord(char)) & 0xFFFFFFFF
    transpose = (0, -2, 2)[seed % 3]
    def midi_hz(note: int) -> float:
        return 440.0 * (2.0 ** ((note - 69) / 12.0))
    chords = [
        (midi_hz(root + transpose), [midi_hz(note + transpose) for note in notes])
        for root, notes in progressions
    ]
    arp_notes = [62, 69, 66, 74, 59, 66, 62, 71, 55, 62, 59, 67, 57, 64, 61, 69]
    arp = [midi_hz(note + transpose) for note in arp_notes]
    beat_interval = 60.0 / 72.0
    arp_interval = beat_interval / 2.0
    random_state = seed or 1
    two_pi = math.tau

    with wave.open(str(destination), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        frames = bytearray()
        for index in range(sample_count):
            t = index / sample_rate
            chord_index = int(t / 4.0) % len(chords)
            within = t % 4.0
            root, notes = chords[chord_index]
            pad_env = min(1.0, within / 0.7, (4.0 - within) / 0.6)
            left_pad = 0.0
            right_pad = 0.0
            for freq in notes:
                left_pad += math.sin(two_pi * freq * t) * 0.55 + math.sin(two_pi * freq * 2 * t) * 0.08
                right_pad += math.sin(two_pi * (freq * 1.0015) * t + 0.08) * 0.55 + math.sin(two_pi * freq * 2 * t) * 0.08
            left_pad *= 0.25 * pad_env
            right_pad *= 0.25 * pad_env

            arp_index = int(t / arp_interval) % len(arp)
            arp_age = t % arp_interval
            freq = arp[arp_index]
            pluck_env = math.exp(-arp_age * 5.5)
            left_arp = (math.sin(two_pi * freq * t) + math.sin(two_pi * freq * 2.01 * t) * 0.23) * pluck_env * 0.095
            right_arp = (math.sin(two_pi * freq * 1.002 * t + 0.2) + math.sin(two_pi * freq * 2.005 * t) * 0.18) * pluck_env * 0.09

            bass = math.sin(two_pi * root * t) * 0.11
            beat_age = t % beat_interval
            kick = 0.0
            hat = 0.0
            if beat_age < 0.16:
                kick = math.sin(two_pi * (82.0 - 38.0 * beat_age / 0.16) * beat_age) * math.exp(-beat_age * 23) * 0.17
            if beat_age < 0.04:
                random_state = (1664525 * random_state + 1013904223) & 0xFFFFFFFF
                noise = (random_state / 4294967295.0) * 2.0 - 1.0
                hat = noise * math.exp(-beat_age * 95) * 0.035

            fade = min(1.0, t / 1.5, (seconds - t) / 2.0)
            left = math.tanh((left_pad + left_arp + bass * 0.45 + kick + hat) * 1.1) * fade * 0.8
            right = math.tanh((right_pad + right_arp + bass * 0.45 + kick * 0.95 + hat * 0.9) * 1.1) * fade * 0.8
            frames += struct.pack("<hh", int(max(-1.0, min(1.0, left)) * 32767), int(max(-1.0, min(1.0, right)) * 32767))
            if len(frames) >= 65536:
                output.writeframesraw(frames)
                frames.clear()
        if frames:
            output.writeframesraw(frames)


def build_fallback_srt(text: str) -> str:
    caption = re.sub(r"\s+", " ", str(text or "")).strip()
    if not caption:
        raise RuntimeError("Task does not include a caption SRT or caption text.")
    # The renderer splits this single cue into readable phrases and retimes it
    # across the exact narration duration measured by ffprobe.
    return "1\n00:00:00,000 --> 00:03:00,000\n" + caption + "\n"


def upload_result(api_base: str, token: str, task_id: str, video_path: Path) -> dict[str, Any]:
    size = video_path.stat().st_size
    if size < 50_000 or size > MAX_OUTPUT_BYTES:
        raise RuntimeError("Rendered MP4 must be between 50 KB and 100 MB.")
    with video_path.open("rb") as source:
        signature = source.read(12)
        if len(signature) < 12 or signature[4:8] != b"ftyp":
            raise RuntimeError("Renderer output is not a valid MP4 file.")
        source.seek(0)
        video_bytes = source.read()

    url = api_base + "/v1/tasks/" + task_id + "/result"
    request = urllib.request.Request(
        url,
        data=video_bytes,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "video/mp4",
            "Accept": "application/json",
        },
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            raw = response.read(256 * 1024)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Cloudflare rejected the rendered MP4 with HTTP " + str(exc.code) + ".") from None
    except Exception:
        raise RuntimeError("Could not return the rendered MP4 to Cloudflare R2.") from None
    try:
        result = json.loads(raw.decode("utf-8"))
    except Exception:
        raise RuntimeError("Cloudflare returned an unreadable render-completion response.")
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Cloudflare did not confirm that the rendered MP4 was saved.")
    return result


def upload_thumbnail(api_base: str, token: str, task_id: str, image_path: Path) -> dict[str, Any]:
    size = image_path.stat().st_size
    if size < 1000 or size > 12 * 1024 * 1024:
        raise RuntimeError("Short thumbnail size is outside the allowed range.")
    data = image_path.read_bytes()
    if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("Short thumbnail is not a valid PNG image.")
    request = urllib.request.Request(
        api_base + "/v1/tasks/" + task_id + "/thumbnail",
        data=data,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "image/png",
            "Accept": "application/json",
        },
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read(256 * 1024)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Cloudflare rejected the Short thumbnail (HTTP " + str(exc.code) + ").") from None
    except Exception:
        raise RuntimeError("Could not return the Short thumbnail to private R2 storage.") from None
    try:
        result = json.loads(raw.decode("utf-8"))
    except Exception:
        raise RuntimeError("Cloudflare returned an unreadable thumbnail response.")
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Cloudflare did not confirm that the Short thumbnail was saved.")
    return result


def report_failure(api_base: str, token: str, task_id: str, message: str) -> None:
    payload = json.dumps({"message": str(message)[:600]}).encode("utf-8")
    request = urllib.request.Request(
        api_base + "/v1/tasks/" + task_id + "/failure",
        data=payload,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read(10_000)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()

    task_id = str(args.task_id).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}", task_id):
        raise ValueError("The supplied render task ID is not a valid UUID.")

    token = os.environ.get("FFMPEG_WORKER_TOKEN", "").strip()
    api_base = os.environ.get("RENDER_API_BASE_URL", "").rstrip("/")
    if len(token) < 32:
        raise RuntimeError("The GitHub Actions secret FFMPEG_WORKER_TOKEN is missing or too short.")
    if not api_base.startswith("https://"):
        raise RuntimeError("RENDER_API_BASE_URL must be an HTTPS Cloudflare Worker URL.")

    print("Requesting the render task from Cloudflare.")
    task = request_json(api_base + "/v1/tasks/" + task_id, token)
    if str(task.get("taskId", "")) != task_id:
        raise RuntimeError("Cloudflare returned a different render task ID.")
    if task.get("status") not in ("queued", "rendering"):
        raise RuntimeError("Render task is not in a renderable state: " + str(task.get("status")))

    scene_assets = task.get("sceneAssets")
    if not isinstance(scene_assets, list) or not 4 <= len(scene_assets) <= 30:
        raise RuntimeError("A Short task must contain between 4 and 30 scene images.")
    scene_assets = sorted(scene_assets, key=lambda item: int(item.get("sceneNumber", 0)))
    if [int(item.get("sceneNumber", 0)) for item in scene_assets] != list(range(1, len(scene_assets) + 1)):
        raise RuntimeError("The saved scene images are incomplete or out of order.")
    narration = task.get("narration")
    if not isinstance(narration, dict) or not narration.get("assetKey"):
        raise RuntimeError("A narration asset is required.")

    with tempfile.TemporaryDirectory(prefix="gospel-inspiration-short-") as temporary:
        root = Path(temporary)
        assets_dir = root / "assets"
        output_dir = root / "output"
        assets_dir.mkdir()
        output_dir.mkdir()
        total_downloaded = 0
        scene_paths: list[str] = []

        for index, asset in enumerate(scene_assets, start=1):
            content_type = str(asset.get("contentType") or "image/png")
            path = assets_dir / ("scene-" + str(index).zfill(2) + safe_extension(content_type, ".png"))
            total_downloaded += download_asset(api_base, token, str(asset["assetKey"]), path, MAX_ASSET_BYTES)
            scene_paths.append("assets/" + path.name)

        narration_path = assets_dir / ("narration" + safe_extension(str(narration.get("contentType") or "audio/mpeg"), ".mp3"))
        total_downloaded += download_asset(api_base, token, str(narration["assetKey"]), narration_path, MAX_ASSET_BYTES)

        music = task.get("music") or {}
        if isinstance(music, dict) and music.get("assetKey"):
            music_path = assets_dir / ("music" + safe_extension(str(music.get("contentType") or "audio/wav"), ".wav"))
            total_downloaded += download_asset(api_base, token, str(music["assetKey"]), music_path, MAX_ASSET_BYTES)
        else:
            music_path = assets_dir / "ambient-music.wav"
            make_ambient_music_wav(music_path, str(task.get("title") or task_id))

        thumbnail_asset = task.get("thumbnailAsset") or {}
        thumbnail_path = None
        if isinstance(thumbnail_asset, dict) and thumbnail_asset.get("assetKey"):
            thumbnail_path = assets_dir / "thumbnail.png"
            total_downloaded += download_asset(api_base, token, str(thumbnail_asset["assetKey"]), thumbnail_path, 12 * 1024 * 1024)

        outro = task.get("outro") or {}
        outro_path = None
        if isinstance(outro, dict) and outro.get("assetKey"):
            outro_path = assets_dir / ("outro" + safe_extension(str(outro.get("contentType") or "image/svg+xml"), ".svg"))
            total_downloaded += download_asset(api_base, token, str(outro["assetKey"]), outro_path, MAX_ASSET_BYTES)

        if total_downloaded > MAX_TOTAL_ASSET_BYTES:
            raise RuntimeError("Total source media exceeds the worker download limit.")

        caption_path = assets_dir / "captions.srt"
        caption_srt = str(task.get("captionSrt") or "").strip()
        if not caption_srt:
            caption_srt = build_fallback_srt(str(task.get("captionText") or ""))
        caption_path.write_text(caption_srt, encoding="utf-8")

        if thumbnail_path:
            upload_thumbnail(api_base, token, task_id, thumbnail_path)

        output_path = output_dir / "rendered-short.mp4"
        manifest_path = root / "manifest.json"
        manifest = {
            "title": str(task.get("title") or "A Message of Faith"),
            "scenes": scene_paths,
            "narration": "assets/" + narration_path.name,
            "music": "assets/" + music_path.name,
            "captions_srt": "assets/captions.srt",
            "outro": ("assets/" + outro_path.name) if outro_path else None,
            "output": "output/rendered-short.mp4",
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        print("Source assets downloaded. Rendering with FFmpeg.")
        subprocess.run(
            [sys.executable, "renderer/render_short.py", "--manifest", str(manifest_path)],
            check=True,
        )
        print("FFmpeg render passed MP4, codec, size and duration checks.")
        saved = upload_result(api_base, token, task_id, output_path)
        print("Cloudflare R2 confirmed the rendered MP4:", saved.get("status"), "size:", output_path.stat().st_size)
    return 0


if __name__ == "__main__":
    api_base = os.environ.get("RENDER_API_BASE_URL", "").rstrip("/")
    token = os.environ.get("FFMPEG_WORKER_TOKEN", "").strip()
    task_id = ""
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument("--task-id", required=True)
        parsed = parser.parse_args()
        task_id = parsed.task_id
        raise SystemExit(main())
    except Exception as exc:
        print("RENDER WORKER FAILED:", str(exc), file=sys.stderr)
        if api_base.startswith("https://") and len(token) >= 32 and re.fullmatch(r"[0-9a-fA-F-]{36}", task_id):
            report_failure(api_base, token, task_id, str(exc))
        raise SystemExit(1)
