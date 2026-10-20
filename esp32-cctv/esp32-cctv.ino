/*
 * ESP32 CCTV bridge - stage 1: pull the camera's RTSP stream and spot motion.
 *
 * The ESP32 never decodes video - it cannot. It only moves bytes and counts
 * them, which is well within what a WROOM-32 can do:
 *
 *   - speak RTSP over TCP and get the H.265 sub stream interleaved on the
 *     same socket (no UDP, so no NAT or packet-reorder trouble)
 *   - unpack RTP into H.265 NAL units (Annex-B), so the bytes are already in
 *     a form VLC/ffmpeg can play once we start saving them
 *   - watch how many bytes arrive per second. A still scene compresses to
 *     almost nothing; the moment something moves the bitrate jumps. That is
 *     our motion detector, and it costs no CPU.
 *
 * Upload to Oracle comes in the next stage. Right now everything is reported
 * over serial so the behaviour can be verified against the real camera.
 */

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include "secrets.h"

// ---- upload ---------------------------------------------------------------
// Video is collected into one buffer and PUT to Oracle Object Storage as a
// numbered .h265 segment. Segments are plain Annex-B, so concatenating them in
// order gives a file ffmpeg and VLC can play.

static void logf(const char *fmt, ...);    // defined below

static const size_t SEG_MAX = 48 * 1024;   // ~2 s of this stream
static uint8_t *segBuf = nullptr;
static size_t segLen = 0;
static uint32_t segSeq = 0;
static uint32_t uploadsOk = 0, uploadsFail = 0;

static bool uploadSegment(const uint8_t *data, size_t len) {
  WiFiClientSecure tls;
  tls.setInsecure();            // the PAR url is the credential; traffic is still encrypted
  tls.setTimeout(15000);
  if (!tls.connect(OCI_HOST, 443)) { uploadsFail++; return false; }

  char name[64];
  snprintf(name, sizeof(name), "esp32/%lu_%06lu.h265",
           (unsigned long)(millis() / 1000), (unsigned long)segSeq);

  char hdr[512];
  int n = snprintf(hdr, sizeof(hdr),
                   "PUT %s%s HTTP/1.1\r\n"
                   "Host: %s\r\n"
                   "Content-Type: application/octet-stream\r\n"
                   "Content-Length: %u\r\n"
                   "Connection: close\r\n\r\n",
                   OCI_PATH, name, OCI_HOST, (unsigned)len);
  tls.write((const uint8_t *)hdr, n);

  size_t sent = 0;
  while (sent < len) {
    size_t chunk = len - sent > 1460 ? 1460 : len - sent;
    int w = tls.write(data + sent, chunk);
    if (w <= 0) { tls.stop(); uploadsFail++; return false; }
    sent += w;
  }

  // read just the status line
  char status[64] = {0};
  uint32_t deadline = millis() + 15000;
  size_t si = 0;
  while (millis() < deadline && si < sizeof(status) - 1) {
    if (!tls.available()) { if (!tls.connected()) break; delay(5); continue; }
    char c = tls.read();
    if (c == '\n') break;
    status[si++] = c;
  }
  tls.stop();

  bool ok = strstr(status, " 200") || strstr(status, " 201");
  if (ok) uploadsOk++; else uploadsFail++;
  logf("    upload %s  %u bytes  -> %s", name, (unsigned)len, ok ? "OK" : status);
  segSeq++;
  return ok;
}

// ---- tuning ---------------------------------------------------------------

static const uint32_t STATS_MS = 1000;      // how often to report
static const float MOTION_MULTIPLIER = 2.2; // bitrate this many x baseline = motion
static const uint32_t MOTION_HOLD_MS = 4000;// keep "in motion" this long after it calms
static const uint32_t BASELINE_MIN_BYTES = 4000; // floor so a dead stream is not "motion"

// ---- RTSP state -----------------------------------------------------------

WiFiClient rtsp;
static char sessionId[64] = {0};
static int cseq = 1;

