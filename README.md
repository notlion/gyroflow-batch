# stabilize-and-grade

Stabilize DJI footage with Gyroflow and apply a D-Log M → Rec.709 LUT in a
**single lossy encode**.

You configure everything once in Gyroflow's GUI and save a normal preset. The
script splits that preset in two: your delivery settings (codec, bitrate, size,
audio, keyframe distance) drive the final ffmpeg encode, while a derived copy
sends Gyroflow's render into a named pipe as ProRes. One ffmpeg reads that pipe
once and writes two deliveries — the graded one through the 3D LUT, and a
stabilized-but-ungraded copy of the log original.

The source is decoded once, Gyroflow renders once, nothing is encoded twice, and
no multi-hundred-gigabyte intermediate file ever touches the disk.

---

## Requirements

| | |
|---|---|
| macOS | Apple silicon or Intel |
| Python | 3.8 or newer — standard library only, nothing to `pip install` |
| ffmpeg / ffprobe | any recent build with `lut3d` and VideoToolbox |
| Gyroflow | **the direct download, not the Mac App Store build** (see below) |

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

Any Python 3.8+ works. The script imports only the standard library, so there is
no virtualenv and no dependency list.

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

This installs `ffprobe` too. Both must be on your `PATH`; the script refuses to
start otherwise. Confirm the two pieces it depends on are present:

```sh
ffmpeg -hide_banner -filters  | grep lut3d
ffmpeg -hide_banner -encoders | grep videotoolbox
```

### 4. Gyroflow — download the .dmg

Get the macOS universal `.dmg` from <https://gyroflow.xyz> and drag `Gyroflow.app`
into `/Applications`.

> **Do not install Gyroflow from the Mac App Store.**
> The App Store build is sandboxed, and the sandbox blocks its command-line
> interface from reading anything outside its own container — including this
> script's work directory. It fails with exit code 101 and a `PermissionDenied`
> panic, which no amount of configuration will fix. Only the direct download
> works.

If Gatekeeper blocks the first launch, right-click the app and choose **Open**,
or clear the quarantine flag:

```sh
xattr -dr com.apple.quarantine /Applications/Gyroflow.app
```

Confirm the CLI binary is where the script expects it:

```sh
ls /Applications/Gyroflow.app/Contents/MacOS/gyroflow
```

Installed somewhere else? Pass `--gyroflow /path/to/Gyroflow.app/Contents/MacOS/gyroflow`.

### 5. The script

```sh
chmod +x stabilize_and_grade.py
```

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

When you save the preset, **tick the "Export settings" group**. The script reads
your delivery settings out of the preset's `output` block and stops with an error
if that block is missing.

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
the LUT. The script lives in this repo rather than beside your footage, so there
is nothing sensible for it to default to; run it without one and you get this
help text back.

```sh
./stabilize_and_grade.py --src-dir "/Volumes/Footage/Race Day"
```

Always start with a dry run — it prints the derived plan and the exact commands
without touching anything:

```sh
./stabilize_and_grade.py --src-dir "/Volumes/Footage/Race Day" --dry-run
```

```
==> preset:       stabilization-settings.gyroflow
==> lut:          DJI O4 Air Unit Series D-Log M to Rec.709 V1.cube
==> final encode: H.265/HEVC  @ 124.00 Mbps, 3840x2160, gpu=True
==>   -> ffmpeg:  -c:v hevc_videotoolbox -profile:v main10 -allow_sw 1 …
==> intermediate: ProRes 4444 through a pipe
==> output:       /Volumes/Footage/Race Day/Stabilized
==>   graded:     Heat 1 Stabilized Graded.mp4
==>   ungraded:   Heat 1 Stabilized.mp4
```

More examples:

```sh
D="/Volumes/Footage/Race Day"

# just two clips (names are relative to --src-dir)
./stabilize_and_grade.py --src-dir "$D" "Heat 1.MP4" "Heat 2.MP4"

# graded delivery only, no ungraded copy
./stabilize_and_grade.py --src-dir "$D" --no-ungraded

# redo something that already exists
./stabilize_and_grade.py --src-dir "$D" --overwrite "Heat 1.MP4"

# no FIFO support on the volume? write a real intermediate instead
./stabilize_and_grade.py --src-dir "$D" --mode file
```

