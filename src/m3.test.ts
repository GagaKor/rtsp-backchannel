import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { test } from 'node:test';
import * as m3 from './m3.ts';

test('keeps RTSP credentials out of m3 process output', () => {
  const result = spawnSync(
    process.execPath,
    [
      '--experimental-transform-types',
      'src/m3.ts',
      '--host',
      'rtsp://tone-user:p%40ss@127.0.0.1:0/live',
      '--user',
      'explicit-user',
      '--pass',
      'explicit-pass',
    ],
    { encoding: 'utf8' },
  );

  assert.notEqual(result.status, 0);
  const output = result.stdout + result.stderr;
  assert.match(output, /rtsp:\/\/127\.0\.0\.1:0\/live/);
  assert.doesNotMatch(output, /tone-user|p%40ss/);
});

test('can import m3 without running the command', () => {
  const result = spawnSync(
    process.execPath,
    ['--experimental-transform-types', '--input-type=module', '-e', "await import('./src/m3.ts')"],
    { encoding: 'utf8' },
  );

  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, '');
  assert.doesNotMatch(result.stderr, /M3 error|missing --host/);
});

test('closes the m3 session when tone encoding or sending fails', async () => {
  type RunTone = (
    options: { host: string; user?: string; pass?: string },
    dependencies: {
      openBackchannel(): Promise<{
        codec: { name: 'pcma'; payloadType: 8; encoding: 'PCMA'; clockRate: 8000 };
        variant: 'PCMA';
        payloadType: number;
        clockRate: number;
        rtpChannel: number;
        send(audio: Buffer): Promise<number>;
        close(): Promise<void>;
      }>;
      generateTonePcm(): Int16Array;
      pcm16ToG711(): Buffer;
      log(message: string): void;
    },
  ) => Promise<number>;
  const runTone = (m3 as unknown as { runTone?: RunTone }).runTone;
  assert.ok(runTone);

  for (const failure of ['encode', 'send'] as const) {
    let closed = 0;
    await assert.rejects(
      runTone(
        { host: 'camera.test' },
        {
          openBackchannel: async () => ({
            codec: { name: 'pcma', payloadType: 8, encoding: 'PCMA', clockRate: 8000 },
            variant: 'PCMA',
            payloadType: 8,
            clockRate: 8000,
            rtpChannel: 0,
            send: async () => {
              if (failure === 'send') throw new Error('send failed');
              return 1;
            },
            close: async () => {
              closed++;
            },
          }),
          generateTonePcm: () => new Int16Array([0]),
          pcm16ToG711: () => {
            if (failure === 'encode') throw new Error('encode failed');
            return Buffer.from([0xd5]);
          },
          log: () => {},
        },
      ),
      new RegExp(`${failure} failed`),
    );
    assert.equal(closed, 1);
  }
});
