#!/usr/bin/env python3
"""
CCTV dashboard - the web/desktop client for the V380 camera.

One page with:
  - live view   (RTSP sub stream transcoded to MJPEG on the fly)
  - PTZ pad     (proxied to the camera's ONVIF service)
  - snapshot    (single frame grab)
  - clip browser (what recorder.py recorded, with thumbnails)

Standard library + ffmpeg only, so it runs on the VPS with nothing to install.
The same URL is the "desktop app" too - point a browser or a webview at it.

Live view and PTZ work over open standards (RTSP, ONVIF). The vendor-only
features (floodlight, siren, AI toggles) are not here - those need the Android
SDK, by design.

    CLIP_DIR=... CAM_IP=... VIEWER_PORT=8080 python3 dashboard.py
"""

import html
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse, parse_qs
from urllib import request as urlreq

# --- config ----------------------------------------------------------------

CAM_IP = os.environ.get("CAM_IP", "192.168.68.50")
RTSP_PORT = int(os.environ.get("CAM_RTSP_PORT", "554"))
ONVIF_PORT = int(os.environ.get("CAM_ONVIF_PORT", "8899"))
LIVE_PATH = os.environ.get("CAM_LIVE_PATH", "/live/ch00_1")   # sub stream, light to transcode
PROFILE = os.environ.get("CAM_ONVIF_PROFILE", "PROFILE_000")

_DEFAULT_CLIPS = (
    os.path.join(os.path.expanduser("~"), "cctv-clips")
    if os.name == "nt" else "/var/lib/cctv/clips"
)
CLIP_DIR = os.path.abspath(os.environ.get("CLIP_DIR", _DEFAULT_CLIPS))
PORT = int(os.environ.get("VIEWER_PORT", "8080"))
BIND = os.environ.get("VIEWER_BIND", "127.0.0.1")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")

LIVE_URL = "rtsp://{}:{}{}".format(CAM_IP, RTSP_PORT, LIVE_PATH)
PTZ_SVC = "http://{}:{}/onvif/ptz_service".format(CAM_IP, ONVIF_PORT)

SAFE_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# --- ONVIF PTZ (raw SOAP, stdlib) ------------------------------------------

PTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"
SCHEMA_NS = "http://www.onvif.org/ver10/schema"

PTZ_VECTORS = {   # dir -> (pan x, tilt y)
    "left":  (-0.6, 0.0),
    "right": (0.6, 0.0),
    "up":    (0.0, 0.6),
    "down":  (0.0, -0.6),
}


def _soap(action, body):
    env = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
           '<s:Body>{}</s:Body></s:Envelope>'.format(body))
    req = urlreq.Request(
        PTZ_SVC, data=env.encode(),
        headers={"Content-Type": 'application/soap+xml; charset=utf-8; action="{}"'.format(action)},
    )
    with urlreq.urlopen(req, timeout=8) as r:
        return r.read().decode("utf-8", "replace")


def ptz_move(direction):
    if direction == "stop":
        _soap(PTZ_NS + "/Stop",
              '<Stop xmlns="{}"><ProfileToken>{}</ProfileToken>'
              '<PanTilt>true</PanTilt></Stop>'.format(PTZ_NS, PROFILE))
        return
    x, y = PTZ_VECTORS[direction]
    _soap(PTZ_NS + "/ContinuousMove",
          '<ContinuousMove xmlns="{}"><ProfileToken>{}</ProfileToken>'
          '<Velocity><PanTilt x="{}" y="{}" xmlns="{}"/></Velocity>'
          '</ContinuousMove>'.format(PTZ_NS, PROFILE, x, y, SCHEMA_NS))


# --- clips -----------------------------------------------------------------

def human_size(n):
    return "{:.1f} MB".format(n / 1e6)


