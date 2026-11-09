#!/usr/bin/env python3
"""Render a 12-section Gospel Inspiration long video with FFmpeg.

The render is landscape 1280x720. Each section has one FLUX-generated scene,
one Fish Audio narration clip and captions timed from that narration script.
An original oscillator-based ambient bed is generated locally; no music model
or copyrighted recording is required.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import re
import struct
import subprocess
import wave
from pathlib import Path
from typing import Any

WIDTH = 1280
HEIGHT = 720
FPS = 15
MIN_TOTAL_SECONDS = 480
MAX_TOTAL_SECONDS = 720
OUTRO_SECONDS = 3
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def run(command: list[str], label: str) -> None:
    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()
        message = " ".join(detail[-8:])[:1200]
        raise RuntimeError(label + " failed: " + message) from None


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        check=True, capture_output=True, text=True,
    )
    try:
        duration = float(result.stdout.strip())
    except Exception:
        raise RuntimeError("Could not read the narration duration for " + path.name) from None
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("The narration duration is invalid for " + path.name)
    return duration


def probe_streams(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "stream=codec_type,codec_name,width,height",
            "-show_entries", "format=duration,size", "-of", "json", str(path),
        ],
        check=True, capture_output=True, text=True,
    )
    try:
        return json.loads(result.stdout)
    except Exception:
        raise RuntimeError("FFprobe returned invalid output details.") from None


def timestamp(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    hours = ms // 3_600_000
    minutes = (ms % 3_600_000) // 60_000
    secs = (ms % 60_000) // 1000
    millis = ms % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def caption_groups(text: str) -> list[str]:
    words = re.sub(r"\s+", " ", str(text or "")).strip().split(" ")
    words = [word for word in words if word]
    return [" ".join(words[index:index + 7]) for index in range(0, len(words), 7)]


def make_srt(text: str, duration: float, output: Path) -> None:
    groups = caption_groups(text)
    if not groups:
        raise RuntimeError("A narration section has no caption text.")
    weights = [max(1, len(re.sub(r"[^a-z0-9]", "", group.lower()))) for group in groups]
    total_weight = sum(weights) or 1
    cursor = 0.0
    entries = []
    for index, group in enumerate(groups):
        end = duration if index == len(groups) - 1 else cursor + duration * weights[index] / total_weight
        entries.append(
            str(index + 1) + "\n" + timestamp(cursor) + " --> " + timestamp(end) + "\n" + group + "\n"
        )
        cursor = end
    output.write_text("\n".join(entries), encoding="utf-8")


def ffmpeg_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace(":", "\\:")


def make_outro_svg(title: str, path: Path) -> None:
    safe = html.escape(str(title or "Gospel Inspiration")[:75])
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">
<defs>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#070913"/><stop offset=".54" stop-color="#1b1734"/><stop offset="1" stop-color="#09212a"/></linearGradient>
<radialGradient id="gold"><stop stop-color="#f3d68d" stop-opacity=".4"/><stop offset="1" stop-color="#f3d68d" stop-opacity="0"/></radialGradient>
</defs>
<rect width="1280" height="720" fill="url(#bg)"/>
<circle cx="640" cy="230" r="420" fill="url(#gold)"/>
<g fill="none" stroke="#8fe5e9" stroke-opacity=".24" stroke-width="2"><ellipse cx="640" cy="210" rx="300" ry="118" transform="rotate(-24 640 210)"/><ellipse cx="640" cy="210" rx="205" ry="290" transform="rotate(28 640 210)"/></g>
<text x="640" y="188" font-family="DejaVu Sans" font-size="25" font-weight="bold" text-anchor="middle" fill="#9be9ec" letter-spacing="5">GOSPEL INSPIRATION</text>
<text x="640" y="302" font-family="DejaVu Sans" font-size="51" font-weight="bold" text-anchor="middle" fill="#fff5de">THANK YOU FOR WATCHING</text>
<text x="640" y="410" font-family="DejaVu Sans" font-size="70" font-weight="bold" text-anchor="middle" fill="#f38188">PLEASE SUBSCRIBE</text>
<text x="640" y="492" font-family="DejaVu Sans" font-size="26" text-anchor="middle" fill="#e9ecf7">More Bible stories, faith reflections and prayer</text>
<text x="640" y="550" font-family="DejaVu Sans" font-size="29" font-weight="bold" text-anchor="middle" fill="#8fe5e9">May God bless you.</text>
<text x="640" y="610" font-family="DejaVu Sans" font-size="18" text-anchor="middle" fill="#bfc4d8">{safe}</text>
</svg>"""
    path.write_text(svg, encoding="utf-8")


