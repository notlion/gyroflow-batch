#!/usr/bin/env python3
"""
Gyroflow Batch: stabilize (Gyroflow) + grade (D-Log M -> Rec.709 LUT) with ONE
lossy encode.

You configure everything in Gyroflow's GUI and save a normal preset -- including
the "Export settings" group. This script then splits that preset in two:

  * the delivery settings (codec, bitrate, audio, size, keyframe distance,
    encoder options) drive the FINAL ffmpeg encode
  * a derived copy with the output block replaced by ProRes drives Gyroflow,
    which renders into a named pipe instead of a file

ffmpeg reads that pipe once and writes two deliveries with those same settings:
the graded one through the 3D LUT, and a stabilized-but-ungraded copy of the log
original (--no-ungraded turns that one off). The source is decoded once, Gyroflow
renders once, and no huge intermediate file is written.

The graded delivery is tagged Rec.709-A -- Rec.709 plus a gama 1.96 atom, so
QuickTime Player on macOS does not shift the gamma. That atom is QuickTime-only,
so H.264/H.265 go into .mov rather than .mp4; --rec709 restores both. The
ungraded copy stays plain Rec.709, the tag its D-Log M source already carries.

Why a pipe works (verified against Gyroflow's source):
  * it renders to "<output_filename>.tmp" and renames when finished
  * it picks the muxer from the extension of the logical name, and ".mkv" is
    explicitly mapped to matroska -- the container that survives a
    non-seekable pipe
  * it hands the path straight to FFmpeg's avformat, with no seekable wrapper

--src-dir is required and holds the clips and the .gyroflow preset. Grading is
opt-in: --lut names the .cube to grade with, and without it only the stabilized,
ungraded delivery is written. Deliveries land in <src-dir>/Stabilized.

A recording the camera split into several files is first joined back into one
with mp4-merge, the tool behind Gyroflow's own merge: losslessly, motion data
included. The parts are moved into <src-dir>/Split parts, and --merge-only does
just this, so the merged files can be renamed before they are stabilized.

Gyroflow is skipped per clip when it would have nothing to do. DJI keeps its
telemetry in a "djmd" data track, and this measures how often orientation samples
arrive there: an unstabilized camera like an O4 Pro air unit logs a couple of
thousand per second, while a gimballed drone like a Mini 5 Pro logs exactly one
per frame -- the attitude of a camera that is already mechanically steady. Clips
of the second kind go straight to the grade, reusing the file mode with the
render already done: ffmpeg reads the clip itself and the LUT is all that is
applied. A clip whose telemetry cannot be read is always stabilized, since
skipping one that needed it is far worse than running one that did not.

Usage:
    ./gyroflow-batch.py --src-dir DIR                    # stabilize every .mp4 there
    ./gyroflow-batch.py --src-dir DIR --lut LUT.cube     # ...and grade them
    ./gyroflow-batch.py --src-dir DIR "ESK8 Heat 1.MP4"  # just these
    ./gyroflow-batch.py --src-dir DIR --dry-run          # the derived plan
    ./gyroflow-batch.py --src-dir DIR --no-ungraded      # graded only
    ./gyroflow-batch.py --src-dir DIR --force-stabilize  # stabilize regardless
    ./gyroflow-batch.py --src-dir DIR --trim-end 5       # drop the landing
    ./gyroflow-batch.py --src-dir DIR --merge-only       # join split recordings
    ./gyroflow-batch.py --src-dir DIR --mode file        # on disk instead
"""

import argparse
import collections
import datetime
import functools
import json
import os
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

# Everything this run puts in the work directory carries this tag, so two
# invocations sharing a work directory cannot unlink each other's pipes.
RUN_ID = os.getpid()

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"


# --------------------------------------------------------------- pretty output

def enable_windows_ansi():
    """Windows 10+ consoles understand the colour and erase-line escapes used
    here, but the classic console window only once a process asks for them."""
    if not IS_WINDOWS:
        return
    import ctypes
    kernel32 = ctypes.windll.kernel32
    for std_handle in (-11, -12):                    # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
        handle = kernel32.GetStdHandle(std_handle)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # VIRTUAL_TERMINAL_PROCESSING

def _c(stream, code, text):
    """Colour only when that stream is a terminal, so redirected logs stay clean."""
    return f"\033[{code}m{text}\033[0m" if stream.isatty() else text

def info(msg):  print(f"{_c(sys.stdout, 36, '==>')} {msg}", flush=True)
def warn(msg):  print(f"{_c(sys.stderr, 33, 'warn:')} {msg}", file=sys.stderr, flush=True)
def die(msg):   print(f"{_c(sys.stderr, 31, 'error:')} {msg}", file=sys.stderr, flush=True); sys.exit(1)


# ------------------------------------------------- gyroflow output -> ffmpeg

# Gyroflow's codec names come from src/ui/menu/Export.qml
PRORES_PROFILE = {
    "proxy": "0", "lt": "1", "standard": "2", "hq": "3", "4444": "4", "4444xq": "5",
}
PRORES_PIXFMT = {
    "0": "yuv422p10le", "1": "yuv422p10le", "2": "yuv422p10le",
    "3": "yuv422p10le", "4": "yuv444p10le", "5": "yuv444p10le",
}
DNXHR_PROFILE = {
    "dnxhr lb": ("dnxhr_lb", "yuv422p"),
    "dnxhr sq": ("dnxhr_sq", "yuv422p"),
    "dnxhr hq": ("dnxhr_hq", "yuv422p"),
    "dnxhr hqx": ("dnxhr_hqx", "yuv422p10le"),
    "dnxhr 444": ("dnxhr_444", "yuv444p10le"),
}
# "AAC" or one of the PCM variants
AUDIO_ENCODER = {
    "aac": "aac",
    "pcm (s16le)": "pcm_s16le", "pcm (s16be)": "pcm_s16be",
    "pcm (s24le)": "pcm_s24le", "pcm (s24be)": "pcm_s24be",
}


def container_ext(codec, rec709a=False):
    """The delivery's extension, or None for a codec this does not deliver --
    image sequences, which have no delivery encode to save, and anything newer
    than the codecs translated in build_video_args."""
    if codec in ("ProRes", "DNxHD", "CineForm"):
        return ".mov"
    if codec in ("H.265/HEVC", "H.264/AVC"):
        # They would default to .mp4, but the gama atom that makes a file
        # Rec.709-A is a QuickTime box: ffmpeg refuses to write it into .mp4
        # ("Not writing 'gama' atom. Format is not MOV."). Delivering them in .mov
        # instead is what Resolve does for a QuickTime H.265 export -- same
        # encode, same bitrate, and the hvc1/avc1 tag is already set either way.
        return ".mov" if rec709a else ".mp4"
    return None


def hw_encoder_args(name):
    """ffmpeg args for one hardware encoder, e.g. hevc_nvenc or h264_qsv."""
    hevc = name.startswith("hevc_")
    args = ["-c:v", name]
    if hevc:
        args += ["-profile:v", "main10"]
    if name == "hevc_videotoolbox":
        args += ["-allow_sw", "1"]
    if name.endswith("_videotoolbox"):
        args += ["-pix_fmt", "yuv420p10le" if hevc else "yuv420p"]
    else:
        # NVENC, Quick Sync and AMF take semi-planar input only
        args += ["-pix_fmt", "p010le" if hevc else "nv12"]
    return args


@functools.lru_cache(maxsize=None)
def hardware_encoder(codec):
    """The hardware encoder for codec ("hevc" or "h264") on this machine, or None.

    A Mac always has VideoToolbox. Anywhere else the GPU vendor is not known in
    advance, and asking ffmpeg is no help: a stock build lists NVENC, Quick Sync
    and AMF whether or not the hardware behind them exists. So each is tried
    with a fraction of a second of black frames, using the exact args the real
    encode will -- which also catches a GPU that has the encoder but not its
    10-bit profile, as NVIDIA cards older than Pascal do for HEVC.
    """
    if IS_MAC:
        return f"{codec}_videotoolbox"
    for family in ("nvenc", "qsv", "amf"):
        name = f"{codec}_{family}"
        try:
            ok = subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error",
                 "-f", "lavfi", "-i", "color=black:s=256x256:r=30:d=0.2"]
                + hw_encoder_args(name) + ["-f", "null", "-"],
                capture_output=True, timeout=30).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        if ok:
            return name
    software = "libx265" if codec == "hevc" else "libx264"
    warn(f"no working GPU encoder for {codec.upper()} (tried NVIDIA NVENC, Intel "
         f"Quick Sync, AMD AMF);\n      encoding with {software} instead, which "
         f"is much slower")
    return None


