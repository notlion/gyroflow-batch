#!/usr/bin/env python3
"""
Stabilize (Gyroflow) + grade (D-Log M -> Rec.709 LUT) with ONE lossy encode.

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

--src-dir is required and holds the clips, the .gyroflow preset and the .cube
LUT. Deliveries land in <src-dir>/Stabilized.

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
    ./stabilize_and_grade.py --src-dir DIR                    # every .mp4 there
    ./stabilize_and_grade.py --src-dir DIR "ESK8 Heat 1.MP4"  # just these
    ./stabilize_and_grade.py --src-dir DIR --dry-run          # the derived plan
    ./stabilize_and_grade.py --src-dir DIR --no-ungraded      # graded only
    ./stabilize_and_grade.py --src-dir DIR --force-stabilize  # stabilize regardless
    ./stabilize_and_grade.py --src-dir DIR --mode file        # on disk instead
"""

import argparse
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


# --------------------------------------------------------------- pretty output

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
    if codec in ("ProRes", "DNxHD", "CineForm"):
        return ".mov"
    if codec in ("EXR Sequence", "PNG Sequence"):
        return None
    # H.264/H.265 would default to .mp4, but the gama atom that makes a file
    # Rec.709-A is a QuickTime box: ffmpeg refuses to write it into .mp4
    # ("Not writing 'gama' atom. Format is not MOV."). Delivering them in .mov
    # instead is what Resolve does for a QuickTime H.265 export -- same encode,
    # same bitrate, and the hvc1/avc1 tag is already set either way.
    return ".mov" if rec709a else ".mp4"


def build_video_args(out, fps):
    """Translate a Gyroflow `output` block into the final ffmpeg encoder args."""
    codec = out.get("codec", "H.265/HEVC")
    variant = str(out.get("codec_options", "")).strip().lower()
    gpu = bool(out.get("use_gpu", True))
    args = []

    if codec == "H.265/HEVC":
        if gpu:
            args += ["-c:v", "hevc_videotoolbox", "-profile:v", "main10", "-allow_sw", "1"]
        else:
            args += ["-c:v", "libx265", "-preset", "medium"]
        args += ["-pix_fmt", "yuv420p10le", "-tag:v", "hvc1"]

    elif codec == "H.264/AVC":
        # H.264 is 8-bit here; Gyroflow itself steers 10-bit sources to HEVC.
        args += ["-c:v", "h264_videotoolbox" if gpu else "libx264"]
        if not gpu:
            args += ["-preset", "medium"]
        args += ["-pix_fmt", "yuv420p"]

    elif codec == "ProRes":
        prof = PRORES_PROFILE.get(variant, "3")
        args += ["-c:v", "prores_ks", "-profile:v", prof,
                 "-pix_fmt", PRORES_PIXFMT[prof]]

    elif codec == "DNxHD":
        prof, pix = DNXHR_PROFILE.get(variant, ("dnxhr_hq", "yuv422p"))
        args += ["-c:v", "dnxhd", "-profile:v", prof, "-pix_fmt", pix]

    elif codec == "CineForm":
        args += ["-c:v", "cfhd", "-pix_fmt", "yuv422p10le"]

    else:
        die(f"codec {codec!r} from the preset is not supported by this script.\n"
            f"   Image sequences have no delivery encode to save -- render those "
            f"straight from Gyroflow and apply the LUT afterwards.")

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

def probe(path):
    """Return (fps, has_audio, seconds) for a source file."""
    try:
        raw = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,r_frame_rate:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, check=True).stdout
        probed = json.loads(raw)
        streams = probed.get("streams", [])
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None, False, None

    try:
        seconds = float(probed.get("format", {}).get("duration"))
    except (TypeError, ValueError):        # missing, or the literal "N/A"
        seconds = None

    fps = None
    has_audio = False
    for s in streams:
        if s.get("codec_type") == "audio":
            has_audio = True
        elif s.get("codec_type") == "video" and fps is None:
            r = s.get("r_frame_rate", "0/0")
            try:
                num, den = r.split("/")
                if float(den):
                    fps = float(num) / float(den)
            except ValueError:
                pass
    return fps, has_audio, seconds


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
    try:
        raw = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=index,codec_tag_string",
             "-of", "json", str(src)],
            capture_output=True, text=True, check=True).stdout
        streams = json.loads(raw).get("streams", [])
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None
    index = next((s["index"] for s in streams
                  if s.get("codec_tag_string") == "djmd"), None)
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


