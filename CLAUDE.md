# Tesla dashcam recovery — session notes

Goal: recover (incl. deleted) Tesla dashcam/Sentry clips from the car's USB drive,
decrypt them, and name them by recording time, camera and location.
Windows, run from an **admin** PowerShell. Drive is `D:`, **exFAT**, 512 B sectors,
256 sectors/cluster (**128 KiB clusters**), ~945k clusters, root dir cluster 4.

## Files

- `teslarecover/` — the GUI app (`python -m teslarecover`, or `Tesla Recover.bat`): FastAPI server +
  plain-JS frontend in a pywebview window (`--browser` / `--serve-only` for dev, `--library DIR` opens the viewer).
  Wizard: drive → scan → choose range → copy → Tesla sign-in → decrypt+rename → 6-camera viewer.
  - `volume.py` raw exFAT/FAT32 reader (bitmap, FAT, live + deleted dir entries), `scan.py` clip finder,
    `export.py` copy, `auth.py` Playwright sign-in, `decrypt.py` keys/decrypt/rename, `naming.py`
    (shared with rename_clips.py), `telemetry.py` SEI → per-frame JSON, `library.py` `<out>/library.json`.
  - Token decision: the user explicitly asked for in-app sign-in + decryption (2026-10-12). Token is
    captured from the dashcam.tesla.com OAuth (PKCE) token response / Bearer headers and kept in memory only.
  - Sign-in must NOT run in a Playwright-launched browser: auth.tesla.com's bot protection (Akamai)
    rejects every captcha answer there. `auth.py` starts the system Edge/Chrome normally (own profile,
    `--remote-debugging-port`), only polls `/json/list` for the URL during login, and attaches with
    `connect_over_cdp` once the tab is back on dashcam.tesla.com (then reloads and listens).
  - Raw `\\.\D:` reads work without admin on this machine; the app offers UAC elevation only if refused.

- `run.py` — raw-volume FAT32/exFAT reader + carver (originally a Python 2 script).
  - `python run.py D: <outdir> [carve|list]` (default `carve`). `D:` is turned into
    `\\.\D:`; `AlignedReader` does sector-aligned reads (raw volumes require it).
  - `list`: walks directories (`readDirectoryExFat`), prints name/size/date/cluster ranges.
    Export of files is disabled by `if True: continue` under "Modify this condition".
  - `carve`: scans every cluster; plain MP4 (`\0\0\0 ftypmp42`) and encrypted clips
    (eCryptfs marker, see below) → `<outdir>/mp4s/<cluster>.mp4` / `<cluster>-encrypted.mp4`.
    Encrypted clips are cut to their exact length from the header, assuming contiguous clusters.