// rolling baseline of the quiet scene
static float baseline = 0;
static bool haveBaseline = false;
static bool inMotion = false;
static uint32_t motionUntil = 0;

// stream stats
static volatile uint32_t bytesThisSec = 0;
static uint32_t nalCount = 0, keyframeCount = 0;
static uint32_t lastStats = 0;

// RTP reassembly
static const size_t NAL_MAX = 48 * 1024;     // biggest single NAL we will hold
static const size_t PKT_MAX = 4096;          // one interleaved RTP packet
static uint8_t *nalBuf = nullptr;
static size_t nalLen = 0;
static bool nalActive = false;

// framing health
static uint32_t resyncs = 0, oversize = 0;

// Read exactly n bytes or give up. Partial reads are what break the framing,
// so the caller must treat false as "connection is no longer trustworthy".
static bool readExact(uint8_t *dst, size_t n, uint32_t timeoutMs) {
  size_t got = 0;
  uint32_t deadline = millis() + timeoutMs;
  while (got < n) {
    int r = rtsp.read(dst + got, n - got);
    if (r > 0) { got += r; continue; }
    if (!rtsp.connected() || millis() > deadline) return false;
    delay(1);
  }
  return true;
}

// ---- helpers --------------------------------------------------------------

static void logf(const char *fmt, ...) {
  char line[256];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(line, sizeof(line), fmt, ap);
  va_end(ap);
  Serial.printf("[%6lus] %s\n", millis() / 1000, line);
}

static bool sendRtsp(const char *method, const char *url, const char *extra) {
  char req[512];
  int n = snprintf(req, sizeof(req),
                   "%s %s RTSP/1.0\r\nCSeq: %d\r\nUser-Agent: esp32-cctv\r\n",
                   method, url, cseq++);
  if (sessionId[0]) n += snprintf(req + n, sizeof(req) - n, "Session: %s\r\n", sessionId);
  if (extra) n += snprintf(req + n, sizeof(req) - n, "%s", extra);
  n += snprintf(req + n, sizeof(req) - n, "\r\n");
  return rtsp.write((const uint8_t *)req, n) == (size_t)n;
}

// Read one RTSP reply (headers only; it ends at the blank line).
static bool readRtspReply(char *out, size_t cap) {
  size_t len = 0;
  uint32_t deadline = millis() + 5000;
  int newlines = 0;
  while (millis() < deadline && len < cap - 1) {
    if (!rtsp.available()) { delay(5); continue; }
    char c = rtsp.read();
    out[len++] = c;
    if (c == '\n') { if (++newlines == 2) break; }
    else if (c != '\r') newlines = 0;
  }
  out[len] = 0;
  return len > 0;
}

static bool rtspConnect() {
  logf("connecting to camera %s:%d", CAM_HOST, CAM_RTSP_PORT);
  if (!rtsp.connect(CAM_HOST, CAM_RTSP_PORT, 8000)) {
    logf("  TCP connect failed");
    return false;
  }
  rtsp.setNoDelay(true);

  char url[160], reply[1024];
  snprintf(url, sizeof(url), "rtsp://%s:%d%s", CAM_HOST, CAM_RTSP_PORT, CAM_RTSP_PATH);

  // DESCRIBE - we do not parse the SDP, but it confirms the path is live
  sendRtsp("DESCRIBE", url, "Accept: application/sdp\r\n");
  if (!readRtspReply(reply, sizeof(reply))) { logf("  no DESCRIBE reply"); return false; }
  if (!strstr(reply, "200 OK")) { logf("  DESCRIBE refused"); return false; }

  // SETUP on track1, asking for RTP interleaved on the same TCP socket
  char setupUrl[200];
  snprintf(setupUrl, sizeof(setupUrl), "%s/track1", url);
  sendRtsp("SETUP", setupUrl, "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n");
  if (!readRtspReply(reply, sizeof(reply))) { logf("  no SETUP reply"); return false; }
  if (!strstr(reply, "200 OK")) { logf("  SETUP refused"); return false; }

  // keep the session id (up to ';' which may carry a timeout)
  char *s = strstr(reply, "Session:");
  if (s) {
    s += 8;
    while (*s == ' ') s++;
    size_t i = 0;
    while (*s && *s != ';' && *s != '\r' && *s != '\n' && i < sizeof(sessionId) - 1)
      sessionId[i++] = *s++;
    sessionId[i] = 0;
  }
  logf("  session %s", sessionId[0] ? sessionId : "(none)");

  sendRtsp("PLAY", url, "Range: npt=0.000-\r\n");
  if (!readRtspReply(reply, sizeof(reply))) { logf("  no PLAY reply"); return false; }
  if (!strstr(reply, "200 OK")) { logf("  PLAY refused"); return false; }

  logf("  streaming");
  return true;
}

