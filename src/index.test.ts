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
  const lockfile = JSON.parse(readFileSync('package-lock.json', 'utf8'));

  assert.equal(manifest.name, 'rtsp-backchannel');
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
    'README.ko.md',
    'LICENSE',
    'LICENSE-MIT',
    'LICENSE-APACHE',
    'THIRD_PARTY_NOTICES.md',
  ]);
  assert.equal(manifest.license, 'MIT OR Apache-2.0');
  assert.equal(lockfile.packages[''].license, manifest.license);
  assert.equal(manifest.main, './dist/index.js');
  assert.equal(manifest.types, './dist/index.d.ts');
  assert.equal(manifest.exports['.'].import, './dist/index.js');
  assert.equal(manifest.exports['.'].types, './dist/index.d.ts');
  assert.equal(manifest.bin['rtsp-backchannel'], 'dist/bin.js');
  assert.equal(
    manifest.repository.url,
    'git+https://github.com/GagaKor/rtsp-backchannel.git',
  );
  assert.equal(
    manifest.homepage,
    'https://github.com/GagaKor/rtsp-backchannel#readme',
  );
  assert.equal(
    manifest.bugs.url,
    'https://github.com/GagaKor/rtsp-backchannel/issues',
  );
  assert.equal(manifest.scripts.build, 'tsc -p tsconfig.build.json');
  assert.equal(manifest.dependencies?.[manifest.name], undefined);
});

test('ships separate English and Korean TypeScript documentation', () => {
  const english = readFileSync('README.md', 'utf8');
  const korean = readFileSync('README.ko.md', 'utf8');

  assert.match(english, /TypeScript/);
  assert.match(english, /README\.ko\.md/);
  assert.doesNotMatch(english, /```(?:python|rust)/);
  assert.match(korean, /TypeScript/);
  assert.match(korean, /README\.md/);
  assert.doesNotMatch(korean, /```(?:python|rust)/);
});
