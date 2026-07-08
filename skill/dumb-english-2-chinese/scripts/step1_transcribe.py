"""
Step 1: 从英文音频提取英文 SRT 字幕
用法:
  python step1_transcribe.py <audio_path> <output_srt> [model_size] [chunk_count] [overlap_sec]

默认把长音频切成 4 段，每段前后加 3 秒 overlap。每个分段独立缓存，
卡住或中断后可从未完成分段继续。最终合并 overlap 后再输出完整 SRT。
"""
import json
import math
import os
import subprocess
import sys
from dataclasses import dataclass

from pipeline_lock import file_lock

DEFAULT_MODEL_SIZE = "medium"
DEFAULT_CHUNK_COUNT = 4
DEFAULT_OVERLAP_SEC = 3.0


@dataclass
class Chunk:
    index: int
    core_start: float
    core_end: float
    extract_start: float
    extract_end: float


def format_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    millis_total = int(round(seconds * 1000))
    hours = millis_total // 3_600_000
    millis_total %= 3_600_000
    minutes = millis_total // 60_000
    millis_total %= 60_000
    secs = millis_total // 1000
    millis = millis_total % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def get_duration(path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def make_chunks(duration: float, chunk_count: int, overlap_sec: float) -> list[Chunk]:
    chunk_count = max(1, min(5, chunk_count))
    core_len = duration / chunk_count
    chunks = []
    for i in range(chunk_count):
        core_start = i * core_len
        core_end = duration if i == chunk_count - 1 else (i + 1) * core_len
        extract_start = max(0.0, core_start - overlap_sec)
        extract_end = min(duration, core_end + overlap_sec)
        chunks.append(Chunk(i, core_start, core_end, extract_start, extract_end))
    return chunks


def run_ffmpeg_extract(audio_path: str, chunk_path: str, start: float, end: float):
    if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 1000:
        return

    os.makedirs(os.path.dirname(chunk_path), exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            "-to", f"{end:.3f}",
            "-i", audio_path,
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            chunk_path,
        ],
        check=True, capture_output=True,
    )


def load_chunk_segments(json_path: str) -> list[dict]:
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)["segments"]


