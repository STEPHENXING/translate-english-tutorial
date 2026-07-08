"""
Step 4: 视频变速同步 + 音频合成
用法:
  python step4_compose_video.py <video_path> <english_merged_srt> <chinese_srt> <segments_dir> <output_dir>

兼容旧用法:
  python step4_compose_video.py <video_path> <english_merged_srt> <segments_dir> <output_dir>
  旧用法会自动读取 <output_dir>/chinese.srt。

以中文音频为主时间轴，视频分段变速适配。即使 speed 被限幅，也会通过 trim/pad
把每段视频精确对齐到中文音频时间槽，避免逐段漂移。
"""
import json
import os
import re
import subprocess
import sys

from pipeline_lock import file_lock

# ============================================================
# Configuration
# ============================================================
GAP_SECONDS = 0.5
MIN_SPEED = 0.6
MAX_SPEED = 1.8
VIDEO_EPSILON = 0.03
PRESERVE_NON_SUBTITLE_GAPS = os.environ.get("PRESERVE_NON_SUBTITLE_GAPS", "").lower() in {"1", "true", "yes", "on"}
MIN_PRESERVED_GAP_SECONDS = 0.25


# ============================================================
# Utilities
# ============================================================
def parse_srt(srt_path: str) -> list:
    with open(srt_path, "r", encoding="utf-8-sig") as f:
        content = f.read()

    pattern = re.compile(
        r"(\d+)\s*\n"
        r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*\n"
        r"(.*?)(?=\n\n|\n\d+\s*\n|\Z)",
        re.DOTALL,
    )

    entries = []
    for match in pattern.finditer(content):
        entries.append({
            "index": int(match.group(1)),
            "start_sec": srt_time_to_seconds(match.group(2)),
            "end_sec": srt_time_to_seconds(match.group(3)),
            "text": match.group(4).strip().replace("\n", " "),
        })
    return entries


def srt_time_to_seconds(t: str) -> float:
    h, m, rest = t.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def seconds_to_srt_time(sec: float) -> str:
    sec = max(0.0, sec)
    millis_total = int(round(sec * 1000))
    hours = millis_total // 3_600_000
    millis_total %= 3_600_000
    minutes = millis_total // 60_000
    millis_total %= 60_000
    seconds = millis_total // 1000
    millis = millis_total % 1000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def get_duration(path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def get_video_fps(path: str) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate,r_frame_rate",
            path,
        ],
        capture_output=True, text=True, check=True,
    )
    streams = json.loads(result.stdout).get("streams", [])
    for stream in streams:
        for key in ("avg_frame_rate", "r_frame_rate"):
            value = stream.get(key)
            if value and value != "0/0":
                num, den = value.split("/")
                fps = float(num) / float(den)
                if fps > 0:
                    return fps
    return 30.0


def clamp(value, min_val, max_val):
    return max(min_val, min(max_val, value))


def run_ffmpeg(cmd: list, label: str):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout)[-1200:]
        raise RuntimeError(f"{label} failed:\n{tail}")


def escape_concat_path(path: str) -> str:
    path = os.path.abspath(path)
    return path.replace("\\", "/").replace("'", "'\\''")


# ============================================================
# Step 4a: Validate inputs and compute timing plan
# ============================================================
def assert_contiguous_indexes(entries: list, label: str):
    if not entries:
        raise ValueError(f"{label} is empty or cannot be parsed.")

    indexes = [entry["index"] for entry in entries]
    expected = list(range(1, len(entries) + 1))
    if indexes != expected:
        raise ValueError(
            f"{label} indexes must be contiguous 1..N. "
            f"Expected {expected[:5]}...{expected[-5:]}, got {indexes[:5]}...{indexes[-5:]}."
        )


def validate_inputs(english_entries: list, chinese_entries: list, segments_dir: str):
    assert_contiguous_indexes(english_entries, "English SRT")
    assert_contiguous_indexes(chinese_entries, "Chinese SRT")

    if len(english_entries) != len(chinese_entries):
        raise ValueError(
            f"Segment count mismatch: English SRT has {len(english_entries)}, "
            f"Chinese SRT has {len(chinese_entries)}."
        )

    bad_durations = [
        entry["index"] for entry in english_entries
        if entry["end_sec"] <= entry["start_sec"]
    ]
    if bad_durations:
        raise ValueError(f"English SRT has non-positive durations: {bad_durations}")

    expected = {f"{i:03d}.wav" for i in range(1, len(english_entries) + 1)}
    actual = {
        name for name in os.listdir(segments_dir)
        if re.fullmatch(r"\d{3}\.wav", name, flags=re.IGNORECASE)
    } if os.path.isdir(segments_dir) else set()

    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing WAV files: {', '.join(missing[:20])}")
        if extra:
            details.append(f"extra WAV files: {', '.join(extra[:20])}")
        raise ValueError("Audio segment 1:1 validation failed: " + "; ".join(details))


