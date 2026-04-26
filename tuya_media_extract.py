#!/usr/bin/env python3
"""
tuya_media_extract.py - Extract and mux Tuya SmartLife camera .media files to MKV

DISCLAIMER:
  This is an independent community project, not affiliated with or endorsed by
  Tuya Inc. or any of its subsidiaries. The .media file format was reverse-
  engineered for personal interoperability purposes only, on unencrypted footage
  stored on the user's own SD card. Use at your own risk.

Tuya .media File Format (reverse-engineered):
----------------------------------------------
Each .media file is a proprietary container with fixed-size chunk headers.
Chunks are interleaved: video (H264) and audio (PCM or G.711 µ-law).

Chunk structure (24-byte header):
  Offset 0  : uint32 LE  - chunk type
                  0 = video frame (P/B-frame)
                  1 = video keyframe (I-frame / SPS+PPS)
                  3 = audio frame
  Offset 4  : uint32 LE  - payload size in bytes
  Offset 8  : uint64 LE  - timestamp (camera epoch, not Unix)
  Offset 16 : uint64 LE  - unknown (sequence / flags)
  Offset 24 : <payload>  - raw H264 NAL units or audio samples

Known audio codecs (varies by camera model / firmware):
  PCM   : 16-bit signed little-endian, 16000 Hz, mono
          chunk size = 1280 bytes, ~250 chunks per 10s segment
          signature : small signed values e.g. c7ff e9ff 0000 ...
  mulaw : G.711 µ-law, 8-bit, 8000 Hz, mono
          chunk size = 320 bytes, ~250 chunks per 10s segment
          signature : values around 0x7e/0x7f/0xff (silence = 0x7f)

SD card layout (continuous recording, not event-based):
  DCIM/
    YYYY/
      MM/
        DD/
          <unix_timestamp>_<session_id>/
            .info          - JSON: version, eventType, codec
            0000.media     - 10-second segment (~100 frames @ 10fps)
            0010.media
            ...
            0590.media     - last segment of the 10-minute session

The input directory can be either:
  - The full SD card DCIM root  -> YYYY/MM/DD/session/*.media
  - A local flat copy           -> DD/session/*.media
The script auto-detects both layouts.

Each session folder = 10 minutes of continuous footage.
Each .media file   = 10 seconds, ~100 video frames, ~250 audio chunks.
"""

import argparse
import io
import signal
import struct
import subprocess
import sys
import tempfile
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def _sigint_handler(sig, frame):
    print("\nInterrupted.")
    sys.exit(1)

signal.signal(signal.SIGINT, _sigint_handler)

# ── Tuya .media format constants ─────────────────────────────────────────────
CHUNK_HEADER_SIZE  = 24       # bytes per chunk header
CHUNK_TYPE_VIDEO_P = 0        # P/B-frame (inter)
CHUNK_TYPE_VIDEO_I = 1        # I-frame / keyframe (intra)
CHUNK_TYPE_AUDIO   = 3        # audio frame (PCM or µ-law depending on camera)

# Default values (camera-dependent, override with CLI flags)
DEFAULT_FPS         = 10      # real frame rate (SPS declares 25, actual is 10)
DEFAULT_SAMPLE_RATE = 16000   # Hz  (PCM cameras)
DEFAULT_AUDIO_CODEC = "pcm"   # pcm or mulaw


