/**
 * Frame pacing for outbound audio, independent of any transport.
 *
 * These primitives live here rather than in `backchannel.ts` because both
 * transports need them and one of them — the VIGI talk session — computes a
 * top-level constant from `SAMPLE_RATE`. While they lived in `backchannel.ts`,
 * that made `backchannel.ts` -> `vigi/talk.ts` -> `backchannel.ts` a load-time
 * cycle: whenever `backchannel.ts` evaluated first, `talk.ts` read
 * `SAMPLE_RATE` in its temporal dead zone and threw
 * `Cannot access 'SAMPLE_RATE' before initialization`. Nothing here imports a
 * transport, so there is no cycle to work around.
 */
import type { EncodedAudioFrame } from './transcode.ts';

export const SAMPLE_RATE = 8000;
export const PACKET_MS = 40;
const SAMPLES_PER_PACKET = (SAMPLE_RATE * PACKET_MS) / 1000; // 320

const sleep = (ms: number): Promise<void> =>
  new Promise<void>((resolve) => setTimeout(resolve, Math.max(0, ms)));

export interface PacingClock {
  now(): number;
  sleep(milliseconds: number): Promise<void>;
}

/** @internal Exported so transports can pass the real clock explicitly. */
export const systemClock: PacingClock = {
  now: () => performance.now(),
  sleep,
};

async function waitUntil(deadline: number, clock: PacingClock): Promise<number> {
  let now = clock.now();
  while (now < deadline) {
    await clock.sleep(deadline - now);
    now = clock.now();
  }
  return now;
}

/** Send timestamped frames without bursty catch-up after scheduler stalls. */
export async function sendPacedFrames(
  frames: Iterable<EncodedAudioFrame>,
  clockRate: number,
  sendPacket: (payload: Buffer, samples: number) => void | Promise<void>,
  clock: PacingClock = systemClock,
  beforePacket?: () => Promise<void>,
): Promise<number> {
  if (!Number.isFinite(clockRate) || clockRate <= 0) {
    throw new RangeError('RTP clock rate must be finite and greater than 0');
  }
  let sent = 0;
  let deadline = clock.now();
  for (const frame of frames) {
    if (!Number.isInteger(frame.samples) || frame.samples <= 0) {
      throw new RangeError('audio frame samples must be a positive integer');
    }
    const durationMs = (frame.samples * 1000) / clockRate;
    let actual = await waitUntil(deadline, clock);
    if (beforePacket) {
      await beforePacket();
      actual = clock.now();
    }
    if (sent > 0 && actual - deadline >= durationMs) deadline = actual;

    await sendPacket(frame.payload, frame.samples);
    sent++;
    deadline += durationMs;
  }
  if (sent > 0) await waitUntil(deadline, clock);
  return sent;
}

/** Send G.711 as 40 ms packets without bursty catch-up after scheduler stalls. */
export function sendPacedG711(
  g711: Buffer,
  sendPacket: (payload: Buffer) => void | Promise<void>,
  clock: PacingClock = systemClock,
  beforePacket?: () => Promise<void>,
): Promise<number> {
  function* frames(): Generator<EncodedAudioFrame> {
    for (let offset = 0; offset < g711.length; offset += SAMPLES_PER_PACKET) {
      const payload = g711.subarray(offset, offset + SAMPLES_PER_PACKET);
      yield { payload, samples: payload.length };
    }
  }
  return sendPacedFrames(
    frames(),
    SAMPLE_RATE,
    (payload) => sendPacket(payload),
    clock,
    beforePacket,
  );
}
