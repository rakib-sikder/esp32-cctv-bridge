// Wrap a raw Annex-B H.265 stream in an mp4 container.
//
// The camera's clips are bare H.265 with start codes - no container - so no
// browser will play them. Re-encoding is unnecessary (phones decode H.265
// fine), the bytes just need an mp4 around them. That is all this does, which
// is why it is a few hundred lines instead of a 32 MB ffmpeg build.
//
// Works in the browser and in Node (same code, no dependencies).

const VCL_MAX = 31;        // NAL types 0..31 are coded slices
const VPS = 32, SPS = 33, PPS = 34;

// --- Annex-B -> NAL units ---------------------------------------------------

function splitNals(buf) {
  const nals = [];
  let i = 0, start = -1;
  while (i < buf.length - 3) {
    if (buf[i] === 0 && buf[i + 1] === 0 &&
        (buf[i + 2] === 1 || (buf[i + 2] === 0 && buf[i + 3] === 1))) {
      const scLen = buf[i + 2] === 1 ? 3 : 4;
      if (start >= 0) nals.push(buf.subarray(start, i));
      start = i + scLen;
      i += scLen;
    } else {
      i++;
    }
  }
  if (start >= 0 && start < buf.length) nals.push(buf.subarray(start, buf.length));
  return nals.filter(n => n.length >= 2);
}

const nalType = n => (n[0] >> 1) & 0x3f;

// Strip the 0x03 bytes H.26x inserts so payload can never look like a start code.
function rbsp(nal) {
  const out = new Uint8Array(nal.length);
  let o = 0, zeros = 0;
  for (let i = 0; i < nal.length; i++) {
    if (zeros === 2 && nal[i] === 3) { zeros = 0; continue; }
    out[o++] = nal[i];
    zeros = nal[i] === 0 ? zeros + 1 : 0;
  }
  return out.subarray(0, o);
}

// --- just enough SPS parsing for the hvcC header ----------------------------

class Bits {
  constructor(b) { this.b = b; this.p = 0; }
  u(n) { let v = 0; for (let i = 0; i < n; i++) {
    v = (v * 2) + ((this.b[this.p >> 3] >> (7 - (this.p & 7))) & 1); this.p++; } return v; }
}

function parseSps(sps) {
  const r = rbsp(sps);
  const s = new Bits(r);
  s.u(16);                       // NAL header
  s.u(4);                        // sps_video_parameter_set_id
  const maxSub = s.u(3);         // sps_max_sub_layers_minus1
  s.u(1);                        // sps_temporal_id_nesting_flag
  // profile_tier_level
  const space = s.u(2), tier = s.u(1), profile = s.u(5);
  const compat = [s.u(8), s.u(8), s.u(8), s.u(8)];
  const constraint = [s.u(8), s.u(8), s.u(8), s.u(8), s.u(8), s.u(8)];
  const level = s.u(8);
  return { space, tier, profile, compat, constraint, level, maxSub };
}

// --- box writing ------------------------------------------------------------

const enc = s => Uint8Array.from(s, c => c.charCodeAt(0));

function box(type, ...payload) {
  let len = 8;
  for (const p of payload) len += p.length;
  const out = new Uint8Array(len);
  const dv = new DataView(out.buffer);
  dv.setUint32(0, len);
  out.set(enc(type), 4);
  let o = 8;
  for (const p of payload) { out.set(p, o); o += p.length; }
  return out;
}

function u32(...vals) {
  const a = new Uint8Array(vals.length * 4);
  const dv = new DataView(a.buffer);
  vals.forEach((v, i) => dv.setUint32(i * 4, v >>> 0));
  return a;
}
function u16(...vals) {
  const a = new Uint8Array(vals.length * 2);
  const dv = new DataView(a.buffer);
  vals.forEach((v, i) => dv.setUint16(i * 2, v & 0xffff));
  return a;
}
const u8 = (...v) => Uint8Array.from(v);

function concat(arrs) {
  let n = 0;
  for (const a of arrs) n += a.length;
  const out = new Uint8Array(n);
  let o = 0;
  for (const a of arrs) { out.set(a, o); o += a.length; }
  return out;
}

// --- hvcC -------------------------------------------------------------------

function hvcC(vps, sps, pps, info) {
  const sets = [[VPS, vps], [SPS, sps], [PPS, pps]].filter(([, n]) => n);
  const arrays = sets.map(([t, n]) => concat([
    u8(0x80 | t),        // array_completeness | NAL type
    u16(1),              // one NAL of this type
    u16(n.length), n,
  ]));
  return box("hvcC", concat([
    u8(1),                                        // configurationVersion
    u8((info.space << 6) | (info.tier << 5) | info.profile),
    u8(...info.compat),
    u8(...info.constraint),
    u8(info.level),
    u16(0xf000),                                  // min_spatial_segmentation
    u8(0xfc),                                     // parallelismType
    u8(0xfc | 1),                                 // chromaFormat 4:2:0
    u8(0xf8),                                     // bitDepthLumaMinus8 = 0
    u8(0xf8),                                     // bitDepthChromaMinus8 = 0
    u16(0),                                       // avgFrameRate (0 = unspecified)
    u8((0 << 6) | (1 << 3) | (0 << 2) | 3),       // constantFrameRate, numTemporalLayers, lengthSizeMinusOne=3
    u8(sets.length),
    concat(arrays),
  ]));
}