def decide_stabilize(src, args, fps):
    """(run Gyroflow?, the sample rate that decided it) for one clip."""
    if args.force_stabilize:
        return True, None
    rate = orientation_rate(src)
    if rate is None or not fps:
        return True, rate
    return rate > MOTION_RATE_FACTOR * fps, rate


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


def run_gyroflow(cmd, log_path, verbose):
    if verbose:
        return subprocess.run(cmd).returncode

    tty = sys.stdout.isatty()
    drew = False
    with log_path.open("w", errors="replace") as log:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log,
                                text=True, errors="replace")
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line or NOISE_RE.search(line):
                    continue

                m = RENDER_RE.search(line)
                if m:
                    cur, total, pct, eta = m.groups()
                    if tty:
                        draw_progress("render", float(pct),
                                      f"{cur}/{total} frames  ETA {fmt_duration(float(eta))}")
                        drew = True
                    continue

                m = SYNC_RE.search(line)
                if m:
                    if tty:
                        draw_progress("sync  ", float(m.group(1)))
                        drew = True
                    continue

                log.write(line + "\n")
        finally:
            proc.stdout.close()
            rc = proc.wait()
            if drew:
                clear_progress()
    return rc


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
    drew = False
    with log_path.open("w", errors="replace") as log:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log,
                                text=True, errors="replace")
        try:
            for line in proc.stdout:
                m = FF_TIME_RE.match(line)
                if not m:
                    continue
                hours, mins, secs = m.groups()
                done = int(hours) * 3600 + int(mins) * 60 + float(secs)
                detail = f"{fmt_duration(done)}/{fmt_duration(seconds)}"
                if done > 0:
                    eta = (seconds - done) * (time.monotonic() - started) / done
                    detail += f"  ETA {fmt_duration(max(0, eta))}"
                draw_progress("grade ", min(100.0, done / seconds * 100), detail)
                drew = True
        except BaseException:
            # subprocess.run, which this replaced, kills its child on Ctrl-C. Keep
            # that: an encode left running would rewrite the output we delete.
            proc.kill()
            raise
        finally:
            proc.stdout.close()
            rc = proc.wait()
            if drew:
                clear_progress()
    return rc


