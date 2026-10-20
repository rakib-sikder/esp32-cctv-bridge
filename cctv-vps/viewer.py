#!/usr/bin/env python3
"""
Small web viewer for the clips recorder.py writes.

Browse by day, see a thumbnail grid, play or download a clip. Standard library
only, so it runs on the VPS with nothing installed.

Note on playback: the clips are H.265 in mp4, exactly as the camera sent them.
Safari plays that; Chrome, Edge and Firefox on most systems do not. So each clip
has a "convert" link that transcodes it to H.264 once, in the background, and
caches the result next to the original as <name>.h264.mp4. That costs CPU, which
is why it is on demand rather than automatic.

    CLIP_DIR=... VIEWER_PORT=8080 python3 viewer.py
"""

import html
import os
import re
import subprocess
import threading
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import unquote

_DEFAULT_CLIPS = (
    os.path.join(os.path.expanduser("~"), "cctv-clips")
    if os.name == "nt" else "/var/lib/cctv/clips"
)
CLIP_DIR = os.path.abspath(os.environ.get("CLIP_DIR", _DEFAULT_CLIPS))
PORT = int(os.environ.get("VIEWER_PORT", "8080"))
BIND = os.environ.get("VIEWER_BIND", "127.0.0.1")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")

SAFE_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SAFE_CLIP = re.compile(r"^[\d\-]+\.mp4$")

_converting = set()
_convert_lock = threading.Lock()

CSS = """
:root { color-scheme: light dark; --bg:#fff; --fg:#111; --muted:#666; --card:#f4f4f5; --line:#e4e4e7; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0e0e11; --fg:#ededf0; --muted:#9a9aa5; --card:#1a1a20; --line:#2a2a33; }
}
* { box-sizing: border-box; }
body { margin:0; padding:24px 16px; background:var(--bg); color:var(--fg);
       font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
.wrap { max-width:1100px; margin:0 auto; }
h1 { font-size:20px; margin:0 0 4px; }
.sub { color:var(--muted); font-size:13px; margin-bottom:24px; }
a { color:inherit; }
.days { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:28px; }
.day { padding:7px 13px; background:var(--card); border:1px solid var(--line);
       border-radius:999px; text-decoration:none; font-size:14px; }
.day.on { background:var(--fg); color:var(--bg); border-color:var(--fg); }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:16px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
.card img { width:100%; aspect-ratio:4/5; object-fit:cover; display:block; background:#000; }
.card .noimg { width:100%; aspect-ratio:4/5; display:grid; place-items:center;
               color:var(--muted); font-size:13px; background:#000; }
.meta { padding:10px 12px; }
.time { font-weight:600; }
.info { color:var(--muted); font-size:13px; margin-top:2px; }
.acts { display:flex; gap:8px; margin-top:9px; font-size:13px; }
.acts a { padding:4px 9px; border:1px solid var(--line); border-radius:6px;
          text-decoration:none; background:var(--bg); }
.empty { color:var(--muted); padding:40px 0; }
video { width:100%; max-width:860px; background:#000; border-radius:8px; }
.back { display:inline-block; margin-bottom:16px; color:var(--muted); text-decoration:none; }
"""


def human_size(n):
    return "{:.1f} MB".format(n / 1e6)


def days():
    if not os.path.isdir(CLIP_DIR):
        return []
    out = [d for d in os.listdir(CLIP_DIR)
           if SAFE_DAY.match(d) and os.path.isdir(os.path.join(CLIP_DIR, d))]
    return sorted(out, reverse=True)


def clips(day):
    d = os.path.join(CLIP_DIR, day)
    if not os.path.isdir(d):
        return []
    out = []
    for f in sorted(os.listdir(d), reverse=True):
        if not f.endswith(".mp4") or f.endswith(".h264.mp4"):
            continue
        p = os.path.join(d, f)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({
            "name": f,
            "size": st.st_size,
            "thumb": os.path.exists(os.path.splitext(p)[0] + ".jpg"),
            "h264": os.path.exists(os.path.splitext(p)[0] + ".h264.mp4"),
        })
    return out


def page(title, body):
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>{}</title><style>{}</style></head><body><div class='wrap'>{}"
            "</div></body></html>").format(html.escape(title), CSS, body)