def days():
    if not os.path.isdir(CLIP_DIR):
        return []
    return sorted((d for d in os.listdir(CLIP_DIR)
                   if SAFE_DAY.match(d) and os.path.isdir(os.path.join(CLIP_DIR, d))),
                  reverse=True)


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
        out.append({"name": f, "size": st.st_size,
                    "thumb": os.path.exists(os.path.splitext(p)[0] + ".jpg"),
                    "h264": os.path.exists(os.path.splitext(p)[0] + ".h264.mp4")})
    return out


# --- MJPEG live transcode ---------------------------------------------------

def mjpeg_frames():
    """
    Spawn ffmpeg to turn the RTSP sub stream into a stream of JPEGs, and yield
    one complete JPEG at a time by scanning for the JPEG SOI/EOI markers.
    """
    cmd = [
        FFMPEG, "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", LIVE_URL,
        "-an",
        "-vf", "fps=8,scale=640:-2",
        "-f", "mjpeg", "-q:v", "6", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    buf = b""
    try:
        while True:
            chunk = proc.stdout.read(8192)
            if not chunk:
                break
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                end = buf.find(b"\xff\xd9", start + 2)
                if start < 0 or end < 0:
                    break
                frame = buf[start:end + 2]
                buf = buf[end + 2:]
                yield frame
    finally:
        try:
            proc.kill()
        except OSError:
            pass


# --- HTML -------------------------------------------------------------------

CSS = """
:root { color-scheme: light dark; --bg:#fff; --fg:#111; --muted:#666; --card:#f4f4f5;
        --line:#e4e4e7; --accent:#2563eb; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0e0e11; --fg:#ededf0; --muted:#9a9aa5; --card:#1a1a20; --line:#2a2a33; --accent:#3b82f6; }
}
* { box-sizing:border-box; }
body { margin:0; padding:24px 16px; background:var(--bg); color:var(--fg);
       font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
.wrap { max-width:1100px; margin:0 auto; }
h1 { font-size:20px; margin:0 0 20px; }
h2 { font-size:15px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted);
     margin:32px 0 14px; }
a { color:inherit; }
.top { display:grid; grid-template-columns:2fr 1fr; gap:20px; align-items:start; }
@media (max-width:760px){ .top { grid-template-columns:1fr; } }
.live { background:#000; border-radius:10px; overflow:hidden; aspect-ratio:16/9; }
.live img { width:100%; height:100%; object-fit:contain; display:block; }
.pad { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; max-width:200px; }
.pad button { padding:14px 0; font-size:16px; background:var(--card); color:var(--fg);
              border:1px solid var(--line); border-radius:8px; cursor:pointer; }
.pad button:active { background:var(--accent); color:#fff; }
.pad .sp { visibility:hidden; }
.row { display:flex; gap:8px; margin-top:12px; flex-wrap:wrap; }
.row a, .row button { padding:8px 13px; background:var(--card); color:var(--fg);
       border:1px solid var(--line); border-radius:8px; text-decoration:none; font-size:14px; cursor:pointer; }
.days { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:18px; }
.day { padding:7px 13px; background:var(--card); border:1px solid var(--line);
       border-radius:999px; text-decoration:none; font-size:14px; }
.day.on { background:var(--fg); color:var(--bg); border-color:var(--fg); }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:16px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
.card img { width:100%; aspect-ratio:4/5; object-fit:cover; display:block; background:#000; }
.card .noimg { width:100%; aspect-ratio:4/5; display:grid; place-items:center; color:var(--muted); background:#000; font-size:13px; }
.meta { padding:10px 12px; }
.time { font-weight:600; } .info { color:var(--muted); font-size:13px; margin-top:2px; }
.acts { display:flex; gap:8px; margin-top:9px; font-size:13px; flex-wrap:wrap; }
.acts a { padding:4px 9px; border:1px solid var(--line); border-radius:6px; text-decoration:none; background:var(--bg); }
.empty { color:var(--muted); padding:30px 0; }
video { width:100%; max-width:860px; background:#000; border-radius:8px; }
.back { display:inline-block; margin-bottom:16px; color:var(--muted); text-decoration:none; }
.hint { color:var(--muted); font-size:13px; margin-top:8px; }
"""

PTZ_JS = """
<script>
function ptz(d){ fetch('/ptz?dir='+d, {method:'POST'}); }
function ptzStop(){ fetch('/ptz?dir=stop', {method:'POST'}); }
function hold(btn,d){
  btn.addEventListener('mousedown',()=>ptz(d));
  btn.addEventListener('touchstart',e=>{e.preventDefault();ptz(d);},{passive:false});
  ['mouseup','mouseleave','touchend','touchcancel'].forEach(ev=>btn.addEventListener(ev,ptzStop));
}
document.querySelectorAll('[data-dir]').forEach(b=>hold(b,b.dataset.dir));
function snap(){ window.open('/snapshot?t='+Date.now(),'_blank'); }
</script>
"""


def page(title, body, scripts=""):
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>{}</title><style>{}</style></head><body><div class='wrap'>{}"
            "</div>{}</body></html>").format(html.escape(title), CSS, body, scripts)