// ---- H.265 over RTP (RFC 7798) -------------------------------------------

static uint16_t typeSeen[64] = {0};   // which NAL types actually arrive

// ffprobe sees an I-frame every ~3 s in this same stream but we label every
// picture NAL as type 1, so dump the raw header of whatever follows a PPS -
// that is where the I-frame must be.
static uint8_t afterPps = 0;
static uint8_t lastPath = 0;          // 1=single 2=FU 3=AP
static bool pendingUpload = false;
static bool segNeedsKey = true;   // a segment must open on a keyframe

// A NAL is worth keeping only if it was assembled without overflowing.
static inline bool nalActiveValid() { return nalLen > 4 && nalLen <= NAL_MAX; }

static void onNalComplete(uint8_t nalType) {
  nalCount++;
  if (afterPps && nalLen >= 6) {
    logf("    after PPS: path %u  type %u  hdr %02X %02X  len %u",
         lastPath, nalType, nalBuf[4], nalBuf[5], (unsigned)nalLen);
    afterPps--;
  }
  // The oversized "type 32" unit is the one that will not decode. Show what is
  // actually inside it instead of guessing.
  static bool dumpedBig = false;
  if (!dumpedBig && nalLen > 10000) {
    dumpedBig = true;
    char hex[160]; size_t h = 0;
    for (size_t i = 0; i < 40 && h < sizeof(hex) - 4; i++)
      h += snprintf(hex + h, sizeof(hex) - h, "%02X ", nalBuf[i]);
    logf("    BIG nal type %u len %u first40: %s", nalType, (unsigned)nalLen, hex);
  }

  if (nalType == 34) afterPps = 2;    // PPS -> watch the next two NALs
  if (nalType < 64) typeSeen[nalType]++;

  // While something is moving, keep the finished NAL for upload. This camera
  // wraps a whole keyframe access unit in one type-32 unit, so a segment that
  // begins at type 32 starts on a keyframe and decodes on its own.
  if (inMotion && segBuf && nalActiveValid()) {
    // A segment is only playable if it opens on a keyframe. This camera ships
    // the whole keyframe access unit (VPS+SPS+PPS+IDR, start codes and all) as
    // one big type-32 unit, so that is what we wait for before recording.
    if (segNeedsKey) {
      if (nalType == 32 && nalLen > 1000) segNeedsKey = false;
      else return;                    // still mid-GOP, nothing decodable yet
    }
    if (segLen + nalLen <= SEG_MAX) {
      memcpy(segBuf + segLen, nalBuf, nalLen);
      segLen += nalLen;
    } else {
      pendingUpload = true;           // full - loop() ships it
    }
  }
  // 16..21 are IRAP pictures (IDR / CRA) - the frames a clip can start from
  if (nalType >= 16 && nalType <= 21) keyframeCount++;
  nalLen = 0;
  nalActive = false;
}

