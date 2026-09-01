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
import {
  openVigiControl,
  type VigiControlOptions,
  type VigiControlSession,
} from './vigi/control.ts';
import {
  createVigiTalkSession,
  type VigiTalkOptions,
  type VigiTalkSession,
} from './vigi/talk.ts';
import {
  PACKET_MS,
  SAMPLE_RATE,
  sendPacedFrames,
  sendPacedG711,
  systemClock,
  type PacingClock,
} from './audio/pacing.ts';

// Pacing lives in ./audio/pacing.ts so that vigi/talk.ts can take
// SAMPLE_RATE from there instead of from here. Importing it from this module
// made backchannel.ts -> vigi/talk.ts -> backchannel.ts a load-time cycle.
// Re-exported so this module's published surface is unchanged.
export {
  PACKET_MS,
  SAMPLE_RATE,
  sendPacedFrames,
  sendPacedG711,
  type PacingClock,
} from './audio/pacing.ts';

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

export type BackchannelTransport = 'auto' | 'onvif' | 'vigi';

/**
 * The camera answered a backchannel DESCRIBE but offered no sendonly audio
 * track. This is the one ONVIF outcome that `transport: 'auto'` treats as
 * "try the other transport"; every other failure propagates, so a network or
 * authentication fault is never reported as a missing vendor API.
 */
export class BackchannelUnavailableError extends Error {
  readonly kind = 'no-sendonly-track' as const;
  constructor() {
    super('no sendonly backchannel audio track');
    this.name = 'BackchannelUnavailableError';
  }
}

export interface BackchannelOptions {
  codec?: CodecPreference;
  transport?: BackchannelTransport;
  /** VIGI OpenAPI control port. Defaults to 20443. */
  vigiControlPort?: number;
}

/** @internal Exported for tests; the transport openers are injected. */
export async function selectBackchannelTransport(
  transport: BackchannelTransport,
  openers: {
    openOnvif(): Promise<BackchannelSession>;
    openVigi(): Promise<BackchannelSession>;
  },
): Promise<BackchannelSession> {
  if (transport === 'onvif') return openers.openOnvif();
  if (transport === 'vigi') return openers.openVigi();
  try {
    return await openers.openOnvif();
  } catch (error) {
    if (!(error instanceof BackchannelUnavailableError)) throw error;
    try {
      return await openers.openVigi();
    } catch (vigiError) {
      const detail = vigiError instanceof Error ? vigiError.message : String(vigiError);
      throw new Error(
        'no audio send path: ONVIF backchannel absent (no sendonly track); '
          + detail,
      );
    }
  }
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

export async function openOnvifBackchannel(
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
    if (!track?.control) throw new BackchannelUnavailableError();
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

/**
 * @internal Exported for tests: the VIGI control session and the talk
 * session factory are injected instead of the real network-backed
 * implementations, so `openVigiBackchannel`'s open-time codec/speaker
 * checks and its `send` framing can be driven without a real device.
 */
export interface VigiBackchannelDependencies {
  openVigiControl(options: VigiControlOptions): Promise<VigiControlSession>;
  createVigiTalkSession(options: VigiTalkOptions): VigiTalkSession;
}

export async function openVigiBackchannel(
  host: string,
  user = '',
  pass = '',
  options: BackchannelOptions = {},
): Promise<BackchannelSession> {
  return openVigiBackchannelWithDependencies(host, user, pass, options, {
    openVigiControl,
    createVigiTalkSession,
  });
}

/** @internal Exported for tests; see VigiBackchannelDependencies. */
export async function openVigiBackchannelWithDependencies(
  host: string,
  user = '',
  pass = '',
  options: BackchannelOptions = {},
  dependencies: VigiBackchannelDependencies,
): Promise<BackchannelSession> {
  const preference = options.codec ?? 'auto';
  if (preference !== 'auto' && preference !== 'pcma') {
    throw new Error(`VIGI talk supports G.711 a-law only, not ${preference}`);
  }
  const control = await dependencies.openVigiControl({
    host,
    user: user || 'admin',
    pass,
    ...(options.vigiControlPort === undefined
      ? {}
      : { port: options.vigiControlPort }),
  });
  const audio = await control.getAudioCapability();
  if (!audio.speaker) throw new Error('VIGI OpenAPI reports no speaker');
  const streamPort = await control.getStreamPort();
  const talk = dependencies.createVigiTalkSession({
    host, user: user || 'admin', pass, streamPort,
  });

  const codec: SendCodec = {
    name: 'pcma',
    payloadType: talk.payloadType,
    encoding: 'PCMA',
    clockRate: talk.clockRate,
  };
  return {
    codec,
    variant: 'PCMA',
    payloadType: talk.payloadType,
    clockRate: talk.clockRate,
    rtpChannel: talk.rtpChannel,
    // No keep-alive exists for a talk session, and none is needed: the stream
    // socket is not opened until the first send.
    withKeepAlive: (operation) => operation(),
    async send(audioData: Buffer | EncodedAudio): Promise<number> {
      if (Buffer.isBuffer(audioData)) return talk.send(audioData);
      if (audioData.codec !== codec.name || audioData.clockRate !== codec.clockRate) {
        throw new Error(
          `encoded audio ${audioData.codec}/${audioData.clockRate} does not match ` +
            `${codec.name}/${codec.clockRate}`,
        );
      }
      // EncodedAudio has no single `data` buffer, only per-packet frames.
      // Flattening frames and letting talk.send re-cut them at 160 bytes is
      // lossless ONLY because PCMA is one byte per sample: there is no frame
      // boundary to preserve. Do not reuse this concat-then-recut approach
      // for AAC or G.726, where a frame is a decode unit and splitting it at
      // an arbitrary byte offset would corrupt the audio.
      return talk.send(Buffer.concat(audioData.frames.map((frame) => frame.payload)));
    },
    close: () => talk.close(),
  };
}

export function openBackchannel(
  host: string,
  user = '',
  pass = '',
  options: BackchannelOptions = {},
): Promise<BackchannelSession> {
  return selectBackchannelTransport(options.transport ?? 'auto', {
    openOnvif: () => openOnvifBackchannel(host, user, pass, options),
    openVigi: () => openVigiBackchannel(host, user, pass, options),
  });
}
