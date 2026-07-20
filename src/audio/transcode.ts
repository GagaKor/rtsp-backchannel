/** Decode an audio file with FFmpeg, then encode G.711 in TypeScript. */
import { spawn } from 'node:child_process';
import { pcm16ToG711, type G711Variant } from './g711.ts';
import type { SendCodec } from '../rtsp/sdp.ts';

const MAX_DECODED_BYTES = 128 * 1024 * 1024;
const MAX_DIAGNOSTIC_BYTES = 64 * 1024;
export const FFMPEG_TIMEOUT_MS = 120_000;
const RESAMPLE_FILTER =
  'aresample=8000:resampler=swr:filter_size=32:phase_shift=10:' +
  'linear_interp=1:exact_rational=1:cutoff=0.97:dither_method=none:' +
  'osf=s16:ochl=mono';

export interface EncodedAudioFrame {
  /** Complete RTP payload, excluding the RTP header. */
  payload: Buffer;
  /** RTP timestamp ticks represented by this payload. */
  samples: number;
}

export interface EncodedAudio {
  codec: SendCodec['name'];
  clockRate: number;
  frames: EncodedAudioFrame[];
  byteLength: number;
  sampleCount: number;
}

export interface AdtsFrame extends EncodedAudioFrame {
  sampleRate: number;
  channels: number;
}

export interface FfmpegRuntime {
  spawn: typeof spawn;
  setTimeout: typeof setTimeout;
  clearTimeout: typeof clearTimeout;
}

const ffmpegRuntime: FfmpegRuntime = {
  spawn,
  setTimeout,
  clearTimeout,
};

export function runFfmpeg(
  args: string[],
  label = 'ffmpeg',
  runtime: FfmpegRuntime = ffmpegRuntime,
): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const ff = runtime.spawn('ffmpeg', args);
    const out: Buffer[] = [];
    const err: Buffer[] = [];
    let outputBytes = 0;
    let diagnosticBytes = 0;
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const settle = (error?: Error, output?: Buffer, kill = false): void => {
      if (settled) return;
      settled = true;
      if (timer !== undefined) runtime.clearTimeout(timer);
      if (kill) ff.kill('SIGKILL');
      if (error) reject(error);
      else resolve(output ?? Buffer.alloc(0));
    };

    ff.stdout.on('data', (chunk: Buffer) => {
      if (settled) return;
      outputBytes += chunk.length;
      if (outputBytes > MAX_DECODED_BYTES) {
        settle(new Error(`${label} output exceeds ${MAX_DECODED_BYTES} bytes`), undefined, true);
        return;
      }
      out.push(chunk);
    });
    ff.stderr.on('data', (chunk: Buffer) => {
      if (settled) return;
      const remaining = MAX_DIAGNOSTIC_BYTES - diagnosticBytes;
      if (remaining <= 0) return;
      err.push(chunk.subarray(0, remaining));
      diagnosticBytes += Math.min(chunk.length, remaining);
    });
    ff.on('error', (error) => {
      settle(new Error(`${label} spawn failed (installed?): ${error.message}`));
    });
    ff.on('close', (code) => {
      if (code !== 0) {
        settle(new Error(`${label} exited ${code}: ${Buffer.concat(err).toString().trim()}`));
        return;
      }
      settle(undefined, Buffer.concat(out));
    });
    timer = runtime.setTimeout(() => {
      settle(new Error(`${label} timed out after ${FFMPEG_TIMEOUT_MS} ms`), undefined, true);
    }, FFMPEG_TIMEOUT_MS);
  });
}

/**
 * Decode `path` (wav/aiff/mp3/...) to S16LE, apply Q11 volume, and encode
 * raw G.711 bytes (1 byte/sample, 8 kHz mono). Requires FFmpeg on PATH.
 */
export function fileToG711(
  path: string,
  variant: G711Variant,
  volume = 0.05,
): Promise<Buffer> {
  return runFfmpeg([
      '-nostdin',
      '-hide_banner',
      '-loglevel', 'error',
      '-i', path,
      '-map', '0:a:0',
      '-vn',
      '-sn',
      '-dn',
      '-af', RESAMPLE_FILTER,
      '-c:a', 'pcm_s16le',
      '-f', 's16le',
      '-fs', String(MAX_DECODED_BYTES + 1),
      'pipe:1',
    ])
    .then((decoded) => {
      if (decoded.length % 2 !== 0) {
        throw new Error('ffmpeg returned an incomplete S16LE sample');
      }
      const pcm = new Int16Array(decoded.length / 2);
      for (let offset = 0; offset < decoded.length; offset += 2) {
        pcm[offset / 2] = decoded.readInt16LE(offset);
      }
      return pcm16ToG711(pcm, variant, volume);
    });
}

const ADTS_SAMPLE_RATES = [
  96_000, 88_200, 64_000, 48_000, 44_100, 32_000, 24_000,
  22_050, 16_000, 12_000, 11_025, 8_000, 7_350,
];

