#!/usr/bin/env python3
"""
Fetch the camera's OWN alarm events from the V380 / macro-video cloud.

This is the SAME feed the vendor app's "alarm messages" screen shows: the
camera's built-in motion / AI / human detection fires, uploads an event to the
vendor cloud, and clients read it back from an HTTPS endpoint. Unlike the on-box
8800 control channel (native-encrypted), this cloud feed is NOT encrypted — the
app's `NVCryptor` turns out to be plain Base64, and auth is just the account
username + md5(password). So it is fully reproducible with stdlib.

Reconstructed from `com.macrovideo.sdk.push.PushMessageUtils.getLatestAlarmMessage`:

    POST https://pushregcheck.nvcam.net:443/GetSSAlarmMsg/NVSSGetAlarmNewMessageList
    body (x-www-form-urlencoded):
        param = base64_nowrap(json_request)
        type  = "1"
    json_request = {
        ftime, did, count, type, key, usr(=base64 of username),
        pwd(=md5 of password), version, filter_tag, filter_summary
    }
    response body = base64( json_response )   # NVCryptor.Decode = base64 decode
    json_response = { result, desc, value:[ alarm events ] }   # ok: result>0 & desc==100

WHAT YOU NEED: the V380 account credentials the camera is bound to, plus the
device id (discover.py prints it — e.g. <DEVICE_ID>). This talks to the
vendor cloud over the internet; it is not a LAN-only path.

LEGITIMATE USE ONLY: your own camera, your own account. This logs in as the
owner to read your own events — nothing is bypassed.

Usage:
    python alarm_poll.py --did <DEVICE_ID> --user admin --password SECRET
    python alarm_poll.py --did <DEVICE_ID> --user admin --password SECRET --watch 10
"""
import argparse
import base64
import hashlib
import json
import sys
import time
import urllib.parse
import urllib.request

# Server fallback chain from PushMessageUtils (_strAlarmServerRecv3 is primary).
PRIMARY = ("https", "pushregcheck.nvcam.net", 443)
FALLBACKS = [
    ("http", "alarm1.nvdvr.cn", 8888),
    ("http", "alarmrec1.nvdvr.cn", 8888),
    ("http", "alarmrec2.nvdvr.cn", 8888),
]
PATH = "/GetSSAlarmMsg/NVSSGetAlarmNewMessageList"   # GET_MESSAGE_LIST_V20


def _b64(s):
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _md5(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _change_dev_pwd(pwd):
    """GlobalDefines.changeDevPwd: only transforms passwords longer than 28
    chars; normal passwords pass through unchanged."""
    if pwd and len(pwd) > 28:
        h = _md5(pwd)                      # md5_16 would be the middle 16; rare path
        return base64.b64encode(h.encode()).decode()
    return pwd


def build_request_json(did, user, password, count=10, ftime=0, type_=0,
                       version=0, key=""):
    """Mirror what getLatestAlarmMessage sends after its usr/pwd transforms."""
    return {
        "ftime": int(ftime),
        "did": int(did),
        "count": int(count),
        "type": int(type_),
        "key": key,
        "usr": _b64(user) if user else "",
        "pwd": _md5(_change_dev_pwd(password)) if password else "",
        "version": int(version),
        "filter_tag": [],
        "filter_summary": "",
    }


def _post(scheme, host, port, req_json, timeout=8):
    url = f"{scheme}://{host}:{port}{PATH}"
    param = base64.b64encode(json.dumps(req_json, separators=(",", ":")).encode()).decode()
    body = urllib.parse.urlencode({"param": param, "type": "1"}).encode()
    r = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "okhttp/3.12"},
    )
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        raw = resp.read().decode("ascii", "replace").strip()
    # NVCryptor.Decode = base64 decode
    try:
        decoded = base64.b64decode(raw).decode("utf-8", "replace")
    except Exception:
        decoded = raw                       # some servers may already return JSON
    return json.loads(decoded)


def fetch(did, user, password, count=10, ftime=0, verbose=False):
    req = build_request_json(did, user, password, count=count, ftime=ftime)
    last_err = None
    for scheme, host, port in [PRIMARY] + FALLBACKS:
        try:
            if verbose:
                print(f"[alarm] POST {scheme}://{host}:{port}{PATH}", file=sys.stderr)
            return _post(scheme, host, port, req)
        except Exception as e:
            last_err = e
            if verbose:
                print(f"[alarm]   {type(e).__name__}: {e}", file=sys.stderr)
    raise SystemExit(f"all alarm servers failed; last error: {last_err}")


def summarize(resp):
    """Pull the event list out of AlarmMsgResponse regardless of field casing."""
    result = resp.get("result")
    desc = resp.get("desc")
    value = resp.get("value") or []
    return result, desc, value


def main():
    ap = argparse.ArgumentParser(description="Fetch V380 cloud alarm events (own account)")
    ap.add_argument("--did", required=True, type=int, help="device id (discover.py prints it)")
    ap.add_argument("--user", required=True, help="V380 account / device username")
    ap.add_argument("--password", required=True, help="V380 account / device password")
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--ftime", type=int, default=0, help="only events newer than this (ms); 0 = latest")
    ap.add_argument("--watch", type=float, default=0, help="poll every N seconds")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    seen = set()
    while True:
        resp = fetch(args.did, args.user, args.password,
                     count=args.count, ftime=args.ftime, verbose=args.verbose)
        result, desc, value = summarize(resp)

        if args.json:
            print(json.dumps(resp, indent=2, ensure_ascii=False))
        else:
            if result is not None and result <= 0:
                print(f"server said result={result} desc={desc} "
                      f"(auth wrong, or no events / cloud alarm off?)")
            elif not value:
                print(f"no new events (desc={desc})")
            else:
                for ev in value:
                    key = json.dumps(ev, sort_keys=True)
                    tag = "  NEW" if key not in seen else ""
                    seen.add(key)
                    # field names vary by firmware; show the whole event compactly
                    print(f"- {json.dumps(ev, ensure_ascii=False)}{tag}")

        if args.watch <= 0:
            break
        time.sleep(args.watch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
