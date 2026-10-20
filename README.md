# CCTV system for a V380 camera

Taking a cheap V380 IP camera off the vendor's app and cloud, and recording it
on hardware and storage you control.

```
camera ──RTSP──> ESP32 ──HTTPS──> Oracle Object Storage ──> a URL on your phone
         H.265    ~$4 chip         60-day retention          joined in the browser
```

No PC has to stay on. No server runs anywhere. No subscription.

## How it works

**The ESP32 watches the camera.** It opens the camera's RTSP sub stream and
counts bytes. A still scene compresses to almost nothing; the moment something
moves the bitrate jumps, and that is the motion detector - no video decoding, no
CPU cost. When motion starts it copies the stream to a buffer and PUTs it to
Object Storage. A WROOM-32 does all of this with ~120 KB of heap to spare.

**The browser does the assembling.** Segments pile up in the bucket as raw
H.265. Open the viewer URL, pick a day, and the page fetches that day's clips,
joins them and wraps them in mp4 using ffmpeg compiled to WebAssembly. The video
is never re-encoded, so it stays exactly as the camera shot it.

**Why not let the camera upload directly?** It can, but only to the vendor's
cloud on a paid plan. Its firmware has no FTP, no SMTP and no custom-server
option - that was established by decompiling the Android app, not assumed. See
[docs/CAMERA-API.md](docs/CAMERA-API.md).

Because every connection is outbound, this works behind CGNAT with no public IP,
no port forwarding and no VPN.

## What is here

| | |
|---|---|
| `esp32-cctv/` | the firmware: RTSP client, H.265 depacketiser, motion detection, TLS upload |
| `viewer.html` | the whole UI - lists days, builds and plays a day in your browser |
| `publish_viewer.py` | injects the listing URL and uploads the page to the bucket |
| `events.py` | command-line browse and rebuild, if you prefer a terminal |
| `daily.py` | optional: pre-join a day on a PC and store the mp4 (see below) |
| `cctv-vps/` | an alternative recorder for the full 2304x2592 stream, needs a PC or VPS |
| `docs/` | what the camera actually does, from decompiling the app and probing it |

## Three things this camera does that cost real time to find

**1. ONVIF motion is a stub.** The camera advertises a `MotionAlarm` topic but
never fires it - not even the initial property value ONVIF requires on
subscribe. Three minutes of deliberate movement produced nothing, while
`ImageTooDark` and `ImageTooBlurry` arrived fine. Motion has to be detected
elsewhere, which is why both recorders do their own.

**2. Keyframes hide inside a "VPS".** The camera packs an entire keyframe access
unit - VPS, SPS, PPS and the IDR, each with its own Annex-B start code - into a
single fragmented RTP unit labelled **NAL type 32**. A real VPS is about 28
bytes; these are ~19 KB:

```
00 00 00 01 40 01 0C 01 FF FF ...   <- type 32, looks like a VPS
00 00 00 01 42 01 01 01 40 ...      <- type 33 (SPS) nested inside it
```

So "type 32 and larger than 1 KB" is the keyframe marker. A segment that does
not open on one of those will not decode at all - ffmpeg reports `PPS id out of
range` and zero frames.

**3. Interleaved RTP framing has no slack.** Read the 4-byte header, then
exactly N bytes. On a packet too big for the buffer, drain it rather than
bailing out - one short read desynchronises the stream and the large packets,
which is to say the keyframes, are the first thing you lose.

## Setup

### 1. Camera

Enable RTSP and ONVIF: in the V380 app, **Advanced settings -> Onvif function**.
The camera reboots. Ports 554 and 8899 open afterwards.

On this firmware both accept **no password at all**, so keep them off the
internet.

### 2. Storage

Create an Object Storage bucket, then two pre-authenticated requests:

```bash
# upload target for the ESP32 (read + write)
oci os preauth-request create --namespace $NS --bucket-name cctv-clips \
    --name cctv-upload --access-type AnyObjectReadWrite \
    --time-expires 2027-01-01T00:00:00Z

# listing URL for the viewer (read + write + list)
oci os preauth-request create --namespace $NS --bucket-name cctv-clips \
    --name cctv-browse --access-type AnyObjectReadWrite \
    --bucket-listing-action ListObjects \
    --time-expires 2027-01-01T00:00:00Z
```

Auto-delete old clips so storage cannot creep:

```bash
echo '[{"name":"delete-old-clips","action":"DELETE","objectNameFilter":{"inclusionPrefixes":["esp32/"]},"timeAmount":60,"timeUnit":"DAYS","isEnabled":true}]' > lc.json
oci os object-lifecycle-policy put --namespace $NS --bucket-name cctv-clips --items file://lc.json --force
```

Lifecycle rules need the service principal to be allowed first, once per
tenancy: `Allow service objectstorage-<region> to manage object-family in tenancy`.

### 3. Firmware

```bash
arduino-cli core install esp32:esp32
cp esp32-cctv/secrets.example.h esp32-cctv/secrets.h   # then edit it
arduino-cli compile --fqbn esp32:esp32:esp32:UploadSpeed=115200 \
    --upload --port COM5 esp32-cctv
```

Use `UploadSpeed=115200`; the default 921600 corrupts the transfer on a CP2102.
Then power the ESP32 from any USB charger near the camera - it reconnects on its
own.

### 4. Viewer

Put the listing PAR in `browse_url.txt`, then:

```bash
python publish_viewer.py
```

It prints a URL. Open it on a phone, bookmark it, done. The page downloads
ffmpeg.wasm once on first use and caches it.

**That URL can read your recordings, so treat it as a password.** If it leaks,
delete the PAR in the console and publish again.

## Playback note

The camera only encodes H.265, and the vendor app on this model has no H.264
option. Most phones decode H.265 happily; some desktop browsers do not. If yours
does not, `daily.py` can pre-join a day on a PC and re-encode it to H.264, which
plays everywhere - at the cost of needing a machine to run it.

## cctv-vps

A separate recorder for the full 2304x2592 stream, for when you do have a
machine that stays on. It decodes the small sub stream at 4 fps to compare
frames, records the main stream with `ffmpeg -c copy` on motion, and includes a
local dashboard with live view, a PTZ pad over ONVIF and a clip browser. See
[cctv-vps/SETUP.md](cctv-vps/SETUP.md).

## Hardware

- ESP32-WROOM-32 (classic ESP32). No PSRAM needed - buffers fit in internal RAM.
  Built and tested on an ESP32-D0WD-V3 with 4 MB flash.
- Any USB charger.
- Nothing else. No SD card, no PIR sensor.

Measured: the sub stream is 0.20 Mbps, about 1.5 MB per minute of recorded
motion.

## Security

- RTSP and ONVIF on this camera take no password. Keep 554 and 8899 off the
  internet.
- `secrets.h`, `par_url.txt` and `browse_url.txt` hold the WiFi password and the
  bucket URLs. All three are gitignored; copy `secrets.example.h` and fill it in.
- A pre-authenticated request is a credential in URL form. It reaches one bucket
  only and can be revoked and reissued at any time.

## Licence

MIT.
