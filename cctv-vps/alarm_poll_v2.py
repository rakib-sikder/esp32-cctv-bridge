#!/usr/bin/env python3
"""
Fetch V380 / macro-video cloud alarm events via the NEWER account (V2) feed.

The legacy V1 feed (alarm_poll.py) authenticates per-device and came back empty
for this camera; the app actually reads events from the account-scoped V2 feed
`/api/v2/data/alarm/list`, which needs an account uid + access token. This script
logs in to the vendor account (your own email + password), gets uid + token, and
queries the alarm list — exactly the sequence the app performs.

Reconstructed from the decompiled app (all standard, no native crypto):

  1) Account login  (OkHttpUtil.accountLogin -> doPostRequest "user/login")
     POST https://mapi.av380.net/user/login   (application/json)
     body = {
       username, password = hex(AES-128-ECB-PKCS5(password, "1234567890123456")),
       phonetoken = "100", timestamp,
       sign = md5("password=<hex>&phonetoken=100&timestamp=<ts>&username=<u>hsshop2016")
     }
     reply -> access_token (GlobalDefines.sAccessToken) + user id (sLoginUserId)

  2) Alarm list V2  (PushMessageUtils.a -> GET_MESSAGE_LIST_V20_V2)
     POST https://alarmlist.av380.net/api/v2/data/alarm/list   (application/json)
     body = {
       dev_id, start_time, end_time, token(=access_token), count, uid, alarm_type,
       asc, filter_tag:[], filter_summary:"",
       sign = md5("alarm_type=..&asc=..&count=..&dev_id=..&end_time=..&start_time=.."
                  "&token=..&uid=..hsdata2021")
     }
     reply -> { result, desc, value:[events] }  (ok: result>0 && desc==100)

The two md5 salts ("hsshop2016", "hsdata2021") and the AES key are hardcoded in
the app. This logs in as the account owner to read that account's own events.

LEGITIMATE USE ONLY: your own account and camera.

Usage:
  python alarm_poll_v2.py --did <DEVICE_ID> --user you@example.com --password SECRET
  python alarm_poll_v2.py --did <DEVICE_ID> --user ... --password ... --watch 10
"""
import argparse
import hashlib
import json
import sys
import time
import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

LOGIN_URL = "https://mapi.av380.net/user/login"
ALARM_V2_URL = "https://alarmlist.av380.net/api/v2/data/alarm/list"
AES_KEY = b"1234567890123456"          # Defines.sEncryptionKey
LOGIN_SALT = "hsshop2016"              # accountLogin sign salt
ALARM_SALT = "hsdata2021"             # getAlarmMsgListV2 sign salt
HEADERS = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "okhttp/3.12"}


def _md5(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _aes_ecb_hex(plaintext):
    """hex(AES-128-ECB-PKCS5(plaintext, key)) — GlobalDefines.encrypt + byte2Hex."""
    padder = PKCS7(128).padder()
    data = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(AES_KEY), modes.ECB()).encryptor()
    return (enc.update(data) + enc.finalize()).hex()


def account_login(user, password, timeout=10, verbose=False):
    ts = int(time.time())
    pwd_hex = _aes_ecb_hex(password)
    sign = _md5(f"password={pwd_hex}&phonetoken=100&timestamp={ts}&username={user}{LOGIN_SALT}")
    body = {"username": user, "password": pwd_hex, "phonetoken": "100",
            "sign": sign, "timestamp": ts}
    if verbose:
        print(f"[login] POST {LOGIN_URL}", file=sys.stderr)
    r = requests.post(LOGIN_URL, data=json.dumps(body), headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if verbose:
        print(f"[login] reply: {json.dumps(data)[:400]}", file=sys.stderr)
    # field names vary; pull token + uid defensively
    token = _dig(data, ("access_token", "accessToken", "token"))
    uid = _dig(data, ("user_id", "userId", "uid", "id"))
    # often nested under "data"/"value"/"result"
    if token is None or uid is None:
        for k in ("data", "value", "result", "info", "user"):
            sub = data.get(k) if isinstance(data, dict) else None
            if isinstance(sub, dict):
                token = token or _dig(sub, ("access_token", "accessToken", "token"))
                uid = uid if uid is not None else _dig(sub, ("user_id", "userId", "uid", "id"))
    return token, uid, data


def _dig(d, keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def fetch_alarms_v2(did, token, uid, count=10, start_time=0, end_time=None,
                    alarm_type=0, asc=0, timeout=10, verbose=False):
    if end_time is None:
        end_time = int(time.time() * 1000)
    # sign string uses the params in alphabetical key order + salt
    sign_src = (f"alarm_type={alarm_type}&asc={asc}&count={count}&dev_id={did}"
                f"&end_time={end_time}&start_time={start_time}&token={token}"
                f"&uid={uid}{ALARM_SALT}")
    body = {
        "dev_id": did, "start_time": start_time, "end_time": end_time,
        "token": token, "count": count, "uid": uid, "alarm_type": alarm_type,
        "asc": asc, "filter_tag": [], "filter_summary": "",
        "sign": _md5(sign_src),
    }
    if verbose:
        print(f"[alarm] POST {ALARM_V2_URL}", file=sys.stderr)
    r = requests.post(ALARM_V2_URL, data=json.dumps(body), headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()


def main():
    ap = argparse.ArgumentParser(description="Fetch V380 cloud alarm events (account V2 feed)")
    ap.add_argument("--did", required=True, type=int)
    ap.add_argument("--user", required=True, help="account email / username")
    ap.add_argument("--password", required=True)
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--days", type=float, default=7, help="look back this many days")
    ap.add_argument("--watch", type=float, default=0, help="poll every N seconds")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    token, uid, raw = account_login(args.user, args.password, verbose=args.verbose)
    if not token or uid is None:
        print("login did not return a token/uid. Raw reply:")
        print(json.dumps(raw, indent=2, ensure_ascii=False)[:1500])
        return 2
    print(f"logged in: uid={uid}  token={str(token)[:12]}…")

    start_time = int((time.time() - args.days * 86400) * 1000)
    seen = set()
    while True:
        resp = fetch_alarms_v2(args.did, token, uid, count=args.count,
                               start_time=start_time, verbose=args.verbose)
        result = resp.get("result")
        desc = resp.get("desc")
        value = resp.get("value") or []

        if args.json:
            print(json.dumps(resp, indent=2, ensure_ascii=False))
        elif not value:
            print(f"result={result} desc={desc} — no events in window")
        else:
            print(f"result={result} desc={desc} — {len(value)} event(s):")
            for ev in value:
                k = json.dumps(ev, sort_keys=True)
                tag = "  <-- NEW" if k not in seen else ""
                seen.add(k)
                print(f"  {json.dumps(ev, ensure_ascii=False)}{tag}")

        if args.watch <= 0:
            break
        time.sleep(args.watch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