def save_chunk_segments(json_path: str, chunk: Chunk, segments: list[dict]):
    payload = {
        "chunk": {
            "index": chunk.index,
            "core_start": chunk.core_start,
            "core_end": chunk.core_end,
            "extract_start": chunk.extract_start,
            "extract_end": chunk.extract_end,
        },
        "segments": segments,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def transcribe_chunk(model, chunk_path: str, chunk: Chunk, json_path: str) -> list[dict]:
    if os.path.exists(json_path) and os.path.getsize(json_path) > 100:
        print(f"  [chunk {chunk.index + 1}] cached transcript exists, skipping")
        return load_chunk_segments(json_path)

    print(
        f"  [chunk {chunk.index + 1}] transcribing "
        f"{chunk.extract_start:.1f}s-{chunk.extract_end:.1f}s "
        f"(core {chunk.core_start:.1f}s-{chunk.core_end:.1f}s)"
    )
    segments_iter, info = model.transcribe(chunk_path, language="en")
    if chunk.index == 0:
        print(f"Detected language: {info.language} (probability: {info.language_probability:.2f})")

    segments = []
    for segment in segments_iter:
        start = chunk.extract_start + float(segment.start)
        end = chunk.extract_start + float(segment.end)
        text = segment.text.strip()
        if text:
            segments.append({"start": start, "end": end, "text": text})

    save_chunk_segments(json_path, chunk, segments)
    print(f"  [chunk {chunk.index + 1}] saved {len(segments)} raw segments")
    return segments


def belongs_to_core(segment: dict, chunk: Chunk, is_last: bool) -> bool:
    midpoint = (segment["start"] + segment["end"]) / 2.0
    if is_last:
        return chunk.core_start <= midpoint <= chunk.core_end
    return chunk.core_start <= midpoint < chunk.core_end


def merge_chunk_segments(chunk_segments: list[tuple[Chunk, list[dict]]]) -> list[dict]:
    merged = []
    for idx, (chunk, segments) in enumerate(chunk_segments):
        is_last = idx == len(chunk_segments) - 1
        for segment in segments:
            if belongs_to_core(segment, chunk, is_last):
                merged.append(segment)

    merged.sort(key=lambda item: (item["start"], item["end"]))

    deduped = []
    for segment in merged:
        if not deduped:
            deduped.append(segment)
            continue

        prev = deduped[-1]
        same_text = prev["text"].strip().lower() == segment["text"].strip().lower()
        overlaps = segment["start"] < prev["end"] + 0.25
        if same_text and overlaps:
            prev["end"] = max(prev["end"], segment["end"])
            continue

        deduped.append(segment)
    return deduped


def write_srt(segments: list[dict], output_srt: str):
    os.makedirs(os.path.dirname(output_srt), exist_ok=True)
    with open(output_srt, "w", encoding="utf-8") as f:
        for i, segment in enumerate(segments, start=1):
            f.write(f"{i}\n")
            f.write(f"{format_timestamp(segment['start'])} --> {format_timestamp(segment['end'])}\n")
            f.write(f"{segment['text']}\n\n")


def transcribe_to_srt(
    audio_path: str,
    output_srt: str,
    model_size: str = DEFAULT_MODEL_SIZE,
    chunk_count: int = DEFAULT_CHUNK_COUNT,
    overlap_sec: float = DEFAULT_OVERLAP_SEC,
):
    from faster_whisper import WhisperModel

    if not os.path.exists(audio_path):
        raise FileNotFoundError(f"Audio not found: {audio_path}")

    output_dir = os.path.dirname(output_srt)
    cache_dir = os.path.join(output_dir, "_step1_chunks")
    os.makedirs(cache_dir, exist_ok=True)

    duration = get_duration(audio_path)
    chunks = make_chunks(duration, chunk_count, overlap_sec)
    print(f"Audio duration: {duration:.1f}s")
    print(f"Chunked STT: {len(chunks)} chunks, overlap={overlap_sec:g}s")

    print(f"Loading faster-whisper model ({model_size})...")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    chunk_segments = []
    for chunk in chunks:
        chunk_path = os.path.join(cache_dir, f"chunk_{chunk.index + 1:02d}.wav")
        json_path = os.path.join(cache_dir, f"chunk_{chunk.index + 1:02d}.json")
        run_ffmpeg_extract(audio_path, chunk_path, chunk.extract_start, chunk.extract_end)
        segments = transcribe_chunk(model, chunk_path, chunk, json_path)
        chunk_segments.append((chunk, segments))

    merged_segments = merge_chunk_segments(chunk_segments)
    write_srt(merged_segments, output_srt)

    print(f"\nDone! Raw chunk segments: {sum(len(s) for _, s in chunk_segments)}")
    print(f"Done! Merged final segments: {len(merged_segments)}")
    print(f"SRT saved to: {output_srt}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "Usage: python step1_transcribe.py <audio_path> <output_srt> "
            "[model_size] [chunk_count] [overlap_sec]"
        )
        sys.exit(1)

    audio_file = sys.argv[1]
    output_file = sys.argv[2]
    model_arg = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_MODEL_SIZE
    chunk_count_arg = int(sys.argv[4]) if len(sys.argv) > 4 else DEFAULT_CHUNK_COUNT
    overlap_arg = float(sys.argv[5]) if len(sys.argv) > 5 else DEFAULT_OVERLAP_SEC

    with file_lock(output_file + ".lock", "Step 1 STT"):
        transcribe_to_srt(
            audio_file,
            output_file,
            model_size=model_arg,
            chunk_count=chunk_count_arg,
            overlap_sec=overlap_arg,
        )
