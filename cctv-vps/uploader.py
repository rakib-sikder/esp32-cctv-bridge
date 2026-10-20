#!/usr/bin/env python3
"""
Upload recorded clips to Oracle Object Storage via a Pre-Authenticated Request.

Runs alongside recorder.py on the home PC. It watches the clip directory and
PUTs each new clip (and its thumbnail) to the bucket over plain HTTPS - no SSH,
no SDK, no keys on this machine. Outbound HTTPS, so CGNAT is not a problem.

The PAR URL is a write credential, so it is NOT hard-coded here. Put it in a
file next to this script called `par_url.txt` (one line), or set PAR_URL in the
environment. The file is the better choice - keep it out of any git repo.

A PAR URL looks like:
  https://objectstorage.ap-singapore-1.oraclecloud.com/p/<token>/n/<ns>/b/<bucket>/o/
Objects are written by appending the object name to it.

    CLIP_DIR=... python3 uploader.py
"""

import os
import sys
import time
from urllib import request, error

HERE = os.path.dirname(os.path.abspath(__file__))

_DEFAULT_CLIPS = (
    os.path.join(os.path.expanduser("~"), "cctv-clips")
    if os.name == "nt" else "/var/lib/cctv/clips"
)
CLIP_DIR = os.path.abspath(os.environ.get("CLIP_DIR", _DEFAULT_CLIPS))

# delete the local file once it is safely uploaded?  default: keep it.
DELETE_AFTER = os.environ.get("DELETE_AFTER_UPLOAD", "0") == "1"
SCAN_SECS = int(os.environ.get("UPLOAD_SCAN_SECS", "20"))

# a small ledger of what has already been uploaded, so a restart does not re-send
LEDGER = os.path.join(CLIP_DIR, ".uploaded")


def log(msg):
    from datetime import datetime
    print("{:%Y-%m-%d %H:%M:%S}  {}".format(datetime.now(), msg), flush=True)


def load_par_url():
    url = os.environ.get("PAR_URL", "").strip()
    if not url:
        f = os.path.join(HERE, "par_url.txt")
        if os.path.exists(f):
            with open(f, "r", encoding="utf-8") as fh:
                url = fh.read().strip()
    if not url:
        log("No PAR URL. Put the Oracle PAR URL in {} or set PAR_URL.".format(
            os.path.join(HERE, "par_url.txt")))
        sys.exit(1)
    if not url.endswith("/"):
        url += "/"
    return url


def load_ledger():
    done = set()
    try:
        with open(LEDGER, "r", encoding="utf-8") as fh:
            done = set(line.strip() for line in fh if line.strip())
    except OSError:
        pass
    return done


def mark_done(rel):
    with open(LEDGER, "a", encoding="utf-8") as fh:
        fh.write(rel + "\n")


def to_upload(done):
    """Clips and thumbnails not yet in the ledger, oldest first."""
    out = []
    for root, _, files in os.walk(CLIP_DIR):
        for f in files:
            if not (f.endswith(".mp4") or f.endswith(".jpg")):
                continue
            p = os.path.join(root, f)
            rel = os.path.relpath(p, CLIP_DIR).replace("\\", "/")
            if rel in done:
                continue
            try:
                out.append((os.path.getmtime(p), p, rel))
            except OSError:
                pass
    out.sort()
    return out


def still_being_written(path):
    """Skip a file ffmpeg is still recording: size changing or <2 s old."""
    try:
        st = os.stat(path)
    except OSError:
        return True
    if time.time() - st.st_mtime < 2:
        return True
    return False


def put_object(par_url, rel, path):
    ctype = "video/mp4" if rel.endswith(".mp4") else "image/jpeg"
    with open(path, "rb") as fh:
        data = fh.read()
    req = request.Request(par_url + rel, data=data, method="PUT",
                          headers={"Content-Type": ctype})
    with request.urlopen(req, timeout=120) as resp:
        return resp.status in (200, 201)


def main():
    par_url = load_par_url()
    os.makedirs(CLIP_DIR, exist_ok=True)
    log("uploading from {}".format(CLIP_DIR))
    log("target bucket via PAR; delete local after upload = {}".format(DELETE_AFTER))
    done = load_ledger()

    while True:
        try:
            for _mtime, path, rel in to_upload(done):
                if still_being_written(path):
                    continue
                try:
                    if put_object(par_url, rel, path):
                        done.add(rel)
                        mark_done(rel)
                        size = os.path.getsize(path) / 1e6
                        log("uploaded {} ({:.1f} MB)".format(rel, size))
                        if DELETE_AFTER:
                            try:
                                os.remove(path)
                            except OSError:
                                pass
                    else:
                        log("unexpected status uploading {}".format(rel))
                except error.HTTPError as e:
                    log("HTTP {} on {} - check the PAR is valid and writable".format(e.code, rel))
                    time.sleep(10)
                except error.URLError as e:
                    log("network error on {}: {} - will retry".format(rel, e.reason))
                    time.sleep(10)
                    break  # stop this pass, retry whole batch after a wait
        except Exception as e:
            log("scan error: {}".format(e))
        time.sleep(SCAN_SECS)


if __name__ == "__main__":
    main()