def append_gap_item(plan: list, start: float, end: float, timeline_cursor: float, label: str) -> float:
    gap_dur = max(0.0, end - start)
    if gap_dur < MIN_PRESERVED_GAP_SECONDS:
        return timeline_cursor

    plan.append({
        "kind": "gap",
        "index": f"gap-{len(plan) + 1:03d}",
        "label": label,
        "start_sec": start,
        "end_sec": end,
        "interval_a": gap_dur,
        "audio_dur": 0.0,
        "gap_after": 0.0,
        "target_start": timeline_cursor,
        "target_end": timeline_cursor + gap_dur,
        "target_slot_dur": gap_dur,
        "raw_speed": 1.0,
        "speed": 1.0,
        "speed_adjusted_dur": gap_dur,
        "trim_dur": gap_dur,
        "pad_dur": 0.0,
        "drift_after_clamp": 0.0,
        "wav_path": None,
        "chinese_text": "",
    })
    return timeline_cursor + gap_dur


def compute_segment_plan(
    english_entries: list,
    chinese_entries: list,
    segments_dir: str,
    preserve_gaps: bool = False,
    video_duration: float | None = None,
) -> list:
    validate_inputs(english_entries, chinese_entries, segments_dir)

    plan = []
    timeline_cursor = 0.0
    total = len(english_entries)
    previous_source_end = 0.0

    for i, (english, chinese) in enumerate(zip(english_entries, chinese_entries)):
        if preserve_gaps:
            timeline_cursor = append_gap_item(
                plan,
                previous_source_end,
                english["start_sec"],
                timeline_cursor,
                f"before-{english['index']:03d}",
            )

        wav_path = os.path.join(segments_dir, f"{english['index']:03d}.wav")
        audio_dur = get_duration(wav_path)
        gap_after = 0.0 if preserve_gaps else (GAP_SECONDS if i < total - 1 else 0.0)
        target_start = timeline_cursor
        target_end = target_start + audio_dur
        target_slot_dur = audio_dur + gap_after

        interval_a = english["end_sec"] - english["start_sec"]
        raw_speed = interval_a / target_slot_dur if target_slot_dur > 0 else 1.0
        speed = clamp(raw_speed, MIN_SPEED, MAX_SPEED)
        speed_adjusted_dur = interval_a / speed
        trim_dur = target_slot_dur
        pad_dur = max(0.0, target_slot_dur - speed_adjusted_dur)
        drift_after_clamp = speed_adjusted_dur - target_slot_dur

        plan.append({
            "kind": "speech",
            "index": english["index"],
            "start_sec": english["start_sec"],
            "end_sec": english["end_sec"],
            "interval_a": interval_a,
            "audio_dur": audio_dur,
            "gap_after": gap_after,
            "target_start": target_start,
            "target_end": target_end,
            "target_slot_dur": target_slot_dur,
            "raw_speed": raw_speed,
            "speed": speed,
            "speed_adjusted_dur": speed_adjusted_dur,
            "trim_dur": trim_dur,
            "pad_dur": pad_dur,
            "drift_after_clamp": drift_after_clamp,
            "wav_path": wav_path,
            "chinese_text": chinese["text"],
        })

        timeline_cursor += target_slot_dur
        previous_source_end = english["end_sec"]

    if preserve_gaps and video_duration is not None:
        append_gap_item(
            plan,
            previous_source_end,
            video_duration,
            timeline_cursor,
            "after-last",
        )

    return plan


def write_chinese_timeline_srt(plan: list, output_srt: str):
    os.makedirs(os.path.dirname(output_srt), exist_ok=True)
    with open(output_srt, "w", encoding="utf-8") as f:
        for item in plan:
            if item.get("kind") != "speech":
                continue
            f.write(f"{item['index']}\n")
            f.write(
                f"{seconds_to_srt_time(item['target_start'])} --> "
                f"{seconds_to_srt_time(item['target_end'])}\n"
            )
            f.write(f"{item['chinese_text']}\n\n")
    print(f"[4a] Chinese timeline SRT: {output_srt}")


