/**
 * VIGI OpenAPI talk stream.
 *
 * One MULTITRANS request opens an audio-send session on an RTSP-framed
 * connection; audio then flows on the same socket as interleaved RTP over TCP.
 * The socket is opened on the first send, not at construction: the caller
 * typically transcodes a file between opening a session and sending it, and no
 * keep-alive is defined for a talk session.
 */
import net from 'node:net';
import { SAMPLE_RATE, sendPacedFrames, type PacingClock } from '../backchannel.ts';
import { RtpPacketizer, interleave } from '../rtp/sender.ts';
import { vigiAuthority } from './control.ts';
import {
  digestAuthorization,
  generateCnonce,
  parseDigestChallenge,
  type DigestChallenge,
} from '../rtsp/digest.ts';

/** PCMA. The device supports G.711 only. */
export const VIGI_TALK_PAYLOAD_TYPE = 8;
/** The talk reply carries no interleaved_id; channel 0 is what works. */
export const VIGI_TALK_CHANNEL = 0;
export const VIGI_TALK_PACKET_MS = 20;
export const VIGI_TALK_SAMPLES_PER_PACKET = (SAMPLE_RATE * VIGI_TALK_PACKET_MS) / 1000;

const DEFAULT_TIMEOUT_MS = 8000;

export type VigiTalkMode = 'half_duplex' | 'aec';

/** @internal The slice of net.Socket this module uses, so tests can supply a double. */
export interface VigiTalkSocket {
  write(chunk: Buffer | string): boolean;
  // `unknown`, not a specific event-payload type: this one method covers
  // 'data' (Buffer), 'error' (Error) and 'close' (no argument) alike.
  on(event: string, handler: (arg?: unknown) => void): unknown;
  once(event: string, handler: (arg?: unknown) => void): unknown;
  off(event: string, handler: (arg?: unknown) => void): unknown;
  end(): void;
  destroy(): void;
  setTimeout(ms: number, handler?: () => void): unknown;
}

/** @internal Exported for tests; the socket transport is injected. */
export interface VigiTalkDependencies {
  connect(port: number, host: string): VigiTalkSocket;
}

export interface VigiTalkOptions {
  host: string;
  user?: string;
  pass: string;
  streamPort: number;
  mode?: VigiTalkMode;
  channel?: number;
  timeoutMs?: number;
  clock?: PacingClock;
}

export interface VigiTalkSession {
  readonly payloadType: number;
  readonly clockRate: number;
  readonly rtpChannel: number;
  /** Stream G.711 a-law bytes in real time. Resolves with packets sent. */
  send(g711: Buffer): Promise<number>;
  close(): Promise<void>;
}

export function buildMultitransRequest(input: {
  uri: string;
  cseq: number;
  json: string;
  authorization?: string;
}): string {
  const lines = [
    `MULTITRANS ${input.uri} RTSP/1.0`,
    `CSeq: ${input.cseq}`,
    'Content-Type: application/json',
    `Content-Length: ${Buffer.byteLength(input.json)}`,
  ];
  if (input.authorization) lines.push(`Authorization: ${input.authorization}`);
  return `${lines.join('\r\n')}\r\n\r\n${input.json}`;
}

interface RtspReply {
  statusLine: string;
  status: number;
  headers: string;
  body: string;
  /** Characters of `raw` this reply consumed; the caller must keep the rest. */
  consumed: number;
}

function parseReply(raw: string): RtspReply | undefined {
  const split = raw.indexOf('\r\n\r\n');
  if (split < 0) return undefined;
  const headers = raw.slice(0, split);
  const statusLine = headers.split('\r\n')[0];
  const lengthMatch = /\r\nContent-Length:\s*(\d+)/i.exec(headers);
  const length = lengthMatch ? Number(lengthMatch[1]) : 0;
  const bodyStart = split + 4;
  const body = raw.slice(bodyStart);
  if (Buffer.byteLength(body, 'binary') < length) return undefined;
  const status = Number(/^RTSP\/\d\.\d\s+(\d+)/.exec(statusLine)?.[1] ?? 0);
  return {
    statusLine,
    status,
    headers,
    body: body.slice(0, length),
    consumed: bodyStart + length,
  };
}

