# Gyroflow Batch

Gyroflow Batch stabilizes DJI footage with Gyroflow and applies a D-Log M →
Rec.709 LUT in a **single lossy encode**.

You configure everything once in Gyroflow's GUI and save a normal preset.
Gyroflow Batch splits that preset in two: your delivery settings (codec,
bitrate, size, audio, keyframe distance) drive the final ffmpeg encode, while a
derived copy sends Gyroflow's render into a named pipe as ProRes. One ffmpeg
reads that pipe once and writes two deliveries — the graded one through the 3D
LUT, and a stabilized-but-ungraded copy of the log original.

The source is decoded once, Gyroflow renders once, nothing is encoded twice, and
no multi-hundred-gigabyte intermediate file ever touches the disk.

Recordings the camera split into several files are joined back into one first,
losslessly and with their motion data, using Gyroflow's own merge tool. You can
also merge on its own and rename the results before stabilizing. See
[Split recordings are merged first](#split-recordings-are-merged-first).

Clips that have nothing to stabilize — gimballed drone footage, say — are
detected and sent straight to the grade, skipping the Gyroflow half entirely.
See [Clips with nothing to stabilize](#clips-with-nothing-to-stabilize-are-graded-directly).

---

## Requirements

| Component | Requirement |
|---|---|
| OS | macOS (Apple silicon or Intel), or Windows 10/11 |
| Python | 3.8 or newer — standard library only, nothing to `pip install` |
| ffmpeg / ffprobe | any recent build with `lut3d` |
| Gyroflow | on macOS, **the direct download, not the Mac App Store build** (see below) |
| mp4_merge | only for recordings split into several files — Gyroflow's merge tool (see below) |

On Windows Gyroflow Batch works with one difference: it renders through a
temporary file rather than a pipe, so it needs about 100 GB of free space per
clip on the work drive. See [Running on Windows](#running-on-windows).

---

## Setup on a stock Mac

### 1. Python

macOS does not ship a usable `python3` until the Command Line Tools are present.
Check:

```sh
python3 --version
```

If that errors or offers to install developer tools, run:

```sh
xcode-select --install
```

Any Python 3.8+ works. Gyroflow Batch imports only the standard library, so
there is no virtualenv and no dependency list.

### 2. Homebrew

Skip if you already have it (`brew --version`).

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Homebrew prints a **Next steps** section at the end telling you to add itself to
your `PATH`. Actually run those commands — on Apple silicon it installs to
`/opt/homebrew`, which is not on the default `PATH`:

```sh
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```

### 3. ffmpeg

```sh
brew install ffmpeg
```

This installs `ffprobe` too. Both must be on your `PATH`; Gyroflow Batch refuses
to start otherwise. Confirm the two pieces it depends on are present:

```sh
ffmpeg -hide_banner -filters  | grep lut3d
ffmpeg -hide_banner -encoders | grep videotoolbox
```

### 4. Gyroflow — download the .dmg

Get the macOS universal `.dmg` from <https://gyroflow.xyz> and drag `Gyroflow.app`
into `/Applications`.

> **Do not install Gyroflow from the Mac App Store.**
> The App Store build is sandboxed, and the sandbox blocks its command-line
> interface from reading anything outside its own container — including Gyroflow
> Batch's work directory. It fails with exit code 101 and a `PermissionDenied`
> panic, which no amount of configuration will fix. Only the direct download
> works.

If Gatekeeper blocks the first launch, right-click the app and choose **Open**,
or clear the quarantine flag:

```sh
xattr -dr com.apple.quarantine /Applications/Gyroflow.app
```

Confirm the CLI binary is where Gyroflow Batch expects it:

```sh
ls /Applications/Gyroflow.app/Contents/MacOS/gyroflow
```

Installed somewhere else? Pass `--gyroflow /path/to/Gyroflow.app/Contents/MacOS/gyroflow`.

### 5. mp4_merge

Only needed if your camera splits long recordings into several files. From
<https://github.com/gyroflow/mp4-merge/releases> download `mp4_merge-mac-arm64`
(Apple silicon) or `mp4_merge-mac64` (Intel) and put it next to
`gyroflow-batch.py`. It is found there under its downloaded name. Then let it
run:

```sh
chmod +x mp4_merge-mac-*
xattr -d com.apple.quarantine mp4_merge-mac-*
```

### 6. Gyroflow Batch

```sh
chmod +x gyroflow-batch.py
```

---

## Setup on Windows

### 1. Python

Install Python 3 from <https://www.python.org/downloads/windows/>, or run
`winget install Python.Python.3.12`. Run Gyroflow Batch with the `py` launcher
instead of `./`:

```powershell
py gyroflow-batch.py --src-dir "D:\Footage\Race Day" --dry-run
```

### 2. ffmpeg

```powershell
winget install Gyan.FFmpeg
```

Open a new terminal afterwards so `ffmpeg` and `ffprobe` are on your `PATH`.
That build includes the NVIDIA, Intel and AMD hardware encoders; Gyroflow Batch
picks whichever one your GPU actually supports — see
[GPU encoding](#gpu-encoding).

### 3. Gyroflow

Download the Windows build from <https://gyroflow.xyz> and unzip it to
`C:\Program Files\Gyroflow`, which is where Gyroflow Batch looks for
`gyroflow.exe` by default (after checking `PATH`). Anywhere else works too —
pass `--gyroflow "C:\path\to\Gyroflow\gyroflow.exe"`.

### 4. mp4_merge

Only needed if your camera splits long recordings into several files. From
<https://github.com/gyroflow/mp4-merge/releases> download
`mp4_merge-windows64.exe` and put it next to `gyroflow-batch.py`.

---

## Preparing a job

Put three things in your footage folder:

1. **The clips** — every `.mp4`/`.MP4` in the folder is processed by default.
2. **One `.gyroflow` preset** — found automatically if it is the only one there.
3. **One `.cube` LUT** — likewise. For DJI O4 that is the *D-Log M to Rec.709*
   cube from DJI's site.

### Saving a usable preset

In the Gyroflow GUI, load one representative clip and set up lens profile,
synchronization, and stabilization as you want them. Then open **Export
settings** and choose your delivery codec, bitrate, resolution, and audio.

When you save the preset, **tick the "Export settings" group**. Gyroflow Batch
reads your delivery settings out of the preset's `output` block and stops with
an error if that block is missing.

Two settings to check:

- **Export trims separately — off.** It splits a render across several files,
  which the single-pipe design cannot express.
- **Preserve other tracks — off.** DJI's metadata tracks cannot survive the pipe
  and are dropped regardless; leaving it on only produces a warning.

The preset's *output folder* is deliberately ignored — see
[Where the files land](#where-the-files-land).

---

## Running it

`--src-dir` is **required** — it is the folder holding the clips, the preset and
the LUT. Gyroflow Batch lives in this repo rather than beside your footage, so
there is nothing sensible for it to default to; run it without one and you get
this help text back.

```sh
./gyroflow-batch.py --src-dir "/Volumes/Footage/Race Day"
```

Always start with a dry run — it prints the derived plan and the exact commands
without touching anything:

```sh
./gyroflow-batch.py --src-dir "/Volumes/Footage/Race Day" --dry-run
```

```
==> preset:       stabilization-settings.gyroflow
==> lut:          DJI O4 Air Unit Series D-Log M to Rec.709 V1.cube
==> stabilize:    on — 2002 orientation samples/s at 59.94 fps  (re-checked per clip)
==> final encode: H.265/HEVC  @ 124.00 Mbps, 3840x2160, gpu=True
==>   -> ffmpeg:  -c:v hevc_videotoolbox -profile:v main10 -allow_sw 1 …
==>   -> color:   graded Rec.709-A (gama 1.96), ungraded Rec.709
==> intermediate: ProRes 4444 through a pipe
==> output:       /Volumes/Footage/Race Day/Stabilized
==>   graded:     Heat 1 Stabilized Graded.mov
==>   ungraded:   Heat 1 Stabilized.mov
```

More examples:

```sh
D="/Volumes/Footage/Race Day"

# just two clips (names are relative to --src-dir)
./gyroflow-batch.py --src-dir "$D" "Heat 1.MP4" "Heat 2.MP4"

# graded delivery only, no ungraded copy
./gyroflow-batch.py --src-dir "$D" --no-ungraded

# redo something that already exists
./gyroflow-batch.py --src-dir "$D" --overwrite "Heat 1.MP4"

# stabilize everything, even clips detected as having no motion data
./gyroflow-batch.py --src-dir "$D" --force-stabilize

# only merge split recordings, so they can be renamed before stabilizing
./gyroflow-batch.py --src-dir "$D" --merge-only

# no FIFO support on the volume? write a real intermediate instead
./gyroflow-batch.py --src-dir "$D" --mode file
```

### Options

| Flag | Meaning |
|---|---|
| `--src-dir DIR` | **required** — where the clips, preset and LUT are |
| `--out-dir DIR` | where deliveries go (default: `<src-dir>/Stabilized`) |
| `--preset FILE` | default: the only `.gyroflow` in `--src-dir` |
| `--lut FILE` | default: the only `.cube` in `--src-dir` |
| `--gyroflow PATH` | path to the Gyroflow CLI binary (`gyroflow.exe` on Windows) |
| `--work DIR` | scratch for the pipe or intermediate (default: `<src-dir>/.gyroflow-work`) |
| `--prores VARIANT` | intermediate quality through the pipe (default: `4444`) |
| `--suffix STR` | name tag for the graded file (default: `" Stabilized Graded"`) |
| `--ungraded-suffix STR` | name tag for the ungraded file (default: `" Stabilized"`) |
| `--unstabilized-suffix STR` | name tag for a clip graded without stabilization (default: `" Graded"`) |
| `--no-ungraded` | skip the ungraded copy |
| `--rec709` | tag the graded file plain Rec.709 instead of Rec.709-A, and keep H.264/H.265 in `.mp4` |
| `--merge-only` | merge split recordings and stop; needs no preset, LUT or Gyroflow |
| `--no-merge` | process the parts of a split recording one by one |
| `--mp4-merge PATH` | path to the mp4_merge tool (default: on `PATH`, or beside `gyroflow-batch.py`) |
| `--force-stabilize` | stabilize every clip, even where no motion data was found |
| `--mode file` | write a real ProRes intermediate instead of piping (~100 GB/clip); always on for Windows |
| `--overwrite` | re-render files that already exist |
| `--dry-run` | print the plan and exit |
| `--verbose` | let Gyroflow's own log through to the terminal |

---

## Things worth knowing

### Running on Windows

Everything above applies, with these differences:

- **The intermediate goes through a file.** The pipe is a POSIX FIFO, which
  Windows does not have, so `--mode file` is the default there and `--mode pipe`
  is refused. It is still a single lossy encode; the cost is ~100 GB of scratch
  per clip on the work drive, deleted as soon as the clip is done.
- **The GPU encoder is detected**, not assumed — see below.
- **Deliveries are still Rec.709-A `.mov` files**, so they look right when they
  reach a Mac. Windows players ignore the `gama` atom, so it costs nothing there.
- **Sleep is held off** through Windows' own power request, in place of
  `caffeinate`.

### GPU encoding

With *Use GPU* ticked in the preset, H.264/H.265 go to a hardware encoder:

| Platform | Encoder |
|---|---|
| macOS | VideoToolbox |
| Windows | the first that works of NVIDIA NVENC, Intel Quick Sync, AMD AMF |

Every ffmpeg build for Windows lists all three, whatever GPU is fitted, so
Gyroflow Batch test-encodes a fraction of a second with each, in that order,
using the exact settings of the real encode. That also catches a GPU that has
the encoder but cannot do 10-bit HEVC, as NVIDIA cards older than the GTX 10
series cannot. The chosen encoder shows on the `-> ffmpeg:` line of the plan.

If none work, Gyroflow Batch warns and encodes in software (`libx265`/`libx264`),
which gives the same file but is much slower. Unticking *Use GPU* in the preset
goes straight to software.

### Where the files land

Deliveries go to `<src-dir>/Stabilized` unless you pass `--out-dir`.

The preset's own output folder is **not** used, on purpose. It usually points at
the footage itself, and deliveries sitting beside the sources get picked up as
sources by the next run — so each run ends up re-stabilizing its own output,
producing files like `Heat 1 Stabilized Graded Stabilized Graded.mp4` and growing
the work list every time. Keeping deliveries in a subfolder avoids that entirely.

### Split recordings are merged first

Cameras split a long recording into files of just under 4 GB, the FAT32 limit
of the SD card. Stabilized one by one, each part would get its own delivery and
the smoothing would restart at every join. So Gyroflow Batch joins each such
recording back into one file before anything else.

The merge is done by **mp4_merge**, the tool from the Gyroflow project that
Gyroflow's own "join the files" prompt uses. Gyroflow's command line cannot
merge, so Gyroflow Batch runs that tool directly. It copies the data without
re-encoding and keeps every track, the motion data included, so the joined
file stabilizes as one clip.

**How parts are recognised.** Nothing in the files links the parts of a
recording, so Gyroflow Batch uses the same rule as mp4_merge itself: a file
continues the one before it when its creation time equals that file's, or
equals that file's plus its duration to within 2 s. It also requires the same
picture size, frame rate and rotation, which Gyroflow checks before merging
too. Separate recordings are further apart than that: the camera has to close
one file and start the next.

```
==> split recording, 3 parts: DJI_20261024203002_0044_D.MP4 … DJI_20261024203712_0046_D.MP4
==>   merged as:  DJI_20261024203002_0044_D_joined.MP4  (parts moved to Split parts/)
```

**What happens to the files.** The merged file goes beside the parts, named
after the first one with `_joined` (as Gyroflow names it). The parts are
**moved, not deleted**, into a `Split parts` subfolder. That way the folder
holds each recording once and the next run does not merge or stabilize it
again. Delete `Split parts` yourself once you are happy with the merged files.
The merge needs as much free space again as the parts take up.

**Merging on its own.** To name recordings before stabilizing them:

```sh
./gyroflow-batch.py --src-dir "$D" --merge-only    # merge, then stop
# rename DJI_…_joined.MP4 to "Heat 1.MP4", and so on
./gyroflow-batch.py --src-dir "$D"                 # stabilize and grade
```

`--merge-only` needs no preset, LUT or Gyroflow, only ffmpeg and mp4_merge.
Deliveries are named after the clip, so renaming first gives you
`Heat 1 Stabilized Graded.mov` rather than a name built from the first part.

A merge is written under a hidden temporary name and gets its real name only
once it has finished. An interrupted or failed merge therefore leaves no
half-written file behind, and its parts stay where they were. `--no-merge` turns
merging off and processes every file on its own.

### Clips with nothing to stabilize are graded directly

Not every clip needs Gyroflow. Footage from a mechanically gimballed drone is
already steady, and running it through a stabilization pass costs a full extra
render to change nothing. Gyroflow Batch detects this **per clip** and skips the
Gyroflow half when there is nothing to work with.

**How it decides.** DJI writes telemetry into a `djmd` data track — protobuf,
with a different schema per camera (`dvtm_O4P.proto`, `dvtm_Mini5Pro.proto`, …).
Rather than model those, Gyroflow Batch reads three seconds of that track and
counts one thing: unit quaternions. What matters is not whether orientation data
exists but **how fast it arrives**:

| Camera | Orientation samples | vs 59.94 fps | Verdict |
|---|---|---|---|
| O4 Pro air unit | 2002–2969 /s | ~33× | real IMU → stabilize |
| Mini 5 Pro (gimballed) | 60 /s | 1.0× | one per frame → grade only |

Sub-frame sampling is motion data — it is what rolling-shutter correction and
smoothing consume. Exactly one sample per frame is a *record* of where an
already-stabilized camera was pointing, which Gyroflow cannot improve on. The
threshold sits at twice the frame rate, with roughly 16× of headroom on one side
and 1.5× on the other, so it is not a delicate number.

```
==> stabilize:    off — 60 samples/s at 59.94 fps is one per frame, so there is nothing to stabilize with
==> intermediate: none — the LUT only, Gyroflow not run
==>   graded:     DJI_20260808163613_0037_D Graded.mov
==>   ungraded:   off (not stabilized)
```

**It fails toward stabilizing.** If the clip has no DJI telemetry track, or
carries a payload the parser does not recognise, the answer is "stabilize" — not
"skip". Running a render that was not needed costs time; skipping one that was
needed silently ships unstabilized footage. Non-DJI clips are therefore never
skipped, and behave exactly as they did before.

Three things change for a clip that is not stabilized:

- **No ungraded copy.** The second delivery exists to preserve the *stabilized*
  log original; without stabilization it would be a plain transcode of a file you
  already have, so it is not written.
- **The name tag is `" Graded"`**, not `" Stabilized Graded"` — see
  `--unstabilized-suffix`.
- **Frames keep their source size.** Resizing to the preset's output size is
  Gyroflow's job, and Gyroflow never sees the clip.

**Override.** `--force-stabilize` runs Gyroflow for every clip whatever the
detection says. Reach for it if a camera logs its motion somewhere this cannot
read and clips are being graded without being stabilized.

If no clip in the batch needs stabilizing, Gyroflow does not have to be installed
at all, the work directory holds nothing but the ffmpeg log so it does not need
to support FIFOs, and `export trims separately` in the preset stops being an
error — all three are Gyroflow's concerns only.

### Progress

Pipe mode shows one bar: Gyroflow is the slow half, and the encoder keeps pace
with it through the pipe.

A clip that is not stabilized, and anything under `--mode file`, runs the encode
on its own, so it gets a bar of its own — drawn from ffmpeg's `-progress` output
and measured against the duration of the file being read. In `--mode file` that
is the second of two:

```
    render [#####################---------]  70.4%  1267/1800 frames  ETA 3m18s
    grade  [############------------------]  41.2%  24s/60s  ETA 1m12s
```

Both bars are terminal-only. Redirect the output and they are left out, so logs
stay clean.

### Color tagging, and why the graded file is a `.mov`

The graded delivery is tagged **Rec.709-A**: the usual Rec.709 tag (primaries,
transfer and matrix all `1`, limited range) plus a `gama` atom of 1.96. That
atom is what stops QuickTime Player on macOS from shifting the gamma, and it is
the same thing Resolve writes when you pick Rec.709-A on a QuickTime delivery.
No pixels change — it is metadata only.

The `gama` atom is a QuickTime box, and ffmpeg refuses to write it into `.mp4`:

```
[mp4 @ ...] Not writing 'gama' atom. Format is not MOV.
```

So when Rec.709-A is on, H.264 and H.265 are delivered in `.mov` rather than
`.mp4` — same encoder, same bitrate, same `hvc1`/`avc1` tag, just the QuickTime
container, which is what Resolve does for a QuickTime H.265 export. ProRes,
DNxHD and CineForm were already `.mov`.

The **ungraded copy is left plain Rec.709**, with no `gama` atom. It is still
D-Log M, and plain Rec.709 is exactly what the camera tagged it (DJI writes
`colr nclx 1/1/1`, limited range) — so its tag survives the round trip
unchanged. Only the graded file is genuinely Rec.709 after the LUT, so only it
gets the Apple gamma.

`--rec709` turns all of this off: the graded file gets the plain tag and
H.264/H.265 stay in `.mp4`, as they were before.

You can check what landed with:

```sh
ffprobe -v error -show_entries stream=color_primaries,color_transfer,color_space,color_range \
  -of default=nk=0 "Heat 1 Stabilized Graded.mov"
```

`ffprobe` does not report the `gama` atom, so to confirm that one:

```sh
xxd "Heat 1 Stabilized Graded.mov" | grep -m1 -A1 gama   # 0001 f5c3  ->  1.96
```

### Existing files are skipped

Each delivery is checked on its own, so a run that lost only one of the two
re-renders just that one. `--overwrite` forces both.

The check is existence only — it does not validate the file. **If a run was
interrupted, delete its partial outputs before re-running**, or they will be
treated as finished. A truncated file has no `moov` atom and probes like this:

```sh
ffprobe -v error -show_entries format=duration -of csv=p=0 "Heat 1 Stabilized Graded.mov"
# moov atom not found  ->  delete it
```

### The work directory must support named pipes

The default mode on macOS renders through a FIFO, which needs a real filesystem.
APFS and HFS+ are fine. **exFAT, FAT, and most network shares are not** —
Gyroflow Batch detects this up front and tells you. Either point `--work` at an
APFS/HFS+ location that Gyroflow can read, or fall back to `--mode file`.

### Disk space

Pipe mode writes only the two deliveries. `--mode file` additionally writes a
full ProRes intermediate, roughly **100 GB per clip** — check free space first.

### Sleep

Gyroflow Batch holds a `caffeinate` power assertion for the duration of the run,
so the Mac will not idle-sleep partway through a long batch (on Windows, the
equivalent system power request). The assertion is tied to the process ID, so it
is released even if Gyroflow Batch is killed outright.

Two limits: the assertion against system sleep only applies on AC power, and
**closing the lid still sleeps the machine** no matter what.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `exit 101`, `PermissionDenied` in the trace | Mac App Store Gyroflow. Install the `.dmg` from gyroflow.xyz. |
| `has no 'output' block` | Preset saved without the **Export settings** group ticked. |
| `no .cube file in …` | LUT missing from `--src-dir`, or pass `--lut` explicitly. |
| A clip was graded but not stabilized | Its telemetry had no sub-frame motion data — check the `stabilize:` line. `--force-stabilize` overrides. |
| `the mp4_merge tool was not found` | Download it from the mp4-merge releases page and put it beside `gyroflow-batch.py`, or pass `--mp4-merge`. `--no-merge` skips merging. |
| `merge failed` | The parts could not be joined, e.g. a part is damaged or from an unfinished recording. The parts are left as they were. |
| A file stayed in the folder after merging | It was already in `Split parts`, so it was not moved over the other copy. Move it out of the way before the next run. |
| `cannot create a named pipe` | Work directory is on exFAT or a network share. Use `--work` or `--mode file`. |
| `ffmpeg/ffprobe not on PATH` | `brew install ffmpeg`, and check Homebrew's shell setup ran. On Windows, `winget install Gyan.FFmpeg` and open a new terminal. |
| `no working GPU encoder` | No NVENC/Quick Sync/AMF available — update the GPU driver. It still works, in software. |
| `--mode pipe needs a FIFO` | Windows has no FIFOs; drop `--mode pipe`. |
| Two `.cube` or `.gyroflow` files found | It warns and uses the first; pass `--lut` / `--preset` to choose. |
| Everything reports "exists, skipping" | Deliveries are already there. `--overwrite` to redo them. |

Gyroflow and ffmpeg logs are written into the work directory during a run and
removed on success. On failure the tail of the relevant log is printed.
