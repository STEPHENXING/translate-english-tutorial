"""
Step 1b: 合并相邻短句 SRT
用法: python step1b_merge_srt.py <input_srt> <output_srt> [max_gap_sec] [max_chars]
"""
import os
import re
import sys

from pipeline_lock import file_lock


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
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int((sec - int(sec)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def merge_short_segments(entries, max_gap_sec=1.5, max_merged_chars=200):
    if not entries:
        return []

    merged = []
    current = {
        "start_sec": entries[0]["start_sec"],
        "end_sec": entries[0]["end_sec"],
        "text": entries[0]["text"],
    }

    for i in range(1, len(entries)):
        entry = entries[i]
        gap = entry["start_sec"] - current["end_sec"]
        combined_text = current["text"] + " " + entry["text"]

        if gap <= max_gap_sec and len(combined_text) <= max_merged_chars:
            current["end_sec"] = entry["end_sec"]
            current["text"] = combined_text
        else:
            merged.append(current)
            current = {
                "start_sec": entry["start_sec"],
                "end_sec": entry["end_sec"],
                "text": entry["text"],
            }

    merged.append(current)
    return merged


def write_srt(entries, output_path):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for i, entry in enumerate(entries):
            f.write(f"{i + 1}\n")
            f.write(
                f"{seconds_to_srt_time(entry['start_sec'])} --> "
                f"{seconds_to_srt_time(entry['end_sec'])}\n"
            )
            f.write(f"{entry['text']}\n\n")


def main():
    if len(sys.argv) < 3:
        print("Usage: python step1b_merge_srt.py <input_srt> <output_srt> [max_gap_sec] [max_chars]")
        sys.exit(1)

    input_srt = sys.argv[1]
    output_srt = sys.argv[2]
    max_gap = float(sys.argv[3]) if len(sys.argv) > 3 else 1.5
    max_chars = int(sys.argv[4]) if len(sys.argv) > 4 else 200

    if not os.path.exists(input_srt):
        print(f"ERROR: Input SRT not found: {input_srt}")
        sys.exit(1)

    with file_lock(output_srt + ".lock", "Step 1b SRT merge"):
        entries = parse_srt(input_srt)
        print(f"Original segments: {len(entries)}")

        merged = merge_short_segments(entries, max_gap_sec=max_gap, max_merged_chars=max_chars)
        print(f"Merged segments:   {len(merged)}")
        print(f"Reduction:         {len(entries) - len(merged)} segments merged away "
              f"({(1 - len(merged)/len(entries))*100:.1f}% reduction)")

        durations = [e["end_sec"] - e["start_sec"] for e in merged]
        avg_dur = sum(durations) / len(durations) if durations else 0
        print(f"\nMerged segment durations: avg={avg_dur:.1f}s, "
              f"min={min(durations):.1f}s, max={max(durations):.1f}s")

        write_srt(merged, output_srt)
        print(f"\nSaved to: {output_srt}")


if __name__ == "__main__":
    main()
