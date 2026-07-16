import assert from 'node:assert/strict';
import { test } from 'node:test';
import * as backchannel from './backchannel.ts';

interface TestClock {
  now(): number;
  sleep(milliseconds: number): Promise<void>;
}

type PacedSender = (
  audio: Buffer,
  sendPacket: (payload: Buffer) => void,
  clock: TestClock,
  beforePacket?: () => Promise<void>,
) => Promise<number>;

function pacedSender(): PacedSender {
  const candidate = (backchannel as unknown as { sendPacedG711?: PacedSender }).sendPacedG711;
  assert.ok(candidate);
  return candidate;
}

test('sends G.711 in fixed 40ms packets and waits through the final sample', async () => {
  let now = 0;
  const clock: TestClock = {
    now: () => now,
    sleep: async (milliseconds) => {
      now += milliseconds;
    },
  };
  const sizes: number[] = [];
  const sentAt: number[] = [];

  const count = await pacedSender()(
    Buffer.alloc(700),
    (payload) => {
      sizes.push(payload.length);
      sentAt.push(now);
    },
    clock,
  );

  assert.equal(count, 3);
  assert.deepEqual(sizes, [320, 320, 60]);
  assert.deepEqual(sentAt, [0, 40, 80]);
  assert.equal(now, 87.5);
});

test('rebases pacing after a full packet interval of lateness', async () => {
  let now = 0;
  let sleepCount = 0;
  const sleeps: number[] = [];
  const clock: TestClock = {
    now: () => now,
    sleep: async (milliseconds) => {
      sleeps.push(milliseconds);
      now += milliseconds;
      if (sleepCount++ === 0) now += 45;
    },
  };
  const sentAt: number[] = [];

  await pacedSender()(Buffer.alloc(960), () => sentAt.push(now), clock);

  assert.deepEqual(sentAt, [0, 85, 125]);
  assert.deepEqual(sleeps, [40, 40, 40]);
});

test('rebases when RTSP maintenance delays a packet', async () => {
  let now = 0;
  let maintenanceCalls = 0;
  const clock: TestClock = {
    now: () => now,
    sleep: async (milliseconds) => {
      now += milliseconds;
    },
  };
  const sentAt: number[] = [];

  await pacedSender()(
    Buffer.alloc(640),
    () => sentAt.push(now),
    clock,
    async () => {
      maintenanceCalls++;
      if (maintenanceCalls === 2) now += 45;
    },
  );

  assert.equal(maintenanceCalls, 2);
  assert.deepEqual(sentAt, [0, 85]);
  assert.equal(now, 125);
});
