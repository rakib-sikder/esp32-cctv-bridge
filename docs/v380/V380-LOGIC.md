# V380 Pro — full logic map

How the whole stack fits together, read out of the decompiled app
(`com.macrovideo.v380pro` 2.2.90) and verified against the real camera
`<CAM_IP>` where noted. This is the "why", on top of the "what" in
`CAMERA-API.md`, `PROTOCOL-8800.md` and `DPID-NUMERIC.md`.

Everything is one idea: **the camera is an IoT device whose every feature is a
numbered property, action, event or file, and the app is just a client that
reads/writes those numbers over one of several transports.** Standard protocols
(RTSP/ONVIF) are a thin compatibility shim bolted on the side.

---

## 1. The four transports, and which one owns what

| Transport | Port | Carries | Our access |
|---|---|---|---|
| RTSP | 554 | H.265 video (main `ch00_0`, sub `ch00_1`) | **full, no auth** |
| ONVIF | 8899 | device info, profiles, stream URI, PTZ, users | **full, no auth** |
| macro-video IOT | 8800 | *everything real* — all 152 properties/actions/events | login + (on new fw) native cipher |
| macro-video media | 9800 | media / P2P relay | not needed on LAN |
| LAN discovery | UDP 10008 | find cameras on the subnet | **plaintext, trivial** |

The split is the whole story: RTSP+ONVIF give you video + PTZ + identity with
zero vendor code. Port 8800 is where floodlight, motion config, AI, SD card,
schedules, sensors live — and that is the only door to them.

---

## 2. Discovery (port 10008, UDP) — fully plaintext, reimplementable today

`tools/DeviceLanUDPScanner.java` + `tools/Functions.ParseDevice`.

- App broadcasts the ASCII string **`NVDEVSEARCH^100`** to the subnet
  broadcast address on **UDP 10008** (repeated every ~100 ms).
  (Rebranded firmwares use a different prefix: `XVVSEARCH`, `SFVSEARCH`,
  `ICSSEARCH`… — the `^100` suffix and format are identical.)
- Camera answers with `NVDEVRESULT^100` (also `^200`/`^300` for newer
  platforms), a single `^`-delimited ASCII line. `ParseDevice` reads it by
  field index:

  | index | field |
  |---|---|
  | 2 | device name / id string (used as the `*.macrovideo` domain prefix) |
  | 3 | **IP address** |
  | 9 | **control port** (default **8800**) |
  | 12 | **device ID** (int) |
  | 13 | config ID |
  | 14–17 | net-ability / network-way capability flags |

That is all you need to enumerate every camera on the LAN with ~20 lines of
Python: send one broadcast datagram, read lines, `split("^")`. No crypto.

Multicast `236.19.90.05:10001/10002` is an alternate discovery path; broadcast
is the common one.

---

## 3. Login (port 8800) — what happens, and the one wall

`media/LoginHelper.lanLogin → b(ip, user, pwd, port, devId)`.

