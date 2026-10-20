#!/usr/bin/env python3
"""
Build index.html for GitHub Pages.

Same viewer, but with no bucket link baked in - the page asks for it once and
keeps it in the visitor's own browser. That makes the file safe to host in a
public repo: it is just a player, and the credential never leaves your phone.

    python build_pages.py       then commit and push index.html
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    html = open(os.path.join(HERE, "viewer.html"), encoding="utf-8").read()
    mux = open(os.path.join(HERE, "mp4mux.js"), encoding="utf-8").read()
    mux = mux.replace("export function ", "function ").replace("export default muxHevcToMp4;", "")

    if "// __MP4MUX__" not in html:
        sys.exit("viewer.html has no __MP4MUX__ marker.")
    html = html.replace("// __MP4MUX__", mux)
    # __PAR_URL__ is deliberately left alone: that is what makes the page ask.

    out = os.path.join(HERE, "index.html")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    print("wrote %s (%.1f KB)" % (out, os.path.getsize(out) / 1024))
    print("no bucket link is embedded - the page asks for one and stores it locally")
    return 0


if __name__ == "__main__":
    sys.exit(main())
