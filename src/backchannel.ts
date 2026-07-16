/**
 * Reusable ONVIF backchannel audio session: connect → DESCRIBE(backchannel)
 * → SETUP all media tracks → PLAY, then stream paced G.711 RTP to the
 * camera speaker.
 * Shared by the tone test (m3) and the file player (cli / M4).
 */
import { OnvifDevice } from './onvif/deviceClient.ts';
import { RtspClient } from './rtsp/backchannelClient.ts';
import { parseSdp, findBackchannelAudio, pickSendCodec } from './rtsp/sdp.ts';
import { RtpPacketizer, interleave } from './rtp/sender.ts';
import type { G711Variant } from './audio/g711.ts';

export const SAMPLE_RATE = 8000;
export const PACKET_MS = 40;
const SAMPLES_PER_PACKET = (SAMPLE_RATE * PACKET_MS) / 1000; // 320

const sleep = (ms: number): Promise<void> =>
  new Promise<void>((resolve) => setTimeout(resolve, Math.max(0, ms)));

export interface PacingClock {
  now(): number;
  sleep(milliseconds: number): Promise<void>;
}

const systemClock: PacingClock = {
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

/** Send G.711 as 40 ms packets without bursty catch-up after scheduler stalls. */
export async function sendPacedG711(
  g711: Buffer,
  sendPacket: (payload: Buffer) => void,
  clock: PacingClock = systemClock,
  beforePacket?: () => Promise<void>,
): Promise<number> {
  let sent = 0;
  let deadline = clock.now();
  for (let offset = 0; offset < g711.length; offset += SAMPLES_PER_PACKET) {
    const chunk = g711.subarray(offset, offset + SAMPLES_PER_PACKET);
    const durationMs = (chunk.length * 1000) / SAMPLE_RATE;
    let actual = await waitUntil(deadline, clock);
    if (beforePacket) {
      await beforePacket();
      actual = clock.now();
    }
    if (sent > 0 && actual - deadline >= durationMs) deadline = actual;

    sendPacket(chunk);
    sent++;
    deadline += durationMs;
  }
  if (sent > 0) await waitUntil(deadline, clock);
  return sent;
}

export interface BackchannelSession {
  variant: G711Variant;
  payloadType: number;
  rtpChannel: number;
  /** Stream a G.711 buffer as paced 40 ms RTP packets. Returns packets sent. */
  send(g711: Buffer): Promise<number>;
  close(): Promise<void>;
}

function rtspHostPort(uri: string): { host: string; port: number } {
  const m = /^rtsp:\/\/(?:[^@/]+@)?([^:/]+)(?::(\d+))?/.exec(uri);
  if (!m) throw new Error(`bad RTSP uri: ${uri}`);
  return { host: m[1], port: Number(m[2] ?? 554) };
}

function resolveTrackUri(baseUri: string, contentBase: string | undefined, control: string): string {
  if (control.startsWith('rtsp://')) return control;
  return `${(contentBase ?? baseUri).replace(/\/$/, '')}/${control}`;
}

export async function openBackchannel(
  host: string,
  user: string,
  pass: string,
): Promise<BackchannelSession> {
  const dev = new OnvifDevice(host, user, pass);
  await dev.connect();
  const profiles = await dev.getProfiles();
  if (profiles.length === 0) throw new Error('no media profiles');
  const streamUri = await dev.getStreamUri(profiles[0].token);

  const { host: rHost, port } = rtspHostPort(streamUri);
  const rtsp = new RtspClient(rHost, port, user, pass);
  await rtsp.connect();

  try {
    const options = await rtsp.options(streamUri);
    if (options.status !== 200) throw new Error(`OPTIONS ${options.statusLine}`);
    const desc = await rtsp.describe(streamUri, { backchannel: true });
    if (desc.status !== 200) throw new Error(`backchannel DESCRIBE ${desc.statusLine}`);
    const sdp = parseSdp(desc.body);
    const track = findBackchannelAudio(sdp);
    if (!track?.control) throw new Error('no sendonly backchannel audio track');
    const codec = pickSendCodec(track);
    if (!codec || (codec.encoding !== 'PCMU' && codec.encoding !== 'PCMA')) {
      throw new Error(`no G.711 codec offered (got ${codec?.encoding})`);
    }
    // ONVIF Streaming 5.3.2 starts a bidirectional session with PLAY. Set up
    // the normal receive tracks first, just as rtspsrc does, then add the
    // sendonly audio track to the same RTSP session.
    let requestedChannel = 0;
    for (const media of sdp.media) {
      if (media === track || media.direction !== 'recvonly' || !media.control) continue;
      const mediaUri = resolveTrackUri(streamUri, desc.headers['content-base'], media.control);
      await rtsp.setup(mediaUri, { rtpChannel: requestedChannel });
      requestedChannel += 2;
    }

    const tUri = resolveTrackUri(streamUri, desc.headers['content-base'], track.control);
    const { rtpChannel } = await rtsp.setup(tUri, {
      rtpChannel: requestedChannel,
      backchannel: true,
    });
    const play = await rtsp.play(streamUri);
    if (play.status !== 200) throw new Error(`PLAY ${play.statusLine}`);

    const pkt = new RtpPacketizer({ payloadType: codec.payloadType, clockRate: SAMPLE_RATE });
    return {
      variant: codec.encoding as G711Variant,
      payloadType: codec.payloadType,
      rtpChannel,
      async send(g711: Buffer): Promise<number> {
        const keepaliveIntervalMs = Math.max(
          PACKET_MS,
          (rtsp.sessionTimeoutSeconds * 1000) / 2,
        );
        let nextKeepalive = performance.now() + keepaliveIntervalMs;
        return sendPacedG711(
          g711,
          (chunk) => {
            rtsp.sendInterleaved(interleave(rtpChannel, pkt.build(chunk, chunk.length)));
          },
          systemClock,
          async () => {
            if (performance.now() < nextKeepalive) return;
            const response = await rtsp.keepAlive(streamUri);
            if (response.status !== 200) {
              throw new Error(`RTSP keepalive ${response.statusLine}`);
            }
            nextKeepalive = performance.now() + keepaliveIntervalMs;
          },
        );
      },
      async close(): Promise<void> {
        await rtsp.teardown(streamUri);
        rtsp.close();
      },
    };
  } catch (err) {
    rtsp.close();
    throw err;
  }
}