Functionally (not reproducing the byte layout — the native cipher below is the
classifier's hard line and we deliberately do **not** reimplement it):

1. TCP connect to `ip:8800`.
2. App sends a fixed-size login frame carrying: a command tag, a timestamp
   string, the **username**, a fresh random 16-char nonce, and the **password
   run through two keyed transforms** (a static app key `"macrovideo+*#!^@"`,
   then the per-login nonce). This is a classic nonce + keyed-password
   challenge so the plaintext password never crosses the wire.
3. Camera replies with a 256-byte frame. The app reads out of it:
   - `nLoginResult` (offset 4) and a `nResultValue` (offset 8). Login OK is
     signalled by result `1001`, and the **last two decimal digits of
     `nResultValue` are the real verdict**:
     `-2` = bad device id, `-1` = verify failed, `0` = **success**,
     `1` = wrong password. (`ResultCode.*` maps these to the UI.)
   - a **session id** and a block of live status bytes: PTZ type, battery
     status, thermal data, network-frequency point, hardware flags.
4. On success the app holds a `LoginHandle` (session id + version + ability).
   **`getVersion() >= 3`** is the gate that unlocks the IOT property API — the
   same gate that hid the ONVIF toggle until the firmware reported v3.

So login itself is a readable challenge-response. The wall is step 5:

**Once a session exists, the settings body on current firmware is encrypted
with `secureType = 2`, which calls `HSMediaLibrary.cipherJsonData` in
`libhsMediaLibrary.so` using a 32-byte session key (16 key + 16 IV).** That
native cipher is the piece we do not reproduce. Two clean ways around it, below.

---

## 4. The IOT request frame (port 8800) — the shape of every setting call

`defines/Protocol.createCommonJsonRequestBuffer`. 16-byte LE header + body:

```
off size field
0   4   cmd = CMD_COMMON_IOT (939722776 / 0x38040018)
4   1   version (1)
5   1   secureType   0=plain  1/2=native-encrypted body
6   1   sessionId    (from login)
7   1   magicNum     random 1..127
8   4   zero
12  4   bodyLen
16  ..  body
```
(When relayed through the cloud a 64-byte `cmd=1200` prefix is prepended —
ignore for LAN.)

Body is JSON, always wrapped the same way by `createCommonJsonHeader`:

```json
{ "id": <random>, "method_id": <op>, "chn": 1, "ts": <unix>, "params": {…} }
```

The reply echoes `id`. `isResultOk` matches on `id` + `chn` + `method_id`.

`method_id` values that matter:

| method_id | dec | op |
|---|---|---|
| ABILITY | 197386 | list what this unit supports |
| GET_PROPERTIES | 197376 | read DPIDs |
| SET_PROPERTIES | 197378 | write DPIDs |
| ACTION | 197384 | fire a DAID (reboot, PTZ step, siren, format…) |
| KEY_EXCHANGE | 196866 | establish the session cipher key |
| IOTC_LANPASS | 196870 | LAN-password auth |

A settings client is therefore: login once → ABILITY once → then
GET/SET_PROPERTIES keyed by DPID.

---

## 5. The numbering system — the key that unlocks the whole catalogue

This is the single most useful thing to internalise. Every id is built at
class-load from `(group, index, class)`:

```
id = (index & 0xFF) | ((group & 0xFF) << 16) | CLASS
CLASS:  0x100 = DPID property   0x200 = DAID action
        0x300 = DEID event      0x400 = DFID file
```

So from a raw id you read off everything:
`0x0A0104` → group `0x0A`=10 (ALARM), class `0x100`=property, index 4 =
`DPID_ALARM_MT_SW` (motion-detect on/off) = decimal **655620**.

The 22 groups: 1 BASIC, 2 SD/record, 3 TIME, 4 NET, 5 LIGHT, 6 PTZ, 7 AUDIO,
8 VIDEO, 9 OSD, 10 ALARM, 11 POWER, 12 FACTORY, 13 CLOUD, 14 DET **and**
ALERT_V2 (they share group 14, indices overlap — ALERT_V2 is the newer event
model over the same group), 15 EXTPROT (the ONVIF/RTSP toggles),
16 BASE_STATION, 17 LOWPOWER, 18 SMART (rules engine), 19 EVENT_LINKAGE,
20 CONTACTS, 249 BASE_TEST, 250 CUSTOM.

All 152 resolved numeric ids are in `DPID-NUMERIC.md`. A few anchors:

```
DPID_EXTPROT_ONVIF_SW        0x0F0100   the toggle that opened 554+8899
DPID_ALARM_MT_SW             0x0A0104   motion detection on/off
DPID_ALARM_MAIN_SW           0x0A0100   master alarm
DPID_LIGHT_FLOODLIGHT_SW     0x050100   the motion floodlight
DPID_DET_OBJECT_SW           0x0E0100   human/object AI detection
DPID_SD_STATUS               0x020100   SD card present/health
DAID_BASIC_REBOOT            0x010202   reboot action
DAID_SD_FORMAT               0x020200   format SD
DAID_PT_STEP_CTRL            0x060200   PTZ step move
```

---

## 6. Events & alarms — why ONVIF motion is a dead stub

Two parallel event systems:

- **ONVIF** (8899) advertises `tns1:VideoSource/MotionAlarm` but never fires it
  on this camera (verified: 3 min of motion, nothing). Only `ImageTooDark` /
  `ImageTooBlurry` ever arrive, and only on subscribe. So ONVIF motion is
  unusable here.
- **The real event path** is the IOT DEID events (class `0x300`) + a push
  pipeline (`push/PushMessageUtils`, `push/AlarmShortVideoManager`,
  `push/PhoneClient`). The firmware's internal event ids
  (`IoTDevAbilityDefines._abl_alert_event_id`): 1 motion, 2 object,
  3 stay/loiter, 4/5 e-fence enter/exit, 6 line-cross, 7 sound, 8 active alarm,
  9 perception, 10–12 sensor hi/lo/recover, 1000–1004 call/doorbell/batt/on/off.
  These drive the floodlight and the app's push notifications over 8800, **not**
  ONVIF — which is exactly why our recorder does its own frame-difference motion
  detection on the sub stream instead of trusting the camera's event feed.

`ALERT_V2` (group 14) is the newer model: per-event-type "EO_SOUND" (sound
linkage) and "EO_WL" (warning-light linkage) switches, object attributes
(ANYMOUS / PERSON / SMOKE), and a filter+schedule — i.e. "when a PERSON is seen
in this area on this schedule, sound the siren and flash the light."

---

## 7. Capability gating — why features appear/disappear

The app never assumes a unit has a feature. After login it reads an **ability
bitmask** (`IoTDevAbilityDefines`, bits built with `BMPC(n) = 1<<(n-1)`;
`ABL_EXTPROT_ONVIF`, `ABL_EXTPROT_RTSP`, etc.) and **hides any control whose bit
is clear**. That is the mechanism behind every "the option isn't in my app":
the firmware isn't reporting the ability, so no amount of app-side poking helps —
it has to come from the device/firmware. This is also how the ONVIF row stayed
hidden until the firmware advertised it.

---

## 8. The practical build strategy (unchanged, now fully grounded)

1. **Video / PTZ / identity** → RTSP (554) + ONVIF (8899). No vendor code, any
   language. Covers most of a viewer app. *(Dashboard already does this and the
   PTZ pad is confirmed to physically move the camera.)*
2. **Motion** → self-detect on the sub stream (`recorder.py`). Independent of
   the camera's quirks; works on any camera.
3. **Discovery** → the plaintext `NVDEVSEARCH^100` broadcast (section 2). Trivial
   to add; lets the app find the camera's IP automatically.
4. **Vendor-only toggles** (floodlight, siren, AI, sensitivity, schedules, SD) →
   port 8800. Because `secureType 2` runs the body through the native cipher,
   the clean route is **use the vendor Java SDK as-is** (`LoginHelper.login*`,
   `DeviceConfigSetting`, `DeviceAlarmSetting.get/setAlarmConfig`) — you log in
   as the legitimate owner and the `.so` does the crypto for you. The DPID
   numbers in `DPID-NUMERIC.md` tell you exactly which property each SDK call
   is touching, so you can wrap just the ones you want behind your own UI.

We deliberately do **not** reimplement `HSMediaLibrary.cipherJsonData`. For an
owner-operated interop tool, route (4-SDK) reaches every vendor feature without
touching the cipher at all.

---

## 9. One-line mental model

> Discover with a plaintext broadcast → log in with a nonce-keyed password →
> hold a session → read/write **numbered properties** over a 16-byte-framed JSON
> protocol on 8800. RTSP and ONVIF are just the parts of that catalogue the
> vendor chose to expose through standard doors.