def parse_args():
    parser = argparse.ArgumentParser(
        prog="tuya_media_extract",
        description=(
            "Convert Tuya SmartLife continuous-recording SD card footage "
            "(.media files) into lossless MKV files (one per day)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Audio codec detection:
  PCM   cameras : chunk size=1280, values like c7ff e9ff (16-bit signed LE)
  mulaw cameras : chunk size=320,  values like 7e 7f ff  (G.711 u-law 8-bit)

Input layouts supported (auto-detected):
  SD card : -i /media/user/SD_CARD/DCIM    (DCIM/YYYY/MM/DD/session/*.media)
  Local   : -i ~/sdcard_copy               (DD/session/*.media)

Examples:
  # PCM camera (default)
  %(prog)s -i /media/user/SD_CARD/DCIM -o ~/Videos/camera

  # G.711 u-law camera (e.g. indoor cam)
  %(prog)s -i ~/javcache/cam_salon -o ~/Videos/salon --audio-codec mulaw --sample-rate 8000

  # More parallel workers
  %(prog)s -i /media/user/SD_CARD/DCIM -o ~/Videos/camera --workers 8

  # Force reprocess existing files
  %(prog)s -i /media/user/SD_CARD/DCIM -o ~/Videos/camera --overwrite

DISCLAIMER:
  This tool is not affiliated with or endorsed by Tuya Inc.
  Reverse-engineered for personal interoperability use only.
        """,
    )
    parser.add_argument(
        "-i", "--input",
        required=True,
        metavar="DIR",
        help=(
            "Input directory: SD card DCIM root (YYYY/MM/DD/session/) "
            "or local copy (DD/session/). Structure is auto-detected."
        ),
    )
    parser.add_argument(
        "-o", "--output",
        required=True,
        metavar="DIR",
        help="Output directory where MKV files will be written",
    )
    parser.add_argument(
        "-w", "--workers",
        type=int,
        default=4,
        metavar="N",
        help="Number of parallel ffmpeg workers (default: 4)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output files (default: skip)",
    )
    parser.add_argument(
        "--tmpdir",
        metavar="DIR",
        default=None,
        help="Temporary directory for intermediate files (default: output dir)",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=DEFAULT_FPS,
        metavar="FPS",
        help=f"Real video frame rate (default: {DEFAULT_FPS})",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=DEFAULT_SAMPLE_RATE,
        metavar="HZ",
        help=(
            "Audio sample rate in Hz (default: auto-detected). "
            "Override only if auto-detection fails."
        ),
    )
    parser.add_argument(
        "--audio-codec",
        choices=["pcm", "mulaw"],
        default=DEFAULT_AUDIO_CODEC,
        metavar="CODEC",
        help=(
            "Audio codec: pcm or mulaw (default: auto-detected). "
            "Override only if auto-detection fails."
        ),
    )
    return parser.parse_args()


def demux_media(media_file: Path):
    """
    Parse a single .media file and return (video_bytes, audio_bytes).
    Video : concatenated raw H264 NAL units (chunk types 0 and 1).
    Audio : concatenated raw audio samples (chunk type 3).
    """
    with open(media_file, "rb") as f:
        data = f.read()

    video_chunks = []
    audio_chunks = []
    offset = 0

    while offset < len(data) - CHUNK_HEADER_SIZE:
        chunk_type = struct.unpack_from("<I", data, offset)[0]
        chunk_size = struct.unpack_from("<I", data, offset + 4)[0]

        if chunk_size == 0 or offset + CHUNK_HEADER_SIZE + chunk_size > len(data):
            break

        payload = data[offset + CHUNK_HEADER_SIZE : offset + CHUNK_HEADER_SIZE + chunk_size]

        if chunk_type in (CHUNK_TYPE_VIDEO_P, CHUNK_TYPE_VIDEO_I):
            video_chunks.append(payload)
        elif chunk_type == CHUNK_TYPE_AUDIO:
            audio_chunks.append(payload)

        offset += CHUNK_HEADER_SIZE + chunk_size

    return b"".join(video_chunks), b"".join(audio_chunks)


def build_wav(pcm_bytes: bytes, sample_rate: int) -> bytes:
    """Wrap raw PCM 16-bit LE bytes in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)       # mono
        wf.setsampwidth(2)       # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def mux_segment(media_file: Path, tmpdir: Path, args) -> Path:
    """
    Demux one .media file and mux video + audio into a temporary MKV segment.
    Handles both PCM and G.711 µ-law audio codecs.
    Returns the path to the .mkv segment.
    """
    video_bytes, audio_bytes = demux_media(media_file)

    # Unique stem avoids filename collisions across sessions
    unique  = f"{media_file.parent.name}_{media_file.stem}"
    vpath   = tmpdir / f"{unique}.h264"
    mkvpath = tmpdir / f"{unique}.mkv"

    vpath.write_bytes(video_bytes)

    if args.audio_codec == "mulaw":
        # G.711 µ-law: write raw bytes, tell ffmpeg the format explicitly
        apath = tmpdir / f"{unique}.ulaw"
        apath.write_bytes(audio_bytes)
        audio_input_args  = ["-f", "mulaw", "-ar", str(args.sample_rate), "-i", str(apath)]
        audio_encode_args = ["-c:a", "pcm_mulaw"]   # lossless copy in MKV
    else:
        # PCM 16-bit LE: wrap in WAV so ffmpeg auto-detects format
        apath = tmpdir / f"{unique}.wav"
        apath.write_bytes(build_wav(audio_bytes, args.sample_rate))
        audio_input_args  = ["-i", str(apath)]
        audio_encode_args = ["-c:a", "pcm_s16le"]   # lossless copy in MKV

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "h264", "-r", str(args.fps), "-i", str(vpath),
            *audio_input_args,
            "-c:v", "copy",          # lossless video: bit-for-bit H264 copy
            *audio_encode_args,      # lossless audio: bit-for-bit copy
            "-async", "1",           # correct minor A/V drift at boundaries
            str(mkvpath),
        ],
        stderr=subprocess.DEVNULL,
        check=False,
    )

    vpath.unlink(missing_ok=True)
    apath.unlink(missing_ok=True)
    return mkvpath


def process_day(day_dir: Path, out_file: Path, args):
    """Convert all .media files in a day folder into a single MKV."""
    media_files = sorted(day_dir.rglob("*.media"))
    if not media_files:
        return

    total    = len(media_files)
    tmp_root = Path(args.tmpdir) if args.tmpdir else Path(args.output)

    print(f"-> {out_file.name}  ({total} segments, audio={args.audio_codec} {args.sample_rate}Hz)")

    with tempfile.TemporaryDirectory(dir=str(tmp_root)) as tmpdir:
        tmpdir  = Path(tmpdir)
        results = {}

        # Parallel muxing of individual 10-second segments
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(mux_segment, m, tmpdir, args): m
                for m in media_files
            }
            done = 0
            for future in as_completed(futures):
                media          = futures[future]
                results[media] = future.result()
                done          += 1
                print(f"  {done}/{total}  ", end="\r", flush=True)

        print()  # clean newline after \r progress

        # Concat list must be in strict chronological order
        concat_list = tmpdir / "concat.txt"
        with open(concat_list, "w") as lst:
            for media in media_files:
                lst.write(f"file '{results[media]}'\n")

        print(f"\n  Concatenating into {out_file.name} ...")
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list),
                "-c", "copy",
                str(out_file),
            ],
            stderr=subprocess.DEVNULL,
            check=False,
        )

    print(f"  OK {out_file.name}")


def main():
    try:
        _main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
    finally:
        print(end="", flush=True)  # ensure terminal is clean on any exit


def detect_audio_format(sdcard: Path):
    """
    Auto-detect audio codec and sample rate from the first .media file found.
    Detection is based on audio chunk size:
      1280 bytes -> PCM 16-bit LE @ 16000 Hz
      320  bytes -> G.711 µ-law   @  8000 Hz
    """
    media = next(sdcard.rglob("*.media"), None)
    if not media:
        return DEFAULT_AUDIO_CODEC, DEFAULT_SAMPLE_RATE

    with open(media, "rb") as f:
        data = f.read()

    offset = 0
    while offset < len(data) - CHUNK_HEADER_SIZE:
        chunk_type = struct.unpack_from("<I", data, offset)[0]
        chunk_size = struct.unpack_from("<I", data, offset + 4)[0]
        if chunk_size == 0 or offset + CHUNK_HEADER_SIZE + chunk_size > len(data):
            break
        if chunk_type == CHUNK_TYPE_AUDIO:
            if chunk_size == 320:
                print(f"  audio detected: G.711 mulaw 8000Hz (chunk={chunk_size}B)")
                return "mulaw", 8000
            elif chunk_size == 1280:
                print(f"  audio detected: PCM 16-bit 16000Hz (chunk={chunk_size}B)")
                return "pcm", 16000
            else:
                # Unknown chunk size — best-effort guess
                guessed_rate = (chunk_size // 2) // 10
                print(f"  WARNING: unknown audio chunk size={chunk_size}B, guessing {guessed_rate}Hz PCM")
                return "pcm", guessed_rate
        offset += CHUNK_HEADER_SIZE + chunk_size

    print("  WARNING: no audio chunk found, using defaults")
    return DEFAULT_AUDIO_CODEC, DEFAULT_SAMPLE_RATE


def _main():
    args   = parse_args()
    sdcard = Path(args.input)
    output = Path(args.output)

    if not sdcard.is_dir():
        print(f"ERROR: input directory not found: {sdcard}", file=sys.stderr)
        sys.exit(1)

    output.mkdir(parents=True, exist_ok=True)

    # Auto-detect day dirs: grandparent of .media files
    # Works with both:
    #   SD card layout : DCIM/YYYY/MM/DD/session/*.media  -> 3 path parts
    #   Local copy     : input/DD/session/*.media          -> 1 path part
    day_dirs = sorted(set(
        m.parent.parent
        for m in sdcard.rglob("*.media")
    ))

    if not day_dirs:
        print(f"ERROR: no .media files found under {sdcard}", file=sys.stderr)
        sys.exit(1)

    # Auto-detect audio format unless explicitly overridden by user
    if args.audio_codec == DEFAULT_AUDIO_CODEC and args.sample_rate == DEFAULT_SAMPLE_RATE:
        print("Auto-detecting audio format...")
        args.audio_codec, args.sample_rate = detect_audio_format(sdcard)
    else:
        print(f"  audio: {args.audio_codec} {args.sample_rate}Hz (forced)")

    for day_dir in day_dirs:
        parts = day_dir.relative_to(sdcard).parts
        if len(parts) == 3:
            date_str = f"{parts[0]}-{parts[1]}-{parts[2]}"   # YYYY/MM/DD
        elif len(parts) == 1:
            date_str = f"day-{parts[0]}"                       # plain DD
        else:
            date_str = "-".join(parts)                         # fallback

        out_file = output / f"{date_str}_full.mkv"

        if out_file.exists() and not args.overwrite:
            print(f"skip: {out_file.name} already exists (use --overwrite)")
            continue

        process_day(day_dir, out_file, args)

    print("\nAll done!")


if __name__ == "__main__":
    main()
