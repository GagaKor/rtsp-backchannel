/**
 * Reusable ONVIF backchannel audio session: connect → DESCRIBE(backchannel)
 * → SETUP all media tracks → PLAY, then stream paced G.711 RTP to the
 * camera speaker.
 * Shared by the tone test (m3) and the file player (cli / M4).
 */
import { OnvifDevice } from './onvif/deviceClient.ts';
import { RtspClient } from './rtsp/backchannelClient.ts';
import {
  parseSdp,
  findBackchannelAudio,
  pickSendCodec,
  type CodecPreference,
  type SendCodec,
} from './rtsp/sdp.ts';
import { RtpPacketizer, interleave } from './rtp/sender.ts';
import type { G711Variant } from './audio/g711.ts';
import type { EncodedAudio, EncodedAudioFrame } from './audio/transcode.ts';

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

export interface BackchannelSession {
  /** Complete SDP codec selected for this RTP sender. */
  codec: SendCodec;
  /** Backward-compatible G.711 variant; undefined for G.726 and AAC. */
  variant?: G711Variant;
  payloadType: number;
  clockRate: number;
  rtpChannel: number;
  /** Run a potentially slow operation while maintaining the active RTSP session. */
  withKeepAlive<T>(operation: () => Promise<T>): Promise<T>;
  /** Stream encoded RTP payloads. Raw buffers remain supported for G.711. */
  send(audio: Buffer | EncodedAudio): Promise<number>;
  close(): Promise<void>;
}

export interface BackchannelOptions {
  codec?: CodecPreference;
}

async function maintainRtspSession<T>(
  operation: () => Promise<T>,
  millisecondsUntilKeepAlive: () => number,
  keepAlive: () => Promise<void>,
): Promise<T> {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let inFlight: Promise<void> | undefined;
  let keepAliveFailed = false;
  let keepAliveError: unknown;

  const schedule = (): void => {
    const delay = Math.max(0, millisecondsUntilKeepAlive());
    timer = setTimeout(() => {
      timer = undefined;
      if (millisecondsUntilKeepAlive() > 0) {
        schedule();
        return;
      }
      inFlight = (async () => {
        try {
          await keepAlive();
        } catch (error) {
          keepAliveFailed = true;
          keepAliveError = error;
        } finally {
          inFlight = undefined;
          if (!stopped && !keepAliveFailed) {
            schedule();
          }
        }
      })();
    }, delay);
  };

  schedule();
  let operationFailed = false;
  let operationError: unknown;
  let result: T | undefined;
  try {
    result = await operation();
  } catch (error) {
    operationFailed = true;
    operationError = error;
  } finally {
    stopped = true;
    if (timer !== undefined) clearTimeout(timer);
    if (inFlight) await inFlight;
  }

  if (operationFailed && keepAliveFailed) {
    throw new AggregateError(
      [operationError, keepAliveError],
      'operation and RTSP keepalive both failed',
      { cause: operationError },
    );
  }
  if (operationFailed) throw operationError;
  if (keepAliveFailed) throw keepAliveError;
  return result as T;
}

export async function closeRtspSession(
  rtsp: Pick<RtspClient, 'teardown' | 'close'>,
  streamUri: string,
): Promise<void> {
  try {
    await rtsp.teardown(streamUri);
  } finally {
    rtsp.close();
  }
}

export interface ParsedRtspTarget {
  /** Socket host without URL brackets around an IPv6 literal. */
  host: string;
  port: number;
  /** Credential-free URI used in RTSP request lines. */
  uri: string;
  user: string;
  pass: string;
}

interface RtspAuthority {
  authorityStart: number;
  authorityEnd: number;
  endpoint: string;
  userInfo: string;
}

