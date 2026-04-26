# tuya-media-extract

Convert **Tuya SmartLife continuous-recording SD card footage** into standard
lossless MKV files — one file per day, with synchronized audio and video.

> **DISCLAIMER:** This is an independent community project, not affiliated with
> or endorsed by Tuya Inc. or any of its subsidiaries. The `.media` file format
> was reverse-engineered for personal interoperability purposes only, on
> unencrypted footage stored on the user's own SD card. Use at your own risk.

---

## Background

This tool was reverse-engineered from **Tuya SmartLife cameras** running
firmware **v79.1.40**, recording in **continuous mode** (not event/motion mode)
to a FAT32 (vfat) micro-SD card.

The Tuya Android app can play footage directly from the card with audio, but
provides no way to export full-day recordings to a PC. This tool fills that gap.

Two audio codecs have been identified across different camera models — the tool
**auto-detects** the correct one.

---

## SD Card Layout

The camera writes footage as a proprietary `.media` container organized like
this on the SD card:

```
/DCIM/
  YYYY/
    MM/
      DD/
        <unix_timestamp>_<session_id>/
          .info          ← JSON metadata: version, eventType, codec
          0000.media     ← segment 0  (10 seconds)
          0010.media     ← segment 1  (10 seconds)
          0020.media
          ...
          0590.media     ← segment 59 (last of the 10-minute session)
```

- One **session folder** = **10 minutes** of continuous footage
- One **`.media` file** = **10 seconds** of footage
- A full day = ~144 session folders = ~8642 `.media` files (measured)

---

## The `.media` File Format

Each `.media` file is a proprietary binary container with interleaved video and
audio chunks. There is **no standard header** — ffprobe identifies the file as
raw H264 but misses the audio track entirely.

### Chunk Structure

Every chunk starts with a **24-byte header**:

| Offset | Type        | Description                        |
|--------|-------------|------------------------------------|
| 0      | uint32 LE   | Chunk type (see below)             |
| 4      | uint32 LE   | Payload size in bytes              |
| 8      | uint64 LE   | Timestamp (camera epoch)           |
| 16     | uint64 LE   | Unknown (sequence / flags)         |
| 24     | `<payload>` | Raw H264 NAL units or audio samples|

### Chunk Types

| Type | Content                                    |
|------|--------------------------------------------|
| `0`  | Video P/B-frame (inter, raw H264 NAL)      |
| `1`  | Video I-frame / keyframe (SPS + PPS + IDR) |
| `3`  | Audio frame (PCM or G.711 µ-law)           |

### Video Stream

- Codec: **H264 (Main profile)**
- Resolution: **1920×1080**
- Declared FPS in SPS: 25 fps (incorrect)
- **Real frame rate: 10 fps**
- ~100 frames per 10-second `.media` file
- Must be fed to ffmpeg with `-r 10` to play at correct speed

### Audio Stream

Two codecs have been identified depending on camera model:

| Codec          | Sample rate | Bit depth | Chunk size | Byte signature        |
|----------------|-------------|-----------|------------|-----------------------|
| **PCM 16-bit LE** | 16000 Hz | 16-bit   | 1280 bytes | `c7ff e9ff 0000 ...`  |
| **G.711 µ-law**   | 8000 Hz  | 8-bit    | 320 bytes  | `7e 7f ff ...`        |

Both produce ~250 audio chunks per 10-second `.media` file (40 ms per chunk).
The script auto-detects the codec from the chunk size of the first audio frame.

> **Note:** The H264 SPS declares 25 fps, causing ffmpeg to produce video
> that plays at 2.5× real speed without the `-r 10` correction.
> The audio is not detected by ffmpeg at all without manual demuxing.

---

## Requirements

- Python 3.8+
- ffmpeg (with MKV / Matroska muxer support)

```bash
# Debian / Ubuntu
sudo apt install ffmpeg python3
```

---

## Installation

```bash
git clone https://github.com/javyer/tuya-media-extract
cd tuya-media-extract
chmod +x tuya_media_extract.py
```

---

## Usage

```
tuya_media_extract.py -i INPUT_DIR -o OUTPUT_DIR [options]

Required:
  -i, --input DIR       Input directory (SD card DCIM root or local copy, auto-detected)
  -o, --output DIR      Output directory for MKV files

Options:
  -w, --workers N       Parallel ffmpeg workers (default: 4)
  --audio-codec CODEC   Audio codec: auto (default), pcm or mulaw
  --sample-rate HZ      Audio sample rate — auto-detected if not set
  --fps FPS             Real video frame rate (default: 10)
  --overwrite           Overwrite existing output files
  --tmpdir DIR          Temporary directory (default: output dir)
  -h, --help            Show this help message and exit
```

