# V380 camera — what it can do and how to reach it

Camera under test: `192.168.68.50`, ONVIF reports `macro-video-soft / IPCamera / firmware 2.4`.
App decompiled: V380 Pro 2.2.90 (`com.macrovideo.v380pro`).

Everything marked **verified** was tested against the real camera. Everything else
comes from reading the app's code and has not been confirmed on the wire yet.

## Network surface (verified by full 1–10000 port scan)

| Port | Protocol | Auth | Use |
|---|---|---|---|
| 554 | RTSP | **none** | video streams |
| 8800 | macro-video proprietary | login required | all the real settings |
| 8899 | ONVIF (SOAP/HTTP) | **none** | standard camera API |
| 9800 | macro-video proprietary | ? | probably media / P2P |

No password is needed for RTSP or ONVIF. Anyone on the LAN can watch the camera
and read its user list. Keep both off the internet.

## What works over standard protocols

### Video — verified

```
rtsp://192.168.68.50:554/live/ch00_0    H.265  2304x2592  ~14 fps  ~1.75 Mbps
rtsp://192.168.68.50:554/live/ch00_1    H.265  640x720    14 fps
```

The camera accepts any path and returns the same stream, so `/onvif1`, `/11`, `/`
all work too. ONVIF reports the encoding as H264 but it actually sends H.265 —
trust the stream, not the profile.

Two ONVIF profiles exist: `PROFILE_000` (main) and `PROFILE_001` (sub).

### PTZ — available, not yet exercised

ONVIF advertises a real PTZ configuration (`PTZCFG_000` / node `PTZNODE_000`), so
`ContinuousMove`, `AbsoluteMove`, `GotoPreset` on `http://192.168.68.50:8899/onvif/ptz_service`
should work. `GetPresets` currently returns none. Not tested, because it physically
moves the camera.

### Other working ONVIF calls — verified

`GetDeviceInformation`, `GetCapabilities`, `GetServices`, `GetProfiles`,
`GetStreamUri`, `GetUsers`, `CreatePullPointSubscription`, `PullMessages`.

## What ONVIF advertises but does NOT do

These are stubs. The names are in the service description, the implementation is not.

| Call / topic | Behaviour |
|---|---|
| `tns1:VideoSource/MotionAlarm` | never fires — not even the initial property value that every ONVIF property event must send on subscribe. Three minutes of deliberate motion produced nothing. |
| `GetSnapshotUri` → `/snapshot/PROFILE_000` | returns the URL, the URL returns **HTTP 500** |
| `GetImagingSettings` | **HTTP 400** |

Only two events ever arrive, both on subscribe:
`tns1:VideoSource/ImageTooDark` and `tns1:VideoSource/ImageTooBlurry`.

**So motion detection cannot be taken from ONVIF on this camera.** The camera does
detect motion — it drives the floodlight and the app's alerts — but that lives in
the proprietary protocol, not ONVIF. Our recorder therefore does its own detection
by differencing frames from the 640x720 sub stream, which is cheap.

## The proprietary protocol (port 8800)

This is where every interesting feature actually lives. From the decompiled SDK:

- Settings are **numbered properties** called DPIDs, 119 of them.
- A DPID is built as `((group & 0xFF) << 16) | (item & 0xFF) | 0x100`
  (`Protocol.MVSPT_IOTC_DPID`).
- Access goes through a session: the app first obtains a `LoginHandle`
  (`com.macrovideo.sdk.media.LoginHelper`), then calls helpers such as
  `DeviceAlarmSetting.getAlarmConfig(deviceInfo, loginHandle)` /
  `setAlarmConfig(...)` in `com.macrovideo.sdk.setting`.
- LAN discovery uses UDP 10008/10009 and multicast 10001/10002
  (`Defines.DEFAULT_SEND_PORT` / `DEFAULT_RECV_PORT` / `MULTICAST_*`).

The packet format on 8800 has not been worked out yet. That is the remaining
piece of work for a fully custom app.

### DPIDs worth having

Motion and alarm:

```
DPID_ALARM_MAIN_SW              master alarm switch
DPID_ALARM_MT_SW                motion detection on/off
DPID_ALARM_MT_SENSITIVITY       motion sensitivity
DPID_ALARM_MT_AREA              motion detection area
DPID_ALARM_SOUND_SW             alarm siren on/off
DPID_ALARM_SOUND_TYPE           which siren sound
DPID_ALARM_TIMER_INFO           alarm schedule
```