function rtspAuthority(target: string): RtspAuthority | undefined {
  const scheme = /^rtsp:\/\//i.exec(target);
  if (!scheme) return undefined;
  const authorityStart = scheme[0].length;
  const suffixOffset = target.slice(authorityStart).search(/[/?#]/);
  const authorityEnd = suffixOffset < 0 ? target.length : authorityStart + suffixOffset;
  const authority = target.slice(authorityStart, authorityEnd);
  const userInfoEnd = authority.lastIndexOf('@');
  return {
    authorityStart,
    authorityEnd,
    endpoint: userInfoEnd < 0 ? authority : authority.slice(userInfoEnd + 1),
    userInfo: userInfoEnd < 0 ? '' : authority.slice(0, userInfoEnd),
  };
}

/** Return a credential-free RTSP target suitable for user-facing output. */
export function displayRtspTarget(target: string): string {
  const authority = rtspAuthority(target);
  if (!authority) return target;
  const sanitized =
    `${target.slice(0, authority.authorityStart)}${authority.endpoint}` +
    target.slice(authority.authorityEnd);
  try {
    const url = new URL(sanitized);
    if (url.protocol.toLowerCase() !== 'rtsp:' || !url.hostname) {
      return 'rtsp://<invalid>';
    }
    url.username = '';
    url.password = '';
    url.hash = '';
    return url.toString();
  } catch {
    return 'rtsp://<invalid>';
  }
}

/** Remove credential-bearing RTSP targets from an arbitrary display message. */
export function redactRtspCredentials(text: string): string {
  return text.replace(/rtsp:\/\/\S+/gi, (target) => displayRtspTarget(target));
}

function decodeUserInfo(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    throw new Error('bad percent-encoding in RTSP userinfo');
  }
}

/** Parse and sanitize an RTSP URL using the final @ in its authority. */
export function parseRtspTarget(
  target: string,
  user = '',
  pass = '',
): ParsedRtspTarget {
  const authority = rtspAuthority(target);
  if (!authority) throw new Error('bad RTSP uri');
  const separator = authority.userInfo.indexOf(':');
  const embeddedUser = decodeUserInfo(
    separator < 0 ? authority.userInfo : authority.userInfo.slice(0, separator),
  );
  const embeddedPass = decodeUserInfo(
    separator < 0 ? '' : authority.userInfo.slice(separator + 1),
  );
  const sanitized =
    `${target.slice(0, authority.authorityStart)}${authority.endpoint}` +
    target.slice(authority.authorityEnd);

  let url: URL;
  try {
    url = new URL(sanitized);
  } catch {
    throw new Error('bad RTSP uri');
  }
  if (url.protocol.toLowerCase() !== 'rtsp:') throw new Error('bad RTSP uri');
  url.username = '';
  url.password = '';
  url.hash = '';
  const hostname = url.hostname;
  const host = hostname.startsWith('[') && hostname.endsWith(']')
    ? hostname.slice(1, -1)
    : hostname;
  if (!host) throw new Error('bad RTSP uri');
  const port = Number(url.port || 554);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new RangeError('RTSP port must be between 1 and 65535');
  }

  return {
    host,
    port,
    uri: url.toString(),
    user: user || embeddedUser,
    pass: pass || embeddedPass,
  };
}