const defaultDependencies: VigiTalkDependencies = {
  connect: (port, host) => net.connect(port, host) as unknown as VigiTalkSocket,
};

/**
 * Write one interleaved RTP frame and report the outcome truthfully — awaits
 * drain, error, and close exactly as `RtspClient.sendInterleaved` does for
 * the ONVIF backchannel, so the two transports behind `BackchannelSession`
 * cannot disagree about whether a packet actually reached the camera. A
 * fire-and-forget `connection.write()` here would let a mid-stream
 * `ECONNRESET` go unnoticed and `send()` would resolve with a packet count
 * for audio the camera never received.
 */
function writeInterleaved(
  connection: VigiTalkSocket,
  frame: Buffer,
  timeoutMs: number,
): Promise<void> {
  return new Promise((resolve, reject) => {
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const cleanup = () => {
      if (timer !== undefined) clearTimeout(timer);
      connection.off('drain', onDrain);
      connection.off('error', onError);
      connection.off('close', onClose);
    };
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (error) reject(error);
      else resolve();
    };
    const onDrain = () => finish();
    const onError = (arg?: unknown) => {
      const cause = arg instanceof Error ? arg : new Error(String(arg));
      finish(new Error(`VIGI talk write failed: ${cause.message}`, { cause }));
    };
    const onClose = () => finish(new Error('VIGI talk connection closed during send'));

    connection.once('drain', onDrain);
    connection.once('error', onError);
    connection.once('close', onClose);
    try {
      if (connection.write(frame)) {
        finish();
        return;
      }
    } catch (error) {
      onError(error);
      return;
    }
    if (settled) return;
    timer = setTimeout(() => {
      finish(new Error(`VIGI talk write timeout after ${timeoutMs} ms`));
      connection.destroy();
    }, timeoutMs);
  });
}

export function createVigiTalkSession(options: VigiTalkOptions): VigiTalkSession {
  return createVigiTalkSessionWithDependencies(options, defaultDependencies);
}

/**
 * @internal Exported for tests; the socket transport is injected. Split out
 * of `createVigiTalkSession` (rather than a defaulted second parameter, as
 * before) so that `VigiTalkSocket` and `VigiTalkDependencies` — injection
 * seams with no reason to be part of the published API — can be marked
 * `@internal` and stripped from the built `.d.ts` without leaving a dangling
 * reference in `createVigiTalkSession`'s own public signature.
 */