def dashboard_page(day=None):
    live = ("<div class='live'><img src='/live' alt='live'></div>"
            "<div class='hint'>Live view is the {}px sub stream, transcoded to "
            "MJPEG.</div>").format(LIVE_PATH.rsplit("/", 1)[-1])

    pad = ("<div class='pad'>"
           "<span class='sp'></span><button data-dir='up'>&uarr;</button><span class='sp'></span>"
           "<button data-dir='left'>&larr;</button><button onclick='ptzStop()'>&#9632;</button>"
           "<button data-dir='right'>&rarr;</button>"
           "<span class='sp'></span><button data-dir='down'>&darr;</button><span class='sp'></span>"
           "</div>"
           "<div class='row'><button onclick='snap()'>&#128247; snapshot</button></div>"
           "<div class='hint'>Hold a button to pan/tilt. PTZ goes over ONVIF.</div>")

    top = "<div class='top'><div>{}</div><div><h2>Control</h2>{}</div></div>".format(live, pad)

    ds = days()
    clips_html = "<h2>Recordings</h2>"
    if not ds:
        clips_html += "<p class='empty'>No clips yet. Start recorder.py.</p>"
    else:
        day = day if day in ds else ds[0]
        tabs = "".join("<a class='day{}' href='/?day={}'>{}</a>".format(
            " on" if d == day else "", d, d) for d in ds)
        cs = clips(day)
        if not cs:
            grid = "<p class='empty'>Nothing on this day.</p>"
        else:
            cards = []
            for c in cs:
                base = c["name"][:-4]
                img = ("<img loading='lazy' src='/thumb/{}/{}.jpg'>".format(day, base)
                       if c["thumb"] else "<div class='noimg'>no preview</div>")
                play = "/play/{}/{}".format(day, c["name"])
                conv = ("<a href='{}?h264=1'>play&#10003;</a>".format(play) if c["h264"]
                        else "<a href='/convert/{}/{}'>convert</a>".format(day, c["name"]))
                cards.append(
                    "<div class='card'>{}<div class='meta'><div class='time'>{}</div>"
                    "<div class='info'>{}</div><div class='acts'><a href='{}'>play</a>{}"
                    "<a href='/clip/{}/{}' download>get</a></div></div></div>".format(
                        img, base.replace("-", ":"), human_size(c["size"]), play, conv, day, c["name"]))
            grid = "<div class='grid'>{}</div>".format("".join(cards))
        clips_html += "<div class='days'>{}</div>{}".format(tabs, grid)

    return page("CCTV", "<h1>CCTV &middot; {}</h1>{}{}".format(CAM_IP, top, clips_html), PTZ_JS)


def play_page(day, name, h264):
    base = name[:-4]
    src = "/clip/{}/{}".format(day, base + (".h264.mp4" if h264 else ".mp4"))
    note = "" if h264 else ("<p class='hint'>Original H.265. If the player is black, "
                            "go back and use <b>convert</b>.</p>")
    return page(base, "<a class='back' href='/?day={}'>&larr; back</a><h1>{}</h1>"
                      "<video controls autoplay src='{}'></video>{}".format(
                          day, base.replace("-", ":"), src, note))


