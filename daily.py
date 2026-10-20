#!/usr/bin/env python3
"""
Turn the day's uploaded segments into one video you can actually watch.

The ESP32 uploads short raw H.265 segments. Raw Annex-B has no container, so no
browser will play it - not even Safari. This joins a day's segments in order,
wraps and re-encodes them to H.264 mp4, and puts the result back in the bucket
as daily/<date>.mp4, which plays on any phone or browser.

Run it once a day (or whenever the machine is on):

    python daily.py              yesterday and today
    python daily.py 2026-10-21   one specific day
    python daily.py --all        every day found, skipping ones already done

Needs a PAR with listing enabled, in browse_url.txt (see events.py).
Segment names carry ESP32 uptime, not wall-clock time, so days are taken from
each object's timeCreated in the bucket listing.
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from urllib import request, error

HERE = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
SRC_PREFIX = "esp32/"
OUT_PREFIX = "daily/"
FPS = os.environ.get("FPS", "14")


def par_url():
    url = os.environ.get("BROWSE_URL", "").strip()
    if not url:
        f = os.path.join(HERE, "browse_url.txt")
        if os.path.exists(f):
            url = open(f, encoding="utf-8").read().strip()
    if not url:
        sys.exit("No listing URL. Put the listing PAR in browse_url.txt.")
    return url if url.endswith("/") else url + "/"


def listing(par, prefix):
    """Every object under prefix, with the time the bucket received it."""
    out, start = [], None
    while True:
        u = (par + "?fields=name,size,timeCreated&limit=1000&prefix=" + prefix +
             ("&start=" + start if start else ""))
        try:
            with request.urlopen(u, timeout=30) as r:
                data = json.loads(r.read().decode())
        except error.HTTPError as e:
            if e.code == 404:
                sys.exit("Listing refused (404). The PAR needs "
                         "--bucket-listing-action ListObjects.")
            raise
        out += data.get("objects", [])
        start = data.get("nextStartWith")
        if not start:
            return out


def day_of(obj):
    # timeCreated looks like 2026-10-21T04:05:06.123Z
    t = obj["timeCreated"].replace("Z", "+00:00")
    return datetime.fromisoformat(t).astimezone().strftime("%Y-%m-%d")


def build_day(par, day, objs, existing):
    name = OUT_PREFIX + day + ".mp4"
    if name in existing:
        print("  %s  already built, skipping" % day)
        return
    objs = sorted(objs, key=lambda o: o["timeCreated"])
    print("  %s  %d segments ..." % (day, len(objs)), end="", flush=True)

    with tempfile.TemporaryDirectory() as tmp:
        raw = os.path.join(tmp, "joined.h265")
        got = 0
        with open(raw, "wb") as out:
            for o in objs:
                try:
                    with request.urlopen(par + o["name"], timeout=60) as r:
                        out.write(r.read())
                    got += 1
                except error.URLError as e:
                    print("\n    skipped %s (%s)" % (o["name"], e))
        if got == 0:
            print(" nothing downloaded")
            return

        mp4 = os.path.join(tmp, "day.mp4")
        # re-encode to H.264: H.265 in a browser is a coin toss, H.264 is not
        rc = subprocess.call(
            [FFMPEG, "-loglevel", "error", "-y", "-fflags", "+genpts",
             "-r", FPS, "-i", raw,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", mp4],
            stdin=subprocess.DEVNULL)
        if rc != 0 or not os.path.exists(mp4):
            print(" ffmpeg failed")
            return

        size = os.path.getsize(mp4)
        with open(mp4, "rb") as fh:
            body = fh.read()
        req = request.Request(par + name, data=body, method="PUT",
                              headers={"Content-Type": "video/mp4"})
        with request.urlopen(req, timeout=300) as r:
            ok = r.status in (200, 201)
        print(" -> %s (%.1f MB) %s" % (name, size / 1e6, "ok" if ok else "FAILED"))


def main():
    par = par_url()
    segs = listing(par, SRC_PREFIX)
    if not segs:
        print("No segments in the bucket yet.")
        return 0

    existing = {o["name"] for o in listing(par, OUT_PREFIX)}

    days = {}
    for o in segs:
        days.setdefault(day_of(o), []).append(o)

    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg == "--all":
        wanted = sorted(days)
    elif arg:
        wanted = [arg]
    else:
        today = datetime.now().strftime("%Y-%m-%d")
        yday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        wanted = [d for d in (yday, today) if d in days]

    print("%d day(s) of footage in the bucket: %s" % (len(days), ", ".join(sorted(days))))
    for d in wanted:
        if d not in days:
            print("  %s  nothing recorded" % d)
            continue
        build_day(par, d, days[d], existing)
    return 0


if __name__ == "__main__":
    sys.exit(main())