/** Split an FFmpeg ADTS stream into raw AAC access units. */
export function parseAdtsFrames(data: Buffer): AdtsFrame[] {
  const frames: AdtsFrame[] = [];
  let offset = 0;
  while (offset < data.length) {
    if (
      offset + 7 > data.length ||
      data[offset] !== 0xff ||
      (data[offset + 1] & 0xf6) !== 0xf0
    ) {
      throw new Error(`invalid ADTS frame at byte ${offset}`);
    }
    const protectionAbsent = data[offset + 1] & 0x01;
    const headerLength = protectionAbsent ? 7 : 9;
    const frameLength =
      ((data[offset + 3] & 0x03) << 11) |
      (data[offset + 4] << 3) |
      (data[offset + 5] >> 5);
    if (frameLength < headerLength || offset + frameLength > data.length) {
      throw new Error(`truncated ADTS frame at byte ${offset}`);
    }
    const sampleRateIndex = (data[offset + 2] >> 2) & 0x0f;
    const sampleRate = ADTS_SAMPLE_RATES[sampleRateIndex];
    if (!sampleRate) throw new Error(`unsupported ADTS sample rate at byte ${offset}`);
    const channels =
      ((data[offset + 2] & 0x01) << 2) |
      ((data[offset + 3] >> 6) & 0x03);
    const rawDataBlocks = data[offset + 6] & 0x03;
    if (rawDataBlocks !== 0) {
      throw new Error(`ADTS raw_data_blocks_in_frame must be 0 at byte ${offset}`);
    }
    frames.push({
      payload: data.subarray(offset + headerLength, offset + frameLength),
      samples: 1024,
      sampleRate,
      channels,
    });
    offset += frameLength;
  }
  return frames;
}

/** Add the one-AU AAC-hbr header required by RFC 3640. */
export function aacRfc3640Payload(accessUnit: Buffer): Buffer {
  if (accessUnit.length > 0x1fff) {
    throw new Error(`AAC access unit too large: ${accessUnit.length} bytes`);
  }
  const payload = Buffer.alloc(4 + accessUnit.length);
  payload.writeUInt16BE(16, 0);
  payload.writeUInt16BE(accessUnit.length << 3, 2);
  accessUnit.copy(payload, 4);
  return payload;
}

function encodedAudio(
  codec: SendCodec,
  frames: EncodedAudioFrame[],
): EncodedAudio {
  return {
    codec: codec.name,
    clockRate: codec.clockRate,
    frames,
    byteLength: frames.reduce((total, frame) => total + frame.payload.length, 0),
    sampleCount: frames.reduce((total, frame) => total + frame.samples, 0),
  };
}

function ffmpegInput(path: string, volume: number): string[] {
  return [
    '-nostdin',
    '-hide_banner',
    '-loglevel', 'error',
    '-i', path,
    '-map', '0:a:0',
    '-vn',
    '-sn',
    '-dn',
    '-af', `volume=${volume}`,
  ];
}

/** Encode a file into complete, timestamped RTP audio payloads. */
export async function fileToRtpAudio(
  path: string,
  codec: SendCodec,
  volume = 0.05,
): Promise<EncodedAudio> {
  if (codec.name === 'pcma' || codec.name === 'pcmu') {
    const variant: G711Variant = codec.name === 'pcma' ? 'PCMA' : 'PCMU';
    const output = await fileToG711(path, variant, volume);
    const frames: EncodedAudioFrame[] = [];
    for (let offset = 0; offset < output.length; offset += 320) {
      const payload = output.subarray(offset, offset + 320);
      frames.push({ payload, samples: payload.length });
    }
    return encodedAudio(codec, frames);
  }

  if (codec.name.startsWith('g726-')) {
    if (codec.clockRate !== 8000) {
      throw new Error(`${codec.encoding} requires an 8000 Hz RTP clock`);
    }
    const bitrateKbps = Number(codec.name.slice('g726-'.length));
    const bitsPerSample = bitrateKbps / 8;
    const bytesPerFrame = bitrateKbps * 5;
    const output = await runFfmpeg([
      ...ffmpegInput(path, volume),
      '-ar', '8000',
      '-ac', '1',
      '-c:a', 'g726le',
      '-b:a', `${bitrateKbps}k`,
      '-f', 'g726le',
      '-fs', String(MAX_DECODED_BYTES + 1),
      'pipe:1',
    ], 'ffmpeg G.726');
    const frames: EncodedAudioFrame[] = [];
    for (let offset = 0; offset < output.length; offset += bytesPerFrame) {
      const payload = output.subarray(offset, offset + bytesPerFrame);
      const samples = (payload.length * 8) / bitsPerSample;
      if (!Number.isInteger(samples)) {
        throw new Error(`ffmpeg returned an incomplete ${codec.encoding} sample`);
      }
      frames.push({ payload, samples });
    }
    return encodedAudio(codec, frames);
  }

  if (codec.name === 'aac') {
    const channels = codec.channels ?? 1;
    const bitrateKbps = Math.min(64, Math.max(16, Math.round(codec.clockRate / 250)));
    const output = await runFfmpeg([
      ...ffmpegInput(path, volume),
      '-ar', String(codec.clockRate),
      '-ac', String(channels),
      '-c:a', 'aac',
      '-profile:a', 'aac_low',
      '-b:a', `${bitrateKbps}k`,
      '-f', 'adts',
      '-fs', String(MAX_DECODED_BYTES + 1),
      'pipe:1',
    ], 'ffmpeg AAC');
    const accessUnits = parseAdtsFrames(output);
    const frames = accessUnits.map((frame) => {
      if (frame.sampleRate !== codec.clockRate) {
        throw new Error(
          `ffmpeg AAC sample rate ${frame.sampleRate} does not match RTP clock ${codec.clockRate}`,
        );
      }
      if (frame.channels !== channels) {
        throw new Error(
          `ffmpeg AAC channel count ${frame.channels} does not match SDP channels ${channels}`,
        );
      }
      return { payload: aacRfc3640Payload(frame.payload), samples: frame.samples };
    });
    return encodedAudio(codec, frames);
  }

  throw new Error(`fileToRtpAudio does not support codec ${codec.name}`);
}