def build_video_args(out, fps):
    """Translate a Gyroflow `output` block into the final ffmpeg encoder args."""
    codec = out.get("codec", "H.265/HEVC")
    variant = str(out.get("codec_options", "")).strip().lower()
    gpu = bool(out.get("use_gpu", True))
    args = []

    if codec == "H.265/HEVC":
        hw = hardware_encoder("hevc") if gpu else None
        if hw:
            args += hw_encoder_args(hw)
        else:
            args += ["-c:v", "libx265", "-preset", "medium", "-pix_fmt", "yuv420p10le"]
        args += ["-tag:v", "hvc1"]

    elif codec == "H.264/AVC":
        # H.264 is 8-bit here; Gyroflow itself steers 10-bit sources to HEVC.
        hw = hardware_encoder("h264") if gpu else None
        if hw:
            args += hw_encoder_args(hw)
        else:
            args += ["-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p"]

    elif codec == "ProRes":
        prof = PRORES_PROFILE.get(variant, "3")
        args += ["-c:v", "prores_ks", "-profile:v", prof,
                 "-pix_fmt", PRORES_PIXFMT[prof]]

    elif codec == "DNxHD":
        prof, pix = DNXHR_PROFILE.get(variant, ("dnxhr_hq", "yuv422p"))
        args += ["-c:v", "dnxhd", "-profile:v", prof, "-pix_fmt", pix]

    elif codec == "CineForm":
        args += ["-c:v", "cfhd", "-pix_fmt", "yuv422p10le"]

    # Bitrate only means something for the long-GOP codecs; the intra ones are
    # driven entirely by their profile.
    if codec in ("H.265/HEVC", "H.264/AVC"):
        mbps = float(out.get("bitrate", 0) or 0)
        if mbps > 0:
            args += ["-b:v", str(int(round(mbps * 1_000_000)))]
        kfd = float(out.get("keyframe_distance", 0) or 0)
        if kfd > 0 and fps:
            args += ["-g", str(max(1, int(round(fps * kfd))))]

    extra = str(out.get("encoder_options", "")).strip()
    if extra:
        args += shlex.split(extra)

    return args


def build_audio_args(out, source_has_audio):
    if not out.get("audio", True) or not source_has_audio:
        return ["-an"]
    name = str(out.get("audio_codec", "AAC")).strip().lower()
    enc = AUDIO_ENCODER.get(name, "aac")
    args = ["-c:a", enc]
    if enc == "aac":
        args += ["-b:a", "320k"]
    return args


# ------------------------------------------------------------------- probing

Probe = collections.namedtuple(
    "Probe", "fps has_audio seconds created picture telemetry error")


@functools.lru_cache(maxsize=None)
def probe(path):
    """Everything this needs to know about a file, from one ffprobe run:

      fps, has_audio, seconds  of the first video stream, and the file's duration
      created                  creation time in seconds since the epoch
      picture                  (width, height, frame rate, rotation)
      telemetry                index of the DJI "djmd" track
      error                    why ffprobe could not read the file

    Any of them None when the file does not say. Asks for whole sections rather
    than -show_entries naming the rotation: the side-data section has not had the
    same name in every ffprobe, and one that does not know it fails the query.
    """
    unknown = Probe(None, False, None, None, None, None, None)
    run = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json",
         str(path)],
        capture_output=True, encoding="utf-8", errors="replace")
    if run.returncode != 0:
        return unknown._replace(
            error=f"ffprobe failed: {run.stderr.strip() or f'exit {run.returncode}'}")
    try:
        probed = json.loads(run.stdout)
    except json.JSONDecodeError as e:
        return unknown._replace(error=f"unreadable ffprobe output ({e})")
    streams = probed.get("streams", [])
    fmt = probed.get("format", {})

    def number(text):
        try:
            return float(text)
        except (TypeError, ValueError):        # missing, or the literal "N/A"
            return None

    video = next((x for x in streams if x.get("codec_type") == "video"), {})
    num, _, den = str(video.get("r_frame_rate", "")).partition("/")
    fps = number(num) / number(den) if number(num) and number(den) else None

    stamp = (fmt.get("tags") or {}).get("creation_time", "")
    try:
        created = datetime.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        created = None

    rotation = next((x["rotation"] for x in video.get("side_data_list", [])
                     if "rotation" in x),
                    (video.get("tags") or {}).get("rotate", 0))
    picture = (video.get("width"), video.get("height"), video.get("r_frame_rate"),
               int(number(rotation) or 0))
    return Probe(
        fps=fps,
        has_audio=any(x.get("codec_type") == "audio" for x in streams),
        seconds=number(fmt.get("duration")),
        created=created,
        picture=picture,
        telemetry=next((x["index"] for x in streams
                        if x.get("codec_tag_string") == "djmd"), None),
        error=None)


# -------------------------------------------- is there anything to stabilize?

# DJI keeps its telemetry in a "djmd" data track holding protobuf whose schema is
# per-camera (dvtm_O4P.proto, dvtm_Mini5Pro.proto, ...). Rather than model any of
# them, this walks the wire format looking for one thing: unit quaternions, in
# both encodings DJI uses -- 16 bytes of packed floats, and four tagged f32
# fields in a 20-byte submessage.
#
# What separates a clip worth stabilizing from one that is not is the RATE. An O4
# Pro air unit logs orientation ~2000-3000 times a second, far finer than the
# frame rate, which is what rolling-shutter correction and smoothing need. A
# gimballed drone like the Mini 5 Pro logs exactly one sample per frame: the
# attitude of a camera that is already mechanically steady, which Gyroflow has
# nothing to improve on.

# Two samples per frame is the line. Measured against the two cameras this was
# built from, an O4 Pro sits at 33x the frame rate and a Mini 5 Pro at 1.0x, so
# there is a wide gap either side and the exact factor is not delicate.
MOTION_RATE_FACTOR = 2

# How many seconds of the telemetry track to read. Enough to average over, small
# enough that it is a few hundred kB and about a tenth of a second.
MOTION_WINDOW = 3


def _varint(d, i):
    value = shift = 0
    while i < len(d):
        b = d[i]; i += 1
        value |= (b & 0x7f) << shift; shift += 7
        if not b & 0x80:
            return value, i
    raise IndexError("truncated varint")


def _is_unit_quaternion(f):
    """Four finite components on the unit sphere. Real orientation samples pass;
    the float-shaped coincidences in a telemetry blob essentially never do."""
    return (all(x == x and abs(x) <= 1.001 for x in f)
            and 0.98 < sum(x * x for x in f) < 1.02)


def _count_quaternions(d, depth=0, maxdepth=8):
    """Count unit quaternions anywhere in a protobuf message.

    Field numbers are never interpreted, only skipped over, so this neither knows
    nor cares which DJI schema it is reading: an unfamiliar one simply scores low
    rather than being misread.
    """
    found = i = 0
    while i < len(d):
        try:
            key, i = _varint(d, i)
            field, wire = key >> 3, key & 7
            if field == 0:                       # not protobuf, or run off the end
                return found
            if wire == 0:
                _, i = _varint(d, i)
            elif wire == 1:
                i += 8
            elif wire == 5:
                i += 4
            elif wire == 2:
                length, i = _varint(d, i)
                if i + length > len(d):
                    return found
                blob = d[i:i + length]; i += length
                if length == 16:
                    if _is_unit_quaternion(struct.unpack("<4f", blob)):
                        found += 1
                elif length == 20 and all(blob[j] & 7 == 5 for j in (0, 5, 10, 15)):
                    if _is_unit_quaternion([struct.unpack("<f", blob[j + 1:j + 5])[0]
                                            for j in (0, 5, 10, 15)]):
                        found += 1
                if depth < maxdepth and length > 1:
                    found += _count_quaternions(blob, depth + 1, maxdepth)
            else:                                # wire types 3/4 are long gone
                return found
        except (IndexError, struct.error):
            return found
    return found