// --- the muxer --------------------------------------------------------------

/**
 * @param {Uint8Array} annexb  concatenated raw H.265
 * @param {object} opts  { width, height, fps }
 * @returns {Uint8Array} an mp4 file
 */
export function muxHevcToMp4(annexb, opts = {}) {
  const fps = opts.fps || 14;
  const nals = splitNals(annexb);

  let vps = null, sps = null, pps = null;
  const samples = [];            // { nals: [], key: bool }
  let cur = null;

  for (const n of nals) {
    const t = nalType(n);
    if (t === VPS) { vps = n; continue; }
    if (t === SPS) { sps = n; continue; }
    if (t === PPS) { pps = n; continue; }
    if (t > VCL_MAX) continue;   // SEI and friends: not needed for playback

    // Every coded slice starts a new sample. Parameter sets are carried in
    // hvcC, so they do not go in the samples.
    cur = { nals: [n], key: t >= 16 && t <= 23 };
    samples.push(cur);
  }

  if (!sps || !samples.length) {
    throw new Error("no usable video found (need an SPS and at least one frame)");
  }

  const info = parseSps(sps);
  const width = opts.width || 640, height = opts.height || 720;

  // mdat: each sample is its NALs, each prefixed with a 4-byte length
  const chunks = [];
  const sizes = [];
  for (const s of samples) {
    let size = 0;
    for (const n of s.nals) {
      chunks.push(u32(n.length));
      chunks.push(n);
      size += 4 + n.length;
    }
    sizes.push(size);
  }
  const mdatPayload = concat(chunks);
  const mdat = box("mdat", mdatPayload);

  const timescale = 1000;
  const dur = Math.round(timescale / fps);
  const total = dur * samples.length;

  const mvhd = box("mvhd", concat([
    u32(0), u32(0), u32(0), u32(timescale), u32(total), u32(0x00010000),
    u16(0x0100), u16(0), u32(0, 0),
    u32(0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000),
    u32(0, 0, 0, 0, 0, 0), u32(2),
  ]));

  const tkhd = box("tkhd", concat([
    u32(0x00000007), u32(0), u32(0), u32(1), u32(0), u32(total),
    u32(0, 0), u16(0), u16(0), u16(0), u16(0),
    u32(0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000),
    u32(width << 16), u32(height << 16),
  ]));

  const mdhd = box("mdhd", concat([
    u32(0), u32(0), u32(0), u32(timescale), u32(total), u16(0x55c4), u16(0),
  ]));
  const hdlr = box("hdlr", concat([
    u32(0), u32(0), enc("vide"), u32(0, 0, 0), enc("VideoHandler\0"),
  ]));

  const hvc1 = box("hvc1", concat([
    u8(0, 0, 0, 0, 0, 0), u16(1),
    u16(0), u16(0), u32(0, 0, 0),
    u16(width), u16(height),
    u32(0x00480000), u32(0x00480000), u32(0), u16(1),
    new Uint8Array(32),
    u16(0x0018), u16(0xffff),
    hvcC(vps, sps, pps, info),
  ]));

  const stsd = box("stsd", concat([u32(0), u32(1), hvc1]));
  const stts = box("stts", concat([u32(0), u32(1), u32(samples.length), u32(dur)]));

  const keys = [];
  samples.forEach((s, i) => { if (s.key) keys.push(i + 1); });
  const stss = keys.length && keys.length < samples.length
    ? box("stss", concat([u32(0), u32(keys.length), u32(...keys)]))
    : new Uint8Array(0);

  const stsc = box("stsc", concat([u32(0), u32(1), u32(1), u32(samples.length), u32(1)]));
  const stsz = box("stsz", concat([u32(0), u32(0), u32(sizes.length), u32(...sizes)]));

  // one chunk holding every sample; its offset is known once the moov size is
  // known, so build moov with a placeholder and patch it afterwards
  const stcoAt = { v: 0 };
  const makeMoov = (chunkOffset) => {
    const stco = box("stco", concat([u32(0), u32(1), u32(chunkOffset)]));
    const stbl = box("stbl", stsd, stts, stss, stsc, stsz, stco);
    const dinf = box("dinf", box("dref", concat([u32(0), u32(1),
      box("url ", u32(0x00000001))])));
    const minf = box("minf", box("vmhd", concat([u32(1), u16(0, 0, 0, 0)])), dinf, stbl);
    const mdia = box("mdia", mdhd, hdlr, minf);
    const trak = box("trak", tkhd, mdia);
    return box("moov", mvhd, trak);
  };

  const ftyp = box("ftyp", concat([
    enc("isom"), u32(0x200), enc("isom"), enc("iso2"), enc("hvc1"), enc("mp41"),
  ]));

  const probe = makeMoov(0);
  const mdatDataOffset = ftyp.length + probe.length + 8;
  const moov = makeMoov(mdatDataOffset);

  return concat([ftyp, moov, mdat]);
}

export default muxHevcToMp4;