The light the camera turns on when it sees movement:

```
DPID_LIGHT_FLOODLIGHT_SW          floodlight on/off
DPID_LIGHT_FLOODLIGHT_STATUS      current state
DPID_LIGHT_FLOODLIGHT_LIGHTNESS   brightness
DPID_LIGHT_FLOODLIGHT_SENSITIVITY trigger sensitivity
DPID_LIGHT_SCHEDULE               schedule
DPID_LIGHT_INDICATOR_SW           the little status LED
```

Smart detection:

```
DPID_DET_OBJECT_SW            object detection on/off
DPID_DET_OBJECT_CONFIDENCE    confidence threshold
DPID_DET_OBJECT_MARK          draw boxes on the video
DPID_DET_AREA                 detection area
DPID_DET_MOVIING_SENS         movement sensitivity
DPID_DET_SOUND_SW             sound detection
DPID_DET_SENSOR_SW            PIR / temperature / humidity sensors
DPID_PT_OBJ_TRACKING_SW       auto-track a moving object
```

Image and recording:

```
DPID_VIDEO_ISP_BRIGHTNESS / CONTRAST / SATURATION / SHARPNESS
DPID_VIDEO_ISP_NIGHTVISION, DPID_VIDEO_ISP_ANTI_FLICKER_MODE
DPID_VIDEO_FLIP_MODE, DPID_VIDEO_ENCODING_FORMAT
DPID_RECORD_MODE / QUALITY / LOOP / AUDIO
DPID_SD_STATUS, DPID_SD_CAPACITY, DPID_SD_FORMAT_STATE
DPID_OSD_TIME_MARK_SW
```

Network, power, protocol switches:

```
DPID_NET_WIFI_STA_SSID, DPID_NET_STATIC_IP, DPID_NET_NETWORK_MODE
DPID_EXTPROT_ONVIF_SW         the toggle that opened ports 554 and 8899
DPID_EXTPROT_RTSP_AUDIO_SW    adds the audio track to RTSP
DPID_LOWPOWER_MODE, DPID_BATTERY_SOC, DPID_POWER_STATE
DPID_PT_SPEED_H / _V, DPID_PT_CRUISE_INFO, DPID_PT_PRESETS_USED_NUM
```

### Alert event types the firmware knows

From `IoTDevAbilityDefines._abl_alert_event_id` — these are the events the camera
can raise internally:

| id | event |
|---|---|
| 1 | motion |
| 2 | object |
| 3 | stay / loitering |
| 4 / 5 | e-fence enter / exit |
| 6 | line crossing |
| 7 | sound |
| 8 | active alarm |
| 9 | perception |
| 10 / 11 / 12 | sensor high / low / recovered |
| 1000–1004 | video call, doorbell, battery low, power on, power off |

Objects it can classify: `ANYMOUS` (any moving thing) and `HUMAN`.
Sensors it supports: temperature, humidity, PIR, animal PIR.

Note these are what the *firmware family* supports. Which ones your specific unit
has is reported by a capability bitmask (`IoTDevAbilityDefines`, values built with
`BMPC(n)` = bit n). The app hides any control whose bit is not set — that is
exactly how the ONVIF row stayed hidden until it was enabled.

## Building your own app

Three tiers, cheapest first:

1. **Live video, recording, PTZ** — use RTSP + ONVIF. Standard, documented, works
   today, any language. This covers most of what a viewer app needs.
2. **Motion detection** — do it yourself on the sub stream, as `cctv-vps/recorder.py`
   does. Independent of the camera's quirks and it also works on any other camera.
3. **Floodlight, sirens, AI detection, sensitivity, schedules** — only reachable
   over port 8800. Needs the wire format to be reverse engineered: capture the app
   talking to the camera on the LAN, match the bytes against `LoginHelper` and
   `DeviceAlarmSetting` in the decompiled source, and work out the framing.

Tier 3 is a real project on its own, but it is tractable — the field names,
property IDs and call structure are all already readable in
`v380-re/jadx-out/sources/com/macrovideo/sdk/`.
