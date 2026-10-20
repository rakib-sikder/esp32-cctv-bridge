# Motion recording for the V380 camera

Records only when something moves, keeps the camera's own quality, and costs
almost nothing to run.

```
camera 192.168.68.50
  ├─ /live/ch00_1  640x720   ──> decoded at 4 fps, 160x180 grey, frame differencing
  └─ /live/ch00_0  2304x2592 ──> on motion: ffmpeg -c copy  ──> clips/<day>/<time>.mp4
                                                                        └─> .jpg thumbnail
```

Detection runs on the small stream, so only a thumbnail-sized image is ever
compared. Recording copies the H.265 stream untouched — no re-encoding, so the
quality is exactly what the camera produced and the CPU barely moves.

## Why detection is done here

The camera advertises an ONVIF `MotionAlarm` topic but never fires it — not even
the initial value that every ONVIF property event is required to send when you
subscribe. Three minutes of deliberate motion produced nothing; the only events
it ever sends are `ImageTooDark` and `ImageTooBlurry`, both on subscribe.

Its motion detection does work, but it reports over the vendor's own protocol on
port 8800, not ONVIF. Rather than reverse engineer that, we detect here. The side
benefit is that this works with any camera, not just this one.

`MOTION_SOURCE=onvif` is still in the code for cameras that implement it properly.

## Files

| File | What it is |
|---|---|
| `recorder.py` | detection + recording + retention |
| `viewer.py` | web UI to browse and play clips |
| `install-windows.ps1` | registers both as Windows startup tasks |
| `cctv-recorder.service` | systemd unit for the VPS |

## Run it on Windows

Prerequisites (both already installed on this machine):

```powershell
winget install Python.Python.3.12
winget install Gyan.FFmpeg
```

Try it in the foreground first and walk in front of the camera:

```powershell
$env:CLIP_DIR = "$env:USERPROFILE\cctv-clips"
$env:CAM_IP   = "192.168.68.50"
python recorder.py
```

Expected:

```
detector: decoding /live/ch00_1 at 4.0 fps, 160x180
motion (17.7% of frame changed)
REC start -> ...\2026-10-20\23-07-18.mp4
REC stop  <- 23-07-18.mp4  14s  2.4 MB
```

Then make it permanent (elevated PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File install-windows.ps1
```

That registers `CCTV-Recorder` and `CCTV-Viewer` to start at logon and restart if
they crash. Remove them with:

```powershell
Unregister-ScheduledTask -TaskName CCTV-Recorder -Confirm:$false
Unregister-ScheduledTask -TaskName CCTV-Viewer   -Confirm:$false
```

## Viewer

```powershell
$env:CLIP_DIR = "$env:USERPROFILE\cctv-clips"
python viewer.py          # http://127.0.0.1:8080
```

Browse by day, thumbnail grid, play or download.

**About playback:** clips are H.265. Safari plays them; Chrome, Edge and Firefox
usually do not and will show a black player. Each clip has a **convert** link
that transcodes it to H.264 once in the background and caches the result. That is
the only part of this system that costs real CPU, which is why it is on demand.

By default the viewer binds `127.0.0.1`, so it is reachable only from this
machine. `VIEWER_BIND=0.0.0.0` exposes it to the LAN — there is no login on it,
so do that only on a network you trust, and never port-forward it.

## Settings

Environment variables, all optional:

| Variable | Default | Meaning |
|---|---|---|
| `CAM_IP` | `192.168.68.50` | camera address |
| `CAM_RECORD_PATH` | `/live/ch00_0` | stream that gets recorded (the HD one) |
| `CAM_DETECT_PATH` | `/live/ch00_1` | stream used for detection (the small one) |
| `MOTION_SOURCE` | `video` | `video` or `onvif` |
| `MOTION_FRACTION` | `0.02` | share of pixels that must change to count as motion |
| `PIXEL_DELTA` | `25` | how much one pixel must change to count |
| `DETECT_FPS` | `4` | detection frame rate |
| `POST_ROLL` | `10` | seconds to keep recording after motion stops |
| `MAX_CLIP` | `300` | hard cap on one clip |
| `RETENTION_DAYS` | `14` | clips older than this are deleted hourly |
| `MIN_FREE_GB` | `2` | below this, oldest clips go regardless of age |
| `CLIP_DIR` | `~/cctv-clips` (Windows) | where clips live |
| `FFMPEG` | `ffmpeg` | path to ffmpeg if not on PATH |

### Tuning sensitivity

Too many clips from shadows or light changes: raise `MOTION_FRACTION` to `0.04`
or `PIXEL_DELTA` to `35`. Missing real events: lower `MOTION_FRACTION` to `0.01`.
The log prints the percentage that triggered each clip, so use real numbers from
your own scene rather than guessing.

## Disk use

Measured on this camera: 2304x2592 H.265 at ~1.75 Mbps, so about **13 MB per
minute** of recorded motion. 30 minutes of events a day is ~400 MB/day; 14 days
is around 5.5 GB.

## Moving it to the VPS later

The code is identical on Linux. What changes:

1. The VPS must reach the camera. The Deco router has a VPN Server — dial in from
   the VPS, then confirm with `curl -m5 http://192.168.68.50:8899/onvif/device_service`.
2. `sudo apt install -y ffmpeg python3`
3. Copy `recorder.py` and `viewer.py` to `/opt/cctv/`, edit the `Environment=`
   lines in `cctv-recorder.service`, then:

```bash
sudo cp cctv-recorder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cctv-recorder
journalctl -u cctv-recorder -f
```

Note the unit still has `ExecStart=/opt/cctv/venv/bin/python`; with detection done
locally there are no third-party imports any more, so plain `/usr/bin/python3`
works unless you set `MOTION_SOURCE=onvif`, which needs `requests`.

## Security

RTSP and ONVIF on this camera take **no password at all** — anyone on the LAN can
watch it and read its user list. Keep ports 554 and 8899 off the internet; reach
them through the VPN only.