def dump_log(path, label, limit=40):
    if path.exists() and path.stat().st_size:
        lines = path.read_text(errors="replace").splitlines()
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
    if rc == 101:
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
    """
    if not shutil.which("caffeinate"):
        warn("caffeinate not on PATH; the machine may sleep mid-render")
        return None
    try:
        return subprocess.Popen(["caffeinate", "-i", "-m", "-s", "-w", str(os.getpid())],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        warn(f"could not start caffeinate ({e}); the machine may sleep mid-render")
        return None


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

def make_intermediate_preset(preset, work):
    """Same preset with the output block dropped -- the ProRes settings are
    passed via -p instead, which keeps Gyroflow's apply_to_all path (and its
    filename regeneration) out of the picture entirely."""
    derived = {k: v for k, v in preset.items() if k != "output"}
    derived["name"] = f"{preset.get('name', 'preset')} (intermediate)"
    path = work / f"intermediate-{RUN_ID}.gyroflow"
    path.write_text(json.dumps(derived, indent=4, sort_keys=True))
    return path


def make_out_params(out, work, filename, prores_variant):
    """Gyroflow's -p JSON: ProRes into the pipe, at the preset's output size."""
    params = {
        "output_folder": f"file://{work}",
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


# ------------------------------------------------------------------- runner

def run_one(src, args, out_block, inter_preset, work, decision):
    stem = src.stem
    codec = out_block.get("codec", "H.265/HEVC")
    ext = container_ext(codec, args.rec709a)
    if ext is None:
        die(f"codec {codec!r} writes an image sequence, which has no delivery "
            f"encode to save. Render those straight from Gyroflow instead.")
    fps, has_audio, seconds = probe(src)
    stabilize, rate = decision
    if not stabilize:
        info(f"no motion data to stabilize with — {rate:.0f} orientation samples/s "
             f"at {fps:.2f} fps is one per frame; grading only")

    # A clip that is not stabilized must not be named as though it were, and its
    # ungraded copy would be a plain transcode of the source.
    targets = [("graded", args.out_dir /
                f"{stem}{args.suffix if stabilize else args.unstabilized_suffix}{ext}")]
    if args.ungraded and stabilize:
        targets.append(("plain", args.out_dir / f"{stem}{args.ungraded_suffix}{ext}"))

    # Each output is skipped on its own, so a run that only lost one of the two
    # re-renders just that one.
    if not args.overwrite:
        for target in list(targets):
            if target[1].exists():
                info(f"exists, skipping: {target[1].name}")
                targets.remove(target)
    if not targets:
        return "skipped"

    want_audio = has_audio and bool(out_block.get("audio", True))
    comment = (out_block.get("metadata") or {}).get("comment", "")

    ffmpeg_cmd = (
        ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats", "-y"]
        + ["-i", None]                      # filled in below
        + ["-filter_complex", build_filter_complex(args.lut, [k for k, _ in targets])]
    )
    for kind, path in targets:
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
    gf_filename = f"{stem}.{RUN_ID}." + ("mov" if args.mode == "file" else "mkv")
    gf_final = work / gf_filename
    gf_tmp = work / f"{gf_filename}.tmp"

    # File mode hands ffmpeg the finished render; pipe mode hands it the FIFO,
    # which lives at the .tmp name because that is what Gyroflow opens. A clip
    # with no motion data has no render at all, so it reads the clip itself.
    ff_input = src if not stabilize else gf_final if args.mode == "file" else gf_tmp
    ffmpeg_cmd[ffmpeg_cmd.index(None)] = str(ff_input)

    out_params = make_out_params(out_block, work, gf_filename, args.prores)
    # --stdout-progress gives us parseable progress lines. Its own bar would fight
    # with them on the same stream, but we consume stdout and discard the bar.
    gf_cmd = [str(args.gyroflow), str(src), "--preset", str(inter_preset),
              "-f", "--stdout-progress", "-p", out_params]

    if args.dry_run:
        if stabilize:
            print(f"  gyroflow: {shlex.join(gf_cmd)}")
        print(f"  ffmpeg:   {shlex.join(ffmpeg_cmd)}")
        return "skipped"

    for leftover in (gf_tmp, gf_final):
        leftover.unlink(missing_ok=True)

    fflog = work / f"{stem}.{RUN_ID}.ffmpeg.log"
    gflog = work / f"{stem}.{RUN_ID}.gyroflow.log"
    ff = None
    started = time.monotonic()
    try:
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
                _, _, seconds = probe(ff_input)
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
    p = argparse.ArgumentParser(
        description="Gyroflow stabilization + LUT grade in a single delivery encode.",
        # Shown whenever --src-dir is missing, which includes running with no
        # arguments at all -- so this doubles as the getting-started text.
        epilog='examples:\n'
               '  %(prog)s --src-dir "/Volumes/Footage/Race Day" --dry-run\n'
               '      show the derived plan without touching anything\n'
               '\n'
               '  %(prog)s --src-dir "/Volumes/Footage/Race Day"\n'
               '      stabilize and grade every .mp4 there, writing both\n'
               '      deliveries into "/Volumes/Footage/Race Day/Stabilized"\n',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="*", type=Path,
                   help="specific clips (default: every .mp4 in --src-dir)")
    p.add_argument("--src-dir", type=Path, default=None,
                   help="REQUIRED: folder holding the clips, the .gyroflow preset "
                        "and the .cube LUT")
    p.add_argument("--out-dir", type=Path, default=None,
                   help="default: <src-dir>/Stabilized")
    p.add_argument("--preset", type=Path, default=None,
                   help="default: the only .gyroflow file in --src-dir")
    p.add_argument("--lut", type=Path, default=None,
                   help="default: the only .cube file in --src-dir")
    p.add_argument("--gyroflow", type=Path,
                   default=Path("/Applications/Gyroflow.app/Contents/MacOS/gyroflow"),
                   help="the Gyroflow CLI binary inside the app bundle "
                        "(default: %(default)s)")
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
    p.add_argument("--force-stabilize", action="store_true",
                   help="run Gyroflow for every clip even where no motion data was "
                        "found. Whether to stabilize is otherwise decided per clip: one "
                        "whose telemetry carries no orientation samples faster than its "
                        "frame rate has nothing to stabilize with, and is graded "
                        "directly. Use this if a camera logs its motion somewhere this "
                        "script cannot read, and clips are being graded without being "
                        "stabilized")
    p.add_argument("--mode", default="pipe", choices=["pipe", "file"],
                   help="'file' writes a real ProRes intermediate (needs ~100 GB/clip)")
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

    # Absolute from here on. Gyroflow is handed the work directory as a file://
    # URL, and "file://.gyroflow-work" is a host name to a URL parser, not a
    # relative path -- Gyroflow writes nowhere, never opens the pipe, and the
    # reader blocks forever on a writer that is not coming. Resolving the
    # command line once is cheaper than auditing every consumer of these paths.
    args.src_dir = args.src_dir.resolve()
    for attr in ("out_dir", "preset", "lut", "gyroflow", "work"):
        given = getattr(args, attr)
        if given is not None:
            setattr(args, attr, given.resolve())

    # ---- preflight
    if not args.src_dir.is_dir():
        die(f"source directory not found: {args.src_dir}")
    if args.preset is None:
        args.preset = find_one(args.src_dir, ".gyroflow", "--preset")
    if args.lut is None:
        args.lut = find_one(args.src_dir, ".cube", "--lut")
    if not args.preset.is_file():
        die(f"preset not found: {args.preset}")
    if not args.lut.is_file():
        die(f"LUT not found: {args.lut}")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        die("ffmpeg/ffprobe not on PATH")
    if args.ungraded and args.ungraded_suffix == args.suffix:
        die("--suffix and --ungraded-suffix are the same, so the two renders would "
            "land on one file. Change one, or pass --no-ungraded.")

    preset = json.loads(args.preset.read_text())
    out_block = preset.get("output")
    if not out_block:
        die(f"{args.preset.name} has no 'output' block.\n"
            f"   Re-save the preset from Gyroflow with the \"Export settings\" group\n"
            f"   ticked — this script reads your delivery settings from it.")

    if out_block.get("preserve_other_tracks"):
        warn("'preserve_other_tracks' is set; the DJI metadata tracks cannot survive "
             "the pipe and will be dropped.")

    # ---- output folder. Deliberately NOT the preset's output_folder: that
    # usually points at the footage itself, and deliveries landing beside the
    # sources get picked up as sources by the next run (the default file list is
    # every .mp4 in --src-dir), so each run re-stabilizes its own output.
    if args.out_dir is None:
        args.out_dir = args.src_dir / "Stabilized"

    # ---- file list. -iname-style matching without duplicating on a
    # case-insensitive volume, and without descending into subfolders.
    if args.files:
        files = [f if f.is_absolute() else args.src_dir / f for f in args.files]
    else:
        files = visible_files(args.src_dir, ".mp4")
    if not files:
        die(f"no .mp4 files found in {args.src_dir}")

    # ---- which clips Gyroflow actually has work to do on. Decided for the whole
    # batch up front: a run that needs Gyroflow then finds out it is missing now
    # rather than four clips in, and a folder of gimballed footage never asks for
    # it at all. Costs one telemetry read per clip, about a tenth of a second.
    decisions = {src: decide_stabilize(src, args, probe(src)[0])
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
            f"   Pass --gyroflow /path/to/Gyroflow.app/Contents/MacOS/gyroflow\n"
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
        if args.mode == "pipe" and stabilizing:
            check_fifo_support(args.work)
        args.out_dir.mkdir(parents=True, exist_ok=True)

    inter_preset = make_intermediate_preset(preset, args.work) if stabilizing else None

    # ---- show the derived plan; this is the bit worth eyeballing. Everything
    # downstream of "stabilize" is decided per clip, so this shows the first one.
    fps, _, _ = probe(files[0])
    stabilize, rate = decisions.get(files[0], (True, None))
    info(f"preset:       {args.preset.name}")
    info(f"lut:          {args.lut.name}")
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
    info(f"final encode: {out_block.get('codec')} "
         f"{out_block.get('codec_options') or ''} @ {out_block.get('bitrate', 0):.2f} Mbps, "
         f"{size}, gpu={out_block.get('use_gpu')}")
    info(f"  -> ffmpeg:  {shlex.join(build_video_args(out_block, fps))}")
    info(f"  -> color:   " + ("Rec.709 (--rec709)" if not args.rec709a else
                              "graded Rec.709-A (gama 1.96)"
                              + (", ungraded Rec.709" if args.ungraded and stabilize else "")))
    info(f"intermediate: " + (f"ProRes {args.prores} through a {args.mode}" if stabilize
                              else "none — the LUT only, Gyroflow not run"))
    # Names for the first clip, which is what the rest are patterned on.
    ext = container_ext(out_block.get("codec", "H.265/HEVC"), args.rec709a) or ""
    info(f"output:       {args.out_dir}"
         + (f"  (first of {len(files)} clips shown)" if len(files) > 1 else ""))
    info(f"  graded:     {files[0].stem}"
         f"{args.suffix if stabilize else args.unstabilized_suffix}{ext}")
    info(f"  ungraded:   " + (f"{files[0].stem}{args.ungraded_suffix}{ext}"
                              if args.ungraded and stabilize
                              else "off (not stabilized)" if not stabilize
                              else "off (--no-ungraded)"))

    if not args.dry_run and prevent_sleep():
        info("idle sleep held off for the duration of this run")

    counts = {"ok": 0, "failed": 0, "skipped": 0}
    failed = []
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
