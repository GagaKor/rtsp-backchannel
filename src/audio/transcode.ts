/** Transcode any audio file to raw G.711 (8 kHz mono) via ffmpeg. */
import { spawn } from 'node:child_process';
import type { G711Variant } from './g711.ts';

/**
 * Decode `path` (wav/aiff/mp3/...) and re-encode to raw G.711 bytes
 * (1 byte/sample, 8 kHz mono). Requires ffmpeg on PATH.
 */
export function fileToG711(path: string, variant: G711Variant): Promise<Buffer> {
  const format = variant === 'PCMA' ? 'alaw' : 'mulaw';
  return new Promise((resolve, reject) => {
    const ff = spawn('ffmpeg', [
      '-hide_banner',
      '-loglevel', 'error',
      '-i', path,
      '-ar', String(8000),
      '-ac', String(1),
      '-f', format,
      '-', // raw G.711 to stdout
    ]);
    const out: Buffer[] = [];
    const err: Buffer[] = [];
    ff.stdout.on('data', (c) => out.push(c));
    ff.stderr.on('data', (c) => err.push(c));
    ff.on('error', (e) =>
      reject(new Error(`ffmpeg spawn failed (installed?): ${e.message}`)),
    );
    ff.on('close', (code) => {
      if (code === 0) resolve(Buffer.concat(out));
      else reject(new Error(`ffmpeg exited ${code}: ${Buffer.concat(err).toString().trim()}`));
    });
  });
}