### Options

| Flag | Meaning |
|---|---|
| `--src-dir DIR` | **required** — where the clips, preset and LUT are |
| `--out-dir DIR` | where deliveries go (default: `<src-dir>/Stabilized`) |
| `--preset FILE` | default: the only `.gyroflow` in `--src-dir` |
| `--lut FILE` | default: the only `.cube` in `--src-dir` |
| `--gyroflow PATH` | path to the Gyroflow CLI binary |
| `--work DIR` | scratch for the pipe (default: `<src-dir>/.gyroflow-work`) |
| `--prores VARIANT` | intermediate quality through the pipe (default: `4444`) |
| `--suffix STR` | name tag for the graded file (default: `" Stabilized Graded"`) |
| `--ungraded-suffix STR` | name tag for the ungraded file (default: `" Stabilized"`) |
| `--no-ungraded` | skip the ungraded copy |
| `--mode file` | write a real ProRes intermediate instead of piping (~100 GB/clip) |
| `--overwrite` | re-render files that already exist |
| `--dry-run` | print the plan and exit |
| `--verbose` | let Gyroflow's own log through to the terminal |

---

## Things worth knowing

### Where the files land

Deliveries go to `<src-dir>/Stabilized` unless you pass `--out-dir`.

The preset's own output folder is **not** used, on purpose. It usually points at
the footage itself, and deliveries sitting beside the sources get picked up as
sources by the next run — so each run ends up re-stabilizing its own output,
producing files like `Heat 1 Stabilized Graded Stabilized Graded.mp4` and growing
the work list every time. Keeping deliveries in a subfolder avoids that entirely.

### Existing files are skipped

Each delivery is checked on its own, so a run that lost only one of the two
re-renders just that one. `--overwrite` forces both.

The check is existence only — it does not validate the file. **If a run was
interrupted, delete its partial outputs before re-running**, or they will be
treated as finished. A truncated MP4 has no `moov` atom and probes like this:

```sh
ffprobe -v error -show_entries format=duration -of csv=p=0 "Heat 1 Stabilized Graded.mp4"
# moov atom not found  ->  delete it
```

### The work directory must support named pipes

The default mode renders through a FIFO, which needs a real filesystem. APFS and
HFS+ are fine. **exFAT, FAT, and most network shares are not** — the script
detects this up front and tells you. Either point `--work` at an APFS/HFS+
location that Gyroflow can read, or fall back to `--mode file`.

### Disk space

Pipe mode writes only the two deliveries. `--mode file` additionally writes a
full ProRes intermediate, roughly **100 GB per clip** — check free space first.

### Sleep

The script holds a `caffeinate` power assertion for the duration of the run, so
the Mac will not idle-sleep partway through a long batch. The assertion is tied
to the process ID, so it is released even if the script is killed outright.

Two limits: the assertion against system sleep only applies on AC power, and
**closing the lid still sleeps the machine** no matter what.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `exit 101`, `PermissionDenied` in the trace | Mac App Store Gyroflow. Install the `.dmg` from gyroflow.xyz. |
| `has no 'output' block` | Preset saved without the **Export settings** group ticked. |
| `no .cube file in …` | LUT missing from `--src-dir`, or pass `--lut` explicitly. |
| `cannot create a named pipe` | Work directory is on exFAT or a network share. Use `--work` or `--mode file`. |
| `ffmpeg/ffprobe not on PATH` | `brew install ffmpeg`, and check Homebrew's shell setup ran. |
| Two `.cube` or `.gyroflow` files found | It warns and uses the first; pass `--lut` / `--preset` to choose. |
| Everything reports "exists, skipping" | Deliveries are already there. `--overwrite` to redo them. |

Gyroflow and ffmpeg logs are written into the work directory during a run and
removed on success. On failure the tail of the relevant log is printed.