def make_ambient_music(path: Path, seed_text: str, seconds: int = 62) -> None:
    sample_rate = 22050
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
    def midi(note: int) -> float:
        return 440.0 * (2.0 ** ((note - 69) / 12.0))
    chords = [(midi(root + transpose), [midi(note + transpose) for note in notes]) for root, notes in progressions]
    arp_notes = [62, 69, 66, 74, 59, 66, 62, 71, 55, 62, 59, 67, 57, 64, 61, 69]
    arp = [midi(note + transpose) for note in arp_notes]
    beat = 60.0 / 72.0
    arp_interval = beat / 2.0
    random_state = seed or 1
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        frames = bytearray()
        for index in range(sample_count):
            t = index / sample_rate
            chord_index = int(t / 4.0) % len(chords)
            within = t % 4.0
            root, notes = chords[chord_index]
            envelope = min(1.0, within / 0.7, (4.0 - within) / 0.6)
            left_pad = 0.0
            right_pad = 0.0
            for frequency in notes:
                left_pad += math.sin(math.tau * frequency * t) * 0.55 + math.sin(math.tau * frequency * 2 * t) * 0.08
                right_pad += math.sin(math.tau * frequency * 1.0015 * t + 0.08) * 0.55 + math.sin(math.tau * frequency * 2 * t) * 0.08
            left_pad *= 0.25 * envelope
            right_pad *= 0.25 * envelope
            arp_index = int(t / arp_interval) % len(arp)
            arp_age = t % arp_interval
            frequency = arp[arp_index]
            pluck = math.exp(-arp_age * 5.5)
            left_arp = (math.sin(math.tau * frequency * t) + math.sin(math.tau * frequency * 2.01 * t) * 0.23) * pluck * 0.095
            right_arp = (math.sin(math.tau * frequency * 1.002 * t + 0.2) + math.sin(math.tau * frequency * 2.005 * t) * 0.18) * pluck * 0.09
            bass = math.sin(math.tau * root * t) * 0.11
            beat_age = t % beat
            kick = math.sin(math.tau * (82.0 - 38.0 * beat_age / 0.16) * beat_age) * math.exp(-beat_age * 23) * 0.17 if beat_age < 0.16 else 0.0
            hat = 0.0
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