export function resolveTrackUri(
  baseUri: string,
  contentBase: string | undefined,
  control: string,
): string {
  if (/^rtsp:\/\//i.test(control)) return parseRtspTarget(control).uri;
  const sanitizedBase = parseRtspTarget(contentBase ?? baseUri).uri;
  try {
    let resolutionBase = sanitizedBase;
    const isRelativePath = !/^(?:[a-z][a-z\d+.-]*:|[/?#])/i.test(control);
    if (contentBase === undefined && isRelativePath) {
      const appendBase = new URL(sanitizedBase);
      if (!appendBase.pathname.endsWith('/')) appendBase.pathname += '/';
      resolutionBase = appendBase.toString();
    }
    return parseRtspTarget(new URL(control, resolutionBase).toString()).uri;
  } catch {
    throw new Error('bad RTSP control URI');
  }
}

export async function openBackchannel(
  host: string,
  user = '',
  pass = '',
  options: BackchannelOptions = {},
): Promise<BackchannelSession> {
  let endpoint: ParsedRtspTarget;
  if (/^rtsp:\/\//i.test(host)) {
    endpoint = parseRtspTarget(host, user, pass);
  } else {
    const dev = new OnvifDevice(host, user, pass);
    await dev.connect();
    const profiles = await dev.getProfiles();
    if (profiles.length === 0) throw new Error('no media profiles');
    endpoint = parseRtspTarget(await dev.getStreamUri(profiles[0].token), user, pass);
  }

  const { uri: streamUri } = endpoint;
  const rtsp = new RtspClient(endpoint.host, endpoint.port, endpoint.user, endpoint.pass);
  await rtsp.connect();

  try {
    const optionsResponse = await rtsp.options(streamUri);
    if (optionsResponse.status !== 200) {
      throw new Error(`OPTIONS ${optionsResponse.statusLine}`);
    }
    const desc = await rtsp.describe(streamUri, { backchannel: true });
    if (desc.status !== 200) throw new Error(`backchannel DESCRIBE ${desc.statusLine}`);
    const sdp = parseSdp(desc.body);
    const track = findBackchannelAudio(sdp);
    if (!track?.control) throw new Error('no sendonly backchannel audio track');
    const preference = options.codec ?? 'auto';
    const codec = pickSendCodec(track, preference);
    if (!codec) {
      if (preference === 'auto') {
        throw new Error('no supported backchannel audio codec offered');
      }
      throw new Error(`requested backchannel codec ${preference} was not offered`);
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

    const pkt = new RtpPacketizer({
      payloadType: codec.payloadType,
      clockRate: codec.clockRate,
    });
    const packetOptions = { marker: codec.name === 'aac' ? true : undefined };
    const variant = codec.name === 'pcma'
      ? 'PCMA'
      : codec.name === 'pcmu'
        ? 'PCMU'
        : undefined;
    const keepAliveIntervalMs = Math.max(
      PACKET_MS,
      (rtsp.sessionTimeoutSeconds * 1000) / 2,
    );
    let nextKeepAliveDeadline = performance.now() + keepAliveIntervalMs;
    let keepAliveInFlight: Promise<void> | undefined;
    const performKeepAlive = async (): Promise<void> => {
      const startedAt = performance.now();
      const response = await rtsp.keepAlive(streamUri);
      if (response.status !== 200) {
        throw new Error(`RTSP keepalive ${response.statusLine}`);
      }
      nextKeepAliveDeadline = startedAt + keepAliveIntervalMs;
    };
    const keepAlive = (): Promise<void> => {
      if (keepAliveInFlight) return keepAliveInFlight;
      const pending = performKeepAlive();
      keepAliveInFlight = pending;
      const clearPending = () => {
        if (keepAliveInFlight === pending) keepAliveInFlight = undefined;
      };
      void pending.then(clearPending, clearPending);
      return pending;
    };
    const millisecondsUntilKeepAlive = () =>
      nextKeepAliveDeadline - performance.now();
    let maintaining = false;
    return {
      codec,
      ...(variant ? { variant } : {}),
      payloadType: codec.payloadType,
      clockRate: codec.clockRate,
      rtpChannel,
      async withKeepAlive<T>(operation: () => Promise<T>): Promise<T> {
        if (maintaining) throw new Error('RTSP session maintenance is already active');
        maintaining = true;
        try {
          return await maintainRtspSession(
            operation,
            millisecondsUntilKeepAlive,
            keepAlive,
          );
        } finally {
          maintaining = false;
        }
      },
      async send(audio: Buffer | EncodedAudio): Promise<number> {
        const beforePacket = async () => {
          if (millisecondsUntilKeepAlive() > 0) return;
          await keepAlive();
        };
        const sendPacket = async (payload: Buffer, samples: number) => {
          await rtsp.sendInterleaved(
            interleave(rtpChannel, pkt.build(payload, samples, packetOptions)),
          );
        };

        if (Buffer.isBuffer(audio)) {
          if (!variant) {
            throw new Error(
              `raw Buffer send is not supported for ${codec.name}; provide EncodedAudio frames`,
            );
          }
          return sendPacedG711(
            audio,
            (payload) => sendPacket(payload, payload.length),
            systemClock,
            beforePacket,
          );
        }
        if (audio.codec !== codec.name || audio.clockRate !== codec.clockRate) {
          throw new Error(
            `encoded audio ${audio.codec}/${audio.clockRate} does not match ` +
              `${codec.name}/${codec.clockRate}`,
          );
        }
        return sendPacedFrames(
          audio.frames,
          codec.clockRate,
          sendPacket,
          systemClock,
          beforePacket,
        );
      },
      async close(): Promise<void> {
        await closeRtspSession(rtsp, streamUri);
      },
    };
  } catch (err) {
    rtsp.close();
    throw err;
  }
}
