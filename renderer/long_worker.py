#!/usr/bin/env python3
"""Builds the 12-section long-form video outside Hatchable.

GitHub Actions asks the Cloudflare Worker for each FLUX image and Fish Audio
narration clip. Private assets are stored in R2, rendered with FFmpeg, then
the final MP4 and PNG thumbnail are returned to the authenticated Worker API.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

MAX_ASSET_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 700 * 1024 * 1024
MAX_RESULT_BYTES = 100 * 1024 * 1024


def headers(token: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer " + token,
        "User-Agent": "Gospel-Inspiration-Long-Renderer/1.0",
    }


def read_json(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={**headers(token), "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read(3 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Cloudflare task lookup returned HTTP " + str(exc.code) + ".") from None
    except Exception:
        raise RuntimeError("Could not reach the Cloudflare render API.") from None
    try:
        result = json.loads(raw.decode("utf-8"))
    except Exception:
        raise RuntimeError("Cloudflare returned unreadable long-video task data.") from None
    if not isinstance(result, dict):
        raise RuntimeError("Cloudflare returned invalid long-video task data.")
    return result


def post_json(url: str, token: str, payload: dict[str, Any], attempts: int = 2) -> dict[str, Any]:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error = ""
    for attempt in range(attempts + 1):
        request = urllib.request.Request(
            url,
            data=raw,
            headers={**headers(token), "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                data = response.read(2 * 1024 * 1024)
            value = json.loads(data.decode("utf-8"))
            if not isinstance(value, dict) or value.get("ok") is not True:
                raise RuntimeError(str(value.get("error", "A media generation endpoint did not confirm success."))[:500])
            return value
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            last_error = "HTTP " + str(exc.code) + " " + detail[:250]
            if exc.code not in (429, 500, 502, 503, 504) or attempt >= attempts:
                raise RuntimeError("Cloudflare media generation request failed: " + last_error) from None
        except RuntimeError:
            raise
        except Exception as exc:
            last_error = str(exc)[:250]
            if attempt >= attempts:
                raise RuntimeError("A media generation request failed: " + last_error) from None
        time.sleep(4 * (attempt + 1))
    raise RuntimeError("Media generation could not be completed: " + last_error)


def safe_extension(content_type: str, fallback: str) -> str:
    mime = content_type.split(";", 1)[0].strip().lower()
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
    }.get(mime, fallback)


def download_asset(api_base: str, token: str, key: str, destination: Path) -> int:
    if not key or ".." in key or len(key) > 500:
        raise RuntimeError("The render task contains an invalid asset key.")
    url = api_base + "/v1/assets/" + urllib.parse.quote(key, safe="/")
    request = urllib.request.Request(url, headers=headers(token))
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=240) as response, destination.open("wb") as output:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > MAX_ASSET_BYTES:
                raise RuntimeError("A source asset exceeds the allowed size.")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ASSET_BYTES:
                    raise RuntimeError("A source asset exceeds the allowed size.")
                output.write(chunk)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Cloudflare R2 asset download returned HTTP " + str(exc.code) + ".") from None
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("A private R2 asset could not be downloaded.") from None
    if total < 1000:
        raise RuntimeError("A downloaded media asset is unexpectedly small.")
    return total


def upload_file(api_base: str, token: str, url_path: str, path: Path, content_type: str, max_bytes: int) -> dict[str, Any]:
    size = path.stat().st_size
    if size < 1000 or size > max_bytes:
        raise RuntimeError("Upload file is outside the accepted size range.")
    data = path.read_bytes()
    request = urllib.request.Request(
        api_base + url_path,
        data=data,
        headers={**headers(token), "Content-Type": content_type, "Accept": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=360) as response:
            result = json.loads(response.read(256 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(1200).decode("utf-8", errors="replace").strip()
        label = url_path.rsplit("/", 1)[-1] or "asset"
        raise RuntimeError(
            "Cloudflare could not store " + label + " (HTTP " + str(exc.code) + "): " + detail[:500]
        ) from None
    except Exception as exc:
        raise RuntimeError("Could not return the rendered asset to Cloudflare R2: " + str(exc)[:300]) from None
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Cloudflare did not confirm the rendered asset upload.")
    return result


def build_scene_prompt(task: dict[str, Any], section: dict[str, Any]) -> str:
    return " ".join([
        "Landscape 16:9 cinematic Christian YouTube scene, 1024x576 framing.",
        "Premium historical drama cinematography, realistic textures, atmospheric depth, natural dramatic lighting, reverent and emotionally engaging tone.",
        "One clear scene for this chapter; no text, lettering, symbols, captions, logos, watermarks or borders.",
        "Maintain consistent faces, age, clothing, props and palette for recurring characters throughout this story.",
        "Respect biblical context and avoid showing details that contradict the narration.",
        "Video title: " + str(task.get("title", ""))[:150] + ".",
        "Chapter: " + str(section.get("heading", ""))[:120] + ".",
        "Scene details: " + str(section.get("visualPrompt", ""))[:3000],
    ])


def generate_one_section(api_base: str, token: str, task_id: str, task: dict[str, Any], section: dict[str, Any]) -> dict[str, Any]:
    number = int(section.get("sectionNumber", 0))
    if not 1 <= number <= 12:
        raise RuntimeError("A long-video section number is invalid.")
    prompt = build_scene_prompt(task, section)
    image = post_json(api_base + "/v1/image", token, {
        "jobId": task_id,
        "fileName": "long-scene-" + str(number).zfill(2),
        "prompt": prompt,
        "width": 1024,
        "height": 576,
    })
    narration = post_json(api_base + "/v1/narration", token, {
        "jobId": task_id,
        "fileName": "long-narration-" + str(number).zfill(2),
        "text": str(section.get("narration", "")),
        "voiceStyle": str(task.get("voiceStyle", "female")),
    })
    if not image.get("assetKey") or not narration.get("assetKey"):
        raise RuntimeError("A section did not return both image and narration asset keys.")
    return {
        "sectionNumber": number,
        "heading": str(section.get("heading", "")),
        "narrationText": str(section.get("narration", "")),
        "imageAssetKey": image["assetKey"],
        "imageContentType": str(image.get("contentType") or "image/png"),
        "narrationAssetKey": narration["assetKey"],
    }


def report_failure(api_base: str, token: str, task_id: str, message: str) -> None:
    payload = json.dumps({"message": str(message)[:600]}).encode("utf-8")
    request = urllib.request.Request(
        api_base + "/v1/tasks/" + task_id + "/failure",
        data=payload,
        headers={**headers(token), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read(10_000)
    except Exception:
        pass


def run(task_id: str) -> int:
    api_base = os.environ.get("RENDER_API_BASE_URL", "").rstrip("/")
    token = os.environ.get("FFMPEG_WORKER_TOKEN", "").strip()
    if not api_base.startswith("https://"):
        raise RuntimeError("RENDER_API_BASE_URL must be the HTTPS Cloudflare Worker URL.")
    if len(token) < 32:
        raise RuntimeError("FFMPEG_WORKER_TOKEN is missing or too short.")
    if not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89a-fA-F][0-9a-fA-F]{3}-[0-9a-fA-F]{12}", task_id):
        raise RuntimeError("A valid long-video task UUID is required.")

    task = read_json(api_base + "/v1/tasks/" + task_id, token)
    if task.get("taskId") != task_id or task.get("format") != "long":
        raise RuntimeError("This is not a valid long-video render task.")
    status = str(task.get("status") or "")
    if status == "ready" and task.get("resultKey"):
        print("The existing long-video render is already stored in Cloudflare; treating this run as successful.")
        return 0
    if status == "failed":
        if task.get("resultKey") or task.get("youtubeVideoId") or task.get("youtubeUploadSessionUrl"):
            raise RuntimeError("A failed task with an existing rendered result or YouTube upload state cannot be retried automatically.")
        previous_error = str(task.get("error") or task.get("lastRenderError") or "").strip()
        print("Requesting a guarded automatic render retry from Cloudflare.")
        if previous_error:
            print("Previous render failure:", previous_error[:500])
        retry = post_json(api_base + "/v1/tasks/" + task_id + "/render-retry", token, {})
        print("Cloudflare approved render attempt", retry.get("attempt"), "of", retry.get("maxAttempts"))
        task = read_json(api_base + "/v1/tasks/" + task_id, token)
        status = str(task.get("status") or "")
    if status not in ("queued_media", "generating_media", "rendering"):
        raise RuntimeError("The long-video task is not ready: " + status)
    sections = task.get("sections")
    if not isinstance(sections, list) or len(sections) != 12:
        raise RuntimeError("A long video must contain exactly 12 narration sections.")

    print("Generating the 12 landscape scenes and narration clips with the configured original providers.")
    ordered = sorted(sections, key=lambda value: int(value.get("sectionNumber", 0)))
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(generate_one_section, api_base, token, task_id, task, section)
            for section in ordered
        ]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda value: value["sectionNumber"])

    thumbnail_prompt = " ".join([
        "Wide 16:9 cinematic YouTube thumbnail for a Christian long-form video.",
        "Highly polished cinematic realism, one strong focal point on the right, warm gold and deep navy, emotionally engaging but respectful composition.",
        "No words, no typography, letters, logos or watermarks.",
        "Video title: " + str(task.get("title", ""))[:150] + ".",
        "Biblical references: " + str(task.get("bibleReferences", "Not applicable"))[:220] + ".",
        str(task.get("thumbnailPrompt", ""))[:1200],
    ])
    thumbnail = post_json(api_base + "/v1/image", token, {
        "jobId": task_id,
        "fileName": "long-thumbnail",
        "prompt": thumbnail_prompt,
        "width": 1280,
        "height": 720,
    })
    if not thumbnail.get("assetKey"):
        raise RuntimeError("Cloudflare did not return a thumbnail asset key.")

    with tempfile.TemporaryDirectory(prefix="gospel-inspiration-long-") as temp:
        root = Path(temp)
        assets = root / "assets"
        output = root / "output"
        assets.mkdir()
        output.mkdir()
        manifest_sections = []
        total = 0

        for result in results:
            number = int(result["sectionNumber"])
            image_path = assets / ("scene-" + str(number).zfill(2) + safe_extension(result.get("imageContentType", "image/png"), ".img"))
            audio_path = assets / ("narration-" + str(number).zfill(2) + ".mp3")
            total += download_asset(api_base, token, result["imageAssetKey"], image_path)
            total += download_asset(api_base, token, result["narrationAssetKey"], audio_path)
            manifest_sections.append({
                "sectionNumber": number,
                "heading": result["heading"],
                "narrationText": result["narrationText"],
                "image": "assets/" + image_path.name,
                "narration": "assets/" + audio_path.name,
            })

        thumbnail_source = assets / ("thumbnail-source" + safe_extension(thumbnail.get("contentType", "image/png"), ".img"))
        thumbnail_path = assets / "thumbnail.png"
        total += download_asset(api_base, token, thumbnail["assetKey"], thumbnail_source)
        if total > MAX_TOTAL_BYTES:
            raise RuntimeError("Total media downloads exceeded the worker limit.")

        # The image provider's encoded output can vary by format. Cloudflare's
        # private thumbnail endpoint intentionally accepts PNG only, so normalize
        # every generated thumbnail to a real 1280x720 PNG before uploading.
        import subprocess
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(thumbnail_source),
                "-frames:v", "1",
                "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
                "-c:v", "png", "-compression_level", "9",
                str(thumbnail_path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if thumbnail_path.stat().st_size > 12 * 1024 * 1024:
            raise RuntimeError("The normalized PNG thumbnail still exceeds Cloudflare's 12 MiB limit.")
        with thumbnail_path.open("rb") as thumb_file:
            if thumb_file.read(8) != b"\x89PNG\r\n\x1a\n":
                raise RuntimeError("FFmpeg did not produce a valid PNG thumbnail.")
        print("Normalized thumbnail PNG:", thumbnail_path.stat().st_size, "bytes")

        thumbnail_store = upload_file(
            api_base, token, "/v1/tasks/" + task_id + "/thumbnail",
            thumbnail_path, "image/png", 12 * 1024 * 1024
        )

        manifest = {
            "taskId": task_id,
            "title": str(task.get("title", "Gospel Inspiration")),
            "description": str(task.get("description", "")),
            "bibleReferences": str(task.get("bibleReferences", "Not applicable")),
            "sections": manifest_sections,
            "thumbnail": "assets/thumbnail.png",
            "output": "output/gospel-inspiration-long.mp4",
        }
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        rendered_path = output / "gospel-inspiration-long.mp4"
        print("Rendering the 12-section landscape production with FFmpeg.")
        import subprocess
        subprocess.run(
            [sys.executable, "renderer/render_long.py", "--manifest", str(manifest_path)],
            check=True,
        )
        result = upload_file(
            api_base, token, "/v1/tasks/" + task_id + "/result",
            rendered_path, "video/mp4", MAX_RESULT_BYTES
        )
        print("Cloudflare confirmed long-video render:", result.get("status"), "size:", rendered_path.stat().st_size)
        print("Private thumbnail stored:", bool(thumbnail_store.get("thumbnailKey")))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    API = os.environ.get("RENDER_API_BASE_URL", "").rstrip("/")
    TOKEN = os.environ.get("FFMPEG_WORKER_TOKEN", "").strip()
    try:
        raise SystemExit(run(args.task_id))
    except Exception as error:
        message = str(error)
        print("LONG RENDER WORKER FAILED:", message, file=sys.stderr)
        if API.startswith("https://") and len(TOKEN) >= 32 and re.fullmatch(r"[0-9a-fA-F-]{36}", args.task_id):
            report_failure(API, TOKEN, args.task_id, message)
        lowered = message.lower()
        transient = any(marker in lowered for marker in (
            "http 408", "http 425", "http 429", "http 500", "http 502", "http 503", "http 504",
            "timed out", "timeout", "temporarily unavailable", "temporary network",
            "connection reset", "connection aborted", "remote end closed", "could not reach",
            "name or service not known", "temporary failure in name resolution"
        ))
        # Exit 75 is reserved for transient failures; the workflow retries only those.
        raise SystemExit(75 if transient else 1)