def render_scene(section: dict[str, Any], duration: float, index: int, root: Path, output: Path, title: str) -> Path:
    image = (root / str(section["image"])).resolve()
    caption = root / "captions" / ("section-" + str(index + 1).zfill(2) + ".srt")
    caption.parent.mkdir(exist_ok=True)
    make_srt(str(section.get("narrationText", "")), duration, caption)
    heading_file = root / "captions" / ("heading-" + str(index + 1).zfill(2) + ".txt")
    heading_file.write_text("CHAPTER " + str(index + 1) + "  |  " + str(section.get("heading", ""))[:90], encoding="utf-8")
    title_file = root / "captions" / "video-title.txt"
    if index == 0:
        title_file.write_text(str(title)[:110], encoding="utf-8")

    filters = [
        "scale=1280:720:force_original_aspect_ratio=increase",
        "crop=1280:720",
        "zoompan=z='min(zoom+0.00003,1.05)':x='iw/2-(iw/zoom/2)+sin(on/120)*4':y='ih/2-(ih/zoom/2)+cos(on/130)*3':d=1:s=1280x720:fps=15",
        "trim=duration=" + str(duration),
        "setpts=PTS-STARTPTS",
        "setsar=1",
        "format=yuv420p",
        "drawtext=fontfile=" + ffmpeg_path(Path(FONT_BOLD)) + ":textfile=" + ffmpeg_path(heading_file) + ":x=54:y=36:fontsize=23:fontcolor=0xfff0ce:box=1:boxcolor=black@0.48:boxborderw=12:enable='lt(t,4)'",
    ]
    if index == 0:
        filters.append(
            "drawtext=fontfile=" + ffmpeg_path(Path(FONT_BOLD)) + ":textfile=" + ffmpeg_path(title_file) +
            ":x=54:y=91:fontsize=31:fontcolor=white:box=1:boxcolor=black@0.42:boxborderw=12:enable='lt(t,6)'"
        )
    filters.append(
        "subtitles=" + ffmpeg_path(caption) +
        ":force_style='FontName=DejaVu Sans,FontSize=23,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H80000000,BorderStyle=3,Outline=1,Shadow=0,MarginV=42'"
    )
    output_path = output / ("section-" + str(index + 1).zfill(2) + ".mp4")
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-framerate", str(FPS), "-t", str(duration), "-i", str(image),
        "-vf", ",".join(filters),
        "-an", "-t", str(duration),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-r", str(FPS), "-b:v", "650k", "-maxrate", "850k", "-bufsize", "1700k",
        "-movflags", "+faststart", str(output_path),
    ], "Landscape scene " + str(index + 1) + " render")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    manifest_path = Path(args.manifest).resolve()
    root = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sections = manifest.get("sections")
    if not isinstance(sections, list) or len(sections) != 12:
        raise RuntimeError("The landscape video must contain exactly 12 sections.")
    sections = sorted(sections, key=lambda item: int(item.get("sectionNumber", 0)))
    if [int(item.get("sectionNumber", 0)) for item in sections] != list(range(1, 13)):
        raise RuntimeError("The 12 long-video sections are incomplete or out of order.")

    durations = []
    audio_paths = []
    for section in sections:
        narration = (root / str(section["narration"])).resolve()
        image = (root / str(section["image"])).resolve()
        if not narration.is_file() or not image.is_file():
            raise RuntimeError("A scene image or narration clip is missing.")
        duration = probe_duration(narration)
        if duration < 25 or duration > 100:
            raise RuntimeError("Section " + str(section["sectionNumber"]) + " narration is " + str(round(duration)) + " seconds; expected 25-100 seconds.")
        durations.append(duration)
        audio_paths.append(narration)

    voice_duration = sum(durations)
    total_duration = voice_duration + OUTRO_SECONDS
    if total_duration < MIN_TOTAL_SECONDS or total_duration > MAX_TOTAL_SECONDS:
        raise RuntimeError("Final duration would be " + str(round(total_duration / 60, 1)) + " minutes; automatic long-video production requires 8-12 minutes.")

    output_path = (root / str(manifest.get("output", "output/gospel-inspiration-long.mp4"))).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_dir = output_path.parent
    segments = []
    for index, (section, duration) in enumerate(zip(sections, durations)):
        segments.append(render_scene(section, duration, index, root, output_dir, str(manifest.get("title", "Gospel Inspiration"))))

    outro_svg = root / "outro.svg"
    outro_png = root / "outro.png"
    make_outro_svg(str(manifest.get("title", "Gospel Inspiration")), outro_svg)
    run(["rsvg-convert", "-w", str(WIDTH), "-h", str(HEIGHT), "-o", str(outro_png), str(outro_svg)], "Subscribe outro image")
    outro_video = output_dir / "outro.mp4"
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-framerate", str(FPS), "-t", str(OUTRO_SECONDS), "-i", str(outro_png),
        "-an", "-t", str(OUTRO_SECONDS), "-c:v", "libx264", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-b:v", "650k", "-maxrate", "850k",
        "-bufsize", "1700k", "-movflags", "+faststart", str(outro_video),
    ], "Subscribe outro video")

    video_list = root / "video-concat.txt"
    video_list.write_text("".join("file '" + str(path.resolve()) + "'\n" for path in [*segments, outro_video]), encoding="utf-8")
    joined_video = output_dir / "joined-video.mp4"
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(video_list), "-c", "copy", "-movflags", "+faststart", str(joined_video),
    ], "Long-video scene concatenation")

    audio_list = root / "audio-concat.txt"
    audio_list.write_text("".join("file '" + str(path.resolve()) + "'\n" for path in audio_paths), encoding="utf-8")
    voice_track = root / "voice-track.wav"
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(audio_list),
        "-vn", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(voice_track),
    ], "Narration audio join")

    music_path = root / "ambient-music.wav"
    make_ambient_music(music_path, str(manifest.get("title", "Gospel Inspiration")))

    mix_filter = (
        "[1:a]apad=pad_dur=3,asetpts=PTS-STARTPTS[voice];"
        "[2:a]volume=0.13,afade=t=out:st=" + str(voice_duration) + ":d=3[music];"
        "[voice][music]amix=inputs=2:duration=longest:dropout_transition=0,alimiter=limit=0.95[aout]"
    )
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(joined_video),
        "-i", str(voice_track),
        "-stream_loop", "-1", "-i", str(music_path),
        "-filter_complex", mix_filter,
        "-map", "0:v:0", "-map", "[aout]",
        "-t", str(total_duration),
        "-c:v", "copy", "-c:a", "aac", "-b:a", "112k", "-ar", "48000",
        "-movflags", "+faststart", str(output_path),
    ], "Final long-video audio mix and MP4 mux")

    info = probe_streams(output_path)
    stream_list = info.get("streams", [])
    video = next((item for item in stream_list if item.get("codec_type") == "video"), None)
    audio = next((item for item in stream_list if item.get("codec_type") == "audio"), None)
    if not video or video.get("codec_name") != "h264" or video.get("width") != WIDTH or video.get("height") != HEIGHT:
        raise RuntimeError("Final video did not pass the 1280x720 H.264 validation.")
    if not audio or audio.get("codec_name") != "aac":
        raise RuntimeError("Final video did not pass the AAC audio validation.")
    final_duration = float(info.get("format", {}).get("duration", 0))
    if final_duration < MIN_TOTAL_SECONDS or final_duration > MAX_TOTAL_SECONDS:
        raise RuntimeError("Final MP4 duration failed the 8-12 minute check.")
    size = output_path.stat().st_size
    if size < 250_000 or size > 100 * 1024 * 1024:
        raise RuntimeError("Final MP4 size is outside the allowed upload range.")
    print("Long video validated: 1280x720 H.264/AAC, " + str(round(final_duration, 1)) + " seconds, " + str(size) + " bytes.")
    print("Thumbnail is stored separately in Cloudflare R2.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
