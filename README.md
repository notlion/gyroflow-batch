# Gyroflow Batch

Gyroflow Batch stabilizes a folder of DJI clips with Gyroflow and, optionally,
grades them with a D-Log M → Rec.709 LUT, in a single lossy encode.

- Recordings the camera split into several files are joined back into one first.
- Clips that need no stabilizing (gimballed drone footage) are detected and only
  graded.
- Stabilizing and delivery settings come from a preset you save in Gyroflow.

---

## Requirements

| Component | Requirement |
|---|---|
| OS | macOS (Apple silicon or Intel), or Windows 10/11 |
| Python | 3.8 or newer (standard library only) |
| ffmpeg / ffprobe | any recent build with `lut3d` |
| Gyroflow | on macOS, **the direct download, not the Mac App Store build** |
| mp4_merge | only if your camera splits long recordings into several files |

---

## Install on macOS

1. **Python.** Check with `python3 --version`. If that fails or offers to install
   developer tools, run `xcode-select --install`.

2. **Homebrew**, if you don't have it (`brew --version`):

   ```sh
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
   eval "$(/opt/homebrew/bin/brew shellenv)"
   ```

3. **ffmpeg** (includes ffprobe):

   ```sh
   brew install ffmpeg
   ```

4. **Gyroflow.** Download the macOS `.dmg` from <https://gyroflow.xyz> and drag
   `Gyroflow.app` into `/Applications`. If Gatekeeper blocks it, run
   `xattr -dr com.apple.quarantine /Applications/Gyroflow.app`.

   > Do not use the Mac App Store build. Its sandbox stops the command-line tool
   > from reading your files, and it fails with exit code 101 and
   > `PermissionDenied`.

5. **mp4_merge** (only for split recordings). From
   <https://github.com/gyroflow/mp4-merge/releases> download
   `mp4_merge-mac-arm64` (Apple silicon) or `mp4_merge-mac64` (Intel), put it
   next to `gyroflow-batch.py`, and let it run:

   ```sh
   chmod +x mp4_merge-mac-*
   xattr -d com.apple.quarantine mp4_merge-mac-*
   ```

6. **Gyroflow Batch:**

   ```sh
   chmod +x gyroflow-batch.py
   ```

## Install on Windows

1. **Python.** Check with `py --version`. If that fails, install it from
   <https://www.python.org/downloads/windows/>, or run:

   ```powershell
   winget install Python.Python.3.12
   ```

2. **ffmpeg** (includes ffprobe):

   ```powershell
   winget install Gyan.FFmpeg
   ```

   Then open a new terminal so `ffmpeg` is on your `PATH`.

3. **Gyroflow.** Download the Windows `.zip` from <https://gyroflow.xyz> and
   unzip it to `C:\Program Files\Gyroflow`. If you unzip it somewhere else, pass
   `--gyroflow "C:\path\to\gyroflow.exe"` when you run the script.

4. **mp4_merge** (only for split recordings). From
   <https://github.com/gyroflow/mp4-merge/releases> download
   `mp4_merge-windows64.exe` and put it next to `gyroflow-batch.py`.

5. **Gyroflow Batch.** Run it with the `py` launcher. Wherever this README shows
   `./gyroflow-batch.py`, use:

   ```powershell
   py gyroflow-batch.py --src-dir "D:\Footage\Race Day"
   ```

---

## Preparing a preset

1. In Gyroflow, load a representative clip and set up the lens profile,
   synchronization and stabilization.
2. Under **Export settings**, choose the delivery codec (H.265, H.264, ProRes,
   DNxHD or CineForm), bitrate, resolution and audio.
3. Turn **Export trims separately** off.
4. Save the preset with the **Export settings** group ticked, and put the
   `.gyroflow` file in your footage folder. A saved project works too: the
   script keeps its settings and ignores its video, motion data, offsets and
   trim.

---

## Usage

```sh
# stabilize every .mp4 in the folder
./gyroflow-batch.py --src-dir "/Volumes/Footage/Race Day"

# stabilize and grade
./gyroflow-batch.py --src-dir "/Volumes/Footage/Race Day" --lut ~/LUTs/DLogM-to-709.cube
```

`--src-dir` is required: the folder holding the clips and the preset.
Deliveries go to `<src-dir>/Stabilized`.

Add `--dry-run` to see the plan and the exact commands without rendering
anything:

```
==> preset:       stabilization-settings.gyroflow
==> lut:          DLogM-to-709.cube
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
LUT=~/LUTs/DLogM-to-709.cube

# only these clips (names relative to --src-dir)
./gyroflow-batch.py --src-dir "$D" "Heat 1.MP4" "Heat 2.MP4"

# graded delivery only
./gyroflow-batch.py --src-dir "$D" --lut "$LUT" --no-ungraded

# cut the last 5 s (a drone's landing) off every stabilized clip
./gyroflow-batch.py --src-dir "$D" --trim-end 5

# merge split recordings and stop, so they can be renamed first
./gyroflow-batch.py --src-dir "$D" --merge-only

# render again over existing deliveries
./gyroflow-batch.py --src-dir "$D" --overwrite "Heat 1.MP4"
```

### Options

