# V380 control protocol (port 8800) — what the decompiled app shows

This is read straight out of the decompiled SDK
(`com.macrovideo.sdk.defines.Protocol`, `...setting.DeviceConfigSetting`,
`...media.LoginHelper`). It is the structure your own client would speak to read
and write the camera's settings over the LAN. Nothing here was captured off the
wire — it is the app's own code.

## Packet frame (LAN mode)

A request is a 16-byte little-endian header followed by a body:

```
offset  size  field
0       4     cmd              (CMD_COMMON_IOT = 939722776 / 0x3804_0018)
4       1     version          (short s, usually 1)
5       1     secureType       (0 = plain, 1 or 2 = encrypted)
6       1     sessionId        (from login)
7       1     magicNum         (0)
8       4     (zero)
12      4     body length
16      ...   body             (the JSON, plain or encrypted)
```

(`Protocol.createCommonJsonRequestBuffer`. When the call is relayed through the
vendor cloud instead of the LAN, a 64-byte prefix is added — ignore that for a
local client.)

## Body JSON

`Protocol.createCommonJsonHeader` wraps every request the same way:

```json
{
  "id":        <random request id>,
  "method_id": <what you want to do>,
  "chn":       1,
  "ts":        <unix seconds>,
  "params":    { ... }
}
```

The reply carries the same `id` so you can match it to the request.

## method_id values that matter

| method_id | dec | does |
|---|---|---|
| ABILITY_REQUEST | 197386 | list which features this unit has |
| GET_PROPERTIES_REQUEST | 197376 | read settings (DPIDs) |
| SET_PROPERTIES_REQUEST | 197378 | write settings (DPIDs) |
| ACTION_REQUEST | 197384 | fire an action (e.g. PTZ, siren) |
| KEY_EXCHANGE_REQUEST | 196866 | set up the encrypted session |
| IOTC_LANPASS_REQUEST | 196870 | LAN-password auth |

So a settings client is mostly ABILITY (once) + GET_PROPERTIES / SET_PROPERTIES.

## params shape

`params` is keyed by DPID. Reading asks for a set of DPIDs; writing sends DPID →
value. The full DPID catalogue (119 of them — motion, floodlight, AI detection,
PTZ, SD card, …) and the id formula are in `CAMERA-API.md`. Example meanings:

```
DPID_ALARM_MT_SW              motion detection on/off
DPID_LIGHT_FLOODLIGHT_SW      the light it turns on when it sees movement
DPID_DET_OBJECT_SW            human/object detection on/off
DPID_ALARM_SOUND_SW           siren on/off
```

## The one piece that is NOT plain

`secureType` 1/2 runs the body through `HSMediaLibrary.cipherJsonData(...)`, a
native function in `libhsMediaLibrary.so`. It takes a 32-byte session key split
16 + 16 (key + IV shape). `DeviceConfigSetting` always calls with `secureType = 2`,
so on a current firmware the settings body is encrypted with a key established at
login, and the cipher lives in native code — not something to reproduce from the
Java alone.

## What this means for building your own app

You do **not** need to defeat that cipher. Two clean routes:

1. **Use the vendor SDK as-is.** The classes above (`LoginHelper.login*`,
   `DeviceConfigSetting`, `DeviceAlarmSetting.getAlarmConfig/setAlarmConfig`) are
   a normal Java API. A thin app that logs in with your own credentials and calls
   these does everything the V380 app does — floodlight, motion, AI, PTZ — and the
   native library does the crypto for you, legitimately, because you are logging
   in as the owner. This is by far the least work.

2. **Standard protocols for everything they cover.** RTSP for video, ONVIF for
   PTZ and device info. This needs no vendor code at all, but it does not reach
   the floodlight / AI toggles — those are 8800-only.

The realistic design is a mix: ONVIF + RTSP for the camera-agnostic parts, and the
vendor SDK (route 1) for the vendor-only features, all behind one UI you control.

Capturing the live traffic to confirm the exact `params` keys per DPID is the
natural next step whenever you actually implement route 1 — but the framing,
method ids and field names above already come straight from the app.
```
