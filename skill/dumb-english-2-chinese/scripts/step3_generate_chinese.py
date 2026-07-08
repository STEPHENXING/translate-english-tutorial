"""
Step 3: 从中文 SRT 逐句生成 VoxCPM2 克隆音频
用法: python step3_generate_chinese.py <reference_audio> <chinese_srt> <segments_dir> <reference_wav>

reference_audio 是用户提供的讲者参考音频，不再从原视频/原音频里截取。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import queue
import time

from pipeline_lock import file_lock

# ============================================================
# VoxCPM2 Configuration
# ============================================================
VOXCPM_PYTHON = os.environ.get(
    "VOXCPM_PYTHON",
    r"<voxcpm2-runtime>\python\python.exe",
)
VOXCPM_RUNNER = os.environ.get(
    "VOXCPM_RUNNER",
    r"<voxcpm2-runtime>\app\storyboard_voxcpm_runner.py",
)
SERVER_RESPONSE_PREFIX = "__SC_EXTENSION_RESPONSE__:"

CFG_VALUE = 2.0
INFERENCE_TIMESTEPS = 10
REFERENCE_CLIP_SECONDS = 12.0
CHINESE_CHAR_RE = re.compile(r"[\u3400-\u9fff]")
ENGLISH_IN_BRACKETS_RE = re.compile(
    r"[\(（\[\【]\s*[A-Za-z][A-Za-z0-9 .,'’&/\-:;!?]*\s*[\)）\]\】]"
)
LATIN_WORD_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9'’.\-]*(?:\s+[A-Za-z][A-Za-z0-9'’.\-]*)*\b")


# ============================================================
# SRT Parsing
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
            "text": match.group(4).strip().replace("\n", " "),
        })
    return entries


def sanitize_text_for_tts(text: str) -> str:
    """Remove English aliases before VoxCPM TTS without changing chinese.srt."""
    if not CHINESE_CHAR_RE.search(text):
        return text

    cleaned = ENGLISH_IN_BRACKETS_RE.sub("", text)
    cleaned = LATIN_WORD_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+([，。！？；：、,.!?;:])", r"\1", cleaned)
    cleaned = re.sub(r"([，。！？；：、,.!?;:])\s+", r"\1", cleaned)
    cleaned = re.sub(r"([（(【\[])\s+", r"\1", cleaned)
    cleaned = re.sub(r"\s+([）)】\]])", r"\1", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or text


# ============================================================
# Reference Audio
# ============================================================
def prepare_reference_audio(reference_audio: str, reference_wav: str):
    if not os.path.exists(reference_audio):
        raise FileNotFoundError(f"Reference audio not found: {reference_audio}")

    source_marker = reference_wav + ".source.txt"
    current_source = json.dumps(
        {
            "path": os.path.abspath(reference_audio),
            "size": os.path.getsize(reference_audio),
            "mtime": os.path.getmtime(reference_audio),
            "clip_seconds": REFERENCE_CLIP_SECONDS,
        },
        ensure_ascii=False,
        sort_keys=True,
    )

    if os.path.exists(reference_wav) and os.path.exists(source_marker):
        with open(source_marker, "r", encoding="utf-8") as f:
            previous_source = f.read().strip()
        if previous_source == current_source:
            print(f"[3a] Reference audio exists: {reference_wav}")
            return

    if os.path.exists(reference_wav):
        print(f"[3a] Reference audio exists: {reference_wav}")
        print("[3a] Source marker missing or changed; regenerating normalized reference wav.")

    print(f"[3a] Clipping first {REFERENCE_CLIP_SECONDS:g}s from user-provided reference audio...")
    os.makedirs(os.path.dirname(reference_wav), exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", reference_audio,
            "-t", str(REFERENCE_CLIP_SECONDS),
            "-ar", "16000", "-ac", "1", reference_wav,
        ],
        check=True, capture_output=True,
    )
    with open(source_marker, "w", encoding="utf-8") as f:
        f.write(current_source)
    print(f"[3a] Saved: {reference_wav}")


# ============================================================
# VoxCPM2 Server Communication
# ============================================================
def read_server_output(process, output_queue):
    for line in iter(process.stdout.readline, ""):
        line = line.strip()
        if line:
            output_queue.put(line)


def wait_for_response(output_queue, request_id, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            line = output_queue.get(timeout=5)
        except queue.Empty:
            continue
        if line.startswith(SERVER_RESPONSE_PREFIX):
            resp = json.loads(line[len(SERVER_RESPONSE_PREFIX):])
            if resp.get("requestId") == request_id:
                return resp
    raise TimeoutError(f"Timeout: {request_id}")


def generate_audio_segments(entries: list, segments_dir: str, reference_wav: str):
    os.makedirs(segments_dir, exist_ok=True)

    # Resume support
    existing = set()
    for entry in entries:
        wav_path = os.path.join(segments_dir, f"{entry['index']:03d}.wav")
        if os.path.exists(wav_path) and os.path.getsize(wav_path) > 1000:
            existing.add(entry["index"])

    to_generate = [e for e in entries if e["index"] not in existing]

    if not to_generate:
        print("[3b] All segments exist. Skipping.")
        return

    if existing:
        print(f"[3b] Resuming: {len(existing)} done, {len(to_generate)} remaining.")

    print("[3b] Starting VoxCPM2 server...")
    process = subprocess.Popen(
        [VOXCPM_PYTHON, "-u", VOXCPM_RUNNER, "--server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8",
    )

    output_queue = queue.Queue()
    threading.Thread(
        target=read_server_output, args=(process, output_queue), daemon=True
    ).start()

    try:
        print("[3b] Warming up VoxCPM2 (~60 seconds)...")
        process.stdin.write(json.dumps({"requestId": "warmup", "command": "warmup"}) + "\n")
        process.stdin.flush()
        resp = wait_for_response(output_queue, "warmup", timeout=600)
        if not resp.get("ok"):
            raise RuntimeError(f"Warmup failed: {resp.get('error')}")
        print("[3b] Warmup complete!")

        total = len(to_generate)
        for i, entry in enumerate(to_generate):
            req_id = f"gen-{entry['index']}"
            original_text = entry["text"]
            text = sanitize_text_for_tts(original_text)
            print(f"[3b] [{i+1}/{total}] #{entry['index']:03d}: {text[:50]}{'...' if len(text) > 50 else ''}")
            if text != original_text:
                print("       cleaned English aliases before TTS")

            req = {
                "requestId": req_id,
                "command": "generate_voice_clone",
                "text": text,
                "referenceAudio": reference_wav,
                "cfgValue": CFG_VALUE,
                "inferenceTimesteps": INFERENCE_TIMESTEPS,
                "outputPrefix": f"seg-{entry['index']:03d}",
            }

            start_time = time.time()
            process.stdin.write(json.dumps(req) + "\n")
            process.stdin.flush()
            resp = wait_for_response(output_queue, req_id, timeout=300)
            elapsed = time.time() - start_time

            if resp.get("ok"):
                src = resp["outputs"][0]["path"]
                dst = os.path.join(segments_dir, f"{entry['index']:03d}.wav")
                shutil.copy2(src, dst)
                print(f"       -> {elapsed:.1f}s")
            else:
                print(f"       -> ERROR: {resp.get('error')}")
    finally:
        print("[3b] Shutting down server...")
        try:
            process.stdin.write(json.dumps({"command": "shutdown"}) + "\n")
            process.stdin.flush()
            process.wait(timeout=30)
        except Exception:
            process.kill()


# ============================================================
# Main
# ============================================================
def main():
    if len(sys.argv) < 5:
        print("Usage: python step3_generate_chinese.py <reference_audio> <chinese_srt> <segments_dir> <reference_wav>")
        sys.exit(1)

    reference_audio = sys.argv[1]
    chinese_srt = sys.argv[2]
    segments_dir = sys.argv[3]
    reference_wav = sys.argv[4]

    print("=" * 60)
    print("Step 3: Generate Chinese Audio from SRT")
    print("=" * 60)

    entries = parse_srt(chinese_srt)
    print(f"Parsed {len(entries)} subtitle entries")

    lock_path = os.path.join(segments_dir, ".step3_generate.lock")
    with file_lock(lock_path, "Step 3 VoxCPM2 generation"):
        prepare_reference_audio(reference_audio, reference_wav)
        generate_audio_segments(entries, segments_dir, reference_wav)

    print("\n" + "=" * 60)
    print(f"Step 3 DONE! Segments: {segments_dir}")
    print("=" * 60)


if __name__ == "__main__":
    main()
