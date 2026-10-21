#!/usr/bin/env python3
"""
Live alarm listener for V380 / macro-video cameras, over the vendor MQTT push.

This is the channel the app actually uses to show alarms in real time (the REST
alarm-list is a dead end on this account). Flow, all reconstructed from the app:

  1) account login  -> access_token (atoken) + uid           [mapi.av380.net]
  2) message-center login  -> MQTT broker url + user/pass/cid [msglogin.av380.net:8443]
       POST /v1/app/login?signature=<md5(md5(body+"oplatformV1")+ts_ms)>&timestamp=<ts_ms>
       body {UID, atoken, pver:3, client:{...}}  -> code 5600, data{url,user,pass,cid,expired}
  3) MQTT connect (TLS) to that broker, SUBSCRIBE  UID/<uid>/message  (QoS 1)
       -> every camera alarm (motion / human / etc.) arrives here as it happens.

Run it, then walk in front of the camera; the pushed alarm prints immediately.

LEGITIMATE USE ONLY: your own account and camera.

    python mqtt_listen.py --user you@example.com --password SECRET
"""
import argparse
import hashlib
import json
import ssl
import sys
import time
import requests
import paho.mqtt.client as mqtt

sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")
from alarm_poll_v2 import account_login   # reuse the reconstructed login

MC_LOGIN_URL = "https://msglogin.av380.net:8443/v1/app/login"
HEADERS = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "okhttp/3.12"}


def mc_login(uid, atoken, verbose=False):
    """Message-center login -> MQTT broker + credentials (code 5600)."""
    client = {"AV": "2.2.90", "SV": "13", "BR": "samsung", "LN": "en",
              "sys": 1, "brand": 0, "appid": 2}
    body = {"UID": uid, "atoken": atoken, "pver": 3, "client": client}
    jstr = json.dumps(body, separators=(",", ":"))
    ts = int(time.time() * 1000)
    inner = hashlib.md5((jstr + "oplatformV1").encode()).hexdigest()
    sig = hashlib.md5((inner + str(ts)).encode()).hexdigest()
    url = f"{MC_LOGIN_URL}?signature={sig}&timestamp={ts}"
    r = requests.post(url, data=jstr, headers=HEADERS, timeout=12)
    r.raise_for_status()
    data = r.json()
    if verbose:
        print(f"[mc] {json.dumps(data)[:300]}", file=sys.stderr)
    if data.get("code") != 5600:
        raise SystemExit(f"message-center login failed: {data}")
    d = data["data"]
    return d["url"], d["user"], d["pass"], d["cid"], d.get("expired")


def parse_broker(url):
    # "mqtts://host:port"
    scheme, rest = url.split("://", 1)
    host, _, port = rest.partition(":")
    return host, int(port or (4883 if scheme == "mqtts" else 1883)), scheme == "mqtts"


def _make_client(cid, muser, mpass, use_tls):
    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid,
                      protocol=mqtt.MQTTv5, transport="tcp")
    cli.username_pw_set(muser, mpass)
    if use_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE      # vendor broker cert chain is not public
        cli.tls_set_context(ctx)
    return cli


def run(user, password, client_suffix="", verbose=False):
    atoken, uid, _ = account_login(user, password, verbose=verbose)
    if not atoken:
        raise SystemExit("account login failed")
    print(f"account ok: uid={uid}")
    topic = f"UID/{uid}/message"

    def on_message(c, u, msg):
        ts = time.strftime("%H:%M:%S")
        payload = msg.payload.decode("utf-8", "replace")
        print(f"\n[{ts}] <<< {msg.topic}\n{payload}", flush=True)
        try:
            print("   parsed:", json.dumps(json.loads(payload), ensure_ascii=False, indent=2)[:1500])
        except Exception:
            pass

    # Supervised loop: fresh message-center creds for every (re)connection, so the
    # broker never sees us with a stale/expired credential. A distinct client_id
    # suffix lets us coexist with the phone app instead of being kicked off.
    while True:
        burl, muser, mpass, cid, expired = mc_login(uid, atoken, verbose=verbose)
        host, port, use_tls = parse_broker(burl)
        use_cid = cid + client_suffix
        print(f"broker {host}:{port} tls={use_tls} client={use_cid} user={muser}", flush=True)

        cli = _make_client(use_cid, muser, mpass, use_tls)
        cli.on_message = on_message
        cli.on_connect = lambda c, u, f, r, p=None: (
            print(f"connected (reason={r}); subscribing {topic} + UID/{uid}/#", flush=True),
            c.subscribe(topic, qos=1), c.subscribe(f"UID/{uid}/#", qos=1))
        cli.on_disconnect = lambda c, u, f, r, p=None: print(f"disconnected (reason={r})", flush=True)

        try:
            cli.connect(host, port, keepalive=30)
        except Exception as e:
            print(f"connect error: {e}; retrying in 3s", flush=True)
            time.sleep(3)
            continue
        print("listening… (trigger motion; Ctrl-C to stop)", flush=True)
        cli.loop_forever()      # returns on disconnect; loop re-logins with fresh creds
        time.sleep(2)


def main():
    ap = argparse.ArgumentParser(description="Live V380 cloud alarm listener (MQTT)")
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--client-suffix", default="",
                    help="append to client id to avoid colliding with the phone app, e.g. _hub")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    run(args.user, args.password, client_suffix=args.client_suffix, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
