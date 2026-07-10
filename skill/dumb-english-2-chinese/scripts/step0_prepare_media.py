"""
Step 0: Prepare audio assets from a single input video.

Usage:
  python step0_prepare_media.py <video_path> <output_dir> [reference_start] [reference_end]

Outputs:
  <output_dir>/source_audio.mp3
  <output_dir>/reference_audio.mp3  (only when reference_start and reference_end are provided)
"""
import os
import re
import subprocess
import sys

TIME_RE = re.compile(r"^\d+(?::\d{1,2}){0,2}(?:[.,]\d+)?$")


def normalize_time(value: str) -> str:
    value = value.strip().replace(",", ".")
    if not TIME_RE.match(value):
        raise ValueError(f"Invalid time value: {value}")
    return value


def run_ffmpeg(args: list[str]):
    subprocess.run(args, check=True, capture_output=True)


def extract_source_audio(video_path: str, output_dir: str) -> str:
    source_audio = os.path.join(output_dir, "source_audio.mp3")
    if os.path.exists(source_audio) and os.path.getsize(source_audio) > 1000:
        print(f"Source audio exists: {source_audio}")
        return source_audio

    print("[0a] Extracting full source audio from video...")
    run_ffmpeg(
        [
            "ffmpeg", "-y",
            "-i", video_path,
            "-vn",
            "-codec:a", "libmp3lame",
            "-q:a", "2",
            source_audio,
        ]
    )
    print(f"[0a] Source audio: {source_audio}")
    return source_audio


def extract_reference_audio(video_path: str, output_dir: str, start: str, end: str) -> str:
    reference_audio = os.path.join(output_dir, "reference_audio.mp3")
    print(f"[0b] Extracting reference audio from {start} to {end}...")
    run_ffmpeg(
        [
            "ffmpeg", "-y",
            "-ss", start,
            "-to", end,
            "-i", video_path,
            "-vn",
            "-codec:a", "libmp3lame",
            "-q:a", "2",
            reference_audio,
        ]
    )
    print(f"[0b] Reference audio: {reference_audio}")
    return reference_audio


def main():
    if len(sys.argv) not in {3, 5}:
        print("Usage: python step0_prepare_media.py <video_path> <output_dir> [reference_start] [reference_end]")
        sys.exit(1)

    video_path = sys.argv[1]
    output_dir = sys.argv[2]
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")

    os.makedirs(output_dir, exist_ok=True)
    source_audio = extract_source_audio(video_path, output_dir)

    reference_audio = ""
    if len(sys.argv) == 5:
        start = normalize_time(sys.argv[3])
        end = normalize_time(sys.argv[4])
        reference_audio = extract_reference_audio(video_path, output_dir, start, end)

    print("")
    print("Prepared media:")
    print(f"  source_audio={source_audio}")
    if reference_audio:
        print(f"  reference_audio={reference_audio}")


if __name__ == "__main__":
    main()
