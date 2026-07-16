/** Decode an audio file with FFmpeg, then encode G.711 in TypeScript. */
import { spawn } from 'node:child_process';
import { pcm16ToG711, type G711Variant } from './g711.ts';

const MAX_DECODED_BYTES = 128 * 1024 * 1024;
const MAX_DIAGNOSTIC_BYTES = 64 * 1024;
const RESAMPLE_FILTER =
  'aresample=8000:resampler=swr:filter_size=32:phase_shift=10:' +
  'linear_interp=1:exact_rational=1:cutoff=0.97:dither_method=none:' +
  'osf=s16:ochl=mono';

/**
 * Decode `path` (wav/aiff/mp3/...) to S16LE, apply Q11 volume, and encode
 * raw G.711 bytes (1 byte/sample, 8 kHz mono). Requires FFmpeg on PATH.
 */
export function fileToG711(
  path: string,
  variant: G711Variant,
  volume = 0.05,
): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const ff = spawn('ffmpeg', [
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
    ]);
    const out: Buffer[] = [];
    const err: Buffer[] = [];
    let outputBytes = 0;
    let diagnosticBytes = 0;
    let overflow = false;
    ff.stdout.on('data', (chunk: Buffer) => {
      outputBytes += chunk.length;
      if (outputBytes > MAX_DECODED_BYTES) {
        overflow = true;
        ff.kill('SIGKILL');
        return;
      }
      out.push(chunk);
    });
    ff.stderr.on('data', (chunk: Buffer) => {
      const remaining = MAX_DIAGNOSTIC_BYTES - diagnosticBytes;
      if (remaining <= 0) return;
      err.push(chunk.subarray(0, remaining));
      diagnosticBytes += Math.min(chunk.length, remaining);
    });
    ff.on('error', (e) =>
      reject(new Error(`ffmpeg spawn failed (installed?): ${e.message}`)),
    );
    ff.on('close', (code) => {
      if (overflow) {
        reject(new Error(`ffmpeg decoded output exceeds ${MAX_DECODED_BYTES} bytes`));
        return;
      }
      if (code !== 0) {
        reject(new Error(`ffmpeg exited ${code}: ${Buffer.concat(err).toString().trim()}`));
        return;
      }
      const decoded = Buffer.concat(out);
      if (decoded.length % 2 !== 0) {
        reject(new Error('ffmpeg returned an incomplete S16LE sample'));
        return;
      }
      const pcm = new Int16Array(decoded.length / 2);
      for (let offset = 0; offset < decoded.length; offset += 2) {
        pcm[offset / 2] = decoded.readInt16LE(offset);
      }
      resolve(pcm16ToG711(pcm, variant, volume));
    });
  });
}