static void startNal(uint8_t hdr0, uint8_t hdr1) {
  nalLen = 0;
  // Annex-B start code, so what we collect is already playable bytes
  nalBuf[nalLen++] = 0x00; nalBuf[nalLen++] = 0x00;
  nalBuf[nalLen++] = 0x00; nalBuf[nalLen++] = 0x01;
  nalBuf[nalLen++] = hdr0; nalBuf[nalLen++] = hdr1;
  nalActive = true;
}

static void appendNal(const uint8_t *p, size_t n) {
  if (!nalActive) return;
  if (nalLen + n > NAL_MAX) { nalActive = false; return; }  // oversized, drop it
  memcpy(nalBuf + nalLen, p, n);
  nalLen += n;
}

static void handleRtp(const uint8_t *pkt, size_t len) {
  if (len < 12) return;
  uint8_t cc = pkt[0] & 0x0F;
  size_t off = 12 + cc * 4;
  if (pkt[0] & 0x10) {                       // extension header present
    if (len < off + 4) return;
    size_t extWords = (pkt[off + 2] << 8) | pkt[off + 3];
    off += 4 + extWords * 4;
  }
  if (len <= off + 2) return;

  const uint8_t *pl = pkt + off;
  size_t plLen = len - off;

  uint8_t type = (pl[0] >> 1) & 0x3F;

  if (type == 49) {                          // fragmentation unit
    if (plLen < 3) return;
    uint8_t fu = pl[2];
    uint8_t fuType = fu & 0x3F;
    bool start = fu & 0x80, end = fu & 0x40;
    if (start) {
      uint8_t h0 = (pl[0] & 0x81) | (fuType << 1);
      startNal(h0, pl[1]);
    }
    appendNal(pl + 3, plLen - 3);
    if (end) { lastPath = 2; onNalComplete(fuType); }
  } else if (type == 48) {                   // aggregation packet
    size_t p = 2;
    while (p + 2 <= plLen) {
      size_t sz = (pl[p] << 8) | pl[p + 1];
      p += 2;
      if (p + sz > plLen || sz < 2) break;
      startNal(pl[p], pl[p + 1]);
      appendNal(pl + p + 2, sz - 2);
      lastPath = 3;
      onNalComplete((pl[p] >> 1) & 0x3F);
      p += sz;
    }
  } else {                                   // single NAL unit
    startNal(pl[0], pl[1]);
    if (plLen > 2) appendNal(pl + 2, plLen - 2);
    lastPath = 1;
    onNalComplete(type);
  }
}

// ---- motion from bitrate --------------------------------------------------

static void reportSecond(uint32_t bytes) {
  float kb = bytes / 1024.0;

  if (!haveBaseline) {
    if (bytes > BASELINE_MIN_BYTES) { baseline = bytes; haveBaseline = true; }
  } else if (!inMotion) {
    baseline = baseline * 0.9 + bytes * 0.1;          // drift with the scene
  } else {
    // Keep creeping towards the current level even while recording. Without
    // this the baseline froze at whatever it was when motion started, and the
    // comparison stayed true forever.
    baseline = baseline * 0.99 + bytes * 0.01;
  }

  bool spike = haveBaseline && bytes > baseline * MOTION_MULTIPLIER &&
               bytes > BASELINE_MIN_BYTES;
  if (spike) {
    if (!inMotion) logf("** MOTION ** %.1f KB/s vs baseline %.1f KB/s",
                        kb, baseline / 1024.0);
    inMotion = true;
    motionUntil = millis() + MOTION_HOLD_MS;
  } else if (inMotion && millis() > motionUntil) {
    inMotion = false;
    logf("-- motion ended --");
  }

  logf("%.1f KB/s  base %.1f  nal %lu  key %lu  resync %lu  big %lu  heap %u%s",
       kb, baseline / 1024.0, nalCount, keyframeCount, resyncs, oversize,
       (unsigned)ESP.getFreeHeap(), inMotion ? "  [REC]" : "");
  // show exactly which NAL types arrived, so nothing is guessed
  char types[120]; size_t tp = 0;
  for (int i = 0; i < 64 && tp < sizeof(types) - 12; i++) {
    if (typeSeen[i]) {
      tp += snprintf(types + tp, sizeof(types) - tp, "%d:%u ", i, typeSeen[i]);
      typeSeen[i] = 0;
    }
  }
  types[tp] = 0;
  if (tp) logf("    nal types -> %s", types);

  nalCount = keyframeCount = 0;
  resyncs = oversize = 0;
}

