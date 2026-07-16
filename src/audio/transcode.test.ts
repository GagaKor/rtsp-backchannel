import assert from 'node:assert/strict';
import { chmod, mkdtemp, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { fileToG711 } from './transcode.ts';

test('decodes to S16LE before applying the TypeScript PCMA encoder', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'onvif-ffmpeg-'));
  const fakeFfmpeg = path.join(directory, 'ffmpeg');
  const pcm = Buffer.alloc(18);
  [-32768, -30000, -1000, -1, 0, 1, 1000, 30000, 32767].forEach((sample, index) => {
    pcm.writeInt16LE(sample, index * 2);
  });
  await writeFile(
    fakeFfmpeg,
    `#!${process.execPath}\nprocess.stdout.write(Buffer.from('${pcm.toString('hex')}', 'hex'));\n`,
  );
  await chmod(fakeFfmpeg, 0o755);

  const previousPath = process.env.PATH;
  process.env.PATH = `${directory}${path.delimiter}${previousPath ?? ''}`;
  try {
    const encoded = await fileToG711('source.wav', 'PCMA', 0.05);
    assert.deepEqual([...encoded], [108, 98, 86, 85, 213, 213, 214, 226, 236]);
  } finally {
    process.env.PATH = previousPath;
    await rm(directory, { recursive: true, force: true });
  }
});
