# Tesla Dashcam Recovery

Get back dashcam and Sentry Mode videos from a Tesla USB drive, **including clips the car has already
deleted**, and watch them with all six cameras side by side.

- Finds clips still on the drive **and** deleted clips left in the drive's free space.
- Lets you pick a time range ("the 10 minutes around the accident") or recover everything.
- Decrypts clips from newer Tesla software (2026.20+) using your own Tesla account.
- Names each file by recording time, camera and street, e.g.
  `2026-10-11_12-49-30-front_Downtown_Main-Street.mp4`.
- Repairs many damaged deleted clips so they play again.
- Plays all six cameras in sync. Click one to enlarge it, overlay speed, steering, pedals,
  Autopilot and GPS, and copy the minutes you need to another folder.

The USB drive is **only read, never changed**.

> This is an independent, unofficial tool. It is not made by or affiliated with Tesla.
> It only works with clips from **your own car**: decryption keys come from your Tesla account.

---

## Contents

1. [What you need](#1-what-you-need)
2. [Install (one time)](#2-install-one-time)
3. [Start the app](#3-start-the-app)
4. [Recover your clips, step by step](#4-recover-your-clips-step-by-step)
5. [Watching the footage](#5-watching-the-footage)
6. [Where your files go](#6-where-your-files-go)
7. [Privacy and safety](#7-privacy-and-safety)
8. [Troubleshooting](#8-troubleshooting)
9. [macOS and Linux](#9-macos-and-linux)
10. [For developers](#10-for-developers)

---

## 1. What you need

- A **Windows 10 or Windows 11** PC (macOS and Linux are not supported yet; see [section 9](#9-macos-and-linux)).
- The **Tesla USB drive** (or SSD) and a way to plug it into the PC. Both **HW3** (4 cameras) and **HW4** (6 cameras)
  cars are supported.
- A **Tesla account** that the car belongs to. This is needed for clips from Tesla software 2026.20
  and newer, which are encrypted.
- **Free disk space** on the PC. Plan on about twice the size of the clips you recover:
  one encrypted copy plus one playable copy. The app shows the size before copying.
- **Microsoft Edge** or **Google Chrome**. Edge comes with Windows, so you almost certainly have it.
- An internet connection. It's used for the one-time install, and for getting the decryption keys from Tesla.

You do **not** need to install Python or know any programming. The steps below install everything automatically.

> **Important — stop using the USB drive in the car.** The car keeps recording over old (deleted)
> clips. The sooner you take the drive out and scan it, the more deleted footage can be recovered.
> Don't format it, and don't let Windows "repair" it if it asks.

---

## 2. Install (one time)

### Step 1 — Install uv

**uv** is a small free tool that downloads the right Python version and everything else the app needs.
You only do this once.

1. Press the **Windows key**, type **PowerShell**, and click **Windows PowerShell**.
   (A normal window is fine; you don't need "Run as administrator".)
2. Copy this line, paste it into the PowerShell window (right-click pastes), and press **Enter**:

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

3. Wait until it says uv is installed (a few seconds), then close the PowerShell window.

More about uv: <https://docs.astral.sh/uv/>

### Step 2 — Download the app

1. Go to the project page: **https://github.com/Arquiteto/tesla-dashcam-recovery**
2. Click the green **Code** button, then **Download ZIP**.
3. Open your **Downloads** folder, right-click the ZIP file, choose **Extract All…**, and pick a place
   that's easy to find, for example `C:\TeslaRecovery`. Click **Extract**.

(If you use Git: `git clone https://github.com/Arquiteto/tesla-dashcam-recovery.git`.)

That's it: there's nothing else to install.

---

## 3. Start the app

1. Open the folder you extracted (e.g. `C:\TeslaRecovery\tesla-dashcam-recovery-main`).
2. Double-click **`Tesla Recover.bat`**.
   - If Windows shows **"Windows protected your PC"**, click **More info → Run anyway**. Windows says
     this about any script downloaded from the internet.
3. A black console window opens. **The first start takes 1–3 minutes**, because it downloads Python and
   the app's components. Later starts take a few seconds.
4. The **Tesla Dashcam Recovery** window opens.

Keep the black console window open while you use the app; closing it closes the app. If something goes
wrong, the error message is shown there.

**Tip:** to make a desktop shortcut, right-click `Tesla Recover.bat` → **Show more options** →
**Send to** → **Desktop (create shortcut)**.

---

## 4. Recover your clips, step by step

The steps are shown at the top of the window (**1 · Drive** to **7 · Watch**). Steps that are done turn
green. You can go back to any step by clicking it.

### 1 · Drive

Plug in the Tesla USB drive. The app lists the drives it finds and selects the Tesla one automatically
(it has a `TeslaCam` folder). Click **Scan drive**.

### 2 · Scan

The app now looks for three kinds of clips:

- clips that are still on the drive,
- deleted clips that the drive's folder records still point to,
- deleted clips left anywhere in the drive's free space.

This takes **about 3–6 minutes for a 128 GB drive**. The recording time of every clip is read during
the scan, so no decryption is needed yet. You can click **Cancel** at any time.

### 3 · Choose clips

You see a timeline of everything found. Blue is clips still on the drive; orange is clips recovered
from deleted data. Below it is a list of **recordings** (drives and Sentry events).

- **All clips** recovers everything, or
- **click a recording** in the list to select it, and **Shift-click** another one to extend the selection, or
- **drag across the timeline** to select a time range, or
- type exact **From** / **to** times.

Clips still on the drive that fall in the selected range are always included. The bottom line shows how
many clips and how much space your selection needs. Click **Next: choose output folder**.

### 4 · Copy

Click **Browse…** and choose (or create) an **empty folder** on your PC, then click **Copy clips**.
The clips are copied to the PC first (still encrypted), so the USB drive can be unplugged afterwards.

### 5 · Sign in

Newer Tesla software encrypts the videos, and only the car's owner can get the keys. Click
**Open Tesla sign-in**:

1. An **Edge** (or Chrome) window opens on **dashcam.tesla.com**, Tesla's own website.
2. Sign in with your Tesla account as usual. Solve the captcha and do the two-step verification if asked.
3. Once you're signed in, the app opens one of your clips on that page by itself, picks up your sign-in,
   and **closes the window**. That's normal.
   If it says *"open any encrypted clip on the dashcam.tesla.com page"*, drag any `.mp4` from the
   `encrypted` subfolder of your output folder onto the Tesla page.

Your password is typed only into Tesla's own website; the app never sees it. If the sign-in window
doesn't work at all, open **"Sign-in window not working? Paste a token instead"** and follow the hint there.

### 6 · Decrypt

Click **Decrypt**. The app gets the keys from Tesla and decrypts the clips **on your PC**; the videos are
never uploaded. Then it:

- names every clip `date_time-camera_area_street.mp4` (in your local time),
- **repairs** deleted clips that were damaged on the drive. These usually keep the first part of their
  minute and get `_repaired` in their name.
- puts clips that can't be rescued into an `unplayable` subfolder.

At the end you see how many clips are ready to watch. Leave **Keep the encrypted copies** ticked
unless you need the disk space. With the encrypted copies you can decrypt again later if anything goes wrong.

### 7 · Watch

Click **Watch footage**. See the next section.

---

## 5. Watching the footage

The left side lists every recorded minute, grouped by day and by drive. Click a minute to play it.
The six cameras play in sync, and the app moves on to the next minute automatically.

| Do this | To |
|---|---|
| Click a camera | show it large (click again for all six) |
| Keys **1–6** / **0** or **Esc** | enlarge front, left repeater, right repeater, back, left pillar, right pillar / back to the grid |
| **Space** | play / pause |
| **← / →** | 5 seconds back / forward |
| **↑ / ↓** or **Page Up / Page Down** | previous / next minute |
| **Telemetry** checkbox | show speed, gear, steering, blinkers, brake/accelerator, Autopilot and GPS |
| **0.5× … 4×** | playback speed |
| **Ctrl-click** minutes in the list, **Shift-click** for a range, then **Save…** | copy those minutes to a folder of your choice |

"`3/6`" next to a minute means only 3 of the 6 cameras were recovered. Cars with **HW3** record
four cameras (no pillar cameras); their minutes are shown as a 2×2 grid, and keys **1–4** enlarge a camera. Cameras marked **Side (unknown)**
were recovered, but the app couldn't tell which side camera they are.

To watch clips you recovered earlier, click **Open recovered folder…** (top right) and choose the output folder.
If some clips are listed as damaged, **Try to repair them** in the list gives them another try.

---

## 6. Where your files go

In the output folder you chose:

| Path | What it is |
|---|---|
| `2026-10-11_12-49-30-front_Area_Street.mp4` … | the playable clips: normal MP4 files that work in any video player |
| `…_repaired.mp4` | deleted clips rebuilt from the part that survived |
| `encrypted/` | the encrypted copies taken from the drive |
| `unplayable/` | clips that couldn't be decrypted or repaired |
| `library.json`, `.telemetry/` | the app's index of the folder (safe to ignore) |

---

## 7. Privacy and safety

- **The USB drive is never written to.** The app reads it directly at a low level, which is how it finds deleted clips.
- **Videos stay on your PC.** Only the small key header of each clip (which includes your car's VIN) is
  sent to Tesla's dashcam service, which is the same thing dashcam.tesla.com does when you open a clip there.
- **Your Tesla password** is only entered on Tesla's website. The app keeps the sign-in token **in
  memory only**, and it's gone when you close the app.
- The sign-in window uses its own browser profile in `%LOCALAPPDATA%\TeslaRecover\browser-profile`, so
  you stay signed in to Tesla there for next time. Delete that folder to sign out completely
  (recommended on a shared PC).
- Clips contain your locations and GPS tracks. Be careful when sharing them.

---

## 8. Troubleshooting

**The console window says `uv` is not installed / not recognized.**
Do [Install → Step 1](#step-1--install-uv) again, then **sign out of Windows and back in** (or restart),
and double-click `Tesla Recover.bat` again.

**The window doesn't open, or it is blank.**
The app window uses Microsoft Edge WebView2, which comes with Windows 11. On Windows 10, install the
"Evergreen Runtime" from <https://developer.microsoft.com/microsoft-edge/webview2/>. Or start the app in
your normal browser: open PowerShell in the app folder and run `uv run python -m teslarecover --browser`.

**"Windows refused direct access to the drive".**
Click **Restart as administrator** when the app offers it, and accept the Windows prompt.

**My drive isn't listed.**
Check that it shows up in File Explorer with a `TeslaCam` folder. The drive must be formatted exFAT
or FAT32, as Tesla requires. If Windows asks to scan or repair the drive, click **No / Cancel**.

**"Another step is still running".**
The app shows that step. Wait for it to finish or click **Cancel**, then try again.

**The sign-in window keeps showing the captcha / "try again".**
Close the window, wait a minute, and click **Open Tesla sign-in** again. If it keeps happening, sign in
once at dashcam.tesla.com in your normal browser, then use the paste-token option.

**"Tesla sign-in expired".**
Sign in again (step 5). Decryption continues where it stopped.

**Some clips are in `unplayable` or show "can't play this clip".**
These are deleted clips whose data was overwritten by the car or scattered across the drive. Try
**Try to repair them**. Whatever is still unplayable is usually lost for good.

**A camera freezes during playback.**
The app reloads a frozen camera by itself. If it happens often, close other programs or play at 1×.
Decoding six videos at once (one of them in high resolution) is demanding for older PCs.

**Still stuck?**
Open an issue on GitHub. Include the text from the black console window and which step you were on.

---

## 9. macOS and Linux

**Not supported yet.** These Windows-only parts of the app would need changes:

- **Finding the drive:** it lists drive letters through the Windows API. Mac and Linux need
  their own device listing (`diskutil` / `lsblk`).
- **Reading the drive directly:** the reader opens `\\.\D:`. On macOS this would be
  `/dev/rdiskN`, and on Linux `/dev/sdX1`. Both need administrator rights (`sudo`), and on
  macOS also "Full Disk Access".
- **Copying clips that are still on the drive:** it builds paths from the drive letter, and
  would need the mount point instead.
- **Starting the sign-in browser:** it looks for Edge and Chrome only in their Windows install
  folders.
- **Smaller things:** "Open folder", "Restart as administrator", and the settings folder
  (`%LOCALAPPDATA%`).

The rest (decryption, repair, renaming, telemetry and the viewer) is plain Python and web code and
should work as it is. On Linux, `--browser` mode avoids needing the GTK/Qt WebKit that pywebview requires.

---

## 10. For developers

```bash
uv run python -m teslarecover                     # the app (native window)
uv run python -m teslarecover --browser            # in your default browser
uv run python -m teslarecover --library <folder>   # straight to the viewer
uv run python -m teslarecover.scan D: scan.json    # scan only, results as JSON
```

| File | Purpose |
|---|---|
| `teslarecover/volume.py` | read-only exFAT/FAT32 reader (allocation bitmap, FAT, live and deleted folder entries) |
| `teslarecover/scan.py` | finds clips: live files, deleted entries, carving of unowned clusters |
| `teslarecover/export.py` | copies the selected clips |
| `teslarecover/auth.py` | Tesla sign-in in a normal Edge/Chrome window and token pickup |
| `teslarecover/decrypt.py` | gets keys, decrypts, names clips, runs repair |
| `teslarecover/repair.py` | rebuilds the MP4 index (`moov`) from the surviving H.264 frames |
| `teslarecover/naming.py` | MP4 metadata, camera detection, file names |
| `teslarecover/telemetry.py` | per-frame telemetry from the SEI data in each frame |
| `teslarecover/server.py`, `static/` | local web server and the app's user interface |
| `run.py`, `rename_clips.py` | the original command-line scripts |
| `tesla_dashcam_decrypt.py` | decryption code from [XGxF3/tesla-dashcam-decrypt](https://github.com/XGxF3/tesla-dashcam-decrypt) |

### Credits

The encrypted clip format and the decryption are based on
[tesla-dashcam-decrypt](https://github.com/XGxF3/tesla-dashcam-decrypt) by XGxF3.

### License

[MIT](LICENSE). `tesla_dashcam_decrypt.py` comes from
[tesla-dashcam-decrypt](https://github.com/XGxF3/tesla-dashcam-decrypt) by XGxF3, also under the MIT License.
