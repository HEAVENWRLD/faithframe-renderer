#!/usr/bin/env python3
"""Safe resumable YouTube publisher for completed Gospel Inspiration videos.

OAuth credentials live only in GitHub Actions secrets. The video and the
resumable upload session are kept private in Cloudflare R2. A retry always
checks YouTube's acknowledged byte offset before sending further bytes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

CHUNK_BYTES = 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
RETRY_STATUSES = {429, 500, 502, 503, 504}


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirectHandler())


def open_http(request: urllib.request.Request, timeout: int = 120):
    try:
        return OPENER.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        # Google uses 308 Resume Incomplete as a normal protocol response.
        if exc.code == 308:
            return exc
        raise


def status_code(response) -> int:
    return int(getattr(response, "status", getattr(response, "code", 0)))


def api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer " + token,
        "User-Agent": "Gospel-Inspiration-YouTube-Publisher/1.0",
    }


def json_request(url: str, token: str, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = api_headers(token)
    headers["Accept"] = "application/json"
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", errors="replace")
        raise RuntimeError("Cloudflare publishing API returned HTTP " + str(exc.code) + ": " + detail[:300]) from None
    except Exception:
        raise RuntimeError("Could not reach the Cloudflare publishing API.") from None
    try:
        result = json.loads(raw.decode("utf-8"))
    except Exception:
        raise RuntimeError("Cloudflare returned an unreadable publishing response.") from None
    if not isinstance(result, dict):
        raise RuntimeError("Cloudflare returned an invalid publishing response.")
    if result.get("error"):
        raise RuntimeError(str(result["error"])[:500])
    return result


def refresh_access_token() -> str:
    client_id = os.environ.get("YOUTUBE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("YOUTUBE_REFRESH_TOKEN", "").strip()
    if not client_id or not client_secret or not refresh_token:
        raise RuntimeError(
            "GitHub Actions secrets YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET and "
            "YOUTUBE_REFRESH_TOKEN must be configured before automatic publishing."
        )
    payload = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("ascii")
    request = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read(100_000).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", errors="replace")
        # Avoid emitting or retaining credentials in error messages.
        raise RuntimeError("Google OAuth token refresh failed (HTTP " + str(exc.code) + "): " + detail[:250]) from None
    except Exception:
        raise RuntimeError("Could not reach Google's OAuth token endpoint.") from None
    token = str(result.get("access_token") or "")
    if not token:
        raise RuntimeError("Google OAuth did not return an access token.")
    return token


def read_task(api_base: str, worker_token: str, task_id: str) -> dict[str, Any]:
    task = json_request(api_base + "/v1/tasks/" + task_id, worker_token)
    if task.get("taskId") != task_id:
        raise RuntimeError("Cloudflare returned a different render task ID.")
    if task.get("status") != "ready":
        raise RuntimeError("The video render is not ready; YouTube upload was stopped.")
    if task.get("youtubeStatus") == "published" or task.get("youtubeVideoId"):
        print("This task already has a YouTube video ID; duplicate upload prevented.")
        return task
    if task.get("youtubeStatus") == "needs_review":
        raise RuntimeError("This task is marked needs_review and will not be uploaded automatically.")
    return task


def download_video(api_base: str, worker_token: str, task_id: str, destination: Path) -> int:
    request = urllib.request.Request(
        api_base + "/v1/tasks/" + task_id + "/result",
        headers=api_headers(worker_token),
        method="GET",
    )
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=300) as response, destination.open("wb") as output:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > MAX_VIDEO_BYTES:
                raise RuntimeError("The finished MP4 exceeds the 100 MB safe-upload limit.")
            while True:
                chunk = response.read(CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_VIDEO_BYTES:
                    raise RuntimeError("The finished MP4 exceeds the 100 MB safe-upload limit.")
                output.write(chunk)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Cloudflare could not provide the finished MP4 (HTTP " + str(exc.code) + ").") from None
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("The finished MP4 could not be downloaded from private storage.") from None
    if total < 50_000:
        raise RuntimeError("The downloaded MP4 is unexpectedly small.")
    with destination.open("rb") as source:
        signature = source.read(12)
    if len(signature) < 12 or signature[4:8] != b"ftyp":
        raise RuntimeError("The downloaded file is not a valid MP4.")
    return total


def store_session(api_base: str, worker_token: str, task_id: str, session_url: str, total_bytes: int) -> None:
    result = json_request(
        api_base + "/v1/tasks/" + task_id + "/youtube-session",
        worker_token,
        method="PUT",
        payload={"sessionUrl": session_url, "totalBytes": total_bytes},
    )
    if result.get("ok") is not True:
        raise RuntimeError("Cloudflare did not confirm that the resumable session was saved.")


def start_session(access_token: str, task: dict[str, Any], total_bytes: int) -> str:
    title = str(task.get("title") or "Gospel Inspiration Christian Video").strip()[:100]
    description = str(task.get("description") or "An original Christian video from Gospel Inspiration. Subscribe for more Bible stories, faith reflections, and prayer.").strip()[:4800]
    source_tags = task.get("tags") if isinstance(task.get("tags"), list) else []
    tags: list[str] = []
    used = 0
    for raw in source_tags:
        tag = re.sub(r"\s+", " ", str(raw or "")).strip()[:100]
        if not tag or tag.casefold() in {value.casefold() for value in tags}:
            continue
        cost = len(tag) + (1 if tags else 0)
        if len(tags) >= 15 or used + cost > 480:
            continue
        tags.append(tag)
        used += cost
    for fallback in ("Christianity", "Bible", "Christian Stories", "Faith", "Jesus"):
        if len(tags) >= 15:
            break
        if fallback.casefold() not in {value.casefold() for value in tags} and used + len(fallback) + 1 <= 480:
            tags.append(fallback)
            used += len(fallback) + 1

    metadata = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
        },
    }
    raw = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        data=raw,
        headers={
            **api_headers(access_token),
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(total_bytes),
            "X-Upload-Content-Type": "video/mp4",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            location = response.headers.get("Location", "")
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = exc.read(1500).decode("utf-8", errors="replace")
        raise RuntimeError("YouTube could not start the resumable upload (HTTP " + str(exc.code) + "): " + detail[:350]) from None
    except Exception:
        raise RuntimeError("Could not start the YouTube resumable upload session.") from None
    if status not in (200, 201) or not location:
        raise RuntimeError("YouTube did not return a valid resumable upload session URL.")
    parsed = urllib.parse.urlparse(location)
    if parsed.scheme != "https" or parsed.hostname != "www.googleapis.com" or not parsed.path.endswith("/youtube/v3/videos"):
        raise RuntimeError("YouTube returned an unexpected resumable session host or path.")
    return location


def retry_delay(attempt: int, response) -> float:
    raw = response.headers.get("Retry-After", "") if response is not None else ""
    if raw:
        try:
            seconds = float(raw)
            if seconds > 0:
                return min(30.0, seconds)
        except ValueError:
            pass
    return min(20.0, 1.0 * (2 ** max(0, attempt - 1)))


def next_confirmed_byte(response, total_bytes: int) -> int:
    value = response.headers.get("Range", "")
    if not value:
        return 0
    match = re.search(r"bytes\s*=\s*0-(\d+)", value, flags=re.I)
    if not match:
        raise RuntimeError("YouTube returned an unreadable upload Range header.")
    next_byte = int(match.group(1)) + 1
    if next_byte < 0 or next_byte > total_bytes:
        raise RuntimeError("YouTube returned an invalid confirmed upload offset.")
    return next_byte


def parse_completed_video(response) -> dict[str, Any]:
    raw = response.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
    try:
        result = json.loads(raw)
    except Exception:
        raise RuntimeError("YouTube completed the upload but returned unreadable video details.") from None
    if not result.get("id"):
        raise RuntimeError("YouTube completed the upload but returned no video ID.")
    return result


def inspect_session(session_url: str, access_token: str, total_bytes: int) -> tuple[int | None, dict[str, Any] | None]:
    request = urllib.request.Request(
        session_url,
        data=b"",
        headers={
            **api_headers(access_token),
            "Content-Length": "0",
            "Content-Range": "bytes */" + str(total_bytes),
        },
        method="PUT",
    )
    try:
        response = open_http(request, timeout=120)
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            raise RuntimeError(
                "The saved YouTube resumable session has expired. Automatic re-creation is blocked to avoid a duplicate video; check YouTube Studio before starting a new upload."
            ) from None
        if exc.code in RETRY_STATUSES:
            raise RuntimeError("YouTube upload status check failed transiently (HTTP " + str(exc.code) + ").") from None
        detail = exc.read(1000).decode("utf-8", errors="replace")
        raise RuntimeError("YouTube upload status check failed (HTTP " + str(exc.code) + "): " + detail[:250]) from None
    with response:
        if status_code(response) in (200, 201):
            return None, parse_completed_video(response)
        if status_code(response) == 308:
            return next_confirmed_byte(response, total_bytes), None
        raise RuntimeError("Unexpected YouTube upload status response: HTTP " + str(status_code(response)))


def resumable_upload(video_path: Path, session_url: str, access_token: str, total_bytes: int) -> dict[str, Any]:
    # Always inspect first, including after a workflow restart. Never assume
    # whether YouTube received the previous chunk if a connection was lost.
    last_error = ""
    for inspect_attempt in range(1, 4):
        try:
            offset, completed = inspect_session(session_url, access_token, total_bytes)
            if completed:
                return completed
            if offset is not None:
                break
        except RuntimeError as error:
            last_error = str(error)
            if "expired" in last_error.lower() or inspect_attempt == 3:
                raise
            time.sleep(retry_delay(inspect_attempt, None))
    else:
        raise RuntimeError("YouTube upload status could not be confirmed: " + last_error)

    offset = offset or 0
    retries = 0
    with video_path.open("rb") as source:
        while offset < total_bytes:
            end = min(total_bytes, offset + CHUNK_BYTES)
            source.seek(offset)
            chunk = source.read(end - offset)
            if len(chunk) != end - offset:
                raise RuntimeError("A video chunk could not be read completely from disk.")
            request = urllib.request.Request(
                session_url,
                data=chunk,
                headers={
                    **api_headers(access_token),
                    "Content-Type": "video/mp4",
                    "Content-Range": "bytes " + str(offset) + "-" + str(end - 1) + "/" + str(total_bytes),
                },
                method="PUT",
            )
            try:
                response = open_http(request, timeout=240)
                with response:
                    if status_code(response) in (200, 201):
                        if end != total_bytes:
                            raise RuntimeError("YouTube finalized the upload before all bytes were sent.")
                        return parse_completed_video(response)
                    if status_code(response) == 308:
                        confirmed = next_confirmed_byte(response, total_bytes)
                        if confirmed < offset or confirmed > end:
                            raise RuntimeError("YouTube acknowledged an unexpected upload position.")
                        if confirmed > offset:
                            offset = confirmed
                            retries = 0
                            continue
                        retries += 1
                        if retries > 6:
                            raise RuntimeError("YouTube repeatedly acknowledged no additional video bytes.")
                        time.sleep(retry_delay(retries, response))
                        continue
                    raise RuntimeError("Unexpected YouTube response while uploading: HTTP " + str(status_code(response)))
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 410):
                    raise RuntimeError(
                        "The saved YouTube resumable session expired. Automatic re-creation is blocked to avoid a duplicate video; check YouTube Studio before retrying."
                    ) from None
                if exc.code not in RETRY_STATUSES:
                    detail = exc.read(1000).decode("utf-8", errors="replace")
                    raise RuntimeError("YouTube rejected an upload chunk (HTTP " + str(exc.code) + "): " + detail[:250]) from None
                retries += 1
                if retries > 6:
                    raise RuntimeError("YouTube upload could not recover after repeated transient errors (HTTP " + str(exc.code) + ").") from None
                time.sleep(retry_delay(retries, exc))
                # The next loop iteration starts with a fresh status check.
                offset, completed = inspect_session(session_url, access_token, total_bytes)
                if completed:
                    return completed
                offset = offset or 0
            except (TimeoutError, OSError, urllib.error.URLError) as exc:
                retries += 1
                if retries > 6:
                    raise RuntimeError("YouTube upload could not recover after repeated network interruptions.") from None
                time.sleep(retry_delay(retries, None))
                offset, completed = inspect_session(session_url, access_token, total_bytes)
                if completed:
                    return completed
                offset = offset or 0
    # A last status check can recover the completed response if the final 201
    # was lost in transit after YouTube committed the video.
    offset, completed = inspect_session(session_url, access_token, total_bytes)
    if completed:
        return completed
    if offset != total_bytes:
        raise RuntimeError("YouTube has only confirmed " + str(offset or 0) + " of " + str(total_bytes) + " bytes.")
    raise RuntimeError("YouTube received all video bytes but has not returned the completed video resource.")


def verify_video(access_token: str, video_id: str) -> dict[str, Any]:
    url = "https://www.googleapis.com/youtube/v3/videos?part=id,status,processingDetails&id=" + urllib.parse.quote(video_id)
    request = urllib.request.Request(url, headers=api_headers(access_token))
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read(1_000_000).decode("utf-8"))
    except Exception:
        raise RuntimeError("YouTube accepted the file, but the new video could not be verified. The saved resumable session can be inspected safely.") from None
    items = result.get("items") or []
    if not items:
        raise RuntimeError("YouTube accepted the file, but the video resource was not returned by videos.list.")
    return items[0]


def upload_thumbnail(api_base: str, worker_token: str, task_id: str, access_token: str, video_id: str, processing_status: str) -> bool:
    if processing_status != "succeeded":
        return False
    request = urllib.request.Request(
        api_base + "/v1/tasks/" + task_id + "/thumbnail",
        headers=api_headers(worker_token),
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            data = response.read(12 * 1024 * 1024 + 1)
    except Exception:
        return False
    if len(data) < 1000 or len(data) > 12 * 1024 * 1024:
        return False
    url = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId=" + urllib.parse.quote(video_id)
    upload = urllib.request.Request(
        url,
        data=data,
        headers={**api_headers(access_token), "Content-Type": "image/png"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(upload, timeout=120) as response:
            response.read(1_000_000)
        return True
    except Exception:
        return False


def report_result(api_base: str, worker_token: str, task_id: str, video_id: str, privacy: str, made_for_kids: bool, thumbnail_applied: bool) -> dict[str, Any]:
    result = json_request(
        api_base + "/v1/tasks/" + task_id + "/youtube-result",
        worker_token,
        method="PUT",
        payload={
            "videoId": video_id,
            "privacyStatus": privacy,
            "madeForKids": made_for_kids,
            "thumbnailApplied": thumbnail_applied,
        },
    )
    if result.get("ok") is not True:
        raise RuntimeError("Cloudflare did not save the verified YouTube upload result.")
    return result


def report_failure(api_base: str, worker_token: str, task_id: str, message: str) -> None:
    try:
        json_request(
            api_base + "/v1/tasks/" + task_id + "/youtube-failure",
            worker_token,
            method="POST",
            payload={"message": str(message)[:700]},
        )
    except Exception:
        pass


def run(task_id: str) -> int:
    api_base = os.environ.get("RENDER_API_BASE_URL", "").rstrip("/")
    worker_token = os.environ.get("FFMPEG_WORKER_TOKEN", "").strip()
    if not api_base.startswith("https://"):
        raise RuntimeError("RENDER_API_BASE_URL must be the HTTPS Cloudflare Worker URL.")
    if len(worker_token) < 32:
        raise RuntimeError("FFMPEG_WORKER_TOKEN is missing or too short.")
    if not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89a-fA-F][0-9a-fA-F]{3}-[0-9a-fA-F]{12}", task_id):
        raise RuntimeError("A valid render task UUID is required.")

    task = read_task(api_base, worker_token, task_id)
    if task.get("youtubeVideoId"):
        print("Already uploaded: " + str(task["youtubeVideoId"]))
        return 0

    access_token = refresh_access_token()
    with __import__("tempfile").TemporaryDirectory(prefix="gospel-youtube-upload-") as temporary:
        video_path = Path(temporary) / "finished-video.mp4"
        total_bytes = download_video(api_base, worker_token, task_id, video_path)
        saved_total = int(task.get("youtubeTotalBytes") or 0)
        if saved_total and saved_total != total_bytes:
            raise RuntimeError("The saved upload session belongs to a different video size; automatic upload was stopped.")
        session_url = str(task.get("youtubeUploadSessionUrl") or "")
        if session_url:
            print("Resuming the saved YouTube session after checking the confirmed byte offset.")
        else:
            session_url = start_session(access_token, task, total_bytes)
            # Save the session before transmitting the first media byte. If a
            # runner stops, the next run can resume this exact session.
            store_session(api_base, worker_token, task_id, session_url, total_bytes)
            print("A YouTube resumable session is safely saved; uploading in 1 MiB chunks.")
        uploaded = resumable_upload(video_path, session_url, access_token, total_bytes)
        video_id = str(uploaded.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", video_id):
            raise RuntimeError("YouTube returned no valid video ID after upload.")
        verified = verify_video(access_token, video_id)
        status = verified.get("status") or {}
        privacy = str(status.get("privacyStatus") or uploaded.get("status", {}).get("privacyStatus") or "")
        made_for_kids = status.get("selfDeclaredMadeForKids")
        if privacy not in ("public", "unlisted", "private") or not isinstance(made_for_kids, bool):
            raise RuntimeError("YouTube returned incomplete visibility/audience status; automatic completion was not marked.")
        processing = str((verified.get("processingDetails") or {}).get("processingStatus") or "")
        thumbnail = False
        if processing == "succeeded":
            thumbnail = upload_thumbnail(api_base, worker_token, task_id, access_token, video_id, processing)
        saved = report_result(api_base, worker_token, task_id, video_id, privacy, made_for_kids, thumbnail)
        print("YouTube video verified:", saved.get("videoUrl"))
        print("Visibility:", privacy, "| Made for kids:", made_for_kids, "| Thumbnail applied:", thumbnail)
        if privacy != "public" or made_for_kids:
            raise RuntimeError("The upload completed, but YouTube did not confirm Public visibility and the expected audience settings. Check YouTube Studio.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    arguments = parser.parse_args()
    API_BASE = os.environ.get("RENDER_API_BASE_URL", "").rstrip("/")
    WORKER_TOKEN = os.environ.get("FFMPEG_WORKER_TOKEN", "").strip()
    try:
        raise SystemExit(run(arguments.task_id))
    except Exception as error:
        message = str(error).replace(API_BASE, "[Cloudflare API]")[:700]
        print("YOUTUBE PUBLISHER FAILED:", message, file=sys.stderr)
        if API_BASE.startswith("https://") and len(WORKER_TOKEN) >= 32 and re.fullmatch(r"[0-9a-fA-F-]{36}", arguments.task_id):
            report_failure(API_BASE, WORKER_TOKEN, arguments.task_id, message)
        raise SystemExit(1)
