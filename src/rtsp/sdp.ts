/** Minimal SDP parser — enough to locate RTP media tracks and their direction. */

export interface RtpMap {
  payloadType: number;
  encoding: string;
  clockRate: number;
  channels?: number;
}

export type AudioCodecName =
  | 'pcma'
  | 'pcmu'
  | 'g726-16'
  | 'g726-24'
  | 'g726-32'
  | 'g726-40'
  | 'aac';

export type CodecPreference = 'auto' | AudioCodecName;

export interface Fmtp {
  payloadType: number;
  parameters: Record<string, string>;
}

export interface SendCodec extends RtpMap {
  name: AudioCodecName;
  fmtp?: Fmtp;
}

export interface MediaDescription {
  media: string; // "audio" | "video" | "application" ...
  port: number;
  proto: string; // e.g. "RTP/AVP"
  formats: string[]; // payload type strings from the m= line
  direction?: 'sendonly' | 'recvonly' | 'sendrecv' | 'inactive';
  control?: string; // a=control value (track URL or "*")
  rtpmaps: Record<number, RtpMap>;
  fmtps: Record<number, Fmtp>;
}

export interface Sdp {
  media: MediaDescription[];
}

export function parseSdp(text: string): Sdp {
  const lines = text.split(/\r?\n/);
  const media: MediaDescription[] = [];
  let cur: MediaDescription | undefined;

  for (const line of lines) {
    if (line.startsWith('m=')) {
      // m=<media> <port> <proto> <fmt> ...
      const parts = line.slice(2).trim().split(/\s+/);
      cur = {
        media: parts[0],
        port: Number(parts[1]) || 0,
        proto: parts[2] ?? '',
        formats: parts.slice(3),
        rtpmaps: {},
        fmtps: {},
      };
      media.push(cur);
    } else if (line.startsWith('a=') && cur) {
      const attr = line.slice(2);
      if (attr === 'sendonly' || attr === 'recvonly' || attr === 'sendrecv' || attr === 'inactive') {
        cur.direction = attr;
      } else if (attr.startsWith('control:')) {
        cur.control = attr.slice('control:'.length).trim();
      } else if (attr.startsWith('rtpmap:')) {
        // rtpmap:<pt> <encoding>/<clock>[/<channels>]
        const m = /^rtpmap:(\d+)\s+([^/]+)\/(\d+)(?:\/(\d+))?/.exec(attr);
        if (m) {
          const pt = Number(m[1]);
          cur.rtpmaps[pt] = {
            payloadType: pt,
            encoding: m[2],
            clockRate: Number(m[3]),
            channels: m[4] ? Number(m[4]) : undefined,
          };
        }
      } else if (attr.startsWith('fmtp:')) {
        const match = /^fmtp:(\d+)\s+(.+)$/.exec(attr);
        if (match) {
          const payloadType = Number(match[1]);
          const parameters: Record<string, string> = {};
          for (const entry of match[2].split(';')) {
            const trimmed = entry.trim();
            if (!trimmed) continue;
            const equals = trimmed.indexOf('=');
            const key = (equals < 0 ? trimmed : trimmed.slice(0, equals)).trim().toLowerCase();
            const value = equals < 0 ? '' : trimmed.slice(equals + 1).trim();
            if (key) parameters[key] = value;
          }
          cur.fmtps[payloadType] = { payloadType, parameters };
        }
      }
    }
  }
  return { media };
}

/** Find the backchannel audio track the client may SEND to (a=sendonly audio). */
export function findBackchannelAudio(sdp: Sdp): MediaDescription | undefined {
  return sdp.media.find((m) => m.media === 'audio' && m.direction === 'sendonly');
}

/** Static RTP payload types we may use without an explicit rtpmap. */
const STATIC: Record<number, RtpMap> = {
  0: { payloadType: 0, encoding: 'PCMU', clockRate: 8000 },
  8: { payloadType: 8, encoding: 'PCMA', clockRate: 8000 },
};

/**
 * Choose which codec to send on the backchannel. Auto mode preserves the
 * historical PCMA then PCMU preference before considering G.726 and AAC.
 * Explicit preferences never fall back to a different codec.
 */
