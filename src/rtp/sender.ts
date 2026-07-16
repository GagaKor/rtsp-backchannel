/** RTP packetization for G.711 audio over the RTSP backchannel. */
import crypto from 'node:crypto';

export interface RtpPacketizerOptions {
  payloadType: number;
  clockRate: number; // 8000 for G.711
  ssrc?: number;
  seqStart?: number;
  timestampStart?: number;
}

export class RtpPacketizer {
  private seq: number;
  private timestamp: number;
  private readonly ssrc: number;
  private firstPacket = true;

  constructor(private readonly opts: RtpPacketizerOptions) {
    this.ssrc = opts.ssrc ?? crypto.randomBytes(4).readUInt32BE(0);
    this.seq = opts.seqStart ?? crypto.randomBytes(2).readUInt16BE(0);
    this.timestamp = opts.timestampStart ?? crypto.randomBytes(4).readUInt32BE(0);
  }

  /**
   * Build one RTP packet. `samples` is the number of audio samples this payload
   * represents (e.g. 160 for a 20 ms G.711 frame at 8 kHz) and advances the
   * RTP timestamp.
   */
  build(payload: Buffer, samples: number): Buffer {
    const header = Buffer.alloc(12);
    header[0] = 0x80; // version 2, no padding/extension/CSRC
    header[1] = (this.firstPacket ? 0x80 : 0x00) | (this.opts.payloadType & 0x7f); // marker on first
    header.writeUInt16BE(this.seq & 0xffff, 2);
    header.writeUInt32BE(this.timestamp >>> 0, 4);
    header.writeUInt32BE(this.ssrc >>> 0, 8);

    this.seq = (this.seq + 1) & 0xffff;
    this.timestamp = (this.timestamp + samples) >>> 0;
    this.firstPacket = false;
    return Buffer.concat([header, payload]);
  }
}

/** Frame an RTP packet as an RTSP interleaved binary message ($ + channel + len). */
export function interleave(channel: number, rtp: Buffer): Buffer {
  const header = Buffer.alloc(4);
  header[0] = 0x24; // '$'
  header[1] = channel & 0xff;
  header.writeUInt16BE(rtp.length, 2);
  return Buffer.concat([header, rtp]);
}
