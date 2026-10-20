#!/usr/bin/env python3
"""
Browse what the ESP32 has recorded, and turn an event back into a video.

The ESP32 uploads short .h265 segments named esp32/<seconds>_<seq>.h265. On
their own they are just pieces; this groups segments that arrived close
together into one "event" and can stitch an event into a playable mp4.

    python events.py                 list events
    python events.py 3               fetch event 3 as an mp4
    python events.py all             fetch every event

Needs a PAR with listing enabled. Put its URL in browse_url.txt next to this
file, or set BROWSE_URL. Creating one:

    oci os preauth-request create --namespace <ns> --bucket-name cctv-clips \
        --name cctv-browse --access-type AnyObjectReadWrite \
        --bucket-listing-action ListObjects --time-expires <date>
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from urllib import request, error

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("EVENT_DIR", os.path.join(HERE, "events"))
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")

# segments more than this far apart belong to different events
EVENT_GAP_SECS = 60


def browse_url():
    url = os.environ.get("BROWSE_URL", "").strip()
    if not url:
        f = os.path.join(HERE, "browse_url.txt")
        if os.path.exists(f):
            url = open(f, encoding="utf-8").read().strip()
    if not url:
        sys.exit("No listing URL. Put it in browse_url.txt (see the notes at the "
                 "top of this file) or set BROWSE_URL.")
    return url if url.endswith("/") else url + "/"


def list_objects(par):
    """Page through the bucket listing a PAR with ListObjects gives us."""
    names, start = [], None
    while True:
        u = par + "?limit=1000" + ("&start=" + start if start else "")
        try:
            with request.urlopen(u, timeout=30) as r:
                data = json.loads(r.read().decode())
        except error.HTTPError as e:
            if e.code == 404:
                sys.exit("Listing refused (404). This PAR probably lacks "
                         "--bucket-listing-action ListObjects.")
            raise
        names += [o["name"] for o in data.get("objects", [])]
        start = data.get("nextStartWith")
        if not start:
            return names


def parse(name):
    """esp32/<uptime seconds>_<seq>.h265 -> (uptime, seq) or None."""
    m = re.search(r"(\d+)_(\d+)\.h265$", name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def group(names):
    segs = []
    for n in names:
        p = parse(n)
        if p:
            segs.append((p[0], p[1], n))
    segs.sort(key=lambda s: (s[1], s[0]))   # sequence number is the real clock

    events, cur, last = [], [], None
    for up, seq, n in segs:
        if last is not None and up - last > EVENT_GAP_SECS:
            events.append(cur); cur = []
        cur.append((up, seq, n))
        last = up
    if cur:
        events.append(cur)
    return events


def fetch(par, event, index):
    os.makedirs(OUT_DIR, exist_ok=True)
    raw = os.path.join(OUT_DIR, "event_%03d.h265" % index)
    mp4 = os.path.join(OUT_DIR, "event_%03d.mp4" % index)

    with open(raw, "wb") as out:
        for up, seq, name in event:
            with request.urlopen(par + name, timeout=60) as r:
                out.write(r.read())
    size = os.path.getsize(raw) / 1e6

    rc = subprocess.call([FFMPEG, "-loglevel", "error", "-y",
                          "-fflags", "+genpts", "-r", "14", "-i", raw,
                          "-c", "copy", mp4], stdin=subprocess.DEVNULL)
    if rc == 0:
        os.remove(raw)
        print("  event %d -> %s  (%.1f MB)" % (index, mp4, size))
    else:
        print("  event %d: kept raw stream at %s (ffmpeg could not wrap it)"
              % (index, raw))


def main():
    par = browse_url()
    names = list_objects(par)
    events = group(names)

    if not events:
        print("No recordings in the bucket yet.")
        return 0

    print("%d event(s), %d segments total\n" % (len(events), sum(len(e) for e in events)))
    print("  #   segments   device uptime      approx length")
    print("  --  ---------  ----------------   -------------")
    for i, e in enumerate(events, 1):
        span = e[-1][0] - e[0][0]
        print("  %-2d  %-9d  %6ds .. %-6ds  ~%ds" % (i, len(e), e[0][0], e[-1][0], max(span, 2)))

    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if not arg:
        print("\nTo save one:  python events.py 1        (or 'all')")
        return 0

    print()
    if arg == "all":
        for i, e in enumerate(events, 1):
            fetch(par, e, i)
    else:
        i = int(arg)
        if not 1 <= i <= len(events):
            sys.exit("No event %d" % i)
        fetch(par, events[i - 1], i)
    return 0


if __name__ == "__main__":
    sys.exit(main())