export function createVigiTalkSessionWithDependencies(
  options: VigiTalkOptions,
  dependencies: VigiTalkDependencies,
): VigiTalkSession {
  const user = options.user ?? 'admin';
  const channel = options.channel ?? VIGI_TALK_CHANNEL;
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  // The same `host:port` string that reaches openVigiControl reaches here, and
  // the port in it belongs to some other service (ONVIF on 2020, say) -- the
  // talk stream is on `streamPort`. Left in, it made net.connect resolve a
  // hostname with a colon in it and advertised the wrong port in the RTSP URI
  // that the digest response is computed over.
  const authority = vigiAuthority(options.host);
  const uri = `rtsp://${authority}/multitrans`;
  const talkJson = JSON.stringify({
    type: 'request',
    seq: '1',
    params: { method: 'get', talk: { mode: options.mode ?? 'half_duplex' } },
  });

  let socket: VigiTalkSocket | undefined;
  let packetizer: RtpPacketizer | undefined;
  let closed = false;
  let opening: Promise<VigiTalkSocket> | undefined;
  // Lets close() cut a handshake short instead of leaking the connection: set
  // while a handshake is in flight, cleared as soon as it settles either way.
  let abortOpening: ((error: Error) => void) | undefined;

  function open(): Promise<VigiTalkSocket> {
    if (opening) return opening;
    opening = new Promise<VigiTalkSocket>((resolve, reject) => {
      const connection = dependencies.connect(options.streamPort, authority);
      // Track the connection as soon as it exists, not only once the
      // handshake succeeds, so close() can always reach it.
      socket = connection;
      let buffer = '';
      let cseq = 1;
      let challenge: DigestChallenge | undefined;
      let settled = false;

      const settle = (error?: Error): void => {
        if (settled) return;
        settled = true;
        abortOpening = undefined;
        clearTimeout(timer);
        if (error) {
          connection.destroy();
          socket = undefined;
          reject(error);
        } else {
          resolve(connection);
        }
      };
      abortOpening = settle;
      const timer = setTimeout(
        () => settle(new Error('VIGI talk MULTITRANS timeout')),
        timeoutMs,
      );

      const sendRequest = (authorization?: string): void => {
        connection.write(buildMultitransRequest({ uri, cseq, json: talkJson, authorization }));
      };

      connection.on('error', () => settle(new Error('VIGI talk connection failed')));
      connection.on('data', ((chunk: Buffer) => {
        // The handshake is over; anything arriving now — a keepalive, a
        // coalesced trailing byte, or downlink audio in aec mode — is not a
        // MULTITRANS reply. Ignore it instead of feeding a text parser with
        // binary audio, which would never match and would buffer unbounded.
        if (settled) return;
        buffer += chunk.toString('binary');
        const reply = parseReply(buffer);
        if (!reply) return;
        buffer = buffer.slice(reply.consumed);

        if (reply.status === 401 && !challenge) {
          const header = /\r\nWWW-Authenticate:\s*(Digest [^\r]+)/i.exec(reply.headers);
          if (!header) {
            settle(new Error('VIGI talk MULTITRANS failed: no Digest challenge'));
            return;
          }
          const parsed = parseDigestChallenge(header[1]);
          if (parsed === undefined || parsed === 'basic') {
            settle(new Error('VIGI talk MULTITRANS failed: no Digest challenge'));
            return;
          }
          challenge = parsed;
          cseq += 1;
          sendRequest(
            digestAuthorization({
              user,
              pass: options.pass,
              method: 'MULTITRANS',
              uri,
              challenge: parsed,
              nonceCount: 1,
              cnonce: generateCnonce(),
              style: 'vigi',
            }),
          );
          return;
        }

        if (reply.status !== 200) {
          settle(new Error(`VIGI talk MULTITRANS failed: ${reply.statusLine}`));
          return;
        }
        let code = 0;
        try {
          const parsed = JSON.parse(reply.body) as {
            params?: { error_code?: number };
          };
          code = parsed.params?.error_code ?? 0;
        } catch {
          settle(new Error('VIGI talk reply was not JSON'));
          return;
        }
        if (code !== 0) {
          settle(new Error(`VIGI talk refused: error_code ${code}`));
          return;
        }
        settle();
      }) as (arg?: unknown) => void);

      sendRequest();
    });
    return opening;
  }

  return {
    payloadType: VIGI_TALK_PAYLOAD_TYPE,
    clockRate: SAMPLE_RATE,
    rtpChannel: channel,

    async send(g711: Buffer): Promise<number> {
      if (closed) throw new Error('VIGI talk session is closed');
      const connection = await open();
      packetizer ??= new RtpPacketizer({
        payloadType: VIGI_TALK_PAYLOAD_TYPE,
        clockRate: SAMPLE_RATE,
      });
      const rtp = packetizer;
      function* frames() {
        for (
          let offset = 0;
          offset < g711.length;
          offset += VIGI_TALK_SAMPLES_PER_PACKET
        ) {
          const payload = g711.subarray(offset, offset + VIGI_TALK_SAMPLES_PER_PACKET);
          yield { payload, samples: payload.length };
        }
      }
      return sendPacedFrames(
        frames(),
        SAMPLE_RATE,
        (payload) => writeInterleaved(
          connection,
          interleave(channel, rtp.build(payload, payload.length)),
          timeoutMs,
        ),
        options.clock,
      );
    },

    async close(): Promise<void> {
      if (closed) return;
      closed = true;
      // If a handshake is still in flight, reject it (consistent with "send
      // after close is rejected") instead of letting it resolve to a
      // connection nothing will ever use again.
      abortOpening?.(new Error('VIGI talk session is closed'));
      // Destroy rather than half-close (end()): VIGI documents no keep-alive
      // for a talk session and no reply to a FIN, so end() would leave the
      // handle referenced — and a CLI run hanging after playback — until the
      // camera closes its side, which it may never do. Matches
      // RtspClient.close() on the ONVIF path.
      socket?.destroy();
      socket = undefined;
    },
  };
}
