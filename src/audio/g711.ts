/** G.711 µ-law / A-law encoding + simple tone/PCM helpers (8 kHz mono). */

const MULAW_BIAS = 0x84;
const MULAW_CLIP = 32635;

/** Encode one 16-bit linear PCM sample to G.711 µ-law (PCMU). */
export function linearToMuLaw(sample: number): number {
  let sign = (sample >> 8) & 0x80;
  if (sign !== 0) sample = -sample;
  if (sample > MULAW_CLIP) sample = MULAW_CLIP;
  sample += MULAW_BIAS;
  let exponent = 7;
  for (let mask = 0x4000; (sample & mask) === 0 && exponent > 0; mask >>= 1) exponent--;
  const mantissa = (sample >> (exponent + 3)) & 0x0f;
  return ~(sign | (exponent << 4) | mantissa) & 0xff;
}

const ALAW_CLIP = 32635;

/** Encode one 16-bit linear PCM sample to G.711 A-law (PCMA). */
export function linearToALaw(sample: number): number {
  let sign = (sample & 0x8000) >> 8;
  if (sign !== 0) sample = -sample;
  if (sample > ALAW_CLIP) sample = ALAW_CLIP;
  let compressed: number;
  if (sample >= 256) {
    let exponent = 7;
    for (let mask = 0x4000; (sample & mask) === 0 && exponent > 0; mask >>= 1) exponent--;
    const mantissa = (sample >> (exponent + 3)) & 0x0f;
    compressed = (exponent << 4) | mantissa;
  } else {
    compressed = sample >> 4;
  }
  return (compressed ^ (sign !== 0 ? 0x55 : 0xd5)) & 0xff;
}

export type G711Variant = 'PCMU' | 'PCMA';

/** Encode an Int16 PCM buffer to a G.711 byte buffer (1 byte/sample). */
export function pcm16ToG711(pcm: Int16Array, variant: G711Variant): Buffer {
  const enc = variant === 'PCMA' ? linearToALaw : linearToMuLaw;
  const out = Buffer.allocUnsafe(pcm.length);
  for (let i = 0; i < pcm.length; i++) out[i] = enc(pcm[i]);
  return out;
}

/** Generate a mono sine tone as 16-bit PCM. */
export function generateTonePcm(
  freqHz: number,
  durationMs: number,
  sampleRate = 8000,
  amplitude = 0.5,
): Int16Array {
  const n = Math.round((durationMs / 1000) * sampleRate);
  const pcm = new Int16Array(n);
  const amp = Math.max(0, Math.min(1, amplitude)) * 32767;
  for (let i = 0; i < n; i++) {
    pcm[i] = Math.round(amp * Math.sin((2 * Math.PI * freqHz * i) / sampleRate));
  }
  return pcm;
}