def orientation_rate(src):
    """Orientation samples per second in this clip's DJI telemetry track.

    None means "could not tell" -- no DJI track, or a payload this does not
    recognise -- and never "none found". Callers must stabilize when unsure:
    running a render that was not needed costs time, skipping one that was
    silently ships unstabilized footage.
    """
    index = probe(src).telemetry
    if index is None:
        return None

    # -t ahead of -i stops the demuxer early, so this reads a few hundred kB off
    # the front of the file rather than walking a 2 GB clip.
    data = subprocess.run(
        ["ffmpeg", "-v", "error", "-t", str(MOTION_WINDOW), "-i", str(src),
         "-map", f"0:{index}", "-c", "copy", "-f", "data", "-"],
        capture_output=True).stdout
    # DJI names its schema in the first packet. Without that marker this is a
    # payload we have never seen, where a low count would prove nothing.
    if b".proto" not in data[:4096]:
        return None
    return _count_quaternions(data) / MOTION_WINDOW


def decide_stabilize(src, args):
    """(run Gyroflow?, the sample rate that decided it) for one clip."""
    if args.force_stabilize:
        return True, None
    fps = probe(src).fps
    rate = orientation_rate(src)
    if rate is None or not fps:
        return True, rate
    return rate > MOTION_RATE_FACTOR * fps, rate


# ----------------------------------------------------------- split recordings

# Cameras split a long recording into files of just under 4 GB, the FAT32 limit.
# Gyroflow joins them back with mp4-merge, losslessly and with every track --
# the motion data included -- but only from its GUI: its CLI has no merge. So
# this runs mp4-merge's own CLI, the same code Gyroflow's merge calls.
#
# Nothing links the parts of one recording, so they are told apart the way
# mp4-merge itself tells its chapters from separate recordings: by the files'
# creation times, in whole seconds. A camera stamps each part with its own start
# (DJI) or every part with the recording's (GoPro), so a file continues the one
# before it when it has the same time, or that time plus the earlier file's
# duration to within 2 s. Separate recordings are further apart -- a camera has
# to close one file and start the next -- and on top of that Gyroflow's own merge
# check applies: the same picture size, frame rate and rotation.
MERGE_TOLERANCE = 2

# The parts are moved here once merged, so the folder holds each recording once
# and the next run does not process it twice. Not deleted: that is for the user.
PARTS_DIR = "Split parts"

# What Gyroflow's GUI names a merged file
MERGED_SUFFIX = "_joined"

MERGE_RE = re.compile(r"Merging\.\.\. ([\d.]+)%")

def continues(a, b):
    """Whether part b is the next file of the recording part a belongs to, and if
    not, why not."""
    if a.created is None or b.created is None or a.seconds is None:
        return False, "no creation time to go by, or could not be read"
    if a.picture != b.picture:
        return False, f"different picture: {a.picture} vs {b.picture}"
    since = b.created - a.created
    if since == 0 or abs(since - a.seconds) <= MERGE_TOLERANCE:
        return True, ""
    return False, (f"starts {since:.0f} s after it, and it runs {a.seconds:.1f} s "
                   f"-- a gap of {since - a.seconds:.1f} s")


def split_recordings(files):
    """files grouped into recordings, in order: a list of one file for most, of
    several for a split recording. Only neighbours in name order can be parts of one
    recording, as in Gyroflow, and camera names sort by time anyway.

    Also returns, for each pair of neighbours that were not joined, why not.
    """
    groups, apart = [], []
    prev = None
    for f in files:
        part = probe(f)
        if part.error and f.is_file():
            warn(f"cannot tell whether {f.name} is part of a split recording: {part.error}")
        joined, why = continues(prev, part) if groups else (False, "")
        if joined:
            groups[-1].append(f)
        else:
            if groups:
                apart.append(f"{groups[-1][-1].name} -> {f.name}: {why}")
            groups.append([f])
        prev = part
    return groups, apart


def find_mp4_merge(given):
    """The mp4-merge CLI. Its releases carry the platform in the file name
    (mp4_merge-mac-arm64, mp4_merge-windows64.exe), so one dropped beside this
    script is found as downloaded, as well as one on PATH."""
    if given:
        return given if given.is_file() else None
    found = shutil.which("mp4_merge")
    if found:
        return Path(found)
    beside = sorted(f for f in Path(__file__).resolve().parent.glob("mp4_merge*")
                    if f.is_file())
    return beside[0] if beside else None


def run_mp4_merge(mp4_merge, parts, target):
    """Merge parts into target, drawing a progress bar. Returns (exit code, the
    tool's output without its progress).

    The merge goes to a hidden name and gets target's only once it succeeded. The
    mp4-merge release (0.1.11) writes straight into its output, so an interrupted
    merge would otherwise leave a truncated file under the name that later runs
    take to mean "already merged".
    """
    partial = target.with_name(f".{target.name}.{RUN_ID}.partial")
    cmd = [str(mp4_merge)] + [str(p) for p in parts] + ["--out", str(partial)]
    output = []

    # Its progress is "\rMerging... 12.34%" with no newline, which universal
    # newlines still split into lines.
    def progress(line):
        m = MERGE_RE.search(line)
        return ("merge ", float(m.group(1)), f"{len(parts)} parts") if m else None

    try:
        rc = run_with_progress(cmd, progress, output.append)
        if rc == 0:
            os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    return rc, output


def merge_split_recordings(files, args):
    """Join each split recording among files into one file beside its parts, and
    move the parts into PARTS_DIR. Returns (the files to process, each recording
    once; the recordings that failed to merge).

    A merged file that already exists is used as it is: run_mp4_merge only ever
    gives it that name once it is complete.
    """
    groups, apart = split_recordings(files)
    split = [g for g in groups if len(g) > 1]
    if not split:
        if args.merge_only:
            info("no split recordings found")
            for line in apart:
                print(f"    {line}")
        return files, []

    mp4_merge = find_mp4_merge(args.mp4_merge)
    if mp4_merge is None and not args.dry_run:
        die(f"{len(split)} split recording(s) to merge, but the mp4_merge tool was not "
            f"found.\n   Download it from https://github.com/gyroflow/mp4-merge/releases "
            f"and put it beside\n   this script or on PATH, or pass --mp4-merge. "
            f"--no-merge processes the parts one by one.")

    result, failed = [], []
    try:
        for group in groups:
            merged = merge_one(group, mp4_merge, args)
            if merged is None:
                failed.append(f"{group[0].stem}{MERGED_SUFFIX}{group[0].suffix}")
            else:
                result.append(merged)
    except KeyboardInterrupt:
        print()
        warn("aborted by user")
        sys.exit(130)
    return result, failed


def merge_one(group, mp4_merge, args):
    """One recording of merge_split_recordings: the file that stands for it from
    here on, or None when its merge failed."""
    if len(group) == 1:
        return group[0]
    first = group[0]
    target = first.with_name(f"{first.stem}{MERGED_SUFFIX}{first.suffix}")
    print()
    info(f"split recording, {len(group)} parts: {first.name} … {group[-1].name}")
    info(f"  merged as:  {target.name}  (parts moved to {PARTS_DIR}/)")
    if args.dry_run:
        # Nothing is merged, so the rest of the plan is shown on the first part
        return first

    if target.exists():
        info(f"already merged, using it: {target.name}")
    else:
        started = time.monotonic()
        rc, output = run_mp4_merge(mp4_merge, group, target)
        if rc != 0:
            warn(f"merge failed (exit {rc}); its parts are left as they are")
            for line in output[-20:]:
                print(f"    mp4_merge| {line}", file=sys.stderr)
            return None
        info(f"merged in {fmt_duration(time.monotonic() - started)}")
        # Dated like the recording rather than the merge, so it sorts among the
        # footage. mp4-merge tries this too, but not on every platform.
        stat = first.stat()
        os.utime(target, (stat.st_atime, stat.st_mtime))

    parts_dir = first.parent / PARTS_DIR
    parts_dir.mkdir(exist_ok=True)
    for part in group:
        dest = parts_dir / part.name
        if dest.exists():
            warn(f"{PARTS_DIR}/{part.name} already exists; leaving {part.name} in place")
            continue
        try:
            part.rename(dest)
        except OSError as e:
            warn(f"could not move {part.name} into {PARTS_DIR}/ ({e.strerror}); "
                 f"move it out of the way before the next run")
    return target


