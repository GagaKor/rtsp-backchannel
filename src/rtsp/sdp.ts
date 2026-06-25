/** Minimal SDP parser — enough to locate RTP media tracks and their direction. */

export interface RtpMap {
  payloadType: number;
  encoding: string;
  clockRate: number;
  channels?: number;
}

export interface MediaDescription {
  media: string; // "audio" | "video" | "application" ...
  port: number;
  proto: string; // e.g. "RTP/AVP"
  formats: string[]; // payload type strings from the m= line
  direction?: 'sendonly' | 'recvonly' | 'sendrecv' | 'inactive';
  control?: string; // a=control value (track URL or "*")
  rtpmaps: Record<number, RtpMap>;
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
 * Choose which codec to SEND on the backchannel, preferring the simplest
 * codec matching the camera's configured G.711A/A-law mode, then falling
 * back to G.711U/µ-law. Returns the rtpmap actually offered.
 */
export function pickSendCodec(track: MediaDescription): RtpMap | undefined {
  const offered = track.formats.map(Number);
  const prefer: Array<{ encoding: string; clockRate: number }> = [
    { encoding: 'PCMA', clockRate: 8000 },
    { encoding: 'PCMU', clockRate: 8000 },
  ];
  for (const p of prefer) {
    for (const pt of offered) {
      const rm = track.rtpmaps[pt] ?? STATIC[pt];
      if (rm && rm.encoding.toUpperCase() === p.encoding && rm.clockRate === p.clockRate) {
        return rm;
      }
    }
  }
  const first = offered[0];
  return track.rtpmaps[first] ?? STATIC[first];
}
