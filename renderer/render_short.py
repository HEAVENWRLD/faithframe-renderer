#!/usr/bin/env python3
"""Render Gospel Inspiration vertical Shorts with FFmpeg.

The manifest points only to files already downloaded to the runner. This
renderer does not fetch remote URLs or read credentials.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

WIDTH, HEIGHT, FPS = 720, 1280, 24
OUTRO_SECONDS = 3.0
MAX_NARRATION_SECONDS = 180.0
MIN_SCENES, MAX_SCENES = 4, 30


def run(command: list[str], capture: bool = False) -> str:
    try:
        result = subprocess.run(
            command, check=True, text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "No command output")[-5000:]
        raise RuntimeError(f"Command failed ({exc.returncode}):\n{detail}") from exc
    return result.stdout.strip() if capture else ""


def resolve_file(base: Path, value: Any, label: str, required: bool = True) -> Path | None:
    if value in (None, "") and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Manifest field '{label}' must be a non-empty file path.")
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Required {label} file was not found: {path.name}")
    return path


def probe_duration(path: Path) -> float:
    raw = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ], capture=True)
    try:
        duration = float(raw)
    except ValueError as exc:
        raise ValueError("Could not read the narration duration.") from exc
    if not math.isfinite(duration) or duration <= 0 or duration > MAX_NARRATION_SECONDS:
        raise ValueError(f"Narration must be between 0 and {MAX_NARRATION_SECONDS:g} seconds.")
    return duration


def parse_srt(path: Path) -> list[dict[str, Any]]:
    content = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    blocks = re.split(r"\r?\n\s*\r?\n", content)
    events: list[dict[str, Any]] = []

    def to_seconds(value: str) -> float:
        match = re.search(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})", value)
        if not match:
            return 0.0
        hours, minutes, secs, millis = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + int(secs) + float("0." + millis)

    for block in blocks:
        lines = block.splitlines()
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if timing_index < 0:
            continue
        start_text, end_text = lines[timing_index].split("-->", 1)
        start, end = to_seconds(start_text.strip()), to_seconds(end_text.strip())
        text = re.sub(r"<[^>]+>", "", " ".join(lines[timing_index + 1:])).strip()
        if text and end > start:
            events.append({"start": start, "end": end, "text": text})
    if not events:
        raise ValueError("Timed SRT captions are empty or invalid.")
    return events


def scene_starts(count: int, captions: list[dict[str, Any]], duration: float) -> list[float]:
    starts = [0.0]
    for index in range(1, count):
        caption = captions[min(len(captions) - 1, math.floor(index * len(captions) / count))]
        candidate = float(caption["start"])
        latest = max(starts[index - 1] + 0.25, duration - 0.25 * (count - index))
        starts.append(min(max(starts[index - 1] + 0.25, candidate), latest))
    return starts


def split_caption_text(value: str, max_words: int = 7) -> list[str]:
    words = re.sub(r"\s+", " ", str(value or "")).strip().split(" ")
    groups: list[str] = []
    current: list[str] = []
    for word in words:
        if not word:
            continue
        current.append(word)
        if len(current) >= max_words or (
            len(current) >= 4 and re.search(r"[,.!?;:]$", word)
        ):
            groups.append(" ".join(current))
            current = []
    if current:
        if len(current) <= 2 and groups:
            groups[-1] += " " + " ".join(current)
        else:
            groups.append(" ".join(current))
    return groups


def caption_weight(value: str) -> float:
    words = re.findall(r"[a-z0-9’'-]+", str(value or "").lower())
    weight = 0.0
    for word in words:
        letters = re.sub(r"[^a-z]", "", word)
        syllables = max(1, len(re.findall(r"[aeiouy]+", letters)))
        if letters.endswith("e") and syllables > 1 and not letters.endswith("le"):
            syllables -= 1
        weight += max(1, syllables)
    ending = str(value or "").strip()[-1:]
    if ending in ".!?":
        weight += 2.2
    elif ending in ";:":
        weight += 1.2
    elif ending == ",":
        weight += 0.7
    return max(1.0, weight)


def normalize_captions(events: list[dict[str, Any]], duration: float) -> list[dict[str, Any]]:
    # Hatchable currently stores the entire narration as one SRT cue. Split
    # that coarse cue into short phrases and time them across the real audio.
    coarse = len(events) == 1 and len(str(events[0].get("text", "")).split()) > 12
    output: list[dict[str, Any]] = []
    for event in events:
        value = re.sub(r"\s+", " ", str(event.get("text", "")).strip())
        if not value:
            continue
        start = 0.0 if coarse else max(0.0, min(duration, float(event.get("start", 0))))
        raw_end = float(event.get("end", 0))
        end = duration if coarse else max(start + 0.1, min(duration, raw_end if raw_end > start else duration))
        groups = split_caption_text(value)
        weights = [caption_weight(group) for group in groups]
        total_weight = sum(weights) or 1.0
        cursor = start
        for index, group in enumerate(groups):
            next_time = end if index == len(groups) - 1 else cursor + (end - start) * weights[index] / total_weight
            output.append({
                "start": max(0.0, min(duration, cursor)),
                "end": max(0.0, min(duration, max(cursor + 0.12, next_time))),
                "text": group,
            })
            cursor = next_time
    return [item for item in output if item["end"] > item["start"] and item["text"]]


def format_srt_timestamp(value: float) -> str:
    milliseconds = max(0, int(round(float(value) * 1000)))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def write_normalized_srt(events: list[dict[str, Any]], destination: Path) -> None:
    blocks: list[str] = []
    for event in events:
        text = re.sub(r"\s+", " ", str(event.get("text", "")).strip())
        start, end = float(event["start"]), float(event["end"])
        if not text or end <= start:
            continue
        blocks.append(
            f"{len(blocks) + 1}\n"
            f"{format_srt_timestamp(start)} --> {format_srt_timestamp(end)}\n"
            f"{text}"
        )
    if not blocks:
        raise ValueError("No usable caption phrases remain after timing normalization.")
    destination.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def filter_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a 9:16 Gospel Inspiration Short.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", help="Optional output-path override.")
    args = parser.parse_args()

    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.is_file():
        raise FileNotFoundError("Render manifest was not found.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Render manifest must be a JSON object.")
    base = manifest_path.parent

    raw_scenes = manifest.get("scenes")
    if not isinstance(raw_scenes, list) or not MIN_SCENES <= len(raw_scenes) <= MAX_SCENES:
        raise ValueError(f"Provide between {MIN_SCENES} and {MAX_SCENES} scene image paths.")
    scenes = [resolve_file(base, value, f"scene {i + 1}") for i, value in enumerate(raw_scenes)]
    narration = resolve_file(base, manifest.get("narration"), "narration")
    music = resolve_file(base, manifest.get("music"), "background music")
    srt = resolve_file(base, manifest.get("captions_srt"), "SRT captions")
    outro = resolve_file(base, manifest.get("outro"), "outro card", required=False)

    title = str(manifest.get("title") or "A Message of Faith").replace("\n", " ").replace("\r", " ")[:120]
    narration_duration = probe_duration(narration)
    captions = normalize_captions(parse_srt(srt), narration_duration)
    normalized_srt = srt.parent / "captions-normalized.srt"
    write_normalized_srt(captions, normalized_srt)
    starts = scene_starts(len(scenes), captions, narration_duration)
    total_duration = narration_duration + OUTRO_SECONDS

    output_value = args.output or manifest.get("output") or "output/faithframe-short.mp4"
    output_path = Path(output_value)
    if not output_path.is_absolute():
        output_path = base / output_path
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Hatchable stores its generated outro card as SVG; rasterize it before FFmpeg reads it.
    if outro and outro.suffix.lower() == ".svg":
        raster_outro = output_path.parent / "render-outro.png"
        run(["rsvg-convert", "--width", str(WIDTH), "--height", str(HEIGHT),
             "--output", str(raster_outro), str(outro)])
        if not raster_outro.is_file() or raster_outro.stat().st_size < 1000:
            raise RuntimeError("The SVG outro card could not be converted to PNG.")
        outro = raster_outro

    title_file = output_path.parent / "render-title.txt"
    brand_file = output_path.parent / "render-brand.txt"
    title_lines = textwrap.wrap(title, width=42, max_lines=2, placeholder="…") or ["A Message of Faith"]
    title_file.write_text("\n".join(title_lines), encoding="utf-8")
    brand_file.write_text("✝  GOSPEL INSPIRATION", encoding="utf-8")

    command = ["ffmpeg", "-hide_banner", "-y", "-loglevel", "warning"]
    for index, scene in enumerate(scenes):
        end = starts[index + 1] if index + 1 < len(starts) else narration_duration
        clip_duration = end - starts[index]
        if clip_duration <= 0:
            raise ValueError("Scene timing produced an empty image segment.")
        command += ["-loop", "1", "-framerate", str(FPS), "-t", f"{clip_duration:.6f}", "-i", str(scene)]

    outro_index = len(scenes)
    if outro:
        command += ["-loop", "1", "-framerate", str(FPS), "-t", str(OUTRO_SECONDS), "-i", str(outro)]
    else:
        command += ["-f", "lavfi", "-t", str(OUTRO_SECONDS),
                    "-i", f"color=c=0x070913:s={WIDTH}x{HEIGHT}:r={FPS}"]

    narration_index, music_index = outro_index + 1, outro_index + 2
    command += ["-i", str(narration), "-stream_loop", "-1", "-i", str(music)]

    graph: list[str] = []
    story_inputs: list[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else narration_duration
        clip_duration = end - start
        label = f"scene{index}"
        graph.append(
            f"[{index}:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},"
            f"zoompan=z='min(zoom+0.00025,1.06)':"
            f"x='iw/2-(iw/zoom/2)+sin(on/80)*5':"
            f"y='ih/2-(ih/zoom/2)':d=1:s={WIDTH}x{HEIGHT}:fps={FPS},"
            f"trim=duration={clip_duration:.6f},setpts=PTS-STARTPTS,"
            f"setsar=1,format=yuv420p[{label}]"
        )
        story_inputs.append(f"[{label}]")
    graph.append("".join(story_inputs) + f"concat=n={len(scenes)}:v=1:a=0[story]")

    if outro:
        graph.append(
            f"[{outro_index}:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},fps={FPS},trim=duration={OUTRO_SECONDS},"
            f"setpts=PTS-STARTPTS,setsar=1,format=yuv420p[outro]"
        )
    else:
        font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        graph.append(
            f"[{outro_index}:v]"
            f"drawtext=fontfile='{font}':text='GOSPEL INSPIRATION':fontcolor=#9beef1:"
            f"fontsize=30:x=(w-text_w)/2:y=390,"
            f"drawtext=fontfile='{font}':text='PLEASE SUBSCRIBE':fontcolor=white:"
            f"fontsize=54:x=(w-text_w)/2:y=560,"
            f"drawtext=fontfile='{font}':text='May God bless you':fontcolor=#ffe6aa:"
            f"fontsize=30:x=(w-text_w)/2:y=680,fps={FPS},format=yuv420p[outro]"
        )

    style = (
        "FontName=DejaVu Sans,FontSize=30,Bold=1,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H80000000,BackColour=&H90040712,BorderStyle=3,"
        "Outline=1,Shadow=0,Alignment=2,MarginV=142"
    )
    graph.append(f"[story]subtitles=filename='{filter_path(normalized_srt)}':force_style='{style}'[captioned]")
    graph.append("[captioned]drawbox=x=0:y=0:w=720:h=172:color=black@0.48:t=fill:enable='lt(t,3.3)'[headerbg]")
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    graph.append(
        f"[headerbg]drawtext=fontfile='{font}':textfile='{filter_path(brand_file)}':"
        "fontsize=20:fontcolor=#9beef1:x=38:y=32:enable='lt(t,3.3)'[brand]"
    )
    graph.append(
        f"[brand]drawtext=fontfile='{font}':textfile='{filter_path(title_file)}':"
        "fontsize=25:fontcolor=#fff5dc:x=38:y=76:enable='lt(t,3.3)'[titled]"
    )
    graph.append("[titled][outro]concat=n=2:v=1:a=0[vout]")

    graph.append(
        f"[{narration_index}:a]aresample=48000,volume=1.0[voice];"
        f"[{music_index}:a]aresample=48000,volume=0.14,"
        f"atrim=duration={total_duration:.6f},asetpts=PTS-STARTPTS[music];"
        f"[voice][music]amix=inputs=2:duration=longest:dropout_transition=2,"
        f"atrim=duration={total_duration:.6f},alimiter=limit=0.95[aout]"
    )

    command += [
        "-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]",
        "-t", f"{total_duration:.6f}", "-r", str(FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(output_path),
    ]

    print(f"Rendering {len(scenes)} scenes; narration {narration_duration:.1f}s; total {total_duration:.1f}s.")
    run(command)
    if not output_path.is_file() or output_path.stat().st_size < 20_000:
        raise RuntimeError("FFmpeg did not produce a valid-size video.")

    metadata = json.loads(run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration,size:stream=codec_name,width,height", "-of", "json", str(output_path),
    ], capture=True))
    video = next((s for s in metadata.get("streams", []) if s.get("codec_name") == "h264"), None)
    audio = next((s for s in metadata.get("streams", []) if s.get("codec_name") == "aac"), None)
    if not video or not audio:
        raise RuntimeError("Output is missing H.264 video or AAC audio.")
    if int(video.get("width", 0)) != WIDTH or int(video.get("height", 0)) != HEIGHT:
        raise RuntimeError("Output is not 720x1280 portrait.")
    actual_duration = float(metadata.get("format", {}).get("duration", 0))
    if abs(actual_duration - total_duration) > 0.75:
        raise RuntimeError(f"Duration mismatch: expected {total_duration:.1f}s, got {actual_duration:.1f}s.")

    print(json.dumps({
        "status": "completed", "output": str(output_path),
        "size_bytes": output_path.stat().st_size,
        "duration_seconds": round(actual_duration, 2),
        "width": WIDTH, "height": HEIGHT,
        "video_codec": "h264", "audio_codec": "aac", "scene_count": len(scenes),
    }, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"RENDER FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