- `tesla_dashcam_decrypt.py` — copy of https://github.com/XGxF3/tesla-dashcam-decrypt.
  User runs it themselves with a Bearer token from dashcam.tesla.com. Decryption was
  deliberately NOT built into run.py (the permission system blocked sending
  token/metadata to Tesla's API from code I wrote) — don't re-add it without the user
  explicitly approving that.
- `rename_clips.py` — renames decrypted clips (dry run unless `--apply`):
  `python rename_clips.py recover\decrypted --listing listing.txt [--apply] [--tz-offset H] [--cluster-size N]`
  → `YYYY-MM-DD_HH-MM-SS-<camera>_<cluster>_<title>_<comment>.mp4`, also sets mtime.
  Only touches files named `<digits>[-encrypted].mp4`; skips files modified < 60 s ago.
- `listing.txt` — output of `run.py D: ... list` (UTF-16, written by PowerShell `>`).
- `recover/decrypted/` — ~3100 decrypted clips named `<cluster>-encrypted.mp4`
  (cluster = where the encrypted clip started on D:). Decryption was still running
  in the background at the end of the session.

## Findings (verified)

- **Encrypted clip format (2026.20+)**: 0x00 plaintext size (u64 BE), 0x08/0x0C magic
  words with `m1 ^ m2 == 0x3c81b7f5` (eCryptfs marker), 0x14 = 0x1000; 0x1000 key
  metadata page (key_id, public key, VIN, timestamp, wrapped key); payload from 0x2000
  in 4096-byte AES-128-CBC pages. Encrypted size = 0x2000 + ceil(plain/4096)*4096.
  No fixed byte signature at offset 0 — must detect via the XOR marker.
- **Time without decrypting**: encrypted header field at 0x1056 (u64 BE) is Unix
  seconds UTC ≈ when the clip *ended* (60–63 s after mvhd start for full 1-min clips,
  less for short last clips). Works even when decryption/moov is broken.
- **Recording time**: `moov/mvhd` creation_time = recording start in **UTC**. Tesla's
  own filenames are local time (+2 h CEST); checked on 667 clips matched to the listing.
- **Location**: `©nam` (title) = area/district, `©cmt` (comment) = street.
- **Telemetry**: every frame has an SEI NAL (type 6, payload type 5, prefix `BBBi`)
  holding a protobuf with emulation-prevention bytes (`00 00 03`): 1 version,
  2 gear, 3 frame_seq_no, 4 speed m/s (float), 5 accelerator %, 6 steering angle,
  7/8 blinkers, 9 brake, 10 autopilot, 11 lat (double), 12 lon, 13 heading,
  14–16 acceleration x/y/z. The SEI has **no timestamp** and **no camera id**
  (it's the same in all 6 cameras). This is what dashcam.tesla.com's overlay uses (local, not server).
- **Camera**: front = 2896×1876, the other five are 1448×938 with identical headers.
  The car writes each minute's 6 files back to back in the order
  `front, left_repeater, right_repeater, back, left_pillar, right_pillar` (true for
  all 140 groups in the listing); each file starts at the cluster right after the
  previous one ends. rename_clips.py uses: listing (exact) → front by width → chain
  of adjacent clusters → full-group order → otherwise `side`. Validated against the
  listing: 0 wrong cameras.
- **Clip counts (dry run)**: 2657 renameable (2332 with a camera name, 325 `side`), 463 skipped:
  ~326 have a corrupt/missing `moov` (clip was fragmented on disk, so the contiguous
  carve grabbed the wrong bytes after `mdat`), ~37 aren't MP4 at all (bad decryption), rest were still being written.
- Clips still listed under `D:\TeslaCam\EncryptedClips\{RecentClips,SentryClips,SavedClips}`
  can simply be copied in Explorer (correct names, and fragmented files come out
  intact); carving is only needed for deleted clips.

- **Scan** (`teslarecover.scan`): ~2–4 min. Carves every cluster not owned by a live file — not just
  bitmap-free ones: ~30k clusters are marked allocated but owned by nothing and hold old clips.
  Carved runs skip live clusters and stop at the next clip header (`truncated`). Files < 100 kB
  with the eCryptfs header are event.json/thumb.png/readme, not clips. Covers every clip of the old carve.
  Only 2 of 71 deleted dir entries validate (the rest were overwritten).

- **Repair** (`teslarecover/repair.py`): rebuilds moov from mdat (length-prefixed NALs; frame
  boundary = SEI/SPS/PPS/AUD or a slice with first_mb_in_slice 0, since pre-2026 clips have no SEI;
  stops at a bad NAL or a jump in the SEI frame counter, i.e. bytes from another clip). Constant
  278/10000 s per frame. Width/height from the SPS. On the 420 clips the old pipeline couldn't play:
  406 rebuilt (~3 h of footage), 394 matched to their minute by header end time (±5 s).
- **Viewer sync**: never seek a video that is still seeking. Correct small drift with playbackRate ±10 %,
  seek at most every 1.5 s. A watchdog reloads a video that stops advancing, and the tile is marked dead after 2 reloads.
- The token pickup needs dashcam.tesla.com to call its API, which it only does once a clip is
  opened. `auth._open_clip` feeds it one of the copied encrypted clips via the file input.

## Open ideas / not done

- Apply the rename once decryption finishes (user's decision; check that the decrypt
  tool's `--skip-existing` won't redo renamed files).
- Recover the *rest* of truncated clips: with the clip's key, decrypt candidate clusters
  elsewhere on the drive and check whether they continue the H.264 stream or frame counter.
- Better carving for fragmented deleted clips (currently contiguous-only).
- Export the SEI telemetry as CSV/GPX, or put exact start GPS coordinates in filenames.

Scratch tools used for the analysis (MP4 box dump, SEI decode, validation against the
listing) were throwaway scripts and are not in this folder.