# ============================================================
# Step 4b: Cut, speed-adjust, then trim/pad video segments
# ============================================================
def process_video_segments(plan: list, video_path: str, temp_dir: str) -> list:
    os.makedirs(temp_dir, exist_ok=True)
    segment_files = []
    total = len(plan)
    video_duration = get_duration(video_path)
    fps = get_video_fps(video_path)

    for i, seg in enumerate(plan):
        index_label = f"{seg['index']:03d}" if isinstance(seg["index"], int) else str(seg["index"])
        out_path = os.path.join(temp_dir, f"vseg_{index_label}.mp4")
        segment_files.append(out_path)

        if os.path.exists(out_path):
            existing_dur = get_duration(out_path)
            if abs(existing_dur - seg["target_slot_dur"]) <= VIDEO_EPSILON:
                print(f"  [{i+1}/{total}] #{index_label} exists, skipping")
                continue
            os.remove(out_path)

        pts_factor = 1.0 / seg["speed"]
        start = seg["start_sec"]
        end = min(seg["end_sec"], video_duration)

        filters = [
            f"setpts={pts_factor:.8f}*(PTS-STARTPTS)",
            f"fps={fps:.6f}",
        ]
        if seg["pad_dur"] > VIDEO_EPSILON:
            filters.append(f"tpad=stop_mode=clone:stop_duration={seg['pad_dur']:.6f}")
        filters.extend([
            f"trim=duration={seg['trim_dur']:.6f}",
            "setpts=PTS-STARTPTS",
        ])

        print(
            f"  [{i+1}/{total}] #{index_label}: "
            f"{start:.1f}s-{end:.1f}s, speed={seg['speed']:.2f}x, "
            f"slot={seg['target_slot_dur']:.2f}s"
        )

        cmd = [
            "ffmpeg", "-y", "-ss", str(start), "-to", str(end),
            "-i", video_path,
            "-filter_complex", f"[0:v]{','.join(filters)}[v]",
            "-map", "[v]", "-an",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            out_path,
        ]
        run_ffmpeg(cmd, f"Video segment #{index_label}")

    return segment_files


# ============================================================
# Step 4c: Compose Chinese audio track
# ============================================================
def compose_chinese_audio(plan: list, temp_dir: str, output_audio: str):
    print("\n[4c] Composing Chinese audio track...")

    silence_path = os.path.join(temp_dir, "silence_gap.wav")
    run_ffmpeg(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i",
            f"anullsrc=r=48000:cl=mono:d={GAP_SECONDS}",
            "-t", str(GAP_SECONDS), silence_path,
        ],
        "Silence gap generation",
    )

    concat_list_path = os.path.join(temp_dir, "audio_concat.txt")
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for seg in plan:
            if seg.get("kind") == "gap":
                gap_path = os.path.join(temp_dir, f"silence_{seg['index']}.wav")
                run_ffmpeg(
                    [
                        "ffmpeg", "-y", "-f", "lavfi", "-i",
                        f"anullsrc=r=48000:cl=mono:d={seg['target_slot_dur']:.6f}",
                        "-t", f"{seg['target_slot_dur']:.6f}", gap_path,
                    ],
                    f"Silence gap {seg['index']} generation",
                )
                f.write(f"file '{escape_concat_path(gap_path)}'\n")
                continue

            f.write(f"file '{escape_concat_path(seg['wav_path'])}'\n")
            if seg["gap_after"] > 0:
                f.write(f"file '{escape_concat_path(silence_path)}'\n")

    run_ffmpeg(
        [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_list_path,
            "-codec:a", "libmp3lame", "-b:a", "192k",
            output_audio,
        ],
        "Chinese audio composition",
    )

    dur = get_duration(output_audio)
    print(f"[4c] Chinese audio: {output_audio} ({dur:.1f}s)")


