#!/usr/bin/env python3
"""
V380 device key-exchange over MQTT (pver>=3) — step toward LAN-free record access.

Creates a relay channel (/v1/app/create), then publishes an X25519 key-exchange to
the camera over its device-MQTT (wakeup topic), encrypted with the device message
cipher. Prints whatever the camera publishes back so we can read its public key.

Own account / own camera only.
"""
import base64, hashlib, hmac, json, os, ssl, struct, sys, time
import requests
import paho.mqtt.client as mqtt
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")
from alarm_poll_v2 import account_login, _md5
from device_listen import device_list, dev_mqtt_login, DEV_MQTT_HOSTS
from mqtt_listen import parse_broker

H = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "okhttp/3.12"}


def dispatch(did):
    ts = int(time.time() * 1000)
    src = f"dev_id={did}&platform=20001&timestamp={ts}hsdata2022"
    b = {"dev_id": did, "platform": 20001, "timestamp": ts,
         "sign": hashlib.sha1(src.encode()).hexdigest()}
    r = requests.post("http://dispatch.av380.net:8001/api/v1/get_stream_server",
                      data=json.dumps(b), headers=H, timeout=10).json()
    return r["data"]


def create_channel(tok, did, relay_ip, call_port, dev_ip=""):
    ts = int(time.time())
    body = {"params": {"dev_ip": dev_ip, "no_cascade": 0},
            "mr": {"params": {"access_token": tok, "device_id": did}}, "timestamp": ts}
    bj = json.dumps(body, separators=(",", ":"))
    sig = _md5(_md5(bj + str(ts)) + "2021Mr")
    hh = dict(H); hh["Signature"] = sig
    r = requests.post(f"http://{relay_ip}:{call_port}/v1/app/create",
                      data=bj, headers=hh, timeout=10).json()
    return r.get("mr", {}).get("result", {})


def _totp(key, t_sec=None, step=30, digits=6):
    if t_sec is None:
        t_sec = int(time.time())
    msg = struct.pack(">Q", t_sec // step)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    off = h[-1] & 0x0F
    binc = ((h[off] & 0x7F) << 24) | ((h[off + 1] & 0xFF) << 16) | \
           ((h[off + 2] & 0xFF) << 8) | (h[off + 3] & 0xFF)
    return binc % (10 ** digits)


def _tag(seg):
    if seg.startswith("CID") and len(seg) > 3:
        return "D" + seg[3:]
    if seg.startswith("UID") and len(seg) > 3:
        return "U" + seg[3:]
    return None


def new_message_load(topic_dev, topic_user, rand_key, payload_str, flag=0, props_len=0):
    """Port of MessagePayloadTools.newMessageLoad (encrypt)."""
    sha1key = hashlib.sha1((rand_key + "2025&N+E+W" + topic_dev[3:] + "&" + topic_user[3:]).encode()).digest()
    totp = _totp(sha1key)
    mt = _tag(topic_dev); mt2 = _tag(topic_user)
    key = hashlib.md5((rand_key + mt + mt2 + str(totp)).encode()).digest()
    p = PKCS7(128).padder()
    data = p.update(payload_str.encode()) + p.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    ct = enc.update(data) + enc.finalize()
    hdr = bytes([2, flag & 0xFF]) + struct.pack("<I", totp) + struct.pack("<H", props_len)
    return hdr + ct


def decrypt_msg(payload, topic, rand_key):
    tags = [t for t in (_tag(s) for s in topic.split("/")) if t]
    mt = tags[0] if tags else ""
    mt2 = tags[1] if len(tags) > 1 else ""
    if len(payload) < 8 or payload[0] != 2:
        return None
    totp = 0
    for i in (5, 4, 3, 2):
        totp = (totp << 8) | payload[i]
    plen = (payload[6] << 8) | payload[7]
    body = payload[8:len(payload) - plen]
    key = hashlib.md5((rand_key + mt + mt2 + str(totp)).encode()).digest()
    try:
        raw = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
        out = raw.update(body) + raw.finalize()
        out = PKCS7(128).unpadder().update(out) + PKCS7(128).unpadder().finalize() if False else out
        try:
            u = PKCS7(128).unpadder(); out = u.update(out) + u.finalize()
        except ValueError:
            pass
        return out.decode("utf-8", "replace")
    except Exception as e:
        return f"<decrypt err {e}>"


def main():
    user, pw = sys.argv[1], sys.argv[2]
    tok, uid, _ = account_login(user, pw)
    devs = device_list(tok)
    d = devs[0]
    did = int(d["device_id"]); rand_key = d["rand_key"]
    print(f"did={did} rand_key={rand_key} uid={uid}")

    relays = dispatch(did)
    relay = relays[0]
    ch = create_channel(tok, did, relay["ip"], relay["tp"]["call_port"])
    print(f"channel: handle={ch.get('channel_handle')} uuid={ch.get('uuid')} trs={ch.get('trs_ip')}:{ch.get('trs_port')}")

    burl, cid, muser, mpass = dev_mqtt_login(DEV_MQTT_HOSTS[0], uid, tok)
    bhost, bport, tls = parse_broker(burl)

    priv = X25519PrivateKey.generate()
    pub = priv.public_key().public_bytes_raw()
    state = {"skey": None}

    kx = {"cmd": 2, "id": 1,
          "turn": {"host": ch.get("trs_ip"), "port": int(ch.get("trs_port") or 32100),
                   "hdl": ch.get("channel_handle")},
          "auth": {"cid": did, "uuid": ch.get("uuid"),
                   "pubKC": base64.b64encode(pub).decode(), "uid": uid}}
    pub_topic = f"CID{did}/UID{uid}/wakeup"
    payload = new_message_load(f"CID{did}", f"UID{uid}", rand_key, json.dumps(kx, separators=(",", ":")))

    def on_connect(c, u, f, r, p=None):
        print(f"mqtt connected ({r}); subscribing")
        c.subscribe([(f"CID{did}/#", 1), (f"CID/CID{did}/#", 1),
                     (f"UID{uid}/#", 1), (f"UID/UID{uid}/#", 1)])
        time.sleep(0.5)
        print(f"publishing key-exchange to {pub_topic} ({len(payload)} bytes)")
        c.publish(pub_topic, payload, qos=1)

    def on_message(c, u, msg):
        txt = decrypt_msg(msg.payload, msg.topic, rand_key)
        print(f"\n<<< {msg.topic} ({len(msg.payload)}B)\n   {txt}")
        if txt and ("pubK" in txt or "dK" in txt):
            try:
                obj = json.loads(txt)
                # find a base64 32-byte key field
                for k, v in _walk(obj):
                    if k in ("pubKD", "dK", "pubK", "key") and isinstance(v, str):
                        dk = base64.b64decode(v)
                        if len(dk) == 32:
                            sk = priv.exchange(X25519PublicKey.from_public_bytes(dk))
                            state["skey"] = sk
                            print(f"   *** sKey = {sk.hex()}  (from {k})")
            except Exception as e:
                print("   parse err", e)

    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid, protocol=mqtt.MQTTv5)
    cli.username_pw_set(muser, mpass)
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    cli.tls_set_context(ctx)
    cli.on_connect = on_connect
    cli.on_message = on_message
    cli.connect(bhost, bport, 30)
    cli.loop_start()
    time.sleep(20)
    cli.loop_stop()
    print("\n[done]", "sKey established" if state["skey"] else "no sKey")


def _walk(o, pfx=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k, v
            yield from _walk(v, k)
    elif isinstance(o, list):
        for x in o:
            yield from _walk(x, pfx)


if __name__ == "__main__":
    main()