def index_page(day=None):
    ds = days()
    if not ds:
        return page("CCTV", "<h1>CCTV</h1><p class='empty'>No clips yet.</p>")
    day = day if day in ds else ds[0]

    tabs = "".join(
        "<a class='day{}' href='/?day={}'>{}</a>".format(" on" if d == day else "", d, d)
        for d in ds
    )

    cs = clips(day)
    if not cs:
        grid = "<p class='empty'>Nothing recorded on this day.</p>"
    else:
        cards = []
        for c in cs:
            base = c["name"][:-4]
            img = ("<img loading='lazy' src='/thumb/{}/{}.jpg'>".format(day, base)
                   if c["thumb"] else "<div class='noimg'>no preview</div>")
            play = "/play/{}/{}".format(day, c["name"])
            conv = ("<a href='{}?h264=1'>play (converted)</a>".format(play)
                    if c["h264"] else "<a href='/convert/{}/{}'>convert</a>".format(day, c["name"]))
            cards.append(
                "<div class='card'>{}<div class='meta'>"
                "<div class='time'>{}</div>"
                "<div class='info'>{}</div>"
                "<div class='acts'><a href='{}'>play</a>{}"
                "<a href='/clip/{}/{}' download>download</a></div>"
                "</div></div>".format(
                    img, base.replace("-", ":"), human_size(c["size"]),
                    play, conv, day, c["name"])
            )
        grid = "<div class='grid'>{}</div>".format("".join(cards))

    total = sum(c["size"] for c in cs)
    return page("CCTV " + day,
                "<h1>CCTV</h1><div class='sub'>{} clips on {} &middot; {}</div>"
                "<div class='days'>{}</div>{}".format(
                    len(cs), day, human_size(total), tabs, grid))


def play_page(day, name, h264):
    base = name[:-4]
    src = "/clip/{}/{}".format(day, base + (".h264.mp4" if h264 else ".mp4"))
    note = "" if h264 else (
        "<p class='info' style='color:var(--muted);font-size:13px'>"
        "This is the original H.265 file. If the player stays black, go back and "
        "use <b>convert</b> &mdash; most browsers cannot decode H.265.</p>")
    return page(base, "<a class='back' href='/?day={}'>&larr; back</a>"
                      "<h1>{} &middot; {}</h1>"
                      "<video controls autoplay src='{}'></video>{}".format(
                          day, day, base.replace("-", ":"), src, note))


def convert(day, name):
    src = os.path.join(CLIP_DIR, day, name)
    dst = os.path.splitext(src)[0] + ".h264.mp4"
    key = (day, name)
    with _convert_lock:
        if key in _converting or os.path.exists(dst):
            return
        _converting.add(key)

    def run():
        try:
            subprocess.call([
                FFMPEG, "-loglevel", "error", "-y", "-i", src,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                "-vf", "scale=720:-2", "-an", "-movflags", "+faststart", dst,
            ], stdin=subprocess.DEVNULL)
        finally:
            with _convert_lock:
                _converting.discard(key)

    threading.Thread(target=run, daemon=True).start()


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):  # serve files out of CLIP_DIR, not cwd
        return os.path.join(CLIP_DIR, path.lstrip("/"))

    def _html(self, body, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _safe(self, day, name, exts=(".mp4", ".jpg")):
        if not SAFE_DAY.match(day):
            return None
        if "/" in name or "\\" in name or ".." in name:
            return None
        if not name.endswith(exts):
            return None
        p = os.path.join(CLIP_DIR, day, name)
        if not os.path.abspath(p).startswith(CLIP_DIR):
            return None
        return p

    def do_GET(self):
        path = unquote(self.path.split("?")[0])
        query = self.path.split("?")[1] if "?" in self.path else ""

        if path == "/":
            day = None
            m = re.search(r"day=([\d\-]+)", query)
            if m:
                day = m.group(1)
            return self._html(index_page(day))

        parts = [p for p in path.split("/") if p]

        if len(parts) == 3 and parts[0] == "play":
            p = self._safe(parts[1], parts[2])
            if not p:
                return self._html(page("?", "<p>bad path</p>"), 400)
            return self._html(play_page(parts[1], parts[2], "h264=1" in query))

        if len(parts) == 3 and parts[0] == "convert":
            if not self._safe(parts[1], parts[2]):
                return self._html(page("?", "<p>bad path</p>"), 400)
            convert(parts[1], parts[2])
            return self._html(page("converting",
                                   "<a class='back' href='/?day={}'>&larr; back</a>"
                                   "<h1>Converting</h1><p class='sub'>Reload the list in a "
                                   "moment and the clip will offer "
                                   "<b>play (converted)</b>.</p>".format(parts[1])))

        if len(parts) == 3 and parts[0] in ("clip", "thumb"):
            p = self._safe(parts[1], parts[2])
            if not p or not os.path.exists(p):
                self.send_error(404)
                return
            self.path = "/{}/{}".format(parts[1], parts[2])
            return SimpleHTTPRequestHandler.do_GET(self)

        self.send_error(404)

    def log_message(self, fmt, *args):
        pass  # the access log is noise here


def main():
    os.makedirs(CLIP_DIR, exist_ok=True)
    print("{:%Y-%m-%d %H:%M:%S}  viewer on http://{}:{}  serving {}".format(
        datetime.now(), BIND, PORT, CLIP_DIR), flush=True)
    HTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