# --- transcode on demand ----------------------------------------------------

_converting = set()
_clock = threading.Lock()


def convert(day, name):
    src = os.path.join(CLIP_DIR, day, name)
    dst = os.path.splitext(src)[0] + ".h264.mp4"
    key = (day, name)
    with _clock:
        if key in _converting or os.path.exists(dst):
            return
        _converting.add(key)

    def run():
        try:
            subprocess.call([FFMPEG, "-loglevel", "error", "-y", "-i", src,
                             "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                             "-vf", "scale=720:-2", "-an", "-movflags", "+faststart", dst],
                            stdin=subprocess.DEVNULL)
        finally:
            with _clock:
                _converting.discard(key)
    threading.Thread(target=run, daemon=True).start()


# --- HTTP -------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _html(self, body, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _safe(self, day, name, exts=(".mp4", ".jpg")):
        if not SAFE_DAY.match(day) or "/" in name or "\\" in name or ".." in name:
            return None
        if not name.endswith(exts):
            return None
        p = os.path.join(CLIP_DIR, day, name)
        if not os.path.abspath(p).startswith(CLIP_DIR):
            return None
        return p

    def _send_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/ptz":
            d = parse_qs(u.query).get("dir", [""])[0]
            if d in ("stop",) or d in PTZ_VECTORS:
                try:
                    ptz_move(d)
                    self._html("ok")
                except Exception as e:
                    self._html("ptz error: " + html.escape(str(e)), 502)
            else:
                self._html("bad dir", 400)
            return
        self.send_error(404)

    def do_GET(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        q = parse_qs(u.query)

        if path == "/":
            return self._html(dashboard_page(q.get("day", [None])[0]))

        if path == "/live":
            return self._stream_mjpeg()

        if path == "/snapshot":
            return self._snapshot()

        parts = [p for p in path.split("/") if p]

        if len(parts) == 3 and parts[0] == "play":
            if not self._safe(parts[1], parts[2]):
                return self._html("bad path", 400)
            return self._html(play_page(parts[1], parts[2], "h264" in q))

        if len(parts) == 3 and parts[0] == "convert":
            if not self._safe(parts[1], parts[2]):
                return self._html("bad path", 400)
            convert(parts[1], parts[2])
            return self._html(page("converting",
                "<a class='back' href='/?day={}'>&larr; back</a><h1>Converting&hellip;</h1>"
                "<p class='hint'>Reload the list in a moment for <b>play&#10003;</b>.</p>".format(parts[1])))

        if len(parts) == 3 and parts[0] == "clip":
            p = self._safe(parts[1], parts[2])
            if not p:
                return self.send_error(404)
            return self._send_file(p, "video/mp4")

        if len(parts) == 3 and parts[0] == "thumb":
            p = self._safe(parts[1], parts[2], exts=(".jpg",))
            if not p:
                return self.send_error(404)
            return self._send_file(p, "image/jpeg")

        self.send_error(404)

    def _snapshot(self):
        try:
            data = subprocess.check_output(
                [FFMPEG, "-loglevel", "error", "-rtsp_transport", "tcp",
                 "-i", LIVE_URL, "-frames:v", "1", "-f", "mjpeg", "-"],
                stdin=subprocess.DEVNULL, timeout=15)
        except Exception as e:
            return self._html("snapshot failed: " + html.escape(str(e)), 502)
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream_mjpeg(self):
        boundary = "frame"
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=" + boundary)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for frame in mjpeg_frames():
                self.wfile.write(("--" + boundary + "\r\n").encode())
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(("Content-Length: %d\r\n\r\n" % len(frame)).encode())
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # browser closed the tab

    def log_message(self, *a):
        pass


def main():
    os.makedirs(CLIP_DIR, exist_ok=True)
    print("{:%Y-%m-%d %H:%M:%S}  dashboard on http://{}:{}  camera {}".format(
        datetime.now(), BIND, PORT, CAM_IP), flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