export function pickSendCodec(
  track: MediaDescription,
  preference: CodecPreference = 'auto',
): SendCodec | undefined {
  const offered = track.formats.map(Number);
  const supported: Array<{
    name: AudioCodecName;
    encoding: string;
    clockRate?: number;
  }> = [
    { name: 'pcma', encoding: 'PCMA', clockRate: 8000 },
    { name: 'pcmu', encoding: 'PCMU', clockRate: 8000 },
    { name: 'g726-32', encoding: 'G726-32', clockRate: 8000 },
    { name: 'g726-24', encoding: 'G726-24', clockRate: 8000 },
    { name: 'g726-16', encoding: 'G726-16', clockRate: 8000 },
    { name: 'g726-40', encoding: 'G726-40', clockRate: 8000 },
    { name: 'aac', encoding: 'MPEG4-GENERIC' },
  ];
  const candidates = preference === 'auto'
    ? supported
    : supported.filter((candidate) => candidate.name === preference);

  for (const candidate of candidates) {
    for (const pt of offered) {
      const rm = track.rtpmaps[pt] ?? STATIC[pt];
      if (
        rm &&
        rm.encoding.toUpperCase() === candidate.encoding &&
        (candidate.clockRate === undefined || rm.clockRate === candidate.clockRate) &&
        (candidate.name === 'aac' || (rm.channels ?? 1) === 1)
      ) {
        const fmtp = track.fmtps[pt];
        if (candidate.name === 'aac') validateAacHbrFmtp(fmtp, rm);
        return {
          name: candidate.name,
          ...rm,
          ...(fmtp ? { fmtp } : {}),
        };
      }
    }
  }

  const latm = offered
    .map((pt) => track.rtpmaps[pt] ?? STATIC[pt])
    .find((rtpmap) => rtpmap?.encoding.toUpperCase() === 'MP4A-LATM');
  if (latm && (preference === 'auto' || preference === 'aac')) {
    throw new Error(
      'MP4A-LATM is recognized but not supported; use RFC 3640 MPEG4-GENERIC AAC-hbr',
    );
  }
  return undefined;
}

const AAC_SAMPLE_RATES = [
  96_000, 88_200, 64_000, 48_000, 44_100, 32_000, 24_000,
  22_050, 16_000, 12_000, 11_025, 8_000, 7_350,
];

const AAC_CHANNEL_COUNTS: Record<number, number> = {
  1: 1,
  2: 2,
  3: 3,
  4: 4,
  5: 5,
  6: 6,
  7: 8,
};

function validateAacHbrFmtp(fmtp: Fmtp | undefined, rtpmap: RtpMap): void {
  const parameters = fmtp?.parameters ?? {};
  const config = parameters.config ?? '';
  if (
    parameters.streamtype !== '5' ||
    parameters.mode?.toLowerCase() !== 'aac-hbr' ||
    parameters.sizelength !== '13' ||
    parameters.indexlength !== '3' ||
    parameters.indexdeltalength !== '3' ||
    !/^(?:[0-9a-f]{2})+$/i.test(config)
  ) {
    throw new Error(
      'MPEG4-GENERIC requires streamtype=5, AAC-hbr fmtp with config, ' +
        'SizeLength=13, IndexLength=3, and IndexDeltaLength=3',
    );
  }
  if (
    parameters.constantduration !== undefined &&
    (!/^\d+$/.test(parameters.constantduration) || Number(parameters.constantduration) !== 1024)
  ) {
    throw new Error('AAC-hbr constantDuration must be 1024 when present');
  }

  const asc = Buffer.from(config, 'hex');
  if (asc.length < 2) {
    throw new Error('AAC AudioSpecificConfig must contain at least 2 bytes');
  }
  const audioObjectType = asc[0] >> 3;
  if (audioObjectType !== 2) {
    throw new Error(
      `AAC AudioSpecificConfig audioObjectType ${audioObjectType} is not supported; ` +
        'expected AAC-LC audioObjectType 2',
    );
  }
  const frequencyIndex = ((asc[0] & 0x07) << 1) | (asc[1] >> 7);
  if (frequencyIndex === 0x0f) {
    throw new Error('AAC AudioSpecificConfig explicit sampling frequency is not supported');
  }
  const sampleRate = AAC_SAMPLE_RATES[frequencyIndex];
  if (!sampleRate) {
    throw new Error(
      `AAC AudioSpecificConfig samplingFrequencyIndex ${frequencyIndex} is not supported`,
    );
  }
  if (sampleRate !== rtpmap.clockRate) {
    throw new Error(
      `AAC AudioSpecificConfig sample rate ${sampleRate} does not match ` +
        `rtpmap clock rate ${rtpmap.clockRate}`,
    );
  }
  const channelConfiguration = (asc[1] >> 3) & 0x0f;
  const channelCount = AAC_CHANNEL_COUNTS[channelConfiguration];
  if (!channelCount) {
    throw new Error(
      `AAC AudioSpecificConfig channelConfiguration ${channelConfiguration} is not supported`,
    );
  }
  const rtpmapChannels = rtpmap.channels ?? 1;
  if (channelCount !== rtpmapChannels) {
    throw new Error(
      `AAC AudioSpecificConfig channel count ${channelCount} does not match ` +
        `rtpmap channel count ${rtpmapChannels}`,
    );
  }
  const frameLengthFlag = (asc[1] >> 2) & 0x01;
  if (frameLengthFlag !== 0) {
    throw new Error('frameLengthFlag=1 selects 960-sample AAC-LC frames');
  }
  const dependsOnCoreCoder = (asc[1] >> 1) & 0x01;
  if (dependsOnCoreCoder !== 0) {
    throw new Error('dependsOnCoreCoder=1 is unsupported');
  }
  const extensionFlag = asc[1] & 0x01;
  if (extensionFlag !== 0) {
    throw new Error('extensionFlag=1 is unsupported');
  }
}
