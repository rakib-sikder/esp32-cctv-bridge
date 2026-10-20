#!/usr/bin/env python3
"""
Put viewer.html in the bucket and print the URL to open on any phone.

The page needs to read the bucket, so the listing PAR is written into it before
upload. That means the page's own URL is a credential - anyone who has it can
see the recordings. Keep it to yourself, and if it leaks, delete the PAR in the
console and run this again.

    python publish_viewer.py
"""

import os
import sys
from urllib import request, error

HERE = os.path.dirname(os.path.abspath(__file__))


def par_url():
    url = os.environ.get("BROWSE_URL", "").strip()
    if not url:
        f = os.path.join(HERE, "browse_url.txt")
        if os.path.exists(f):
            url = open(f, encoding="utf-8").read().strip()
    if not url:
        sys.exit("No listing URL. Put the listing PAR in browse_url.txt first.")
    return url if url.endswith("/") else url + "/"


def main():
    par = par_url()
    src = os.path.join(HERE, "viewer.html")
    html = open(src, encoding="utf-8").read().replace("__PAR_URL__", par)

    target = par + "viewer.html"
    req = request.Request(target, data=html.encode("utf-8"), method="PUT",
                          headers={"Content-Type": "text/html; charset=utf-8"})
    try:
        with request.urlopen(req, timeout=60) as r:
            if r.status not in (200, 201):
                sys.exit("upload returned HTTP %d" % r.status)
    except error.HTTPError as e:
        sys.exit("upload failed: HTTP %d - is the PAR writable?" % e.code)

    print("Viewer published. Open this on your phone or any browser:\n")
    print("  " + target + "\n")
    print("Bookmark it. Treat the link as private - it can read your recordings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