# ------------------------------------------------------------ filter graph

# Gyroflow writes its log AND its progress bar to stdout, so they cannot be
# separated by redirection. Instead we take the stream over: --stdout-progress
# emits these parseable lines, we filter out everything else into a log file,
# and we draw the progress ourselves.
RENDER_RE = re.compile(r"Rendering progress:\s*(\d+)/(\d+) frames \(([\d.]+)%\) ETA ([\d.]+)s")
SYNC_RE = re.compile(r"Synchronization progress:\s*([\d.]+)%")
# Gyroflow's own indicatif bar, which we discard rather than echo.
NOISE_RE = re.compile(r"Elapsed:|^\[[#>\-]*\]|^[⠀-⣿]")


def fmt_duration(seconds):
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds}s"


def draw_progress(label, pct, detail=""):
    # The line has to stay inside the window. One that reaches the last column
    # wraps, and then \r rewinds only to the start of the wrapped fragment --
    # every update leaves the row above it behind. Shrink the bar to fit, and
    # trim the detail once the bar is down to its floor.
    budget = max(1, shutil.get_terminal_size((80, 24)).columns - 1)
    fixed = len(f"    {label} [] {pct:5.1f}%  ")
    width = max(10, min(30, budget - fixed - len(detail)))
    detail = detail[:max(0, budget - fixed - width)]
    filled = max(0, min(width, int(round(width * pct / 100))))
    bar = "#" * filled + "-" * (width - filled)
    line = f"    {label} [{bar}] {pct:5.1f}%  {detail}".rstrip()
    sys.stdout.write(f"\r{line[:budget]}\033[K")
    sys.stdout.flush()


def clear_progress():
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()


def run_with_progress(cmd, progress, keep=None, stderr=subprocess.STDOUT):
    """Run cmd, drawing a progress bar from its output. Returns its exit code.

    progress(line) reads one stripped line of the tool's stdout: it returns a
    (label, percent, detail) to draw, None for a line to hand to keep, or False
    for one to drop. The bar is drawn on a terminal only, so redirected output
    stays clean. Interrupted, the tool is killed rather than left running: a
    render or encode carrying on would write the files the caller cleans up.

    A tool that cannot be started at all -- not executable (a download not yet
    chmod +x), or built for another platform -- is reported to keep as a line of
    its own, with exit code 127, as a shell would.
    """
    tty = sys.stdout.isatty()
    drew = False
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr,
                                encoding="utf-8", errors="replace")
    except OSError as e:
        if keep:
            keep(f"could not start {cmd[0]}: {e.strerror}")
        return 127
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            bar = progress(line)
            if bar is None:
                if keep:
                    keep(line)
            elif bar and tty:
                draw_progress(*bar)
                drew = True
    except BaseException:
        proc.kill()
        raise
    finally:
        proc.stdout.close()
        rc = proc.wait()
        if drew:
            clear_progress()
    return rc


def gyroflow_progress(line):
    if NOISE_RE.search(line):
        return False
    m = RENDER_RE.search(line)
    if m:
        cur, total, pct, eta = m.groups()
        return ("render", float(pct), f"{cur}/{total} frames  ETA {fmt_duration(float(eta))}")
    m = SYNC_RE.search(line)
    if m:
        return ("sync  ", float(m.group(1)), "")
    return None


def run_gyroflow(cmd, log_path, verbose):
    if verbose:
        return subprocess.run(cmd).returncode
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        return run_with_progress(cmd, gyroflow_progress,
                                 lambda line: log.write(line + "\n"), stderr=log)


# -progress emits plain key=value lines; out_time is the only one worth reading.
# The out_time_us/out_time_ms lines beside it do not match, which is the point.
FF_TIME_RE = re.compile(r"^out_time=(\d+):(\d+):([\d.]+)")


def run_ffmpeg(cmd, log_path, seconds):
    """Encode, drawing a progress bar from ffmpeg's own -progress stream.

    Only for the branch where ffmpeg runs on its own. In pipe mode it is Gyroflow
    that sets the pace and its bar already has that line; here nothing else is
    reporting, so without this the terminal sits silent for the whole encode.
    """
    if not (sys.stdout.isatty() and seconds):
        with log_path.open("wb") as log:
            return subprocess.run(cmd, stdout=log, stderr=log).returncode

    # Progress goes to stdout for us to read, which leaves stderr alone as the log.
    cmd = cmd[:1] + ["-progress", "pipe:1"] + cmd[1:]
    started = time.monotonic()

    def progress(line):
        m = FF_TIME_RE.match(line)
        if not m:
            return False
        hours, mins, secs = m.groups()
        done = int(hours) * 3600 + int(mins) * 60 + float(secs)
        detail = f"{fmt_duration(done)}/{fmt_duration(seconds)}"
        if done > 0:
            eta = (seconds - done) * (time.monotonic() - started) / done
            detail += f"  ETA {fmt_duration(max(0, eta))}"
        return ("grade ", min(100.0, done / seconds * 100), detail)

    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        return run_with_progress(cmd, progress, stderr=log)


def dump_log(path, label, limit=40):
    if path.exists() and path.stat().st_size:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > limit:
            print(f"    {label}| … {len(lines) - limit} earlier lines omitted",
                  file=sys.stderr)
        for line in lines[-limit:]:
            print(f"    {label}| {line}", file=sys.stderr)


def visible_files(folder, suffix):
    """Files with this suffix, ignoring dotfiles — on external volumes macOS
    litters AppleDouble stubs (._name.cube) that sort ahead of the real thing."""
    return sorted(f for f in folder.iterdir()
                  if f.is_file()
                  and not f.name.startswith(".")
                  and f.suffix.lower() == suffix)


def find_one(folder, suffix, flag):
    matches = visible_files(folder, suffix)
    if not matches:
        die(f"no {suffix} file in {folder}\n   Put one there or pass {flag} explicitly.")
    if len(matches) > 1:
        warn(f"{len(matches)} {suffix} files here; using {matches[0].name} "
             f"— pass {flag} to pick another")
    return matches[0]


def gyroflow_failure_hint(rc):
    if rc == 101 and IS_MAC:
        warn("exit 101 is a Gyroflow panic. If the trace says PermissionDenied, you are\n"
             "      running the sandboxed Mac App Store build — its sandbox blocks the CLI\n"
             "      from reading files outside its own container, including this script's\n"
             "      temp directory. Install the direct download from https://gyroflow.xyz.")


def check_fifo_support(work):
    probe_path = work / ".fifo-support-probe"
    probe_path.unlink(missing_ok=True)
    try:
        os.mkfifo(probe_path)
    except OSError as e:
        die(f"cannot create a named pipe in {work} ({e.strerror}).\n"
            f"   That filesystem does not support FIFOs — exFAT and most network\n"
            f"   shares do not. Point --work at an APFS/HFS+ location that Gyroflow\n"
            f"   can read, or fall back to --mode file.")
    finally:
        probe_path.unlink(missing_ok=True)


