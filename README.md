# CCTV system for a V380 camera

Getting a cheap V380 IP camera out of the vendor's app and cloud, and recording
it on hardware you own.

Two independent recorders are here. They solve the same problem at different
costs:

| | runs on | records | needs a PC on? |
|---|---|---|---|
| **esp32-cctv** | an ESP32 (~$4) | 640x720 sub stream | no |
| **cctv-vps** | a PC or VPS | 2304x2592 main stream | yes |

Both detect motion themselves and push clips to Oracle Object Storage, so the
footage survives the camera and the local machine.

## Why not just use the camera?

The camera can upload on motion, but only to the vendor's own cloud on a paid
plan. Its firmware has no FTP, no SMTP and no custom-server option - that was
checked by decompiling the Android app, not assumed. See
[docs/CAMERA-API.md](docs/CAMERA-API.md).

So something on the LAN has to pull the stream. Uploads are outbound, which is
why this works behind CGNAT with no port forwarding and no public IP.

```
camera ──RTSP──> ESP32 or PC ──HTTPS──> Oracle Object Storage
```

## esp32-cctv

An ESP32 speaks RTSP, unpacks H.265 from RTP, and PUTs segments to the bucket.
It never decodes video - it cannot, and does not need to:

- **Motion detection is bitrate based.** A still scene compresses to almost
  nothing; movement makes the bitrate jump. Counting bytes costs no CPU.
- **Recording is a byte copy.** The camera's own H.265 is written straight out
  as Annex-B, so quality is untouched.

Measured on a WROOM-32 (no PSRAM): sub stream is 0.20 Mbps, heap stays ~120 KB
free, segments upload in a couple of seconds.

### One thing worth knowing about this camera

It packs an entire keyframe access unit - VPS, SPS, PPS and the IDR, each with
its own Annex-B start code - inside a single fragmented RTP unit labelled
**NAL type 32**. A real VPS is about 28 bytes; these are ~19 KB.

This matters: a segment that does not begin on one of those units will not
decode at all (`PPS id out of range`, zero frames). The firmware waits for one
before it starts a segment.

### Build

```bash
arduino-cli core install esp32:esp32
cp esp32-cctv/secrets.example.h esp32-cctv/secrets.h   # then edit it
arduino-cli compile --fqbn esp32:esp32:esp32:UploadSpeed=115200 \
    --upload --port COM5 esp32-cctv
```

Use `UploadSpeed=115200`. The default 921600 corrupts the transfer on a CP2102.

## cctv-vps

Python, standard library plus ffmpeg. Use this when you want the full 6 MP
stream and have a machine that stays on.

- `recorder.py` - decodes the small sub stream at 4 fps and compares frames;
  on motion it records the main stream with `ffmpeg -c copy`
- `dashboard.py` - live view (MJPEG), PTZ pad over ONVIF, clip browser
- `uploader.py` - pushes finished clips to the same bucket
- `install-windows.ps1` - registers both as startup tasks

Setup and tuning: [cctv-vps/SETUP.md](cctv-vps/SETUP.md).

Motion detection is done here rather than taken from the camera because the
camera advertises an ONVIF `MotionAlarm` topic but never fires it - not even the
initial property value ONVIF requires on subscribe. Three minutes of deliberate
movement produced nothing.

## docs

- [CAMERA-API.md](docs/CAMERA-API.md) - open ports, what ONVIF really does
  versus what it claims, the 119 settings IDs, and how to build your own client
- [PROTOCOL-8800.md](docs/PROTOCOL-8800.md) - the vendor control protocol:
  framing, method ids, and where the encrypted part sits

## Security notes

RTSP and ONVIF on this camera accept **no password at all**. Anyone on the LAN
can watch it. Keep ports 554 and 8899 off the internet.

`secrets.h` and `par_url.txt` hold your WiFi password and upload URL. Both are
gitignored; copy the `.example` file and fill in your own.

A pre-authenticated request is a credential in URL form. It only reaches the one
bucket, and you can delete and reissue it at any time.