// ---- main -----------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println();
  logf("ESP32 CCTV bridge starting");
  logf("free heap at boot: %u", (unsigned)ESP.getFreeHeap());

  nalBuf = (uint8_t *)malloc(NAL_MAX);
  if (!nalBuf) { logf("FATAL: cannot allocate NAL buffer"); while (true) delay(1000); }
  segBuf = (uint8_t *)malloc(SEG_MAX);
  if (!segBuf) { logf("FATAL: cannot allocate segment buffer"); while (true) delay(1000); }
  logf("buffers: nal %u + segment %u, heap left %u",
       (unsigned)NAL_MAX, (unsigned)SEG_MAX, (unsigned)ESP.getFreeHeap());

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  logf("joining WiFi \"%s\"", WIFI_SSID);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 30000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() != WL_CONNECTED) {
    logf("WiFi FAILED - check the name/password in secrets.h");
    return;
  }
  logf("WiFi ok, ESP32 is %s, signal %d dBm",
       WiFi.localIP().toString().c_str(), WiFi.RSSI());
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) { delay(1000); return; }

  if (!rtsp.connected()) {
    sessionId[0] = 0;
    if (!rtspConnect()) {
      rtsp.stop();
      logf("retrying in 5s");
      delay(5000);
      return;
    }
    lastStats = millis();
    bytesThisSec = 0;
  }

  // Interleaved frame: '$' | channel | length(2, big endian) | RTP packet.
  // Framing has to stay exact: one short read and every following packet is
  // garbage, which is what was eating the keyframes.
  uint32_t budget = millis() + 200;           // keep loop() responsive
  while (millis() < budget && rtsp.connected()) {
    uint8_t hdr[4];
    if (!readExact(hdr, 1, 50)) break;        // nothing waiting right now
    if (hdr[0] != '$') { resyncs++; continue; }   // scan on for the marker
    if (!readExact(hdr + 1, 3, 500)) break;

    int ch = hdr[1];
    size_t plen = ((size_t)hdr[2] << 8) | hdr[3];
    if (plen == 0) continue;

    static uint8_t pkt[PKT_MAX];
    if (plen <= PKT_MAX) {
      if (!readExact(pkt, plen, 1000)) break;
      bytesThisSec += plen;
      if (ch == 0) handleRtp(pkt, plen);      // channel 0 = RTP, 1 = RTCP
    } else {
      // Too big for our buffer: swallow it rather than bailing out, so the
      // next '$' still lands where it should.
      oversize++;
      bytesThisSec += plen;
      size_t left = plen;
      while (left) {
        size_t chunk = left > PKT_MAX ? PKT_MAX : left;
        if (!readExact(pkt, chunk, 1000)) return;
        left -= chunk;
      }
    }
  }

  // Ship a full segment, or flush what we have once motion is over.
  if (pendingUpload || (!inMotion && segLen > 8192)) {
    size_t len = segLen;
    segLen = 0;                 // free the buffer before the slow TLS upload
    pendingUpload = false;
    segNeedsKey = true;          // next segment waits for the next keyframe
    uploadSegment(segBuf, len);
  }

  if (millis() - lastStats >= STATS_MS) {
    // reset to "now" rather than stepping by STATS_MS: if a read ever blocks
    // for longer than a second, stepping would fire a burst of catch-up reports
    lastStats = millis();
    uint32_t b = bytesThisSec;
    bytesThisSec = 0;
    reportSecond(b);
  }
}