### Input directory — two supported layouts

The tool **auto-detects** the input structure:

| Layout     | Example                              | Use case                  |
|------------|--------------------------------------|---------------------------|
| SD card    | `DCIM/YYYY/MM/DD/session/*.media`    | Direct from mounted card  |
| Local copy | `backup/DD/session/*.media`          | Copied to hard drive      |

### Audio codec — auto-detection

The script reads the first audio chunk of the first `.media` file and detects
the codec automatically:

| Chunk size | Detected codec        |
|------------|-----------------------|
| 1280 bytes | PCM 16-bit LE 16000Hz |
| 320 bytes  | G.711 µ-law 8000Hz    |

You can override with `--audio-codec pcm` or `--audio-codec mulaw`.

### Examples

**From SD card (auto-detects everything):**
```bash
./tuya_media_extract.py \
    -i /media/user/AD28-21D5/DCIM \
    -o ~/Videos/camera
```

**From a local copy with more workers:**
```bash
./tuya_media_extract.py \
    -i ~/tmp/cam-19-22-Avril2026 \
    -o ~/Videos/camera \
    --workers 8
```

**Force mulaw (override auto-detection):**
```bash
./tuya_media_extract.py \
    -i ~/javcache/cam_salon \
    -o ~/Videos/salon \
    --audio-codec mulaw \
    --sample-rate 8000
```

**Force reprocessing of existing files:**
```bash
./tuya_media_extract.py \
    -i /media/user/AD28-XEDE/DCIM \
    -o ~/Videos/camera \
    --overwrite
```

---

## Output

One MKV file per day, named `YYYY-MM-DD_full.mkv`:

```
~/Videos/camera/
  2026-04-19_full.mkv
  2026-04-20_full.mkv
  2026-04-21_full.mkv
  2026-04-22_full.mkv
```

### Why MKV?

| Feature                    | MP4 | MKV |
|----------------------------|-----|-----|
| Native raw PCM audio       | ⚠️  | ✅  |
| Recoverable if truncated   | ❌  | ✅  |
| Open standard              | ❌  | ✅  |
| Legal / forensic use       | ~   | ✅  |

MKV stores its index at the **beginning** of the file — if a file is
interrupted or partially corrupted, all footage written before the corruption
is still recoverable. MP4 stores its index at the end, making partial files
unplayable.

### Lossless Output

- Video: `-c:v copy` — H264 bitstream copied byte-for-byte from source
- Audio PCM: `-c:a pcm_s16le` — samples copied as-is into MKV
- Audio µ-law: `-c:a pcm_mulaw` — samples copied as-is into MKV

No transcoding, no quality loss.

---

## Known Issues

- Some `.media` files contain fewer than 250 audio chunks (e.g. 156 or 201).
  This happens at session boundaries or after brief camera glitches.
  The tool uses `-async 1` to compensate for minor A/V drift caused by these
  incomplete segments.

- The H264 SPS reports `25 fps` and `20 tbr`. The actual capture rate is
  **10 fps**. Always use `--fps 10` (the default).

---

## How It Works

```
.media files
    │
    ▼
Audio auto-detect     ← reads first audio chunk size to identify codec
    │
    ▼
Chunk parser          ← splits interleaved video/audio chunks by type
    │
    ├─ H264 NAL units ──→ raw .h264 file
    └─ audio samples  ──→ .wav (PCM) or .ulaw (mulaw) file
                              │
                              ▼
                         ffmpeg mux  ← -r 10 -c:v copy -c:a pcm_s16le/pcm_mulaw
                              │
                              ▼
                         segment.mkv (10 seconds)
                              │
                    (×8642 segments, parallel)
                              │
                              ▼
                    ffmpeg concat  ← -f concat -c copy
                              │
                              ▼
                    YYYY-MM-DD_full.mkv
```

---

## Camera Info

| Property          | Value                          |
|-------------------|--------------------------------|
| Platform          | Tuya SmartLife                 |
| Firmware          | v79.1.40                       |
| Recording mode    | Continuous (24/7)              |
| SD card filesystem| FAT32 (vfat)                   |
| Resolution        | 1920×1080                      |
| Video codec       | H264 Main profile, 10 fps real |
| Audio (outdoor)   | PCM 16-bit LE, 16000 Hz, mono  |
| Audio (indoor)    | G.711 µ-law, 8000 Hz, mono     |

---

## License

MIT
