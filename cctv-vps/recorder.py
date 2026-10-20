#!/usr/bin/env python3
"""
Motion-triggered CCTV recorder for a V380 / macro-video camera.

Two streams are used at once, which is what keeps this cheap:

  detect  - the 640x720 sub stream is decoded at a few frames per second and
            scaled down to a thumbnail. Motion is a simple frame difference.
  record  - when motion starts, the full 2304x2592 main stream is copied
            straight to mp4 with `-c copy`. Nothing is re-encoded, so the
            recording costs almost no CPU and keeps the camera's own quality.

The camera advertises an ONVIF `MotionAlarm` topic but never actually fires it
(not even the initial property value every ONVIF property event must send), so
detection is done here. MOTION_SOURCE=onvif is kept for cameras that do
implement it.

Only dependency is `requests`, and that is only needed for MOTION_SOURCE=onvif.
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

# --- configuration ---------------------------------------------------------

CAMERA_IP = os.environ.get("CAM_IP", "192.168.68.50")
ONVIF_PORT = int(os.environ.get("CAM_ONVIF_PORT", "8899"))
RTSP_PORT = int(os.environ.get("CAM_RTSP_PORT", "554"))

RECORD_PATH = os.environ.get("CAM_RECORD_PATH", "/live/ch00_0")  # main, 2304x2592
DETECT_PATH = os.environ.get("CAM_DETECT_PATH", "/live/ch00_1")  # sub, 640x720

MOTION_SOURCE = os.environ.get("MOTION_SOURCE", "video").lower()  # video | onvif

# detection tuning
DETECT_FPS = float(os.environ.get("DETECT_FPS", "4"))
DETECT_W = int(os.environ.get("DETECT_W", "160"))
DETECT_H = int(os.environ.get("DETECT_H", "180"))
PIXEL_DELTA = int(os.environ.get("PIXEL_DELTA", "25"))        # 0-255, per-pixel change to count
MOTION_FRACTION = float(os.environ.get("MOTION_FRACTION", "0.02"))  # share of pixels that must change
WARMUP_FRAMES = int(os.environ.get("WARMUP_FRAMES", "8"))     # ignore the first frames after connect

# recording
_DEFAULT_CLIPS = (
    os.path.join(os.path.expanduser("~"), "cctv-clips")
    if os.name == "nt" else "/var/lib/cctv/clips"
)
CLIP_DIR = os.environ.get("CLIP_DIR", _DEFAULT_CLIPS)
POST_ROLL = int(os.environ.get("POST_ROLL", "10"))       # keep recording N s after motion stops
MAX_CLIP_SECS = int(os.environ.get("MAX_CLIP", "300"))   # cap so one event cannot fill the disk
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "14"))
MIN_FREE_GB = float(os.environ.get("MIN_FREE_GB", "2"))

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")

RECORD_URL = "rtsp://{}:{}{}".format(CAMERA_IP, RTSP_PORT, RECORD_PATH)
DETECT_URL = "rtsp://{}:{}{}".format(CAMERA_IP, RTSP_PORT, DETECT_PATH)
EVENT_SVC = "http://{}:{}/onvif/event_service".format(CAMERA_IP, ONVIF_PORT)

FRAME_BYTES = DETECT_W * DETECT_H


def log(msg):
    print("{:%Y-%m-%d %H:%M:%S}  {}".format(datetime.now(), msg), flush=True)


# --- recording -------------------------------------------------------------

def _make_thumb(clip_path):
    """
    One small JPEG per clip, so the viewer can show a grid without decoding
    every H.265 file. Decoding a single frame is cheap; it runs detached so it
    never delays the next recording.
    """
    thumb = os.path.splitext(clip_path)[0] + ".jpg"
    cmd = [
        FFMPEG, "-loglevel", "error", "-y",
        "-ss", "1",                 # skip the first second, it is often a blank keyframe
        "-i", clip_path,
        "-frames:v", "1",
        "-vf", "scale=320:-2",
        thumb,
    ]
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        log("thumbnail failed: {}".format(e))


class Recorder:
    def __init__(self):
        self.proc = None
        self.path = None
        self.started = None
        self.stop_at = None  # set when motion ends: the deadline to stop
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.stop_at = None  # motion resumed, cancel the pending stop
                return
            if self.proc:
                return  # a finished process is cleared by tick(), not here
            now = datetime.now()
            day = os.path.join(CLIP_DIR, now.strftime("%Y-%m-%d"))
            os.makedirs(day, exist_ok=True)
            self.path = os.path.join(day, now.strftime("%H-%M-%S") + ".mp4")
            cmd = [
                FFMPEG, "-loglevel", "error",
                "-rtsp_transport", "tcp",      # UDP drops badly over a VPN
                "-i", RECORD_URL,
                "-t", str(MAX_CLIP_SECS),
                "-c", "copy",                  # no re-encode: near-zero CPU
                "-movflags", "+faststart",
                self.path,
            ]
            # stdin stays open so we can send 'q' for a clean shutdown; that is
            # the one stop signal ffmpeg honours identically on Windows and Linux
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            self.started = now
            self.stop_at = None
            log("REC start -> " + self.path)

    def schedule_stop(self):
        with self.lock:
            if self.proc and self.proc.poll() is None and self.stop_at is None:
                self.stop_at = time.time() + POST_ROLL

    def tick(self):
        """Called regularly: finish a clip whose post-roll has elapsed."""
        with self.lock:
            if not self.proc:
                return
            if self.proc.poll() is not None:  # ffmpeg exited (hit MAX_CLIP, or died)
                self._finish()
                return
            if self.stop_at is not None and time.time() >= self.stop_at:
                self._quit_ffmpeg()
                self._finish()

    def _quit_ffmpeg(self):
        """Ask ffmpeg to finish the file properly, then make sure it is gone."""
        try:
            self.proc.stdin.write(b"q")
            self.proc.stdin.flush()
        except (OSError, ValueError, AttributeError):
            pass
        try:
            self.proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def _finish(self):
        size = 0.0
        if self.path and os.path.exists(self.path):
            size = os.path.getsize(self.path) / 1e6
        secs = (datetime.now() - self.started).total_seconds() if self.started else 0
        name = os.path.basename(self.path) if self.path else "?"
        log("REC stop  <- {}  {:.0f}s  {:.1f} MB".format(name, secs, size))
        if size > 0:
            _make_thumb(self.path)
        self.proc = None
        self.path = None
        self.started = None
        self.stop_at = None


# --- motion detection by frame difference ----------------------------------

class VideoMotion(threading.Thread):
    """
    Decode the sub stream as small grayscale frames and flag motion when enough
    pixels change between consecutive frames.

    `last_motion` is the only thing the main loop reads.
    """

    daemon = True

    def __init__(self):
        super().__init__(name="detector")
        self.last_motion = 0.0
        self.frames = 0

    def _spawn(self):
        cmd = [
            FFMPEG, "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", DETECT_URL,
            "-an",
            "-vf", "fps={},scale={}:{},format=gray".format(DETECT_FPS, DETECT_W, DETECT_H),
            "-f", "rawvideo", "-",
        ]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE)

    def run(self):
        while True:
            proc = None
            try:
                proc = self._spawn()
                log("detector: decoding {} at {} fps, {}x{}".format(
                    DETECT_PATH, DETECT_FPS, DETECT_W, DETECT_H))
                prev = None
                seen = 0
                while True:
                    frame = proc.stdout.read(FRAME_BYTES)
                    if len(frame) < FRAME_BYTES:
                        raise RuntimeError("stream ended")
                    seen += 1
                    self.frames += 1
                    if seen <= WARMUP_FRAMES:
                        prev = frame
                        continue
                    if prev is not None:
                        changed = 0
                        for a, b in zip(prev, frame):
                            d = a - b
                            if d >= PIXEL_DELTA or -d >= PIXEL_DELTA:
                                changed += 1
                        if changed >= FRAME_BYTES * MOTION_FRACTION:
                            if time.time() - self.last_motion > 3:
                                log("motion ({:.1%} of frame changed)".format(
                                    changed / float(FRAME_BYTES)))
                            self.last_motion = time.time()
                    prev = frame
            except Exception as e:
                log("detector: {} - restarting in 5s".format(e))
            finally:
                if proc:
                    try:
                        proc.kill()
                    except OSError:
                        pass
            time.sleep(5)


# --- motion detection from ONVIF events (unused on this camera) ------------

class OnvifMotion(threading.Thread):
    daemon = True
    NS = (
        'xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:tev="http://www.onvif.org/ver10/events/wsdl" '
        'xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"'
    )

    def __init__(self):
        super().__init__(name="onvif")
        self.last_motion = 0.0

    def _soap(self, url, body, action, timeout=40):
        import requests
        ct = 'application/soap+xml; charset=utf-8; action="{}"'.format(action)
        env = ('<?xml version="1.0" encoding="UTF-8"?><s:Envelope ' + self.NS +
               "><s:Body>" + body + "</s:Body></s:Envelope>")
        r = requests.post(url, data=env.encode(), headers={"Content-Type": ct}, timeout=timeout)
        r.raise_for_status()
        return r.text

    def run(self):
        while True:
            try:
                xml = self._soap(
                    EVENT_SVC,
                    "<tev:CreatePullPointSubscription>"
                    "<tev:InitialTerminationTime>PT10M</tev:InitialTerminationTime>"
                    "</tev:CreatePullPointSubscription>",
                    "http://www.onvif.org/ver10/events/wsdl/EventPortType/"
                    "CreatePullPointSubscriptionRequest",
                )
                m = re.search(r"<(?:\w+:)?Address[^>]*>(.*?)</(?:\w+:)?Address>", xml, re.S)
                if not m:
                    raise RuntimeError("no SubscriptionReference")
                addr = m.group(1).strip()
                log("onvif: subscribed " + addr)

                while True:
                    batch = self._soap(
                        addr,
                        "<tev:PullMessages><tev:Timeout>PT10S</tev:Timeout>"
                        "<tev:MessageLimit>32</tev:MessageLimit></tev:PullMessages>",
                        "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/"
                        "PullMessagesRequest",
                    )
                    for msg in re.findall(
                        r"<(?:\w+:)?NotificationMessage.*?</(?:\w+:)?NotificationMessage>",
                        batch, re.S,
                    ):
                        if "motion" not in msg.lower():
                            continue
                        for name, value in re.findall(
                            r'<(?:\w+:)?SimpleItem\s+Name="([^"]+)"\s+Value="([^"]+)"', msg
                        ):
                            if name.lower() in ("state", "ismotion", "motion", "logicalstate"):
                                if value.strip().lower() in ("true", "1", "on"):
                                    self.last_motion = time.time()
            except Exception as e:
                log("onvif: {} - resubscribing in 10s".format(e))
                time.sleep(10)


# --- retention -------------------------------------------------------------

def clip_files():
    out = []
    for root, _, files in os.walk(CLIP_DIR):
        for f in files:
            if f.endswith(".mp4"):
                p = os.path.join(root, f)
                try:
                    out.append((os.path.getmtime(p), p))
                except OSError:
                    pass
    return sorted(out)


def remove_clip(path):
    """Delete a clip and the thumbnail that belongs to it."""
    for p in (path, os.path.splitext(path)[0] + ".jpg"):
        try:
            os.remove(p)
        except OSError:
            pass


def cleanup():
    cutoff = time.time() - RETENTION_DAYS * 86400
    for mtime, path in clip_files():
        if mtime < cutoff:
            remove_clip(path)
            log("retention: removed " + path)

    # then, if the disk is still tight, drop oldest clips until there is room
    while True:
        free_gb = shutil.disk_usage(CLIP_DIR).free / 1e9
        if free_gb >= MIN_FREE_GB:
            break
        files = clip_files()
        if not files:
            break
        remove_clip(files[0][1])
        log("low disk ({:.1f} GB free): removed {}".format(free_gb, files[0][1]))

    for d in os.listdir(CLIP_DIR):
        p = os.path.join(CLIP_DIR, d)
        if os.path.isdir(p) and not os.listdir(p):
            try:
                os.rmdir(p)
            except OSError:
                pass


def cleanup_loop():
    while True:
        try:
            cleanup()
        except Exception as e:
            log("cleanup error: {}".format(e))
        time.sleep(3600)


# --- main ------------------------------------------------------------------

def main():
    os.makedirs(CLIP_DIR, exist_ok=True)
    log("camera    {}".format(CAMERA_IP))
    log("record    {}".format(RECORD_URL))
    log("detect    {} via {}".format(DETECT_URL, MOTION_SOURCE))
    log("clips  -> {}  (keep {} days, min {} GB free)".format(
        CLIP_DIR, RETENTION_DAYS, MIN_FREE_GB))

    threading.Thread(target=cleanup_loop, daemon=True).start()

    detector = OnvifMotion() if MOTION_SOURCE == "onvif" else VideoMotion()
    detector.start()

    rec = Recorder()
    try:
        while True:
            # the detector refreshes last_motion continuously while something
            # is moving; a 2 s gap is treated as "motion has stopped"
            if time.time() - detector.last_motion <= 2.0:
                rec.start()
            else:
                rec.schedule_stop()
            rec.tick()
            time.sleep(0.5)
    except KeyboardInterrupt:
        log("shutting down")
        rec.stop_at = 0
        rec.tick()
        return 0


if __name__ == "__main__":
    sys.exit(main())