| Flag | Meaning |
|---|---|
| `--src-dir DIR` | **required** — folder with the clips and preset |
| `--lut FILE` | `.cube` LUT to grade with; without it nothing is graded |
| `--out-dir DIR` | where deliveries go (default: `<src-dir>/Stabilized`) |
| `--preset FILE` | Gyroflow preset (default: the only `.gyroflow` in `--src-dir`) |
| `--gyroflow PATH` | Gyroflow command-line binary, if not in the default location |
| `--trim-end SECONDS` | cut this many seconds off the end of every stabilized clip |
| `--no-ungraded` | with `--lut`, skip the ungraded copy |
| `--merge-only` | merge split recordings and stop |
| `--no-merge` | process the parts of split recordings separately |
| `--mp4-merge PATH` | mp4_merge binary (default: on `PATH` or next to the script) |
| `--force-stabilize` | stabilize every clip, even those detected as not needing it |
| `--overwrite` | render deliveries that already exist again |
| `--dry-run` | print the plan and commands, then exit |
| `--suffix STR` | name tag of the graded file (default: `" Stabilized Graded"`) |
| `--ungraded-suffix STR` | name tag of the ungraded file (default: `" Stabilized"`) |
| `--unstabilized-suffix STR` | name tag of a clip graded without stabilizing (default: `" Graded"`) |
| `--rec709` | tag the graded file plain Rec.709 instead of Rec.709-A |
| `--work DIR` | scratch directory (default: `<src-dir>/.gyroflow-work`) |
| `--mode file` | render the intermediate to a file instead of a pipe (~100 GB per clip) |
| `--prores VARIANT` | ProRes variant of the intermediate (default: `4444`) |
| `--verbose` | show Gyroflow's own log |

---

## How it works

### Deliveries

| | Stabilized clip | Clip with nothing to stabilize |
|---|---|---|
| with `--lut` | `<name> Stabilized Graded.mov` and `<name> Stabilized.mov` | `<name> Graded.mov` |
| without `--lut` | `<name> Stabilized.mov` | skipped |

The ungraded file keeps the D-Log M look. Deliveries that already exist are
skipped, so an interrupted batch can be run again. If the script was killed
outright, delete any half-written delivery first.

### Split recordings

Cameras split long recordings into files of about 4 GB. Before anything else,
Gyroflow Batch finds the parts of each recording, using their creation times
and durations, and joins them with mp4_merge. The tool comes from the Gyroflow
project; it joins the files without re-encoding and keeps the motion data.

- The merged file is saved next to the parts as `<first part>_joined.MP4`.
- The parts are moved into a `Split parts` subfolder. Delete it once you are
  happy with the merged files.
- Merging needs as much free space again as the parts use.

To name recordings before stabilizing them, merge first, rename the `_joined`
files, then run as usual:

```sh
./gyroflow-batch.py --src-dir "$D" --merge-only
./gyroflow-batch.py --src-dir "$D" --lut "$LUT"
```

### Clips with nothing to stabilize

Gyroflow Batch reads each DJI clip's telemetry. A clip that logs one orientation
sample per frame comes from a gimballed camera that is already steady, so it
skips Gyroflow. With `--lut` it is graded only, at its original size. Clips
with fast motion data, and any clip whose telemetry it cannot read (including
non-DJI footage), are stabilized. `--force-stabilize` stabilizes every clip.

### Trimming the end

A drone's landing often contains the roughest movement of the flight, and
Gyroflow crops in far enough to steady it, which zooms the whole clip in.
`--trim-end 5` makes Gyroflow ignore the last 5 seconds of each clip, so the
zoom is set by the rest of the flight. The trimmed part is left out of the
delivery. The setting replaces any trim saved in the preset, and clips shorter
than the trim are skipped.

### Without a LUT

Gyroflow writes the delivery itself, using the preset's export settings. This
is faster than grading and needs no intermediate.

### With a LUT

Gyroflow renders a ProRes intermediate, which ffmpeg grades and encodes with
the preset's export settings. ffmpeg writes both deliveries from that one
render.

- **Intermediate.** On macOS it goes through a pipe, so nothing large is
  written to disk. The work directory must be on APFS or HFS+, not exFAT or a
  network share; otherwise use `--work` or `--mode file`. On Windows it is
  always a file of about 100 GB per clip, deleted once the clip is done.
- **GPU encoding.** With *Use GPU* in the preset, H.264/H.265 use VideoToolbox
  on macOS. On Windows the script uses the first that works of NVIDIA NVENC,
  Intel Quick Sync and AMD AMF. If none works, it encodes in software, which
  is much slower.
- **Color tags.** The graded file is tagged Rec.709-A (Rec.709 plus a QuickTime
  `gama` atom of 1.96), so QuickTime Player shows the correct gamma. That needs
  a `.mov`, so H.264/H.265 deliveries are `.mov` too. `--rec709` writes a plain
  Rec.709 tag and `.mp4` instead. The ungraded file is plain Rec.709, like the
  camera original.

### Sleep

While it runs, Gyroflow Batch keeps the computer from idle-sleeping. Closing a
laptop's lid still puts it to sleep.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `exit 101`, `PermissionDenied` | Mac App Store Gyroflow. Install the `.dmg` from gyroflow.xyz. |
| `has no 'output' block` | Save the preset again with the **Export settings** group ticked. |
| `Gyroflow CLI not found` | Pass `--gyroflow` with the path to Gyroflow's command-line binary. |
| `ffmpeg/ffprobe not on PATH` | Install ffmpeg; on Windows, open a new terminal afterwards. |
| No graded file | Pass `--lut`. |
| A clip was graded but not stabilized | It was detected as already steady. Use `--force-stabilize`. |
| A clip was skipped with "nothing to do" | It needs no stabilizing, and without `--lut` there is nothing else to do. |
| `the mp4_merge tool was not found` | Put mp4_merge next to the script, or pass `--mp4-merge`. `--no-merge` skips merging. |
| `could not start …: Permission denied` | Run `chmod +x` on that binary. |
| `merge failed` | A part is damaged or incomplete. The parts are left untouched. |
| `cannot create a named pipe` | The work directory is on exFAT or a network share. Use `--work` or `--mode file`. |
| `no working GPU encoder` | Update the GPU driver. Encoding still works in software. |
| Everything reports "exists, skipping" | The deliveries are already there. Use `--overwrite` to render them again. |
