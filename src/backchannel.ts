/**
 * Reusable ONVIF backchannel audio session: connect → DESCRIBE(backchannel)
 * → SETUP → RECORD, then stream paced G.711 RTP to the camera speaker.
 * Shared by the tone test (m3) and the file player (cli / M4).
 */
import { OnvifDevice } from './onvif/deviceClient.ts';
import { RtspClient } from './rtsp/backchannelClient.ts';
import { parseSdp, findBackchannelAudio, pickSendCodec } from './rtsp/sdp.ts';
import { RtpPacketizer, interleave } from './rtp/sender.ts';
import type { G711Variant } from './audio/g711.ts';

export const SAMPLE_RATE = 8000;
const FRAME_MS = 20;
const SAMPLES_PER_FRAME = (SAMPLE_RATE * FRAME_MS) / 1000; // 160

const sleep = (ms: number) => new Promise((r) => setTimeout(r, Math.max(0, ms)));

export interface BackchannelSession {
  variant: G711Variant;
  payloadType: number;
  rtpChannel: number;
  /** Stream a G.711 buffer as paced 20 ms RTP frames. Returns frames sent. */
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
    const desc = await rtsp.describe(streamUri, { backchannel: true });
    if (desc.status !== 200) throw new Error(`backchannel DESCRIBE ${desc.statusLine}`);
    const track = findBackchannelAudio(parseSdp(desc.body));
    if (!track?.control) throw new Error('no sendonly backchannel audio track');
    const codec = pickSendCodec(track);
    if (!codec || (codec.encoding !== 'PCMU' && codec.encoding !== 'PCMA')) {
      throw new Error(`no G.711 codec offered (got ${codec?.encoding})`);
    }
    const tUri = resolveTrackUri(streamUri, desc.headers['content-base'], track.control);
    const { rtpChannel } = await rtsp.setup(tUri);
    const rec = await rtsp.record(streamUri);
    if (rec.status !== 200) throw new Error(`RECORD ${rec.statusLine}`);

    const pkt = new RtpPacketizer({ payloadType: codec.payloadType, clockRate: SAMPLE_RATE });
    return {
      variant: codec.encoding as G711Variant,
      payloadType: codec.payloadType,
      rtpChannel,
      async send(g711: Buffer): Promise<number> {
        let sent = 0;
        let next = performance.now();
        for (let off = 0; off < g711.length; off += SAMPLES_PER_FRAME) {
          const chunk = g711.subarray(off, off + SAMPLES_PER_FRAME);
          rtsp.sendInterleaved(interleave(rtpChannel, pkt.build(chunk, chunk.length)));
          sent++;
          next += FRAME_MS;
          await sleep(next - performance.now());
        }
        return sent;
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
