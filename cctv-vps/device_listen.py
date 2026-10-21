#!/usr/bin/env python3
"""
Receive the CAMERA's OWN real-time events (motion / alarm / status) over the
V380 device-MQTT channel — the same push the app uses, not our own detection.

Reconstructed from the decompiled app (MqttHelper + MessagePayloadTools),
all standard crypto, no native code:

  1) account login            -> access_token (atoken) + uid      [mapi.av380.net]
  2) device/list              -> dev_id + rand_key per device     [mapi.av380.net]
  3) device-MQTT login        -> broker url + user/pass/cid        [gmt*cli.av380.net:8443]
       POST /v1/app/login?signature=<md5(md5(body+"oplatformV1")+ts_ms)>&timestamp=<ts_ms>
       body {UID, atoken, pver:3, client:{AV,SV,BR}}  -> data{url,cid,user,pass}
  4) MQTT connect (TLS), subscribe the device's topics under CID<dev_id> / UID<uid>
  5) each pushed message is decrypted with MessagePayloadTools.DecryptDeviceCipherText:
       hdr: [0]=2(AES-CBC) [1]=flag  [2..5]=totp(LE int)  [6..7]=propsLen(BE)
       body = bytes[8 : len-propsLen]
       key = iv = md5( rand_key + "D"<dev_id> + "U"<uid> + str(totp) )   (16 raw bytes)
       plaintext = AES-128-CBC-PKCS7-Decrypt(body, key, iv)  -> JSON

Close the V380 app on the phone first: the broker binds the client-id, so the
phone and this tool cannot both hold it.

LEGITIMATE USE ONLY: your own account and camera.

    python device_listen.py --user you@example.com --password SECRET
"""
import argparse
import hashlib
import json
import ssl
import sys
import time
import requests
import paho.mqtt.client as mqtt
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")
from alarm_poll_v2 import account_login, _md5
from mqtt_listen import parse_broker

HEADERS = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "okhttp/3.12"}
# Device-MQTT access servers (MqttHelper): Guangzhou + Hong Kong.
DEV_MQTT_HOSTS = ["gmtgzcli.av380.net:8443", "gmthkcli.av380.net:8443"]


def device_list(token, verbose=False):
    ts = int(time.time())
    src = (f"accesstoken={token}&from_app=V380Pro&get_sub_server=20&language=en"
           f"&registrationid=&timestamp={ts}&type=all&ver=1008hsshop2016")
    body = {"accesstoken": token, "from_app": "V380Pro", "get_sub_server": 20,
            "language": "en", "registrationid": "", "sign": _md5(src),
            "timestamp": ts, "type": "all", "ver": 1008}
    r = requests.post("https://mapi.av380.net/device/list", data=json.dumps(body),
                      headers=HEADERS, timeout=12)
    r.raise_for_status()
    return r.json().get("data", [])


def dev_mqtt_login(host, uid, atoken, verbose=False):
    body = {"UID": uid, "atoken": atoken, "pver": 3,
            "client": {"AV": "2.2.90", "SV": "13", "BR": "samsung"}}
    jstr = json.dumps(body, separators=(",", ":"))
    ts = int(time.time() * 1000)
    sig = hashlib.md5((hashlib.md5((jstr + "oplatformV1").encode()).hexdigest()
                       + str(ts)).encode()).hexdigest()
    url = f"https://{host}/v1/app/login?signature={sig}&timestamp={ts}"
    r = requests.post(url, data=jstr, headers=HEADERS, timeout=12)
    r.raise_for_status()
    data = r.json()
    if verbose:
        print(f"[devmq {host}] {json.dumps(data)[:300]}", file=sys.stderr)
    d = data.get("data") or {}
    if not d.get("url"):
        raise RuntimeError(f"no broker in reply: {data}")
    return d["url"], d.get("cid"), d.get("user"), d.get("pass")


def _topic_tag(seg):
    if seg.startswith("CID") and len(seg) > 3:
        return "D" + seg[3:]
    if seg.startswith("UID") and len(seg) > 3:
        return "U" + seg[3:]
    return None


def decrypt_payload(payload, topic, rand_key):
    """Port of MessagePayloadTools.DecryptDeviceCipherText."""
    tags = [t for t in (_topic_tag(s) for s in topic.split("/")) if t]
    mt = tags[0] if len(tags) > 0 else ""
    mt2 = tags[1] if len(tags) > 1 else ""
    if len(payload) < 8 or payload[0] != 2:
        return None
    totp = 0
    for i2 in (5, 4, 3, 2):
        totp = (totp << 8) | payload[i2]
    props_len = (payload[6] << 8) | payload[7]
    body = payload[8:len(payload) - props_len]
    key = hashlib.md5((rand_key + mt + mt2 + str(totp)).encode()).digest()  # 16 bytes
    dec = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
    raw = dec.update(body) + dec.finalize()
    try:
        unp = PKCS7(128).unpadder()
        raw = unp.update(raw) + unp.finalize()
    except ValueError:
        pass
    return raw.decode("utf-8", "replace")


def run(user, password, verbose=False):
    atoken, uid, _ = account_login(user, password, verbose=verbose)
    if not atoken:
        raise SystemExit("account login failed")
    devs = device_list(atoken, verbose=verbose)
    if not devs:
        raise SystemExit("no devices on this account")
    for d in devs:
        print(f"device {d.get('device_id')} '{d.get('nickname')}' rand_key={d.get('rand_key')}")
    keys = {int(d["device_id"]): d.get("rand_key", "") for d in devs}
    dids = list(keys)

    # device-MQTT login (try each regional broker)
    burl = cid = muser = mpass = None
    for host in DEV_MQTT_HOSTS:
        try:
            burl, cid, muser, mpass = dev_mqtt_login(host, uid, atoken, verbose=verbose)
            print(f"device-mqtt via {host}: broker={burl} client={cid}")
            break
        except Exception as e:
            print(f"devmq {host} failed: {e}")
    if not burl:
        raise SystemExit("device-mqtt login failed on all hosts")

    bhost, bport, tls = parse_broker(burl)

    def on_connect(c, u, flags, reason, props=None):
        print(f"connected (reason={reason})")
        subs = []
        for did in dids:
            subs += [(f"CID{did}/#", 1), (f"CID/CID{did}/#", 1)]
        subs += [(f"UID{uid}/#", 1), (f"UID/UID{uid}/#", 1)]
        c.subscribe(subs)
        print("subscribed:", [s[0] for s in subs])

    def on_message(c, u, msg):
        ts = time.strftime("%H:%M:%S")
        # pick the rand_key of whichever device id appears in the topic
        rk = ""
        for did, k in keys.items():
            if str(did) in msg.topic:
                rk = k
                break
        print(f"\n[{ts}] <<< {msg.topic}  ({len(msg.payload)} bytes)")
        if rk:
            txt = decrypt_payload(msg.payload, msg.topic, rk)
            if txt:
                print("   decrypted:", txt[:1500])
                return
        print("   raw:", msg.payload[:64].hex())

    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid, protocol=mqtt.MQTTv5)
    if muser:
        cli.username_pw_set(muser, mpass)
    if tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        cli.tls_set_context(ctx)
    cli.on_connect = on_connect
    cli.on_message = on_message
    cli.on_disconnect = lambda c, u, f, r, p=None: print(f"disconnected (reason={r})")
    cli.connect(bhost, bport, keepalive=30)
    print("listening… trigger motion in front of the camera (Ctrl-C to stop)")
    cli.loop_forever()


def main():
    ap = argparse.ArgumentParser(description="Receive V380 camera's own events over device-MQTT")
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    run(args.user, args.password, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