def prevent_sleep():
    """Hold off idle sleep for as long as this process lives.

    -w hands caffeinate our pid to watch, so it releases the assertion when we
    go away *however* we go away. Holding it in-process would leak it on the one
    exit path that matters: a default-disposition SIGHUP (closing the terminal)
    or SIGTERM tears the interpreter down without running a single finally block.

    Display sleep is left alone -- the screen can go dark overnight without
    touching the render. Add "-d" if you want it awake too. Note that -s only
    applies on AC power, and closing the lid still sleeps the machine.

    Windows gives the same guarantee natively: the request belongs to this
    thread and lapses when the process exits, however it exits.
    """
    if IS_WINDOWS:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        if ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
            return True
        warn("could not hold off sleep; the machine may sleep mid-render")
        return None
    if not shutil.which("caffeinate"):
        warn("caffeinate not on PATH; the machine may sleep mid-render")
        return None
    try:
        return subprocess.Popen(["caffeinate", "-i", "-m", "-s", "-w", str(os.getpid())],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        warn(f"could not start caffeinate ({e}); the machine may sleep mid-render")
        return None


def default_gyroflow():
    """Where the Gyroflow CLI usually lives. The Mac app installs to one place;
    the Windows download is a zip that can be unpacked anywhere, so PATH is
    asked first and Program Files is only the guess after that."""
    if IS_MAC:
        return Path("/Applications/Gyroflow.app/Contents/MacOS/gyroflow")
    found = shutil.which("gyroflow")
    if found:
        return Path(found)
    if IS_WINDOWS:
        return Path(os.environ.get("ProgramFiles", r"C:\Program Files"), "Gyroflow", "gyroflow.exe")
    return Path("/usr/local/bin/gyroflow")


GYROFLOW_EXAMPLE = ("/path/to/Gyroflow.app/Contents/MacOS/gyroflow" if IS_MAC
                    else r"C:\path\to\Gyroflow\gyroflow.exe" if IS_WINDOWS
                    else "/path/to/gyroflow")


def escape_filter_path(p):
    """Escape a path for use as a bare option value in an ffmpeg filtergraph.

    ffmpeg unescapes such a value twice, so each pass gets its own backslashes:
    first the filter's own option parser (\\ ' :), then the filtergraph parser
    (\\ ' , ; [ ]). Quoting instead of escaping cannot express a path with an
    apostrophe — a quoted run has no escape character of its own — so a folder
    like "Ryan's Footage" only survives this way.
    """
    escaped = re.sub(r"([\\':])", r"\\\1", str(p))          # first level
    return re.sub(r"([\\',;\[\]])", r"\\\1", escaped)       # second level


def build_filter_complex(lut, kinds):
    """One decode of the pipe feeding one or both delivery encodes.

    'graded' gets the 3D LUT; 'plain' is the stabilized D-Log M passed straight
    through, so the log original survives with the same delivery settings.
    """
    # setparams pins how the pipe's frames are interpreted (DJI tags D-Log M as
    # bt709/limited); the LUT is then evaluated on 0..1 full-range values in
    # 16-bit, exactly as Resolve does with clip levels set to Video.
    head = "setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=bt709"
    grade = ("format=gbrp16le,"
             f"lut3d=file={escape_filter_path(lut)}:interp=tetrahedral,"
             "format=yuv420p10le")

    if kinds == ["graded"]:
        return f"[0:v]{head},{grade}[graded]"
    if kinds == ["plain"]:
        return f"[0:v]{head}[plain]"
    # split feeds both branches from the single decode; the encoders run in
    # parallel inside this one ffmpeg, so Gyroflow still renders only once.
    return f"[0:v]{head},split=2[gin][plain];[gin]{grade}[graded]"


# --------------------------------------------------------------- the two ends

# Every way a Gyroflow preset or project has spelled its trims: trim_ranges_ms
# now, trim_ranges (fractions of the clip) and trim_start/trim_end before that.
TRIM_KEYS = ("trim_ranges_ms", "trim_ranges", "trim_start", "trim_end")


def make_intermediate_preset(preset, work, trim_end=0):
    """Same preset with the output block dropped -- the ProRes settings are
    passed via -p instead, which keeps Gyroflow's apply_to_all path (and its
    filename regeneration) out of the picture entirely.

    trim_end cuts that many seconds off the end of every clip Gyroflow renders.
    Gyroflow reads a negative end in trim_ranges_ms as counted back from the end
    of the clip, so one preset serves clips of any length. And it is a real trim
    rather than a cut afterwards: the zoom is fitted to the frames inside the
    range only, so a rough landing no longer sets the crop for the whole flight.
    """
    derived = {k: v for k, v in preset.items() if k != "output"}
    derived["name"] = f"{preset.get('name', 'preset')} (intermediate)"
    if trim_end:
        for key in TRIM_KEYS:
            derived.pop(key, None)
        derived["trim_ranges_ms"] = [[0, -round(trim_end * 1000)]]
    path = work / f"intermediate-{RUN_ID}.gyroflow"
    path.write_text(json.dumps(derived, indent=4, sort_keys=True))
    return path


def folder_url(folder):
    """The file:// URL Gyroflow takes as its output folder, unencoded, as it has
    always been handed one. A Windows path needs forward slashes and a third
    slash ahead of its drive letter: file:///C:/Users/..., not file://C:\\Users."""
    path = folder.as_posix()
    return "file://" + (path if path.startswith("/") else "/" + path)


def make_out_params(out, work, filename, prores_variant):
    """Gyroflow's -p JSON: ProRes into the pipe, at the preset's output size."""
    params = {
        "output_folder": folder_url(work),
        "output_filename": filename,
        "codec": "ProRes",
        "codec_options": prores_variant,
        "bitrate": 0,
        "use_gpu": True,
        "audio": bool(out.get("audio", True)),
        # PCM through the pipe so the delivery encode is the only audio encode
        "audio_codec": "PCM (s24le)",
        "output_width": int(out.get("output_width", 3840)),
        "output_height": int(out.get("output_height", 2160)),
        "interpolation": out.get("interpolation", "Lanczos4"),
        "pad_with_black": bool(out.get("pad_with_black", False)),
        "preserve_other_tracks": False,
    }
    return json.dumps(params)


def make_delivery_params(out, work, filename):
    """Gyroflow's -p JSON for rendering the delivery itself: the preset's own
    export settings, aimed at the work directory. Where they would point the
    render elsewhere -- an output_path, a forced extension -- they are dropped."""
    params = {k: v for k, v in out.items() if k not in ("output_path", "output_extension")}
    params["output_folder"] = folder_url(work)
    params["output_filename"] = filename
    return json.dumps(params)


def place(rendered, target):
    """Move a finished render onto its delivery name: a rename on one volume, a
    copy across two. Replaces what is there, which --overwrite has allowed."""
    try:
        os.replace(rendered, target)
    except OSError:
        shutil.move(str(rendered), str(target))


def deliveries(src, stabilize, args, ext):
    """What a clip is delivered as: (kind, path) for each file, "graded" through
    the LUT and "plain" for the ungraded copy.

    A clip that is not stabilized must not be named as though it were, and its
    ungraded copy would be a plain transcode of the source. Without a LUT there
    is only the ungraded copy.
    """
    out = []
    if args.lut:
        tag = args.suffix if stabilize else args.unstabilized_suffix
        out.append(("graded", args.out_dir / f"{src.stem}{tag}{ext}"))
    if args.ungraded and stabilize:
        out.append(("plain", args.out_dir / f"{src.stem}{args.ungraded_suffix}{ext}"))
    return out


# ------------------------------------------------------------------- runner

def run_one(src, args, out_block, inter_preset, work, decision):
    stem = src.stem
    codec = out_block.get("codec", "H.265/HEVC")
    ext = container_ext(codec, args.rec709a)
    clip = probe(src)
    fps, has_audio, seconds = clip.fps, clip.has_audio, clip.seconds
    stabilize, rate = decision
    grade = args.lut is not None
    if not stabilize:
        why = (f"no motion data to stabilize with — {rate:.0f} orientation samples/s "
               f"at {fps:.2f} fps is one per frame")
        if not grade:
            info(f"{why}, and no --lut to grade with; nothing to do")
            return "skipped"
        info(f"{why}; grading only")
    elif args.trim_end and seconds and seconds <= args.trim_end:
        warn(f"{src.name} runs {seconds:.1f} s, so trimming {args.trim_end:g} s off "
             f"its end would leave nothing; skipping it")
        return "skipped"

    targets = deliveries(src, stabilize, args, ext)

    # Each output is skipped on its own, so a run that only lost one of the two
    # re-renders just that one.
    if not args.overwrite:
        for target in list(targets):
            if target[1].exists():
                info(f"exists, skipping: {target[1].name}")
                targets.remove(target)
    if not targets:
        return "skipped"

    # Without a LUT a stabilized clip has a single delivery and nothing left to do
    # to it after Gyroflow, so Gyroflow writes it itself with the preset's own
    # export settings. That is still the one lossy encode, but without the
    # ProRes intermediate, the pipe (or ~100 GB file), or ffmpeg decoding and
    # re-encoding it. Gyroflow carries the source's colour tags over to its
    # output -- the plain Rec.709 the ungraded copy gets anyway -- and tags H.265
    # hvc1 itself.
    direct = stabilize and not grade

    want_audio = has_audio and bool(out_block.get("audio", True))
    comment = (out_block.get("metadata") or {}).get("comment", "")

    ffmpeg_cmd = (
        ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats", "-y"]
        + ["-i", None]                      # filled in below
        + ["-filter_complex", build_filter_complex(args.lut, [k for k, _ in targets])]
    )
    for kind, path in ([] if direct else targets):
        ffmpeg_cmd += ["-map", f"[{kind}]"]
        ffmpeg_cmd += ["-map", "0:a:0"] if want_audio else []
        ffmpeg_cmd += build_video_args(out_block, fps)
        ffmpeg_cmd += build_audio_args(out_block, has_audio)
        ffmpeg_cmd += ["-color_range", "tv", "-colorspace", "bt709",
                       "-color_primaries", "bt709", "-color_trc", "bt709"]
        # Those four tags alone are plain Rec.709 -- 1/1/1 limited, which is
        # exactly what the DJI source carries, so the ungraded log copy keeps
        # them and nothing else. Rec.709-A is that same tag plus a gama atom of
        # 1.96; QuickTime Player reads it and stops shifting the gamma, and it
        # only ever goes on the graded delivery, which is the one that is
        # actually Rec.709 after the LUT.
        gama = args.rec709a and kind == "graded" and ext == ".mov"
        movflags = "+faststart" + ("+write_gama" if gama else "")
        ffmpeg_cmd += ["-movflags", movflags] if ext in (".mp4", ".mov") else []
        ffmpeg_cmd += ["-mov_gamma", "1.96"] if gama else []
        ffmpeg_cmd += ["-metadata", f"comment={comment}"] if comment else []
        ffmpeg_cmd += [str(path)]

    # Gyroflow renders to "<output_filename>.tmp" and renames only once it has
    # finished, so both names have to be tracked in both modes. Miss the .tmp and
    # an interrupted --mode file run strands a full-size intermediate -- upwards
    # of 100 GB per clip that nothing else will ever clean up.
    #
    # The name goes to Gyroflow inside -p, whose parser turns every ' into " before
    # reading the JSON, so a clip named "Ryan's Heat" would break it: quotes are
    # left out. A direct render is moved onto its delivery name once finished.
    safe_stem = re.sub(r"['\"]", "_", stem)
    gf_ext = ext[1:] if direct else "mov" if args.mode == "file" else "mkv"
    gf_filename = f"{safe_stem}.{RUN_ID}.{gf_ext}"
    gf_final = work / gf_filename
    gf_tmp = work / f"{gf_filename}.tmp"

    # File mode hands ffmpeg the finished render; pipe mode hands it the FIFO,
    # which lives at the .tmp name because that is what Gyroflow opens. A clip
    # with no motion data has no render at all, so it reads the clip itself.
    ff_input = src if not stabilize else gf_final if args.mode == "file" else gf_tmp
    ffmpeg_cmd[ffmpeg_cmd.index(None)] = str(ff_input)

    out_params = (make_delivery_params(out_block, work, gf_filename) if direct
                  else make_out_params(out_block, work, gf_filename, args.prores))
    # --stdout-progress gives us parseable progress lines. Its own bar would fight
    # with them on the same stream, but we consume stdout and discard the bar.
    gf_cmd = [str(args.gyroflow), str(src), "--preset", str(inter_preset),
              "-f", "--stdout-progress", "-p", out_params]

    if args.dry_run:
        if stabilize:
            print(f"  gyroflow: {shlex.join(gf_cmd)}")
        if direct:
            print(f"  then:     move {gf_final.name} to {targets[0][1]}")
        else:
            print(f"  ffmpeg:   {shlex.join(ffmpeg_cmd)}")
        return "skipped"

    for leftover in (gf_tmp, gf_final):
        leftover.unlink(missing_ok=True)

    fflog = work / f"{stem}.{RUN_ID}.ffmpeg.log"
    gflog = work / f"{stem}.{RUN_ID}.gyroflow.log"
    ff = None
    started = time.monotonic()
    try:
        if direct:
            gf_rc = run_gyroflow(gf_cmd, gflog, args.verbose)
            if gf_rc != 0:
                warn(f"gyroflow failed on {src.name} (exit {gf_rc})")
                dump_log(gflog, "gyroflow")
                gyroflow_failure_hint(gf_rc)
                return "failed"
            place(gf_final, targets[0][1])
            info(f"done in {fmt_duration(time.monotonic() - started)}: {targets[0][1].name}")
            return "ok"

        if not stabilize or args.mode == "file":
            # A clip we are not stabilizing is this branch with the render already
            # sitting there: ffmpeg reads a finished file either way.
            gf_rc = run_gyroflow(gf_cmd, gflog, args.verbose) if stabilize else 0
            if gf_rc != 0:
                warn(f"gyroflow failed on {src.name} (exit {gf_rc})")
                dump_log(gflog, "gyroflow")
                gyroflow_failure_hint(gf_rc)
                return "failed"
            # How long the file ffmpeg is about to read runs for, which is the
            # source only when it was not stabilized: trims in the preset make its
            # render shorter, and the bar should reach 100% on the real thing.
            if ff_input != src:
                seconds = probe(ff_input).seconds
            ff_rc = run_ffmpeg(ffmpeg_cmd, fflog, seconds)
        else:
            os.mkfifo(gf_tmp)
            # Reader first: opening a FIFO blocks until the other end shows up.
            with fflog.open("wb") as log:
                ff = subprocess.Popen(ffmpeg_cmd, stdout=log, stderr=log)
                gf_rc = run_gyroflow(gf_cmd, gflog, args.verbose)
                if gf_rc != 0:
                    warn(f"gyroflow failed on {src.name} (exit {gf_rc}) — stopping the encoder")
                    dump_log(gflog, "gyroflow")
                    gyroflow_failure_hint(gf_rc)
                    ff.send_signal(signal.SIGTERM)
                    # If Gyroflow died before opening its end, ffmpeg is still
                    # blocked in open() on the FIFO and can sit through SIGTERM.
                    # Escalate instead of waiting on a writer that is not coming.
                    try:
                        ff.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        warn("encoder did not stop; killing it")
                        ff.kill()
                ff_rc = ff.wait()

        elapsed = fmt_duration(time.monotonic() - started)
        if gf_rc == 0 and ff_rc == 0:
            info(f"done in {elapsed}: {', '.join(p.name for _, p in targets)}")
            return "ok"

        rcs = f"gyroflow={gf_rc} ffmpeg={ff_rc}" if stabilize else f"ffmpeg={ff_rc}"
        warn(f"failed after {elapsed}: {src.name} ({rcs})")
        dump_log(fflog, "ffmpeg")
        for _, path in targets:
            path.unlink(missing_ok=True)
        return "failed"

    except KeyboardInterrupt:
        warn("interrupted — cleaning up")
        if ff and ff.poll() is None:
            ff.kill()
            ff.wait()
        for _, path in targets:
            path.unlink(missing_ok=True)
        raise
    finally:
        for leftover in (gf_tmp, gf_final, fflog, gflog):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass


# --------------------------------------------------------------------- main

def main():
    enable_windows_ansi()
    p = argparse.ArgumentParser(
        description="Gyroflow stabilization + LUT grade in a single delivery encode.",
        # Shown whenever --src-dir is missing, which includes running with no
        # arguments at all -- so this doubles as the getting-started text.
        epilog='examples:\n'
               '  %(prog)s --src-dir "/Volumes/Footage/Race Day" --dry-run\n'
               '      show the derived plan without touching anything\n'
               '\n'
               '  %(prog)s --src-dir "/Volumes/Footage/Race Day"\n'
               '      stabilize every .mp4 there into\n'
               '      "/Volumes/Footage/Race Day/Stabilized"\n'
               '\n'
               '  %(prog)s --src-dir "/Volumes/Footage/Race Day" --lut "D-Log M to Rec.709.cube"\n'
               '      the same, plus a graded delivery beside each one\n',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="*", type=Path,
                   help="specific clips (default: every .mp4 in --src-dir)")
    p.add_argument("--src-dir", type=Path, default=None,
                   help="REQUIRED: folder holding the clips and the .gyroflow preset")
    p.add_argument("--out-dir", type=Path, default=None,
                   help="default: <src-dir>/Stabilized")
    p.add_argument("--preset", type=Path, default=None,
                   help="default: the only .gyroflow file in --src-dir")
    p.add_argument("--lut", type=Path, default=None,
                   help="the .cube LUT to grade with. Without it nothing is graded: "
                        "stabilized clips get only the ungraded delivery, and clips "
                        "with nothing to stabilize are skipped")
    p.add_argument("--gyroflow", type=Path,
                   default=default_gyroflow(),
                   help="the Gyroflow CLI binary -- on macOS the one inside the app "
                        "bundle, on Windows gyroflow.exe (default: %(default)s)")
    p.add_argument("--work", type=Path, default=None,
                   help="where the pipe and derived preset live; must support FIFOs "
                        "(exFAT and most network shares do not). "
                        "Default: <src-dir>/.gyroflow-work, removed again if left empty.")
    p.add_argument("--prores", default="4444",
                   choices=["Proxy", "LT", "Standard", "HQ", "4444", "4444XQ"],
                   help="intermediate ProRes variant through the pipe (default: 4444)")
    p.add_argument("--suffix", default=" Stabilized Graded",
                   help="name tag for the graded delivery, which is only used for a "
                        "clip that was stabilized (default: %(default)r)")
    p.add_argument("--ungraded-suffix", default=" Stabilized",
                   help="name tag for the ungraded delivery (default: %(default)r)")
    p.add_argument("--unstabilized-suffix", default=" Graded",
                   help="name tag for a clip that is graded without being stabilized, "
                        "because it carries no motion data (default: %(default)r)")
    p.add_argument("--no-ungraded", dest="ungraded", action="store_false",
                   help="skip the stabilized-but-ungraded copy that is written alongside "
                        "the graded one by default")
    p.add_argument("--rec709", dest="rec709a", action="store_false",
                   help="tag the graded delivery plain Rec.709 instead of Rec.709-A, "
                        "and leave H.264/H.265 in .mp4. By default the graded file gets "
                        "a gama 1.96 atom so QuickTime Player on macOS shows the gamma "
                        "correctly; since that atom is QuickTime-only, those codecs are "
                        "delivered in .mov. The ungraded copy is plain Rec.709 either "
                        "way, matching the tag the camera wrote")
    p.add_argument("--trim-end", type=float, default=0, metavar="SECONDS",
                   help="cut this many seconds off the end of every clip that is "
                        "stabilized, e.g. a drone's landing. Gyroflow fits its zoom "
                        "to what is left, so fast movement there no longer crops the "
                        "whole clip in tighter. Replaces any trim in the preset. Clips "
                        "that are only graded are not trimmed")
    p.add_argument("--force-stabilize", action="store_true",
                   help="run Gyroflow for every clip even where no motion data was "
                        "found. Whether to stabilize is otherwise decided per clip: one "
                        "whose telemetry carries no orientation samples faster than its "
                        "frame rate has nothing to stabilize with, and is graded "
                        "directly. Use this if a camera logs its motion somewhere this "
                        "script cannot read, and clips are being graded without being "
                        "stabilized")
    p.add_argument("--merge-only", action="store_true",
                   help="merge split recordings and stop, so the merged files can be "
                        "renamed before stabilizing. Needs no preset, LUT or Gyroflow")
    p.add_argument("--no-merge", dest="merge", action="store_false",
                   help="process the parts of a split recording one by one instead of "
                        "merging them first. By default a recording the camera split "
                        "into several files is joined into one with mp4-merge -- "
                        "losslessly, motion data included -- and its parts are moved "
                        f"into <src-dir>/{PARTS_DIR}")
    p.add_argument("--mp4-merge", type=Path, default=None,
                   help="the mp4_merge tool from github.com/gyroflow/mp4-merge "
                        "(default: on PATH, or any mp4_merge* beside this script)")
    p.add_argument("--mode", default=None, choices=["pipe", "file"],
                   help="'file' writes a real ProRes intermediate (needs ~100 GB/clip). "
                        "Default: pipe, except on Windows, which has no FIFOs and "
                        "always uses file")
    p.add_argument("--overwrite", action="store_true",
                   help="re-render deliveries that already exist. Without it each of the "
                        "two is skipped on its own, and the check is existence only — a "
                        "truncated file from an interrupted run still counts as done")
    p.add_argument("--dry-run", action="store_true",
                   help="print the derived plan and the exact gyroflow and ffmpeg "
                        "commands, then exit without rendering anything")
    p.add_argument("--verbose", action="store_true",
                   help="let Gyroflow's log through to the terminal instead of a file; "
                        "it will tear through its own progress bar")
    args = p.parse_args()

    # Required, but shown as the whole help rather than argparse's one-line usage
    # error: the script no longer sits beside the footage, so where the clips are
    # is the one thing it cannot guess and the first thing to get wrong.
    if args.src_dir is None:
        p.print_help()
        sys.exit(2)

    # Pipe mode is built on a POSIX FIFO. Windows named pipes are another thing
    # entirely -- a server has to create each one, and Gyroflow cannot be aimed
    # at one as its output folder -- so there the render goes through a file.
    if args.mode is None:
        args.mode = "pipe" if hasattr(os, "mkfifo") else "file"
    elif args.mode == "pipe" and not hasattr(os, "mkfifo"):
        die("--mode pipe needs a FIFO, which Windows does not have. Use --mode file "
            "(the default here).")

    # Absolute from here on. Gyroflow is handed the work directory as a file://
    # URL, and "file://.gyroflow-work" is a host name to a URL parser, not a
    # relative path -- Gyroflow writes nowhere, never opens the pipe, and the
    # reader blocks forever on a writer that is not coming. Resolving the
    # command line once is cheaper than auditing every consumer of these paths.
    args.src_dir = args.src_dir.resolve()
    for attr in ("out_dir", "preset", "lut", "gyroflow", "work", "mp4_merge"):
        given = getattr(args, attr)
        if given is not None:
            setattr(args, attr, given.resolve())

    # ---- preflight
    if not args.src_dir.is_dir():
        die(f"source directory not found: {args.src_dir}")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        die("ffmpeg/ffprobe not on PATH")
    if args.merge_only and not args.merge:
        die("--merge-only and --no-merge together leave nothing to do.")

    # ---- file list. -iname-style matching without duplicating on a
    # case-insensitive volume, and without descending into subfolders -- which
    # also keeps the parts of merged recordings, in PARTS_DIR, out of it.
    if args.files:
        files = [f if f.is_absolute() else args.src_dir / f for f in args.files]
    else:
        files = visible_files(args.src_dir, ".mp4")
    if not files:
        die(f"no .mp4 files found in {args.src_dir}")

    # ---- merging needs none of what follows, so --merge-only stops here
    if args.merge_only:
        files, failed = merge_split_recordings(files, args)
        if failed:
            print()
            for name in failed:
                print(f"  failed: {name}")
        sys.exit(1 if failed else 0)

    if args.preset is None:
        args.preset = find_one(args.src_dir, ".gyroflow", "--preset")
    if not args.preset.is_file():
        die(f"preset not found: {args.preset}")
    if args.lut is not None and not args.lut.is_file():
        die(f"LUT not found: {args.lut}")
    if args.lut is None and not args.ungraded:
        die("--no-ungraded without --lut leaves nothing to write: the ungraded copy is "
            "the only delivery when nothing is graded.")
    if args.trim_end < 0:
        die("--trim-end must be zero or more seconds")
    if args.lut and args.ungraded and args.ungraded_suffix == args.suffix:
        die("--suffix and --ungraded-suffix are the same, so the two renders would "
            "land on one file. Change one, or pass --no-ungraded.")

    preset = json.loads(args.preset.read_text(encoding="utf-8"))
    out_block = preset.get("output")
    if not out_block:
        die(f"{args.preset.name} has no 'output' block.\n"
            f"   Re-save the preset from Gyroflow with the \"Export settings\" group\n"
            f"   ticked — this script reads your delivery settings from it.")

    # Only the graded path goes through ffmpeg; without a LUT Gyroflow writes the
    # delivery itself and keeps the tracks as the preset says.
    codec = out_block.get("codec", "H.265/HEVC")
    if container_ext(codec) is None:
        die(f"codec {codec!r} from the preset is not supported by this script.\n"
            f"   Image sequences have no delivery encode to save -- render those "
            f"straight from Gyroflow and apply the LUT afterwards.")

    if out_block.get("preserve_other_tracks") and args.lut:
        warn("'preserve_other_tracks' is set; the DJI metadata tracks cannot survive "
             "the pipe and will be dropped.")

    # ---- split recordings, joined before anything looks at the clips: the
    # merged file is what gets stabilized and named.
    merge_failed = []
    if args.merge:
        files, merge_failed = merge_split_recordings(files, args)
        if not files:
            die("no clip left to process: every split recording failed to merge")

    # ---- output folder. Deliberately NOT the preset's output_folder: that
    # usually points at the footage itself, and deliveries landing beside the
    # sources get picked up as sources by the next run (the default file list is
    # every .mp4 in --src-dir), so each run re-stabilizes its own output.
    if args.out_dir is None:
        args.out_dir = args.src_dir / "Stabilized"

    # ---- which clips Gyroflow actually has work to do on. Decided for the whole
    # batch up front: a run that needs Gyroflow then finds out it is missing now
    # rather than four clips in, and a folder of gimballed footage never asks for
    # it at all. Costs one telemetry read per clip, about a tenth of a second.
    decisions = {src: decide_stabilize(src, args)
                 for src in files if src.is_file()}
    stabilizing = [src for src, (yes, _) in decisions.items() if yes]

    # Only Gyroflow acts on the trims, so a batch that never starts it can ignore
    # them -- which is why this waits for the decisions rather than sitting with
    # the other preset checks above.
    if stabilizing and out_block.get("export_trims_separately"):
        die("'export_trims_separately' splits the render across several files, "
            "which the single-pipe design cannot express. Turn it off in the preset.")
    if stabilizing and not args.gyroflow.exists() and not args.dry_run:
        die(f"Gyroflow CLI not found at: {args.gyroflow}\n"
            f"   Pass --gyroflow {GYROFLOW_EXAMPLE}\n"
            f"   ({len(stabilizing)} of {len(decisions)} clips carry motion data)")

    # Beside the footage by default: that drive is usually the roomy one, and it
    # keeps the pipe — and any --mode file intermediate — off the system disk.
    default_work = args.work is None
    if default_work:
        args.work = args.src_dir / ".gyroflow-work"
    args.work.mkdir(parents=True, exist_ok=True)
    # Gyroflow's -p parser rewrites every ' into " before parsing the JSON. It
    # never sees the path when no clip is being stabilized, where only the log
    # lands there.
    if stabilizing and ("'" in str(args.work) or '"' in str(args.work)):
        die("the work directory path must not contain quotes or apostrophes")
    if not args.dry_run:
        # Without a LUT nothing goes through the pipe
        if args.mode == "pipe" and stabilizing and args.lut:
            check_fifo_support(args.work)
        args.out_dir.mkdir(parents=True, exist_ok=True)

    if stabilizing and args.trim_end and any(k in preset for k in TRIM_KEYS):
        warn(f"{args.preset.name} has a trim of its own; --trim-end replaces it")
    inter_preset = (make_intermediate_preset(preset, args.work, args.trim_end)
                    if stabilizing else None)

    # ---- show the derived plan; this is the bit worth eyeballing. Everything
    # downstream of "stabilize" is decided per clip, so this shows the first one.
    fps = probe(files[0]).fps
    stabilize, rate = decisions.get(files[0], (True, None))
    info(f"preset:       {args.preset.name}")
    grade = args.lut is not None
    info(f"lut:          " + (args.lut.name if grade else "none — not grading (pass --lut to grade)"))
    if args.force_stabilize:
        motion = "on for every clip (--force-stabilize)"
    elif rate is None:
        motion = "on — no DJI telemetry to read, so nothing is skipped"
    elif stabilize:
        motion = f"on — {rate:.0f} orientation samples/s at {fps:.2f} fps"
    else:
        motion = (f"off — {rate:.0f} samples/s at {fps:.2f} fps is one per frame, "
                  f"so there is nothing to stabilize with")
    info(f"stabilize:    {motion}"
         + ("  (re-checked per clip)"
            if len(files) > 1 and not args.force_stabilize else ""))
    # Resizing to the preset's output size is Gyroflow's to do, so with Gyroflow
    # out of the picture the frames arrive at whatever size the camera wrote.
    size = (f"{out_block.get('output_width')}x{out_block.get('output_height')}"
            if stabilize else "source size")
    direct = stabilize and not grade
    info(f"final encode: {out_block.get('codec')} "
         f"{out_block.get('codec_options') or ''} @ {out_block.get('bitrate', 0):.2f} Mbps, "
         f"{size}, gpu={out_block.get('use_gpu')}")
    if direct:
        info(f"  -> by Gyroflow itself, from the preset's export settings")
    else:
        info(f"  -> ffmpeg:  {shlex.join(build_video_args(out_block, fps))}")
    info(f"  -> color:   " + ("Rec.709, ungraded" if not grade else
                              "Rec.709 (--rec709)" if not args.rec709a else
                              "graded Rec.709-A (gama 1.96)"
                              + (", ungraded Rec.709" if args.ungraded and stabilize else "")))
    if args.trim_end:
        info(f"trim:         last {args.trim_end:g} s of every stabilized clip")
    info(f"intermediate: " + ("none — no LUT, so Gyroflow writes the delivery itself" if direct
                              else f"ProRes {args.prores} through a {args.mode}" if stabilize
                              else "none — the LUT only, Gyroflow not run" if grade
                              else "none — nothing to stabilize or grade, clip skipped"))
    # Names for the first clip, which is what the rest are patterned on.
    ext = container_ext(codec, args.rec709a)
    info(f"output:       {args.out_dir}"
         + (f"  (first of {len(files)} clips shown)" if len(files) > 1 else ""))
    names = {kind: path.name for kind, path in deliveries(files[0], stabilize, args, ext)}
    info(f"  graded:     {names.get('graded', 'off (no --lut)')}")
    info(f"  ungraded:   " + names.get("plain", "off (not stabilized)" if not stabilize
                                               else "off (--no-ungraded)"))

    if not args.dry_run and prevent_sleep():
        info("idle sleep held off for the duration of this run")

    counts = {"ok": 0, "failed": len(merge_failed), "skipped": 0}
    failed = [f"{name} (merge)" for name in merge_failed]
    try:
        for src in files:
            if not src.is_file():
                warn(f"not found, skipping: {src}")
                counts["skipped"] += 1
                continue
            print()
            info(src.name)
            result = run_one(src, args, out_block, inter_preset, args.work,
                             decisions[src])
            counts[result] += 1
            if result == "failed":
                failed.append(src.name)
    except KeyboardInterrupt:
        print()
        warn("aborted by user")
    finally:
        if inter_preset:
            inter_preset.unlink(missing_ok=True)
        if default_work:
            try:
                args.work.rmdir()   # only succeeds when empty, so it also tidies
            except OSError:         # up after an earlier run that was interrupted
                pass

    print()
    info(f"finished — {counts['ok']} ok, {counts['failed']} failed, {counts['skipped']} skipped")
    for name in failed:
        print(f"  failed: {name}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
