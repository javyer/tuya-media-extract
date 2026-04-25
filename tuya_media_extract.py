#!/usr/bin/env python3
"""
tuya_media_extract.py - Extract and mux Tuya SmartLife camera .media files to MKV

Tuya .media File Format (reverse-engineered):
----------------------------------------------
Each .media file is a proprietary container with fixed-size chunk headers.
Chunks are interleaved: video (H264) and audio (raw PCM 16-bit LE).

Chunk structure (24-byte header):
  Offset 0  : uint32 LE  - chunk type
                  0 = video frame (P/B-frame)
                  1 = video keyframe (I-frame / SPS+PPS)
                  3 = audio frame (PCM 16-bit signed LE, 16000 Hz, mono)
  Offset 4  : uint32 LE  - payload size in bytes
  Offset 8  : uint64 LE  - timestamp (camera epoch, not Unix)
  Offset 16 : uint64 LE  - unknown (sequence / flags)
  Offset 24 : <payload>  - raw H264 NAL units or PCM samples

SD card layout (continuous recording, not event-based):
  DCIM/
    YYYY/
      MM/
        DD/
          <unix_timestamp>_<session_id>/
            .info          - JSON: version, eventType, codec
            0000.media     - 10-second segment (100 frames @ 10fps)
            0010.media
            ...
            0590.media     - last segment of the 10-minute session

Each session folder = 10 minutes of continuous footage.
Each .media file   = 10 seconds, 100 video frames, 250 audio chunks.
Audio chunk size   = 1280 bytes = 640 samples = 40ms @ 16000 Hz.
"""

import argparse
import io
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ── Tuya .media format constants ─────────────────────────────────────────────
CHUNK_HEADER_SIZE  = 24       # bytes per chunk header
CHUNK_TYPE_VIDEO_P = 0        # P/B-frame (inter)
CHUNK_TYPE_VIDEO_I = 1        # I-frame / keyframe (intra)
CHUNK_TYPE_AUDIO   = 3        # PCM audio frame
AUDIO_SAMPLE_RATE  = 16000    # Hz
AUDIO_CHANNELS     = 1        # mono
AUDIO_SAMPLE_WIDTH = 2        # bytes (16-bit)
VIDEO_FPS          = 10       # real frame rate (declared as 25 in SPS, actual 10)


def parse_args():
    parser = argparse.ArgumentParser(
        prog="tuya_media_extract",
        description=(
            "Convert Tuya SmartLife continuous-recording SD card footage "
            "(.media files) into lossless MKV files (one per day)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s -i /media/user/SD_CARD/DCIM -o ~/Videos/camera
  %(prog)s -i /media/user/SD_CARD/DCIM -o ~/Videos/camera --workers 8
  %(prog)s -i /mnt/sdcard -o /nas/cctv --overwrite
        """,
    )
    parser.add_argument(
        "-i", "--input",
        required=True,
        metavar="DIR",
        help="Input directory: root of the Tuya DCIM folder on the SD card",
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
        default=VIDEO_FPS,
        metavar="FPS",
        help=f"Real video frame rate (default: {VIDEO_FPS})",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=AUDIO_SAMPLE_RATE,
        metavar="HZ",
        help=f"Audio sample rate in Hz (default: {AUDIO_SAMPLE_RATE})",
    )
    return parser.parse_args()


def demux_media(media_file: Path, sample_rate: int):
    """
    Parse a single .media file and return (video_bytes, audio_bytes).
    Video  : concatenated raw H264 NAL units (types 0 and 1).
    Audio  : concatenated PCM 16-bit LE samples (type 3).
    """
    with open(media_file, "rb") as f:
        data = f.read()

    video_chunks = []
    audio_chunks = []
    offset = 0

    while offset < len(data) - CHUNK_HEADER_SIZE:
        chunk_type = struct.unpack_from("<I", data, offset)[0]
        chunk_size = struct.unpack_from("<I", data, offset + 4)[0]

        # Sanity check
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
    """Wrap raw PCM bytes in a proper WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(AUDIO_CHANNELS)
        wf.setsampwidth(AUDIO_SAMPLE_WIDTH)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def mux_segment(media_file: Path, tmpdir: Path, fps: float, sample_rate: int) -> Path:
    """
    Demux one .media file and mux into a temporary MKV segment.
    Returns the path to the .mkv segment.
    """
    video_bytes, audio_bytes = demux_media(media_file, sample_rate)

    # Unique stem to avoid collisions when sessions overlap
    unique  = f"{media_file.parent.name}_{media_file.stem}"
    vpath   = tmpdir / f"{unique}.h264"
    apath   = tmpdir / f"{unique}.wav"
    mkvpath = tmpdir / f"{unique}.mkv"

    vpath.write_bytes(video_bytes)
    apath.write_bytes(build_wav(audio_bytes, sample_rate))

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "h264", "-r", str(fps), "-i", str(vpath),  # raw H264 input
            "-i", str(apath),                                  # WAV input
            "-c:v", "copy",                                    # lossless video copy
            "-c:a", "pcm_s16le",                              # lossless audio copy
            "-async", "1",                                     # fix minor A/V drift
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

    print(f"→ {out_file.name}  ({total} segments)")

    with tempfile.TemporaryDirectory(dir=str(tmp_root)) as tmpdir:
        tmpdir  = Path(tmpdir)
        results = {}

        # Parallel muxing of individual segments
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(mux_segment, m, tmpdir, args.fps, args.sample_rate): m
                for m in media_files
            }
            done = 0
            for future in as_completed(futures):
                media         = futures[future]
                results[media] = future.result()
                done          += 1
                print(f"  {done}/{total}", end="\r", flush=True)

        # Build concat list in strict chronological order
        concat_list = tmpdir / "concat.txt"
        with open(concat_list, "w") as lst:
            for media in media_files:
                lst.write(f"file '{results[media]}'\n")

        print(f"\n  Concatenating into {out_file.name} …")
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

    print(f"  ✅ {out_file.name}")


def main():
    args   = parse_args()
    sdcard = Path(args.input)
    output = Path(args.output)

    if not sdcard.is_dir():
        print(f"ERROR: input directory not found: {sdcard}", file=sys.stderr)
        sys.exit(1)

    output.mkdir(parents=True, exist_ok=True)

    # Auto-detect day dirs: parent of session folders (which contain .media files)
    # Works with both:
    #   SD card layout : DCIM/YYYY/MM/DD/session/*.media
    #   Local copy     : input/DD/session/*.media
    day_dirs = sorted(set(
        m.parent.parent
        for m in sdcard.rglob("*.media")
    ))

    if not day_dirs:
        print(f"ERROR: no .media files found under {sdcard}", file=sys.stderr)
        sys.exit(1)

    for day_dir in day_dirs:
        # Build date string from path — handle both YYYY/MM/DD and plain DD
        parts = day_dir.relative_to(sdcard).parts
        if len(parts) == 3:
            date_str = f"{parts[0]}-{parts[1]}-{parts[2]}"   # YYYY/MM/DD
        elif len(parts) == 1:
            date_str = f"day-{parts[0]}"                       # plain DD (local copy)
        else:
            date_str = "-".join(parts)                         # fallback
        out_file  = output / f"{date_str}_full.mkv"

        if out_file.exists() and not args.overwrite:
            print(f"⏭️  {out_file.name} already exists, skipping (use --overwrite)")
            continue

        process_day(day_dir, out_file, args)

    print("\n✅ All done!")


if __name__ == "__main__":
    main()