# ============================================================
# Step 4d: Merge video + audio
# ============================================================
def merge_final_video(segment_files: list, temp_dir: str,
                      output_audio: str, output_video: str):
    print("\n[4d] Merging final video...")

    video_concat_path = os.path.join(temp_dir, "video_concat.txt")
    with open(video_concat_path, "w", encoding="utf-8") as f:
        for seg_file in segment_files:
            if not os.path.exists(seg_file):
                raise FileNotFoundError(f"Missing processed video segment: {seg_file}")
            f.write(f"file '{escape_concat_path(seg_file)}'\n")

    temp_video = os.path.join(temp_dir, "concat_video.mp4")
    run_ffmpeg(
        [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", video_concat_path, "-c:v", "copy", temp_video,
        ],
        "Video concatenation",
    )

    run_ffmpeg(
        [
            "ffmpeg", "-y", "-i", temp_video, "-i", output_audio,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", output_video,
        ],
        "Final video merge",
    )

    dur = get_duration(output_video)
    print(f"[4d] Final video: {output_video} ({dur:.1f}s)")


# ============================================================
# Main
# ============================================================
def resolve_args(argv: list):
    if len(argv) == 6:
        return argv[1], argv[2], argv[3], argv[4], argv[5]
    if len(argv) == 5:
        video_path = argv[1]
        english_srt = argv[2]
        segments_dir = argv[3]
        output_dir = argv[4]
        chinese_srt = os.path.join(output_dir, "chinese.srt")
        if not os.path.exists(chinese_srt):
            raise SystemExit(
                "ERROR: Old Step 4 usage requires <output_dir>/chinese.srt to exist. "
                "Preferred usage: python step4_compose_video.py <video_path> "
                "<english_merged_srt> <chinese_srt> <segments_dir> <output_dir>"
            )
        return video_path, english_srt, chinese_srt, segments_dir, output_dir

    raise SystemExit(
        "Usage: python step4_compose_video.py <video_path> <english_merged_srt> "
        "<chinese_srt> <segments_dir> <output_dir>"
    )


def main():
    video_path, english_srt, chinese_srt, segments_dir, output_dir = resolve_args(sys.argv)

    os.makedirs(output_dir, exist_ok=True)
    output_video = os.path.join(output_dir, "final_translated.mp4")
    output_audio = os.path.join(output_dir, "chinese_full.mp3")
    output_timeline_srt = os.path.join(output_dir, "chinese_timeline.srt")
    temp_dir = os.path.join(output_dir, "_temp_step4")
    lock_path = os.path.join(output_dir, ".step4_compose.lock")

    with file_lock(lock_path, "Step 4 video composition"):
        print("=" * 60)
        print("Step 4: Video Speed-Sync + Audio Composition")
        print("=" * 60)

        english_entries = parse_srt(english_srt)
        chinese_entries = parse_srt(chinese_srt)
        print(f"Parsed {len(english_entries)} English entries")
        print(f"Parsed {len(chinese_entries)} Chinese entries")

        print("\n[4a] Validating 1:1 alignment and computing speed ratios...")
        if PRESERVE_NON_SUBTITLE_GAPS:
            print("  Preserving non-subtitle source gaps as silent video/audio intervals.")
        video_duration = get_duration(video_path) if PRESERVE_NON_SUBTITLE_GAPS else None
        plan = compute_segment_plan(
            english_entries,
            chinese_entries,
            segments_dir,
            preserve_gaps=PRESERVE_NON_SUBTITLE_GAPS,
            video_duration=video_duration,
        )
        write_chinese_timeline_srt(plan, output_timeline_srt)

        speech_plan = [s for s in plan if s.get("kind") == "speech"]
        speeds = [s["speed"] for s in speech_plan]
        clamped = [s for s in speech_plan if abs(s["raw_speed"] - s["speed"]) > 0.001]
        print(
            f"  {len(speech_plan)} speech segments | {len(plan) - len(speech_plan)} preserved gaps | "
            f"avg={sum(speeds)/len(speeds):.2f}x "
            f"min={min(speeds):.2f}x max={max(speeds):.2f}x | "
            f"clamped={len(clamped)}"
        )

        if clamped:
            print("  Clamped segments are trim/pad aligned to prevent timeline drift.")

        print("\n[4b] Processing video segments...")
        segment_files = process_video_segments(plan, video_path, temp_dir)

        compose_chinese_audio(plan, temp_dir, output_audio)
        merge_final_video(segment_files, temp_dir, output_audio, output_video)

        print("\n" + "=" * 60)
        print("ALL DONE!")
        print(f"  Video: {output_video}")
        print(f"  Audio: {output_audio}")
        print(f"  Chinese timeline SRT: {output_timeline_srt}")
        print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
