"""
Validate 1:1 SRT alignment before TTS or final composition.

Usage:
  python validate_srt_pair.py <english_srt> <chinese_srt> [segments_dir]
"""
import os
import re
import sys


SRT_PATTERN = re.compile(
    r"(\d+)\s*\n"
    r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*\n"
    r"(.*?)(?=\n\n|\n\d+\s*\n|\Z)",
    re.DOTALL,
)


def srt_time_to_seconds(value: str) -> float:
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(",")
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis) / 1000.0
    )


def parse_srt(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8-sig") as f:
        content = f.read()

    entries = []
    for match in SRT_PATTERN.finditer(content):
        start_text = match.group(2)
        end_text = match.group(3)
        entries.append({
            "index": int(match.group(1)),
            "start_text": start_text,
            "end_text": end_text,
            "start_sec": srt_time_to_seconds(start_text),
            "end_sec": srt_time_to_seconds(end_text),
            "text": match.group(4).strip().replace("\n", " "),
        })
    return entries


def validate_contiguous(entries: list[dict], label: str, errors: list[str]):
    if not entries:
        errors.append(f"{label}: no parseable SRT entries.")
        return

    indexes = [entry["index"] for entry in entries]
    expected = list(range(1, len(entries) + 1))
    if indexes != expected:
        errors.append(
            f"{label}: indexes are not contiguous 1..N "
            f"(first got {indexes[:5]}, expected {expected[:5]})."
        )

    previous_end = -1.0
    for entry in entries:
        if entry["end_sec"] <= entry["start_sec"]:
            errors.append(f"{label}: #{entry['index']} has non-positive duration.")
        if entry["start_sec"] < previous_end:
            errors.append(f"{label}: #{entry['index']} starts before the previous entry ends.")
        previous_end = max(previous_end, entry["end_sec"])


def validate_pair(english: list[dict], chinese: list[dict], errors: list[str]):
    if len(english) != len(chinese):
        errors.append(f"Count mismatch: English={len(english)}, Chinese={len(chinese)}.")
        return

    for en, zh in zip(english, chinese):
        if en["index"] != zh["index"]:
            errors.append(f"Index mismatch: English #{en['index']} vs Chinese #{zh['index']}.")
        if en["start_text"] != zh["start_text"] or en["end_text"] != zh["end_text"]:
            errors.append(
                f"Timestamp mismatch at #{en['index']}: "
                f"{en['start_text']} --> {en['end_text']} vs "
                f"{zh['start_text']} --> {zh['end_text']}."
            )
        if not zh["text"].strip():
            errors.append(f"Chinese #{zh['index']} is empty.")


def validate_segments(segments_dir: str, expected_count: int, errors: list[str]):
    if not os.path.isdir(segments_dir):
        errors.append(f"Segments directory does not exist: {segments_dir}")
        return

    expected = {f"{i:03d}.wav" for i in range(1, expected_count + 1)}
    actual = {
        name for name in os.listdir(segments_dir)
        if re.fullmatch(r"\d{3}\.wav", name, flags=re.IGNORECASE)
    }
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        errors.append(f"Missing WAV files: {', '.join(missing[:30])}")
    if extra:
        errors.append(f"Extra WAV files: {', '.join(extra[:30])}")

    for name in sorted(expected & actual):
        path = os.path.join(segments_dir, name)
        if os.path.getsize(path) <= 1000:
            errors.append(f"WAV file looks too small: {name}")


def main():
    if len(sys.argv) < 3:
        print("Usage: python validate_srt_pair.py <english_srt> <chinese_srt> [segments_dir]")
        sys.exit(2)

    english_srt = sys.argv[1]
    chinese_srt = sys.argv[2]
    segments_dir = sys.argv[3] if len(sys.argv) >= 4 else None

    errors = []
    english = parse_srt(english_srt)
    chinese = parse_srt(chinese_srt)

    validate_contiguous(english, "English SRT", errors)
    validate_contiguous(chinese, "Chinese SRT", errors)
    validate_pair(english, chinese, errors)
    if segments_dir:
        validate_segments(segments_dir, len(english), errors)

    if errors:
        print("SRT validation FAILED:")
        for error in errors:
            print(f"  - {error}")
        sys.exit(1)

    print("SRT validation OK")
    print(f"  entries: {len(english)}")
    if segments_dir:
        print(f"  wav files: {len(english)}")


if __name__ == "__main__":
    main()
