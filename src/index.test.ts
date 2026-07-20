import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import * as library from './index.ts';

test('exports the supported npm library surface from one entry point', () => {
  assert.equal(typeof library.playFile, 'function');
  assert.equal(typeof library.openBackchannel, 'function');
  assert.equal(typeof library.fileToG711, 'function');
  assert.equal(typeof library.pcm16ToG711, 'function');
  assert.equal(typeof library.linearToALaw, 'function');
  assert.equal(typeof library.discoverDevices, 'function');
  assert.equal(typeof library.getStreamUris, 'function');
  assert.equal(library.SAMPLE_RATE, 8000);
  assert.equal(library.PACKET_MS, 40);
});

test('declares an installable npm package with ESM types and CLI exports', () => {
  const manifest = JSON.parse(readFileSync('package.json', 'utf8'));

  assert.equal(manifest.name, 'onvif-backchannel');
  assert.notEqual(manifest.private, true);
  assert.deepEqual(manifest.files, [
    'dist/index.*',
    'dist/bin.*',
    'dist/cli.*',
    'dist/backchannel.*',
    'dist/audio',
    'dist/onvif',
    'dist/rtp',
    'dist/rtsp',
    'README.md',
  ]);
  assert.equal(manifest.main, './dist/index.js');
  assert.equal(manifest.types, './dist/index.d.ts');
  assert.equal(manifest.exports['.'].import, './dist/index.js');
  assert.equal(manifest.exports['.'].types, './dist/index.d.ts');
  assert.equal(manifest.bin['onvif-backchannel'], './dist/bin.js');
  assert.equal(manifest.scripts.build, 'tsc -p tsconfig.build.json');
  assert.equal(manifest.dependencies?.[manifest.name], undefined);
});
