import assert from 'node:assert/strict';
import { test } from 'node:test';
import { RtpPacketizer } from './sender.ts';

test('uses sender-owned RTP identity and advances 40ms PCMA timestamps', () => {
  const packetizer = new RtpPacketizer({
    payloadType: 8,
    clockRate: 8000,
    ssrc: 0x11223344,
    seqStart: 0x5566,
    timestampStart: 0x778899aa,
  });

  const first = packetizer.build(Buffer.alloc(320, 0xd5), 320);
  const second = packetizer.build(Buffer.alloc(320, 0xd5), 320);

  assert.equal(first[1], 0x88);
  assert.equal(first.readUInt16BE(2), 0x5566);
  assert.equal(first.readUInt32BE(4), 0x778899aa);
  assert.equal(first.readUInt32BE(8), 0x11223344);
  assert.equal(second[1], 0x08);
  assert.equal(second.readUInt16BE(2), 0x5567);
  assert.equal(second.readUInt32BE(4), 0x77889aea);
  assert.equal(second.readUInt32BE(8), 0x11223344);
});
